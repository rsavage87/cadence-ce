"""Scan tag (slice 18, part B): open a device from its label on the Equipment screen, and the portal's note for someone signed in.

The code rule (apps/equipment/scan.py) reads a label's request link with any scheme and host, a link to the device's page, or a plain
tag as a handheld scanner types it, in any letter case; another facility's label says so and nothing more. The view (/scan/) looks
the device up among those the user may see (a vendor's or a unit's share), opens its drawer from the modal or redirects a direct
visit, and says "No device" in the same words for a tag outside the share as for one that does not exist. The portal shows "Open
<tag> in Cadence" only to someone who could open that device in Cadence, and the page is otherwise exactly as before. The last test
runs the scan and the portal note as the runtime role under PostgreSQL's row-level security."""
import json
import re

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils.html import escape, strip_tags
from pg_helpers import as_app_role, needs_postgres

from apps.accounts.models import Level, Module, Role, User, create_default_roles
from apps.equipment import scan
from apps.equipment.models import Asset, Department, DeviceModel
from apps.facility.services import asset_request_url
from apps.tenants.context import tenant_context
from apps.web.templatetags.web import ICONS
from apps.workorders.services import create_work_order

HX = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "modal-card"}
HAMILTON = "Hamilton Medical field service"  # how work orders name the vendor persona's company


def no_device(tag):
    return f"No device with tag “{tag}”."


@pytest.fixture
def world(ctx, dept, vent, pump, vent_model):
    """The ICU's vent (CE-10001, Hamilton's vendor work on it) and pump (CE-10002); Med-Surg's vent (MED-3, Hamilton's too)."""
    med = Department.objects.create(name="Med-Surg 4")
    med_vent = Asset.objects.create(tag="MED-3", device_model=vent_model, department=med)
    for asset in (vent, med_vent):
        create_work_order(asset=asset, type="repair", priority="normal", problem="Alarm", vendor_service=True, vendor_name=HAMILTON)
    return {"vent": vent, "pump": pump, "med_vent": med_vent}


@pytest.fixture
def theirs(tenant, other_tenant):
    """Another facility's device THEIRS-1, and the roles its users take."""
    create_default_roles(other_tenant)
    with tenant_context(other_tenant):
        model = DeviceModel.objects.create(manufacturer="X", model="Y", description="Their pump", category="C")
        return Asset.objects.create(tag="THEIRS-1", device_model=model, department=Department.objects.create(name="ICU"))


def sign_in(client, make_user, slug, tenant_=None, **fields):
    user = make_user(slug, tenant_=tenant_)
    for name, value in fields.items():
        setattr(user, name, value)
    user.save()
    client.force_login(user)
    return user


def no_equipment_user(tenant):
    """A facility-wide role with work orders but no Equipment access."""
    with tenant_context(tenant):
        role = Role.objects.create(name="Dispatch", slug="dispatch")
        role.set_levels({Module.WORKORDERS: Level.VIEW})
    return User.objects.create_user(username="dispatch@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role,
                                    first_name="Dee", last_name="Spatch")


def triggers(r, header) -> dict:
    return json.loads(r[header]) if header in r else {}


def scan_get(client, code, **headers):
    return client.get(reverse("web:scan"), {"code": code}, **headers)


# --- the code rule ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw, cleaned", [
    ("CE-10241", "CE-10241"), ("CE-10241\r\n", "CE-10241"), ("CE-10241\n", "CE-10241"), ("CE-10241\t", "CE-10241"),
    ("  CE-10241  ", "CE-10241"), ("\x02CE-10241\x03", "CE-10241"), ("﻿CE-10241​", "CE-10241"), ("\r\n\t ", ""), ("", ""),
    (None, ""), ("CE 10241", "CE 10241"),  # inside the code nothing is touched
])
def test_clean_strips_what_a_scanner_adds_around_the_code(raw, cleaned):
    assert scan.clean(raw) == cleaned


