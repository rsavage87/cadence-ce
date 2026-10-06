"""
My work's page and its cards (slice 24, part A; apps/web/views_my_work.py): the groups in a technician's order with a recall batch as
one row, what a card says (where, the problem's first line, how late, the chips, a PM's procedure), the moves each status and level
offers (each naming its own target, never opening the card too), Waiting on parts with its note, the Log time and Add part modals
answering a card without the drawer, the nav badge swapped out of band, hours and the credential line, a vendor's page, and the
parts C and D templates included only when they exist.
"""
import json
import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from pg_helpers import as_app_role, needs_postgres

from apps.credentials.models import Credential, Scope, Technician
from apps.equipment.models import Asset, Department, DeviceModel
from apps.pm.models import PmProcedure
from apps.recalls.models import Alert
from apps.tenants.context import tenant_context
from apps.web import views_my_work
from apps.workorders import costs, my_work
from apps.workorders.models import LaborLine, Priority, Source, WorkOrder, WorkOrderStatusHistory, WoStatus, WoType
from apps.workorders.services import STATUS_NOTE_MAX, change_status, create_work_order

TODAY = date(2026, 10, 6)  # a Tuesday
HX = {"HTTP_HX_REQUEST": "true"}
BODY = {**HX, "HTTP_HX_TARGET": "my-work-body"}


@pytest.fixture
def frozen(monkeypatch):
    monkeypatch.setattr(timezone, "localdate", lambda *a, **kw: TODAY)
    return TODAY


@pytest.fixture
def floor(ctx, vent_model, pump_model):
    icu, ed = Department.objects.create(name="ICU"), Department.objects.create(name="ED")
    return {"vent": Asset.objects.create(tag="ICU-1", device_model=vent_model, department=icu, room="12"),
            "pump": Asset.objects.create(tag="ED-1", device_model=pump_model, department=ed, room="3"),
            "pump2": Asset.objects.create(tag="ED-2", device_model=pump_model, department=ed, room="1")}


@pytest.fixture
def dana(client, make_user, floor, frozen):
    """Dana, a technician signed in, credentialed for infusion pumps (not ventilators)."""
    user = make_user("technician")
    t = Technician.objects.create(user=user, name="Dana Whitfield")
    Credential.objects.create(technician=t, scope=Scope.CATEGORY, value="Infusion pumps", expires_on=TODAY + timedelta(days=400))
    client.force_login(user)
    return {"user": user, "tech": t}


def wo(asset, *, type=WoType.REPAIR, priority=Priority.NORMAL, due=TODAY, tech=None, status=WoStatus.OPEN, problem="Alarm", **extra):
    w = create_work_order(asset=asset, type=type, priority=priority, problem=problem, assigned_to=tech, opened_on=TODAY - timedelta(days=10),
                          due_on=due, **extra)
    for to in {WoStatus.IN_PROGRESS: [WoStatus.IN_PROGRESS], WoStatus.AWAITING_PARTS: [WoStatus.IN_PROGRESS, WoStatus.AWAITING_PARTS]}.get(status, []):
        change_status(w, to)
    return w


def page(client, **params) -> str:
    return client.get("/my-work/", params, **BODY).content.decode()


def card(body: str, number: str) -> str:
    """The card of work order `number` in a My work body."""
    m = re.search(r'<article class="mw-card[^"]*"[^>]*hx-get="/work-orders/%s/".*?</article>' % re.escape(number), body, re.S)
    assert m, f"no card for {number}"
    return m.group(0)


def buttons(html: str) -> dict:
    """{label: the button's tag} for each move on a card."""
    return {label: tag for tag, label in re.findall(r"(<button [^>]*>)([^<]+)</button>", html)}


def triggers(r, header="HX-Trigger") -> dict:
    return json.loads(r[header]) if header in r else {}


def began_waiting(w, day: date):
    """Say `w` began waiting on parts at noon on `day`, in the facility's time zone (the history's timestamp is the real clock's)."""
    at = timezone.make_aware(datetime.combine(day, time(12)))
    WorkOrderStatusHistory.objects.filter(work_order=w, to_status=WoStatus.AWAITING_PARTS).update(created_at=at)


