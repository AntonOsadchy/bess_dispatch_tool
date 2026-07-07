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
| `prices_csv` | yes | Path to single-column price CSV (€/MWh, one row per hour) |
| `output_csv` | yes | Path for the output CSV |
| `power` | yes | Rated AC power (MW) |
| `capacity_mwh` | yes | Usable energy capacity (MWh) |
| `round_trip_efficiency` | yes | Grid-to-grid round-trip efficiency (0–1] |
| `charge_tariff` | no | Extra cost per MWh charged at the grid meter (default 0) |
| `discharge_tariff` | no | Extra cost per MWh discharged at the grid meter (default 0) |
| `max_cycles` | no | Cap on equivalent full cycles over the horizon; comment out to disable |
| `grid_connection_mw` | no | Grid connection limit in MW; caps both imports and exports each timestep |
| `generation_profile_csv` | no | Uncomment to enable co-location mode (single-column headerless capacity-factor CSV) |
| `generation_max_mw` | no* | Nameplate capacity of the co-located generator (MW); required when `generation_profile_csv` is set |

\* Required only when `generation_profile_csv` is active.

### Grid connection limit

When `grid_connection_mw` is set, both `ch_mwh[t]` (grid import) and `dsch_mwh[t]` (grid export)
are bounded by `grid_connection_mw × interval_hours` each timestep.  If the BESS rated power
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
soc_mwh[0] = SOC_INITIAL × capacity_mwh + ch_mwh[0] × η_leg − dsch_mwh[0] / η_leg
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
| `ch_from_gen_avail[t]` | Var | `[0, gen_avail[t]]` (pinned to `min(ch_btm[t], gen_avail[t])` by B5/C6) | BTM charging sourced from exportable generation (discharge-tariff-refund-eligible share) |

Derived (not a separate Pyomo variable, computed from the above): `ch_btm[t] = ch_mwh[t] − ch_grid_mwh[t]` — total behind-the-meter charging in a timestep.

### Modified and additional bounds

`ch_mwh[t]` (**B2**) is redefined: in co-location mode it represents total charging (BTM + grid
combined), and the import cap moves to the new `ch_grid_mwh[t]` variable instead:

```
0 ≤ ch_mwh[t] ≤ power_mw × dt
```

Two additional bounds:

| Label | Constraint | Notes |
|-------|-----------|-------|
| **B4** | `0 ≤ ch_grid_mwh[t] ≤ grid_import_mw × dt` | Grid-imported share of charging bounded by import connection; falls back to `power_mw × dt` if `grid_import_mw` not set |
| **B5** | `0 ≤ ch_from_gen_avail[t] ≤ gen_avail[t]` | BTM charging from exportable generation; upper-bounded by available exportable generation per timestep |

### Additional constraints

**C1 is expanded** — total charging in the SOC balance splits into three sources instead of one:

```
soc_mwh[0] = SOC_INITIAL × capacity_mwh
           + (ch_grid_mwh[0] + ch_from_gen_avail[0] + ch_from_free_gen[0]) × η_leg
           − dsch_mwh[0] / η_leg

soc_mwh[t] = soc_mwh[t-1]
           + (ch_grid_mwh[t] + ch_from_gen_avail[t] + ch_from_free_gen[t]) × η_leg
           − dsch_mwh[t] / η_leg   ∀ t > 0
```

| Term | Source |
|------|--------|
| `ch_grid_mwh[t]` | Grid import |
| `ch_from_gen_avail[t]` | BTM charging from exportable generation |
| `ch_from_free_gen[t]` | BTM charging from curtailed (`gen_curt`) or clipped surplus (`gen_surplus`) generation |

The individual split between `ch_from_gen_avail` and `ch_from_free_gen` is not tracked separately — only their combined BTM share matters for the SOC:

```
ch_btm[t]  =  ch_from_gen_avail[t] + ch_from_free_gen[t]
           =  ch_mwh[t] − ch_grid_mwh[t]
           ≤  gen_avail[t] + gen_curt[t] + gen_surplus[t]
           =  generation_mwh[t]
```

#### Charging sources

In co-location mode total charging `ch_mwh[t]` draws from four distinct sources:

| Source | Definition | Exportable? | Tariff treatment |
|--------|-----------|-------------|-----------------|
| **Grid** | `ch_grid_mwh[t]` | — | `charge_tariff` on import + `discharge_tariff` on eventual export |
| **Exportable BTM generation** | `gen_avail[t] = min(generation_mwh[t] − gen_curt[t], export_connection_dt)` | Yes, but used for BTM charging instead | Both tariffs exempt; opportunity cost: foregone export revenue |
| **Curtailed generation** | `gen_curt[t] = generation_mwh[t]` if `price[t] ≤ curtailment_threshold`, else `0` | No — price too low, would be curtailed | Both tariffs exempt; no opportunity cost (would have been wasted) |
| **Surplus (clipped) generation** | `gen_surplus[t] = max(0, (generation_mwh[t] − gen_curt[t]) − export_connection_dt)` | No — exceeds export connection | Both tariffs exempt; no opportunity cost |

The three BTM sources sum to total available generation:

```
gen_avail[t] + gen_curt[t] + gen_surplus[t] = generation_mwh[t]
```

so the BTM-charged share is bounded by total generation:

```
ch_btm[t]  =  ch_mwh[t] − ch_grid_mwh[t]  ≤  generation_mwh[t]
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

**C6 — Gen-avail BTM charging ceiling**:

```
ch_from_gen_avail[t] ≤ ch_mwh[t] − ch_grid_mwh[t]
```

Combined with B5 this pins `ch_from_gen_avail[t] = min(ch_btm[t], gen_avail[t])`. The optimizer
drives it to this value naturally because `ch_from_gen_avail` earns a `discharge_tariff` refund in
the objective.

### Objective addendum

**O2 — Co-location** replaces O1 (maximise over all timesteps):

Total charging splits into two physically distinct streams:

```
ch_mwh[t]  =  ch_btm[t]  +  ch_grid_mwh[t]
```

where `ch_btm[t] = ch_mwh[t] − ch_grid_mwh[t]` is energy charged behind the meter from generation
(never crossing the import meter, so exempt from both tariffs).

**Full form** — all revenue and cost terms explicit:

```
max  Σ_t [   price[t]          × dsch_mwh[t]                  (1) spot revenue from discharge
           − price[t]          × ch_btm[t]                     (2) spot cost of BTM charging
           − price[t]          × ch_grid_mwh[t]                (3) spot cost of grid charging
           − charge_tariff     × ch_grid_mwh[t]                (4) import tariff, grid share only
           − discharge_tariff  × dsch_mwh[t]                   (5) export tariff on all discharge
           + discharge_tariff  × ch_from_gen_avail[t]  ]       (6) export tariff refund, gen_avail share only
```

Terms (2)+(3) collapse to `− price[t] × ch_mwh[t]`.

**Term (6) applies only to `ch_from_gen_avail`, not to all of `ch_btm`:**
- `gen_avail` charging: BESS absorbs generation that *could* have been exported directly. Discharging it later creates no *new* net export — it merely shifts the timing. Discharge tariff refund is warranted.
- `gen_curt` charging: BESS absorbs generation that would have been curtailed (price too low to export). Discharging it later creates genuinely *new* export that crosses the meter. Discharge tariff applies.
- `gen_surplus` charging: BESS absorbs clipped generation that *cannot* be exported (connection full). Discharging it later creates genuinely *new* export that crosses the meter. Discharge tariff applies.

`charge_tariff` is replaced by `ctariff[t]` when a per-timestep consumption tariff series is supplied.

**Coded form** (as implemented):

```
max  Σ_t [ price[t]         × (dsch_mwh[t] − ch_mwh[t])
         − discharge_tariff × dsch_mwh[t]
         + discharge_tariff × ch_from_gen_avail[t]
         − charge_tariff    × ch_grid_mwh[t] ]
```

Verification:

| Scenario | `ch_grid` | `ch_from_gen_avail` | Net tariff |
|----------|-----------|---------------------|------------|
| Fully BTM from gen_avail | 0 | `ch_mwh` | `discharge_tariff × (ch_mwh − dsch)` → 0 at perfect RTE ✓ |
| Fully BTM from gen_curt or gen_surplus | 0 | 0 | `− discharge_tariff × dsch` (pays export tariff) ✓ |
| Fully grid | `ch_mwh` | 0 | `− discharge_tariff × dsch − charge_tariff × ch_mwh` ✓ |
