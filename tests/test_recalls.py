"""Recall disposition services (slice 6): status moves, the recall work-order batch, progress, summaries, and the grouped lists."""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from apps.credentials.models import Credential
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel
from apps.recalls import services as rc
from apps.recalls.models import Alert, AlertMatch
from apps.tenants.context import tenant_context
from apps.workorders import services as wo_services
from apps.workorders.models import Priority, Source, WorkOrder, WoStatus, WoType

S = AlertMatch.Status
TODAY = date.today()


@pytest.fixture
def pumps(ctx, dept, pump_model, pump):
    """Three active pumps (ICU 2, ED 1) and one retired."""
    ed = Department.objects.create(name="ED")
    Asset.objects.create(tag="CE-10003", device_model=pump_model, department=dept)
    Asset.objects.create(tag="CE-10004", device_model=pump_model, department=ed)
    Asset.objects.create(tag="CE-10005", device_model=pump_model, department=ed, status=AssetStatus.RETIRED)
    return list(Asset.objects.filter(device_model=pump_model).exclude(status=AssetStatus.RETIRED))


def make_alert(external_id, manufacturer="BD", terms=("Alaris",), days_ago=1, classification="Class II"):
    return Alert.objects.create(source=Alert.Source.FDA, external_id=external_id, classification=classification, manufacturer=manufacturer,
                                product="Pump", model_terms=list(terms), title=f"Notice {external_id}", published_on=TODAY - timedelta(days=days_ago))


def force(match, status):
    """Put a match into a status directly; tests of the rules start from every state, not only the ones the workflow reaches."""
    match.status = status
    match.save()
    return match


# --- transitions --------------------------------------------------------------------------------------

@pytest.mark.parametrize("from_status,to_status", [(f, t) for f, tos in rc.ALLOWED_TRANSITIONS.items() for t in sorted(tos)])
def test_allowed_transitions(pump_recall, from_status, to_status):
    force(pump_recall, from_status)
    rc.set_status(pump_recall, to_status)
    pump_recall.refresh_from_db()
    assert pump_recall.status == to_status


@pytest.mark.parametrize("from_status,to_status", [(S.NEEDS_ACTION, S.CLOSED), (S.CLOSED, S.IN_PROGRESS), (S.NOT_AFFECTED, S.CLOSED),
                                                   (S.IN_PROGRESS, S.NEEDS_ACTION), (S.NEEDS_ACTION, "bogus"), (S.NEEDS_ACTION, None)])
def test_disallowed_transitions(pump_recall, from_status, to_status):
    force(pump_recall, from_status)
    with pytest.raises(ValidationError, match="Cannot move FDA Z-TEST-1"):
        rc.set_status(pump_recall, to_status)
    pump_recall.refresh_from_db()
    assert pump_recall.status == from_status and pump_recall.closed_on is None


def test_closing_sets_closed_on_and_the_default_note(pump_recall, make_user):
    kim = make_user("manager", username="kim")
    kim.first_name, kim.last_name = "Kim", "Alvarez"
    when = date(2026, 9, 26)
    force(pump_recall, S.IN_PROGRESS)
    rc.set_status(pump_recall, S.CLOSED, by=kim, today=when)
    assert pump_recall.closed_on == when and pump_recall.disposition_note == "Closed Sep 26, 2026 by Kim Alvarez"

    other = force(AlertMatch.objects.create(alert=make_alert("Z-2"), device_model=pump_recall.device_model), S.UNDER_REVIEW)
    rc.set_status(other, S.NOT_AFFECTED, today=when)  # no user: no "by"
    assert other.closed_on == when and other.disposition_note == "Reviewed, not affected Sep 26, 2026"


def test_a_given_note_is_kept_verbatim(pump_recall):
    rc.set_status(pump_recall, S.NOT_AFFECTED, note="  Serial range not in our fleet. ")
    assert pump_recall.disposition_note == "Serial range not in our fleet." and pump_recall.closed_on == TODAY


