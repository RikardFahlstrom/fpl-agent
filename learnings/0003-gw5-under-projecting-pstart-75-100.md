---
id: 0003
gameweek: 5
model_version: 0.5.0
metric: bias_by_start_probability
slice: P(start) 75-100%
observation: under-projected by 0.78 points across 218 players
status: proposed
action: none yet
---

# P(start) 75-100%: under-projected by 0.78 in gameweek 5

Across 218 players in this slice the model predicted 2.86 points on average and they scored 3.64, a bias of -0.78 and a mean absolute error of 2.47.

For comparison the whole gameweek ran at a bias of -0.22 and an MAE of 1.17 over 659 players, so this slice is worse than the model as a whole.

## Slices this gameweek

| group | slice | n | predicted | actual | bias | MAE |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| overall | all players | 659 | 1.24 | 1.46 | -0.22 | 1.17 |
| by_position | GKP | 71 | 0.88 | 1.21 | -0.33 | 1.14 |
| by_position | DEF | 215 | 1.27 | 1.63 | -0.36 | 1.40 |
| by_position | MID | 295 | 1.31 | 1.46 | -0.15 | 1.08 |
| by_position | FWD | 78 | 1.23 | 1.24 | -0.01 | 0.92 |
| by_price | budget (<£5.0m) | 316 | 0.83 | 0.91 | -0.08 | 0.94 |
| by_price | mid (£5.0-7.5m) | 326 | 1.51 | 1.86 | -0.34 | 1.31 |
| by_price | premium (£7.5-10.0m) | 15 | 3.49 | 4.27 | -0.78 | 3.15 |
| by_price | elite (£10.0m+) | 2 | 5.32 | 4.00 | +1.32 | 1.32 |
| by_start_probability | P(start) 0-25% | 439 | 0.44 | 0.38 | +0.05 | 0.53 |
| by_start_probability | P(start) 50-75% | 2 | 2.04 | 1.50 | +0.54 | 0.54 |
| by_start_probability | P(start) 75-100% | 218 | 2.86 | 3.64 | -0.78 | 2.47 |

## Next

One gameweek is one sample. Change a weight only once three drafts on this slice agree on the sign; two is worth noticing, not acting on. Bump `model_version` when you do, so the change can be measured against this baseline rather than replacing it. The deciding is `/fpl-learn`.
