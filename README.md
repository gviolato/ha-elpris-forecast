# Elpris Forecast (DK)

Home Assistant custom integration for the Danish all-in hourly electricity price
(spot + grid tariff + Energinet tariffs + electricity tax, incl. VAT) for **your** grid area,
merged with a **7-day forecast** and **learned p10/p50/p90 uncertainty bands**.

* Firm prices (today + tomorrow, ~36 h) from `https://elpriser.dk/data/timepris.json?sted=<place>`
* 7-day forecast from `https://elpriser.dk/data/prognose.json`
* Every forecast ever seen is stored in SQLite and scored against the published price once it
  is known; the residuals feed an exponentially-forgetting (EWMA) error model per horizon.
* Two uncertainty bands per forecast hour: the publisher's own p10/p90 (asymmetric, shown by
  default) and the learned Gaussian EWMA band. The option *Band shown on forecast hours* picks
  one; the other is the fallback where the preferred one is missing (`band_source` tells which).

Data: [Elpriser.dk](https://elpriser.dk/data/) (CC BY 4.0), based on Energinet / Energi Data Service.
The source asks for at most one request per five minutes; this integration polls firm prices
every 15 minutes and the forecast every 30 minutes.

## Installation

HACS → ⋮ → *Custom repositories* → add this repository (type *Integration*) → download →
restart Home Assistant → *Settings → Devices & services → Add integration → Elpris Forecast*.

The *place* is anything `elpriser.dk` accepts as `sted`: a four-digit postcode, `dk1`/`dk2`, or
`a` + a grid area code.

### Supplier markup

elpriser.dk prices include VAT but not your electricity supplier's per-kWh markup (*tillæg*) or
subscription. To see what you actually pay per kWh, set *Electricity agreement* under *Configure*
to your product's `productSlug` from <https://elpriser.dk/data/elpriser.json> (spot-based /
variable agreements only). The markup is then read from the API on every poll and added to all
shown prices and bands; the stored history and the error model stay markup-free, so changing
agreement never disturbs the residuals.

## Entities

### `sensor.<entry>_price`

State: price of the current hour in DKK/kWh. Attributes:

| attribute | |
|---|---|
| `current_price` | same as the state |
| `timeline` | 168 hourly objects from the current hour (see below); not written to the recorder |
| `place`, `region`, `grid_company` | as resolved by elpriser.dk |
| `agreement`, `supplier_markup` | configured agreement and its markup (kr/kWh incl. VAT) |
| `firm_until` | start of the last published hour |
| `forecast_issued_at` | issue times of the forecast runs in use |
| `band_sources` | which band sources the timeline currently uses |

Each `timeline` entry:

```json
{"datetime": "2026-01-07T18:00:00+01:00", "price": 3.21, "is_forecast": true,
 "horizon_hours": 31, "p10": 2.71, "p50": 3.21, "p90": 3.88, "band_source": "publisher"}
```

* `price`: the firm price if published, otherwise the forecast for your place (incl. supplier markup).
* `horizon_hours`: hours ahead of the current hour.
* `p10`/`p50`/`p90`: equal to `price` for firm hours; for forecast hours an 80 % interval.
* `band_source`: `model`, `publisher` or `null` (firm hours / no band available).

### `sensor.<entry>_forecast_error_model` (diagnostic)

State: number of distinct target days scored so far. Attributes summarise the model per forecast
day (ready horizons, mean bias, mean sigma) plus database row counts.

## How it works

### Merge and place shift

The forecast is published for each region's representative grid area. Its all-in price is
`spot_ex_vat × 1.25 + fixed(hour)`, so the integration keeps the forecast spot part and replaces
`fixed(hour)` (grid tariff + charges) with the latest per-hour profile published for your place.
The publisher band is moved by the same amount. For every hour of the timeline the firm price is
used when published (`is_forecast: false`), else the shifted forecast (`is_forecast: true`).

### Database

`<config>/elpris_forecast.db` (SQLite, UTC timestamps, nothing is ever deleted):

| table | contents |
|---|---|
| `forecasts` | `place, target_datetime, forecast_timestamp, horizon_hours, predicted_price, actual_price, residual`, run/model ids and the publisher's raw values and band. Unique per (place, target, issue time). |
| `actuals` | published hourly prices per place, with spot/grid/charges parts |
| `error_model` | snapshot of the per-horizon EWMA state after each refit |
| `v_scored` | view of the scored forecasts |

`actual_price`/`residual` are filled in when the firm price appears, but only for forecasts that
were issued **before** the price was first seen. Example audit query:

