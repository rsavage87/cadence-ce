"""
Nightly job: create PM work orders that are due within the lead window, for every active tenant.

    python manage.py generate_pm            # all tenants
    python manage.py generate_pm --tenant riverside --lead-days 30
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.pm.services import generate_pm_work_orders
from apps.tenants.context import tenant_context
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Generate preventive-maintenance work orders that are coming due."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", help="Tenant slug; default is every active tenant")
        parser.add_argument("--lead-days", type=int, default=None)

    def handle(self, *args, **opts):
        tenants = Tenant.objects.filter(is_active=True)
        if opts["tenant"]:
            tenants = tenants.filter(slug=opts["tenant"])
        failed = []
        for tenant in tenants:
            try:
                with transaction.atomic(), tenant_context(tenant):
                    n = generate_pm_work_orders(lead_days=opts["lead_days"])
            except Exception as e:  # one hospital's bad data must not stop the others' PMs; the run still fails at the end
                failed.append(tenant.slug)
                self.stderr.write(f"{tenant.slug}: failed: {e!r}")
                continue
            self.stdout.write(f"{tenant.slug}: {n} PM work orders created")
        if failed:
            raise CommandError(f"PM generation failed for {', '.join(failed)}")