def test_reopening_clears_closed_on_and_keeps_the_note(pump_recall):
    rc.set_status(pump_recall, S.NOT_AFFECTED, note="Checked lots")
    rc.set_status(pump_recall, S.UNDER_REVIEW)
    pump_recall.refresh_from_db()
    assert pump_recall.status == S.UNDER_REVIEW and pump_recall.closed_on is None and pump_recall.disposition_note == "Checked lots"
    rc.set_status(pump_recall, S.IN_PROGRESS)
    assert pump_recall.closed_on is None


# --- recall work orders ---------------------------------------------------------------------------------

def test_create_recall_work_orders_one_per_active_device_assigned_to_credentialed_technician(pump_recall, pumps, techs, make_user):
    by = make_user("manager")
    assert rc.create_recall_work_orders(pump_recall, by=by).created == 3
    wos = list(WorkOrder.objects.filter(alert=pump_recall.alert).order_by("asset__tag"))
    assert [w.asset.tag for w in wos] == ["CE-10002", "CE-10003", "CE-10004"]  # the retired CE-10005 is skipped
    for w in wos:
        assert (w.type, w.priority, w.source, w.requester, w.status) == (WoType.RECALL, Priority.HIGH, Source.RECALL, "Recall coordinator", WoStatus.OPEN)
        assert w.due_on == TODAY + timedelta(days=14) and w.estimated_hours == Decimal("0.5") and w.created_by == by
        assert w.problem == "FDA Z-TEST-1: Keypad membrane may allow fluid ingress."
        assert w.assigned_to == techs["dana"] and not w.vendor_service  # Dana is credentialed and not expiring; Tom's credential expires soon
        assert w.status_history.last().note == "Assigned to Dana Whitfield"
    pump_recall.refresh_from_db()
    assert pump_recall.status == S.IN_PROGRESS and rc.unassigned_recall_work_orders(pump_recall) == 0


def test_create_recall_work_orders_skips_devices_with_an_open_one_and_stays_in_progress(pump_recall, pumps, techs):
    assert rc.create_recall_work_orders(pump_recall).created == 3
    assert rc.create_recall_work_orders(pump_recall).created == 0
    assert WorkOrder.objects.filter(alert=pump_recall.alert).count() == 3
    # a device whose recall work order was cancelled gets a fresh one
    wo = WorkOrder.objects.get(alert=pump_recall.alert, asset__tag="CE-10004")
    wo_services.change_status(wo, WoStatus.CANCELLED)
    assert rc.create_recall_work_orders(pump_recall).created == 1
    assert WorkOrder.objects.filter(alert=pump_recall.alert, asset__tag="CE-10004", status=WoStatus.OPEN).count() == 1
    pump_recall.refresh_from_db()
    assert pump_recall.status == S.IN_PROGRESS


def test_create_recall_work_orders_leaves_unassigned_without_a_credentialed_technician(pump_recall, pumps):
    assert rc.create_recall_work_orders(pump_recall).created == 3
    assert rc.unassigned_recall_work_orders(pump_recall) == 3
    assert not WorkOrder.objects.filter(alert=pump_recall.alert).exclude(assigned_to=None).exists()


def test_create_recall_work_orders_includes_the_action_and_starts_from_under_review(pump_recall, pumps):
    pump_recall.alert.action = "Inspect keypad; replace per service bulletin"
    pump_recall.alert.save()
    rc.set_status(pump_recall, S.UNDER_REVIEW)
    assert rc.create_recall_work_orders(pump_recall).created == 3
    assert WorkOrder.objects.filter(alert=pump_recall.alert).first().problem == ("FDA Z-TEST-1: Keypad membrane may allow fluid ingress. "
                                                                                  "Inspect keypad; replace per service bulletin")
    assert pump_recall.status == S.IN_PROGRESS