# --- the groups and the cards --------------------------------------------------------------------------------------------------

def test_a_card_says_what_a_technician_needs_at_the_device(client, dana, floor, pump_model):
    t = dana["tech"]
    late = wo(floor["vent"], priority=Priority.HIGH, due=TODAY - timedelta(days=3), tech=t, tag_out=True,
              problem="Alarm sounds on startup\nSecond line with more detail")
    portal = wo(floor["pump"], tech=t, source=Source.PORTAL, reported_location="ED bay 4", due=TODAY + timedelta(days=2))
    proc = PmProcedure.objects.create(code="PM-PUMP-12", name="Pump PM", estimated_hours=Decimal("0.75"), checklist=["Inspect"])
    DeviceModel.objects.filter(pk=pump_model.pk).update(pm_procedure=proc)
    pm = wo(floor["pump2"], type=WoType.PM, tech=t, estimated_hours=Decimal("0.75"))
    body = page(client)

    c = card(body, late.number)
    assert "3 d late" in c and "ICU · Room 12" in c and "ICU-1" in c and "ICU ventilator" in c
    assert "Alarm sounds on startup" in c and "Second line" not in c  # the problem's first line
    assert ">High<" in c and ">Open<" in c and ">Tagged out<" in c and ">Not credentialed<" in c  # Dana has no ventilator credential
    assert f'href="/work-orders/{late.number}/"' in c and 'hx-target="#drawer"' in c  # the card opens the drawer; the number is a link
    c = card(body, portal.number)
    assert "ED bay 4" in c and "Room 3" not in c and "Due Thu Oct 8" in c and "Not credentialed" not in c  # the unit's own words
    c = card(body, pm.number)
    assert "PM-PUMP-12 · 0.75 h" in c and "Due today" in c and "ED · Room 1" in c


def test_the_groups_come_in_a_technicians_order_with_a_recall_batch_as_one_row(client, dana, floor):
    t = dana["tech"]
    crit = wo(floor["pump2"], priority=Priority.CRITICAL, due=TODAY + timedelta(days=3), tech=t)
    low = wo(floor["pump"], priority=Priority.LOW, due=TODAY - timedelta(days=1), tech=t)
    alert = Alert.objects.create(source=Alert.Source.FDA, external_id="Z-1", classification="Class II", manufacturer="BD", product="Pump",
                                 title="Keypad membrane", published_on=TODAY)
    recall_a = wo(floor["pump2"], type=WoType.RECALL, tech=t, alert=alert)
    recall_b = wo(floor["pump"], type=WoType.RECALL, tech=t, alert=alert, due=TODAY - timedelta(days=2))
    pm_today = wo(floor["pump"], type=WoType.PM, tech=t)
    waiting = wo(floor["pump2"], tech=t, status=WoStatus.AWAITING_PARTS)
    soon = wo(floor["pump2"], type=WoType.PM, due=TODAY + timedelta(days=5), tech=t)
    wo(floor["pump2"], type=WoType.PM, due=TODAY + timedelta(days=30), tech=t)
    body = page(client)

    order = [body.index(s) for s in ("Repairs and requests", crit.number, "Recall: FDA Z-1 · 2 devices", low.number, "Today's PMs",
                                     pm_today.number, "Waiting on parts", waiting.number, "Coming up this week", soon.number, "1 more PM later")]
    assert order == sorted(order)  # the critical repair, then the batch (normal, 2 d late) ahead of the low one
    batch = body[body.index('<details class="mw-batch"'):body.index("</details>")]
    assert recall_a.number in batch and recall_b.number in batch and batch.index(recall_a.number) < batch.index(recall_b.number)  # ED room 1, 3
    assert "2 d late" in batch and "Keypad membrane" in batch and '<details class="mw-batch">' in body  # closed until opened
    assert f'href="/work-orders/?assigned={t.pk}&amp;type=pm"' in body  # the later PMs, in the Work orders list
    assert f'href="/work-orders/?assigned={t.pk}&amp;status=completed&amp;open=0"' in body  # their completed work, for forgotten lines
    # an open batch stays open when the list re-fetches itself (its hidden input goes with the request)
    assert '<details class="mw-batch" open>' in page(client, open=str(alert.pk))
    assert 'hx-include="#my-work-body details[open] input[name=open]"' in body
    assert 'hx-disinherit="hx-swap hx-target hx-include"' in body


