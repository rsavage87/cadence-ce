"""
Slice 32: the program's annual evaluation (apps/evaluations/services.py and indicators.py).

The services: drafting (the period's rules, overlap under the facility's lock, the number from the period's end, this period's
objectives carried from the adjacent approved evaluation only), Refresh figures (a draft only, by someone who may see every area, and
refused when the figures changed or it was sent meanwhile), the text one field at a time (limits, the token, the dates), objectives
(limits, results only on this period's, moves and removal), the statuses (sent to committee freezes, back to draft, discard never an
approved one), approvals (the first approves it, dates against the day sent and the approval, another committee's name, corrections
and removal), amendments (a copy of the approved version, one at a time, superseding only when approved), and the read helpers.

The indicators: each figure on a small facility; the PM window and the targets in force at the period's end, and the changes since;
provisional while a PM is inside its window; Cadence's records begin; the previous column from an approved evaluation and counted;
agreement with the binder's maintenance section; the AEM table; a fixed number of queries. The cut per reader. Tenant isolation, the
role levels, and on PostgreSQL the races (two refreshes, two drafts) and the services under row-level security.
"""
import threading
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from evaluation_fixtures import approved, draft, last_year, results_for_all, sent, year_before
from incident_fixtures import make_incident
from pg_helpers import as_app_role, needs_postgres
from survey_helpers import period as binder_period
from survey_helpers import rows_of

from apps.accounts.models import Level, Module, Role, RolePermission, User
from apps.equipment.models import AddedAs, Asset, AssetStatus, DeviceModel, Ownership, RiskClass
from apps.evaluations import indicators, services
from apps.evaluations.models import (
    OBJECTIVES_MAX,
    TEXT_MAX,
    Body,
    EvaluationApproval,
    EvaluationObjective,
    EvaluationSnapshot,
    ObjectiveKind,
    ObjectiveResult,
    ProgramEvaluation,
    Status,
)
from apps.facility import services as fac
from apps.facility.models import FacilitySettings, PmWindow
from apps.incidents.models import Affected, Outcome
from apps.pm.dates import add_months
from apps.pm.models import AemDecision, AemStatus
from apps.recalls.models import Alert, AlertMatch
from apps.reports.survey import maintenance
from apps.tenants.context import tenant_context
from apps.workorders.models import (
    InspectionResult,
    LaborLine,
    LateReason,
    PartLine,
    PmResult,
    Priority,
    RepairCheck,
    Source,
    WorkOrder,
    WoStatus,
    WoType,
)
from apps.workorders.services import create_work_order

DAY = timedelta(days=1)


def aware(day, hour=12):
    return timezone.make_aware(datetime.combine(day, time(hour)))


@pytest.fixture
def today(ctx):
    return timezone.localdate()


@pytest.fixture
def span(today):
    """The 12 months ending at the end of last month."""
    return last_year(today)


@pytest.fixture
def director(make_user):
    return make_user("director")


@pytest.fixture
def manager(make_user):
    return make_user("manager")


def custom_role(name, levels: dict) -> Role:
    role = Role.objects.create(name=name, slug=name.lower().replace(" ", "-"))
    role.set_levels({m: levels.get(m, Level.NONE) for m in Module.values})
    return Role.objects.get(pk=role.pk)


def user_with(tenant, name, levels: dict) -> User:
    return User.objects.create_user(username=f"{name}@riverside.example", password="Test-Pass-2026-x", tenant=tenant,
                                    role=custom_role(name.title(), levels))


EVERY_VIEW = {m: Level.VIEW for m in Module.values}


def device(tag, model, dept, *, added=None, in_service_from=None, **fields):
    """A device; `added` backdates when it was added (created_at) and its first history row, `in_service_from` its first history row."""
    a = Asset.objects.create(tag=tag, device_model=model, department=dept, **fields)
    if added:
        Asset.objects.filter(pk=a.pk).update(created_at=aware(added))
    first = in_service_from or added
    if first:
        Asset.history.filter(id=a.pk).update(history_date=aware(first, 9))
    a.refresh_from_db()
    return a


def pm(asset, due, done=None, *, reason="", result="", source=Source.PM_PLANNER, status=None):
    wo = create_work_order(asset=asset, type=WoType.PM, priority=Priority.NORMAL, problem="PM", opened_on=due - 20 * DAY, due_on=due,
                           source=source)
    fields = {}
    if done:
        fields.update(status=WoStatus.COMPLETED, completed_on=done)
    if status:
        fields["status"] = status
    if reason:
        fields["late_reason"] = reason
    if result:
        fields["pm_result"] = result
    if fields:
        WorkOrder.objects.filter(pk=wo.pk).update(**fields)
        wo.refresh_from_db()
    return wo


def repair(asset, opened, done, *, check="", source=Source.MANUAL, **fields):
    wo = create_work_order(asset=asset, type=WoType.REPAIR, priority=Priority.NORMAL, problem="Broken", opened_on=opened, source=source)
    WorkOrder.objects.filter(pk=wo.pk).update(status=WoStatus.COMPLETED, completed_on=done, repair_check=check, **fields)
    wo.refresh_from_db()
    return wo


def inspection(asset, done):
    wo = create_work_order(asset=asset, type=WoType.INSPECTION, priority=Priority.NORMAL, problem="Incoming", opened_on=done - 5 * DAY)
    WorkOrder.objects.filter(pk=wo.pk).update(status=WoStatus.COMPLETED, completed_on=done, inspection_result=InspectionResult.PASSED)
    return wo


def settings_on(day, **fields):
    """Change Settings and date the change's history row on `day`."""
    s = fac.update_settings(**fields)
    latest = FacilitySettings.history.order_by("-history_date", "-history_id").first()
    FacilitySettings.history.filter(history_id=latest.history_id).update(history_date=aware(day))
    return s


def figures(data) -> dict:
    return {i["key"]: i for i in data["indicators"]}


@pytest.fixture
def models(ctx, vent_model, pump_model):
    monitor = DeviceModel.objects.create(manufacturer="Philips", model="MX450", description="Patient monitor", category="Monitors",
                                         risk_class=RiskClass.MEDIUM, oem_pm_interval_months=12)
    thermo = DeviceModel.objects.create(manufacturer="Welch Allyn", model="SureTemp", description="Thermometer", category="Thermometers",
                                        risk_class=RiskClass.LOW, oem_pm_interval_months=12, aem_interval_months=24)
    scanner = DeviceModel.objects.create(manufacturer="GE", model="Revolution", description="CT scanner", category="CT",
                                         risk_class=RiskClass.MEDIUM, oem_pm_interval_months=6, aem_interval_months=12,
                                         oem_schedule_required=True)
    return {"ls": vent_model, "high": pump_model, "medium": monitor, "low": thermo, "cms": scanner}


