"""
Slice 33, the adversarial review's fixes to what a repair found (apps.workorders.findings), one test or more per fix:

1. findings.facility_holds: the stored finding is the facility's (a scoped user cannot choose it, the repair's own limits still allow it).
   completion answers a scoped user the same whatever they send while it holds (any non-blank value, the stored one and an unknown one
   included, is refused with findings.FACILITY_RECORDED; blank or left out keeps it), so no refusal tells a vendor's account what the
   facility recorded: through the service, the API, and the completion modal.
2. The API's completion_checklist empties a vendor's choices only while the facility's finding holds: once the repair's limits refuse
   it (a "PM could have caught it" on a repair a failed PM's failure was recorded on since), it offers the observations and says one is
   required, as the transition, the modal, and the service do.
3. The API's words for a missing or not-offered finding never name a vendor's observations while the facility's finding holds.
4. A reopened repair's drawer says its finding is "kept unless another is chosen" only while this user may keep it.
5. The AEM tab's "Record what was found" lists only the repairs this user may record (a closed one needs Work orders Approve) and says
   how many closed ones are not listed.
6. findings.hint and findings.order_words: the battery hint's "PM could have caught it" sentence and the order to decide by name only
   the choices a door offers (a vendor's account, a failed PM's repair); with every choice offered they are the words from before. The
   modal, the API's checklist, and the AEM tab read them.

The last test runs the vendor's doors under the row-level security policies as the runtime role, and one checks that the modal's
refusal of a vendor's blank on a repair found on a PM never tells a stored finding the repair's limits now refuse from none stored.
"""
import html
import re
from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone
from pg_helpers import as_app_role, needs_postgres
from repair_fixtures import CHECKED, POST_REPAIR_ALL_PASS, checked_post
from rest_framework.authtoken.models import Token

from apps.accounts.models import Level, Module, Role, User
from apps.api.views import IN_CHECKLIST, _finding_words
from apps.equipment.models import Asset, DeviceModel, RiskClass
from apps.pm.dates import add_months
from apps.pm.models import PmProcedure
from apps.tenants.context import tenant_context
from apps.web import views_aem
from apps.workorders import findings
from apps.workorders.completion import complete_work_order
from apps.workorders.models import PmResult, Priority, RepairFinding, WorkOrder, WoStatus, WoType
from apps.workorders.services import assign, change_status, create_work_order

F = RepairFinding
HX = {"HTTP_HX_REQUEST": "true"}
API = "/api/v1/work-orders/"
DAY = timedelta(days=1)
VENDOR = "Hamilton Medical field service"
IN_HOUSE = ["no_problem_found", "use_error", "environment", "damage", "battery", "accessory", "software", "wear", "pm_preventable",
            "service_induced"]
OBSERVED = ["no_problem_found", "environment", "damage", "battery", "accessory", "software", "wear"]  # a vendor's account
# The facility's judgments, never a vendor's to choose; then what a vendor's account might send instead (an observation, an unknown
# value, the right value in the wrong case)
JUDGMENTS = (F.USE_ERROR, F.PM_PREVENTABLE, F.SERVICE_INDUCED, F.NOT_STATED)
GUESSES = (*JUDGMENTS, F.WEAR, F.BATTERY, "worn_out", "USE_ERROR")
# The words from before the review, which a door offering every choice still shows
FULL_BATTERY = ("The battery failed before it was due for replacement. One past due, or one the PM's battery test should have caught, is PM "
                "could have caught it.")
SHORT_BATTERY = "The battery failed before it was due for replacement."
FULL_ORDER = ("When more than one fits, the first of: caused by earlier service, damaged in handling, power or room conditions, PM could "
              "have caught it, battery, accessory, software, wore out.")
VENDOR_ORDER = "When more than one fits, the first of: damaged in handling, power or room conditions, battery, accessory, software, wore out."
FROM_PM_ORDER = ("When more than one fits, the first of: caused by earlier service, damaged in handling, power or room conditions, battery, "
                 "accessory, software, wore out.")
VENDOR_SEND = ('Send repair_finding, what the repair found: one of "no_problem_found" (No problem found), "environment" (Power, gas, '
               'network, room), "damage" (Damaged in handling), "battery" (Battery failed), "accessory" (Accessory or cable), "software" '
               '(Software or firmware fault), "wear" (Wore out or failed in use); the work order\'s completion_checklist gives each one\'s hint.')
