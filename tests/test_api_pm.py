"""The PM program over the API (slice 19, part B; apps/api/views_pm.py): PM procedures, a device model's procedure, risk score, and
AEM program, the AEM decisions, and Auto-assign week. Each endpoint has the web screen's door (the same levels, checked on every
request), goes through the same service as the screen with the same arguments, and answers with what the screen's toast reports.
Refusals: a 403 in plain words, a service's ValidationError as a 400 keyed by field, another facility's id in the address a 404 and
in a body a 400, unknown and read-only fields in a body a 400, scoped users a 403 everywhere. Every endpoint by session, a write per
endpoint by token, and the same walk as the runtime role under row-level security on PostgreSQL."""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from pg_helpers import as_app_role, needs_postgres
from rest_framework.authtoken.models import Token

from apps.accounts.models import DataScope, Level, Module, Role, User, create_default_roles
from apps.api import views_pm
from apps.credentials.models import Credential, Scope, Technician
from apps.equipment.models import Asset, Department, DeviceModel, RiskClass
from apps.pm import aem
from apps.pm.dates import add_months
from apps.pm.models import AemDecision, AemStatus, PmProcedure
from apps.pm.services import generate_pm_work_orders, week_assignment_preview
from apps.tenants.context import tenant_context
from apps.workorders.models import WorkOrder, WoType

API = "/api/v1/"
PROCS = f"{API}pm-procedures/"
DECISIONS = f"{API}aem-decisions/"
WEEK, ASSIGN_WEEK = f"{API}pm/week/", f"{API}pm/assign-week/"
TODAY = date.today()
ALL_FULL = {m: Level.FULL for m in Module.values}
LIFE = {"function": 10, "physical": 5, "maintenance": 3, "incidents": 0}  # 18: life support
G5_STEPS = ["Inspect", {"text": "Ground resistance", "measure": "Ω, limit 0.3"}, {"text": "Leakage current", "measure": True}]
NO_EFFECT = {"ended": False, "withdrawn": False, "cleared": False, "devices_moved": 0, "rule": ""}


def months_ago(n: int) -> date:
    return add_months(TODAY, -n)


def model_url(dm, part: str) -> str:
    return f"{API}device-models/{dm.pk}/{part}/"


def decision_url(d, act: str = "") -> str:
    return f"{DECISIONS}{d.pk}/" + (f"{act}/" if act else "")


def send(client, method, url, body=None, token=None):
    extra = {"HTTP_AUTHORIZATION": f"Token {token.key}"} if token else {}
    if method == "get":
        return client.get(url, **extra)
    if method == "delete":
        return client.delete(url, **extra)
    return getattr(client, method)(url, body if body is not None else {}, content_type="application/json", **extra)


def post(client, url, body=None, token=None):
    return send(client, "post", url, body, token)


def put(client, url, body=None, token=None):
    return send(client, "put", url, body, token)


def patch(client, url, body=None, token=None):
    return send(client, "patch", url, body, token)


def ok(r, code=200):
    assert r.status_code == code, (r.status_code, r.content)
    return r.json()


def refused(r, words):
    assert r.status_code == 403, (r.status_code, r.content)
    assert r.json()["detail"] == words
    return r


def bad(r, field=None, match=""):
    """A 400; with `field`, keyed by it, and `match` found in its messages."""
    assert r.status_code == 400, (r.status_code, r.content)
    data = r.json()
    if field is not None:
        assert field in data, data
        text = " ".join(data[field]) if isinstance(data[field], list) else data[field]
        assert match in text, (match, text)
    return data


@pytest.fixture
def signed_in(client, make_user):
    n = iter(range(1000))

    def _as(role_slug):
        user = make_user(role_slug, username=f"{role_slug}-{next(n)}@riverside.example")
        client.force_login(user)
        return user

    return _as


@pytest.fixture
def library(ctx, vent_model):
    """Two procedures: the G5's (in use by the ventilator model, a reading of each kind) and a pump's (unused)."""
    g5 = PmProcedure.objects.create(code="HA-G5-PM6", name="G5 6-month PM", estimated_hours=Decimal("1.5"), checklist=G5_STEPS,
                                    revision="Rev K", source_reference="G5 Service Manual, section 7")
    pcu = PmProcedure.objects.create(code="BD-PCU-PM12", name="Alaris PCU 12-month PM", estimated_hours=Decimal("0.5"), checklist=["Inspect"])
    vent_model.pm_procedure = g5
    vent_model.save()
    return {"g5": g5, "pcu": pcu}


@pytest.fixture
def monitor(ctx, dept):
    """High risk, OEM 12 months, a device installed four years ago (the policy's history) last serviced eight months ago."""
    dm = DeviceModel.objects.create(manufacturer="Philips", model="IntelliVue MX750", description="Patient monitor", category="Patient monitoring",
                                    risk_class=RiskClass.HIGH, oem_pm_interval_months=12)
    Asset.objects.create(tag="M-1", device_model=dm, department=dept, installed_on=add_months(TODAY, -48), last_pm_on=months_ago(8),
                         next_pm_on=add_months(months_ago(8), 12))
    return dm


@pytest.fixture
def theirs(tenant, other_tenant):
    """Another facility's model, procedure, and open AEM proposal."""
    create_default_roles(other_tenant)
    with tenant_context(other_tenant):
        d = Department.objects.create(name="Their ICU")
        procedure = PmProcedure.objects.create(code="THEIRS-PM1", name="Theirs", estimated_hours=1, checklist=["Inspect"])
        dm = DeviceModel.objects.create(manufacturer="GE", model="B650", description="Monitor", category="Monitors", pm_procedure=procedure)
        Asset.objects.create(tag="THEIRS-1", device_model=dm, department=d, installed_on=add_months(TODAY, -60), next_pm_on=TODAY)
        decision = AemDecision.objects.create(device_model=dm, interval_months=18, oem_interval_months=12, rationale="x", proposed_on=TODAY)
    return {"model": dm, "procedure": procedure, "decision": decision}


def propose(dm, by, months=24):
    return aem.propose(dm, interval_months=months, rationale="Few failures in three years.", by=by)


# --- PM procedures -----------------------------------------------------------------------------------------------------------