@pytest.fixture
def world(ctx, today, span, dept, models, techs):
    """A small facility with every figure known (S = the period's start). Ours in use: v1 (life support), p1 (high, on AEM), m1, x1 (a
    CMS-marked scanner), n1, n2, e1, i1 (medium), t1 (low, on AEM); a retired monitor; a rental. PMs: life support and high 2 of 3 on
    time (one late, the device not located), medium and low on the manufacturer's interval 2 of 3, on AEM 2 of 2. Two new devices, one
    inspected before first use and one after. Three repairs (2, 40, and 1 days; checked, the vendor's, imported): $214.00 of labor
    and parts, Tom not credentialed for the monitor. One recall received and closed in 10 days and a Class I open now. Two incidents:
    one reportable reported on time, one waiting for a decision."""
    S, E = span
    old = S - 400 * DAY
    settings_on(S - 30 * DAY, repair_budget_monthly=Decimal("100.00"))
    w = {}
    w["v1"] = device("CE-1", models["ls"], dept, added=old, installed_on=old, next_pm_on=today - 5 * DAY)
    w["p1"] = device("CE-2", models["high"], dept, added=old, installed_on=old, next_pm_on=today + 60 * DAY)
    w["m1"] = device("CE-3", models["medium"], dept, added=old, installed_on=old, next_pm_on=today + 60 * DAY)
    w["t1"] = device("CE-4", models["low"], dept, added=old, installed_on=old, next_pm_on=today + 60 * DAY)
    w["x1"] = device("CE-5", models["cms"], dept, added=old, installed_on=old, next_pm_on=today + 60 * DAY)
    w["n1"] = device("CE-6", models["medium"], dept, added=S + 20 * DAY, in_service_from=S + 25 * DAY, added_as=AddedAs.NEW,
                     next_pm_on=today + 60 * DAY)
    w["n2"] = device("CE-7", models["medium"], dept, added=S + 30 * DAY, added_as=AddedAs.NEW, next_pm_on=today + 60 * DAY)
    w["e1"] = device("CE-8", models["medium"], dept, added=S + 35 * DAY, added_as=AddedAs.EXISTING, installed_on=old, next_pm_on=today + 60 * DAY)
    w["i1"] = device("CE-9", models["medium"], dept, added=S + 36 * DAY, added_as=AddedAs.IMPORTED, next_pm_on=today + 60 * DAY)
    w["r1"] = device("CE-10", models["medium"], dept, added=old)
    w["r1"].status = AssetStatus.RETIRED
    w["r1"].save()
    Asset.history.filter(id=w["r1"].pk, status=AssetStatus.RETIRED).update(history_date=aware(S + 80 * DAY))
    w["rental"] = device("RENT-1", models["medium"], dept, ownership=Ownership.RENTAL, owner="BedCo")
    # PMs
    pm(w["v1"], S + 10 * DAY, S + 10 * DAY)
    pm(w["v1"], S + 100 * DAY, S + 105 * DAY, reason=LateReason.NOT_LOCATED)
    pm(w["p1"], S + 40 * DAY, S + 39 * DAY)
    pm(w["m1"], S + 50 * DAY, S + 50 * DAY)
    pm(w["m1"], S + 200 * DAY, S + 201 * DAY)
    pm(w["x1"], S + 60 * DAY, S + 60 * DAY)
    pm(w["t1"], S + 70 * DAY, S + 70 * DAY)
    pm(w["t1"], S + 300 * DAY, S + 299 * DAY, result=PmResult.FAIL)
    pm(w["m1"], S - 10 * DAY, S - 10 * DAY)  # before the period
    # Incoming inspections: n1 inspected before it went into service, n2 after
    inspection(w["n1"], S + 24 * DAY)
    inspection(w["n2"], S + 40 * DAY)
    # Repairs
    w["rep1"] = repair(w["m1"], S + 100 * DAY, S + 102 * DAY, check=RepairCheck.PASSED, assigned_to=techs["tom"])
    LaborLine.objects.create(work_order=w["rep1"], technician=techs["tom"], worked_on=S + 101 * DAY, hours=Decimal("2"), rate=Decimal("82"))
    PartLine.objects.create(work_order=w["rep1"], description="Fan", quantity=Decimal("1"), unit_cost=Decimal("50"))
    w["rep2"] = repair(w["v1"], S + 150 * DAY, S + 190 * DAY, check=RepairCheck.VENDOR_REPORT, vendor_service=True,
                       vendor_name="Hamilton field service", checks_reference="SR-1")
    w["rep3"] = repair(w["t1"], S + 200 * DAY, S + 201 * DAY, source=Source.IMPORTED)
    # Recalls
    alert = Alert.objects.create(source=Alert.Source.FDA, external_id="Z-1", classification="Class II", manufacturer="BD", product="Pump",
                                 title="Keypad", published_on=S + 5 * DAY)
    match = AlertMatch.objects.create(alert=alert, device_model=models["high"], status=AlertMatch.Status.CLOSED, closed_on=S + 20 * DAY)
    AlertMatch.objects.filter(pk=match.pk).update(created_at=aware(S + 10 * DAY))
    urgent = Alert.objects.create(source=Alert.Source.FDA, external_id="Z-2", classification="Class I", manufacturer="Hamilton",
                                  product="Vent", title="Valve", published_on=today - 2 * DAY)
    AlertMatch.objects.create(alert=urgent, device_model=models["ls"])
    # Incidents
    reported = make_incident(w["p1"], outcome=Outcome.SERIOUS_INJURY, affected=Affected.PATIENT, occurred_on=S + 120 * DAY, hold=False,
                             open_work_order=False, event_reference="ER-1")
    reported.reportable = True
    reported.manufacturer_reported_on = S + 122 * DAY
    reported.decided_on = S + 121 * DAY
    reported.save()
    make_incident(w["m1"], outcome=Outcome.UNKNOWN, affected=Affected.PATIENT, occurred_on=S + 300 * DAY, hold=False, open_work_order=False)
    w["S"], w["E"] = S, E
    return w


# --- the role levels and the facility's walls -------------------------------------------------------------------------------------

@pytest.mark.parametrize("slug,level", [("director", Level.FULL), ("manager", Level.APPROVE), ("analyst", Level.VIEW),
                                        ("technician", Level.NONE), ("requester", Level.NONE), ("vendor", Level.NONE)])
def test_a_role_made_before_evaluations_takes_its_default(ctx, slug, level):
    role = Role.objects.get(slug=slug)
    RolePermission.objects.filter(role=role, module=Module.EVALUATIONS).delete()  # a facility from before slice 32
    assert Role.objects.get(pk=role.pk).level_for(Module.EVALUATIONS) == level


def test_a_custom_role_has_none_and_a_saved_none_stays(ctx):
    quality = custom_role("Quality", {Module.REPORTS: Level.VIEW})
    RolePermission.objects.filter(role=quality, module=Module.EVALUATIONS).delete()
    assert Role.objects.get(pk=quality.pk).level_for(Module.EVALUATIONS) == Level.NONE
    manager = Role.objects.get(slug="manager")
    manager.set_levels({Module.EVALUATIONS: Level.NONE})
    assert Role.objects.get(pk=manager.pk).level_for(Module.EVALUATIONS) == Level.NONE


def test_the_four_models_stay_in_their_facility(ctx, today, other_tenant):
    ev = approved(today=today, next_objectives=["Cut MTTR"])
    assert ProgramEvaluation.objects.count() == 1 and EvaluationSnapshot.objects.count() == 1
    assert EvaluationObjective.objects.count() == 1 and EvaluationApproval.objects.count() == 1
    with tenant_context(other_tenant):
        assert not ProgramEvaluation.objects.exists() and not EvaluationSnapshot.objects.exists()
        assert not EvaluationObjective.objects.exists() and not EvaluationApproval.objects.exists()
        theirs = draft(today=today)
        assert theirs.number == ev.number  # numbers repeat across facilities
        assert ProgramEvaluation.objects.get() == theirs
    assert ProgramEvaluation.objects.get() == ev
    assert services.current(ev.number) == ev


# --- drafting ---------------------------------------------------------------------------------------------------------------------

