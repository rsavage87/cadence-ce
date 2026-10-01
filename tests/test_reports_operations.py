"""Operations reports (slice 7): technician productivity and the recall response log. Math against fixed dates, tenant isolation,
the rendered page and CSV, and the empty tenant."""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from csvutil import csv_text

from apps.accounts.models import Level, Role
from apps.credentials.models import Technician
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel
from apps.recalls.models import Alert, AlertMatch
from apps.reports.operations import report_recall, report_tech
from apps.reports.services import run_report
from apps.tenants.context import tenant_context
from apps.workorders.models import LaborLine, WorkOrder, WoStatus
from apps.workorders.services import change_status, create_work_order

TODAY = date(2026, 9, 28)
S = AlertMatch.Status


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug):
        user = make_user(role_slug)
        client.force_login(user)
        return user

    return _as


def d(month, day):
    return date(2026, month, day)


def closed_wo(asset, type, tech, opened, completed, due=None, hours=None, **extra):
    wo = create_work_order(asset=asset, type=type, priority="normal", problem=f"{type} work", opened_on=opened, due_on=due or opened + timedelta(days=5),
                           assigned_to=tech, **extra)
    if hours is not None:
        LaborLine.objects.create(work_order=wo, technician=tech, hours=Decimal(str(hours)), rate=82)
    change_status(wo, "in_progress", as_of=opened)
    change_status(wo, "completed", as_of=completed)
    return wo


# --- technician productivity --------------------------------------------------------------------------

@pytest.fixture
def tech_data(ctx, vent, pump, techs):
    dana, tom = techs["dana"], techs["tom"]
    Technician.objects.create(name="Old Hand", title="Retired BMET", is_active=False)
    # Dana, inside the window: three PMs (one early, one late, one completed exactly on its due date, which counts as on time),
    # two repairs (turnaround 4 and 2 days), 5.0 hours over three of them.
    closed_wo(vent, "pm", dana, d(9, 1), d(9, 8), due=d(9, 10), hours=2.5)
    closed_wo(pump, "pm", dana, d(9, 2), d(9, 7), due=d(9, 5), hours=1.0)
    closed_wo(pump, "pm", dana, d(9, 12), d(9, 16), due=d(9, 16))
    closed_wo(vent, "repair", dana, d(9, 10), d(9, 14), hours=1.5)
    late_closed = closed_wo(pump, "repair", dana, d(9, 20), d(9, 22))
    change_status(late_closed, "closed", as_of=d(9, 23))  # closed after completion still counts once
    # Outside the window or not hers: a PM completed 31 days ago, a vendor repair, and one done exactly 30 days ago (counts).
    closed_wo(vent, "pm", dana, d(8, 20), d(8, 28), hours=9)
    closed_wo(pump, "repair", dana, d(9, 1), d(9, 3), hours=9, vendor_service=True, vendor_name="BD")
    closed_wo(pump, "inspection", dana, d(8, 25), d(8, 29), hours=0.5)
    # Open now: Dana holds two, Tom one; a cancelled one counts for nobody.
    create_work_order(asset=vent, type="repair", priority="high", problem="Alarm", opened_on=d(9, 27), assigned_to=dana)
    wip = create_work_order(asset=pump, type="repair", priority="normal", problem="Occlusion", opened_on=d(9, 26), assigned_to=dana)
    change_status(wip, "in_progress", as_of=d(9, 27))
    create_work_order(asset=pump, type="safety", priority="low", problem="Check", opened_on=d(9, 27), assigned_to=tom)
    cancelled = create_work_order(asset=pump, type="repair", priority="low", problem="Dup", opened_on=d(9, 27), assigned_to=tom)
    change_status(cancelled, "cancelled", as_of=d(9, 27))
    return techs


