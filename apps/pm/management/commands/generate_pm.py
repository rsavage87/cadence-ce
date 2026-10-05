"""
Nightly job: create PM work orders that are due within the lead window, for every active facility, each as of its own today (its
time zone, Tenant.timezone: the lead window starts at the facility's today). The daily job (apps.jobs: generate_pm) runs it for one
facility and that facility's day.

    python manage.py generate_pm                                    # every facility, each as of its today
    python manage.py generate_pm --tenant riverside --lead-days 30
    python manage.py generate_pm --tenant riverside --date 2026-10-05   # a missed day, as of that day
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.jobs import cli
from apps.pm.services import generate_pm_work_orders
from apps.tenants.context import tenant_context


class Command(BaseCommand):
    help = "Generate preventive-maintenance work orders that are coming due."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", help="Facility slug; default every active facility")
        parser.add_argument("--lead-days", type=int, default=None)
        parser.add_argument("--date", help="YYYY-MM-DD: generate as of that day, for a missed run; default each facility's today (its time zone)")

    def handle(self, *args, **opts):
        tenants = cli.facilities(opts["tenant"])
        day = cli.parse_day(opts["date"]) if opts["date"] else None
        if day is not None:
            cli.refuse_future(day, tenants)
        failed = []
        for tenant in tenants:
            try:
                with transaction.atomic(), tenant_context(tenant):
                    # Inside the facility's context timezone.localdate() is its today: the lead window starts there.
                    n = generate_pm_work_orders(as_of=day or timezone.localdate(), lead_days=opts["lead_days"])
            except Exception as e:  # one hospital's bad data must not stop the others' PMs; the run still fails at the end
                failed.append(tenant.slug)
                self.stderr.write(f"{tenant.slug}: failed: {e!r}")
                continue
            self.stdout.write(f"{tenant.slug}: {n} PM work orders created")
        if failed:
            raise CommandError(f"PM generation failed for {', '.join(failed)}")
