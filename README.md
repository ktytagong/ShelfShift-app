# ShelfShift

**Public sales dataset · Simulated operational scenario.** ShelfShift is a working prototype that helps branch
managers and planners decide what to do with short-life stock one branch will not sell before it expires: move it to
another branch, mark it down or keep it, with every number computed by a deterministic engine and the stock facts
checked before anything moves. The scenario is hypothetical: the sales history is public (see Data below), and stock,
lots, expiry and deliveries are simulated. The app and these docs are in English.

**Reviewers: start with the [reviewer guide](docs/reviewer-guide.md).** It covers signing in, the test accounts, a
walkthrough and where Gemini works. The sample files reviewers can submit are in [docs/guide](docs/guide/README.md).

## Run it locally (engine-only mode, GCP off)

You need Docker running. No Google account and no cloud project are needed: the local app runs with GCP off.

1. Choose a passcode for the gate and put it in a file named `.env` beside `docker-compose.yml`:

   ```
   DEMO_PASSCODE=<choose one>
   ```

   `.env` is git-ignored; never commit it, and never write the passcode in a tracked file.

2. Start everything:

   ```
   docker compose up
   ```

   This starts the database, then a one-off init (it migrates the schema, installs the reference data and builds the
   demo template from the generated (synthetic) reference set in `data/reference`), then the app at
   127.0.0.1:8080. Open it and enter your passcode. A second `docker compose up` is safe: the init reruns and
   builds a fresh template.

`SS_APP_PORT` and `SS_DB_PORT` move the app and database ports on 127.0.0.1 when 8080 or 5432 is taken, for example
`SS_APP_PORT=8090 SS_DB_PORT=15433 docker compose up`.

## Tests

`make setup` once: it builds `.venv` from `requirements.lock` with a final, non-conda CPython 3.12 (it uses `uv`;
pass `PYTHON312=<path>` when your Python 3.12 lives elsewhere).

- `make test`: the engine, data and deploy checks. It needs no database and no cloud account, and passes with Docker
  stopped.
- `make db-up`, then `make test-api`: the API suite against a local Postgres 17 (database `shelfshift_test`).
- `make test-web`: the browser suite (its own `shelfshift_web_test` database, a production build of `web/`, an API
  child and a preview server driven by headless Chromium).
- `make compose-check`: the full local acceptance. From an empty volume, `docker compose up`, the smoke check and the
  judge's journey in a browser, then a second `up` and the smoke again. It uses its own Compose project and ports and
  removes them when it ends.

`make db-up` is for a fresh machine with port 5432 free. When a database container is already running on 5432,
reuse it (the API and browser suites only need it to be up), or give the new one its own Compose project name and
port, for example `COMPOSE_PROJECT_NAME=shelfshift-second SS_DB_PORT=15433 make db-up`, and point the suites at it
with `TEST_DB=` and `TEST_WEB_DB=`. Run one browser suite at a time: two `make test-web` runs share the same ports.

## Dependencies

Every Python package is pinned in `requirements.lock` (compiled from `requirements.in`) and every web package in
`web/package-lock.json`. `make setup` and `npm ci` install exactly those versions.

## Deploying

Deployment to Google Cloud is a sequence of commands the project owner runs in their own terminal; this repository
never runs them for you. The ordered runbook is [deploy/README.md](deploy/README.md), and the configuration keys it
uses are listed, with placeholders, in `deploy/.env.example`.

## Assistant

Gemini drafts evidence requests; managers send them. People also authorize assumption adoption, stock confirmation, approvals and commitment. The engine computes the options and every figure. Numeric traceability checks references to tool data; it does not certify physical stock, food safety or reasoning. Missing information stays explicit; Gemini can explain the limit and suggest a relevant check.

ADK's LlmAgent and Runner run inside the existing FastAPI service on Cloud Run, calling Gemini through Vertex. The engine stays in-process; the existing small database connection pool, idle cost and latency do not require a separate agent hosting service. Grounding uses only scoped application tools, with no web search. The model cannot choose the workspace, case or human role. Its only write tool stores a checked evidence draft in CASE scope; personal ASK can read, simulate and propose.

History belongs to the app's chat_requests, ai_outputs and tool_results tables. Each request gets a fresh InMemorySessionService and only completed, checked personal history as quoted data. Hidden thoughts are never saved or shown. Personal questions and answers stay session-scoped and out of shared Activity; CASE briefs are shared only with authorized case readers. Historical sources are read-only and require current permission.

