"""
Minimal BESS arbitrage LP: Pyomo + HiGHS.
Decision variables ch_mwh / dsch_mwh are grid-side MWh per timestep (bounded by power_mw * INTERVAL_HOURS).
Usable energy capacity is capacity_mwh from specification; state variable soc_mwh is stored energy in MWh.
SOC: Δsoc_mwh = grid_import * η_leg − grid_export / η_leg with η_leg = √(round_trip_efficiency).
Using the spec value on both legs as η (instead of η_leg) would imply grid-to-grid round-trip η², not η.
CSV: grid_import_mwh / grid_export_mwh at the meter; charge_mwh / discharge_mwh are stored-side MWh
(stored gain = grid_import × η_leg; stored loss to deliver export = grid_export / η_leg), so Δsoc_mwh ≈ charge_mwh − discharge_mwh.
Each price CSV row is treated as one hour (see INTERVAL_HOURS).
Optional max equivalent cycles; charge/discharge tariffs apply to grid MWh (same units as prices).
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


def build_and_solve(
    prices: list[float],
    *,
    power_mw: float,
    capacity_mwh: float,
    round_trip_efficiency: float,
    charge_tariff: float,
    discharge_tariff: float,
    max_cycles: float | None,
) -> tuple[pyo.ConcreteModel, pyo.SolverResults]:
    rte = round_trip_efficiency
    if not (0 < rte <= 1):
        sys.exit("round_trip_efficiency must be in (0, 1]")
    T = len(prices)
    if T == 0:
        sys.exit("Empty price series")

    eta_leg = math.sqrt(rte)
    cap = capacity_mwh
    dt = INTERVAL_HOURS
    max_mwh = power_mw * dt
    times = list(range(T))

    m = pyo.ConcreteModel()
    m.T = pyo.Set(initialize=times)
    m.price = pyo.Param(m.T, initialize={t: prices[t] for t in times})

    m.soc_mwh = pyo.Var(m.T, bounds=(0.0, cap))
    m.ch_mwh = pyo.Var(m.T, bounds=(0.0, max_mwh))
    m.dsch_mwh = pyo.Var(m.T, bounds=(0.0, max_mwh))

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

    m.soc_cons = pyo.Constraint(m.T, rule=soc_rule)

    if max_cycles is not None:

        def cycle_cap_rule(mm):
            return eta_leg * sum(mm.ch_mwh[t] for t in times) <= max_cycles * cap

        m.cycle_cap = pyo.Constraint(rule=cycle_cap_rule)

    def profit_rule(mm):
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
) -> None:
    eta_leg = math.sqrt(round_trip_efficiency)
    rows = []
    cumulative_revenue = 0.0
    for t in range(len(prices)):
        ch_grid = pyo.value(model.ch_mwh[t])
        dsch_grid = pyo.value(model.dsch_mwh[t])
        ch_stored = ch_grid * eta_leg
        dsch_stored = dsch_grid / eta_leg
        soc_mwh_val = pyo.value(model.soc_mwh[t])
        soc_frac = soc_mwh_val / capacity_mwh
        p = prices[t]
        revenue = (
            p * (dsch_grid - ch_grid)
            - discharge_tariff * dsch_grid
            - charge_tariff * ch_grid
        )
        cumulative_revenue += revenue
        rows.append(
            {
                "price": p,
                "soc": soc_frac,
                "soc_mwh": soc_mwh_val,
                "grid_import_mwh": ch_grid,
                "grid_export_mwh": dsch_grid,
                "charge_mwh": ch_stored,
                "discharge_mwh": dsch_stored,
                "revenue": revenue,
                "cumulative_revenue": cumulative_revenue,
            }
        )
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

    if power_mw <= 0:
        sys.exit("power must be positive")
    if capacity_mwh <= 0:
        sys.exit("capacity_mwh must be positive")
    if max_cycles is not None and max_cycles < 0:
        sys.exit("max_cycles must be non-negative")
    prices = load_prices_csv(prices_path)

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
        charge_tariff * pyo.value(model.ch_mwh[t])
        + discharge_tariff * pyo.value(model.dsch_mwh[t])
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
