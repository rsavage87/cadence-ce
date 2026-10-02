"""
Pull device recall records from openFDA and match them to every tenant's inventory.

    python manage.py import_openfda --days 30

openFDA is free and needs no key for light use: https://open.fda.gov/apis/device/recall/
The fetch and the upsert are apps.recalls.feeds, shared with the Recalls screen's Check FDA feed (which matches only the
signed-in user's facility); this command matches every active facility. A failed fetch ends it with an error and no matching.
ECRI alerts require an ECRI membership and API agreement; add a second importer for them.
"""
import requests  # noqa: F401  (the fetch is in apps.recalls.feeds; tests patch requests.get through this module's name)
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.recalls.feeds import IMPORT_TIMEOUT, MAX_LIMIT, FeedError, import_recalls, parse_date
from apps.recalls.services import match_all_open_alerts
from apps.tenants.context import tenant_context
from apps.tenants.models import Tenant

_parse = parse_date  # the importer's date parsing, by its old name


class Command(BaseCommand):
    help = "Import recent openFDA device recalls and match them to inventory."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=30, help="Recalls FDA posted in the last N days (default 30)")
        parser.add_argument("--manufacturer", help="Limit to one manufacturer name (openFDA text search)")
        parser.add_argument("--limit", type=int, default=MAX_LIMIT, help="Records to fetch; openFDA's maximum per request is 1000")

    def handle(self, *args, **opts):
        try:
            result = import_recalls(days=opts["days"], limit=opts["limit"], manufacturer=opts["manufacturer"], timeout=IMPORT_TIMEOUT)
        except FeedError as e:
            raise CommandError(f"{e}; nothing imported") from e
        if result.total == 0 and not result.returned:  # openFDA's 404, its "no results"
            self.stdout.write("No recalls in that window.")
            return
        if result.truncated:
            # openFDA returns at most --limit records (1000 per request); say so instead of silently dropping the rest.
            self.stderr.write(f"openFDA reports {result.total} recalls in the window but returned {result.returned}; raise --limit or shorten --days.")
        self.stdout.write(f"Imported {result.new} new alerts")
        failed = []
        for tenant in Tenant.objects.filter(is_active=True):
            try:
                with transaction.atomic(), tenant_context(tenant):
                    n = match_all_open_alerts()
            except Exception as e:  # one hospital's bad data must not keep the others from their matches
                failed.append(tenant.slug)
                self.stderr.write(f"{tenant.slug}: matching failed: {e!r}")
                continue
            self.stdout.write(f"{tenant.slug}: {n} new matches")
        if failed:
            raise CommandError(f"Recall matching failed for {', '.join(failed)}")
