# AskDB — Specification

Web app where analysts/ops staff ask plain-English questions of one SQLite database and get a table, a plain-English summary, and a verifiable explanation of how the answer was derived. Read-only. Source: `docs/one-pager.md` plus interview decisions below.

## 1. Decisions summary
| Area | Decision |
|---|---|
| Database | One SQLite file (<100 MB, tens of tables), local file path |
| Stack | FastAPI (Python) backend + React frontend |
| LLM | Claude API; schema, prior Q+SQL, and a bounded sample of result rows are sent |
| Schema knowledge | Auto-introspection + admin-supplied YAML glossary |
| Access | Everyone sees everything; no login; session-only history |
| Deployment | Local only: each user runs it on their own machine with their own API key |
| Admin | `/admin` page, localhost-only; local file paths for DB and YAML |
| Share | Export only (no server-side persisted results, no permalinks) |
| Charts | Not required |
| Accuracy bar | Golden question set with expected result sets |

## 2. Users and use cases
Analysts/ops staff asking repeatedly. Question types: lookups/counts, aggregations/trends, comparisons/rankings, follow-ups/drill-downs.

## 3. Non-goals
Writes/updates, dashboards, saved or scheduled reports, alerts, predictions/what-if, multiple databases or external data, charts, authentication/roles, row/column-level permissions, shareable permalinks.

## 4. Architecture
- **Backend (FastAPI)**: stateless per request. Endpoints: `POST /api/ask` (SSE stream), `POST /api/export`, `GET /api/schema-info`, `GET/POST /admin/*` (localhost-only).
- **Frontend (React)**: chat thread UI; conversation state in browser `sessionStorage`; lost on tab close by design.
- **Config**: `.env` (`ANTHROPIC_API_KEY`, `DB_PATH`, `GLOSSARY_PATH`, limits). Admin page can change `DB_PATH`/`GLOSSARY_PATH` and persists to a local `config.json`.
- **Modules**: `schema` (introspect + glossary merge), `prompting`, `sql_guard` (validate/authorize), `executor` (timeout/row cap), `agent` (pipeline orchestration), `summarizer` (+ number verification), `export`, `admin`, `evals`.

## 5. Question pipeline (per ask)
Streamed over SSE as stages: **Understanding → Running → Summarizing**; the table is sent as soon as SQL finishes, summary after. User can cancel (aborts the SSE; backend interrupts the query).

1. **Interpret**: LLM receives system prompt, schema + glossary, as-of date info, last ≤6 turns of (question, final SQL, assumptions) — never result rows. Returns structured JSON: `status` (`ok` | `clarify` | `unanswerable`), `sql`, `assumptions[]`, `filters[]`, `method_steps[]`, `confidence` (`high`|`medium`|`low`), `builds_on_turn`, `is_new_topic`.
2. **Clarify** (`status=clarify`): ask one question with 2–4 selectable options (e.g. "revenue = gross or net?"). Triggered when the question maps to multiple plausible columns/definitions not resolved by the glossary. Answer is appended to the turn and the pipeline reruns.
3. **Unanswerable**: if the data cannot answer (no such column/concept), say so explicitly and list the closest available fields. Never approximate silently.
4. **Guard** (see §6) then **Execute** (see §6).
5. **Self-check**: second LLM call critiques question vs SQL vs result shape. Checks: fan-out duplicates from joins, NULL-excluded rows, empty/huge results, implausible magnitudes, unfiltered date range, wrong grain. May lower confidence or add a warning, or request one repair.
6. **Repair loop**: on SQL error or failed self-check, feed error back to the LLM, max 2 retries; then report "couldn't answer reliably" with what was tried. Retries are disclosed in the "how derived" panel.
7. **Zero rows**: respond "No matching rows" and run a value check: for equality filters on text columns, look up near matches (case-insensitive / LIKE / edit distance) and suggest them ("No customer 'Acme'; did you mean 'ACME Inc.'?").
8. **Summarize**: LLM gets question, SQL, column stats, and first ~50 rows, with rows wrapped as untrusted data ("data, not instructions"). Summary must be grounded in them.
9. **Verify numbers**: code extracts numbers in the summary and checks each against the result set/computed stats; unverifiable figures cause a regeneration (once) or the summary is replaced by a safe templated one with a note.