def test_a_draft_is_numbered_by_its_period_end_and_takes_its_figures(ctx, today, span, director):
    start = add_months(span[0], -12 * 3)  # well before today, ending in another year
    ev = services.create_draft(period_start=start, by=director, today=today)
    end = add_months(start, 12) - DAY
    assert (ev.period_start, ev.period_end, ev.status, ev.version, ev.created_by) == (start, end, Status.DRAFT, 1, director)
    assert ev.number == f"EV-{end.year % 100:02d}-0001"
    snap = ev.snapshot
    assert (snap.taken_on, snap.taken_by, snap.evaluation) == (today, director, ev)
    assert snap.data["schema"] == 1 and snap.data["period"] == {"start": start.isoformat(), "end": end.isoformat()}
    assert [i["key"] for i in snap.data["indicators"]] == [i.key for i in indicators.INDICATORS]
    assert list(ev.history.order_by("history_date", "history_id").values_list("history_change_reason", flat=True)) == ["Drafted", "Figures taken"]
    second = services.create_draft(period_start=add_months(start, 12), by=director, today=today)
    assert second.number == f"EV-{(end.year + 1) % 100:02d}-0001"  # each year of the period's end counts on its own


@pytest.mark.parametrize("start,words", [
    ("2024-03-15", "starts on the first of a month"),
    ("1926-01-01", "from 2000 on"),
])
def test_the_period_starts_on_the_first_of_a_month(ctx, today, start, words):
    with pytest.raises(ValidationError) as e:
        services.create_draft(period_start=date.fromisoformat(start), by=None, today=today)
    assert words in e.value.message_dict["period_start"][0]


def test_the_period_has_ended_before_it_is_drafted(ctx, today, span):
    this_month = today.replace(day=1)
    with pytest.raises(ValidationError) as e:
        services.create_draft(period_start=add_months(this_month, -11), by=None, today=today)
    assert "12 months that have ended" in e.value.message_dict["period_start"][0]
    ev = services.create_draft(period_start=span[0], by=None, today=today)  # the 12 months that ended at the end of last month
    assert ev.period_end == this_month - DAY < today


def test_periods_never_overlap_and_an_approved_one_is_amended(ctx, today, span):
    S, _E = span
    ev = draft(today=today)
    with pytest.raises(ValidationError) as e:
        services.create_draft(period_start=add_months(S, -6), by=None, today=today)
    assert f"{ev.label} (draft) covers" in e.value.message_dict["period_start"][0]
    approved(today=today, ev=ev)
    with pytest.raises(ValidationError) as e:
        services.create_draft(period_start=S, by=None, today=today)
    assert "is approved: amend it" in e.value.message_dict["period_start"][0]
    with pytest.raises(ValidationError) as e:
        services.create_draft(period_start=add_months(S, -3), by=None, today=today)
    assert "periods never overlap" in e.value.message_dict["period_start"][0]
    before = services.create_draft(period_start=add_months(S, -12), by=None, today=today)  # the 12 months before: no overlap
    assert before.period_end == S - DAY


def test_a_discarded_draft_frees_its_period(ctx, today, span):
    ev = draft(today=today)
    services.discard(ev, by=None)
    again = draft(today=today)
    assert again.number != ev.number and again.status == Status.DRAFT
    assert ProgramEvaluation.objects.get(pk=ev.pk).status == Status.DISCARDED


def test_drafting_needs_edit_and_every_area(ctx, today, tenant, make_user):
    with pytest.raises(PermissionDenied):
        draft(by=make_user("technician"), today=today)
    with pytest.raises(PermissionDenied):
        draft(by=make_user("analyst"), today=today)  # View only
    partial = user_with(tenant, "author", {**EVERY_VIEW, Module.EVALUATIONS: Level.EDIT, Module.INCIDENTS: Level.NONE,
                                           Module.USERS: Level.NONE})
    with pytest.raises(PermissionDenied) as e:
        draft(by=partial, today=today)
    assert "Users and access View and Incidents View, which your role does not have" in str(e.value)
    assert not ProgramEvaluation.objects.exists()
    assert not indicators.can_refresh(partial) and indicators.missing_for(partial) == [Module.USERS, Module.INCIDENTS]
    full = user_with(tenant, "writer", {**EVERY_VIEW, Module.EVALUATIONS: Level.EDIT})
    assert indicators.can_refresh(full) and draft(by=full, today=today).created_by == full


def test_objectives_are_carried_from_the_adjacent_approved_evaluation_only(ctx, today, span):
    S, _E = span
    earlier = approved(start=add_months(S, -12), today=today, next_objectives=["Cut MTTR to 3 days", "Inventory every ICU device"])
    ev = draft(today=today)
    carried = list(ev.objectives.order_by("position"))
    assert [(o.kind, o.text, o.result, o.carried_from.evaluation_id) for o in carried] == [
        (ObjectiveKind.REVIEW, "Cut MTTR to 3 days", "", earlier.pk), (ObjectiveKind.REVIEW, "Inventory every ICU device", "", earlier.pk)]
    assert set(earlier.objectives.values_list("result", flat=True)) == {""}  # results are written on the copies only
    services.discard(ev, by=None)
    gap = approved(start=add_months(S, -48), today=today, next_objectives=["Never copied"])
    after_gap = services.create_draft(period_start=add_months(S, -24), by=None, today=today)  # gap ends 12 months before this starts
    assert gap.period_end < after_gap.period_start - DAY and not after_gap.objectives.exists()


# --- Refresh figures --------------------------------------------------------------------------------------------------------------

def test_refresh_takes_new_figures_on_a_draft_only(ctx, today, director, dept, vent_model):
    ev = draft(by=director, today=today)
    first = ev.snapshot_id
    device("CE-99", vent_model, dept)
    services.refresh(ev, by=director, today=today)
    ev.refresh_from_db()
    assert ev.snapshot_id != first and EvaluationSnapshot.objects.filter(evaluation=ev).count() == 2
    assert figures(ev.snapshot.data)["devices_life_support"]["value"] == 1
    sent(ev=ev, today=today)
    with pytest.raises(ValidationError) as e:
        services.refresh(ev, by=director, today=today)
    assert "only a draft's figures are taken again. Take it back to draft first." in e.value.messages[0]


def test_refresh_needs_every_area(ctx, today, tenant):
    ev = draft(today=today)
    editor = user_with(tenant, "editor", {**EVERY_VIEW, Module.EVALUATIONS: Level.EDIT, Module.CONTRACTS: Level.NONE})
    with pytest.raises(PermissionDenied) as e:
        services.refresh(ev, by=editor, today=today)
    assert "Contracts View" in str(e.value)


def test_refresh_is_refused_when_the_figures_changed_meanwhile(ctx, today, monkeypatch):
    ev = draft(today=today)
    real = indicators.snapshot

    def another_refresh_lands(*args, **kwargs):
        data = real(*args, **kwargs)
        other = EvaluationSnapshot.objects.create(evaluation=ev, taken_at=timezone.now(), taken_on=today, data=data)
        ProgramEvaluation.objects.filter(pk=ev.pk).update(snapshot=other)
        return data

    monkeypatch.setattr(indicators, "snapshot", another_refresh_lands)
    with pytest.raises(ValidationError) as e:
        services.refresh(ev, by=None, today=today)
    assert e.value.messages == [services.FIGURES_CHANGED]


def test_refresh_is_refused_when_it_is_sent_meanwhile(ctx, today, monkeypatch):
    ev = draft(today=today)
    real = indicators.snapshot

    def sent_meanwhile(*args, **kwargs):
        data = real(*args, **kwargs)
        services.send_to_committee(ev, by=None, today=today)
        return data

    monkeypatch.setattr(indicators, "snapshot", sent_meanwhile)
    before = ev.snapshot_id
    with pytest.raises(ValidationError) as e:
        services.refresh(ev, by=None, today=today)
    assert "is sent to committee: only a draft's figures are taken again" in e.value.messages[0]
    assert ProgramEvaluation.objects.get(pk=ev.pk).snapshot_id == before


# --- the text ---------------------------------------------------------------------------------------------------------------------

