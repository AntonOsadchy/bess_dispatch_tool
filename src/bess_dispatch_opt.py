"""
Minimal BESS arbitrage LP: Pyomo + HiGHS.
Decision variables ch_mwh / dsch_mwh are grid-side MWh per timestep.
Usable energy capacity is capacity_mwh from specification; state variable soc_mwh is stored energy in MWh.
SOC: Δsoc_mwh = grid_import * η_leg − grid_export / η_leg with η_leg = √(round_trip_efficiency).
Using the spec value on both legs as η (instead of η_leg) would imply grid-to-grid round-trip η², not η.
CSV: grid_import_mwh / grid_export_mwh at the meter; charge_mwh / discharge_mwh are stored-side MWh
(stored gain = grid_import × η_leg; stored loss to deliver export = grid_export / η_leg), so Δsoc_mwh ≈ charge_mwh − discharge_mwh.
Each price CSV row is treated as one hour (see INTERVAL_HOURS).
Optional max equivalent cycles; charge/discharge tariffs apply to grid MWh (same units as prices).

Grid connection limits (optional, independent):
  grid_import_mw  — limits how much the BESS can draw from the grid each interval.
  grid_export_mw  — limits how much the BESS can export to the grid each interval.
  grid_connection_mw — legacy key; sets both import and export to the same value if the
                       split keys are absent (backward compatibility).

Stand-alone mode (no generation profile):
  ch_mwh[t]   ≤ min(power_mw, grid_import_mw)  × dt   (total charging = grid import)
  dsch_mwh[t] ≤ min(power_mw, grid_export_mw) × dt   (total discharging = grid export)

Co-location mode (optional):
When generation_profile_csv and generation_max_mw are provided in the spec, the model treats the
BESS as co-located with a generator.  The profile CSV contains per-timestep capacity factors [0–1];
multiplied by generation_max_mw they give available generation MWh per interval.

Three additional constraints are added in co-location mode:

  1. Discharge headroom (BESS export limited to remaining export connection after generation):
         dsch_mwh[t] ≤ export_connection_mwh - gen_avail[t]
     where export_connection_mwh = grid_export_mw × interval_hours (falls back to power_mw if
     grid_export_mw is not set).  Generation already occupies part of the export connection,
     so the BESS can only use what is left.

  2. Charging power limit:
         ch_mwh[t] ≤ power_mw × dt
     BESS can charge from grid, from generation behind the meter, or a combination. The BESS
     power rating caps total charging regardless of source. Grid import is separately limited
     by grid_import_mw via the ch_grid_mwh auxiliary variable (see below).

   3. Tariff treatment of BTM-charged energy:
      charge_tariff is exempt for all BTM charging (energy never crossed the import meter).
      discharge_tariff treatment differs between the two BTM sources:

        a) gen_avail[t] = min(generation_mwh[t], export_connection_dt)
           Exportable generation used for BTM charging instead of direct export.
           Discharging this energy later replaces export that would have happened anyway
           — no new net export is created, so discharge_tariff is EXEMPT.

        b) surplus_gen[t] = max(0, generation_mwh[t] − export_connection_dt)
           Clipped generation that cannot be exported (connection full).
           Discharging this energy creates NEW export that would not otherwise occur
           — it physically crosses the export meter, so discharge_tariff APPLIES.

      Auxiliary variables:
          ch_grid_mwh[t]:       grid-imported share of charging [B4, C4, C5]
          ch_from_gen_avail[t]: BTM charging from exportable generation only [B5, C6]
                                (the discharge_tariff-exempt portion of BTM charging)

      ch_grid_mwh[t] is pinned to max(0, ch_mwh[t] − gen_avail[t] − surplus_gen[t]).
      ch_from_gen_avail[t] is pinned to min(ch_btm[t], gen_avail[t]).
      The optimizer drives both to their natural values because
          ch_grid_mwh pays (charge_tariff + discharge_tariff) per MWh
          ch_from_gen_avail earns back discharge_tariff per MWh.

     Objective tariff terms in co-location mode:
         − charge_tariff × ch_grid_mwh[t]
         − discharge_tariff × dsch_mwh[t]
         + discharge_tariff × ch_from_gen_avail[t]
     Verification:
         fully BTM gen_avail cycle (ch_grid=0, ch_from_gen_avail=ch_mwh):
             tariff = dsch_t × (ch_mwh − dsch) = 0 for perfect RTE ✓
         fully BTM surplus_gen cycle (ch_grid=0, ch_from_gen_avail=0):
             tariff = −dsch_t × dsch  (pays discharge tariff) ✓
         fully grid cycle (ch_grid=ch_mwh, ch_from_gen_avail=0):
             tariff = −ch_t × ch_mwh − dsch_t × dsch ✓
"""

from __future__ import annotations

import argparse
import math
import random
import sys
from pathlib import Path

import pandas as pd
import pyomo.environ as pyo
from pyomo.opt import SolverStatus, TerminationCondition


SOC_INITIAL = 0.5
# Hours represented by each price row (not read from specification.txt).
INTERVAL_HOURS = 1.0


