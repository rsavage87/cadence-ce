"""The AEM program (slice 14, apps.pm.aem): the evidence, proposing, the committee's decision, withdrawing, ending and the pull-in of
next PMs, the hook from update_device_model, concurrency, the demo seed, and tenant isolation."""
import json
from datetime import date, datetime, time, timedelta
from io import StringIO

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.equipment import services as eq
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel, RiskClass
from apps.facility.services import update_settings
from apps.pm import aem
from apps.pm.dates import add_months
from apps.pm.models import AemDecision, AemStatus
from apps.recalls.models import Alert, AlertMatch
from apps.tenants.context import tenant_context
from apps.tenants.models import Tenant
from apps.workorders.models import WorkOrder, WorkOrderStatusHistory, WoStatus, WoType
from apps.workorders.services import change_status, create_work_order

TODAY = date.today()
SINCE = add_months(TODAY, -36)


def years_ago(n: int) -> date:
    return add_months(TODAY, -12 * n)


def months_ago(n: int) -> date:
    return add_months(TODAY, -n)


@pytest.fixture
def monitor(ctx):
    """High risk, OEM 12 months, no AEM: high-risk models stay eligible."""
    return DeviceModel.objects.create(manufacturer="Philips", model="IntelliVue MX750", description="Patient monitor", category="Patient monitoring",
                                      risk_class=RiskClass.HIGH, oem_pm_interval_months=12)


def device(dm, dept, tag, *, installed_on=None, last_pm_on=None, next_pm_on=None, status=AssetStatus.IN_SERVICE):
    return Asset.objects.create(tag=tag, device_model=dm, department=dept, installed_on=installed_on, last_pm_on=last_pm_on,
                                next_pm_on=next_pm_on or TODAY + timedelta(days=30), status=status)


@pytest.fixture
def fleet(monitor, dept):
    """Three monitors: one installed four years ago (the history the policy needs), one last year, one with no install date."""
    return [device(monitor, dept, "M-1", installed_on=years_ago(4)), device(monitor, dept, "M-2", installed_on=years_ago(1)), device(monitor, dept, "M-3")]


@pytest.fixture
def people(make_user):
    return {"tech": make_user("technician"), "manager": make_user("manager"), "director": make_user("director")}


def propose(dm, people, months=24, **kw):
    return aem.propose(dm, interval_months=months, rationale=kw.pop("rationale", "Few failures in three years."), by=kw.pop("by", people["tech"]), **kw)


def approve(decision, people, **kw):
    return aem.approve(decision, by=kw.pop("by", people["manager"]), decided_on=kw.pop("decided_on", TODAY), note=kw.pop("note", "EMC minutes, item 4"), **kw)


def in_force(fleet, monitor, people, months=24):
    d = approve(propose(monitor, people, months), people)
    # The services work on their own locked copy of the row. update_device_model saves every field of the copy it is given, so a
    # copy read before the approval would write the old interval back: read it again, as a request would.
    monitor.refresh_from_db()
    return d


def refused(fn, *args, match="", field=None, **kwargs):
    with pytest.raises(ValidationError) as e:
        fn(*args, **kwargs)
    text = " ".join(e.value.message_dict[field]) if field else " ".join(e.value.messages)
    assert match in text, text
    return text


def work_order(asset, type, opened, *, done=None, due=None):
    problem = "Alarm fault" if type == WoType.REPAIR else "Scheduled PM"
    wo = create_work_order(asset=asset, type=type, priority="normal", problem=problem, opened_on=opened, due_on=due)
    if done:
        change_status(wo, WoStatus.IN_PROGRESS, as_of=opened)
        change_status(wo, WoStatus.COMPLETED, as_of=done)
    return wo


def retire(asset, on: date):
    """Retire through the service, then date the retirement in the device's history (the evidence reads it from there)."""
    eq.set_status(asset, AssetStatus.RETIRED)
    when = timezone.make_aware(datetime.combine(on, time(12)))
    Asset.history.filter(id=asset.id, status=AssetStatus.RETIRED).update(history_date=when)


