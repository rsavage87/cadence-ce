"""
Slice 38 wave 2 B: software updates on the device drawer's Security tab (apps/web/asset_cyber_updates.py, web/_asset_cyber_updates.html)
and the Device security screen's model panel (apps/web/cyber_release_actions.py, web/_cyber_release_actions.html), with their doors
(apps/web/views_cyber_updates.py, forms_cyber_updates.py, web/_cyber_update_*.html, web/_cyber_open_updates.html), on SQLite and
PostgreSQL.

- The tab: each state of an update in rules' words (rules.STATE_LABELS, rules.update_words), newest first; the work order's number
  always, a link only with Work orders View; Open software update, Record update, Confirm version, and Entered in error by level and by
  state; no priority chip; a fixed number of queries however many updates.
- The doors by level: the director, a technician, the analyst, a custom IT role with Device security Edit and Equipment View but Work
  orders None (records and confirms, never opens), and a scoped vendor (403 everywhere); each modal's GET and POST through the real
  views; every refusal on the field it names; the blockers in the services' words; the toasts' words; the drawer or the panel back with
  the events; another facility's rows a 404.
- The panel: a release's campaign line (counts only) and "Open updates to this release" where devices of the model on site run another
  release or none; the batch end to end (created, unassigned for a technician with the reason, assigned for the director, the skips
  named with their tags), the panel's filters kept.
- The never words over every rendered state, modal, and toast.
"""
import json
import re
import uuid
from datetime import timedelta
from html import unescape

import pytest
from cyber_fixtures import facts, release, update_wo
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.accounts.models import DataScope, Level, Module, Role, User
from apps.cybersecurity import rules, updates
from apps.cybersecurity.models import (
    DeviceSecurity,
    InstalledBy,
    SoftwareRelease,
    SoftwareUpdate,
    UpdateBasis,
    UpdateOutcome,
    UpdatesBy,
)
from apps.cybersecurity.updates import NOT_CONFIRMED, UpdateChoice
from apps.equipment.models import Asset, AssetStatus, Department, Ownership
from apps.tenants.context import tenant_context
from apps.web import asset_cyber_updates
from apps.web import forms_cyber_updates as forms
from apps.web import views_cyber_updates as views
from apps.workorders import services as wo_services
from apps.workorders.completion import complete_work_order
from apps.workorders.models import WorkOrder, WoStatus, WoType

DAY = timedelta(days=1)
HX = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "drawer"}
MODAL = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "modal-card"}
MR, VL, NS = UpdateBasis.MANUFACTURER_RELEASE, UpdateBasis.VALIDATED_LIST, UpdateBasis.NOT_STATED
INSTALLED, PATCHES, ALREADY = UpdateOutcome.INSTALLED, UpdateOutcome.PATCHES, UpdateOutcome.ALREADY
SEEN: list = []  # every body and toast rendered here, for the never-words scan


# --- helpers ------------------------------------------------------------------------------------------------------------------------

def never(text: str) -> list:
    """rules.NEVER_WORDS found in `text` as whole words, in any letter case (none may be)."""
    return [w for w in rules.NEVER_WORDS if re.search(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", unescape(text), re.IGNORECASE)]


def html(r) -> str:
    body = r.content.decode()
    SEEN.append(body)
    assert not never(body), never(body)
    return body


def text(r) -> str:
    """The body as a reader sees it: tags out, entities read, spaces collapsed."""
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html(r))).split())


def triggers(r) -> dict:
    out = {}
    for h in ("HX-Trigger", "HX-Trigger-After-Settle"):
        if h in r:
            out.update(json.loads(r[h]))
    return out


def toast_of(r) -> str:
    message = triggers(r).get("toast", {}).get("value", "")
    SEEN.append(message)
    assert not never(message), never(message)
    return message


def errors(body: str) -> dict:
    """{field name: its error} from a modal (ids "cu-<name>_error"), "" for the one at the top."""
    out = {m.group(1): unescape(m.group(2)) for m in re.finditer(r'<span class="err" id="cu-(\w+)_error">([^<]*)</span>', body)}
    top = re.search(r'<span class="err" role="alert">([^<]*)</span>', body)
    if top:
        out[""] = unescape(top.group(1))
    return out


def section(body: str) -> str:
    """The Security tab's "Software updates" section alone."""
    start = body.index('<div class="sec cy-ups">')
    return body[start:body.index('<div class="sec">', start)]


def blocker_of(body: str) -> str:
    found = re.search(r'<div class="full note" role="alert">([^<]*)</div>', body)
    return unescape(found.group(1)) if found else ""


def saved_drawer(r, tag: str):
    """A door saved: the device's drawer on its Security tab into #drawer, the events, the modal's close after settle."""
    assert r.status_code == 200 and r["HX-Retarget"] == "#drawer", r.content.decode()[:600]
    assert r.context["tab"] == "security" and r.context["asset"].tag == tag
    fired = triggers(r)
    assert {"cyber-changed", "wo-changed", "toast", "modal-close"} <= set(fired)
    assert json.loads(r["HX-Trigger-After-Settle"]) == {"modal-close": {}}


def custom_user(tenant, slug, levels, scope="", **extra):
    role = Role.objects.create(name=slug.title(), slug=slug, scope=scope)
    role.set_levels({m: levels.get(m, Level.NONE) for m in Module.values})
    return User.objects.create_user(username=f"{slug}@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role,
                                    first_name=slug.title(), last_name="User", **extra)


def tab(client, asset):
    return client.get(f"/equipment/{asset.tag}/?tab=security", **HX)


def open_url(asset) -> str:
    return f"/security/devices/{asset.tag}/updates/open/"


def record_url(asset) -> str:
    return f"/security/devices/{asset.tag}/updates/record/"


def confirm_url(row) -> str:
    return f"/security/updates/{row.pk}/confirm/"


def remove_url(row) -> str:
    return f"/security/updates/{row.pk}/remove/"


def batch_url(rel, query: str = "") -> str:
    return f"/security/releases/{rel.pk}/open-updates/" + (f"?{query}" if query else "")


def complete(wo, *, software=None, by=None, resolution="Done", today=None):
    from apps.cybersecurity.services import KEEP

    return complete_work_order(wo, resolution=resolution, software=KEEP if software is None else software, by=by,
                               today=today or timezone.localdate())


def fresh(row) -> SoftwareUpdate:
    return SoftwareUpdate.objects.select_related("work_order", "target_release", "release_before", "release_after").get(pk=row.pk)


# --- fixtures -----------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def today(ctx):
    return timezone.localdate()


@pytest.fixture
def world(ctx, dept, pump_model, pump, techs, today):
    """The pump model's profile (clinical engineering installs its updates), 5.2 (demo) and 5.6 (demo), and 5.9 (demo) entered in
    error; CE-10002 on 5.2 (demo), checked a month ago."""
    profile = release(pump_model, updates_by=UpdatesBy.CE)
    r52 = release(pump_model, "5.2 (demo)")
    r56 = release(pump_model, "5.6 (demo)")
    gone = release(pump_model, "5.9 (demo)", removed_on=today)
    facts(pump, r52, release_since=today - 30 * DAY, checked_on=today - 30 * DAY)
    return {"profile": profile, "r52": r52, "r56": r56, "gone": gone, "pump": pump, "model": pump_model, "tom": techs["tom"],
            "dana": techs["dana"]}


@pytest.fixture
def users(tenant, make_user):
    """The roles the doors are walked with: the director, a technician, the manager, the analyst (Device security None), a custom IT
    role (Device security Edit, Equipment View, Work orders None), and a scoped vendor (BD)."""
    vendor = make_user("vendor")
    vendor.company = "BD"
    vendor.save(update_fields=["company"])
    return {"director": make_user("director"), "technician": make_user("technician"), "manager": make_user("manager"),
            "analyst": make_user("analyst"), "vendor": vendor,
            "it": custom_user(tenant, "itsec", {Module.CYBERSECURITY: Level.EDIT, Module.EQUIPMENT: Level.VIEW})}


