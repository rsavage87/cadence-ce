"""
Slice 28, wave 2 A: the incident screens (apps/web/views_incidents.py). The Incidents list (open first by the report's due date, the
clock's rail, the filters) and its CSV (a year's file named for Form FDA 3419); Record incident (the device by tag, the hold, the
investigation adopted from a repair or opened, the due date before saving); the incident drawer, full page or partial, and each of its
actions with its level (technician Edit records, manager Approve decides, analyst View reads, requester and vendor refused); refusals in
the modal; History with the holds. Then the hold everywhere else: the device and work order drawers' banners and Record incident, no
Start or Mark completed on a held device's other work, My work's Held chip, the PM day panel and Auto-assign week, the portal's words,
the Roles tab's notice; and the privacy rule (the outcome, who was affected, and the incident's number never reach a user without
Incidents View, a scoped user above all). The services are tests/test_incident_services.py and tests/test_incident_guards.py.
"""
import json
import re
from datetime import date, timedelta

import pytest
from django.utils import timezone
from incident_fixtures import days_ago
from pg_helpers import as_app_role, needs_postgres

from apps.accounts.models import Level, Module, Role
from apps.core.workdays import add_work_days
from apps.credentials.models import Credential, Scope, Technician
from apps.equipment.models import Asset, AssetStatus
from apps.incidents import services as inc
from apps.incidents.models import Affected, Basis, DecidedBy, Finding, Incident, IncidentHold, Outcome, Release, Status
from apps.web import views_incidents
from apps.web.views_pm_week import assigned_message
from apps.workorders.models import Priority, Urgency, WorkOrder, WoStatus, WoType
from apps.workorders.services import assign, change_status, create_service_request, create_work_order

HX = {"HTTP_HX_REQUEST": "true"}
OUTCOME_WORDS = ["Not known yet", "Serious injury", "treated as serious", "Death", "Injury, not serious", "No harm (malfunction"]
AFFECTED_WORDS = ["Staff member on duty", "Visitor or another person"]


@pytest.fixture
def today(ctx):
    return timezone.localdate()


@pytest.fixture
def users(make_user):
    return {slug: make_user(slug) for slug in ("director", "manager", "technician", "analyst", "requester", "vendor")}


@pytest.fixture
def sign_in(client, users):
    def _as(slug, **fields):
        user = users[slug]
        for k, v in fields.items():
            setattr(user, k, v)
        if fields:
            user.save()
        client.force_login(user)
        return user

    return _as


def record(asset, **kwargs):
    kwargs.setdefault("outcome", Outcome.UNKNOWN)
    kwargs.setdefault("affected", Affected.PATIENT)
    return inc.record_incident(asset=asset, **kwargs)


def triggers(r, header="HX-Trigger") -> dict:
    out = {}
    for h in (header, "HX-Trigger-After-Settle"):
        if h in r:
            out.update(json.loads(r[h]))
    return out


def toast_of(r) -> str:
    return triggers(r).get("toast", {}).get("value", "")


def html(r) -> str:
    return r.content.decode()


def custom_user(make_user, tenant, slug, levels, **fields):
    """An account with a custom role holding only `levels` (every other module None)."""
    role = Role.objects.create(name=slug.title(), slug=slug)
    role.set_levels(levels)
    user = make_user("analyst", username=f"{slug}@riverside.example")  # its role is replaced
    user.role = role
    for k, v in fields.items():
        setattr(user, k, v)
    user.save()
    return user


def recording_post(device, today, **overrides) -> dict:
    data = {"asset": device.tag, "shown": "1", "occurred_on": today.isoformat(), "aware_on": "", "outcome": Outcome.UNKNOWN,
            "affected": Affected.PATIENT, "hold": "on", "investigation": "new", "accessories": "kept", "event_log": "saved",
            "event_reference": "EV-2026-0412"}
    data.update(overrides)
    return {k: v for k, v in data.items() if v is not None}


# --- who may do what --------------------------------------------------------------------------------------------------------------

def test_the_list_and_drawer_are_incidents_view_and_scoped_users_are_refused(client, sign_in, vent, today):
    i = record(vent)
    for slug in ("director", "manager", "technician", "analyst"):
        sign_in(slug)
        assert client.get("/incidents/").status_code == 200, slug
        assert client.get(f"/incidents/{i.number}/", **HX).status_code == 200, slug
        assert client.get(f"/incidents/{i.number}/").status_code == 200, slug
        assert client.get("/incidents/incidents.csv").status_code == 200, slug
    for slug, fields in (("requester", {"department": "ICU"}), ("vendor", {"company": "Hamilton Medical"})):
        sign_in(slug, **fields)
        for url in ("/incidents/", f"/incidents/{i.number}/", "/incidents/incidents.csv", "/incidents/new/"):
            assert client.get(url).status_code == 403, (slug, url)
            assert client.get(url, **HX).status_code == 403, (slug, url)


def test_another_facilitys_incident_is_a_404(client, sign_in, vent, other_tenant):
    sign_in("director")
    assert client.get("/incidents/IN-26-0001/", **HX).status_code == 404
    assert client.get("/incidents/IN-26-0001/decide/", **HX).status_code == 404


