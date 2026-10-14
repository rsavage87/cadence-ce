"""
Slice 33, wave 2C: the readers of what each repair found (WorkOrder.repair_finding; apps.workorders.findings).

- Reliability by model (apps.reports.fleet.report_mtbf): "Repairs, 6 mo" every repair, "Failures, 6 mo" by findings.failure_q (a repair
  with no finding, not stated, or open counts); the rate, MTBF, flagged, and the ranking from failures; turnaround and cost over every
  completed repair; a model with repairs and no failures listed at rate 0 with no MTBF; the note's left-out counts and coverage; six
  queries.
- The AEM evidence (apps.pm.aem.evidence): failures, pm_related, found_on_pm, findings_recorded, completed, and failure_rule beside
  repairs; finding_figures (PM-related only at 75% coverage and 5 findings); evidence from before reads "not recorded then" everywhere
  (the tab, the binder, the history) and is never compared with failures.
- The AEM tab: the figures with their coverage, and "Record what was found" (PM Approve with Work orders Edit; Approve once closed; each
  row offers findings.choices_for; a POST records through set_repair_finding; refusals as toasts; a fixed number of queries).
- The survey binder: the repairs section's "What was found" column, its table by finding, and its figures; the AEM section's failures
  since approval only beside evidence that counted them.
- The program evaluation: the findings indicators (coverage, then shares of the repairs with a finding; "Not recorded" with none), the
  AEM table's failures and PM-related per device-year, an older approved snapshot's previous column "Not counted then", and an older
  snapshot's AEM rows on the page and the print.
- Custom reports: the work orders source's "Repair finding" column, grouping, and filter.
- Another facility's findings never counted; the readers under row-level security as the runtime role.
"""
import json
from datetime import date, timedelta
from decimal import Decimal

import pytest
from csvutil import csv_text
from django.core.exceptions import ValidationError
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from evaluation_fixtures import approved, last_year
from incident_fixtures import make_incident
from pg_helpers import as_app_role, needs_postgres
from survey_helpers import period, rows_of

from apps.accounts.models import Level, Module, Role, User
from apps.core.history import _evidence_text
from apps.equipment.models import Asset, Department, DeviceModel, RiskClass
from apps.evaluations import indicators
from apps.pm import aem
from apps.pm.dates import add_months
from apps.pm.models import AemDecision, AemStatus
from apps.reports import custom
from apps.reports.fleet import report_mtbf
from apps.reports.survey import aem as binder_aem
from apps.reports.survey import repairs as binder_repairs
from apps.tenants.context import tenant_context
from apps.web import views_aem
from apps.workorders import findings
from apps.workorders.models import LaborLine, RepairFinding, WorkOrder, WoStatus, WoType
from apps.workorders.services import create_work_order

F = RepairFinding
DAY = timedelta(days=1)
HX = {"HTTP_HX_REQUEST": "true"}
MTBF_TODAY = date(2026, 9, 28)  # the trailing window opens Mar 30, 2026
OLD_EVIDENCE = {"as_of": "2025-06-01", "since": "2022-06-01", "years": 3, "devices_active": 4, "devices_retired": 0, "devices_undated": 0,
                "device_years": 10.0, "repairs": 6, "repairs_per_device_year": 0.6, "pm_completed": 8, "pm_on_time": 8, "pm_on_time_pct": 100,
                "recall_work_orders": 0, "open_recalls": 0, "oldest_install": "2020-01-01", "history_years": 5.4, "enough_history": True}


def repair(asset, opened, *, completed=None, finding="", status=None, follow_up_of=None, problem="Alarm fault") -> WorkOrder:
    """A repair opened on `opened`, stored as completed on `completed` with `finding` (or left open, or given `status`): the readers count
    what is stored, whichever door wrote it."""
    wo = create_work_order(asset=asset, type=WoType.REPAIR, priority="normal", problem=problem, opened_on=opened)
    fields = {"repair_finding": str(finding), "follow_up_of": follow_up_of}
    if completed:
        fields.update(status=WoStatus.COMPLETED, started_on=opened, completed_on=completed)
    if status:
        fields["status"] = status
    WorkOrder.objects.filter(pk=wo.pk).update(**fields)
    wo.refresh_from_db()
    return wo


def a_pm(asset, opened) -> WorkOrder:
    return create_work_order(asset=asset, type=WoType.PM, priority="normal", problem="Scheduled PM", opened_on=opened)


def device(dm, dept, tag, **kw) -> Asset:
    return Asset.objects.create(tag=tag, device_model=dm, department=dept, **kw)


def model(name, risk=RiskClass.MEDIUM, **kw) -> DeviceModel:
    return DeviceModel.objects.create(manufacturer="Acme", model=name, description=f"{name} device", category="Monitors", risk_class=risk,
                                      oem_pm_interval_months=12, **kw)


@pytest.fixture
def today(ctx):
    return timezone.localdate()


# --- Reliability by model -----------------------------------------------------------------------------------------------------------


