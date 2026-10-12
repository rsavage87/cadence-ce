"""
The survey binder's "Program and policies" section (slice 25, part 1; apps/reports/survey/program.py): the facility, when its Settings
last changed, the PM targets by risk class and the other KPI targets, the policy lines as set in Cadence (default or not), the
risk-scoring bands with today's active devices, the note on what on time means, and the CHECK on a PM policy line that speaks of a
month or a grace period. Also the two default PM policy texts, which now say what Cadence measures.

Slice 32: the program's annual evaluations approved whose 12 months overlap the binder's period (never a draft, one sent to committee,
a discarded one, or a superseded version), with the committees that approved each; the CHECK once the facility has approved one and
the next is overdue (never before a first approval), linked for a reader with Program evaluation View; NOT_COVERED and the note on the
written plan; a fixed number of queries.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from evaluation_fixtures import approved, draft, sent
from survey_helpers import gaps_of, period, rows_of

from apps.accounts.models import Level, Module, Role, User
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel, RiskClass
from apps.evaluations import services as evaluations
from apps.evaluations.models import Body
from apps.facility import services as fs
from apps.facility.models import POLICY_DEFAULTS, FacilitySettings
from apps.pm.dates import add_months
from apps.reports import survey
from apps.reports.survey import CHECK, EVALUATION, NOT_COVERED, program

OLD_LIFE_SUPPORT = "OEM interval, complete within due month, no grace"
OLD_MEDIUM_LOW = "AEM allowed, 30-day grace after due month"


@pytest.fixture
def today(ctx):
    return timezone.localdate()


def build(today):
    return program.build(period(today - timedelta(days=90), today), None)


def figures(section) -> dict:
    return {f.label: f.value for f in section.figures}


def test_the_default_pm_policy_texts_say_what_cadence_measures(ctx):
    assert POLICY_DEFAULTS["policy_life_support"] == "OEM interval, complete by the due date, no grace"
    assert POLICY_DEFAULTS["policy_medium_low"] == "AEM allowed, complete by the due date"
    s = FacilitySettings()
    assert (s.policy_life_support, s.policy_medium_low) == (POLICY_DEFAULTS["policy_life_support"], POLICY_DEFAULTS["policy_medium_low"])
    assert not program.disagrees_with_due_date(s.policy_life_support) and not program.disagrees_with_due_date(s.policy_medium_low)


@pytest.mark.parametrize("text, flagged", [
    (OLD_LIFE_SUPPORT, True), (OLD_MEDIUM_LOW, True), ("Complete within the MONTH it is due", True), ("15-day Grace period", True),
    ("Complete by the due date, no grace period", False), ("Complete by the due date; without a grace period", False),
    ("By the due date, zero grace", False), ("No grace, but a grace period for loaners", True), ("Done by the due date", False),
])
def test_a_pm_policy_line_disagrees_when_it_speaks_of_a_month_or_a_grace_period(text, flagged):
    assert program.disagrees_with_due_date(text) is flagged


def test_a_facility_that_never_saved_shows_cadences_defaults(ctx, today):
    s = build(today)
    f = figures(s)
    assert s.key == "program" and s.title == "Program and policies" and s.covers == f"as of today, {today:%b} {today.day}, {today.year}"
    assert f["Facility"] == "Riverside Regional" and f["Settings last changed in Cadence"] == "Never changed: Cadence's defaults"
    assert (f["PM completion target, life support"], f["PM completion target, high risk"], f["PM completion target, medium risk"],
            f["PM completion target, low risk"]) == ("100%", "100%", "95%", "95%")
    assert f["Fleet uptime target"] == "99.5%" and f["Mean time to repair target"] == "3 days"
    policy = rows_of(s, "policy")
    assert len(policy) == 8 and all(row[2] == "Yes" for row in policy)
    assert policy[0] == ["Life support and high risk", "OEM interval, complete by the due date, no grace", "Yes"]
    assert s.gaps == []
    assert program.ON_TIME_NOTE in s.notes
    assert "management plan" not in s.table("policy").title.lower() and "Cadence" in s.table("policy").title  # never reads as the plan
    assert any("not the facility's written medical equipment management plan" in n for n in s.notes)


def test_changed_settings_show_their_day_targets_and_non_default_lines(ctx, today):
    fs.update_settings(target_pm_pct=Decimal("97.5"), target_mttr_days=Decimal("1.0"), policy_portal="Triage within 15 minutes")
    s = build(today)
    f = figures(s)
    assert f["Settings last changed in Cadence"] == today
    assert f["PM completion target, medium risk"] == "97.5%" and f["PM completion target, low risk"] == "97.5%"
    assert f["PM completion target, life support"] == "100%" and f["Mean time to repair target"] == "1 day"
    policy = {row[0]: row[1:] for row in rows_of(s, "policy")}
    assert policy["Portal requests"] == ["Triage within 15 minutes", "No"] and policy["Medium and low risk"][1] == "Yes"
    assert s.gaps == []  # only the two PM lines are read, and they are the defaults


def test_a_pm_policy_line_with_a_month_or_a_grace_period_is_a_check(ctx, today):
    """A facility that saved before slice 25 keeps the old texts, which speak of the due month and a grace period."""
    fs.update_settings(policy_life_support=OLD_LIFE_SUPPORT, policy_medium_low=OLD_MEDIUM_LOW, policy_missing="Report monthly")
    s = build(today)
    checks = gaps_of(s, CHECK)
    assert len(checks) == 2 and s.gaps == checks  # the missing-devices line is not a PM line
    assert checks[0].text == (f"Your life support and high risk policy text says “{OLD_LIFE_SUPPORT}”; Cadence counts a PM on time only "
                              "when done by its due date. Change the text in Settings, or be ready to explain the difference.")
    assert checks[1].text.startswith(f"Your medium and low risk policy text says “{OLD_MEDIUM_LOW}”")
    assert all(g.url == "/settings/" and g.record == "Settings" for g in checks)
    assert {row[0]: row[2] for row in rows_of(s, "policy")}["Life support and high risk"] == "No"
    fs.reset_policy()
    assert build(today).gaps == []


def test_risk_bands_count_todays_active_devices_by_class(ctx, today, dept, vent_model, pump_model):
    monitor = DeviceModel.objects.create(manufacturer="Philips", model="MX450", description="Monitor", category="Monitors", risk_class=RiskClass.MEDIUM)
    for tag, model, status in (("V-1", vent_model, AssetStatus.IN_SERVICE), ("V-2", vent_model, AssetStatus.MISSING),
                               ("V-3", vent_model, AssetStatus.RETIRED), ("P-1", pump_model, AssetStatus.IN_REPAIR), ("M-1", monitor, AssetStatus.ON_LOAN)):
        Asset.objects.create(tag=tag, device_model=model, department=dept, status=status)
    assert rows_of(build(today), "risk_bands") == [["16 and above", "Life support", 2], ["12 to 15", "High", 1], ["9 to 11", "Medium", 1],
                                                   ["8 and below", "Low", 0]]


def test_another_facility_is_never_read(ctx, today, tenant, other_tenant, dept, vent_model):
    from apps.tenants.context import tenant_context

    Asset.objects.create(tag="V-1", device_model=vent_model, department=dept)
    with tenant_context(other_tenant):
        fs.update_settings(policy_life_support=OLD_LIFE_SUPPORT)
        theirs = DeviceModel.objects.create(manufacturer="Zoll", model="R", description="Defib", category="Defibrillators", risk_class=RiskClass.LIFE_SUPPORT)
        Asset.objects.create(tag="Z-1", device_model=theirs, department=Department.objects.create(name="ED"))
    s = build(today)
    assert s.gaps == [] and figures(s)["Settings last changed in Cadence"] == "Never changed: Cadence's defaults"
    assert rows_of(s, "risk_bands")[0][2] == 1


def test_a_fixed_number_of_queries(ctx, today, dept, vent_model):
    first = months_back(today, 12)
    since = add_months(first, -24)  # slice 32: a three-year binder, reaching three evaluations

    def queries():
        with CaptureQueriesContext(connection) as q:
            s = build_over(today, since)
            for t in s.tables:
                list(t.rows())
        return len(q.captured_queries)

    approved(start=add_months(first, -12), today=today)  # slice 32: one approved evaluation with one approval
    few = queries()
    for i in range(40):
        Asset.objects.create(tag=f"V-{i}", device_model=vent_model, department=dept)
    fs.update_settings(policy_medium_low=OLD_MEDIUM_LOW)
    for start in (first, since):  # two more, each with a later committee's approval too
        ev = approved(start=start, today=today)
        evaluations.record_approval(ev, body=Body.GOVERNING, meeting_on=today, by=None, today=today)
    assert len(rows_of(build_over(today, since), "evaluations")) == 3
    # the settings row, its history, the devices by class, the approved evaluations and their approvals, and the last approved
    assert queries() == few <= 6


# --- slice 32: the program's annual evaluations -------------------------------------------------------------------------------

def build_over(today, start):
    """The section for a period from `start` to today."""
    return program.build(period(start, today, today), None)


def months_back(today, n: int) -> date:
    """The first of the month `n` months before this one."""
    return add_months(today.replace(day=1), -n)


def test_a_facility_that_never_approved_one_has_no_item(ctx, today):
    s = build_over(today, months_back(today, 36))
    assert s.gaps == [] and rows_of(s, "evaluations") == []
    assert s.table("evaluations").empty == "No approved program evaluation covers any of this period."
    draft(start=months_back(today, 36), today=today)  # a draft, then one sent to committee: still nothing approved
    sent(start=months_back(today, 24), today=today)
    s = build_over(today, months_back(today, 36))
    assert s.gaps == [] and rows_of(s, "evaluations") == []


def test_the_table_lists_the_approved_evaluations_overlapping_the_period(ctx, today):
    old = approved(start=months_back(today, 36), today=today)  # ended two years ago: outside a 13-month binder
    last = approved(start=months_back(today, 12), today=today, meeting_on=today - timedelta(days=3))
    evaluations.record_approval(last, body=Body.OTHER, body_name="Board quality committee", meeting_on=today, by=None, today=today)
    evaluations.record_approval(last, body=Body.GOVERNING, meeting_on=today, by=None, today=today)
    middle = approved(start=months_back(today, 24), today=today)
    amendment = evaluations.amend(middle, by=None, today=today)  # approved below: the earlier version is superseded
    sent(ev=amendment, today=today)
    evaluations.record_approval(amendment, body=Body.QUALITY, meeting_on=today, by=None, today=today)
    discarded = draft(start=months_back(today, 48), today=today)
    evaluations.discard(discarded, by=None)
    s = build_over(today, months_back(today, 13))
    table = s.table("evaluations")
    assert table.columns == ["Evaluation", "Version", "Period from", "Period to", "Approved on", "Approved by"] and table.links == {0: EVALUATION}
    d = program._day
    assert rows_of(s, "evaluations") == [
        [middle.number, 2, middle.period_start, middle.period_end, today, f"Quality oversight (QAPI), {d(today)}"],
        [last.number, 1, last.period_start, last.period_end, today - timedelta(days=3),
         f"Environment of care or safety committee, {d(today - timedelta(days=3))}; Board quality committee, {d(today)}; Governing body, {d(today)}"],
    ]
    assert table.count == 2 and old.number not in {r[0] for r in rows_of(s, "evaluations")}
    assert [r[0] for r in rows_of(build_over(today, months_back(today, 48)), "evaluations")] == [old.number, middle.number, last.number]
    assert s.gaps == []  # the last one is not overdue


def test_never_the_text_or_the_figures(ctx, today):
    ev = draft(start=months_back(today, 12), today=today)
    evaluations.update_text(ev, "summary", "Words for the committee only", by=None)
    approved(ev=ev, today=today)
    cells = [str(c) for r in rows_of(build_over(today, months_back(today, 12)), "evaluations") for c in r]
    assert len(cells) == 6 and not any("Words for the committee" in c for c in cells)


def test_the_check_once_the_next_is_overdue(ctx, today):
    last = approved(start=months_back(today, 36), today=today)  # the 12 months after it ended about a year ago
    ended = evaluations.period_end_of(last.period_end + timedelta(days=1))
    [check] = gaps_of(build_over(today, months_back(today, 12)), CHECK)
    assert check.text == (f"No approved program evaluation of the 12 months after {evaluations.period_label(last.period_start, last.period_end)} "
                          f"({last.number}, the last approved), 90 days after they ended on {program._day(ended)}. Record its approval in "
                          "Program evaluation, or be ready to explain.")
    assert (check.url, check.record) == ("/reports/evaluations/", "Program evaluation")
    assert not any(word in check.text for word in ("Joint Commission", "DNV", "ACHC", "CMS", "require"))
    following = draft(start=months_back(today, 24), today=today)  # drafting the next is not approving it
    assert len(gaps_of(build_over(today, months_back(today, 12)), CHECK)) == 1
    approved(ev=following, today=today)  # the next approved: the one after it is not due yet
    assert gaps_of(build_over(today, months_back(today, 12)), CHECK) == []


def test_the_check_starts_the_day_after_it_is_due(ctx, today):
    last = approved(start=months_back(today, 36), today=today)
    due = evaluations.next_due(today).due_on
    assert due == add_months(last.period_end, 12) + timedelta(days=90)
    for day, checks in ((due, 0), (due + timedelta(days=1), 1)):
        assert len(gaps_of(program.build(period(day - timedelta(days=30), day, day), None), CHECK)) == checks, day


def test_the_check_counts_90_days_from_a_february_29(ctx):
    on = date(2024, 5, 29)  # the 12 months after Mar 1, 2022 to Feb 28, 2023 ended on Feb 29, 2024: May 29 is the 90th day after
    approved(start=date(2022, 3, 1), today=on)
    assert evaluations.period_end_of(date(2023, 3, 1)) == date(2024, 2, 29)
    assert gaps_of(program.build(period(date(2024, 1, 1), on, on), None), CHECK) == []


def _reports_only(tenant) -> User:
    role = Role.objects.create(name="Reports only", slug="reports-only")
    role.set_levels({m: Level.VIEW if m == Module.REPORTS else Level.NONE for m in Module.values})
    return User.objects.create_user(username="ro@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role)


@pytest.mark.parametrize("who, linked", [("director", True), ("manager", True), ("analyst", True), ("technician", False),
                                         ("reports-only", False)])
def test_the_check_links_for_program_evaluation_view_only(ctx, today, tenant, make_user, client, who, linked):
    approved(start=months_back(today, 36), today=today)
    user = _reports_only(tenant) if who == "reports-only" else make_user(who)
    [check] = gaps_of(build_over(today, months_back(today, 12)), CHECK)
    assert (survey.gap_links(user)(check) == "/reports/evaluations/") is linked
    client.force_login(user)
    [gap] = [g for g in client.get("/api/v1/survey/program/").json()["gaps"] if g["record"] == "Program evaluation"]
    assert gap["url"] == ("/reports/evaluations/?facility=riverside" if linked else "")
    page = client.get("/reports/survey/").content.decode()
    assert "No approved program evaluation of the 12 months after" in page
    assert ('href="/reports/evaluations/"' in page) is linked


def test_not_covered_and_the_note_name_the_plan_itself(ctx, today):
    assert "The facility's written medical equipment management plan itself (an evaluation records the version reviewed)" in NOT_COVERED
    assert not any("annual evaluation" in item for item in NOT_COVERED)
    s = build_over(today, months_back(today, 12))
    assert program.PLAN_NOTE in s.notes and program.EVALUATIONS_NOTE in s.notes
    assert "plan itself, which Cadence does not keep: a program evaluation records the version reviewed" in program.PLAN_NOTE
    assert not any(word in note for note in s.notes for word in ("Joint Commission", "DNV", "ACHC"))
