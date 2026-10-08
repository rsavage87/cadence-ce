"""
Slice 28 review fixes on the web screens (apps/web/views_incidents.py and the screens the hold reaches), one test per finding through
the test client: [8] the Record incident modal re-rendered with what was typed (a device picked) never hands its due date text where a
date belongs; [9] a closed or recorded-in-error incident's own address keeps the page head's Record incident; [10] the device drawer's
PM tab and the route sheets say a held device's PM waits for the incident's release, never that nobody is credentialed; [11] releasing
a hold tells My work and the Work orders list (wo-changed); [12] Auto-assign week names the held devices even when other PMs are
already with someone. Then Decide with who was affected as now known, and the recorded-in-error modal's rule and refusals.
"""
import json
import re
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.accounts.models import Level, Module, Role
from apps.core.workdays import add_work_days
from apps.equipment.models import AssetStatus
from apps.incidents import services as inc
from apps.incidents.models import Affected, Basis, DecidedBy, Finding, Outcome, Release, Status
from apps.pm.services import WeekAssignment
from apps.web.views_pm_week import assigned_message
from apps.workorders.models import Priority, WorkOrder, WoStatus, WoType
from apps.workorders.services import change_status, create_work_order

HX = {"HTTP_HX_REQUEST": "true"}


@pytest.fixture
def today(ctx):
    return timezone.localdate()


@pytest.fixture
def sign_in(client, make_user):
    made = {}

    def _as(slug):
        if slug not in made:
            made[slug] = make_user(slug)
        client.force_login(made[slug])
        return made[slug]

    return _as


def record(asset, **kwargs):
    kwargs.setdefault("outcome", Outcome.UNKNOWN)
    kwargs.setdefault("affected", Affected.PATIENT)
    return inc.record_incident(asset=asset, **kwargs)


def triggers(r) -> dict:
    out = {}
    for h in ("HX-Trigger", "HX-Trigger-After-Settle"):
        if h in r:
            out.update(json.loads(r[h]))
    return out


def toast_of(r) -> str:
    return triggers(r).get("toast", {}).get("value", "")


def html(r) -> str:
    return r.content.decode()


def custom_user(make_user, slug, levels):
    """An account with a custom role holding only `levels` (every other module None)."""
    role = Role.objects.create(name=slug.title(), slug=slug)
    role.set_levels(levels)
    user = make_user("analyst", username=f"{slug}@riverside.example")  # its role is replaced
    user.role = role
    user.save()
    return user


def input_value(body: str, name: str) -> str:
    m = re.search(rf'<input[^>]*name="{name}"[^>]*>', body)
    assert m, name
    v = re.search(r'value="([^"]*)"', m.group(0))
    return v.group(1) if v else ""


# --- [8] the record modal re-rendered with what was typed --------------------------------------------------------------------------

def test_picking_a_device_after_typing_dates_renders_the_modal_and_its_due_date(client, sign_in, vent, today):
    sign_in("director")
    base = {"asset": vent.tag, "shown": "1", "outcome": Outcome.UNKNOWN, "affected": Affected.PATIENT, "hold": "on", "investigation": "new"}
    # the finding's probe: today's date as text, the first-knew day empty
    r = client.get("/incidents/new/", {**base, "occurred_on": today.isoformat(), "aware_on": ""}, **HX)
    assert r.status_code == 200 and f"10 work days after {today:%b} {today.day}" in html(r)
    assert input_value(html(r), "occurred_on") == today.isoformat()
    # a first-knew day typed: compared and counted as a date
    aware = today - timedelta(days=1)
    r = client.get("/incidents/new/", {**base, "occurred_on": (today - timedelta(days=2)).isoformat(), "aware_on": aware.isoformat()}, **HX)
    assert r.status_code == 200 and f"10 work days after {aware:%b} {aware.day}" in html(r)
    assert input_value(html(r), "aware_on") == aware.isoformat()
    r = client.get("/incidents/new/", {**base, "occurred_on": today.isoformat(), "aware_on": (today - timedelta(days=3)).isoformat()}, **HX)
    assert r.status_code == 200 and "Clinical staff cannot have known before it happened." in html(r)
    # cleared, half typed, or not a date: the default takes its place (today; the first-knew day empty)
    for occurred, aware_text in (("", ""), ("2026-10-0", "soon"), ("garbage", today.isoformat() + "x")):
        r = client.get("/incidents/new/", {**base, "occurred_on": occurred, "aware_on": aware_text}, **HX)
        assert r.status_code == 200, (occurred, aware_text)
        body = html(r)
        assert input_value(body, "occurred_on") == today.isoformat() and input_value(body, "aware_on") == ""
        assert f"10 work days after {today:%b} {today.day}" in body