Tool progress streams before one complete, number-checked answer. Gemini failure, empty output, timeout or a tool limit shows **Fallback · Gemini unavailable · figures from the engine**. A disconnect shows **Interrupted, ask again**, with no partial answer. Retained attempt metrics and tool audits survive both paths. Otherwise-permitted manual requests, options, approvals and Tasks keep working.

A saved output's usage describes its original generation: model/provider calls, tools and their durations, retries, reported input/output/thinking tokens, first-text and total time, configured model/location and terminal state. Missing provider fields remain null and are listed as Missing; they are never invented zeroes. A legacy output without metrics says usage is missing. Opening stored text separately reports **0 new Gemini calls** and its measured API read-preparation time; that measurement excludes network transit and browser rendering and never replaces the original generation latency.

## User-run baseline and release order

The release owner first finalizes the public dataset, forecasts and evaluated recommendations, then builds a completed inactive template. The baseline job is an explicit paid **user-run** step; it processes every evaluated case, stores checked numbers/sources, records actual successes, failures, reuse, calls, tokens and retries, and does not spend reviewer AI allowances. No billed cost is inferred from token counts.

In the release owner's already-configured database/job environment, the user runs:

```sh
.venv/bin/python -m api.sandbox build "$RELEASE_DATA_DIR" --no-activate --run-id "$TEMPLATE_RUN_ID"
GCP=on .venv/bin/python -m agent.briefs "$TEMPLATE_RUN_ID"
```

Retain the job report. Review the eligible/ineligible, success/failure/reused counts and each saved brief's numbers and sources before activation. Reuse requires the same case version, plan, model and assistant revision. A failed baseline remains Fallback and does not fail the release; it is the explicit **first-open** generation exception, spending the opener's normal AI allowance. Successful unchanged precomputed cases open with zero new model calls. Workspace changes refresh only the affected case; post-change briefs and personal questions stay private to that workspace.

After that review, the user runs:

```sh
.venv/bin/python -m api.sandbox activate "$TEMPLATE_RUN_ID"
```

The release owner's existing benchmark and smoke checks follow activation. The default build still activates automatically unless --no-activate is supplied; the ordered baseline release path must use that option. Startup, deployment, activation, clone and ordinary saved-output reads never run the baseline job. Validate exact copying of baseline briefs, original drafts, usage and cited results, and keep the existing 3 s case and 2 s reset gates before releasing. A draft has no send deadline or consumed request slot until its source manager sends it.

## Configured assistant limits

These are the current api/settings.json values, with model/location supplied through the existing deployment configuration. The finite token values use the observed user-run 09-23 maximums plus a safety margin; four samples do not establish an optimum. The user approved 40 seconds on 2026-10-18 after reviewing the results.

| Limit | Configured value |
|---|---:|
| Tool invocations | 8 tool calls per request |
| Total producer budget, including tools and retry | 40 s per request |
| Thinking | LOW |
| Model-call backstop | 10 per request |
| Output token ceiling | 8,192 per model response |
| Cumulative reported token budget | 65,536 per request |
| Session AI requests | 60 per hour |
| Provider retry | At most one quick retry for HTTP 429 inside the same request budget |
| Evidence requests | 2 groups per case version, only on human Send |
| Evidence wording | 4,000 characters |
| Response deadline | 60 real minutes after human Send |

8 tool calls, 40 s and LOW require an explicit evidence-backed user decision before they change. There is no automatic region switch. The daily AI limit is configured privately; its number is not published. Daily refusal says **Today's AI limit is reached. It resets at 00:00 Bangkok time.** Saved briefs and otherwise-permitted human actions remain available.

## Measured local fake timings

These are local fake measurements through the real ADK Runner, scoped tools, PostgreSQL persistence and authorized HTTP reads. They do not measure Vertex latency or billed token usage. Timings use nearest-rank p50/p95 over retained attempt rows, grouped by action and precomputed admission mode. Missing values are omitted from percentiles with their coverage counted explicitly; failed and interrupted rows remain in their own terminal-state counts. An empty cohort has no percentile, rather than zero.

Observed 2026-10-18 on Darwin arm64, CPython 3.12.12 and local PostgreSQL, under the shared benchmark slot with GCP off. The script ran one precomputed generation, 20 independent two-turn questions using get_case, and 20 openings of the same saved brief. No artificial sleep or provider latency was added.

| Action | Attempts | First text ms p50 / p95 | Total ms p50 / p95 | Model calls p50 / p95 | Tool calls p50 / p95 | Tool duration ms p50 / p95 |
|---|---:|---:|---:|---:|---:|---:|
| Scripted Ask: get_case then answer | 20 | 23.08 / 27.39 | 111.57 / 128.97 | 2.00 / 2.00 | 1.00 / 1.00 | 11.00 / 12.00 |
| Scripted precomputed generation | 1 | 313.89 / 313.89 | 404.90 / 404.90 | 1.00 / 1.00 | 0.00 / 0.00 | No tools |