@pytest.fixture
def states(world, today):
    """CE-10002 with an update in each state: cancelled (to install 5.6 (demo)), done at completion (5.6 (demo) installed, so the device
    now runs it), completed with the version not confirmed, one recorded by hand (patches by the manufacturer remotely), and one open (to
    apply the manufacturer's patches, ref ICAR-2026-10)."""
    pump, r56, tom = world["pump"], world["r56"], world["tom"]
    cancelled_wo, cancelled = update_wo(pump, r56, assigned_to=tom)
    wo_services.change_status(cancelled_wo, WoStatus.CANCELLED)
    done_wo, done = update_wo(pump, r56, assigned_to=tom)
    complete(done_wo, software=UpdateChoice(INSTALLED, str(r56.pk), basis=MR))
    nc_wo, not_confirmed = update_wo(pump, r56, assigned_to=tom)
    complete(nc_wo, software=UpdateChoice(NOT_CONFIRMED))
    hand = updates.record(pump, UpdateChoice(PATCHES, reference="ICAR-2026-09", basis=VL), installed_by=InstalledBy.MANUFACTURER,
                          applied_on=today, by=None)
    open_wo, open_row = update_wo(pump, None, reference="ICAR-2026-10", assigned_to=tom)
    return {"cancelled": cancelled, "done": done, "not_confirmed": not_confirmed, "hand": hand, "open": open_row,
            "numbers": {"cancelled": cancelled_wo.number, "done": done_wo.number, "not_confirmed": nc_wo.number, "open": open_wo.number}}


# --- the tab ------------------------------------------------------------------------------------------------------------------------

def test_the_tab_shows_each_state_in_rules_words_newest_first(client, world, users, states, today):
    client.force_login(users["director"])
    r = tab(client, world["pump"])
    body = html(r)
    ups = r.context["cyber"]["updates"]
    by_pk = {u["pk"]: u for u in ups["rows"]}
    day = rules.day_words(today)
    expected = {
        "done": ("done", "Done", f"5.6 (demo) installed {day} (recorded before: 5.2 (demo))"),
        "not_confirmed": ("not_confirmed", "Version not confirmed", f"Completed {day}; version not confirmed"),
        "hand": ("done", "Done", f"Patches on the manufacturer's validated patch list applied {day}, version unchanged (5.6 (demo)); "
                                 "ref ICAR-2026-09; installed by the manufacturer remotely"),
        "open": ("open", "Open", "Open: to apply the manufacturer's patches (ref ICAR-2026-10)"),
        "cancelled": ("cancelled", "Cancelled", "Cancelled"),
    }
    for key, (state, label, words) in expected.items():
        row = by_pk[states[key].pk]
        assert (row["state"], row["label"], row["words"]) == (state, label, words), key
        assert f'<span class="chip {row["tone"]}">{label}</span>' in body
        assert words in unescape(body)
    assert [u["pk"] for u in ups["rows"]] == [r.pk for r in rules.updates_of(world["pump"])]  # rules' order: newest first
    # the work order's number, a link for Work orders View; a hand row has none
    number = states["numbers"]["done"]
    assert f'<a class="link" href="/work-orders/{number}/" hx-get="/work-orders/{number}/" hx-target="#drawer">{number}</a>' in body
    assert by_pk[states["hand"].pk]["number"] == "" and by_pk[states["hand"].pk]["wo_url"] == ""
    # the actions: open and record for the device, Confirm on the not-confirmed row, Entered in error on the done ones
    assert 'hx-get="/security/devices/CE-10002/updates/open/" hx-target="#modal-card">Open software update</button>' in body
    assert 'hx-get="/security/devices/CE-10002/updates/record/" hx-target="#modal-card">Record update</button>' in body
    assert [k for k in expected if by_pk[states[k].pk]["confirm_url"]] == ["not_confirmed"]
    assert sorted(k for k in expected if by_pk[states[k].pk]["remove_url"]) == ["done", "hand"]
    assert f'hx-get="{confirm_url(states["not_confirmed"])}" hx-target="#modal-card">Confirm version</button>' in body
    assert f'hx-get="{remove_url(states["hand"])}" hx-target="#modal-card">Entered in error</button>' in body
    assert "Software updates <span>newest first</span>" in body
    part = section(body)
    assert "priority" not in part.lower() and "Normal" not in part and "High" not in part  # no priority chip


def test_the_tab_reads_a_fixed_number_of_queries(client, world, users, states, today):
    client.force_login(users["director"])

    def counted():
        with CaptureQueriesContext(connection) as q:
            assert tab(client, world["pump"]).status_code == 200
        return len(q)

    first = counted()
    pump, r56, tom = world["pump"], world["r56"], world["tom"]
    for i in range(3):  # more of every kind: cancelled, not confirmed, by hand, the vendor's, open
        wo, _row = update_wo(pump, r56, assigned_to=tom)
        wo_services.change_status(wo, WoStatus.CANCELLED)
        wo, _row = update_wo(pump, None, reference=f"ICAR-2026-0{i}", assigned_to=tom)
        complete(wo, software=UpdateChoice(NOT_CONFIRMED))
        updates.record(pump, UpdateChoice(PATCHES, reference=f"DEMO-{i}", basis=NS), installed_by=InstalledBy.IT, applied_on=today, by=None)
        vendor_wo, _row = update_wo(pump, None, reference=f"V-{i}", vendor_service=True, vendor_name="BD field service")
        complete(vendor_wo)
        update_wo(pump, r56, assigned_to=tom)
    assert len(tab(client, pump).context["cyber"]["updates"]["rows"]) == 5 + 15
    assert counted() == first


def test_no_update_yet_reads_the_empty_line(client, world, users):
    client.force_login(users["technician"])
    part = section(html(tab(client, world["pump"])))
    assert '<div class="empty">No software updates recorded yet.</div>' in part
    assert "Open software update</button>" in part and "Record update</button>" in part


@pytest.mark.parametrize("who, can", [
    ("director", {"open", "record", "confirm", "remove", "link"}),
    ("manager", {"open", "record", "confirm", "remove", "link"}),
    ("technician", {"open", "record", "confirm", "link"}),
    ("it", {"record", "confirm"}),  # Device security Edit and Equipment View, Work orders None: records and confirms, never opens
])
def test_the_tabs_actions_follow_the_levels(client, world, users, states, who, can):
    client.force_login(users[who])
    r = tab(client, world["pump"])
    part = section(html(r))
    seen = {name for name, shown in (("open", "Open software update</button>" in part), ("record", "Record update</button>" in part),
                                     ("confirm", "Confirm version</button>" in part), ("remove", "Entered in error</button>" in part),
                                     ("link", '<a class="link" href="/work-orders/' in part)) if shown}
    assert seen == can
    number = states["numbers"]["done"]
    assert number in part  # the number always, a link only with Work orders View
    if "link" not in can:
        assert f'<span class="cy-up-n">{number}</span>' in part
        assert all(not u["wo_url"] for u in r.context["cyber"]["updates"]["rows"])


@pytest.mark.parametrize("who", ["analyst", "vendor"])
def test_the_section_is_never_offered_without_the_register(client, world, users, states, who):
    client.force_login(users[who])
    r = tab(client, world["pump"])
    assert r.status_code in (200, 404)  # the vendor: no work of BD's on the pump, so not in its share
    if r.status_code == 200:  # the analyst: the drawer, never the Security tab
        body = html(r)
        assert "security" not in r.context["tabs"] and "cy-ups" not in body and "5.6 (demo)" not in body