REOPENED_KEPT = "What was found: PM could have caught it, kept unless another is chosen."


@pytest.fixture
def today(ctx):
    return timezone.localdate()


@pytest.fixture
def vendor_user(make_user):
    user = make_user("vendor")
    user.company = "Hamilton Medical"
    user.save()
    return user


@pytest.fixture
def manager(make_user):
    return make_user("manager")


def repair(asset, *, tech=None, vendor="", start=True, **fields):
    wo = create_work_order(asset=asset, type=WoType.REPAIR, priority=Priority.HIGH, problem="Low tidal volume alarm", **fields)
    if tech is not None or vendor:
        assign(wo, technician=tech, vendor_name=vendor)
    if start:
        change_status(wo, WoStatus.IN_PROGRESS)
    return wo


def done(wo, **kwargs):
    complete_work_order(wo, **{"resolution": "Replaced the flow sensor", **CHECKED, **kwargs})
    wo.refresh_from_db()
    return wo


def reopened(wo):
    wo.refresh_from_db()
    change_status(wo, WoStatus.IN_PROGRESS)
    return wo


def judged(asset, finding, by):
    """A vendor's repair the facility completed with `finding`, reopened."""
    return reopened(done(repair(asset, vendor=VENDOR), repair_finding=finding, by=by))


def refused(wo, **kwargs) -> dict:
    """complete_work_order refused, and nothing saved."""
    before = WorkOrder.objects.get(pk=wo.pk)
    with pytest.raises(ValidationError) as e:
        complete_work_order(wo, **{"resolution": "Replaced the flow sensor", **CHECKED, **kwargs})
    wo.refresh_from_db()
    assert (wo.status, wo.repair_finding, wo.repair_check) == (before.status, before.repair_finding, before.repair_check)
    return {k: " ".join(v) for k, v in e.value.message_dict.items()}


def pm_failure_recorded_on(open_repair, tech, by):
    """A PM on the open repair's device completed as failed with "Open a new repair" cleared (open_repair=False): completion records
    the failure on that repair and names the PM as what it follows up, so findings.found_on_pm is true for it now."""
    asset = open_repair.asset
    dm = asset.device_model
    if dm.pm_procedure_id is None:
        dm.pm_procedure = PmProcedure.objects.create(code=f"PM-{dm.model}"[:40], name=f"{dm.model} PM", checklist=["Inspect", "Alarm check"])
        dm.save()
    pm = create_work_order(asset=asset, type=WoType.PM, priority=Priority.NORMAL, problem="Scheduled preventive maintenance")
    assign(pm, technician=tech)
    change_status(pm, WoStatus.IN_PROGRESS)
    done_ = complete_work_order(pm, pm_result=PmResult.FAIL, results=[{"result": "fail"}, {"result": "pass"}], open_repair=False, by=by)
    assert done_.follow_up is None and done_.repair.pk == open_repair.pk
    open_repair.refresh_from_db()
    assert open_repair.follow_up_of_id == pm.pk and findings.found_on_pm(open_repair)
    return pm


# --- the API's doors ------------------------------------------------------------------------------------------------------------


def api_get(client, wo, **headers) -> dict:
    r = client.get(f"{API}{wo.id}/", **headers)
    assert r.status_code == 200, r.content
    return r.json()


def api_rows(client, **headers) -> dict:
    r = client.get(API, **headers)
    assert r.status_code == 200, r.content
    body = r.json()
    return {row["id"]: row for row in (body["results"] if isinstance(body, dict) else body)}


def transition(client, wo, body=None, **headers):
    data = {"status": "completed", "resolution": "Replaced the flow sensor", "repair_check": "passed", "results": POST_REPAIR_ALL_PASS,
            **(body or {})}
    return client.post(f"{API}{wo.id}/transition/", data, content_type="application/json", **headers)


def api_refused(client, wo, body=None, **headers) -> dict:
    before = WorkOrder.objects.get(pk=wo.pk)
    r = transition(client, wo, body, **headers)
    assert r.status_code == 400, r.content
    wo.refresh_from_db()
    assert (wo.status, wo.repair_finding) == (before.status, before.repair_finding)
    return r.json()