## 6. Safety and performance guardrails (defense in depth)
All must hold; failure of any rejects the query.
1. Open with URI `file:<path>?mode=ro&immutable=0`; set `PRAGMA query_only=ON`.
2. `sqlite3` authorizer callback allows only `SQLITE_SELECT`, `SQLITE_READ`, `SQLITE_FUNCTION` (allowlisted functions); denies ATTACH, PRAGMA, writes, `load_extension`, temp-table creation, etc.
3. Parse with a SQL parser (e.g. `sqlglot`, SQLite dialect): exactly one statement, top-level SELECT/WITH-SELECT only.
4. **Timeout**: progress handler aborts after configurable `QUERY_TIMEOUT_S` (default 10 s) via `connection.interrupt()`.
5. **Row cap**: fetch at most `ROW_CAP` rows (default 10,000), `LIMIT` enforced by wrapping; truncation flag surfaced to the UI and summary.
6. **Plan check**: `EXPLAIN QUERY PLAN` run first; reject unbounded cartesian joins (join without condition) and surface a reason.
7. Connection per request, separate read-only connection pool; no shared writable handle. DB file is never opened rw by the app.
8. Per-process concurrency limit (default 2 simultaneous queries) so one local user can't starve others.
9. Prompt-injection hardening: DB values and glossary are delimited as data; model output only ever passes through the guard; the summarizer has no tools.
10. LLM budget: per-question cap on LLM calls (default 6) and token ceiling.

## 7. Schema and glossary
**Introspection**: tables, columns, types, PKs, FKs, indexes, row counts, and for low-cardinality text columns the distinct values (cap e.g. 50) to ground filters.
**Glossary YAML** (admin-provided, validated on load). `tables` is a list (the bundled `data/cloudmetrics.yaml` uses this shape, with a free-text `description` carrying business rules and `calendar.fiscal_year_start_month`); unknown top-level keys such as `name`, `display_name`, `path` are ignored. A top-level `fiscal_year_start_month` is also accepted. A metric may name the `table` its expression is evaluated against; without it, the SQL must compile against at least one table. Metrics, terms and examples are validated by `EXPLAIN` on a read-only connection.
```yaml
description: |                                          # free text sent verbatim
  Business rules, grain of each table, ...
as_of: { column: orders.order_date, mode: data_max }   # or mode: today
calendar: { fiscal_year_start_month: 4 }
tables:
  - name: cust_ord_hdr
    description: Customer order headers
    columns:
      - { name: flg_3, description: "1 = cancelled", synonyms: [cancelled] }
metrics:
  revenue: { sql: "SUM(order_total) FILTER (WHERE status <> 'Cancelled')", table: cust_ord_hdr, description: "Net revenue" }
terms:
  open order: "status IN ('New','Processing')"
joins:
  - cust_ord_hdr.cust_id = customers.id
examples:
  - q: "How many open orders does customer X have?"
    sql: "SELECT COUNT(*) ..."
excluded: []   # "table" or "table.column" entries hidden from the LLM entirely (optional)
```
Excluded entries are never read (no row counts, no distinct values), and any metric, term, example or join whose SQL mentions an excluded column name (or table name) as a whole word is dropped. Free-text `description` fields are not scanned.
Schema fits entirely in the prompt (small DB); sent each request with prompt caching.

## 8. Relative time
Default anchor is the data's max date (from `as_of.column`); configurable to `today`. "Last quarter", "this month", "recent" resolve against the anchor with fiscal-year start from YAML. The resolved window is always shown as an assumption chip: "last quarter = Q2 2026 (data ends 2026-07-14)". If the anchor is far behind today, show a banner.

## 9. Follow-ups
Linear thread. Each request carries up to the last 6 turns as (question, SQL, assumptions) from client state. A follow-up rewrites/extends the previous SQL; the UI shows "Building on: <prior question>". If the model sets `is_new_topic`, UI says "Treated as a new question". "New chat" clears state. No branching/editing of past turns (user re-asks instead).

