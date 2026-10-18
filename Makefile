PY := .venv/bin/python
# A conda environment puts its own Python first, so the interpreter is named (01-05 flag 4); another machine overrides it with `make setup PYTHON312=<path>`.
PYTHON312 ?= /opt/homebrew/bin/python3.12
# The interpreter rule (review WR-04): a final CPython 3.12 whose prefix is not a conda-family one. Every conda-family
# install (anaconda, miniconda, miniforge, mambaforge, micromamba, pixi) keeps a conda-meta directory in its prefix,
# so that is what is checked, whatever the path is called. In a venv, sys.base_prefix is the base interpreter's.
CHECK_PY := import os, sys; v = sys.version_info; bad = [p for p in {sys.prefix, sys.base_prefix} if os.path.isdir(os.path.join(p, 'conda-meta'))]; sys.exit(None if sys.implementation.name == 'cpython' and v[:2] == (3, 12) and v.releaselevel == 'final' and not bad else 'refused interpreter: needs a final non-conda CPython 3.12, got ' + sys.version.split()[0] + ' at ' + sys.base_prefix)

# The API suite's database: the _test database docker-compose.yml creates (D-11). Override to point elsewhere.
TEST_DB ?= postgresql+pg8000://postgres@localhost:5432/shelfshift_test
# What test-api runs; a later plan narrows it, e.g. `make test-api TESTS=tests/api/test_db.py`.
TESTS ?= tests/api
# The browser suite's own database, so a browser run never drops the schema an API run is using (05-03, D-09).
TEST_WEB_DB ?= postgresql+pg8000://postgres@localhost:5432/shelfshift_web_test
# What test-web runs; narrow it with e.g. `make test-web WEB_TESTS=tests/web/test_gate.py`.
WEB_TESTS ?= tests/web
# The deploy configuration deploy-check reads (06-CONTEXT D-03): the user's filled copy by default. Claude always
# passes ENV_FILE=<a temporary file> instead and never runs the default.
ENV_FILE ?= deploy/.env.local
# compose-check's own Compose project and loopback ports, so it never collides with the Phase 4 database on 5432.
COMPOSE_CHECK_PROJECT ?= shelfshift-compose-check
CHECK_DB_PORT ?= 15432
CHECK_APP_PORT ?= 18088
# Creates the database WEB_DB names on its own server, only when the name ends in _test and only when it is missing
# (it connects to the server's postgres database to ask pg_database); any other name is refused before connecting.
CREATE_WEB_DB := import os, re, sys, sqlalchemy as sa; u = sa.engine.make_url(os.environ['WEB_DB']); n = u.database or ''; re.fullmatch(r'[a-z0-9_]+_test', n) or sys.exit('refused: web-db creates only a database whose name ends in _test, not ' + repr(n)); e = sa.create_engine(u.set(database='postgres'), isolation_level='AUTOCOMMIT'); c = e.connect(); found = c.execute(sa.text('SELECT 1 FROM pg_database WHERE datname = :n'), {'n': n}).first(); found or c.execute(sa.text('CREATE DATABASE ' + n)); print('web-db: ' + n + (' exists' if found else ' created')); c.close(); e.dispose()

.PHONY: setup lock test db-up test-api template serve bench bench-input web-install web-build web-dev web-db test-web \
	web-check journey-deployed compose-check deploy-check

# The interpreter is checked before .venv is touched, so a refused one never replaces a working .venv; the built
# .venv is checked again and removed if it fails, so `make test` never runs on a refused interpreter.
setup:
	$(PYTHON312) -c "$(CHECK_PY)"
	uv venv --clear --python $(PYTHON312) .venv
	$(PY) -c "$(CHECK_PY)" || { rm -rf .venv; exit 1; }
	uv pip sync --python $(PY) requirements.lock

lock:
	uv pip compile requirements.in -o requirements.lock --python-version 3.12