def test_a_device_held_or_retired_offers_what_still_applies(client, world, users, states):
    client.force_login(users["director"])
    pump = world["pump"]
    Asset.objects.filter(pk=pump.pk).update(incident_hold=True)
    part = section(html(tab(client, pump)))
    assert "Open software update</button>" not in part and "Record update</button>" in part and "Confirm version</button>" in part
    Asset.objects.filter(pk=pump.pk).update(incident_hold=False, status=AssetStatus.RETIRED)
    part = section(html(tab(client, pump)))
    assert "Open software update</button>" not in part and "Record update</button>" not in part and "Confirm version</button>" not in part
    assert "Entered in error</button>" in part  # a recorded outcome can still be corrected


# --- the doors by level ------------------------------------------------------------------------------------------------------------

def door_urls(world, states) -> dict:
    return {"open": open_url(world["pump"]), "record": record_url(world["pump"]), "confirm": confirm_url(states["not_confirmed"]),
            "remove": remove_url(states["hand"]), "batch": batch_url(world["r52"])}


ALLOWED = {"director": {"open", "record", "confirm", "remove", "batch"}, "manager": {"open", "record", "confirm", "remove", "batch"},
           "technician": {"open", "record", "confirm", "batch"}, "it": {"record", "confirm"}, "analyst": set(), "vendor": set(),
           "scoped_full": set()}


@pytest.mark.parametrize("who", sorted(ALLOWED))
def test_each_door_answers_by_level(client, tenant, world, users, states, who):
    if who == "scoped_full":  # a unit's requester role with Full everywhere: the register is never a scoped user's
        users[who] = custom_user(tenant, "unitfull", {m: Level.FULL for m in Module.values}, scope=DataScope.DEPARTMENT, department="ICU")
    client.force_login(users[who])
    before = (WorkOrder.objects.count(), list(SoftwareUpdate.objects.order_by("pk").values_list("pk", "outcome", "removed_on")),
              list(DeviceSecurity.objects.values_list("release_id", "checked_on")))
    for name, url in door_urls(world, states).items():
        r = client.get(url, **MODAL)
        if name in ALLOWED[who]:
            assert r.status_code == 200, (name, url)
            html(r)
        else:
            assert r.status_code == 403, (name, url)
            for data in ({}, {"target": "patches", "target_reference": "X-1", "due_on": timezone.localdate().isoformat(),
                              "priority": "normal", "software_outcome": "installed", "software_release": str(world["r56"].pk),
                              "software_basis": MR, "installed_by": "it", "include_not_recorded": "on"}):
                assert client.post(url, data, **MODAL).status_code == 403, (name, url)
    after = (WorkOrder.objects.count(), list(SoftwareUpdate.objects.order_by("pk").values_list("pk", "outcome", "removed_on")),
             list(DeviceSecurity.objects.values_list("release_id", "checked_on")))
    assert after == before


def test_opened_directly_each_door_opens_the_drawer_or_the_panel(client, world, users, states, dept):
    client.force_login(users["director"])
    for name, url in door_urls(world, states).items():
        r = client.get(url)
        expected = (f"/security/models/{world['model'].pk}/" if name == "batch" else "/equipment/CE-10002/?tab=security")
        assert r.status_code == 302 and r["Location"] == expected, name
    r = client.get(batch_url(world["r52"], f"dept={dept.pk}"))
    assert r["Location"] == f"/security/models/{world['model'].pk}/?dept={dept.pk}"  # the panel's filters kept


def test_another_facilitys_rows_and_hidden_ones_are_a_404(client, world, users, states, other_tenant, pump_model, today):
    from apps.equipment.models import DeviceModel

    with tenant_context(other_tenant):
        dm = DeviceModel.objects.create(manufacturer="BD", model="Alaris 8015 PCU", description="Infusion pump", category="Infusion pumps")
        theirs = Asset.objects.create(tag="OT-1", device_model=dm, department=Department.objects.create(name="ICU"))
        their_release = release(dm, "5.6 (demo)")
        _wo, their_row = update_wo(theirs, their_release)
    client.force_login(users["director"])
    removed = updates.record(world["pump"], UpdateChoice(PATCHES, reference="X-9", basis=NS), installed_by=InstalledBy.IT, applied_on=today,
                             by=None)
    updates.remove(removed, by=None)
    for url in (confirm_url(their_row), remove_url(their_row), batch_url(their_release), open_url(theirs), record_url(theirs),
                f"/security/updates/{uuid.uuid4()}/confirm/", f"/security/updates/{uuid.uuid4()}/remove/",
                f"/security/releases/{uuid.uuid4()}/open-updates/", confirm_url(removed), remove_url(removed),
                batch_url(world["profile"]), batch_url(world["gone"]), "/security/devices/NOPE-1/updates/open/"):
        assert client.get(url, **MODAL).status_code == 404, url
        assert client.post(url, {"priority": "normal"}, **MODAL).status_code == 404, url
    with tenant_context(other_tenant):
        assert WorkOrder.objects.count() == 1 and SoftwareUpdate.objects.get().outcome == ""


# --- Open software update ----------------------------------------------------------------------------------------------------------

def open_data(world, today, **changes) -> dict:
    data = {"target": "release", "target_release": str(world["r56"].pk), "target_reference": "", "due_on": (today + 10 * DAY).isoformat(),
            "priority": "normal"}
    data.update(changes)
    return {k: v for k, v in data.items() if v is not None}


def test_the_open_modal_offers_the_models_other_releases_normal_or_high_and_assign_to(client, world, users, today):
    client.force_login(users["director"])
    r = client.get(open_url(world["pump"]), **MODAL)
    body = html(r)
    form = r.context["form"]
    assert form.fields["target_release"].widget.choices == [("", "Choose…"), (str(world["r56"].pk), "5.6 (demo)")]  # never 5.2 (on record) or 5.9
    assert [c[0] for c in form.fields["target"].widget.choices] == ["release", "patches"] and form["target"].value() == "release"
    assert form["due_on"].value() == (today + 30 * DAY).isoformat() and form["priority"].value() == "normal"
    assert form.fields["priority"].widget.choices == [("normal", "Normal"), ("high", "High")] and "Critical" not in body
    assert "Drug libraries are not recorded here" in unescape(body)
    labels = [label for _v, label in form.fields["assign_to"].widget.choices]
    assert labels == ["Leave unassigned", "Dana Whitfield (product category: Infusion pumps)", "Tom Okafor (product category: Infusion pumps, expiring)"]
    assert r.context["route"] == "" and "cu-route" not in body
    assert 'hx-post="/security/devices/CE-10002/updates/open/" hx-target="#modal-card"' in body


def test_a_technician_opens_one_left_unassigned_in_the_services_words(client, world, users, today):
    client.force_login(users["technician"])
    r = client.get(open_url(world["pump"]), **MODAL)
    body = html(r)
    assert "assign_to" not in r.context["form"].fields and r.context["route"] == updates.ASSIGN_NEEDS and updates.ASSIGN_NEEDS in unescape(body)
    r = client.post(open_url(world["pump"]), open_data(world, today, target="patches", target_reference="ICAR-2026-09",
                                                       target_release=str(world["r56"].pk), priority="high"), **MODAL)
    saved_drawer(r, "CE-10002")
    wo = WorkOrder.objects.get(type=WoType.SOFTWARE_UPDATE)
    row = wo.software_update
    assert (row.target, row.target_release, row.target_reference) == ("patches", None, "ICAR-2026-09")  # patches name no release
    assert wo.assigned_to is None and wo.priority == "high" and wo.due_on == today + 10 * DAY and wo.problem == updates.PATCHES_PROBLEM
    assert toast_of(r) == (f"{wo.number} opened for CE-10002. Open: to apply the manufacturer's patches (ref ICAR-2026-09). "
                           "Left unassigned: assigning needs Work orders Approve, so a manager assigns it or a technician takes it.")
    rows = r.context["cyber"]["updates"]["rows"]
    assert [(u["number"], u["label"]) for u in rows] == [(wo.number, "Open")]  # the drawer's tab lists it