def test_the_library_lists_this_facilitys_procedures_with_their_lines_models_and_open_pms(client, signed_in, library, theirs, vent, vent_model):
    assert generate_pm_work_orders(as_of=TODAY, lead_days=30) == 1  # the ventilator's PM, due in ten days
    signed_in("analyst")  # PM View
    data = ok(client.get(PROCS))
    assert [p["code"] for p in data["results"]] == ["BD-PCU-PM12", "HA-G5-PM6"] and data["count"] == 2
    g5 = data["results"][1]
    assert g5["checklist"] == [{"text": "Inspect", "measure": None}, {"text": "Ground resistance", "measure": "Ω, limit 0.3"},
                               {"text": "Leakage current", "measure": True}]
    assert g5["checklist_text"] == "Inspect\nGround resistance | Ω, limit 0.3\nLeakage current |"
    assert g5["models"] == [{"id": str(vent_model.pk), "manufacturer": "Hamilton Medical", "model": "Hamilton-G5", "description": "ICU ventilator"}]
    assert (g5["estimated_hours"], g5["open_pm_work_orders"], g5["revision"], g5["source_kind"]) == ("1.50", 1, "Rev K", "oem")
    assert data["results"][0]["models"] == [] and data["results"][0]["open_pm_work_orders"] == 0
    assert ok(client.get(f"{PROCS}{g5['id']}/")) == g5
    assert [p["code"] for p in ok(client.get(f"{PROCS}?search=alaris"))["results"]] == ["BD-PCU-PM12"]
    assert client.get(f"{PROCS}{theirs['procedure'].pk}/").status_code == 404
    assert client.get(f"{PROCS}not-an-id/").status_code == 404


def test_a_procedure_is_added_through_the_service_and_made_the_models(client, signed_in, monitor):
    user = signed_in("technician")  # PM Edit
    body = {"code": "PH-MX750-PM12", "name": "Monitor 12-month PM", "source_kind": "oem", "estimated_hours": "0.75",
            "checklist": "Inspect cables\nNIBP accuracy | mmHg, ±3\nAlarm test |", "device_model": str(monitor.pk)}
    data = ok(post(client, PROCS, body), 201)
    p = PmProcedure.objects.get(code="PH-MX750-PM12")
    assert p.checklist == ["Inspect cables", {"text": "NIBP accuracy", "measure": "mmHg, ±3"}, {"text": "Alarm test", "measure": True}]
    assert p.estimated_hours == Decimal("0.75") and p.history.first().history_change_reason == "Added" and p.history.first().history_user == user
    monitor.refresh_from_db()
    assert monitor.pm_procedure == p and monitor.history.first().history_change_reason == "PM procedure: PH-MX750-PM12"
    assert data["id"] == str(p.pk) and [m["id"] for m in data["models"]] == [str(monitor.pk)] and data["estimated_hours"] == "0.75"
    # Without a model it joins the library only.
    ok(post(client, PROCS, {"code": "GEN-1", "name": "Generic", "source_kind": "in_house", "estimated_hours": 1, "checklist": ["Inspect"]}), 201)
    assert PmProcedure.objects.get(code="GEN-1").device_models.count() == 0


def test_the_api_saves_what_the_web_form_saves(client, signed_in, pump_model, monitor):
    signed_in("technician")
    form = {"name": "Pump 12-month PM", "source_kind": "ecri", "estimated_hours": "1.25", "source_reference": "  ECRI 438-0595  ",
            "source_url": "https://example.com/ipm.pdf", "revision": "Rev 3", "checklist": "Visual  inspection\n\nLeakage | µA,  limit 100\n"}
    web = client.post(f"/pm/procedures/new/?model={pump_model.pk}", {**form, "code": "WEB-1"}, HTTP_HX_REQUEST="true")
    assert web.status_code == 200 and web["HX-Retarget"] == "#drawer"
    ok(post(client, PROCS, {**form, "code": "API-1", "device_model": str(monitor.pk)}), 201)
    a, b = PmProcedure.objects.get(code="WEB-1"), PmProcedure.objects.get(code="API-1")
    for f in ("name", "source_kind", "source_reference", "source_url", "revision", "estimated_hours", "checklist"):
        assert getattr(a, f) == getattr(b, f), f
    pump_model.refresh_from_db()
    monitor.refresh_from_db()
    assert pump_model.pm_procedure == a and monitor.pm_procedure == b


def test_adding_a_procedure_needs_pm_edit(client, signed_in, library, monitor):
    body = {"code": "X-1", "name": "X", "source_kind": "oem", "estimated_hours": 1, "checklist": ["Inspect"]}
    signed_in("analyst")
    refused(post(client, PROCS, body), views_pm.PROCEDURE_REFUSAL)
    refused(patch(client, f"{PROCS}{library['g5'].pk}/", {"name": "x"}), views_pm.PROCEDURE_REFUSAL)
    refused(put(client, f"{PROCS}{library['g5'].pk}/", body), views_pm.PROCEDURE_REFUSAL)
    assert PmProcedure.objects.count() == 2 and PmProcedure.objects.get(code="HA-G5-PM6").name == "G5 6-month PM"


def test_the_procedure_rules_are_the_services(client, signed_in, library, monitor, theirs):
    signed_in("technician")
    base = {"code": "NEW-1", "name": "New", "source_kind": "oem", "estimated_hours": "1", "checklist": ["Inspect"]}
    bad(post(client, PROCS, {**base, "code": "ha-g5-pm6"}), "code", "ha-g5-pm6 is already the code of another procedure here.")
    bad(post(client, PROCS, {**base, "code": "HAS SPACE"}), "code", "A code has no spaces.")
    bad(post(client, PROCS, {**base, "estimated_hours": "0.05"}), "estimated_hours", "Estimated hours are 0.1 to 40.")
    bad(post(client, PROCS, {**base, "estimated_hours": 40.5}), "estimated_hours", "Estimated hours are 0.1 to 40.")
    bad(post(client, PROCS, {**base, "estimated_hours": "1.234"}), "estimated_hours", "Use at most 2 decimal places.")
    bad(post(client, PROCS, {**base, "estimated_hours": True}), "estimated_hours", "Enter the hours as a number")
    bad(post(client, PROCS, {**base, "checklist": [f"Step {n}" for n in range(61)]}), "checklist", "Keep the checklist to 60 steps (this one has 61).")
    bad(post(client, PROCS, {**base, "checklist": []}), "checklist", "Add at least one step")
    bad(post(client, PROCS, {**base, "checklist": "\n\n"}), "checklist", "Add at least one step")
    bad(post(client, PROCS, {**base, "checklist": [{"text": "a | b"}]}), "checklist", "Step 1: a step cannot contain |")
    bad(post(client, PROCS, {**base, "checklist": 42}), "checklist", "Send the checklist as a list of steps")
    bad(post(client, PROCS, {**base, "source_kind": "manual"}), "source_kind", "Choose where the procedure comes from.")
    missing = bad(post(client, PROCS, {"code": "NEW-2"}))
    assert set(missing) == {"name", "source_kind", "estimated_hours", "checklist"}
    bad(post(client, PROCS, {**base, "colour": "red"}), "colour", "Unknown field.")
    bad(post(client, PROCS, {**base, "id": "x", "models": []}), "id", "Read only.")
    bad(post(client, PROCS, {**base, "checklist_text": "Inspect"}), "checklist_text", "send the checklist in checklist")
    bad(post(client, PROCS, [base]), "detail", "JSON object")
    # Another facility's model in the body: a 400, and no procedure is left behind.
    bad(post(client, PROCS, {**base, "device_model": str(theirs["model"].pk)}), "device_model", "Choose a device model from this facility.")
    bad(post(client, PROCS, {**base, "device_model": "nope"}), "device_model", "Choose a device model from this facility.")
    assert sorted(PmProcedure.objects.values_list("code", flat=True)) == ["BD-PCU-PM12", "HA-G5-PM6"]
    # Codes are per facility: another facility's code is free here.
    ok(post(client, PROCS, {**base, "code": "THEIRS-PM1"}), 201)


