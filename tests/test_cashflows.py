"""hourly_cashflows(): the per-timestep money callers store must add up exactly to the objective."""
from __future__ import annotations

import math

import pandas as pd
import pyomo.environ as pyo
import pytest

import bess_dispatch_opt as bdo

HOURS = 24 * 28
BATTERY = dict(power_mw=1.0, capacity_mwh=2.0, round_trip_efficiency=0.9, max_cycles=None)


def _prices() -> list[float]:
    # Evening peak every day; every third day a slightly negative midday dip, so generation is
    # curtailed and (being shallower than the charge tariff) charging from it beats grid import.
    out = []
    for t in range(HOURS):
        day, hour = divmod(t, 24)
        p = 60.0 + 50.0 * math.sin(2 * math.pi * (hour - 9) / 24)
        if day % 3 == 0 and 10 <= hour <= 14:
            p = -2.0 - 0.5 * (hour - 10)
        out.append(p)
    return out


def _solar(peak_mw: float) -> list[float]:
    return [max(0.0, math.sin(math.pi * ((t % 24) - 6) / 14)) * peak_mw for t in range(HOURS)]


def _tariffs() -> list[float]:
    return [40.0 if 17 <= t % 24 <= 20 else 8.0 for t in range(HOURS)]


CASES = {
    "stand-alone": dict(charge_tariff=5.0, discharge_tariff=1.5),
    "stand-alone, hourly tariffs": dict(charge_tariff=0.0, discharge_tariff=1.5,
                                        consumption_tariffs=_tariffs()),
    # 2 MWp behind a 1 MW export connection: clipped surplus and negative-price curtailment.
    "co-located, clipping + curtailment": dict(charge_tariff=0.0, discharge_tariff=1.5,
                                               consumption_tariffs=_tariffs(),
                                               generation_mwh=_solar(2.0), grid_export_mw=1.0),
    "co-located, export = PV": dict(charge_tariff=0.0, discharge_tariff=1.5,
                                    consumption_tariffs=_tariffs(),
                                    generation_mwh=_solar(2.0), grid_export_mw=2.0),
    "co-located, no tariffs": dict(charge_tariff=0.0, discharge_tariff=0.0,
                                   generation_mwh=_solar(1.0), grid_export_mw=1.0),
}


def _solve(case: dict, **extra):
    prices = bdo.resolve_prices(None, None, _prices())
    m, results, _ = bdo.build_and_solve(prices, **BATTERY, **case, **extra)
    assert results.solver.termination_condition == pyo.TerminationCondition.optimal
    return prices, m


@pytest.mark.parametrize("name", CASES)
def test_hourly_profit_sums_to_the_objective(name):
    _, m = _solve(CASES[name])
    cf = bdo.hourly_cashflows(m)
    assert sum(cf["profit_eur"]) == pytest.approx(pyo.value(m.obj), rel=1e-12, abs=1e-9)
    for t in range(HOURS):
        assert cf["profit_eur"][t] == pytest.approx(
            cf["spot_revenue_eur"][t] - cf["spot_cost_eur"][t]
            - cf["charge_tariff_eur"][t] - cf["discharge_tariff_eur"][t], abs=1e-9)


@pytest.mark.parametrize("name", CASES)
def test_cashflows_match_the_dispatch(name):
    """With a single price series the components equal the plain formulas on the solved
    dispatch: spot price on grid charging plus exportable generation, charge tariff on grid
    charging only, discharge tariff on discharge not fed by exportable generation."""
    case = CASES[name]
    prices, m = _solve(case)
    cf = bdo.hourly_cashflows(m)
    colocated = "generation_mwh" in case
    for t in range(HOURS):
        p = prices.buy[t]
        ct = case["consumption_tariffs"][t] if "consumption_tariffs" in case else case["charge_tariff"]
        ch, dsch = pyo.value(m.ch_mwh[t]), pyo.value(m.dsch_mwh[t])
        grid = pyo.value(m.ch_grid_mwh[t]) if colocated else ch
        avail = pyo.value(m.ch_from_gen_avail[t]) if colocated else 0.0
        assert cf["spot_revenue_eur"][t] == pytest.approx(p * dsch, abs=1e-6)
        assert cf["spot_cost_eur"][t] == pytest.approx(p * (grid + avail), abs=1e-6)
        assert cf["charge_tariff_eur"][t] == pytest.approx(ct * grid, abs=1e-6)
        assert cf["discharge_tariff_eur"][t] == pytest.approx(case["discharge_tariff"] * (dsch - avail), abs=1e-6)


def test_the_colocated_case_exercises_wasted_generation():
    # Guard for the tests above: the clipping case must actually charge from curtailed and
    # clipped generation, the share whose spot cost is waived.
    _, m = _solve(CASES["co-located, clipping + curtailment"])
    assert sum(pyo.value(m.ch_from_gen_curt[t]) for t in m.T) > 1.0
    assert sum(pyo.value(m.ch_from_gen_surplus[t]) for t in m.T) > 1.0


def test_discounted_objective_uses_the_same_cashflows():
    df = [0.97 ** (t // (24 * 7)) for t in range(HOURS)]
    _, m = _solve(CASES["co-located, clipping + curtailment"], discount_factors=df)
    cf = bdo.hourly_cashflows(m)
    assert sum(d * p for d, p in zip(df, cf["profit_eur"])) == pytest.approx(pyo.value(m.obj), rel=1e-12)
    assert cf["profit_eur"] == [pyo.value(m.op_cash[t]) for t in m.T]


def test_output_csv_carries_the_cashflows(tmp_path):
    case = CASES["co-located, clipping + curtailment"]
    prices, m = _solve(case)
    path = tmp_path / "out.csv"
    bdo.write_output(
        path, prices, m, capacity_mwh=BATTERY["capacity_mwh"],
        round_trip_efficiency=BATTERY["round_trip_efficiency"],
        charge_tariff=case["charge_tariff"], discharge_tariff=case["discharge_tariff"],
        curtailment_threshold=case["discharge_tariff"], generation_mwh=case["generation_mwh"],
        consumption_tariffs=case["consumption_tariffs"],
    )
    df = pd.read_csv(path)
    cf = bdo.hourly_cashflows(m)
    for name in bdo.CASHFLOW_COMPONENTS:
        assert df[name].tolist() == pytest.approx(cf[name], abs=1e-9)
    assert df["revenue"].tolist() == pytest.approx(df["profit_eur"].tolist(), abs=1e-9)
    assert df["cumulative_revenue"].iloc[-1] == pytest.approx(pyo.value(m.obj), rel=1e-12)