@pytest.fixture
def mtbf(ctx, dept, vent_model, pump_model, vent, pump):
    """The ventilators: 9 repairs in the window, 4 of them failures (worn out; none recorded; not stated by the vendor; open again with a
    use error stored), 5 left out (one of each non-failure finding), and a cancelled one. The pumps: two repairs, neither a failure.
    A monitor: one open repair. A ventilator repair before the window."""
    device(vent_model, dept, "CE-10003")
    device(pump_model, dept, "CE-10005")
    device(pump_model, dept, "CE-10006")
    monitor = model("Mon-2")
    mon = device(monitor, dept, "CE-10007")
    o = date(2026, 9, 1)
    worn = repair(vent, o, completed=o + 4 * DAY, finding=F.WEAR)
    use = repair(vent, o, completed=o + 2 * DAY, finding=F.USE_ERROR)
    LaborLine.objects.create(work_order=use, hours=2, rate=82)  # a use error's work still costs: in the average
    for finding in (F.NO_PROBLEM_FOUND, F.DAMAGE, F.ACCESSORY, F.ENVIRONMENT, "", F.NOT_STATED):
        repair(vent, o, completed=o + 2 * DAY, finding=finding)
    repair(vent, o, finding=F.USE_ERROR, status=WoStatus.IN_PROGRESS)  # reopened: open, a failure whatever it stored
    repair(vent, o, finding="", status=WoStatus.CANCELLED)  # never a repair that counts
    repair(vent, date(2026, 3, 1), completed=date(2026, 3, 3), finding=F.WEAR)  # before the window
    repair(pump, o, completed=o + 1 * DAY, finding=F.USE_ERROR)
    repair(pump, o, completed=o + 3 * DAY, finding=F.NO_PROBLEM_FOUND)
    repair(mon, o)
    return {"vent": vent_model, "pump": pump_model, "monitor": monitor, "worn": worn}


def test_mtbf_ranks_by_failures_and_keeps_every_repair(mtbf):
    with CaptureQueriesContext(connection) as q:
        r = report_mtbf(MTBF_TODAY)
    assert len(q) <= 6  # the failures and coverage ride on the repairs query
    v, m, p = r["models"]
    assert [x["device_model"] for x in (v, m, p)] == [mtbf["vent"], mtbf["monitor"], mtbf["pump"]]
    assert (v["in_service"], v["repairs"], v["failures"]) == (2, 9, 4)
    assert v["rate"] == 4 / 2 * 2 and v["mtbf_days"] == 2 * 182 / 4 and v["flagged"]
    assert v["turnaround"] == (4 + 2 * 7) / 8 and v["cost"] == 164 / 8  # every completed repair, the use error's labor included
    assert (m["repairs"], m["failures"], m["rate"], m["mtbf_days"]) == (1, 1, 2.0, 182.0)  # open: a failure until completed
    # repairs, no failures: listed, rate 0, no time between failures to measure, never flagged
    assert (p["in_service"], p["repairs"], p["failures"], p["rate"], p["mtbf_days"], p["flagged"]) == (3, 2, 0, 0.0, None, False)
    assert p["turnaround"] == 2.0
    assert r["left_out"] == [{"value": "no_problem_found", "label": "No problem found", "n": 2}, {"value": "use_error", "label": "Use error", "n": 2},
                             {"value": "environment", "label": "Power, gas, network, room", "n": 1},
                             {"value": "damage", "label": "Damaged in handling", "n": 1},
                             {"value": "accessory", "label": "Accessory or cable", "n": 1}]
    assert (r["left_out_total"], r["completed"], r["recorded"]) == (7, 10, 8)  # blank and not stated have no finding recorded
    assert r["columns"][4:7] == ["Repairs, 6 mo", "Failures, 6 mo", "Failures per device per year"]
    assert r["rows"][0][3:8] == [2, 9, 4, 4.0, 91] and r["rows"][2][3:8] == [3, 2, 0, 0.0, None]


def test_mtbf_page_and_csv_say_what_was_left_out(client, make_user, mtbf, freeze_today):
    freeze_today(MTBF_TODAY)
    client.force_login(make_user("director"))
    body = client.get("/reports/mtbf/").content.decode()
    assert '<td class="num">2</td><td class="num">9</td><td class="num">4</td><td class="num down">4.00</td><td class="num">91</td>' in body
    assert '<td class="num">3</td><td class="num">2</td><td class="num">0</td><td class="num">0.00</td><td class="num">—</td>' in body
    assert findings.FAILURE_RULE in body
    assert ("Left out of the failures, still in turnaround and cost: 7 repairs (no problem found: 2; use error: 2; power, gas, network, "
            "room: 1; damaged in handling: 1; accessory or cable: 1).") in body
    assert "What was found is recorded on 8 of the 10 repairs completed in this window: the others count as failures." in body
    csv = csv_text(client.get("/reports/mtbf.csv")).splitlines()
    assert csv[1] == "Hamilton Medical,Hamilton-G5,ICU ventilator,2,9,4,4.00,91,2.25,20.50"
    assert "user error" not in body.lower() and "abuse" not in body.lower()


