---
name: fpl-verify
description: Check the code's assumptions against the live FPL API, for when something looks wrong or before trusting a new field. Use when an FPL result looks suspicious or an undocumented API behaviour needs confirming.
---

# Verify an assumption against the live API

Most defects in this project were assumptions about FPL's data that were never checked.
Check them the same way each time.

## The method

1. **Fetch the real payload** and look at it, rather than reasoning about what it should
   contain. `/me/` was assumed to carry league membership for weeks; it does not.
2. **Reconstruct a known-good number.** The scoring rules were confirmed by rebuilding all
   1236 stored `player_gameweek` rows from their components and finding zero mismatches.
   That is what made the defensive-contribution thresholds derivable at all - from the 622
   of those rows with minutes on them, since a benching carries no signal. Reconstruct the
   whole corpus; derive from the part that can tell you anything.
3. **Look at the distribution, not one example.** The `likelihood` field looked like a
   confidence score until every value was bucketed against `projected_percent` and turned
   out to be a derived band of it.
4. **Check both the present and absent cases.** A missing row means "scored zero" after a
   gameweek and "not played yet" before it. The same absence, two meanings.

## Signals that an assumption is wrong

- A suspiciously round number (two players at exactly 6.00 xP)
- A count that is implausible in the domain (190 predicted price changes in one night when
  FPL moves 10-40)
- A category that swallows most of the data ("unknown" ownership on 165 of 200 candidates)
- A result that is confidently precise about something that has not happened yet

## After confirming

Put the fact in the **docstring of the code it constrains**, not in a separate document,
so it is read by whoever next touches that code. Add a regression test that would fail
against the old assumption, and say in the test name what the wrong behaviour was.

If it changes projections, bump `MODEL_VERSION`.