@pytest.mark.parametrize("code, tag", [
    ("CE-10241", "CE-10241"),
    ("ce-10241\r\n", "ce-10241"),
    # a label's request link: any scheme and host (PORTAL_BASE_URL differs between deployments), or none, as the label prints it
    ("https://cadence.rrmc.org/r/riverside/?asset=CE-10241", "CE-10241"),
    ("http://localhost:8000/r/riverside/?asset=CE-10241", "CE-10241"),
    ("https://old-host.example:8443/r/riverside/?asset=CE-10241&dept=ICU", "CE-10241"),
    ("cadence.rrmc.org/r/riverside/?asset=CE-10241", "CE-10241"),
    ("/r/riverside/?asset=CE-10241", "CE-10241"),
    ("https://cadence.rrmc.org/r/riverside?asset=CE-10241", "CE-10241"),
    ("HTTPS://CADENCE.RRMC.ORG/R/RIVERSIDE/?ASSET=CE-10241", "CE-10241"),  # a scanner that sends capitals
    ("https://cadence.rrmc.org/r/riverside/?asset=CE%2D10241%20", "CE-10241"),
    # the device's own page
    ("https://cadence.rrmc.org/equipment/CE-10241/", "CE-10241"),
    ("https://cadence.rrmc.org/equipment/CE-10241/?tab=pm", "CE-10241"),
    ("/equipment/ce-10241", "ce-10241"),
    ("/equipment/CE%2D10241/", "CE-10241"),
])
def test_every_form_of_code_names_its_tag(code, tag):
    assert scan.tag_in(code, "riverside") == tag


@pytest.mark.parametrize("code, message", [
    ("https://cadence.rrmc.org/r/other/?asset=CE-10241", scan.ANOTHER_FACILITY),
    ("https://cadence.rrmc.org/r/Other/", scan.ANOTHER_FACILITY),
    ("https://cadence.rrmc.org/r/riverside/", scan.REQUEST_FORM),
    ("https://cadence.rrmc.org/r/riverside/?dept=ICU", scan.REQUEST_FORM),
    ("https://cadence.rrmc.org/r/riverside/?asset=%20", scan.REQUEST_FORM),
    ("https://www.manufacturer.example/manuals/G5.pdf", scan.NOT_A_LABEL),
    ("https://cadence.rrmc.org/work-orders/WO-26-0001/", scan.NOT_A_LABEL),
    ("https://cadence.rrmc.org/equipment/", scan.NOT_A_LABEL),
    ("https://cadence.rrmc.org/r/riverside/done/SR-00001/", scan.NOT_A_LABEL),
    ("http://[::1/r/riverside/?asset=CE-1", scan.NOT_A_LABEL),  # not a URL at all
    ("SN/4471", scan.NOT_A_LABEL),  # tags have no slashes
    ("A" * 301, scan.TOO_LONG),
    ("https://cadence.rrmc.org/r/riverside/?asset=" + "A" * 300, scan.TOO_LONG),
])
def test_codes_that_name_no_tag_say_why(code, message):
    with pytest.raises(ValidationError) as e:
        scan.tag_in(code, "riverside")
    assert e.value.messages == [message]


def test_the_length_limit_is_on_the_code_without_what_a_scanner_adds():
    assert scan.tag_in("A" * 300 + "\r\n", "riverside") == "A" * 300
    assert scan.MAX_LENGTH == 300


def test_label_links_read_back_whatever_the_portal_address(ctx, vent, settings):
    """asset_request_url's links, as the labels' QR codes carry them and as the labels print them, name the device's tag."""
    for base in ("http://localhost:8000", "https://cadence.rrmc.org/", "https://ce.example.org:8443"):
        settings.PORTAL_BASE_URL = base
        url = asset_request_url(vent)
        assert scan.tag_in(url, "riverside") == vent.tag, url
        assert scan.tag_in(url.split("://", 1)[1], "riverside") == vent.tag, url
    assert scan.tag_in(reverse("web:asset", args=[vent.tag]), "riverside") == vent.tag