def finding_part(checklist) -> dict:
    return {k: checklist[k] for k in ("findings", "found_on_pm", "finding_required")}


def hints_of(checklist) -> dict:
    return {f["value"]: f["hint"] for f in checklist["findings"]}


# --- the web's doors ------------------------------------------------------------------------------------------------------------


def modal(client, wo) -> str:
    r = client.get(f"/work-orders/{wo.number}/complete/", **HX)
    assert r.status_code == 200
    return r.content.decode()


def drawer(client, wo) -> str:
    r = client.get(f"/work-orders/{wo.number}/", **HX)
    assert r.status_code == 200
    return r.content.decode()


def finding_error(body: str) -> str:
    m = re.search(r'<span class="err" id="wc-repair_finding_error">(.*?)</span>', body)
    return html.unescape(m.group(1)) if m else ""


def offered(body: str) -> list[str]:
    m = re.search(r'<select id="wc-repair_finding" name="repair_finding".*?</select>', body, re.S)
    return [v for v in re.findall(r'<option value="([^"]*)"', m.group(0)) if v] if m else []


def modal_hints(body: str) -> dict:
    """The live hint under the select, per value."""
    return {v: html.unescape(t) for v, t in re.findall(r'<span class="wf-hint[^"]*" data-for="([a-z_]+)">([^<]*)</span>', body)}


def modal_means(body: str) -> tuple[str, dict]:
    """"What each means": the order to decide by, and each offered choice's hint."""
    part = body[body.index('<details class="wf-means">'):]
    part = part[:part.index("</details>")]
    order = html.unescape(re.search(r'<p class="hint">([^<]*)</p>', part).group(1))
    return order, {html.unescape(dt): html.unescape(dd) for dt, dd in re.findall(r"<dt>([^<]*)</dt><dd>([^<]*)</dd>", part)}


# --- 1. one answer whatever a vendor's account sends --------------------------------------------------------------------------


@pytest.mark.parametrize("stored", JUDGMENTS)
def test_a_vendors_every_guess_gets_one_answer_while_the_facilitys_finding_holds(vent, manager, vendor_user, stored):
    wo = judged(vent, stored, manager)
    assert findings.facility_holds(wo, vendor_user, investigation=False, from_pm=False)
    assert not findings.facility_holds(wo, manager, investigation=False, from_pm=False)  # the facility's own account chooses
    # a completion refused anyway (no resolution): left out, None, or blank adds no finding refusal (the finding is kept) ...
    base = refused(wo, resolution="", repair_finding=None, by=vendor_user)
    assert "repair_finding" not in base and "resolution" in base
    assert refused(wo, resolution="", repair_finding="", by=vendor_user) == base
    # ... and every value sent adds the same one, the stored value among them: none tells what the facility recorded
    answers = {guess: refused(wo, resolution="", repair_finding=guess, by=vendor_user) for guess in GUESSES}
    assert answers == {guess: {**base, "repair_finding": findings.FACILITY_RECORDED} for guess in GUESSES}
    assert refused(wo, repair_finding=stored, by=vendor_user) == {"repair_finding": findings.FACILITY_RECORDED}
    assert done(wo, repair_finding=None, by=vendor_user).repair_finding == stored  # sent nothing: kept as the facility recorded it


def test_over_the_api_a_vendors_guesses_all_get_the_same_400(client, vent, manager, vendor_user):
    wo = judged(vent, F.SERVICE_INDUCED, manager)
    client.force_login(vendor_user)
    data = api_get(client, wo)
    assert data["repair_finding"] is None
    assert (data["completion_checklist"]["findings"], data["completion_checklist"]["finding_required"]) == ([], False)
    left_out = api_refused(client, wo, {"resolution": ""})
    assert list(left_out) == ["resolution"]
    bodies = {guess: api_refused(client, wo, {"resolution": "", "repair_finding": guess}) for guess in GUESSES}
    assert bodies == {guess: {**left_out, "repair_finding": [findings.FACILITY_RECORDED]} for guess in GUESSES}
    r = transition(client, wo, {"repair_finding": ""})
    assert r.status_code == 200 and WorkOrder.objects.get(pk=wo.pk).repair_finding == F.SERVICE_INDUCED