def test_the_director_opens_one_assigned(client, world, users, today):
    client.force_login(users["director"])
    r = client.post(open_url(world["pump"]), open_data(world, today, target_reference="DROPPED-1", assign_to=str(world["tom"].pk)), **MODAL)
    saved_drawer(r, "CE-10002")
    wo = WorkOrder.objects.get(type=WoType.SOFTWARE_UPDATE)
    assert wo.assigned_to == world["tom"] and wo.problem == updates.UPDATE_PROBLEM and wo.requester == str(users["director"])
    assert (wo.software_update.target_release, wo.software_update.target_reference) == (world["r56"], "")  # a release names no patch list
    assert toast_of(r) == f"{wo.number} opened for CE-10002. Open: to install 5.6 (demo)."
    for wo in WorkOrder.objects.all():
        wo_services.change_status(wo, WoStatus.CANCELLED)
    r = client.post(open_url(world["pump"]), open_data(world, today), **MODAL)  # nobody chosen
    assert toast_of(r).endswith("Left unassigned: nobody was chosen to assign, so a manager assigns it or a technician takes it.")


@pytest.mark.parametrize("changes, expected", [
    ({"target": ""}, {"target": updates.TARGET_WORDS}),
    ({"target": "firmware"}, {"target": updates.TARGET_WORDS}),
    ({"target_release": ""}, {"target_release": updates.CHOOSE_TARGET}),
    ({"target_release": "gone"}, {"target_release": updates.CHOOSE_TARGET}),
    ({"target_release": "r52"}, {"target_release": updates.ALREADY_RUNS.format(tag="CE-10002", version="5.2 (demo)")}),
    ({"target": "patches"}, {"target_reference": updates.REFERENCE_NEEDED}),
    ({"target": "patches", "target_reference": "ICAR 2026"}, {"target_reference": "validator"}),
    ({"due_on": ""}, {"due_on": updates.DUE_NEEDED}),
    ({"due_on": "past"}, {"due_on": updates.DUE_PAST}),
    ({"due_on": "2999-01-01"}, {"due_on": updates.DUE_FAR}),
    ({"priority": "critical"}, {"priority": updates.PRIORITY_WORDS}),
    ({"priority": ""}, {"priority": updates.PRIORITY_WORDS}),
    ({"assign_to": "nobody"}, {"assign_to": updates.CHOOSE_TECHNICIAN}),
])
def test_each_open_refusal_comes_back_on_its_field(client, world, users, today, changes, expected):
    from apps.cybersecurity.models import UPDATE_REFERENCE_VALIDATOR

    client.force_login(users["director"])
    values = {"gone": str(world["gone"].pk), "r52": str(world["r52"].pk), "past": (today - DAY).isoformat(), "nobody": str(uuid.uuid4())}
    data = open_data(world, today, **{k: values.get(v, v) for k, v in changes.items()})
    r = client.post(open_url(world["pump"]), data, **MODAL)
    body = html(r)
    assert r.status_code == 200 and "HX-Retarget" not in r and not WorkOrder.objects.exists()
    want = {k: (UPDATE_REFERENCE_VALIDATOR.message if v == "validator" else v) for k, v in expected.items()}
    assert errors(body) == want
    if data.get("target_reference"):
        assert f'value="{data["target_reference"]}"' in body  # what was typed is kept


def test_open_blockers_and_a_device_that_changed_meanwhile(client, world, users, today):
    pump = world["pump"]
    client.force_login(users["director"])
    wo, _row = update_wo(pump, world["r56"])
    assert blocker_of(html(client.get(open_url(pump), **MODAL))) == updates.ALREADY_OPEN.format(tag="CE-10002", number=wo.number)
    wo_services.change_status(wo, WoStatus.CANCELLED)
    page = html(client.get(open_url(pump), **MODAL))
    assert not blocker_of(page) and 'id="cu-open-form"' in page
    Asset.objects.filter(pk=pump.pk).update(incident_hold=True)  # held meanwhile: the service's refusal at the top
    r = client.post(open_url(pump), open_data(world, today), **MODAL)
    assert errors(html(r)) == {"": updates.HELD.format(tag="CE-10002")}
    assert blocker_of(html(client.get(open_url(pump), **MODAL))) == updates.HELD.format(tag="CE-10002")
    Asset.objects.filter(pk=pump.pk).update(incident_hold=False, status=AssetStatus.RETIRED)
    assert blocker_of(html(client.get(open_url(pump), **MODAL))) == "CE-10002 is retired: its facts stay as they were."
    assert WorkOrder.objects.filter(status=WoStatus.OPEN).count() == 0


@pytest.mark.parametrize("updates_by, route, offered", [
    (UpdatesBy.IT, updates.INSTALLED_HERE.format(who="information technology"), False),
    (UpdatesBy.DEPARTMENT, updates.INSTALLED_HERE.format(who="the department that uses it"), False),
    (UpdatesBy.MANUFACTURER, forms.VENDOR_ROUTE.format(vendor="BD field service"), False),
    ("", "", True),  # not recorded: clinical engineering's choice
])
def test_open_routes_by_who_installs_updates_here(client, world, users, today, updates_by, route, offered):
    SoftwareRelease.objects.filter(pk=world["profile"].pk).update(updates_by=updates_by)
    client.force_login(users["director"])
    r = client.get(open_url(world["pump"]), **MODAL)
    body = html(r)
    assert ("assign_to" in r.context["form"].fields) is offered and r.context["route"] == route
    if route:
        assert f'<p class="full note cu-route">{route}</p>' in unescape(body)
    r = client.post(open_url(world["pump"]), open_data(world, today), **MODAL)
    wo = WorkOrder.objects.get(type=WoType.SOFTWARE_UPDATE)
    message = toast_of(r)
    if updates_by == UpdatesBy.MANUFACTURER:
        assert wo.vendor_service and wo.vendor_name == "BD field service" and message.endswith("Open: to install 5.6 (demo).")
    elif updates_by:
        assert wo.assigned_to is None and not wo.vendor_service and message.endswith(route)
    else:
        assert message.endswith("Left unassigned: nobody was chosen to assign, so a manager assigns it or a technician takes it.")


def test_a_temporary_device_goes_to_its_owner(client, world, users, dept, pump_model, today):
    rental = Asset.objects.create(tag="CE-RENT-1", device_model=pump_model, department=dept, ownership=Ownership.RENTAL, owner="RentCo Medical")
    client.force_login(users["director"])
    r = client.get(open_url(rental), **MODAL)
    route = forms.OWNER_ROUTE.format(vendor="RentCo Medical", kind="rental")
    assert "assign_to" not in r.context["form"].fields and r.context["route"] == route and route in unescape(html(r))
    r = client.post(open_url(rental), open_data(world, today), **MODAL)
    saved_drawer(r, "CE-RENT-1")
    assert WorkOrder.objects.get().vendor_name == "RentCo Medical"


# --- Record update -----------------------------------------------------------------------------------------------------------------

def record_data(world, today, **changes) -> dict:
    data = {"software_outcome": "installed", "software_release": str(world["r56"].pk), "software_reference": "", "software_basis": MR,
            "installed_by": "it", "applied_on": today.isoformat()}
    data.update(changes)
    return data


