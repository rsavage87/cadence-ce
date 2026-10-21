"""
Slice 38 wave 2 C: software updates over the API (apps/api/serializers.py WorkOrderSerializer, serializers_cybersecurity.py,
views_cybersecurity.py SoftwareUpdateViewSet), on SQLite and PostgreSQL.

- A work order's software_update: what an update work order is to install and what it did, in apps.cybersecurity.rules' words, only for
  a reader with Device security View and Equipment View (null for the analyst, a custom role without Device security or without
  Equipment View, a scoped vendor, any other type, and an update imported from another system); a list's page reads them in one query
  (the query count does not grow with the rows).
- software_choices: on a single work order only (null in a list), for a user who may record what it did (rules.release_offer) and may
  complete it now: the target, the outcomes, the model's releases with the recorded one marked, the bases each outcome may name.
- The transition to completed with software_* fields end to end: a missing choice names what to send ("Send software_outcome: ..."), a
  user who may not record is refused keyed software_outcome, installed and not confirmed record as the service does, software_* on any
  other move is refused.
- GET /api/v1/software-updates/: the fields as the Security tab shows them (the work order's number, never a link), newest first, the
  filters (device by id or tag in any letter case, release as the target or what it runs after, state; an unknown value a 400 naming the
  parameter; each state as rules.update_state reads every row), an entry entered in error left out, the levels' 403 words, a 405 on
  every write, a token reads only its facility, a fixed number of queries, never a never word.
"""
import re
from datetime import timedelta

import pytest
from cyber_fixtures import facts, release, update_wo
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.authtoken.models import Token

from apps.accounts.models import DataScope, Level, Module, Role, User, create_default_roles
from apps.api import serializers_cybersecurity as scs
from apps.api import views_cybersecurity as api
from apps.api.permissions import ModulePermission
from apps.api.views import SOFTWARE_RECORDED
from apps.cybersecurity import rules, updates
from apps.cybersecurity.models import InstalledBy, SoftwareUpdate, UpdateBasis, UpdateOutcome, UpdatesBy
from apps.cybersecurity.services import record_facts
from apps.cybersecurity.updates import NOT_CONFIRMED, UpdateChoice, record, remove
from apps.equipment.models import Asset, Department, DeviceModel
from apps.tenants.context import tenant_context
from apps.workorders import services as wo_services
from apps.workorders.completion import complete_work_order
from apps.workorders.models import Priority, Source, WorkOrder, WoStatus, WoType

UPDATES = "/api/v1/software-updates/"
WORK_ORDERS = "/api/v1/work-orders/"
DAY = timedelta(days=1)
UPDATE_FIELDS = {"id", "device", "work_order", "target", "target_release", "target_reference", "state", "state_label", "outcome",
                 "outcome_label", "words", "applied_on", "release_before", "release_after", "reference", "basis", "basis_label",
                 "installed_by", "installed_by_label", "recorded_by", "later_kept", "opened_on"}
WO_UPDATE_FIELDS = {"target", "target_release", "target_reference", "state", "outcome", "outcome_label", "words", "applied_on",
                    "release_after"}