def test_mtbf_never_counts_another_facilitys_findings(mtbf, other_tenant):
    before = report_mtbf(MTBF_TODAY)
    with tenant_context(other_tenant):
        d = Department.objects.create(name="Their ICU")
        theirs = device(DeviceModel.objects.create(manufacturer="Hamilton Medical", model="Hamilton-G5", description="Vent",
                                                   category="Ventilators"), d, "THEIRS-1")
        repair(theirs, date(2026, 9, 2), completed=date(2026, 9, 3), finding=F.WEAR)
    after = report_mtbf(MTBF_TODAY)
    assert after["rows"] == before["rows"] and (after["completed"], after["recorded"]) == (before["completed"], before["recorded"])


# --- the AEM evidence ---------------------------------------------------------------------------------------------------------------


@pytest.fixture
def monitor(ctx, dept, today):
    """A model with four years of history on two devices."""
    dm = model("MX750", RiskClass.HIGH)
    for tag in ("M-1", "M-2"):
        device(dm, dept, tag, installed_on=add_months(today, -48), next_pm_on=today + 30 * DAY)
    return dm


def history(dm, today):
    """Repairs in the evidence's three years: worn out, PM could have caught it, caused by earlier service found on a PM, a use error, none
    recorded, not stated, an open one, an open one from a failed PM; a cancelled one, and one before the window."""
    m1, m2 = Asset.objects.filter(device_model=dm).order_by("tag")
    d = lambda n: today - n * DAY  # noqa: E731
    pm = a_pm(m1, d(300))
    out = {
        "worn": repair(m1, d(400), completed=d(398), finding=F.WEAR),
        "pm_preventable": repair(m1, d(350), completed=d(349), finding=F.PM_PREVENTABLE),
        "found_on_pm": repair(m2, d(300), completed=d(290), finding=F.SERVICE_INDUCED, follow_up_of=pm),
        "use": repair(m2, d(200), completed=d(199), finding=F.USE_ERROR),
        "blank": repair(m1, d(150), completed=d(149)),
        "not_stated": repair(m2, d(120), completed=d(110), finding=F.NOT_STATED),
        "open": repair(m1, d(20)),
        "open_from_pm": repair(m2, d(10), follow_up_of=pm),
    }
    repair(m1, d(30), finding=F.PM_PREVENTABLE, status=WoStatus.CANCELLED)
    repair(m1, add_months(today, -37), completed=add_months(today, -37) + DAY, finding=F.WEAR)
    return out


def test_evidence_counts_failures_coverage_and_the_rule_beside_repairs(monitor, today):
    history(monitor, today)
    ev = aem.evidence(monitor, today)
    assert json.loads(json.dumps(ev)) == ev
    assert ev["repairs"] == 8 and ev["repairs_per_device_year"] == round(8 / ev["device_years"], 2)  # every repair, as before
    assert (ev["failures"], ev["completed"], ev["findings_recorded"], ev["pm_related"], ev["found_on_pm"]) == (7, 6, 4, 2, 2)
    assert ev["failure_rule"] == findings.FAILURE_RULE
    found = aem.finding_figures(ev)
    assert found["coverage"] == "4 of the 6 completed repairs with a finding recorded" and not found["enough"]  # 4 < 5
    assert found["pm_related_pct"] is None and found["pm_related_per_device_year"] is None
    assert found["failures_per_device_year"] == round(7 / ev["device_years"], 2)


def test_pm_related_reads_only_with_enough_findings_recorded(monitor, today):
    history(monitor, today)
    m1 = Asset.objects.get(tag="M-1")
    for n in (60, 61):
        repair(m1, today - n * DAY, completed=today - (n - 1) * DAY, finding=F.WEAR)
    found = aem.finding_figures(aem.evidence(monitor, today))
    assert (found["recorded"], found["completed"]) == (6, 8) and found["enough"]  # 6 of 8 is 75%, and 6 >= 5
    assert found["pm_related_pct"] == 33 and found["pm_related"] == 2
    repair(m1, today - 50 * DAY, completed=today - 49 * DAY)  # one more with none: 6 of 9 is under 75%
    assert not aem.finding_figures(aem.evidence(monitor, today))["enough"]
    assert aem.coverage_enough(5, 5) and not aem.coverage_enough(4, 4) and aem.coverage_enough(75, 100) and not aem.coverage_enough(74, 100)


def test_evidence_from_before_reads_not_recorded_then(monitor, today, make_user):
    assert aem.finding_figures(OLD_EVIDENCE) is None and aem.finding_figures(None) is None and aem.finding_figures({}) is None
    old = _evidence_text(OLD_EVIDENCE)
    assert "failure" not in old and old.startswith("Jun 1, 2022 to Jun 1, 2025: 4 devices, 10 device-years, 6 repairs (0.6 per device-year)")
    history(monitor, today)
    ev = aem.evidence(monitor, today)
    text = _evidence_text(ev)
    assert f"7 device failures ({round(7 / ev['device_years'], 2)} per device-year), what was found recorded on 4 of 6 completed" in text
    assert "PM-related" not in text  # not enough findings recorded


# --- the AEM tab ----------------------------------------------------------------------------------------------------------------


def tab(client, dm) -> str:
    r = client.get(f"/pm/models/{dm.pk}/?tab=aem", **HX)
    assert r.status_code == 200
    return r.content.decode()


