"""
Slice 27, the PM completion window in the survey binder (apps/reports/survey/maintenance.py, program.py, aem.py).

Maintenance: by class, the PMs counted, on time, on time but done after the due date, not on time, and still inside their window today;
the life-support and high-risk PMs on time only because of the window (marking the models CMS keeps on the manufacturer's schedule);
"Window ended" beside each PM not on time (Days late from the due date); every life-support or high-risk device past its due date
listed, a GAP only past its window; the open PMs past their window never dropped (the negation trap); the totals agree with
pm_on_time_rate, pm_pending, missed_pms, the custom report, and the series for the same window; a fixed number of queries. The default
window keeps every table's columns (the slice 25 tests stand unchanged).

Program: each group's window and when it last changed (and who), every change in a table, a CHECK for a change made since the period
started, the PM policy lines read against their group's window, another facility's history never read, a fixed number of queries.
AEM: the evidence's PM window printed as recorded.
"""
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest
from csvutil import csv_rows
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from pg_helpers import as_app_role, needs_postgres
from survey_helpers import gaps_of, period, rows_of

from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel, RiskClass
from apps.facility import services as fac
from apps.facility.models import FacilitySettings, PmWindow
from apps.pm import windows as W
from apps.pm.models import AemDecision, AemStatus
from apps.pm.services import missed_pms, pm_due_queryset, pm_on_time_rate, pm_on_time_series, pm_pending
from apps.reports import custom
from apps.reports.survey import CHECK, FINDING, GAP, aem, maintenance, program
from apps.tenants.context import tenant_context
from apps.workorders.models import LateReason, Priority, WorkOrder, WoStatus, WoType
from apps.workorders.services import create_work_order, set_late_reason

K = PmWindow
TODAY = date(2026, 10, 20)
START = date(2026, 7, 1)
MIXED = W.Windows(W.Window(K.DUE_MONTH), W.Window(K.DAYS_AFTER, 14))


def set_window(w: W.Windows, by=None):
    return fac.update_settings(by=by, pm_window_high=w.high.kind, pm_window_high_days=w.high.days, pm_window_other=w.other.kind,
                               pm_window_other_days=w.other.days)


def aware(day, hour=12):
    return timezone.make_aware(datetime.combine(day, time(hour)))


def d(month, day):
    return date(2026, month, day)


def pm(asset, due, done=None, status=None):
    wo = create_work_order(asset=asset, type=WoType.PM, priority=Priority.NORMAL, problem="PM", opened_on=due - timedelta(days=20), due_on=due)
    fields = {}
    if done:
        fields.update(status=WoStatus.COMPLETED, completed_on=done)
    if status:
        fields["status"] = status
    if fields:
        WorkOrder.objects.filter(pk=wo.pk).update(**fields)
        wo.refresh_from_db()
    return wo


def build(today=TODAY, start=START):
    return maintenance.build(period(start, today, today), None)


def day(x: date) -> str:
    return f"{x:%b} {x.day}, {x.year}"