def test_the_modal_keeps_only_choices_it_offers(client, sign_in, vent, pump, today):
    """A choice not among the field's (or another device's repair as the investigation) falls back to the default."""
    other = create_work_order(asset=pump, type=WoType.REPAIR, priority=Priority.NORMAL, problem="Door latch")
    sign_in("director")
    r = client.get("/incidents/new/", {"asset": vent.tag, "shown": "1", "occurred_on": today.isoformat(), "outcome": "bogus",
                                       "affected": "someone", "hold": "on", "investigation": other.number}, **HX)
    body = html(r)
    assert r.status_code == 200
    assert '<option value="unknown" selected>' in body  # the default outcome
    assert 'value="new" checked' in body and other.number not in body  # the default investigation: a new one


def test_the_modal_opened_from_a_device_drawer_takes_another_device_with_typed_dates(client, sign_in, vent, pump, today):
    sign_in("manager")
    day = (today - timedelta(days=1)).isoformat()
    r = client.get("/incidents/new/", {"asset": pump.tag, "shown": "1", "occurred_on": day, "aware_on": day, "outcome": Outcome.DEATH,
                                       "affected": Affected.STAFF, "hold": "on", "investigation": "new"}, **HX)
    body = html(r)
    assert r.status_code == 200 and pump.tag in body and "the FDA and the manufacturer" in body


# --- [9] the full page of a closed or recorded-in-error incident -------------------------------------------------------------------

def test_a_closed_or_in_error_incidents_address_keeps_record_incident(client, sign_in, vent, pump, today):
    closed = record(vent, outcome=Outcome.NO_HARM, affected=Affected.NONE, hold=False, open_work_order=False)
    inc.record_finding(closed, Finding.MET_SPECS)
    inc.close(closed)
    in_error = record(pump, outcome=Outcome.NO_HARM, affected=Affected.NONE, hold=False, open_work_order=False)
    inc.recorded_in_error(in_error)
    sign_in("director")
    assert 'hx-get="/incidents/new/"' in html(client.get("/incidents/"))
    for i in (closed, in_error):
        body = html(client.get(f"/incidents/{i.number}/"))  # the address opened directly: the page with the drawer open
        assert '<aside class="drawer open"' in body and 'hx-get="/incidents/new/"' in body, i.number
        assert f"/incidents/{i.number}/facts/" not in body, i.number  # its drawer still offers no Edit facts
    open_one = record(vent, outcome=Outcome.NO_HARM, affected=Affected.NONE, hold=False, open_work_order=False)
    body = html(client.get(f"/incidents/{open_one.number}/"))
    assert 'hx-get="/incidents/new/"' in body and f"/incidents/{open_one.number}/facts/" in body


# --- [10] the PM tab and the route sheets for a held device ------------------------------------------------------------------------

NOBODY_WORDS = ("No credentialed technician", "No active technician is credentialed")