The 20 stored-brief openings made **0 new model calls**. API read-preparation p50 / p95 was **84.17 / 98.48 ms**; the stored generation metrics above were unchanged. The single baseline sample is reported as one observation, not a reliable latency distribution. All 21 retained attempts ended DONE, with 41 scripted model invocations, 20 completed tools and **0 paid or remote calls**. Input, output and thinking tokens were Missing for all 21 attempts because the fake returned no provider usage metadata. These are fixture observations, not billed tokens or real Vertex performance.

**Paid Vertex measurements: observed 2026-10-18, 2 passed, 2 failed.** One sample per scenario under the earlier producer budget: normal Ask passed (24.85 s total, 19.88 s first text, 2 model calls, 1 tool, 5,830 tokens); a brief with drafts passed (23.21 s total, 6 model calls, 5 tools, 16,959 tokens). The hard six-tool Ask timed out (27.64 s including finalization); the unrelated question failed because it read the case before correctly explaining the limit. These are individual observations, not reliable p50/p95 estimates or deployed performance claims. The user's local database was heavily loaded during the hard question.

The largest accepted output plus thinking was 3,126 tokens: twice that, rounded up to a power of two, sets 8,192 per response. The largest accepted cumulative total was 16,959 (a brief with 5 tools); the 6-tool question timed out before its second turn, so its full use is unobserved. About four times the observed maximum, a power of two, sets 65,536 per request, so an 8-tool question meets the 40 s deadline before the token bound and keeps a safety margin. Both remain within documented model ceilings. The producer budget is now 40 s by explicit user decision; tools stay at eight and thinking stays LOW. Prompts now require unnumbered formatting, one metric reference per figure, and no tools for unrelated questions; numeric checks are unchanged.

**Paid Vertex rerun: pending user-run 09-23.** The earlier two failures remain failures. The updated budget, token limits and prompts require a user-run paid rerun; no success under those settings, full-release baseline success or deployed streaming/disconnect result is claimed. The user supplies credentials locally and selects the opt-in paid suite with its pytest marker, GCP on and explicit consent; automatic tests keep GCP off.

Retain observed counts, missing coverage, failures and the deployed progressive-stream/disconnect check before making real performance claims. The user runs all paid/cloud/deployed checks.

## Read-only usage report

Read a selected local database run's existing ai_usage rows without calling Gemini, invoking tools, spending a cap or changing business data:

```sh
GCP=off .venv/bin/python -m agent.usage "$RUN_ID" --measurement-source local-fake --output assistant-usage-report.json
```

Use the user-observed --measurement-source choice only for records actually observed from a user-run paid Vertex cohort. The source label is an explicit operator declaration, never inferred from model identity or output provenance. Keep fake and paid runs/reports separate; copied template attempts are retained generation records, not independent new provider observations. The JSON contains only aggregate status/reason counts, metric coverage and percentiles, plus public configured limits. It exports no questions, answers, tool inputs/results, session/workspace identifiers, infrastructure identifiers or global-cap numbers.

The deterministic timing fixture is tests/agent/test_usage.py::test_local_usage_benchmark. Its report flag is P09_USAGE_BENCHMARK_REPORT; run it only in a serialized benchmark slot with a disposable local TEST_DB. Normal functional runs skip timing publication. The local table records the measured sample counts and machine context, without a performance claim about the deployed service.

## AI evaluation (user-run)

**Preliminary evaluation · 10 scenarios × 3 runs** measures the assistant and reading services separately from the engine. Review the scenario and reference approval sheet first. Record the human decisions, then commit the approved references and fingerprint through the release approval step before continuing.

Configure GEMINI_MODEL, EVAL_JOB and EVAL_BUCKET in the evaluation job's environment before the checks below. Run preflight from the source checkout containing the committed approval lock, with access to the configured template database.

```sh
python -m agent.eval approval-sheet
python -m agent.eval preflight --template "$TEMPLATE"
```

The user approves the limits before each paid run. Use the same configured image and service identity, one task, no job retries, and the restricted evaluation evidence location. Set the job arguments to the command below with the approved MAX_CALLS, MAX_TOKENS and MAX_COST_USD values; EVAL_ID identifies its retained results.

```sh
python -m agent.eval run --template "$TEMPLATE" --eval-id "$EVAL_ID" --max-calls "$MAX_CALLS" --max-tokens "$MAX_TOKENS" --max-cost-usd "$MAX_COST_USD"
```

Execute the configured job once, then export its checklist:

```sh
gcloud run jobs execute "$EVAL_JOB" --wait
python -m agent.eval export --eval-id "$EVAL_ID" --out "$REVIEW_FILE"
```

Fill the exported checklist in a spreadsheet without changing metadata or item keys. Run the export, import and publish commands in the same configured job environment through its argument override. Import the completed review file before publication:

```sh
python -m agent.eval import --eval-id "$EVAL_ID" --file "$REVIEW_FILE" --reviewer "$REVIEWER"
python -m agent.eval publish --eval-id "$EVAL_ID"
```

Published results appear on About after the completeness and protection checks pass. Failed or unfinished runs remain visible. Cached responses and fresh deterministic results with no model call are recorded separately from paid model observations. A budget refusal prevents the next provider attempt; the evaluation leaves reviewer allowances unchanged.

## Limitations

- Fact checks test each unresolved fact on its own: a plan that survives each fact alone but fails when two are true together is not detected, so ShelfShift claims no complete feasibility protection.
- A lot with mixed expiry dates is held for people to separate; the app doesn't split it.
- Staff must state when they checked a count or a date; that stops silent misdating but can't prove a typed time accurate.

## Data

Sales history: FreshRetailNet-50K by Dingdong-Inc, https://huggingface.co/datasets/Dingdong-Inc/FreshRetailNet-50K, © Dingdong-Inc, licensed under CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/). ShelfShift is not affiliated with or endorsed by Dingdong-Inc.

Stock, lots, expiry and deliveries are simulated; prices, costs, shelf life, routes and storage are assumptions.

See [DATASET.md](DATASET.md) for the attribution, the assumptions, the modifications and the limits.

## What is real, simulated and not yet proven

Public sales from FreshRetailNet-50K, with simulated stock and labelled assumptions. A deterministic engine scores the supported options. Precomputed forecasts name their own provider, run and history cutoff; an installed run supplies its provider summary rather than an assumed provider claim.

**Forecast training inputs.** Up to each run's cutoff, public sale_amount is scaled by the assumed units factor. Full-price in-stock days are kept as recorded. Discounted in-stock days are converted to estimated full-price units using the store's assumed markdown response; days with an unknown price stay at base price. Stockout days (any hour out of stock between 06:00 and 22:00) are imputed from the same weekday's in-stock days, never below the day's own converted units.

**Benchmark sales replay.** The evaluation uses recorded units sold as a demand proxy. It does not estimate unmet demand during stockouts or remove historical promotion effects. It is a sales replay, not true demand, with the same price response in every scenario so promotions are not counted twice. This describes the evaluation method; no result is published in this release.

### Synthetic 5-seed engineering check

Synthetic retail scenarios with a weekday profile derived from FreshRetailNet-50K and explicitly stated demand, ordering and markdown assumptions.

In the synthetic 5-seed engineering check, delivery sizing from in-stock days only is biased low: when the order is 90–100% of the estimate, the in-stock days are the lower-demand days, which produces the slow rise in stockouts shown in the synthetic check.

In public FreshRetailNet-50K histories, sales during stockout hours are censored and cannot by themselves establish uncensored demand; stockout-day imputation for forecast training is an explicit modeling assumption.

The check exists in code and has no published result in this release.

### Not yet proven

There is no real retailer data. Stock, lots and deliveries are simulated from the public sales. Costs, shelf life, markdown recovery, holding and transfer costs are assumed parameters. The markdown response and the cost of a trip that ships nothing are assumptions.

This dataset comes from online grocery. Its stockout patterns may differ from those in other retail settings. The extra sales a markdown brings are an assumption: the dataset's discounts are promotions, not markdowns for stock near expiry.

Stock records follow a declared error model, an assumption rather than an error measured at a store. The forecast does not learn from what happens in the app.

Execution is simulated. No point-of-sale, ordering or supplier data feed is connected, and there has been no store trial. The modeled store rule ignores inbound deliveries when estimating surplus. Differences reflect this limitation as well as ShelfShift's planning. Waste reductions are compared only with automatic markdowns and no other action, never claimed lower than the modeled store rule.

Reviewers each get their own workspace in their own browser, with shared prototype accounts per store and for HQ. A real pilot needs a personal sign-in for every user and one shared organization workspace across devices, with access limited to that organization.

The prototype's reference data can be rebuilt from release files, but that does not bring back counts, approvals, uploaded evidence or activity recorded here. A real pilot needs automatic database backups, point-in-time recovery, a tested restore and agreed recovery targets.

The prototype runs on one small server and one small database. What-if runs started at the same time wait their turn and can take a few seconds.

Branch orders is not part of this release.
