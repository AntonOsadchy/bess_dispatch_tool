"""
Minimal BESS arbitrage LP: Pyomo + HiGHS.
Decision variables ch_mwh / dsch_mwh are grid-side MWh per timestep.
Usable energy capacity is capacity_mwh from specification; state variable soc_mwh is stored energy in MWh.
SOC: Δsoc_mwh = grid_import * η_leg − grid_export / η_leg with η_leg = √(round_trip_efficiency).
Using the spec value on both legs as η (instead of η_leg) would imply grid-to-grid round-trip η², not η.
CSV: grid_import_mwh / grid_export_mwh at the meter; charge_mwh / discharge_mwh are stored-side MWh
(stored gain = grid_import × η_leg; stored loss to deliver export = grid_export / η_leg), so Δsoc_mwh ≈ charge_mwh − discharge_mwh.
Each price CSV row is treated as one hour (see INTERVAL_HOURS).
Optional max equivalent cycles (whole horizon and/or per calendar day); charge/discharge tariffs apply to grid MWh
(same units as prices). Optional mutual exclusion of charging and discharging within a timestep (binary per timestep).

Buy / sell prices: charging is paid at the buy price (+ charge tariff), discharging earns the sell
price (− discharge tariff). Spec keys buy_prices_csv / sell_prices_csv supply them; prices_csv is the
fallback for whichever side is missing and, in co-location mode, the reference price for curtailment,
generation revenue and the wasted-generation refund (see resolve_prices). A single series (any one of
the three keys) is used for buy and sell alike.

Endogenous degradation (optional, endogenous_degradation = true): capacity becomes a variable per
degradation period (year or month) that falls with calendar ageing and cycling, using convex cycle bands
derived from SoH curves; cash flows are discounted per age year. Pure LP, solved with HiGHS interior
point. See add_degradation_block and README § "Endogenous degradation".

Inputs can be headerless single-column CSVs or dated CSVs (year,month,day,hour,value); see
load_timeseries_csv.

Grid connection limits (optional, independent):
  grid_import_mw  — limits how much the BESS can draw from the grid each interval.
  grid_export_mw  — limits how much the BESS can export to the grid each interval.

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
      discharge_tariff treatment differs between the three BTM sources:

        a) gen_avail[t] = min(generation_mwh[t] − gen_curt[t], export_connection_dt)
           Exportable generation used for BTM charging instead of direct export.
           Discharging this energy later replaces export that would have happened anyway
           — no new net export is created, so discharge_tariff is EXEMPT.

        b) gen_curt[t] = generation_mwh[t] if price_curt[t] ≤ curtailment_threshold else 0
           Generation that is fully curtailed for the hour (e.g. negative-price hours) —
           it would not have been exported at all, so there is no export it could displace.
           Discharging BESS energy sourced from it creates NEW export, but the source
           itself was worthless (would have been wasted), so discharge_tariff APPLIES
           (same treatment as surplus, no refund — see ch_from_gen_avail below).

        c) gen_surplus[t] = max(0, (generation_mwh[t] − gen_curt[t]) − export_connection_dt)
           Clipped, non-curtailed generation that cannot be exported (connection full).
           Discharging this energy creates NEW export that would not otherwise occur
           — it physically crosses the export meter, so discharge_tariff APPLIES.

      Auxiliary variables:
          ch_grid_mwh[t]:         grid-imported share of charging [B4, C4]
          ch_from_gen_avail[t]:   BTM charging sourced from gen_avail[t] [B5, C5]
                                  (the discharge_tariff-exempt portion of BTM charging)
          ch_from_gen_curt[t]:    BTM charging sourced from gen_curt[t] [B6, C5]
          ch_from_gen_surplus[t]: BTM charging sourced from gen_surplus[t] [B7, C5]

      ch_from_gen_avail[t] + ch_from_gen_curt[t] + ch_from_gen_surplus[t] == ch_btm[t] (C5), each
      individually capped by its own source (B5-B7). Summing those three caps and substituting C5
      shows ch_grid_mwh[t] >= ch_mwh[t] − gen_avail[t] − gen_curt[t] − gen_surplus[t] automatically
      — no separate lower-bound constraint on ch_grid_mwh is needed. ch_from_gen_curt and
      ch_from_gen_surplus get identical (no-refund) tariff treatment, so the LP has no preference
      between them — only their sum matters economically; ch_from_gen_avail is driven to its
      natural value min(ch_btm[t], gen_avail[t]) because, unlike the other two, it earns back
      discharge_tariff per MWh (except when price_buy[t] is negative enough that grid import itself
      becomes more profitable than free BTM charging — see README for the full derivation).

     Objective in co-location mode adds, on top of price_sell[t] × dsch_mwh[t] − price_buy[t] × ch_mwh[t]:
         + price_refund[t] × (ch_from_gen_curt[t] + ch_from_gen_surplus[t])   — spot opportunity-cost
             refund: this BTM-charged energy would have been wasted (curtailed or clipped)
             regardless, so — unlike gen_avail-sourced charging, which forgoes real export
             revenue — it has zero true opportunity cost.
         − charge_tariff × ch_grid_mwh[t]
         − discharge_tariff × dsch_mwh[t]
         + discharge_tariff × ch_from_gen_avail[t]
"""

from __future__ import annotations

import argparse
import math
import os
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pyomo.environ as pyo
from pyomo.opt import SolverStatus, TerminationCondition


# Default initial SOC (fraction of capacity_mwh), used when initial_soc is not set in the spec.
DEFAULT_INITIAL_SOC = 0.5
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


def spec_bool(spec: dict[str, str], key: str, default: bool = False) -> bool:
    val = spec_optional_str(spec, key)
    if val is None:
        return default
    if val.lower() in ("1", "true", "yes"):
        return True
    if val.lower() in ("0", "false", "no"):
        return False
    sys.exit(f"{key} must be true or false")


# Header of a dated time-series CSV: one row per hour, hour = 0-23, hour-beginning, local time.
DATED_COLUMNS = ["year", "month", "day", "hour", "value"]


@dataclass(frozen=True)
class TimeSeries:
    """Values of an hourly input series, plus its timestamps when the file was in dated format."""

    values: list[float]
    index: pd.DatetimeIndex | None  # tz-aware, one per value; None for headerless single-column files


def load_timeseries_csv(path: Path, timezone: str, what: str) -> TimeSeries:
    """Load an hourly input series in either supported format.

    Dated format: header `year,month,day,hour,value`, hour 0-23 (hour-beginning, local time in
    `timezone`). Rows must form a gap-free hourly sequence; DST is handled as in exchange data:
    on the autumn change the repeated hour appears twice (in order), on the spring change the
    skipped hour is absent.
    Legacy format: headerless single column, one value per row; non-numeric rows are dropped.
    """
    with path.open(encoding="utf-8-sig") as f:
        header = [c.strip().lower() for c in f.readline().strip().split(",")]
    if header != DATED_COLUMNS:
        df = pd.read_csv(path, header=None)
        if df.shape[1] < 1:
            sys.exit(f"No columns in {path}")
        s = pd.to_numeric(df.iloc[:, 0], errors="coerce").dropna()
        if s.empty:
            sys.exit(f"No numeric {what} in {path}")
        return TimeSeries(s.astype(float).tolist(), None)

    df = pd.read_csv(path, encoding="utf-8-sig")
    df.columns = [c.strip().lower() for c in df.columns]
    if df.empty:
        sys.exit(f"No rows in {path}")
    if df[DATED_COLUMNS].isna().any().any():
        sys.exit(f"{path}: missing values in year/month/day/hour/value")
    values = pd.to_numeric(df["value"], errors="coerce")
    if values.isna().any():
        sys.exit(f"{path}: non-numeric value in row {int(values.isna().idxmax()) + 2}")
    try:
        local = pd.DatetimeIndex(pd.to_datetime(df[["year", "month", "day", "hour"]].astype(int)))
        index = local.tz_localize(timezone, ambiguous="infer", nonexistent="raise")
    except Exception as e:  # invalid dates, hours outside 0-23, unresolvable DST
        sys.exit(f"{path}: timestamps are not a valid hourly sequence in {timezone} ({e})")
    if len(index) > 1:
        steps = np.diff(index.asi8) / 3.6e12
        bad = np.flatnonzero(steps != INTERVAL_HOURS)
        if bad.size:
            i = int(bad[0])
            sys.exit(
                f"{path}: rows {i + 2} and {i + 3} ({local[i]} -> {local[i + 1]}) are not "
                "consecutive hours. Dated inputs must be a gap-free hourly sequence."
            )
    return TimeSeries(values.astype(float).tolist(), index)


@dataclass(frozen=True)
class PriceSeries:
    """Per-timestep power prices (€/MWh) as used by the model.

    buy    — paid on charging (grid import), charge tariff is added on top.
    sell   — earned on discharging (grid export), discharge tariff is deducted.
    curt   — co-location: price compared against the curtailment threshold and used for
             generation revenue.
    refund — co-location: price refunded on charging from curtailed / clipped generation.
    """

    buy: list[float]
    sell: list[float]
    curt: list[float]
    refund: list[float]

    def __len__(self) -> int:
        return len(self.buy)


def truncate_prices(prices: PriceSeries, n_steps: int) -> PriceSeries:
    """The first n_steps hours of a price series."""
    if n_steps >= len(prices):
        return prices
    return PriceSeries(
        buy=prices.buy[:n_steps], sell=prices.sell[:n_steps],
        curt=prices.curt[:n_steps], refund=prices.refund[:n_steps],
    )


def parse_year_list(raw: str) -> list[int]:
    """Parse "18-20" or "15,18,20" (or a mix) into sorted, unique positive whole years."""
    years: set[int] = set()
    try:
        for part in raw.split(","):
            part = part.strip()
            if "-" in part:
                lo, hi = (int(v) for v in part.split("-"))
                years.update(range(lo, hi + 1))
            elif part:
                years.add(int(part))
    except ValueError:
        sys.exit(f"retirement_years: expected e.g. 18-20 or 15,18,20 (got '{raw}')")
    if not years or min(years) < 1:
        sys.exit(f"retirement_years: need whole years >= 1 (got '{raw}')")
    return sorted(years)


def resolve_prices(
    buy: list[float] | None,
    sell: list[float] | None,
    ref: list[float] | None,
) -> PriceSeries:
    """Combine the optional buy_prices_csv / sell_prices_csv / prices_csv series.

    A missing buy or sell side falls back to prices_csv, then to the other side, so a single
    series is used for both. prices_csv (ref) is the co-location reference price for curtailment,
    generation revenue and the refund; without it curtailment and generation revenue use the sell
    price and the refund uses the buy price.
    """
    given = {
        name: series
        for name, series in (
            ("buy_prices_csv", buy),
            ("sell_prices_csv", sell),
            ("prices_csv", ref),
        )
        if series is not None
    }
    if not given:
        sys.exit("Specify at least one of prices_csv, buy_prices_csv, sell_prices_csv in specification.txt")
    if len({len(series) for series in given.values()}) > 1:
        lengths = ", ".join(f"{name}={len(series)}" for name, series in given.items())
        sys.exit(f"Price series lengths do not match ({lengths}). Align the CSVs to the same period.")

    def first(*candidates: list[float] | None) -> list[float]:
        return next(c for c in candidates if c is not None)

    price_buy = first(buy, ref, sell)
    price_sell = first(sell, ref, buy)
    return PriceSeries(
        buy=price_buy,
        sell=price_sell,
        curt=first(ref, price_sell),
        refund=first(ref, price_buy),
    )


def make_timeline(
    n: int,
    start: str | pd.Timestamp,
    timezone: str = "Europe/Copenhagen",
    interval_hours: float = INTERVAL_HOURS,
) -> pd.DatetimeIndex:
    """DST-aware local timestamps for n consecutive intervals from start.

    start is a tz-aware Timestamp (e.g. the first row of a dated input) or a date /
    'YYYY-MM-DD HH:MM' string in local time (the prices_start_date spec key).
    """
    if isinstance(start, pd.Timestamp) and start.tzinfo is not None:
        start_ts = start.tz_convert(timezone)
    else:
        start_ts = pd.Timestamp(start, tz=timezone)
    return pd.date_range(start=start_ts, periods=n, freq=pd.Timedelta(hours=interval_hours))


