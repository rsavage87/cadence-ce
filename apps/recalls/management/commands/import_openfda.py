"""
Pull device recall records from openFDA and match them to every tenant's inventory.

    python manage.py import_openfda --days 30

openFDA is free and needs no key for light use: https://open.fda.gov/apis/device/recall/
ECRI alerts require an ECRI membership and API agreement; add a second importer for them.
"""
from datetime import date, timedelta

import requests
from django.core.management.base import BaseCommand

from apps.recalls.models import Alert
from apps.recalls.services import match_all_open_alerts
from apps.tenants.context import tenant_context
from apps.tenants.models import Tenant

ENDPOINT = "https://api.fda.gov/device/recall.json"


class Command(BaseCommand):
    help = "Import recent openFDA device recalls and match them to inventory."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=30)
        parser.add_argument("--manufacturer", help="Limit to one manufacturer name (openFDA text search)")
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **opts):
        since = (date.today() - timedelta(days=opts["days"])).strftime("%Y%m%d")
        search = f"event_date_initiated:[{since}+TO+{date.today():%Y%m%d}]"
        if opts["manufacturer"]:
            search += f'+AND+recalling_firm:"{opts["manufacturer"]}"'
        resp = requests.get(ENDPOINT, params={"search": search, "limit": opts["limit"]}, timeout=30)
        if resp.status_code == 404:  # openFDA returns 404 for "no results"
            self.stdout.write("No recalls in that window.")
            return
        resp.raise_for_status()
        imported = 0
        for rec in resp.json().get("results", []):
            ext = rec.get("res_event_number") or rec.get("product_res_number") or rec.get("cfres_id")
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
                    "published_on": _parse(rec.get("event_date_initiated")),
                    "raw": rec,
                },
            )
            imported += int(created)
        self.stdout.write(f"Imported {imported} new alerts")
        for tenant in Tenant.objects.filter(is_active=True):
            with tenant_context(tenant):
                n = match_all_open_alerts()
            self.stdout.write(f"{tenant.slug}: {n} new matches")


def _parse(s):
    try:
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except (TypeError, ValueError):
        return None