def test_the_record_modal_and_an_install_by_it(client, world, users, today):
    client.force_login(users["it"])  # the IT role records with no work order
    r = client.get(record_url(world["pump"]), **MODAL)
    body = html(r)
    form = r.context["form"]
    assert [v for v, _l in form.fields["software_outcome"].widget.choices] == ["installed", "patches"]  # never "already" by hand
    assert form.fields["software_release"].widget.choices == [("", "Choose…"), (str(world["r52"].pk), "5.2 (demo) (the device's record now)"),
                                                              (str(world["r56"].pk), "5.6 (demo)")]
    assert form["software_release"].value() == str(world["r52"].pk) and form["applied_on"].value() == today.isoformat()
    assert [v for v, _l in form.fields["installed_by"].widget.choices] == ["", *InstalledBy.values]
    assert [v for v, _l in form.fields["software_basis"].widget.choices] == ["", *UpdateBasis.values]
    assert 'hx-post="/security/devices/CE-10002/updates/record/"' in body
    r = client.post(record_url(world["pump"]), record_data(world, today), **MODAL)
    saved_drawer(r, "CE-10002")
    day = rules.day_words(today)
    assert toast_of(r) == (f"Update recorded for CE-10002: 5.6 (demo) installed {day} (recorded before: 5.2 (demo)); installed by "
                           "information technology")
    assert DeviceSecurity.objects.get(asset=world["pump"]).release == world["r56"]
    row = SoftwareUpdate.objects.get()
    assert (row.work_order, row.installed_by, row.recorded_by) == (None, "it", users["it"])


def test_patches_by_hand_keep_the_version_whatever_release_was_left_chosen(client, world, users, today):
    client.force_login(users["technician"])
    r = client.post(record_url(world["pump"]), record_data(world, today, software_outcome="patches", software_reference="DEMO-PATCH-2026-09",
                                                           software_basis=VL, installed_by="manufacturer"), **MODAL)
    saved_drawer(r, "CE-10002")
    assert toast_of(r) == (f"Update recorded for CE-10002: Patches on the manufacturer's validated patch list applied {rules.day_words(today)}, "
                           "version unchanged (5.2 (demo)); ref DEMO-PATCH-2026-09; installed by the manufacturer remotely")
    assert DeviceSecurity.objects.get(asset=world["pump"]).release == world["r52"]


@pytest.mark.parametrize("changes, expected", [
    ({"software_outcome": ""}, {"software_outcome": updates.HAND_OUTCOME}),
    ({"software_outcome": "already"}, {"software_outcome": updates.HAND_OUTCOME}),
    ({"software_release": ""}, {"software_release": updates.CHOOSE_RUNS_NOW}),
    ({"software_release": "gone"}, {"software_release": updates.CHOOSE_RUNS_NOW}),
    ({"software_release": "not-an-id"}, {"software_release": updates.CHOOSE_RUNS_NOW}),
    ({"software_basis": VL}, {"software_basis": updates.VALIDATED_PATCHES_ONLY}),
    ({"software_basis": ""}, {"software_basis": updates.BASIS_WORDS}),
    ({"software_outcome": "patches"}, {"software_reference": updates.REFERENCE_NEEDED}),
    ({"installed_by": ""}, {"installed_by": updates.INSTALLER_WORDS}),
    ({"installed_by": "vendor"}, {"installed_by": updates.INSTALLER_WORDS}),
    ({"applied_on": ""}, {"applied_on": updates.APPLIED_NEEDED}),
    ({"applied_on": "tomorrow"}, {"applied_on": "Installed on cannot be after today."}),
    ({"applied_on": "before"}, {"applied_on": "before"}),
])
def test_each_record_refusal_comes_back_on_its_field(client, world, users, today, changes, expected):
    pump = world["pump"]
    Asset.objects.filter(pk=pump.pk).update(installed_on=today - 100 * DAY)
    client.force_login(users["director"])
    values = {"r52": str(world["r52"].pk), "gone": str(world["gone"].pk), "tomorrow": (today + DAY).isoformat(),
              "before": (today - 101 * DAY).isoformat()}
    r = client.post(record_url(pump), record_data(world, today, **{k: values.get(v, v) for k, v in changes.items()}), **MODAL)
    body = html(r)
    assert r.status_code == 200 and "HX-Retarget" not in r and not SoftwareUpdate.objects.exists()
    before_words = updates.BEFORE_INSTALL.format(day=rules.day_words(today - 100 * DAY))
    assert errors(body) == {k: (before_words if v == "before" else v) for k, v in expected.items()}


def test_a_record_dated_before_the_devices_facts_keeps_the_later_record(client, world, users, today):
    DeviceSecurity.objects.filter(asset=world["pump"]).update(checked_on=today)
    client.force_login(users["director"])
    r = client.post(record_url(world["pump"]), record_data(world, today, applied_on=(today - 10 * DAY).isoformat()), **MODAL)
    saved_drawer(r, "CE-10002")
    message = toast_of(r)
    assert message.endswith("; installed by information technology; a later record of the device is kept")
    assert message.count(rules.LATER_KEPT_WORDS) == 1 and SoftwareUpdate.objects.get().later_kept
    assert DeviceSecurity.objects.get(asset=world["pump"]).release == world["r52"]


def test_record_on_a_device_not_on_site(client, world, users, today, dept, pump_model):
    client.force_login(users["director"])
    pump = world["pump"]
    page = html(client.get(record_url(pump), **MODAL))
    assert 'id="cu-record-form"' in page
    Asset.objects.filter(pk=pump.pk).update(status=AssetStatus.RETIRED)  # retired meanwhile: the service's words at the top
    assert errors(html(client.post(record_url(pump), record_data(world, today), **MODAL))) == {
        "": "CE-10002 is retired: its facts stay as they were."}
    assert blocker_of(html(client.get(record_url(pump), **MODAL))) == "CE-10002 is retired: its facts stay as they were."
    returned = Asset.objects.create(tag="CE-RENT-2", device_model=pump_model, department=dept, ownership=Ownership.RENTAL, owner="RentCo",
                                    status=AssetStatus.RETIRED)
    assert blocker_of(html(client.get(record_url(returned), **MODAL))) == "CE-RENT-2 was returned to its owner: its facts stay as they were."
    assert not SoftwareUpdate.objects.exists()


# --- Confirm version ---------------------------------------------------------------------------------------------------------------

@pytest.fixture
def unconfirmed(world, today):
    """An update to install 5.6 (demo) on CE-10002 (on 5.2 (demo)), completed five days ago with the version not confirmed."""
    wo, row = update_wo(world["pump"], world["r56"], assigned_to=world["tom"], opened_on=today - 10 * DAY)
    complete(wo, software=UpdateChoice(NOT_CONFIRMED), today=today - 5 * DAY)
    wo.refresh_from_db()
    return wo, row


def confirm_data(world, **changes) -> dict:
    data = {"software_outcome": "installed", "software_release": str(world["r56"].pk), "software_reference": "", "software_basis": MR}
    data.update(changes)
    return data


def test_the_confirm_modal_and_a_confirmation(client, world, users, unconfirmed, today):
    wo, row = unconfirmed
    client.force_login(users["it"])
    r = client.get(confirm_url(row), **MODAL)
    body = html(r)
    form = r.context["form"]
    done_day = rules.day_words(wo.completed_on)
    assert f"Completed {done_day}; version not confirmed. Opened to install 5.6 (demo)." in unescape(body)
    assert [v for v, _l in form.fields["software_outcome"].widget.choices] == ["installed", "patches", "already"]
    assert form["software_release"].value() == str(world["r56"].pk)  # the target, chosen to start with
    assert (str(world["r52"].pk), "5.2 (demo) (the device's record now)") in form.fields["software_release"].widget.choices
    assert f"<h2>{wo.number}: confirm version</h2>" in body
    r = client.post(confirm_url(row), confirm_data(world), **MODAL)
    saved_drawer(r, "CE-10002")
    assert toast_of(r) == f"{wo.number} confirmed: 5.6 (demo) installed {done_day} (recorded before: 5.2 (demo))"
    assert DeviceSecurity.objects.get(asset=world["pump"]).release == world["r56"] and fresh(row).recorded_by == users["it"]
    rows = r.context["cyber"]["updates"]["rows"]
    assert [(u["label"], u["confirm_url"]) for u in rows] == [("Done", "")]


