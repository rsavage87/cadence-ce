"""Asset labels and the work-order print (slice 11): permissions, tenant isolation, the label's request QR code, the list's filters
and the cap, both label layouts, and what a printed work order carries."""
import re
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.accounts.models import Level, Role, User
from apps.contracts import services as ct
from apps.contracts.models import ContractType
from apps.equipment.models import Asset, Department, DeviceModel, RiskClass
from apps.facility.services import asset_request_url, update_settings
from apps.pm.models import PmProcedure
from apps.tenants.context import tenant_context
from apps.web import views_print
from apps.web.qr import qr_svg
from apps.workorders.models import LaborLine, PartLine, Source, WoStatus
from apps.workorders.services import add_note, assign, change_status, create_work_order

HX = {"HTTP_HX_REQUEST": "true"}


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug):
        user = make_user(role_slug)
        client.force_login(user)
        return user

    return _as


def custom_user(tenant, levels, username):
    role = Role.objects.create(name="Custom", slug=username.split("@")[0])
    role.set_levels(levels)
    return User.objects.create_user(username=username, password="Test-Pass-2026-x", tenant=tenant, role=role)


def label_url(asset) -> str:
    return f"/print/labels/?tag={asset.tag}"


def wo_url(wo) -> str:
    return f"/print/work-orders/{wo.number}/"


def tags_in(body: str) -> list[str]:
    """The tags printed on the labels, in order."""
    return re.findall(r'<div class="lb-tag mono[^"]*">([^<]+)</div>', body)


@pytest.fixture
def wo(ctx, vent):
    return create_work_order(asset=vent, type="repair", priority="high", problem="Low tidal volume alarm", requester="RN Patel")


@pytest.fixture
def fleet(ctx, dept, pump_model):
    """Fifteen pumps in two departments, CE-20000 to CE-20014; the even ones in the ED."""
    ed = Department.objects.create(name="Emergency")
    return [Asset.objects.create(tag=f"CE-200{i:02d}", serial=f"SN{i:04d}", device_model=pump_model, department=ed if i % 2 == 0 else dept, room=str(i))
            for i in range(15)]


# --- access ---------------------------------------------------------------------------------------

def test_both_pages_require_sign_in(client, wo, vent):
    for url in (label_url(vent), "/print/labels/", wo_url(wo)):
        r = client.get(url)
        assert r.status_code == 302 and r["Location"].startswith("/login/"), url


def test_labels_need_equipment_view_and_the_print_needs_work_orders_view(client, tenant, wo, vent):
    client.force_login(custom_user(tenant, {"workorders": Level.VIEW}, "wo-only@riverside.example"))
    assert client.get(label_url(vent)).status_code == 403
    assert client.get("/print/labels/").status_code == 403
    assert client.get(wo_url(wo)).status_code == 200
    client.force_login(custom_user(tenant, {"equipment": Level.VIEW}, "eq-only@riverside.example"))
    assert client.get(label_url(vent)).status_code == 200
    assert client.get("/print/labels/").status_code == 200
    assert client.get(wo_url(wo)).status_code == 403


def test_every_default_role_can_print(client, signed_in, wo, vent):
    # Every default role holds View on Equipment and on Work orders (the requester's Request level includes it).
    for slug in ("director", "manager", "technician", "requester", "analyst", "vendor"):
        signed_in(slug)
        assert client.get(label_url(vent)).status_code == 200, slug
        assert client.get(wo_url(wo)).status_code == 200, slug