# --- the evidence --------------------------------------------------------------------------------------------------------------


def test_evidence_counts_the_models_own_history_over_three_years(fleet, monitor, vent):
    old, recent, _undated = fleet
    work_order(old, WoType.REPAIR, TODAY - timedelta(days=100))
    work_order(recent, WoType.REPAIR, TODAY - timedelta(days=10))
    work_order(old, WoType.REPAIR, SINCE - timedelta(days=1))  # before the window
    change_status(work_order(old, WoType.REPAIR, TODAY - timedelta(days=5)), WoStatus.CANCELLED)  # never done
    work_order(vent, WoType.REPAIR, TODAY - timedelta(days=5))  # another model
    work_order(old, WoType.PM, TODAY - timedelta(days=200), done=TODAY - timedelta(days=195), due=TODAY - timedelta(days=190))  # on time
    work_order(recent, WoType.PM, TODAY - timedelta(days=100), done=TODAY - timedelta(days=90), due=TODAY - timedelta(days=95))  # late
    work_order(old, WoType.PM, TODAY - timedelta(days=1))  # still open: not completed
    work_order(old, WoType.RECALL, TODAY - timedelta(days=20))
    alert = Alert.objects.create(source=Alert.Source.FDA, external_id="Z-AEM-1", manufacturer="Philips", product="MX750", title="Battery")
    done_alert = Alert.objects.create(source=Alert.Source.FDA, external_id="Z-AEM-2", manufacturer="Philips", product="MX750", title="Label")
    AlertMatch.objects.create(alert=alert, device_model=monitor)
    AlertMatch.objects.create(alert=done_alert, device_model=monitor, status=AlertMatch.Status.CLOSED)

    ev = aem.evidence(monitor, TODAY)
    assert json.loads(json.dumps(ev)) == ev  # stored as JSON on the proposal
    days = (TODAY - SINCE).days + (TODAY - years_ago(1)).days  # the undated monitor was added today: no time in use yet
    assert (ev["as_of"], ev["since"], ev["years"]) == (TODAY.isoformat(), SINCE.isoformat(), 3)
    assert (ev["devices_active"], ev["devices_retired"], ev["devices_undated"]) == (3, 0, 1)
    assert ev["device_years"] == round(days / 365.25, 1)
    assert ev["repairs"] == 2 and ev["repairs_per_device_year"] == round(2 / ev["device_years"], 2)
    assert (ev["pm_completed"], ev["pm_on_time"], ev["pm_on_time_pct"]) == (2, 1, 50)
    assert (ev["recall_work_orders"], ev["open_recalls"]) == (1, 1)
    assert ev["oldest_install"] == years_ago(4).isoformat() and ev["history_years"] == round((TODAY - years_ago(4)).days / 365.25, 1)
    assert ev["enough_history"] is True


def test_retired_devices_count_for_the_years_they_were_in_use(monitor, dept):
    retire(device(monitor, dept, "M-7", installed_on=years_ago(5)), years_ago(1))
    retire(device(monitor, dept, "M-8", installed_on=years_ago(9)), years_ago(4))  # out of use before the window opened
    ev = aem.evidence(monitor, TODAY)
    assert (ev["devices_active"], ev["devices_retired"]) == (0, 1)
    assert ev["device_years"] == round((years_ago(1) - SINCE).days / 365.25, 1)
    assert ev["oldest_install"] == years_ago(5).isoformat() and ev["enough_history"]  # in use through the window: its history counts


def test_enough_history_means_a_device_installed_three_years_ago(monitor, dept):
    device(monitor, dept, "M-1", installed_on=SINCE + timedelta(days=1))
    assert not aem.evidence(monitor, TODAY)["enough_history"]
    device(monitor, dept, "M-2", installed_on=SINCE)
    assert aem.evidence(monitor, TODAY)["enough_history"]


