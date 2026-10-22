# Deterministic command sequences. Judgement lives in the skills that call these.
# The console script is installed by `uv sync`; PYTHONPATH is no longer needed.
AGENT := .venv/bin/fpl-agent

.PHONY: now snapshot backfill project rivals recommend hedge record settle status brief lint test

# The one to reach for when you do not want to think about which one to reach for.
# Every other target here is a step; this asks the warehouse what is due and runs the
# steps that answer. It takes the run lock, so two at once cannot refresh the login
# together. `make now DRY=--dry-run` says what it would do without doing it.
now:                 ## capture, settle what has finished, update every number, rank
	$(AGENT) schedule $(DRY)

# --force is deliberate. Bare `snapshot` skips when one already exists for today, which
# is a guard for a hand-run repeat; every scheduled caller wants the opposite. Prices
# resolve nightly and predicted lineups firm up through matchday, so the second capture
# of a day is a different market, not a duplicate. The schedule forces for the same
# reason, which is why this target and the plan's first step agree.
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

hedge:               ## price selling 1-3 players to match the field: make hedge ARGS='--player Haaland'
	$(AGENT) hedge $(ARGS)

# Separate from `recommend` rather than a flag on it, because recording is a claim about
# what you actually did. `make now` must never record on your behalf.
record:              ## log what you did: make record ARGS='--move 1 --captain Haaland --chip none'
	$(AGENT) record $(ARGS)

status:              ## check the warehouse agrees with itself (read-only; exits 7 if not)
	$(AGENT) status

settle:              ## grade a finished gameweek and draft a learning: make settle GW=3
	$(AGENT) settle --gameweek $(GW) --learn

# Read-only, and writes exactly one file: logs/gwNN.md, which is tracked. `make now`
# writes it as its last step; this is for rewriting it without capturing again.
brief:               ## write the gameweek brief: make brief
	$(AGENT) brief

# Pyflakes rules only: unused imports, unused locals, undefined names. Deliberately not
# the style rules - `Optional[x]` -> `x | None` and friends would rewrite hundreds of lines
# of working code to satisfy a preference, and none of the bugs this project has actually
# had were visible to a linter. This catches the one thing that is: a name that is not
# there. `make lint FIX=--fix` applies the safe fixes. uvx keeps ruff out of uv.lock, so
# nothing new reaches the server on its next `uv sync`.
lint:                ## report unused imports and undefined names: make lint FIX=--fix
	uvx ruff check --select F $(FIX) src tests

test:                ## run the suite (tests/ is not a package, so -t tests)
	PYTHONPATH=src:tests .venv/bin/python -m unittest discover -s tests -t tests -p 'test_*.py'
