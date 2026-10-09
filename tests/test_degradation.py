"""Endogenous degradation: preprocessing, model behaviour and validation checks 2-9 of the plan."""
from __future__ import annotations

import math
import os
import subprocess
import time
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pyomo.environ as pyo
import pytest

import bess_dispatch_opt as bdo
from synthetic import (
    ROOT, TZ, daily_prices, report_value, run_tool, timeline, write_curves, write_dated,
    write_legacy, write_spec, write_year_value,
)

NOMINAL = 2.0
LOSS = {1.0: 0.02, 1.5: 0.03, 2.0: 0.05}    # SoH lost per year at 1 / 1.5 / 2 cycles per day
EOL = 0.7                                    # curves end there after 15 / 10 / 6 years


# ------------------------------------------------------------------ preprocessing (unit tests)

def test_derived_parameters_match_template_formulas():
    curves = {r: [1.0 - l * y for y in range(4)] for r, l in LOSS.items()}
    params, warnings = bdo.derive_degradation_params(curves)
    p = params[2]
    assert not warnings
    assert p.calendar == pytest.approx(3 * 0.02 - 2 * 0.03)            # 0.0 (floored at 0)
    assert p.damage[0] == pytest.approx((0.02 - p.calendar) / 365)
    assert p.damage[1] == pytest.approx((0.03 - 0.02) / 182.5)
    assert p.damage[2] == pytest.approx((0.05 - 0.03) / 182.5)


def test_shorter_curve_keeps_its_last_year_loss():
    # The 2 cycles/day curve reaches end of life (0.9) after 2 years; in year 3 it keeps its
    # last-year loss (0.05), so the year-3 parameters equal the year-2 ones.
    curves = {1.0: [1, 0.98, 0.96, 0.94, 0.92, 0.9], 1.5: [1, 0.97, 0.94, 0.91, 0.9], 2.0: [1, 0.95, 0.9]}
    params, _ = bdo.derive_degradation_params(curves)
    assert len(params) == 5
    assert params[3].damage == pytest.approx(params[2].damage)


def test_extrapolated_calendar_makes_bands_1_and_2_equal():
    # Linear extrapolation through the 1 and 1.5 cycles/day points gives the 0-1 band the same
    # slope as the 1-1.5 band, unless the extrapolated calendar loss is floored at 0.
    curves = {1.0: [1, 0.97], 1.5: [1, 0.96], 2.0: [1, 0.94]}
    params, _ = bdo.derive_degradation_params(curves)
    d = params[1].damage
    assert params[1].calendar == pytest.approx(0.01)
    assert d[0] == pytest.approx(d[1])


def test_convex_envelope_repair():
    curves = {1.0: [1, 0.97], 1.5: [1, 0.968], 2.0: [1, 0.96]}   # band 3 cheaper than band 2
    params, warnings = bdo.derive_degradation_params(curves, calendar_curve=[1, 0.995])
    d = params[1].damage
    assert any("convex envelope" in w for w in warnings)
    assert d[0] <= d[1] + 1e-15 <= d[2] + 2e-15


def test_lower_convex_envelope():
    assert bdo.lower_convex_envelope([0, 1, 2, 3], [0, 2, 1, 3]) == pytest.approx([0, 0.5, 1, 3])


def test_periods_year_and_month():
    idx = timeline("2030-01-01", 2)
    years = bdo.build_periods(idx, "year")
    assert years.hours == [8760, 8760] and years.year_fraction == [1.0, 1.0]
    months = bdo.build_periods(idx, "month")
    assert len(months.hours) == 24
    assert months.hours[2] == 743 and months.hours[9] == 745          # March / October 2030 (DST)
    assert sum(months.year_fraction[:12]) == pytest.approx(1.0)
    assert months.age_year[11] == 1 and months.age_year[12] == 2
    partial = bdo.build_periods(timeline("2030-01-01", 1.5), "year")
    jan_to_jun_2031 = 181 * 24 - 1                                    # spring DST hour lost
    assert partial.year_fraction[1] == pytest.approx(jan_to_jun_2031 / 8760)
    assert partial.n_years == pytest.approx(1 + jan_to_jun_2031 / 8760)


def test_discount_factor_conventions():
    assert bdo.discount_factor(1, 0.08) == 1.0
    assert bdo.discount_factor(3, 0.08) == pytest.approx(1 / 1.08 ** 2)
    assert bdo.discount_factor(1, 0.08, "mid") == pytest.approx(1 / 1.08 ** 0.5)