def test_update_text_saves_one_field(ctx, today, director):
    ev = draft(today=today)
    stale = ProgramEvaluation.objects.get(pk=ev.pk)
    services.update_text(ev, "summary", "  A steady year.  ", by=director)
    services.update_text(stale, "went_well", "PMs on time.", by=director)  # an older copy: the summary saved above stays
    ev.refresh_from_db()
    assert (ev.summary, ev.went_well) == ("A steady year.", "PMs on time.")
    reasons = list(ev.history.order_by("history_date", "history_id").values_list("history_change_reason", flat=True))
    assert reasons[-2:] == ["Summary changed", "What went well changed"]
    services.update_text(ev, "summary", "A steady year.", by=director)  # unchanged: no history row
    assert ev.history.count() == len(reasons)
    services.update_text(ev, "inventory_reviewed_on", today, by=director)
    services.update_text(ev, "plan_title", "Medical equipment management plan", by=director)
    services.update_text(ev, "plan_version", "v4.2", by=director)
    services.update_text(ev, "plan_effective_on", "2026-01-01", by=director)
    ev.refresh_from_db()
    assert (ev.inventory_reviewed_on, ev.plan_version, ev.plan_effective_on.isoformat()) == (today, "v4.2", "2026-01-01")


@pytest.mark.parametrize("field,value,words", [
    ("summary", "x" * (TEXT_MAX + 1), f"At most {TEXT_MAX:,} characters"),
    ("plan_title", "x" * 121, "At most 120 characters"),
    ("plan_version", "v 4", "without spaces"),
    ("inventory_reviewed_on", "not a day", "YYYY-MM-DD"),
])
def test_text_limits_and_the_token(ctx, today, field, value, words):
    ev = draft(today=today)
    with pytest.raises(ValidationError) as e:
        services.update_text(ev, field, value, by=None)
    assert words in e.value.message_dict[field][0]


def test_the_inventory_review_is_never_after_today_and_fields_are_known(ctx, today):
    ev = draft(today=today)
    with pytest.raises(ValidationError) as e:
        services.update_text(ev, "inventory_reviewed_on", today + DAY, by=None, today=today)
    assert "after today" in e.value.message_dict["inventory_reviewed_on"][0]
    with pytest.raises(ValidationError):
        services.update_text(ev, "status", "approved", by=None)
    with pytest.raises(ValidationError):
        services.update_text(ev, "number", "EV-99-0001", by=None)


def test_the_text_is_frozen_once_sent(ctx, today):
    ev = sent(today=today)
    with pytest.raises(ValidationError) as e:
        services.update_text(ev, "summary", "Late edit", by=None)
    assert "Take it back to draft first" in e.value.messages[0]
    services.back_to_draft(ev, by=None)
    services.update_text(ev, "summary", "Edited after the committee asked", by=None)


# --- objectives -------------------------------------------------------------------------------------------------------------------

def test_objectives_are_added_changed_moved_and_removed(ctx, today, director):
    ev = draft(today=today)
    a = services.add_objective(ev, kind=ObjectiveKind.NEXT, text=" Cut MTTR ", target="3 days", by=director)
    b = services.add_objective(ev, kind=ObjectiveKind.NEXT, text="Inventory review", by=director)
    review = services.add_objective(ev, kind=ObjectiveKind.REVIEW, text="Life-support PMs on time", target="100%", by=director)
    assert (a.text, a.position, b.position, review.position) == ("Cut MTTR", 1, 2, 1)
    services.move_objective(b, up=True, by=director)
    assert list(ev.objectives.filter(kind=ObjectiveKind.NEXT).order_by("position").values_list("text", flat=True)) == ["Inventory review", "Cut MTTR"]
    services.move_objective(b, up=True, by=director)  # first already: nothing moves
    services.update_objective(review, result=ObjectiveResult.PARTLY, comment="Two late", by=director)
    review.refresh_from_db()
    assert (review.result, review.comment) == (ObjectiveResult.PARTLY, "Two late")
    with pytest.raises(ValidationError) as e:
        services.update_objective(a, result=ObjectiveResult.MET, by=director)
    assert "no result yet" in e.value.message_dict["result"][0]
    with pytest.raises(ValidationError):
        services.update_objective(review, result="done", by=director)
    with pytest.raises(ValidationError):
        services.update_objective(review, text="  ", by=director)
    services.remove_objective(a, by=director)
    assert not EvaluationObjective.objects.filter(pk=a.pk).exists()
    assert EvaluationObjective.history.filter(id=a.pk, history_type="-").get().history_change_reason == "Objective removed"


@pytest.mark.parametrize("fields,key", [({"text": "x" * 301}, "text"), ({"text": "ok", "target": "x" * 121}, "target"),
                                        ({"text": ""}, "text"), ({"text": "ok", "kind": "later"}, "kind")])
def test_objective_limits(ctx, today, fields, key):
    ev = draft(today=today)
    with pytest.raises(ValidationError) as e:
        services.add_objective(ev, **{"kind": ObjectiveKind.NEXT, **fields}, by=None)
    assert key in e.value.message_dict


def test_at_most_twenty_of_each_kind_and_a_comment_limit(ctx, today):
    ev = draft(today=today)
    for n in range(OBJECTIVES_MAX):
        services.add_objective(ev, kind=ObjectiveKind.NEXT, text=f"Objective {n}", by=None)
    with pytest.raises(ValidationError) as e:
        services.add_objective(ev, kind=ObjectiveKind.NEXT, text="One too many", by=None)
    assert f"at most {OBJECTIVES_MAX}" in e.value.messages[0]
    review = services.add_objective(ev, kind=ObjectiveKind.REVIEW, text="Still room for this period's", by=None)
    with pytest.raises(ValidationError) as e:
        services.update_objective(review, comment="x" * 301, by=None)
    assert "comment" in e.value.message_dict


def test_objectives_are_frozen_once_sent(ctx, today):
    ev = draft(today=today)
    obj = services.add_objective(ev, kind=ObjectiveKind.NEXT, text="Cut MTTR", by=None)
    sent(ev=ev, today=today)
    for call in (lambda: services.add_objective(ev, kind=ObjectiveKind.NEXT, text="More", by=None),
                 lambda: services.update_objective(obj, text="Changed", by=None), lambda: services.remove_objective(obj, by=None),
                 lambda: services.move_objective(obj, up=False, by=None)):
        with pytest.raises(ValidationError):
            call()


# --- statuses ---------------------------------------------------------------------------------------------------------------------

def test_sending_needs_a_result_for_every_objective_of_the_period(ctx, today, span, director):
    S, _E = span
    approved(start=add_months(S, -12), today=today, next_objectives=["Cut MTTR", "Review the inventory"])
    ev = draft(today=today)
    with pytest.raises(ValidationError) as e:
        services.send_to_committee(ev, by=director, today=today)
    assert "2 have none" in e.value.messages[0]
    results_for_all(ev, result=ObjectiveResult.CARRIED)
    services.send_to_committee(ev, by=director, today=today)
    ev.refresh_from_db()
    assert (ev.status, ev.sent_on) == (Status.READY, today)


def test_sending_needs_the_figures(ctx, today):
    ev = draft(today=today)
    ProgramEvaluation.objects.filter(pk=ev.pk).update(snapshot=None)
    with pytest.raises(ValidationError) as e:
        services.send_to_committee(ev, by=None, today=today)
    assert "Take the figures" in e.value.messages[0]


def test_back_to_draft_keeps_the_day_sent(ctx, today):
    ev = sent(today=today - 3 * DAY)
    services.back_to_draft(ev, by=None)
    ev.refresh_from_db()
    assert (ev.status, ev.sent_on) == (Status.DRAFT, today - 3 * DAY)
    with pytest.raises(ValidationError):
        services.back_to_draft(ev, by=None)
    services.send_to_committee(ev, by=None, today=today)
    ev.refresh_from_db()
    assert ev.sent_on == today


