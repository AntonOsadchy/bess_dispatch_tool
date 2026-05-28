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

  3. Round-trip tariff relief for BTM-charged energy:
     Energy charged behind the meter (from generation, up to gen_avail[t]) avoids both the
     charge_tariff on import and the discharge_tariff on its eventual export — it never crossed
     the import meter and its discharge is treated as unmetered.
     Auxiliary variable ch_grid_mwh[t] = max(0, ch_mwh[t] − gen_avail[t]) tracks the
     taxable (grid-imported) share of charging. It is bounded above by grid_import_mw × dt
     (the import connection cap) and pinned by:
         ch_grid_mwh[t] ≥ ch_mwh[t] − gen_avail[t]   (active when ch > gen; forces grid import)
         ch_grid_mwh[t] ≤ ch_mwh[t]                   (grid share ≤ total charging)
         ch_grid_mwh[t] ≥ 0                            (from variable bound)
     The BTM share ch_btm[t] = ch_mwh[t] − ch_grid_mwh[t] ≤ gen_avail[t] follows from the LB.
     Objective tariff terms in co-location mode:
         − (charge_tariff + discharge_tariff) × ch_grid_mwh[t]
         − discharge_tariff × (dsch_mwh[t] − ch_mwh[t])
     Verification: fully BTM cycle (ch_grid=0): total tariff = 0 ✓
                   fully grid cycle (ch_grid=ch_mwh): total tariff = charge_tariff + discharge_tariff ✓
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
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[key.strip()] = val.strip()
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

    # -------------------------------------------------------------------------
    # Constraints and objective
    # -------------------------------------------------------------------------
    #
    # Variable bounds (enforced implicitly by Pyomo Var bounds):
    #   [B1]  0 <= soc_mwh[t] <= capacity_mwh
    #         (SOC within usable battery limits)
    #
    #   [B2]  0 <= ch_mwh[t] <= min(power_mw, grid_import_mw) × dt   [stand-alone]
    #         0 <= ch_mwh[t] <= power_mw × dt                         [co-location]
    #         (total charging bounded by BESS power rating;
    #          in stand-alone grid import cap applied here directly;
    #          in co-location import cap moves to ch_grid_mwh [B4])
    #
    #   [B3]  0 <= dsch_mwh[t] <= min(power_mw, grid_export_mw) × dt
    #         (grid export bounded by BESS power rating and export connection)
    #
    # Explicit constraints (all modes):
    #   [C1]  soc_mwh[0] = SOC_INITIAL × capacity_mwh
    #                     + ch_mwh[0] × η_leg − dsch_mwh[0] / η_leg
    #         soc_mwh[t] = soc_mwh[t-1]
    #                     + ch_mwh[t] × η_leg − dsch_mwh[t] / η_leg   ∀ t > 0
    #         (SOC energy balance; η_leg = √round_trip_efficiency)
    #
    #   [C2]  η_leg × Σ_t ch_mwh[t] <= max_cycles × capacity_mwh      [optional]
    #         (lifetime cycle cap; omit max_cycles to disable)
    #
    # Co-location only:
    #   [B4]  0 <= ch_grid_mwh[t] <= grid_import_mw × dt
    #         (grid-imported share of charging bounded by import connection;
    #          falls back to power_mw × dt if grid_import_mw not set)
    #
    #   [C3]  dsch_mwh[t] <= export_connection_dt − gen_avail[t]
    #         (export headroom: generation occupies part of the export connection;
    #          BESS can only use the remainder;
    #          export_connection_dt = grid_export_mw × dt or power_mw × dt;
    #          gen_avail[t] = min(generation_mwh[t], export_connection_dt))
    #
    #   [C4]  ch_grid_mwh[t] >= ch_mwh[t] − gen_avail[t]
    #         (lower bound on grid import: forces ch_grid_mwh > 0 when total
    #          charging exceeds available solar; combined with [B4] this pins
    #          ch_grid_mwh[t] = max(0, ch_mwh[t] − gen_avail[t]);
    #          rearranged: BTM share ch_mwh[t] − ch_grid_mwh[t] <= gen_avail[t])
    #
    #   [C5]  ch_grid_mwh[t] <= ch_mwh[t]
    #         (grid import cannot exceed total charging; prevents ch_grid_mwh
    #          from being inflated when charge_tariff = 0 gives no cost signal)
    #
    # Objective (maximise over all timesteps):
    #   [O1]  max  Σ_t [ price[t] × (dsch_mwh[t] − ch_mwh[t])
    #                  − discharge_tariff × dsch_mwh[t]
    #                  − charge_tariff × ch_mwh[t] ]               [stand-alone]
    #
    #   [O2]  max  Σ_t [ price[t] × (dsch_mwh[t] − ch_mwh[t])
    #                  − discharge_tariff × dsch_mwh[t]
    #                  − charge_tariff    × ch_grid_mwh[t]
    #                  + discharge_tariff × (ch_mwh[t] − ch_grid_mwh[t]) ]
    #                                                               [co-location]
    #         where charge_tariff is replaced by ctariff[t] when a per-timestep
    #         consumption tariff series is supplied.
    #         Term by term:
    #           − discharge_tariff × dsch_mwh[t]
    #               export tariff on all discharge
    #           − charge_tariff × ch_grid_mwh[t]
    #               import tariff on grid-charged share only (BTM charging exempt)
    #           + discharge_tariff × (ch_mwh[t] − ch_grid_mwh[t])
    #               refund of export tariff on BTM-charged discharge
    #               (energy charged from solar never crossed the import meter,
    #               so its eventual discharge is untaxed)
    #         Algebraically equivalent to the coded form:
    #           − discharge_tariff × (dsch_mwh[t] − ch_mwh[t])
    #           − (charge_tariff + discharge_tariff) × ch_grid_mwh[t]
    # -------------------------------------------------------------------------

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

        def colocation_rule(mm, t):
            return mm.dsch_mwh[t] <= export_connection_dt - mm.gen_avail[t]

        m.colocation_cons = pyo.Constraint(m.T, rule=colocation_rule)  # [C3]

        # ch_grid_mwh[t]: grid-imported share of charging, bounded by import connection [B4].
        m.ch_grid_mwh = pyo.Var(m.T, bounds=(0.0, max_grid_import_mwh))

        def ch_grid_lb_rule(mm, t):
            return mm.ch_grid_mwh[t] >= mm.ch_mwh[t] - mm.gen_avail[t]

        m.ch_grid_lb = pyo.Constraint(m.T, rule=ch_grid_lb_rule)  # [C4]

        def ch_grid_ub_rule(mm, t):
            return mm.ch_grid_mwh[t] <= mm.ch_mwh[t]

        m.ch_grid_ub = pyo.Constraint(m.T, rule=ch_grid_ub_rule)  # [C5]

    def profit_rule(mm):
        if generation_mwh is not None:
            # Round-trip tariff relief for BTM-charged energy:
            # ch_btm[t] = ch_mwh[t] - ch_grid_mwh[t]  (charged from generation, no meter crossing)
            # That energy avoids charge_tariff on the way in AND discharge_tariff on the way out.
            # Equivalently: apply both tariffs to ch_grid_mwh[t] (grid-charged share) and
            # discharge_tariff only to (dsch_mwh[t] - ch_btm[t]) = dsch_mwh[t] - ch_mwh[t] + ch_grid_mwh[t].
            # Expanding: charge_tariff*ch_grid + discharge_tariff*(dsch - ch_mwh + ch_grid)
            #          = charge_tariff*ch_grid + discharge_tariff*dsch
            #            - discharge_tariff*ch_mwh + discharge_tariff*ch_grid
            #          = (charge_tariff + discharge_tariff)*ch_grid
            #            + discharge_tariff*(dsch - ch_mwh)
            if consumption_tariffs is not None:
                # Per-timestep tariff replaces scalar charge_tariff for the grid-charged share.
                return sum(
                    mm.price[t] * (mm.dsch_mwh[t] - mm.ch_mwh[t])
                    - discharge_tariff * (mm.dsch_mwh[t] - mm.ch_mwh[t])
                    - (mm.ctariff[t] + discharge_tariff) * mm.ch_grid_mwh[t]
                    for t in times
                )
            return sum(
                mm.price[t] * (mm.dsch_mwh[t] - mm.ch_mwh[t])
                - discharge_tariff * (mm.dsch_mwh[t] - mm.ch_mwh[t])
                - (charge_tariff + discharge_tariff) * mm.ch_grid_mwh[t]
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
            revenue = (
                p * (dsch_grid - ch_total)
                - discharge_tariff * dsch_grid
                - eff_charge_tariff * ch_grid_taxable
            )
        else:
            # Stand-alone: ch_mwh is purely grid import.
            ch_grid_taxable = ch_total
            ch_btm = 0.0
            revenue = (
                p * (dsch_grid - ch_total)
                - discharge_tariff * dsch_grid
                - eff_charge_tariff * ch_total
            )

        cumulative_revenue += revenue
        row: dict = {
            "price": p,
            "soc": soc_frac,
            "soc_mwh": soc_mwh_val,
            # grid_import_mwh: actual meter-crossing import (= ch_grid_taxable in co-loc; ch_total stand-alone)
            "grid_import_mwh": ch_grid_taxable,
            "grid_export_mwh": dsch_grid,
            "charge_mwh": ch_stored,
            "discharge_mwh": dsch_stored,
            "revenue": revenue,
            "cumulative_revenue": cumulative_revenue,
        }
        if generation_mwh is not None:
            row["generation_mw"] = generation_mwh[t] / INTERVAL_HOURS
            row["charge_btm_mwh"] = ch_btm           # charged from generation (no tariff)
            row["charge_grid_mwh"] = ch_grid_taxable  # charged from grid (tariff applied)
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

    if args.write_sample_prices is not None:
        n = args.write_sample_prices
        if n < 1:
            sys.exit("N must be >= 1")
        out_p = Path(spec_str(spec, "prices_csv"))
        write_sample_prices(out_p, n, args.sample_seed)
        print(f"Wrote {n} sample prices to {out_p.resolve()}")
        return

    prices_path = Path(spec_str(spec, "prices_csv"))
    output_path = Path(spec_str(spec, "output_csv"))
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
        print(
            f"Note: grid_connection_mw={grid_connection_mw} MW used for both import and export "
            "(legacy key). Use grid_import_mw / grid_export_mw to set them independently.",
            flush=True,
        )

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
        print(
            f"Co-location mode: profile={gen_profile_csv}, generation_max_mw={gen_max_mw} MW",
            flush=True,
        )

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
    prices = load_prices_csv(prices_path)

    if consumption_tariff_csv is not None:
        consumption_tariffs = load_tariff_csv(Path(consumption_tariff_csv))
        if len(consumption_tariffs) != len(prices):
            sys.exit(
                f"Consumption tariff series length ({len(consumption_tariffs)}) does not match "
                f"price series length ({len(prices)}). Align the two CSVs to the same period."
            )
        print(
            f"Consumption tariff: per-timestep series loaded from '{consumption_tariff_csv}' "
            f"({len(consumption_tariffs)} timesteps); scalar charge_tariff ignored.",
            flush=True,
        )

    if grid_import_mw is not None:
        effective_import = min(power_mw, grid_import_mw)
        print(
            f"Grid import cap: {grid_import_mw} MW "
            f"(effective: {effective_import} MW per interval)",
            flush=True,
        )
    if grid_export_mw is not None:
        effective_export = min(power_mw, grid_export_mw)
        print(
            f"Grid export cap: {grid_export_mw} MW "
            f"(effective: {effective_export} MW per interval)",
            flush=True,
        )

    if max_cycles is not None:
        print(f"Cycle cap applied: max_cycles={max_cycles}", flush=True)
    else:
        print(
            "No cycle cap (omit max_cycles or leave the line commented — lines starting "
            "with # are ignored).",
            flush=True,
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
    spot_gross = sum(
        prices[t]
        * (pyo.value(model.dsch_mwh[t]) - pyo.value(model.ch_mwh[t]))
        for t in range(Tn)
    )
    tariff_component = sum(
        (
            discharge_tariff * (pyo.value(model.dsch_mwh[t]) - pyo.value(model.ch_mwh[t]))
            + (
                (consumption_tariffs[t] if consumption_tariffs is not None else charge_tariff)
                + discharge_tariff
            ) * pyo.value(model.ch_grid_mwh[t])
            if generation_mwh is not None
            else (consumption_tariffs[t] if consumption_tariffs is not None else charge_tariff)
            * pyo.value(model.ch_mwh[t])
            + discharge_tariff * pyo.value(model.dsch_mwh[t])
        )
        for t in range(Tn)
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
    )

    print(f"Spot revenue (price × net grid MWh, before tariffs): {spot_gross:.6g} €")
    print(f"Tariff charges (on grid MWh): {tariff_component:.6g} €")
    print(f"Total profit (net arbitrage, horizon): {total_profit:.6g} €")
    print(f"Equivalent full cycles (charge-based): {n_cycles:.6g} cycles")
    if math.isnan(profit_per_cycle):
        print("Profit per cycle: n/a (zero cycles) [€/cycle]")
    else:
        print(f"Profit per cycle: {profit_per_cycle:.6g} €/cycle")
    print(
        f"Profit 365 cycles normalized (total profit / 365): "
        f"{profit_365_cycles_normalized:.6g} € (≈ €/cycle if 365 cycles/yr)"
    )
    print(f"Wrote {output_path.resolve()}")


if __name__ == "__main__":
    main()