def _action_urls(i, hold) -> dict:
    """Each action's URL and the level it needs (views_incidents; the services check the same and more)."""
    base = f"/incidents/{i.number}"
    return {
        f"{base}/facts/": Level.EDIT, f"{base}/finding/": Level.EDIT, f"{base}/hold/": Level.EDIT, f"{base}/investigation/": Level.EDIT,
        "/incidents/new/": Level.EDIT, "/incidents/new/due/": Level.EDIT, "/incidents/devices/?asset_q=CE": Level.EDIT,
        f"{base}/decide/": Level.APPROVE, f"{base}/reports/": Level.APPROVE, f"{base}/close/": Level.APPROVE,
        f"{base}/reopen/": Level.APPROVE, f"{base}/in-error/": Level.APPROVE, f"{base}/holds/{hold.pk}/sent/": Level.APPROVE,
        f"{base}/holds/{hold.pk}/back/": Level.APPROVE, f"{base}/holds/{hold.pk}/release/": Level.APPROVE,
    }


def test_each_action_needs_its_level(client, sign_in, vent, today):
    i = record(vent)
    hold = i.holds.get()
    urls = _action_urls(i, hold)
    for slug, level in (("analyst", Level.VIEW), ("technician", Level.EDIT), ("manager", Level.APPROVE)):
        sign_in(slug)
        for url, needs in urls.items():
            get, post = client.get(url, **HX), client.post(url, {}, **HX)
            if level < needs:
                assert get.status_code in (403, 405) and post.status_code == 403, (slug, url, get.status_code, post.status_code)
            else:
                assert get.status_code in (200, 405) and post.status_code == 200, (slug, url, get.status_code, post.status_code)
    i.refresh_from_db()
    assert i.status == Status.IN_ERROR  # the one empty post that acts (it asks nothing more) was the manager's


def test_the_drawer_offers_each_role_its_own_actions(client, sign_in, vent, today):
    i = record(vent)
    url = f"/incidents/{i.number}/"
    actions = {"Edit facts": "/facts/", "Decide": "/decide/", "Record reports": "/reports/", "Hold another device": "/hold/",
               "Close incident": "/close/", "Recorded in error": "/in-error/", "finding": "/finding/"}
    seen = {}
    for slug in ("analyst", "technician", "manager"):
        sign_in(slug)
        body = html(client.get(url, **HX))
        seen[slug] = {name for name, path in actions.items() if f"/incidents/{i.number}{path}" in body}
    assert seen["analyst"] == set()
    assert seen["technician"] == {"Edit facts", "Hold another device", "finding"}
    assert seen["manager"] == set(actions)


def test_record_incident_is_offered_to_incidents_edit(client, sign_in, vent, today):
    sign_in("analyst")
    assert "Record incident" not in html(client.get("/incidents/"))
    assert "/incidents/new/" not in html(client.get(f"/equipment/{vent.tag}/", **HX))
    sign_in("technician")
    assert "Record incident" in html(client.get("/incidents/"))
    assert f'/incidents/new/?asset={vent.tag}' in html(client.get(f"/equipment/{vent.tag}/", **HX))


# --- the list and its CSV ---------------------------------------------------------------------------------------------------------

def test_the_list_puts_open_incidents_first_by_their_report_due_date(client, sign_in, vent, pump, dept, vent_model, today):
    spare = Asset.objects.create(tag="CE-10003", device_model=vent_model, department=dept)
    later = record(vent, occurred_on=today, hold=False, open_work_order=False)  # due 10 work days from today
    sooner = record(pump, occurred_on=days_ago(5), hold=False, open_work_order=False)  # due 10 work days from 5 days ago
    quiet = record(spare, outcome=Outcome.NO_HARM, affected=Affected.NONE, hold=False, open_work_order=False)  # no clock
    closed = record(spare, outcome=Outcome.NO_HARM, affected=Affected.NONE, occurred_on=days_ago(20), hold=False, open_work_order=False)
    inc.record_finding(closed, Finding.MET_SPECS)
    inc.close(closed)
    wrong = record(spare, outcome=Outcome.NO_HARM, affected=Affected.NONE, hold=False, open_work_order=False)
    inc.recorded_in_error(wrong)
    sign_in("analyst")
    body = html(client.get("/incidents/"))
    order = [n for n in re.findall(r'href="/incidents/(IN-\d\d-\d{4})/"', body)]
    assert order == [sooner.number, later.number, quiet.number, closed.number]  # in error is counted nowhere
    assert "rail warn" in body or "rail info" in body
    r = client.get("/incidents/?status=in_error", HTTP_HX_TARGET="inc-body", **HX)
    part = html(r)
    assert part.lstrip().startswith('<div id="inc-body"') and wrong.number in part and later.number not in part
    assert 'id="inc-summary" hx-swap-oob="true"' in part  # the page head's line comes along
    assert re.findall(r'href="/incidents/(IN-\d\d-\d{4})/"', html(client.get("/incidents/?status=all"))).count(wrong.number) == 1
    assert closed.number in html(client.get("/incidents/?status=closed")) and later.number not in html(client.get("/incidents/?status=closed"))


def test_the_clock_colours_the_card_and_the_drawer_says_it_in_words(client, sign_in, vent, today):
    i = record(vent, occurred_on=days_ago(30), event_reference="EV-1")  # long past due, undecided
    sign_in("analyst")
    body = html(client.get("/incidents/"))
    assert "rail crit" in body and "Report overdue" in body
    drawer = html(client.get(f"/incidents/{i.number}/", **HX))
    assert "if the device cannot be ruled out, report it" in drawer
    due = i.report_due_on
    assert f"{due:%a} {due:%b} {due.day}" in drawer
    words = views_incidents.clock_words(Incident.objects.get(pk=i.pk), today)
    assert words.tone == "crit" and words.rail == "crit"