@pytest.fixture
def win(ctx, dept, vent_model, pump_model):
    """Oct 20, the period Jul 1 to Oct 20; life support and high risk by the end of the due month, medium and low within 14 days.

    Life support V-1: a done Sep 25 (due Sep 10: on time by the window), b done Sep 2 (due Aug 5: past Aug 31), x cancelled (due Aug 20),
    c open (due Oct 5: inside), d open (due Sep 20: past Sep 30). High C-1, a C-arm CMS keeps on the manufacturer's schedule: e done Oct 12
    (due Oct 2), f done on its due date. Medium M-1: g 13 days after, h 15 days after, i open inside (due Oct 10), j open past (due Sep 25).
    Low L-1: k exactly 14 days after. Devices: V-1 past its window, V-2 past its due date inside it, P-2 missing, M-1 inside, L-1 past."""
    monitor = DeviceModel.objects.create(manufacturer="Philips", model="MX450", description="Patient monitor", category="Monitors",
                                         risk_class=RiskClass.MEDIUM, oem_pm_interval_months=12)
    thermo = DeviceModel.objects.create(manufacturer="Welch Allyn", model="SureTemp", description="Thermometer", category="Thermometers",
                                        risk_class=RiskClass.LOW, oem_pm_interval_months=12)
    carm = DeviceModel.objects.create(manufacturer="GE", model="OEC 9900", description="C-arm", category="Imaging", risk_class=RiskClass.HIGH,
                                      oem_pm_interval_months=6, oem_schedule_required=True)

    def device(tag, model, next_pm, **extra):
        a = Asset.objects.create(tag=tag, device_model=model, department=dept, **extra)
        Asset.history.filter(id=a.pk).update(history_date=aware(d(1, 5)))  # in use all year (the retirement rule reads history)
        return a, next_pm

    v1, v2, c1, p2, m1, l1 = (device("V-1", vent_model, d(9, 20)), device("V-2", vent_model, d(10, 5)), device("C-1", carm, d(11, 1)),
                              device("P-2", pump_model, d(12, 1), status=AssetStatus.MISSING), device("M-1", monitor, d(10, 10)),
                              device("L-1", thermo, d(10, 1)))
    w = {"a": pm(v1[0], d(9, 10), done=d(9, 25)), "b": pm(v1[0], d(8, 5), done=d(9, 2)), "x": pm(v1[0], d(8, 20), status=WoStatus.CANCELLED),
         "c": pm(v1[0], d(10, 5)), "d": pm(v1[0], d(9, 20)), "e": pm(c1[0], d(10, 2), done=d(10, 12)), "f": pm(c1[0], d(8, 15), done=d(8, 15)),
         "g": pm(m1[0], d(9, 1), done=d(9, 14)), "h": pm(m1[0], d(9, 1), done=d(9, 16)), "i": pm(m1[0], d(10, 10)), "j": pm(m1[0], d(9, 25)),
         "k": pm(l1[0], d(10, 1), done=d(10, 15))}
    for a, next_pm in (v1, v2, c1, p2, m1, l1):
        Asset.objects.filter(pk=a.pk).update(next_pm_on=next_pm)  # pinned after the PMs exist
    set_window(MIXED)
    return w


def by_class(section) -> dict:
    return {row[0]: row[1:] for row in rows_of(section, "by_class")}


def figures(section) -> dict:
    return {f.label: f for f in section.figures}


def test_the_figures_by_class_follow_the_window(win):
    s = build()
    assert s.table("by_class").columns == ["Risk class", "PMs due", "On time", "On time, done after the due date", "Not on time",
                                           "Still inside their window today", "On time %", "Target %", "Target met",
                                           "Devices past their PM window today"]
    assert by_class(s) == {
        # due, on time, on time after the due date, not on time, still inside, on time %, target %, met, devices past their window
        "Life support": [4, 1, 1, 3, 1, Decimal("25.0"), Decimal("100.0"), "No", 1],  # a; b, x, d; c inside
        "High": [2, 2, 1, 0, 0, Decimal("100.0"), Decimal("100.0"), "Yes", 1],  # e (after its due date), f; P-2 missing
        "Medium": [3, 1, 1, 2, 1, Decimal("33.3"), Decimal("95.0"), "No", 0],  # g; h, j; i inside; M-1 inside its window
        "Low": [1, 1, 1, 0, 0, Decimal("100.0"), Decimal("95.0"), "Yes", 1],  # k on its 14th day; L-1 past
    }
    f = figures(s)
    assert (f["PMs due in the period"].value, f["Completed on time"].value) == (10, "50%")
    assert f["On time, done after the due date"].value == 4 and f["Still inside their window today"].value == 2
    assert f["Devices past their PM window today"].value == 3
    assert f["Devices past their PM window today"].hint.endswith("; 2 more past their PM date, still inside their window")
    assert "Devices past their PM date today" not in f


