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


def test_query_uses_the_posting_date_with_spaces_and_parses_dashed_dates(monkeypatch, tenant, pump_model):
    """The live feed: a literal "+" in the search makes openFDA answer 500, recent recalls are found by posting date, and dates
    come back as YYYY-MM-DD. The posting date is when the alert was received."""
    sent = []
    live = {**RECORD, "event_date_initiated": "2026-08-10", "event_date_posted": "2026-09-11"}

    class Live(FakeResponse):
        def json(self):
            return {"meta": {"results": {"total": 1}}, "results": [live]}

    def fake_get(url, params=None, timeout=None):
        sent.append(params)
        return Live()

    monkeypatch.setattr("apps.recalls.management.commands.import_openfda.requests.get", fake_get)
    call_command("import_openfda", days=30, manufacturer="BD", stdout=StringIO())
    search = sent[0]["search"]
    assert search.startswith("event_date_posted:[") and " TO " in search and "+" not in search and ' AND recalling_firm:"BD"' in search
    assert sent[0]["limit"] == 1000
    assert Alert.objects.get(external_id="97001").published_on.isoformat() == "2026-09-11"


def test_dates_parse_in_both_forms_and_nonsense_is_none():
    from apps.recalls.management.commands.import_openfda import _parse

    assert _parse("2026-09-14").isoformat() == _parse("20260914").isoformat() == "2026-09-14"
    assert [_parse(v) for v in (None, "", "2026-13-01", "2026-9-1", "soon")] == [None] * 5
