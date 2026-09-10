---
id: 0001
gameweek: 3
model_version: 0.5.0
metric: bias_by_start_probability
slice: P(start) 75-100%
observation: under-projected by 0.59 points across 218 players
status: proposed
action: none yet
---

# P(start) 75-100%: under-projected by 0.59 in gameweek 3

Across 218 players in this slice the model predicted 2.75 points on average and they scored 3.35, a bias of -0.59 and a mean absolute error of 2.23.

For comparison the whole gameweek ran at a bias of -0.15 and an MAE of 1.12 over 652 players, so this slice is worse than the model as a whole.

## Slices this gameweek

| group | slice | n | predicted | actual | bias | MAE |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| overall | all players | 652 | 1.24 | 1.40 | -0.15 | 1.12 |
| by_position | GKP | 71 | 0.84 | 1.01 | -0.17 | 0.85 |
| by_position | DEF | 214 | 1.21 | 1.74 | -0.52 | 1.35 |
| by_position | MID | 289 | 1.31 | 1.33 | -0.02 | 1.05 |
| by_position | FWD | 78 | 1.46 | 1.09 | +0.37 | 1.01 |
| by_price | budget (<£5.0m) | 261 | 0.79 | 0.97 | -0.17 | 0.93 |
| by_price | mid (£5.0-7.5m) | 371 | 1.44 | 1.57 | -0.13 | 1.19 |
| by_price | premium (£7.5-10.0m) | 18 | 3.28 | 3.72 | -0.44 | 2.19 |
| by_price | elite (£10.0m+) | 2 | 5.00 | 5.50 | -0.50 | 3.60 |
| by_start_probability | P(start) 0-25% | 432 | 0.48 | 0.40 | +0.08 | 0.56 |
| by_start_probability | P(start) 50-75% | 2 | 1.71 | 4.00 | -2.29 | 2.34 |
| by_start_probability | P(start) 75-100% | 218 | 2.75 | 3.35 | -0.59 | 2.23 |

## Next

One gameweek is one sample. Confirm the direction holds before changing a weight, and bump `model_version` when you do, so the change can be measured against this baseline rather than replacing it.