```sql
SELECT horizon_hours / 24 AS day, COUNT(*), AVG(residual), AVG(residual * residual)
FROM v_scored GROUP BY day;
```

### Error model

Residual `e = predicted − actual`, horizon `h = ⌊target − issued⌋` in hours, clamped to 1…168.
Per horizon, in target-time order, with forgetting factor λ (default 0.95):

```
W ← λW + 1            Q ← λ²Q + 1            (effective n = W²/Q)
d = e − μ;  μ ← μ + d/W;  S ← λS + d(e − μ);  σ² = S / (W − Q/W)
```

Buckets within ±6 h are pooled with a triangular kernel (forecast runs are issued at fixed
times of day, so a single bucket is also a single hour of day), σ is made non-decreasing in
`h`, and a horizon is used once its pool contains ≥ 3 distinct scored days and an effective
sample size ≥ 5. Quantiles are Gaussian: `p50 = price − μ`, `p10/p90 = p50 ∓ 1.2816 σ`.

The model is re-fitted from the full history whenever new residuals arrive or options change
(*Configure* on the integration: band shown, λ, minimum days, minimum effective n, smoothing window).

Note that this band is symmetric around p50, whereas price errors are typically right-skewed
(spikes). That is why the publisher's asymmetric band is the default; the learned model is kept
running, and since the full history is stored both bands can be back-tested later (coverage of
p10/p90, pinball loss) by replaying the residuals.

## Dashboard example (apexcharts-card)

Needs [apexcharts-card](https://github.com/RomRider/apexcharts-card) and an
`input_select` helper with options `24h` / `7d` (here `input_select.price_horizon`). The band is
drawn as a translucent p90 area with an opaque p10 area in the card's background colour on top,
the price columns over both, and the grid in front.

```yaml
type: vertical-stack
cards:
  - type: heading
    heading: Electricity
    badges:
      - type: entity
        entity: sensor.elpris_price
      - type: entity
        entity: input_select.price_horizon
        tap_action:
          action: perform-action
          perform_action: input_select.select_next
          target: {entity_id: input_select.price_horizon}
  - type: custom:apexcharts-card
    visibility:
      - {condition: state, entity: input_select.price_horizon, state: 24h}
    graph_span: 24h
    span: {start: hour}
    now: {show: true}
    yaxis: [{decimals: 1}]
    series:
      - entity: sensor.elpris_price
        name: Price
        type: column
        unit: kr/kWh
        data_generator: |
          return entity.attributes.timeline.map(e => [new Date(e.datetime).getTime(), e.is_forecast ? null : e.price]);
      - entity: sensor.elpris_price
        name: Forecast
        type: column
        unit: kr/kWh
        data_generator: |
          return entity.attributes.timeline.map(e => [new Date(e.datetime).getTime(), e.is_forecast ? e.price : null]);
  - type: custom:apexcharts-card
    visibility:
      - {condition: state, entity: input_select.price_horizon, state: 7d}
    graph_span: 7d
    span: {start: hour}
    now: {show: true}
    apex_config:
      grid: {position: front}
      stroke: {width: 0}
    series:
      - entity: sensor.elpris_price
        name: p90
        type: area
        color: '#ff9800'
        opacity: 0.3
        unit: kr/kWh
        show: {in_legend: false}
        data_generator: |
          return entity.attributes.timeline.map(e => [new Date(e.datetime).getTime(), e.is_forecast ? e.p90 : null]);
      - entity: sensor.elpris_price
        name: p10
        type: area
        color: var(--card-background-color)
        opacity: 1
        unit: kr/kWh
        show: {in_legend: false}
        data_generator: |
          return entity.attributes.timeline.map(e => [new Date(e.datetime).getTime(), e.is_forecast ? e.p10 : null]);
      - entity: sensor.elpris_price
        name: Price
        type: column
        unit: kr/kWh
        data_generator: |
          return entity.attributes.timeline.map(e => [new Date(e.datetime).getTime(), e.is_forecast ? null : e.price]);
      - entity: sensor.elpris_price
        name: Forecast (p50)
        type: column
        unit: kr/kWh
        data_generator: |
          return entity.attributes.timeline.map(e => [new Date(e.datetime).getTime(), e.is_forecast ? e.p50 : null]);
```

The masking trick assumes p10 ≥ 0 (all-in prices including tariffs and tax practically always are).

## Development

```sh
pip install pytest-homeassistant-custom-component
pytest
```

## License

MIT