def test_the_modal_refuses_the_stored_value_sent_back_like_any_other(client, vent, manager, vendor_user):
    wo = judged(vent, F.USE_ERROR, manager)
    client.force_login(vendor_user)
    url = f"/work-orders/{wo.number}/complete/"
    # the stored value posted (never shown to them): refused, nothing saved
    r = client.post(url, checked_post(resolution="Replaced the flow sensor", repair_finding="use_error"), **HX)
    assert finding_error(r.content.decode()) == findings.FACILITY_RECORDED
    assert WorkOrder.objects.get(pk=wo.pk).status == WoStatus.IN_PROGRESS
    # with the resolution left blank too: the same finding refusal for every guess, none for the blank
    errors = {guess: finding_error(client.post(url, checked_post(resolution="", repair_finding=guess), **HX).content.decode())
              for guess in (*GUESSES, "")}
    assert errors == {**{guess: findings.FACILITY_RECORDED for guess in GUESSES}, "": ""}
    r = client.post(url, checked_post(resolution="Replaced the flow sensor", repair_finding=""), **HX)
    assert r["HX-Retarget"] == "#drawer" and WorkOrder.objects.get(pk=wo.pk).repair_finding == F.USE_ERROR


# --- 2. the checklist once the repair's limits refuse the facility's finding ------------------------------------------------------


def test_the_checklist_offers_a_vendor_its_observations_once_the_limits_refuse_the_facilitys_finding(client, vent, techs, manager,
                                                                                                        vendor_user):
    wo = judged(vent, F.PM_PREVENTABLE, manager)
    client.force_login(vendor_user)
    checklist = api_get(client, wo)["completion_checklist"]
    assert (checklist["findings"], checklist["found_on_pm"], checklist["finding_required"]) == ([], False, False)  # the facility's
    pm_failure_recorded_on(wo, techs["dana"], manager)
    assert not findings.facility_holds(wo, vendor_user, investigation=False, from_pm=True)
    checklist = api_get(client, wo)["completion_checklist"]
    assert finding_part(checklist) == {"findings": [{"value": v, "label": F(v).label, "hint": findings.hint(v, OBSERVED)} for v in OBSERVED],
                                       "found_on_pm": True, "finding_required": True}
    assert api_rows(client)[str(wo.id)]["completion_checklist"] == checklist  # a list's page reads the same
    # the transition agrees: left out, it names the values the checklist lists; one of them completes it
    assert api_refused(client, wo) == {"repair_finding": [VENDOR_SEND]}
    assert all(f'"{v}"' in VENDOR_SEND for v in OBSERVED)
    r = transition(client, wo, {"repair_finding": "wear"})
    assert r.status_code == 200 and WorkOrder.objects.get(pk=wo.pk).repair_finding == F.WEAR


# --- 3. the API's words while the facility's finding holds -----------------------------------------------------------------------


def test_the_apis_words_never_name_a_vendors_observations_while_the_facilitys_finding_holds(client, vent, pump, techs, manager,
                                                                                           vendor_user):
    held = judged(vent, F.USE_ERROR, manager)
    leave = [findings.FACILITY_RECORDED + " Leave repair_finding out."]
    for refusal in (findings.REQUIRED, findings.NOT_OFFERED):
        errors = {"repair_finding": [refusal], "resolution": ["Say what was found and done."]}
        assert _finding_words(held, vendor_user, errors, IN_CHECKLIST) == {**errors, "repair_finding": leave}, refusal
    # the facility's account, and a vendor's once the limits refuse the stored finding: the values to send, named
    assert _finding_words(held, manager, {"repair_finding": [findings.NOT_OFFERED]}, IN_CHECKLIST)["repair_finding"][0].startswith(
        "Send repair_finding, what the repair found: one of")
    stale = judged(pump, F.PM_PREVENTABLE, manager)
    pm_failure_recorded_on(stale, techs["tom"], manager)
    assert _finding_words(stale, vendor_user, {"repair_finding": [findings.NOT_OFFERED]}, IN_CHECKLIST) == {"repair_finding": [VENDOR_SEND]}
    # over the API, a typo from the vendor's account: never told to send an observation the service would refuse
    client.force_login(vendor_user)
    body = api_refused(client, held, {"repair_finding": "worn"})
    assert list(body) == ["repair_finding"] and body["repair_finding"][0].startswith(findings.FACILITY_RECORDED)
    assert not any(f'"{v}"' in body["repair_finding"][0] for v in OBSERVED)


