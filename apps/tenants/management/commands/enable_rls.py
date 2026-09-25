"""
Enable Postgres row-level security on every tenant-scoped table.

Idempotent. Run after `migrate`, as the table owner:
    python manage.py enable_rls --database=migrate

Policies read the session setting `app.tenant_id`, which TenantMiddleware sets per request.
The runtime role (DATABASE_URL) must not be the table owner or a superuser, or RLS is bypassed.
"""
from django.apps import apps
from django.core.management.base import BaseCommand
from django.db import connections

from apps.core.models import TenantModel

POLICY_SQL = """
ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON {table};
CREATE POLICY tenant_isolation ON {table}
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
"""


def tenant_scoped_tables():
    tables = []
    for model in apps.get_models():
        if issubclass(model, TenantModel) and not model._meta.abstract and not model._meta.proxy:
            tables.append(model._meta.db_table)
        # django-simple-history tables carry tenant_id too
        if model.__name__.startswith("Historical") and any(f.name == "tenant" for f in model._meta.fields):
            tables.append(model._meta.db_table)
    return sorted(set(tables))


class Command(BaseCommand):
    help = "Enable row-level security policies on tenant-scoped tables (PostgreSQL only)."

    def add_arguments(self, parser):
        parser.add_argument("--database", default="migrate")

    def handle(self, *args, **options):
        conn = connections[options["database"]]
        if conn.vendor != "postgresql":
            self.stdout.write("Skipped: row-level security is only available on PostgreSQL.")
            return
        with conn.cursor() as cur:
            for table in tenant_scoped_tables():
                cur.execute(POLICY_SQL.format(table=conn.ops.quote_name(table)))
                self.stdout.write(f"RLS enabled on {table}")
        self.stdout.write(self.style.SUCCESS("Row-level security is in place."))