def test_the_clock_reads_days_left_while_running(vent, today, ctx):
    i = record(vent, occurred_on=today)
    words = views_incidents.clock_words(i, today)
    due = add_work_days(today, 10)
    assert words.text.startswith("Report to the manufacturer (the FDA when the manufacturer cannot be identified) as soon as practicable, "
                                 f"no later than {due:%a} {due:%b} {due.day}")
    assert "(10 work days after" in words.text and "10 work days left" in words.text and words.tone == "info"


def test_the_csv_lists_the_filtered_incidents_and_a_year_is_form_3419(client, sign_in, vent, pump, today):
    i = record(vent, occurred_on=days_ago(3), event_reference="EV-7")
    inc.decide(i, outcome=Outcome.SERIOUS_INJURY, basis=Basis.MAY_HAVE, decided_on=today, decided_by=DecidedBy.RISK)
    inc.record_reports(i, manufacturer_reported_on=today, report_number=f"0123456789-{today.year}-0001")
    j = record(pump, outcome=Outcome.NO_HARM, affected=Affected.NONE, hold=False, open_work_order=False)
    inc.recorded_in_error(j)
    sign_in("analyst")
    r = client.get("/incidents/incidents.csv")
    assert r["Content-Disposition"] == f'attachment; filename="cadence-incidents-{today:%Y-%m-%d}.csv"'
    rows = b"".join(r.streaming_content).decode("utf-8-sig").splitlines()
    assert rows[0] == ("Incident,Device,Model,Occurred,Clinical staff first knew,Outcome,Affected,Decision,Basis,Decided on,Report due,"
                       "Sent to the FDA,Sent to the manufacturer,Report number,Finding,Status")
    assert len(rows) == 2 and rows[1].startswith(f"{i.number},{vent.tag},Hamilton Medical Hamilton-G5,{days_ago(3).isoformat()}")
    assert "Serious injury,Patient,Reportable," in rows[1] and f"0123456789-{today.year}-0001" in rows[1] and rows[1].endswith(",Open")
    r = client.get(f"/incidents/incidents.csv?year={today.year}")
    assert f'filename="cadence-incidents-{today.year}-form-3419-{today:%Y-%m-%d}.csv"' in r["Content-Disposition"]
    assert len(b"".join(r.streaming_content).decode("utf-8-sig").splitlines()) == 2
    page = html(client.get(f"/incidents/?year={today.year}"))
    assert "CE&#x27;s reports for the facility&#x27;s annual report (Form FDA 3419)" in page
    all_rows = b"".join(client.get("/incidents/incidents.csv?status=all").streaming_content).decode("utf-8-sig").splitlines()
    assert len(all_rows) == 3 and any(row.endswith(",Recorded in error") for row in all_rows)


def test_a_year_takes_the_incidents_reported_that_year(vent, ctx, today):
    """An event late in one year reported early the next is in both years: it happened in the first, and its report (its number
    carries the year) is in the second's annual report."""
    i = record(vent, occurred_on=date(today.year - 1, 12, 30), aware_on=date(today.year - 1, 12, 30), event_reference="EV-9", today=today)
    inc.decide(i, outcome=Outcome.SERIOUS_INJURY, basis=Basis.MAY_HAVE, decided_on=date(today.year - 1, 12, 31), decided_by=DecidedBy.RISK,
               today=today)
    inc.record_reports(i, manufacturer_reported_on=date(today.year, 1, 2), report_number=f"0123456789-{today.year}-0003", today=today)
    from apps.web.forms_incidents import IncidentFilters

    for year in (today.year - 1, today.year):
        assert list(views_incidents.filter_incidents(IncidentFilters(status="", year=year))) == [i]
    assert views_incidents._years()[:2] == [today.year, today.year - 1]


# --- Record incident ------------------------------------------------------------------------------------------------------------

def test_recording_from_the_incidents_page_holds_the_device_and_opens_its_investigation(client, sign_in, vent, today):
    sign_in("technician")
    modal = html(client.get("/incidents/new/", **HX))
    assert 'id="inc-asset-q"' in modal and "Keep it as found." in modal and "Your facility's safety event report" in modal
    assert "The 10-work-day clock starts that day, not when CE was told." in modal
    assert "Never a patient&#x27;s name, MRN, or account number." in modal
    picks = html(client.get("/incidents/devices/?asset_q=CE-1000", **HX))
    assert vent.tag in picks and 'hx-include="#inc-form"' in picks
    modal = html(client.get(f"/incidents/new/?asset={vent.tag}&shown=1&hold=on", **HX))
    assert f'name="asset" value="{vent.tag}"' in modal and 'value="new" checked' in modal
    r = client.post("/incidents/new/", recording_post(vent, today), **HX)
    i = Incident.objects.get()
    assert r["HX-Retarget"] == "#drawer" and i.number in html(r)
    t = triggers(r)
    assert {"incidents-changed", "devices-changed", "wo-changed", "modal-close"} <= set(t)
    assert toast_of(r).startswith(f"{i.number} recorded; {vent.tag} held as evidence; {i.work_order.number} is its investigation; report due ")
    vent.refresh_from_db()
    assert vent.incident_hold and i.opened_work_order and i.accessories == "kept" and i.event_reference == "EV-2026-0412"
    assert i.created_by.username == "technician@riverside.example"


