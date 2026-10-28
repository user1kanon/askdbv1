# AskDB implementation plan: 8 vertical slices

`docs/SPEC.md` is the source of truth. This file tracks progress: update the status table when a slice lands.

## Status

| Slice | Name | Status | Notes |
|---|---|---|---|
| 1 | Foundation: settings, schema introspection, glossary | done ✅ | 54 tests; log in `docs/slice-1-log.txt` |
| 2 | Guard and read-only executor | todo | |
| 3 | Walking skeleton: ask → table, streamed | todo | |
| 4 | Summary, number verification, audit log | todo | |
| 5 | Derivation panel, relative time, follow-ups, confidence | todo | |
| 6 | Clarify, unanswerable, refusal, self-check, repair, zero-row, golden harness | todo | |
| 7 | Export, table polish, cancel | todo | |
| 8 | Admin page, release gate, docs | todo | |

SPEC §15 milestone mapping: M1 → slice 1, M2 → slice 2, M3 → slices 3 and 4, M4 → slice 6 (number verification is in slice 4), M5 → slices 5 and 7, M6 → slice 8.

## Context
`docs/SPEC.md` is fully written, but the repo has only the scaffold: docs, `data/cloudmetrics.db`, `data/cloudmetrics.yaml`, `data/cloudmetrics_eval.yaml`, `.env.example`. There is no code. SPEC §15 lists 6 milestones, which are too coarse for one-session work. This plan splits them into 8 thin slices. Each slice leaves a runnable, tested system. The riskiest safety piece (guard and executor) comes early. The UI starts as a walking skeleton in slice 3 instead of arriving last. Each slice also updates this file, per CLAUDE.md.

Conventions for every slice: tests land with the code; the guard gets adversarial cases; pipeline tests use a stubbed LLM; `data/cloudmetrics_eval.yaml` is never loaded at startup or sent to the model; no result rows are logged.

---

## Slice 1: Foundation: settings, schema introspection, glossary
- **Goal:** The backend starts, reads config, introspects the sample DB, loads and validates the glossary, and serves `GET /api/schema-info`. A startup health check gives actionable errors for a missing key, unreadable DB, or empty schema.
- **Files:** `pyproject.toml`, `askdb/__init__.py`, `askdb/settings.py` (reads `.env` only through this module), `askdb/schema.py` (tables, columns, PKs, FKs, indexes, row counts, low-cardinality distincts capped at 50), `askdb/glossary.py` (YAML load and validate, `excluded:` applied), `askdb/main.py` (FastAPI app, health check, schema-info), `tests/test_schema.py`, `tests/test_glossary.py`, `docs/PLAN.md`, `.gitignore` check.
- **Verify:** `pytest` covers introspection against `data/cloudmetrics.db`. It also covers glossary validation (bad table, bad column, bad metric SQL) and health-check failures. `ruff check .` is clean. `uvicorn askdb.main:app` binds 127.0.0.1 and `curl /api/schema-info` returns the schema merged with the glossary.
- **Depends on:** none.

## Slice 2: Guard and read-only executor
- **Goal:** Any SQL string is either rejected with a reason or run safely, bounded by the row cap, timeout, and concurrency limit.
- **Files:** `askdb/sql_guard.py` (sqlglot single-statement SELECT/WITH-SELECT check, function allowlist, `EXPLAIN QUERY PLAN` cartesian-join rejection), `askdb/executor.py` (`file:...?mode=ro`, `PRAGMA query_only=ON`, authorizer callback, progress-handler timeout via `interrupt()`, `LIMIT` wrapping with truncation flag, BLOB and long-text display handling, semaphore of 2, connection per request), `tests/test_sql_guard.py`, `tests/test_executor.py`.
- **Verify:** Adversarial cases all rejected: DROP/INSERT/UPDATE, multi-statement, PRAGMA, ATTACH, `load_extension`, CTE-wrapped writes, comment tricks. A slow recursive CTE hits the timeout. A 20k-row query comes back truncated at the cap with the flag set. The cartesian join is rejected. A write attempted past the guard still fails at the authorizer and `query_only` layers (defense-in-depth tests). `pytest tests/test_sql_guard.py tests/test_executor.py`.
- **Depends on:** Slice 1 (settings, DB path).
- **Note from slice 1:** `askdb/schema.py::connect_readonly` already opens `file:...?mode=ro&immutable=0` plus `PRAGMA query_only=ON`; build the executor on it instead of a second opener, and add the authorizer, progress handler and `LIMIT` wrapping on top. Settings already has `row_cap`, `query_timeout_s`, `max_concurrent_queries`. Introspection and glossary validation use PRAGMA/EXPLAIN on trusted code paths only; they must never go through `sql_guard`.