def test_the_pm_tab_says_a_held_devices_pm_waits_for_the_release(client, sign_in, vent, today, techs):
    sign_in("manager")
    assert "Dana Whitfield, suggested" in html(client.get(f"/equipment/{vent.tag}/?tab=pm", **HX))
    record(vent)
    body = html(client.get(f"/equipment/{vent.tag}/?tab=pm", **HX))
    assert "Held: waits for the incident's release" in body and "nobody may start its PM until the incident releases it" in body
    assert "Dana Whitfield" not in body and not any(w in body for w in NOBODY_WORDS)


def test_the_pm_tab_of_a_held_device_with_an_assigned_pm(client, sign_in, vent, today, techs):
    pm = create_work_order(asset=vent, type=WoType.PM, priority=Priority.NORMAL, problem="PM", assigned_to=techs["dana"])
    record(vent)
    sign_in("manager")
    body = html(client.get(f"/equipment/{vent.tag}/?tab=pm", **HX))
    assert "Held: waits for the incident's release" in body and f"the next PM is on {pm.number}" in body


def _sheet(body: str, title: str) -> str:
    """The route sheet whose heading is `title`, or ""."""
    for section in re.findall(r'<section class="sheet rs">(.*?)</section>', body, re.S):
        if f"<h1>{title}</h1>" in section:
            return section
    return ""


def test_route_sheets_put_a_held_device_on_its_own_sheet(client, sign_in, vent, pump, today, techs, monkeypatch):
    record(vent)
    vent.refresh_from_db()
    sign_in("manager")
    body = html(client.get(f"/print/route-sheets/?day={vent.next_pm_on.isoformat()}"))
    held = _sheet(body, "Held for an incident investigation")
    assert vent.tag in held and "held as evidence for an incident investigation" in held and "Its PM waits" in held
    assert not any(w in body for w in NOBODY_WORDS)
    # the week's sheets the same way
    vent.next_pm_on = today + timedelta(days=2)
    vent.save()
    monkeypatch.setattr("apps.web.views_pm._today", lambda: today)
    body = html(client.get("/print/route-sheets/?scope=week"))
    assert vent.tag in _sheet(body, "Held for an incident investigation") and not any(w in body for w in NOBODY_WORDS)


def test_a_held_devices_assigned_pm_is_not_on_its_technicians_round(client, sign_in, vent, today, techs):
    pm = create_work_order(asset=vent, type=WoType.PM, priority=Priority.NORMAL, problem="PM", assigned_to=techs["dana"])
    sign_in("manager")
    day = vent.next_pm_on.isoformat()
    assert vent.tag in _sheet(html(client.get(f"/print/route-sheets/?day={day}")), "Dana Whitfield")  # before the hold: her round
    record(vent)
    body = html(client.get(f"/print/route-sheets/?day={day}"))
    held = _sheet(body, "Held for an incident investigation")
    assert vent.tag in held and pm.number in held and "with Dana Whitfield" in held
    assert "<h1>Dana Whitfield</h1>" not in body  # not on her round: nobody may start it


# --- [11] releasing a hold fires wo-changed ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("how", [Release.KEEP_OUT, Release.RETURN_TO_USE])
def test_releasing_a_hold_tells_the_work_order_screens(client, sign_in, vent, today, how):
    i = record(vent, outcome=Outcome.NO_HARM, affected=Affected.NONE)
    if how == Release.RETURN_TO_USE:  # the investigation done first (started, then completed, as the screens do it)
        change_status(i.work_order, WoStatus.IN_PROGRESS)
        change_status(i.work_order, WoStatus.COMPLETED)
    hold = i.holds.get()
    sign_in("manager")
    r = client.post(f"/incidents/{i.number}/holds/{hold.pk}/release/", {"release": how}, **HX)
    assert r["HX-Retarget"] == "#drawer", html(r)
    assert {"wo-changed", "devices-changed", "incidents-changed"} <= set(triggers(r))
    vent.refresh_from_db()
    assert not vent.incident_hold and vent.status == (AssetStatus.IN_SERVICE if how == Release.RETURN_TO_USE else AssetStatus.OUT_OF_SERVICE)


