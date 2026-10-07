"""
The survey binder's "Program and policies" section (slice 25, part 1; apps/reports/survey/program.py): the facility, when its Settings
last changed, the PM targets by risk class and the other KPI targets, the policy lines as set in Cadence (default or not), the
risk-scoring bands with today's active devices, the note on what on time means, and the CHECK on a PM policy line that speaks of a
month or a grace period. Also the two default PM policy texts, which now say what Cadence measures.
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from survey_helpers import gaps_of, period, rows_of

from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel, RiskClass
from apps.facility import services as fs
from apps.facility.models import POLICY_DEFAULTS, FacilitySettings
from apps.reports.survey import CHECK, program

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
    def queries():
        with CaptureQueriesContext(connection) as q:
            s = build(today)
            for t in s.tables:
                list(t.rows())
        return len(q.captured_queries)

    few = queries()
    for i in range(40):
        Asset.objects.create(tag=f"V-{i}", device_model=vent_model, department=dept)
    fs.update_settings(policy_medium_low=OLD_MEDIUM_LOW)
    assert queries() == few <= 3