def test_find_matches_any_letter_case_among_the_devices_given(ctx, vent, pump):
    assert scan.find("ce-10001", slug="riverside", qs=Asset.objects.all()) == vent
    with pytest.raises(ValidationError) as e:
        scan.find("ce-10001", slug="riverside", qs=Asset.objects.exclude(pk=vent.pk))
    assert e.value.messages == [no_device("ce-10001")]
    with pytest.raises(ValidationError):
        scan.find("CE%", slug="riverside", qs=Asset.objects.all())  # no wildcards
    with pytest.raises(ValidationError):
        scan.find("CE_10001", slug="riverside", qs=Asset.objects.all())


# --- the modal and the page ------------------------------------------------------------------------------------------------------

def test_the_modal(client, make_user, world):
    sign_in(client, make_user, "technician")
    r = client.get("/scan/", **HX)
    body = r.content.decode()
    assert r.status_code == 200 and [t.name for t in r.templates][0] == "web/_scan.html"
    assert "<h2>Scan tag</h2>" in body and 'data-act="close-modal"' in body and "<html" not in body
    form = re.search(r"<form [^>]*>", body).group(0)
    assert 'action="/scan/"' in form and 'method="get"' in form and 'hx-get="/scan/"' in form and 'hx-target="#modal-card"' in form
    field = re.search(r"<input [^>]*>", body, re.S).group(0)
    for attr in ('name="code"', 'value=""', "autofocus", 'autocomplete="off"', 'autocapitalize="characters"', 'spellcheck="false"',
                 'enterkeyhint="go"'):
        assert attr in field, attr
    # the camera block: "Use camera" for scan.js to show where the browser can; the line saying it cannot shows by default
    assert "data-scan-camera" in body and re.search(r"<button [^>]*data-scan-start hidden>", body) and "<video data-scan-video" in body
    assert "This browser cannot read codes with the camera. Use a handheld scanner or type the tag; on a phone, the camera app opens a " \
           "label's QR code." in body
    assert "No device" not in body and "aria-invalid" not in body


def test_the_page_opened_directly(client, make_user, world):
    """Without HTMX /scan/ is a page of its own (a phone, a bookmark): the app shell, and the form as a plain GET."""
    sign_in(client, make_user, "technician")
    r = client.get("/scan/")
    body = r.content.decode()
    assert r.status_code == 200 and [t.name for t in r.templates][:2] == ["web/scan.html", "web/base.html"]
    assert r.context["nav_active"] == "equipment" and "<h1>Scan tag</h1>" in body and 'id="view"' in body
    form = re.search(r'<form class="scan-form"[^>]*>', body).group(0)
    assert 'action="/scan/"' in form and "hx-get" not in form
    assert 'name="code"' in body and "data-scan-camera" in body
    for blank in ("", "\r\n", "%09"):
        assert client.get(f"/scan/?code={blank}").context["error"] == ""  # nothing scanned yet: no error


def test_the_shell_loads_the_camera_script_and_the_icon(client, make_user, world):
    sign_in(client, make_user, "technician")
    body = client.get("/equipment/").content.decode()
    assert re.search(r'<script src="/static/web/cadence\.js" defer></script>\s*<script src="/static/web/scan\.js" defer></script>', body)
    assert ICONS["scan"] == ("1.8", '<path d="M4 8V5a1 1 0 0 1 1-1h3M16 4h3a1 1 0 0 1 1 1v3M20 16v3a1 1 0 0 1-1 1h-3M8 20H5a1 1 0 0 1-1-1v-3M4 12h16"/>')


@pytest.mark.parametrize("slug, fields", [("technician", {}), ("vendor", {"company": "Hamilton Medical"}), ("requester", {"department": "ICU"})])
def test_the_button_is_first_in_the_equipment_page_head(client, make_user, world, slug, fields):
    sign_in(client, make_user, slug, **fields)
    body = client.get("/equipment/").content.decode()
    actions = body.split('<div class="actions">', 1)[1]
    first = re.search(r"<(a|button) [^>]*>", actions).group(0)
    assert 'href="/scan/"' in first and 'hx-get="/scan/"' in first and 'hx-target="#modal-card"' in first
    assert "Scan tag</a>" in actions and "mobile app" not in body