# --- [12] Auto-assign week with held devices and PMs already with someone ------------------------------------------------------------

def test_auto_assign_week_names_held_devices_when_other_pms_are_with_someone(client, sign_in, vent, pump, today, techs, monkeypatch):
    pump.next_pm_on = today + timedelta(days=3)
    pump.save()
    create_work_order(asset=pump, type=WoType.PM, priority=Priority.NORMAL, problem="PM", assigned_to=techs["tom"])
    vent.next_pm_on = today + timedelta(days=2)
    vent.save()
    record(vent)
    monkeypatch.setattr("apps.web.views_pm._today", lambda: today)
    sign_in("manager")
    body = html(client.get("/pm/week/assign/", **HX))
    assert "every PM due this week is already with a technician or the vendor" not in body
    assert "already with a technician or the vendor, or on devices held for an incident investigation." in body
    assert f"{vent.tag} (ICU ventilator, due" in body
    r = client.post("/pm/week/assign/", **HX)
    assert toast_of(r) == ("Nothing to assign: 1 PM due this week is already with a technician or the vendor, and the device due this "
                           "week is held for an incident investigation")


@pytest.mark.parametrize("kw, expected", [
    ({"held": 2, "on_hold": 1}, "Nothing to assign: 2 PMs due this week are already with a technician or the vendor, and the device due "
                                "this week is held for an incident investigation"),
    ({"held": 1, "on_hold": 3}, "Nothing to assign: 1 PM due this week is already with a technician or the vendor, and the 3 devices due "
                                "this week are held for an incident investigation"),
    ({"uncovered": 1, "on_hold": 1}, "Nothing to assign: 1 PM this week still needs a credentialed technician; the device due this week "
                                     "is held for an incident investigation"),
    ({"held": 4}, "Nothing to assign: every PM due this week is already with a technician or the vendor"),
])
def test_assigned_message_never_says_every_pm_is_with_someone_while_one_is_held(kw, expected):
    w = WeekAssignment(start=timezone.localdate(), end=timezone.localdate() + timedelta(days=6), held=kw.get("held", 0),
                       on_hold=[{"asset": None, "has_open_pm": False}] * kw.get("on_hold", 0),
                       uncovered=[{"asset": None, "has_open_pm": True}] * kw.get("uncovered", 0))
    assert assigned_message(w) == expected


# --- Decide with who was affected ------------------------------------------------------------------------------------------------

def _decision(**kw) -> dict:
    return {"decided_on": timezone.localdate().isoformat(), "decided_by": DecidedBy.RISK, **kw}


def test_deciding_again_with_who_was_affected_as_now_known(client, sign_in, vent, today):
    i = record(vent, outcome=Outcome.SERIOUS_INJURY, affected=Affected.OTHER, event_reference="EV-1")
    sign_in("manager")
    modal = html(client.get(f"/incidents/{i.number}/decide/", **HX))
    assert '<option value="other" selected>' in modal and "is decided again here" in modal
    r = client.post(f"/incidents/{i.number}/decide/", _decision(outcome=Outcome.SERIOUS_INJURY, affected=Affected.OTHER, basis=Basis.NOT_PATIENT), **HX)
    assert toast_of(r) == f"{i.number}: decided not reportable"
    i.refresh_from_db()
    assert i.reportable is False and i.report_due_on is None
    facts = html(client.get(f"/incidents/{i.number}/facts/", **HX))
    assert "Decided: change who was affected by deciding again." in facts
    # learned later: a staff member on duty (803.3(t)): decided again, reportable, and the clock runs from the first-knew day
    r = client.post(f"/incidents/{i.number}/decide/", _decision(outcome=Outcome.SERIOUS_INJURY, affected=Affected.STAFF, basis=Basis.MAY_HAVE), **HX)
    assert r["HX-Retarget"] == "#drawer" and toast_of(r) == f"{i.number}: decided reportable"
    i.refresh_from_db()
    assert (i.affected, i.reportable, i.basis) == (Affected.STAFF, True, Basis.MAY_HAVE)
    assert i.report_due_on == add_work_days(i.aware_on, 10)
    assert "Staff member on duty" in html(r)


