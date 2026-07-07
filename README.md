# BESS Dispatch Analyser

Pyomo + HiGHS optimisation of battery energy storage system (BESS) dispatch.
The optimiser reads a spec file and price series from `inputs/` and writes
results to `outputs/dispatch_results.csv`.

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
| `prices_csv` | yes | Path to single-column price CSV (€/MWh, one row per hour); accepts a bracketed list `[a.csv, b.csv]` to run multiple simulations in one go |
| `output_csv` | yes | Path for the output CSV |
| `output_suffix` | no | String appended to output filenames before the extension (e.g. `_v2` → `dispatch_results_v2.csv`); omit or leave blank for the default name |
| `power` | yes | Rated AC power (MW) |
| `capacity_mwh` | yes | Usable energy capacity (MWh) |
| `round_trip_efficiency` | yes | Grid-to-grid round-trip efficiency (0–1] |
| `initial_soc` | no | Initial state of charge as a fraction of `capacity_mwh` [0–1] (default 0.5) |
| `charge_tariff` | no | Extra cost per MWh charged at the grid meter (default 0) |
| `discharge_tariff` | no | Extra cost per MWh discharged at the grid meter (default 0) |
| `curtailment_price` | no | Price threshold (€/MWh) at or below which co-located generation is treated as curtailed; defaults to `discharge_tariff` when omitted |
| `max_cycles` | no | Cap on equivalent full cycles over the horizon; comment out to disable |
| `grid_import_mw` | no | Grid connection import capacity (MW); caps how much the BESS can charge from the grid each timestep |
| `grid_export_mw` | no | Grid connection export capacity (MW); caps how much the BESS can discharge to the grid each timestep |
| `consumption_tariff_csv` | no | Path to a single-column, headerless per-timestep charge tariff CSV (€/MWh); overrides scalar `charge_tariff` for every timestep when set |
| `generation_profile_csv` | no | Uncomment to enable co-location mode (single-column, headerless capacity-factor CSV `[0–1]`, one row per timestep) |
| `generation_max_mw` | no* | Nameplate capacity of the co-located generator (MW); required when `generation_profile_csv` is set |
| `existing_dispatch_profile_csv` | no | Path to a prior run's output CSV (needs `charge_mwh`/`discharge_mwh` columns) that the BESS must honour as a dispatch floor; the optimizer finds additional value on top |

\* Required only when `generation_profile_csv` is active.

### Grid connection limit

`grid_import_mw` bounds `ch_mwh[t]` (grid import) and `grid_export_mw` bounds `dsch_mwh[t]`
(grid export), each to `grid_*_mw × interval_hours` per timestep. If the BESS rated power
(`power`) is lower, the tighter of the two limits applies.

## Stand-alone model

