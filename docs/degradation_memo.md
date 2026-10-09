# Memo: endogenous battery degradation in the BESS dispatch tool

**Branch:** `feature_endogenous_degradation` · **Date:** 9 October 2026

## Summary

The dispatch tool can now optimise multi-year hourly dispatch (tested up to 25 years) while the
battery degrades as a result of how it is used. The model trades revenue today against capacity
tomorrow in one optimisation, with no explicit degradation cost in the objective. A retirement search
lets the model choose when the battery reaches end of life and stops.

On DK1 day-ahead prices (2025 repeated for 25 years) with the Hithium curves, 1 MW / 2 MWh, 0% discount
rate, **the best plan runs the battery for the full 25 years at about 1 cycle/day** (NPV 1.60 M€).
Shorter lives with harder cycling were all worse: 13 years gave 0.91 M€, 22 years 1.45 M€.

## What the tool does

- **Inputs:** hourly prices (dated or single-column) and SoH curves by age, one column per cycling rate
  (e.g. `year,1,1.5,2`). A trailing 0 marks a dead curve. All curves end at the same SoH, which is the
  end-of-life limit (0.645 for Hithium).
- **Degradation (default `cumulative`):** at the end of each year, capacity is read from the curves at
  the battery's age and its **average cycles/day since commissioning**, interpolating between the rate
  columns. This reproduces the spreadsheet logic (column F of the Hithium sheet) within 0.0003.
- **Limits:** the average cycles/day since commissioning stays within the curves' range (e.g. 1–2/day).
  Capacity must stay at or above end of life while the battery operates.
- **Lifetime:** `retirement_years` solves one LP per candidate lifetime and keeps the highest NPV.
- **Outputs:** hourly dispatch plus a per-year table (capacity, cycles, cumulative cycles, average rate,
  calendar and cycle loss, value of capacity, implied degradation cost) and an NPV report.
- **Fixed-capacity model unchanged:** with degradation off, results are byte-identical to `main`.

## Results: 25-year DK1 retirement search (Hithium curves)

| Lifetime | NPV | Avg cycles/day | End capacity |
|---|---|---|---|
| 13 y | 914,359 € | 1.36 | 72.2% |
| 16 y | 1,099,283 € | 1.31 | 69.1% |
| 19 y | 1,277,654 € | 1.26 | 66.4% |
| 22 y | 1,449,816 € | 1.19 | 64.5% (end of life) |
| **25 y** | **1,598,275 €** | **1.00** | **64.5% (end of life)** |

The five candidates ran two at a time in 26 minutes, at most 2 GB of memory each.

**Why a long life wins:** DK1 2025 spreads don't reward hard cycling. Even with only 13 years the
battery averages 1.36 cycles/day, well below the 2/day the curves allow, and retires with capacity
unused. Lasting 25 instead of 22 years costs only 3–4% of annual revenue.

**Caveats:**
- **No discounting:** the runs use a 0% discount rate (no WACC), which favours long lives.
- **Flat prices and no terminal value:** the same 2025 prices every year, and no value for capacity
  left at the end.
- **Untested lifetimes:** 23 and 24 years weren't tested, although NPV rises steadily with lifetime.
- **Price horizon:** 25 years is both the price horizon and the 1/day curve's life, so longer lives
  couldn't be tested.

## Learnings

1. **Death can't be modelled exactly in one LP.** An LP allows every blend of two allowed plans, so
   "alive at 70%" and "dead" blend into a half-dead battery. Curves dropping to 0 inside a single LP
   produced such "zombie" batteries, with NPV overstated by 26–60% in tests.
2. **A MIP with yearly alive/dead switches is exact but doesn't scale.** It solved 3 years in 11 s, but
   on 7 years the optimality gap stalled at 10.8% after 13 minutes. It found the right answer early
   but couldn't prove it.
3. **The retirement search is the practical exact alternative.** A dead battery stays dead, so there is
   one switch pattern per death year. Solving each death year as an LP gives exactly the MIP answer,
   with predictable run time and memory, and it parallelises (one process per candidate).
4. **Curve shape matters.** The lookup is exact only if, at every age, each extra step in cycling rate
   costs at least as much SoH as the previous one.
   - **Hithium curves:** meet this everywhere, so the model reproduces them exactly.
   - **DK1 test curves:** don't, because of two-decimal rounding and steep extensions of dead curves.
     The model then reads up to 0.03 SoH too high, which can loosen the end-of-life limit.
   - **Practice:** supply curves with three decimals and check this property.
5. **Cumulative vs per-period degradation.** With varying cycling, reading SoH at the average rate since
   commissioning differs from adding up each year's loss. The cumulative reading matches the business
   spreadsheet and is the default; the per-period method remains as an option.
6. **Use curves as given.** Smoothing (a power fit) moved values away from the reference table and was
   removed. Extrapolating a zero-cycling point invented data that rose with age; it is now optional and
   off for real runs.
7. **Solver and memory.** HiGHS dual simplex was the only reliable method: interior point without
   crossover gave unusable results. A 25-year hourly LP takes about 4–9 minutes and 2 GB. Long runs
   need a memory guard on an 8 GB machine: one unguarded run froze VS Code.

## Recommendations / next steps

- **Rerun the 25-year search with the project WACC** as `discount_rate`: a shorter life may win once
  later years are discounted.
- **Use real price projections** instead of 2025 repeated, and set `terminal_value_per_mwh` if leftover
  capacity has resale or second-life value.
- **Ask suppliers for three-decimal SoH curves** at more cycling rates.
- **Treat `allow_battery_death` (MIP) as experimental;** use `retirement_years` for decisions.