# ------------------------------------------------------------------ solving helpers

def solve(tmp_path, years, *, peaks=2, amp=50.0, loss=LOSS, eol=EOL, calendar=None,
          max_cycles_per_day=None, sell_offset=0.0, degradation=True,
          degradation_method="period", **keys):
    """Solve a synthetic 1 MW / 2 MWh case directly through build_and_solve."""
    idx = timeline("2030-01-01", years)
    buy = daily_prices(idx, peaks=peaks, amp=amp)
    sell = [p + sell_offset for p in buy]
    prices = bdo.resolve_prices(buy, sell, None)
    setup = None
    if degradation:
        spec = {
            "degradation_curves_csv": str(write_curves(tmp_path / "curves.csv", loss, eol, math.ceil(years))),
            "degradation_method": degradation_method,
            **{k: str(v) for k, v in keys.items()},
        }
        if calendar is not None:
            spec["calendar_curve_csv"] = str(write_year_value(tmp_path / "cal.csv", calendar))
        setup, _ = bdo.prepare_degradation(
            spec, n_steps=len(idx), start=idx[0], timezone=TZ, initial_soc=0.5,
            lp_power_sharing=sell_offset != 0.0,
        )
    m, _, metrics = bdo.build_and_solve(
        prices, power_mw=1.0, capacity_mwh=NOMINAL, round_trip_efficiency=0.9,
        charge_tariff=0.0, discharge_tariff=0.0, max_cycles=None,
        max_cycles_per_day=max_cycles_per_day,
        day_index=bdo.day_labels(len(idx), start_date=idx[0]) if max_cycles_per_day else None,
        prices_start_date=idx[0], degradation=setup,
    )
    return m, metrics, setup


def caps(m):
    return [pyo.value(m.cap[p]) for p in m.Pcap]


def bands(m, p):
    return [pyo.value(m.band[p, k]) for k in m.K]


def physical_caps(m, setup):
    cycles = [pyo.value(m.throughput[p]) / NOMINAL for p in m.P]
    return bdo.physical_capacity_path(setup, cycles, NOMINAL)[0]


# ------------------------------------------------------------------ validation checks

def test_2_zero_degradation_matches_fixed_capacity(tmp_path):
    zero = {r: 0.0 for r in LOSS}
    m, _, _ = solve(tmp_path, 2, peaks=1, loss=zero, eol=1.0, final_soc_fraction=0)
    fixed, _, _ = solve(tmp_path, 2, peaks=1, degradation=False)
    assert caps(m) == pytest.approx([NOMINAL] * 3)
    op = sum(pyo.value(m.op_cash[t]) for t in m.T)
    assert op == pytest.approx(pyo.value(fixed.obj), rel=1e-6)
    assert pyo.value(m.npv) == pytest.approx(op, rel=1e-9)            # r = 0


def test_3_capacity_transition_by_hand(tmp_path):
    m, _, setup = solve(tmp_path, 2)
    par = setup.params
    for p in m.P:
        loss = NOMINAL * (par.calendar[p] + sum(par.damage[p][k] * b for k, b in enumerate(bands(m, p))))
        assert caps(m)[p + 1] == pytest.approx(caps(m)[p] - loss, abs=1e-7)
        assert sum(bands(m, p)) == pytest.approx(pyo.value(m.throughput[p]) / NOMINAL, abs=1e-7)


@pytest.mark.parametrize("rate", [1.0, 2.0])
def test_4_forced_cycling_follows_curve(tmp_path, rate):
    # Strong prices with four peaks a day and a daily cap make the battery cycle exactly `rate`
    # times every day, even as its capacity shrinks.
    m, _, setup = solve(tmp_path, 3, peaks=4, amp=200.0, max_cycles_per_day=rate)
    cycles_per_day = [pyo.value(m.throughput[p]) / NOMINAL / (h / 24) for p, h in enumerate(setup.periods.hours)]
    assert cycles_per_day == pytest.approx([rate] * 3, abs=0.01)
    soh = [c / NOMINAL for c in physical_caps(m, setup)]
    assert soh == pytest.approx([1.0 - LOSS[rate] * y for y in range(4)], abs=1e-3)