def test_a_harm_outcome_with_no_one_affected_is_refused_on_who_was_affected(client, sign_in, vent, today):
    i = record(vent, outcome=Outcome.NO_HARM, affected=Affected.NONE, event_reference="EV-2")
    sign_in("manager")
    r = client.post(f"/incidents/{i.number}/decide/", _decision(outcome=Outcome.DEATH, affected=Affected.NONE, basis=Basis.NO_SUGGESTION), **HX)
    body = html(r)
    assert "HX-Retarget" not in r and "Someone was harmed, or may have been: choose who" in body
    err = re.search(r'Who was affected<select.*?</select>.*?<span class="err">(.*?)</span>', body, re.S)
    assert err and "Someone was harmed" in err.group(1)  # on the field it names
    i.refresh_from_db()
    assert (i.outcome, i.reportable) == (Outcome.NO_HARM, None)
    # a near miss that turned out to be a patient's death: decided again with both
    r = client.post(f"/incidents/{i.number}/decide/", _decision(outcome=Outcome.DEATH, affected=Affected.PATIENT, basis=Basis.MAY_HAVE), **HX)
    assert toast_of(r) == f"{i.number}: decided reportable"
    i.refresh_from_db()
    assert (i.outcome, i.affected, i.reportable) == (Outcome.DEATH, Affected.PATIENT, True) and i.report_due_on is not None


# --- Recorded in error: the rule and the service's refusals ------------------------------------------------------------------------

def test_recorded_in_error_needs_equipment_and_work_orders_edit_and_says_so(client, make_user, sign_in, vent, today):
    assert vent.status == AssetStatus.IN_SERVICE  # its status before the hold: recorded in error puts it back in use
    i = record(vent)
    sign_in("manager")
    modal = html(client.get(f"/incidents/{i.number}/in-error/", **HX))
    assert "also needs Equipment Edit" in modal and "needs Work orders Edit" in modal
    risk = custom_user(make_user, "risk", {Module.INCIDENTS: Level.APPROVE, Module.EQUIPMENT: Level.VIEW, Module.WORKORDERS: Level.VIEW})
    client.force_login(risk)
    r = client.post(f"/incidents/{i.number}/in-error/", **HX)
    assert r.status_code == 200 and "HX-Retarget" not in r
    assert f"Marking {i.number} recorded in error puts {vent.tag} back in use: that needs Incidents Approve and Equipment Edit." in html(r)
    safety = custom_user(make_user, "safety", {Module.INCIDENTS: Level.APPROVE, Module.EQUIPMENT: Level.EDIT, Module.WORKORDERS: Level.VIEW})
    client.force_login(safety)
    r = client.post(f"/incidents/{i.number}/in-error/", **HX)
    assert "HX-Retarget" not in r
    assert f"Marking {i.number} recorded in error cancels {i.work_order.number}: that needs Work orders Edit." in html(r)
    i.refresh_from_db()
    vent.refresh_from_db()
    assert i.status == Status.OPEN and vent.incident_hold and i.work_order.status == WoStatus.OPEN  # nothing changed
    sign_in("manager")
    r = client.post(f"/incidents/{i.number}/in-error/", **HX)
    assert r["HX-Retarget"] == "#drawer" and {"wo-changed", "devices-changed"} <= set(triggers(r))
    i.refresh_from_db()
    vent.refresh_from_db()
    assert i.status == Status.IN_ERROR and not vent.incident_hold and vent.status == AssetStatus.IN_SERVICE
    assert WorkOrder.objects.get(pk=i.work_order_id).status == WoStatus.CANCELLED