def test_tech_math_over_the_last_30_days(tech_data):
    r = report_tech(TODAY)
    assert r["columns"] == ["Technician", "Title", "Closed", "PMs", "Repairs", "Hours logged", "PM on time %", "Avg repair turnaround days", "Open now"]
    assert r["since"] == d(8, 29) and r["days"] == 30
    assert [row[0] for row in r["rows"]] == ["Dana Whitfield", "Tom Okafor"]  # active only, by name
    dana, tom = r["rows"]
    assert dana == ["Dana Whitfield", "Lead BMET", 6, 3, 2, 5.5, 2 / 3 * 100, 3.0, 2]  # two of three PMs on time (<= due_on)
    assert tom == ["Tom Okafor", "BMET I", 0, 0, 0, 0.0, 100.0, 0.0, 1]
    assert r["technicians"][0]["technician"] == tech_data["dana"] and r["technicians"][0]["pm_on_time_pct"] == pytest.approx(66.67, abs=0.01)
    assert all(isinstance(v, (str, int, float)) for row in r["rows"] for v in row)


def test_tech_turnaround_is_clamped_at_zero_for_a_bad_row(ctx, vent, techs):
    """change_status refuses to complete before opening, so build the bad row on the model directly (as a bad import could)."""
    dana = techs["dana"]
    WorkOrder.objects.create(asset=vent, type="repair", priority="normal", problem="Backdated", assigned_to=dana, status=WoStatus.COMPLETED,
                             opened_on=d(9, 20), due_on=d(9, 25), started_on=d(9, 20), completed_on=d(9, 15))
    closed_wo(vent, "repair", dana, d(9, 10), d(9, 14))
    row = report_tech(TODAY)["rows"][0]
    assert row[0] == "Dana Whitfield" and row[4] == 2
    assert row[7] == 2.0  # (0 + 4) / 2, not (-5 + 4) / 2
    assert row[7] >= 0


def test_tech_ignores_other_tenants_work(ctx, techs, vent, other_tenant):
    with tenant_context(other_tenant):
        dm = DeviceModel.objects.create(manufacturer="BD", model="Alaris 8015 PCU", description="Pump", category="Infusion pumps")
        a = Asset.objects.create(tag="THEIRS-1", device_model=dm, department=Department.objects.create(name="ICU"))
        t = Technician.objects.create(name="Their Tech", title="BMET")
        closed_wo(a, "pm", t, d(9, 10), d(9, 12), hours=4)
        create_work_order(asset=a, type="repair", priority="normal", problem="x", opened_on=d(9, 27), assigned_to=t)
    r = report_tech(TODAY)
    assert [row[0] for row in r["rows"]] == ["Dana Whitfield", "Tom Okafor"]
    assert all(row[2] == 0 and row[5] == 0.0 and row[8] == 0 for row in r["rows"])


def test_tech_page_and_csv(client, signed_in, freeze_today, tech_data):
    freeze_today(TODAY)
    signed_in("director")
    r = client.get("/reports/tech/")
    assert r.status_code == 200
    body = r.content.decode()
    assert 'aria-label="Work orders closed by technician"' in body
    assert "Dana Whitfield<small>Lead BMET</small>" in body and "Tom Okafor<small>BMET I</small>" in body
    assert '<td class="num down">67%</td>' in body and '<td class="num up">100%</td>' in body
    assert "<td class=\"num\">3.0 d</td>" in body and "<td class=\"num\">5.5</td>" in body
    assert "Vendor-performed work is excluded." in body and "Last 30 days, Aug 29 to Sep 28, 2026." in body
    assert "No active technicians." not in body
    csv = csv_text(client.get("/reports/tech.csv"))
    assert csv.startswith("Technician,Title,Closed,PMs,Repairs,Hours logged,PM on time %,Avg repair turnaround days,Open now")
    assert "Dana Whitfield,Lead BMET,6,3,2,5.50,66.67,3.00,2" in csv


def test_tech_renders_for_an_empty_tenant(client, signed_in, ctx):
    assert run_report("tech", TODAY) == {"columns": report_tech(TODAY)["columns"], "rows": [], "technicians": [], "since": d(8, 29), "days": 30,
                                         "pm_target": 95.0}
    signed_in("director")
    r = client.get("/reports/tech/")
    assert r.status_code == 200 and "No active technicians." in r.content.decode()
    assert "Work orders closed by technician" not in r.content.decode()


# --- recall response log ------------------------------------------------------------------------------

def make_alert(external_id, published, classification="Class II", manufacturer="BD", **extra):
    return Alert.objects.create(source=Alert.Source.FDA, external_id=external_id, classification=classification, manufacturer=manufacturer,
                                product="Pump", title=f"Notice {external_id}", published_on=published, **extra)


