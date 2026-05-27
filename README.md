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

### Co-location mode

Uncomment `generation_profile_csv` in the spec to enable co-location mode.
The profile CSV must be a **single-column, headerless** file of capacity factors [0–1],
one row per timestep (same layout as the prices CSV, e.g. `solar_profile_Denmark_1h_2024.csv`).

Available generation per timestep:

```
generation_mwh[t] = capacity_factor[t] × generation_max_mw × interval_hours
```

Two LP constraints are added in co-location mode:

**1. Discharge headroom** — generation occupies part of the grid connection; the BESS can only export what remains:

```
dsch_mwh[t] ≤ connection_mwh - generation_mwh[t]
```

where `connection_mwh = grid_connection_mw × interval_hours` (falls back to `power × interval_hours` if `grid_connection_mw` is not set).

**2. BTM charge tariff split** — charging up to available generation flows behind the meter and is not taxed; only charging above generation incurs `charge_tariff`:

```
taxable_charge[t] = max(0, ch_mwh[t] - generation_mwh[t])
```

Grid import for charging is otherwise unaffected by the generation profile.
The output CSV gains `generation_mw`, `charge_btm_mwh`, and `charge_grid_mwh` columns.