# --- finding the device ---------------------------------------------------------------------------------------------------------

LABEL = "https://cadence.rrmc.org/r/riverside/?asset=CE-10001"


@pytest.mark.parametrize("code", [
    "CE-10001", "ce-10001", "Ce-10001", "CE-10001\r\n", "CE-10001\n", "CE-10001\t", " CE-10001 ",
    LABEL, "http://10.0.0.5:8000/r/riverside/?asset=ce-10001", "cadence.rrmc.org/r/riverside/?asset=CE-10001", "/r/riverside/?asset=CE-10001",
    "https://cadence.rrmc.org/equipment/CE-10001/", "/equipment/ce-10001/", LABEL + "\r\n",
])
def test_every_code_opens_the_device_from_the_modal(client, make_user, world, code):
    sign_in(client, make_user, "technician")
    r = scan_get(client, code, **HX)
    assert r.status_code == 200, r.content.decode()[:300]
    # the drawer swaps into #drawer with the device's own address (in its own letter case) pushed, and the modal closes after settle
    assert r["HX-Retarget"] == "#drawer" and r["HX-Push-Url"] == "/equipment/CE-10001/"
    assert "modal-close" in triggers(r, "HX-Trigger-After-Settle")
    assert r.context["asset"] == world["vent"] and r.context["tab"] == "overview"
    assert [t.name for t in r.templates][0] == "web/_asset_drawer.html" and 'hx-get="/equipment/CE-10001/?tab=wo"' in r.content.decode()


@pytest.mark.parametrize("code", ["ce-10001\r\n", LABEL, "/equipment/CE-10001/"])
def test_a_direct_visit_redirects_to_the_device(client, make_user, world, code):
    sign_in(client, make_user, "technician")
    r = scan_get(client, code)
    assert r.status_code == 302 and r["Location"] == "/equipment/CE-10001/"
    page = client.get(r["Location"])
    assert page.status_code == 200 and page.context["drawer_template"] == "web/_asset_drawer.html"


def error_of(r) -> str:
    return r.context["error"]


@pytest.mark.parametrize("code, shown, error", [
    ("CE-99999", "CE-99999", no_device("CE-99999")),
    ("ce-99999\r\n", "ce-99999", no_device("ce-99999")),
    ("https://cadence.rrmc.org/r/riverside/?asset=CE-99999", "https://cadence.rrmc.org/r/riverside/?asset=CE-99999", no_device("CE-99999")),
    ("/equipment/CE-99999/", "/equipment/CE-99999/", no_device("CE-99999")),
    ("THEIRS-1", "THEIRS-1", no_device("THEIRS-1")),  # another facility's device: none here
    ("CE 10001", "CE 10001", no_device("CE 10001")),
    ("https://cadence.rrmc.org/r/riverside/", "https://cadence.rrmc.org/r/riverside/", scan.REQUEST_FORM),
    ("https://example.org/manual.pdf", "https://example.org/manual.pdf", scan.NOT_A_LABEL),
    ("A" * 301, "A" * 301, scan.TOO_LONG),
])
def test_a_code_that_names_no_device_keeps_it_in_the_field(client, make_user, world, theirs, code, shown, error):
    sign_in(client, make_user, "technician")
    r = scan_get(client, code, **HX)
    body = r.content.decode()
    assert r.status_code == 200 and "HX-Retarget" not in r and "HX-Push-Url" not in r and "HX-Trigger-After-Settle" not in r
    assert [t.name for t in r.templates][0] == "web/_scan.html" and error_of(r) == error
    assert f'<p class="err" id="scan-error" role="alert">{escape(error)}</p>' in body
    field = re.search(r"<input [^>]*>", body, re.S).group(0)
    assert f'value="{escape(shown)}"' in field and 'aria-invalid="true"' in field
    page = scan_get(client, code)  # the same opened directly: the page again, with the reason
    assert page.status_code == 200 and [t.name for t in page.templates][0] == "web/scan.html" and error_of(page) == error


