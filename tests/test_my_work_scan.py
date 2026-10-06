"""
Scan and the digest (slice 24, part D). One rule (apps.workorders.my_work.scan_target) says what a scanned label opens: for someone with
My work and Work orders View, the one open work order of theirs on the device, else the device at its Work orders tab; anyone else the
device. Scan opened from My work follows it (its drawer over My work, no address pushed; a direct visit redirects), and so does the
portal's "Open in Cadence" link for a signed-in member, where an iPhone's Camera app lands. My work's Scan button opens the form
with from=my_work for whoever may use Scan tag. The daily digest's "due" is My work's and its link opens My work. The last test runs
them as the runtime role under PostgreSQL's row-level security.
"""
import json
import re
from datetime import date, timedelta

import pytest
from django.template.loader import render_to_string
from django.test import RequestFactory
from django.utils.html import strip_tags
from pg_helpers import as_app_role, needs_postgres

from apps.accounts.models import Level, Module, Role
from apps.credentials.models import Technician
from apps.equipment.models import Asset, Department
from apps.notifications import daily
from apps.notifications.models import NotificationPreference
from apps.tenants.context import tenant_context
from apps.workorders import my_work
from apps.workorders.models import Priority, WoStatus, WoType
from apps.workorders.services import create_work_order

TODAY = date(2026, 10, 6)
APP = "https://ce.example.org"
HX = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "modal-card"}
HAMILTON = "Hamilton Medical field service"


def wo(asset, *, tech=None, type=WoType.REPAIR, due=TODAY, status=WoStatus.OPEN, **extra):
    w = create_work_order(asset=asset, type=type, priority=Priority.NORMAL, problem="Alarm", assigned_to=tech,
                          opened_on=TODAY - timedelta(days=10), due_on=due, **extra)
    if status != WoStatus.OPEN:
        w.__class__.objects.filter(pk=w.pk).update(status=status)
        w.refresh_from_db()
    return w


@pytest.fixture
def floor(ctx, make_user, vent_model, pump_model):
    """Dana (a technician with her profile) has one open work order on ED-1 and two on ICU-1 (one waiting on parts); ED-2 has none
    of hers. Tom's and a completed one of hers on ED-1 are not her open work. Hamilton has vendor work on ICU-1."""
    icu, ed = Department.objects.create(name="ICU"), Department.objects.create(name="ED")
    icu1 = Asset.objects.create(tag="ICU-1", device_model=vent_model, department=icu, room="12")
    ed1 = Asset.objects.create(tag="ED-1", device_model=pump_model, department=ed, room="3")
    ed2 = Asset.objects.create(tag="ED-2", device_model=pump_model, department=ed, room="1")
    dana_user = make_user("technician")
    dana = Technician.objects.create(user=dana_user, name="Dana Whitfield")
    tom = Technician.objects.create(name="Tom Okafor")
    the_one = wo(ed1, tech=dana)
    wo(ed1, tech=tom)
    wo(ed1, tech=dana, status=WoStatus.COMPLETED)
    icu_a = wo(icu1, tech=dana)
    icu_b = wo(icu1, tech=dana, status=WoStatus.AWAITING_PARTS)
    vendor_wo = wo(icu1, vendor_service=True, vendor_name=HAMILTON)
    return {"icu1": icu1, "ed1": ed1, "ed2": ed2, "dana_user": dana_user, "dana": dana, "tom": tom, "the_one": the_one,
            "icu_wos": [icu_a, icu_b], "vendor_wo": vendor_wo}


def vendor_user(make_user, company="Hamilton Medical"):
    user = make_user("vendor")
    user.company = company
    user.save()
    return user


def bench_role(tenant, levels, slug="bench"):
    """A facility-wide role with only `levels`."""
    with tenant_context(tenant):
        role = Role.objects.create(name=slug.title(), slug=slug)
        role.set_levels(levels)
    return role


def triggers(r, header) -> dict:
    return json.loads(r[header]) if header in r else {}


def scan(client, code="", *, mine=True, **headers):
    params = {"code": code} if code else {}
    if mine:
        params["from"] = "my_work"
    return client.get("/scan/", params, **headers)


# --- the rule -----------------------------------------------------------------------------------------------------------------