def test_empty_groups_say_so_or_stay_out_of_the_way(client, dana):
    body = page(client)
    assert "No repairs or requests open." in body and "No PMs due today." in body
    assert "Waiting on parts" not in body and "Coming up" not in body and "You could take" not in body
    assert "<b>0</b> due today or late" in body


# --- the moves -----------------------------------------------------------------------------------------------------------------

def test_each_status_offers_its_moves_and_each_names_its_own_target(client, dana, floor):
    t = dana["tech"]
    opened = wo(floor["pump"], tech=t)
    started = wo(floor["pump"], tech=t, status=WoStatus.IN_PROGRESS)
    waiting = wo(floor["pump"], tech=t, status=WoStatus.AWAITING_PARTS)
    pm = wo(floor["pump2"], type=WoType.PM, tech=t)
    body = page(client)

    moves = buttons(card(body, opened.number))
    assert list(moves) == ["Start", "Log time"]
    assert f'hx-post="/work-orders/{opened.number}/status/"' in moves["Start"] and '"to": "in_progress"' in moves["Start"]
    assert 'hx-swap="none"' in moves["Start"] and 'hx-target="this"' in moves["Start"]  # the toast and wo-changed do the rest
    assert f'hx-get="/work-orders/{opened.number}/labor/"' in moves["Log time"] and '"from": "my_work"' in moves["Log time"]
    assert list(buttons(card(body, started.number))) == ["Complete", "Waiting on parts", "Log time"]
    assert f'hx-get="/work-orders/{started.number}/waiting/"' in buttons(card(body, started.number))["Waiting on parts"]
    moves = buttons(card(body, waiting.number))
    assert list(moves) == ["Resume", "Log time", "Add part"] and '"to": "in_progress"' in moves["Resume"]
    assert list(buttons(card(body, pm.number))) == ["Complete", "Log time"]  # a PM completes from open (part B), no Start first
    assert f'hx-get="/work-orders/{pm.number}/complete/"' in buttons(card(body, pm.number))["Complete"]
    for c in (opened, started, waiting, pm):
        for tag in buttons(card(body, c.number)).values():
            assert 'hx-trigger="click consume"' in tag and "hx-target=" in tag  # never opens the card's drawer as well

    r = client.post(f"/work-orders/{opened.number}/status/", {"to": WoStatus.IN_PROGRESS}, **HX)  # Start, as the card posts it
    assert r.status_code == 200 and "wo-changed" in triggers(r)
    assert list(buttons(card(page(client), opened.number))) == ["Complete", "Waiting on parts", "Log time"]


def test_view_only_or_unassigned_work_offers_no_moves(client, make_user, floor, frozen):
    analyst = make_user("analyst")  # work orders: View
    t = Technician.objects.create(user=analyst, name="Ann Analyst")
    mine = wo(floor["pump"], tech=t)
    client.force_login(analyst)
    c = card(page(client), mine.number)
    assert "<button" not in c and "mw-acts" not in c
    assert client.post(f"/work-orders/{mine.number}/waiting/", {"note": "x"}, **HX).status_code == 403
    assert client.get(f"/work-orders/{mine.number}/waiting/", **HX).status_code == 403
    assert views_my_work._actions(analyst, wo(floor["pump"])) == []


# --- Waiting on parts ----------------------------------------------------------------------------------------------------------