# test re-checks the .venv's interpreter, so a .venv built on a refused one (e.g. before this rule) is refused too.
# The API suite needs Postgres, so it is left out here and runs under test-api; `make test` stays database-free and
# keeps passing with Docker down (Phase 4 research Pitfall 7). The browser suite (tests/web) needs the database, an API
# child and a built web/, so it is left out too and runs under its own target (D-09). The deployed journey
# (tests/deployed) needs a running URL, so it is left out as well and runs under journey-deployed and compose-check.
test:
	$(PY) -c "$(CHECK_PY)"
	$(PY) -m pytest -q tests --ignore=tests/api --ignore=tests/web --ignore=tests/deployed

# Starts the local Postgres 17 (shelfshift and shelfshift_test) and waits until its healthcheck passes (D-11).
# Local only: Docker Desktop must be running; nothing here touches Google Cloud.
db-up:
	docker compose up -d --wait db

# Runs the API tests against the _test database; needs `make db-up` first. TESTS narrows the run.
test-api:
	$(PY) -c "$(CHECK_PY)"
	DATABASE_URL=$(TEST_DB) $(PY) -m pytest -q $(TESTS)

# Builds the demo template into the local database from data/reference (the local stand-in build; the deployed
# build runs as a Cloud Run job the user starts).
template:
	$(PY) -c "$(CHECK_PY)"
	$(PY) -m api.sandbox build --allow-local

# Serves the API locally on port 8080 against DATABASE_URL (the local shelfshift database by default). The app refuses
# to start without DEMO_PASSCODE, and after the product merge also without a sound HQ_PASSCODE (at least 12
# characters, not DEMO_PASSCODE): set them in the environment or in the gitignored .env, which is read here if present
# (never write a passcode in this file or the README); proxy headers stay off, so the counted address is the TCP peer
# (06-CONTEXT D-24).
serve:
	$(PY) -c "$(CHECK_PY)"
	set -a; [ ! -f .env ] || . ./.env; set +a; $(PY) -m uvicorn api.app:app --port 8080 --no-proxy-headers

# Times the case budget (3 s) and reset target (2 s) on this machine; the deployed benchmark is a job the user runs.
bench:
	$(PY) -c "$(CHECK_PY)"
	$(PY) -m api.bench

# Writes api/bench_input.json (the early bench's frozen input, 06-CONTEXT D-15) from SUS-19 on the ACTIVE template of
# the database DATABASE_URL names. Run it after a template build from the committed reference data, for example on the
# test database after `make test-api TESTS=tests/api/test_bench.py`. The file is engine input made by code, not a hand
# fixture, so re-exporting it after an engine or data change is allowed; tests/api/test_bench.py says when it is stale.
bench-input:
	$(PY) -c "$(CHECK_PY)"
	$(PY) -m api.bench --export api/bench_input.json

# Installs web/'s packages exactly as web/package-lock.json pins them (the lock the user approved; npm ci never
# rewrites it).
web-install:
	npm --prefix web ci --no-audit --no-fund

# Builds the production web/dist (tsc, then vite build) from a clean install; this is the bundle Phase 6 serves (D-18).
web-build: web-install
	npm --prefix web run build

# Serves web/ with hot reload on :5173 and forwards /api to `make serve` on :8080 (SS_API_ORIGIN overrides the target).
web-dev:
	npm --prefix web run dev

# Creates the browser suite's _test database on the running Postgres (`make -C ShelfShift-api db-up` starts it) when
# it is missing; the tests themselves rebuild its schema.
web-db:
	$(PY) -c "$(CHECK_PY)"
	WEB_DB=$(TEST_WEB_DB) $(PY) -c "$(CREATE_WEB_DB)"

# Runs the browser suite in its own pytest invocation (never together with test-api, so each run drops one schema
# once): the web test database, a production build of web/, then a real API child and vite preview on 127.0.0.1:18080
# and :14173 driven by headless Chromium. Never run two test-web runs at once. WEB_TESTS narrows the run.
test-web: web-db
	$(PY) -c "$(CHECK_PY)"
	test -d web/node_modules || npm --prefix web ci --no-audit --no-fund
	npm --prefix web run build
	DATABASE_URL=$(TEST_WEB_DB) $(PY) -m pytest -q -m "web and not live" $(WEB_TESTS)

# The quick check after every change to web/: tsc, the node unit tests and the honesty scan of web/src. It needs no
# database, no API and no browser, so it passes with Docker stopped.
web-check:
	$(PY) -c "$(CHECK_PY)"
	npm --prefix web run typecheck
	npm --prefix web test
	$(PY) -m pytest -q -m web tests/web/test_honesty_web.py