def test_another_facilitys_label_says_so_and_nothing_more(client, make_user, world, theirs):
    sign_in(client, make_user, "technician")
    for tag in ("CE-10001", "THEIRS-1", "NOWHERE-1"):  # a device here, theirs, none: the same words
        r = scan_get(client, f"https://cadence.rrmc.org/r/other/?asset={tag}", **HX)
        assert error_of(r) == "This label belongs to another facility." and "HX-Retarget" not in r
        assert "No device" not in r.content.decode()


# --- the user's share -----------------------------------------------------------------------------------------------------------

def normalized(r, code) -> str:
    return r.content.decode().replace(code, "CODE")


@pytest.mark.parametrize("slug, fields, mine, not_mine", [
    ("vendor", {"company": "hamilton medical"}, ["CE-10001", "MED-3"], ["CE-10002"]),
    ("requester", {"department": "icu"}, ["CE-10001", "CE-10002"], ["MED-3"]),
])
def test_a_scoped_user_finds_only_their_share(client, make_user, world, theirs, slug, fields, mine, not_mine):
    sign_in(client, make_user, slug, **fields)
    for tag in mine:
        r = scan_get(client, tag.lower(), **HX)
        assert r["HX-Retarget"] == "#drawer" and r["HX-Push-Url"] == f"/equipment/{tag}/" and r.context["asset"].tag == tag
        assert scan_get(client, f"https://h.example/r/riverside/?asset={tag}")["Location"] == f"/equipment/{tag}/"
    unknown = scan_get(client, "CE-77777", **HX)
    for tag in not_mine + ["THEIRS-1"]:
        for code in (tag, f"https://h.example/r/riverside/?asset={tag}"):
            r = scan_get(client, code, **HX)
            assert "HX-Retarget" not in r and error_of(r) == no_device(tag)
            if code == tag:  # word for word what a tag that does not exist gets
                assert normalized(r, tag) == normalized(unknown, "CE-77777")
            direct = scan_get(client, code)
            assert direct.status_code == 200 and error_of(direct) == no_device(tag)


def test_a_scoped_user_without_a_share_finds_nothing(client, make_user, world):
    sign_in(client, make_user, "vendor")  # no company
    assert error_of(scan_get(client, "CE-10001", **HX)) == no_device("CE-10001")


def test_equipment_view_is_required(client, tenant, world):
    assert client.get("/scan/")["Location"].startswith("/login/")
    assert client.get("/scan/?code=CE-10001")["Location"].startswith("/login/")
    client.force_login(no_equipment_user(tenant))
    for url in ("/scan/", "/scan/?code=CE-10001", "/scan/?code=CE-99999"):
        assert client.get(url, **HX).status_code == 403, url
        assert client.get(url).status_code == 403, url


# --- the portal's note ------------------------------------------------------------------------------------------------------------

CSRF = re.compile(r'name="csrfmiddlewaretoken" value="[^"]+"')


def portal(client, query) -> str:
    r = client.get(f"/r/riverside/{query}")
    assert r.status_code == 200
    return CSRF.sub('name="csrfmiddlewaretoken" value=""', r.content.decode())


def signed_out(client, query) -> str:
    client.logout()
    return portal(client, query)


@pytest.mark.parametrize("slug, fields, tag", [
    ("technician", {}, "CE-10001"), ("director", {}, "CE-10002"), ("analyst", {}, "MED-3"),
    ("vendor", {"company": "Hamilton Medical"}, "MED-3"), ("requester", {"department": "ICU"}, "CE-10002"),
])
def test_the_note_for_someone_who_could_open_the_device(client, make_user, world, slug, fields, tag):
    user = sign_in(client, make_user, slug, **fields)
    body = portal(client, f"?asset={tag.lower()}")
    note = re.search(r'<div class="note in-cadence"[^>]*>.*?</div>\n', body).group(0)
    assert strip_tags(note).strip() == f"Signed in to Cadence as {user.get_full_name()}. Open {tag} in Cadence"
    assert re.search(rf'<a href="/equipment/{tag}/"[^>]*>Open {tag} in Cadence</a>', note)
    assert body.index("in-cadence") < body.index("<form")  # at the top of the page
    # the rest of the page is the one everyone gets
    assert body.replace(note, "") == signed_out(client, f"?asset={tag.lower()}")


