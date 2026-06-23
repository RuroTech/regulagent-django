import pytest
from django.conf import settings


# Ensure Django settings are configured
def pytest_configure(config):
    settings.DEBUG = False


def pytest_sessionstart(session):
    """
    Force ``TRUNCATE ... CASCADE`` for the PostgreSQL test-DB flush.

    django-tenants creates per-tenant schemas whose tables hold FK references
    into public-schema tables. When pytest-django flushes the test DB between
    tests (TransactionTestCase / ``django_db(transaction=True)`` semantics), a
    plain ``TRUNCATE`` without CASCADE raises FeatureNotSupported on those
    referenced tables. This is also reached by async-ORM code that commits via
    ``run_in_executor`` (e.g. FilingSyncer._resolve_well), whose writes escape
    the per-test savepoint and must be truncated at teardown.

    The patch only affects test-DB teardown (never production) and is strictly
    more permissive, so it cannot change test assertions.
    """
    try:
        from django.db.backends.postgresql.operations import DatabaseOperations

        _orig_sql_flush = DatabaseOperations.sql_flush

        def _sql_flush_cascade(self, style, tables, *, reset_sequences=False, allow_cascade=False):
            return _orig_sql_flush(
                self, style, tables,
                reset_sequences=reset_sequences,
                allow_cascade=True,
            )

        DatabaseOperations.sql_flush = _sql_flush_cascade
    except Exception:
        pass  # Non-PostgreSQL backend or import issue — skip the patch.


@pytest.fixture(scope="session")
def django_db_setup(django_db_blocker):
    """
    Custom DB setup for django-tenants + pgvector.

    Strategy:
    1. Connect to the default (non-test) DB and drop/create the test DB manually
    2. Install the pgvector extension on the fresh test DB
    3. Run django-tenants migrate_schemas for the public schema
    """
    import django
    from django.conf import settings as dj_settings
    from django.db import connections, connection as default_connection
    from django.core.management import call_command
    import psycopg2

    test_db_name = dj_settings.DATABASES["default"].get("TEST", {}).get(
        "NAME", "test_" + dj_settings.DATABASES["default"]["NAME"]
    )
    db_cfg = dj_settings.DATABASES["default"]

    with django_db_blocker.unblock():
        # Step 1: Drop and recreate the test DB using a direct psycopg2 connection
        # (must connect to a different DB to drop the target)
        admin_conn = psycopg2.connect(
            host=db_cfg.get("HOST", "localhost"),
            port=db_cfg.get("PORT", 5432),
            user=db_cfg.get("USER", "postgres"),
            password=db_cfg.get("PASSWORD", ""),
            dbname="postgres",
        )
        admin_conn.autocommit = True
        with admin_conn.cursor() as cur:
            cur.execute(
                f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
                [test_db_name],
            )
            cur.execute(f"DROP DATABASE IF EXISTS {test_db_name}")
            cur.execute(f"CREATE DATABASE {test_db_name}")
        admin_conn.close()

        # Step 2: Install pgvector BEFORE Django runs any migrations
        ext_conn = psycopg2.connect(
            host=db_cfg.get("HOST", "localhost"),
            port=db_cfg.get("PORT", 5432),
            user=db_cfg.get("USER", "postgres"),
            password=db_cfg.get("PASSWORD", ""),
            dbname=test_db_name,
        )
        ext_conn.autocommit = True
        with ext_conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        ext_conn.close()

        # Step 3: Point Django's default connection at the test DB and run migrations
        from django.test.utils import setup_databases
        # Override DB name for the test connection
        connections["default"].settings_dict["NAME"] = test_db_name
        connections.close_all()

        call_command("migrate_schemas", schema_name="public", verbosity=0)

    yield

    with django_db_blocker.unblock():
        connections.close_all()
        # Drop test DB after session
        admin_conn = psycopg2.connect(
            host=db_cfg.get("HOST", "localhost"),
            port=db_cfg.get("PORT", 5432),
            user=db_cfg.get("USER", "postgres"),
            password=db_cfg.get("PASSWORD", ""),
            dbname="postgres",
        )
        admin_conn.autocommit = True
        with admin_conn.cursor() as cur:
            cur.execute(
                f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
                [test_db_name],
            )
            cur.execute(f"DROP DATABASE IF EXISTS {test_db_name}")
        admin_conn.close()


@pytest.fixture
def api_client():
    """Return DRF APIClient for testing."""
    from rest_framework.test import APIClient
    return APIClient()


@pytest.fixture
def authenticated_client(api_client, test_user):
    """Return authenticated APIClient."""
    from rest_framework_simplejwt.tokens import RefreshToken
    refresh = RefreshToken.for_user(test_user)
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")
    return api_client
