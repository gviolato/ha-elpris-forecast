"""SQLite persistence: every forecast ever seen, the published prices, and the error model.

Synchronous on purpose (plain ``sqlite3``); Home Assistant calls it from an executor thread.
All timestamps are stored as UTC ISO-8601 strings (``YYYY-MM-DDTHH:MM:SSZ``) so they sort
lexicographically and are easy to query by hand::

    sqlite3 elpris_forecast.db "SELECT * FROM v_scored ORDER BY target_datetime DESC LIMIT 20"
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import sqlite3

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per (place, target hour, forecast issue). Never deleted: the full history is the
-- training set for the error model and can be re-scored with other parameters later.
CREATE TABLE IF NOT EXISTS forecasts (
    id                  INTEGER PRIMARY KEY,
    place               TEXT    NOT NULL,
    target_datetime     TEXT    NOT NULL,  -- start of the hour the price refers to (UTC)
    forecast_timestamp  TEXT    NOT NULL,  -- when the forecast was issued (UTC)
    horizon_hours       INTEGER NOT NULL,  -- floor((target - issued) / 1h)
    predicted_price     REAL    NOT NULL,  -- kr/kWh incl. VAT at `place`
    actual_price        REAL,              -- filled in when the firm price is published
    residual            REAL,              -- predicted_price - actual_price
    run_id              INTEGER,
    model_id            TEXT,
    kind                TEXT,
    source_all_in       REAL,              -- kr/kWh, representative grid area of the region
    source_spot_ex_vat  REAL,              -- kr/kWh
    source_p10          REAL,              -- kr/kWh, the publisher's own band (for comparison)
    source_p90          REAL,
    first_seen_at       TEXT    NOT NULL,
    actual_filled_at    TEXT,
    UNIQUE (place, target_datetime, forecast_timestamp)
);
CREATE INDEX IF NOT EXISTS ix_forecasts_target ON forecasts (place, target_datetime);
CREATE INDEX IF NOT EXISTS ix_forecasts_pending ON forecasts (place, target_datetime)
    WHERE actual_price IS NULL;

-- Published (firm) hourly prices at `place`.
CREATE TABLE IF NOT EXISTS actuals (
    place            TEXT NOT NULL,
    target_datetime  TEXT NOT NULL,
    price            REAL NOT NULL,  -- kr/kWh incl. VAT
    spot_ore         REAL,
    grid_ore         REAL,
    charges_ore      REAL,
    first_seen_at    TEXT NOT NULL,
    PRIMARY KEY (place, target_datetime)
);

-- Snapshot of the EWMA error statistics per horizon (rebuilt from `forecasts`).
CREATE TABLE IF NOT EXISTS error_model (
    place             TEXT    NOT NULL,
    horizon_hours     INTEGER NOT NULL,
    n                 INTEGER NOT NULL,  -- raw number of residuals seen
    weight            REAL    NOT NULL,  -- sum of exponential weights
    effective_n       REAL    NOT NULL,  -- (sum w)^2 / sum w^2
    mean              REAL    NOT NULL,  -- EWMA of residuals (bias), kr/kWh
    variance          REAL    NOT NULL,  -- EW variance of residuals, (kr/kWh)^2
    forgetting_factor REAL    NOT NULL,
    updated_at        TEXT    NOT NULL,
    PRIMARY KEY (place, horizon_hours)
);

CREATE VIEW IF NOT EXISTS v_scored AS
    SELECT place, target_datetime, forecast_timestamp, horizon_hours,
           predicted_price, actual_price, residual, run_id, source_p10, source_p90
    FROM forecasts WHERE residual IS NOT NULL;
"""


