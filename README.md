# BESS Dispatch Analyser

Pyomo + HiGHS optimisation of battery energy storage system (BESS) dispatch.
The optimiser reads a spec file and power price series (a single price, or separate buy and
sell prices) from `inputs/` and writes results to `outputs/dispatch_results.csv`.

## Running with Docker

The project ships with a `Dockerfile` and `docker-compose.yml` under `docker/`,
so you don't need a local Python install — just Docker Desktop.

### Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) installed and running.
- Verify with:
  ```bash
  docker --version && docker compose version
  ```

### Build the image (one-time, or after changing `requirements.txt`)

From the project root:

```bash
docker compose -f docker/docker-compose.yml build app
```

### Run the optimisation

```bash
docker compose -f docker/docker-compose.yml run --rm app
```

This executes `python3 src/bess_dispatch_opt.py --spec specification.txt`
inside the container. The compose file mounts the project root into `/app`,
so any edits to `src/`, `specification.txt`, or files in `inputs/` are picked
up immediately without rebuilding, and outputs land back in `outputs/` on
your host machine.

### Run with a different spec file

Override the `BESS_SPEC` env var (path is inside the container, i.e. under `/app`):

```bash
docker compose -f docker/docker-compose.yml run --rm \
  -e BESS_SPEC=/app/some_other_spec.txt app
```

### Open a shell inside the container

Useful for debugging or running ad-hoc Python:

```bash
docker compose -f docker/docker-compose.yml run --rm app bash
```

### Run an arbitrary Python script

```bash
docker compose -f docker/docker-compose.yml run --rm app python3 path/to/script.py
```

### Tear down

```bash
docker compose -f docker/docker-compose.yml down --rmi local
```

## Shortcut: `make`

If you have Apple Command Line Tools installed (`xcode-select --install`),
the included `Makefile` wraps the commands above:

| Command            | Equivalent                                      |
| ------------------ | ----------------------------------------------- |
| `make build`       | Build the image                                 |
| `make run`         | Run the dispatch optimisation                   |
| `make shell`       | Open a bash shell in the container              |
| `make python ARGS="script.py"` | Run an arbitrary Python file        |
| `make pip-install` | Reinstall `docker/requirements.txt`             |
| `make clean`       | Stop containers and remove the local image      |
| `make test`        | Run the test suite (`tests/`, pytest)           |

If `make` isn't installed, use the `docker compose ...` commands above directly —
Docker Desktop alone is enough.

## Project layout

```
.
├── src/bess_dispatch_opt.py   # entry point
├── specification.txt          # default BESS spec (overridable via BESS_SPEC)
├── inputs/                    # price series CSVs and generation profiles
├── outputs/                   # written by the optimiser
├── tests/                     # pytest suite with synthetic inputs (make test)
├── docker/
│   ├── Dockerfile
│   ├── docker-compose.yml
│   └── requirements.txt
└── Makefile
```

## Specification keys

| Key | Required | Description |
|---|---|---|
| `prices_csv` | see below | Path to single-column price CSV (€/MWh, one row per hour). Used for both charging and discharging when `buy_prices_csv` / `sell_prices_csv` are not set; otherwise it fills in whichever of the two is missing and acts as the co-location reference price (see "Buy and sell prices"). |
| `buy_prices_csv` | no | Path to single-column, headerless buy-price CSV (€/MWh, same layout as `prices_csv`): the price paid for charging. `charge_tariff` / `consumption_tariff_csv` are added on top. |
| `sell_prices_csv` | no | Path to single-column, headerless sell-price CSV (€/MWh, same layout as `prices_csv`): the price earned for discharging. `discharge_tariff` is deducted from it. |
| `prices_start_date` | no | Calendar date (`YYYY-MM-DD`), or local datetime (`YYYY-MM-DD HH:MM`) when the first row is not at midnight, of the first row of the price series, in local time. Used to correctly bucket rows into real local calendar days (handling DST — a spring-forward day has 23 rows, a fall-back day has 25) for the "Avg daily spread (sell max-buy min)" report metric. Without it, day boundaries are assumed at fixed 24-row offsets from row 0, which silently drifts out of sync with real calendar days for months at a time around each DST transition. |
| `prices_timezone` | no | IANA timezone of the inputs (default `Europe/Copenhagen`); used with `prices_start_date` and to read dated inputs (see "Input file formats") |
| `output_csv` | yes | Path for the output CSV |
| `output_suffix` | no | String appended to output filenames before the extension (e.g. `_v2` → `dispatch_results_v2.csv`); omit or leave blank for the default name |
| `power` | yes | Rated AC power (MW) |
| `capacity_mwh` | yes | Usable energy capacity (MWh) |
| `round_trip_efficiency` | yes | Grid-to-grid round-trip efficiency (0–1] |
| `initial_soc` | no | Initial state of charge as a fraction of `capacity_mwh` [0–1] (default 0.5) |
| `charge_tariff` | no | Extra cost per MWh charged at the grid meter (default 0) |
| `discharge_tariff` | no | Extra cost per MWh discharged at the grid meter (default 0) |
| `curtailment_price` | no | Price threshold (€/MWh) at or below which co-located generation is treated as curtailed; defaults to `discharge_tariff` when omitted. Set explicitly to decouple curtailment reporting from the export tariff. |
| `max_cycles` | no | Cap on equivalent full cycles over the horizon (constraint C2); comment out to disable |
| `max_cycles_per_day` | no | Cap on equivalent full cycles in each calendar day (constraint C2d). Days come from `prices_start_date` / `prices_timezone` (DST-aware); without `prices_start_date` they are fixed 24-row blocks. A partial first or last day gets the full daily allowance. Can be combined with `max_cycles`. |
| `no_simultaneous_charge_discharge` | no | Forbids charging and discharging in the same timestep (constraint C6, one binary per timestep, so the model becomes mixed-integer and solves more slowly). **Default: on whenever `buy_prices_csv` or `sell_prices_csv` is set and the resulting buy and sell series differ; off with a single price.** Without the constraint, separate prices let the optimiser charge and discharge at full power at once and earn the spread on energy that never leaves the battery. Set `true` or `false` to override the default. |
| `grid_import_mw` | no | Grid connection import capacity (MW); caps how much the BESS can charge from the grid each timestep. In co-location mode this caps only the grid-imported share — BTM charging from the generator is separate and adds on top, up to the `power` limit (see "Co-location mode"). |
| `grid_export_mw` | no | Grid connection export capacity (MW); caps how much the BESS can discharge to the grid each timestep. In co-location mode, generation already occupies part of this connection — see constraint C3 under "Co-location mode" for the exact export-headroom formula. |
| `consumption_tariff_csv` | no | Path to a single-column, headerless per-timestep charge tariff CSV (€/MWh); overrides scalar `charge_tariff` for every timestep when set |
| `generation_profile_csv` | no | Uncomment to enable co-location mode (single-column, headerless capacity-factor CSV `[0–1]`, one row per timestep). See "Co-location mode" for how available generation is computed. |
| `generation_max_mw` | no* | Nameplate capacity of the co-located generator (MW); required when `generation_profile_csv` is set |
| `existing_dispatch_profile_csv` | no | Path to a prior run's output CSV that the BESS must honour as a dispatch floor; the optimizer finds additional value on top. See "Existing dispatch profile workflow" below. |
| `degradation_curves_csv` | no | SoH curves by battery age. **Giving this file switches degradation on**; the number of curves picks the mode (see "How the tool picks the mode"). Further keys in "Endogenous degradation". |
| `endogenous_degradation` | no | Normally not needed. `false` ignores `degradation_curves_csv` (fixed-capacity model); `true` without curves is an error. |
| `discount_rate` | no | Discount rate per year applied to every hour's cash flow by battery age year (default 0, undiscounted). Works with and without degradation. With a non-zero rate and no degradation the report adds the NPV; "Total profit" stays undiscounted. |
| `discount_convention` | no | `end` (default): DF[y] = 1/(1+r)^(y−1); `mid`: 1/(1+r)^(y−0.5). |

