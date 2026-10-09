# Prompt: run the BESS dispatch tool with endogenous degradation

Copy the text below into a new Claude Code session opened in this repository. Fill in the `<...>` parts.

---

I want to run a multi-year BESS dispatch optimisation with endogenous degradation, using
`src/bess_dispatch_opt.py` on branch `feature_endogenous_degradation`. Read the "Endogenous degradation"
section of `README.md` first, including "Retirement choice" and its note on curve shape.

**Inputs**
- Prices: `<path>`, hourly, either dated (`year,month,day,hour,value`) or single-column with
  `prices_start_date = <YYYY-MM-DD>`, timezone `<Europe/Copenhagen>`. Separate buy/sell files:
  `<paths or "none">`.
- Degradation curves: `<path>`, wide layout `year,<rate>,<rate>,...` (SoH fraction by battery age; a
  trailing 0 marks a dead curve; all curves must end at the same SoH, which is the end-of-life limit).
- Battery: `<power>` MW, `<capacity>` MWh, round-trip efficiency `<0.9>`, initial SoC `<0.5>`.
- Economics: discount rate (WACC) `<e.g. 0.07>`, terminal value per MWh left `<0>`, variable O&M
  `<0>` €/MWh.

**Settings to use**
```
degradation_curves_csv = <path>     # giving curves switches degradation on
discount_rate = <WACC>              # default 0 = undiscounted
```
These defaults then apply:
- **No curve file:** the fixed-capacity model.
- **One curve:** capacity follows that curve for exactly its lifetime.
- **Several curves:** the cumulative interpolation, plus an automatic retirement search over every year
  from the fastest to the slowest curve's life.

`retirement_years = <list>` overrides the candidates, and `retirement_years = off` disables the search.
Don't use `allow_battery_death` (experimental, doesn't scale). `degradation_curve_fit` has been
removed.

**What I want**
1. **Check the inputs before any long run.**
   - The curves load without warnings, and all curves end at the same SoH.
   - Report each curve's life and the end-of-life value.
   - If there is a "not concave" warning, show which years and how far the model reads above the
     curves (`capacity_end_curves_mwh` vs `capacity_end_mwh`), and stop to ask me.
   - The price horizon covers the longest candidate lifetime.
2. **Retirement search** (several curves only). Start with a coarse pass of `<e.g. 4-5 years spread over
   the range>` instead of the default every-year search. For speed, use one spec and output folder per
   candidate (`retirement_years = H`). Run at most two at a time, pairing a long with
   a short candidate, each under a memory guard: kill a run if it exceeds about 4.5 GB or if system free
   memory drops below 8% (the machine has 8 GB of RAM).
3. **Report back** with a table per candidate:
   - NPV, average cycles/day, capacity at end vs end of life, net revenue in the first and last year;
   - the chosen lifetime, and why it wins;
   - the caveats: discount rate used, price assumptions, terminal value, untested years.
4. **Before refining around the best year,** ask me. Don't commit or push anything without asking.

---

**Reference results** (DK1 2025 prices × 25, Hithium curves, 1 MW / 2 MWh, 0% discount rate): the best
lifetime was 25 years at about 1.0 cycle/day, NPV 1,598,275 €. Each 25-year candidate LP took about
4–9 minutes and 2 GB.