def test_the_due_date_shows_before_saving(client, sign_in, today, ctx):
    sign_in("technician")
    d = today.isoformat()
    body = html(client.get(f"/incidents/new/due/?occurred_on={d}&outcome=unknown&affected=patient", **HX))
    due = add_work_days(today, 10)
    assert f"no later than {due:%a} {due:%b} {due.day}" in body and "Not known yet counts as serious" in body
    body = html(client.get(f"/incidents/new/due/?occurred_on={d}&outcome=death&affected=staff", **HX))
    assert "Report to the FDA and the manufacturer" in body
    assert "No report clock: a visitor" in html(client.get(f"/incidents/new/due/?occurred_on={d}&outcome=death&affected=other", **HX))
    assert "No report is due: no one was affected." in html(client.get(f"/incidents/new/due/?occurred_on={d}&outcome=no_harm&affected=none", **HX))
    assert "choose who" in html(client.get(f"/incidents/new/due/?occurred_on={d}&outcome=unknown&affected=none", **HX))
    assert "Choose who was affected" in html(client.get(f"/incidents/new/due/?occurred_on={d}&outcome=unknown", **HX))


def test_recording_refusals_come_back_on_the_modal(client, sign_in, vent, today):
    sign_in("technician")
    r = client.post("/incidents/new/", recording_post(vent, today, affected=Affected.NONE), **HX)
    assert r.status_code == 200 and "HX-Retarget" not in r and "Someone was harmed, or may have been: choose who" in html(r)
    r = client.post("/incidents/new/", recording_post(vent, today, event_reference="Jane Doe"), **HX)
    assert "without spaces" in html(r)
    r = client.post("/incidents/new/", recording_post(vent, today, investigation="none"), **HX)
    assert "Holding the device always comes with an investigation" in html(r)
    r = client.post("/incidents/new/", recording_post(vent, today, asset=""), **HX)
    assert "Choose the device from the list first." in html(r)
    vent.status = AssetStatus.RETIRED
    vent.save()
    r = client.post("/incidents/new/", recording_post(vent, today), **HX)
    assert "record the incident without holding it" in html(r)
    assert not Incident.objects.exists()


def test_a_role_without_equipment_edit_records_without_the_hold(client, make_user, tenant, vent, today):
    user = custom_user(make_user, tenant, "quality", {Module.INCIDENTS: Level.EDIT, Module.EQUIPMENT: Level.VIEW, Module.WORKORDERS: Level.EDIT})
    client.force_login(user)
    modal = html(client.get(f"/incidents/new/?asset={vent.tag}", **HX))
    assert '<input type="checkbox" name="hold" disabled>' in modal and "Holding a device needs Incidents Edit and Equipment Edit." in modal
    r = client.post("/incidents/new/", recording_post(vent, today, hold="on"), **HX)
    assert "Holding a device as evidence needs Incidents Edit and Equipment Edit." in html(r) and not Incident.objects.exists()
    r = client.post("/incidents/new/", recording_post(vent, today, hold=None, investigation="none"), **HX)
    assert r["HX-Retarget"] == "#drawer"
    i = Incident.objects.get()
    assert not i.holds.exists() and i.work_order is None


def test_a_repairs_drawer_records_an_incident_that_adopts_it(client, sign_in, vent, today, techs):
    sr = create_service_request(asset=vent, department=vent.department, problem="Ventilator alarmed and stopped", urgency=Urgency.CRITICAL,
                                requester_name="RN Lee", callback="x4410", tagged_out=True)
    wo = sr.work_order
    assign(wo, technician=techs["dana"])
    sign_in("technician")
    drawer = html(client.get(f"/work-orders/{wo.number}/", **HX))
    assert f"/incidents/new/?work_order={wo.number}" in drawer and "Record incident on this device" in drawer
    modal = html(client.get(f"/incidents/new/?work_order={wo.number}", **HX))
    assert f'name="work_order" value="{wo.number}"' in modal and f'value="{wo.number}" checked' in modal
    assert 'value="new"' not in modal and f'value="{wo.opened_on.isoformat()}"' in modal  # it happened the day it was reported
    n = WorkOrder.objects.count()
    r = client.post("/incidents/new/", recording_post(vent, today, work_order=wo.number, investigation=wo.number,
                                                      occurred_on=wo.opened_on.isoformat()), **HX)
    assert r["HX-Retarget"] == "#drawer"
    i = Incident.objects.get()
    assert i.work_order == wo and not i.opened_work_order and WorkOrder.objects.count() == n
    wo.refresh_from_db()
    assert wo.assigned_to == techs["dana"]  # it keeps its assignee
    drawer = html(client.get(f"/work-orders/{wo.number}/", **HX))
    assert "Record incident on this device" not in drawer and f'href="/incidents/{i.number}/"' in drawer
    assert "Investigation of incident" in drawer and inc.NOTES_PLACEHOLDER.replace("'", "&#x27;") in drawer
    assert 'hx-vals=\'{"to": "in_progress"}\'' in drawer  # the investigation may start


def test_a_scanned_label_picks_its_device_first(client, sign_in, vent, pump, today):
    sign_in("technician")
    picks = html(client.get("/incidents/devices/", {"asset_q": f"https://cadence.example/r/riverside/?asset={pump.tag.lower()}"}, **HX))
    assert picks.count("<button") == 1 and f"<b style=\"font-weight:600\">{pump.tag}</b>" in picks
    picks = html(client.get("/incidents/devices/", {"asset_q": vent.tag}, **HX))
    assert picks.index(vent.tag) < picks.index("</button>")
    assert client.get("/incidents/devices/", {"asset_q": "x"}, **HX).content == b""