def test_one_open_work_order_of_theirs_opens_it_else_the_devices_work_orders(floor):
    dana = floor["dana_user"]
    target = my_work.scan_target(dana, floor["ed1"])  # Tom's and her completed one are not her open work
    assert target.work_order == floor["the_one"] and target.url == f"/work-orders/{floor['the_one'].number}/"
    several = my_work.scan_target(dana, floor["icu1"])  # two open (waiting on parts is open): she chooses on the device
    assert several.work_order is None and several.tab == "wo" and several.url == "/equipment/ICU-1/?tab=wo"
    none = my_work.scan_target(dana, floor["ed2"])
    assert none.work_order is None and none.url == "/equipment/ED-2/?tab=wo"


def test_without_my_work_a_label_opens_the_device_as_before(floor, tenant, make_user):
    director = make_user("director")  # no technician profile: no My work
    assert my_work.scan_target(director, floor["ed1"]).url == "/equipment/ED-1/"
    requester = make_user("requester")
    requester.department = "ED"
    requester.save()
    Technician.objects.create(user=requester, name="Rita Requester")  # a unit's requester has no My work, whatever the account carries
    assert my_work.scan_target(requester, floor["ed1"]).url == "/equipment/ED-1/"
    Technician.objects.filter(pk=floor["dana"].pk).update(is_active=False)  # a profile no longer active
    assert my_work.scan_target(floor["dana_user"], floor["ed1"]).url == "/equipment/ED-1/"
    # a profile, but no Work orders View: the device
    bench = make_user("technician", username="bench@riverside.example")
    bench.role = bench_role(tenant, {Module.EQUIPMENT: Level.VIEW})
    bench.save()
    Technician.objects.create(user=bench, name="Bea Bench")
    assert my_work.has_my_work(bench) and my_work.scan_target(bench, floor["ed1"]).url == "/equipment/ED-1/"


def test_a_vendor_gets_their_companys_one_work_order_never_their_profiles(floor, make_user):
    """Scope first: a vendor whose account still carries a technician profile has in-house work on that profile, outside their share."""
    vendor = vendor_user(make_user)
    profile = Technician.objects.create(user=vendor, name="Val Vendor")
    wo(floor["icu1"], tech=profile)  # in-house work assigned to the profile, on a device the vendor can see
    target = my_work.scan_target(vendor, floor["icu1"])
    assert target.work_order == floor["vendor_wo"]
    wo(floor["icu1"], vendor_service=True, vendor_name="Hamilton Medical")  # a second of the company's
    assert my_work.scan_target(vendor, floor["icu1"]).url == "/equipment/ICU-1/?tab=wo"


# --- Scan from My work --------------------------------------------------------------------------------------------------------

def test_the_form_from_my_work_keeps_where_it_came_from(client, floor):
    client.force_login(floor["dana_user"])
    body = scan(client, **HX).content.decode()
    form = re.search(r"<form [^>]*>", body).group(0)
    assert 'class="scan-form from-my-work"' in form and 'hx-get="/scan/"' in form and 'hx-target="#modal-card"' in form
    assert '<input type="hidden" name="from" value="my_work">' in body
    assert 'class="iconbtn scan-close"' in body  # the modal's close button, 44px like the form's targets
    assert "On an iPhone, point the Camera app at the label" in body and "Your one open work order on it opens" in body
    plain = scan(client, mine=False, **HX).content.decode()  # Equipment's Scan tag is as it was
    assert 'name="from"' not in plain and "from-my-work" not in plain and "scan-close" not in plain and "iPhone" not in plain
    page = scan(client)  # opened directly: a page of its own, under My work
    text = page.content.decode()
    assert page.status_code == 200 and [t.name for t in page.templates][0] == "web/scan.html" and page.context["nav_active"] == "my_work"
    assert '<a href="/my-work/">Back to My work</a>' in text and 'name="from" value="my_work"' in text


@pytest.mark.parametrize("code", ["ed-1", "ED-1\r\n", "https://cadence.rrmc.org/r/riverside/?asset=ED-1", "/equipment/ED-1/"])
def test_scanning_from_my_work_opens_the_one_work_order_over_my_work(client, floor, code):
    client.force_login(floor["dana_user"])
    r = scan(client, code, **HX)
    assert r.status_code == 200 and [t.name for t in r.templates][0] == "web/_wo_drawer.html" and r.context["wo"] == floor["the_one"]
    assert r["HX-Retarget"] == "#drawer" and "HX-Push-Url" not in r  # going back stays on My work
    assert "modal-close" in triggers(r, "HX-Trigger-After-Settle")
    direct = scan(client, code)
    assert direct.status_code == 302 and direct["Location"] == f"/work-orders/{floor['the_one'].number}/"