def test_a_revision_reaches_every_model_while_open_pms_keep_their_hours(client, signed_in, library, vent, vent_model, dept):
    generate_pm_work_orders(as_of=TODAY, lead_days=30)
    open_pm = WorkOrder.objects.get(asset=vent, type=WoType.PM)
    assert open_pm.estimated_hours == Decimal("1.5")
    user = signed_in("technician")
    url = f"{PROCS}{library['g5'].pk}/"
    data = ok(patch(client, url, {"estimated_hours": 2, "checklist": [{"text": "Inspect", "measure": None}, "Function test"]}))
    assert data["changed"] == ["estimated_hours", "checklist"] and data["estimated_hours"] == "2.00" and data["open_pm_work_orders"] == 1
    assert data["checklist"] == [{"text": "Inspect", "measure": None}, {"text": "Function test", "measure": None}]
    g5 = PmProcedure.objects.get(pk=library["g5"].pk)
    assert g5.checklist == ["Inspect", "Function test"] and g5.revision == "Rev K"  # PATCH: only the fields given
    assert g5.history.first().history_change_reason == "Edited: hours, checklist" and g5.history.first().history_user == user
    open_pm.refresh_from_db()
    assert open_pm.estimated_hours == Decimal("1.5")  # an open PM keeps the hours it was created with
    Asset.objects.create(tag="CE-10009", device_model=vent_model, department=dept, next_pm_on=TODAY + timedelta(days=5))
    generate_pm_work_orders(as_of=TODAY, lead_days=30)
    assert WorkOrder.objects.get(asset__tag="CE-10009").estimated_hours == Decimal("2")  # one created from now on plans the new hours


def test_put_saves_every_field_as_edit_procedure_does(client, signed_in, library):
    signed_in("technician")
    url = f"{PROCS}{library['g5'].pk}/"
    data = ok(put(client, url, {"code": "HA-G5-PM6", "name": "G5 6-month PM", "source_kind": "oem", "estimated_hours": "1.5", "checklist": "Inspect"}))
    assert data["changed"] == ["source_reference", "revision", "checklist"]  # left out: saved blank
    g5 = PmProcedure.objects.get(pk=library["g5"].pk)
    assert (g5.source_reference, g5.revision, g5.checklist) == ("", "", ["Inspect"])
    missing = bad(put(client, url, {"code": "HA-G5-PM6"}))
    assert set(missing) == {"name", "source_kind", "estimated_hours", "checklist"}
    signed_in("director")
    assert client.delete(url).status_code == 405 and PmProcedure.objects.filter(pk=g5.pk).exists()  # the library keeps its procedures


def test_what_a_get_returned_can_be_sent_back(client, signed_in, library):
    signed_in("technician")
    url = f"{PROCS}{library['g5'].pk}/"
    shown = ok(client.get(url))
    before = library["g5"].history.count()
    assert ok(patch(client, url, shown))["changed"] == [] and ok(put(client, url, shown))["changed"] == []
    assert library["g5"].history.count() == before  # nothing saved
    bad(patch(client, url, {**shown, "checklist_text": "Inspect"}), "checklist_text", "Read only: send the checklist in checklist")
    bad(patch(client, url, {**shown, "models": []}), "models", "Read only.")
    bad(patch(client, url, {"open_pm_work_orders": 3}), "open_pm_work_orders", "Read only.")
    bad(patch(client, f"{PROCS}{library['pcu'].pk}/", {"code": "ha-g5-pm6"}), "code", "already the code of another procedure here")
    assert PmProcedure.objects.get(pk=library["pcu"].pk).code == "BD-PCU-PM12"


def test_another_facilitys_procedure_cannot_be_revised(client, signed_in, theirs):
    signed_in("director")
    assert patch(client, f"{PROCS}{theirs['procedure'].pk}/", {"name": "Mine now"}).status_code == 404
    with tenant_context(theirs["procedure"].tenant):
        assert PmProcedure.objects.get().name == "Theirs"


# --- a device model's procedure ------------------------------------------------------------------------------------------------


def test_choosing_a_models_procedure(client, signed_in, library, pump_model, theirs):
    user = signed_in("technician")
    url = model_url(pump_model, "procedure")
    assert ok(client.get(url)) == {"device_model": str(pump_model.pk), "procedure": None, "procedure_detail": None, "hours": "1.00",
                                   "default_hours": "1.00"}
    data = ok(put(client, url, {"procedure": str(library["pcu"].pk)}))
    assert data["changed"] is True and data["procedure"] == str(library["pcu"].pk) and data["procedure_detail"]["code"] == "BD-PCU-PM12"
    assert data["hours"] == "0.50"
    pump_model.refresh_from_db()
    assert pump_model.pm_procedure == library["pcu"]
    assert pump_model.history.first().history_change_reason == "PM procedure: BD-PCU-PM12" and pump_model.history.first().history_user == user
    count = pump_model.history.count()
    shown = ok(client.get(url))
    assert ok(put(client, url, shown))["changed"] is False and pump_model.history.count() == count  # the same one: no change
    assert ok(put(client, url, {"procedure": None}))["procedure"] is None
    pump_model.refresh_from_db()
    assert pump_model.pm_procedure is None and pump_model.history.first().history_change_reason == "PM procedure removed"
    bad(put(client, url, {"procedure": str(theirs["procedure"].pk)}), "procedure", "Choose a procedure from this facility.")
    bad(put(client, url, {}), "procedure", "Required")
    bad(put(client, url, {"procedure": None, "colour": 1}), "colour", "Unknown field.")
    bad(put(client, url, {"procedure": None, "hours": "9.00"}), "hours", "Read only.")
    assert put(client, model_url(theirs["model"], "procedure"), {"procedure": None}).status_code == 404
    with tenant_context(theirs["model"].tenant):
        assert DeviceModel.objects.get().pm_procedure is not None


def test_choosing_a_procedure_needs_pm_edit(client, signed_in, library, pump_model):
    signed_in("analyst")
    assert client.get(model_url(pump_model, "procedure")).status_code == 200
    refused(put(client, model_url(pump_model, "procedure"), {"procedure": str(library["pcu"].pk)}), views_pm.CHOOSE_REFUSAL)
    pump_model.refresh_from_db()
    assert pump_model.pm_procedure is None


# --- the risk score --------------------------------------------------------------------------------------------------------------


@pytest.fixture
def on_aem(ctx, dept, pump_model):
    """A pump on the model's 18-month interval (on file without a recorded approval), last serviced 14 months ago."""
    return Asset.objects.create(tag="P-1", device_model=pump_model, department=dept, last_pm_on=months_ago(14),
                                next_pm_on=add_months(months_ago(14), 18))