def test_another_tenants_tag_and_work_order_are_404(client, signed_in, other_tenant, vent, wo):
    with tenant_context(other_tenant):
        theirs_dm = DeviceModel.objects.create(manufacturer="Acme", model="X1", description="Thing", category="Things")
        theirs = Asset.objects.create(tag="CE-90001", device_model=theirs_dm, department=Department.objects.create(name="Theirs"))
        # The same tag as ours: the label must be ours, never theirs.
        Asset.objects.create(tag=vent.tag, serial="THEIR-SERIAL", device_model=theirs_dm, department=Department.objects.create(name="Their ICU"))
        # Numbers run per tenant: their first work order has our first one's number, their second has no match here.
        create_work_order(asset=theirs, type="repair", priority="normal", problem="Their first problem")
        their_wo = create_work_order(asset=theirs, type="repair", priority="normal", problem="Their second problem")
    signed_in("director")
    assert their_wo.number != wo.number
    assert client.get(label_url(theirs)).status_code == 404
    assert client.get(wo_url(their_wo)).status_code == 404
    body = client.get(wo_url(wo)).content.decode()
    assert "Low tidal volume alarm" in body and "Their first problem" not in body
    body = client.get(label_url(vent)).content.decode()
    assert "THEIR-SERIAL" not in body and "Their ICU" not in body and "Hamilton-G5" in body
    assert "CE-90001" not in client.get("/print/labels/").content.decode()


def test_unknown_tag_and_number_are_404(client, signed_in, vent):
    signed_in("director")
    assert client.get("/print/labels/?tag=CE-NOPE").status_code == 404
    assert client.get("/print/work-orders/WO-99-9999/").status_code == 404


# --- labels ---------------------------------------------------------------------------------------

def test_qr_svg_is_inline_svg_with_an_escaped_name():
    svg = str(qr_svg("http://localhost:8000/r/riverside/?asset=CE-1", 'Report "<b>"'))
    assert svg.startswith('<svg role="img" aria-label="Report &quot;&lt;b&gt;&quot;" viewBox="0 0 ')
    assert 'class="qr"' in svg and 'stroke="#000"' in svg and 'fill="#fff"' in svg
    assert "<b>" not in svg and "asset=CE-1" not in svg  # the text is in the modules, not the markup
    assert "width=" not in svg and "height=" not in svg  # CSS sizes it


def test_label_carries_the_request_link_as_qr_and_text(client, signed_in, monkeypatch, vent):
    vent.serial, vent.room = "HM-55102", "4B"
    vent.save()
    calls = []

    def spy(text, label):
        calls.append(text)
        return qr_svg(text, label)

    monkeypatch.setattr(views_print, "qr_svg", spy)
    signed_in("technician")
    r = client.get(label_url(vent))
    assert r.status_code == 200
    url = asset_request_url(vent)
    assert calls == [url] and url == "http://localhost:8000/r/riverside/?asset=CE-10001"
    body = r.content.decode()
    assert '<div class="lb-url mono">localhost:8000/r/riverside/?asset=CE-10001</div>' in body
    assert body.count('<svg role="img" aria-label="QR code: report a problem with CE-10001"') == 1
    for text in ("Riverside Regional", "Clinical Engineering", ">CE-10001<", "Hamilton Medical Hamilton-G5", "SN HM-55102", "ICU, room 4B",
                 "Scan to report a problem"):
        assert text in body, text
    assert "Urgent: call" not in body  # no hotline set


def test_label_shows_the_hotline_when_set(client, signed_in, ctx, vent):
    update_settings(portal_hotline="ext. 4400")
    signed_in("technician")
    assert "Urgent: call ext. 4400" in client.get(label_url(vent)).content.decode()


def test_a_single_tag_defaults_to_the_label_printer_and_a_list_to_letter_sheets(client, signed_in, vent, pump):
    signed_in("technician")
    r = client.get(label_url(vent))
    body = r.content.decode()
    assert r.context["layout"] == "label" and "@page{size:2.25in 1.25in;margin:0}" in body and 'class="sheet lb-page"' in body
    assert "Asset label for CE-10001" in body
    r = client.get("/print/labels/")
    body = r.content.decode()
    assert r.context["layout"] == "sheet" and "@page{size:letter;margin:.5in .15625in}" in body and 'class="sheet lb-sheet"' in body
    assert "2 asset labels" in body
    assert client.get(label_url(vent) + "&layout=sheet").context["layout"] == "sheet"
    assert client.get("/print/labels/?layout=label").context["layout"] == "label"
    assert client.get("/print/labels/?layout=poster").context["layout"] == "sheet"


