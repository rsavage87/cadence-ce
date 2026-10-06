"""
Daily job (apps.jobs: expire_imports, slice 23 review): in every active facility, end the import runs left unfinished (two days for a
file whose columns were never chosen, which still holds every column of the export; EXPIRE_DAYS otherwise) and clear their rows, so
a file nobody comes back to is not kept until someone happens to open the Import data screen.

    python manage.py expire_imports

Reads only Tenant before each facility's tenant_context (CLAUDE.md, non-negotiable 2).
"""
from django.core.management.base import BaseCommand

from apps.imports import services
from apps.tenants.context import tenant_context
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "End import runs left unfinished, clearing the rows they hold."

    def handle(self, *args, **opts):
        total = 0
        for tenant in Tenant.objects.filter(is_active=True).order_by("slug"):  # a system table: read before any tenant is set
            with tenant_context(tenant):
                expired = services.expire_stale()
            if expired:
                self.stdout.write(f"{tenant.slug}: {expired} unfinished import{'s' if expired != 1 else ''} expired")
            total += expired
        self.stdout.write(f"Imports expired: {total}")