def test_waiting_on_parts_asks_for_a_note_and_the_card_shows_it(client, dana, floor):
    w = wo(floor["pump"], tech=dana["tech"], status=WoStatus.IN_PROGRESS)
    url = f"/work-orders/{w.number}/waiting/"
    modal = client.get(url, **HX).content.decode()
    assert "No patient information" in modal and 'name="from" value="my_work"' in modal and f'hx-post="{url}"' in modal
    assert client.get(url)["Location"] == f"/work-orders/{w.number}/"  # only ever a modal

    r = client.post(url, {"note": "Door latch kit, PO 4471, Friday", "from": "my_work"}, **HX)
    w.refresh_from_db()
    assert r.status_code == 200 and r.content == b"" and r["HX-Reswap"] == "none" and "HX-Retarget" not in r  # no drawer
    assert triggers(r)["toast"]["value"] == f"{w.number}: waiting on parts" and "wo-changed" in triggers(r)
    assert "modal-close" in triggers(r, "HX-Trigger-After-Settle")
    assert w.status == WoStatus.AWAITING_PARTS and w.status_history.last().note == "Door latch kit, PO 4471, Friday"

    costs.add_part(w, description="Door latch kit", quantity="1", unit_cost="40", po_number="PO-4471", by=None)
    costs.add_part(w, description="Screws", quantity="4", unit_cost="1", by=None)
    began_waiting(w, TODAY)
    c = card(page(client), w.number)
    assert "Waiting since today" in c and "Door latch kit, PO 4471, Friday" in c and "PO PO-4471" in c and ">Resume<" in c
    began_waiting(w, TODAY - timedelta(days=3))
    assert "Waiting 3 d" in card(page(client), w.number)

    again = client.post(url, {"note": "Twice"}, **HX).content.decode()  # already waiting: the modal says so, nothing is written
    assert "already waiting on parts" in again and w.status_history.filter(to_status=WoStatus.AWAITING_PARTS).count() == 1


def test_waiting_on_parts_refusals(client, dana, floor, make_user, other_tenant):
    w = wo(floor["pump"], tech=dana["tech"], status=WoStatus.IN_PROGRESS)
    r = client.post(f"/work-orders/{w.number}/waiting/", {"note": "x" * (STATUS_NOTE_MAX + 1)}, **HX)
    w.refresh_from_db()
    assert f"Keep the note to {STATUS_NOTE_MAX} characters." in r.content.decode() and w.status == WoStatus.IN_PROGRESS
    assert 'aria-invalid="true"' in r.content.decode()
    done = wo(floor["pump"], tech=dana["tech"], status=WoStatus.IN_PROGRESS)
    WorkOrder.objects.filter(pk=done.pk).update(status=WoStatus.COMPLETED)
    assert "only open work can wait on parts" in client.get(f"/work-orders/{done.number}/waiting/", **HX).content.decode()
    with tenant_context(other_tenant):
        d = Department.objects.create(name="Their ICU")
        dm = DeviceModel.objects.create(manufacturer="X", model="Y", description="Their pump", category="C")
        theirs = create_work_order(asset=Asset.objects.create(tag="THEIRS-1", device_model=dm, department=d), type="repair",
                                   priority="normal", problem="Theirs", opened_on=date(2025, 6, 1))
    assert client.post(f"/work-orders/{theirs.number}/waiting/", {"note": "x"}, **HX).status_code == 404
    requester = make_user("requester")
    client.force_login(requester)
    assert client.get(f"/work-orders/{w.number}/waiting/", **HX).status_code == 403