def test_a_model_without_install_dates_or_devices_has_no_history(monitor, dept):
    ev = aem.evidence(monitor, TODAY)
    assert "This model has no devices on record" in aem.history_refusal(ev)
    device(monitor, dept, "M-1")
    ev = aem.evidence(monitor, TODAY)
    assert (ev["oldest_install"], ev["history_years"], ev["enough_history"], ev["device_years"]) == (None, None, False, 0.0)
    assert ev["repairs_per_device_year"] is None and ev["pm_on_time_pct"] is None
    assert "None of this model's devices has an install date on file" in aem.history_refusal(ev)


def test_evidence_counts_only_this_facilitys_rows(fleet, monitor, other_tenant):
    before = aem.evidence(monitor, TODAY)
    # Rows of another facility pointing at this model (impossible through the services; unscoped to prove the scoping holds)
    their_dept = Department.unscoped.create(tenant=other_tenant, name="Their ICU")
    theirs = Asset.unscoped.create(tenant=other_tenant, tag="X-1", device_model=monitor, department=their_dept, installed_on=years_ago(10),
                                   next_pm_on=TODAY)
    with tenant_context(other_tenant):
        work_order(theirs, WoType.REPAIR, TODAY - timedelta(days=3))
    assert aem.evidence(monitor, TODAY) == before


# --- proposing -----------------------------------------------------------------------------------------------------------------


def test_a_proposal_keeps_the_case_and_the_evidence(fleet, monitor, people):
    d = propose(monitor, people, "24", rationale="  Few failures in three years.  ")
    d.refresh_from_db()
    assert (d.status, d.interval_months, d.oem_interval_months, d.proposed_by, d.proposed_on) == (AemStatus.PROPOSED, 24, 12, people["tech"], TODAY)
    assert d.rationale == "Few failures in three years." and d.evidence == aem.evidence(monitor, TODAY)
    assert d.history.first().history_change_reason == "Proposed"
    monitor.refresh_from_db()
    assert monitor.aem_interval_months is None and monitor.pm_interval_months == 12  # nothing changes until the committee approves


def test_life_support_is_never_proposed(ctx, dept, vent_model, people):
    device(vent_model, dept, "V-1", installed_on=years_ago(5))
    refused(propose, vent_model, people, 12, match="Life-support devices are excluded from AEM by policy")
    assert aem.propose_blocker(vent_model, TODAY) == aem.LIFE_SUPPORT_REFUSAL
    assert not AemDecision.objects.exists()


def test_one_open_proposal_per_model(fleet, monitor, people):
    propose(monitor, people)
    refused(propose, monitor, people, 18, match="An AEM proposal for this model is open (24 months")
    assert aem.propose_blocker(monitor, TODAY).startswith("An AEM proposal for this model is open")


@pytest.mark.parametrize("value, message", [
    (0, "1 to 120"), (121, "1 to 120"), ("abc", "whole number"), ("1.5", "whole number"), ("-3", "whole number"), (True, "whole number"),
    ("", "Enter the proposed interval"), (None, "Enter the proposed interval"), (12, "That is the OEM interval (12 months)"),
])
def test_the_interval_is_a_whole_number_that_differs_from_the_oem(fleet, monitor, people, value, message):
    refused(propose, monitor, people, value, match=message, field="interval_months")


def test_the_interval_differs_from_the_one_in_force(fleet, monitor, people):
    in_force(fleet, monitor, people, 24)
    refused(propose, monitor, people, 24, match="This model already runs on 24 months", field="interval_months")
    assert propose(monitor, people, 6).interval_months == 6  # shorter than the OEM is allowed too


def test_an_interval_on_file_without_approval_can_be_ratified_as_it_is(ctx, dept, pump_model, people):
    """The fixture's 18 months were set before approvals were recorded: the committee can approve the interval in use, and nothing moves."""
    p1 = device(pump_model, dept, "P-1", installed_on=years_ago(4), last_pm_on=months_ago(10), next_pm_on=add_months(months_ago(10), 18))
    d = approve(propose(pump_model, people, 18), people)
    pump_model.refresh_from_db()
    p1.refresh_from_db()
    assert d.status == AemStatus.APPROVED and d.devices_moved == 0 and pump_model.aem_interval_months == 18
    assert p1.next_pm_on == add_months(months_ago(10), 18)
    assert aem.in_force(pump_model) == d


