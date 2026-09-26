"""The openFDA importer, run against a canned response so no network is touched."""
from io import StringIO

from django.core.management import call_command

from apps.recalls.models import Alert, AlertMatch
from apps.tenants.context import tenant_context

RECORD = {"res_event_number": "97001", "recalling_firm": "BD", "product_description": "Alaris 8015 PCU infusion pump, front case assembly",
          "product_code": "FRN", "root_cause_description": "Software design", "reason_for_recall": "Keypad membrane may lift",
          "action": "Inspect keypad; replace per service bulletin", "event_date_initiated": "20260901"}


class FakeResponse:
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return {"results": [RECORD]}


def test_import_creates_the_alert_and_matches_it_per_tenant(monkeypatch, tenant, pump_model):
    monkeypatch.setattr("apps.recalls.management.commands.import_openfda.requests.get", lambda *a, **kw: FakeResponse())
    out = StringIO()
    call_command("import_openfda", days=30, stdout=out)
    alert = Alert.objects.get(source=Alert.Source.FDA, external_id="97001")
    # The feed carries no recall class and its product codes are not model names; the description drives matching.
    assert alert.classification == "" and alert.model_terms == [] and alert.raw["root_cause_description"] == "Software design"
    assert alert.title == "Keypad membrane may lift" and alert.published_on.isoformat() == "2026-09-01"
    with tenant_context(tenant):
        assert AlertMatch.objects.get(alert=alert).device_model == pump_model
    assert "Imported 1 new alerts" in out.getvalue()
    call_command("import_openfda", days=30, stdout=StringIO())  # idempotent
    assert Alert.objects.filter(external_id="97001").count() == 1