def test_a_vendor_works_their_companys_cards(client, make_user, floor, frozen):
    vendor = make_user("vendor")
    vendor.company = "Hamilton Medical"
    vendor.save()
    Technician.objects.create(user=vendor, name="Val Vendor")  # a profile the vendor role keeps: not what My work lists
    house = wo(floor["pump"], tech=Technician.objects.create(name="Tom Okafor"))
    theirs = wo(floor["vent"], vendor_service=True, vendor_name="Hamilton Medical", status=WoStatus.IN_PROGRESS,
                problem=f"Follow-up to {house.number}")
    wo(floor["vent"], type=WoType.PM, vendor_service=True, vendor_name="Hamilton Medical", due=TODAY + timedelta(days=30))
    client.force_login(vendor)
    body = page(client)
    c = card(body, theirs.number)
    assert house.number not in body and "another work order" in c  # another work order's number is masked, as the drawer does
    assert "Vendor" not in c and "Not credentialed" not in c and "logged today" not in body  # their page: no chips about themselves
    assert list(buttons(c)) == ["Complete", "Waiting on parts", "Log time"]
    assert 'href="/work-orders/?type=pm">1 more PM later' in body and 'href="/work-orders/?status=completed&amp;open=0"' in body  # their share
    r = client.post(f"/work-orders/{theirs.number}/waiting/", {"note": "Flow sensor on order"}, **HX)
    assert r.status_code == 200 and WorkOrder.objects.get(pk=theirs.pk).status == WoStatus.AWAITING_PARTS
    assert client.get(f"/work-orders/{house.number}/waiting/", **HX).status_code == 404  # outside their share


# --- the modals a card opens ---------------------------------------------------------------------------------------------------

def test_log_time_and_add_part_from_a_card_answer_without_the_drawer(client, dana, floor):
    w = wo(floor["pump"], tech=dana["tech"], status=WoStatus.IN_PROGRESS)
    labor, parts = f"/work-orders/{w.number}/labor/", f"/work-orders/{w.number}/parts/"
    assert 'name="from" value="my_work"' in client.get(labor, {"from": "my_work"}, **HX).content.decode()
    assert 'name="from"' not in client.get(labor, **HX).content.decode()  # from the drawer: unchanged
    assert 'name="from" value="my_work"' in client.get(parts, {"from": "my_work"}, **HX).content.decode()

    r = client.post(labor, {"from": "my_work", "worked_on": TODAY.isoformat(), "hours": "1.5"}, **HX)
    assert r.status_code == 200 and r.content == b"" and r["HX-Reswap"] == "none" and "HX-Retarget" not in r
    assert triggers(r)["toast"]["value"] == f"1.5 h logged on {w.number}" and "wo-changed" in triggers(r)
    assert "modal-close" in triggers(r, "HX-Trigger-After-Settle")
    assert LaborLine.objects.get(work_order=w).technician == dana["tech"]
    bad = client.post(labor, {"from": "my_work", "worked_on": TODAY.isoformat(), "hours": ""}, **HX).content.decode()
    assert 'name="from" value="my_work"' in bad and "wl-form" in bad  # the modal again, still from the card
    r = client.post(parts, {"from": "my_work", "description": "Fuse", "quantity": "1", "unit_cost": "2", "po_number": "PO-9"}, **HX)
    assert r.content == b"" and triggers(r)["toast"]["value"] == "Part added" and w.part_lines.count() == 1
    assert client.post(labor, {"worked_on": TODAY.isoformat(), "hours": "1"}, **HX)["HX-Retarget"] == "#drawer"  # the drawer's own


# --- the badge, hours, credentials ---------------------------------------------------------------------------------------------

def test_the_list_swaps_the_nav_badge_out_of_band(client, dana, floor, make_user):
    late = wo(floor["pump"], due=TODAY - timedelta(days=1), tech=dana["tech"])
    part = page(client)
    assert part.lstrip().startswith('<div id="my-work-body"')
    oob = re.search(r'<a hx-swap-oob="outerHTML:#nav a\[href=\'/my-work/\'\]"[^>]*>.*?</a>', part, re.S).group(0)
    assert 'class="active"' in oob and '<span class="cnt hot">1</span>' in oob and "My work" in oob
    full = client.get("/my-work/").content.decode()
    assert "hx-swap-oob" not in full and '<span class="cnt hot">1</span>' in full  # base.html draws the same row
    change_status(late, WoStatus.IN_PROGRESS)
    change_status(late, WoStatus.AWAITING_PARTS)  # waiting on parts is not due
    assert '<span class="cnt' not in re.search(r"<a hx-swap-oob.*?</a>", page(client), re.S).group(0)
    client.force_login(make_user("director"))  # no technician profile: no My work row to update
    assert "hx-swap-oob" not in page(client)