def test_the_rationale_is_required_and_bounded(fleet, monitor, people):
    refused(propose, monitor, people, rationale="   ", match="Make the case", field="rationale")
    refused(propose, monitor, people, rationale="x" * (aem.RATIONALE_MAX + 1), match=f"Keep it to {aem.RATIONALE_MAX} characters", field="rationale")
    assert propose(monitor, people, rationale="x" * aem.RATIONALE_MAX).rationale == "x" * aem.RATIONALE_MAX


def test_the_policy_needs_three_years_of_history_in_its_own_words(monitor, dept, people):
    device(monitor, dept, "M-1", installed_on=years_ago(2))
    update_settings(policy_aem="EMC sign-off on three years of service records.")
    text = refused(propose, monitor, people)
    assert text.startswith("The facility's AEM policy: EMC sign-off on three years of service records. The oldest device of this model on record")
    # A date, not a rounded figure: "3.0 years of history; a proposal needs 3 years" could happen a few days short of three years.
    assert f"installed {aem._day(years_ago(2))}, so the model has 3 years of history from {aem._day(years_ago(-1))}." in text
    assert aem.propose_blocker(monitor, TODAY) == text


# --- the committee's decision -----------------------------------------------------------------------------------------------


def test_approval_puts_the_interval_in_force_and_a_longer_one_moves_no_pm(fleet, monitor, people):
    next_pms = {a.pk: a.next_pm_on for a in fleet}
    d = approve(propose(monitor, people), people, decided_on=TODAY - timedelta(days=0), note="  EMC minutes, item 4 ")
    d.refresh_from_db()
    assert (d.status, d.decided_by, d.decided_on, d.decision_note) == (AemStatus.APPROVED, people["manager"], TODAY, "EMC minutes, item 4")
    monitor.refresh_from_db()
    assert monitor.aem_interval_months == 24 and monitor.pm_interval_months == 24
    assert monitor.history.first().history_change_reason == "AEM approved: 24 months"
    assert {a.pk: a.next_pm_on for a in Asset.objects.filter(device_model=monitor)} == next_pms  # applies from each device's next PM
    assert aem.in_force(monitor) == d and aem.open_proposal(monitor) is None


def test_the_approval_returns_the_devices_moved(fleet, monitor, people):
    assert approve(propose(monitor, people), people).devices_moved == 0


def test_the_proposer_never_decides_their_own_case(fleet, monitor, people):
    d = propose(monitor, people, by=people["director"])  # the director holds PM Approve too
    refused(approve, d, people, by=people["director"], match="You proposed this change. The committee signs off on someone else's case")
    refused(aem.reject, d, by=people["director"], decided_on=TODAY, note="No", match="You proposed this change")
    assert aem.decide_blocker(d, people["director"]).startswith("You proposed this change") and aem.decide_blocker(d, people["manager"]) == ""
    assert approve(d, people).status == AemStatus.APPROVED  # someone else signs off


def test_a_decision_is_recorded_by_someone(fleet, monitor, people):
    refused(approve, propose(monitor, people), people, by=None, match="recorded by a signed-in approver")


def test_the_committee_date(fleet, monitor, people):
    d = propose(monitor, people, today=TODAY - timedelta(days=10))
    refused(approve, d, people, decided_on=None, match="Enter the date of the committee's meeting", field="decided_on")
    refused(approve, d, people, decided_on=TODAY + timedelta(days=1), match="cannot be dated in the future", field="decided_on")
    refused(approve, d, people, decided_on=TODAY - timedelta(days=11), match="before the proposal was made", field="decided_on")
    assert approve(d, people, decided_on=TODAY - timedelta(days=10)).decided_on == TODAY - timedelta(days=10)


