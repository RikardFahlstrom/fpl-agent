"""Compare exact and previous greedy rebuilds on identical stored projections.

Run: .venv/bin/python tools/benchmark-squad.py [--db data/fpl.db]
Reads the warehouse without capturing, projecting, recording or sending anything.
"""

import argparse
from time import perf_counter

from fpl_agent.engine import chips, held, recommend, squad, storage, warehouse
from fpl_agent.engine.projection import MODEL_VERSION


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(storage.DEFAULT_DB_PATH))
    args = parser.parse_args()
    with storage.connect_readonly(args.db) as conn:
        target = warehouse.require_target(conn)
        holding = held.read(conn, target.capture)
        if holding is None:
            parser.error("the latest capture has no squad")
        moves = recommend.recommend(conn, limit=1)
        holding = holding.with_move(moves[0] if moves else None)
        first = target.gameweek
        wall = chips.window_end(list(holding.chips), first)
        if wall is None:
            parser.error("the captured chips have no window for the target gameweek")
        market = chips.market(conn, holding.snapshot_id, MODEL_VERSION, first, wall)
        owned = chips.squad_projections(
            conn, holding.snapshot_id, MODEL_VERSION,
            [p.element_id for p in holding.players], first, wall)
        print(f"Capture {holding.snapshot_id}, {target.capture.captured_at}, "
              f"GW{first}-{wall}, model {MODEL_VERSION}", flush=True)
        print("Same post-move squad, budget, club limit and scoring objective.", flush=True)
        print("chip       GW   greedy    exact     gain  greedy_s   exact_s", flush=True)
        results = {name: ([], []) for name in ("freehit", "wildcard")}
        gains = []
        for name, span in (("freehit", 1), ("wildcard", chips.WILDCARD_HORIZON)):
            for week in sorted(market):
                weeks = list(range(week, min(week + span, wall + 1)))
                candidates = chips._summed(market, weeks)
                if not candidates:
                    continue
                held_xp = {p.element_id: sum(
                    owned.get(w, {}).get(p.element_id, (0.0,))[0] for w in weeks)
                    for p in holding.players}
                baseline = chips.held_squad(holding, candidates, held_xp)
                if baseline is None:
                    parser.error("the held squad is not legal")
                start = perf_counter()
                greedy = squad.greedy_squad(candidates, holding.budget, holding.team_limit)
                greedy_time = perf_counter() - start
                start = perf_counter()
                exact = squad.best_squad(candidates, holding.budget, holding.team_limit)
                exact_time = perf_counter() - start
                if greedy is None or exact is None:
                    print(f"{name:10} {week:2} no squad: greedy={greedy is not None}, "
                          f"exact={exact is not None}", flush=True)
                    continue
                gain = exact.objective - greedy.objective
                if gain < -1e-5:
                    raise RuntimeError("exact objective is worse than the greedy reference")
                gains.append(gain)
                for values, result in zip(results[name], (greedy, exact)):
                    values.append(chips.ChipValue(
                        week, round(result.objective - baseline.objective, 2), "benchmark"))
                print(f"{name:10} {week:2} {greedy.objective:8.2f} {exact.objective:8.2f} "
                      f"{gain:8.2f} {greedy_time:9.3f} {exact_time:9.3f}", flush=True)
        for state in holding.chips:
            if state.name in results and state.evaluable(first):
                old, new = [chips.decide(state, values, first)
                            for values in results[state.name]]
                print(f"{state.title}: greedy {old.short}; exact {new.short}")
        if gains:
            print(f"{len(gains)} rebuilds: mean gain {sum(gains) / len(gains):.2f}, "
                  f"max gain {max(gains):.2f} objective points")


if __name__ == "__main__":
    main()