def test_the_tab_shows_failures_with_their_coverage_and_old_evidence_as_not_recorded_then(client, make_user, monitor, today):
    history(monitor, today)
    tech = make_user("technician")
    AemDecision.objects.create(device_model=monitor, interval_months=24, oem_interval_months=12, rationale="Few failures.", proposed_on=today,
                               proposed_by=tech, evidence=OLD_EVIDENCE)
    client.force_login(make_user("analyst"))
    body = tab(client, monitor)
    assert '<div class="v">7</div><div class="l">device failures,' in body
    assert "What was found: 4 of the 6 completed repairs with a finding recorded; PM-related is shown once at least 75% of them" in body
    assert "PM-related: not enough findings recorded" in body and '<div class="v">2</div><div class="l">found on a PM</div>' in body
    assert findings.FAILURE_RULE in body
    # the proposal's evidence, from before findings: never failures beside its repairs
    assert "Failure history when proposed" in body and "device failures: not recorded then" in body
    assert "taken before Cadence recorded what each repair found: its repairs are every corrective repair, never compared with device" in body
    assert "Record what was found" not in body  # an analyst records nothing


@pytest.fixture
def unrecorded(monitor, today, dept):
    """Completed repairs of the model with no finding: an investigation of an incident, a failed PM's repair, a plain one closed, a
    plain one; besides, one with a finding, one not stated, an open one, a cancelled one, one before the window, another model's."""
    m1, m2 = Asset.objects.filter(device_model=monitor).order_by("tag")
    d = lambda n: today - n * DAY  # noqa: E731
    investigation = make_incident(m2, hold=False).work_order
    WorkOrder.objects.filter(pk=investigation.pk).update(status=WoStatus.COMPLETED, started_on=investigation.opened_on,
                                                         completed_on=investigation.opened_on)
    out = {
        "investigation": investigation,
        "from_pm": repair(m1, d(40), completed=d(39), follow_up_of=a_pm(m1, d(41))),
        "closed": repair(m1, d(100), completed=d(99), status=WoStatus.CLOSED),
        "plain": repair(m2, d(200), completed=d(198)),
    }
    repair(m1, d(10), completed=d(9), finding=F.WEAR)
    repair(m1, d(11), completed=d(10), finding=F.NOT_STATED)
    repair(m1, d(12))
    repair(m1, d(13), status=WoStatus.CANCELLED)
    repair(m1, add_months(today, -37), completed=add_months(today, -37) + DAY)
    other = device(model("Other"), dept, "O-1")
    repair(other, d(14), completed=d(13))
    out["other"] = WorkOrder.objects.get(asset=other)
    return out


def options(body: str, number: str) -> list[str]:
    """The finding values offered on `number`'s row."""
    row = body.split(f'name="number" value="{number}"', 1)[1].split("</select>", 1)[0]
    return [part.split('"', 1)[0] for part in row.split('<option value="')[1:] if part.split('"', 1)[0]]


def test_record_what_was_found_lists_the_models_repairs_with_none(client, make_user, unrecorded, monitor):
    client.force_login(make_user("manager"))
    body = tab(client, monitor)
    assert "Record what was found <span>4 completed repairs with none</span>" in body
    numbers = [unrecorded[k].number for k in ("investigation", "from_pm", "closed", "plain")]
    assert all(f'name="number" value="{n}"' in body for n in numbers) and unrecorded["other"].number not in body
    in_house = ["no_problem_found", "use_error", "environment", "damage", "battery", "accessory", "software", "wear", "pm_preventable",
                "service_induced"]
    assert options(body, unrecorded["plain"].number) == in_house  # never not stated: not vendor service
    assert options(body, unrecorded["investigation"].number) == [v for v in in_house if v != "use_error"]
    assert options(body, unrecorded["from_pm"].number) == [v for v in in_house if v != "pm_preventable"]
    assert "found on a PM: choose what failed" in body and findings.ORDER_WORDS in body and findings.HINTS[F.USE_ERROR] in body
    assert f'hx-post="/pm/models/{monitor.pk}/aem/finding/"' in body


