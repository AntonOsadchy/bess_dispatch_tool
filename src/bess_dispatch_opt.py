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
import random
import sys
from dataclasses import dataclass
from pathlib import Path

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


def load_prices_csv(path: Path) -> list[float]:
    df = pd.read_csv(path, header=None)
    if df.shape[1] < 1:
        sys.exit(f"No columns in {path}")
    s = pd.to_numeric(df.iloc[:, 0], errors="coerce").dropna()
    if s.empty:
        sys.exit(f"No numeric prices in {path}")
    return s.astype(float).tolist()


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


def day_labels(
    n: int,
    *,
    start_date: str | None = None,
    timezone: str = "Europe/Copenhagen",
    interval_hours: float = INTERVAL_HOURS,
) -> list[int]:
    """Consecutive calendar-day index (0, 1, 2, ...) for each of n rows.

    With start_date (a date or a 'YYYY-MM-DD HH:MM' datetime, local time) rows are mapped to real
    local calendar dates via a DST-aware index, so a spring-forward day has 23 rows and a fall-back
    day 25. Without it, rows are chunked into fixed 24/interval_hours-row blocks from row 0.
    """
    if start_date is not None:
        idx = pd.date_range(
            start=pd.Timestamp(start_date, tz=timezone),
            periods=n,
            freq=pd.Timedelta(hours=interval_hours),
        )
        return pd.factorize(idx.date)[0].tolist()
    steps_per_day = round(24.0 / interval_hours)
    return [i // steps_per_day for i in range(n)]


def average_daily_price_spread(
    sell_prices: list[float],
    buy_prices: list[float],
    *,
    start_date: str | None = None,
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


def load_profile_csv(path: Path, max_mw: float | None = None) -> list[float]:
    """Load a single-column, headerless generation profile CSV.

    When max_mw is provided the file is treated as capacity factors [0–1]:
      values are clipped to [0, 1] and scaled → MWh per interval = cf × max_mw × INTERVAL_HOURS.
    When max_mw is None the scaling step is skipped and each row is used directly as
      MWh per interval (the CSV must already contain absolute energy values).
    """
    df = pd.read_csv(path, header=None)
    s = pd.to_numeric(df.iloc[:, 0], errors="coerce").dropna()
    if s.empty:
        sys.exit(f"No numeric values found in profile CSV: {path}")
    if max_mw is not None:
        cf = s.clip(0.0, 1.0).astype(float)
        return (cf * max_mw * INTERVAL_HOURS).tolist()
    return s.astype(float).tolist()


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
    prices_start_date: str | None = None,
    prices_timezone: str = "Europe/Copenhagen",
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

    m.obj = pyo.Objective(rule=profit_rule, sense=pyo.maximize)

    solver = pyo.SolverFactory("appsi_highs")
    if not solver.available(False):
        sys.exit("HiGHS solver not available. Install highspy and use Pyomo appsi_highs.")
    results = solver.solve(m)

    # Standard output metrics, independent of what the battery actually did -
    # a dict (not a bare float) so more can be added here later without
    # another breaking change to this return signature.
    metrics = {
        "avg_daily_spread_eur_per_mwh": average_daily_price_spread(
            prices.sell, prices.buy, start_date=prices_start_date, timezone=prices_timezone
        ),
    }
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
        generation_mwh = load_profile_csv(Path(gen_profile_csv), gen_max_mw)

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
        consumption_tariffs = load_tariff_csv(Path(consumption_tariff_csv))

    existing_dispatch_profile_csv = spec_optional_str(spec, "existing_dispatch_profile_csv")
    existing_dispatch_stored: tuple[list[float], list[float]] | None = None
    if existing_dispatch_profile_csv is not None:
        existing_dispatch_stored = load_existing_profile_csv(Path(existing_dispatch_profile_csv))

    output_path = output_path_base.parent / (
        output_path_base.stem + output_suffix + output_path_base.suffix
    )

    prices = resolve_prices(
        load_prices_csv(Path(buy_prices_csv)) if buy_prices_csv else None,
        load_prices_csv(Path(sell_prices_csv)) if sell_prices_csv else None,
        load_prices_csv(Path(prices_csv)) if prices_csv else None,
    )
    # Charging and discharging in the same hour is only exploitable when the sell price can exceed the
    # buy price, so the exclusivity constraint (C6) defaults to on when separate buy/sell series
    # are supplied and they differ. An explicit spec value always wins.
    if no_simultaneous_raw is not None:
        no_simultaneous = no_simultaneous_raw.lower() in ("1", "true", "yes")
    else:
        no_simultaneous = (
            (buy_prices_csv is not None or sell_prices_csv is not None)
            and prices.buy != prices.sell
        )
    # Source file shown in the report for each side (mirrors the fallback in resolve_prices).
    buy_source = buy_prices_csv or prices_csv or sell_prices_csv
    sell_source = sell_prices_csv or prices_csv or buy_prices_csv

    if consumption_tariffs is not None and len(consumption_tariffs) != len(prices):
        sys.exit(
            f"Consumption tariff series length ({len(consumption_tariffs)}) does not match "
            f"price series length ({len(prices)}). Align the CSVs to the same period."
        )

    model, results, metrics = build_and_solve(
        prices,
        power_mw=power_mw,
        capacity_mwh=capacity_mwh,
        round_trip_efficiency=rte,
        charge_tariff=charge_tariff,
        discharge_tariff=discharge_tariff,
        max_cycles=max_cycles,
        max_cycles_per_day=max_cycles_per_day,
        day_index=day_labels(len(prices), start_date=prices_start_date, timezone=prices_timezone)
        if max_cycles_per_day is not None
        else None,
        no_simultaneous_charge_discharge=no_simultaneous,
        generation_mwh=generation_mwh,
        grid_import_mw=grid_import_mw,
        grid_export_mw=grid_export_mw,
        consumption_tariffs=consumption_tariffs,
        existing_dispatch_stored=existing_dispatch_stored,
        curtailment_threshold=curtailment_threshold,
        initial_soc=initial_soc,
        prices_start_date=prices_start_date,
        prices_timezone=prices_timezone,
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
    )

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

    print(f"\nWrote {output_path.resolve()}")
    print(f"Wrote {report_path.resolve()}")
    print(f"Wrote {excel_path.resolve()}")


if __name__ == "__main__":
    main()