# --- 4. the drawer's reopened note ------------------------------------------------------------------------------------------


def test_the_drawer_says_kept_only_while_this_user_may_keep_the_finding(client, vent, techs, manager):
    wo = judged(vent, F.PM_PREVENTABLE, manager)
    client.force_login(manager)
    assert REOPENED_KEPT in drawer(client, wo)
    pm_failure_recorded_on(wo, techs["dana"], manager)  # the PM caught it: "PM could have caught it" is no longer allowed on it
    body = drawer(client, wo)
    assert "Reopened. When last completed this repair was recorded as checked and ready for use" in body
    assert "kept unless another is chosen" not in body and "What was found: PM could have caught it" not in body
    # the modal and the service say the same: chosen again
    form = modal(client, wo)
    assert "pm_preventable" not in offered(form) and '<option value="pm_preventable" selected>' not in form
    r = client.post(f"/work-orders/{wo.number}/complete/", checked_post(resolution="Replaced the flow sensor", repair_finding=""), **HX)
    assert finding_error(r.content.decode()) == findings.FOUND_ON_PM
    assert WorkOrder.objects.get(pk=wo.pk).status == WoStatus.IN_PROGRESS


# --- 5. the AEM tab's list -------------------------------------------------------------------------------------------------------


def stored_repair(asset, opened, completed, status) -> WorkOrder:
    """A repair stored as done on `completed` with no finding (completed before Cadence recorded findings, or imported)."""
    wo = create_work_order(asset=asset, type=WoType.REPAIR, priority="normal", problem="Alarm fault", opened_on=opened)
    WorkOrder.objects.filter(pk=wo.pk).update(status=status, started_on=opened, completed_on=completed)
    wo.refresh_from_db()
    return wo


@pytest.fixture
def monitor(ctx, dept, today):
    """A model with four years of history on two devices: the 25 latest of its repairs with no finding are closed, an older one is only
    completed."""
    dm = DeviceModel.objects.create(manufacturer="Acme", model="MX750", description="MX750 device", category="Monitors",
                                    risk_class=RiskClass.HIGH, oem_pm_interval_months=12)
    m1, _m2 = (Asset.objects.create(tag=tag, device_model=dm, department=dept, installed_on=add_months(today, -48),
                                    next_pm_on=today + 30 * DAY) for tag in ("M-1", "M-2"))
    closed = [stored_repair(m1, today - (20 + n) * DAY, today - (19 + n) * DAY, WoStatus.CLOSED) for n in range(views_aem.RECORD_SHOWN)]
    older = stored_repair(m1, today - 300 * DAY, today - 299 * DAY, WoStatus.COMPLETED)
    return {"dm": dm, "closed": closed, "older": older}


def aem_tab(client, dm) -> str:
    r = client.get(f"/pm/models/{dm.pk}/?tab=aem", **HX)
    assert r.status_code == 200
    return r.content.decode()


def test_the_aem_list_shows_only_the_repairs_this_user_may_record(client, ctx, make_user, monitor):
    dm, closed, older = monitor["dm"], monitor["closed"], monitor["older"]
    role = Role.objects.create(name="AEM reviewer", slug="aem-reviewer")
    role.set_levels({Module.PM: Level.APPROVE, Module.WORKORDERS: Level.EDIT, Module.EQUIPMENT: Level.VIEW})
    reviewer = User.objects.create_user(username="reviewer@riverside.example", password="Test-Pass-2026-x", tenant=ctx, role=role)
    client.force_login(reviewer)
    body = aem_tab(client, dm)
    # the one they may record is listed, though 25 closed ones are later; the closed ones are counted, never listed
    assert "Record what was found <span>1 completed repair with none</span>" in body
    assert f'name="number" value="{older.number}"' in body and body.count('<form class="aem-rec-f"') == 1
    assert not any(wo.number in body for wo in closed) and "Closed: recording needs Work orders Approve" not in body
    assert "25 closed repairs with none are not listed: recording one needs Work orders Approve." in body
    assert "recording one brings the next" not in body
    r = client.post(f"/pm/models/{dm.pk}/aem/finding/", {"number": older.number, "repair_finding": F.WEAR}, **HX)
    assert r.status_code == 200 and WorkOrder.objects.get(pk=older.pk).repair_finding == F.WEAR
    # none left they may record: the section still says the closed ones need Approve, with nothing to record
    body = aem_tab(client, dm)
    assert "25 closed repairs with none are not listed: recording one needs Work orders Approve." in body
    assert '<form class="aem-rec-f"' not in body
    # Work orders Approve: the closed ones are theirs to record, listed, and nothing is said not listed
    client.force_login(make_user("manager"))
    body = aem_tab(client, dm)
    assert "Record what was found <span>25 completed repairs with none</span>" in body and "not listed" not in body
    assert body.count('<form class="aem-rec-f"') == views_aem.RECORD_SHOWN and all(wo.number in body for wo in closed)


