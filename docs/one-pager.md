# AskDB: One-Pager

A web app where business users ask questions of a database in plain English.

## Who it's for
- Analysts and ops staff who work with data daily and ask questions repeatedly.

## What they ask
- Lookups and counts (e.g. "How many open orders does customer X have?")
- Aggregations and trends (e.g. monthly revenue by region)
- Comparisons and rankings (e.g. top 10 products vs. last quarter)
- Follow-ups and drill-downs (e.g. "now break that down by sales rep"), so context carries between questions

## What they need to see
- A table of results
- A plain-English summary of the finding, plus how the answer was derived so users can verify it
- Export / share of results
- Charts were not selected and are not a requirement

## What must never happen
- Data is never changed or deleted; the app is read-only
- No confident wrong answers; when unsure or the question may be misread, the app says so
- No runaway or slow questions that hurt the database for others

## Out of scope
- Writing or updating data
- Dashboards, saved/scheduled reports, alerts
- Predictions and what-if modelling
- Multiple databases and external data (one database only)

## Open point
- Restricted data was not selected as a "must never". Confirm whether all users may see all data, or whether some data must be hidden from some users.