def test_the_qr_code_stays_large_enough_to_scan(client, signed_in, vent):
    signed_in("technician")
    body = client.get(label_url(vent)).content.decode()
    for layout, smallest in (("label", 0.8), ("sheet", 1.0)):
        m = re.search(rf"\.layout-{layout} \.lb-qr\{{width:([\d.]+)in;height:([\d.]+)in\}}", body)
        assert m and float(m[1]) >= smallest and float(m[2]) >= smallest, layout


def test_the_layout_switch_keeps_the_other_parameters(client, signed_in, vent, pump):
    signed_in("technician")
    body = client.get("/print/labels/?category=Ventilators&sort=tag").content.decode()
    assert 'href="?category=Ventilators&amp;sort=tag&amp;layout=label">Label printer</a>' in body
    assert 'href="?category=Ventilators&amp;sort=tag&amp;layout=sheet" aria-current="true">Letter sheets</a>' in body
    body = client.get(label_url(vent) + "&layout=sheet").content.decode()
    assert 'href="?tag=CE-10001&amp;layout=label">Label printer</a>' in body


def test_the_list_filters_pick_the_devices_in_the_lists_order(client, signed_in, vent, pump, fleet):
    signed_in("technician")
    # The Equipment screen's own parameters; page, mode, and the Overview's y and m are not filters here and are ignored.
    assert tags_in(client.get("/print/labels/?category=Ventilators&page=3&mode=board&y=2026&m=9").content.decode()) == ["CE-10001"]
    assert tags_in(client.get("/print/labels/?dept=Emergency&sort=tag&dir=desc").content.decode()) == [f"CE-200{i:02d}" for i in range(14, -1, -2)]
    assert tags_in(client.get("/print/labels/?q=CE-1000").content.decode()) == ["CE-10001", "CE-10002"]
    assert len(tags_in(client.get("/print/labels/").content.decode())) == 17
    body = client.get("/print/labels/?q=nothing-matches").content.decode()
    assert "No devices match this list" in body and tags_in(body) == [] and "Label printer" not in body


def test_letter_sheets_hold_ten_labels_and_the_label_printer_one(client, signed_in, fleet):
    signed_in("technician")
    body = client.get("/print/labels/").content.decode()
    assert body.count('class="sheet lb-sheet"') == 2 and body.count('<article class="lb">') == 15
    body = client.get("/print/labels/?layout=label").content.decode()
    assert body.count('class="sheet lb-page"') == 15 and body.count('<article class="lb">') == 15


def test_above_the_cap_the_page_asks_to_narrow_the_list(client, signed_in, monkeypatch, vent, pump, fleet):
    signed_in("technician")
    monkeypatch.setattr(views_print, "LABELS_MAX", 17)
    assert len(tags_in(client.get("/print/labels/").content.decode())) == 17  # at the cap, everything prints
    monkeypatch.setattr(views_print, "LABELS_MAX", 16)
    r = client.get("/print/labels/?dept=ICU")  # 2 + 7 devices: under the cap
    assert len(tags_in(r.content.decode())) == 9
    r = client.get("/print/labels/?status=in_service")
    body = r.content.decode()
    assert r.status_code == 200 and "17 devices match this list" in body and "at most 16 devices at a time" in body
    assert tags_in(body) == [] and "<svg role=" not in body and "Label printer" not in body
    assert 'href="/equipment/?status=in_service">Back to Equipment</a>' in body