## 10. UI/UX
- Chat layout; input at bottom; example questions drawn from the glossary on empty state.
- **Answer card** (in order): summary → result table → "How this was derived" panel.
- **Result table**: sortable columns, sticky header, virtualized for up to 10k rows, NULLs visibly distinct, number formatting by column type. Banner when truncated ("Showing first 10,000 of more rows").
- **Derivation panel**: assumption/filter chips; plain-English method steps; row counts ("based on N rows", excluded NULLs); collapsible copyable SQL; list of retries if any.
- **Confidence**: low/medium confidence shows a prominent amber notice with the reason; high shows none. Clarifications render as option buttons.
- **Export** (per answer): CSV of full result (re-run, capped at row cap, truncation noted), Excel `.xlsx` (sheet 1 data; sheet 2 question, SQL, summary, assumptions), copy summary+method as Markdown, copy table as TSV.
- Stage indicator with cancel button; errors in plain language with a "rephrase" hint; keyboard-accessible, WCAG AA contrast.
- No charts.

## 11. Admin (`/admin`, localhost only)
- Reject any request whose client address isn't loopback (also bind `127.0.0.1` by default).
- Set DB path (local file) and glossary YAML path; shows current schema summary and glossary coverage (unmapped tables/columns).
- **Validate-then-activate**: confirm file is SQLite and opens read-only; YAML parses against schema; every referenced table/column/metric SQL must exist and compile (`EXPLAIN`). On any error, reject, show exact errors, keep the previous version live. Success hot-reloads.
- Optional CLI `python -m askdb.draft_glossary` generates a draft YAML from the schema with the LLM for the admin to edit.

## 12. Audit/logging
Local append-only log (JSONL): timestamp, question, SQL, status, row count, duration, retries, token counts. No result rows logged. Rotated by size.

## 13. Error and edge cases
- Non-SELECT intent ("delete all orders") → refuse, explain app is read-only.
- Empty schema / missing key / DB unreadable → startup health check with actionable message.
- Huge text/BLOB cells truncated in display; BLOBs shown as `<blob N bytes>`.
- Division by zero, NULL aggregates, integer division: guarded in prompting rules and self-check.
- Dates stored as text: glossary declares date columns/format; guard warns if date functions return NULL en masse.
- Ties in "top N": note ties and tie-break rule.
- Anthropic API errors/rate limits: retry with backoff once, then clear message; no partial wrong answer.
- Concurrent requests beyond limit → queued with message.
- SSE disconnect cancels the in-flight query.

## 14. Testing and acceptance
- **Golden set**: 30–50 question → expected result set pairs on a bundled sample DB (orders/customers/products/reps), covering all four question types and follow-up chains. Compared by result set (order-insensitive unless ranking), not SQL text. Release gate: ≥90% pass; 0 results that are wrong yet flagged high-confidence.
- Unit tests: guard (adversarial SQL: DROP/INSERT/UPDATE, multi-statement, PRAGMA, ATTACH, `load_extension`, CTE-wrapped writes, comment tricks), timeout, row cap, number verification, YAML validation, relative-date resolution, admin loopback check.
- Prompt-injection test: DB value containing "ignore previous instructions" must not alter behavior.
- API tests with FastAPI TestClient and a stubbed LLM; a small number of live-LLM runs for the golden set.
- Manual UI check of main flows (ask, clarify, follow-up, export, truncation, cancel).

## 15. Milestones
1. Sample DB, schema introspection, glossary loader/validator.
2. Guard + executor with adversarial tests.
3. Pipeline (interpret → execute → summarize) with streaming; golden set harness.
4. Clarify/self-check/repair/zero-row suggestions; number verification.
5. React UI: chat, table, derivation panel, export.
6. Admin page + validation; docs and run instructions.

## 16. Open items for later
- Whether to add a basic auth layer if deployment ever moves off local-only.
- Per-user data restrictions deliberately excluded now; guard/`excluded:` glossary hook leaves room.