def test_discard_never_an_approved_one(ctx, today, make_user):
    ready = sent(today=today)
    with pytest.raises(PermissionDenied):
        services.discard(ready, by=make_user("analyst"))
    services.discard(ready, by=make_user("manager"))
    assert ProgramEvaluation.objects.get(pk=ready.pk).status == Status.DISCARDED
    with pytest.raises(ValidationError):
        services.discard(ready, by=None)
    ev = approved(today=today)
    with pytest.raises(ValidationError) as e:
        services.discard(ev, by=None)
    assert "never discarded. Amend it instead." in e.value.messages[0]


# --- approvals --------------------------------------------------------------------------------------------------------------------

def test_the_first_approval_approves_it(ctx, today, manager):
    ev = sent(today=today - 10 * DAY)
    editor = user_with(ev.tenant, "editor", {**EVERY_VIEW, Module.EVALUATIONS: Level.EDIT})
    with pytest.raises(PermissionDenied):
        services.record_approval(ev, body=Body.EOC, meeting_on=today, by=editor, today=today)
    a = services.record_approval(ev, body=Body.EOC, meeting_on=today - 2 * DAY, chair_name=" Dr. Lee ", chair_title="Chair, EOC",
                                 minutes_reference="EOC-2026-10", by=manager, today=today)
    ev.refresh_from_db()
    assert (ev.status, ev.approved_on, a.recorded_by, a.chair_name, a.body_name) == (Status.APPROVED, today - 2 * DAY, manager, "Dr. Lee", "")
    assert list(ev.history.order_by("history_date", "history_id").values_list("history_change_reason", flat=True))[-1] == "Approved"


@pytest.mark.parametrize("fields,key,words", [
    ({"meeting_on": "sent"}, None, None),
    ({"body": "board"}, "body", "Choose the committee"),
    ({"body": Body.OTHER}, "body_name", "Name the committee"),
    ({"body": Body.OTHER, "body_name": "x" * 81}, "body_name", "At most 80"),
    ({"minutes_reference": "EOC minutes 4"}, "minutes_reference", "without spaces"),
    ({"meeting_on": "future"}, "meeting_on", "cannot be after today"),
    ({"meeting_on": "before"}, "meeting_on", "on or after that day"),
    ({"chair_name": "x" * 121}, "chair_name", "At most 120"),
])
def test_approval_dates_and_bodies(ctx, today, fields, key, words):
    ev = sent(today=today - 5 * DAY)
    days = {"sent": today - 5 * DAY, "future": today + DAY, "before": today - 6 * DAY}
    values = {"body": Body.EOC, "meeting_on": today, **fields}
    values["meeting_on"] = days.get(values["meeting_on"], values["meeting_on"])
    if key is None:  # the meeting may be on the day it was sent
        services.record_approval(ev, **values, by=None, today=today)
        return
    with pytest.raises(ValidationError) as e:
        services.record_approval(ev, **values, by=None, today=today)
    assert words in e.value.message_dict[key][0]
    assert ProgramEvaluation.objects.get(pk=ev.pk).status == Status.READY


def test_another_committee_is_named_and_a_draft_is_never_approved(ctx, today):
    ev = draft(today=today)
    with pytest.raises(ValidationError) as e:
        services.record_approval(ev, body=Body.EOC, meeting_on=today, by=None, today=today)
    assert "send it to committee before recording its approval" in e.value.messages[0]
    sent(ev=ev, today=today)
    a = services.record_approval(ev, body=Body.OTHER, body_name="Patient Safety Council", meeting_on=today, by=None, today=today)
    assert a.body_name == "Patient Safety Council"
    b = services.record_approval(ev, body=Body.QUALITY, body_name="ignored", meeting_on=today, by=None, today=today)
    assert b.body_name == ""


def test_later_approvals_are_dated_on_or_after_it(ctx, today):
    ev = approved(today=today, meeting_on=today - 10 * DAY)
    with pytest.raises(ValidationError) as e:
        services.record_approval(ev, body=Body.QUALITY, meeting_on=today - 11 * DAY, by=None, today=today)
    assert "a later committee's meeting is on or after that day" in e.value.message_dict["meeting_on"][0]
    later = services.record_approval(ev, body=Body.GOVERNING, meeting_on=today - 10 * DAY, by=None, today=today)
    ev.refresh_from_db()
    assert ev.approved_on == today - 10 * DAY and ev.approvals.count() == 2
    assert later.evaluation == ev


def test_correcting_an_approval(ctx, today, manager):
    ev = sent(today=today - 20 * DAY)
    first = services.record_approval(ev, body=Body.EOC, meeting_on=today - 15 * DAY, by=None, today=today)
    later = services.record_approval(ev, body=Body.QUALITY, meeting_on=today - 5 * DAY, by=None, today=today)
    services.correct_approval(first, meeting_on=today - 18 * DAY, minutes_reference="EOC-7", by=manager, today=today)
    ev.refresh_from_db()
    first.refresh_from_db()
    assert (ev.approved_on, first.meeting_on, first.minutes_reference) == (today - 18 * DAY, today - 18 * DAY, "EOC-7")
    assert first.history.order_by("-history_date").first().history_change_reason == "Approval corrected"
    with pytest.raises(ValidationError) as e:
        services.correct_approval(first, meeting_on=today - 21 * DAY, by=manager, today=today)
    assert "sent to committee on" in e.value.message_dict["meeting_on"][0]
    with pytest.raises(ValidationError) as e:
        services.correct_approval(first, meeting_on=today - 4 * DAY, by=manager, today=today)
    assert "A later approval is dated" in e.value.message_dict["meeting_on"][0]
    with pytest.raises(ValidationError) as e:
        services.correct_approval(later, meeting_on=today - 19 * DAY, by=manager, today=today)
    assert "was approved on" in e.value.message_dict["meeting_on"][0]
    with pytest.raises(ValidationError):
        services.correct_approval(later, recorded_by=None, by=manager, today=today)
    services.correct_approval(later, body=Body.OTHER, body_name="Board of Trustees", by=manager, today=today)
    later.refresh_from_db()
    assert (later.body, later.body_name) == (Body.OTHER, "Board of Trustees")


def test_removing_a_later_approval_entered_in_error(ctx, today, make_user):
    ev = approved(today=today)
    first = ev.approvals.get()
    later = services.record_approval(ev, body=Body.MEC, meeting_on=today, by=None, today=today)
    with pytest.raises(PermissionDenied):
        services.remove_approval(later, by=make_user("analyst"))
    with pytest.raises(ValidationError) as e:
        services.remove_approval(first, by=None)
    assert "never removed" in e.value.messages[0]
    services.remove_approval(later, by=make_user("manager"))
    assert list(ev.approvals.all()) == [first]
    assert EvaluationApproval.history.filter(id=later.pk, history_type="-").get().history_change_reason == "Removed: entered in error"


# --- amendments -------------------------------------------------------------------------------------------------------------------