# --- 6. the hints and the order name only what the door offers -------------------------------------------------------------------


def test_hint_and_order_words_name_only_the_choices_offered():
    # every choice offered: the words from before
    assert findings.hint(F.BATTERY, F.values) == FULL_BATTERY and findings.order_words(F.values) == FULL_ORDER == findings.ORDER_WORDS
    assert findings.hint(F.BATTERY, IN_HOUSE) == FULL_BATTERY and findings.order_words(IN_HOUSE) == FULL_ORDER
    # a vendor's account: neither of the facility's judgments
    assert findings.hint(F.BATTERY, OBSERVED) == SHORT_BATTERY and findings.order_words(OBSERVED) == VENDOR_ORDER
    # a failed PM's repair: never "PM could have caught it"
    from_pm = [v for v in IN_HOUSE if v != F.PM_PREVENTABLE]
    assert findings.hint(F.BATTERY, from_pm) == SHORT_BATTERY and findings.order_words(from_pm) == FROM_PM_ORDER
    # the other hints whatever is offered; one choice or none in the order: no order to decide by
    assert all(findings.hint(v, []) == findings.HINTS[v] for v in F.values if v != F.BATTERY)
    assert findings.order_words([F.WEAR]) == findings.order_words([]) == ""


def test_the_modal_words_the_hints_and_the_order_from_this_users_choices(client, vent, pump, techs, manager, vendor_user):
    in_house = repair(vent, tech=techs["dana"])
    client.force_login(manager)
    body = modal(client, in_house)
    assert offered(body) == IN_HOUSE and modal_hints(body)[F.BATTERY] == FULL_BATTERY
    assert modal_means(body) == (FULL_ORDER, {F(v).label: findings.hint(v, IN_HOUSE) for v in IN_HOUSE})
    # a failed PM's repair, for the same account
    pm = create_work_order(asset=pump, type=WoType.PM, priority=Priority.NORMAL, problem="Scheduled PM", assigned_to=techs["tom"])
    from_pm = create_work_order(asset=pump, type=WoType.REPAIR, priority=Priority.HIGH, problem="PM failed", assigned_to=techs["tom"],
                                follow_up_of=pm)
    change_status(from_pm, WoStatus.IN_PROGRESS)
    body = modal(client, from_pm)
    assert "pm_preventable" not in offered(body) and modal_hints(body)[F.BATTERY] == SHORT_BATTERY
    assert modal_means(body)[0] == FROM_PM_ORDER and "PM could have caught it" not in html.unescape(body)
    # a vendor's account: its observations, and words naming nothing else
    vendor_job = repair(vent, vendor=VENDOR)
    client.force_login(vendor_user)
    body = modal(client, vendor_job)
    assert offered(body) == OBSERVED and modal_hints(body)[F.BATTERY] == SHORT_BATTERY
    assert modal_means(body) == (VENDOR_ORDER, {F(v).label: findings.HINTS[v] for v in OBSERVED})
    text = html.unescape(body)
    assert "PM could have caught it" not in text and "caused by earlier service" not in text.lower()


