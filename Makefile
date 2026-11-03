.PHONY: lint typecheck test-fast test-integration eval eval-dry

# Route table for `make eval` (CI: ROUTES=config/llm_routes.deepseek.yml, which also needs
# SCRIBE_LLM__PROVIDERS__OPENAI_COMPAT__DATA_USE_CONFIRMED=true); prices always come from
# config/llm_prices.yml.
ROUTES ?= config/llm_routes.yml
# Extra runner flags for `make eval`, e.g. EVAL_ARGS="--suite drafts --json evals/results/eval.json".
# ONLY="user_gender/*,*/us3-*" narrows it to a subset (--only) while iterating on a failure.
EVAL_ARGS ?=
ONLY ?=

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run mypy src tests

test-fast:
	uv run pytest tests/unit tests/contract -q

test-integration:
	uv run pytest tests/integration -q

eval:
	uv run python evals/run.py --threads evals/threads --thresholds evals/thresholds.yml --routes $(ROUTES) $(EVAL_ARGS) \
		$(if $(ONLY),--only '$(ONLY)')

# Offline: fake adapter with the canned outputs in each thread (proves the runner, not a model).
eval-dry:
	uv run python evals/run.py --dry-run --threads evals/threads --thresholds evals/thresholds.yml \
		--json evals/results/dry-run.json