def never(text: str) -> list:
    return [w for w in rules.NEVER_WORDS if re.search(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", text, re.IGNORECASE)]


def ok(r):
    assert r.status_code == 200, r.content
    return r.json()


@pytest.fixture
def today(ctx):
    return timezone.localdate()


def custom_user(tenant, name, levels, scope="", **extra):
    role = Role.objects.create(name=name.title(), slug=name, scope=scope)
    role.set_levels({m: levels.get(m, Level.NONE) for m in Module.values})
    return User.objects.create_user(username=f"{name}@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role,
                                    first_name=name.title(), last_name="User", **extra)


def day(d) -> str:
    return rules.day_words(d)


@pytest.fixture
def world(ctx, dept, pump_model, pump, techs, today, make_user):
    """The pumps on 5.2-DEMO (CE installs their updates here), 5.6-DEMO on file: CE-10002's update to 5.6-DEMO open (Tom's);
    CE-10003's completed as installed by a technician; CE-10004's completed with the version not confirmed; CE-10005's cancelled;
    CE-10006's patches recorded by hand (by IT, two days ago, ICAR-2026-09 on the validated list); CE-10007's hand row entered in error;
    CE-10008 a vendor's (BD) update, open."""
    release(pump_model, updates_by=UpdatesBy.CE)
    r52, r56 = release(pump_model, "5.2-DEMO"), release(pump_model, "5.6-DEMO")
    tech = make_user("technician")
    devices = {"CE-10002": pump}
    for n in range(3, 9):
        devices[f"CE-1000{n}"] = Asset.objects.create(tag=f"CE-1000{n}", device_model=pump_model, department=dept,
                                                      installed_on=today - 400 * DAY)
    for a in devices.values():
        facts(a, r52, release_since=today - 30 * DAY, checked_on=today - 30 * DAY)
    wos = {}
    for tag in ("CE-10002", "CE-10003", "CE-10004", "CE-10005"):
        wos[tag], _row = update_wo(devices[tag], r56, assigned_to=techs["tom"], opened_on=today, due_on=today + 30 * DAY)
    complete_work_order(wos["CE-10003"], software=UpdateChoice(UpdateOutcome.INSTALLED, release_id=str(r56.pk),
                                                               basis=UpdateBasis.MANUFACTURER_RELEASE), by=tech, today=today)
    complete_work_order(wos["CE-10004"], software=UpdateChoice(NOT_CONFIRMED), resolution="Done; version to be checked", by=tech, today=today)
    wo_services.change_status(wos["CE-10005"], WoStatus.CANCELLED)
    hand = record(devices["CE-10006"], UpdateChoice(UpdateOutcome.PATCHES, reference="ICAR-2026-09", basis=UpdateBasis.VALIDATED_LIST),
                  installed_by=InstalledBy.IT, applied_on=today - 2 * DAY, by=tech, today=today)
    removed = record(devices["CE-10007"], UpdateChoice(UpdateOutcome.PATCHES, reference="ICAR-2026-08", basis=UpdateBasis.NOT_STATED),
                     installed_by=InstalledBy.CE, applied_on=today - 3 * DAY, by=None, today=today)
    remove(removed, by=None, today=today)
    wos["CE-10008"], _row = update_wo(devices["CE-10008"], r56, vendor_service=True, vendor_name="BD", opened_on=today,
                                      due_on=today + 30 * DAY)
    for wo in wos.values():
        wo.refresh_from_db()
    return {"r52": r52, "r56": r56, "devices": devices, "wos": wos, "hand": hand, "removed": removed, "tech": tech, "today": today}


# --- the work order's software_update ----------------------------------------------------------------------------------------------

def test_the_work_orders_software_update_in_the_rules_words(client, make_user, world, today):
    client.force_login(make_user("director"))
    wos, r52, r56 = world["wos"], world["r52"], world["r56"]
    opened = ok(client.get(f"{WORK_ORDERS}{wos['CE-10002'].pk}/"))["software_update"]
    assert set(opened) == WO_UPDATE_FIELDS
    assert opened == {"target": "release", "target_release": {"id": str(r56.pk), "version": "5.6-DEMO"}, "target_reference": "",
                      "state": "open", "outcome": "", "outcome_label": "", "words": "Open: to install 5.6-DEMO", "applied_on": None,
                      "release_after": None}
    done = ok(client.get(f"{WORK_ORDERS}{wos['CE-10003'].pk}/"))["software_update"]
    assert (done["state"], done["outcome"], done["outcome_label"], done["applied_on"], done["release_after"]) == (
        "done", "installed", UpdateOutcome.INSTALLED.label, today.isoformat(), {"id": str(r56.pk), "version": "5.6-DEMO"})
    assert done["words"] == f"5.6-DEMO installed {day(today)} (recorded before: 5.2-DEMO)"
    unconfirmed = ok(client.get(f"{WORK_ORDERS}{wos['CE-10004'].pk}/"))["software_update"]
    assert (unconfirmed["state"], unconfirmed["words"]) == ("not_confirmed", f"Completed {day(today)}; version not confirmed")
    cancelled = ok(client.get(f"{WORK_ORDERS}{wos['CE-10005'].pk}/"))["software_update"]
    assert (cancelled["state"], cancelled["words"]) == ("cancelled", "Cancelled")
    assert r52.pk  # the version before is the record's own (the words), never a field of the work order
    other = wo_services.create_work_order(asset=world["devices"]["CE-10002"], type=WoType.REPAIR, priority=Priority.NORMAL,
                                          problem="Door latch loose")
    assert ok(client.get(f"{WORK_ORDERS}{other.pk}/"))["software_update"] is None  # another type


def test_an_update_imported_from_another_system_has_no_record(client, make_user, world, today):
    imported = wo_services.create_work_order(asset=world["devices"]["CE-10006"], type=WoType.SOFTWARE_UPDATE, priority=Priority.NORMAL,
                                             problem="Firmware 5.6 loaded", source=Source.IMPORTED, _register_opener=True)
    client.force_login(make_user("director"))
    data = ok(client.get(f"{WORK_ORDERS}{imported.pk}/"))
    assert data["software_update"] is None and data["software_choices"] is None


@pytest.mark.parametrize("who", ["analyst", "no_register", "no_equipment", "vendor"])
def test_every_other_reader_reads_null(client, make_user, tenant, world, who):
    if who == "analyst":
        user = make_user("analyst")  # Device security None (slice 37's default)
    elif who == "no_register":
        user = custom_user(tenant, "woedit", {Module.WORKORDERS: Level.EDIT, Module.EQUIPMENT: Level.VIEW})
    elif who == "no_equipment":
        user = custom_user(tenant, "itsec", {Module.WORKORDERS: Level.EDIT, Module.CYBERSECURITY: Level.EDIT})
    else:
        user = make_user("vendor")
        user.company = "BD"
        user.save(update_fields=["company"])
    client.force_login(user)
    seen = ["CE-10008"] if who == "vendor" else ["CE-10002", "CE-10003", "CE-10004", "CE-10008"]
    for tag in seen:
        data = ok(client.get(f"{WORK_ORDERS}{world['wos'][tag].pk}/"))
        assert data["type"] == "software_update" and data["software_update"] is None and data["software_choices"] is None, tag
        assert "DEMO" not in str(data) and "software release" not in str(data).lower(), tag
    rows = ok(client.get(WORK_ORDERS))["results"]
    assert rows and all(r["software_update"] is None and r["software_choices"] is None for r in rows)
    assert "DEMO" not in str(rows)


def test_device_security_view_and_equipment_view_read_it(client, tenant, world):
    reader = custom_user(tenant, "reader", {Module.WORKORDERS: Level.VIEW, Module.EQUIPMENT: Level.VIEW, Module.CYBERSECURITY: Level.VIEW})
    client.force_login(reader)
    data = ok(client.get(f"{WORK_ORDERS}{world['wos']['CE-10002'].pk}/"))
    assert data["software_update"]["words"] == "Open: to install 5.6-DEMO"
    assert data["software_choices"] is None  # View records nothing


def test_a_list_reads_the_page_s_updates_in_one_query(client, make_user, world, pump_model, dept, techs, today):
    client.force_login(make_user("director"))

    def listed():
        with CaptureQueriesContext(connection) as q:
            rows = ok(client.get(WORK_ORDERS))["results"]
        return rows, len(q.captured_queries)

    rows, before = listed()
    shown = {r["number"]: r for r in rows}
    assert shown[world["wos"]["CE-10003"].number]["software_update"]["state"] == "done"
    assert all(r["software_choices"] is None for r in rows)  # retrieve only
    for n in range(10, 16):  # six more update work orders on six more pumps
        a = Asset.objects.create(tag=f"CE-100{n}", device_model=pump_model, department=dept)
        facts(a, world["r52"])
        update_wo(a, world["r56"], assigned_to=techs["tom"], opened_on=today, due_on=today + 30 * DAY)
    rows, after = listed()
    assert sum(1 for r in rows if r["software_update"]) == 11 and after == before


def test_the_list_reads_no_update_for_a_reader_who_may_not(client, make_user, world):
    client.force_login(make_user("analyst"))
    with CaptureQueriesContext(connection) as q:
        ok(client.get(WORK_ORDERS))
    assert not [s for s in q.captured_queries if "cybersecurity_softwareupdate" in s["sql"]]


# --- software_choices --------------------------------------------------------------------------------------------------------------

def test_software_choices_offer_what_the_completion_may_send(client, make_user, world):
    client.force_login(world["tech"])
    r52, r56 = world["r52"], world["r56"]
    choices = ok(client.get(f"{WORK_ORDERS}{world['wos']['CE-10002'].pk}/"))["software_choices"]
    assert choices == {
        "needed": True, "target": "release", "target_release": {"id": str(r56.pk), "version": "5.6-DEMO"}, "target_reference": "",
        "target_words": "Open: to install 5.6-DEMO",
        "outcomes": [{"value": "installed", "label": "Release installed"}, {"value": "patches", "label": "Patches applied, version unchanged"},
                     {"value": "already", "label": "Already on the update; nothing installed"},
                     {"value": "not_confirmed", "label": "Not confirmed now"}],
        "releases": [{"id": str(r52.pk), "version": "5.2-DEMO", "recorded": True}, {"id": str(r56.pk), "version": "5.6-DEMO", "recorded": False}],
        "bases": {"installed": [{"value": "manufacturer_release", "label": UpdateBasis.MANUFACTURER_RELEASE.label},
                                {"value": "not_stated", "label": "Not stated"}],
                  "patches": [{"value": "validated_list", "label": UpdateBasis.VALIDATED_LIST.label},
                              {"value": "manufacturer_release", "label": UpdateBasis.MANUFACTURER_RELEASE.label},
                              {"value": "not_stated", "label": "Not stated"}],
                  "already": []},
    }
    assert never(str(choices)) == []
    # left out of a list's rows; null on one completed (nothing to complete now), and for a user who may not record or complete
    assert ok(client.get(f"{WORK_ORDERS}{world['wos']['CE-10003'].pk}/"))["software_choices"] is None


def test_software_choices_need_the_offer_and_the_completion(client, tenant, world):
    wo = world["wos"]["CE-10002"]
    cases = {
        "view only": {Module.WORKORDERS: Level.EDIT, Module.EQUIPMENT: Level.VIEW, Module.CYBERSECURITY: Level.VIEW},
        "work orders view": {Module.WORKORDERS: Level.VIEW, Module.EQUIPMENT: Level.VIEW, Module.CYBERSECURITY: Level.EDIT},
        "everything": {Module.WORKORDERS: Level.EDIT, Module.EQUIPMENT: Level.VIEW, Module.CYBERSECURITY: Level.EDIT},
    }
    for n, (name, levels) in enumerate(cases.items()):
        client.force_login(custom_user(tenant, f"case{n}", levels))
        choices = ok(client.get(f"{WORK_ORDERS}{wo.pk}/"))["software_choices"]
        assert (choices is not None) == (name == "everything"), name


# --- the transition to completed ---------------------------------------------------------------------------------------------------

def transition(client, wo, **body):
    return client.post(f"{WORK_ORDERS}{wo.pk}/transition/", {"status": "completed", **body}, content_type="application/json")


def test_the_transition_records_what_the_update_did(client, world, today):
    wo, r56 = world["wos"]["CE-10002"], world["r56"]
    client.force_login(world["tech"])
    missing = transition(client, wo)
    assert missing.status_code == 400 and missing.json()["software_outcome"] == [updates.SEND_OUTCOME]
    assert missing.json()["software_outcome"] == ["Send software_outcome: installed, patches, already, or not_confirmed."]
    bad = transition(client, wo, software_outcome="upgraded")
    assert bad.status_code == 400 and bad.json() == {"software_outcome": [updates.OUTCOME_WORDS]}
    listed = transition(client, wo, software_outcome=["installed"])
    assert listed.status_code == 400 and listed.json() == {"software_outcome": ["Send this as text."]}
    keyed = transition(client, wo, software_outcome="installed", resolution="Loaded 5.6")
    assert keyed.status_code == 400 and keyed.json() == {"software_release": [updates.CHOOSE_RUNS_NOW], "software_basis": [updates.BASIS_WORDS]}
    done = transition(client, wo, software_outcome="installed", software_release=str(r56.pk), software_basis="manufacturer_release")
    data = ok(done)
    assert data["status"] == "completed" and data["resolution"] == updates.DEFAULT_RESOLUTION[UpdateOutcome.INSTALLED]
    assert data["software_update"]["state"] == "done"
    assert data["software_update"]["words"] == f"5.6-DEMO installed {day(today)} (recorded before: 5.2-DEMO)"
    assert rules.release_of(Asset.objects.get(pk=wo.asset_id)) == r56
    assert data["software_choices"] is None  # completed: nothing to send now


def test_not_confirmed_now_through_the_transition(client, world, today):
    wo = world["wos"]["CE-10002"]
    client.force_login(world["tech"])
    data = ok(transition(client, wo, software_outcome="not_confirmed", resolution="Loaded the update; version to be checked"))
    assert data["software_update"]["state"] == "not_confirmed"
    assert data["software_update"]["words"] == f"Completed {day(today)}; version not confirmed"
    assert SoftwareUpdate.objects.get(work_order=wo).outcome == ""


def test_a_user_who_may_not_record_is_refused_keyed_software_outcome(client, tenant, world):
    wo, r56 = world["wos"]["CE-10002"], world["r56"]
    client.force_login(custom_user(tenant, "woedit", {Module.WORKORDERS: Level.EDIT, Module.EQUIPMENT: Level.VIEW}))
    r = transition(client, wo, software_outcome="installed", software_release=str(r56.pk), software_basis="manufacturer_release",
                   resolution="Done")
    assert r.status_code == 400 and r.json() == {"software_outcome": [updates.RECORD_PERMISSION]}
    data = ok(transition(client, wo, resolution="Done"))  # left out: completed, nothing recorded
    assert data["status"] == "completed" and data["software_update"] is None
    assert SoftwareUpdate.objects.get(work_order=wo).outcome == ""


def test_a_scoped_vendor_completes_its_update_and_records_nothing(client, make_user, world, today):
    vendor = make_user("vendor")
    vendor.company = "BD"
    vendor.save(update_fields=["company"])
    wo = world["wos"]["CE-10008"]
    client.force_login(vendor)
    refused = transition(client, wo, software_outcome="not_confirmed", resolution="Updated per our visit")
    assert refused.status_code == 400 and refused.json() == {"software_outcome": [updates.RECORD_PERMISSION]}
    data = ok(transition(client, wo, resolution="Updated per our visit"))
    assert data["status"] == "completed" and data["software_update"] is None
    row = SoftwareUpdate.objects.select_related("work_order").get(work_order=wo)
    assert rules.update_words(row) == f"Completed by the vendor {day(today)}; version not confirmed"


def test_software_fields_on_any_other_move_or_a_new_work_order_are_refused(client, make_user, world):
    wo = world["wos"]["CE-10002"]
    client.force_login(make_user("director"))
    r = client.post(f"{WORK_ORDERS}{wo.pk}/transition/", {"status": "in_progress", "software_outcome": "installed"},
                    content_type="application/json")
    assert r.status_code == 400 and r.json() == {"software_outcome": [SOFTWARE_RECORDED]}
    due = (timezone.localdate() + DAY).isoformat()
    r = client.post(WORK_ORDERS, {"asset": str(wo.asset_id), "type": "repair", "problem": "x", "due_on": due, "software_basis": "not_stated"},
                    content_type="application/json")
    assert r.status_code == 400 and r.json() == {"software_basis": [SOFTWARE_RECORDED]}


# --- GET /api/v1/software-updates/ -------------------------------------------------------------------------------------------------

def test_the_updates_read_as_the_security_tab_shows_them(client, make_user, world, today):
    client.force_login(make_user("director"))
    rows = ok(client.get(UPDATES))["results"]
    assert all(set(r) == UPDATE_FIELDS for r in rows)
    by_tag = {r["device"]["tag"]: r for r in rows}
    assert set(by_tag) == {"CE-10002", "CE-10003", "CE-10004", "CE-10005", "CE-10006", "CE-10008"}  # the one entered in error left out
    wo, r52, r56, tech = world["wos"]["CE-10003"], world["r52"], world["r56"], world["tech"]
    assert by_tag["CE-10003"] == {
        "id": str(SoftwareUpdate.objects.get(work_order=wo).pk), "device": {"id": str(wo.asset_id), "tag": "CE-10003"},
        "work_order": {"id": str(wo.pk), "number": wo.number}, "target": "release",
        "target_release": {"id": str(r56.pk), "version": "5.6-DEMO"}, "target_reference": "", "state": "done", "state_label": "Done",
        "outcome": "installed", "outcome_label": "Release installed", "words": f"5.6-DEMO installed {day(today)} (recorded before: 5.2-DEMO)",
        "applied_on": today.isoformat(), "release_before": {"id": str(r52.pk), "version": "5.2-DEMO"},
        "release_after": {"id": str(r56.pk), "version": "5.6-DEMO"}, "reference": "", "basis": "manufacturer_release",
        "basis_label": UpdateBasis.MANUFACTURER_RELEASE.label, "installed_by": "", "installed_by_label": "", "recorded_by": str(tech),
        "later_kept": False, "opened_on": today.isoformat()}
    hand = by_tag["CE-10006"]
    assert (hand["work_order"], hand["target"], hand["state"], hand["outcome"], hand["reference"], hand["installed_by_label"]) == (
        None, "", "done", "patches", "ICAR-2026-09", "Information technology")
    assert hand["words"] == (f"Patches on the manufacturer's validated patch list applied {day(today - 2 * DAY)}, version unchanged "
                             "(5.2-DEMO); ref ICAR-2026-09; installed by information technology")
    assert (by_tag["CE-10002"]["state_label"], by_tag["CE-10004"]["state_label"], by_tag["CE-10005"]["state_label"]) == (
        "Open", "Version not confirmed", "Cancelled")
    body = client.get(UPDATES).content.decode()
    assert "/work-orders/" not in body and "http" not in body  # the work order's number, never a link
    assert never(body) == []
    # newest first, as the tab lists them: the record's day (applied, completed, opened), then the newest row
    assert [r["device"]["tag"] for r in rows][-1] == "CE-10006"
    one = ok(client.get(f"{UPDATES}{hand['id']}/"))
    assert one == hand
    assert client.get(f"{UPDATES}{world['removed'].pk}/").status_code == 404
    assert client.get(f"{UPDATES}not-an-id/").status_code == 404


def test_the_filters(client, make_user, world, today):
    client.force_login(make_user("director"))

    def tags(query):
        return sorted(r["device"]["tag"] for r in ok(client.get(f"{UPDATES}?{query}"))["results"])

    pump = world["devices"]["CE-10002"]
    assert tags(f"device={pump.pk}") == tags("device=ce-10002") == tags("device=CE-10002") == ["CE-10002"]
    assert tags("device=CE-99999") == []
    # the release: the target, or what the device runs after it
    assert tags(f"release={world['r56'].pk}") == ["CE-10002", "CE-10003", "CE-10004", "CE-10005", "CE-10008"]
    assert tags(f"release={world['r52'].pk}") == ["CE-10006"]  # patches: the version unchanged
    assert tags("state=open") == ["CE-10002", "CE-10008"]
    assert tags("state=done") == ["CE-10003", "CE-10006"]
    assert tags("state=not_confirmed") == ["CE-10004"]
    assert tags("state=cancelled") == ["CE-10005"]
    assert tags(f"state=open&device={pump.pk}&release={world['r56'].pk}") == ["CE-10002"]
    for query, key in (("state=removed", "state"), ("state=Open", "state"), ("release=5.6", "release"), ("device=&release=x", "release")):
        r = client.get(f"{UPDATES}?{query}")
        assert r.status_code == 400 and list(r.json()) == [key], query
    assert client.get(f"{UPDATES}?state=bogus").json() == {"state": ["One of open, done, not_confirmed, cancelled."]}


def test_each_state_filter_reads_every_row_as_the_rules_do(world, today, techs):
    reopened = world["wos"]["CE-10003"]
    wo_services.change_status(reopened, WoStatus.IN_PROGRESS)  # reopened: open again, whatever it recorded
    closed = world["wos"]["CE-10004"]
    wo_services.change_status(closed, WoStatus.CLOSED)
    rows = list(SoftwareUpdate.objects.select_related("work_order").filter(removed_on__isnull=True))
    every = SoftwareUpdate.objects.all()
    for state in rules.STATES:
        assert set(every.filter(api.state_q(state))) == {r for r in rows if rules.update_state(r) == state}, state
    assert {rules.update_state(r) for r in rows} == set(rules.STATES)


def test_a_page_of_updates_is_a_fixed_number_of_queries(client, make_user, world, pump_model, dept, today):
    client.force_login(make_user("director"))

    def count():
        with CaptureQueriesContext(connection) as q:
            assert client.get(UPDATES).status_code == 200
        return len(q.captured_queries)

    before = count()
    for n in range(20, 26):
        a = Asset.objects.create(tag=f"CE-100{n}", device_model=pump_model, department=dept, installed_on=today - 100 * DAY)
        record(a, UpdateChoice(UpdateOutcome.PATCHES, reference="ICAR-2026-09", basis=UpdateBasis.NOT_STATED),
               installed_by=InstalledBy.AUTOMATIC, applied_on=today, by=world["tech"], today=today)
        update_wo(a, world["r56"], opened_on=today, due_on=today + DAY)
    assert count() == before


def test_the_levels_answer_in_words(client, tenant, make_user, world):
    url = UPDATES
    client.force_login(make_user("analyst"))  # Device security None
    r = client.get(url)
    assert r.status_code == 403 and r.json()["detail"] == api.UPDATES_REFUSAL
    client.force_login(custom_user(tenant, "models", {Module.CYBERSECURITY: Level.VIEW, Module.WORKORDERS: Level.VIEW}))  # no Equipment View
    r = client.get(url)
    assert r.status_code == 403 and r.json()["detail"] == api.UPDATES_REFUSAL
    client.force_login(custom_user(tenant, "reader", {Module.CYBERSECURITY: Level.VIEW, Module.EQUIPMENT: Level.VIEW}))
    assert ok(client.get(url))["count"] == 6
    for method in ("post", "put", "patch", "delete"):
        target = url if method == "post" else f"{url}{world['hand'].pk}/"
        r = getattr(client, method)(target, {}, content_type="application/json")
        assert r.status_code == 403 and r.json()["detail"] == api.UPDATES_WRITE_REFUSAL, method
    client.force_login(make_user("director"))
    for method in ("post", "put", "patch", "delete"):
        target = url if method == "post" else f"{url}{world['hand'].pk}/"
        r = getattr(client, method)(target, {}, content_type="application/json")
        assert r.status_code == 405 and r.json()["detail"] == api.UPDATES_READ_ONLY, method
    assert SoftwareUpdate.objects.count() == 7
    for text in (api.UPDATES_REFUSAL, api.UPDATES_WRITE_REFUSAL, api.UPDATES_READ_ONLY, api.UPDATE_NOT_FOUND, api.STATE_WORDS):
        assert never(text) == [], text


@pytest.mark.parametrize("scope, attrs", [(DataScope.COMPANY, {"company": "BD"}), (DataScope.DEPARTMENT, {"department": "ICU"})])
def test_never_a_scoped_user_s(client, tenant, world, scope, attrs):
    client.force_login(custom_user(tenant, f"scoped{scope}", {m: Level.FULL for m in Module.values}, scope=scope, **attrs))
    for url in (UPDATES, f"{UPDATES}{world['hand'].pk}/"):
        r = client.get(url)
        assert r.status_code == 403 and r.json()["detail"] == ModulePermission.SCOPED_REFUSAL, url


def test_a_token_reads_only_its_own_facility(client, world, other_tenant, today):
    create_default_roles(other_tenant)
    with tenant_context(other_tenant):
        dept = Department.objects.create(name="Their ICU")
        model = DeviceModel.objects.create(manufacturer="BD", model="Alaris 8015 PCU", description="Infusion pump", category="Infusion pumps")
        theirs = Asset.objects.create(tag="CE-10002", device_model=model, department=dept)  # the same tag in another facility
        their_release = release(model, "9.9")
        record_facts(theirs, by=None, release=release(model, "9.8"))
        their_wo, their_row = update_wo(theirs, their_release, opened_on=today, due_on=today + DAY)
        role = Role.objects.get(slug="director")
    them = User.objects.create_user(username="director@other.example", password="Test-Pass-2026-x", tenant=other_tenant, role=role)
    auth = {"HTTP_AUTHORIZATION": f"Token {Token.objects.create(user=them).key}"}
    rows = ok(client.get(UPDATES, **auth))["results"]
    assert [(r["device"]["tag"], r["target_release"]["version"], r["work_order"]["number"]) for r in rows] == [
        ("CE-10002", "9.9", their_wo.number)]
    assert ok(client.get(f"{UPDATES}?device=ce-10002", **auth))["count"] == 1
    assert client.get(f"{UPDATES}{world['hand'].pk}/", **auth).status_code == 404
    assert ok(client.get(f"{WORK_ORDERS}{their_wo.pk}/", **auth))["software_update"]["words"] == "Open: to install 9.9"
    assert client.get(f"{WORK_ORDERS}{world['wos']['CE-10002'].pk}/", **auth).status_code == 404
    mine = {"HTTP_AUTHORIZATION": f"Token {Token.objects.create(user=world['tech']).key}"}
    assert their_row.pk not in {r["id"] for r in ok(client.get(UPDATES, **mine))["results"]}


def test_the_serializer_words_say_no_never_word(world):
    for text in (scs.NOT_CONFIRMED_LABEL, *(label for _v, label in UpdateOutcome.choices), *(label for _v, label in UpdateBasis.choices),
                 *(label for _v, label in InstalledBy.choices), *rules.STATE_LABELS.values()):
        assert never(text) == [], text
    assert WorkOrder.objects.filter(type=WoType.SOFTWARE_UPDATE).count() == 5
