---
id: 0002
gameweek: 4
model_version: 0.5.0
metric: bias_by_start_probability
slice: P(start) 75-100%
observation: under-projected by 0.54 points across 218 players
status: proposed
action: none yet
---

# P(start) 75-100%: under-projected by 0.54 in gameweek 4

Across 218 players in this slice the model predicted 2.90 points on average and they scored 3.43, a bias of -0.54 and a mean absolute error of 2.48.

For comparison the whole gameweek ran at a bias of -0.12 and an MAE of 1.19 over 655 players, so this slice is worse than the model as a whole.

## Slices this gameweek

| group | slice | n | predicted | actual | bias | MAE |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| overall | all players | 655 | 1.28 | 1.41 | -0.12 | 1.19 |
| by_position | GKP | 71 | 0.88 | 1.18 | -0.30 | 1.07 |
| by_position | DEF | 214 | 1.31 | 1.57 | -0.26 | 1.45 |
| by_position | MID | 292 | 1.35 | 1.33 | +0.02 | 1.02 |
| by_position | FWD | 78 | 1.32 | 1.45 | -0.12 | 1.25 |
| by_price | budget (<£5.0m) | 292 | 0.88 | 0.86 | +0.02 | 1.02 |
| by_price | mid (£5.0-7.5m) | 346 | 1.50 | 1.72 | -0.22 | 1.27 |
| by_price | premium (£7.5-10.0m) | 15 | 3.75 | 4.33 | -0.59 | 2.55 |
| by_price | elite (£10.0m+) | 2 | 4.40 | 5.50 | -1.10 | 3.31 |
| by_start_probability | P(start) 0-25% | 435 | 0.47 | 0.39 | +0.08 | 0.55 |
| by_start_probability | P(start) 50-75% | 2 | 1.58 | 2.00 | -0.42 | 1.75 |
| by_start_probability | P(start) 75-100% | 218 | 2.90 | 3.43 | -0.54 | 2.48 |

## Next

One gameweek is one sample. Confirm the direction holds before changing a weight, and bump `model_version` when you do, so the change can be measured against this baseline rather than replacing it.
