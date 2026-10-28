# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

AskDB is a local web app where analysts ask plain-English questions of one read-only SQLite database.
It returns a table, a plain-English summary, and a verifiable "how this was derived" explanation.

## Source of truth

- `docs/SPEC.md` is the source of truth. If code and spec disagree, the spec wins; change the spec
  deliberately and in the same change, never silently.
- `docs/PLAN.md` tracks progress (milestones, what is done and next). Update it when work lands.
  It does not exist yet; create it from SPEC §15 when starting implementation.
- `docs/one-pager.md` is the original product brief.

## Stack

FastAPI (Python) backend, React frontend, SQLite (read-only), Claude API, `sqlglot` for SQL parsing.

## Commands

No code exists yet. These are the intended commands; correct them here once scaffolding is in place.

```
pip install -e ".[dev]"                 # backend deps (Python)
cd frontend && npm install              # frontend deps
uvicorn askdb.main:app --reload         # run backend (binds 127.0.0.1)
cd frontend && npm run dev              # run frontend
pytest                                  # all backend tests
pytest tests/test_sql_guard.py -k name  # a single test
ruff check . && ruff format --check .   # lint backend
cd frontend && npm run lint && npm test # lint and test frontend
```

## Layout

```
askdb/       backend modules: schema, prompting, sql_guard, executor, agent, summarizer, export, admin, evals
frontend/    React app (chat UI, result table, derivation panel, export)
tests/       backend tests; tests/validation runs the golden set
docs/        SPEC.md, PLAN.md, one-pager.md
data/        sample DB (cloudmetrics.db), glossary (cloudmetrics.yaml), eval cases (cloudmetrics_eval.yaml)
```

## Guardrails

- **Read-only SQL, always.** Open the DB with `mode=ro` plus `PRAGMA query_only=ON`. Every LLM-generated
  query passes through `sql_guard` (single SELECT/WITH-SELECT, authorizer allowlist) before execution.
  Never add a code path that writes to the user's database or bypasses the guard.
- **Row cap and timeout.** Every query is bounded by `ROW_CAP` (default 10,000) and `QUERY_TIMEOUT_S`
  (default 10s). Do not remove or loosen these to make a test pass.
- **Secrets.** Never commit secrets (API keys, `.env`, `config.json` with local paths). Never read `.env`.
  Read configuration only through the app's settings code; keep `.env` in `.gitignore`.
- Result rows are never logged, and DB values sent to the model are always delimited as untrusted data.

## Conventions

- Every change comes with tests: new behavior gets new tests, bug fixes get a regression test.
  Guard changes need adversarial-SQL cases; pipeline changes run against a stubbed LLM.
- `data/cloudmetrics_eval.yaml` is for validation only; never load it at startup or send it to the model.
- Release gate (SPEC §14): golden set ≥90% pass with zero wrong-but-high-confidence results.