def test_5_bands_fill_in_order(tmp_path):
    calendar = {0: 1.0, 1: 0.995, 2: 0.99, 3: 0.985}                  # strictly convex damage
    m, _, setup = solve(tmp_path, 2, amp=200.0, calendar=calendar, terminal_value_per_mwh=200_000)
    d = setup.params.damage[0]
    assert d[0] < d[1] < d[2]
    for p in m.P:
        b, w = bands(m, p), setup.params.widths[p]
        if b[1] > 1e-6:
            assert b[0] == pytest.approx(w[0], abs=1e-5)
        if b[2] > 1e-6:
            assert b[1] == pytest.approx(w[1], abs=1e-5)


def test_6_higher_discount_rate_shifts_cycling_earlier(tmp_path, capsys):
    # Costly cycling plus a terminal value on remaining capacity: at 0% the capacity kept for the
    # end is worth as much as revenue today, so dear cycles are skipped; at a high rate that value
    # is discounted away and the battery cycles harder now.
    heavy = {1.0: 0.12, 1.5: 0.2, 2.0: 0.3}
    kw = dict(peaks=4, amp=40.0, loss=heavy, eol=0.1, terminal_value_per_mwh=120_000)
    low, _, _ = solve(tmp_path, 3, discount_rate=0.0, **kw)
    high, _, _ = solve(tmp_path, 3, discount_rate=0.5, **kw)

    def cycles(m):
        return [pyo.value(m.throughput[p]) / NOMINAL for p in m.P]

    cl, ch = cycles(low), cycles(high)
    with capsys.disabled():
        print(f"\n  cycles per year at 0%:  {', '.join(f'{c:.0f}' for c in cl)}"
              f"\n  cycles per year at 50%: {', '.join(f'{c:.0f}' for c in ch)}")
    assert ch[0] > cl[0] + 5
    assert sum(ch) > sum(cl)


def test_dual_sign_and_degradation_cost(tmp_path):
    # Capacity value from the duals vs a finite difference: more calendar loss in period 0 must
    # lower the NPV by value[0] × nominal × extra loss.
    kw = dict(amp=40.0, terminal_value_per_mwh=50_000, discount_rate=0.05, final_soc_fraction=0)
    m0, metrics, setup = solve(tmp_path, 2, **kw)
    value = metrics["capacity_value"][0]
    assert value > 0
    eps = 1e-3
    bumped = bdo.PeriodDegradation(
        calendar=[setup.params.calendar[0] + eps] + setup.params.calendar[1:],
        widths=setup.params.widths, damage=setup.params.damage,
    )
    setup2 = bdo.DegradationSetup(**{**setup.__dict__, "params": bumped})
    idx = timeline("2030-01-01", 2)
    prices = daily_prices(idx, amp=40.0, peaks=2)
    m1, _, _ = bdo.build_and_solve(
        bdo.resolve_prices(prices, None, None), power_mw=1.0, capacity_mwh=NOMINAL,
        round_trip_efficiency=0.9, charge_tariff=0.0, discharge_tariff=0.0, max_cycles=None,
        prices_start_date=idx[0], degradation=setup2,
    )
    delta = pyo.value(m1.npv) - pyo.value(m0.npv)
    assert delta == pytest.approx(-value * NOMINAL * eps, rel=0.05)


def test_7_yearly_vs_monthly_periods(tmp_path, capsys):
    yearly, _, ys = solve(tmp_path, 3, amp=40.0, terminal_value_per_mwh=50_000)
    monthly, _, ms = solve(tmp_path, 3, amp=40.0, terminal_value_per_mwh=50_000, degradation_period="month")
    rev = lambda m: sum(pyo.value(m.op_cash[t]) for t in m.T)
    cyc = lambda m: sum(pyo.value(m.throughput[p]) for p in m.P) / NOMINAL
    with capsys.disabled():
        print(
            f"\n  yearly : revenue {rev(yearly):,.0f}  cycles {cyc(yearly):,.1f}  end capacity {physical_caps(yearly, ys)[-1]:.4f}"
            f"\n  monthly: revenue {rev(monthly):,.0f}  cycles {cyc(monthly):,.1f}  end capacity {physical_caps(monthly, ms)[-1]:.4f}"
        )
    assert rev(monthly) == pytest.approx(rev(yearly), rel=0.02)
    assert physical_caps(monthly, ms)[-1] == pytest.approx(physical_caps(yearly, ys)[-1], rel=0.02)


