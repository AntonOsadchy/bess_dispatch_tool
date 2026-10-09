"""Synthetic inputs for the tests, written in the tool's input formats.

Nothing here is real market data: prices are smooth daily shapes and the SoH curves are linear
in age with made-up yearly losses.
"""
from __future__ import annotations

import math
import subprocess
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "src" / "bess_dispatch_opt.py"
TZ = "Europe/Copenhagen"


def timeline(start: str, years: float, tz: str = TZ) -> pd.DatetimeIndex:
    """Hourly local timestamps from start for the given number of years (DST-aware)."""
    begin = pd.Timestamp(start, tz=tz)
    end = (begin.tz_localize(None) + pd.DateOffset(months=round(years * 12))).tz_localize(tz)
    n = int((end - begin) / pd.Timedelta(hours=1))
    return pd.date_range(begin, periods=n, freq="h")


def daily_prices(index: pd.DatetimeIndex, peaks: int = 1, base: float = 60.0, amp: float = 50.0) -> list[float]:
    """Smooth daily price shape with `peaks` peaks per day (by local hour)."""
    return [base + amp * math.sin(2 * math.pi * peaks * (ts.hour - 3) / 24) for ts in index]


def write_dated(path: Path, index: pd.DatetimeIndex, values: list[float]) -> Path:
    """Write year,month,day,hour,value (local time; the repeated autumn hour appears twice)."""
    pd.DataFrame(
        {
            "year": index.year,
            "month": index.month,
            "day": index.day,
            "hour": index.hour,
            "value": values,
        }
    ).to_csv(path, index=False)
    return path


def write_legacy(path: Path, values: list[float]) -> Path:
    pd.DataFrame(values).to_csv(path, index=False, header=False)
    return path


def write_curves(path: Path, loss: dict[float, float], eol: float = 0.7, years: int = 3) -> Path:
    """cycles_per_day,year,value curves, each running until it reaches the common end-of-life SoH.

    SoH = 1 − L·y per rate, clamped at `eol` in the year it is reached, so faster cycling gives a
    shorter curve and every curve ends at `eol`. With zero loss the curves are flat at 1.0 for
    `years` years and `eol` must be 1.0.
    """
    rows = []
    for rate, per_year in loss.items():
        if per_year == 0:
            assert eol == 1.0, "flat curves end at 1.0"
            values = [1.0] * (years + 1)
        else:
            n = math.ceil((1.0 - eol) / per_year - 1e-9)
            values = [max(eol, 1.0 - per_year * y) for y in range(n + 1)]
        rows += [{"cycles_per_day": rate, "year": y, "value": v} for y, v in enumerate(values)]
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


def write_year_value(path: Path, values: dict[int, float]) -> Path:
    pd.DataFrame({"year": list(values), "value": list(values.values())}).to_csv(path, index=False)
    return path


def write_spec(path: Path, keys: dict[str, object]) -> Path:
    path.write_text("".join(f"{k} = {v}\n" for k, v in keys.items()), encoding="utf-8")
    return path


def run_tool(spec: Path, tool: Path = TOOL) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(tool), "--spec", str(spec)],
        capture_output=True, text=True, cwd=spec.parent,
    )


def report_value(report: str, label: str) -> float:
    """Numeric value of a `  Label : value unit` report line."""
    for line in report.splitlines():
        if " : " in line and line.split(" : ")[0].strip() == label:
            return float(line.split(" : ")[1].split()[0])
    raise KeyError(label)