@pytest.fixture
def recall_data(ctx, dept, vent_model, pump_model, vent, pump):
    """Six matches, one per disposition (plus one with no published date), oldest alert first here; the report reverses them."""
    Asset.objects.create(tag="CE-10003", device_model=pump_model, department=dept, status=AssetStatus.RETIRED)  # never counted
    vent2 = Asset.objects.create(tag="CE-10004", device_model=vent_model, department=dept)
    m = {}
    m["closed"] = AlertMatch.objects.create(alert=make_alert("Z-0001-2026", d(8, 1)), device_model=pump_model, status=S.CLOSED,
                                            disposition_note="Firmware updated on all pumps", closed_on=d(8, 20))
    m["closed_bare"] = AlertMatch.objects.create(alert=make_alert("Z-0002-2026", d(8, 5), classification=""), device_model=pump_model, status=S.CLOSED,
                                                 closed_on=d(8, 21))
    m["not_affected"] = AlertMatch.objects.create(alert=make_alert("Z-0003-2026", d(8, 10)), device_model=pump_model, status=S.NOT_AFFECTED, closed_on=d(8, 11))
    progressing = make_alert("Z-0004-2026", d(9, 1), manufacturer="Hamilton Medical", classification="Class I")
    m["in_progress"] = AlertMatch.objects.create(alert=progressing, device_model=vent_model, status=S.IN_PROGRESS)
    # Progress counts devices, not work orders: vent is done twice (one device), vent2 is not, and the retired vent's completed
    # work order counts for nothing, so the match reads "1 of 2 devices completed".
    retired_vent = Asset.objects.create(tag="CE-10005", device_model=vent_model, department=dept, status=AssetStatus.RETIRED)
    for asset, done in ((vent, True), (vent, True), (vent2, False), (retired_vent, True)):
        wo = create_work_order(asset=asset, type="recall", priority="high", problem="Recall", opened_on=d(9, 2), alert=progressing)
        if done:
            change_status(wo, "in_progress", as_of=d(9, 3))
            change_status(wo, "completed", as_of=d(9, 4))
    m["needs_action"] = AlertMatch.objects.create(alert=make_alert("Z-0005-2026", d(9, 26)), device_model=pump_model)
    m["undated"] = AlertMatch.objects.create(alert=make_alert("Z-0006-2026", None), device_model=pump_model, status=S.UNDER_REVIEW)
    return m


def test_recall_log_rows_newest_first_with_responses(recall_data):
    r = report_recall(TODAY)
    assert r["columns"] == ["Received", "Alert", "Source", "Class", "Manufacturer", "Model", "Affected devices", "Status", "Response", "Closed on"]
    assert r["count"] == 6 and not r["has_sample"]
    # Newest first, undated last: the report orders with nulls_last=True, because a bare "-alert__published_on" puts NULLs first
    # on PostgreSQL and last on SQLite, and this assertion must hold on both.
    assert [row[1] for row in r["rows"]] == [f"FDA Z-000{n}-2026" for n in (5, 4, 3, 2, 1, 6)]
    by_id = {row[1]: row for row in r["rows"]}
    assert by_id["FDA Z-0005-2026"] == [d(9, 26), "FDA Z-0005-2026", "FDA", "Class II", "BD", "Alaris 8015 PCU", 1, "Needs action", "Open 2 d", None]
    assert by_id["FDA Z-0004-2026"] == [d(9, 1), "FDA Z-0004-2026", "FDA", "Class I", "Hamilton Medical", "Hamilton-G5", 2, "Action in progress",
                                        "1 of 2 devices completed", None]
    assert by_id["FDA Z-0003-2026"][7:] == ["Reviewed, not affected", "Reviewed, not affected", d(8, 11)]
    assert by_id["FDA Z-0002-2026"][3] == "" and by_id["FDA Z-0002-2026"][7:] == ["Closed", "Closed", d(8, 21)]
    assert by_id["FDA Z-0001-2026"][8:] == ["Firmware updated on all pumps", d(8, 20)]
    assert by_id["FDA Z-0006-2026"][0] is None and by_id["FDA Z-0006-2026"][7:] == ["Under review", "Open", None]
    first = r["matches"][0]
    assert first["match"] == recall_data["needs_action"] and first["devices"] == 1 and first["url"] == f"/recalls/?match={first['match'].pk}"