def test_a_confirmation_dated_before_the_devices_facts_keeps_the_later_record(client, world, users, unconfirmed, today):
    wo, row = unconfirmed
    DeviceSecurity.objects.filter(asset=world["pump"]).update(checked_on=today - DAY)  # checked after the work order's completion
    client.force_login(users["director"])
    r = client.post(confirm_url(row), confirm_data(world), **MODAL)
    saved_drawer(r, "CE-10002")
    assert toast_of(r) == f"{wo.number} confirmed: 5.6 (demo) installed {rules.day_words(wo.completed_on)}; {rules.LATER_KEPT_WORDS}"
    assert DeviceSecurity.objects.get(asset=world["pump"]).release == world["r52"]


def test_already_on_the_update_sends_no_basis_and_the_vendors_job_says_so(client, world, users, today):
    wo, row = update_wo(world["pump"], None, reference="ICAR-2026-09", vendor_service=True, vendor_name="BD field service",
                        checks_reference="FSR-7781")
    complete(wo)  # the vendor's job: nothing recorded
    client.force_login(users["director"])
    r = client.post(confirm_url(row), confirm_data(world, software_outcome="already", software_release=str(world["r52"].pk)), **MODAL)
    saved_drawer(r, "CE-10002")  # the basis left chosen is not sent ("already" names none)
    assert toast_of(r) == (f"{wo.number} confirmed: Checked {rules.day_words(today)}: already on 5.2 (demo); nothing installed; as the "
                           "vendor's report FSR-7781 states")
    assert fresh(row).basis == ""


@pytest.mark.parametrize("changes, expected", [
    ({"software_outcome": ""}, {"software_outcome": updates.CONFIRM_OUTCOME}),
    ({"software_outcome": "not_confirmed"}, {"software_outcome": updates.CONFIRM_OUTCOME}),
    ({"software_release": ""}, {"software_release": updates.CHOOSE_RUNS_NOW}),
    ({"software_basis": VL}, {"software_basis": updates.VALIDATED_PATCHES_ONLY}),
    ({"software_outcome": "patches"}, {"software_reference": updates.REFERENCE_NEEDED}),
    ({"software_outcome": "already"}, {"software_release": updates.RECORDED_AS.format(version="5.2 (demo)")}),  # never NO_BASIS
    ({"software_outcome": "already", "software_release": "r52"},
     {"software_release": updates.NOT_THE_TARGET_DONE.format(version="5.2 (demo)", target="5.6 (demo)")}),
])
def test_each_confirm_refusal_comes_back_on_its_field(client, world, users, unconfirmed, changes, expected):
    _wo, row = unconfirmed
    client.force_login(users["technician"])
    values = {"r52": str(world["r52"].pk)}
    r = client.post(confirm_url(row), confirm_data(world, **{k: values.get(v, v) for k, v in changes.items()}), **MODAL)
    body = html(r)
    assert r.status_code == 200 and "HX-Retarget" not in r and fresh(row).outcome == ""
    assert errors(body) == expected


def test_confirm_blockers_in_the_services_words(client, world, users, states, unconfirmed):
    client.force_login(users["director"])
    numbers = states["numbers"]
    assert blocker_of(html(client.get(confirm_url(states["open"]), **MODAL))) == updates.REOPENED.format(number=numbers["open"])
    assert blocker_of(html(client.get(confirm_url(states["done"]), **MODAL))) == updates.RECORDED_MEANWHILE
    assert blocker_of(html(client.get(confirm_url(states["hand"]), **MODAL))) == updates.CONFIRM_WHAT
    assert blocker_of(html(client.get(confirm_url(states["cancelled"]), **MODAL))) == updates.CONFIRM_WHAT
    _wo, row = unconfirmed
    r = client.post(confirm_url(states["done"]), confirm_data(world), **MODAL)  # posted anyway: the service's refusal, nothing written
    assert errors(html(r)) == {"software_outcome": updates.RECORDED_MEANWHILE}
    Asset.objects.filter(pk=world["pump"].pk).update(status=AssetStatus.RETIRED)
    assert blocker_of(html(client.get(confirm_url(row), **MODAL))) == "CE-10002 is retired: its facts stay as they were."


# --- Entered in error ---------------------------------------------------------------------------------------------------------------

def test_entered_in_error_on_a_hand_row_hides_it_and_keeps_the_facts(client, world, users, states):
    client.force_login(users["manager"])
    hand = states["hand"]
    facts_before = list(DeviceSecurity.objects.values_list("release_id", "release_since", "checked_on"))
    r = client.get(remove_url(hand), **MODAL)
    body = unescape(html(r))
    assert "This update recorded by hand on CE-10002 stays on record, marked entered in error" in body
    assert "CE-10002's facts stay as they are: correct its release with Record facts if it should change." in body
    assert rules.update_words(fresh(hand)) in body
    r = client.post(remove_url(hand), {}, **MODAL)
    saved_drawer(r, "CE-10002")
    assert toast_of(r) == views.HAND_REMOVED.format(tag="CE-10002")
    assert fresh(hand).removed_on is not None and fresh(hand).removed_by == users["manager"]
    assert hand.pk not in {u["pk"] for u in r.context["cyber"]["updates"]["rows"]}
    assert list(DeviceSecurity.objects.values_list("release_id", "release_since", "checked_on")) == facts_before
    assert client.get(remove_url(hand), **MODAL).status_code == 404  # hidden from now on


def test_entered_in_error_on_a_work_orders_outcome_offers_confirm_again(client, world, users, states):
    client.force_login(users["director"])
    done = states["done"]
    number = states["numbers"]["done"]
    body = unescape(html(client.get(remove_url(done), **MODAL)))
    assert f"What {number} recorded is cleared" in body and f"<h2>{number}: entered in error</h2>" in body
    r = client.post(remove_url(done), {}, **MODAL)
    saved_drawer(r, "CE-10002")
    assert toast_of(r) == views.OUTCOME_REMOVED.format(number=number)
    row = next(u for u in r.context["cyber"]["updates"]["rows"] if u["pk"] == done.pk)
    assert (row["label"], row["confirm_url"], row["remove_url"]) == ("Version not confirmed", confirm_url(done), "")
    assert DeviceSecurity.objects.get(asset=world["pump"]).release == world["r56"]  # the facts stay


def test_entered_in_error_with_nothing_recorded(client, world, users, states):
    client.force_login(users["director"])
    row = states["not_confirmed"]
    assert blocker_of(html(client.get(remove_url(row), **MODAL))) == updates.NOTHING_RECORDED
    r = client.post(remove_url(row), {}, **MODAL)
    assert re.search(r'<span class="err" role="alert">([^<]*)</span>', html(r)).group(1) == updates.NOTHING_RECORDED
    assert "HX-Retarget" not in r


# --- the model panel: a release's campaign ------------------------------------------------------------------------------------------

def panel(client, world, query: str = ""):
    return client.get(f"/security/models/{world['model'].pk}/" + (f"?{query}" if query else ""), **HX)


def release_row(r, rel) -> dict:
    return next(x for x in r.context["releases"] if x["r"].pk == rel.pk)