def test_the_minutes_reference_is_required(fleet, monitor, people):
    d = propose(monitor, people)
    refused(approve, d, people, note=" ", match="minutes reference", field="note")
    refused(aem.reject, d, by=people["manager"], decided_on=TODAY, note="", match="reason or minutes reference", field="note")
    refused(approve, d, people, note="x" * (aem.NOTE_MAX + 1), match="Keep it to", field="note")


def test_only_an_open_proposal_is_decided(fleet, monitor, people):
    d = propose(monitor, people)
    stale = AemDecision.objects.get(pk=d.pk)  # a second approver's copy, read before the first approval
    approve(d, people)
    refused(approve, stale, people, by=people["director"], match="This proposal is no longer open: it was approved.")
    refused(aem.reject, stale, by=people["director"], decided_on=TODAY, note="No", match="no longer open")
    assert AemDecision.objects.filter(status=AemStatus.APPROVED).count() == 1


def test_approval_checks_life_support_and_the_history_again(fleet, monitor, people):
    d = propose(monitor, people)
    DeviceModel.objects.filter(pk=monitor.pk).update(risk_class=RiskClass.LIFE_SUPPORT)  # behind the services' back (admin)
    refused(approve, d, people, match="Life-support devices are excluded from AEM by policy")
    DeviceModel.objects.filter(pk=monitor.pk).update(risk_class=RiskClass.HIGH)
    Asset.objects.filter(pk=fleet[0].pk).update(installed_on=years_ago(1))  # the install date was a typo
    refused(approve, d, people, match="The facility's AEM policy: Equipment Management Committee, 3-year failure history required.")
    assert AemDecision.objects.get(pk=d.pk).status == AemStatus.PROPOSED


def test_approval_refuses_a_proposal_the_oem_interval_caught_up_with(fleet, monitor, people):
    d = propose(monitor, people)
    eq.update_device_model(monitor, oem_pm_interval_months=24)
    refused(approve, d, people, match="The OEM interval is now 24 months, the same as this proposal. Withdraw it instead.")


def test_a_new_approval_ends_the_interval_in_force(fleet, monitor, people):
    first = in_force(fleet, monitor, people)
    second = approve(propose(monitor, people, 18), people, by=people["director"])
    first.refresh_from_db()
    assert (first.status, first.ended_by, first.ended_on) == (AemStatus.ENDED, people["director"], TODAY)
    assert first.end_reason == f"Replaced by the 18-month interval approved on {TODAY:%b} {TODAY.day}, {TODAY.year}."
    monitor.refresh_from_db()
    assert second.status == AemStatus.APPROVED and monitor.aem_interval_months == 18 and aem.in_force(monitor) == second


def test_a_shorter_interval_pulls_next_pms_in_and_moves_the_open_pm_work_order(fleet, monitor, people, dept):
    in_force(fleet, monitor, people)  # 24 months
    far = device(monitor, dept, "M-4", last_pm_on=months_ago(14), next_pm_on=add_months(months_ago(14), 24))
    late = device(monitor, dept, "M-5", last_pm_on=months_ago(20), next_pm_on=add_months(months_ago(20), 24))
    near = device(monitor, dept, "M-6", last_pm_on=months_ago(2), next_pm_on=add_months(months_ago(2), 12))
    pm_wo = work_order(far, WoType.PM, TODAY, due=far.next_pm_on)
    d = approve(propose(monitor, people, 18), people)
    for a in (far, late, near):
        a.refresh_from_db()
    assert far.next_pm_on == add_months(months_ago(14), 18)  # 4 months from now, not 10
    assert late.next_pm_on == TODAY  # 18 months after its last PM has passed: due now
    assert near.next_pm_on == add_months(months_ago(2), 12)  # already inside the new interval
    pm_wo.refresh_from_db()
    assert pm_wo.due_on == far.next_pm_on and WorkOrderStatusHistory.objects.filter(work_order=pm_wo, note__startswith="Due date moved").exists()
    # the two above, and M-1: no PM on record, installed four years ago, so 18 months from its install date has passed (due now)
    assert d.devices_moved == 3 and Asset.objects.get(tag="M-1").next_pm_on == TODAY
    assert Asset.objects.get(tag="M-3").next_pm_on == TODAY + timedelta(days=30)  # neither date on file: 18 months from today is later