def test_create_recall_work_orders_rejects_no_devices_and_closed_matches(ctx, vent_model, pump_recall, pumps):
    empty = AlertMatch.objects.create(alert=make_alert("Z-3", manufacturer="Hamilton Medical", terms=("Hamilton",)), device_model=vent_model)
    with pytest.raises(ValidationError, match="No active devices match FDA Z-3"):
        rc.create_recall_work_orders(empty)
    for done in (S.CLOSED, S.NOT_AFFECTED):
        force(pump_recall, done)
        with pytest.raises(ValidationError, match="Reopen FDA Z-TEST-1 before creating work orders"):
            rc.create_recall_work_orders(pump_recall)
    assert not WorkOrder.objects.exists()
    rc.set_status(pump_recall, S.UNDER_REVIEW)  # reopened: the batch is allowed again
    assert rc.create_recall_work_orders(pump_recall).created == 3


def test_progress_counts_completed_and_closed_work_orders(pump_recall, pumps):
    assert rc.progress(pump_recall) == {"total": 3, "completed": 0, "pct": 0.0}  # devices affected, none done yet
    rc.create_recall_work_orders(pump_recall)
    a, b, c = WorkOrder.objects.filter(alert=pump_recall.alert).order_by("asset__tag")
    for w in (a, b):
        wo_services.change_status(w, WoStatus.IN_PROGRESS)
        wo_services.change_status(w, WoStatus.COMPLETED)
    wo_services.change_status(a, WoStatus.CLOSED)
    wo_services.change_status(c, WoStatus.CANCELLED)  # the bar counts devices: a cancelled work order leaves its device not done
    p = rc.progress(pump_recall)
    assert (p["total"], p["completed"]) == (3, 2) and round(p["pct"], 1) == 66.7
    # a repair on the same device for another reason does not count, nor does a second recall work order for a done device
    wo_services.create_work_order(asset=a.asset, type=WoType.REPAIR, priority=Priority.NORMAL, problem="Door latch")
    assert rc.progress(pump_recall) == p


# --- summaries and lists --------------------------------------------------------------------------------

def test_department_summary_lists_the_top_four_departments(pump_recall, pumps, pump_model):
    assert rc.department_summary(pump_recall) == "ICU 2, ED 1"
    for name in ("Telemetry", "Radiology", "NICU"):
        Asset.objects.create(tag=f"CE-{name}", device_model=pump_model, department=Department.objects.create(name=name))
    assert rc.department_summary(pump_recall) == "ICU 2, ED 1, NICU 1, Radiology 1"  # ties break by name; Telemetry is fifth


def test_group_counts_and_filter_matches(ctx, pump_recall, pump_model, pumps):
    old = AlertMatch.objects.create(alert=make_alert("Z-OLD", days_ago=40), device_model=pump_model)
    new = AlertMatch.objects.create(alert=make_alert("Z-NEW", days_ago=0), device_model=pump_model)
    AlertMatch.objects.create(alert=make_alert("Z-ALSO", days_ago=0), device_model=pump_model)  # same day as Z-NEW: sorts by number
    done = AlertMatch.objects.create(alert=make_alert("Z-DONE", days_ago=10), device_model=pump_model)
    na = AlertMatch.objects.create(alert=make_alert("Z-NA", days_ago=20), device_model=pump_model)
    force(old, S.UNDER_REVIEW)
    force(new, S.IN_PROGRESS)
    force(done, S.CLOSED)
    force(na, S.NOT_AFFECTED)
    assert rc.group_counts() == {"all": 6, "action": 3, "progress": 1, "closed": 2}
    ids = [m.alert.external_id for m in rc.filter_matches("all")]
    assert ids == ["Z-ALSO", "Z-NEW", "Z-TEST-1", "Z-DONE", "Z-NA", "Z-OLD"]  # newest first, then by number
    assert [m.alert.external_id for m in rc.filter_matches("action")] == ["Z-ALSO", "Z-TEST-1", "Z-OLD"]
    assert [m.alert.external_id for m in rc.filter_matches("progress")] == ["Z-NEW"]
    assert [m.alert.external_id for m in rc.filter_matches("closed")] == ["Z-DONE", "Z-NA"]
    assert [m.devices for m in rc.filter_matches("progress")] == [3]  # active devices only
    assert rc.filter_matches("nonsense").count() == 6