@pytest.fixture
def fleet(world, dept, pump_model, today):
    """The pump model's devices on site: CE-10002 (the world's), P-1 and P-2 on 5.2 (demo), P-3 with none recorded, P-4 on 5.6 (demo),
    P-H on 5.2 (demo) held as evidence, P-O on 5.2 (demo) with an open update already (to 5.6 (demo)), all in the ICU; P-5 on 5.2 (demo)
    in 4 West."""
    west = Department.objects.create(name="4 West")
    devices = {}
    for tag, department in (("P-1", dept), ("P-2", dept), ("P-3", dept), ("P-4", dept), ("P-H", dept), ("P-O", dept), ("P-5", west)):
        devices[tag] = Asset.objects.create(tag=tag, device_model=pump_model, department=department, next_pm_on=today + 40 * DAY)
    for tag in ("P-1", "P-2", "P-H", "P-O", "P-5"):
        facts(devices[tag], world["r52"])
    facts(devices["P-4"], world["r56"])
    Asset.objects.filter(pk=devices["P-H"].pk).update(incident_hold=True)
    open_wo, _row = update_wo(devices["P-O"], world["r56"])
    return {"devices": devices, "west": west, "open_wo": open_wo}


def test_a_releases_campaign_counts_its_updates_and_offers_the_batch(client, world, users, fleet, dept, today):
    pump, r56, tom = world["pump"], world["r56"], world["tom"]
    cancelled, _row = update_wo(pump, r56, assigned_to=tom)
    wo_services.change_status(cancelled, WoStatus.CANCELLED)
    nc, _row = update_wo(fleet["devices"]["P-1"], r56, assigned_to=tom)
    complete(nc, software=UpdateChoice(NOT_CONFIRMED))
    done, _row = update_wo(fleet["devices"]["P-2"], r56, assigned_to=tom)
    complete(done, software=UpdateChoice(INSTALLED, str(r56.pk), basis=MR))
    client.force_login(users["director"])
    r = panel(client, world, f"dept={dept.pk}")
    body = html(r)
    row56, row52 = release_row(r, r56), release_row(r, world["r52"])
    assert row56["update"]["words"] == "Updates to 5.6 (demo): 1 open, 1 done, 1 not confirmed, 1 cancelled"
    assert row52["update"]["words"] == ""  # nothing targets it
    assert row56["update"]["open_url"] == f"/security/releases/{r56.pk}/open-updates/?dept={dept.pk}"  # the panel's filters kept
    assert row56["update"]["can_open"] and row52["update"]["can_open"]
    assert '<span class="cy-campaign-w">Updates to 5.6 (demo): 1 open, 1 done, 1 not confirmed, 1 cancelled</span>' in body
    assert (f'hx-get="/security/releases/{r56.pk}/open-updates/?dept={dept.pk}" hx-target="#modal-card">Open updates to this release'
            "</button>") in body
    assert r.context["can_open_updates"]


def test_the_campaign_reads_counts_at_view_and_the_batch_only_where_devices_run_another(client, world, users, tenant, dept, pump_model):
    _wo, _row = update_wo(world["pump"], world["r56"])
    custom_user(tenant, "itread", {Module.CYBERSECURITY: Level.VIEW})  # Device security View alone: counts, no device, no batch
    client.force_login(User.objects.get(username="itread@riverside.example"))
    r = panel(client, world)
    assert release_row(r, world["r56"])["update"] == {"words": "Updates to 5.6 (demo): 1 open", "can_open": False,
                                                      "open_url": f"/security/releases/{world['r56'].pk}/open-updates/"}
    assert "Open updates to this release" not in html(r) and "CE-10002" not in html(r)
    client.force_login(users["it"])  # records, never opens
    r = panel(client, world)
    assert "Updates to 5.6 (demo): 1 open" in html(r) and "Open updates to this release" not in html(r)
    client.force_login(users["technician"])
    r = panel(client, world)
    assert release_row(r, world["r56"])["update"]["can_open"]  # CE-10002 runs 5.2 (demo)
    assert not release_row(r, world["r52"])["update"]["can_open"]  # every device on site runs 5.2 (demo) already


def test_the_panel_reads_the_campaigns_in_a_fixed_number_of_queries(client, world, users, fleet):
    client.force_login(users["director"])

    def counted():
        with CaptureQueriesContext(connection) as q:
            assert panel(client, world).status_code == 200
        return len(q)

    first = counted()
    for tag in ("P-1", "P-2", "P-3", "P-4"):
        wo, _row = update_wo(fleet["devices"][tag], world["r56"] if tag != "P-4" else world["r52"])
        if tag in ("P-2", "P-4"):
            wo_services.change_status(wo, WoStatus.CANCELLED)
    assert counted() == first


# --- the batch: Open updates to this release ----------------------------------------------------------------------------------------

def batch_data(dept, today, *, releases=(), **changes) -> dict:
    data = {"from_releases": [str(r.pk) for r in releases], "department": str(dept.pk), "due_on": (today + 30 * DAY).isoformat(),
            "priority": "normal"}
    data.update(changes)
    return data


def test_the_batch_modal_offers_the_models_other_releases_with_their_devices(client, world, users, fleet, dept, today):
    client.force_login(users["director"])
    r = client.get(batch_url(world["r56"], f"dept={dept.pk}"), **MODAL)
    body = html(r)
    form = r.context["form"]
    assert form.fields["from_releases"].choices == [(str(world["r52"].pk), "5.2 (demo) (6 devices on site)")]  # never 5.6 or 5.9
    assert form.fields["include_not_recorded"].label == "Release not recorded (1 device on site)"
    assert r.context["already"] == "8 devices of this model on site; 1 already runs 5.6 (demo)." and r.context["already"] in body
    assert form.fields["department"].widget.choices == [("", "Every department"), (str(fleet["west"].pk), "4 West only"),
                                                        (str(dept.pk), "ICU only")]
    assert form.fields["priority"].widget.choices == [("normal", "Normal"), ("high", "High")] and "Critical" not in body
    assert [label for _v, label in form.fields["assign_to"].widget.choices][0] == "Leave unassigned" and r.context["routes"] == []
    assert f'hx-post="/security/releases/{world["r56"].pk}/open-updates/?dept={dept.pk}"' in body
    assert "Drug libraries are not recorded here." in body
    client.force_login(users["technician"])
    r = client.get(batch_url(world["r56"]), **MODAL)
    assert "assign_to" not in r.context["form"].fields and r.context["routes"] == [updates.ASSIGN_NEEDS]
    assert updates.ASSIGN_NEEDS in unescape(html(r))


def test_a_technicians_batch_is_left_unassigned_and_names_each_skip(client, world, users, fleet, dept, today):
    client.force_login(users["technician"])
    r = client.post(batch_url(world["r56"], f"dept={dept.pk}"), batch_data(dept, today, releases=[world["r52"]]), **MODAL)
    assert r.status_code == 200 and r["HX-Retarget"] == "#drawer" and r.context["dm"] == world["model"]
    assert r.context["f"].dept == str(dept.pk) and r.context["narrowed"]  # the panel as it was opened
    fired = triggers(r)
    assert {"cyber-changed", "wo-changed", "toast", "modal-close"} <= set(fired)
    opened = WorkOrder.objects.filter(type=WoType.SOFTWARE_UPDATE).exclude(pk=fleet["open_wo"].pk).order_by("asset__tag")
    assert [wo.asset.tag for wo in opened] == ["CE-10002", "P-1", "P-2"] and not any(wo.assigned_to_id for wo in opened)
    assert toast_of(r) == ("3 software updates opened to install 5.6 (demo). 3 left unassigned: assigning needs Work orders Approve, so "
                           "a manager assigns them or technicians take them. Skipped, held as evidence for an incident: P-H. Skipped, with an "
                           "open software update already: P-O.")
    assert release_row(r, world["r56"])["update"]["words"] == "Updates to 5.6 (demo): 4 open"


