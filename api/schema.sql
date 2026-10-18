-- ShelfShift app state (spec §5 Postgres). Applied by `python -m api.db migrate`; every statement is idempotent.
-- Business times are demo-day indexes plus minutes after midnight, as in the engine (spec §8 Demo clock);
-- real times (holds, decisions, uploads) are timestamptz. Every quantity and amount is float8, never numeric:
-- pg8000 returns numeric as Decimal, which the engine and the overrides must never see (04-RESEARCH Pattern 5).
-- The whole plan schema is ported, including the signal, request and assistant tables later phases use, so the
-- deployed Cloud SQL database needs no migration for them (04-CONTEXT, Claude's discretion).
-- Comments never hold a semicolon: migrate splits the file on semicolons.

-- Shared, read-only reference data keyed by dataset_version (loaded once by the deploy job)
CREATE TABLE IF NOT EXISTS ref_data (dataset_version text, part text, payload jsonb NOT NULL,
  PRIMARY KEY (dataset_version, part));                        -- parts are seed and forecast
CREATE TABLE IF NOT EXISTS ref_truth (dataset_version text PRIMARY KEY, payload jsonb NOT NULL);  -- hidden truth

-- A public artifact set (05.2 D-08, D-11, api/publicref.py): shared, read-only, relational, keyed by dataset_version,
-- store and product, loaded by COPY from a manifest-checked set and read one series at a time. No table here has a
-- run_id column, so no clone or prune ever touches it. ref_set is written last in the install transaction: a version
-- listed there is complete. Each table's columns after dataset_version are its part's columns in the part's order.
CREATE TABLE IF NOT EXISTS ref_set (dataset_version text PRIMARY KEY, kind text NOT NULL, manifest jsonb NOT NULL,
  loaded_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS ref_sales_day (dataset_version text, store text, sku text, day int,
  units float8 NOT NULL, sale_amount float8 NOT NULL, oos_hours int NOT NULL, stockout boolean NOT NULL,
  discount float8 NOT NULL, unknown_price boolean NOT NULL, holiday boolean NOT NULL, activity boolean NOT NULL,
  PRIMARY KEY (dataset_version, store, sku, day));
CREATE TABLE IF NOT EXISTS ref_weather_day (dataset_version text, store text, day int, precip float8, temp float8,
  humidity float8, wind float8, PRIMARY KEY (dataset_version, store, day));
CREATE TABLE IF NOT EXISTS ref_product (dataset_version text, sku text, mgmt text, cat1 text, cat2 text, cat3 text,
  category text, zone text NOT NULL, price float8, disposal_cost float8 NOT NULL, units_per_tote int NOT NULL,
  kg_per_unit float8 NOT NULL, shelf_life_days int NOT NULL, schedule jsonb NOT NULL, name text,
  PRIMARY KEY (dataset_version, sku));                       -- name is null: products show their codes (D-02)
CREATE TABLE IF NOT EXISTS ref_store (dataset_version text, store text, city text NOT NULL, geo_known boolean NOT NULL,
  PRIMARY KEY (dataset_version, store));
CREATE TABLE IF NOT EXISTS ref_lot0 (dataset_version text, lot_id text, store text NOT NULL, sku text NOT NULL,
  qty float8 NOT NULL, expiry_day int NOT NULL, received_seq int NOT NULL, markdown_pct int NOT NULL,
  expiry_source text NOT NULL, last_verified_day int, tag text, origin text,
  PRIMARY KEY (dataset_version, lot_id));                    -- the day-0 record, simulated (D-03)
CREATE TABLE IF NOT EXISTS ref_inbound0 (dataset_version text, inbound_id text, store text NOT NULL, sku text NOT NULL,
  qty float8 NOT NULL, arrival_day int NOT NULL, arrival_min int NOT NULL, expiry_day int NOT NULL,
  received_seq int NOT NULL, expiry_source text NOT NULL, last_verified_day int, origin text,
  PRIMARY KEY (dataset_version, inbound_id));
CREATE TABLE IF NOT EXISTS ref_run (dataset_version text, run_code text, day int, origin text NOT NULL,
  dest text NOT NULL, loading_cutoff_min int NOT NULL, departs_min int NOT NULL, arrives_min int NOT NULL,
  spare_totes int NOT NULL, cold_chain boolean NOT NULL, fixed_cost float8 NOT NULL, per_unit_cost float8 NOT NULL,
  cancel_cost float8, travel_min int, PRIMARY KEY (dataset_version, run_code, day));  -- cancel_cost null = the quote
CREATE TABLE IF NOT EXISTS ref_capacity (dataset_version text, store text, zone text, day int, free_totes int NOT NULL,
  PRIMARY KEY (dataset_version, store, zone, day));
CREATE TABLE IF NOT EXISTS ref_forecast_run (dataset_version text, forecast_run_id text, job text NOT NULL,
  demo_day int, cutoff_day int NOT NULL, first_day int NOT NULL, last_day int NOT NULL, meta jsonb NOT NULL,
  PRIMARY KEY (dataset_version, forecast_run_id));           -- demo_day is null for the backtest
CREATE TABLE IF NOT EXISTS ref_forecast_point (dataset_version text, forecast_run_id text, store text, sku text,
  day int, low float8 NOT NULL, base float8 NOT NULL, high float8 NOT NULL, provider text NOT NULL,
  clamped boolean NOT NULL, raw_low float8, raw_base float8, raw_high float8,
  PRIMARY KEY (dataset_version, forecast_run_id, store, sku, day));
-- Hidden truth (spec §5): read only by advance-day and the template checks, never by a route
CREATE TABLE IF NOT EXISTS ref_truth_physical (dataset_version text, lot_id text, qty float8 NOT NULL,
  PRIMARY KEY (dataset_version, lot_id));
CREATE TABLE IF NOT EXISTS ref_truth_demand (dataset_version text, store text, sku text, day int,
  demand float8 NOT NULL, PRIMARY KEY (dataset_version, store, sku, day));
CREATE INDEX IF NOT EXISTS ref_run_by_origin ON ref_run (dataset_version, origin, day);
CREATE INDEX IF NOT EXISTS ref_lot0_by_series ON ref_lot0 (dataset_version, store, sku);
CREATE INDEX IF NOT EXISTS ref_inbound0_by_series ON ref_inbound0 (dataset_version, store, sku);
CREATE INDEX IF NOT EXISTS ref_truth_demand_by_day ON ref_truth_demand (dataset_version, sku, day);

CREATE TABLE IF NOT EXISTS demo_runs (
  run_id text PRIMARY KEY,
  kind text NOT NULL CHECK (kind IN ('TEMPLATE', 'SANDBOX')),
  dataset_version text NOT NULL,
  template_run_id text,
  status text NOT NULL CHECK (status IN ('INITIALIZING', 'ACTIVE', 'RETIRED', 'FAILED')),
  day int NOT NULL,
  version int NOT NULL DEFAULT 0,
  next_case_no int NOT NULL,
  failure text,
  created_at timestamptz NOT NULL DEFAULT now(),
  retired_at timestamptz);                                   -- when a reset retired it (IN-01 pruning)

-- READY means the template passed every build check but has not been published (09 D-25).
ALTER TABLE demo_runs DROP CONSTRAINT IF EXISTS demo_runs_status_check;
ALTER TABLE demo_runs ADD CONSTRAINT demo_runs_status_check
  CHECK (status IN ('INITIALIZING', 'READY', 'ACTIVE', 'RETIRED', 'FAILED'));
ALTER TABLE demo_runs ADD COLUMN IF NOT EXISTS activated_at timestamptz;
ALTER TABLE demo_runs ADD COLUMN IF NOT EXISTS predecessor_run_id text;

-- Policy settings written in one sandbox (04-CONTEXT D-09, D-23), as field and JSON value. The seeded Policy is
-- in ref_data, and a clone copies these rows like any per-run table.
CREATE TABLE IF NOT EXISTS run_policy (
  run_id text, field text, value jsonb NOT NULL,
  PRIMARY KEY (run_id, field));

-- Reading proposals only (10-CONTEXT D-02, D-22, D-24; research Pitfall 1).
-- run_id first preserves precomputed cloning; a content key is not an ai_outputs key.
CREATE TABLE IF NOT EXISTS readings (
  run_id text NOT NULL, reading_id text NOT NULL, input_key text NOT NULL,
  purpose text NOT NULL CHECK (purpose IN ('REPORT', 'ANSWER')),
  signal_id text, signal_revision int, request_id text,
  status text NOT NULL CHECK (status IN ('GENERATING', 'DONE', 'INTERRUPTED', 'FAILED', 'REFUSED')),
  attempt int NOT NULL DEFAULT 1 CHECK (attempt BETWEEN 1 AND 2),
  after_confirmation boolean NOT NULL DEFAULT false, kind text,
  suggestions jsonb NOT NULL DEFAULT '[]', needs_input jsonb NOT NULL DEFAULT '[]',
  observations jsonb NOT NULL DEFAULT '[]', note boolean NOT NULL DEFAULT false,
  model_id text NOT NULL, assistant_version text NOT NULL,
  provenance text NOT NULL CHECK (provenance IN ('LIVE', 'PRECOMPUTED')),
  reason text, usage jsonb, lease_until timestamptz, created_by text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(), finished_at timestamptz,
  PRIMARY KEY (run_id, reading_id));
CREATE UNIQUE INDEX IF NOT EXISTS one_reading_at_a_time
  ON readings (run_id, input_key) WHERE status = 'GENERATING';

-- Not keyed by run: the browser session, its seat and its current sandbox
CREATE TABLE IF NOT EXISTS sessions (
  session_id text PRIMARY KEY,
  role text NOT NULL DEFAULT 'manager',                      -- manager or hq
  branch text,                                               -- the manager's branch
  run_id text,
  last_reset_ms int,
  created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS usage_counts (
  scope text, kind text, window_start timestamptz, count int NOT NULL,
  PRIMARY KEY (scope, kind, window_start));                  -- scope is a session_id, or 'global'
-- Keyed by sha256 of the full-precision input (round-3 IN-01), so two inputs a cent apart never share a result.
-- rec_version stays the version label and the stale-version check.
CREATE TABLE IF NOT EXISTS engine_cache (
  cache_key text PRIMARY KEY, rec_version text NOT NULL, result jsonb NOT NULL, fact_checks jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now());
-- A sandbox write stores its run. Session-level writes (seat switch, reset) store run_id '' so a retried reset
-- finds its key after the session moved to a new run (04-API-CONTRACT Conventions, research A13).
CREATE TABLE IF NOT EXISTS idempotency_keys (
  key text, session_id text, run_id text, endpoint text, body_hash text NOT NULL, status int, response jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (key, session_id, run_id, endpoint));
-- Pruning (IN-01) finds a retired run's keys and old session-scoped keys through this index. A database made
-- before these columns existed gets them here.
ALTER TABLE demo_runs ADD COLUMN IF NOT EXISTS retired_at timestamptz;
ALTER TABLE engine_cache ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE idempotency_keys ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now();
CREATE INDEX IF NOT EXISTS idempotency_keys_by_run ON idempotency_keys (run_id, created_at);

-- Per-run tables. Composite keys start with run_id and there are no shared serials, so a clone copies rows
-- changing only run_id. World is shelfshift, baseline_rule or do_nothing.
CREATE TABLE IF NOT EXISTS lots (                            -- RECORDED stock, what the app sees
  run_id text, world text, lot_id text, branch text NOT NULL, sku text NOT NULL, qty float8 NOT NULL,
  expiry_day int NOT NULL, received_seq int NOT NULL, markdown_pct int NOT NULL DEFAULT 0,
  expiry_source text NOT NULL, last_verified_day int, verified_by text, tag text,
  origin text,                                               -- the seed's origin tag (03-CONTEXT D-12)
  moved_from text,                                           -- the source lot of a delivered transfer, else null
  PRIMARY KEY (run_id, world, lot_id));
CREATE TABLE IF NOT EXISTS truth_lots (                      -- PHYSICAL stock, read only by advance-day and metrics
  run_id text, world text, lot_id text, qty float8 NOT NULL, PRIMARY KEY (run_id, world, lot_id));
CREATE TABLE IF NOT EXISTS inbound (
  run_id text, world text, inbound_id text, branch text NOT NULL, sku text NOT NULL, qty float8 NOT NULL,
  arrival_day int NOT NULL, arrival_min int NOT NULL, expiry_day int NOT NULL, received_seq int NOT NULL,
  expiry_source text NOT NULL DEFAULT 'PRINTED', last_verified_day int, tag text, source_case_id text,
  origin text,
  PRIMARY KEY (run_id, world, inbound_id));
CREATE TABLE IF NOT EXISTS world_actions (                   -- prepared work orders, executed by advance-day
  run_id text, world text, action_id text, case_id text, kind text NOT NULL, lot_id text NOT NULL, sku text NOT NULL,
  qty float8, dest text, run_code text, run_day int, pct int, effective_day int, sellable_from int,
  fixed_cost float8, per_unit_cost float8, cancel_cost float8, state text NOT NULL DEFAULT 'PLANNED', new_lot_id text,
  origin text,                                               -- the shipment's source branch
  CHECK (kind <> 'TRANSFER' OR origin IS NOT NULL),          -- a transfer always names its origin (contract-diff 1, 55)
  PRIMARY KEY (run_id, world, action_id));
CREATE TABLE IF NOT EXISTS outcomes (
  run_id text, world text, day int, branch text, sku text, demand float8 NOT NULL, sold float8 NOT NULL,
  wasted float8 NOT NULL, lost float8 NOT NULL, revenue float8 NOT NULL, transport float8 NOT NULL,
  handling float8 NOT NULL, disposal float8 NOT NULL, PRIMARY KEY (run_id, world, day, branch, sku));
CREATE TABLE IF NOT EXISTS world_events (                    -- SHORT_SHIPMENT, RECORD_CORRECTION, TRANSFER_RECEIVED, TRANSFER_EXPIRED
  run_id text, world text, day int, seq int, kind text NOT NULL, payload jsonb NOT NULL,
  PRIMARY KEY (run_id, world, day, seq));
-- The series (world, branch, sku) a workspace owns (05.2 D-09, api/overlay.py). A workspace reads the world tables
-- above through its template until its first write to a series copies that series in. A world value of '*' means
-- every series (Next day). Reset copies none of these rows.
CREATE TABLE IF NOT EXISTS run_series (
  run_id text, world text, branch text, sku text,
  PRIMARY KEY (run_id, world, branch, sku));
-- One series of a run is read and copied through these (05.2-MEASUREMENTS: 50 ms per series with them, 156 without)
CREATE INDEX IF NOT EXISTS lots_by_series ON lots (run_id, world, sku, branch);
CREATE INDEX IF NOT EXISTS inbound_by_series ON inbound (run_id, world, sku, branch);
CREATE INDEX IF NOT EXISTS outcomes_by_series ON outcomes (run_id, world, sku, branch);

CREATE TABLE IF NOT EXISTS cases (
  run_id text, case_id text, key text NOT NULL, lot_id text NOT NULL, source_branch text NOT NULL, sku text NOT NULL,
  tag text, state text NOT NULL, active_rec_version text, round int NOT NULL DEFAULT 0, opened_day int NOT NULL,
  look_round int NOT NULL DEFAULT 0,                         -- rounds up to this one belong to earlier looks (second look)
  waiting_until_day int, reason text, hold_expired boolean NOT NULL DEFAULT false, re_review boolean NOT NULL DEFAULT false,
  adopted jsonb NOT NULL DEFAULT '[]', decided_ms int,
  missing_data text,                                         -- the engine's missing-data state, apart from reason (WR-05)
  PRIMARY KEY (run_id, case_id));
-- reason holds only the human why (waiting, rejected, invalidated, closed). A database made before missing_data
-- existed gets the column here.
ALTER TABLE cases ADD COLUMN IF NOT EXISTS missing_data text;
CREATE UNIQUE INDEX IF NOT EXISTS one_open_case_per_lot ON cases (run_id, lot_id)
  WHERE state NOT IN ('CLOSED', 'REJECTED');
-- The recommended plan's destination (null unless a transfer), so the board can scope a case to either store
-- (05.2 D-26). A database made before this column existed gets it here.
ALTER TABLE cases ADD COLUMN IF NOT EXISTS plan_dest text;
-- Select another plan (07-CONTEXT D-23): {rec_version, plan_key, label, fact_checks, by, at}, the source manager's
-- choice among the active version's engine options with that option's own fact checks; in force only while its
-- rec_version is the active one, cleared by a new version (research F-21, Pitfall 11). Null: the recommended plan.
ALTER TABLE cases ADD COLUMN IF NOT EXISTS selected jsonb;
-- A lot the morning screen flagged on a public set whose engine search waits until someone opens it (05.2 D-23..D-25,
-- D-28, api/flags.py): the screen's own figures, never a plan. A workspace reads its template's flags (it is never
-- cloned, api/overlay.py OVERLAID) and its own once its day differs. missing_data names a lot the screen could not
-- judge for lack of data: flagged, never skipped.
CREATE TABLE IF NOT EXISTS case_flags (
  run_id text, day int, lot_id text, key text NOT NULL, branch text NOT NULL, sku text NOT NULL,
  a boolean, b boolean, keep_waste float8, stock_at_step float8, step_day int,
  usable_runs jsonb NOT NULL DEFAULT '[]', missing_data text, tag text, origin text,
  PRIMARY KEY (run_id, day, lot_id));
CREATE INDEX IF NOT EXISTS case_flags_by_branch ON case_flags (run_id, day, branch);
CREATE TABLE IF NOT EXISTS recommendations (
  run_id text, case_id text, rec_version text, day int NOT NULL, engine_input jsonb NOT NULL, result jsonb NOT NULL,
  fact_checks jsonb NOT NULL, is_whatif boolean NOT NULL DEFAULT false, overrides jsonb NOT NULL DEFAULT '[]',
  source_note text, created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (run_id, case_id, rec_version));
CREATE TABLE IF NOT EXISTS reservations (                    -- DEMAND, STORAGE, TRUCK, SOURCE_LOT, LOT_PRICE
  run_id text, res_id text, case_id text NOT NULL, round int NOT NULL, kind text NOT NULL, bucket text NOT NULL,
  day int, qty float8 NOT NULL, state text NOT NULL,         -- HELD, COMMITTED, CONVERTED, RELEASED, EXPIRED, DONE
  expires_at timestamptz, PRIMARY KEY (run_id, res_id));
CREATE TABLE IF NOT EXISTS decisions (
  run_id text, case_id text, rec_version text, round int, role text, decision text NOT NULL, session_id text,
  reason text, decided_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (run_id, case_id, rec_version, round, role));
-- The acknowledgement an approve or accept gave (07-CONTEXT D-12, D-26, research F-27): {rec_version, plan_key,
-- fact_ids} of the plan in force's Advisory facts, null when it had none (and on reject, decline, or a row stored
-- before Phase 7)
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS acknowledged jsonb;
-- The key of the plan the decision was given for (07-CONTEXT D-23): the plan in force's, on every decision
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS plan_key text;
-- When the approved plan this decision belongs to was cancelled (07-CONTEXT D-23): the row stays in history, marked
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS cancelled_at timestamptz;
CREATE TABLE IF NOT EXISTS activity_log (
  run_id text, seq int, at timestamptz NOT NULL DEFAULT now(), day int NOT NULL, type text NOT NULL, case_id text,
  payload jsonb NOT NULL DEFAULT '{}', PRIMARY KEY (run_id, seq));

CREATE TABLE IF NOT EXISTS signals (
  run_id text, signal_id text, revision int NOT NULL DEFAULT 1, reporter_role text NOT NULL, reporter_branch text,
  branch text, text text NOT NULL DEFAULT '', file_uri text, file_type text, kind text, fields jsonb NOT NULL DEFAULT '{}',
  state text NOT NULL, request_id text, provenance text, created_day int NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (run_id, signal_id));
CREATE TABLE IF NOT EXISTS reported_facts (
  run_id text, fact_id text, signal_id text NOT NULL, kind text NOT NULL, adjustments jsonb NOT NULL, sku text,
  valid_from int NOT NULL, valid_to int NOT NULL, label text NOT NULL, confirmed_by text NOT NULL, confirmed_day int NOT NULL,
  confirmed_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (run_id, fact_id));
CREATE TABLE IF NOT EXISTS evidence_requests (
  run_id text, request_id text, case_id text NOT NULL, rec_version text NOT NULL, fact_id text NOT NULL,
  fact_kind text NOT NULL, sku text NOT NULL, lot_id text NOT NULL, to_branch text NOT NULL, ask_text text NOT NULL,
  lot_ids jsonb NOT NULL DEFAULT '[]',                       -- every lot the fact covers (round-2 WR-03)
  language text, due_day int NOT NULL, due_min int NOT NULL, state text NOT NULL, partial_note text,
  answer_signal_id text, regret float8, created_by text NOT NULL, created_day int NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (run_id, request_id),
  UNIQUE (run_id, case_id, rec_version, fact_id));
-- The recount that replaced this request (G-05-2 item 12).
ALTER TABLE evidence_requests ADD COLUMN IF NOT EXISTS replaced_by text;
-- A replaced request's engine fact id: its fact_id becomes fact_id#request_id, so the UNIQUE above keeps holding the
-- one live request per fact (additive only, Phase 6 D-17).
ALTER TABLE evidence_requests ADD COLUMN IF NOT EXISTS replaced_fact_id text;
-- A request's real deadline (07-CONTEXT D-05), stored UTC
ALTER TABLE evidence_requests ADD COLUMN IF NOT EXISTS due_at timestamptz;
-- When the branch says it checked (07-CONTEXT D-22), stored UTC
ALTER TABLE signals ADD COLUMN IF NOT EXISTS observed_at timestamptz;
-- When the addressed branch closed the request with Can't check (07-CONTEXT D-07), stored UTC
ALTER TABLE evidence_requests ADD COLUMN IF NOT EXISTS closed_at timestamptz;
-- Who closed it: the addressed branch's manager as "manager:<branch>" (07-CONTEXT D-07)
ALTER TABLE evidence_requests ADD COLUMN IF NOT EXISTS closed_by text;
-- The combined request a part belongs to (07-CONTEXT D-11, D-22): the first part's request_id, shared by a count part
-- and an expiry part asked together; null on a single request, whose own request_id stands in. The per-version limit
-- counts distinct groups, and Can't check closes every open part of one
ALTER TABLE evidence_requests ADD COLUMN IF NOT EXISTS request_group text;
-- What the part asks (07-CONTEXT D-11): "count" (Count stock) or "expiry" (Check printed expiry date); null on a row
-- stored before 07-08, which is a count
ALTER TABLE evidence_requests ADD COLUMN IF NOT EXISTS part text;
-- Phase 09 human Send. Original checked Gemini wording stays on evidence_drafts.
-- Nullable additions preserve older requests without inventing a sender or send time.
ALTER TABLE evidence_requests ADD COLUMN IF NOT EXISTS draft_id text;
ALTER TABLE evidence_requests ADD COLUMN IF NOT EXISTS message text;
ALTER TABLE evidence_requests ADD COLUMN IF NOT EXISTS sent_by text;
ALTER TABLE evidence_requests ADD COLUMN IF NOT EXISTS sent_at timestamptz;
ALTER TABLE evidence_requests ADD COLUMN IF NOT EXISTS asked_at timestamptz;
ALTER TABLE evidence_requests ADD COLUMN IF NOT EXISTS plan_key text;
ALTER TABLE evidence_requests ADD COLUMN IF NOT EXISTS request_context jsonb;
-- A confirmed fact's check time (07-CONTEXT D-22): compared with the fact in force at confirm; null before Phase 7,
-- when confirmed_at stands in for it
ALTER TABLE reported_facts ADD COLUMN IF NOT EXISTS observed_at timestamptz;
-- The branch the fact is about (a count's addressed branch; a field report's branch), for its label and scope (D-15)
ALTER TABLE reported_facts ADD COLUMN IF NOT EXISTS branch text;
-- The storage zone a branch fact applies to (research F-9); null applies to every zone
ALTER TABLE reported_facts ADD COLUMN IF NOT EXISTS zone text;
-- The later fact that replaced this one (a D-15 correction, 07-13); a replaced fact is never in force
ALTER TABLE reported_facts ADD COLUMN IF NOT EXISTS replaced_by text;
-- Where the fact came from: "request" (an answered request), or a field report's source (07-13)
ALTER TABLE reported_facts ADD COLUMN IF NOT EXISTS source text;
-- An attached file's sha256 (07-CONTEXT D-20): its object name is <run_id>/<sha256>.<ext>, never the client's name
ALTER TABLE signals ADD COLUMN IF NOT EXISTS file_sha256 text;
-- Optional reading provenance; confirmation remains a separate human operation (D-24).
ALTER TABLE signals ADD COLUMN IF NOT EXISTS reading_id text;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS field_sources jsonb;
ALTER TABLE reported_facts ADD COLUMN IF NOT EXISTS reading_id text;
-- The attached file's size in bytes (at most uploads.max_bytes)
ALTER TABLE signals ADD COLUMN IF NOT EXISTS file_size int;
-- Whether the file spent the workspace's file allowance (a report's first file, D-20); false for an answer photo or a
-- replacement, which spend only the cross-workspace upload cap; null without a file
ALTER TABLE signals ADD COLUMN IF NOT EXISTS file_counted boolean;
-- Field reports filed by hand (07-CONTEXT D-14, D-15, D-17, D-20; research F-26). A report is a signals row with
-- report_id set (the first revision's signal_id, shared by every revision); an answer to a request has none. Its own
-- states: READ (filed), CONFIRMED, DISMISSED (by the owner; dismissed_at and dismissed_by tell it apart from an
-- answer's replaced revision, F-26), WITHDRAWN (by the reporter, before confirmation).
ALTER TABLE signals ADD COLUMN IF NOT EXISTS report_id text;
-- The case it was filed from, and the lot, product and run it is about (scope and pre-filled fields)
ALTER TABLE signals ADD COLUMN IF NOT EXISTS case_id text;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS lot_id text;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS sku text;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS run_code text;
-- Who confirmed it and when (the owner; self-confirmation allowed, D-15), stored UTC
ALTER TABLE signals ADD COLUMN IF NOT EXISTS confirmed_at timestamptz;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS confirmed_by text;
-- Who dismissed it, when and why (the owner; never applied, D-15)
ALTER TABLE signals ADD COLUMN IF NOT EXISTS dismissed_at timestamptz;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS dismissed_by text;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS dismiss_reason text;
-- When the reporter withdrew it, before confirmation (no operational effect, D-15)
ALTER TABLE signals ADD COLUMN IF NOT EXISTS withdrawn_at timestamptz;
-- The CONFIRMED report this one corrects: its confirmed fact replaces that report's fact from then on (D-15)
ALTER TABLE signals ADD COLUMN IF NOT EXISTS corrects text;
-- The live cases in the report's scope when it was filed (the New report badge, D-16), a JSON array of case ids
ALTER TABLE signals ADD COLUMN IF NOT EXISTS affects jsonb;
-- The reported fact its confirmation wrote
ALTER TABLE signals ADD COLUMN IF NOT EXISTS fact_id text;
-- Mark done on an advisory task (a confirmed DISPLAY condition report, 07-CONTEXT D-13): when and by whom its branch's
-- manager closed it, stored UTC; the task leaves the open count, its history is kept, and no hold is touched
ALTER TABLE signals ADD COLUMN IF NOT EXISTS task_done_at timestamptz;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS task_done_by text;
-- The impact preview of this revision (07-CONTEXT D-16): "Potential impact · unconfirmed", up to reports.preview_cases
-- affected live cases re-run as what-ifs and the rest counted; stored per revision, so a repeat runs no engine and an
-- edit (a new revision) previews afresh. It changes nothing and blocks nothing
ALTER TABLE signals ADD COLUMN IF NOT EXISTS preview jsonb;
-- Where a request came from (07-CONTEXT D-18, 07-16): 'simulated' for one the template build preloaded ("Simulated
-- request"); null for a person's or the agent's. A preloaded answer or report carries it in signals.provenance
ALTER TABLE evidence_requests ADD COLUMN IF NOT EXISTS provenance text;
-- When the template build preloaded its simulated material (07-CONTEXT D-05, D-18; api/preload.py): a clone shifts
-- every preloaded time by its own now() minus this, so each workspace's deadlines start when it is created. demo_runs
-- is not cloned; null on a sandbox and on a template built before 07-16
ALTER TABLE demo_runs ADD COLUMN IF NOT EXISTS preloaded_at timestamptz;

-- Assistant (Phase 9). Chat history lives in our own tables, the spec's documented fallback to ADK's
-- DatabaseSessionService. The agent gets the case's earlier turns passed in on each call.
CREATE TABLE IF NOT EXISTS tool_results (                    -- the audit trail and what metric references resolve to
  run_id text, result_id text, scope text NOT NULL, session_id text, case_id text, rec_version text, signal_id text,
  signal_revision int, request_id text, tool text NOT NULL, inputs jsonb NOT NULL, result jsonb NOT NULL, duration_ms int,
  created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (run_id, result_id));
CREATE TABLE IF NOT EXISTS ai_outputs (                      -- briefs, answers, signal readings and impact summaries
  run_id text, output_id text, kind text NOT NULL, case_id text, rec_version text, signal_id text, signal_revision int,
  session_id text, request_id text, question text, text text NOT NULL DEFAULT '', refs jsonb NOT NULL DEFAULT '[]',
  checks jsonb NOT NULL DEFAULT '{}', model_id text, provenance text NOT NULL, status text NOT NULL, reason text,
  created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (run_id, output_id));
CREATE UNIQUE INDEX IF NOT EXISTS one_generation_at_a_time ON ai_outputs (run_id, kind, coalesce(case_id, ''),
  coalesce(rec_version, ''), coalesce(signal_id, ''), coalesce(signal_revision, 0)) WHERE status = 'GENERATING';
CREATE TABLE IF NOT EXISTS chat_requests (
  run_id text, request_id text, session_id text NOT NULL, case_id text, kind text NOT NULL, status text NOT NULL,
  started_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (run_id, request_id));

-- Phase 09 admission and checked publication. Extend the existing output store.
ALTER TABLE ai_outputs ADD COLUMN IF NOT EXISTS plan_key text;
ALTER TABLE ai_outputs ADD COLUMN IF NOT EXISTS assistant_version text;
ALTER TABLE ai_outputs ADD COLUMN IF NOT EXISTS snapshot_id text;
ALTER TABLE ai_outputs ADD COLUMN IF NOT EXISTS actor jsonb;
ALTER TABLE ai_outputs ADD COLUMN IF NOT EXISTS sources jsonb NOT NULL DEFAULT '[]';
ALTER TABLE ai_outputs ADD COLUMN IF NOT EXISTS cards jsonb NOT NULL DEFAULT '[]';
-- 10-CONTEXT D-23: the deterministic preview key this impact summary was written from.
ALTER TABLE ai_outputs ADD COLUMN IF NOT EXISTS input_key text;
-- SIGNAL attempts have no CASE tuple; preserve honest usage without weakening ai_usage.
ALTER TABLE ai_outputs ADD COLUMN IF NOT EXISTS signal_usage jsonb;
-- Install the more precise guard before removing its older, overly broad index.
CREATE UNIQUE INDEX IF NOT EXISTS one_scoped_generation_at_a_time ON ai_outputs
  (run_id, kind, coalesce(case_id, ''), coalesce(rec_version, ''), coalesce(plan_key, ''),
   coalesce(signal_id, ''), coalesce(signal_revision, 0), coalesce(session_id, ''), coalesce(request_id, ''))
  WHERE status = 'GENERATING';
DROP INDEX IF EXISTS one_generation_at_a_time;
ALTER TABLE chat_requests ADD COLUMN IF NOT EXISTS rec_version text;
ALTER TABLE chat_requests ADD COLUMN IF NOT EXISTS plan_key text;
ALTER TABLE chat_requests ADD COLUMN IF NOT EXISTS actor jsonb;
ALTER TABLE chat_requests ADD COLUMN IF NOT EXISTS question text;
ALTER TABLE chat_requests ADD COLUMN IF NOT EXISTS body_hash text;
ALTER TABLE chat_requests ADD COLUMN IF NOT EXISTS idempotency_key text;
CREATE TABLE IF NOT EXISTS ai_usage (
  run_id text, attempt_id text, output_id text NOT NULL, request_id text, session_id text,
  case_id text NOT NULL, rec_version text NOT NULL, plan_key text NOT NULL, kind text NOT NULL,
  model_id text NOT NULL, location text NOT NULL, status text NOT NULL, reason text,
  model_calls int, provider_attempts int, tool_calls int, retries int,
  input_tokens int, output_tokens int, thinking_tokens int, total_tokens int, tool_use_prompt_tokens int,
  first_text_ms float8, total_ms float8, tool_durations jsonb NOT NULL DEFAULT '[]',
  started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
  PRIMARY KEY (run_id, attempt_id), UNIQUE (run_id, output_id));
-- Retain admission mode even when final provenance becomes FALLBACK. Legacy
-- attempts remain unknown; do not infer that a failed baseline spent a cap.
ALTER TABLE ai_usage ADD COLUMN IF NOT EXISTS precomputed boolean;

-- Phase 09 tools: exact plan/session/actor scope and an honest success/failure audit.
ALTER TABLE tool_results ADD COLUMN IF NOT EXISTS plan_key text;
ALTER TABLE tool_results ADD COLUMN IF NOT EXISTS actor jsonb;
ALTER TABLE tool_results ADD COLUMN IF NOT EXISTS snapshot_id text;
ALTER TABLE tool_results ADD COLUMN IF NOT EXISTS status text;
ALTER TABLE tool_results ADD COLUMN IF NOT EXISTS purpose text;
-- The model's only application write is an immutable checked draft. Human Send
-- and its deadline/slot remain on evidence_requests; a draft has neither.
CREATE TABLE IF NOT EXISTS evidence_drafts (
  run_id text, draft_id text, case_id text NOT NULL, rec_version text NOT NULL, plan_key text NOT NULL,
  fact_id text NOT NULL, class text NOT NULL, owner text NOT NULL, to_branch text NOT NULL, sku text NOT NULL,
  lot_ids jsonb NOT NULL, original_text text NOT NULL, refs jsonb NOT NULL, checks jsonb NOT NULL,
  model_id text NOT NULL, assistant_version text NOT NULL, request_context jsonb NOT NULL,
  state text NOT NULL DEFAULT 'DRAFT', request_id text, created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (run_id, draft_id), UNIQUE (run_id, case_id, rec_version, plan_key, fact_id));

-- 06.1 (D-25, D-47, D-48, D-49, D-65, D-67): every additive statement of the 06.1 contract, written once
-- (04-API-CONTRACT "Product mode (06.1)", Tables and error codes), so the 06.1 API plans after 06.1-04 never share
-- this file. Additive only; every new per-run table has run_id first, so a workspace clone copies it.
--
-- 06.1 D-27: the case page's three-way comparison (Recommended, Simple rule, Keep; api/simplerule.py), cached beside
-- the search under the same key. Null on a row stored before 06.1, until the next write computes it.
ALTER TABLE engine_cache ADD COLUMN IF NOT EXISTS simple_rule jsonb;
-- 06.1 D-27: the version's comparison, copied from the cache row; the case detail reads it as `comparison`.
ALTER TABLE recommendations ADD COLUMN IF NOT EXISTS simple_rule jsonb;
-- 06.1 D-47: the signed-in account (a store code or HQ); null = signed out, the workspace (run_id) kept.
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS account text;
-- 06.1 D-25, D-67: Take ownership (the account and when).
ALTER TABLE cases ADD COLUMN IF NOT EXISTS owner_taken_by text;
ALTER TABLE cases ADD COLUMN IF NOT EXISTS owner_taken_at timestamptz;
-- 06.1 D-48 with 07-11's names and types (07-11-PLAN.md step 2): Close (Archive).
ALTER TABLE cases ADD COLUMN IF NOT EXISTS closed_as text;
ALTER TABLE cases ADD COLUMN IF NOT EXISTS closed_at timestamptz;
ALTER TABLE cases ADD COLUMN IF NOT EXISTS closed_by text;
-- Cancel approved plan (07-CONTEXT D-23, D-25): a cancelled case is state CLOSED with closed_as CANCELLED, closed_at,
-- closed_by and this required explanation; no new state, so the unchanged one-open-case index frees the lot.
ALTER TABLE cases ADD COLUMN IF NOT EXISTS cancel_reason text;
-- Closing never hides at-risk stock (07-CONTEXT D-24, D-28; api/closing.py): every reject, decline and cancel stores
-- the lot's decision-inputs fingerprint (sha256 of the date-invariant inputs) and its status then (AT_RISK, or null
-- when the lot was gone or no longer at risk).
ALTER TABLE cases ADD COLUMN IF NOT EXISTS fingerprint text;
ALTER TABLE cases ADD COLUMN IF NOT EXISTS lot_status text;
-- Reassess and the linked successor (07-CONTEXT D-24): the closed case a successor follows (one active case per lot
-- stays the index's rule; every case but a lot's first names the case it follows).
ALTER TABLE cases ADD COLUMN IF NOT EXISTS predecessor text;
CREATE INDEX IF NOT EXISTS cases_by_predecessor ON cases (run_id, predecessor);
-- What was already considered (07-CONTEXT D-28): {day, days_left, band, step_pct, clearance, transfer_open, actions}
-- stored at the close and merged on each synthetic Next-day morning the lot qualifies, so one crossing opens one case.
ALTER TABLE cases ADD COLUMN IF NOT EXISTS considered jsonb;
-- 06.1 D-48: recorded dispatch, receipt and price-change movements (quantities, lot, condition, who, when).
-- Quantities are float8, as the header asks of every quantity (04-RESEARCH Pattern 5).
CREATE TABLE IF NOT EXISTS movements (run_id text, movement_id text, case_id text NOT NULL, action_id text NOT NULL, kind text NOT NULL, lot_id text, qty float8 NOT NULL, damaged_qty float8 NOT NULL DEFAULT 0, condition text, reason text, recorded_by text NOT NULL, role text NOT NULL, branch text, recorded_at timestamptz NOT NULL DEFAULT now(), scenario_day int NOT NULL, PRIMARY KEY (run_id, movement_id));
-- 06.1 D-65: a short or damaged receipt's discrepancy, open until reconciled.
CREATE TABLE IF NOT EXISTS discrepancies (run_id text, discrepancy_id text, case_id text NOT NULL, action_id text NOT NULL, kind text NOT NULL, units float8 NOT NULL, open_units float8 NOT NULL, state text NOT NULL, opened_at timestamptz NOT NULL DEFAULT now(), resolution text, resolved_by text, resolved_at timestamptz, reason text, PRIMARY KEY (run_id, discrepancy_id));
-- 06.1 D-49 with Phase 7's DDL, character for character (07-08-PLAN.md step 2): lot holds the dispatch check reads.
CREATE TABLE IF NOT EXISTS lot_holds (run_id text, hold_id text, lot_id text NOT NULL, branch text NOT NULL, kind text NOT NULL, reason text NOT NULL, placed_by text NOT NULL, placed_at timestamptz NOT NULL DEFAULT now(), signal_id text, request_id text, state text NOT NULL, resolved_by text, resolved_at timestamptz, resolution text, note text, PRIMARY KEY (run_id, hold_id));
CREATE UNIQUE INDEX IF NOT EXISTS one_active_hold_per_lot_kind ON lot_holds (run_id, lot_id, kind) WHERE state = 'ACTIVE';
-- 06.1 D-49 with Phase 7's DDL, character for character (07-06-PLAN.md step 2): review flags the dispatch check reads.
CREATE TABLE IF NOT EXISTS review_flags (run_id text, flag_id text, case_id text NOT NULL, fact_id text NOT NULL, reason text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), cleared_at timestamptz, cleared_by text, note text, PRIMARY KEY (run_id, flag_id));
CREATE UNIQUE INDEX IF NOT EXISTS one_flag_per_case_fact ON review_flags (run_id, case_id, fact_id);

-- Phase 8 (08-CONTEXT D-02, D-35, 04-API-CONTRACT 'Reports (08)'): shared, no run_id, written once
CREATE TABLE IF NOT EXISTS benchmark_runs (
  benchmark_id text PRIMARY KEY, status text NOT NULL, trial boolean NOT NULL DEFAULT false,
  sim_run_id text REFERENCES demo_runs(run_id), dataset_version text NOT NULL, engine_version text NOT NULL,
  policy_version text NOT NULL, forecast_runs jsonb NOT NULL, eval_version text NOT NULL,
  settings_digest text NOT NULL, settings jsonb NOT NULL, image_digest text, git_commit text NOT NULL,
  first_day int NOT NULL, last_day int NOT NULL, last_day_done int, started_at timestamptz NOT NULL DEFAULT now(),
  published_at timestamptz, failure text, stats jsonb NOT NULL DEFAULT '{}', expected_domain jsonb,
  validation_receipt jsonb);
CREATE TABLE IF NOT EXISTS benchmark_days (
  benchmark_id text REFERENCES benchmark_runs(benchmark_id), day int, completed_at timestamptz NOT NULL,
  elapsed_s float8 NOT NULL, stats jsonb NOT NULL, PRIMARY KEY (benchmark_id, day));
CREATE TABLE IF NOT EXISTS benchmark_observations (
  benchmark_id text REFERENCES benchmark_runs(benchmark_id), day int, seq int, lot_id text, branch text,
  case_id text, rec_version text, request_id text, results jsonb NOT NULL, PRIMARY KEY (benchmark_id, day, seq));
CREATE TABLE IF NOT EXISTS benchmark_branch_day (
  benchmark_id text REFERENCES benchmark_runs(benchmark_id), scenario text, day int, branch text,
  revenue float8 NOT NULL, transport float8 NOT NULL, handling float8 NOT NULL, disposal float8 NOT NULL,
  net float8 NOT NULL, sold float8 NOT NULL, wasted float8 NOT NULL, wasted_kg float8 NOT NULL,
  lost float8 NOT NULL, demand float8 NOT NULL, moved_out float8 NOT NULL, moved_in float8 NOT NULL,
  PRIMARY KEY (benchmark_id, scenario, day, branch));
CREATE TABLE IF NOT EXISTS benchmark_summary (
  benchmark_id text PRIMARY KEY REFERENCES benchmark_runs(benchmark_id), payload jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS benchmark_transfer_audit (
  sim_run_id text REFERENCES demo_runs(run_id) ON DELETE CASCADE,
  benchmark_id text REFERENCES benchmark_runs(benchmark_id), world text, day int NOT NULL, action_id text,
  rec_version text, run_code text, planned_units float8 NOT NULL, shipped_units float8 NOT NULL,
  destination text, effective_input jsonb NOT NULL, constraint_reasons jsonb NOT NULL, charge_id text,
  transport_amount float8 NOT NULL, handling_amount float8 NOT NULL,
  PRIMARY KEY (sim_run_id, world, action_id), UNIQUE (sim_run_id, world, charge_id));
CREATE TABLE IF NOT EXISTS evaluation_check_history (
  sim_run_id text REFERENCES demo_runs(run_id) ON DELETE CASCADE,
  benchmark_id text REFERENCES benchmark_runs(benchmark_id), day int, world text, seq int,
  event_kind text NOT NULL, case_id text, rec_version text, fact_id text, request_id text, signal_id text,
  lot_id text, lot_ids jsonb NOT NULL, observed_counts jsonb NOT NULL, outcome jsonb NOT NULL,
  PRIMARY KEY (sim_run_id, day, world, seq));
CREATE UNIQUE INDEX IF NOT EXISTS evaluation_one_observation_per_lot_day
  ON evaluation_check_history (sim_run_id, day, world, lot_id) WHERE event_kind = 'observation';
-- Scored AI eval (10-CONTEXT D-15, D-16, D-17, D-21): global: no run_id column, Pitfall 9.
-- Workspace clones may be pruned; review, scores and the immutable publication survive them.
CREATE TABLE IF NOT EXISTS eval_runs (
  eval_id text PRIMARY KEY, fingerprint text NOT NULL, model_id text, build_sha text, assistant_version text,
  engine_version text, dataset_version text, template_ref text, started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz, status text NOT NULL CHECK (status IN ('RUNNING', 'COMPLETE', 'CAPPED', 'FAILED', 'DRY')),
  usage jsonb NOT NULL DEFAULT '{}', caps jsonb NOT NULL DEFAULT '{}',
  publish_state text NOT NULL DEFAULT 'PRIVATE' CHECK (publish_state IN ('PRIVATE', 'PUBLISHED', 'BLOCKED')),
  block_reason text, reviewed_by text, reviewed_at timestamptz);
-- The clone reference is deliberately workspace_run_id, without a foreign key to disposable workspaces.
CREATE TABLE IF NOT EXISTS eval_attempts (
  eval_id text REFERENCES eval_runs(eval_id), scenario_id int, run_no int CHECK (run_no BETWEEN 1 AND 3),
  workspace_run_id text, valid boolean NOT NULL, invalid_reason text, model_calls int, cache_hits int,
  status text NOT NULL, duration_ms int, evidence_ref text, PRIMARY KEY (eval_id, scenario_id, run_no));
-- details retains private checklist wording/reference/actual evidence, never dumped into the public view (D-17).
CREATE TABLE IF NOT EXISTS eval_items (
  eval_id text, scenario_id int, run_no int, item_id text, grp text NOT NULL CHECK (grp IN ('safety', 'assistant', 'reading')),
  gate text CHECK (gate IN ('unsourced', 'self_approval', 'hidden_truth')), facet text NOT NULL,
  scorer text NOT NULL CHECK (scorer IN ('script', 'human')),
  result text NOT NULL CHECK (result IN ('PASS', 'FAIL', 'NA', 'NOT_SCORED')), note text,
  reviewed_by text, reviewed_at timestamptz, details jsonb NOT NULL DEFAULT '{}',
  PRIMARY KEY (eval_id, scenario_id, run_no, item_id),
  FOREIGN KEY (eval_id, scenario_id, run_no) REFERENCES eval_attempts(eval_id, scenario_id, run_no));
-- A publication is an immutable summary snapshot, written only after every gate passes under the eval row lock.
CREATE TABLE IF NOT EXISTS eval_publications (
  eval_id text PRIMARY KEY REFERENCES eval_runs(eval_id), published_at timestamptz NOT NULL DEFAULT now(),
  summary jsonb NOT NULL);