def test_recording_one_goes_through_the_service_and_answers_with_the_tab(client, make_user, unrecorded, monitor, today):
    manager = make_user("manager")
    client.force_login(manager)
    wo = unrecorded["plain"]
    r = client.post(f"/pm/models/{monitor.pk}/aem/finding/", {"number": wo.number, "repair_finding": F.BATTERY}, **HX)
    body = r.content.decode()
    assert r.status_code == 200 and json.loads(r["HX-Trigger"])["toast"]["value"] == f"Recorded what {wo.number} found: Battery failed"
    assert "Record what was found <span>3 completed repairs with none</span>" in body and f'value="{wo.number}"' not in body
    wo.refresh_from_db()
    assert wo.repair_finding == F.BATTERY
    rec = wo.history.order_by("-history_date", "-history_id").first()
    assert rec.history_change_reason == "Recorded later: Battery failed" and rec.history_user_id == manager.pk
    # refused in the service's words, nothing stored
    inv = unrecorded["investigation"]
    r = client.post(f"/pm/models/{monitor.pk}/aem/finding/", {"number": inv.number, "repair_finding": F.USE_ERROR}, **HX)
    assert r.status_code == 200 and json.loads(r["HX-Trigger"])["toast"]["value"] == findings.INVESTIGATION
    inv.refresh_from_db()
    assert inv.repair_finding == ""
    r = client.post(f"/pm/models/{monitor.pk}/aem/finding/", {"number": unrecorded["from_pm"].number, "repair_finding": ""}, **HX)
    assert json.loads(r["HX-Trigger"])["toast"]["value"] == findings.REQUIRED
    # a closed one needs Work orders Approve, which a manager has
    closed = unrecorded["closed"]
    assert client.post(f"/pm/models/{monitor.pk}/aem/finding/", {"number": closed.number, "repair_finding": F.WEAR}, **HX).status_code == 200
    closed.refresh_from_db()
    assert closed.repair_finding == F.WEAR
    # another model's work order through this model's address, and a number that is no repair of it: 404
    assert client.post(f"/pm/models/{monitor.pk}/aem/finding/", {"number": unrecorded["other"].number, "repair_finding": F.WEAR},
                       **HX).status_code == 404
    assert client.get(f"/pm/models/{monitor.pk}/aem/finding/", **HX).status_code == 405


def test_who_may_record_from_the_tab(client, make_user, ctx, unrecorded, monitor):
    url = f"/pm/models/{monitor.pk}/aem/finding/"
    plain, closed = unrecorded["plain"], unrecorded["closed"]
    for slug in ("technician", "analyst"):  # PM Edit, PM View: no list, and refused
        client.force_login(make_user(slug))
        assert "Record what was found" not in tab(client, monitor)
        assert client.post(url, {"number": plain.number, "repair_finding": F.WEAR}, **HX).status_code == 403
    for slug in ("vendor", "requester"):  # scoped: never
        client.force_login(make_user(slug))
        assert client.post(url, {"number": plain.number, "repair_finding": F.WEAR}, **HX).status_code == 403
    # PM Approve with Work orders Edit: open ones, but a closed one needs Work orders Approve
    role = Role.objects.create(name="AEM reviewer", slug="aem-reviewer")
    role.set_levels({Module.PM: Level.APPROVE, Module.WORKORDERS: Level.EDIT, Module.EQUIPMENT: Level.VIEW})
    user = User.objects.create_user(username="reviewer@riverside.example", password="Test-Pass-2026-x", tenant=ctx, role=role)
    client.force_login(user)
    body = tab(client, monitor)
    assert f'name="number" value="{plain.number}"' in body and closed.number not in body
    assert "1 closed repair with none is not listed: recording one needs Work orders Approve." in body  # review fix: never listed
    assert client.post(url, {"number": closed.number, "repair_finding": F.WEAR}, **HX).status_code == 403
    assert client.post(url, {"number": plain.number, "repair_finding": F.WEAR}, **HX).status_code == 200
    role.set_levels({Module.WORKORDERS: Level.VIEW})  # PM Approve alone: no list
    assert "Record what was found" not in tab(client, monitor)
    for wo in (plain, closed):
        wo.refresh_from_db()
    assert (plain.repair_finding, closed.repair_finding) == (F.WEAR, "")


def test_the_list_takes_a_fixed_number_of_queries_and_shows_the_latest(client, make_user, monitor, today):
    m1 = Asset.objects.get(tag="M-1")
    for n in range(3):
        repair(m1, today - (100 + n) * DAY, completed=today - (99 + n) * DAY)
    client.force_login(make_user("manager"))
    tab(client, monitor)  # warm up

    def queries() -> int:
        with CaptureQueriesContext(connection) as q:
            tab(client, monitor)
        return len(q)

    few = queries()
    for n in range(30):
        repair(m1, today - (200 + n) * DAY, completed=today - (199 + n) * DAY, follow_up_of=a_pm(m1, today - (201 + n) * DAY) if n % 2 else None)
    assert queries() == few
    body = tab(client, monitor)
    assert f"The latest {views_aem.RECORD_SHOWN} are listed; recording one brings the next." in body
    assert body.count('<form class="aem-rec-f"') == views_aem.RECORD_SHOWN


# --- the survey binder ----------------------------------------------------------------------------------------------------------


