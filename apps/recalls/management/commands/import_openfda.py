"""
Pull device recall records from openFDA and match them to every tenant's inventory.

    python manage.py import_openfda --days 30

openFDA is free and needs no key for light use: https://open.fda.gov/apis/device/recall/
ECRI alerts require an ECRI membership and API agreement; add a second importer for them.
"""
from datetime import date, timedelta

import requests
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.recalls.models import Alert
from apps.recalls.services import match_all_open_alerts
from apps.tenants.context import tenant_context
from apps.tenants.models import Tenant

ENDPOINT = "https://api.fda.gov/device/recall.json"


class Command(BaseCommand):
    help = "Import recent openFDA device recalls and match them to inventory."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=30, help="Recalls FDA posted in the last N days (default 30)")
        parser.add_argument("--manufacturer", help="Limit to one manufacturer name (openFDA text search)")
        parser.add_argument("--limit", type=int, default=1000, help="Records to fetch; openFDA's maximum per request is 1000")

    def handle(self, *args, **opts):
        since = (date.today() - timedelta(days=opts["days"])).strftime("%Y%m%d")
        # By posting date, not initiation date: FDA posts a recall weeks after the firm starts it, so a window on the start date
        # finds almost nothing recent (1 record against 273 for the same 30 days when this was checked). Spaces, not "+":
        # requests encodes the spaces, and a literal "+" makes openFDA answer 500.
        search = f"event_date_posted:[{since} TO {date.today():%Y%m%d}]"
        if opts["manufacturer"]:
            search += f' AND recalling_firm:"{opts["manufacturer"]}"'
        resp = requests.get(ENDPOINT, params={"search": search, "limit": opts["limit"]}, timeout=30)
        if resp.status_code == 404:  # openFDA returns 404 for "no results"
            self.stdout.write("No recalls in that window.")
            return
        resp.raise_for_status()
        payload = resp.json()
        results = payload.get("results", [])
        total = (payload.get("meta") or {}).get("results", {}).get("total")
        if isinstance(total, int) and total > len(results):
            # openFDA returns at most --limit records (1000 per request); say so instead of silently dropping the rest.
            self.stderr.write(f"openFDA reports {total} recalls in the window but returned {len(results)}; raise --limit or shorten --days.")
        imported = 0
        for rec in results:
            # One record per recalled product: key on the product's recall number (Z-1234-2026), not the event, which covers
            # several products; keyed by event, every product but the last would be lost, with its device description.
            ext = rec.get("product_res_number") or rec.get("cfres_id") or rec.get("res_event_number")
            if not ext:
                continue
            _, created = Alert.objects.update_or_create(
                source=Alert.Source.FDA, external_id=str(ext),
                defaults={
                    # device/recall.json carries no recall class (the enforcement feed does); the root cause stays in `raw`.
                    "classification": "",
                    "manufacturer": rec.get("recalling_firm", "")[:160],
                    "product": rec.get("product_description", "")[:300],
                    # FDA product codes (e.g. "FRN") are not model names; matching falls back to the product description.
                    "model_terms": [],
                    "title": (rec.get("reason_for_recall") or rec.get("product_description") or "")[:300],
                    "action": rec.get("action", ""),
                    # When FDA made it public, which is when a CE department could have received it; the start date if missing.
                    "published_on": _parse(rec.get("event_date_posted")) or _parse(rec.get("event_date_initiated")),
                    "raw": rec,
                },
            )
            imported += int(created)
        self.stdout.write(f"Imported {imported} new alerts")
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


def _parse(s):
    """openFDA dates: "2026-09-14" in the live feed, "20260914" in older records and in search syntax."""
    digits = str(s or "").replace("-", "")
    try:
        return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8])) if len(digits) == 8 and digits.isdigit() else None
    except ValueError:
        return None
