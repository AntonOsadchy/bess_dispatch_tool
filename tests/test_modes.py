"""How the tool picks its mode from the inputs, and discounting without degradation."""
import pandas as pd
import pytest

from synthetic import daily_prices, report_value, run_tool, timeline, write_curves, write_dated, write_spec

BATTERY = {"power": 1, "capacity_mwh": 2, "round_trip_efficiency": 0.9}
LONG_LIFE = {1.0: 0.02, 1.5: 0.03, 2.0: 0.05}     # lives 15 / 10 / 6 years at EOL 0.7
SHORT_LIFE = {1.0: 0.1, 1.5: 0.15, 2.0: 0.3}      # lives 3 / 2 / 1 years at EOL 0.7


def _prices(tmp_path, years, **kw):
    idx = timeline("2030-01-01", years)
    write_dated(tmp_path / "p.csv", idx, daily_prices(idx, **kw))
    return idx


def _run(tmp_path, name, keys):
    spec = write_spec(tmp_path / f"{name}.txt", {**BATTERY, "prices_csv": "p.csv",
                                                 "output_csv": f"{name}.csv", **keys})
    res = run_tool(spec)
    assert res.returncode == 0, res.stderr + res.stdout
    return res, (tmp_path / f"{name}.txt").read_text()


def test_curves_switch_degradation_on_without_the_key(tmp_path):
    _prices(tmp_path, 2, peaks=2)
    write_curves(tmp_path / "c.csv", LONG_LIFE)
    _, report = _run(tmp_path, "auto", {"degradation_curves_csv": "c.csv"})
    assert "--- Degradation ---" in report and "multi-curve" in report
    # Prices (2 years) end before the fastest curve's life (6 years): no search, must survive.
    assert "must survive the price horizon" in report
    assert (tmp_path / "auto_periods.csv").is_file()


def test_explicit_off_ignores_curves(tmp_path):
    _prices(tmp_path, 1, peaks=2)
    write_curves(tmp_path / "c.csv", LONG_LIFE)
    _run(tmp_path, "off", {"degradation_curves_csv": "c.csv", "endogenous_degradation": "false"})
    _run(tmp_path, "plain", {})
    assert not (tmp_path / "off_periods.csv").exists()
    assert (tmp_path / "off.csv").read_bytes() == (tmp_path / "plain.csv").read_bytes()


def test_zero_discount_rate_changes_nothing(tmp_path):
    _prices(tmp_path, 1, peaks=2)
    _run(tmp_path, "zero", {"discount_rate": 0})
    _run(tmp_path, "plain", {})
    assert (tmp_path / "zero.csv").read_bytes() == (tmp_path / "plain.csv").read_bytes()
    assert (tmp_path / "zero.txt").read_bytes() == (tmp_path / "plain.txt").read_bytes()


def test_discounting_without_degradation(tmp_path):
    # Identical prices every year: year 2 earns as much as year 1, so the NPV at 10% is about
    # profit × (1 + 1/1.1) / 2. Total profit stays undiscounted.
    _prices(tmp_path, 2, peaks=2)
    _, plain = _run(tmp_path, "plain", {"prices_start_date": "2030-01-01"})
    _, disc = _run(tmp_path, "disc", {"discount_rate": 0.1})
    profit = report_value(disc, "Total profit")
    assert profit == pytest.approx(report_value(plain, "Total profit"), rel=1e-6)
    assert report_value(disc, "NPV (discounted profit)") == pytest.approx(profit * (1 + 1 / 1.1) / 2, rel=0.01)


def test_several_curves_run_the_retirement_search(tmp_path):
    _prices(tmp_path, 3, peaks=4, amp=80.0)
    write_curves(tmp_path / "c.csv", SHORT_LIFE)
    keys = {"degradation_curves_csv": "c.csv", "discount_rate": 0.3, "final_soc_fraction": 0}
    res, auto = _run(tmp_path, "auto", keys)
    _, explicit = _run(tmp_path, "explicit", {**keys, "retirement_years": "1-3"})
    assert "retirement search 1-3 years (3 candidates)" in auto
    npvs = {y: report_value(auto, f"NPV, LP end capacity if {y} years") for y in (1, 2, 3)}
    chosen = int(report_value(auto, "Chosen operating life"))
    assert chosen == int(report_value(explicit, "Chosen operating life"))
    # On a tie the shorter life wins, and the reported NPV is the winner's, re-solved if needed.
    assert npvs[chosen] == pytest.approx(max(npvs.values()), rel=1e-6)
    assert all(npvs[y] < npvs[chosen] * (1 - 1e-6) for y in npvs if y < chosen)
    assert report_value(auto, "NPV (objective)") == pytest.approx(npvs[chosen], rel=1e-6)
    assert len(pd.read_csv(tmp_path / "auto_periods.csv")) == chosen
    if chosen != 3:
        assert "Re-solving the chosen lifetime" in res.stdout


def test_retirement_search_can_be_switched_off(tmp_path):
    _prices(tmp_path, 3, peaks=2)
    write_curves(tmp_path / "c.csv", SHORT_LIFE)
    _, report = _run(tmp_path, "off", {"degradation_curves_csv": "c.csv", "retirement_years": "off"})
    assert "must survive the price horizon" in report and "Chosen operating life" not in report


def test_single_curve_runs_exactly_its_lifetime(tmp_path):
    _prices(tmp_path, 4, peaks=4, amp=80.0)                       # prices longer than the curve
    write_curves(tmp_path / "c.csv", {1.0: 0.1}, eol=0.7)         # one rate, 3-year life
    res, report = _run(tmp_path, "single", {"degradation_curves_csv": "c.csv", "final_soc_fraction": 0})
    assert "single curve" in report and "fixed 3 years" in report
    assert "later prices are not used" in res.stdout
    periods = pd.read_csv(tmp_path / "single_periods.csv")
    assert len(periods) == 3
    assert (periods["capacity_end_mwh"] / 2).tolist() == pytest.approx([0.9, 0.8, 0.7], abs=1e-6)
    assert (periods["avg_cycles_per_day_since_start"] <= 1.0 + 1e-6).all()   # the curve's rate
    assert periods["cycle_loss_mwh"].abs().max() < 1e-6                      # loss is by age only


def test_single_curve_needs_prices_for_its_whole_life(tmp_path):
    _prices(tmp_path, 2, peaks=2)
    write_curves(tmp_path / "c.csv", {1.0: 0.1}, eol=0.7)
    spec = write_spec(tmp_path / "s.txt", {**BATTERY, "prices_csv": "p.csv", "output_csv": "s.csv",
                                           "degradation_curves_csv": "c.csv"})
    res = run_tool(spec)
    assert res.returncode != 0 and "covers 3 years" in res.stderr + res.stdout
    spec = write_spec(tmp_path / "r.txt", {**BATTERY, "prices_csv": "p.csv", "output_csv": "r.csv",
                                           "degradation_curves_csv": "c.csv", "retirement_years": "1-2"})
    res = run_tool(spec)
    assert res.returncode != 0 and "single" in res.stderr + res.stdout