def test_recall_log_ignores_other_tenants_matches(ctx, pump, pump_recall, other_tenant):
    with tenant_context(other_tenant):
        dm = DeviceModel.objects.create(manufacturer="BD", model="Alaris 8015 PCU", description="Pump", category="Infusion pumps")
        Asset.objects.create(tag="THEIRS-1", device_model=dm, department=Department.objects.create(name="ICU"))
        Asset.objects.create(tag="THEIRS-2", device_model=dm, department=Department.objects.get(name="ICU"))
        AlertMatch.objects.create(alert=pump_recall.alert, device_model=dm, status=S.IN_PROGRESS)
        AlertMatch.objects.create(alert=make_alert("Z-THEIRS", d(9, 27)), device_model=dm)
    r = report_recall(TODAY)
    assert r["count"] == 1 and r["rows"][0][1] == "FDA Z-TEST-1" and r["rows"][0][6] == 1  # our one pump, not their two


def test_recall_page_links_and_csv(client, signed_in, freeze_today, recall_data):
    freeze_today(TODAY)
    signed_in("director")
    r = client.get("/reports/recall/")
    assert r.status_code == 200
    body = r.content.decode()
    assert body.count('<span class="chip crit">Needs action</span>') == 1 and '<span class="chip info">Action in progress</span>' in body
    assert '<span class="chip ok">Closed</span>' in body and '<span class="chip neutral">Reviewed, not affected</span>' in body
    assert f'<a class="link" href="/recalls/?match={recall_data["in_progress"].pk}"><span class="tag">FDA Z-0004-2026</span></a>' in body
    assert "<small>FDA · Class I</small>" in body and "<small>FDA · Class not stated</small>" in body
    assert "Hamilton-G5<small>Hamilton Medical</small>" in body
    assert '<td class="muted">Sep 26</td>' in body and '<td class="muted">—</td>' in body
    assert "1 of 2 devices completed" in body and "Open 2 d" in body and "Firmware updated on all pumps" in body
    assert "This log is what a surveyor asks for" in body and "Sample alerts for demonstration only" not in body
    assert "Each row links to the alert and its work orders on the Recalls screen." in body
    csv = csv_text(client.get("/reports/recall.csv"))
    assert csv.startswith("Received,Alert,Source,Class,Manufacturer,Model,Affected devices,Status,Response,Closed on")
    assert "2026-08-01,FDA Z-0001-2026,FDA,Class II,BD,Alaris 8015 PCU,1,Closed,Firmware updated on all pumps,2026-08-20" in csv


def test_recall_page_does_not_link_without_recalls_access(client, make_user, signed_in, recall_data):
    role = Role.objects.create(name="Reports only", slug="reports_only")
    role.set_levels({"reports": Level.VIEW})
    client.force_login(make_user("reports_only"))
    body = client.get("/reports/recall/").content.decode()
    assert "?match=" not in body and '<span class="tag">FDA Z-0004-2026</span>' in body
    assert "This log is what a surveyor asks for" in body and "Each row links to the alert" not in body
    signed_in("director")
    assert "Each row links to the alert and its work orders on the Recalls screen." in client.get("/reports/recall/").content.decode()


def test_recall_page_flags_sample_alerts(client, signed_in, ctx, pump_model):
    AlertMatch.objects.create(alert=make_alert("Z-DEMO", d(9, 20), raw={"demo": True}), device_model=pump_model)
    assert report_recall(TODAY)["has_sample"]
    signed_in("director")
    assert "Sample alerts for demonstration only" in client.get("/reports/recall/").content.decode()


def test_recall_renders_for_an_empty_tenant(client, signed_in, ctx):
    r = run_report("recall", TODAY)
    assert r["rows"] == [] and r["matches"] == [] and r["count"] == 0 and not r["has_sample"]
    signed_in("director")
    page = client.get("/reports/recall/")
    assert page.status_code == 200 and "No alerts have matched the inventory." in page.content.decode()
    assert WorkOrder.objects.count() == 0