def test_an_unscored_model_reads_as_unscored(client, signed_in, pump_model):
    signed_in("analyst")  # PM View: the model drawer's door
    data = ok(client.get(model_url(pump_model, "risk-score")))
    assert {k: data[k] for k in ("function", "physical", "maintenance", "incidents", "score", "band", "band_class")} == dict.fromkeys(
        ("function", "physical", "maintenance", "incidents", "score", "band", "band_class"))
    assert (data["risk_class"], data["reviewed_on"], data["review_due_on"], data["review_due"]) == ("high", None, None, True)
    assert data["rubric"] == [{"part": "function", "label": "Clinical function", "low": 1, "high": 10},
                              {"part": "physical", "label": "Physical risk of failure", "low": 1, "high": 5},
                              {"part": "maintenance", "label": "Maintenance requirement", "low": 1, "high": 5},
                              {"part": "incidents", "label": "Incident history", "low": 0, "high": 2}]


def test_scored_into_life_support_the_model_leaves_aem_and_says_how_many_pms_moved(client, signed_in, pump_model, on_aem):
    user = signed_in("director")
    url = model_url(pump_model, "risk-score")
    data = ok(put(client, url, LIFE))
    assert (data["score"], data["band"], data["band_class"], data["risk_class"]) == (18, "16 and above", "life_support", "life_support")
    assert (data["reviewed_on"], data["review_due_on"], data["review_due"]) == (TODAY.isoformat(), add_months(TODAY, 12).isoformat(), False)
    assert data["effects"] == {"previous_class": "high", "class_changed": True, "review": False,
                               "aem": {"ended": True, "withdrawn": False, "cleared": False, "devices_moved": 1, "rule": "life_support"}}
    pump_model.refresh_from_db()
    on_aem.refresh_from_db()
    assert pump_model.aem_interval_months is None and on_aem.next_pm_on == TODAY
    assert pump_model.history.filter(history_change_reason="Risk scored 18 (Life support)", history_user=user).exists()
    # The same score again is the yearly review: only the date, nothing for AEM.
    again = ok(put(client, url, LIFE))
    assert again["effects"] == {"previous_class": "life_support", "class_changed": False, "review": True, "aem": NO_EFFECT}
    # Sending back what the GET shows is fine; changing a part rescores.
    shown = ok(client.get(url))
    lower = ok(put(client, url, {**shown, "function": 5}))
    assert (lower["score"], lower["band_class"], lower["effects"]["class_changed"]) == (13, "high", True)


def test_the_api_scores_as_the_risk_modal_does(client, signed_in, ctx, dept):
    """Two models alike, one scored on the web and one through the API: the same class, the same AEM effect, the same PMs moved."""
    twins = []
    for name in ("A", "B"):
        dm = DeviceModel.objects.create(manufacturer="Acme", model=name, description="Pump", category="Pumps", risk_class=RiskClass.HIGH,
                                        oem_pm_interval_months=12, aem_interval_months=18)
        a = Asset.objects.create(tag=f"T-{name}", device_model=dm, department=dept, last_pm_on=months_ago(14), next_pm_on=add_months(months_ago(14), 18))
        twins.append((dm, a))
    signed_in("director")
    (web_dm, web_a), (api_dm, api_a) = twins
    r = client.post(f"/pm/models/{web_dm.pk}/risk/", {k: str(v) for k, v in LIFE.items()}, HTTP_HX_REQUEST="true")
    assert r.status_code == 200 and r["HX-Retarget"] == "#drawer"
    data = ok(put(client, model_url(api_dm, "risk-score"), LIFE))
    for obj in (web_dm, web_a, api_dm, api_a):
        obj.refresh_from_db()
    fields = ("risk_function", "risk_physical", "risk_maintenance", "risk_incidents", "risk_reviewed_on", "risk_class", "aem_interval_months")
    assert [getattr(web_dm, f) for f in fields] == [getattr(api_dm, f) for f in fields]
    assert web_a.next_pm_on == api_a.next_pm_on == TODAY
    assert "1 device's next PM moved earlier" in r["HX-Trigger"] and data["effects"]["aem"]["devices_moved"] == 1


@pytest.mark.parametrize("slug", ["manager", "technician", "analyst"])
def test_scoring_needs_equipment_approve(client, signed_in, pump_model, slug):
    signed_in(slug)
    url = model_url(pump_model, "risk-score")
    assert client.get(url).status_code == 200
    refused(put(client, url, LIFE), views_pm.RISK_REFUSAL)
    refused(client.delete(url), views_pm.RISK_REFUSAL)
    pump_model.refresh_from_db()
    assert pump_model.risk_score is None and pump_model.risk_class == RiskClass.HIGH


def test_a_facility_can_give_its_manager_the_risk_score(client, signed_in, pump_model):
    Role.objects.get(slug="manager").set_levels({"equipment": Level.APPROVE})
    signed_in("manager")
    assert ok(put(client, model_url(pump_model, "risk-score"), LIFE))["score"] == 18


def test_the_rubric_is_the_services(client, signed_in, pump_model, theirs):
    signed_in("director")
    url = model_url(pump_model, "risk-score")
    bad(put(client, url, {**LIFE, "function": 11}), "function", "Clinical function is a whole number from 1 to 10.")
    bad(put(client, url, {**LIFE, "incidents": 1.5}), "incidents", "Incident history is a whole number from 0 to 2.")
    data = bad(put(client, url, {"function": 10}))
    assert set(data) == {"physical", "maintenance", "incidents"}
    bad(put(client, url, {**LIFE, "score": 18}), "score", "Read only.")
    bad(put(client, url, {**LIFE, "colour": 1}), "colour", "Unknown field.")
    bad(put(client, url, [LIFE]), "detail", "JSON object")
    assert put(client, model_url(theirs["model"], "risk-score"), LIFE).status_code == 404
    assert client.get(model_url(theirs["model"], "risk-score")).status_code == 404
    assert put(client, f"{API}device-models/not-an-id/risk-score/", LIFE).status_code == 404
    pump_model.refresh_from_db()
    assert pump_model.risk_score is None


def test_clearing_the_score_keeps_the_class(client, signed_in, pump_model):
    user = signed_in("director")
    url = model_url(pump_model, "risk-score")
    ok(put(client, url, LIFE))
    data = ok(client.delete(url))
    assert data["score"] is None and data["function"] is None and data["risk_class"] == "life_support" and data["reviewed_on"] is None
    pump_model.refresh_from_db()
    assert pump_model.risk_score is None and pump_model.history.first().history_change_reason == "Risk score cleared"
    assert pump_model.history.first().history_user == user
    bad(client.delete(url), "detail", "has no risk score to clear.")


# --- AEM ---------------------------------------------------------------------------------------------------------------------------