def test_the_totals_agree_with_the_services_the_custom_report_and_the_series(win):
    s = build()
    rate, pending = pm_on_time_rate(START, TODAY, TODAY, w=MIXED), pm_pending(START, TODAY, TODAY, w=MIXED)
    rows = by_class(s)
    assert (sum(r[0] for r in rows.values()), sum(r[1] for r in rows.values()), sum(r[4] for r in rows.values())) == (rate["due"], rate["on_time"], pending)
    missed = pm_due_queryset(START, TODAY, TODAY, w=MIXED).filter(pk__in=missed_pms(TODAY, w=MIXED))
    assert {r[0] for r in rows_of(s, "not_on_time")} == set(missed.values_list("number", flat=True)) == {win[k].number for k in "bxdhj"}
    r = custom.run(custom.clean_definition("work_orders", ["pm_on_time"], {"type": ["pm"], "date": {"field": "due", "period": "custom",
                   "from": START.isoformat(), "to": TODAY.isoformat()}}, "type"), TODAY)
    assert r["rows"][0][2] == pytest.approx(rate["on_time"] * 100 / rate["due"])
    series = pm_on_time_series(2026, 10, months=4, today=TODAY, w=MIXED)
    assert (sum(p["due"] for p in series), sum(p["on_time"] for p in series)) == (rate["due"], rate["on_time"])


def test_pms_not_on_time_show_when_their_window_ended(win):
    s = build()
    t = s.table("not_on_time")
    assert t.columns[4:8] == ["Due", "Window ended", "Completed on", "Days late"] and t.links == {0: "work_order", 1: "device", 9: "work_order"}
    rows = {r[0]: r for r in rows_of(s, "not_on_time")}
    assert [r[0] for r in rows_of(s, "not_on_time")] == [win[k].number for k in "bxhdj"]  # by due date
    assert rows[win["b"].number][4:8] == [d(8, 5), d(8, 31), d(9, 2), 28]  # Days late stays from the due date
    assert rows[win["x"].number][4:8] == [d(8, 20), d(8, 31), "Cancelled", 61]
    assert rows[win["d"].number][4:8] == [d(9, 20), d(9, 30), "Open", 30]  # open past its window: never dropped
    assert rows[win["h"].number][4:8] == [d(9, 1), d(9, 15), d(9, 16), 15]
    assert rows[win["j"].number][4:8] == [d(9, 25), d(10, 9), "Open", 25]
    assert len(rows[win["b"].number]) == len(t.columns) == 12


def test_life_support_and_high_risk_pms_on_time_only_because_of_the_window(win):
    s = build()
    t = s.table("on_time_by_window")
    assert t.count == 2 and t.links == {0: "work_order", 1: "device"}
    assert rows_of(s, "on_time_by_window") == [
        [win["a"].number, "V-1", "Hamilton Medical Hamilton-G5", "Life support", d(9, 10), d(9, 30), d(9, 25), 15, ""],
        [win["e"].number, "C-1", "GE OEC 9900", "High", d(10, 2), d(10, 31), d(10, 12), 10, "Yes"],  # the CMS mark
    ]  # medium and low ones done after their due date are counted, not listed


def test_devices_past_their_due_date_are_listed_and_only_those_past_their_window_are_gaps(win):
    s = build()
    assert s.table("past_pm_date").columns == ["Device", "Model", "Risk class", "Status", "Next PM", "Days past", "PM window", "Open PM work order"]
    assert rows_of(s, "past_pm_date") == [
        ["V-1", "Hamilton Medical Hamilton-G5", "Life support", "In service", d(9, 20), 30, "Ended Sep 30, 2026", win["d"].number],
        ["V-2", "Hamilton Medical Hamilton-G5", "Life support", "In service", d(10, 5), 15, "On time until Oct 31, 2026", ""],
        ["P-2", "BD Alaris 8015 PCU", "High", "Missing", d(12, 1), None, None, ""],
    ]
    gaps = [(g.text, g.record) for g in gaps_of(s, GAP)]
    b, x, dd = win["b"].number, win["x"].number, win["d"].number
    assert gaps == [
        (f"{b} on V-1 was done 28 days after its due date, after its window ended (Aug 31, 2026), with no reason recorded", b),
        (f"{x} on V-1 was cancelled without being done (due Aug 20, 2026, window ended Aug 31, 2026) with no reason recorded", x),
        (f"{dd} on V-1 is 30 days past its due date and its window ended Sep 30, 2026, with no reason recorded", dd),
        (f"V-1 (life support) is 30 days past its PM date (Sep 20, 2026) and its window ended Sep 30, 2026; {dd} is open", "V-1"),
        ("P-2 (high risk) is marked missing; a missing device counts as past its PM date until it is found", "P-2"),
    ]  # V-2 is past its due date but inside its window: listed, never a gap
    assert [g.text for g in gaps_of(s, FINDING)] == ["Medium risk: 33.3% on time against the facility's 95% target"]
    set_late_reason(win["d"], LateReason.STAFFING, today=TODAY)
    assert dd not in " ".join(g.text for g in gaps_of(build(), GAP) if g.record != "V-1")