\* Required only when `generation_profile_csv` is active.

At least one of `prices_csv`, `buy_prices_csv`, `sell_prices_csv` must be set (`--write-sample-prices`
needs `prices_csv`). All price series, the consumption tariff and the generation profile must have the
same number of rows.

### Input file formats

Every hourly input (`prices_csv`, `buy_prices_csv`, `sell_prices_csv`, `consumption_tariff_csv`,
`generation_profile_csv`) can be in either format; the tool detects which from the first line.

- **Dated:** header `year,month,day,hour,value`, one row per hour, `hour` 0–23 (hour-beginning) in local
  time (`prices_timezone`). Rows must be a gap-free hourly sequence. DST follows exchange data: on the
  autumn change the repeated hour appears twice, in order; on the spring change the skipped hour is
  absent. All dated inputs in a run must have exactly the same timestamps. The first timestamp replaces
  `prices_start_date` (if both are given they must agree).
- **Legacy:** headerless single column, one value per row; rows are positional and day boundaries come
  from `prices_start_date`.

```
year,month,day,hour,value
2030,1,1,0,58.2
2030,1,1,1,55.9
```

### Buy and sell prices

Charging is paid at the **buy price** and discharging earns the **sell price**; the tariffs apply on top
exactly as before (`charge_tariff` added to the buy price, `discharge_tariff` deducted from the sell
price). The series are resolved as follows:

| Given | Buy price | Sell price | Reference price (co-location) |
|---|---|---|---|
| only one of the three keys | that series | that series | that series |
| `buy_prices_csv` + `sell_prices_csv` | `buy_prices_csv` | `sell_prices_csv` | sell price (curtailment, generation revenue) / buy price (refund) |
| `prices_csv` + `buy_prices_csv` | `buy_prices_csv` | `prices_csv` | `prices_csv` |
| `prices_csv` + `sell_prices_csv` | `prices_csv` | `sell_prices_csv` | `prices_csv` |
| all three | `buy_prices_csv` | `sell_prices_csv` | `prices_csv` |

The reference price is used only in co-location mode, as `price_curt[t]` for the curtailment check
(`gen_curt`) and the generation revenue in the reports, and as `price_refund[t]` for the refund on
charging from curtailed / clipped generation (O2). Both equal `prices_csv` when it is set; without it
`price_curt[t] = price_sell[t]` and `price_refund[t] = price_buy[t]`. That refund cancels the buy-price charge exactly only when the reference price equals the buy
price; with a distinct `prices_csv` alongside `buy_prices_csv` the two differ by design.
The output CSV has `price_buy` and `price_sell` columns instead of a single `price`.

### Grid connection limit

`grid_import_mw` bounds `ch_mwh[t]` (grid import) and `grid_export_mw` bounds `dsch_mwh[t]`
(grid export), each to `grid_*_mw × interval_hours` per timestep. If the BESS rated power
(`power`) is lower, the tighter of the two limits applies.

### Existing dispatch profile workflow

