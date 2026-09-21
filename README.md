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

If `make` isn't installed, use the `docker compose ...` commands above directly —
Docker Desktop alone is enough.

## Project layout

```
.
├── src/bess_dispatch_opt.py   # entry point
├── specification.txt          # default BESS spec (overridable via BESS_SPEC)
├── inputs/                    # price series CSVs and generation profiles
├── outputs/                   # written by the optimiser
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
| `prices_timezone` | no | IANA timezone of the price series (default `Europe/Copenhagen`); only used together with `prices_start_date` |
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

\* Required only when `generation_profile_csv` is active.

At least one of `prices_csv`, `buy_prices_csv`, `sell_prices_csv` must be set (`--write-sample-prices`
needs `prices_csv`). All price series, the consumption tariff and the generation profile must have the
same number of rows.

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