def test_the_notes_say_the_window(win):
    notes = build().notes
    assert notes[0] == ("On time: a PM is on time when completed within the facility's PM window, as on the Overview: by the end of the due "
                        "month for life support and high risk, within 14 days after the due date for medium and low risk (Settings). Each "
                        "PM is judged by today's window, so a change to the window recounts past months; the Program and policies section "
                        "says when it last changed.")
    assert not any("No grace period is added" in n for n in notes)
    assert any(n.startswith("Days late: from the PM's due date, not its window") for n in notes)


def test_the_default_window_keeps_the_tables_as_they_were(win):
    set_window(W.DEFAULT)
    s = build()
    assert [t.key for t in s.tables] == ["by_class", "not_on_time", "due_moved", "failed", "past_pm_date"]
    assert s.table("by_class").columns == ["Risk class", "PMs due", "On time", "Not on time", "On time %", "Target %", "Target met",
                                           "Devices past their PM date today"]
    assert "Window ended" not in s.table("not_on_time").columns and "PM window" not in s.table("past_pm_date").columns
    assert s.notes[0] == "On time: a PM is on time when completed on or before its due date, as on the Overview. No grace period is added."
    assert [r[0] for r in rows_of(s, "past_pm_date")] == ["V-1", "V-2", "P-2"] and len(gaps_of(s, GAP)) == 6 + 3  # a b x d c e; V-1 V-2 P-2


def test_only_the_medium_and_low_window_set(win):
    """Life support and high risk by the due date, medium and low within 14 days: the window table is there, empty, and V-2 is a gap."""
    set_window(W.Windows(W.Window(), W.Window(K.DAYS_AFTER, 14)))
    s = build()
    assert s.table("on_time_by_window").count == 0 and rows_of(s, "on_time_by_window") == []
    assert by_class(s)["Low"][:3] == [1, 1, 1] and "V-2" in [g.record for g in gaps_of(s, GAP)]


def test_the_page_its_csvs_and_the_print_show_the_window(win, client, make_user, monkeypatch):
    monkeypatch.setattr("apps.web.views_survey._today", lambda: TODAY)
    client.force_login(make_user("director"))
    query = f"from={START.isoformat()}&to={TODAY.isoformat()}"
    body = client.get(f"/reports/survey/?{query}").content.decode()
    assert "Life-support and high-risk PMs on time only because of the window" in body and "Still inside their window today" in body
    rows = csv_rows(client.get(f"/reports/survey/maintenance/on_time_by_window.csv?{query}"))
    assert rows[0][5] == "Window ends" and [r[0] for r in rows[1:]] == [win["a"].number, win["e"].number] and rows[2][8] == "Yes"
    late = csv_rows(client.get(f"/reports/survey/maintenance/not_on_time.csv?{query}"))
    assert late[0][4:6] == ["Due", "Window ended"] and late[1][4:6] == ["2026-08-05", "2026-08-31"]
    printed = client.get(f"/print/survey/?{query}").content.decode()
    assert "On time until Oct 31, 2026" in printed and "PM on time, life support and high risk" in printed


def test_a_fixed_number_of_queries(win, dept, vent_model):
    def queries():
        with CaptureQueriesContext(connection) as q:
            s = build()
            for t in s.tables:
                list(t.rows())
        return len(q.captured_queries)

    few = queries()
    for i in range(12):
        a = Asset.objects.create(tag=f"Q-{i}", device_model=vent_model, department=dept)
        pm(a, d(9, 8), done=d(9, 20))
        pm(a, d(8, 3), done=d(9, 3))
        pm(a, d(10, 3))
        Asset.objects.filter(pk=a.pk).update(next_pm_on=d(10, 2))
    assert queries() == few