## Slice 3: Walking skeleton: ask → table, streamed
- **Goal:** Type a question in the browser and see a result table. The pipeline is interpret → guard → execute, streamed over SSE (Understanding → Running stages, table event). It uses a real Claude client with structured JSON output, plus a stub for tests.
- **Files:** `askdb/llm.py` (client wrapper, backoff retry once, per-question call cap, `LLMClient` protocol, stub), `askdb/prompting.py` (system prompt, schema and glossary as cached delimited data, rules for division by zero, NULLs, and ties), `askdb/agent.py` (orchestrator, `status=ok` path only), `POST /api/ask` in `main.py`, `frontend/` (Vite React scaffold, input box, stage indicator, plain table, SSE client), `tests/test_agent.py`, `tests/test_api_ask.py` (TestClient with stubbed LLM), `frontend` lint and test setup, CLAUDE.md Commands section corrected.
- **Verify:** The API test streams stages then a table event for a stubbed `ok` response. A stubbed non-SELECT SQL yields a guard error event. A prompt-injection DB value in schema distincts does not change behavior (delimiting test). Manual: `uvicorn` plus `npm run dev`, ask one live question, see rows. `pytest` and `npm run lint && npm test`.
- **Depends on:** Slices 1 and 2.
- **Note from slice 1:** The prompt's schema and glossary come from `app.state.health.schema_info` (`SchemaInfo` from `build_schema_info`): per-table/column descriptions, `distinct_values`, plus the glossary's free-text `description` (which carries the metric definitions, so send it verbatim). `excluded` items are already stripped from it; build prompts from that object, never from a fresh introspection. `/api/ask` should return a clear error when `health.schema_info is None`, and `ANTHROPIC_API_KEY` is only checked by `/api/health` so far.

## Slice 4: Summary, number verification, audit log
- **Goal:** After the table, a grounded plain-English summary streams in. Every number in it is verified against the result set. Unverifiable numbers trigger one regeneration, then a templated fallback with a note. Each ask appends one JSONL audit record.
- **Files:** `askdb/summarizer.py` (rows wrapped as untrusted data, column stats, first ~50 rows, no tools), `askdb/verify.py` (number extraction and matching: rounding, percentages, thousands separators), `askdb/audit.py` (JSONL with size rotation; question, SQL, status, row count, duration, retries, tokens; never rows), `agent.py` and `main.py` summary event, frontend summary above table, `tests/test_summarizer.py`, `tests/test_verify.py`, `tests/test_audit.py`.
- **Verify:** Unit tests: a hallucinated figure is rejected and regenerated once; a second failure gives the templated summary; formatted numbers (`1,234`, `12.5%`) verify correctly. The audit test confirms no row values appear in the log. API test sees the summary event after the table. Manual live ask shows the summary.
- **Depends on:** Slice 3.

## Slice 5: Derivation panel, relative time, follow-ups, confidence
- **Goal:** Each answer card shows summary → table → "How this was derived" (assumption and filter chips, method steps, row counts, collapsible copyable SQL). Relative dates resolve against the as-of anchor. Follow-ups carry the last ≤6 turns. Low and medium confidence show the amber notice.
- **Files:** `askdb/timeutil.py` (as-of anchor from `data_max` or `today`, fiscal-year start, quarter and month windows, far-behind-today banner flag), `prompting.py` (turn history of question/SQL/assumptions only, as-of info), `agent.py` (`builds_on_turn`, `is_new_topic`, confidence passthrough), frontend `AnswerCard`, `DerivationPanel`, `ConfidenceNotice`, `sessionStorage` thread state, "New chat", "Building on:" and "Treated as a new question" labels, example questions from glossary on the empty state, `tests/test_timeutil.py`, `tests/test_followups.py`.
- **Verify:** Unit tests for "last quarter", "this month", and "recent" with fiscal start month 4 and both anchor modes. A follow-up request carries history without rows (assert on the stubbed prompt). Frontend component tests for chips, the amber notice (medium and low), and persistence across reload but not across a new tab. Manual: ask, then "break that down by region".
- **Depends on:** Slice 4.
- **Note from slice 1:** `data/cloudmetrics.yaml` has `calendar.fiscal_year_start_month` but no `as_of` entry, so `data_max` anchoring needs one added (e.g. `as_of: { column: invoices.invoice_date, mode: data_max }`, or pick the right column); the glossary loader and `SchemaInfo.as_of` already support it. The glossary itself warns that data ends November 2025, so "last N months" relative to today would return nothing.