def test_end_of_life_limit_from_curves(tmp_path):
    # Strong prices push cycling to 2/day. Over 3 years that ends near 0.85 of nominal: fine with
    # curves ending at 0.7, but curves ending at 0.9 make the end-of-life limit bind exactly.
    free, _, free_setup = solve(tmp_path, 3, peaks=4, amp=200.0, eol=0.7)
    assert free_setup.eol_fraction == pytest.approx(0.7)
    assert physical_caps(free, free_setup)[-1] < 0.9 * NOMINAL
    limited, _, setup = solve(tmp_path, 3, peaks=4, amp=200.0, eol=0.9)
    assert setup.eol_fraction == pytest.approx(0.9)
    assert physical_caps(limited, setup)[-1] == pytest.approx(0.9 * NOMINAL, abs=1e-4)


def test_eol_fraction_key_rejected(tmp_path):
    spec = {"degradation_curves_csv": str(write_curves(tmp_path / "c.csv", LOSS)), "eol_fraction": "0.7"}
    with pytest.raises(SystemExit):
        bdo.prepare_degradation(spec, n_steps=100, start="2030-01-01", timezone=TZ,
                                initial_soc=0.5, lp_power_sharing=False)


def test_9_pure_lp(tmp_path):
    m, _, _ = solve(tmp_path, 1, sell_offset=20.0)
    assert all(v.is_continuous() for v in m.component_data_objects(pyo.Var))
    assert hasattr(m, "power_sharing")                                # LP stand-in for C6


def test_9_explicit_binary_exclusivity_rejected(tmp_path):
    idx = timeline("2030-01-01", 1)
    write_dated(tmp_path / "buy.csv", idx, daily_prices(idx))
    write_dated(tmp_path / "sell.csv", idx, [p + 10 for p in daily_prices(idx)])
    write_curves(tmp_path / "curves.csv", LOSS)
    spec = write_spec(tmp_path / "s.txt", {
        "output_csv": "out.csv", "power": 1, "capacity_mwh": 2, "round_trip_efficiency": 0.9,
        "buy_prices_csv": "buy.csv", "sell_prices_csv": "sell.csv",
        "endogenous_degradation": "true", "degradation_curves_csv": "curves.csv",
        "no_simultaneous_charge_discharge": "true",
    })
    res = run_tool(spec)
    assert res.returncode != 0 and "binary" in (res.stderr + res.stdout)


def test_end_to_end_outputs(tmp_path):
    idx = timeline("2030-01-01", 2)
    write_dated(tmp_path / "p.csv", idx, daily_prices(idx, peaks=2))
    write_curves(tmp_path / "curves.csv", LOSS)
    spec = write_spec(tmp_path / "s.txt", {
        "output_csv": "out.csv", "power": 1, "capacity_mwh": 2, "round_trip_efficiency": 0.9,
        "prices_csv": "p.csv", "endogenous_degradation": "true",
        "degradation_curves_csv": "curves.csv", "discount_rate": 0.07,
        "terminal_value_per_mwh": 30000, "var_om_per_mwh": 2,
    })
    res = run_tool(spec)
    assert res.returncode == 0, res.stderr + res.stdout
    hourly = pd.read_csv(tmp_path / "out.csv")
    periods = pd.read_csv(tmp_path / "out_periods.csv")
    yearly = pd.read_csv(tmp_path / "out_yearly.csv")
    report = (tmp_path / "out.txt").read_text()
    assert {"timestamp", "period", "age_year", "capacity_mwh_period", "discount_factor"} <= set(hourly.columns)
    assert len(periods) == 2 and len(yearly) == 2
    assert periods["capacity_end_mwh"].iloc[-1] == pytest.approx(report_value(report, "End capacity"), abs=0.01)
    npv = yearly["net_discounted_eur"].sum() + report_value(report, "Terminal value (discounted)")
    assert npv == pytest.approx(report_value(report, "NPV (objective)"), abs=1.0)
    assert "Warning: objective does not match" not in res.stdout
    sheets = pd.ExcelFile(tmp_path / "out.xlsx").sheet_names
    assert sheets == ["Dispatch", "Results", "Periods", "Yearly"]


# ------------------------------------------------------------------ check 1: regression