# --- program -------------------------------------------------------------------------------------------------------------------


def program_build(today, start):
    return program.build(period(start, today, today), None)


def test_the_program_shows_each_window_and_when_it_last_changed(ctx, make_user):
    kim = make_user("director")
    today = timezone.localdate()
    s = program_build(today, today - timedelta(days=90))
    f = figures(s)
    assert (f["PM on time, life support and high risk"].value, f["PM on time, life support and high risk"].hint) == (
        "By the due date", "Cadence's default, never changed")
    assert rows_of(s, "pm_window_changes") == [] and not gaps_of(s, CHECK)
    assert program.ON_TIME_NOTE in s.notes
    fac.update_settings(by=kim, pm_window_high=K.DUE_MONTH)
    fac.update_settings(by=kim, portal_hotline="ext. 4400")  # a save that leaves the window alone is not a change
    s = program_build(today, today - timedelta(days=90))
    f = figures(s)
    assert (f["PM on time, life support and high risk"].value, f["PM on time, life support and high risk"].hint) == (
        "By the end of the due month", f"Changed {day(today)} by Director User")
    assert f["PM on time, medium and low risk"].value == "By the due date"
    assert rows_of(s, "pm_window_changes") == [[today, "Life support and high risk", "By the due date", "By the end of the due month", "Director User"]]
    checks = gaps_of(s, CHECK)
    assert [(g.text, g.url, g.record) for g in checks] == [(
        f"The PM window for life support and high risk was changed on {day(today)} by Director User, from “By the due date” to “By the end "
        "of the due month”: every PM in the period is judged by today's window. Be ready to explain the change.", "/settings/", "Settings")]
    assert s.notes[0] == ("How on time is measured: a PM is on time when completed within the facility's PM window, as on the Overview: by "
                          "the end of the due month for life support and high risk, by the due date for medium and low risk. Each PM is judged "
                          "by today's window and its model's class today, so a change to the window recounts past months.")


def test_a_change_before_the_period_is_listed_but_not_a_check(ctx, make_user):
    kim = make_user("director")
    today = timezone.localdate()
    fac.update_settings(by=kim, pm_window_other=K.DAYS_AFTER, pm_window_other_days=10)
    FacilitySettings.history.update(history_date=aware(today - timedelta(days=200)))
    s = program_build(today, today - timedelta(days=90))
    assert not gaps_of(s, CHECK)
    assert rows_of(s, "pm_window_changes") == [[today - timedelta(days=200), "Medium and low risk", "By the due date",
                                                "Within 10 days after the due date", "Director User"]]
    assert figures(s)["PM on time, medium and low risk"].hint == f"Changed {day(today - timedelta(days=200))} by Director User"
    assert program_build(today, today - timedelta(days=300)).gaps != []  # a period that started before it: the CHECK


def test_the_pm_policy_lines_are_read_against_their_groups_window(ctx):
    today = timezone.localdate()
    long_ago = today - timedelta(days=400)
    fac.update_settings(pm_window_high=K.DUE_MONTH)  # the default line follows the window: no CHECK
    FacilitySettings.history.update(history_date=aware(long_ago))
    assert not gaps_of(program_build(today, today - timedelta(days=90)), CHECK)
    fac.update_settings(policy_life_support="OEM interval, complete by the due date, no grace")
    FacilitySettings.history.update(history_date=aware(long_ago))
    checks = gaps_of(program_build(today, today - timedelta(days=90)), CHECK)
    assert [g.text for g in checks] == [
        "Your life support and high risk policy text says “OEM interval, complete by the due date, no grace”; Cadence counts this group's "
        "PMs on time when done by the end of the due month (its PM window). Change the text or the window in Settings, or be ready to "
        "explain the difference."]
    # By the due date the binder's own rule still reads a denied grace period carefully (the default's CHECK is unchanged).
    fac.update_settings(policy_medium_low="No grace, but a grace period for loaners")
    FacilitySettings.history.update(history_date=aware(long_ago))
    texts = [g.text for g in gaps_of(program_build(today, today - timedelta(days=90)), CHECK)]
    assert texts[1] == ("Your medium and low risk policy text says “No grace, but a grace period for loaners”; Cadence counts a PM on time "
                        "only when done by its due date. Change the text in Settings, or be ready to explain the difference.")
    assert program.disagrees("No grace, but a grace period for loaners", W.Window())