def test_an_amendment_copies_the_approved_version(ctx, today, span, manager):
    S, _E = span
    approved(start=add_months(S, -12), today=today, next_objectives=["Cut MTTR"])
    ev = draft(today=today)
    services.update_text(ev, "summary", "The year in short.", by=None)
    services.update_text(ev, "plan_version", "v4", by=None)
    services.add_objective(ev, kind=ObjectiveKind.NEXT, text="Next year", target="All", by=None)
    ev = approved(ev=ev, today=today)
    with pytest.raises(PermissionDenied):
        services.amend(ev, by=user_with(ev.tenant, "editor", {**EVERY_VIEW, Module.EVALUATIONS: Level.EDIT}))
    new = services.amend(ev, by=manager, today=today)
    assert (new.number, new.version, new.amends, new.status, new.label) == (ev.number, 2, ev, Status.DRAFT, f"{ev.number} v2")
    assert (new.summary, new.plan_version, new.period_start, new.period_end) == ("The year in short.", "v4", ev.period_start, ev.period_end)
    old_objectives = list(ev.objectives.order_by("kind", "position").values_list("kind", "text", "result", "carried_from"))
    assert list(new.objectives.order_by("kind", "position").values_list("kind", "text", "result", "carried_from")) == old_objectives
    assert new.snapshot_id != ev.snapshot_id and new.snapshot.data == ev.snapshot.data
    assert (new.snapshot.taken_on, new.snapshot.taken_at) == (ev.snapshot.taken_on, ev.snapshot.taken_at)
    assert ProgramEvaluation.objects.get(pk=ev.pk).status == Status.APPROVED  # the version of record until the amendment is approved
    assert services.current(ev.number) == ev
    with pytest.raises(ValidationError) as e:
        services.amend(ev, by=manager, today=today)
    assert f"{new.label} already amends {ev.label}" in e.value.messages[0]


def test_approving_an_amendment_supersedes_the_earlier_version(ctx, today):
    ev = approved(today=today, meeting_on=today - 30 * DAY)
    new = services.amend(ev, by=None, today=today)
    services.update_text(new, "improvements", "Added after the committee asked", by=None)
    services.refresh(new, by=None, today=today)  # a separate choice
    sent(ev=new, today=today - 2 * DAY)
    with pytest.raises(ValidationError):
        services.record_approval(ev, body=Body.QUALITY, meeting_on=today - 40 * DAY, by=None, today=today)
    services.record_approval(new, body=Body.EOC, meeting_on=today, by=None, today=today)
    ev.refresh_from_db()
    new.refresh_from_db()
    assert (ev.status, new.status, new.approved_on) == (Status.SUPERSEDED, Status.APPROVED, today)
    assert ev.history.order_by("-history_date", "-history_id").first().history_change_reason == f"Superseded by {new.label}"
    assert services.current(ev.number) == new
    with pytest.raises(ValidationError) as e:
        services.record_approval(ev, body=Body.QUALITY, meeting_on=today, by=None, today=today)
    assert "superseded by an amendment" in e.value.messages[0]
    with pytest.raises(ValidationError):
        services.amend(ev, by=None, today=today)
    third = services.amend(new, by=None, today=today)
    services.discard(third, by=None)  # a discarded amendment: the approved version stays
    assert services.amend(new, by=None, today=today).version == 4
    assert ProgramEvaluation.objects.get(pk=new.pk).status == Status.APPROVED


def test_the_read_helpers(ctx, today, span):
    S, E = span
    assert services.next_due(today) is None and services.records_begin() is None
    assert services.suggested_start(today) == add_months(today.replace(day=1), -12)  # the 12 months ending last month
    older = approved(start=add_months(S, -24), today=today)
    ev = draft(today=today)
    assert services.previous_approved(ev) == older
    due = services.next_due(today)
    assert due.due_on == services.period_end_of(older.period_end + DAY) + 90 * DAY and due.after == older
    assert due.overdue == (today > due.due_on)
    assert services.suggested_start(today) == E + DAY
    discarded = draft(start=add_months(S, -12), today=today)
    services.discard(discarded, by=None)
    assert services.current(discarded.number) == discarded


# --- the indicators ---------------------------------------------------------------------------------------------------------------

def test_each_figure_on_a_small_facility(world, today):
    S, E = world["S"], world["E"]
    data = indicators.snapshot(S, E, today=today)
    f = figures(data)

    def shown(key):
        return (f[key]["num"], f[key]["den"], f[key]["display"], f[key]["met"])

    assert shown("devices_ours") == (9, None, "9", None)
    assert [f[k]["value"] for k in ("devices_life_support", "devices_high", "devices_medium", "devices_low")] == [1, 1, 6, 1]
    assert f["devices_temporary"]["value"] == 1 and f["devices_retired"]["value"] == 1
    assert [f[k]["value"] for k in ("devices_added_new", "devices_added_existing", "devices_added_imported")] == [2, 1, 1]
    assert shown("work_imported") == (1, 13, "7.6%", None)
    assert shown("pm_high") == (2, 3, "66.6%", False) and f["pm_high"]["target_display"] == "100%"
    assert [(q["num"], q["den"]) for q in f["pm_high"]["quarters"]] == [(2, 2), (0, 1), (0, 0), (0, 0)]
    assert f["pm_high"]["quarters"][2]["display"] == "None due"
    assert shown("pm_oem") == (2, 3, "66.6%", False)
    assert shown("pm_aem") == (2, 2, "100%", True) and f["pm_aem"]["target"] == 95.0
    assert shown("pm_not_located") == (1, 2, "1", None)
    assert shown("pm_pending") == (0, None, "0", None) and data["provisional_until"] is None
    assert shown("devices_past_window") == (1, 9, "1", None)
    assert shown("inspections_new") == (2, None, "2", None)
    assert shown("inspections_before_use") == (1, 2, "50%", None)
    assert f["inspections_used_before"]["value"] == 0
    assert shown("repairs_completed") == (3, None, "3", None)
    assert shown("repairs_mttr") == (43, 3, "14.4 days", False) and f["repairs_mttr"]["target_display"] == "3 days"
    assert shown("repairs_within_30") == (2, 3, "66.6%", None)
    assert [f[k]["value"] for k in ("repairs_checked", "repairs_vendor_checked", "repairs_not_returned", "repairs_no_outcome")] == [1, 1, 0, 1]
    assert [f[k]["value"] for k in ("recalls_received", "recalls_closed", "recalls_open_class1")] == [1, 1, 1]
    assert f["recalls_median_days"]["display"] == "10 days"
    assert [f[k]["value"] for k in ("incidents_recorded", "incidents_reportable", "incidents_reports_on_time", "incidents_reports_late",
                                    "incidents_pending")] == [2, 1, 1, 0, 1]
    assert shown("staff_lapse_work") == (1, 1, "1", None)
    assert shown("vendor_nothing_on_file") == (1, 1, "1", None)
    assert shown("repair_spend") == (214.0, None, "$214.00", True) and f["repair_spend"]["target_display"] == "$1,200.00"
    assert f["aem_models"]["value"] == 2
    assert data["not_recorded"] == list(indicators.NOT_RECORDED)
    assert set(data["binder_gaps"]) == {"program", "inventory", "maintenance", "aem", "inspections", "repairs", "recalls", "incidents",
                                        "staff", "vendors"}
    assert data["binder_gaps"]["staff"] == {"gap": 0, "finding": 1, "check": 0}
    for i in data["indicators"]:  # every stored indicator reads on its own
        assert {"label", "group", "unit", "needs", "rule", "display", "basis"} <= set(i) and i["label"]
    assert "compliant" not in str(data).lower()


def test_the_aem_table(world, today, models):
    S, E = world["S"], world["E"]
    AemDecision.objects.create(device_model=models["low"], interval_months=24, oem_interval_months=12, status=AemStatus.APPROVED,
                               proposed_on=S - 60 * DAY, decided_on=S - 30 * DAY, evidence={"repairs_per_device_year": 0.4})
    rows = {r["model"]: r for r in indicators.snapshot(S, E, today=today)["tables"]["aem"]}
    assert set(rows) == {"BD Alaris 8015 PCU", "Welch Allyn SureTemp"}  # the CMS-marked scanner and life support never
    thermo = rows["Welch Allyn SureTemp"]
    assert (thermo["devices"], thermo["device_years"], thermo["repairs"], thermo["per_device_year"], thermo["failed_pms"]) == (1, 1.0, 1, 1.0, 1)
    assert (thermo["approved_on"], thermo["evidence_per_device_year"], thermo["aem_months"], thermo["oem_months"]) == (
        (S - 30 * DAY).isoformat(), 0.4, 24, 12)
    assert rows["BD Alaris 8015 PCU"]["repairs"] == 0 and rows["BD Alaris 8015 PCU"]["approved_on"] is None