def test_the_aem_program_reads_as_the_aem_tab(client, signed_in, monitor, vent_model, pump_model, make_user, theirs):
    signed_in("analyst")  # PM View
    tech = make_user("technician", username="tech-x@riverside.example")
    d = propose(monitor, tech)
    data = ok(client.get(model_url(monitor, "aem")))
    assert (data["oem_interval_months"], data["interval_months"], data["on_aem"], data["excluded"], data["exclusion_rule"]) == (12, 12, False, "", "")
    assert data["in_force"] is None and data["unapproved_interval_months"] is None and data["history_years"] == aem.AEM_HISTORY_YEARS
    assert data["open_proposal"]["id"] == str(d.pk) and data["open_proposal"]["proposed_by_name"] == str(tech)
    assert data["propose_blocker"].startswith("An AEM proposal for this model is open (24 months")
    assert data["evidence"] == aem.evidence(monitor, TODAY) and data["evidence"]["enough_history"] is True
    assert [row["id"] for row in data["decisions"]] == [str(d.pk)]
    vent = ok(client.get(model_url(vent_model, "aem")))
    assert (vent["excluded"], vent["exclusion_rule"], vent["propose_blocker"]) == (aem.LIFE_SUPPORT_REFUSAL, "life_support", aem.LIFE_SUPPORT_REFUSAL)
    pump = ok(client.get(model_url(pump_model, "aem")))  # 18 months on file without a recorded approval
    assert (pump["interval_months"], pump["on_aem"], pump["unapproved_interval_months"], pump["in_force"]) == (18, True, 18, None)
    assert client.get(model_url(theirs["model"], "aem")).status_code == 404


def test_proposing_through_the_service(client, signed_in, monitor):
    user = signed_in("technician")  # PM Edit
    data = ok(post(client, model_url(monitor, "aem"), {"interval_months": 24, "rationale": "Two repairs in four years."}), 201)
    d = AemDecision.objects.get()
    assert data["id"] == str(d.pk) and (d.status, d.interval_months, d.oem_interval_months, d.proposed_by) == (AemStatus.PROPOSED, 24, 12, user)
    assert data["evidence"] == d.evidence == aem.evidence(monitor, TODAY) and data["rationale"] == "Two repairs in four years."
    assert data["proposed_by_name"] == str(user) and data["proposed_on"] == TODAY.isoformat()
    bad(post(client, model_url(monitor, "aem"), {"interval_months": 18, "rationale": "Again"}), "detail", "An AEM proposal for this model is open")
    monitor.refresh_from_db()
    assert monitor.aem_interval_months is None  # proposing changes nothing until the committee approves


def test_the_proposal_rules_are_the_services(client, signed_in, monitor, vent_model, ctx):
    signed_in("technician")
    url = model_url(monitor, "aem")
    bad(post(client, url, {"interval_months": "abc", "rationale": "x"}), "interval_months", "a whole number of months, 1 to 120")
    bad(post(client, url, {"interval_months": 12, "rationale": "x"}), "interval_months", "That is the OEM interval (12 months).")
    bad(post(client, url, {"interval_months": 24}), "rationale", "Make the case for the new interval")
    bad(post(client, url, {"interval_months": 24, "rationale": 5}), "rationale", "Send this as text.")
    bad(post(client, url, {"interval_months": 24, "rationale": "x", "decided_on": "2026-01-01"}), "decided_on", "Unknown field.")
    bad(post(client, model_url(vent_model, "aem"), {"interval_months": 12, "rationale": "x"}), "detail", aem.LIFE_SUPPORT_REFUSAL)
    young = DeviceModel.objects.create(manufacturer="New", model="N1", description="New", category="New", risk_class=RiskClass.LOW)
    bad(post(client, model_url(young, "aem"), {"interval_months": 24, "rationale": "x"}), "detail", "This model has no devices on record")
    assert not AemDecision.objects.exists()


def test_proposing_needs_pm_edit(client, signed_in, monitor):
    signed_in("analyst")
    refused(post(client, model_url(monitor, "aem"), {"interval_months": 24, "rationale": "x"}), views_pm.PROPOSE_REFUSAL)
    assert not AemDecision.objects.exists()


def test_the_committee_approves_and_a_shorter_interval_pulls_pms_in(client, signed_in, monitor, make_user):
    d = propose(monitor, make_user("technician", username="t1@riverside.example"), months=6)
    moves = len(aem.pull_in_plan(monitor, 6))  # what the decide modal says will move
    assert moves == 1
    manager = signed_in("manager")  # PM Approve
    data = ok(post(client, decision_url(d, "approve"), {"decided_on": TODAY.isoformat(), "note": "EMC minutes, item 4"}))
    assert (data["status"], data["devices_moved"], data["decided_on"], data["decision_note"]) == ("approved", moves, TODAY.isoformat(), "EMC minutes, item 4")
    assert data["decided_by_name"] == str(manager)
    monitor.refresh_from_db()
    assert monitor.aem_interval_months == 6 and Asset.objects.get(tag="M-1").next_pm_on == TODAY
    program = ok(client.get(model_url(monitor, "aem")))
    assert program["in_force"]["id"] == str(d.pk) and (program["interval_months"], program["on_aem"]) == (6, True)
    bad(post(client, decision_url(d, "approve"), {"decided_on": TODAY.isoformat(), "note": "x"}), "detail", "This proposal is no longer open: it was approved.")


def test_a_longer_interval_moves_nothing(client, signed_in, monitor, make_user):
    d = propose(monitor, make_user("technician", username="t1@riverside.example"))
    signed_in("director")
    assert ok(post(client, decision_url(d, "approve"), {"decided_on": TODAY.isoformat(), "note": "Minutes"}))["devices_moved"] == 0


def test_the_proposer_never_decides_their_own_case(client, signed_in, monitor):
    manager = signed_in("manager")
    d = propose(monitor, manager)
    for act in ("approve", "reject"):
        r = post(client, decision_url(d, act), {"decided_on": TODAY.isoformat(), "note": "Minutes"})
        assert r.status_code == 403 and r.json()["detail"].startswith("You proposed this change.")
    d.refresh_from_db()
    assert d.status == AemStatus.PROPOSED


def test_deciding_needs_pm_approve(client, signed_in, monitor, make_user):
    d = propose(monitor, make_user("technician", username="t1@riverside.example"))
    signed_in("technician")
    for act in ("approve", "reject"):
        refused(post(client, decision_url(d, act), {"decided_on": TODAY.isoformat(), "note": "Minutes"}), views_pm.DECIDE_REFUSAL)
    d.refresh_from_db()
    assert d.status == AemStatus.PROPOSED