def test_another_facilitys_window_history_is_never_read(ctx, other_tenant):
    today = timezone.localdate()
    with tenant_context(other_tenant):
        fac.update_settings(pm_window_high=K.NEXT_MONTH, pm_window_other=K.DAYS_AFTER, pm_window_other_days=30)
    s = program_build(today, today - timedelta(days=90))
    assert rows_of(s, "pm_window_changes") == [] and not s.gaps
    assert figures(s)["PM on time, medium and low risk"].value == "By the due date"


def test_the_program_makes_a_fixed_number_of_queries(ctx, make_user):
    kim = make_user("director")
    today = timezone.localdate()

    def queries():
        with CaptureQueriesContext(connection) as q:
            s = program_build(today, today - timedelta(days=90))
            for t in s.tables:
                list(t.rows())
        return len(q.captured_queries)

    fac.update_settings(by=kim, pm_window_high=K.DUE_MONTH)
    few = queries()
    for days in (5, 10, 20, 30, 45):
        fac.update_settings(by=kim, pm_window_other=K.DAYS_AFTER, pm_window_other_days=days)
    assert queries() == few <= 3
    assert len(rows_of(program_build(today, today - timedelta(days=90)), "pm_window_changes")) == 6


@needs_postgres
def test_the_window_history_is_read_under_the_policies(ctx, tenant, other_tenant, make_user):
    kim = make_user("director")
    today = timezone.localdate()
    fac.update_settings(by=kim, pm_window_high=K.DUE_MONTH)
    with tenant_context(other_tenant):
        fac.update_settings(pm_window_other=K.NEXT_MONTH)
    as_app_role()
    with tenant_context(tenant):
        s = program_build(today, today - timedelta(days=90))
        assert [r[1] for r in rows_of(s, "pm_window_changes")] == ["Life support and high risk"]
        assert figures(s)["PM on time, life support and high risk"].hint == f"Changed {day(today)} by Director User"


# --- AEM ----------------------------------------------------------------------------------------------------------------------


def test_the_aem_evidence_prints_its_pm_window_as_recorded(ctx, make_user):
    today = timezone.localdate()
    people = {"tech": make_user("technician"), "manager": make_user("manager")}
    dept = Department.objects.create(name="Imaging")
    for name, ev in (("With", {"as_of": (today - timedelta(days=40)).isoformat(), "pm_completed": 6, "pm_on_time": 5,
                               "pm_window": "By the end of the due month"}),
                     ("Before", {"as_of": (today - timedelta(days=40)).isoformat(), "pm_completed": 6, "pm_on_time": 6})):
        dm = DeviceModel.objects.create(manufacturer="Acme", model=name, description="Monitor", category="Monitors", risk_class=RiskClass.MEDIUM,
                                        oem_pm_interval_months=12, aem_interval_months=24)
        Asset.objects.create(tag=f"A-{name}", device_model=dm, department=dept)
        AemDecision.objects.create(device_model=dm, interval_months=24, oem_interval_months=12, status=AemStatus.APPROVED, rationale="r",
                                   evidence=ev, proposed_by=people["tech"], proposed_on=today - timedelta(days=40),
                                   decided_by=people["manager"], decided_on=today - timedelta(days=20), decision_note="EMC")
    s = aem.build(period(today - timedelta(days=90), today), people["manager"])
    column = s.table("in_force").columns.index("Evidence: PM window")
    assert column == s.table("in_force").columns.index("Evidence: PMs on time") + 1
    assert {r[1]: (r[column - 1], r[column]) for r in rows_of(s, "in_force")} == {"Before": (6, None), "With": (5, "By the end of the due month")}
    assert any("blank for evidence recorded before Cadence kept one" in n for n in s.notes)