def test_holding_without_work_orders_edit_needs_a_request_to_take_over(client, make_user, tenant, vent, today):
    """Equipment Edit holds, but with no repair to take over the hold needs a new work order, which needs Work orders Edit: the hold is
    then off by default, and taking over the device's open repair turns it back on."""
    user = custom_user(make_user, tenant, "safety", {Module.INCIDENTS: Level.EDIT, Module.EQUIPMENT: Level.EDIT, Module.WORKORDERS: Level.VIEW})
    client.force_login(user)
    modal = html(client.get(f"/incidents/new/?asset={vent.tag}", **HX))
    assert "checked" not in re.search(r'<input type="checkbox" name="hold"[^>]*>', modal).group(0)
    assert 'value="new" disabled' in modal and 'value="none" checked' in modal
    wo = create_work_order(asset=vent, type=WoType.REPAIR, priority=Priority.HIGH, problem="Alarm")
    modal = html(client.get(f"/incidents/new/?asset={vent.tag}", **HX))
    assert 'checked' in re.search(r'<input type="checkbox" name="hold"[^>]*>', modal).group(0) and f'value="{wo.number}" checked' in modal
    r = client.post("/incidents/new/", recording_post(vent, today, investigation=wo.number), **HX)
    assert r["HX-Retarget"] == "#drawer" and Incident.objects.get().work_order == wo


def test_the_device_drawer_offers_its_one_open_repair(client, sign_in, vent, today):
    wo = create_work_order(asset=vent, type=WoType.REPAIR, priority=Priority.HIGH, problem="Flow sensor fault")
    sign_in("technician")
    modal = html(client.get(f"/incidents/new/?asset={vent.tag}", **HX))
    assert f'value="{wo.number}" checked' in modal and 'value="new"' in modal
    create_work_order(asset=vent, type=WoType.REPAIR, priority=Priority.NORMAL, problem="Second request")
    modal = html(client.get(f"/incidents/new/?asset={vent.tag}", **HX))
    assert f'value="{wo.number}" checked' not in modal and 'value="new" checked' in modal  # two: none is chosen for the user


# --- the drawer -----------------------------------------------------------------------------------------------------------------

def test_the_drawer_is_a_partial_or_the_page_with_it_open(client, sign_in, vent, today):
    i = record(vent)
    sign_in("analyst")
    part = html(client.get(f"/incidents/{i.number}/", **HX))
    assert part.lstrip().startswith('<div class="dr-h">') and "<html" not in part
    full = client.get(f"/incidents/{i.number}/")
    body = html(full)
    assert full.context["drawer_template"] == "web/_incident_drawer.html" and '<aside class="drawer open"' in body
    assert 'id="inc-body"' in body and i.number in body


def test_deciding_and_recording_reports_from_the_drawer(client, sign_in, vent, today):
    i = record(vent, occurred_on=days_ago(1))
    sign_in("manager")
    modal = html(client.get(f"/incidents/{i.number}/decide/", **HX))
    assert "reasonably suggest" in modal and 'name="event_reference"' in modal  # none on file: asked here
    data = {"outcome": Outcome.SERIOUS_INJURY, "basis": Basis.MAY_HAVE, "decided_on": today.isoformat(), "decided_by": DecidedBy.RISK}
    r = client.post(f"/incidents/{i.number}/decide/", data, **HX)
    assert "Enter the facility&#x27;s event report number first" in html(r)
    r = client.post(f"/incidents/{i.number}/decide/", {**data, "event_reference": "EV-55"}, **HX)
    assert r["HX-Retarget"] == "#drawer" and toast_of(r) == f"{i.number}: decided reportable"
    i.refresh_from_db()
    assert i.reportable and i.event_reference == "EV-55" and i.decided_by == DecidedBy.RISK
    assert 'name="event_reference"' not in html(client.get(f"/incidents/{i.number}/decide/", **HX))
    r = client.post(f"/incidents/{i.number}/reports/", {"manufacturer_reported_on": today.isoformat(), "report_number": "123"}, **HX)
    assert "0123456789-2026-0001" in html(r) and "HX-Retarget" not in r  # the format, in the modal
    r = client.post(f"/incidents/{i.number}/reports/", {"manufacturer_reported_on": today.isoformat(),
                                                         "report_number": f"0123456789-{today.year}-0001"}, **HX)
    assert r["HX-Retarget"] == "#drawer"
    i.refresh_from_db()
    assert i.report_number == f"0123456789-{today.year}-0001"
    assert "within the 10 work days" in html(client.get(f"/incidents/{i.number}/", **HX))


def test_a_decision_the_rules_refuse_says_why_in_the_modal(client, sign_in, vent, today):
    i = record(vent, event_reference="EV-1")
    sign_in("manager")
    r = client.post(f"/incidents/{i.number}/decide/", {"outcome": Outcome.INJURY, "basis": Basis.MAY_HAVE, "decided_on": today.isoformat(),
                                                        "decided_by": DecidedBy.CE}, **HX)
    assert "A report is required only for a death or a serious injury" in html(r)
    r = client.post(f"/incidents/{i.number}/decide/", {"outcome": Outcome.UNKNOWN, "basis": Basis.NOT_SERIOUS, "decided_on": today.isoformat(),
                                                        "decided_by": DecidedBy.CE}, **HX)
    assert r.status_code == 200 and "HX-Retarget" not in r  # never "not known yet" (not among the choices)