@pytest.mark.parametrize("case", ["another facility", "no equipment view", "vendor of another company", "requester of another unit",
                                  "vendor without a company", "requester of no unit", "unknown tag", "no tag"])
def test_the_page_is_as_before_for_everyone_else(client, make_user, tenant, other_tenant, world, theirs, case):
    query = "?asset=CE-10002"
    if case == "another facility":
        sign_in(client, make_user, "director", tenant_=other_tenant)
        query = "?asset=CE-10001"
    elif case == "no equipment view":
        client.force_login(no_equipment_user(tenant))
    elif case == "vendor of another company":
        sign_in(client, make_user, "vendor", company="Hamilton Medical")  # Hamilton has no work on the pump
    elif case == "requester of another unit":
        sign_in(client, make_user, "requester", department="Med-Surg 4")
    elif case == "vendor without a company":
        sign_in(client, make_user, "vendor")
    elif case == "requester of no unit":
        sign_in(client, make_user, "requester", department="Nowhere")
    elif case == "unknown tag":
        sign_in(client, make_user, "director")
        query = "?asset=CE-99999"
    else:  # no tag
        sign_in(client, make_user, "director")
        query = ""
    body = portal(client, query)
    assert "in Cadence" not in body and "Signed in" not in body and "/equipment/" not in body
    assert body == signed_out(client, query)


def test_another_facilitys_member_never_has_their_role_read_at_this_facility(client, make_user, other_tenant, world, theirs):
    """Under row-level security their role is hidden inside this facility (reading it fails the request); the note is decided
    before any role is read."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    sign_in(client, make_user, "director", tenant_=other_tenant)
    with CaptureQueriesContext(connection) as queries:
        assert client.get("/r/riverside/?asset=CE-10001").status_code == 200
    assert not [q["sql"] for q in queries.captured_queries if "accounts_role" in q["sql"]]


# --- under row-level security ---------------------------------------------------------------------------------------------------

@needs_postgres
def test_scan_and_the_portal_note_under_the_policies(client, make_user, tenant, other_tenant, world, theirs):
    tech = make_user("technician")
    vendor = make_user("vendor")
    vendor.company = "Hamilton Medical"
    vendor.save()
    theirs_user = make_user("director", tenant_=other_tenant)
    client.force_login(tech)
    as_app_role()
    r = scan_get(client, "https://cadence.rrmc.org/r/riverside/?asset=ce-10001", **HX)
    assert r.status_code == 200 and r["HX-Retarget"] == "#drawer" and r["HX-Push-Url"] == "/equipment/CE-10001/"
    assert scan_get(client, "CE-10001\r\n")["Location"] == "/equipment/CE-10001/"
    assert error_of(scan_get(client, "THEIRS-1", **HX)) == no_device("THEIRS-1")
    assert "Open CE-10001 in Cadence" in portal(client, "?asset=CE-10001")
    client.force_login(vendor)
    assert scan_get(client, "MED-3", **HX)["HX-Retarget"] == "#drawer"
    assert error_of(scan_get(client, "CE-10002", **HX)) == no_device("CE-10002")
    assert "Open MED-3 in Cadence" in portal(client, "?asset=MED-3") and "in Cadence" not in portal(client, "?asset=CE-10002")
    client.force_login(theirs_user)  # their role is hidden here: the note must not try to read it
    assert "in Cadence" not in portal(client, "?asset=CE-10001")
    assert "Open THEIRS-1 in Cadence" in client.get("/r/other/?asset=THEIRS-1").content.decode()