def test_a_sheet_of_fifteen_labels_runs_as_many_queries_as_one(client, signed_in, fleet, django_assert_max_num_queries):
    signed_in("technician")
    client.get("/print/labels/?q=CE-20000")  # warm the session and caches
    with CaptureQueriesContext(connection) as one:
        assert len(tags_in(client.get("/print/labels/?q=CE-20000").content.decode())) == 1
    with CaptureQueriesContext(connection) as fifteen:
        assert len(tags_in(client.get("/print/labels/?q=CE-200").content.decode())) == 15
    assert len(fifteen) == len(one)
    with django_assert_max_num_queries(10):
        client.get("/print/labels/?q=CE-200")


def test_the_device_drawer_links_to_its_label(client, signed_in, vent):
    signed_in("requester")
    body = client.get(f"/equipment/{vent.tag}/", **HX).content.decode()
    assert 'href="/print/labels/?tag=CE-10001" target="_blank"' in body


def test_the_equipment_screen_links_to_labels_for_its_list(client, signed_in, vent):
    signed_in("technician")
    body = client.get("/equipment/?dept=ICU").content.decode()
    assert 'href="/print/labels/?dept=ICU" data-act="with-filters" data-base="/print/labels/"' in body
    assert client.get("/print/labels/?dept=ICU").status_code == 200


# --- the work-order print -------------------------------------------------------------------------

def test_the_header_device_and_request(client, signed_in, ctx, vent, techs):
    c = ct.create_contract(reference="SC-2026-118", vendor="Hamilton Medical", type=ContractType.OEM, start_on=date.today() - timedelta(days=100),
                           end_on=date.today() + timedelta(days=200), annual_cost=Decimal("4000"))
    ct.add_asset(c, vent)
    vent.refresh_from_db()
    vent.serial, vent.room, vent.warranty_end = "HM-55102", "4B", date(2025, 3, 31)
    vent.save()
    w = create_work_order(asset=vent, type="repair", priority="critical", problem="Fails self test", requester="RN Patel", source=Source.PORTAL,
                          callback="x4410", reported_location="ICU bay 4", tag_out=True, opened_on=date.today() - timedelta(days=4),
                          due_on=date.today() - timedelta(days=2))
    assign(w, technician=techs["dana"])
    change_status(w, WoStatus.IN_PROGRESS, as_of=date.today() - timedelta(days=3))
    signed_in("technician")
    body = client.get(wo_url(w)).content.decode()
    for text in (w.number, "Corrective repair", "Critical", "In progress", "Riverside Regional", (date.today() - timedelta(days=4)).strftime("%b %-d, %Y"),
                 "Past due", "2 d late",
                 "CE-10001", "ICU ventilator", "Hamilton Medical Hamilton-G5", "HM-55102", "Ventilators", "Life support", "<dd>ICU</dd>", "<dd>4B</dd>",
                 "OEM contract · SC-2026-118, Hamilton Medical through", "Expired Mar 31, 2025",
                 "Fails self test", "RN Patel", "x4410", "ICU bay 4", "Service request portal", "Yes, removed from use by the unit",
                 "Dana Whitfield"):
        assert text in body, text
    assert "Started</dt><dd>" + (date.today() - timedelta(days=3)).strftime("%b %-d, %Y") in body


def test_past_due_only_while_open_and_late(client, signed_in, ctx, vent):
    signed_in("technician")
    on_time = create_work_order(asset=vent, type="repair", priority="normal", problem="Ok")
    assert "Past due" not in client.get(wo_url(on_time)).content.decode()
    late = create_work_order(asset=vent, type="repair", priority="normal", problem="Late", opened_on=date.today() - timedelta(days=9),
                             due_on=date.today() - timedelta(days=1))
    assert "Past due" in client.get(wo_url(late)).content.decode()
    change_status(late, WoStatus.IN_PROGRESS)
    change_status(late, WoStatus.COMPLETED)
    assert "Past due" not in client.get(wo_url(late)).content.decode()