This section describes the LP as it exists with no generation profile configured
(`generation_profile_csv` unset). See [Co-location mode](#co-location-mode) below for what
changes when a co-located generator is added.

### Model variables

Pyomo decision variables and parameters built from the spec/CSV inputs (see `build_and_solve` in `src/bess_dispatch_opt.py`). One entry per timestep `t` unless noted otherwise.

| Name | Kind | Bounds / value | Description |
|---|---|---|---|
| `price[t]` | Param | from `prices_csv` | Spot price (€/MWh) |
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

### Objective

**O1 — Stand-alone** (maximise over all timesteps):

```
max  Σ_t [ price[t] × (dsch_mwh[t] − ch_mwh[t])
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
| `gen_curt[t]` | Param | `generation_mwh[t]` if `price[t] ≤ curtailment_threshold`, else `0` | Generation fully curtailed for the hour (e.g. negative-price hours); would not be exported at all |
| `gen_avail[t]` | Param | `min(generation_mwh[t] − gen_curt[t], export_connection_dt)` | Exportable generation available for BTM charging or direct export |
| `gen_surplus[t]` | Param | `max(0, (generation_mwh[t] − gen_curt[t]) − export_connection_dt)` | Clipped, non-curtailed generation beyond export connection capacity; free BTM charging source |
| `ch_grid_mwh[t]` | Var | `[0, grid_import_mw×dt]` (pinned to `max(0, ch_mwh[t] − gen_avail[t] − gen_curt[t] − gen_surplus[t])` by C4/C5) | Grid-imported share of charging |
| `ch_from_gen_avail[t]` | Var | `[0, gen_avail[t]]` (pinned to `min(ch_btm[t], gen_avail[t])` by B5, C6) | BTM charging sourced from `gen_avail[t]` (discharge-tariff-refund-eligible share) |
| `ch_from_gen_curt[t]` | Var | `[0, gen_curt[t]]` (B6, C6) | BTM charging sourced from `gen_curt[t]` (no discharge-tariff refund) |
| `ch_from_gen_surplus[t]` | Var | `[0, gen_surplus[t]]` (B7, C6) | BTM charging sourced from `gen_surplus[t]` (no discharge-tariff refund) |

Derived (not a separate Pyomo variable): `ch_btm[t] = ch_mwh[t] − ch_grid_mwh[t] = ch_from_gen_avail[t] + ch_from_gen_curt[t] + ch_from_gen_surplus[t]` — total behind-the-meter charging in a timestep, exactly attributed across its three sources by C6.

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

The three BTM terms are pinned by an equality (C6), not just bounded above:

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

**C4 — Lower bound on grid import** — forces `ch_grid_mwh > 0` only when total charging exceeds all three BTM sources:

```
ch_grid_mwh[t] ≥ ch_mwh[t] − gen_avail[t] − gen_curt[t] − gen_surplus[t]
```

Combined with B4 this pins `ch_grid_mwh[t] = max(0, ch_mwh[t] − gen_avail[t] − gen_curt[t] − gen_surplus[t])`,  
where `gen_curt[t]` is fully curtailed generation (price too low to export) and `gen_surplus[t]` is clipped  
generation beyond the export connection — both are free to charge the BESS.  
Rearranged: BTM share `ch_mwh[t] − ch_grid_mwh[t] ≤ gen_avail[t] + gen_curt[t] + gen_surplus[t]`.

**C5 — Grid import ceiling**:

```
ch_grid_mwh[t] ≤ ch_mwh[t]
```

Prevents `ch_grid_mwh` from being inflated when `charge_tariff = 0` gives no cost signal.

**C6 — BTM charging source split**:

```
ch_from_gen_avail[t] + ch_from_gen_curt[t] + ch_from_gen_surplus[t] = ch_mwh[t] − ch_grid_mwh[t]
```

BTM charging is fully attributed across the three sources (equality, not just an upper bound).
Combined with B5 this pins `ch_from_gen_avail[t] = min(ch_btm[t], gen_avail[t])` — the optimizer
drives it to this value naturally because `ch_from_gen_avail` earns a `discharge_tariff` refund in
the objective, while `ch_from_gen_curt[t]` and `ch_from_gen_surplus[t]` split the remainder up to
their own availability (B6, B7).

### Objective addendum

**O2 — Co-location** replaces O1 (maximise over all timesteps):

**Full form** — all revenue and cost terms explicit:

```
max  Σ_t [   price[t]          × dsch_mwh[t]                                     (1) spot revenue from discharge
           − price[t]          × ch_btm[t]                                        (2) spot cost of BTM charging
           − price[t]          × ch_grid_mwh[t]                                   (3) spot cost of grid charging
           + price[t]          × (ch_from_gen_curt[t] + ch_from_gen_surplus[t])   (4) opportunity-cost refund, gen_curt/gen_surplus share only
           − charge_tariff     × ch_grid_mwh[t]                                   (5) import tariff, grid share only
           − discharge_tariff  × dsch_mwh[t]                                      (6) export tariff on all discharge
           + discharge_tariff  × ch_from_gen_avail[t]  ]                          (7) export tariff refund, gen_avail share only
```

Terms (2)+(3)+(4) collapse to `− price[t] × (ch_grid_mwh[t] + ch_from_gen_avail[t])` — the spot term
applies only to the two sources with real opportunity cost.

**Term (4) applies only to `ch_from_gen_curt` and `ch_from_gen_surplus`, not to `ch_from_gen_avail`:**
- `gen_avail` charging: BESS absorbs generation that *could* have been exported directly at `price[t]`. That foregone export revenue is a real opportunity cost, so the spot term is charged in full.
- `gen_curt` / `gen_surplus` charging: BESS absorbs generation that would have been wasted this hour regardless (curtailed, or clipped beyond the export connection). No export was ever possible, so there is no real opportunity cost — the spot term is refunded in full. Without this refund, the plain `price[t] × (dsch_mwh[t] − ch_mwh[t])` term would misprice this energy: at a negative price it would fictitiously *credit* the BESS as if it had been paid to import, even though the energy never crossed the meter.

**Term (7) applies only to `ch_from_gen_avail`, not to all of `ch_btm`:**
- `gen_avail` charging: BESS absorbs generation that *could* have been exported directly. Discharging it later creates no *new* net export — it merely shifts the timing. Discharge tariff refund is warranted.
- `gen_curt` charging: BESS absorbs generation that would have been curtailed (price too low to export). Discharging it later creates genuinely *new* export that crosses the meter. Discharge tariff applies.
- `gen_surplus` charging: BESS absorbs clipped generation that *cannot* be exported (connection full). Discharging it later creates genuinely *new* export that crosses the meter. Discharge tariff applies.

`charge_tariff` is replaced by `ctariff[t]` when a per-timestep consumption tariff series is supplied.

**Coded form** (as implemented):

```
max  Σ_t [ price[t]         × (dsch_mwh[t] − ch_mwh[t])
         + price[t]         × (ch_from_gen_curt[t] + ch_from_gen_surplus[t])
         − discharge_tariff × dsch_mwh[t]
         + discharge_tariff × ch_from_gen_avail[t]
         − charge_tariff    × ch_grid_mwh[t] ]
```
