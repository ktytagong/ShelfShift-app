"""The printed schema grants plus migrate()'s build-time grants let the runtime role clone what the deploy role built
(06-RESEARCH Pitfall 5; 06-CONTEXT D-05; OPS-08).

On the local Postgres 17, in a database of its own (never shelfshift_test, so the session template is untouched), with
two LOGIN roles named like Cloud SQL IAM database users. Only postgres runs deploy.provision.grant_sql's statements,
as the user does once through `gcloud sql connect`. The deploy role then migrates (granting the runtime role its table
and sequence privileges), installs the reference data and builds a template; the runtime role must see the same
per-run tables (information_schema hides tables a role has no privilege on, so a missing grant would make the clone
silently skip them), clone every row of each workflow table into a sandbox, and read the template's rows of the world
tables a sandbox reads through its template (overlay.OVERLAID, 05.2 D-08..D-10).

The database and both roles are named per run and dropped afterwards; the settings and caches are put back, so the
rest of the API suite keeps its session template.
"""
from __future__ import annotations

import secrets

import sqlalchemy as sa

from api import config, db, overlay, refdata, sandbox
from deploy import provision
from tests.api.conftest import require_test_database

PROJECT = "x-demo-12"  # synthetic, as in tests.deploy.synthetic_env


def _clear() -> None:
    db.reset_engine()
    sandbox.clear_cache()
    refdata.get.cache_clear()
    refdata.truth.cache_clear()


def _as(url: sa.engine.URL, run_db_user: str = "") -> None:
    config.DATABASE_URL = url.render_as_string(hide_password=False)
    config.RUN_DB_USER = run_db_user
    _clear()


def _counts(conn, tables: list[str], run_id: str) -> dict[str, int]:
    return {t: db.one(conn, f"SELECT count(*) AS n FROM {t} WHERE run_id = :r", r=run_id)["n"] for t in tables}


def test_the_runtime_role_clones_every_per_run_table_the_deploy_role_built():
    assert not config.CLOUD_SQL_INSTANCE, "API tests never run against Cloud SQL"
    server = sa.engine.make_url(config.DATABASE_URL)
    tag = secrets.token_hex(4)
    name = f"shelfshift_grants_{tag}_test"
    url = server.set(database=name)
    require_test_database(url.render_as_string(hide_password=False))
    deploy_sa = f"p6deploy{tag}@{PROJECT}.iam.gserviceaccount.com"
    run_sa = f"p6run{tag}@{PROJECT}.iam.gserviceaccount.com"
    grants = provision.grant_sql({"DEPLOY_SA_EMAIL": deploy_sa, "RUN_SA_EMAIL": run_sa})
    deploy_role, run_role = deploy_sa.removesuffix(".gserviceaccount.com"), run_sa.removesuffix(".gserviceaccount.com")
    passwords = {r: secrets.token_hex(16) for r in (deploy_role, run_role)}  # trust auth ignores them; never printed

    saved = (config.DATABASE_URL, config.RUN_DB_USER)
    admin = sa.create_engine(server.set(database="postgres"), isolation_level="AUTOCOMMIT")
    created: list[str] = []
    try:
        with admin.connect() as c:
            for role, pw in passwords.items():
                c.execute(sa.text(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{pw}'"))
                created.append(role)
            c.execute(sa.text(f"CREATE DATABASE {name}"))
            created.append(name)
        su = sa.create_engine(url, isolation_level="AUTOCOMMIT")  # postgres on the new database, as gcloud connect
        try:
            with su.connect() as c:
                # PostgreSQL 17 still lets PUBLIC use schema public; take that away, so the two printed grants
                # alone must carry both roles.
                c.execute(sa.text("REVOKE ALL ON SCHEMA public FROM PUBLIC"))
                for stmt in grants:
                    c.execute(sa.text(stmt))
        finally:
            su.dispose()

        # The deploy job: migrate (and grant the runtime user), install the reference data, build the template.
        _as(url.set(username=deploy_role, password=passwords[deploy_role]), run_db_user=run_role)
        db.migrate()
        dataset_version = refdata.install(allow_local=True)
        template = sandbox.build_template(dataset_version)
        with db.tx() as conn:
            assert db.one(conn, "SELECT current_user AS u")["u"] == deploy_role
            built = list(sandbox.per_run_tables(conn))
            built_rows = _counts(conn, built, template)
            catalog = [r["t"] for r in db.rows(conn, "SELECT c.relname AS t FROM pg_attribute a "
                                                     "JOIN pg_class c ON c.oid = a.attrelid "
                                                     "JOIN pg_namespace n ON n.oid = c.relnamespace "
                                                     "WHERE n.nspname = 'public' AND c.relkind = 'r' "
                                                     "AND a.attname = 'run_id' AND NOT a.attisdropped ORDER BY 1")]
        assert built == [t for t in catalog if t not in sandbox.NOT_CLONED]
        assert built_rows["cases"] > 0 and sum(built_rows.values()) > len(built)

        # The service: the runtime role sees the same tables and clones every row of each.
        _as(url.set(username=run_role, password=passwords[run_role]))
        session_id = f"grants-{tag}"
        with db.tx() as conn:
            assert db.one(conn, "SELECT current_user AS u")["u"] == run_role
            db.run(conn, "INSERT INTO sessions (session_id, role, branch) VALUES (:s, 'hq', NULL)", s=session_id)
            assert sandbox.per_run_tables(conn) == built
            new, _ = sandbox.reset(conn, session_id)
        with db.tx() as conn:
            assert db.one(conn, "SELECT template_run_id FROM demo_runs WHERE run_id = :r", r=new)["template_run_id"] \
                == template
            # the workflow tables are copied; the world tables (overlay.OVERLAID) are read through the template, so
            # the runtime role must read the template's rows of each (05.2 D-08..D-10)
            assert _counts(conn, built, new) == {t: 0 if t in overlay.OVERLAID else n for t, n in built_rows.items()}
            shared = [t for t in built if t in overlay.OVERLAID]
            assert shared and _counts(conn, shared, template) == {t: built_rows[t] for t in shared}
    finally:
        config.DATABASE_URL, config.RUN_DB_USER = saved
        _clear()
        with admin.connect() as c:
            if name in created:
                c.execute(sa.text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
            for role in passwords:
                if role in created:
                    c.execute(sa.text(f'DROP ROLE IF EXISTS "{role}"'))
            left = c.execute(sa.text("SELECT count(*) FROM pg_roles WHERE rolname = ANY(:r)"),
                             {"r": list(passwords)}).scalar()
            gone = c.execute(sa.text("SELECT count(*) FROM pg_database WHERE datname = :n"), {"n": name}).scalar()
        admin.dispose()
    assert left == 0 and gone == 0