def test_a_failure_finding_clears_a_no_suggestion_decision_and_says_so(client, sign_in, vent, today):
    i = record(vent, event_reference="EV-1")
    inc.decide(i, outcome=Outcome.SERIOUS_INJURY, basis=Basis.NO_SUGGESTION, decided_on=today, decided_by=DecidedBy.COMMITTEE)
    sign_in("technician")
    r = client.post(f"/incidents/{i.number}/finding/", {"finding": Finding.DEVICE_FAILURE}, **HX)
    assert toast_of(r) == inc.DECISION_CLEARED and "incidents-changed" in triggers(r)
    i.refresh_from_db()
    assert i.finding == Finding.DEVICE_FAILURE and i.reportable is None
    r = client.post(f"/incidents/{i.number}/finding/", {"finding": "blame"}, **HX)
    assert "Choose one of the choices listed." in html(r) and "role=\"alert\"" in html(r)


def test_facts_a_technician_raises_and_a_manager_lowers(client, sign_in, vent, today):
    i = record(vent, outcome=Outcome.INJURY, affected=Affected.PATIENT, hold=False, open_work_order=False)
    data = {"occurred_on": i.occurred_on.isoformat(), "aware_on": i.aware_on.isoformat(), "outcome": Outcome.SERIOUS_INJURY,
            "affected": Affected.PATIENT, "event_reference": "", "accessories": "", "event_log": ""}
    sign_in("technician")
    modal = html(client.get(f"/incidents/{i.number}/facts/", **HX))
    assert 'name="aware_reason"' not in modal  # a decider's
    r = client.post(f"/incidents/{i.number}/facts/", data, **HX)
    assert r["HX-Retarget"] == "#drawer"
    r = client.post(f"/incidents/{i.number}/facts/", {**data, "outcome": Outcome.NO_HARM}, **HX)
    assert "Lowering the outcome needs Incidents Approve." in html(r)
    sign_in("manager")
    assert 'name="aware_reason"' in html(client.get(f"/incidents/{i.number}/facts/", **HX))
    r = client.post(f"/incidents/{i.number}/facts/", {**data, "outcome": Outcome.INJURY}, **HX)
    assert r["HX-Retarget"] == "#drawer"
    i.refresh_from_db()
    assert i.outcome == Outcome.INJURY and i.report_due_on is None


def test_holding_another_device_custody_and_release(client, sign_in, vent, pump, today):
    i = record(vent)
    sign_in("technician")
    picks = html(client.get(f"/incidents/devices/?asset_q=CE-1000&incident={i.number}", **HX))
    assert f'hx-get="/incidents/{i.number}/hold/"' in picks and 'hx-include="#inch-form"' in picks
    assert pump.tag in html(client.get(f"/incidents/{i.number}/hold/?asset={pump.tag}", **HX))
    r = client.post(f"/incidents/{i.number}/hold/", {"asset": pump.tag}, **HX)
    assert r["HX-Retarget"] == "#drawer" and toast_of(r) == f"{pump.tag} held as evidence for {i.number}"
    r = client.post(f"/incidents/{i.number}/hold/", {"asset": pump.tag}, **HX)
    assert f"{i.number} already holds {pump.tag}." in html(r)
    hold = IncidentHold.objects.get(incident=i, asset=pump)
    sign_in("manager")
    drawer = html(client.get(f"/incidents/{i.number}/", **HX))
    assert f"/holds/{hold.pk}/sent/" in drawer and f"/holds/{hold.pk}/release/" in drawer
    r = client.post(f"/incidents/{i.number}/holds/{hold.pk}/sent/", {"on": (today + timedelta(days=1)).isoformat()}, **HX)
    assert "not a day to come" in html(r)
    r = client.post(f"/incidents/{i.number}/holds/{hold.pk}/sent/", {"on": today.isoformat()}, **HX)
    assert r["HX-Retarget"] == "#drawer"
    assert f"/holds/{hold.pk}/back/" in html(client.get(f"/incidents/{i.number}/", **HX))
    r = client.post(f"/incidents/{i.number}/holds/{hold.pk}/release/", {"release": Release.RETURN_TO_USE}, **HX)
    assert "with the manufacturer" in html(r) and "HX-Retarget" not in r
    r = client.post(f"/incidents/{i.number}/holds/{hold.pk}/back/", {"on": today.isoformat()}, **HX)
    assert r["HX-Retarget"] == "#drawer"
    r = client.post(f"/incidents/{i.number}/holds/{hold.pk}/release/", {"release": Release.KEEP_OUT}, **HX)
    assert toast_of(r) == f"{pump.tag} released: kept out of service" and "devices-changed" in triggers(r)
    pump.refresh_from_db()
    assert not pump.incident_hold and pump.status == AssetStatus.OUT_OF_SERVICE
    assert "released" in html(client.get(f"/incidents/{i.number}/holds/{hold.pk}/release/", **HX))  # no longer active: says so


def test_a_release_names_what_still_keeps_the_device_out(client, sign_in, vent, today):
    first = record(vent, outcome=Outcome.NO_HARM, affected=Affected.NONE)
    record(vent, outcome=Outcome.NO_HARM, affected=Affected.NONE, open_work_order=False)
    hold = first.holds.get()
    sign_in("manager")
    r = client.post(f"/incidents/{first.number}/holds/{hold.pk}/release/", {"release": Release.KEEP_OUT}, **HX)
    assert toast_of(r).startswith(f"{vent.tag} released: kept out of service. {vent.tag} stays held: incident IN-")