def test_an_approval_replaces_an_interval_on_file_without_a_recorded_approval(ctx, dept, pump_model, people):
    device(pump_model, dept, "P-1", installed_on=years_ago(4))
    assert aem.is_legacy(pump_model, aem.in_force(pump_model))
    d = approve(propose(pump_model, people, 24), people)
    pump_model.refresh_from_db()
    assert pump_model.aem_interval_months == 24 and AemDecision.objects.count() == 1 and d.status == AemStatus.APPROVED
    assert pump_model.history.first().history_change_reason == "AEM approved: 24 months (replacing one on file without a recorded approval)"
    assert not aem.is_legacy(pump_model, d)


def test_rejection_leaves_the_interval(fleet, monitor, people):
    d = aem.reject(propose(monitor, people), by=people["manager"], decided_on=TODAY, note="Wait for another year of data")
    d.refresh_from_db()
    assert (d.status, d.decided_by, d.decided_on, d.decision_note) == (AemStatus.REJECTED, people["manager"], TODAY, "Wait for another year of data")
    monitor.refresh_from_db()
    assert monitor.aem_interval_months is None
    assert propose(monitor, people, 18).status == AemStatus.PROPOSED  # a new case can be made


# --- withdrawing ----------------------------------------------------------------------------------------------------------


def test_withdrawing_closes_the_proposal_without_a_decision(fleet, monitor, people):
    d = aem.withdraw(propose(monitor, people), by=people["tech"])
    assert (d.status, d.ended_by, d.ended_on, d.end_reason, d.decided_on) == (AemStatus.WITHDRAWN, people["tech"], TODAY, "Withdrawn by the proposer.", None)
    refused(aem.withdraw, d, by=people["tech"], match="no longer open: it was withdrawn")
    other = aem.withdraw(propose(monitor, people, 18), by=people["manager"])
    assert other.end_reason == "Withdrawn." and AemDecision.objects.filter(status=AemStatus.WITHDRAWN).count() == 2


# --- ending ---------------------------------------------------------------------------------------------------------------


def test_ending_goes_back_to_the_oem_interval_and_pulls_next_pms_in(fleet, monitor, people, dept):
    d = in_force(fleet, monitor, people)
    long = device(monitor, dept, "M-4", last_pm_on=months_ago(14), next_pm_on=add_months(months_ago(14), 24))
    installed = device(monitor, dept, "M-5", installed_on=months_ago(6), next_pm_on=add_months(months_ago(6), 24))
    overdue = device(monitor, dept, "M-6", last_pm_on=months_ago(30), next_pm_on=TODAY - timedelta(days=1))
    retired = device(monitor, dept, "M-7", last_pm_on=months_ago(20), next_pm_on=add_months(months_ago(20), 24))
    eq.set_status(retired, AssetStatus.RETIRED)
    pm_wo = work_order(long, WoType.PM, TODAY - timedelta(days=1), due=long.next_pm_on)
    moved = aem.end(d, by=people["manager"], reason="  Repairs rose at 24 months ")
    d.refresh_from_db()
    assert (d.status, d.ended_by, d.ended_on, d.end_reason) == (AemStatus.ENDED, people["manager"], TODAY, "Repairs rose at 24 months")
    monitor.refresh_from_db()
    assert monitor.aem_interval_months is None and monitor.pm_interval_months == 12
    assert monitor.history.first().history_change_reason == "AEM ended"  # the typed reason stays on the decision (PM View), not the model
    for a in (long, installed, overdue, retired):
        a.refresh_from_db()
    assert long.next_pm_on == TODAY  # 12 months after its last PM has passed
    assert installed.next_pm_on == add_months(months_ago(6), 12)  # from its install date
    assert overdue.next_pm_on == TODAY - timedelta(days=1)  # already earlier: never pushed out
    assert retired.next_pm_on is None
    pm_wo.refresh_from_db()
    assert pm_wo.due_on == TODAY
    fleet_moved = [a for a in fleet if Asset.objects.get(pk=a.pk).next_pm_on != a.next_pm_on]
    assert moved == 2 + len(fleet_moved)