def test_the_directors_batch_is_assigned(client, world, users, fleet, dept, today):
    client.force_login(users["director"])
    data = batch_data(dept, today, releases=[world["r52"]], department="", include_not_recorded="on", due_at_next_pm="on",
                      assign_to=str(world["dana"].pk), priority="high")
    r = client.post(batch_url(world["r56"]), data, **MODAL)
    assert r["HX-Retarget"] == "#drawer"
    opened = list(WorkOrder.objects.filter(type=WoType.SOFTWARE_UPDATE).exclude(pk=fleet["open_wo"].pk).order_by("asset__tag"))
    assert [wo.asset.tag for wo in opened] == ["CE-10002", "P-1", "P-2", "P-3", "P-5"]
    assert {wo.assigned_to for wo in opened} == {world["dana"]} and {wo.priority for wo in opened} == {"high"}
    assert [wo.due_on for wo in opened] == [today + 90 * DAY, *([today + 40 * DAY] * 4)]  # each device's next PM
    assert toast_of(r) == ("5 software updates opened to install 5.6 (demo). Skipped, held as evidence for an incident: P-H. "
                           "Skipped, with an open software update already: P-O.")


@pytest.mark.parametrize("changes, expected", [
    ({"from_releases": []}, {"from_releases": updates.FROM_NEEDED}),
    ({"from_releases": ["r56"]}, {"from_releases": updates.FROM_TARGET.format(version="5.6 (demo)")}),
    ({"from_releases": ["gone"]}, {"from_releases": updates.CHOOSE_FROM}),
    ({"from_releases": ["junk"]}, {"from_releases": updates.CHOOSE_FROM}),
    ({"department": "nowhere"}, {"department": updates.CHOOSE_DEPARTMENT}),
    ({"department": "west", "from_releases": []}, {"from_releases": updates.FROM_NEEDED}),
    ({"due_on": "past"}, {"due_on": updates.DUE_PAST}),
    ({"due_on": ""}, {"due_on": updates.DUE_NEEDED}),
    ({"priority": "critical"}, {"priority": updates.PRIORITY_WORDS}),
    ({"assign_to": "nobody"}, {"assign_to": updates.CHOOSE_TECHNICIAN}),
])
def test_each_batch_refusal_comes_back_on_its_field(client, world, users, fleet, dept, today, changes, expected):
    client.force_login(users["director"])
    values = {"r56": str(world["r56"].pk), "gone": str(world["gone"].pk), "nowhere": str(uuid.uuid4()), "west": str(fleet["west"].pk),
              "past": (today - DAY).isoformat(), "nobody": str(uuid.uuid4())}
    data = batch_data(dept, today, releases=[world["r52"]])
    for k, v in changes.items():
        data[k] = [values.get(x, x) for x in v] if isinstance(v, list) else values.get(v, v)
    r = client.post(batch_url(world["r56"]), data, **MODAL)
    body = html(r)
    assert "HX-Retarget" not in r and WorkOrder.objects.count() == 1
    assert errors(body) == expected
    if data["from_releases"] == [str(world["r52"].pk)]:
        assert re.search(rf'<input type="checkbox" name="from_releases" value="{world["r52"].pk}"[^>]* checked>', body)  # kept as chosen


def test_the_batch_with_nothing_to_open_says_why(client, world, users, dept, pump_model):
    client.force_login(users["director"])
    facts(world["pump"], world["r56"])
    assert blocker_of(html(client.get(batch_url(world["r56"]), **MODAL))) == views.ALL_ON_IT.format(version="5.6 (demo)")
    Asset.objects.filter(pk=world["pump"].pk).update(status=AssetStatus.RETIRED)
    assert blocker_of(html(client.get(batch_url(world["r56"]), **MODAL))) == views.NO_DEVICES
    r = client.post(batch_url(world["r56"]), batch_data(dept, timezone.localdate(), releases=[world["r52"]]), **MODAL)
    assert errors(html(r)) == {"from_releases": updates.NONE_SELECTED}


def test_the_batch_routes_by_who_installs_updates_here(client, world, users, fleet, dept, pump_model, today):
    Asset.objects.create(tag="P-R", device_model=pump_model, department=dept, ownership=Ownership.RENTAL, owner="RentCo Medical")
    SoftwareRelease.objects.filter(pk=world["profile"].pk).update(updates_by=UpdatesBy.MANUFACTURER)
    client.force_login(users["director"])
    r = client.get(batch_url(world["r56"]), **MODAL)
    assert "assign_to" not in r.context["form"].fields
    assert r.context["routes"] == [forms.BATCH_VENDOR_ROUTE, forms.BATCH_OWNER_ROUTE]
    SoftwareRelease.objects.filter(pk=world["profile"].pk).update(updates_by=UpdatesBy.IT)
    r = client.get(batch_url(world["r56"]), **MODAL)
    assert r.context["routes"] == [updates.INSTALLED_HERE.format(who="information technology"), forms.BATCH_OWNER_ROUTE]
    r = client.post(batch_url(world["r56"]), batch_data(dept, today, releases=[world["r52"]], department=str(fleet["west"].pk)), **MODAL)
    assert toast_of(r) == ("1 software update opened to install 5.6 (demo). 1 left unassigned: updates to this model are installed here "
                           "by information technology.")


# --- the never words ---------------------------------------------------------------------------------------------------------------

def test_no_rendered_state_modal_or_toast_says_a_never_word(client, world, users, states, fleet, dept, today):
    """Every state of the tab, every modal (forms, blockers, refusals), and the batch's panel, for the director and the IT role."""
    for who in ("director", "it", "technician"):
        client.force_login(users[who])
        bodies = [html(tab(client, world["pump"])), html(panel(client, world))]
        for url in (*door_urls(world, states).values(), confirm_url(states["open"]), confirm_url(states["done"]),
                    confirm_url(states["hand"]), remove_url(states["not_confirmed"]), remove_url(states["done"]),
                    open_url(fleet["devices"]["P-H"]), batch_url(world["r56"])):
            r = client.get(url, **MODAL)
            if r.status_code == 200:
                bodies.append(html(r))
        for url, data in ((open_url(fleet["devices"]["P-1"]), {"priority": "critical", "target": "patches"}),
                          (record_url(world["pump"]), {"software_outcome": "patches"}),
                          (confirm_url(states["not_confirmed"]), {"software_outcome": "already", "software_basis": VL}),
                          (batch_url(world["r56"]), {"priority": "urgent"})):
            r = client.post(url, data, **MODAL)
            if r.status_code == 200:
                bodies.append(html(r))
        assert all(not never(b) for b in bodies)
    for words in (forms.TARGET_HELP, forms.TARGET_RELEASE_HELP, forms.NO_OTHER_RELEASE, forms.TARGET_REFERENCE_HELP, forms.RELEASE_HELP,
                  forms.NO_RELEASE_ON_FILE, forms.REFERENCE_HELP, forms.BASIS_HELP, forms.INSTALLED_BY_HELP, forms.APPLIED_HELP,
                  forms.ASSIGN_HELP, forms.NO_QUALIFIED, forms.OWNER_ROUTE, forms.VENDOR_ROUTE, forms.BATCH_VENDOR_ROUTE,
                  forms.BATCH_OWNER_ROUTE, forms.FROM_HELP, forms.DEPARTMENT_HELP, forms.DUE_AT_NEXT_PM, views.OPENED, views.RECORDED,
                  views.CONFIRMED, views.FACTS_STAY, views.HAND_REMOVED, views.OUTCOME_REMOVED, views.BATCH_OPENED, views.NONE_OPENED,
                  views.LEFT_UNASSIGNED, views.NO_DEVICES, views.ALL_ON_IT, views.ALREADY_ON_IT, views.OPENED_TO_INSTALL,
                  views.OPENED_TO_PATCH, asset_cyber_updates.EMPTY, *(w for _k, w in views.SKIPS)):
        assert not never(words), words
    assert SEEN and not [t for t in SEEN if never(t)]
