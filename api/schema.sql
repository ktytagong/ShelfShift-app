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

-- Policy settings written in one sandbox (04-CONTEXT D-09, D-23), as field and JSON value. The seeded Policy is
-- in ref_data, and a clone copies these rows like any per-run table.
CREATE TABLE IF NOT EXISTS run_policy (
  run_id text, field text, value jsonb NOT NULL,
  PRIMARY KEY (run_id, field));

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
-- Phase 7's DDL, character for character (07-04), so the workspace read runs before Phase 7 merges
ALTER TABLE evidence_requests ADD COLUMN IF NOT EXISTS due_at timestamptz;