def test_ending_needs_a_reason_and_an_interval_in_force(fleet, monitor, people):
    refused(aem.end, monitor, by=people["manager"], reason="Done", match="This model has no AEM interval in force")
    d = in_force(fleet, monitor, people)
    refused(aem.end, d, by=people["manager"], reason="  ", match="Say why the AEM interval ends", field="reason")
    refused(aem.end, d, by=people["manager"], reason="x" * (aem.REASON_MAX + 1), match="Keep it to", field="reason")
    aem.end(monitor, by=people["manager"], reason="Committee review")  # through the model: the decision in force ends
    d.refresh_from_db()
    assert d.status == AemStatus.ENDED
    refused(aem.end, d, by=people["manager"], reason="Again", match="This AEM interval is no longer in force.")


def test_ending_an_interval_on_file_without_a_recorded_approval(ctx, dept, pump_model, people):
    p = device(pump_model, dept, "P-1", last_pm_on=months_ago(14), next_pm_on=add_months(months_ago(14), 18))
    assert aem.end(pump_model, by=people["manager"], reason="No approval on record") == 1
    pump_model.refresh_from_db()
    p.refresh_from_db()
    assert pump_model.aem_interval_months is None and p.next_pm_on == TODAY
    ended = AemDecision.objects.get()  # the interval had no case: its end is recorded as one, so the reason is kept (PM View)
    assert (ended.status, ended.end_reason, ended.interval_months, ended.ended_by) == (AemStatus.ENDED, "No approval on record", 18, people["manager"])
    assert pump_model.history.first().history_change_reason == "AEM on file without a recorded approval ended"


def test_pull_in_plan_is_what_ending_moves(fleet, monitor, people, dept):
    d = in_force(fleet, monitor, people)
    device(monitor, dept, "M-4", last_pm_on=months_ago(14), next_pm_on=add_months(months_ago(14), 24))
    plan = aem.pull_in_plan(monitor, 12, TODAY)
    assert ("M-4", TODAY) in [(a.tag, due) for a, due in plan] and "M-3" not in [a.tag for a, _due in plan]
    assert aem.end(d, by=people["manager"], reason="Review") == len(plan)


# --- the hook from update_device_model --------------------------------------------------------------------------------------


def test_a_model_scored_into_life_support_leaves_aem(fleet, monitor, people, dept):
    approved = in_force(fleet, monitor, people)
    far = device(monitor, dept, "M-4", last_pm_on=months_ago(14), next_pm_on=add_months(months_ago(14), 24))
    proposal = propose(monitor, people, 18)
    eq.update_device_model(monitor, risk_class="life_support", by=people["director"])
    approved.refresh_from_db()
    proposal.refresh_from_db()
    assert approved.status == AemStatus.ENDED and "life-support devices are excluded from AEM by policy" in approved.end_reason
    assert proposal.status == AemStatus.WITHDRAWN and proposal.end_reason.startswith("The model is now life support")
    assert monitor.aem_interval_months is None  # the caller's copy too
    monitor.refresh_from_db()
    far.refresh_from_db()
    assert monitor.aem_interval_months is None and far.next_pm_on == TODAY


def test_a_legacy_interval_leaves_with_life_support_too(ctx, dept, pump_model):
    eq.update_device_model(pump_model, risk_class=RiskClass.LIFE_SUPPORT)
    pump_model.refresh_from_db()
    assert pump_model.aem_interval_months is None


