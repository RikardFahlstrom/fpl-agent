# Deterministic command sequences. Judgement lives in the skills that call these.
PY := PYTHONPATH=src .venv/bin/python

.PHONY: snapshot project rivals recommend deadline settle test

snapshot:            ## capture market + squad (refuses if auth is not configured)
	$(PY) -m fpl_agent.engine.snapshot --force

backfill:            ## pull per-gameweek actuals
	$(PY) -m fpl_agent.engine.snapshot --backfill-only

project:             ## project the 3-gameweek horizon
	$(PY) -m fpl_agent.engine.projection --horizon 3

rivals:              ## capture rival squads for the last finished gameweek
	$(PY) -m fpl_agent.engine.rivals

recommend:           ## rank transfers
	$(PY) -m fpl_agent.engine.recommend

# The order matters: actuals feed the projection's rates, and rivals must exist before
# ownership can be judged.
deadline: snapshot backfill project rivals recommend

settle:              ## grade a finished gameweek and draft a learning: make settle GW=3
	$(PY) -m fpl_agent.engine.settle --gameweek $(GW) --learn

test:                ## run the suite (tests/ is not a package, so -t tests)
	PYTHONPATH=src:tests .venv/bin/python -m unittest discover -s tests -t tests -p 'test_*.py'