def test_the_window_and_targets_at_the_period_end(world, today):
    """The window and target in force at the period's end judge it; changes made since are listed."""
    S, E = world["S"], world["E"]
    settings_on(S + 10 * DAY, pm_window_high=PmWindow.DAYS_AFTER, pm_window_high_days=10, target_mttr_days=Decimal("20.0"))
    settings_on(E + DAY, pm_window_high=PmWindow.DUE_DATE, pm_window_high_days=None, target_pm_pct=Decimal("90.0"))
    data = indicators.snapshot(S, E, today=today)
    f = figures(data)
    assert f["pm_high"]["num"] == 3 and f["pm_high"]["den"] == 3 and f["pm_high"]["met"] is True  # 5 days late, inside 10
    assert f["pm_not_located"]["num"] == 0
    assert f["pm_aem"]["target"] == 95.0  # 90 came after the period
    assert f["repairs_mttr"]["met"] is True and f["repairs_mttr"]["target_display"] == "20 days"
    assert data["settings"]["pm_windows"]["high"] == {"kind": PmWindow.DAYS_AFTER, "days": 10, "words": "Within 10 days after the due date"}
    assert data["settings"]["targets"]["pm_other"] == 95.0 and data["settings"]["as_of"] == E.isoformat()
    changes = {c["field"]: c for c in data["settings"]["changed_since"]}
    assert set(changes) == {"pm_window_high", "target_pm_pct", "policy_life_support"}
    assert (changes["pm_window_high"]["before"], changes["pm_window_high"]["after"]) == ("Within 10 days after the due date", "By the due date")
    assert changes["target_pm_pct"] == {"on": (E + DAY).isoformat(), "field": "target_pm_pct", "what": "PM completion target, medium and low risk",
                                        "before": "95%", "after": "90%"}
    # The binder, judged by today's window, counts the late PM as late: the evaluation keeps the period's own rule.
    binder = maintenance.build(binder_period(S, E, today), None)
    assert rows_of(binder, "by_class")[0][2] == 1


def test_defaults_when_settings_were_first_saved_after_the_period(ctx, today, span):
    S, E = span
    settings_on(E + DAY, target_pm_pct=Decimal("80.0"))
    data = indicators.snapshot(S, E, today=today)
    assert data["settings"]["targets"]["pm_other"] == 95.0
    assert [c["field"] for c in data["settings"]["changed_since"]] == ["target_pm_pct"]


def test_provisional_while_a_pm_is_inside_its_window(ctx, today, span, dept, vent_model):
    S, E = span
    settings_on(S - DAY, pm_window_high=PmWindow.NEXT_MONTH)
    v = device("CE-1", vent_model, dept, added=S - 100 * DAY)
    pm(v, E)  # due on the period's last day, not done: its window runs to the end of this month
    pm(v, S + 5 * DAY, S + 5 * DAY)
    data = indicators.snapshot(S, E, today=today)
    f = figures(data)
    end_of_month = add_months(E + DAY, 1) - DAY
    assert f["pm_pending"]["value"] == 1 and data["provisional_until"] == end_of_month.isoformat()
    assert (f["pm_high"]["num"], f["pm_high"]["den"]) == (1, 1)  # not counted until done or its window closes
    later = indicators.snapshot(S, E, today=end_of_month + DAY)
    assert later["provisional_until"] is None and figures(later)["pm_high"]["den"] == 2


def test_records_begin(ctx, today, span, dept, vent_model):
    S, E = span
    assert indicators.snapshot(S, E, today=today)["records_begin"] is None
    v = device("CE-1", vent_model, dept)
    pm(v, S + 40 * DAY, S + 40 * DAY, source=Source.IMPORTED)
    data = indicators.snapshot(S, E, today=today)
    assert data["records_begin"] == (S + 20 * DAY).isoformat()  # imports included
    assert services.records_begin() == S + 20 * DAY


def test_the_previous_column_counted_then_from_the_approval(world, today):
    S, E = world["S"], world["E"]
    pm(world["v1"], S - 100 * DAY, S - 90 * DAY)  # the 12 months before: one life-support PM, late
    data = indicators.snapshot(S, E, today=today)
    prev = data["previous"]
    assert (prev["source"], prev["counted_on"]) == ("counted", today.isoformat())
    assert prev["period"] == {"start": year_before(S).isoformat(), "end": (S - DAY).isoformat()}
    assert prev["values"]["pm_high"] == {"display": "0%", "num": 0, "den": 1, "value": 0.0}
    assert prev["values"]["pm_oem"]["num"] == 1  # m1's PM due before the period
    assert "devices_ours" not in prev["values"] and "pm_pending" not in prev["values"]  # a state on the day taken: not counted again
    earlier = approved(start=year_before(S), today=today)
    ev = draft(today=today)
    prev = ev.snapshot.data["previous"]
    assert (prev["source"], prev["number"], prev["version"], prev["approved_on"]) == ("approved", earlier.number, 1, today.isoformat())
    stored = figures(earlier.snapshot.data)
    assert prev["values"]["pm_high"]["display"] == stored["pm_high"]["display"]
    assert prev["values"]["devices_ours"]["value"] == stored["devices_ours"]["value"]  # the approved figures, whatever their basis


def test_no_previous_values_before_the_records_begin(ctx, today, span, dept, vent_model):
    S, E = span
    pm(device("CE-1", vent_model, dept), S + 30 * DAY, S + 30 * DAY)
    prev = indicators.snapshot(S, E, today=today)["previous"]
    assert prev["source"] == "counted" and prev["values"] == {}


def test_the_pm_figures_agree_with_the_binder(world, today):
    S, E = world["S"], world["E"]
    f = figures(indicators.snapshot(S, E, today=today))
    binder = maintenance.build(binder_period(S, E, today), None)
    by_class = {row[0]: row for row in rows_of(binder, "by_class")}
    high = [by_class["Life support"], by_class["High"]]
    other = [by_class["Medium"], by_class["Low"]]
    assert (f["pm_high"]["num"], f["pm_high"]["den"]) == (sum(r[2] for r in high), sum(r[1] for r in high))
    assert (f["pm_oem"]["num"] + f["pm_aem"]["num"], f["pm_oem"]["den"] + f["pm_aem"]["den"]) == (sum(r[2] for r in other), sum(r[1] for r in other))
    past = next(fig for fig in binder.figures if fig.label == "Devices past their PM date today")
    assert f["devices_past_window"]["value"] == past.value


def _scaled(world, n):
    """`n` more PMs, repairs (with labor and parts), and inspections of the world's kinds."""
    S = world["S"]
    for k in range(n):
        pm(world["m1"], S + (k % 300) * DAY, S + (k % 300 + k % 3) * DAY)
        wo = repair(world["t1"], S + (k % 200) * DAY, S + (k % 200 + 5) * DAY, check=RepairCheck.PASSED)
        LaborLine.objects.create(work_order=wo, technician=None, worked_on=S, hours=Decimal("1"), rate=Decimal("82"))
        PartLine.objects.create(work_order=wo, description="Part", quantity=Decimal("1"), unit_cost=Decimal("5"))