def test_open_investigation_close_reopen_and_in_error(client, sign_in, vent, today):
    i = record(vent, outcome=Outcome.NO_HARM, affected=Affected.NONE, hold=False, open_work_order=False)
    sign_in("manager")
    assert "Open investigation" in html(client.get(f"/incidents/{i.number}/", **HX))
    r = client.post(f"/incidents/{i.number}/investigation/", **HX)
    i.refresh_from_db()
    assert toast_of(r) == f"{i.work_order.number} opened as the investigation of {i.number}" and "wo-changed" in triggers(r)
    r = client.post(f"/incidents/{i.number}/investigation/", **HX)
    assert "has its investigation" in toast_of(r) and "has its investigation" in html(r)  # in the drawer too
    modal = html(client.post(f"/incidents/{i.number}/close/", **HX))
    assert "Complete or cancel investigation" in modal and "Record what the device evaluation found." in modal
    change_status(i.work_order, WoStatus.CANCELLED)
    inc.record_finding(i, Finding.MET_SPECS)
    r = client.post(f"/incidents/{i.number}/close/", **HX)
    assert toast_of(r) == f"{i.number} closed"
    drawer = html(client.get(f"/incidents/{i.number}/", **HX))
    assert f"/incidents/{i.number}/reopen/" in drawer and "/decide/" not in drawer
    assert "is closed: reopen it first." in html(client.get(f"/incidents/{i.number}/facts/", **HX))
    r = client.post(f"/incidents/{i.number}/reopen/", **HX)
    assert toast_of(r) == f"{i.number} reopened"
    assert "counted nowhere" in html(client.get(f"/incidents/{i.number}/in-error/", **HX))
    r = client.post(f"/incidents/{i.number}/in-error/", **HX)
    i.refresh_from_db()
    assert i.status == Status.IN_ERROR and r["HX-Retarget"] == "#drawer"
    assert "Recorded in error: kept, and counted nowhere." in html(client.get(f"/incidents/{i.number}/", **HX))


def test_history_shows_the_incident_and_its_holds(client, sign_in, vent, pump, today):
    i = record(vent)
    inc.hold_device(i, pump)
    sign_in("analyst")
    r = client.get(f"/incidents/{i.number}/?history=1", HTTP_HX_TARGET="hist", **HX)
    body = html(r)
    assert "Hold added" in body and "Recorded" in body
    sign_in("requester", department="ICU")
    assert client.get(f"/incidents/{i.number}/?history=1", HTTP_HX_TARGET="hist", **HX).status_code == 403


# --- the hold on the other screens ------------------------------------------------------------------------------------------------

def test_the_device_drawer_banner_names_the_incident_only_for_incidents_view(client, sign_in, make_user, tenant, vent, pump, today):
    i = record(vent)
    sign_in("technician")
    drawer = html(client.get(f"/equipment/{vent.tag}/?tab=pm", **HX))  # every tab
    assert f"Held as evidence for incident {i.number}" in drawer and f'href="/incidents/{i.number}/"' in drawer
    assert f"/equipment/{vent.tag}/status/" not in drawer  # no status buttons while held
    assert f"/equipment/{pump.tag}/status/" in html(client.get(f"/equipment/{pump.tag}/", **HX))  # a device not held has them
    user = custom_user(make_user, tenant, "biomed-view", {Module.EQUIPMENT: Level.VIEW, Module.WORKORDERS: Level.VIEW})
    client.force_login(user)
    drawer = html(client.get(f"/equipment/{vent.tag}/", **HX))
    assert inc.HELD_WORDS in drawer and i.number not in drawer and "Record incident" not in drawer


def test_a_held_devices_other_work_has_no_start_or_mark_completed(client, sign_in, vent, today, techs):
    other = create_work_order(asset=vent, type=WoType.REPAIR, priority=Priority.NORMAL, problem="Screen cracked", assigned_to=techs["dana"])
    i = record(vent)
    sign_in("technician")
    body = html(client.get(f"/work-orders/{other.number}/", **HX))
    assert '"to": "in_progress"' not in body and "Mark completed" not in body and ">Held</span>" in body
    assert f"Held as evidence for incident {i.number}" in body
    investigation = i.work_order
    assign(investigation, technician=techs["dana"])
    body = html(client.get(f"/work-orders/{investigation.number}/", **HX))
    assert '"to": "in_progress"' in body and ">Held</span>" not in body


def test_my_work_marks_held_work_and_drops_its_moves(client, users, vent, today):
    user = users["technician"]
    me = Technician.objects.create(user=user, name="Terry Tech")
    Credential.objects.create(technician=me, scope=Scope.CATEGORY, value="Ventilators")
    other = create_work_order(asset=vent, type=WoType.REPAIR, priority=Priority.NORMAL, problem="Screen cracked", assigned_to=me)
    i = record(vent)
    assign(i.work_order, technician=me)
    client.force_login(user)
    body = html(client.get("/my-work/", HTTP_HX_TARGET="my-work-body", **HX))
    card = re.search(rf'<article class="mw-card[^"]*" id="mw-{other.number}".*?</article>', body, re.S).group(0)
    assert ">Held</span>" in card and inc.HELD_WORDS in card and ">Start</button>" not in card and "Log time" in card
    investigation = re.search(rf'<article class="mw-card[^"]*" id="mw-{i.work_order.number}".*?</article>', body, re.S).group(0)
    assert ">Held</span>" not in investigation and ">Start</button>" in investigation