def test_vendor_assignment_and_recall(client, signed_in, ctx, pump, pump_recall):
    w = create_work_order(asset=pump, type="recall", priority="high", problem="Apply keypad fix", alert=pump_recall.alert)
    assign(w, vendor_name="BD Field Service")
    signed_in("technician")
    body = client.get(wo_url(w)).content.decode()
    assert "Vendor: BD Field Service" in body
    assert "<h2>Recall</h2>" in body and "FDA Z-TEST-1" in body and "Keypad membrane may allow fluid ingress" in body
    unassigned = create_work_order(asset=pump, type="repair", priority="normal", problem="Noisy")
    body = client.get(wo_url(unassigned)).content.decode()
    assert "<dd>Unassigned</dd>" in body and "<h2>Recall</h2>" not in body


def test_pm_checklist_prints_both_step_shapes(client, signed_in, ctx, vent, vent_model):
    vent_model.pm_procedure = PmProcedure.objects.create(code="HM-G5-PM6", name="Hamilton G5 6-month PM", source_reference="G5 service manual",
                                                         revision="C", checklist=["Inspect & clean", {"text": "Leakage current", "measure": "µA, limit 100"},
                                                                                  {"text": "Battery run time", "measure": True}, {"text": "Alarm check"}])
    vent_model.save()
    w = create_work_order(asset=vent, type="pm", priority="high", problem="Scheduled preventive maintenance", source=Source.PM_PLANNER)
    signed_in("technician")
    r = client.get(wo_url(w))
    body = r.content.decode()
    assert [s["text"] for s in r.context["steps"]] == ["Inspect & clean", "Leakage current", "Battery run time", "Alarm check"]
    assert [s["measured"] for s in r.context["steps"]] == [False, True, True, False]
    assert "<h2>PM checklist</h2>" in body and "HM-G5-PM6" in body and "Hamilton G5 6-month PM" in body
    assert "OEM service manual · G5 service manual · revision C" in body
    assert body.count('<td class="chk"><span class="box"></span></td>') == 4
    assert "<td>Inspect &amp; clean</td>" in body
    assert '<td class="meas"><span class="blank"></span> µA, limit 100</td>' in body
    assert '<td class="meas"><span class="blank"></span></td>' in body  # a measured step with no label still gets the blank
    assert body.count('<span class="blank">') == 2
    assert "Findings and repair" not in body and "No PM procedure is on file" not in body


def test_pm_without_a_procedure_leaves_ruled_lines(client, signed_in, ctx, vent):
    w = create_work_order(asset=vent, type="pm", priority="high", problem="Scheduled preventive maintenance")
    signed_in("technician")
    body = client.get(wo_url(w)).content.decode()
    assert "No PM procedure is on file for this model." in body
    assert body.count('<div class="rule"></div>') == views_print.RULED_LINES


def test_repair_has_findings_lines_while_open_and_the_resolution_when_completed(client, signed_in, ctx, vent):
    w = create_work_order(asset=vent, type="repair", priority="normal", problem="Cracked housing", resolution="Replaced the front housing")
    signed_in("technician")
    body = client.get(wo_url(w)).content.decode()
    assert "<h2>Findings and repair</h2>" in body and body.count('<div class="rule"></div>') == views_print.RULED_LINES
    assert "Replaced the front housing" not in body and "PM checklist" not in body
    change_status(w, WoStatus.IN_PROGRESS)
    change_status(w, WoStatus.COMPLETED)
    body = client.get(wo_url(w)).content.decode()
    assert "Findings and repair" not in body and '<div class="rule">' not in body
    assert "<h2>Resolution</h2>" in body and "Replaced the front housing" in body
    bare = create_work_order(asset=vent, type="inspection", priority="normal", problem="Incoming")
    change_status(bare, WoStatus.CANCELLED)
    assert "No resolution recorded." in client.get(wo_url(bare)).content.decode()