def test_several_or_none_opens_the_device_at_its_work_orders(client, floor):
    client.force_login(floor["dana_user"])
    for tag in ("ICU-1", "ED-2"):
        r = scan(client, tag, **HX)
        assert [t.name for t in r.templates][0] == "web/_asset_drawer.html" and r.context["asset"].tag == tag and r.context["tab"] == "wo"
        assert r["HX-Retarget"] == "#drawer" and "HX-Push-Url" not in r and "modal-close" in triggers(r, "HX-Trigger-After-Settle")
        direct = scan(client, tag)
        assert direct["Location"] == f"/equipment/{tag}/?tab=wo" and client.get(direct["Location"]).context["tab"] == "wo"
    numbers = [w.number for w in floor["icu_wos"]]
    body = scan(client, "ICU-1", **HX).content.decode()
    assert all(n in body for n in numbers)  # the tab lists them to choose from


def test_equipments_scan_is_unchanged(client, floor):
    client.force_login(floor["dana_user"])
    r = scan(client, "ED-1", mine=False, **HX)
    assert r.context["asset"] == floor["ed1"] and r.context["tab"] == "overview" and r["HX-Push-Url"] == "/equipment/ED-1/"
    assert scan(client, "ED-1", mine=False)["Location"] == "/equipment/ED-1/"


def test_a_code_that_names_none_of_their_devices_keeps_the_form_from_my_work(client, floor, make_user):
    client.force_login(floor["dana_user"])
    r = scan(client, "NOPE-1", **HX)
    assert r.context["error"] == "No device with tag “NOPE-1”." and "HX-Retarget" not in r
    assert '<input type="hidden" name="from" value="my_work">' in r.content.decode()
    vendor = vendor_user(make_user)
    client.force_login(vendor)
    assert scan(client, "ED-1", **HX).context["error"] == "No device with tag “ED-1”."  # outside the vendor's share: no device
    r = scan(client, "icu-1", **HX)
    assert r.context["wo"] == floor["vendor_wo"] and "HX-Push-Url" not in r


def test_scan_from_my_work_keeps_scan_tags_door(client, floor, tenant, make_user):
    """Scan tag needs Equipment View, from My work too (a scoped user's share included)."""
    user = make_user("technician", username="dispatch@riverside.example")
    user.role = bench_role(tenant, {Module.WORKORDERS: Level.EDIT}, slug="dispatch")
    user.save()
    Technician.objects.create(user=user, name="Dee Spatch")
    client.force_login(user)
    assert scan(client, "ED-1", **HX).status_code == 403 and scan(client, **HX).status_code == 403
    client.logout()
    assert scan(client, "ED-1")["Location"].startswith("/login/")


# --- My work's Scan button ------------------------------------------------------------------------------------------------------

def button(tenant, user) -> str:
    request = RequestFactory().get("/my-work/")
    request.user, request.tenant = user, tenant
    return render_to_string("web/_my_work_scan.html", request=request)


def test_the_scan_button_opens_the_form_from_my_work(floor, tenant, make_user):
    html = button(tenant, floor["dana_user"])
    link = re.search(r"<a [^>]*>", html, re.S).group(0)
    assert 'class="btn primary mw-scan-btn"' in link and 'href="/scan/?from=my_work"' in link and 'hx-get="/scan/?from=my_work"' in link
    assert 'hx-target="#modal-card"' in link and "Scan a label" in html
    assert 'href="/scan/?from=my_work"' in button(tenant, vendor_user(make_user))  # a vendor's share has Scan tag too
    user = make_user("technician", username="dispatch@riverside.example")
    user.role = bench_role(tenant, {Module.WORKORDERS: Level.EDIT}, slug="dispatch")  # no Equipment View: Scan tag would refuse
    user.save()
    assert strip_tags(button(tenant, user)).strip() == "" and "/scan/" not in button(tenant, user)


# --- the portal's link ------------------------------------------------------------------------------------------------------------

def note(client, tag) -> str:
    body = client.get(f"/r/riverside/?asset={tag}").content.decode()
    return re.search(r'<div class="note in-cadence"[^>]*>.*?</div>\n', body).group(0)


