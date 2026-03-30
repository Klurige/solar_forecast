# Solar Forecast Refinement

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
![HA Version](https://img.shields.io/badge/HA-2024.1%2B-blue)

A Home Assistant custom component that **learns from your inverter's actual production** to continuously refine an Open Meteo (or Solcast) solar forecast. The refined forecast is published as a standard HA sensor with a `forecasts` attribute at 15-minute resolution for the next 24 hours.

---

## How it works

```
Open Meteo forecast ──┐
                       ├──► Bias correction model ──► sensor.solar_forecast_refined
Inverter actual W  ────┘         (SQLite, 10 yrs)
```

Every 15 minutes (aligned to clock boundaries) the component:

1. **Samples** the current Open Meteo forecast for that time slot.
2. **Records** the inverter's actual power at the same moment.
3. **Stores** the pair `(forecast W, actual W)` in a local SQLite database.
4. **Recomputes** a per-slot correction factor:

   ```
   correction_factor[slot] = Σ(actual_W/om_W × weight) / Σ(weight)

   where weight = exp(−ln(2) / 35 × days_ago)   ← 35-day half-life
   ```

5. **Publishes** a refined 24-hour forecast:

   ```
   refined_W[slot] = om_W[slot] × correction_factor[slot]
   ```

The model also updates **immediately** whenever the Open Meteo sensor receives new forecast data.

### What the model learns

| Pattern | Effect |
|---|---|
| OM consistently over-predicts → `factor < 1` | Forecast scaled down |
| OM consistently under-predicts → `factor > 1` | Forecast scaled up |
| Morning/afternoon bias differs per slot | Each 15-min slot has its own factor |
| Recent days weighted 2× over 35-day-old data | Model adapts to seasonal change |
| Slots with < 5 samples | Fall back to overall weighted mean |

Night-time slots (< 10 W) are excluded from learning and always output 0 W.

---

## Requirements

- Home Assistant 2024.1 or newer
- An **Open Meteo Solar Forecast** integration providing a sensor with a `watts` attribute (dict of `ISO-timestamp → W`), e.g. `sensor.energy_production_today_2`
- An **inverter power sensor** whose state is the current AC output in **Watts**, e.g. `sensor.input_power_with_efficiency_loss`

> **Tip – tomorrow coverage**: Optionally configure a second Open Meteo entity for tomorrow (`sensor.energy_production_tomorrow_2`) to extend the forecast beyond midnight.

---

## Installation

### Via HACS (recommended)

1. In HACS → **Integrations** → ⋮ → **Custom repositories**
2. Add the URL of this repository, category **Integration**
3. Install **Solar Forecast Refinement**
4. Restart Home Assistant

### Manual

1. Copy the `solar_forecast/` folder to `<config>/custom_components/solar_forecast/`
2. Restart Home Assistant

### Setup

1. Go to **Settings → Integrations → + Add integration**
2. Search for **Solar Forecast Refinement**
3. Fill in the form:

| Field | Description | Example |
|---|---|---|
| **Open Meteo forecast sensor (today)** | Sensor with `watts` attribute | `sensor.energy_production_today_2` |
| **Inverter actual power sensor (W)** | Current AC output in W | `sensor.input_power_with_efficiency_loss` |
| **Open Meteo forecast sensor (tomorrow)** *(optional)* | Extends forecast past midnight | `sensor.energy_production_tomorrow_2` |

---

## Output sensor

**Entity ID:** `sensor.solar_forecast_refined`

### State

Total corrected energy expected over the **next 24 hours** (kWh).

### Attributes

| Attribute | Type | Description |
|---|---|---|
| `forecasts` | list | 96 entries for the next 24 h (see below) |
| `energy_today_kwh` | float | kWh remaining today (local calendar day) |
| `energy_tomorrow_kwh` | float | kWh forecast for tomorrow |
| `correction_factors` | dict | Learned factor per hour-slot (every 4th slot) |
| `total_samples` | int | Total `(om, actual)` pairs stored in the database |
| `data_since` | string | ISO date of the oldest stored reading |

### `forecasts` list entry

```json
{
  "period_end":        "2026-03-29T14:15:00+00:00",
  "pv_estimate":       2.8450,
  "pv_estimate_raw":   3.1200,
  "correction_factor": 0.9118
}
```

| Key | Unit | Description |
|---|---|---|
| `period_end` | ISO UTC | End of the 15-min period |
| `pv_estimate` | kW | **Refined** forecast power |
| `pv_estimate_raw` | kW | Original Open Meteo forecast (uncorrected) |
| `correction_factor` | – | Factor applied (`actual / om` weighted mean) |

The format is intentionally identical to Solcast and the Open Meteo HA integration, so the sensor can be used as a drop-in replacement.

---

## Data storage

The SQLite database is stored at:

```
<config>/.storage/solar_forecast_<entry_id>.db
```

- Up to **10 years** of readings are kept (configurable via `MAX_HISTORY_YEARS` in `const.py`)
- Old data is pruned automatically every 15 minutes
- WAL journal mode is used for safe concurrent access
- A HA restart does **not** lose any data — learning continues from where it left off

---

## Example Lovelace card

The card below shows three series on the same chart:

- 🟡 **Uppmätt** – actual inverter power (real-time)
- 🟢 **Prognos (Open Meteo)** – original uncorrected forecast
- 🔵 **Prognos (Refined)** – bias-corrected forecast from this component

Paste this into an **ApexCharts Card** in the Lovelace code editor.

```yaml
type: custom:apexcharts-card
header:
  show: true
  title: Solprognos – Original vs Raffinerad vs Uppmätt (idag, kW)
graph_span: 24h
span:
  start: day
yaxis:
  - id: kW
    decimals: 1
    min: 0
apex_config:
  yaxis:
    - title:
        text: kW
  markers:
    size: 0
  legend:
    position: top

series:
  # ── Actual inverter power ─────────────────────────────────────────────────
  - entity: sensor.input_power_with_efficiency_loss
    name: Uppmätt
    type: line
    yaxis_id: kW
    color: "#f9a825"
    stroke_width: 2
    curve: smooth
    transform: "return x / 1000;"
    unit: kW

  # ── Original Open Meteo forecast (uncorrected) ────────────────────────────
  - entity: sensor.solar_forecast_refined
    name: Prognos – Open Meteo
    type: line
    yaxis_id: kW
    color: "#4caf50"
    stroke_width: 1
    curve: smooth
    opacity: 0.6
    data_generator: |
      const list = entity?.attributes?.forecasts ?? [];
      return list
        .map(f => {
          const x = f.period_end ? new Date(f.period_end) : null;
          const y = Number(f.pv_estimate_raw);
          return (x && Number.isFinite(y)) ? [x, y] : null;
        })
        .filter(Boolean)
        .sort((a, b) => a[0] - b[0]);
    unit: kW

  # ── Refined forecast (bias-corrected) ────────────────────────────────────
  - entity: sensor.solar_forecast_refined
    name: Prognos – Raffinerad
    type: line
    yaxis_id: kW
    color: "#00b3ff"
    stroke_width: 2
    curve: smooth
    data_generator: |
      const list = entity?.attributes?.forecasts ?? [];
      return list
        .map(f => {
          const x = f.period_end ? new Date(f.period_end) : null;
          const y = Number(f.pv_estimate);
          return (x && Number.isFinite(y)) ? [x, y] : null;
        })
        .filter(Boolean)
        .sort((a, b) => a[0] - b[0]);
    unit: kW
```

### Preview

```
kW
 │
 │         ╭──────╮
 │        ╱  Blue ╲──╮        ← Refined (corrected)
 │       ╱    ╭────╮  ╲
 │      ╱  Green   ╲   ╲      ← Open Meteo (original, typically higher)
 │     ╱   ╱    Yellow╲  ╲    ← Actual inverter
 │────╱───╱────────────╲──╲──
 06  08  10  12  14  16  18   UTC
```

---

## Tuning

All algorithm parameters live in `const.py`:

| Constant | Default | Description |
|---|---|---|
| `CORRECTION_HALF_LIFE_DAYS` | `35` | Days until a sample's weight halves |
| `MIN_CORRECTION_SAMPLES` | `5` | Minimum samples before using per-slot factor |
| `NIGHT_THRESHOLD_W` | `10` | W below which slots are skipped |
| `MAX_RATIO` | `8.0` | Maximum allowed correction factor |
| `MIN_RATIO` | `0.05` | Minimum allowed correction factor |
| `MAX_HISTORY_YEARS` | `10` | Years of data to retain |

---

## Compatibility

| Forecast integration | Attribute format | Supported |
|---|---|---|
| Open Meteo Solar Forecast | `watts` dict | ✅ |
| Solcast | `forecasts` list | ✅ |
| Custom sensors with either format | — | ✅ |

---

## Troubleshooting

**No data / all zeros**
Check that the forecast entity has a `watts` or `forecasts` attribute in **Developer Tools → States**.

**`entity_not_found` error during setup**
The entity must exist (have a state) before setup. Make sure the upstream integration is loaded and the sensor has reported at least once.

**Forecast doesn't improve over time**
The model needs a minimum of `MIN_CORRECTION_SAMPLES` (default 5) days of data per time-slot before it departs from the raw OM forecast. After ~2 weeks the correction should be clearly visible.

**Database location**
`<config>/.storage/solar_forecast_<entry_id>.db` – back this up along with your HA configuration if you want to preserve the learned model.