def test_the_committees_date_and_minutes(client, signed_in, monitor, make_user, theirs):
    d = propose(monitor, make_user("technician", username="t1@riverside.example"))
    signed_in("manager")
    url = decision_url(d, "approve")
    tomorrow = (TODAY + timedelta(days=1)).isoformat()
    bad(post(client, url, {"decided_on": tomorrow, "note": "x"}), "decided_on", "cannot be dated in the future")
    bad(post(client, url, {"decided_on": "2026-13-01", "note": "x"}), "decided_on", "as YYYY-MM-DD")
    bad(post(client, url, {"note": "x"}), "decided_on", "Enter the date of the committee's meeting.")
    bad(post(client, url, {"decided_on": TODAY.isoformat()}), "note", "Enter the committee's minutes reference.")
    bad(post(client, decision_url(d, "reject"), {"decided_on": TODAY.isoformat()}), "note", "Enter the committee's reason or minutes reference.")
    bad(post(client, url, {"decided_on": TODAY.isoformat(), "note": "x", "interval_months": 6}), "interval_months", "Unknown field.")
    assert post(client, decision_url(theirs["decision"], "approve"), {"decided_on": TODAY.isoformat(), "note": "x"}).status_code == 404
    d.refresh_from_db()
    assert d.status == AemStatus.PROPOSED


def test_the_committee_rejects(client, signed_in, monitor, make_user):
    d = propose(monitor, make_user("technician", username="t1@riverside.example"))
    manager = signed_in("manager")
    data = ok(post(client, decision_url(d, "reject"), {"decided_on": TODAY.isoformat(), "note": "Not enough PM data"}))
    assert (data["status"], data["decision_note"], data["decided_by_name"]) == ("rejected", "Not enough PM data", str(manager))
    monitor.refresh_from_db()
    assert monitor.aem_interval_months is None


def test_withdrawing(client, signed_in, monitor, make_user):
    proposer = signed_in("technician")
    d = propose(monitor, proposer)
    data = ok(post(client, decision_url(d, "withdraw")))
    assert (data["status"], data["end_reason"], data["ended_by_name"]) == ("withdrawn", "Withdrawn by the proposer.", str(proposer))
    bad(post(client, decision_url(d, "withdraw")), "detail", "This proposal is no longer open: it was withdrawn.")
    # Another technician may not; a PM Approve holder may.
    d2 = propose(monitor, proposer, months=18)
    signed_in("technician")
    refused(post(client, decision_url(d2, "withdraw")), views_pm.WITHDRAW_REFUSAL)
    signed_in("analyst")
    refused(post(client, decision_url(d2, "withdraw")), views_pm.WITHDRAW_REFUSAL)
    signed_in("manager")
    bad(post(client, decision_url(d2, "withdraw"), {"reason": "x"}), "reason", "Unknown field.")
    assert ok(post(client, decision_url(d2, "withdraw")))["end_reason"] == "Withdrawn."


def test_ending_an_aem_interval(client, signed_in, monitor, make_user):
    d = propose(monitor, make_user("technician", username="t1@riverside.example"))
    aem.approve(d, by=make_user("director", username="d1@riverside.example"), decided_on=TODAY, note="Minutes")
    Asset.objects.filter(tag="M-1").update(last_pm_on=months_ago(14), next_pm_on=add_months(months_ago(14), 24))
    monitor.refresh_from_db()
    moves = len(aem.pull_in_plan(monitor, 12))  # what the End AEM modal says will move
    signed_in("technician")
    refused(post(client, model_url(monitor, "aem/end"), {"reason": "Repairs rose"}), views_pm.END_REFUSAL)
    manager = signed_in("manager")
    bad(post(client, model_url(monitor, "aem/end"), {}), "reason", "Say why the AEM interval ends.")
    bad(post(client, model_url(monitor, "aem/end"), {"reason": "x", "note": "y"}), "note", "Unknown field.")
    data = ok(post(client, model_url(monitor, "aem/end"), {"reason": "Repairs rose at 24 months"}))
    assert data["devices_moved"] == moves == 1 and (data["on_aem"], data["interval_months"], data["in_force"]) == (False, 12, None)
    d.refresh_from_db()
    assert (d.status, d.ended_by, d.end_reason) == (AemStatus.ENDED, manager, "Repairs rose at 24 months")
    assert Asset.objects.get(tag="M-1").next_pm_on == TODAY
    bad(post(client, model_url(monitor, "aem/end"), {"reason": "Again"}), "detail", "This model has no AEM interval in force")


def test_ending_an_interval_on_file_without_a_recorded_approval(client, signed_in, pump_model, on_aem):
    signed_in("manager")
    data = ok(post(client, model_url(pump_model, "aem/end"), {"reason": "No approval on record"}))
    assert data["devices_moved"] == 1 and data["unapproved_interval_months"] is None
    pump_model.refresh_from_db()
    assert pump_model.aem_interval_months is None


def test_the_decisions_list(client, signed_in, monitor, pump_model, make_user, theirs, dept):
    tech = make_user("technician", username="t1@riverside.example")
    d1 = propose(monitor, tech)
    aem.withdraw(d1, by=tech)
    d2 = propose(monitor, tech, months=18)
    Asset.objects.create(tag="P-9", device_model=pump_model, department=dept, installed_on=add_months(TODAY, -48), next_pm_on=TODAY + timedelta(days=30))
    d3 = propose(pump_model, tech)
    signed_in("analyst")
    ids = {row["id"] for row in ok(client.get(DECISIONS))["results"]}
    assert ids == {str(d1.pk), str(d2.pk), str(d3.pk)}  # never another facility's
    assert {row["id"] for row in ok(client.get(f"{DECISIONS}?device_model={monitor.pk}"))["results"]} == {str(d1.pk), str(d2.pk)}
    assert [row["id"] for row in ok(client.get(f"{DECISIONS}?status=proposed&device_model={monitor.pk}"))["results"]] == [str(d2.pk)]
    assert ok(client.get(decision_url(d3)))["device_model_name"] == str(pump_model)
    bad(client.get(f"{DECISIONS}?device_model=nope"), "device_model", "Not a device model's id.")
    bad(client.get(f"{DECISIONS}?status=open"), "status", "One of proposed")
    assert client.get(decision_url(theirs["decision"])).status_code == 404
    signed_in("director")
    assert post(client, DECISIONS, {"device_model": str(monitor.pk)}).status_code == 405  # cases are opened by proposing
    assert AemDecision.objects.count() == 3


# --- Auto-assign week --------------------------------------------------------------------------------------------------------------


@pytest.fixture
def week(ctx, dept, vent_model, pump_model, techs):
    """Due tomorrow: two ventilators (only Dana is credentialed), a pump (Dana and Tom), and a monitor nobody is credentialed for."""
    tomorrow = TODAY + timedelta(days=1)
    monitor = DeviceModel.objects.create(manufacturer="Philips", model="MX450", description="Patient monitor", category="Monitors",
                                         risk_class=RiskClass.MEDIUM)
    return {tag: Asset.objects.create(tag=tag, device_model=dm, department=dept, next_pm_on=tomorrow)
            for tag, dm in (("CE-V1", vent_model), ("CE-V2", vent_model), ("CE-P1", pump_model), ("CE-M1", monitor))}