def test_the_repairs_section_by_finding(ctx, today, monitor):
    m1, m2 = Asset.objects.filter(device_model=monitor).order_by("tag")
    d = lambda n: today - n * DAY  # noqa: E731
    pm = a_pm(m1, d(40))
    worn = repair(m1, d(30), completed=d(29), finding=F.WEAR)
    repair(m1, d(28), completed=d(27), finding=F.USE_ERROR)
    repair(m2, d(26), completed=d(25), finding=F.BATTERY, follow_up_of=pm)
    blank = repair(m2, d(24), completed=d(23))
    repair(m2, d(22), completed=d(21), finding=F.NOT_STATED)
    repair(m2, d(20), finding=F.WEAR)  # open: not completed in the period
    s = binder_repairs.build(period(d(60), today), None)
    figures = {f.label: f.value for f in s.figures}
    assert figures["What was found recorded"] == "3 of 5" and figures["Found on a PM"] == 1
    rows = {r[0]: r for r in rows_of(s, "repairs")}
    assert rows[worn.number][-1] == "Wore out or failed in use" and rows[blank.number][-1] == "Not recorded"
    by = {r[0]: r for r in rows_of(s, "findings")}
    assert s.table("findings").columns == binder_repairs.FINDING_COLUMNS
    assert list(by) == [findings.label(v) for _label, values in findings.GROUPS for v in values] + ["Not recorded"]
    assert by["Wore out or failed in use"] == ["Wore out or failed in use", "Device fault", "Yes", 1, 0]
    assert by["Use error"] == ["Use error", "No device fault", "No", 1, 0]
    assert by["Battery failed"] == ["Battery failed", "Device fault", "Yes", 1, 1]
    assert by["Not stated by the vendor"] == ["Not stated by the vendor", "The vendor's report", "Yes", 1, 0]
    assert by["Not recorded"] == ["Not recorded", "Not recorded", "Yes", 1, 0]
    assert by["Damaged in handling"][3:] == [0, 0]
    assert any(findings.FAILURE_RULE in n for n in s.notes) and "Alarm fault" not in repr(rows_of(s, "repairs"))


def test_the_aem_section_compares_failures_only_with_evidence_that_counted_them(ctx, today, monitor, make_user, dept):
    history(monitor, today)
    tech, manager = make_user("technician"), make_user("manager")
    new = aem.approve(aem.propose(monitor, interval_months=24, rationale="Few failures.", by=tech, today=today - 60 * DAY),
                      by=manager, decided_on=today - 60 * DAY, note="EMC 4", today=today - 60 * DAY)
    old_model = model("Old", aem_interval_months=24)
    old_dev = device(old_model, dept, "OLD-1", installed_on=add_months(today, -60))
    AemDecision.objects.create(device_model=old_model, interval_months=24, oem_interval_months=12, status=AemStatus.APPROVED, rationale="r",
                               proposed_on=today - 90 * DAY, decided_on=today - 80 * DAY, decision_note="EMC 2", evidence=OLD_EVIDENCE)
    repair(old_dev, today - 5 * DAY)  # open: a failure, and a repair
    s = binder_aem.build(period(today - 100 * DAY, today), None)
    cols = binder_aem.IN_FORCE_COLUMNS
    rows = {r[1]: dict(zip(cols, r)) for r in rows_of(s, "in_force")}
    mx, old = rows["MX750"], rows["Old"]
    ev = new.evidence
    # as of the proposal 60 days ago: the two open repairs since are not in it, the one 37 months back is
    assert (ev["repairs"], ev["failures"], ev["findings_recorded"], ev["completed"]) == (7, 6, 5, 7)
    assert mx["Evidence: device failures"] == 6 and mx["Evidence: failures per device-year"] == Decimal(str(round(6 / ev["device_years"], 2)))
    assert mx["Evidence: what was found recorded"] == "5 of the 7 completed repairs with a finding recorded"
    assert mx["Device failures since approval"] == 2 and mx["Corrective repairs since approval"] == 2  # the open repairs since
    assert (old["Evidence: device failures"], old["Evidence: failures per device-year"], old["Device failures since approval"]) == (None, None, None)
    assert old["Evidence: what was found recorded"] == "Not recorded then"
    assert old["Evidence: corrective repairs"] == 6 and old["Corrective repairs since approval"] == 1  # repairs with repairs
    assert any("Since approval they are printed only beside evidence that counted them" in n for n in s.notes)


def test_the_binder_sections_take_a_fixed_number_of_queries(ctx, today, monitor):
    history(monitor, today)

    def count(build):
        with CaptureQueriesContext(connection) as q:
            s = build()
            for t in s.tables:
                list(t.rows())
        return len(q)

    small = count(lambda: binder_repairs.build(period(today - 500 * DAY, today), None))
    m1 = Asset.objects.get(tag="M-1")
    for n in range(12):
        repair(m1, today - (60 + n) * DAY, completed=today - (59 + n) * DAY, finding=list(F.values)[n % len(F.values)],
               follow_up_of=a_pm(m1, today - (70 + n) * DAY) if n % 3 == 0 else None)
    assert count(lambda: binder_repairs.build(period(today - 500 * DAY, today), None)) == small <= 4


# --- the program evaluation -----------------------------------------------------------------------------------------------------


def figures(data) -> dict:
    return {i["key"]: i for i in data["indicators"]}