def test_labor_parts_and_totals(client, signed_in, ctx, wo, techs):
    LaborLine.objects.create(work_order=wo, technician=techs["dana"], worked_on=date(2026, 9, 28), hours=Decimal("1.5"), rate=Decimal("82"),
                             description="Diagnosed flow sensor")
    LaborLine.objects.create(work_order=wo, technician=None, worked_on=date(2026, 9, 29), hours=Decimal("0.25"), rate=Decimal("82"))
    PartLine.objects.create(work_order=wo, description="Flow sensor", part_number="FS-282", quantity=Decimal("2"), unit_cost=Decimal("45.50"),
                            po_number="PO-7781")
    PartLine.objects.create(work_order=wo, description="Main board", quantity=Decimal("1"), unit_cost=Decimal("1250"))
    signed_in("technician")
    body = client.get(wo_url(wo)).content.decode()
    assert "<td>Sep 28, 2026</td><td>Dana Whitfield</td><td>Diagnosed flow sensor</td>" in body
    assert '<td class="num">1.50</td><td class="num">$82.00</td><td class="num">$123.00</td>' in body
    assert '<td class="num">0.25</td><td class="num">$82.00</td><td class="num">$20.50</td>' in body
    assert '<td>Flow sensor</td><td class="mono">FS-282</td><td>PO-7781</td><td class="num">2</td>' in body
    assert '<td class="num">$45.50</td><td class="num">$91.00</td>' in body
    assert '<td class="num">$1,250.00</td>' in body
    assert '<td class="num">1.75 h</td><td></td><td class="num">$143.50</td>' in body  # labor total
    assert '<td colspan="3">Labor total so far</td>' in body  # open: totals are what is recorded so far
    assert '<td colspan="5">Parts total so far</td><td class="num">$1,341.00</td>' in body
    assert "<b>Total so far $1,484.50</b>" in body
    assert body.count('<tr class="fill">') == 2 * views_print.BLANK_ROWS  # room to write while open


def test_a_closed_work_order_without_lines_says_so(client, signed_in, ctx, wo):
    change_status(wo, WoStatus.IN_PROGRESS)
    change_status(wo, WoStatus.COMPLETED)
    signed_in("technician")
    body = client.get(wo_url(wo)).content.decode()
    assert "No labor recorded." in body and "No parts recorded." in body and '<tr class="fill">' not in body
    assert "<b>Total $0.00</b>" in body and "so far" not in body


def test_timeline_entries(client, signed_in, ctx, wo, make_user):
    tech = make_user("technician")
    change_status(wo, WoStatus.IN_PROGRESS, by=tech)
    add_note(wo, "Ordered a flow sensor", by=tech)
    signed_in("analyst")
    body = client.get(wo_url(wo)).content.decode()
    assert "Opened: Low tidal volume alarm" in body and "RN Patel" in body
    assert "Status changed to in progress" in body and "Ordered a flow sensor" in body and "Technician User" in body
    assert body.index("Opened: Low tidal volume alarm") < body.index("Status changed to in progress") < body.index("Ordered a flow sensor")


def test_user_text_is_escaped(client, signed_in, ctx, vent):
    w = create_work_order(asset=vent, type="repair", priority="normal", problem="<script>alert(1)</script> & <b>bold</b>", requester="<i>x</i>")
    signed_in("technician")
    body = client.get(wo_url(w)).content.decode()
    assert "<script>alert(1)" not in body and "<b>bold</b>" not in body and "<i>x</i>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; &lt;b&gt;bold&lt;/b&gt;" in body


def test_sign_off_lines(client, signed_in, wo):
    signed_in("technician")
    body = client.get(wo_url(wo)).content.decode()
    assert '<div class="sign"><div>Technician</div><div>Date</div></div>' in body
    assert "Electrical safety test" in body and "</span> Pass</span>" in body and "</span> Fail</span>" in body
    assert '<div class="sign"><div>Returned to service by</div><div>Date</div></div>' in body