`existing_dispatch_profile_csv` lets you pre-commit the BESS to a dispatch schedule (e.g. a
contracted profile, or a prior run's BTM-only result) and have the optimizer add incremental
value on top using the remaining capacity headroom:

1. Run a first optimisation (e.g. BTM/co-location only) → produces `dispatch_results.csv`.
2. Set `existing_dispatch_profile_csv` to that output file's path.
3. Run again — the BESS honours the prior dispatch as a minimum floor at every timestep and
   optimises additional arbitrage on top (e.g. grid arbitrage layered on a BTM-only base).

The file must be a named-column CSV with at least `charge_mwh` and `discharge_mwh` (stored-side
MWh per timestep) — matching the column names in this tool's own output CSV, so you can point it
directly at a prior run's output. The profile length must match the price series.

## Stand-alone model

This section describes the LP as it exists with no generation profile configured
(`generation_profile_csv` unset). See [Co-location mode](#co-location-mode) below for what
changes when a co-located generator is added.

### Model variables

Pyomo decision variables and parameters built from the spec/CSV inputs (see `build_and_solve` in `src/bess_dispatch_opt.py`). One entry per timestep `t` unless noted otherwise.

| Name | Kind | Bounds / value | Description |
|---|---|---|---|
| `price_buy[t]` | Param | from `buy_prices_csv` (or the fallback, see "Buy and sell prices") | Price paid for charging (€/MWh) |
| `price_sell[t]` | Param | from `sell_prices_csv` (or the fallback) | Price earned for discharging (€/MWh) |
| `ctariff[t]` | Param | from `consumption_tariff_csv` (optional) | Per-timestep charge tariff (€/MWh); overrides scalar `charge_tariff` when set |
| `profile_ch_param[t]` / `profile_dsch_param[t]` | Param | from `existing_dispatch_profile_csv` (optional), converted to grid-side via `η_leg` | Pre-committed charge/discharge floor that `ch_mwh[t]` / `dsch_mwh[t]` must meet or exceed |
| `initial_soc` | scalar (not indexed by `t`) | from `initial_soc` (default 0.5) | Initial state of charge as a fraction of `capacity_mwh`, applied at `t=0` in C1 |
| `soc_mwh[t]` | Var | `[0, capacity_mwh]` | Battery state of charge at end of timestep |
| `ch_mwh[t]` | Var | `[0, min(power, grid_import_mw)×dt]` | Total charging = grid import |
| `dsch_mwh[t]` | Var | `[0, min(power, grid_export_mw)×dt]` | Grid export (discharge) |

### Variable bounds

| Label | Constraint | Notes |
|-------|-----------|-------|
| **B1** | `0 ≤ soc_mwh[t] ≤ capacity_mwh` | SOC within usable battery limits |
| **B2** | `0 ≤ ch_mwh[t] ≤ min(power_mw, grid_import_mw) × dt` | Total charging bounded by BESS power rating and grid import cap |
| **B3** | `0 ≤ dsch_mwh[t] ≤ min(power_mw, grid_export_mw) × dt` | Grid export bounded by BESS power rating and export connection |

### Constraints

**C1 — SOC energy balance** (`η_leg = √round_trip_efficiency`):

```
soc_mwh[0] = initial_soc × capacity_mwh + ch_mwh[0] × η_leg − dsch_mwh[0] / η_leg
soc_mwh[t] = soc_mwh[t-1]              + ch_mwh[t] × η_leg − dsch_mwh[t] / η_leg   ∀ t > 0
```

**C2 — Lifetime cycle cap** (optional; omit `max_cycles` to disable):

```
η_leg × Σ_t ch_mwh[t] ≤ max_cycles × capacity_mwh
```

**C2d — Daily cycle cap** (optional; omit `max_cycles_per_day` to disable), for every calendar day `d`:

```
η_leg × Σ_{t in d} ch_mwh[t] ≤ max_cycles_per_day × capacity_mwh
```

**C6 — No simultaneous charge and discharge** (on by default with separate buy and sell prices; see `no_simultaneous_charge_discharge`), with a binary `is_charging[t]`:

```
ch_mwh[t]   ≤ max_ch_mwh   × is_charging[t]
dsch_mwh[t] ≤ max_dsch_mwh × (1 − is_charging[t])
```

### Objective

**O1 — Stand-alone** (maximise over all timesteps):

```
max  Σ_t [ price_sell[t] × dsch_mwh[t] − price_buy[t] × ch_mwh[t]
         − discharge_tariff × dsch_mwh[t]
         − charge_tariff    × ch_mwh[t] ]
```

`charge_tariff` is replaced by `ctariff[t]` when a per-timestep consumption tariff series is supplied.

## Co-location mode

Uncomment `generation_profile_csv` in the spec to enable co-location mode.
The profile CSV must be a **single-column, headerless** file of capacity factors [0–1],
one row per timestep (same layout as the prices CSV, e.g. `solar_profile_Denmark_1h_2024.csv`).
The BESS is then co-located behind the meter with a generator, and can charge from the grid,
from the generator, or a combination of both.

Available generation per timestep:

```
generation_mwh[t] = capacity_factor[t] × generation_max_mw × interval_hours
```

Everything in [Stand-alone model](#stand-alone-model) still applies; this section lists what is
added or changed on top of it.

### Additional variables and parameters

| Name | Kind | Bounds / value | Description |
|---|---|---|---|
| `gen_curt[t]` | Param | `generation_mwh[t]` if `price_curt[t] ≤ curtailment_threshold`, else `0` | Generation fully curtailed for the hour (e.g. negative-price hours); would not be exported at all |
| `gen_avail[t]` | Param | `min(generation_mwh[t] − gen_curt[t], export_connection_dt)` | Exportable generation available for BTM charging or direct export |
| `gen_surplus[t]` | Param | `max(0, (generation_mwh[t] − gen_curt[t]) − export_connection_dt)` | Clipped, non-curtailed generation beyond export connection capacity; free BTM charging source |
| `ch_grid_mwh[t]` | Var | `[0, grid_import_mw×dt]` (B4; also `≤ ch_mwh[t]` via C4) | Grid-imported share of charging |
| `ch_from_gen_avail[t]` | Var | `[0, gen_avail[t]]` (pinned to `min(ch_btm[t], gen_avail[t])` by B5, C5) | BTM charging sourced from `gen_avail[t]` (discharge-tariff-refund-eligible share) |
| `ch_from_gen_curt[t]` | Var | `[0, gen_curt[t]]` (B6, C5) | BTM charging sourced from `gen_curt[t]` (no discharge-tariff refund) |
| `ch_from_gen_surplus[t]` | Var | `[0, gen_surplus[t]]` (B7, C5) | BTM charging sourced from `gen_surplus[t]` (no discharge-tariff refund) |

Derived (not a separate Pyomo variable): `ch_btm[t] = ch_mwh[t] − ch_grid_mwh[t] = ch_from_gen_avail[t] + ch_from_gen_curt[t] + ch_from_gen_surplus[t]` — total behind-the-meter charging in a timestep, exactly attributed across its three sources by C5.

### Modified and additional bounds

`ch_mwh[t]` (**B2**) is redefined: in co-location mode it represents total charging (BTM + grid
combined), and the import cap moves to the new `ch_grid_mwh[t]` variable instead:

```
0 ≤ ch_mwh[t] ≤ power_mw × dt
```

Four additional bounds:

| Label | Constraint | Notes |
|-------|-----------|-------|
| **B4** | `0 ≤ ch_grid_mwh[t] ≤ grid_import_mw × dt` | Grid-imported share of charging bounded by import connection; falls back to `power_mw × dt` if `grid_import_mw` not set |
| **B5** | `0 ≤ ch_from_gen_avail[t] ≤ gen_avail[t]` | BTM charging sourced from exportable generation; upper-bounded by availability per timestep |
| **B6** | `0 ≤ ch_from_gen_curt[t] ≤ gen_curt[t]` | BTM charging sourced from curtailed generation; upper-bounded by availability per timestep |
| **B7** | `0 ≤ ch_from_gen_surplus[t] ≤ gen_surplus[t]` | BTM charging sourced from surplus generation; upper-bounded by availability per timestep |

### Additional constraints

**C1 is expanded** — total charging in the SOC balance splits into four sources instead of one:

```
soc_mwh[0] = initial_soc × capacity_mwh
           + (ch_grid_mwh[0] + ch_from_gen_avail[0] + ch_from_gen_curt[0] + ch_from_gen_surplus[0]) × η_leg
           − dsch_mwh[0] / η_leg

soc_mwh[t] = soc_mwh[t-1]
           + (ch_grid_mwh[t] + ch_from_gen_avail[t] + ch_from_gen_curt[t] + ch_from_gen_surplus[t]) × η_leg
           − dsch_mwh[t] / η_leg   ∀ t > 0
```

| Term | Source |
|------|--------|
| `ch_grid_mwh[t]` | Grid import |
| `ch_from_gen_avail[t]` | BTM charging sourced from exportable generation |
| `ch_from_gen_curt[t]` | BTM charging sourced from curtailed generation |
| `ch_from_gen_surplus[t]` | BTM charging sourced from surplus (clipped) generation |

`ch_from_gen_curt[t]` and `ch_from_gen_surplus[t]` get identical (no-refund) tariff treatment in the
objective, so the LP has no economic preference between them — in practice at most one of
`gen_curt[t]`/`gen_surplus[t]` is nonzero in a given hour anyway (a curtailed hour has no export
connection headroom left over to be "surplus"), so the split is not actually ambiguous in the
solved model.

The three BTM terms are pinned by an equality (C5), not just bounded above:

```
ch_btm[t]  =  ch_from_gen_avail[t] + ch_from_gen_curt[t] + ch_from_gen_surplus[t]
           =  ch_mwh[t] − ch_grid_mwh[t]
```

**C3 — Export headroom** — generation occupies part of the export connection; the BESS can only use the remainder:

```
dsch_mwh[t] ≤ export_connection_dt − gen_avail[t]
```

where `export_connection_dt = grid_export_mw × dt` (or `power_mw × dt`), and  
`gen_avail[t] = min(generation_mwh[t] − gen_curt[t], export_connection_dt)`.

**C4 — Grid import ceiling**:

```
ch_grid_mwh[t] ≤ ch_mwh[t]
```

Prevents `ch_grid_mwh` from being inflated when `charge_tariff = 0` gives no cost signal.

**C5 — BTM charging source split**:

```
ch_from_gen_avail[t] + ch_from_gen_curt[t] + ch_from_gen_surplus[t] = ch_mwh[t] − ch_grid_mwh[t]
```

BTM charging is fully attributed across the three sources (equality, not just an upper bound).
Combined with B5 this pins `ch_from_gen_avail[t] = min(ch_btm[t], gen_avail[t])` — the optimizer
drives it to this value naturally because `ch_from_gen_avail` earns a `discharge_tariff` refund in
the objective, while `ch_from_gen_curt[t]` and `ch_from_gen_surplus[t]` split the remainder up to
their own availability (B6, B7).

Note there is no separate "grid import lower bound" constraint: summing the B5/B6/B7 upper bounds
and substituting C5 gives `ch_mwh[t] − ch_grid_mwh[t] ≤ gen_avail[t] + gen_curt[t] + gen_surplus[t]`,
i.e. `ch_grid_mwh[t] ≥ ch_mwh[t] − gen_avail[t] − gen_curt[t] − gen_surplus[t]`, automatically — an
explicit constraint to that effect would be redundant with B5–B7 and C5 (confirmed by deactivating
it and re-solving: identical objective and identical `ch_grid_mwh[t]` at every timestep). Note this
does *not* mean `ch_grid_mwh[t]` is always pinned to that floor — when `price_buy[t]` is negative enough
that grid import itself earns more than the `charge_tariff` cost, the optimizer prefers grid import
over "free" `gen_curt`/`gen_surplus` charging and uses more than the floor requires.

### Objective addendum

**O2 — Co-location**: `standalone_term` (O1) applies unconditionally; `colocation_addendum_term`
is added only in co-location mode (see `profit_rule` in `src/bess_dispatch_opt.py`):

```
standalone_term[t]          =  price_sell[t]    × dsch_mwh[t] − price_buy[t] × ch_mwh[t]
                              − discharge_tariff × dsch_mwh[t]
                              − charge_tariff    × ch_mwh[t]

colocation_addendum_term[t] =  charge_tariff    × ch_btm[t]
                              + price_refund[t]  × (ch_from_gen_curt[t] + ch_from_gen_surplus[t])
                              + discharge_tariff × ch_from_gen_avail[t]

max  Σ_t [ standalone_term[t] + colocation_addendum_term[t] ]   (co-location mode)
max  Σ_t   standalone_term[t]                                    (stand-alone mode)
```

`standalone_term` prices and taxes all of `ch_mwh[t]` as if it were grid-imported;
`colocation_addendum_term` refunds exactly the three BTM treatments already established above —
`charge_tariff` on the full `ch_btm[t]`, the reference price on the `gen_curt`/`gen_surplus` share, and
`discharge_tariff` on the `gen_avail` share — with `ch_btm[t] = ch_from_gen_avail[t] +
ch_from_gen_curt[t] + ch_from_gen_surplus[t]`.

`charge_tariff` is replaced by `ctariff[t]` when a per-timestep consumption tariff series is supplied.

## Endogenous degradation

When degradation curves are given, the optimiser decides how hard to cycle in each period knowing that
cycling (and age) reduces future capacity, and trades today's revenue against tomorrow's capacity in one
solve. The model stays a **pure LP** (no binaries) and is solved with HiGHS (dual simplex by default; see
"Solver choice"). Everything in the fixed-capacity model (tariffs, co-location, grid caps, cycle caps,
profile floors) still applies.

### How the tool picks the mode

| Inputs | What runs |
|---|---|
| No `degradation_curves_csv` | Fixed capacity over the whole price series: the original model, unchanged (results identical to before). |
| One curve (one cycling rate, e.g. `year,1`) | SoH follows that curve by age; the run lasts **exactly the curve's lifetime** (later prices are ignored; fewer prices than the lifetime is an error). Average cycling since commissioning is capped at the curve's rate. |
| Two or more curves (e.g. `year,1,1.25,1.5`) | SoH interpolated between the curves at the average cycles/day since commissioning (`cumulative` method), and a **retirement search** over lifetimes from the fastest to the slowest curve's life (within the price horizon). See "Retirement choice". |

With several curves, `retirement_years` overrides the candidate lifetimes (e.g. `18,21,25`), and
`retirement_years = off` runs the price horizon with the battery required to survive it. If the prices
end before the fastest curve's life, the battery cannot die within them and a single run is made.

### Additional specification keys

| Key | Default | Description |
|---|---|---|
| `degradation_method` | `cumulative` | `cumulative`: capacity at the end of each period is read from the curves at the average cycling rate since commissioning. `period`: each period's loss follows that period's own cycling (bands and transitions). See "Two degradation methods". |
| `degradation_curves_csv` | — | SoH curves, long layout `cycles_per_day,year,value` or wide layout `year,<rate>,<rate>,...` (e.g. `year,1,1.25,1.5`, blank once a curve has ended): `year` = battery age in years (0 = commissioning, value 1.0, added if missing), `value` = state of health as a fraction of nominal energy; one or more cycling rates. A trailing 0 marks a dead curve and is dropped. Each curve runs until end of life, so curves may have different lengths (the longest must cover the horizon), but they **must all end at the same SoH**, which becomes the end-of-life limit (C10). Different final values are an error (tolerance 0.0001). |
| `calendar_curve_csv` | extrapolated | Optional zero-cycling SoH curve, header `year,value`. Without it calendar loss is extrapolated linearly from the two lowest-rate curves. |
| `extrapolate_below_lowest_rate` | `false` | `cumulative` method without `calendar_curve_csv`. `false` (default) takes the curves exactly as given and keeps the average cycles/day since commissioning at or above the lowest rate; `true` estimates a zero-cycling SoH by linear extrapolation through the two lowest rates still alive (capped at 1, never rising with age); `false` takes the curves exactly as given and uses the lowest-rate curve's value below that rate. |
| `degradation_period` | `year` | `year` or `month`. Years are age years from the first timestep (anniversaries); months are months since the first timestep, 12 to each age year. |
| `month_degradation_multipliers` | 1 | 12 comma-separated factors (January…December) on calendar loss and damage per cycle, for monthly periods with the `period` method only (hook for temperature effects). |
| `cycle_weight_charge`, `cycle_weight_discharge` | 0, 1 | Weights `a_ch`, `a_dis` in the cycle count: (0, 1) discharge-based, (0.5, 0.5) average-based. Must match how the curves define cycles. |
| `discount_rate`, `discount_convention` | 0, `end` | See the main key table; with degradation the NPV is the objective. |
| `soc_min_fraction`, `soc_max_fraction` | 0, 1 | SoC limits as fractions of the degraded capacity. |
| `final_soc_fraction` | `initial_soc` | SoC in the last hour ≥ this × end capacity (0 disables). |
| `terminal_value_per_mwh` | 0 | Value of each MWh of capacity left at the end of the horizon, discounted to today. |
| `var_om_per_mwh` | 0 | Variable O&M cost per MWh discharged (grid side). |
| `warranty_throughput_mwh` | off | Limit on total cycle-weighted stored throughput over the horizon. |
| `allow_battery_death` | `false` | **Experimental, does not scale; use `retirement_years`.** `cumulative` method: one yes/no "alive" switch per age year, so the optimiser may let the battery die (capacity 0, no revenue) within the horizon. Makes the model a MIP. See "Battery death". |
| `mip_rel_gap`, `mip_time_limit_s` | 0.0001, off | Stopping rules for the MIP with `allow_battery_death` (relative gap; time limit in seconds, keeping the best solution found). |
| `retirement_years` | automatic | Several curves: candidate lifetimes, default every year from the fastest to the slowest curve's life within the price horizon. Override with e.g. `18-20` or `15,18,20`; `off` disables the search. Not allowed with a single curve. See "Retirement choice". |
| `solver_method` | `simplex` | HiGHS method (`simplex`, `ipm`, `choose`). See "Solver choice". |
| `run_crossover` | `on` | Crossover after interior point (`on`, `off`, `choose`); only used with `ipm`. With `off`, a run that does not end optimal is repeated with crossover on. |

Degradation needs real timestamps: dated inputs, or `prices_start_date` with legacy inputs.

### Two degradation methods

- **`cumulative` (default)** — degradation depends on the cycles since commissioning. At the end of each
  period (age `a` years) the battery has done `N` cycles in total, i.e. an average of `N / (365·a)`
  cycles/day; its capacity is read from the curves at age `a`, interpolating linearly between the rate
  columns at that average rate. Example: at age 10 with an average of 1.3 cycles/day, SoH lies between
  the year-10 values of the 1.25 and 1.5 curves. Below the lowest rate the lookup interpolates towards
  a zero-cycling point (calendar curve, or linear extrapolation through the two lowest rates, capped at
  1). Cycling in any period is capped at the highest rate in the curves, so nothing is extrapolated
  above it.
- **`period`** — degradation in each period depends only on that period's cycling: per-cycle damage
  from the curves' year-on-year losses at that age, summed through capacity transitions (below).

The two differ when cycling changes over life: under `cumulative`, a year at high cycling followed by
a year at low cycling ends at the same capacity as two years at the average rate; under `period`, the
losses of each year are taken from the curves' slopes in that year and added up.

### Preprocessing, `cumulative` method (`build_soh_lookup`)

For each period `p`, with `a[p]` the battery age at its end (hours-based, so partial years are exact),
the points `(N, SoH) = (365·a[p]·r, SoH_r(a[p]))` for `r = 0, r1, r2, ...` (curves interpolated linearly
between whole years; a curve past its end continues with its last-year loss). Points are made
non-increasing in `N`, then replaced by their upper concave envelope if needed (warning printed), which
keeps the lookup a set of linear constraints. The longest curve must cover the horizon.

**A curve ending at 0** marks death; see "Battery death".

The envelope is exact when damage per cycle rises (or stays flat) with the cycling rate. When it falls
(e.g. curves where lifetime cycles grow with the rate), the envelope lies above the intermediate
points: an LP cannot represent "cycling faster is cheaper per cycle". The periods table and report then
show capacity read straight from the curves alongside, so the size of the gap is visible.

### Preprocessing, `period` method (`derive_degradation_params`, `period_degradation_params`)

For each age year `y` and rate `n ∈ {1, 1.5, 2}` cycles/day: `Loss[y,n] = SoH_n(y−1) − SoH_n(y)`.
A curve that ends before year `y` (it reached end of life sooner) keeps its last-year loss; the
end-of-life limit still stops capacity going below the curves' common final SoH.

```
Calendar[y]  = SoH_cal(y−1) − SoH_cal(y)                 (with calendar_curve_csv)
             = max(0, 3·Loss[y,1] − 2·Loss[y,1.5])       (otherwise, linear extrapolation)
Damage[y,1]  = (Loss[y,1]   − Calendar[y]) / 365         loss per cycle, 0–1 cycles/day
Damage[y,2]  = (Loss[y,1.5] − Loss[y,1])   / 182.5       1–1.5 cycles/day
Damage[y,3]  = (Loss[y,2]   − Loss[y,1.5]) / 182.5       1.5–2 cycles/day
```

If `Damage[y,1] ≤ Damage[y,2] ≤ Damage[y,3]` fails, a warning is printed and the four points are replaced
by their lower convex envelope; negative damage is floored at 0. With extrapolated calendar loss,
`Damage[y,1] = Damage[y,2]` always (the extrapolation is the line through the 1 and 1.5 points), unless
the calendar loss is floored at 0; supply `calendar_curve_csv` to distinguish the two bands.

Per period `p` in age year `y`, with `f[p]` = hours in the period ÷ hours in that full age year (so
DST, leap years and partial first or last periods are exact): `Calendar[p] = Calendar[y]·f[p]`,
band widths `Width[p] = (365, 182.5, 182.5)·f[p]` cycles, `Damage[p,k] = Damage[y,k]`, each times the
month multiplier for monthly periods.

### Variables, constraints and objective

`Cap[p]` (MWh) is the capacity at the start of period `p`, with `Cap[1] = nominal` fixed and `Cap[P+1]`
the end of the horizon; `x[p,k] ∈ [0, Width[p,k]]` are the cycles in band `k`.

**C7 — SoC within degraded capacity**, for every hour `t` in period `p` (C1 keeps SoC continuous across
periods; the fixed bound `soc_mwh ≤ capacity_mwh` stays):

```
soc_min · ½(Cap[p] + Cap[p+1])  ≤  soc_mwh[t]  ≤  soc_max · ½(Cap[p] + Cap[p+1])
```

**C8 — Cycles and bands** (stored-side energy, the same basis as C2):

```
Throughput[p] = Σ_{t∈p} ( a_ch · η_leg · ch_mwh[t]  +  a_dis · dsch_mwh[t] / η_leg )
Σ_k x[p,k]    = Throughput[p] / nominal
```

Total cycling is therefore at most 2 cycles/day on average in each period.

The constraints below, C8 bands and C9, belong to the `period` method; C8c and C9c replace them for
`cumulative`.

**C9 — Capacity transition**:

```
Cap[p+1] = Cap[p] − nominal · ( Calendar[p] + Σ_k Damage[p,k] · x[p,k] )
```

**C8c — Cumulative cycles** (`cumulative` method): `N[p] = N[p−1] + Throughput[p] / nominal`, and
`Throughput[p] ≤ nominal · r_max · hours[p] / 24` (no cycling above the highest curve rate).

**C9c — SoH lookup** (`cumulative` method), one row per segment `i` of period `p`'s concave lookup:

```
Cap[p+1] ≤ nominal · ( c0[p,i] + c1[p,i] · N[p] )
Cap[p+1] ≤ Cap[p]
```

The lookup is an upper bound: when capacity has value to the optimiser (C7 binds, terminal value),
`Cap[p+1]` sits on the lookup. Constraints that reward a *lower* capacity (`soc_min_fraction`,
`final_soc_fraction`) can pull the last `Cap` below it; reported capacity always follows the lookup
(see "Reported capacity").

**C10 — End conditions and limits.** Always: `Cap[P+1] ≥ EOL · nominal`, where `EOL` is the SoH all
curves end at (so the battery never ends the horizon below end of life; if calendar loss alone
makes this impossible the solve reports infeasible with a hint). Optional:
`soc_mwh[last] ≥ final_soc_fraction · Cap[P+1]` and `Σ_p Throughput[p] ≤ warranty_throughput_mwh`.

**C11 — LP stand-in for C6.** C6 needs binaries, so with degradation an explicit
`no_simultaneous_charge_discharge = true` is an error. Under C6's default rule (separate buy and sell
prices that differ) C11 is added instead: `ch_mwh[t]/max_ch + dsch_mwh[t]/max_dsch ≤ 1`, so charging and
discharging share one power budget. This limits but does not forbid same-hour loops; the report counts
the simultaneous hours and warns if there are any. With a single price series the issue does not arise.

**O3 — Objective** (`DF[y]` per age year, `DF_end = 1/(1+r)^(horizon in years)`):

```
max  Σ_t DF[y(t)] · ( op_cash[t] − var_om · dsch_mwh[t] )
     + DF_end · terminal_value · Cap[P+1]
```

`op_cash[t]` is exactly the fixed-capacity hourly term (O1, plus O2 in co-location mode), so tariffs
and co-location are included. In code the LP minimises −NPV: with crossover off, this HiGHS version
reports status "Unknown" and returns duals with the wrong sign for maximisation problems, but handles
minimisation correctly.

### Battery death (`allow_battery_death`, experimental)

> **Does not scale.** On a 3-year case the MIP matched the retirement search exactly in about 11 s, but
> on a 7-year hourly case it found the right answer within ~4 minutes and then could not prove it: the
> gap stalled at 10.8 % after 13 minutes (the same case as a plain LP: 147 s). For real horizons use
> `retirement_years`, which gives the same answer exactly. The option is kept for experiments only.

**C12.** One binary `alive[y]` per age year `y`, non-increasing (`alive[y+1] ≤ alive[y]`: a dead battery
stays dead). For every period `p` in year `y`:

```
ch_mwh[t]  ≤ max_ch  · alive[y]          dsch_mwh[t] ≤ max_dsch · alive[y]      (no dispatch when dead)
Cap[p+1]   ≤ nominal · alive[y]                                                 (no capacity when dead)
Cap[p+1]   ≥ EOL · nominal · alive[y]                                           (alive ⇒ at or above end of life)
N[p]       ≥ r_min · 365 · a[p] · alive[y]                                      (minimum average, only while alive)
```

The SoH lookup (C9c) is relaxed for a dead battery only where a lookup line could fall below 0 (big-M as
small as possible), which keeps the relaxation tight: on the synthetic test the MIP solved in about 11 s,
against 215 s with loose big-Ms. A dead battery has zero capacity, so C7 forces it to be empty when it
dies. A year in which SoH would end below the limit is a dead year (as in a spreadsheet that switches
capacity to 0 below end of life). After the MIP the death year is fixed and the model re-solved as an LP,
so the dual-based outputs are available as usual. The periods table gets `alive` and `death_loss_mwh`
columns; the report gives the operating life, the MIP time and gap. Trailing years that are alive but
idle at the end-of-life limit (a tie with dying) are reported as dead.

Curves may end with 0 to mark death; those zeros are dropped on loading and the curve continues past its
end with its last yearly loss. This interpolates consistently between a dead and a live rate.

With `extrapolate_below_lowest_rate = false`, the average cycles/day since commissioning is kept between
the lowest and highest curve rates (the minimum only while alive); otherwise only the maximum applies.
Either way it is the *average* that is limited: single years may lie outside the range.

### Retirement choice

With several curves (or `retirement_years` set), the tool solves one LP per candidate operating life
`H`: the battery operates for the first `H` age years, must be at or
above end of life at the end of year `H` (C10), and earns nothing afterwards (terminal value, if set,
is counted at `H`). The candidate with the highest NPV is kept (the shorter one on a tie) and all
outputs cover its operating life only; the report lists every candidate's NPV and end capacity.
Keeping it a series of LPs (instead of binary "alive" variables) keeps each solve a pure LP; run time
grows with the number of candidates.

The earliest meaningful candidate is the lifetime of the fastest curve (cycling is capped at its rate);
candidates beyond the price horizon are an error, so extend the price series to test longer lives.

**Equivalence with the MIP.** Because a dead battery stays dead, the alive switches can only take the
pattern 1…1 0…0, i.e. one pattern per death year. Solving every candidate year and keeping the best NPV
is therefore exactly the MIP's answer (checked in `tests/test_degradation.py`), provided the candidates
cover every death year that could win and no terminal value is set (the search values capacity left
at `H`; the MIP gives a dead battery none).

**Run time and memory.** Only one candidate model is kept in memory at a time; if the winner is not
the last candidate solved, it is solved again at the end (one extra LP). A 25-year hourly candidate takes
about 4–9 minutes and 2 GB, so the default 13-candidate search on 13–25-year curves takes about 1–1.5
hours. To save time, give a coarse `retirement_years` (e.g. `13,16,19,22,25`), or run one spec per
candidate (`retirement_years = H`, own `output_csv`) in parallel, at most two at once on an 8 GB machine.

**Curve shape.** The end-of-life limit is exact by the input curves only if, at every age, each extra
step in cycling rate costs at least as much SoH as the previous one (SoH concave in cumulative cycles).
Otherwise the lookup is replaced by its concave envelope (warning printed), which reads up to the
envelope gap above the curves and can let a candidate cycle slightly more than the curves allow. Curves
rounded to two decimals or with dead curves extended by a steep last-year loss can trigger this; the
periods table column `capacity_end_curves_mwh` shows the curves' own value.

### Solver choice

Measured on a 7-year hourly case (61,368 hours, 1 MW / 2 MWh), same model, same optimum:

| HiGHS setting | Time | Result |
|---|---|---|
| `simplex` (default) | 108 s | optimal, exact duals |
| `ipm`, crossover on | 126 s | optimal, exact duals |
| `ipm`, crossover off | 27 s | **not usable**: stops as "Unknown", primal infeasibility 0.025 MWh, inconsistent duals |
| `pdlp` | > 410 s | no solution within the time limit |

Interior point alone does not converge on this model (each period's capacity appears in thousands of SoC
rows), and its crossover is essentially a full simplex run, so plain simplex is the default.

### Degradation cost from the duals

For a minimisation, the dual of C9 is d(objective)/d(right-hand side); adding 1 MWh to `Cap[p+1]` raises
the NPV by `−dual`, so `V[p] = −dual(C9[p])` is the present value of 1 MWh of capacity at the end of
period `p`. The implied degradation cost per MWh of cycle-weighted throughput in band `k`, in period-`p`
money, is

```
DegCost[p,k] = V[p] · Damage[p,k] / DF[p]
```

(one cycle is `nominal` MWh of throughput and costs `nominal · Damage[p,k]` MWh of capacity). The sign
is checked in `tests/test_degradation.py` against a finite difference.

With the `cumulative` method, `V[p] = −Σ_i dual(C9c[p,i])`, and the cost of one more MWh of throughput
in period `p` comes from the dual of C8c (adding a cycle to `N[p]` without revenue tightens every later
lookup):

```
DegCost[p] = dual(C8c[p]) / nominal / DF[p]
```

These are post-solve diagnostics only. The objective has no degradation cost term: the cost of
cycling appears only through lost future capacity.

### Outputs

- **Hourly CSV:** extra columns `timestamp`, `period`, `age_year`, `capacity_mwh_period` (the period's
  average capacity) and `discount_factor`.
- **`<output>_periods.csv`** (and xlsx sheet "Periods"): start, hours, age year, capacity at start and
  end, throughput, cycles, cycles per day, cycles per band, calendar and cycle loss,
  discount factor, `capacity_value_pv_eur_per_mwh` (`V[p]`) and `deg_cost_band1..3_eur_per_mwh`
  (`period`), or age at period end, cumulative cycles, average cycles/day since commissioning and
  `deg_cost_eur_per_mwh` (`cumulative`, no band columns), plus `capacity_end_curves_mwh`: the same
  lookup read straight from the curve points, without the concave envelope. Under `cumulative` the calendar loss is the
  drop of the zero-cycling SoH over the period and the cycle loss is the rest.
- **`<output>_yearly.csv`** (and sheet "Yearly"): per age year, operating cash, variable O&M, net, the
  discount factor and discounted net.
- **Report:** a "--- Degradation ---" section (curves, period, horizon, discount rate, start and end
  capacity, end-of-life limit, calendar and cycle loss, cycles, NPV, simultaneous hours). "Total profit" in "BESS Results"
  stays the undiscounted operating profit, comparable with the fixed-capacity model.

**Reported capacity.** When capacity has no value to the optimiser in some periods (no terminal value
and SoC limits not binding), any band split is optimal (`period`) or `Cap` may sit below the lookup
(`cumulative`), so the solver may report a higher loss than physics implies. Reported capacity
therefore follows `physical_capacity_path`: the cheapest band fill for each period's cycles, or the
lookup at the actual cumulative cycles. That is what the LP itself chooses whenever capacity has value; a
note is printed when the two differ. The dispatch stays feasible because the physical path is never
below the LP's.

### Validation

`make test` (or `pytest tests`) checks, on synthetic data:

1. With degradation off, outputs are byte-identical to the `main` branch (co-location case) and the DK1
   test-repo results are unchanged.
2. With zero calendar and cycle loss, capacity stays at nominal and the dispatch value equals the
   fixed-capacity model.
3. One capacity transition per period recomputed by hand.
4. Forcing exactly 1 and 2 cycles/day reproduces the input curves (within 0.1% of nominal).
5. Bands fill in order when damage strictly increases across bands.
6. A higher discount rate cycles harder now (0% vs 50% with a terminal value).
7. Yearly and monthly periods agree on revenue, cycles and end capacity (within 2%).
8. Convex-envelope repair, the dual sign (finite difference), the pure-LP property, input formats
   and DST, and end-to-end outputs. `RUN_SLOW=1` adds a 7-year hourly smoke test.
9. Modes from inputs: curves switch degradation on, `endogenous_degradation = false` ignores them
   (identical outputs), several curves run the retirement search automatically (same answer as an
   explicit search), `retirement_years = off` disables it, a single curve runs exactly its lifetime with
   capacity following the curve; `discount_rate = 0` changes nothing and a non-zero rate discounts the
   fixed-capacity model.
10. `cumulative` method: the lookup interpolates between rate columns and hits every curve point; the
   capacity path depends only on total cycles (2 then 1 cycles/day ends where 1.5/day does); forced 1
   and 2 cycles/day reproduce the curves; cycling is capped at the highest rate; capacity value and
   degradation cost match finite differences; end-to-end outputs.

11. Battery death: trailing zeros in curves are dropped; the average cycling limits hold; the
    `allow_battery_death` MIP and the `retirement_years` search give the same NPV and lifetime; removed
    or misplaced keys (`eol_fraction`, `degradation_curve_fit`) are rejected.

Tests 2–8 run with `degradation_method = period`.