@pytest.mark.parametrize("who", ["vendor", "requester"])
def test_a_scoped_user_never_sees_the_incident(client, sign_in, vent, today, who):
    wo = create_work_order(asset=vent, type=WoType.REPAIR, priority=Priority.NORMAL, problem="Alarm", vendor_service=True,
                           vendor_name="Hamilton Medical field service")
    i = record(vent, outcome=Outcome.SERIOUS_INJURY, affected=Affected.STAFF, event_reference="EV-77")
    assign(i.work_order, vendor_name="Hamilton Medical field service")
    sign_in(who, **({"company": "Hamilton Medical"} if who == "vendor" else {"department": "ICU"}))
    pages = [html(client.get(f"/equipment/{vent.tag}/", **HX)), html(client.get(f"/equipment/{vent.tag}/?tab=wo", **HX)),
             html(client.get(f"/work-orders/{wo.number}/", **HX)), html(client.get(f"/work-orders/{i.work_order.number}/", **HX)),
             html(client.get("/my-work/")) if who == "vendor" else ""]
    for page in pages:
        assert i.number not in page and "EV-77" not in page and "/incidents/" not in page
        for word in OUTCOME_WORDS + AFFECTED_WORDS:
            assert word not in page, word
    assert inc.HELD_WORDS in pages[0] and inc.HELD_WORDS in pages[2]
    assert "Record incident" not in "".join(pages)


def test_the_pm_day_panel_marks_a_held_device(client, sign_in, vent, today):
    record(vent)
    vent.refresh_from_db()
    day = vent.next_pm_on
    sign_in("manager")
    body = html(client.get(f"/pm/?y={day.year}&m={day.month}&day={day.isoformat()}"))
    row = re.search(rf'{vent.tag}</a>.*?</div>\s*</div>\s*<div class="r">(.*?)</div>', body, re.S).group(1)
    assert ">Held</span>" in row and "Nobody credentialed" not in row


def test_auto_assign_week_says_which_held_devices_get_no_pm(client, sign_in, vent, today, monkeypatch):
    vent.next_pm_on = today + timedelta(days=2)
    vent.save()
    record(vent)
    monkeypatch.setattr("apps.web.views_pm._today", lambda: today)
    sign_in("manager")
    body = html(client.get("/pm/week/assign/", **HX))
    assert f"{vent.tag} (ICU ventilator, due {(today + timedelta(days=2)):%b} {(today + timedelta(days=2)).day})" in body
    assert "held as evidence for an incident investigation" in body and "no PMs are due this week" not in body
    assert "Nothing to assign: the PMs due this week are on devices held for an incident investigation." in body
    r = client.post("/pm/week/assign/", **HX)
    assert toast_of(r) == "Nothing to assign: the device due this week is held for an incident investigation"
    assert not WorkOrder.objects.filter(asset=vent, type=WoType.PM).exists()


def test_assigned_message_counts_held_devices():
    from apps.pm.services import WeekAssignment

    w = WeekAssignment(start=date(2026, 10, 8), end=date(2026, 10, 14), on_hold=[{"asset": None, "has_open_pm": False}] * 2)
    assert assigned_message(w) == "Nothing to assign: the 2 devices due this week are held for an incident investigation"


def test_the_portal_says_a_held_device_is_held_without_the_incident(client, vent, today):
    i = record(vent, outcome=Outcome.DEATH, affected=Affected.PATIENT, event_reference="EV-3")
    body = html(client.get(f"/r/riverside/?asset={vent.tag}"))
    assert "Held by Clinical Engineering: do not use it." in body
    assert i.number not in body and "incident" not in body.lower() and "Death" not in body


def test_the_put_in_use_before_inspection_offer_is_gone_while_held(client, sign_in, vent, today):
    from apps.web.asset_tabs import incoming_banner
    from apps.web.views_equipment import _use_before_blocker

    vent.awaiting_inspection = True
    vent.status = AssetStatus.OUT_OF_SERVICE
    vent.save()
    record(vent)
    vent.refresh_from_db()
    director = sign_in("director")
    assert not incoming_banner(vent, director)["use_before"]
    assert "held as evidence" in _use_before_blocker(vent)


def test_the_roles_tab_says_incidents_is_new(client, sign_in, ctx):
    sign_in("director")
    assert "New: Incidents" in html(client.get("/users/roles/"))


# --- under PostgreSQL's row-level security -------------------------------------------------------------------------------------

@needs_postgres
def test_the_screens_under_the_policies(client, sign_in, vent, pump, today):
    """The list, its CSV (streamed after the middleware has left the facility), the drawer with its holds and History, recording, and
    the device and work order drawers' hold words, as the runtime role inside this facility."""
    i = record(vent, event_reference="EV-1")
    sign_in("manager")
    as_app_role()
    assert i.number in html(client.get("/incidents/"))
    rows = b"".join(client.get("/incidents/incidents.csv").streaming_content).decode("utf-8-sig").splitlines()
    assert len(rows) == 2 and rows[1].startswith(i.number)
    drawer = html(client.get(f"/incidents/{i.number}/", **HX))
    assert vent.tag in drawer and "/holds/" in drawer
    assert "Hold added" in html(client.get(f"/incidents/{i.number}/?history=1", HTTP_HX_TARGET="hist", **HX))
    r = client.post("/incidents/new/", recording_post(pump, today), **HX)  # the test's own reads after a request see no facility
    assert r["HX-Retarget"] == "#drawer" and f"· {pump.tag} " in html(r) and toast_of(r).startswith("IN-")
    assert f"Held as evidence for incident {i.number}" in html(client.get(f"/equipment/{vent.tag}/", **HX))
    assert f'href="/incidents/{i.number}/"' in html(client.get(f"/work-orders/{i.work_order.number}/", **HX))
