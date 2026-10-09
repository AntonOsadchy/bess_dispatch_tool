"""Input formats: dated (year,month,day,hour,value) vs legacy single-column, DST, validation."""
from pathlib import Path

import pandas as pd
import pytest

import bess_dispatch_opt as bdo
from synthetic import (
    TZ, daily_prices, report_value, run_tool, timeline, write_curves, write_dated,
    write_legacy, write_spec,
)


def test_dated_parses_both_dst_changes(tmp_path):
    idx = timeline("2030-01-01", 1)          # 2030: spring 31 Mar, autumn 27 Oct
    path = write_dated(tmp_path / "p.csv", idx, list(range(len(idx))))
    raw = pd.read_csv(path)
    autumn = raw[(raw.month == 10) & (raw.day == 27)]
    spring = raw[(raw.month == 3) & (raw.day == 31)]
    assert (autumn.hour == 2).sum() == 2 and len(autumn) == 25
    assert (spring.hour == 2).sum() == 0 and len(spring) == 23

    series = bdo.load_timeseries_csv(path, TZ, "prices")
    assert len(series.values) == len(idx) == 8760
    assert (series.index == idx).all()


def test_legacy_format_still_loads(tmp_path):
    path = write_legacy(tmp_path / "p.csv", [1.0, 2.5, 3.0])
    series = bdo.load_timeseries_csv(path, TZ, "prices")
    assert series.values == [1.0, 2.5, 3.0] and series.index is None


@pytest.mark.parametrize("drop", ["gap", "duplicate"])
def test_dated_rejects_gaps_and_duplicates(tmp_path, drop):
    idx = timeline("2030-06-01", 1 / 12)
    df = pd.DataFrame({"year": idx.year, "month": idx.month, "day": idx.day, "hour": idx.hour, "value": 1.0})
    df = df.drop(index=100) if drop == "gap" else pd.concat([df.iloc[:101], df.iloc[100:]])
    path = tmp_path / "p.csv"
    df.to_csv(path, index=False)
    with pytest.raises(SystemExit):
        bdo.load_timeseries_csv(path, TZ, "prices")


def test_misaligned_dated_inputs_rejected():
    a = timeline("2030-01-01", 1 / 12)
    b = a + pd.Timedelta(hours=1)
    with pytest.raises(SystemExit):
        bdo.resolve_timeline({"buy_prices_csv": a, "sell_prices_csv": b}, None, TZ)
    with pytest.raises(SystemExit):
        bdo.resolve_timeline({"prices_csv": a}, "2030-01-02", TZ)
    assert bdo.resolve_timeline({"prices_csv": a}, "2030-01-01", TZ)[0] == a[0]


def test_dated_and_legacy_give_identical_results(tmp_path):
    idx = timeline("2030-01-01", 1)
    prices = daily_prices(idx, peaks=2)
    common = {"power": 1, "capacity_mwh": 2, "round_trip_efficiency": 0.9, "max_cycles_per_day": 2}
    write_dated(tmp_path / "dated.csv", idx, prices)
    write_legacy(tmp_path / "legacy.csv", prices)
    write_spec(tmp_path / "dated.txt", {**common, "output_csv": "out_dated.csv", "prices_csv": "dated.csv"})
    write_spec(tmp_path / "legacy.txt", {
        **common, "output_csv": "out_legacy.csv", "prices_csv": "legacy.csv",
        "prices_start_date": "2030-01-01",
    })
    for spec in ("dated.txt", "legacy.txt"):
        res = run_tool(tmp_path / spec)
        assert res.returncode == 0, res.stderr + res.stdout
    a = pd.read_csv(tmp_path / "out_dated.csv")
    b = pd.read_csv(tmp_path / "out_legacy.csv")
    pd.testing.assert_frame_equal(a, b)
    ra = (tmp_path / "out_dated.txt").read_text()
    rb = (tmp_path / "out_legacy.txt").read_text()
    assert report_value(ra, "Total profit") == report_value(rb, "Total profit")


def _curves(rows):
    return pd.DataFrame(rows, columns=["cycles_per_day", "year", "value"])


@pytest.mark.parametrize(
    "rows",
    [
        [(0, 0, 1), (0, 1, 0.98)],                                                        # rate 0
        [(r, 0, 1) for r in (1, 1.5, 2)] + [(1, 1, 1.01), (1.5, 1, 0.97), (2, 1, 0.96)],  # SoH rises
        [(r, 0, 0.9) for r in (1, 1.5, 2)] + [(r, 1, 0.8) for r in (1, 1.5, 2)],          # year 0 != 1
        [(r, y, 1 - 0.01 * y) for r in (1, 1.5, 2) for y in (0, 1, 3)],                   # year gap
    ],
)
def test_curve_file_validation(tmp_path, rows):
    path = tmp_path / "c.csv"
    _curves(rows).to_csv(path, index=False)
    with pytest.raises(SystemExit):
        bdo.load_degradation_curves(path)


def test_curve_file_loads(tmp_path):
    path = write_curves(tmp_path / "c.csv", {1.0: 0.02, 1.5: 0.03, 2.0: 0.05}, eol=0.8)
    curves = bdo.load_degradation_curves(path)
    assert [len(curves[r]) - 1 for r in (1.0, 1.5, 2.0)] == [10, 7, 4]  # faster cycling, shorter life
    assert curves[1.5] == pytest.approx([1.0, 0.97, 0.94, 0.91, 0.88, 0.85, 0.82, 0.8])
    assert bdo.curves_end_of_life(curves) == pytest.approx(0.8)


def test_curves_must_end_at_same_soh(tmp_path):
    rows = [(r, y, 1 - l * y) for r, l in ((1, 0.02), (1.5, 0.03), (2, 0.045)) for y in range(4)]
    path = tmp_path / "c.csv"
    _curves(rows).to_csv(path, index=False)                            # ends 0.94 / 0.91 / 0.865
    with pytest.raises(SystemExit):
        bdo.load_degradation_curves(path)


def test_wide_curve_layout_and_other_rates(tmp_path):
    path = tmp_path / "wide.csv"
    path.write_text("year,1,1.25,1.5\n1,0.9,0.85,0.7\n2,0.8,0.7,\n3,0.7,,\n")
    curves = bdo.load_degradation_curves(path)
    assert sorted(curves) == [1.0, 1.25, 1.5]
    assert curves[1.0] == pytest.approx([1.0, 0.9, 0.8, 0.7]) and curves[1.5] == pytest.approx([1.0, 0.7])
    params, _ = bdo.derive_degradation_params(curves)
    assert params[1].widths == pytest.approx((365.0, 91.25, 91.25))  # 0-1, 1-1.25, 1.25-1.5 cycles/day


def test_curve_fit_key_rejected(tmp_path):
    spec = {"degradation_curves_csv": str(write_curves(tmp_path / "c.csv", {1.0: 0.02, 1.5: 0.03})),
            "degradation_curve_fit": "power"}
    with pytest.raises(SystemExit):
        bdo.prepare_degradation(spec, n_steps=100, start="2030-01-01", timezone=TZ,
                                initial_soc=0.5, lp_power_sharing=False)