def day_labels(
    n: int,
    *,
    start_date: str | pd.Timestamp | None = None,
    timezone: str = "Europe/Copenhagen",
    interval_hours: float = INTERVAL_HOURS,
) -> list[int]:
    """Consecutive calendar-day index (0, 1, 2, ...) for each of n rows.

    With start_date (a date or a 'YYYY-MM-DD HH:MM' datetime, local time, or a tz-aware Timestamp)
    rows are mapped to real local calendar dates via a DST-aware index, so a spring-forward day has
    23 rows and a fall-back day 25. Without it, rows are chunked into fixed 24/interval_hours-row
    blocks from row 0.
    """
    if start_date is not None:
        idx = make_timeline(n, start_date, timezone, interval_hours)
        return pd.factorize(idx.date)[0].tolist()
    steps_per_day = round(24.0 / interval_hours)
    return [i // steps_per_day for i in range(n)]


def average_daily_price_spread(
    sell_prices: list[float],
    buy_prices: list[float],
    *,
    start_date: str | pd.Timestamp | None = None,
    timezone: str = "Europe/Copenhagen",
    interval_hours: float = INTERVAL_HOURS,
) -> float:
    """Average, over all days in the series, of each day's (max sell price − min buy price).

    With a single price series (buy == sell) this is the plain daily max − min.

    When start_date is given, rows are mapped to real local calendar dates via a
    DST-aware datetime index — a spring-forward day has 23 rows and a fall-back day
    has 25, matching how spot-price data is actually timestamped (e.g. Europe/Copenhagen).
    Without start_date there is no way to know where calendar-day boundaries fall, so
    rows are chunked into fixed 24/interval_hours-row blocks from the start of the
    series instead; this silently drifts out of sync with real calendar days for
    months at a time around each DST transition, so prefer passing start_date whenever
    the series' first-row date is known.
    """
    labels = day_labels(
        len(sell_prices), start_date=start_date, timezone=timezone, interval_hours=interval_hours
    )
    daily = pd.Series(sell_prices).groupby(labels).max() - pd.Series(buy_prices).groupby(labels).min()
    return float(daily.mean()) if not daily.empty else float("nan")


def scale_generation_profile(values: list[float], max_mw: float | None = None) -> list[float]:
    """Turn a loaded generation profile into MWh per interval.

    When max_mw is provided the values are capacity factors [0–1]:
      clipped to [0, 1] and scaled → MWh per interval = cf × max_mw × INTERVAL_HOURS.
    When max_mw is None each value is used directly as MWh per interval.
    """
    if max_mw is None:
        return list(values)
    return [min(max(v, 0.0), 1.0) * max_mw * INTERVAL_HOURS for v in values]


def load_existing_profile_csv(path: Path) -> tuple[list[float], list[float]]:
    """Load a pre-committed dispatch profile from a named-column CSV.

    Expected columns: 'charge_mwh' (stored-side charging MWh per timestep) and
    'discharge_mwh' (stored-side discharging MWh per timestep) — matching the
    column names in this tool's own output CSV, so users can reference a prior
    run's output file directly.

    Returns (profile_ch_stored, profile_dsch_stored), both in stored-side MWh.
    """
    df = pd.read_csv(path)
    for col in ("charge_mwh", "discharge_mwh"):
        if col not in df.columns:
            sys.exit(
                f"Existing dispatch profile CSV '{path}' is missing required column '{col}'. "
                "Expected columns: charge_mwh, discharge_mwh"
            )
    ch = pd.to_numeric(df["charge_mwh"], errors="coerce").fillna(0.0).tolist()
    dsch = pd.to_numeric(df["discharge_mwh"], errors="coerce").fillna(0.0).tolist()
    if any(v < 0 for v in ch) or any(v < 0 for v in dsch):
        sys.exit(
            f"Existing dispatch profile CSV '{path}' contains negative values. "
            "All charge_mwh and discharge_mwh values must be >= 0."
        )
    return ch, dsch


# ---------------------------------------------------------------------------------------------
# Endogenous degradation: inputs and preprocessing (see README § "Endogenous degradation")
# ---------------------------------------------------------------------------------------------

# Largest allowed difference between the curves' final SoH values (fraction of nominal).
EOL_TOLERANCE = 1e-4
# Days per age year used to turn cycles/day into cycles per year (band widths, curve points).
DAYS_PER_YEAR = 365.0


def _validate_soh(years: list[int], soh: list[float], what: str) -> list[float]:
    if years and years[0] == 1:  # age 0 omitted: a new battery is at SoH 1.0
        years, soh = [0] + years, [1.0] + soh
    if years != list(range(len(years))) or len(years) < 2:
        sys.exit(f"{what}: years must be consecutive integers from 0 (got {years[:5]}...)")
    if abs(soh[0] - 1.0) > 1e-6:
        sys.exit(f"{what}: value at year 0 must be 1.0 (got {soh[0]})")
    for y in range(1, len(soh)):
        if soh[y] > soh[y - 1] + 1e-12:
            sys.exit(f"{what}: SoH increases between year {y - 1} and {y}")
    return soh


def load_degradation_curves(path: Path) -> dict[float, list[float]]:
    """Load SoH curves, one per cycling rate (equivalent full cycles per day).

    Two layouts are accepted:
      long: header `cycles_per_day, year, value`, one row per rate and year;
      wide: header `year, <rate>, <rate>, ...` (e.g. `year,1,1.25,1.5`), one column per rate,
            blank once a curve has ended.
    year = battery age in years (0 = commissioning, value 1.0; a missing year 0 is added), value =
    state of health as a fraction of nominal energy. Trailing zeros (dead battery) are dropped. At
    least two rates are needed. Each curve runs
    until end of life, so curves may have different lengths, but they must all end at the same SoH
    (the end-of-life limit). Returns {rate: [SoH(0), SoH(1), ...]} in ascending rate order.
    """
    df = pd.read_csv(path, encoding="utf-8-sig")
    df.columns = [str(c).strip().lower() for c in df.columns]
    raw: dict[float, tuple[list[int], list[float]]] = {}
    if {"cycles_per_day", "year", "value"} <= set(df.columns):
        for rate, sub in df.groupby(df["cycles_per_day"].astype(float)):
            sub = sub.dropna(subset=["value"]).sort_values("year")
            raw[float(rate)] = (sub["year"].astype(int).tolist(), sub["value"].astype(float).tolist())
    elif "year" in df.columns and len(df.columns) >= 3:
        for col in df.columns.drop("year"):
            try:
                rate = float(col)
            except ValueError:
                sys.exit(f"{path}: column '{col}' is not a cycling rate (cycles per day)")
            sub = df[["year", col]].dropna().sort_values("year")
            raw[rate] = (sub["year"].astype(int).tolist(), sub[col].astype(float).tolist())
    else:
        sys.exit(
            f"{path}: expected columns cycles_per_day,year,value or year,<rate>,<rate>,... "
            "(e.g. year,1,1.5,2)"
        )
    if len(raw) < 2 or min(raw) <= 0:
        sys.exit(f"{path}: need curves for at least two positive cycling rates (got {sorted(raw)})")
    # A curve may end with 0 to mark that the battery is dead from then on (as in a table that
    # switches capacity to 0 below end of life). Those zeros are dropped: the curve ends at its last
    # real value, and past it the model continues with the curve's last yearly loss.
    for rate, (years, values) in raw.items():
        while len(values) > 1 and values[-1] <= 0:
            years.pop()
            values.pop()
    curves = {
        rate: _validate_soh(years, values, f"{path} ({rate:g} cycles/day)")
        for rate, (years, values) in sorted(raw.items())
    }
    ends = {rate: c[-1] for rate, c in curves.items()}
    if max(ends.values()) - min(ends.values()) > EOL_TOLERANCE:
        sys.exit(
            f"{path}: the curves must end at the same SoH, which is used as the end-of-life limit "
            f"(got {', '.join(f'{v:.4f} at {r:g} cycles/day' for r, v in ends.items())})."
        )
    return curves


def curves_end_of_life(curves: dict[float, list[float]]) -> float:
    """End-of-life SoH: the value all curves end at (checked by load_degradation_curves)."""
    return min(c[-1] for c in curves.values())


def load_year_value_csv(path: Path, what: str) -> dict[int, float]:
    """Load a CSV with header `year, value` into {year: value}."""
    df = pd.read_csv(path, encoding="utf-8-sig")
    df.columns = [c.strip().lower() for c in df.columns]
    if {"year", "value"} - set(df.columns):
        sys.exit(f"{path}: expected columns year, value for {what}")
    return dict(zip(df["year"].astype(int), df["value"].astype(float)))


def lower_convex_envelope(xs: list[float], ys: list[float]) -> list[float]:
    """Values at xs (ascending) of the lower convex hull of the points (xs, ys)."""
    hull: list[tuple[float, float]] = []
    for x, y in zip(xs, ys):
        while len(hull) >= 2:
            (ox, oy), (ax, ay) = hull[-2], hull[-1]
            if (ax - ox) * (y - oy) - (ay - oy) * (x - ox) <= 0:
                hull.pop()
            else:
                break
        hull.append((x, y))
    hx, hy = zip(*hull)
    return [float(np.interp(x, hx, hy)) for x in xs]


@dataclass(frozen=True)
class YearDegradation:
    """Degradation parameters for one battery age year, as fractions of nominal energy."""

    calendar: float                  # lost over a full year with no cycling
    damage: tuple[float, ...]        # lost per equivalent full cycle, one value per band
    widths: tuple[float, ...]        # band widths in cycles per full age year


def derive_degradation_params(
    curves: dict[float, list[float]],
    calendar_curve: list[float] | None = None,
) -> tuple[dict[int, YearDegradation], list[str]]:
    """Calendar loss and per-cycle damage for each age year from the SoH curves.

    With rates r1 < r2 < ... (cycles/day), Loss[y,r] = SoH_r(y-1) − SoH_r(y); a curve that ends
    before year y keeps its last-year loss. Calendar[y] comes from the zero-cycling curve when
    given, otherwise by linear extrapolation through the r1 and r2 points to 0 cycles/day:
    max(0, Loss[y,r1] − r1·(Loss[y,r2] − Loss[y,r1])/(r2 − r1)) (= 3·Loss[1] − 2·Loss[1.5] for rates
    1 and 1.5). Band k runs from r(k-1) to r(k) cycles/day (r0 = 0), width 365·(r(k) − r(k-1))
    cycles per year, and its damage per cycle is the slope between the two points. Non-convex points
    (damage not non-decreasing) are replaced by their lower convex envelope; negative damage is
    floored at 0. Returns the parameters and any warnings.
    """
    rates = sorted(curves)
    n_years = max(len(c) for c in curves.values()) - 1
    if calendar_curve is not None:
        n_years = min(n_years, len(calendar_curve) - 1)

    def curve_loss(c: list[float], y: int) -> float:
        return c[y - 1] - c[y] if y < len(c) else c[-2] - c[-1]

    points = [0.0] + list(rates)
    xs = [DAYS_PER_YEAR * r for r in points]
    widths = tuple(xs[k + 1] - xs[k] for k in range(len(rates)))
    params: dict[int, YearDegradation] = {}
    warnings: list[str] = []
    repaired: list[int] = []
    floored: list[int] = []
    for y in range(1, n_years + 1):
        loss = [curve_loss(curves[r], y) for r in rates]
        if calendar_curve is not None:
            cal = calendar_curve[y - 1] - calendar_curve[y]
        else:
            r1, r2 = rates[0], rates[1]
            cal = max(0.0, loss[0] - r1 * (loss[1] - loss[0]) / (r2 - r1))
        ys = [cal] + loss
        slopes = [(ys[k + 1] - ys[k]) / widths[k] for k in range(len(rates))]
        if any(slopes[k] > slopes[k + 1] + 1e-15 for k in range(len(rates) - 1)):
            repaired.append(y)
            ys = lower_convex_envelope(xs, ys)
            slopes = [(ys[k + 1] - ys[k]) / widths[k] for k in range(len(rates))]
        if min(slopes) < 0:
            floored.append(y)
            slopes = [max(0.0, v) for v in slopes]
        params[y] = YearDegradation(calendar=ys[0], damage=tuple(slopes), widths=widths)

    def years_text(ys: list[int]) -> str:
        return f"year {ys[0]}" if len(ys) == 1 else f"{len(ys)} years ({ys[0]}-{ys[-1]})"

    if repaired:
        warnings.append(
            f"Damage per cycle is not increasing across bands in {years_text(repaired)}; "
            "using the lower convex envelope (faster cycling then costs the same per cycle)."
        )
    if floored:
        warnings.append(f"Negative damage per cycle floored at 0 in {years_text(floored)}.")
    return params, warnings


@dataclass(frozen=True)
class Periods:
    """Degradation periods. Years are age years from the first timestep (anniversaries), months
    are months since the first timestep, so 12 months nest exactly in each age year."""

    of_step: list[int]           # period index (0-based) of each timestep
    start: list[pd.Timestamp]    # local start of each period
    hours: list[int]             # hours of data in each period
    age_year: list[int]          # 1-based battery age year of each period
    year_fraction: list[float]   # hours / hours in that full age year (1 for a full year)
    calendar_month: list[int]    # calendar month (1-12) the period starts in
    n_years: float               # horizon length in age years (fractional for a partial year)


def build_periods(timeline: pd.DatetimeIndex, freq: str) -> Periods:
    """Assign each hourly timestep to a degradation period ("year" or "month")."""
    tz = timeline.tz
    start = timeline[0]
    naive_start = start.tz_localize(None)
    end = timeline[-1] + pd.Timedelta(hours=INTERVAL_HOURS)

    def boundaries(unit: str) -> list[pd.Timestamp]:
        out = [start]
        k = 1
        while out[-1] < end:
            out.append(
                (naive_start + pd.DateOffset(**{unit: k})).tz_localize(
                    tz, ambiguous=True, nonexistent="shift_forward"
                )
            )
            k += 1
        return out

    year_bounds = boundaries("years")
    bounds = year_bounds if freq == "year" else boundaries("months")
    year_ns = np.array([b.value for b in year_bounds])
    bound_ns = np.array([b.value for b in bounds])
    of_step = np.searchsorted(bound_ns, timeline.asi8, side="right") - 1
    n_periods = int(of_step[-1]) + 1
    hours = np.bincount(of_step, minlength=n_periods)
    full_year_hours = np.diff(year_ns) / 3.6e12
    age_year = (np.searchsorted(year_ns, bound_ns[:n_periods], side="right")).tolist()
    year_fraction = [float(hours[p] / full_year_hours[age_year[p] - 1]) for p in range(n_periods)]
    year_of_step = np.searchsorted(year_ns, timeline.asi8, side="right")
    hours_per_year = np.bincount(year_of_step)[1:]
    n_years = float(sum(hours_per_year[i] / full_year_hours[i] for i in range(len(hours_per_year))))
    return Periods(
        of_step=of_step.tolist(),
        start=[bounds[p] for p in range(n_periods)],
        hours=hours.tolist(),
        age_year=age_year,
        year_fraction=year_fraction,
        calendar_month=[bounds[p].month for p in range(n_periods)],
        n_years=n_years,
    )


@dataclass(frozen=True)
class PeriodDegradation:
    calendar: list[float]                       # fraction of nominal lost to calendar ageing
    widths: list[tuple[float, ...]]    # band widths in cycles
    damage: list[tuple[float, ...]]    # fraction of nominal lost per cycle, per band


def period_degradation_params(
    periods: Periods,
    yearly: dict[int, YearDegradation],
    month_multipliers: list[float] | None = None,
) -> PeriodDegradation:
    """Scale yearly parameters to periods.

    Calendar loss and band widths scale with the period's share of its age year (hours-based, so
    DST, leap years and partial first/last periods are exact); damage per cycle is unchanged.
    month_multipliers (12 values, monthly periods only) scale both calendar loss and damage, e.g.
    for temperature effects.
    """
    max_year = max(periods.age_year)
    if max_year not in yearly:
        sys.exit(
            f"The longest degradation curve covers {max(yearly)} years but the price horizon "
            f"needs {max_year}."
        )
    cal, widths, damage = [], [], []
    for p, y in enumerate(periods.age_year):
        f = periods.year_fraction[p]
        mult = month_multipliers[periods.calendar_month[p] - 1] if month_multipliers else 1.0
        cal.append(yearly[y].calendar * f * mult)
        widths.append(tuple(w * f for w in yearly[y].widths))
        damage.append(tuple(d * mult for d in yearly[y].damage))
    return PeriodDegradation(calendar=cal, widths=widths, damage=damage)


def discount_factor(age_year: float, rate: float, convention: str = "end") -> float:
    """1/(1+r)^(y−1) for convention "end" (as specified), 1/(1+r)^(y−0.5) for "mid"."""
    exponent = age_year - (0.5 if convention == "mid" else 1.0)
    return 1.0 / (1.0 + rate) ** exponent


def soh_at_age(curve: list[float], age: float) -> float:
    """SoH of a yearly curve at a fractional age: linear between years, and past the curve's end
    continuing with its last-year loss (a curve that ends at 0 stays at 0: the battery is dead)."""
    last = len(curve) - 1
    if age <= last:
        return float(np.interp(age, np.arange(last + 1), curve))
    if curve[-1] <= 0:
        return 0.0
    return curve[-1] - (curve[-2] - curve[-1]) * (age - last)


def upper_concave_envelope(xs: list[float], ys: list[float]) -> list[float]:
    """Values at xs (ascending) of the upper concave hull of the points (xs, ys)."""
    return [-v for v in lower_convex_envelope(xs, [-y for y in ys])]


@dataclass(frozen=True)
class SohLookup:
    """Cumulative degradation: SoH at the end of each period as a function of the cycles since
    commissioning, SoH ≤ c0 + c1·N for every line (c0, c1) of that period (a concave piecewise-
    linear function of N, so the model stays an LP)."""

    age_end: list[float]                         # battery age in years at the end of each period
    rate_points: list[float]                     # cycles/day of the lookup points (0 first)
    soh_points: list[list[float]]                # per period: SoH at each rate point (after repair)
    raw_points: list[list[float]]                # per period: SoH at each rate point (curves as given)
    lines: list[list[tuple[float, float]]]       # per period: (c0, c1), N in cycles
    calendar_soh: list[float]                    # SoH with no cycling at the end of each period
    max_rate: float                              # highest rate in the curves, cycles/day

    def soh(self, p: int, cycles: float) -> float:
        return min(c0 + c1 * cycles for c0, c1 in self.lines[p])

    def soh_curves(self, p: int, cycles: float) -> float:
        """SoH interpolated from the curve points without the concave envelope (for reporting)."""
        xs = [DAYS_PER_YEAR * self.age_end[p] * r for r in self.rate_points]
        return float(np.interp(cycles, xs, self.raw_points[p]))


def build_soh_lookup(
    curves: dict[float, list[float]],
    periods: Periods,
    calendar_curve: list[float] | None = None,
    extrapolate_below_lowest_rate: bool = True,
) -> tuple[SohLookup, list[str]]:
    """Lookup of SoH against cumulative cycles at the end of each period.

    At age a (years since commissioning) a battery that has averaged r cycles/day has done
    N = 365·a·r cycles, so each curve gives the point (365·a·r_i, SoH_ri(a)). A zero-cycling point
    comes from the calendar curve, or else by linear extrapolation through the two lowest rates
    (capped at 1). SoH at any N is the linear interpolation between neighbouring points, i.e. the
    curve value at the average cycling rate since commissioning. With
    extrapolate_below_lowest_rate off there is no zero-cycling estimate: below the lowest rate the
    lowest curve's value is used (curves taken exactly as given). Points are first made
    non-increasing in N and then replaced by their upper concave envelope where needed.
    """
    rates = sorted(curves)
    n_years = max(len(c) for c in curves.values()) - 1
    if calendar_curve is not None:
        n_years = min(n_years, len(calendar_curve) - 1)
    age_end = list(np.cumsum(periods.year_fraction))
    if age_end[-1] > n_years + 1e-9:
        sys.exit(
            f"The longest degradation curve covers {n_years} years but the price horizon "
            f"needs {age_end[-1]:.2f}."
        )
    # A curve may drop to 0 after its end of life (the battery is dead from then on). It only
    # informs the zero-cycling extrapolation up to its last non-zero year.
    life = {
        r: max(y for y, v in enumerate(c) if v > 0) if c[-1] <= 0 else math.inf
        for r, c in curves.items()
    }
    points = [0.0] + rates
    soh_points, raw_points, lines, calendar_soh = [], [], [], []
    repaired: list[int] = []
    for p, a in enumerate(age_end):
        s = [soh_at_age(curves[r], a) for r in rates]
        alive = [r for r in rates if a <= life[r] + 1e-9]
        if calendar_curve is not None:
            s0 = soh_at_age(calendar_curve, a)
        elif not extrapolate_below_lowest_rate:
            s0 = s[0]
        elif len(alive) >= 2:
            r1, r2 = alive[0], alive[1]
            s1, s2 = soh_at_age(curves[r1], a), soh_at_age(curves[r2], a)
            s0 = min(1.0, s1 + r1 * (s1 - s2) / (r2 - r1))
        else:
            # Fewer than two curves still alive: continue the zero-cycling SoH along its last slope.
            prev = [1.0] + calendar_soh
            ages = [0.0] + age_end[:p]
            slope = (prev[-1] - prev[-2]) / (ages[-1] - ages[-2]) if p >= 2 else 0.0
            s0 = prev[-1] + slope * (a - ages[-1])
        if calendar_curve is None and extrapolate_below_lowest_rate and calendar_soh:
            s0 = min(s0, calendar_soh[-1])       # an estimate, so never let it rise with age
        ys = [max(s0, s[0])] + s
        ys = list(np.minimum.accumulate(ys))
        xs = [DAYS_PER_YEAR * a * r for r in points]
        env = upper_concave_envelope(xs, ys)
        if max(abs(e - y) for e, y in zip(env, ys)) > 1e-9 or ys != [max(s0, s[0])] + s:
            repaired.append(p)
        segs: list[tuple[float, float]] = []
        for k in range(len(xs) - 1):
            c1 = (env[k + 1] - env[k]) / (xs[k + 1] - xs[k])
            if segs and abs(segs[-1][1] - c1) < 1e-12:
                continue                              # collinear with the previous segment
            segs.append((env[k] - c1 * xs[k], c1))
        soh_points.append(env)
        raw_points.append([float(y) for y in ys])
        lines.append(segs)
        calendar_soh.append(env[0])
    warnings = []
    if repaired:
        warnings.append(
            f"SoH is not concave/non-increasing in cumulative cycles in {len(repaired)} periods; "
            "using the upper concave envelope of the curve points."
        )
    lookup = SohLookup(
        age_end=age_end, rate_points=points, soh_points=soh_points, raw_points=raw_points,
        lines=lines,
        calendar_soh=calendar_soh, max_rate=rates[-1],
    )
    return lookup, warnings


@dataclass(frozen=True)
class DegradationSetup:
    """Everything build_and_solve needs to add the endogenous degradation block."""

    periods: Periods
    params: PeriodDegradation | None     # method "period": per-period calendar loss and bands
    df_step: list[float]          # discount factor of each timestep (of its age year)
    df_period: list[float]        # discount factor of each period
    df_end: float                 # discount factor at the end of the horizon (terminal value)
    a_ch: float = 0.0             # cycle weight on stored charging energy
    a_dis: float = 1.0            # cycle weight on stored discharging energy
    soc_min: float = 0.0          # SoC limits as fractions of degraded capacity
    soc_max: float = 1.0
    final_soc_fraction: float = 0.0   # SoC[last] >= this × end capacity
    eol_fraction: float | None = None     # end capacity >= this × nominal (from the curves)
    terminal_value_per_mwh: float = 0.0
    var_om_per_mwh: float = 0.0
    warranty_throughput_mwh: float | None = None
    lp_power_sharing: bool = False        # LP stand-in for C6 (no binaries)
    solver_method: str = "simplex"   # fastest reliable choice in tests (see README)
    run_crossover: str = "on"        # only used with ipm
    method: str = "period"           # "cumulative" (SoH from cycles since start) or "period"
    lookup: SohLookup | None = None  # method "cumulative"
    min_average_rate: float | None = None   # average cycles/day since start >= this while alive
    allow_death: bool = False        # one binary per age year: the battery may die (MIP)
    mip_time_limit_s: float | None = None
    mip_rel_gap: float = 1e-4


def add_degradation_block(
    m: pyo.ConcreteModel,
    setup: DegradationSetup,
    *,
    nominal: float,
    eta_leg: float,
    times: list[int],
    max_ch_mwh: float,
    max_dsch_mwh: float,
) -> None:
    """Capacity as a variable per period, the degradation link (cycle bands and capacity
    transitions, or the cumulative SoH lookup) and end conditions [C7-C12]. Pure LP unless
    allow_death adds one binary per age year [C12]."""
    per, par = setup.periods, setup.params
    n_periods = len(per.hours)
    steps: list[list[int]] = [[] for _ in range(n_periods)]
    for t, p in zip(times, per.of_step):
        steps[p].append(t)

    m.P = pyo.Set(initialize=range(n_periods))
    m.Pcap = pyo.Set(initialize=range(n_periods + 1))   # Cap[p] = start of period p, Cap[P] = end
    m.cap = pyo.Var(m.Pcap, bounds=(0.0, nominal))
    m.cap[0].fix(nominal)

    # [C12] alive[y] = 1 while the battery operates in age year y; once 0 it stays 0. A dead
    # battery has no capacity, so the SoC limits (C7) empty it before it dies; only the SoH lookup
    # needs relaxing, and only where its value could go negative (big-M kept as small as possible
    # so the relaxation stays tight).
    if setup.allow_death:
        m.Y = pyo.Set(initialize=sorted(set(per.age_year)))
        m.alive = pyo.Var(m.Y, domain=pyo.Binary)
        m.alive_order = pyo.Constraint(
            m.Y, rule=lambda mm, y: mm.alive[y + 1] <= mm.alive[y] if y + 1 in mm.Y
            else pyo.Constraint.Skip,
        )

    def alive(mm, p):
        return mm.alive[per.age_year[p]] if setup.allow_death else 1.0

    def dead(mm, p):
        return 1.0 - mm.alive[per.age_year[p]] if setup.allow_death else 0.0

    # [C7] SoC limited by the period's average degraded capacity (C1 keeps SoC continuous).
    def avg_cap(mm, t):
        p = per.of_step[t]
        return 0.5 * (mm.cap[p] + mm.cap[p + 1])

    m.soc_cap_ub = pyo.Constraint(
        m.T, rule=lambda mm, t: mm.soc_mwh[t] <= setup.soc_max * avg_cap(mm, t)
    )
    if setup.soc_min > 0:
        m.soc_cap_lb = pyo.Constraint(
            m.T, rule=lambda mm, t: mm.soc_mwh[t] >= setup.soc_min * avg_cap(mm, t)
        )
    if setup.allow_death:
        # A dead battery neither charges nor discharges.
        m.dead_no_ch = pyo.Constraint(
            m.T, rule=lambda mm, t: mm.ch_mwh[t] <= max_ch_mwh * alive(mm, per.of_step[t])
        )
        m.dead_no_dsch = pyo.Constraint(
            m.T, rule=lambda mm, t: mm.dsch_mwh[t] <= max_dsch_mwh * alive(mm, per.of_step[t])
        )
        # ... and has no capacity (also removes terminal value).
        m.dead_cap = pyo.Constraint(
            m.P, rule=lambda mm, p: mm.cap[p + 1] <= nominal * alive(mm, p)
        )

    # [C8] Cycle-weighted throughput per period (stored-side energy, same basis as C2).
    m.throughput = pyo.Expression(
        m.P,
        rule=lambda mm, p: sum(
            setup.a_ch * eta_leg * mm.ch_mwh[t] + setup.a_dis * mm.dsch_mwh[t] / eta_leg
            for t in steps[p]
        ),
    )

    if setup.method == "cumulative":
        lk = setup.lookup
        # [C8c] Cycles since commissioning at the end of each period.
        m.cum_cycles = pyo.Var(m.P, bounds=(0.0, None))
        m.cum_def = pyo.Constraint(
            m.P,
            rule=lambda mm, p: mm.cum_cycles[p] - (mm.cum_cycles[p - 1] if p > 0 else 0.0)
            - mm.throughput[p] / nominal == 0,
        )
        # [C9c] Capacity at the end of a period is at most the curves' SoH at the average cycling
        # rate since commissioning (concave piecewise-linear lookup: one constraint per segment).
        m.L = pyo.Set(initialize=[(p, i) for p in range(n_periods) for i in range(len(lk.lines[p]))])
        # Lowest value each line can take at the highest possible cycle count: a line only needs
        # relaxing for a dead battery if it can go below 0 (then capacity 0 would be infeasible).
        n_max = [lk.max_rate * DAYS_PER_YEAR * a for a in lk.age_end]
        big_m = {
            (p, i): max(0.0, -(c0 + min(0.0, c1) * n_max[p]))
            for p in range(n_periods) for i, (c0, c1) in enumerate(lk.lines[p])
        }
        m.soh_lookup = pyo.Constraint(
            m.L,
            rule=lambda mm, p, i: mm.cap[p + 1] - nominal * lk.lines[p][i][1] * mm.cum_cycles[p]
            <= nominal * lk.lines[p][i][0] + nominal * big_m[p, i] * dead(mm, p),
        )
        m.cap_monotone = pyo.Constraint(m.P, rule=lambda mm, p: mm.cap[p + 1] <= mm.cap[p])
        # The average cycling rate since commissioning stays within the curves' rates: at most the
        # highest rate, and (when there is no data below the lowest rate) at least the lowest rate
        # while the battery is alive.
        m.avg_rate_max = pyo.Constraint(
            m.P,
            rule=lambda mm, p: mm.cum_cycles[p] <= lk.max_rate * DAYS_PER_YEAR * lk.age_end[p],
        )
        if setup.min_average_rate is not None:
            m.avg_rate_min = pyo.Constraint(
                m.P,
                rule=lambda mm, p: mm.cum_cycles[p]
                >= setup.min_average_rate * DAYS_PER_YEAR * lk.age_end[p] * alive(mm, p),
            )
    else:
        # [C8] Cycles split into convex bands.
        m.K = pyo.Set(initialize=range(len(par.damage[0])))
        m.band = pyo.Var(m.P, m.K, bounds=lambda mm, p, k: (0.0, par.widths[p][k]))
        m.band_split = pyo.Constraint(
            m.P, rule=lambda mm, p: sum(mm.band[p, k] for k in mm.K) == mm.throughput[p] / nominal
        )

        # [C9] Capacity transition.
        def transition_rule(mm, p):
            loss = nominal * (par.calendar[p] + sum(par.damage[p][k] * mm.band[p, k] for k in mm.K))
            return mm.cap[p + 1] == mm.cap[p] - loss

        m.cap_transition = pyo.Constraint(m.P, rule=transition_rule)

    # [C10] End-of-horizon conditions and optional limits. With allow_death the end-of-life limit
    # holds at the end of every period the battery is alive in; a year that would end below it
    # is a dead year.
    if setup.eol_fraction is not None and setup.allow_death:
        m.eol_cap = pyo.Constraint(
            m.P, rule=lambda mm, p: mm.cap[p + 1] >= setup.eol_fraction * nominal * alive(mm, p)
        )
    elif setup.eol_fraction is not None:
        m.eol_cap = pyo.Constraint(expr=m.cap[n_periods] >= setup.eol_fraction * nominal)
    if setup.final_soc_fraction > 0:
        m.final_soc = pyo.Constraint(
            expr=m.soc_mwh[times[-1]] >= setup.final_soc_fraction * m.cap[n_periods]
        )
    if setup.warranty_throughput_mwh is not None:
        m.warranty = pyo.Constraint(
            expr=sum(m.throughput[p] for p in m.P) <= setup.warranty_throughput_mwh
        )

    # [C11] LP stand-in for C6: charging and discharging share one power budget in each hour.
    if setup.lp_power_sharing:
        m.power_sharing = pyo.Constraint(
            m.T,
            rule=lambda mm, t: mm.ch_mwh[t] * max_dsch_mwh + mm.dsch_mwh[t] * max_ch_mwh
            <= max_ch_mwh * max_dsch_mwh,
        )


def prepare_degradation(
    spec: dict[str, str],
    *,
    n_steps: int,
    start: str | pd.Timestamp | None,
    timezone: str,
    initial_soc: float,
    lp_power_sharing: bool,
) -> tuple[DegradationSetup, dict]:
    """Read the degradation spec keys, preprocess the curves and build the DegradationSetup.

    Returns the setup and a dict of report details (curve file, warnings, rates, conventions).
    """
    if start is None:
        sys.exit(
            "endogenous_degradation needs real timestamps: use dated inputs "
            "(year,month,day,hour,value) or set prices_start_date."
        )
    freq = (spec_optional_str(spec, "degradation_period") or "year").lower()
    if freq not in ("year", "month"):
        sys.exit("degradation_period must be year or month")
    method = (spec_optional_str(spec, "degradation_method") or "cumulative").lower()
    if method not in ("cumulative", "period"):
        sys.exit("degradation_method must be cumulative or period")
    curves_csv = spec_optional_str(spec, "degradation_curves_csv")
    if curves_csv is None:
        sys.exit("endogenous_degradation needs degradation_curves_csv")
    curves = load_degradation_curves(Path(curves_csv))
    eol = curves_end_of_life(curves)
    if "degradation_curve_fit" in spec:
        sys.exit(
            "degradation_curve_fit has been removed: degradation curves are always used as given. "
            "Remove the key."
        )

    calendar_csv = spec_optional_str(spec, "calendar_curve_csv")
    calendar_curve = None
    if calendar_csv is not None:
        raw = load_year_value_csv(Path(calendar_csv), "the calendar (zero-cycling) curve")
        years = sorted(raw)
        calendar_curve = _validate_soh(years, [raw[y] for y in years], calendar_csv)
    yearly: dict[int, YearDegradation] = {}
    warnings: list[str] = []
    if method == "period":
        yearly, warnings = derive_degradation_params(curves, calendar_curve)

    month_mult_raw = spec_optional_str(spec, "month_degradation_multipliers")
    month_mult = None
    if month_mult_raw is not None:
        month_mult = [float(v) for v in month_mult_raw.split(",")]
        if len(month_mult) != 12 or min(month_mult) < 0:
            sys.exit("month_degradation_multipliers needs 12 non-negative comma-separated values")
        if freq != "month" or method != "period":
            warnings.append(
                "month_degradation_multipliers ignored: they need degradation_period = month and "
                "degradation_method = period."
            )
            month_mult = None

    timeline = make_timeline(n_steps, start, timezone)
    periods = build_periods(timeline, freq)
    params, lookup = None, None
    if method == "period":
        params = period_degradation_params(periods, yearly, month_mult)
    else:
        lookup, lookup_warnings = build_soh_lookup(
            curves, periods, calendar_curve,
            extrapolate_below_lowest_rate=spec_bool(spec, "extrapolate_below_lowest_rate", default=True),
        )
        warnings += lookup_warnings

    rate = spec_float(spec, "discount_rate", default=0.0)
    convention = (spec_optional_str(spec, "discount_convention") or "end").lower()
    if convention not in ("end", "mid"):
        sys.exit("discount_convention must be end or mid")
    if rate <= -1:
        sys.exit("discount_rate must be > -1")
    df_period = [discount_factor(y, rate, convention) for y in periods.age_year]
    df_step = [df_period[p] for p in periods.of_step]
    df_end = 1.0 / (1.0 + rate) ** periods.n_years

    soc_min = spec_float(spec, "soc_min_fraction", default=0.0)
    soc_max = spec_float(spec, "soc_max_fraction", default=1.0)
    if not (0 <= soc_min < soc_max <= 1):
        sys.exit("soc_min_fraction and soc_max_fraction must satisfy 0 <= min < max <= 1")
    if not (soc_min <= initial_soc <= soc_max):
        sys.exit("initial_soc must lie between soc_min_fraction and soc_max_fraction")
    final_soc = spec_float(spec, "final_soc_fraction", default=initial_soc)

    if "eol_fraction" in spec:
        sys.exit(
            "eol_fraction is not a setting: the end-of-life limit is the SoH all degradation "
            "curves end at. Remove the key."
        )

    crossover = (spec_optional_str(spec, "run_crossover") or "on").lower()
    if crossover not in ("on", "off", "choose"):
        sys.exit("run_crossover must be on, off or choose")
    extrapolate_below = spec_bool(spec, "extrapolate_below_lowest_rate", default=True)
    allow_death = spec_bool(spec, "allow_battery_death", default=False)
    if allow_death and method != "cumulative":
        sys.exit("allow_battery_death needs degradation_method = cumulative")
    setup = DegradationSetup(
        periods=periods,
        params=params,
        df_step=df_step,
        df_period=df_period,
        df_end=df_end,
        a_ch=spec_float(spec, "cycle_weight_charge", default=0.0),
        a_dis=spec_float(spec, "cycle_weight_discharge", default=1.0),
        soc_min=soc_min,
        soc_max=soc_max,
        final_soc_fraction=final_soc,
        eol_fraction=eol,
        terminal_value_per_mwh=spec_float(spec, "terminal_value_per_mwh", default=0.0),
        var_om_per_mwh=spec_float(spec, "var_om_per_mwh", default=0.0),
        warranty_throughput_mwh=spec_optional_float(spec, "warranty_throughput_mwh"),
        lp_power_sharing=lp_power_sharing,
        solver_method=(spec_optional_str(spec, "solver_method") or "simplex").lower(),
        run_crossover=crossover,
        method=method,
        lookup=lookup,
        min_average_rate=min(curves) if method == "cumulative" and not extrapolate_below else None,
        allow_death=allow_death,
        mip_time_limit_s=spec_optional_float(spec, "mip_time_limit_s"),
        mip_rel_gap=spec_float(spec, "mip_rel_gap", default=1e-4),
    )
    info = {
        "freq": freq,
        "method": method,
        "curves_csv": curves_csv,
        "calendar_csv": calendar_csv,
        "warnings": warnings,
        "discount_rate": rate,
        "discount_convention": convention,
        "eol_fraction": eol,
        "rates": sorted(curves),
        "yearly": yearly,
    }
    return setup, info


def physical_capacity_path(
    setup: DegradationSetup,
    cycles: list[float],
    nominal: float,
    alive: list[bool] | None = None,
) -> tuple[list[float], list[list[float]]]:
    """Capacity path and band split implied by the cycles of each period.

    Method "cumulative": capacity at the end of period p is the lookup SoH at the cycles since
    commissioning (never rising), with no band split. Method "period": physical loss for a given number of cycles is the cheapest band fill (band 1 first, as damage
    is non-decreasing across bands). The LP reaches the same split whenever capacity has value to
    it; when it has none (no terminal value and SoC limits not binding) every split is optimal and
    the LP's own split can overstate the loss.
    """
    if setup.method == "cumulative":
        caps, total = [nominal], 0.0
        for p, c in enumerate(cycles):
            total += max(0.0, c)
            soh = setup.lookup.soh(p, total) if alive is None or alive[p] else 0.0
            caps.append(min(caps[-1], nominal * soh))
        return caps, [[] for _ in cycles]
    par = setup.params
    caps = [nominal]
    bands: list[list[float]] = []
    for p, c in enumerate(cycles):
        remaining = max(0.0, c)
        split = []
        for width in par.widths[p]:
            split.append(min(width, remaining))
            remaining -= split[-1]
        bands.append(split)
        loss = nominal * (par.calendar[p] + sum(d * b for d, b in zip(par.damage[p], split)))
        caps.append(caps[-1] - loss)
    return caps, bands


def degradation_results(
    model: pyo.ConcreteModel,
    setup: DegradationSetup,
    metrics: dict,
    *,
    nominal: float,
    eta_leg: float,
    timeline: pd.DatetimeIndex,
) -> tuple[dict[str, list], pd.DataFrame, pd.DataFrame]:
    """Per-hour extra columns, per-period table and per-age-year table after a degradation solve."""
    per, par = setup.periods, setup.params
    n_periods = len(per.hours)
    lp_cap = [pyo.value(model.cap[p]) for p in range(n_periods + 1)]
    cycles_by_period = [pyo.value(model.throughput[p]) / nominal for p in range(n_periods)]
    alive = None
    if setup.allow_death:
        alive = [pyo.value(model.alive[per.age_year[p]]) > 0.5 for p in range(n_periods)]
        # A tie the solver may break either way: trailing periods that are "alive" but idle at the
        # end-of-life limit earn exactly what a dead battery earns. Report them as dead.
        for p in reversed(range(n_periods)):
            if not alive[p]:
                continue
            idle = pyo.value(model.throughput[p]) < 1e-6 * nominal
            at_eol = pyo.value(model.cap[p + 1]) <= (setup.eol_fraction or 0.0) * nominal + 1e-6 * nominal
            if not (alive[p] and idle and at_eol):
                break
            alive[p] = False
    cap, band_split = physical_capacity_path(setup, cycles_by_period, nominal, alive)
    gap = max(abs(a - b) for a, b in zip(cap, lp_cap))
    if gap > 1e-6 * nominal:
        print(
            f"Note: the optimiser's capacity variables are up to {gap:.4f} MWh below the physical "
            "path because capacity had no value to it in some periods (no terminal value and SoC "
            "limits not binding). Reported capacity follows the physical path.",
            flush=True,
        )
    times = range(len(per.of_step))
    op_cash = [pyo.value(model.op_cash[t]) for t in times]
    dsch = [pyo.value(model.dsch_mwh[t]) for t in times]

    hourly = {
        "timestamp": [ts.strftime("%Y-%m-%d %H:%M%z") for ts in timeline],
        "period": [p + 1 for p in per.of_step],
        "age_year": [per.age_year[p] for p in per.of_step],
        "capacity_mwh_period": [0.5 * (cap[p] + cap[p + 1]) for p in per.of_step],
        "discount_factor": setup.df_step,
    }

    rows = []
    cumulative = 0.0
    for p in range(n_periods):
        throughput = pyo.value(model.throughput[p])
        cycles = cycles_by_period[p]
        value = metrics["capacity_value"][p]
        if setup.method == "cumulative":
            lk = setup.lookup
            cumulative += cycles
            cal_start = lk.calendar_soh[p - 1] if p > 0 else 1.0
            is_alive = alive is None or alive[p]
            calendar_loss = nominal * (cal_start - lk.calendar_soh[p]) if is_alive else 0.0
            death_loss = 0.0 if is_alive else cap[p] - cap[p + 1]
            rows.append({
                "period": p + 1,
                "start": per.start[p].strftime("%Y-%m-%d %H:%M%z"),
                "hours": per.hours[p],
                "age_year": per.age_year[p],
                "alive": is_alive,
                "age_end_years": lk.age_end[p],
                "capacity_start_mwh": cap[p],
                "capacity_end_mwh": cap[p + 1],
                # Same lookup without the concave envelope: differs where the curves are not
                # concave in cumulative cycles (see the warning).
                "capacity_end_curves_mwh": nominal * lk.soh_curves(p, cumulative),
                "throughput_mwh": throughput,
                "cycles": cycles,
                "cycles_per_day": cycles / (per.hours[p] / 24.0),
                "cumulative_cycles": cumulative,
                "avg_cycles_per_day_since_start": cumulative / (DAYS_PER_YEAR * lk.age_end[p]),
                "calendar_loss_mwh": calendar_loss,
                "cycle_loss_mwh": cap[p] - cap[p + 1] - calendar_loss - death_loss,
                "death_loss_mwh": death_loss,
                "discount_factor": setup.df_period[p],
                "capacity_value_pv_eur_per_mwh": value,
                # € (in period p money) per MWh of cycle-weighted throughput in period p: the NPV
                # lost because every later capacity limit tightens.
                "deg_cost_eur_per_mwh": metrics["degradation_cost_pv"][p] / setup.df_period[p],
            })
            continue
        bands = band_split[p]
        cycle_loss = nominal * sum(d * b for d, b in zip(par.damage[p], bands))
        row = {
            "period": p + 1,
            "start": per.start[p].strftime("%Y-%m-%d %H:%M%z"),
            "hours": per.hours[p],
            "age_year": per.age_year[p],
            "capacity_start_mwh": cap[p],
            "capacity_end_mwh": cap[p + 1],
            "throughput_mwh": throughput,
            "cycles": cycles,
            "cycles_per_day": cycles / (per.hours[p] / 24.0),
            **{f"cycles_band{k + 1}": b for k, b in enumerate(bands)},
            "calendar_loss_mwh": nominal * par.calendar[p],
            "cycle_loss_mwh": cycle_loss,
            "discount_factor": setup.df_period[p],
            "capacity_value_pv_eur_per_mwh": value,
        }
        # DegCost[p,k] = value of capacity × damage per cycle ÷ DF: € (in period p money) per MWh of
        # cycle-weighted throughput, since one cycle is `nominal` MWh and costs nominal·damage MWh.
        for k, d in enumerate(par.damage[p]):
            row[f"deg_cost_band{k + 1}_eur_per_mwh"] = value * d / setup.df_period[p]
        rows.append(row)
    periods_df = pd.DataFrame(rows)

    yearly = pd.DataFrame(
        {
            "age_year": [per.age_year[p] for p in per.of_step],
            "operating_cash_eur": op_cash,
            "var_om_eur": [setup.var_om_per_mwh * d for d in dsch],
            "discount_factor": setup.df_step,
        }
    )
    yearly["net_eur"] = yearly["operating_cash_eur"] - yearly["var_om_eur"]
    yearly["net_discounted_eur"] = yearly["net_eur"] * yearly["discount_factor"]
    yearly_df = (
        yearly.groupby("age_year")
        .agg(
            operating_cash_eur=("operating_cash_eur", "sum"),
            var_om_eur=("var_om_eur", "sum"),
            net_eur=("net_eur", "sum"),
            discount_factor=("discount_factor", "first"),
            net_discounted_eur=("net_discounted_eur", "sum"),
        )
        .reset_index()
    )
    return hourly, periods_df, yearly_df


def resolve_timeline(
    dated_inputs: dict[str, pd.DatetimeIndex | None],
    prices_start_date: str | None,
    timezone: str,
) -> pd.DatetimeIndex | None:
    """Common timestamps of the dated inputs (None if every input is in legacy format).

    All dated inputs must have identical timestamps; prices_start_date, if also given, must equal
    the first timestamp.
    """
    dated = {key: idx for key, idx in dated_inputs.items() if idx is not None}
    if not dated:
        return None
    (ref_key, ref), *others = dated.items()
    for key, idx in others:
        if len(idx) != len(ref) or not (idx == ref).all():
            i = next(
                (i for i in range(min(len(idx), len(ref))) if idx[i] != ref[i]),
                min(len(idx), len(ref)),
            )
            sys.exit(
                f"{key} and {ref_key} have different timestamps (first difference at row {i + 2}). "
                "Dated inputs must cover exactly the same hours."
            )
    if prices_start_date is not None:
        spec_start = make_timeline(1, prices_start_date, timezone)[0]
        if spec_start != ref[0]:
            sys.exit(
                f"prices_start_date ({spec_start}) does not match the first timestamp of the "
                f"dated inputs ({ref[0]}). Remove prices_start_date or correct it."
            )
    return ref


def solve_lp(m: pyo.ConcreteModel, method: str, crossover: str, dual_cons: list) -> dict:
    """Solve a pure LP with HiGHS (default dual simplex) and return the duals of dual_cons.

    The model must be a minimisation: with crossover off this HiGHS build reports status
    "Unknown" and flips dual signs on maximisation problems, but handles minimisation correctly.
    If the run without crossover is not optimal it is repeated with crossover on.
    """
    from pyomo.contrib.appsi.base import TerminationCondition as AppsiTC
    from pyomo.contrib.appsi.solvers import Highs

    opt = Highs()
    if not opt.available():
        sys.exit("HiGHS solver not available. Install highspy.")
    opt.config.load_solution = False
    attempts = [crossover] if (method != "ipm" or crossover == "on") else [crossover, "on"]
    for attempt in attempts:
        opt.highs_options = {"solver": method, "run_crossover": attempt}
        res = opt.solve(m)
        if res.termination_condition == AppsiTC.optimal:
            break
        if attempt != attempts[-1]:
            print(
                f"Note: HiGHS {method} without crossover ended '{res.termination_condition.name}'; "
                "retrying with crossover on.",
                flush=True,
            )
    else:
        hint = ""
        if "infeasible" in res.termination_condition.name.lower():
            hint = (
                " The end-of-life limit (the SoH the curves end at) may be unreachable over this "
                "horizon, e.g. if calendar loss alone takes capacity below it."
            )
        sys.exit(f"Solver did not finish optimally: termination={res.termination_condition.name}.{hint}")
    opt.load_vars()
    return opt.get_duals(dual_cons)


def solve_mip(m: pyo.ConcreteModel, setup: DegradationSetup) -> dict:
    """Solve the model with its alive binaries as a MIP (HiGHS branch and bound).

    Stops at mip_rel_gap or mip_time_limit_s; a time-limited run keeps the best solution found.
    Returns {"seconds", "gap", "time_limited"}.
    """
    from pyomo.contrib.appsi.base import TerminationCondition as AppsiTC
    from pyomo.contrib.appsi.solvers import Highs

    opt = Highs()
    if not opt.available():
        sys.exit("HiGHS solver not available. Install highspy.")
    opt.config.load_solution = False
    opt.highs_options = {"mip_rel_gap": setup.mip_rel_gap}
    opt.config.stream_solver = os.environ.get("BESS_SOLVER_LOG") == "1"
    if os.environ.get("BESS_SOLVER_LOG_FILE"):
        # HiGHS writes this file itself as it goes, so progress survives a killed run.
        opt.highs_options["log_file"] = os.environ["BESS_SOLVER_LOG_FILE"]
    if setup.mip_time_limit_s is not None:
        opt.config.time_limit = setup.mip_time_limit_s
    start = time.time()
    res = opt.solve(m)
    elapsed = time.time() - start
    time_limited = res.termination_condition == AppsiTC.maxTimeLimit
    has_solution = res.best_feasible_objective is not None and math.isfinite(res.best_feasible_objective)
    if not (res.termination_condition == AppsiTC.optimal or (time_limited and has_solution)):
        sys.exit(f"MIP did not finish: termination={res.termination_condition.name}.")
    opt.load_vars()
    best, bound = res.best_feasible_objective, res.best_objective_bound
    gap = abs(best - bound) / max(1e-9, abs(best)) if bound is not None and math.isfinite(bound) else float("nan")
    if time_limited:
        print(f"Note: MIP stopped at the time limit; best solution kept (gap {gap * 100:.3f} %).", flush=True)
    return {"seconds": elapsed, "gap": gap, "time_limited": time_limited}


def build_and_solve(
    prices: PriceSeries,
    *,
    power_mw: float,
    capacity_mwh: float,
    round_trip_efficiency: float,
    charge_tariff: float,
    discharge_tariff: float,
    max_cycles: float | None,
    max_cycles_per_day: float | None = None,
    day_index: list[int] | None = None,
    no_simultaneous_charge_discharge: bool = False,
    generation_mwh: list[float] | None = None,
    grid_import_mw: float | None = None,
    grid_export_mw: float | None = None,
    consumption_tariffs: list[float] | None = None,
    existing_dispatch_stored: tuple[list[float], list[float]] | None = None,
    curtailment_threshold: float | None = None,
    initial_soc: float = DEFAULT_INITIAL_SOC,
    # Only used for the avg_daily_spread_eur_per_mwh standard output metric
    # below (see average_daily_price_spread) - day boundaries elsewhere in
    # this function (the max_cycles_per_day constraint) come from the
    # caller's own day_index instead.
    prices_start_date: str | pd.Timestamp | None = None,
    prices_timezone: str = "Europe/Copenhagen",
    # Endogenous degradation (None = fixed capacity, the original model). When set, the model is
    # a pure LP solved with HiGHS and C6 must be off.
    degradation: DegradationSetup | None = None,
) -> tuple[pyo.ConcreteModel, pyo.SolverResults, dict]:
    rte = round_trip_efficiency
    if not (0 < rte <= 1):
        sys.exit("round_trip_efficiency must be in (0, 1]")
    if not (0 <= initial_soc <= 1):
        sys.exit("initial_soc must be in [0, 1]")
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

    if existing_dispatch_stored is not None:
        if len(existing_dispatch_stored[0]) != T:
            sys.exit(
                f"Existing dispatch profile length ({len(existing_dispatch_stored[0])}) does not match "
                f"price series length ({T}). Align the profile CSV to the same period."
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
    m.price_buy = pyo.Param(m.T, initialize={t: prices.buy[t] for t in times})
    m.price_sell = pyo.Param(m.T, initialize={t: prices.sell[t] for t in times})

    # Per-timestep consumption tariff: replaces scalar charge_tariff when supplied.
    if consumption_tariffs is not None:
        m.ctariff = pyo.Param(m.T, initialize={t: consumption_tariffs[t] for t in times})

    m.soc_mwh = pyo.Var(m.T, bounds=(0.0, cap))
    # ch_mwh: total charging (stand-alone: = grid import; co-location: BTM + grid).
    m.ch_mwh = pyo.Var(m.T, bounds=(0.0, max_ch_mwh))
    # dsch_mwh: grid export, always bounded by min(power_mw, grid_export_mw).
    m.dsch_mwh = pyo.Var(m.T, bounds=(0.0, max_dsch_mwh))

    # Constraints [B1-B7, C1-C5] and objective [O1-O2] — see README.md § "Optimisation model"

    def soc_rule(mm, i):
        if i == 0:
            return (
                mm.soc_mwh[0] - initial_soc * cap
                == mm.ch_mwh[0] * eta_leg - mm.dsch_mwh[0] / eta_leg
            )
        return (
            mm.ch_mwh[i] * eta_leg - mm.dsch_mwh[i] / eta_leg + mm.soc_mwh[i - 1]
            == mm.soc_mwh[i]
        )

    m.soc_cons = pyo.Constraint(m.T, rule=soc_rule)  # [C1]

    # Profile lower-bound constraints: force total dispatch to honour the committed profile.
    # The existing ch_mwh/dsch_mwh variables represent combined (profile + additional) dispatch.
    # The optimizer naturally maximises the additional component since the profile floor is fixed.
    if existing_dispatch_stored is not None:
        prof_ch_list, prof_dsch_list = existing_dispatch_stored
        prof_ch_ac   = {t: prof_ch_list[t]   / eta_leg for t in times}
        prof_dsch_ac = {t: prof_dsch_list[t] * eta_leg for t in times}
        m.profile_ch_param   = pyo.Param(m.T, initialize=prof_ch_ac)
        m.profile_dsch_param = pyo.Param(m.T, initialize=prof_dsch_ac)
        m.profile_ch_lb = pyo.Constraint(
            m.T, rule=lambda mm, t: mm.ch_mwh[t] >= mm.profile_ch_param[t]
        )
        m.profile_dsch_lb = pyo.Constraint(
            m.T, rule=lambda mm, t: mm.dsch_mwh[t] >= mm.profile_dsch_param[t]
        )

    if max_cycles is not None:

        def cycle_cap_rule(mm):
            return eta_leg * sum(mm.ch_mwh[t] for t in times) <= max_cycles * cap

        m.cycle_cap = pyo.Constraint(rule=cycle_cap_rule)  # [C2]

    if max_cycles_per_day is not None:
        if day_index is None or len(day_index) != T:
            sys.exit("max_cycles_per_day needs one calendar-day index per timestep")
        times_by_day: dict[int, list[int]] = {}
        for t, day in zip(times, day_index):
            times_by_day.setdefault(day, []).append(t)
        m.days = pyo.Set(initialize=sorted(times_by_day))

        def daily_cycle_cap_rule(mm, day):
            return (
                eta_leg * sum(mm.ch_mwh[t] for t in times_by_day[day])
                <= max_cycles_per_day * cap
            )

        m.daily_cycle_cap = pyo.Constraint(m.days, rule=daily_cycle_cap_rule)  # [C2d]

    if no_simultaneous_charge_discharge:
        # [C6] Charging and discharging are mutually exclusive within a timestep. Needed whenever
        # price_sell[t] can exceed price_buy[t]: without it the LP charges and discharges at full
        # power in the same hour and earns the spread on energy that never leaves the battery.
        m.is_charging = pyo.Var(m.T, domain=pyo.Binary)
        m.excl_ch = pyo.Constraint(
            m.T, rule=lambda mm, t: mm.ch_mwh[t] <= max_ch_mwh * mm.is_charging[t]
        )
        m.excl_dsch = pyo.Constraint(
            m.T, rule=lambda mm, t: mm.dsch_mwh[t] <= max_dsch_mwh * (1 - mm.is_charging[t])
        )

    # Co-location constraints.
    if generation_mwh is not None:
        # Export connection reference: grid_export_mw if set, otherwise power_mw.
        export_connection_dt = (
            grid_export_mw if grid_export_mw is not None else power_mw
        ) * dt

        # Refund price for charging from curtailed / clipped generation (see colocation_addendum_term).
        m.price_refund = pyo.Param(m.T, initialize={t: prices.refund[t] for t in times})

        # Curtailed generation: fully curtailed (would not be exported at all) whenever the
        # curtailment reference price is at or below curtailment_threshold — e.g. negative-price
        # hours. Falls back to discharge_tariff when no explicit threshold is supplied, matching
        # main()'s default.
        curt_threshold = (
            curtailment_threshold if curtailment_threshold is not None else discharge_tariff
        )
        gen_curt_param = {
            t: (generation_mwh[t] if prices.curt[t] <= curt_threshold else 0.0) for t in times
        }
        m.gen_curt = pyo.Param(m.T, initialize=gen_curt_param)

        # Generation left after curtailment is what could actually be exported.
        remaining_gen = {t: generation_mwh[t] - gen_curt_param[t] for t in times}

        # Cap generation at export connection capacity so discharge headroom never goes negative.
        gen_param = {t: min(remaining_gen[t], export_connection_dt) for t in times}
        m.gen_avail = pyo.Param(m.T, initialize=gen_param)

        # Surplus generation: clipped, non-curtailed portion that cannot be exported
        # (free BTM charging source). gen_surplus[t] = max(0, remaining_gen[t] − export_connection_dt)
        surplus_param = {t: max(0.0, remaining_gen[t] - export_connection_dt) for t in times}
        m.gen_surplus = pyo.Param(m.T, initialize=surplus_param)

        # Guard: profile discharge must not exceed the export headroom available after generation.
        if existing_dispatch_stored is not None:
            for t in times:
                prof_dsch_ac_t = existing_dispatch_stored[1][t] * eta_leg
                headroom = export_connection_dt - gen_param[t]
                if prof_dsch_ac_t > headroom + 1e-4:
                    sys.exit(
                        f"Existing dispatch profile discharge at t={t} ({prof_dsch_ac_t:.4f} MWh) "
                        f"exceeds co-location export headroom ({headroom:.4f} MWh). Profile is infeasible."
                    )

        def colocation_rule(mm, t):
            return mm.dsch_mwh[t] <= export_connection_dt - mm.gen_avail[t]

        m.colocation_cons = pyo.Constraint(m.T, rule=colocation_rule)  # [C3]

        # ch_grid_mwh[t]: grid-imported share of charging, bounded by import connection [B4].
        m.ch_grid_mwh = pyo.Var(m.T, bounds=(0.0, max_grid_import_mwh))

        def ch_grid_ub_rule(mm, t):
            return mm.ch_grid_mwh[t] <= mm.ch_mwh[t]

        m.ch_grid_ub = pyo.Constraint(m.T, rule=ch_grid_ub_rule)  # [C4]

        # Per-source BTM charging split: ch_from_gen_avail/_curt/_surplus[t] track exactly how much
        # charging was drawn from each of the three generation streams. Only ch_from_gen_avail
        # earns a discharge_tariff refund [B5]; gen_curt/gen_surplus discharge is not
        # discharge_tariff-exempt, so the objective has no preference between ch_from_gen_curt and
        # ch_from_gen_surplus for a given hour — any feasible split satisfying C5 below (bounded by
        # each source's own availability [B6, B7]) is equally optimal.
        #
        # Note: a grid-import lower bound (ch_grid_mwh[t] >= ch_mwh[t] - gen_avail[t] - gen_curt[t]
        # - gen_surplus[t]) is NOT needed as a separate constraint — it's implied by summing the
        # B5/B6/B7 upper bounds and substituting C5 below, so imposing it explicitly would be
        # redundant (verified: deactivating it changes neither the objective nor any solved value).
        m.ch_from_gen_avail = pyo.Var(
            m.T, bounds=lambda mm, t: (0.0, pyo.value(mm.gen_avail[t]))
        )
        m.ch_from_gen_curt = pyo.Var(
            m.T, bounds=lambda mm, t: (0.0, pyo.value(mm.gen_curt[t]))
        )
        m.ch_from_gen_surplus = pyo.Var(
            m.T, bounds=lambda mm, t: (0.0, pyo.value(mm.gen_surplus[t]))
        )

        def ch_btm_split_rule(mm, t):
            # BTM charging (ch_mwh - ch_grid_mwh) is fully attributed across the three sources.
            return (
                mm.ch_from_gen_avail[t] + mm.ch_from_gen_curt[t] + mm.ch_from_gen_surplus[t]
                == mm.ch_mwh[t] - mm.ch_grid_mwh[t]
            )

        m.ch_btm_split = pyo.Constraint(m.T, rule=ch_btm_split_rule)  # [C5]

    def standalone_term(mm, t):
        # O1 — stand-alone objective term. Applied unconditionally: in co-location mode this
        # still taxes all of ch_mwh[t] (as if every MWh were grid-taxable) and prices all of it
        # at the buy price; colocation_addendum_term below corrects both for the BTM-sourced share.
        ct = mm.ctariff[t] if consumption_tariffs is not None else charge_tariff
        return (
            mm.price_sell[t] * mm.dsch_mwh[t]
            - mm.price_buy[t] * mm.ch_mwh[t]
            - discharge_tariff * mm.dsch_mwh[t]
            - ct * mm.ch_mwh[t]
        )

    def colocation_addendum_term(mm, t):
        # Co-location addendum on top of standalone_term — added, not substituted:
        #   + ct * ch_btm[t]: refunds charge_tariff on BTM charging (standalone_term taxed all of
        #     ch_mwh[t]; only the grid-imported share ch_grid_mwh[t] should actually be taxed).
        #   + price_refund[t] * (ch_from_gen_curt + ch_from_gen_surplus): refunds the spot
        #     opportunity cost standalone_term charged on this share — it would have been wasted
        #     (curtailed or clipped) regardless, so it has zero true opportunity cost. Cancels the
        #     buy-price charge exactly only when price_refund == price_buy (single price series).
        #   + discharge_tariff * ch_from_gen_avail: refund for the only BTM source whose later
        #     discharge doesn't create new net export (see B5/C5 discussion above).
        ct = mm.ctariff[t] if consumption_tariffs is not None else charge_tariff
        ch_btm_t = mm.ch_from_gen_avail[t] + mm.ch_from_gen_curt[t] + mm.ch_from_gen_surplus[t]
        return (
            ct * ch_btm_t
            + mm.price_refund[t] * (mm.ch_from_gen_curt[t] + mm.ch_from_gen_surplus[t])
            + discharge_tariff * mm.ch_from_gen_avail[t]
        )

    def profit_rule(mm):
        if generation_mwh is not None:
            return sum(
                standalone_term(mm, t) + colocation_addendum_term(mm, t) for t in times
            )
        return sum(standalone_term(mm, t) for t in times)

    # Standard output metrics, independent of what the battery actually did -
    # a dict (not a bare float) so more can be added here later without
    # another breaking change to this return signature.
    metrics = {
        "avg_daily_spread_eur_per_mwh": average_daily_price_spread(
            prices.sell, prices.buy, start_date=prices_start_date, timezone=prices_timezone
        ),
    }

    if degradation is None:
        m.obj = pyo.Objective(rule=profit_rule, sense=pyo.maximize)

        solver = pyo.SolverFactory("appsi_highs")
        if not solver.available(False):
            sys.exit("HiGHS solver not available. Install highspy and use Pyomo appsi_highs.")
        results = solver.solve(m)
        return m, results, metrics

    if no_simultaneous_charge_discharge:
        sys.exit("Endogenous degradation keeps the model a pure LP: C6 (binary) cannot be on.")
    setup = degradation
    if setup.allow_death and existing_dispatch_stored is not None:
        sys.exit("allow_battery_death cannot be combined with an existing dispatch profile.")
    add_degradation_block(
        m, setup, nominal=cap, eta_leg=eta_leg, times=times,
        max_ch_mwh=max_ch_mwh, max_dsch_mwh=max_dsch_mwh,
    )
    # [O3] Hourly operating cash flow is exactly the fixed-capacity objective term (tariffs and
    # co-location included); it is discounted by the age year's factor, net of variable O&M.
    m.op_cash = pyo.Expression(
        m.T,
        rule=lambda mm, t: standalone_term(mm, t)
        + (colocation_addendum_term(mm, t) if generation_mwh is not None else 0.0),
    )
    n_periods = len(setup.periods.hours)
    npv = sum(
        setup.df_step[t] * (m.op_cash[t] - setup.var_om_per_mwh * m.dsch_mwh[t]) for t in times
    ) + setup.df_end * setup.terminal_value_per_mwh * m.cap[n_periods]
    m.npv = pyo.Expression(expr=npv)
    # Minimise −NPV rather than maximise NPV: see solve_lp.
    m.obj = pyo.Objective(expr=-m.npv, sense=pyo.minimize)

    if setup.allow_death:
        # [C12] Choose the death year with branch and bound, then fix it and re-solve as an LP so
        # the duals (capacity value, degradation cost) are available as in the pure-LP case.
        metrics["mip"] = solve_mip(m, setup)
        for y in m.Y:
            v = round(pyo.value(m.alive[y]))
            m.alive[y].domain = pyo.Reals
            m.alive[y].fix(v)
        start = time.time()

    # For a minimisation the dual is d(objective)/d(rhs) = −d(NPV)/d(rhs).
    if setup.method == "cumulative":
        cons = [m.soh_lookup[k] for k in m.L] + [m.cum_def[p] for p in m.P]
        duals = solve_lp(m, setup.solver_method, setup.run_crossover, cons)
        # Loosening every lookup line of period p by 1 MWh = 1 MWh more capacity at its end.
        metrics["capacity_value"] = [
            -sum(duals[m.soh_lookup[p, i]] for i in range(len(setup.lookup.lines[p]))) for p in m.P
        ]
        # Raising the rhs of cum_def[p] by 1 adds one cycle to the count from period p on without
        # any revenue: the NPV lost per cycle, divided by nominal, is the PV cost per MWh.
        metrics["degradation_cost_pv"] = [duals[m.cum_def[p]] / cap for p in m.P]
        if setup.allow_death:
            metrics["mip"]["lp_seconds"] = time.time() - start
    else:
        duals = solve_lp(
            m, setup.solver_method, setup.run_crossover, [m.cap_transition[p] for p in m.P]
        )
        # Adding 1 MWh to Cap[p+1] raises the NPV by −dual, so the present value of 1 MWh of
        # capacity at the end of period p is −dual.
        metrics["capacity_value"] = [-duals[m.cap_transition[p]] for p in m.P]
    results = SimpleNamespace(
        solver=SimpleNamespace(
            status=SolverStatus.ok, termination_condition=TerminationCondition.optimal
        )
    )
    return m, results, metrics


def write_output(
    path: Path,
    prices: PriceSeries,
    model: pyo.ConcreteModel,
    *,
    capacity_mwh: float,
    round_trip_efficiency: float,
    charge_tariff: float,
    discharge_tariff: float,
    curtailment_threshold: float,
    generation_mwh: list[float] | None = None,
    consumption_tariffs: list[float] | None = None,
    existing_dispatch_stored: tuple[list[float], list[float]] | None = None,
    extra_columns: dict[str, list] | None = None,
) -> None:
    """extra_columns: additional per-timestep columns (e.g. degradation period, capacity)."""
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
        p_buy = prices.buy[t]
        p_sell = prices.sell[t]
        p_curt = prices.curt[t]
        p_refund = prices.refund[t]
        # Effective charge tariff for this timestep: per-timestep series takes precedence.
        eff_charge_tariff = consumption_tariffs[t] if consumption_tariffs is not None else charge_tariff

        if generation_mwh is not None:
            # Taxable share of charging: grid-imported portion (above available BTM generation).
            ch_grid_taxable = pyo.value(model.ch_grid_mwh[t])
            ch_btm = ch_total - ch_grid_taxable          # behind-the-meter share (untaxed)
            ch_from_gen_avail_t = pyo.value(model.ch_from_gen_avail[t])
            ch_from_gen_curt_t = pyo.value(model.ch_from_gen_curt[t])
            ch_from_gen_surplus_t = pyo.value(model.ch_from_gen_surplus[t])
            revenue = (
                p_sell * dsch_grid
                - p_buy * ch_total
                + p_refund * (ch_from_gen_curt_t + ch_from_gen_surplus_t)
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
                p_sell * dsch_grid
                - p_buy * ch_total
                - discharge_tariff * dsch_grid
                - eff_charge_tariff * ch_total
            )

        cumulative_revenue += revenue

        # Co-location columns — computed for all rows; zero-filled when feature is disabled.
        if generation_mwh is not None:
            gen_mwh_t = generation_mwh[t]
            threshold = curtailment_threshold
            curtailed = p_curt <= threshold
            gen_gen_curtailed = 0.0 if curtailed else gen_mwh_t
            pv_net_export = max(0.0, gen_gen_curtailed - ch_btm)
            row_generation_mw               = gen_mwh_t / INTERVAL_HOURS
            row_charge_btm_mwh              = ch_btm
            row_charge_grid_mwh             = ch_grid_taxable
            row_charge_curtailed_mwh        = ch_from_gen_curt_t
            row_charge_surplus_mwh          = ch_from_gen_surplus_t
            row_generation_mwh              = gen_mwh_t
            row_generation_rev_uncurtailed  = (p_curt - discharge_tariff) * gen_mwh_t
            row_generation_curtailed_mwh    = gen_gen_curtailed
            row_generation_rev_curtailed    = 0.0 if curtailed else (p_curt - discharge_tariff) * gen_mwh_t
            row_pv_net_export_mwh           = pv_net_export
            row_total_export_mwh            = pv_net_export + dsch_grid
            row_bess_additional_export_mwh  = row_total_export_mwh - gen_gen_curtailed
        else:
            row_generation_mw               = 0.0
            row_charge_btm_mwh              = 0.0
            row_charge_grid_mwh             = ch_grid_taxable
            row_charge_curtailed_mwh        = 0.0
            row_charge_surplus_mwh          = 0.0
            row_generation_mwh              = 0.0
            row_generation_rev_uncurtailed  = 0.0
            row_generation_curtailed_mwh    = 0.0
            row_generation_rev_curtailed    = 0.0
            row_pv_net_export_mwh           = 0.0
            row_total_export_mwh            = dsch_grid
            row_bess_additional_export_mwh  = dsch_grid

        row: dict = {
            "price_buy": p_buy,
            "price_sell": p_sell,
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
            "charge_curtailed_mwh": row_charge_curtailed_mwh,
            "charge_surplus_mwh": row_charge_surplus_mwh,
            "generation_mwh": row_generation_mwh,
            "generation_revenue_uncurtailed": row_generation_rev_uncurtailed,
            "generation_curtailed_mwh": row_generation_curtailed_mwh,
            "generation_revenue_curtailed": row_generation_rev_curtailed,
            "pv_net_export_mwh": row_pv_net_export_mwh,
            "total_export_mwh": row_total_export_mwh,
            "bess_additional_export_mwh": row_bess_additional_export_mwh,
            # Profile columns: stored-side committed dispatch from existing_dispatch_profile_csv.
            # charge_mwh/discharge_mwh above show combined totals (profile + additional).
            "profile_charge_mwh": existing_dispatch_stored[0][t] if existing_dispatch_stored is not None else 0.0,
            "profile_discharge_mwh": existing_dispatch_stored[1][t] if existing_dispatch_stored is not None else 0.0,
        }
        if extra_columns:
            for name, values in extra_columns.items():
                row[name] = values[t]
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

    # Power prices: any of the three may be given (at least one). See resolve_prices.
    prices_csv = spec_optional_str(spec, "prices_csv")
    buy_prices_csv = spec_optional_str(spec, "buy_prices_csv")
    sell_prices_csv = spec_optional_str(spec, "sell_prices_csv")

    if args.write_sample_prices is not None:
        n = args.write_sample_prices
        if n < 1:
            sys.exit("N must be >= 1")
        out_p = Path(spec_str(spec, "prices_csv"))
        write_sample_prices(out_p, n, args.sample_seed)
        print(f"Wrote {n} sample prices to {out_p.resolve()}")
        return

    output_path_base = Path(spec_str(spec, "output_csv"))
    output_suffix = spec_optional_str(spec, "output_suffix") or ""

    # Calendar date of the first price row, used to correctly group rows into real
    # (DST-aware) local calendar days for the average daily price spread metric below.
    # Without it, day boundaries are assumed at fixed 24-row offsets from row 0.
    prices_start_date = spec_optional_str(spec, "prices_start_date")
    prices_timezone = spec_optional_str(spec, "prices_timezone") or "Europe/Copenhagen"
    endogenous_degradation = spec_bool(spec, "endogenous_degradation", default=False)

    power_mw = spec_float(spec, "power")
    rte = spec_float(spec, "round_trip_efficiency")
    charge_tariff = spec_float(spec, "charge_tariff", default=0.0)
    discharge_tariff = spec_float(spec, "discharge_tariff", default=0.0)
    curtailment_price_raw = spec_optional_float(spec, "curtailment_price")
    curtailment_threshold = curtailment_price_raw if curtailment_price_raw is not None else discharge_tariff
    max_cycles = spec_optional_float(spec, "max_cycles")
    max_cycles_per_day = spec_optional_float(spec, "max_cycles_per_day")
    no_simultaneous_raw = spec_optional_str(spec, "no_simultaneous_charge_discharge")
    if no_simultaneous_raw is not None and no_simultaneous_raw.lower() not in (
        "1", "true", "yes", "0", "false", "no",
    ):
        sys.exit("no_simultaneous_charge_discharge must be true or false")
    capacity_mwh = spec_float(spec, "capacity_mwh")
    initial_soc = spec_float(spec, "initial_soc", default=DEFAULT_INITIAL_SOC)
    if not (0 <= initial_soc <= 1):
        sys.exit("initial_soc must be in [0, 1]")

    # Grid connection: split import / export limits.
    # grid_import_mw  — caps how much the BESS can draw from the grid (charging).
    # grid_export_mw  — caps how much the BESS can push to the grid (discharging).
    grid_import_mw = spec_optional_float(spec, "grid_import_mw")
    grid_export_mw = spec_optional_float(spec, "grid_export_mw")

    # Optional per-timestep consumption tariff CSV (overrides scalar charge_tariff when set).
    consumption_tariff_csv = spec_optional_str(spec, "consumption_tariff_csv")
    consumption_tariffs: list[float] | None = None

    # Timestamps of every input given in dated (year,month,day,hour,value) format; all must agree.
    dated_inputs: dict[str, pd.DatetimeIndex | None] = {}

    # Co-location: enabled by uncommenting generation_profile_csv in the spec.
    # generation_max_mw is optional: when set the profile is treated as capacity factors
    # [0–1] and scaled accordingly; when omitted the CSV values are used as-is (MWh/interval).
    gen_profile_csv = spec.get("generation_profile_csv", "").strip()
    gen_max_mw: float | None = None
    generation_mwh: list[float] | None = None
    if gen_profile_csv:
        gen_max_mw_raw = spec.get("generation_max_mw", "").strip()
        if gen_max_mw_raw:
            gen_max_mw = float(gen_max_mw_raw)
            if gen_max_mw <= 0:
                sys.exit("generation_max_mw must be positive")
        gen_series = load_timeseries_csv(Path(gen_profile_csv), prices_timezone, "generation values")
        dated_inputs["generation_profile_csv"] = gen_series.index
        generation_mwh = scale_generation_profile(gen_series.values, gen_max_mw)

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
    if max_cycles_per_day is not None and max_cycles_per_day < 0:
        sys.exit("max_cycles_per_day must be non-negative")

    if consumption_tariff_csv is not None:
        tariff_series = load_timeseries_csv(Path(consumption_tariff_csv), prices_timezone, "tariff values")
        dated_inputs["consumption_tariff_csv"] = tariff_series.index
        consumption_tariffs = tariff_series.values

    existing_dispatch_profile_csv = spec_optional_str(spec, "existing_dispatch_profile_csv")
    existing_dispatch_stored: tuple[list[float], list[float]] | None = None
    if existing_dispatch_profile_csv is not None:
        existing_dispatch_stored = load_existing_profile_csv(Path(existing_dispatch_profile_csv))

    output_path = output_path_base.parent / (
        output_path_base.stem + output_suffix + output_path_base.suffix
    )

    def load_price_input(key: str, path: str | None) -> list[float] | None:
        if path is None:
            return None
        series = load_timeseries_csv(Path(path), prices_timezone, "prices")
        dated_inputs[key] = series.index
        return series.values

    prices = resolve_prices(
        load_price_input("buy_prices_csv", buy_prices_csv),
        load_price_input("sell_prices_csv", sell_prices_csv),
        load_price_input("prices_csv", prices_csv),
    )
    timeline = resolve_timeline(dated_inputs, prices_start_date, prices_timezone)
    if timeline is not None:
        # Dated inputs define the start; day buckets and degradation periods follow from it.
        prices_start_date = timeline[0]

    # Charging and discharging in the same hour is only exploitable when the sell price can exceed the
    # buy price, so the exclusivity constraint (C6) defaults to on when separate buy/sell series
    # are supplied and they differ. An explicit spec value always wins.
    # With endogenous degradation the model must stay a pure LP, so the binary C6 is replaced by its
    # LP stand-in C11 (shared power budget) under the same default rule.
    exclusivity_default = (
        (buy_prices_csv is not None or sell_prices_csv is not None)
        and prices.buy != prices.sell
    )
    if no_simultaneous_raw is not None:
        no_simultaneous = no_simultaneous_raw.lower() in ("1", "true", "yes")
    else:
        no_simultaneous = exclusivity_default
    lp_power_sharing = False
    if endogenous_degradation:
        if no_simultaneous_raw is not None and no_simultaneous:
            sys.exit(
                "no_simultaneous_charge_discharge = true needs binary variables, which endogenous "
                "degradation does not allow. Remove the key: separate buy/sell prices then get the "
                "LP power-sharing constraint instead."
            )
        lp_power_sharing = no_simultaneous
        no_simultaneous = False
    # Source file shown in the report for each side (mirrors the fallback in resolve_prices).
    buy_source = buy_prices_csv or prices_csv or sell_prices_csv
    sell_source = sell_prices_csv or prices_csv or buy_prices_csv

    if consumption_tariffs is not None and len(consumption_tariffs) != len(prices):
        sys.exit(
            f"Consumption tariff series length ({len(consumption_tariffs)}) does not match "
            f"price series length ({len(prices)}). Align the CSVs to the same period."
        )

    def solve_horizon(n_steps: int, show_warnings: bool = True):
        """Prepare degradation (if on) and solve over the first n_steps hours."""
        setup, info = None, {}
        if endogenous_degradation:
            setup, info = prepare_degradation(
                spec,
                n_steps=n_steps,
                start=prices_start_date,
                timezone=prices_timezone,
                initial_soc=initial_soc,
                lp_power_sharing=lp_power_sharing,
            )
            if show_warnings:
                for warning in info["warnings"]:
                    print(f"Warning: {warning}", flush=True)
        cut = lambda xs: None if xs is None else xs[:n_steps]
        out = build_and_solve(
            truncate_prices(prices, n_steps),
            power_mw=power_mw,
            capacity_mwh=capacity_mwh,
            round_trip_efficiency=rte,
            charge_tariff=charge_tariff,
            discharge_tariff=discharge_tariff,
            max_cycles=max_cycles,
            max_cycles_per_day=max_cycles_per_day,
            day_index=day_labels(n_steps, start_date=prices_start_date, timezone=prices_timezone)
            if max_cycles_per_day is not None
            else None,
            no_simultaneous_charge_discharge=no_simultaneous,
            generation_mwh=cut(generation_mwh),
            grid_import_mw=grid_import_mw,
            grid_export_mw=grid_export_mw,
            consumption_tariffs=cut(consumption_tariffs),
            existing_dispatch_stored=None if existing_dispatch_stored is None
            else (existing_dispatch_stored[0][:n_steps], existing_dispatch_stored[1][:n_steps]),
            curtailment_threshold=curtailment_threshold,
            initial_soc=initial_soc,
            prices_start_date=prices_start_date,
            prices_timezone=prices_timezone,
            degradation=setup,
        )
        return setup, info, out

    # Retirement choice: one LP per candidate operating life (whole age years from the start); the
    # battery operates up to the end of that year, must still be at or above end of life then, and
    # earns nothing afterwards. The candidate with the highest NPV is kept.
    retirement_raw = spec_optional_str(spec, "retirement_years")
    retirement_table: list[tuple[int, float, float]] = []   # (years, NPV, end capacity)
    n_steps = len(prices)
    if retirement_raw is not None:
        if not endogenous_degradation:
            sys.exit("retirement_years needs endogenous_degradation = true")
        if spec_bool(spec, "allow_battery_death", default=False):
            sys.exit("Use either retirement_years or allow_battery_death, not both.")
        if prices_start_date is None:
            sys.exit("retirement_years needs real timestamps (dated inputs or prices_start_date)")
        candidates = parse_year_list(retirement_raw)
        years_full = build_periods(
            make_timeline(len(prices), prices_start_date, prices_timezone), "year"
        )
        year_of_step = [years_full.age_year[p] for p in years_full.of_step]
        steps_by_year = {y: sum(1 for a in year_of_step if a <= y) for y in candidates}
        horizon_years = max(year_of_step)
        too_long = [y for y in candidates if y > horizon_years]
        if too_long:
            sys.exit(
                f"retirement_years {too_long} exceed the price horizon ({horizon_years} age years); "
                "extend the price series or drop them."
            )
        best = None
        for i, years in enumerate(candidates):
            start_time = time.time()
            print(f"Retirement candidate: {years} years ...", flush=True)
            setup_y, info_y, out_y = solve_horizon(steps_by_year[years], show_warnings=i == 0)
            npv_y = pyo.value(out_y[0].npv)
            end_cap_y = pyo.value(out_y[0].cap[len(setup_y.periods.hours)])
            print(
                f"  {years} years: NPV {npv_y:,.0f} €, LP end capacity {end_cap_y:.3f} MWh "
                f"({time.time() - start_time:.0f} s)",
                flush=True,
            )
            retirement_table.append((years, npv_y, end_cap_y))
            if best is None or npv_y > best[0]:
                best = (npv_y, years, setup_y, info_y, out_y)
        _, best_years, degradation_setup, degradation_info, (model, results, metrics) = best
        n_steps = steps_by_year[best_years]
        degradation_info["retirement_years"] = best_years
    else:
        degradation_setup, degradation_info, (model, results, metrics) = solve_horizon(n_steps)

    # Everything below reports the chosen operating life only.
    if n_steps < len(prices):
        prices = truncate_prices(prices, n_steps)
        if generation_mwh is not None:
            generation_mwh = generation_mwh[:n_steps]
        if consumption_tariffs is not None:
            consumption_tariffs = consumption_tariffs[:n_steps]
        if existing_dispatch_stored is not None:
            existing_dispatch_stored = (
                existing_dispatch_stored[0][:n_steps], existing_dispatch_stored[1][:n_steps]
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

    if degradation_setup is None:
        total_profit = pyo.value(model.obj)
    else:
        # Undiscounted operating profit, comparable with the fixed-capacity model; the NPV
        # (the objective) is reported in the degradation section.
        total_profit = sum(pyo.value(model.op_cash[t]) for t in range(len(prices)))
    Tn = len(prices)

    # Single pass over all timesteps — compute all summary stats together.
    total_export_mwh    = 0.0
    total_export_revenue = 0.0
    total_charge_mwh    = 0.0
    total_charge_cost   = 0.0   # spot cost + charge tariff on grid-imported share only
    total_dsch_profit   = 0.0   # spot revenue minus net discharge tariff (with BTM refund)
    spot_gross          = 0.0
    tariff_component    = 0.0
    curtailment_reduction_charge = 0.0  # BTM charge attributable to curtailment (pre-efficiency)
    total_surplus_charged_mwh: float | None = 0.0 if generation_mwh is not None else None

    for t in range(Tn):
        p_buy    = prices.buy[t]
        p_sell   = prices.sell[t]
        p_refund = prices.refund[t]
        dsch     = pyo.value(model.dsch_mwh[t])
        ch_total = pyo.value(model.ch_mwh[t])
        eff_ct   = consumption_tariffs[t] if consumption_tariffs is not None else charge_tariff

        if generation_mwh is not None:
            ch_grid               = pyo.value(model.ch_grid_mwh[t])
            ch_btm                = ch_total - ch_grid
            ch_from_gen_avail_t   = pyo.value(model.ch_from_gen_avail[t])
            ch_from_gen_curt_t    = pyo.value(model.ch_from_gen_curt[t])
            ch_from_gen_surplus_t = pyo.value(model.ch_from_gen_surplus[t])
            # Curtailment-reduction charge: BTM charge sourced from curtailed or surplus
            # generation — both would otherwise have been wasted this hour.
            curtailment_reduction_charge += ch_from_gen_curt_t + ch_from_gen_surplus_t
            total_surplus_charged_mwh += ch_from_gen_surplus_t
        else:
            ch_grid              = ch_total
            ch_btm               = 0.0
            ch_from_gen_avail_t  = 0.0
            ch_from_gen_curt_t   = 0.0
            ch_from_gen_surplus_t = 0.0

        total_export_mwh     += dsch
        total_export_revenue += p_sell * dsch
        total_charge_mwh     += ch_total
        # Charging cost: buy price + charge tariff on grid-imported share, plus buy-price
        # opportunity cost on gen_avail-sourced BTM share only (generation that could have been
        # exported instead). gen_curt/gen_surplus-sourced BTM charging has zero opportunity cost
        # (would have been wasted regardless), so its buy-price charge is refunded at p_refund
        # (== p_buy with a single price series, so it is free here).
        total_charge_cost    += (
            (p_buy + eff_ct) * ch_grid
            + p_buy * ch_from_gen_avail_t
            + (p_buy - p_refund) * (ch_from_gen_curt_t + ch_from_gen_surplus_t)
        )
        # Discharge profit: sell-price revenue minus discharge tariff; only gen_avail-sourced BTM
        # gets the refund (gen_curt/gen_surplus discharge creates new export, so tariff applies).
        total_dsch_profit    += p_sell * dsch - discharge_tariff * (dsch - ch_from_gen_avail_t)
        spot_gross           += (
            p_sell * dsch
            - p_buy * ch_total
            + p_refund * (ch_from_gen_curt_t + ch_from_gen_surplus_t)
        )

        if generation_mwh is not None:
            tariff_component += (
                discharge_tariff * dsch
                - discharge_tariff * ch_from_gen_avail_t
                + eff_ct * ch_grid
            )
        else:
            tariff_component += eff_ct * ch_total + discharge_tariff * dsch

    # Curtailment reduction: BTM charge attributable to curtailment, converted to expected
    # re-exported volume via round-trip efficiency.
    curtailment_reduction_mwh = rte * curtailment_reduction_charge

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

    hourly_extra: dict[str, list] | None = None
    periods_df: pd.DataFrame | None = None
    yearly_df: pd.DataFrame | None = None
    if degradation_setup is not None:
        hourly_extra, periods_df, yearly_df = degradation_results(
            model,
            degradation_setup,
            metrics,
            nominal=capacity_mwh,
            eta_leg=math.sqrt(rte),
            timeline=make_timeline(len(prices), prices_start_date, prices_timezone),
        )

    write_output(
        output_path,
        prices,
        model,
        capacity_mwh=capacity_mwh,
        round_trip_efficiency=rte,
        charge_tariff=charge_tariff,
        discharge_tariff=discharge_tariff,
        curtailment_threshold=curtailment_threshold,
        generation_mwh=generation_mwh,
        consumption_tariffs=consumption_tariffs,
        existing_dispatch_stored=existing_dispatch_stored,
        extra_columns=hourly_extra,
    )
    periods_path = output_path.with_name(output_path.stem + "_periods.csv")
    yearly_path = output_path.with_name(output_path.stem + "_yearly.csv")
    if periods_df is not None:
        periods_df.to_csv(periods_path, index=False)
        yearly_df.to_csv(yearly_path, index=False)

    # -------------------------------------------------------------------------
    # Build report lines (written to terminal and .txt file)
    # -------------------------------------------------------------------------
    report_lines: list[str] = []

    # --- Inputs summary ---
    report_lines.append("--- Inputs ---")
    report_lines.append(f"  Buy prices CSV                       : {buy_source}")
    report_lines.append(f"  Sell prices CSV                      : {sell_source}")
    if prices_csv is not None and (buy_prices_csv is not None or sell_prices_csv is not None):
        report_lines.append(f"  Reference prices CSV                 : {prices_csv}")
    report_lines.append(f"  Timesteps                            : {len(prices):>10d} h")
    for side, series in (("Buy", prices.buy), ("Sell", prices.sell)):
        report_lines.append(f"  {side + ' price mean':<36} : {sum(series)/len(series):>10.2f} €/MWh")
        report_lines.append(f"  {side + ' price min':<36} : {min(series):>10.2f} €/MWh")
        report_lines.append(f"  {side + ' price max':<36} : {max(series):>10.2f} €/MWh")
    report_lines.append(f"  {'Avg daily spread (sell max-buy min)':<36} : {metrics['avg_daily_spread_eur_per_mwh']:>10.2f} €/MWh")
    report_lines.append(f"  {'Hours with negative buy price':<36} : {sum(1 for p in prices.buy if p < 0):>10d} h")
    report_lines.append(f"  {'Hours with negative sell price':<36} : {sum(1 for p in prices.sell if p < 0):>10d} h")
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
    report_lines.append(f"  Initial SOC                          : {initial_soc*100:>10.1f} %")
    report_lines.append(f"  Grid import cap                      : {grid_import_mw if grid_import_mw is not None else power_mw:>10.2f} MW")
    report_lines.append(f"  Grid export cap                      : {grid_export_mw if grid_export_mw is not None else power_mw:>10.2f} MW")
    report_lines.append(f"  Max cycles                           : {'unlimited' if max_cycles is None else f'{max_cycles:>6.0f}':>10}")
    report_lines.append(f"  Max cycles per day                   : {'unlimited' if max_cycles_per_day is None else f'{max_cycles_per_day:>6.1f}':>10}")
    if degradation_setup is not None:
        sharing = "LP power-sharing" if lp_power_sharing else "no"
        report_lines.append(f"  No simultaneous charge/discharge     : {sharing + (' (default)' if no_simultaneous_raw is None else ''):>10}")
    else:
        report_lines.append(f"  No simultaneous charge/discharge     : {('yes' if no_simultaneous else 'no') + (' (default)' if no_simultaneous_raw is None else ''):>10}")
    report_lines.append("")
    if generation_mwh is not None:
        gen_total = sum(generation_mwh)
        gen_peak  = max(generation_mwh)
        gen_hours = sum(1 for g in generation_mwh if g > 0)
        report_lines.append(f"  Generation profile CSV               : {gen_profile_csv}")
        if gen_max_mw is not None:
            capacity_factor = gen_total / (gen_max_mw * len(generation_mwh))
            report_lines.append(f"  Generation nameplate capacity        : {gen_max_mw:>10.2f} MW")
            report_lines.append(f"  Capacity factor                      : {capacity_factor*100:>10.1f} %")
        else:
            report_lines.append(f"  Generation nameplate capacity        : {'N/A (not set)':>10}")
            report_lines.append(f"  Capacity factor                      : {'N/A (not set)':>10}")
        report_lines.append(f"  Annual generation                    : {gen_total:>10.2f} MWh")
        report_lines.append(f"  Peak output                          : {gen_peak:>10.2f} MW")
        report_lines.append(f"  Generating hours                     : {gen_hours:>10d} h")
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
    report_lines.append(f"  BESS curtailment reduction           : {curtailment_reduction_mwh:>10.2f} MWh")
    report_lines.append(f"  BESS charged from surplus generation : {total_surplus_charged_mwh if total_surplus_charged_mwh is not None else 0.0:>10.2f} MWh")

    if degradation_setup is not None:
        ds, info = degradation_setup, degradation_info
        npv = pyo.value(model.npv)
        end_cap = periods_df["capacity_end_mwh"].iloc[-1]
        total_cycles = periods_df["cycles"].sum()
        total_days = sum(ds.periods.hours) / 24.0
        simultaneous = sum(
            1
            for t in range(Tn)
            if pyo.value(model.ch_mwh[t]) > 1e-6 and pyo.value(model.dsch_mwh[t]) > 1e-6
        )
        report_lines.append("")
        report_lines.append("--- Degradation ---")
        report_lines.append(f"  Degradation curves CSV               : {info['curves_csv']}")
        report_lines.append(f"  Calendar curve CSV                   : {info['calendar_csv'] or 'extrapolated'}")
        report_lines.append(f"  Cycling rates in curves              : {', '.join(f'{r:g}' for r in sorted(info['rates']))} per day")
        report_lines.append(f"  Degradation method                   : {info['method']:>10}")
        report_lines.append(f"  Degradation period                   : {info['freq']:>10}")
        report_lines.append(f"  Periods                              : {len(ds.periods.hours):>10d}")
        report_lines.append(f"  Horizon                              : {ds.periods.n_years:>10.2f} years")
        report_lines.append(f"  Discount rate                        : {info['discount_rate']*100:>10.2f} % ({info['discount_convention']}-year)")
        report_lines.append(f"  Cycle weights charge / discharge     : {ds.a_ch:>4.2f} / {ds.a_dis:.2f}")
        report_lines.append(f"  Start capacity                       : {capacity_mwh:>10.2f} MWh")
        report_lines.append(f"  End capacity                         : {end_cap:>10.2f} MWh")
        report_lines.append(f"  End capacity (share of nominal)      : {end_cap / capacity_mwh * 100:>10.1f} %")
        if ds.method == "cumulative":
            curves_end = periods_df["capacity_end_curves_mwh"].iloc[-1]
            report_lines.append(f"  End capacity, curves w/o envelope    : {curves_end:>10.2f} MWh")
        report_lines.append(f"  Calendar loss                        : {periods_df['calendar_loss_mwh'].sum():>10.2f} MWh")
        report_lines.append(f"  Cycle loss                           : {periods_df['cycle_loss_mwh'].sum():>10.2f} MWh")
        report_lines.append(f"  End-of-life limit (from curves)      : {info['eol_fraction'] * capacity_mwh:>10.2f} MWh")
        report_lines.append(f"  Degradation-weighted cycles          : {total_cycles:>10.2f} cycles")
        report_lines.append(f"  Average cycles per day               : {total_cycles / total_days:>10.3f}")
        report_lines.append(f"  Variable O&M (undiscounted)          : {yearly_df['var_om_eur'].sum():>10.2f} €")
        report_lines.append(f"  Terminal value (discounted)          : {ds.df_end * ds.terminal_value_per_mwh * end_cap:>10.2f} €")
        report_lines.append(f"  NPV (objective)                      : {npv:>10.2f} €")
        if ds.allow_death:
            alive_years = sorted({r.age_year for r in periods_df.itertuples() if r.alive})
            last_alive = alive_years[-1] if alive_years else 0
            death_text = (
                "survives the horizon" if periods_df["alive"].all()
                else f"dies after year {last_alive}"
            )
            mip = metrics["mip"]
            report_lines.append(f"  Battery death allowed                : {'yes':>10}")
            report_lines.append(f"  Battery                              : {death_text:>10}")
            report_lines.append(f"  Operating life                       : {last_alive:>10d} years")
            report_lines.append(f"  MIP time / gap                       : {mip['seconds']:>10.0f} s | {mip['gap'] * 100:.4f} %")
            report_lines.append(f"  LP re-solve for duals                : {mip['lp_seconds']:>10.0f} s")
        if "retirement_years" in info:
            report_lines.append(f"  Chosen operating life                : {info['retirement_years']:>10d} years")
            for years, npv_y, cap_y in retirement_table:
                report_lines.append(f"  {f'  NPV, LP end capacity if {years} years':<36} : {npv_y:>10.2f} | {cap_y:.3f}")
        report_lines.append(f"  Simultaneous charge/discharge hours  : {simultaneous:>10d} h")
        if simultaneous and ds.lp_power_sharing:
            print(
                f"Warning: {simultaneous} hours charge and discharge at once. The LP power-sharing "
                "constraint limits but does not forbid this with separate buy/sell prices.",
                flush=True,
            )

    # Combined system (BESS + generation).
    if generation_mwh is not None:
        vol_uncurtailed = 0.0
        rev_uncurtailed = 0.0
        vol_curtailed = 0.0
        rev_curtailed = 0.0
        for t, gen_mwh_t in enumerate(generation_mwh):
            p = prices.curt[t]
            vol_uncurtailed += gen_mwh_t
            rev_uncurtailed += (p - discharge_tariff) * gen_mwh_t
            if p > curtailment_threshold:
                vol_curtailed += gen_mwh_t
                rev_curtailed += (p - discharge_tariff) * gen_mwh_t
        capture_price_uncurtailed = rev_uncurtailed / vol_uncurtailed if vol_uncurtailed > 1e-12 else float("nan")
        capture_price_curtailed   = rev_curtailed / vol_curtailed if vol_curtailed > 1e-12 else float("nan")
    if generation_mwh is None:
        vol_uncurtailed = rev_uncurtailed = vol_curtailed = rev_curtailed = 0.0
        capture_price_uncurtailed = capture_price_curtailed = float("nan")

    if generation_mwh is not None:
        report_lines.append("")
        report_lines.append("--- Renewable Generation Summary ---")
        report_lines.append(f"  Total uncurtailed generation (MWh)         : {vol_uncurtailed:>10.2f}")
        report_lines.append(f"  Total curtailed generation (MWh)           : {vol_curtailed:>10.2f}")
        report_lines.append(f"  Curtailment volume (MWh)                   : {vol_uncurtailed - vol_curtailed:>10.2f}")
        report_lines.append(f"  Weighted avg price, curtailed gen (€/MWh)  : {0.0 if math.isnan(capture_price_curtailed) else capture_price_curtailed:>10.2f}")
        report_lines.append(f"  Weighted avg price, uncurtailed gen (€/MWh) : {0.0 if math.isnan(capture_price_uncurtailed) else capture_price_uncurtailed:>10.2f}")

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

    # Excel sheet names are capped at 31 chars.
    sheet_dispatch = ("Dispatch" + output_suffix)[:31]
    sheet_results  = ("Results"  + output_suffix)[:31]
    with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
        pd.read_csv(output_path).to_excel(writer, sheet_name=sheet_dispatch, index=False)
        pd.DataFrame(report_rows, columns=["Label", "Value", "Value2"]).to_excel(
            writer, sheet_name=sheet_results, index=False, header=False
        )

        # Column A (Label) width, column B (Value) width and Excel's built-in "Comma [0]" style.
        results_ws = writer.sheets[sheet_results]
        results_ws.column_dimensions["A"].width = 45
        results_ws.column_dimensions["B"].width = 30
        comma_format = '_-* #,##0_-;-* #,##0_-;_-* "-"_-;_-@_-'
        for row in results_ws.iter_rows(min_col=2, max_col=2):
            for cell in row:
                cell.number_format = comma_format

        if periods_df is not None:
            periods_df.to_excel(writer, sheet_name=("Periods" + output_suffix)[:31], index=False)
            yearly_df.to_excel(writer, sheet_name=("Yearly" + output_suffix)[:31], index=False)

    print(f"\nWrote {output_path.resolve()}")
    if periods_df is not None:
        print(f"Wrote {periods_path.resolve()}")
        print(f"Wrote {yearly_path.resolve()}")
    print(f"Wrote {report_path.resolve()}")
    print(f"Wrote {excel_path.resolve()}")


if __name__ == "__main__":
    main()