def test_the_evaluations_findings_indicators(ctx, today, monitor):
    S, E = last_year(today)
    m1, m2 = Asset.objects.filter(device_model=monitor).order_by("tag")
    on = lambda n: S + n * DAY  # noqa: E731
    pm = a_pm(m1, on(1))
    for finding, follow in ((F.WEAR, None), (F.USE_ERROR, None), (F.DAMAGE, None), (F.NO_PROBLEM_FOUND, None), (F.PM_PREVENTABLE, None),
                            (F.SERVICE_INDUCED, pm), (F.BATTERY, pm), ("", None), (F.NOT_STATED, None)):
        repair(m1, on(10), completed=on(12), finding=finding, follow_up_of=follow)
    repair(m2, S - 100 * DAY, completed=S - 99 * DAY)  # the 12 months before: completed before Cadence recorded findings
    data = indicators.snapshot(S, E, today=today)
    f = figures(data)

    def shown(key):
        i = f[key]
        return i["num"], i["den"], i["display"], i["detail"]

    assert shown("repairs_finding_recorded") == (7, 9, "7", "7 of 9")
    assert shown("repairs_no_problem_found") == (1, 7, "14.2%", "1 of 7")
    assert shown("repairs_use_related") == (2, 7, "28.5%", "2 of 7")
    assert shown("repairs_pm_related") == (2, 7, "28.5%", "2 of 7")
    assert shown("repairs_found_on_pm") == (2, 7, "28.5%", "2 of 7")
    assert f["repairs_use_related"]["label"] == "Use-related (use error and damage)" and f["repairs_use_related"]["needs"] == ["workorders"]
    assert data["not_recorded"] == list(indicators.NOT_RECORDED) == [
        "Equipment uptime and the cost of service ratio for a year are not counted in this evaluation."]
    keys = [i["key"] for i in data["indicators"]]
    assert keys.index("repairs_no_outcome") + 1 == keys.index("repairs_finding_recorded")
    # the previous 12 months counted from Cadence's records: nothing recorded there
    prev = data["previous"]["values"]
    assert data["previous"]["source"] == "counted"
    assert (prev["repairs_finding_recorded"]["display"], prev["repairs_finding_recorded"]["den"]) == ("0", 1)
    assert prev["repairs_use_related"]["display"] == "Not recorded" and prev["repairs_use_related"]["value"] is None


def test_none_recorded_reads_not_recorded_never_zero(ctx, today, monitor):
    S, E = last_year(today)
    repair(Asset.objects.get(tag="M-1"), S + 5 * DAY, completed=S + 6 * DAY)
    f = figures(indicators.snapshot(S, E, today=today))
    assert f["repairs_finding_recorded"]["display"] == "0" and f["repairs_finding_recorded"]["detail"] == "0 of 1"
    for key in ("repairs_no_problem_found", "repairs_use_related", "repairs_pm_related", "repairs_found_on_pm"):
        assert (f[key]["value"], f[key]["display"], f[key]["met"]) == (None, "Not recorded", None), key


def test_the_aem_table_adds_failures_and_pm_related_with_coverage(ctx, today, dept):
    S, E = last_year(today)
    dm = model("Pulse", aem_interval_months=24)
    devices = [device(dm, dept, f"P-{n}", installed_on=S - 400 * DAY) for n in range(4)]
    AemDecision.objects.create(device_model=dm, interval_months=24, oem_interval_months=12, status=AemStatus.APPROVED, rationale="r",
                               proposed_on=S - 60 * DAY, decided_on=S - 30 * DAY, evidence=OLD_EVIDENCE)
    for n, finding in enumerate((F.WEAR, F.PM_PREVENTABLE, F.USE_ERROR, F.SERVICE_INDUCED, "")):
        repair(devices[n % 4], S + (10 + n) * DAY, completed=S + (11 + n) * DAY, finding=finding)
    row = indicators.snapshot(S, E, today=today)["tables"]["aem"][0]
    years = row["device_years"]
    assert (row["repairs"], row["failures"], row["findings_recorded"]) == (5, 4, 4)
    assert row["failures_per_device_year"] == round(4 / years, 2) and row["coverage"] == "4 of the 5 completed repairs with a finding recorded"
    assert not row["pm_related_enough"] and row["pm_related"] is None and row["pm_related_per_device_year"] is None  # 4 < 5
    assert not row["evidence_failures_recorded"] and row["evidence_failures_per_device_year"] is None  # old evidence: no failures beside it
    assert row["evidence_per_device_year"] == 0.6
    repair(devices[0], S + 20 * DAY, completed=S + 21 * DAY, finding=F.WEAR)
    decision = AemDecision.objects.get(device_model=dm)
    decision.evidence = {**OLD_EVIDENCE, "failures": 5, "findings_recorded": 5, "completed": 6, "pm_related": 1, "found_on_pm": 0,
                         "failure_rule": findings.FAILURE_RULE}
    decision.save()
    row = indicators.snapshot(S, E, today=today)["tables"]["aem"][0]
    assert row["pm_related_enough"] and row["pm_related"] == 2 and row["pm_related_per_device_year"] == round(2 / years, 2)  # 5 of 6
    assert row["evidence_failures_recorded"] and row["evidence_failures_per_device_year"] == 0.5  # 5 over 10 device-years


