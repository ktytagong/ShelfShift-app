# ShelfShift

**Public sales dataset · Simulated operational scenario.** ShelfShift is a working prototype that helps branch
managers and planners decide what to do with short-life stock one branch will not sell before it expires: move it to
another branch, mark it down or keep it, with every number computed by a deterministic engine and the stock facts
checked before anything moves. The scenario is hypothetical: the sales history is public (see Data below), and stock,
lots, expiry and deliveries are simulated. The app and these docs are in English.

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
   http://127.0.0.1:8080. Open it and enter your passcode. A second `docker compose up` is safe: the init reruns and
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