def test_hours_and_the_credential_line(client, dana, floor, make_user):
    t = dana["tech"]
    w = wo(floor["pump"], tech=t)
    for day, hours in ((TODAY, "1.5"), (TODAY - timedelta(days=1), "2"), (TODAY - timedelta(days=9), "4")):
        LaborLine.objects.create(work_order=w, technician=t, worked_on=day, hours=Decimal(hours), rate=Decimal("85"))
    body = page(client)
    assert "<b>1.5 h</b> logged today" in body and "<b>3.5 h</b> this week" in body and "mw-cred" not in body
    expiring = Credential.objects.create(technician=t, scope=Scope.MODEL, value="Alaris 8015 PCU", expires_on=TODAY + timedelta(days=30))
    Credential.objects.create(technician=t, scope=Scope.MODEL, value="Hamilton-G5", expires_on=TODAY - timedelta(days=1))  # expired
    Credential.objects.create(technician=t, scope=Scope.MODEL, value="Hamilton-C6", status=Credential.Status.IN_TRAINING,
                              expires_on=TODAY + timedelta(days=5))
    body = page(client)
    assert "Your credential for Alaris 8015 PCU expires in 30 d, on Nov 5." in body
    assert "Ask a CE manager about renewing it." in body and "/users/credentials/" not in body  # a technician cannot open that tab
    Credential.objects.create(technician=t, scope=Scope.MANUFACTURER, value="BD", expires_on=TODAY + timedelta(days=50))
    assert "2 of your credentials expire within 60 days; the first, Alaris 8015 PCU, in 30 d, on Nov 5." in page(client)

    manager = make_user("manager")  # Users and access: View
    expiring.technician = Technician.objects.create(user=manager, name="Mo Manager")
    expiring.save()
    client.force_login(manager)
    assert f'href="/users/credentials/?technician={expiring.technician.pk}">See your credentials</a>' in page(client)


# --- parts C and D's templates -------------------------------------------------------------------------------------------------

def test_the_take_and_scan_templates_are_included_only_when_they_exist(client, dana, floor, settings, tmp_path, monkeypatch):
    assert views_my_work.optional_template("web/_my_work_body.html") == "web/_my_work_body.html"
    assert views_my_work.optional_template("web/_my_work_nothing.html") is None
    unassigned = wo(floor["pump2"])
    monkeypatch.setattr(my_work, "takeable", lambda user, today: [unassigned])
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "_my_work_take.html").write_text('<button class="btn" data-test="take">Take {{ wo.number }}</button>')
    (tmp_path / "web" / "_my_work_scan.html").write_text('<div data-test="scan">Scan a tag</div>')
    settings.TEMPLATES = [{**settings.TEMPLATES[0], "DIRS": [tmp_path, *settings.TEMPLATES[0]["DIRS"]]}]
    full = client.get("/my-work/").content.decode()
    c = card(full, unassigned.number)
    assert "You could take" in full and f'data-test="take">Take {unassigned.number}</button>' in c and "Start" not in c
    assert full.index('data-test="scan"') < full.index('id="my-work-body"')  # at the top of the page


# --- under PostgreSQL's row-level security -------------------------------------------------------------------------------------

@needs_postgres
def test_the_cards_and_waiting_on_parts_under_the_policies(client, make_user, floor, frozen):
    """The page reads the cards, the status history behind "waiting N d", and the part lines as the runtime role, inside this facility."""
    user = make_user("technician")
    t = Technician.objects.create(user=user, name="Dana Whitfield")
    w = wo(floor["pump"], tech=t, status=WoStatus.IN_PROGRESS)
    client.force_login(user)
    as_app_role()
    assert w.number in client.get("/my-work/").content.decode()
    r = client.post(f"/work-orders/{w.number}/waiting/", {"note": "Latch kit on order", "from": "my_work"}, **HX)
    assert r.status_code == 200 and r.content == b""
    body = client.get("/my-work/", **BODY).content.decode()
    assert "Latch kit on order" in card(body, w.number) and ">Resume<" in card(body, w.number) and "hx-swap-oob" in body