def test_an_older_approved_snapshot_reads_not_counted_then(ctx, today, client, make_user, monitor):
    """An evaluation approved before slice 33 stored none of the findings indicators nor the AEM table's new keys: the next one's
    previous column says "Not counted then", and its own page and print read the AEM rows the same way."""
    S, _E = last_year(today)
    AemDecision.objects.create(device_model=monitor, interval_months=24, oem_interval_months=12, status=AemStatus.APPROVED, rationale="r",
                               proposed_on=S - 60 * DAY, decided_on=S - 30 * DAY, evidence=OLD_EVIDENCE)
    DeviceModel.objects.filter(pk=monitor.pk).update(aem_interval_months=24)
    start = add_months(S, -12)
    first = approved(start=start, today=today)
    snap = first.snapshot
    new_keys = {"repairs_finding_recorded", "repairs_no_problem_found", "repairs_use_related", "repairs_pm_related", "repairs_found_on_pm"}
    snap.data = {**snap.data, "indicators": [i for i in snap.data["indicators"] if i["key"] not in new_keys],
                 "tables": {"aem": [{k: v for k, v in r.items() if k in ("model", "risk_class", "oem_months", "aem_months", "devices",
                                                                          "device_years", "repairs", "per_device_year", "failed_pms",
                                                                          "approved_on", "evidence_per_device_year")}
                                    for r in snap.data["tables"]["aem"]]}}
    type(snap).objects.filter(pk=snap.pk).update(data=snap.data)
    data = indicators.snapshot(S, add_months(S, 12) - DAY, today=today, previous=first)
    prev = data["previous"]["values"]
    assert data["previous"]["source"] == "approved"
    assert all(prev[k]["display"] == indicators.NOT_COUNTED_THEN and prev[k]["value"] is None for k in new_keys)
    assert prev["repairs_completed"]["display"] != indicators.NOT_COUNTED_THEN
    client.force_login(make_user("director"))
    for url in (f"/reports/evaluations/{first.number}/", f"/print/evaluations/{first.number}/"):
        r = client.get(url)
        assert r.status_code == 200, url
        body = r.content.decode()
        assert body.count("Not counted then") == 3, url  # the AEM row's three findings cells
        assert "Repairs with what was found recorded" not in body  # never stored: never shown


# --- custom reports -----------------------------------------------------------------------------------------------------------


def test_the_work_orders_source_shows_groups_and_filters_by_finding(ctx, today, vent, pump, dept):
    use = repair(vent, today - 10 * DAY, completed=today - 9 * DAY, finding=F.USE_ERROR)
    blank = repair(pump, today - 8 * DAY, completed=today - 7 * DAY)
    worn = repair(pump, today - 6 * DAY, completed=today - 5 * DAY, finding=F.WEAR)
    pm = a_pm(vent, today - 4 * DAY)
    r = custom.run({"source": "work_orders", "columns": ["number", "type", "department", "repair_finding"]}, today)
    shown = {row[0]: row[3] for row in r["rows"]}
    assert shown == {use.number: "Use error", blank.number: "Not recorded", worn.number: "Wore out or failed in use", pm.number: None}
    assert "Repair finding" in r["columns"]
    g = custom.run({"source": "work_orders", "columns": ["repair_finding"], "group_by": "repair_finding"}, today)
    assert [row[:2] for row in g["rows"]] == [["Use error", 1], ["Wore out or failed in use", 1], ["Not recorded", 1], ["—", 1]]
    icu = Department.objects.create(name="Med-Surg")
    Asset.objects.filter(pk=pump.pk).update(department=icu)
    by_unit = custom.run({"source": "work_orders", "columns": ["department"], "group_by": "department",
                          "filters": {"repair_finding": ["use_error"]}}, today)
    assert by_unit["rows"] == [[dept.name, 1]] and "Repair finding: Use error" in by_unit["description"]
    clean = custom.clean_definition("work_orders", ["department", "repair_finding"], {"repair_finding": ["not_recorded", "use_error"]},
                                    "repair_finding")
    assert clean["filters"]["repair_finding"] == ["use_error", "not_recorded"] and clean["group_by"] == "repair_finding"
    with pytest.raises(ValidationError):
        custom.clean_definition("work_orders", ["number"], {"repair_finding": ["user_error"]})
    assert "Alarm fault" not in repr(r["rows"])


# --- under the policies ---------------------------------------------------------------------------------------------------------


@needs_postgres
def test_the_readers_under_row_level_security(ctx, today, monitor, other_tenant):
    history(monitor, today)
    with tenant_context(other_tenant):
        d = Department.objects.create(name="Their ICU")
        theirs = DeviceModel.objects.create(manufacturer="Acme", model="MX750", description="X", category="Monitors", oem_pm_interval_months=12)
        their = device(theirs, d, "THEIRS-1", installed_on=add_months(today, -48))
        repair(their, today - 30 * DAY, completed=today - 29 * DAY, finding=F.PM_PREVENTABLE)
    expected = aem.evidence(monitor, today)
    as_app_role()
    with tenant_context(ctx):
        ev = aem.evidence(monitor, today)
        assert ev == expected and (ev["failures"], ev["findings_recorded"], ev["pm_related"]) == (7, 4, 2)
        assert {r[0] for r in rows_of(binder_repairs.build(period(today - 500 * DAY, today), None), "repairs")} == set(
            WorkOrder.objects.filter(asset__device_model=monitor, status=WoStatus.COMPLETED, completed_on__gte=today - 500 * DAY)
            .values_list("number", flat=True))
        r = custom.run({"source": "work_orders", "columns": ["repair_finding"], "group_by": "repair_finding"}, today)
        assert sum(row[1] for row in r["rows"]) == WorkOrder.objects.count()
        assert report_mtbf(today)["total_models"] == 1