def test_the_portal_link_opens_the_same(client, floor):
    """An iPhone's Camera app opens the label's portal link: a signed-in technician's link opens what Scan from My work would."""
    client.force_login(floor["dana_user"])
    number = floor["the_one"].number
    one = note(client, "ed-1")
    assert re.search(rf'<a href="/work-orders/{number}/"[^>]*>Open {number} on ED-1 in Cadence</a>', one)
    assert strip_tags(one).strip() == f"Signed in to Cadence as Technician User. Open {number} on ED-1 in Cadence"
    assert re.search(r'<a href="/equipment/ICU-1/\?tab=wo"[^>]*>Open ICU-1 in Cadence</a>', note(client, "ICU-1"))
    assert "min-height:44px" in one  # a phone's target


# --- the daily digest ------------------------------------------------------------------------------------------------------------

@pytest.fixture
def digest_on(floor, settings):
    settings.APP_BASE_URL = APP
    dana = floor["dana_user"]
    dana.email = "dana@riverside.example"
    dana.save()
    NotificationPreference.objects.create(user=dana, daily_digest=True)
    return dana


def test_the_digests_due_is_my_works_and_its_link_opens_my_work(tenant, floor, digest_on, mailoutbox):
    late_waiting = wo(floor["ed2"], tech=floor["dana"], due=TODAY - timedelta(days=3), status=WoStatus.AWAITING_PARTS)
    mailoutbox.clear()
    daily.send_due(TODAY)
    [mail] = mailoutbox
    due = mail.body.split("All your open work")[0]
    count, _late = my_work.due_count(digest_on, TODAY)
    assert f"DUE TODAY OR OVERDUE ({count})" in due and count == 2  # ED-1's and ICU-1's open one: the badge's count
    assert late_waiting.number not in mail.body and floor["icu_wos"][1].number not in mail.body  # waiting on parts is not due
    assert f"All your open work, on My work:\n{APP}/my-work/?facility=riverside\n" in mail.body
    assert "/work-orders/?assigned=" not in mail.body


def test_work_only_waiting_on_parts_sends_no_digest(tenant, floor, digest_on, mailoutbox):
    for w in [floor["the_one"], floor["icu_wos"][0]]:
        w.__class__.objects.filter(pk=w.pk).update(status=WoStatus.AWAITING_PARTS)
    mailoutbox.clear()
    summary = daily.send_due(TODAY)
    assert mailoutbox == [] and summary["tenants"][0]["quiet"] == 1


def test_the_digests_pms_keep_their_rule(tenant, floor, digest_on, mailoutbox):
    """The week's PMs are still their open PMs due after today and within PM_DAYS, a PM waiting on parts included."""
    soon = wo(floor["ed2"], tech=floor["dana"], type=WoType.PM, due=TODAY + timedelta(days=2), status=WoStatus.AWAITING_PARTS)
    wo(floor["ed2"], tech=floor["tom"], type=WoType.PM, due=TODAY + timedelta(days=2))
    mailoutbox.clear()
    daily.send_due(TODAY)
    body = mailoutbox[0].body
    assert "PMS DUE IN THE NEXT 7 DAYS (1)" in body and soon.number in body.split("PMS DUE")[1]


# --- under row-level security ---------------------------------------------------------------------------------------------------

@needs_postgres
def test_scan_the_portal_link_and_the_digest_under_the_policies(client, tenant, floor, digest_on, make_user, mailoutbox):
    vendor = vendor_user(make_user)
    number = floor["the_one"].number
    client.force_login(floor["dana_user"])
    as_app_role()
    r = scan(client, "https://cadence.rrmc.org/r/riverside/?asset=ed-1", **HX)
    assert r.status_code == 200 and r.context["wo"].number == number and "HX-Push-Url" not in r
    assert scan(client, "ICU-1")["Location"] == "/equipment/ICU-1/?tab=wo"
    assert f"Open {number} on ED-1 in Cadence" in note(client, "ED-1")
    client.force_login(vendor)
    assert scan(client, "ICU-1", **HX).context["wo"].number == floor["vendor_wo"].number
    assert f"Open {floor['vendor_wo'].number} on ICU-1 in Cadence" in note(client, "ICU-1")
    mailoutbox.clear()
    with tenant_context(None):  # the daily job starts with no tenant
        daily.send_due(TODAY)
    [mail] = mailoutbox
    assert "DUE TODAY OR OVERDUE (2)" in mail.body and f"{APP}/my-work/?facility=riverside" in mail.body