def test_an_oem_interval_equal_to_the_aem_in_force_is_refused(fleet, monitor, people):
    """It would end a committee decision, which is End AEM's (PM Approve), while the OEM interval is Equipment Edit's: a technician
    could otherwise end an AEM by setting the OEM interval to it and back."""
    d = in_force(fleet, monitor, people)
    eq.update_device_model(monitor, oem_pm_interval_months=6)  # different: nothing happens
    d.refresh_from_db()
    assert d.status == AemStatus.APPROVED
    refused(eq.update_device_model, monitor, oem_pm_interval_months=24, field="oem_pm_interval_months",
            match="This model is on an AEM interval of 24 months. End the AEM on the model's AEM tab first")
    d.refresh_from_db()
    monitor.refresh_from_db()
    assert d.status == AemStatus.APPROVED and monitor.oem_pm_interval_months == 6 and monitor.aem_interval_months == 24


def test_other_model_changes_leave_aem_alone(fleet, monitor, people):
    d = in_force(fleet, monitor, people)
    eq.update_device_model(monitor, description="Bedside monitor", risk_class=RiskClass.MEDIUM)
    d.refresh_from_db()
    monitor.refresh_from_db()
    assert d.status == AemStatus.APPROVED and monitor.aem_interval_months == 24


def test_update_device_model_still_refuses_a_direct_aem_change(fleet, monitor, people):
    in_force(fleet, monitor, people)
    with pytest.raises(ValidationError):
        eq.update_device_model(monitor, aem_interval_months=36)


# --- concurrency and isolation ---------------------------------------------------------------------------------------------


def test_one_proposal_and_one_interval_in_force_per_model_in_the_database(fleet, monitor, people):
    d = in_force(fleet, monitor, people)
    with pytest.raises(IntegrityError), transaction.atomic():
        AemDecision.objects.create(device_model=monitor, interval_months=18, oem_interval_months=12, status=AemStatus.APPROVED, rationale="x",
                                   proposed_on=TODAY)
    propose(monitor, people, 18)
    with pytest.raises(IntegrityError), transaction.atomic():
        AemDecision.objects.create(device_model=monitor, interval_months=30, oem_interval_months=12, rationale="x", proposed_on=TODAY)
    assert aem.in_force(monitor) == d


def test_another_facilitys_decisions_are_invisible(fleet, monitor, people, other_tenant):
    propose(monitor, people)
    with tenant_context(other_tenant):
        assert not AemDecision.objects.exists()
        dept = Department.objects.create(name="Their ICU")
        theirs = DeviceModel.objects.create(manufacturer="GE", model="B650", description="Monitor", category="Monitors")
        Asset.objects.create(tag="THEIRS-1", device_model=theirs, department=dept, installed_on=years_ago(5), next_pm_on=TODAY)
        AemDecision.objects.create(device_model=theirs, interval_months=24, oem_interval_months=12, rationale="x", proposed_on=TODAY)
    assert AemDecision.objects.count() == 1 and AemDecision.objects.get().device_model == monitor
    assert aem.open_proposal(monitor).device_model == monitor


# --- the demo ---------------------------------------------------------------------------------------------------------------


def test_the_demo_has_an_approved_aem_with_its_history(db):
    call_command("seed_demo", stdout=StringIO())
    tenant = Tenant.objects.get(slug="riverside")
    with tenant_context(tenant):
        d = AemDecision.objects.select_related("device_model").get()
        assert d.status == AemStatus.APPROVED and d.device_model.model == "IntelliVue MX750" and d.device_model.risk_class != RiskClass.LIFE_SUPPORT
        assert d.device_model.aem_interval_months == 24 and d.device_model.pm_interval_months == 24
        assert d.proposed_by.username == "dwhitfield@riverside.example" and d.decided_by.username == "rfeldman@riverside.example"
        assert d.proposed_by != d.decided_by and d.proposed_on <= d.decided_on <= date.today()
        assert d.evidence["enough_history"] and d.evidence["as_of"] == d.proposed_on.isoformat()
        assert d.evidence["devices_active"] == Asset.objects.filter(device_model=d.device_model).count()
        assert WorkOrder.objects.filter(asset__device_model=d.device_model).exists()
    call_command("seed_demo", stdout=StringIO())  # a second run is a no-op
    with tenant_context(tenant):
        assert AemDecision.objects.count() == 1 and User.objects.filter(tenant=tenant).count() == 14