def test_the_week_preview_is_the_modals(client, signed_in, week):
    signed_in("manager")  # PM Approve and Work orders Approve
    data = ok(client.get(WEEK))
    w = week_assignment_preview(TODAY)
    assert (data["start"], data["end"]) == (TODAY.isoformat(), (TODAY + timedelta(days=6)).isoformat())
    assert (data["assigned"], data["created"], data["unassigned"], data["unassigned_new"], data["held"], data["nothing_to_do"]) == (3, 4, 1, 1, 0, False)
    assert [(sh["technician"]["name"], sh["new"], sh["existing"], sh["count"], sh["hours"]) for sh in data["shares"]] == [
        (sh.technician.name, sh.new, sh.existing, sh.count, f"{sh.hours:.2f}") for sh in w.shares]
    assert data["technicians"] == w.technicians and data["hours"] == f"{w.hours:.2f}"
    assert data["uncovered"] == [{"asset_id": str(week["CE-M1"].pk), "tag": "CE-M1", "description": "Patient monitor",
                                  "due_on": (TODAY + timedelta(days=1)).isoformat(), "has_open_pm": False}]
    assert not WorkOrder.objects.exists()  # the preview changes nothing


def test_assign_week_does_what_the_preview_said(client, signed_in, week):
    signed_in("manager")
    preview = ok(client.get(WEEK))
    done = ok(post(client, ASSIGN_WEEK))
    assert done == preview
    pms = WorkOrder.objects.filter(type=WoType.PM)
    assert pms.count() == 4 and pms.filter(assigned_to__isnull=True).get().asset.tag == "CE-M1"
    for sh in preview["shares"]:
        assert pms.filter(assigned_to_id=sh["technician"]["id"]).count() == sh["count"]
    again = ok(post(client, ASSIGN_WEEK))  # the planner lock, then nothing left to do
    assert again["nothing_to_do"] is True and (again["assigned"], again["created"]) == (0, 0) and pms.count() == 4
    bad(post(client, ASSIGN_WEEK, {"day": TODAY.isoformat()}), "day", "Unknown field.")


def test_the_week_uses_the_api_clock(client, signed_in, week, monkeypatch):
    monkeypatch.setattr(views_pm, "_today", lambda: TODAY + timedelta(days=10))
    signed_in("manager")
    data = ok(client.get(WEEK))
    assert data["start"] == (TODAY + timedelta(days=10)).isoformat() and data["overdue"] == 4 and data["assigned"] == 0


@pytest.mark.parametrize("slug", ["technician", "analyst"])
def test_auto_assign_week_needs_pm_approve_and_assign(client, signed_in, week, slug):
    signed_in(slug)
    refused(client.get(WEEK), views_pm.WEEK_REFUSAL)
    refused(post(client, ASSIGN_WEEK), views_pm.WEEK_REFUSAL)
    assert not WorkOrder.objects.exists()


def test_pm_approve_without_the_right_to_assign_is_not_enough(client, signed_in, week):
    Role.objects.get(slug="manager").set_levels({"workorders": Level.EDIT})
    signed_in("manager")
    refused(client.get(WEEK), views_pm.WEEK_REFUSAL)
    refused(post(client, ASSIGN_WEEK), views_pm.WEEK_REFUSAL)
    assert not WorkOrder.objects.exists()


# --- scoped users, and no tenant ----------------------------------------------------------------------------------------------------


def every_endpoint(dm, procedure, decision):
    """Every endpoint this module adds: (method, url, body)."""
    return [
        ("get", WEEK, None), ("post", ASSIGN_WEEK, None),
        ("get", PROCS, None), ("get", f"{PROCS}{procedure.pk}/", None),
        ("post", PROCS, {"code": "S-1", "name": "S", "source_kind": "oem", "estimated_hours": 1, "checklist": ["Inspect"]}),
        ("patch", f"{PROCS}{procedure.pk}/", {"name": "Scoped"}),
        ("put", f"{PROCS}{procedure.pk}/", {"code": "S-2", "name": "S", "source_kind": "oem", "estimated_hours": 1, "checklist": ["Inspect"]}),
        ("get", model_url(dm, "procedure"), None), ("put", model_url(dm, "procedure"), {"procedure": None}),
        ("get", model_url(dm, "risk-score"), None), ("put", model_url(dm, "risk-score"), LIFE), ("delete", model_url(dm, "risk-score"), None),
        ("get", model_url(dm, "aem"), None), ("post", model_url(dm, "aem"), {"interval_months": 18, "rationale": "x"}),
        ("post", model_url(dm, "aem/end"), {"reason": "x"}),
        ("get", DECISIONS, None), ("get", decision_url(decision), None),
        ("post", decision_url(decision, "approve"), {"decided_on": TODAY.isoformat(), "note": "x"}),
        ("post", decision_url(decision, "reject"), {"decided_on": TODAY.isoformat(), "note": "x"}),
        ("post", decision_url(decision, "withdraw"), None),
    ]


@pytest.fixture
def program(library, monitor, make_user, week):
    tech = make_user("technician", username="t1@riverside.example")
    return {"dm": monitor, "procedure": library["g5"], "decision": propose(monitor, tech)}


def _unchanged(program):
    d = AemDecision.objects.get(pk=program["decision"].pk)
    dm = DeviceModel.objects.get(pk=program["dm"].pk)
    return (d.status, dm.risk_score, dm.pm_procedure_id, PmProcedure.objects.count(), PmProcedure.objects.get(pk=program["procedure"].pk).name,
            WorkOrder.objects.count())


def test_every_endpoint_refuses_a_scoped_role_with_full_levels(client, ctx, program):
    role = Role.objects.create(name="Scoped full", slug="scoped-full", scope=DataScope.COMPANY)
    role.set_levels(ALL_FULL)
    user = User.objects.create_user(username="scoped@riverside.example", password="Test-Pass-2026-x", tenant=ctx, role=role, company="Hamilton Medical")
    client.force_login(user)
    before = _unchanged(program)
    for method, url, body in every_endpoint(program["dm"], program["procedure"], program["decision"]):
        r = send(client, method, url, body)
        assert r.status_code == 403 and "part of this facility" in r.json()["detail"], (method, url, r.status_code)
    assert _unchanged(program) == before


@pytest.mark.parametrize("who, attrs", [("vendor", {"company": "Hamilton Medical"}), ("requester", {"department": "ICU"})])
def test_every_endpoint_refuses_the_default_scoped_roles(client, make_user, program, who, attrs):
    user = make_user(who, username=f"{who}-s@riverside.example")
    for k, v in attrs.items():
        setattr(user, k, v)
    user.save()
    client.force_login(user)
    before = _unchanged(program)
    for method, url, body in every_endpoint(program["dm"], program["procedure"], program["decision"]):
        assert send(client, method, url, body).status_code == 403, (method, url)
    assert _unchanged(program) == before


def test_the_reads_answer_a_facility_wide_role(client, signed_in, program):
    """As JSON, and as the browsable API's page (which reads while it renders its forms)."""
    signed_in("director")
    for method, url, _body in every_endpoint(program["dm"], program["procedure"], program["decision"]):
        if method == "get":
            assert client.get(url).status_code == 200, url
            assert client.get(url, HTTP_ACCEPT="text/html").status_code == 200, url