def test_the_checklist_words_the_hints_from_this_users_choices(client, dept, vent, pump, techs, manager, vendor_user):
    in_house = repair(vent, tech=techs["dana"])
    vendor_job = repair(pump, vendor=VENDOR)
    another = Asset.objects.create(tag="CE-10003", device_model=vent.device_model, department=dept)
    from_pm_job = judged(another, F.WEAR, manager)  # a vendor repair reopened, then found on a PM
    pm_failure_recorded_on(from_pm_job, techs["dana"], manager)
    client.force_login(manager)
    assert hints_of(api_get(client, in_house)["completion_checklist"])[F.BATTERY] == FULL_BATTERY
    assert hints_of(api_get(client, vendor_job)["completion_checklist"])[F.BATTERY] == FULL_BATTERY  # the facility on vendor service
    hints = hints_of(api_get(client, from_pm_job)["completion_checklist"])
    assert F.PM_PREVENTABLE not in hints and hints[F.BATTERY] == SHORT_BATTERY
    client.force_login(vendor_user)
    for wo in (vendor_job, from_pm_job):
        hints = hints_of(api_get(client, wo)["completion_checklist"])
        assert list(hints) == OBSERVED and hints[F.BATTERY] == SHORT_BATTERY, wo.number
        assert not any("PM could have caught it" in h or "earlier service" in h for h in hints.values()), wo.number


def test_the_aem_tab_keeps_the_words_from_before(client, make_user, monitor):
    """Its legend explains every finding for the facility (no scoped user reaches the list): the full battery hint and order."""
    client.force_login(make_user("manager"))
    body = html.unescape(aem_tab(client, monitor["dm"]))
    part = body[body.index('<details class="aem-rec-help"'):]
    part = part[:part.index("</details>")]
    assert f'<div class="hint" style="margin-top:4px">{FULL_ORDER}</div>' in part
    assert f'<dt>Battery failed</dt><dd style="white-space:normal">{FULL_BATTERY}</dd>' in part


# --- what the fixes left ---------------------------------------------------------------------------------------------------------


def test_a_vendors_blank_never_tells_a_stale_finding_from_none(client, vent, pump, techs, manager, vendor_user):
    stale = judged(vent, F.PM_PREVENTABLE, manager)
    pm_failure_recorded_on(stale, techs["dana"], manager)
    none_stored = judged(pump, F.WEAR, manager)
    WorkOrder.objects.filter(pk=none_stored.pk).update(repair_finding="")  # nothing stored (completed before findings, then reopened)
    pm_failure_recorded_on(none_stored, techs["tom"], manager)
    client.force_login(vendor_user)
    assert offered(modal(client, stale)) == offered(modal(client, none_stored)) == OBSERVED  # the modal shows them alike
    answers = [finding_error(client.post(f"/work-orders/{wo.number}/complete/", checked_post(resolution="Replaced the flow sensor",
                                                                                           repair_finding=""), **HX).content.decode())
               for wo in (stale, none_stored)]
    assert answers[0] and answers[0] == answers[1], answers  # today: NOT_OFFERED for the stale one, REQUIRED for none


# --- under the policies -----------------------------------------------------------------------------------------------------------


@needs_postgres
def test_the_vendors_doors_under_the_policies(client, ctx, vent, pump, techs, manager, vendor_user):
    """As the runtime role, by the vendor's token: the facility's finding held (no choices, one answer for every guess, kept when left
    out), and one the repair's limits refuse offered again from the observations. Rows read back inside the facility."""
    held = judged(vent, F.USE_ERROR, manager)
    stale = judged(pump, F.PM_PREVENTABLE, manager)
    pm_failure_recorded_on(stale, techs["tom"], manager)
    auth = {"HTTP_AUTHORIZATION": f"Token {Token.objects.create(user=vendor_user).key}"}
    as_app_role()
    checklist = api_get(client, held, **auth)["completion_checklist"]
    assert (checklist["findings"], checklist["finding_required"]) == ([], False)
    for guess in (F.USE_ERROR, F.WEAR, "worn_out"):
        r = transition(client, held, {"repair_finding": guess}, **auth)
        assert r.status_code == 400 and r.json() == {"repair_finding": [findings.FACILITY_RECORDED]}, guess
    checklist = api_get(client, stale, **auth)["completion_checklist"]
    assert ([f["value"] for f in checklist["findings"]], checklist["found_on_pm"], checklist["finding_required"]) == (OBSERVED, True, True)
    assert hints_of(checklist)[F.BATTERY] == SHORT_BATTERY
    assert transition(client, held, **auth).status_code == 200
    assert transition(client, stale, {"repair_finding": "battery"}, **auth).status_code == 200
    with tenant_context(ctx):
        assert WorkOrder.objects.get(pk=held.pk).repair_finding == F.USE_ERROR
        assert WorkOrder.objects.get(pk=stale.pk).repair_finding == F.BATTERY
