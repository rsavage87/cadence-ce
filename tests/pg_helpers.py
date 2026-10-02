"""Helpers for tests that run under PostgreSQL's row-level security (tests/test_postgres_rls.py and each slice's own checks).

Import them as `from pg_helpers import ...` (tests/ is on the path: it has no __init__.py). Mark such tests with `needs_postgres`:
they are skipped unless CADENCE_TEST_DATABASE_URL points the suite at PostgreSQL."""
import os

import pytest
from django.db import connection

APP_ROLE = "cadence_app"  # tests/conftest.py grants it the app's privileges on the test database

# Skipped only when no PostgreSQL was asked for; asked for and not there (a misconfigured CI job) fails test_the_database_is_postgres.
needs_postgres = pytest.mark.skipif(not os.environ.get("CADENCE_TEST_DATABASE_URL"), reason="row-level security needs PostgreSQL (CADENCE_TEST_DATABASE_URL)")


def as_app_role():
    """From here to the end of the test's transaction, act as the runtime role: the policies apply."""
    with connection.cursor() as cur:
        cur.execute(f"SET LOCAL ROLE {APP_ROLE}")
        cur.execute("SELECT current_user, (SELECT rolbypassrls OR rolsuper FROM pg_roles WHERE rolname = current_user)")
        user, bypass = cur.fetchone()
    assert user == APP_ROLE and not bypass