def test_a_superuser_with_no_facility_is_told_to_pick_one(client, db):
    root = User.objects.create_superuser(username="root", password="Test-Pass-2026-x", email="root@example.com")
    token = Token.objects.create(user=root)
    some = "00000000-0000-0000-0000-000000000001"
    for url in [PROCS, DECISIONS, WEEK, f"{API}device-models/{some}/aem/", f"{API}device-models/{some}/risk-score/"]:
        r = send(client, "get", url, token=token)
        assert r.status_code == 403 and "Pick a tenant first" in r.json()["detail"], url
    r = post(client, PROCS, {"code": "R-1", "name": "R", "source_kind": "oem", "estimated_hours": 1, "checklist": ["x"]}, token=token)
    assert r.status_code == 403 and not PmProcedure.unscoped.exists()  # unscoped: nothing written in any facility


# --- by token, and under row-level security ------------------------------------------------------------------------------------------


@pytest.fixture
def token_rows(tenant, make_user, other_tenant):
    """Rows made inside the facility; the requests then run with no tenant set, as a token request arrives."""
    create_default_roles(other_tenant)
    with tenant_context(tenant):
        dept = Department.objects.create(name="ICU")
        dm = DeviceModel.objects.create(manufacturer="Philips", model="IntelliVue MX750", description="Patient monitor", category="Monitors",
                                        risk_class=RiskClass.HIGH, oem_pm_interval_months=12)
        Asset.objects.create(tag="M-1", device_model=dm, department=dept, installed_on=add_months(TODAY, -48), last_pm_on=months_ago(8),
                             next_pm_on=TODAY + timedelta(days=1))
        dana = Technician.objects.create(name="Dana Whitfield")
        Credential.objects.create(technician=dana, scope=Scope.CATEGORY, value="Monitors")
    tokens = {slug: Token.objects.create(user=make_user(slug, username=f"tok-{slug}@riverside.example")) for slug in ("technician", "manager", "director")}
    return {"tenant": tenant, "dm": dm, "tokens": tokens}


def walk_by_token(client, rows):
    """One write (and a read) per endpoint, by token, outside any tenant context."""
    tech, manager, director = (rows["tokens"][k] for k in ("technician", "manager", "director"))
    dm = rows["dm"]
    created = ok(post(client, PROCS, {"code": "TOK-1", "name": "Token PM", "source_kind": "oem", "estimated_hours": "1.5",
                                      "checklist": ["Inspect", {"text": "Test", "measure": "ok"}]}, token=tech), 201)
    assert ok(send(client, "get", PROCS, token=tech))["count"] == 1
    assert ok(patch(client, f"{PROCS}{created['id']}/", {"estimated_hours": 2}, token=tech))["changed"] == ["estimated_hours"]
    assert ok(put(client, f"{PROCS}{created['id']}/", {"code": "TOK-1", "name": "Token PM", "source_kind": "oem", "estimated_hours": 2,
                                                       "checklist": ["Inspect"]}, token=tech))["changed"] == ["checklist"]
    assert ok(put(client, model_url(dm, "procedure"), {"procedure": created["id"]}, token=tech))["changed"] is True
    assert ok(send(client, "get", model_url(dm, "procedure"), token=tech))["hours"] == "2.00"
    assert ok(put(client, model_url(dm, "risk-score"), {"function": 6, "physical": 3, "maintenance": 2, "incidents": 0}, token=director))["score"] == 11
    assert ok(send(client, "delete", model_url(dm, "risk-score"), token=director))["score"] is None
    assert ok(send(client, "get", model_url(dm, "aem"), token=tech))["propose_blocker"] == ""
    first = ok(post(client, model_url(dm, "aem"), {"interval_months": 24, "rationale": "Few failures."}, token=tech), 201)
    decided = {"decided_on": TODAY.isoformat(), "note": "Minutes"}
    assert ok(post(client, decision_url_id(first["id"], "approve"), decided, token=manager))["status"] == "approved"
    second = ok(post(client, model_url(dm, "aem"), {"interval_months": 18, "rationale": "Shorter."}, token=tech), 201)
    assert ok(post(client, decision_url_id(second["id"], "reject"), decided, token=manager))["status"] == "rejected"
    third = ok(post(client, model_url(dm, "aem"), {"interval_months": 18, "rationale": "Again."}, token=tech), 201)
    assert ok(post(client, decision_url_id(third["id"], "withdraw"), token=tech))["status"] == "withdrawn"
    assert ok(send(client, "get", DECISIONS, token=tech))["count"] == 3
    assert ok(post(client, model_url(dm, "aem/end"), {"reason": "Committee review"}, token=manager))["on_aem"] is False
    assert ok(send(client, "get", WEEK, token=manager))["assigned"] == 1
    assert ok(post(client, ASSIGN_WEEK, token=manager))["created"] == 1


def decision_url_id(pk, act):
    return f"{DECISIONS}{pk}/{act}/"


def test_every_write_by_token(client, token_rows):
    walk_by_token(client, token_rows)
    # unscoped: checking which facility the rows landed in
    assert PmProcedure.unscoped.get(code="TOK-1").tenant_id == token_rows["tenant"].id
    assert {d.tenant_id for d in AemDecision.unscoped.all()} == {token_rows["tenant"].id}
    with tenant_context(token_rows["tenant"]):
        wo = WorkOrder.objects.get(type=WoType.PM)
        assert wo.assigned_to.name == "Dana Whitfield" and wo.estimated_hours == Decimal("2")


def test_a_token_from_another_facility_reaches_nothing_here(client, token_rows, make_user, other_tenant):
    theirs = Token.objects.create(user=make_user("director", other_tenant, username="their-director@other.example"))
    dm = token_rows["dm"]
    assert send(client, "get", model_url(dm, "aem"), token=theirs).status_code == 404
    assert put(client, model_url(dm, "risk-score"), LIFE, token=theirs).status_code == 404
    assert ok(send(client, "get", PROCS, token=theirs))["count"] == 0


@needs_postgres
def test_the_pm_program_by_token_under_the_policies(client, token_rows):
    """As the runtime role: procedures, the model's procedure, the risk score, every AEM step, and Auto-assign week (the planner
    lock, the model row lock) by token, with the tenant the token resolves to."""
    as_app_role()
    walk_by_token(client, token_rows)
    tech = token_rows["tokens"]["technician"]
    for url in (PROCS, DECISIONS, model_url(token_rows["dm"], "aem"), model_url(token_rows["dm"], "risk-score")):
        assert send(client, "get", url, token=tech).status_code == 200
        assert client.get(url, HTTP_ACCEPT="text/html", HTTP_AUTHORIZATION=f"Token {tech.key}").status_code == 200, url
    with tenant_context(token_rows["tenant"]):
        assert PmProcedure.objects.get().code == "TOK-1" and AemDecision.objects.count() == 3
        assert WorkOrder.objects.filter(type=WoType.PM).count() == 1
