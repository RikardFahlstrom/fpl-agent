# Deterministic command sequences. Judgement lives in the skills that call these.
# The console script is installed by `uv sync`; PYTHONPATH is no longer needed.
AGENT := .venv/bin/fpl-agent

.PHONY: snapshot project rivals recommend deadline settle test

snapshot:            ## capture market + squad (refuses if auth is not configured)
	$(AGENT) snapshot --force

backfill:            ## pull per-gameweek actuals
	$(AGENT) snapshot --backfill-only

project:             ## project the 3-gameweek horizon
	$(AGENT) project --horizon 3

rivals:              ## capture rival squads for the last finished gameweek
	$(AGENT) rivals

recommend:           ## rank transfers
	$(AGENT) recommend

# The order matters: actuals feed the projection's rates, and rivals must exist before
# ownership can be judged.
deadline: snapshot backfill project rivals recommend

settle:              ## grade a finished gameweek and draft a learning: make settle GW=3
	$(AGENT) settle --gameweek $(GW) --learn

test:                ## run the suite (tests/ is not a package, so -t tests)
	PYTHONPATH=src:tests .venv/bin/python -m unittest discover -s tests -t tests -p 'test_*.py'