# The judge's path (06-CONTEXT D-20) through a browser against a running URL: `make journey-deployed URL=<url>`. It
# never touches a database, starts no server and never resets the service; it opens two sandboxes through the gate
# (the dataset probe and the path). The URL must be https (an http loopback URL only with SS_JOURNEY_LOCAL=1). The two
# credentials come from SMOKE_PASSCODE and SMOKE_HQ_PASSCODE in the environment or from the hidden prompts
# "Store passcode: " and "HQ passcode: ": run it in your own terminal so they stay out of any transcript.
journey-deployed:
	$(PY) -c "$(CHECK_PY)"
	@test -n "$(URL)" || { echo "usage: make journey-deployed URL=<https service URL>" >&2; exit 2; }
	SS_DEPLOYED_URL='$(URL)' $(PY) -m pytest -q -p no:cacheprovider -m deployed tests/deployed

# D-23's acceptance as one target: from an empty database volume, `docker compose up` starts db, then the one-off init
# (migrate, reference data, template), then the app; the smoke and the deployed journey run against 127.0.0.1; then a
# second `up` and the smoke again, because a second up must be safe. No host-side migration or template command and no
# Google account. It runs under its own project name and ports (COMPOSE_CHECK_PROJECT, CHECK_DB_PORT, CHECK_APP_PORT),
# so it never touches port 5432's Phase 4 container, and it removes its project, volume included, on exit. Both
# passcodes (the store one and HQ's, which the product app needs; 06.1 D-47) are generated in memory for the run,
# handed to the smoke and the journey as SMOKE_PASSCODE and SMOKE_HQ_PASSCODE, and never echoed.
compose-check:
	$(PY) -c "$(CHECK_PY)"
	@export COMPOSE_PROJECT_NAME='$(COMPOSE_CHECK_PROJECT)' SS_DB_PORT='$(CHECK_DB_PORT)' SS_APP_PORT='$(CHECK_APP_PORT)'; \
	DEMO_PASSCODE="$$($(PY) -c 'import secrets; print(secrets.token_urlsafe(18))')"; SMOKE_PASSCODE="$$DEMO_PASSCODE"; \
	HQ_PASSCODE="$$($(PY) -c 'import secrets; print(secrets.token_urlsafe(18))')"; SMOKE_HQ_PASSCODE="$$HQ_PASSCODE"; \
	export DEMO_PASSCODE SMOKE_PASSCODE HQ_PASSCODE SMOKE_HQ_PASSCODE; \
	trap 'docker compose down -v >/dev/null 2>&1' EXIT; \
	docker compose down -v >/dev/null 2>&1; \
	docker compose up -d --wait --build && \
	$(PY) -m deploy.smoke "http://127.0.0.1:$$SS_APP_PORT" && \
	SS_DEPLOYED_URL="http://127.0.0.1:$$SS_APP_PORT" SS_JOURNEY_LOCAL=1 \
		$(PY) -m pytest -q -p no:cacheprovider -m deployed tests/deployed && \
	docker compose up -d --wait && \
	$(PY) -m deploy.smoke "http://127.0.0.1:$$SS_APP_PORT" && \
	echo COMPOSE-CHECK-OK

# The push and release gate (06-08; the no-exposure rule, D-07): the DB-free deploy tests in gate mode, then a syntax
# check of every deploy/*.sh. Gate mode loads PROJECT from ENV_FILE through deploy/envfile.py and scans every tracked
# file (deploy/releases/ excluded) for it and for the earlier project's ID; it fails closed, never skips, when the
# file, the key or a real value is missing. The user runs it before every push (deploy/README.md step 9), and
# deploy/release.py runs the same tests before any cloud command. Claude runs it only with ENV_FILE=<temporary file>.
deploy-check:
	$(PY) -c "$(CHECK_PY)"
	SS_DEPLOY_ENV_FILE="$(ENV_FILE)" $(PY) -m pytest -q -p no:cacheprovider tests/deploy
	for f in deploy/*.sh; do bash -n "$$f" || exit 1; done