def test_filter_matches_is_tenant_scoped(ctx, pump_recall, other_tenant):
    from apps.tenants.context import tenant_context

    with tenant_context(other_tenant):
        dm = DeviceModel.objects.create(manufacturer="BD", model="Alaris 8015 PCU", description="Pump", category="Infusion pumps")
        AlertMatch.objects.create(alert=pump_recall.alert, device_model=dm)
        assert rc.group_counts()["all"] == 1 and [m.device_model_id for m in rc.filter_matches()] == [dm.id]
    assert rc.group_counts()["all"] == 1 and [m.id for m in rc.filter_matches()] == [pump_recall.id]


def test_rematch_picks_up_a_device_model_added_after_the_alert(ctx, pump_recall):
    assert rc.rematch() == 0
    DeviceModel.objects.create(manufacturer="BD", model="Alaris 8100", description="Pump module", category="Infusion pumps")
    pump_recall.alert.model_terms = ["Alaris"]
    pump_recall.alert.save()
    assert rc.rematch() == 1
    assert AlertMatch.objects.filter(alert=pump_recall.alert).count() == 2
    assert rc.rematch() == 0


def test_feed_imported_at(db, ctx):
    assert rc.feed_imported_at() is None
    make_alert("Z-9")
    assert rc.feed_imported_at() is not None


# --- review follow-ups ---------------------------------------------------------------------------------

def test_every_move_outside_the_table_is_rejected(pump_recall):
    for from_status in S:
        for to_status in list(S) + ["bogus", None]:
            if to_status in rc.ALLOWED_TRANSITIONS[from_status]:
                continue
            force(pump_recall, from_status)
            with pytest.raises(ValidationError):
                rc.set_status(pump_recall, to_status)
            pump_recall.refresh_from_db()
            assert pump_recall.status == from_status


def test_completed_devices_are_not_redone_by_a_second_batch(pump_recall, pumps):
    assert rc.create_recall_work_orders(pump_recall).created == 3
    done = WorkOrder.objects.get(alert=pump_recall.alert, asset__tag="CE-10003")
    wo_services.change_status(done, WoStatus.IN_PROGRESS)
    wo_services.change_status(done, WoStatus.COMPLETED)
    assert rc.create_recall_work_orders(pump_recall).created == 0
    assert WorkOrder.objects.filter(alert=pump_recall.alert, asset__tag="CE-10003").count() == 1


def test_batch_reports_how_many_found_no_technician(pump_recall, pumps, techs):
    assert rc.create_recall_work_orders(pump_recall) == (3, 0)
    Credential.objects.all().delete()
    rc.set_status(pump_recall, S.UNDER_REVIEW)
    WorkOrder.objects.get(alert=pump_recall.alert, asset__tag="CE-10004").delete()
    assert rc.create_recall_work_orders(pump_recall) == (1, 1)


def test_rematch_creates_nothing_for_another_tenant(ctx, pump_recall, other_tenant):
    with tenant_context(other_tenant):
        d = Department.objects.create(name="ICU")
        DeviceModel.objects.create(manufacturer="BD", model="Alaris 8015 PCU", description="Pump", category="Infusion pumps")
        Asset.objects.create(tag="THEIRS-1", device_model=DeviceModel.objects.get(model="Alaris 8015 PCU"), department=d)
    assert rc.rematch() == 0  # this tenant already has its match
    assert AlertMatch.unscoped.filter(tenant=other_tenant).count() == 0  # unscoped: proves the other tenant was untouched