## Slice 6: Clarify, unanswerable, refusal, self-check, repair, zero-row, golden harness
- **Goal:** The pipeline handles every non-happy path in SPEC §5 and §13. The golden-set harness can measure accuracy.
- **Files:** `agent.py` (`clarify` → 2–4 option buttons and a rerun with the answer appended; `unanswerable` with closest fields; read-only refusal for write intent; self-check LLM call that may lower confidence, warn, or request a repair; repair loop max 2 retries with a retries list; zero-row near-match suggestions via LIKE, case-insensitive, and edit distance; tie notes), `askdb/selfcheck.py`, `askdb/nearmatch.py`, `askdb/evals.py` (loads `data/cloudmetrics_eval.yaml` only when run explicitly, order-insensitive set comparison unless ranking, reports pass rate and wrong-but-high-confidence count), `tests/validation/` runner, frontend clarify buttons, "No matching rows" with suggestions, retries listed in the derivation panel, `tests/test_agent_paths.py`, `tests/test_evals.py`.
- **Verify:** Stubbed-LLM tests for each path: clarify rerun, unanswerable, "delete all orders" refusal, a self-check-triggered repair, an SQL-error repair that stops after 2 retries with "couldn't answer reliably", and "Acme" → "ACME Inc." suggestion. The harness compares result sets correctly on fabricated cases. `pytest tests/validation` runs a first live baseline (record the number in this file, no gate yet).
- **Depends on:** Slices 4 and 5.

## Slice 7: Export, table polish, cancel
- **Goal:** Results are usable at 10k rows. Users can export in all four ways and cancel a running question.
- **Files:** `askdb/export.py` (CSV re-run capped with truncation noted; xlsx via openpyxl with sheet 1 data and sheet 2 question, SQL, summary, assumptions), `POST /api/export`, frontend `ResultTable` (sortable columns, sticky header, virtualized, visible NULLs, number formatting by type, truncation banner, BLOB and long-text display), copy buttons (summary and method as Markdown, table as TSV), cancel button wired to SSE abort and backend query interrupt on disconnect, queued-with-message for concurrency overflow, plain-language errors with a "rephrase" hint, `tests/test_export.py`, `tests/test_cancel.py`, frontend table and export tests.
- **Verify:** The xlsx test reopens the file and checks both sheets. The CSV truncation note appears for an over-cap query. A cancel test shows the backend query is interrupted after the client disconnects. Component test: 10k rows render without mounting all rows. Manual UI check: sort, copy, export, cancel, truncation banner. WCAG AA contrast is spot-checked.
- **Depends on:** Slices 3 and 5 (UI and card). It is independent of slice 6 except for shared answer types.

## Slice 8: Admin page, release gate, docs
- **Goal:** Localhost-only `/admin` can change DB and glossary paths with validate-then-activate and hot reload. The golden set passes the release gate. A new user can follow the run instructions.
- **Files:** `askdb/admin.py` (loopback-only middleware, validate SQLite opens read-only, YAML parses, every referenced table, column, and metric SQL compiles via `EXPLAIN`, atomic swap on success, previous version kept on failure, persists `config.json`, coverage report of unmapped tables and columns), `/admin/*` routes, frontend `/admin` page, optional `askdb/draft_glossary.py` CLI, README and run instructions, `docs/PLAN.md` final status, `tests/test_admin.py`, `tests/test_loopback.py`.
- **Verify:** Non-loopback client address gets 403. A bad YAML, a missing-table reference, and a non-SQLite file are each rejected with exact errors while the old config stays live. A good change hot-reloads with no restart and `config.json` is gitignored. Release gate (SPEC §14): `pytest tests/validation` with live LLM, ≥90% golden pass and zero wrong-but-high-confidence results. If it fails, iterate prompts and glossary here. Manual end-to-end walkthrough of ask, clarify, follow-up, export, truncation, and cancel.
- **Depends on:** Slices 6 and 7.
- **Note from slice 1:** Reuse `askdb.glossary.load_glossary` + `validate_glossary(glossary, introspect(db, distinct_cap=0), db_path)` for validate-then-activate (it already collects all errors and compiles metric/example SQL with `EXPLAIN`), and `askdb.health.run_health_check` for the activation check. The health report is built once in the app lifespan, so hot reload must rebuild `app.state.health`. The coverage report (unmapped tables/columns) is not built yet.

---

## Dependency chain
1 → 2 → 3 → 4 → 5 → 6 → 8, with 7 branching off after 5 (needs 3 and 5) and merging into 8.

## Open points for your review
- **Slice 6 is the biggest.** If it overruns one session, split it into "clarify/unanswerable/refusal" and "self-check/repair/zero-row + harness".
- **Live LLM runs:** Slices 3 and 6 and the gate in slice 8 need `ANTHROPIC_API_KEY` set locally. Everything else runs offline on the stub.