def to_iso(value: datetime) -> str:
    """Format an aware datetime as a UTC ISO string."""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_iso(value: str) -> datetime:
    """Parse a stored UTC ISO string."""
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class ForecastRow:
    """A forecast to store."""

    target: datetime
    issued: datetime
    predicted_price: float
    run_id: int | None = None
    model_id: str | None = None
    kind: str | None = None
    source_all_in: float | None = None
    source_spot_ex_vat: float | None = None
    source_p10: float | None = None
    source_p90: float | None = None

    @property
    def horizon_hours(self) -> int:
        return int((self.target - self.issued).total_seconds() // 3600)


@dataclass(frozen=True)
class ActualRow:
    """A published price to store."""

    target: datetime
    price: float
    spot_ore: float | None = None
    grid_ore: float | None = None
    charges_ore: float | None = None


@dataclass(frozen=True)
class Residual:
    """A scored forecast, as fed to the error model."""

    target: datetime
    horizon_hours: int
    residual: float


class Store:
    """Thin wrapper around the SQLite file."""

    def __init__(self, path: str) -> None:
        self.path = path
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            with conn:  # commit / rollback
                yield conn
        finally:
            conn.close()

    def add_forecasts(self, place: str, rows: Iterable[ForecastRow], now: datetime) -> int:
        """Insert forecasts not seen before. Returns the number of new rows."""
        seen = to_iso(now)
        data = [
            (
                place,
                to_iso(r.target),
                to_iso(r.issued),
                r.horizon_hours,
                r.predicted_price,
                r.run_id,
                r.model_id,
                r.kind,
                r.source_all_in,
                r.source_spot_ex_vat,
                r.source_p10,
                r.source_p90,
                seen,
            )
            for r in rows
            if r.target > r.issued
        ]
        with self._connect() as conn:
            before = conn.total_changes
            conn.executemany(
                """INSERT OR IGNORE INTO forecasts
                   (place, target_datetime, forecast_timestamp, horizon_hours, predicted_price,
                    run_id, model_id, kind, source_all_in, source_spot_ex_vat,
                    source_p10, source_p90, first_seen_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                data,
            )
            return conn.total_changes - before

    def add_actuals(self, place: str, rows: Iterable[ActualRow], now: datetime) -> int:
        """Store published prices and back-fill pending forecasts. Returns rows scored.

        A forecast is only scored when it was issued before we first saw the firm price, so a
        forecast that already "knew" the answer can never enter the error statistics.
        """
        seen = to_iso(now)
        rows = list(rows)
        with self._connect() as conn:
            conn.executemany(
                """INSERT INTO actuals
                   (place, target_datetime, price, spot_ore, grid_ore, charges_ore, first_seen_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT (place, target_datetime) DO UPDATE SET
                     price = excluded.price, spot_ore = excluded.spot_ore,
                     grid_ore = excluded.grid_ore, charges_ore = excluded.charges_ore""",
                [
                    (place, to_iso(r.target), r.price, r.spot_ore, r.grid_ore, r.charges_ore, seen)
                    for r in rows
                ],
            )
            before = conn.total_changes
            conn.execute(
                """UPDATE forecasts SET
                     actual_price = (SELECT a.price FROM actuals a
                                     WHERE a.place = forecasts.place
                                       AND a.target_datetime = forecasts.target_datetime),
                     residual = predicted_price - (SELECT a.price FROM actuals a
                                     WHERE a.place = forecasts.place
                                       AND a.target_datetime = forecasts.target_datetime),
                     actual_filled_at = ?
                   WHERE place = ? AND actual_price IS NULL
                     AND EXISTS (SELECT 1 FROM actuals a
                                 WHERE a.place = forecasts.place
                                   AND a.target_datetime = forecasts.target_datetime
                                   AND forecasts.forecast_timestamp < a.first_seen_at)""",
                (seen, place),
            )
            return conn.total_changes - before

    def residuals(self, place: str) -> list[Residual]:
        """All scored forecasts, oldest target first (the order the EWMA consumes them)."""
        with self._connect() as conn:
            cur = conn.execute(
                """SELECT target_datetime, horizon_hours, residual FROM forecasts
                   WHERE place = ? AND residual IS NOT NULL
                   ORDER BY target_datetime, forecast_timestamp""",
                (place,),
            )
            return [Residual(from_iso(t), int(h), float(e)) for t, h, e in cur]

    def save_error_model(self, place: str, rows: Iterable[tuple], now: datetime) -> None:
        """Replace the stored error-model snapshot for ``place``.

        ``rows``: (horizon, n, weight, effective_n, mean, variance, forgetting_factor).
        """
        with self._connect() as conn:
            conn.execute("DELETE FROM error_model WHERE place = ?", (place,))
            conn.executemany(
                """INSERT INTO error_model
                   (place, horizon_hours, n, weight, effective_n, mean, variance,
                    forgetting_factor, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                [(place, *r, to_iso(now)) for r in rows],
            )

    def counts(self, place: str) -> dict[str, int]:
        """Row counts, for diagnostics."""
        with self._connect() as conn:
            forecasts, scored = conn.execute(
                "SELECT COUNT(*), COUNT(residual) FROM forecasts WHERE place = ?", (place,)
            ).fetchone()
            (actuals,) = conn.execute(
                "SELECT COUNT(*) FROM actuals WHERE place = ?", (place,)
            ).fetchone()
        return {"forecasts": forecasts, "scored": scored, "actuals": actuals}