def parse_spec(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    pending_key: str | None = None
    pending_val: str = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if pending_key is not None:
            # Accumulating a multi-line bracket value; append until the closing ] is found.
            pending_val += " " + line
            if "]" in line:
                out[pending_key] = pending_val.strip()
                pending_key = None
                pending_val = ""
            continue
        if not line or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip()
        if val.startswith("[") and "]" not in val:
            pending_key = key.strip()
            pending_val = val
        else:
            out[key.strip()] = val
    return out


def spec_float(spec: dict[str, str], key: str, default: float | None = None) -> float:
    if key not in spec:
        if default is not None:
            return default
        sys.exit(f"Missing required key in specification.txt: {key}")
    return float(spec[key])


def spec_optional_float(spec: dict[str, str], key: str) -> float | None:
    if key not in spec or not spec[key].strip():
        return None
    return float(spec[key])


def spec_optional_str(spec: dict[str, str], key: str) -> str | None:
    val = spec.get(key, "").strip()
    return val if val else None


def spec_str(spec: dict[str, str], key: str) -> str:
    if key not in spec:
        sys.exit(f"Missing required key in specification.txt: {key}")
    return spec[key]


def parse_spec_list(val: str) -> list[str] | None:
    """Parse '[item1, item2, ...]' into a list of stripped strings, or None if not bracket-syntax."""
    stripped = val.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        items = [item.strip() for item in stripped[1:-1].split(",")]
        return [item for item in items if item]
    return None


def load_prices_csv(path: Path) -> list[float]:
    df = pd.read_csv(path, header=None)
    if df.shape[1] < 1:
        sys.exit(f"No columns in {path}")
    s = pd.to_numeric(df.iloc[:, 0], errors="coerce").dropna()
    if s.empty:
        sys.exit(f"No numeric prices in {path}")
    return s.astype(float).tolist()


def load_profile_csv(path: Path, max_mw: float) -> list[float]:
    """Load a single-column, headerless capacity-factor profile and scale by max_mw.

    The file must contain one numeric value per row representing a capacity factor [0–1]
    (same layout as the prices CSV, e.g. solar_profile_Denmark_1h_2024.csv).
    Values are clipped to [0, 1] before scaling.
    Returns MWh per interval (= capacity_factor × max_mw × INTERVAL_HOURS).
    """
    df = pd.read_csv(path, header=None)
    s = pd.to_numeric(df.iloc[:, 0], errors="coerce").dropna()
    if s.empty:
        sys.exit(f"No numeric values found in profile CSV: {path}")
    cf = s.clip(0.0, 1.0).astype(float)
    return (cf * max_mw * INTERVAL_HOURS).tolist()


def load_tariff_csv(path: Path) -> list[float]:
    """Load a single-column, headerless consumption tariff time-series CSV.

    Each row is the charge tariff in €/MWh for one timestep (same positional layout
    as the prices CSV).  When this series is supplied it replaces the scalar
    charge_tariff from the spec for every timestep.
    """
    df = pd.read_csv(path, header=None)
    if df.shape[1] < 1:
        sys.exit(f"No columns in {path}")
    s = pd.to_numeric(df.iloc[:, 0], errors="coerce").dropna()
    if s.empty:
        sys.exit(f"No numeric tariff values in {path}")
    return s.astype(float).tolist()


def build_and_solve(
    prices: list[float],
    *,
    power_mw: float,
    capacity_mwh: float,
    round_trip_efficiency: float,
    charge_tariff: float,
    discharge_tariff: float,
    max_cycles: float | None,
    generation_mwh: list[float] | None = None,
    grid_import_mw: float | None = None,
    grid_export_mw: float | None = None,
    consumption_tariffs: list[float] | None = None,
) -> tuple[pyo.ConcreteModel, pyo.SolverResults]:
    rte = round_trip_efficiency
    if not (0 < rte <= 1):
        sys.exit("round_trip_efficiency must be in (0, 1]")
    T = len(prices)
    if T == 0:
        sys.exit("Empty price series")

    if generation_mwh is not None:
        if len(generation_mwh) != T:
            sys.exit(
                f"Generation profile length ({len(generation_mwh)}) does not match "
                f"price series length ({T}). Align the two CSVs to the same period."
            )

    if consumption_tariffs is not None:
        if len(consumption_tariffs) != T:
            sys.exit(
                f"Consumption tariff series length ({len(consumption_tariffs)}) does not match "
                f"price series length ({T}). Align the two CSVs to the same period."
            )

    eta_leg = math.sqrt(rte)
    cap = capacity_mwh
    dt = INTERVAL_HOURS

    # Stand-alone mode: ch_mwh = grid import, so apply import cap directly to ch_mwh.
    # Co-location mode: ch_mwh = total charging (BTM + grid); import cap moves to ch_grid_mwh.
    # dsch_mwh is always grid export, so export cap always applies directly to dsch_mwh.
    max_ch_mwh = (
        min(power_mw, grid_import_mw) * dt
        if (grid_import_mw is not None and generation_mwh is None)
        else power_mw * dt
    )
    max_dsch_mwh = (
        min(power_mw, grid_export_mw) * dt
        if grid_export_mw is not None
        else power_mw * dt
    )
    # Import cap applied to ch_grid_mwh in co-location mode.
    max_grid_import_mwh = (
        grid_import_mw * dt if grid_import_mw is not None else power_mw * dt
    )

    times = list(range(T))

    m = pyo.ConcreteModel()
    m.T = pyo.Set(initialize=times)
    m.price = pyo.Param(m.T, initialize={t: prices[t] for t in times})

    # Per-timestep consumption tariff: replaces scalar charge_tariff when supplied.
    if consumption_tariffs is not None:
        m.ctariff = pyo.Param(m.T, initialize={t: consumption_tariffs[t] for t in times})

    m.soc_mwh = pyo.Var(m.T, bounds=(0.0, cap))
    # ch_mwh: total charging (stand-alone: = grid import; co-location: BTM + grid).
    m.ch_mwh = pyo.Var(m.T, bounds=(0.0, max_ch_mwh))
    # dsch_mwh: grid export, always bounded by min(power_mw, grid_export_mw).
    m.dsch_mwh = pyo.Var(m.T, bounds=(0.0, max_dsch_mwh))

    # Constraints [B1-B4, C1-C5] and objective [O1-O2] — see README.md § "Optimisation model"

    def soc_rule(mm, i):
        if i == 0:
            return (
                mm.soc_mwh[0] - SOC_INITIAL * cap
                == mm.ch_mwh[0] * eta_leg - mm.dsch_mwh[0] / eta_leg
            )
        return (
            mm.ch_mwh[i] * eta_leg - mm.dsch_mwh[i] / eta_leg + mm.soc_mwh[i - 1]
            == mm.soc_mwh[i]
        )

    m.soc_cons = pyo.Constraint(m.T, rule=soc_rule)  # [C1]

    if max_cycles is not None:

        def cycle_cap_rule(mm):
            return eta_leg * sum(mm.ch_mwh[t] for t in times) <= max_cycles * cap

        m.cycle_cap = pyo.Constraint(rule=cycle_cap_rule)  # [C2]

    # Co-location constraints.
    if generation_mwh is not None:
        # Export connection reference: grid_export_mw if set, otherwise power_mw.
        export_connection_dt = (
            grid_export_mw if grid_export_mw is not None else power_mw
        ) * dt

        # Cap generation at export connection capacity so discharge headroom never goes negative.
        gen_param = {t: min(generation_mwh[t], export_connection_dt) for t in times}
        m.gen_avail = pyo.Param(m.T, initialize=gen_param)

        # Surplus generation: clipped portion that cannot be exported (free BTM charging source).
        # surplus_gen[t] = max(0, generation_mwh[t] − export_connection_dt)
        surplus_param = {t: max(0.0, generation_mwh[t] - export_connection_dt) for t in times}
        m.surplus_gen = pyo.Param(m.T, initialize=surplus_param)

        def colocation_rule(mm, t):
            return mm.dsch_mwh[t] <= export_connection_dt - mm.gen_avail[t]

        m.colocation_cons = pyo.Constraint(m.T, rule=colocation_rule)  # [C3]

        # ch_grid_mwh[t]: grid-imported share of charging, bounded by import connection [B4].
        m.ch_grid_mwh = pyo.Var(m.T, bounds=(0.0, max_grid_import_mwh))

        def ch_grid_lb_rule(mm, t):
            # Grid import is only needed when charging exceeds both BTM sources
            # (exportable generation gen_avail[t] and free surplus surplus_gen[t]).
            return mm.ch_grid_mwh[t] >= mm.ch_mwh[t] - mm.gen_avail[t] - mm.surplus_gen[t]

        m.ch_grid_lb = pyo.Constraint(m.T, rule=ch_grid_lb_rule)  # [C4]

        def ch_grid_ub_rule(mm, t):
            return mm.ch_grid_mwh[t] <= mm.ch_mwh[t]

        m.ch_grid_ub = pyo.Constraint(m.T, rule=ch_grid_ub_rule)  # [C5]

        # ch_from_gen_avail[t]: BTM charging from exportable generation (gen_avail portion only).
        # Discharge of this share is discharge_tariff-exempt; surplus_gen discharge is not [B5].
        m.ch_from_gen_avail = pyo.Var(
            m.T, bounds=lambda mm, t: (0.0, pyo.value(mm.gen_avail[t]))
        )

        def ch_gen_avail_btm_rule(mm, t):
            # Cannot exceed total BTM charging (ch_mwh - ch_grid_mwh = ch_btm).
            return mm.ch_from_gen_avail[t] <= mm.ch_mwh[t] - mm.ch_grid_mwh[t]

        m.ch_gen_avail_btm = pyo.Constraint(m.T, rule=ch_gen_avail_btm_rule)  # [C6]

    def profit_rule(mm):
        if generation_mwh is not None:
            # charge_tariff exempt for all BTM charging (no import meter crossing).
            # discharge_tariff refund applies only to ch_from_gen_avail (gen_avail-sourced BTM):
            #   gen_avail discharge replaces would-have-happened direct export → no new net export.
            #   surplus_gen discharge creates new export → discharge_tariff applies.
            if consumption_tariffs is not None:
                return sum(
                    mm.price[t] * (mm.dsch_mwh[t] - mm.ch_mwh[t])
                    - discharge_tariff * mm.dsch_mwh[t]
                    + discharge_tariff * mm.ch_from_gen_avail[t]
                    - mm.ctariff[t] * mm.ch_grid_mwh[t]
                    for t in times
                )
            return sum(
                mm.price[t] * (mm.dsch_mwh[t] - mm.ch_mwh[t])
                - discharge_tariff * mm.dsch_mwh[t]
                + discharge_tariff * mm.ch_from_gen_avail[t]
                - charge_tariff * mm.ch_grid_mwh[t]
                for t in times
            )
        if consumption_tariffs is not None:
            # Per-timestep tariff replaces scalar charge_tariff.
            return sum(
                mm.price[t] * (mm.dsch_mwh[t] - mm.ch_mwh[t])
                - discharge_tariff * mm.dsch_mwh[t]
                - mm.ctariff[t] * mm.ch_mwh[t]
                for t in times
            )
        return sum(
            mm.price[t] * (mm.dsch_mwh[t] - mm.ch_mwh[t])
            - discharge_tariff * mm.dsch_mwh[t]
            - charge_tariff * mm.ch_mwh[t]
            for t in times
        )

    m.obj = pyo.Objective(rule=profit_rule, sense=pyo.maximize)

    solver = pyo.SolverFactory("appsi_highs")
    if not solver.available(False):
        sys.exit("HiGHS solver not available. Install highspy and use Pyomo appsi_highs.")
    results = solver.solve(m)
    return m, results


def write_output(
    path: Path,
    prices: list[float],
    model: pyo.ConcreteModel,
    *,
    capacity_mwh: float,
    round_trip_efficiency: float,
    charge_tariff: float,
    discharge_tariff: float,
    generation_mwh: list[float] | None = None,
    consumption_tariffs: list[float] | None = None,
    surplus_generation_mwh: list[float] | None = None,
) -> None:
    eta_leg = math.sqrt(round_trip_efficiency)
    rows = []
    cumulative_revenue = 0.0
    for t in range(len(prices)):
        ch_total = pyo.value(model.ch_mwh[t])   # total charging (BTM + grid in co-loc; grid-only stand-alone)
        dsch_grid = pyo.value(model.dsch_mwh[t])
        ch_stored = ch_total * eta_leg
        dsch_stored = dsch_grid / eta_leg
        soc_mwh_val = pyo.value(model.soc_mwh[t])
        soc_frac = soc_mwh_val / capacity_mwh
        p = prices[t]
        # Effective charge tariff for this timestep: per-timestep series takes precedence.
        eff_charge_tariff = consumption_tariffs[t] if consumption_tariffs is not None else charge_tariff

        if generation_mwh is not None:
            # Taxable share of charging: grid-imported portion (above available BTM generation).
            ch_grid_taxable = pyo.value(model.ch_grid_mwh[t])
            ch_btm = ch_total - ch_grid_taxable          # behind-the-meter share (untaxed)
            ch_from_gen_avail_t = pyo.value(model.ch_from_gen_avail[t])
            revenue = (
                p * (dsch_grid - ch_total)
                - discharge_tariff * dsch_grid
                + discharge_tariff * ch_from_gen_avail_t
                - eff_charge_tariff * ch_grid_taxable
            )
        else:
            # Stand-alone: ch_mwh is purely grid import.
            ch_grid_taxable = ch_total
            ch_btm = 0.0
            ch_from_gen_avail_t = 0.0
            revenue = (
                p * (dsch_grid - ch_total)
                - discharge_tariff * dsch_grid
                - eff_charge_tariff * ch_total
            )

        cumulative_revenue += revenue

        # Co-location columns — computed for all rows; zero-filled when feature is disabled.
        if generation_mwh is not None:
            gen_mwh_t = generation_mwh[t]
            threshold = discharge_tariff
            curtailed = p <= threshold
            gen_gen_curtailed = 0.0 if curtailed else gen_mwh_t
            pv_net_export = max(0.0, gen_gen_curtailed - ch_btm)
            row_generation_mw               = gen_mwh_t / INTERVAL_HOURS
            row_charge_btm_mwh              = ch_btm
            row_charge_grid_mwh             = ch_grid_taxable
            row_charge_surplus_mwh          = min(ch_btm, surplus_generation_mwh[t]) if surplus_generation_mwh is not None else 0.0
            row_generation_mwh              = gen_mwh_t
            row_generation_rev_uncurtailed  = (p - discharge_tariff) * gen_mwh_t
            row_generation_curtailed_mwh    = gen_gen_curtailed
            row_generation_rev_curtailed    = 0.0 if curtailed else (p - discharge_tariff) * gen_mwh_t
            row_pv_net_export_mwh           = pv_net_export
            row_total_export_mwh            = pv_net_export + dsch_grid
        else:
            row_generation_mw               = 0.0
            row_charge_btm_mwh              = 0.0
            row_charge_grid_mwh             = ch_grid_taxable
            row_charge_surplus_mwh          = 0.0
            row_generation_mwh              = 0.0
            row_generation_rev_uncurtailed  = 0.0
            row_generation_curtailed_mwh    = 0.0
            row_generation_rev_curtailed    = 0.0
            row_pv_net_export_mwh           = 0.0
            row_total_export_mwh            = dsch_grid

        row: dict = {
            "price": p,
            "soc": soc_frac,
            "soc_mwh": soc_mwh_val,
            # grid_import_mwh: actual meter-crossing import (= ch_grid_taxable in co-loc; ch_total stand-alone)
            "grid_import_mwh": ch_grid_taxable,
            "grid_export_mwh": dsch_grid,
            "charge_mwh": ch_stored,
            "discharge_mwh": dsch_stored,
            "objective": revenue,
            "revenue": revenue,
            "cumulative_revenue": cumulative_revenue,
            "generation_mw": row_generation_mw,
            "charge_btm_mwh": row_charge_btm_mwh,
            "charge_grid_mwh": row_charge_grid_mwh,
            "charge_surplus_mwh": row_charge_surplus_mwh,
            "generation_mwh": row_generation_mwh,
            "generation_revenue_uncurtailed": row_generation_rev_uncurtailed,
            "generation_curtailed_mwh": row_generation_curtailed_mwh,
            "generation_revenue_curtailed": row_generation_rev_curtailed,
            "pv_net_export_mwh": row_pv_net_export_mwh,
            "total_export_mwh": row_total_export_mwh,
        }
        rows.append(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False)


def write_sample_prices(path: Path, n: int, seed: int | None) -> None:
    rng = random.Random(seed)
    values = [rng.uniform(20.0, 120.0) for _ in range(n)]
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(values).to_csv(path, index=False, header=False)


def main() -> None:
    ap = argparse.ArgumentParser(description="BESS dispatch optimisation (Pyomo + HiGHS).")
    ap.add_argument(
        "--spec",
        type=Path,
        default=Path("specification.txt"),
        help="Path to specification.txt",
    )
    ap.add_argument(
        "--write-sample-prices",
        type=int,
        metavar="N",
        help="Write N random prices (single column) to prices_csv from spec, then exit",
    )
    ap.add_argument(
        "--sample-seed",
        type=int,
        default=None,
        help="RNG seed for --write-sample-prices",
    )
    args = ap.parse_args()

    if not args.spec.is_file():
        sys.exit(f"Spec file not found: {args.spec}")

    spec = parse_spec(args.spec)

    prices_csv_raw = spec_str(spec, "prices_csv")
    prices_paths_raw = parse_spec_list(prices_csv_raw) or [prices_csv_raw]

    if args.write_sample_prices is not None:
        n = args.write_sample_prices
        if n < 1:
            sys.exit("N must be >= 1")
        out_p = Path(prices_paths_raw[0])
        write_sample_prices(out_p, n, args.sample_seed)
        print(f"Wrote {n} sample prices to {out_p.resolve()}")
        return

    output_path_base = Path(spec_str(spec, "output_csv"))
    output_suffix = spec_optional_str(spec, "output_suffix") or ""
    multi_mode = len(prices_paths_raw) > 1

    power_mw = spec_float(spec, "power")
    rte = spec_float(spec, "round_trip_efficiency")
    charge_tariff = spec_float(spec, "charge_tariff", default=0.0)
    discharge_tariff = spec_float(spec, "discharge_tariff", default=0.0)
    max_cycles = spec_optional_float(spec, "max_cycles")
    capacity_mwh = spec_float(spec, "capacity_mwh")

    # Grid connection: split import / export limits.
    # grid_import_mw  — caps how much the BESS can draw from the grid (charging).
    # grid_export_mw  — caps how much the BESS can push to the grid (discharging).
    # grid_connection_mw — legacy key: sets both if neither split key is present.
    grid_import_mw = spec_optional_float(spec, "grid_import_mw")
    grid_export_mw = spec_optional_float(spec, "grid_export_mw")
    grid_connection_mw = spec_optional_float(spec, "grid_connection_mw")
    if grid_import_mw is None and grid_export_mw is None and grid_connection_mw is not None:
        grid_import_mw = grid_connection_mw
        grid_export_mw = grid_connection_mw

    # Optional per-timestep consumption tariff CSV (overrides scalar charge_tariff when set).
    consumption_tariff_csv = spec_optional_str(spec, "consumption_tariff_csv")
    consumption_tariffs: list[float] | None = None

    # Co-location: enabled by uncommenting generation_profile_csv in the spec.
    # generation_max_mw must also be set when the profile is active.
    gen_profile_csv = spec.get("generation_profile_csv", "").strip()
    generation_mwh: list[float] | None = None
    if gen_profile_csv:
        gen_max_mw_raw = spec.get("generation_max_mw", "").strip()
        if not gen_max_mw_raw:
            sys.exit(
                "generation_profile_csv is set but generation_max_mw is missing. "
                "Add generation_max_mw to the spec."
            )
        gen_max_mw = float(gen_max_mw_raw)
        if gen_max_mw <= 0:
            sys.exit("generation_max_mw must be positive")
        generation_mwh = load_profile_csv(Path(gen_profile_csv), gen_max_mw)

    # Surplus generation: clipped portion that cannot be exported (free BTM charging for BESS).
    # Computed here (after grid_export_mw is resolved) and passed through to write_output.
    # surplus_gen[t] = max(0, generation_mwh[t] − export_connection_dt)
    surplus_generation_mwh: list[float] | None = None
    if generation_mwh is not None:
        export_connection_dt_main = (
            grid_export_mw if grid_export_mw is not None else power_mw
        ) * INTERVAL_HOURS
        surplus_generation_mwh = [
            max(0.0, g - export_connection_dt_main) for g in generation_mwh
        ]

    if power_mw <= 0:
        sys.exit("power must be positive")
    if capacity_mwh <= 0:
        sys.exit("capacity_mwh must be positive")
    if grid_import_mw is not None and grid_import_mw < 0:
        sys.exit("grid_import_mw must be >= 0")
    if grid_export_mw is not None and grid_export_mw < 0:
        sys.exit("grid_export_mw must be >= 0")
    if max_cycles is not None and max_cycles < 0:
        sys.exit("max_cycles must be non-negative")

    if consumption_tariff_csv is not None:
        consumption_tariffs = load_tariff_csv(Path(consumption_tariff_csv))

    for i, prices_path_str in enumerate(prices_paths_raw):
        prices_path = Path(prices_path_str)

        if multi_mode:
            print(f"\n{'=' * 60}")
            print(f"Simulation {i + 1}/{len(prices_paths_raw)}: {prices_path.name}")
            print(f"{'=' * 60}")
            output_path = output_path_base.parent / (
                output_path_base.stem + "_" + prices_path.stem + output_suffix + output_path_base.suffix
            )
        else:
            output_path = output_path_base.parent / (
                output_path_base.stem + output_suffix + output_path_base.suffix
            )

        prices = load_prices_csv(prices_path)

        if consumption_tariffs is not None and len(consumption_tariffs) != len(prices):
            sys.exit(
                f"Consumption tariff series length ({len(consumption_tariffs)}) does not match "
                f"price series length ({len(prices)}) for {prices_path}. Align the two CSVs to the same period."
            )

        model, results = build_and_solve(
            prices,
            power_mw=power_mw,
            capacity_mwh=capacity_mwh,
            round_trip_efficiency=rte,
            charge_tariff=charge_tariff,
            discharge_tariff=discharge_tariff,
            max_cycles=max_cycles,
            generation_mwh=generation_mwh,
            grid_import_mw=grid_import_mw,
            grid_export_mw=grid_export_mw,
            consumption_tariffs=consumption_tariffs,
        )

        ok = (
            results.solver.status == SolverStatus.ok
            and results.solver.termination_condition == TerminationCondition.optimal
        )
        if not ok:
            sys.exit(
                f"Solver did not finish optimally: status={results.solver.status} "
                f"termination={results.solver.termination_condition}"
            )

        total_profit = pyo.value(model.obj)
        Tn = len(prices)

        # Single pass over all timesteps — compute all summary stats together.
        total_export_mwh    = 0.0
        total_export_revenue = 0.0
        total_charge_mwh    = 0.0
        total_charge_cost   = 0.0   # spot cost + charge tariff on grid-imported share only
        total_dsch_profit   = 0.0   # spot revenue minus net discharge tariff (with BTM refund)
        spot_gross          = 0.0
        tariff_component    = 0.0

        for t in range(Tn):
            p        = prices[t]
            dsch     = pyo.value(model.dsch_mwh[t])
            ch_total = pyo.value(model.ch_mwh[t])
            eff_ct   = consumption_tariffs[t] if consumption_tariffs is not None else charge_tariff

            if generation_mwh is not None:
                ch_grid              = pyo.value(model.ch_grid_mwh[t])
                ch_btm               = ch_total - ch_grid
                ch_from_gen_avail_t  = pyo.value(model.ch_from_gen_avail[t])
            else:
                ch_grid              = ch_total
                ch_btm               = 0.0
                ch_from_gen_avail_t  = 0.0

            total_export_mwh     += dsch
            total_export_revenue += p * dsch
            total_charge_mwh     += ch_total
            # Charging cost: spot + charge tariff on grid-imported share, plus spot opportunity
            # cost on BTM share (generation that could have been exported at spot price instead).
            total_charge_cost    += (p + eff_ct) * ch_grid + p * ch_btm
            # Discharge profit: spot revenue minus discharge tariff; only gen_avail-sourced BTM
            # gets the refund (surplus_gen discharge creates new export, so tariff applies).
            total_dsch_profit    += p * dsch - discharge_tariff * (dsch - ch_from_gen_avail_t)
            spot_gross           += p * (dsch - ch_total)

            if generation_mwh is not None:
                tariff_component += (
                    discharge_tariff * dsch
                    - discharge_tariff * ch_from_gen_avail_t
                    + eff_ct * ch_grid
                )
            else:
                tariff_component += eff_ct * ch_total + discharge_tariff * dsch

        weighted_avg_export_price = (
            total_export_revenue / total_export_mwh if total_export_mwh > 1e-12 else float("nan")
        )
        weighted_avg_charge_cost = (
            total_charge_cost / total_charge_mwh if total_charge_mwh > 1e-12 else float("nan")
        )
        weighted_avg_dsch_profit = (
            total_dsch_profit / total_export_mwh if total_export_mwh > 1e-12 else float("nan")
        )
        validation_profit = (
            total_export_mwh * weighted_avg_dsch_profit
            - total_charge_mwh * weighted_avg_charge_cost
            if not (math.isnan(weighted_avg_dsch_profit) or math.isnan(weighted_avg_charge_cost))
            else float("nan")
        )
        if abs(spot_gross - tariff_component - total_profit) > 1e-4 * max(1.0, abs(total_profit)):
            print(
                "Warning: objective does not match spot revenue minus tariffs; check model.",
                flush=True,
            )

        eta_leg = math.sqrt(rte)
        n_cycles = (
            eta_leg * sum(pyo.value(model.ch_mwh[t]) for t in range(len(prices))) / capacity_mwh
        )
        if n_cycles > 1e-12:
            profit_per_cycle = total_profit / n_cycles
        else:
            profit_per_cycle = float("nan")
        # Profit normalised to 365 cycles: total profit divided by 365.
        profit_365_cycles_normalized = total_profit / 365.0

        # Total energy charged from surplus (clipped) generation — co-location only.
        total_surplus_charged_mwh: float | None = None
        if surplus_generation_mwh is not None:
            eta_leg_val = math.sqrt(rte)
            total_surplus_charged_mwh = 0.0
            for t in range(len(prices)):
                ch_total = pyo.value(model.ch_mwh[t])
                ch_grid = pyo.value(model.ch_grid_mwh[t])
                ch_btm = ch_total - ch_grid
                total_surplus_charged_mwh += min(ch_btm, surplus_generation_mwh[t])

        write_output(
            output_path,
            prices,
            model,
            capacity_mwh=capacity_mwh,
            round_trip_efficiency=rte,
            charge_tariff=charge_tariff,
            discharge_tariff=discharge_tariff,
            generation_mwh=generation_mwh,
            consumption_tariffs=consumption_tariffs,
            surplus_generation_mwh=surplus_generation_mwh,
        )

        # -------------------------------------------------------------------------
        # Build report lines (written to terminal and .txt file)
        # -------------------------------------------------------------------------
        report_lines: list[str] = []

        # --- Inputs summary ---
        report_lines.append("--- Inputs ---")
        report_lines.append(f"  Prices CSV                           : {prices_path}")
        report_lines.append(f"  Timesteps                            : {len(prices):>10d} h")
        report_lines.append(f"  Price mean                           : {sum(prices)/len(prices):>10.2f} €/MWh")
        report_lines.append(f"  Price min                            : {min(prices):>10.2f} €/MWh")
        report_lines.append(f"  Price max                            : {max(prices):>10.2f} €/MWh")
        report_lines.append(f"  Hours with negative price            : {sum(1 for p in prices if p < 0):>10d} h")
        if consumption_tariff_csv is not None:
            ct_mean = sum(consumption_tariffs) / len(consumption_tariffs)
            ct_min  = min(consumption_tariffs)
            ct_max  = max(consumption_tariffs)
            report_lines.append(f"  Charge tariff (mean / min / max)     : {ct_mean:>6.2f} / {ct_min:.2f} / {ct_max:.2f} €/MWh")
        else:
            report_lines.append(f"  Charge tariff                        : {charge_tariff:>10.2f} €/MWh")
        report_lines.append(f"  Discharge tariff                     : {discharge_tariff:>10.2f} €/MWh")
        report_lines.append("")
        report_lines.append(f"  BESS power                           : {power_mw:>10.2f} MW")
        report_lines.append(f"  BESS capacity                        : {capacity_mwh:>10.2f} MWh")
        report_lines.append(f"  Round-trip efficiency                : {rte*100:>10.1f} %")
        report_lines.append(f"  Grid import cap                      : {grid_import_mw if grid_import_mw is not None else power_mw:>10.2f} MW")
        report_lines.append(f"  Grid export cap                      : {grid_export_mw if grid_export_mw is not None else power_mw:>10.2f} MW")
        report_lines.append(f"  Max cycles                           : {'unlimited' if max_cycles is None else f'{max_cycles:>6.0f}':>10}")
        report_lines.append("")
        if generation_mwh is not None:
            gen_total = sum(generation_mwh)
            gen_peak  = max(generation_mwh)
            gen_hours = sum(1 for g in generation_mwh if g > 0)
            capacity_factor = gen_total / (gen_max_mw * len(generation_mwh))
            report_lines.append(f"  Generation profile CSV               : {gen_profile_csv}")
            report_lines.append(f"  Generation nameplate capacity        : {gen_max_mw:>10.2f} MW")
            report_lines.append(f"  Annual generation                    : {gen_total:>10.2f} MWh")
            report_lines.append(f"  Peak output                          : {gen_peak:>10.2f} MW")
            report_lines.append(f"  Generating hours                     : {gen_hours:>10d} h")
            report_lines.append(f"  Capacity factor                      : {capacity_factor*100:>10.1f} %")
        else:
            report_lines.append("  Generation profile CSV               :   disabled")
            report_lines.append(f"  Generation nameplate capacity        : {0.0:>10.2f} MW")
            report_lines.append(f"  Annual generation                    : {0.0:>10.2f} MWh")
            report_lines.append(f"  Peak output                          : {0.0:>10.2f} MW")
            report_lines.append(f"  Generating hours                     : {0:>10d} h")
            report_lines.append(f"  Capacity factor                      : {0.0:>10.1f} %")

        report_lines.append("")
        report_lines.append("--- BESS Results ---")
        report_lines.append(f"  Spot revenue (gross, before tariffs) : {spot_gross:>10.2f} €")
        report_lines.append(f"  Tariff charges                       : {tariff_component:>10.2f} €")
        report_lines.append(f"  Total profit                         : {total_profit:>10.2f} €")
        report_lines.append(f"  Charging volume                      : {total_charge_mwh:>10.2f} MWh")
        if math.isnan(weighted_avg_charge_cost):
            report_lines.append("  Weighted avg charging cost           :        n/a  €/MWh")
        else:
            report_lines.append(f"  Weighted avg charging cost           : {weighted_avg_charge_cost:>10.2f} €/MWh")
        report_lines.append(f"  Discharging volume                   : {total_export_mwh:>10.2f} MWh")
        if math.isnan(weighted_avg_dsch_profit):
            report_lines.append("  Weighted avg discharge profit        :        n/a  €/MWh")
        else:
            report_lines.append(f"  Weighted avg discharge profit        : {weighted_avg_dsch_profit:>10.2f} €/MWh")
        if math.isnan(validation_profit):
            report_lines.append("  Vol × avg price cross-check          :        n/a  €")
        else:
            report_lines.append(f"  Vol × avg price cross-check          : {validation_profit:>10.2f} €")
        report_lines.append(f"  Equivalent full cycles               : {n_cycles:>10.2f} cycles")
        if math.isnan(profit_per_cycle):
            report_lines.append("  Profit per cycle                     :        n/a  €/cycle")
            report_lines.append("  Profit per cycle per MW              :        n/a  €/cycle/MW")
        else:
            report_lines.append(f"  Profit per cycle                     : {profit_per_cycle:>10.2f} €/cycle")
            report_lines.append(f"  Profit per cycle per MW              : {profit_per_cycle / power_mw:>10.2f} €/cycle/MW")
        report_lines.append(f"  Profit / 365 cycles (normalised)     : {profit_365_cycles_normalized:>10.2f} €/cycle")
        report_lines.append(f"  Profit / 365 cycles per MW           : {profit_365_cycles_normalized / power_mw:>10.2f} €/cycle/MW")
        report_lines.append(f"  BESS charged from surplus generation : {total_surplus_charged_mwh if total_surplus_charged_mwh is not None else 0.0:>10.2f} MWh")

        # Combined system (BESS + generation).
        if generation_mwh is not None:
            vol_uncurtailed = 0.0
            rev_uncurtailed = 0.0
            vol_curtailed = 0.0
            rev_curtailed = 0.0
            for t, gen_mwh_t in enumerate(generation_mwh):
                p = prices[t]
                vol_uncurtailed += gen_mwh_t
                rev_uncurtailed += (p - discharge_tariff) * gen_mwh_t
                if p > discharge_tariff:
                    vol_curtailed += gen_mwh_t
                    rev_curtailed += (p - discharge_tariff) * gen_mwh_t
            capture_price_uncurtailed = rev_uncurtailed / vol_uncurtailed if vol_uncurtailed > 1e-12 else float("nan")
            capture_price_curtailed   = rev_curtailed / vol_curtailed if vol_curtailed > 1e-12 else float("nan")
            combined_export_mwh     = total_export_mwh + vol_curtailed
            combined_export_revenue = total_export_revenue + rev_curtailed
            combined_avg_export_price = combined_export_revenue / combined_export_mwh if combined_export_mwh > 1e-12 else float("nan")
        else:
            vol_uncurtailed = rev_uncurtailed = vol_curtailed = rev_curtailed = 0.0
            capture_price_uncurtailed = capture_price_curtailed = float("nan")
            combined_export_mwh     = total_export_mwh
            combined_export_revenue = total_export_revenue
            combined_avg_export_price = weighted_avg_export_price

        report_lines.append("")
        report_lines.append("--- Combined System (BESS + Generation) ---")
        report_lines.append(f"  {'Source':<36} : {'Volume (MWh)':>12} | {'Avg price (€/MWh)':>17}")
        report_lines.append(f"  {'Direct generation export (curtailed)':<36} : {vol_curtailed:>12.2f} | {capture_price_curtailed:>17.2f}")
        report_lines.append(f"  {'BESS export':<36} : {total_export_mwh:>12.2f} | {weighted_avg_export_price:>17.2f}")
        report_lines.append(f"  {'Combined':<36} : {combined_export_mwh:>12.2f} | {combined_avg_export_price:>17.2f}")

        report_lines.append("")
        report_lines.append("--- Generation Revenue Report ---")
        report_lines.append("Uncurtailed (production at all prices, including negative):")
        report_lines.append(f"  Generation volume                    : {vol_uncurtailed:>10.2f} MWh")
        report_lines.append(f"  Total revenue                        : {rev_uncurtailed:>10.2f} €")
        report_lines.append(f"  Capture price                        : {0.0 if math.isnan(capture_price_uncurtailed) else capture_price_uncurtailed:>10.2f} €/MWh")
        report_lines.append("")
        report_lines.append("Curtailed (no production when price ≤ tariff):")
        report_lines.append(f"  Generation volume                    : {vol_curtailed:>10.2f} MWh")
        report_lines.append(f"  Total revenue                        : {rev_curtailed:>10.2f} €")
        report_lines.append(f"  Capture price                        : {0.0 if math.isnan(capture_price_curtailed) else capture_price_curtailed:>10.2f} €/MWh")

        # Print to terminal
        print()
        for line in report_lines:
            print(line)

        # Write to .txt file alongside the CSV output
        report_path = output_path.with_suffix(".txt")
        report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")

        # Write Excel workbook: two sheets — per-timestep dispatch data and text report.
        # Report sheet: split "  Label : value" lines into col A (label) / col B (value).
        # Two-value lines "  Label : val1 | val2" additionally populate col C.
        excel_path = output_path.with_suffix(".xlsx")

        def _try_numeric(s: str) -> float | str:
            """Return float if the first whitespace-separated token is numeric, else the raw string."""
            try:
                return float(s.strip().split()[0].replace(",", ""))
            except (ValueError, IndexError):
                return s.strip()

        report_rows: list[tuple] = []
        for line in report_lines:
            if " : " in line:
                label, _, rest = line.partition(" : ")
                if " | " in rest:
                    left, _, right = rest.partition(" | ")
                    report_rows.append((label.rstrip(), _try_numeric(left), _try_numeric(right)))
                else:
                    report_rows.append((label.rstrip(), _try_numeric(rest), None))
            else:
                report_rows.append((line, None, None))

        # Excel sheet names are capped at 31 chars; derive prefix from prices filename stem + suffix.
        _prefix = (prices_path.stem + output_suffix)[:22]  # leave room for " Dispatch" (9 chars)
        sheet_dispatch = _prefix + " Dispatch"
        sheet_results  = _prefix + " Results"
        with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
            pd.read_csv(output_path).to_excel(writer, sheet_name=sheet_dispatch, index=False)
            pd.DataFrame(report_rows, columns=["Label", "Value", "Value2"]).to_excel(
                writer, sheet_name=sheet_results, index=False, header=False
            )

        print(f"\nWrote {output_path.resolve()}")
        print(f"Wrote {report_path.resolve()}")
        print(f"Wrote {excel_path.resolve()}")


if __name__ == "__main__":
    main()