def _main_branch_tool(tmp_path):
    try:
        src = subprocess.run(
            ["git", "show", "main:src/bess_dispatch_opt.py"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("main branch not available for the regression baseline")
    path = tmp_path / "tool_main.py"
    path.write_text(src, encoding="utf-8")
    return path


def test_1_degradation_off_matches_main_branch_colocation(tmp_path):
    # Co-location, tariff series, discharge tariff, grid caps and a daily cycle cap, legacy format.
    old_tool = _main_branch_tool(tmp_path)
    idx = timeline("2030-01-01", 1)
    write_legacy(tmp_path / "p.csv", daily_prices(idx, peaks=2, amp=70))
    write_legacy(tmp_path / "t.csv", [2 + 3 * (ts.hour >= 17) for ts in idx])
    write_legacy(tmp_path / "g.csv", [max(0.0, math.sin(math.pi * (ts.hour - 6) / 12)) for ts in idx])
    keys = {
        "power": 1, "capacity_mwh": 2, "round_trip_efficiency": 0.9, "prices_csv": "p.csv",
        "consumption_tariff_csv": "t.csv", "discharge_tariff": 1.38, "generation_profile_csv": "g.csv",
        "generation_max_mw": 3, "grid_import_mw": 0.5, "grid_export_mw": 2.5,
        "max_cycles_per_day": 2, "prices_start_date": "2030-01-01",
    }
    for name, tool in (("old", old_tool), ("new", None)):
        spec = write_spec(tmp_path / f"{name}.txt", {**keys, "output_csv": f"out_{name}.csv"})
        res = run_tool(spec, tool) if tool else run_tool(spec)
        assert res.returncode == 0, res.stderr + res.stdout
    assert (tmp_path / "out_old.csv").read_bytes() == (tmp_path / "out_new.csv").read_bytes()
    assert (tmp_path / "out_old.txt").read_bytes() == (tmp_path / "out_new.txt").read_bytes()


DK1 = ROOT.parent / "bess_dispatch_tool_dk1_test"


@pytest.mark.skipif(not DK1.is_dir(), reason="DK1 test repo not present")
@pytest.mark.parametrize("spec_name,profit", [("01_spot_baseline", 88427.18), ("02_buy_sell", 450090.43)])
def test_1_dk1_results_unchanged(tmp_path, spec_name, profit):
    text = (DK1 / "specs" / f"{spec_name}.txt").read_text()
    text = text.replace("inputs/", f"{DK1}/inputs/").replace("output_csv = outputs/", "output_csv = ")
    spec = tmp_path / "spec.txt"
    spec.write_text(text)
    res = run_tool(spec)
    assert res.returncode == 0, res.stderr
    report = next(tmp_path.glob("dispatch_results*.txt")).read_text()
    assert report_value(report, "Total profit") == pytest.approx(profit, abs=0.005)


# ------------------------------------------------------------------ check 10: scale (opt-in)

# Faster-ageing curves for the scale run: end of life (70% SoH) after 5 years at 2 cycles/day,
# 6 years at 1.5 and 7 years at 1, so a 7-year price horizon is covered by the longest curve.
FAST_AGEING = {1.0: 0.3 / 7, 1.5: 0.05, 2.0: 0.06}


@pytest.mark.skipif(os.environ.get("RUN_SLOW") != "1", reason="set RUN_SLOW=1 to run the 7-year scale test")
def test_11_seven_year_horizon_solves(tmp_path, capsys):
    start = time.time()
    m, _, setup = solve(tmp_path, 7, loss=FAST_AGEING, eol=0.7)
    elapsed = time.time() - start
    cycles = [pyo.value(m.throughput[p]) / NOMINAL for p in m.P]
    soh = [c / NOMINAL for c in physical_caps(m, setup)]
    with capsys.disabled():
        print(f"\n  7 years, {len(setup.periods.of_step):,} hours: build + solve {elapsed:.0f} s"
              f"\n  cycles per year: {', '.join(f'{c:.0f}' for c in cycles)}"
              f"\n  SoH at year end: {', '.join(f'{v:.3f}' for v in soh[1:])}")
    assert len(setup.periods.hours) == 7
    assert soh[-1] >= 0.7 - 1e-6


# ------------------------------------------------------------------ cumulative method

def _lookup(curves, years=3, calendar=None):
    periods = bdo.build_periods(timeline("2030-01-01", years), "year")
    return bdo.build_soh_lookup(curves, periods, calendar)


def test_cumulative_lookup_interpolates_between_rate_columns():
    # At age 2 an average of 1.25 cycles/day reads halfway between the 1 and 1.5 curves.
    curves = {r: [1.0 - l * y for y in range(4)] for r, l in LOSS.items()}
    lookup, warnings = _lookup(curves)
    assert not warnings
    n = 365 * 2 * 1.25
    assert lookup.soh(1, n) == pytest.approx(1 - 0.5 * (0.02 + 0.03) * 2)
    for r, l in LOSS.items():                                         # every curve point is exact
        assert lookup.soh(2, 365 * 3 * r) == pytest.approx(1 - l * 3)
    assert lookup.soh(0, 0.0) == pytest.approx(1.0)                   # extrapolated, capped at 1


def test_cumulative_lookup_uses_calendar_curve_and_repairs():
    curves = {1.0: [1, 0.97, 0.94], 1.5: [1, 0.968, 0.936], 2.0: [1, 0.96, 0.92]}
    lookup, warnings = _lookup(curves, years=2, calendar=[1, 0.995, 0.99])
    assert lookup.calendar_soh == pytest.approx([0.995, 0.99])
    assert any("concave envelope" in w for w in warnings)
    slopes = [c1 for _, c1 in lookup.lines[0]]
    assert all(a >= b for a, b in zip(slopes, slopes[1:]))            # concave: steeper with N


def test_cumulative_path_depends_on_total_cycles_only():
    # 2/day then 1/day (avg 1.5 at age 2) ends where 1.5/day for two years ends.
    curves = {r: [1.0 - l * y for y in range(4)] for r, l in LOSS.items()}
    lookup, _ = _lookup(curves, years=2)
    setup = SimpleNamespace(method="cumulative", lookup=lookup)
    uneven = bdo.physical_capacity_path(setup, [730, 365], NOMINAL)[0]
    even = bdo.physical_capacity_path(setup, [547.5, 547.5], NOMINAL)[0]
    assert uneven[-1] == pytest.approx(even[-1]) == pytest.approx(NOMINAL * (1 - 0.03 * 2))
    assert uneven[1] == pytest.approx(NOMINAL * (1 - 0.05))           # year 1 read at 2/day


def test_cumulative_is_default(tmp_path):
    spec = {"degradation_curves_csv": str(write_curves(tmp_path / "c.csv", LOSS))}
    setup, info = bdo.prepare_degradation(spec, n_steps=8760, start="2030-01-01", timezone=TZ,
                                          initial_soc=0.5, lp_power_sharing=False)
    assert setup.method == info["method"] == "cumulative" and setup.params is None


@pytest.mark.parametrize("rate", [1.0, 2.0])
def test_cumulative_forced_cycling_follows_curve(tmp_path, rate):
    # A small terminal value makes capacity worth keeping, so the LP's capacities are tight (the
    # final-SoC rule is off: it rewards a lower end capacity, see README).
    m, _, setup = solve(tmp_path, 3, peaks=4, amp=200.0, max_cycles_per_day=rate,
                        degradation_method="cumulative", terminal_value_per_mwh=100,
                        final_soc_fraction=0)
    soh = [c / NOMINAL for c in physical_caps(m, setup)]
    assert soh == pytest.approx([1.0 - LOSS[rate] * y for y in range(4)], abs=1e-3)
    assert [pyo.value(m.cap[p]) for p in m.Pcap] == pytest.approx([s * NOMINAL for s in soh], abs=1e-4)


def test_cumulative_rate_capped_at_highest_curve(tmp_path):
    m, _, setup = solve(tmp_path, 1, peaks=4, amp=300.0, degradation_method="cumulative",
                        terminal_value_per_mwh=0)
    assert pyo.value(m.throughput[0]) / NOMINAL <= 2.0 * 365 + 1e-6


def test_cumulative_duals_match_finite_differences(tmp_path):
    kw = dict(amp=40.0, terminal_value_per_mwh=50_000, discount_rate=0.05, final_soc_fraction=0,
              degradation_method="cumulative")
    m0, metrics, setup = solve(tmp_path, 2, **kw)
    value, cost = metrics["capacity_value"][0], metrics["degradation_cost_pv"][0]
    assert value > 0 and cost > 0
    lk = setup.lookup

    def resolve(lines):
        bumped = bdo.SohLookup(**{**lk.__dict__, "lines": lines})
        s2 = bdo.DegradationSetup(**{**setup.__dict__, "lookup": bumped})
        idx = timeline("2030-01-01", 2)
        m, _, _ = bdo.build_and_solve(
            bdo.resolve_prices(daily_prices(idx, amp=40.0, peaks=2), None, None), power_mw=1.0,
            capacity_mwh=NOMINAL, round_trip_efficiency=0.9, charge_tariff=0.0,
            discharge_tariff=0.0, max_cycles=None, prices_start_date=idx[0], degradation=s2,
        )
        return pyo.value(m.npv) - pyo.value(m0.npv)

    eps = 1e-3                                                        # 1e-3 of nominal at end of year 1
    lines = [[(c0 - eps, c1) for c0, c1 in lk.lines[0]]] + lk.lines[1:]
    assert resolve(lines) == pytest.approx(-value * NOMINAL * eps, rel=0.05)
    delta = 2.0                                                       # 2 extra cycles from year 1 on
    lines = [[(c0 + c1 * delta, c1) for c0, c1 in per] for per in lk.lines]
    assert resolve(lines) == pytest.approx(-cost * NOMINAL * delta, rel=0.05)


def test_cumulative_end_to_end_outputs(tmp_path):
    idx = timeline("2030-01-01", 2)
    write_dated(tmp_path / "p.csv", idx, daily_prices(idx, peaks=2))
    write_curves(tmp_path / "curves.csv", LOSS)
    spec = write_spec(tmp_path / "s.txt", {
        "output_csv": "out.csv", "power": 1, "capacity_mwh": 2, "round_trip_efficiency": 0.9,
        "prices_csv": "p.csv", "endogenous_degradation": "true",
        "degradation_curves_csv": "curves.csv", "terminal_value_per_mwh": 30000,
    })
    res = run_tool(spec)
    assert res.returncode == 0, res.stderr + res.stdout
    periods = pd.read_csv(tmp_path / "out_periods.csv")
    report = (tmp_path / "out.txt").read_text()
    assert "cumulative" in report
    assert {"cumulative_cycles", "avg_cycles_per_day_since_start", "deg_cost_eur_per_mwh"} <= set(periods.columns)
    avg = periods["avg_cycles_per_day_since_start"].iloc[-1]
    expected = 1 - 2 * np.interp(avg, [0, 1, 1.5, 2], [0, 0.02, 0.03, 0.05])   # curve value at age 2
    assert periods["capacity_end_mwh"].iloc[-1] == pytest.approx(NOMINAL * expected, abs=1e-4)
    loss = periods["calendar_loss_mwh"] + periods["cycle_loss_mwh"]
    assert loss.sum() == pytest.approx(NOMINAL - periods["capacity_end_mwh"].iloc[-1])


# ------------------------------------------------------------------ retirement choice

def test_retirement_year_chosen_by_npv(tmp_path):
    # End of life (0.7) after 3 years at 1 cycle/day, 2 at 1.5 and 1 at 2: candidates 1-3 years.
    idx = timeline("2030-01-01", 3)
    write_dated(tmp_path / "p.csv", idx, daily_prices(idx, peaks=4, amp=80.0))
    write_curves(tmp_path / "curves.csv", {1.0: 0.1, 1.5: 0.15, 2.0: 0.3}, eol=0.7)
    spec = write_spec(tmp_path / "s.txt", {
        "output_csv": "out.csv", "power": 1, "capacity_mwh": 2, "round_trip_efficiency": 0.9,
        "prices_csv": "p.csv", "endogenous_degradation": "true",
        "degradation_curves_csv": "curves.csv", "discount_rate": 0.3, "retirement_years": "1-3",
    })
    res = run_tool(spec)
    assert res.returncode == 0, res.stderr + res.stdout
    report = (tmp_path / "out.txt").read_text()
    npvs = {y: report_value(report, f"NPV, LP end capacity if {y} years") for y in (1, 2, 3)}
    chosen = int(report_value(report, "Chosen operating life"))
    assert chosen == max(npvs, key=npvs.get)
    assert report_value(report, "NPV (objective)") == pytest.approx(npvs[chosen], abs=0.01)
    periods = pd.read_csv(tmp_path / "out_periods.csv")
    assert len(periods) == chosen and len(pd.read_csv(tmp_path / "out.csv")) < len(idx) + (chosen == 3)
    assert periods["capacity_end_mwh"].iloc[-1] >= 0.7 * 2 - 1e-6
    with_print = res.stdout.splitlines()
    print("\n".join(l for l in with_print if "years:" in l))


def test_retirement_years_parsing():
    assert bdo.parse_year_list("18-20") == [18, 19, 20]
    assert bdo.parse_year_list("15, 18-19,15") == [15, 18, 19]
    with pytest.raises(SystemExit):
        bdo.parse_year_list("0-2")


# ------------------------------------------------------------------ death by zero curves

def _curves_with_death(path, loss, eol):
    """write_curves, then one more year at 0 per curve: the battery is dead after end of life."""
    write_curves(path, loss, eol)
    df = pd.read_csv(path)
    last = df.groupby("cycles_per_day")["year"].max().reset_index()
    dead = last.assign(year=last["year"] + 1, value=0.0)
    pd.concat([df, dead]).to_csv(path, index=False)
    return path


DEATH_LOSS = {1.0: 0.1, 1.5: 0.15, 2.0: 0.3}   # 70% after 3 / 2 / 1 years, 0 the year after


def test_trailing_zeros_mark_death_and_are_dropped(tmp_path):
    plain = bdo.load_degradation_curves(write_curves(tmp_path / "plain.csv", DEATH_LOSS, 0.7))
    dying = bdo.load_degradation_curves(_curves_with_death(tmp_path / "dying.csv", DEATH_LOSS, 0.7))
    assert dying == plain
    assert bdo.curves_end_of_life(dying) == pytest.approx(0.7)


def test_battery_death_mip_matches_retirement_search(tmp_path, capsys):
    # One model with a yes/no alive switch per year must find the same NPV as solving one LP per
    # possible death year.
    idx = timeline("2030-01-01", 3)
    write_dated(tmp_path / "p.csv", idx, daily_prices(idx, peaks=4, amp=80.0))
    _curves_with_death(tmp_path / "curves.csv", DEATH_LOSS, 0.7)
    common = {
        "power": 1, "capacity_mwh": 2, "round_trip_efficiency": 0.9, "prices_csv": "p.csv",
        "endogenous_degradation": "true", "degradation_curves_csv": "curves.csv",
        "discount_rate": 0.3, "final_soc_fraction": 0, "extrapolate_below_lowest_rate": "false",
    }
    mip = write_spec(tmp_path / "spec_mip.txt", {
        **common, "output_csv": "mip.csv", "allow_battery_death": "true", "mip_rel_gap": 1e-6})
    search = write_spec(tmp_path / "spec_search.txt", {
        **common, "output_csv": "search.csv", "retirement_years": "1-3"})
    for spec in (mip, search):
        res = run_tool(spec)
        assert res.returncode == 0, res.stderr + res.stdout
    mip_report = (tmp_path / "mip.txt").read_text()
    search_report = (tmp_path / "search.txt").read_text()
    npv_mip = report_value(mip_report, "NPV (objective)")
    npv_search = report_value(search_report, "NPV (objective)")
    life_mip = int(report_value(mip_report, "Operating life"))
    life_search = int(report_value(search_report, "Chosen operating life"))
    with capsys.disabled():
        print(f"\n  MIP: NPV {npv_mip:,.2f}, life {life_mip} y | search: NPV {npv_search:,.2f}, "
              f"life {life_search} y")
    assert npv_mip == pytest.approx(npv_search, rel=1e-5)
    # Years 2 and 3 tie here (the battery is used up after year 2), so either may be chosen; the
    # MIP's choice must be as good as the best in the search.
    npv_search_at_mip_life = report_value(search_report, f"NPV, LP end capacity if {life_mip} years")
    assert npv_search_at_mip_life == pytest.approx(npv_search, rel=1e-5)
    periods = pd.read_csv(tmp_path / "mip_periods.csv")
    assert periods["alive"].tolist() == [y <= life_mip for y in (1, 2, 3)]
    if life_mip < 3:
        assert periods["capacity_end_mwh"].iloc[-1] == 0.0
        assert pd.read_csv(tmp_path / "mip_yearly.csv")["net_eur"].iloc[-1] == pytest.approx(0.0, abs=1.0)


def test_average_cycles_limits_without_extrapolation(tmp_path):
    # Without data below the lowest rate, the average since commissioning stays within 1-2/day,
    # even when prices are too weak to want 1 cycle/day.
    m, _, setup = solve(tmp_path, 2, peaks=1, amp=1.0, degradation_method="cumulative",
                        extrapolate_below_lowest_rate="false")
    assert setup.min_average_rate == 1.0
    for p in m.P:
        avg = pyo.value(m.cum_cycles[p]) / (365 * setup.lookup.age_end[p])
        assert 1.0 - 1e-6 <= avg <= 2.0 + 1e-6