def test_a_fixed_number_of_queries(world, today):
    """About 20 work orders, then about 70: the same queries."""
    S, E = world["S"], world["E"]
    _scaled(world, 2)
    with CaptureQueriesContext(connection) as small:
        indicators.snapshot(S, E, today=today)
    _scaled(world, 25)
    with CaptureQueriesContext(connection) as large:
        data = indicators.snapshot(S, E, today=today)
    assert figures(data)["repairs_completed"]["value"] == 3 + 27
    assert len(small) == len(large), [q["sql"][:120] for q in large.captured_queries[len(small):]]


# --- the cut ----------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def stored(world, today):
    S, E = world["S"], world["E"]
    return indicators.snapshot(S, E, today=today)


def test_the_director_sees_everything(stored, director):
    seen = indicators.cut(stored, director)
    assert seen["left_out"] == [] and len(seen["indicators"]) == len(indicators.INDICATORS)
    assert seen["tables"] == stored["tables"] and seen["binder_gaps"] == stored["binder_gaps"]


def test_without_incidents_view(stored, tenant):
    reader = user_with(tenant, "reader", {**EVERY_VIEW, Module.INCIDENTS: Level.NONE})
    seen = indicators.cut(stored, reader)
    keys = {i["key"] for i in seen["indicators"]}
    assert not any(k.startswith("incidents_") for k in keys) and "pm_high" in keys
    assert indicators.tally(seen["indicators"]) == indicators.tally(stored["indicators"]) == (2, 5)  # incidents have no target
    assert [indicators.met_words(m) for m in (True, False, None)] == ["Met", "Not met", ""]
    assert "incidents" not in seen["binder_gaps"] and "staff" in seen["binder_gaps"]
    assert not any(k.startswith("incidents_") for k in seen["previous"]["values"])
    assert seen["left_out"] == ["Device incidents (needs Incidents View)",
                                "The survey binder's items for Device incidents (needs Incidents View)"]


def test_without_users_view(stored, tenant):
    reader = user_with(tenant, "reader", {**EVERY_VIEW, Module.USERS: Level.NONE})
    seen = indicators.cut(stored, reader)
    keys = {i["key"] for i in seen["indicators"]}
    assert "staff_lapse_work" not in keys and "vendor_nothing_on_file" in keys
    assert seen["left_out"][0] == "Staff and vendors: Work completed without a credential for the device (needs Users and access View)"
    assert "staff" not in seen["binder_gaps"]


def test_without_contracts_view(stored, tenant):
    reader = user_with(tenant, "reader", {**EVERY_VIEW, Module.CONTRACTS: Level.NONE})
    seen = indicators.cut(stored, reader)
    keys = {i["key"] for i in seen["indicators"]}
    assert "repair_spend" not in keys and "vendor_nothing_on_file" not in keys and "staff_lapse_work" in keys
    assert "Cost (needs Contracts View)" in seen["left_out"]
    assert indicators.tally(seen["indicators"]) == (1, 4)  # "n of m met" counts only what is shown
    assert "vendors" not in seen["binder_gaps"]


def test_without_pm_view_the_aem_table_goes_too(stored, tenant):
    reader = user_with(tenant, "reader", {**EVERY_VIEW, Module.PM: Level.NONE})
    seen = indicators.cut(stored, reader)
    assert seen["tables"] == {} and "AEM effectiveness by model (needs PM schedule View)" in seen["left_out"]
    assert not any(i["group"] == "pm" for i in seen["indicators"])


def test_without_reports_view_no_figures_at_all(stored, tenant, make_user):
    reader = user_with(tenant, "reader", {**EVERY_VIEW, Module.REPORTS: Level.NONE, Module.EVALUATIONS: Level.APPROVE})
    for who in (reader, make_user("vendor")):
        seen = indicators.cut(stored, who)
        assert seen["indicators"] == [] and seen["tables"] == {} and seen["binder_gaps"] == {} and seen["previous"]["values"] == {}
        assert seen["settings"] == {} and seen["left_out"] == ["Every figure (needs Reports View)"]
        assert seen["period"] == stored["period"]


def test_the_cut_reads_the_stored_needs(stored, tenant):
    old = dict(stored)
    old["indicators"] = [{**stored["indicators"][0], "key": "retired_key", "needs": ["incidents"]}, {"key": "unknown", "group": "scope"}]
    reader = user_with(tenant, "reader", {**EVERY_VIEW, Module.INCIDENTS: Level.NONE})
    seen = indicators.cut(old, reader)
    assert seen["indicators"] == []
    analyst = indicators.cut(old, user_with(tenant, "everyone", EVERY_VIEW))
    assert [i["key"] for i in analyst["indicators"]] == ["retired_key"]  # a key no longer in the registry still reads as stored


# --- PostgreSQL ---------------------------------------------------------------------------------------------------------------------

def _race(tenant, work, n=2):
    errors, done = [], []

    def run(i):
        try:
            with tenant_context(tenant):
                done.append(work(i))
        except Exception as e:  # noqa: BLE001 - reported below
            errors.append(e)
        finally:
            connection.close()

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    return done, errors


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_two_refreshes_at_once(tenant, monkeypatch):
    """Both count, then both reach the lock: the first saves its figures, the second finds them changed and is refused."""
    with tenant_context(tenant):
        ev = draft()
        first = ev.snapshot_id
    barrier = threading.Barrier(2, timeout=30)
    real = indicators.snapshot

    def counted_together(*args, **kwargs):
        data = real(*args, **kwargs)
        barrier.wait()
        return data

    monkeypatch.setattr(indicators, "snapshot", counted_together)
    done, errors = _race(tenant, lambda i: services.refresh(ev, by=None))
    assert len(done) == 1 and len(errors) == 1 and errors[0].messages == [services.FIGURES_CHANGED]
    with tenant_context(tenant):
        ev.refresh_from_db()
        assert ev.snapshot_id not in (None, first) and EvaluationSnapshot.objects.filter(evaluation=ev).count() == 2


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_two_drafts_of_overlapping_periods_at_once(tenant, monkeypatch):
    """Both pass the early check and count; the facility's lock lets one in, and the other finds it and is refused."""
    with tenant_context(tenant):
        today = timezone.localdate()
        start = last_year(today)[0]
    barrier = threading.Barrier(2, timeout=30)
    real = indicators.snapshot

    def counted_together(*args, **kwargs):
        data = real(*args, **kwargs)
        barrier.wait()
        return data

    monkeypatch.setattr(indicators, "snapshot", counted_together)
    starts = [start, add_months(start, -3)]
    done, errors = _race(tenant, lambda i: services.create_draft(period_start=starts[i], by=None, today=today))
    assert len(done) == 1 and len(errors) == 1 and "periods never overlap" in errors[0].message_dict["period_start"][0]
    with tenant_context(tenant):
        assert ProgramEvaluation.objects.count() == 1


@needs_postgres
def test_the_services_under_the_policies(world, today, other_tenant, manager):
    with tenant_context(other_tenant):
        theirs = draft(today=today)
    as_app_role()
    ev = draft(by=manager, today=today)
    assert ev.number == theirs.number and ProgramEvaluation.objects.get() == ev
    services.refresh(ev, by=manager, today=today)
    services.update_text(ev, "summary", "Under the policies", by=manager)
    services.add_objective(ev, kind=ObjectiveKind.NEXT, text="Cut MTTR", by=manager)
    services.send_to_committee(ev, by=manager, today=today)
    services.record_approval(ev, body=Body.EOC, meeting_on=today, by=manager, today=today)
    new = services.amend(ev, by=manager, today=today)
    assert new.snapshot.data == ProgramEvaluation.objects.get(pk=ev.pk).snapshot.data
    f = figures(ev.snapshot.data)
    assert f["pm_high"]["den"] == 3 and f["incidents_recorded"]["value"] == 2
    assert indicators.cut(ev.snapshot.data, manager)["left_out"] == []