def test_the_print_runs_a_fixed_number_of_queries(client, signed_in, ctx, wo, techs):
    signed_in("technician")
    client.get(wo_url(wo))
    with CaptureQueriesContext(connection) as before:
        client.get(wo_url(wo))
    for i in range(6):
        LaborLine.objects.create(work_order=wo, technician=techs["dana" if i % 2 else "tom"], hours=Decimal("1"), rate=Decimal("80"))
        PartLine.objects.create(work_order=wo, description=f"Part {i}", unit_cost=Decimal("10"))
        add_note(wo, f"Note {i}")
    with CaptureQueriesContext(connection) as after:
        client.get(wo_url(wo))
    assert len(after) == len(before)


def test_the_work_order_drawer_always_offers_print(client, signed_in, wo):
    signed_in("analyst")  # View only: no status actions, but the footer and its Print link are there
    r = client.get(f"/work-orders/{wo.number}/", **HX)
    body = r.content.decode()
    assert r.context["actions"] == []
    assert f'<a class="btn" href="/print/work-orders/{wo.number}/" target="_blank"' in body


def test_retired_and_other_risk_devices_still_print_in_the_list(client, signed_in, ctx, dept):
    dm = DeviceModel.objects.create(manufacturer="Welch Allyn", model="Spot 4400", description="Vital signs monitor", category="Monitors",
                                    risk_class=RiskClass.LOW)
    Asset.objects.create(tag="CE-30001", device_model=dm, department=dept, status="retired")
    signed_in("technician")
    assert tags_in(client.get("/print/labels/").content.decode()) == ["CE-30001"]
    assert tags_in(client.get("/print/labels/?status=active").content.decode()) == []


def test_a_long_request_url_never_wraps_into_the_hotline():
    """Under the QR there is room for one URL line plus the hotline: a longer URL gets a smaller face, then is left off the
    small label (the QR code carries it), then off the sheet label too."""
    from apps.web.views_print import url_size

    assert url_size("localhost:8000/r/riverside/?asset=CE-10241") == ""
    assert url_size("cadence.riversidehealth.org/r/riverside/?asset=CE-10241") == "long"  # 55 characters, the review's case
    assert url_size("cadence.riversidehealth.org/r/riverside-regional/?asset=CE-10241-B") == "wide"
    assert url_size("x" * 73) == "xwide"


def test_odd_checklist_shapes_print_rather_than_drop():
    from types import SimpleNamespace

    from apps.web.views_print import checklist_steps

    steps = checklist_steps(SimpleNamespace(checklist=[{"step": "Inspect housing"}, {"text": "Ground resistance", "measure": 0.3},
                                                         {"text": "Zero check", "measure": 0}, {"text": "Record leakage", "measure": True}, 7]))
    assert [(s["text"], s["measured"], s["measure"]) for s in steps] == [
        ("Inspect housing", False, ""), ("Ground resistance", True, "0.3"), ("Zero check", True, "0"), ("Record leakage", True, ""), ("7", False, "")]
    assert [s["text"] for s in checklist_steps(SimpleNamespace(checklist="1. Inspect\n\n2. Test\n"))] == ["1. Inspect", "2. Test"]
    assert checklist_steps(SimpleNamespace(checklist=[])) == [] and checklist_steps(None) == []


def test_printed_line_costs_add_up_to_the_printed_totals(client, signed_in, ctx, vent, techs):
    from decimal import Decimal

    from apps.workorders.models import LaborLine
    from apps.workorders.services import create_work_order

    wo = create_work_order(asset=vent, type="repair", priority="normal", problem="Alarm")
    for _ in range(2):
        LaborLine.objects.create(work_order=wo, technician=techs["dana"], hours=Decimal("1.33"), rate=Decimal("82.50"))
    signed_in("director")
    r = client.get(f"/print/work-orders/{wo.number}/")
    assert [cost for _line, cost in r.context["labor"]] == [Decimal("109.73"), Decimal("109.73")]
    assert r.context["labor_total"] == Decimal("219.46") == r.context["total"]
