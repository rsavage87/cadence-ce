"""
Slice 30, checks after a repair: the review fixes.

1. Put in use before inspection (equipment.services.use_before_inspection) is refused while a repair holds the waiting device out (its
   checks after repair failed and keep_open held it, or a tagged-out repair is open), in the repair hold's words; the drawer no longer
   offers it (asset_tabs.incoming_banner), the modal answers the hold's words, and the API refuses it too; once the repair is completed
   as checked it is offered again.
2. The API's work order edit never ends a repair's hold: tagged_out is never changed by an edit (any work order), and an open repair
   holding its device (tagged out, or its device in repair) keeps its type and device; a repair that holds nothing still changes, and
   sending back the current values is fine.
3. The drawer's "Reopening it and completing it as checked returns it." for a repair completed not returned to use only while that is
   so: the device out of service or in repair, never retired (the seed's bed), missing, or back in service.
4. A device's last repair is the one completed last (repair_checks.last_completed_first), never the higher number of two completed the
   same day: the survey binder's repairs section and equipment.services.loaner_device_back / loaner_back_q.
The last test runs them under the row-level security policies.
"""
from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone
from incoming_fixtures import in_use_device, waiting_device
from pg_helpers import as_app_role, needs_postgres
from repair_fixtures import CHECKED, FINDING, post_repair_results
from survey_helpers import period

from apps.equipment import services as eq
from apps.equipment.models import AddedAs, Asset, AssetStatus, Ownership, UseBeforeInspection
from apps.reports.survey import repairs as binder
from apps.tenants.context import tenant_context
from apps.web.asset_tabs import incoming_banner
from apps.workorders import repair_checks
from apps.workorders.completion import complete_work_order, keep_open
from apps.workorders.models import Priority, RepairCheck, WorkOrder, WoStatus, WoType
from apps.workorders.services import assign, change_status, create_work_order

HX = {"HTTP_HX_REQUEST": "true"}
FAILED_LEAKAGE = ("pass", "pass", "fail", "pass", "pass", "pass", "pass")  # step 3, touch leakage
EMERGENCY = UseBeforeInspection.EMERGENCY
REOPEN_RETURNS = "Reopening it and completing it as checked returns it."


@pytest.fixture
def today(ctx):
    return timezone.localdate()


@pytest.fixture
def director(make_user):
    return make_user("director")


def repair(asset, *, tech=None, tag_out=False, start=True, problem="Low tidal volume alarm"):
    wo = create_work_order(asset=asset, type=WoType.REPAIR, priority=Priority.HIGH, problem=problem, tag_out=tag_out)
    if tech is not None:
        assign(wo, technician=tech)
    if start:
        change_status(wo, WoStatus.IN_PROGRESS)
    return wo


def held_words(asset, wo) -> str:
    return f"{asset.tag} is held out of use by repair {wo.number}. Complete {wo.number} with its checks after repair."


def refused_use_before(asset, by) -> list[str]:
    with pytest.raises(ValidationError) as e:
        eq.use_before_inspection(asset, EMERGENCY, by=by)
    asset.refresh_from_db()
    assert asset.awaiting_inspection and asset.status != AssetStatus.IN_SERVICE  # nothing moved
    return e.value.messages


# --- 1. put in use before inspection while a repair holds the device out -------------------------------------------------------------


def test_use_before_inspection_is_refused_after_failed_checks_hold_a_device_in_use_before_it(vent_model, techs, director):
    device = in_use_device(vent_model, by=director)
    assert device.status == AssetStatus.IN_SERVICE and device.awaiting_inspection
    wo = repair(device, tech=techs["dana"])
    kept = keep_open(wo, results=post_repair_results(*FAILED_LEAKAGE))
    device.refresh_from_db()
    assert kept.held and wo.tagged_out and device.status == AssetStatus.OUT_OF_SERVICE
    assert eq.repair_hold_message(device) == held_words(device, wo)
    assert refused_use_before(device, director) == [held_words(device, wo)]
    assert incoming_banner(device, director)["use_before"] is False
    # put right and checked: the device still waits (out of service), and Put in use before inspection is offered again
    complete_work_order(wo, resolution="Replaced the line cord", **CHECKED)
    device.refresh_from_db()
    assert device.status == AssetStatus.OUT_OF_SERVICE and device.awaiting_inspection and eq.repair_hold_message(device) == ""
    assert incoming_banner(device, director)["use_before"] is True
    device = eq.use_before_inspection(device, EMERGENCY, by=director)
    assert device.status == AssetStatus.IN_SERVICE and device.awaiting_inspection


def test_use_before_inspection_is_refused_while_a_tagged_out_repair_is_open_out_of_service_or_missing(vent_model, techs, director):
    device = waiting_device(vent_model)
    assert device.status == AssetStatus.OUT_OF_SERVICE and incoming_banner(device, director)["use_before"] is True
    wo = repair(device, tag_out=True, start=False, problem="Screen cracked on arrival")  # a tagged-out request, not yet started
    device.refresh_from_db()
    assert eq.repair_hold_message(device) == held_words(device, wo)
    assert refused_use_before(device, director) == [held_words(device, wo)]
    assert incoming_banner(device, director)["use_before"] is False
    eq.set_status(device, AssetStatus.MISSING)  # a move the hold allows; still held by its repair
    device.refresh_from_db()
    assert refused_use_before(device, director) == [held_words(device, wo)]
    assert incoming_banner(device, director)["use_before"] is False
    eq.set_status(device, AssetStatus.OUT_OF_SERVICE)  # found
    assign(wo, technician=techs["dana"])
    change_status(wo, WoStatus.IN_PROGRESS)
    complete_work_order(wo, resolution="Replaced the screen", **CHECKED)
    device.refresh_from_db()
    assert device.status == AssetStatus.OUT_OF_SERVICE and incoming_banner(device, director)["use_before"] is True


def test_the_drawer_the_modal_and_the_api_answer_the_holds_words(client, vent_model, techs, director):
    device = in_use_device(vent_model, by=director)
    wo = repair(device, tech=techs["dana"])
    keep_open(wo, results=post_repair_results(*FAILED_LEAKAGE))
    device.refresh_from_db()
    words = held_words(device, wo)
    client.force_login(director)
    drawer = client.get(f"/equipment/{device.tag}/", **HX).content.decode()
    assert "Put in use before inspection" not in drawer
    url = f"/equipment/{device.tag}/use-before-inspection/"
    body = client.get(url, **HX).content.decode()
    assert words in body and 'id="ub-form"' not in body  # the modal says why, with no form to post
    body = client.post(url, {"reason": EMERGENCY}, **HX).content.decode()
    assert words in body and 'id="ub-form"' not in body
    r = client.post(f"/api/v1/assets/{device.id}/use-before-inspection/", {"reason": EMERGENCY}, content_type="application/json")
    assert r.status_code == 400 and words in str(r.json())
    device.refresh_from_db()
    assert device.status == AssetStatus.OUT_OF_SERVICE
    # checked: the drawer offers it again and the modal posts it
    complete_work_order(wo, resolution="Replaced the line cord", **CHECKED)
    assert "Put in use before inspection" in client.get(f"/equipment/{device.tag}/", **HX).content.decode()
    assert 'id="ub-form"' in client.get(url, **HX).content.decode()
    client.post(url, {"reason": EMERGENCY}, **HX)
    device.refresh_from_db()
    assert device.status == AssetStatus.IN_SERVICE and device.awaiting_inspection


# --- 2. the API's edit never ends a repair's hold -----------------------------------------------------------------------------------


def _patch(client, wo, body):
    return client.patch(f"/api/v1/work-orders/{wo.id}/", body, content_type="application/json")


def _to_service(client, asset):
    return client.post(f"/api/v1/assets/{asset.id}/status/", {"to": AssetStatus.IN_SERVICE}, content_type="application/json")


def test_the_api_never_changes_a_tag_out(client, vent, pump, techs, make_user):
    client.force_login(make_user("technician"))
    held = repair(vent, tech=techs["dana"], tag_out=True)
    untagged = repair(pump, tech=techs["dana"])
    safety = create_work_order(asset=pump, type=WoType.SAFETY, priority=Priority.NORMAL, problem="Electrical safety check")
    words = ["A tag-out is set when the request is opened or when a check fails, and ends when the repair is completed: POST "
             "/api/v1/work-orders/{id}/transition/."]
    for wo, value in ((held, False), (untagged, True), (safety, True)):
        r = _patch(client, wo, {"tagged_out": value})
        assert r.status_code == 400 and r.json() == {"tagged_out": words}, wo.number
        wo.refresh_from_db()
        assert wo.tagged_out is not value
    # a completed work order: still keyed tagged_out (before "reopen it first")
    complete_work_order(untagged, resolution="Reseated the door", **CHECKED)
    r = _patch(client, untagged, {"tagged_out": True})
    assert r.status_code == 400 and list(r.json()) == ["tagged_out"]
    r = _to_service(client, vent)
    assert r.status_code == 400 and f"Complete {held.number} with its checks after repair" in str(r.json())


def test_the_api_keeps_a_held_repairs_type_and_device(client, vent, pump, techs, make_user):
    """The finding's scenario: a repair opened tagged out, its touch leakage failed (keep_open), then edited by a technician."""
    client.force_login(make_user("technician"))
    wo = repair(vent, tech=techs["dana"], tag_out=True)
    keep_open(wo, results=post_repair_results(*FAILED_LEAKAGE))
    assert _to_service(client, vent).status_code == 400
    type_words = f"{wo.number} holds CE-10001 out of use: it stays a repair until it is completed with its checks after repair, or as not returned to use."
    asset_words = (f"{wo.number} holds CE-10001 out of use: it stays with CE-10001 until it is completed with its checks after repair, or as "
                   "not returned to use.")
    for body, expected in (({"tagged_out": False}, {"tagged_out"}), ({"type": WoType.SAFETY}, {"type"}), ({"type": WoType.PM}, {"type"}),
                           ({"asset": pump.id}, {"asset"}), ({"type": WoType.SAFETY, "asset": pump.id}, {"type", "asset"})):
        r = _patch(client, wo, body)
        assert r.status_code == 400 and set(r.json()) == expected, body
    assert _patch(client, wo, {"type": WoType.SAFETY}).json() == {"type": [type_words]}
    assert _patch(client, wo, {"asset": pump.id}).json() == {"asset": [asset_words]}
    # the current values sent back, with a real change: saved
    r = _patch(client, wo, {"tagged_out": True, "type": WoType.REPAIR, "asset": vent.id, "priority": Priority.CRITICAL})
    assert r.status_code == 200 and r.json()["priority"] == Priority.CRITICAL
    wo.refresh_from_db()
    assert (wo.type, wo.asset_id, wo.tagged_out, wo.status) == (WoType.REPAIR, vent.id, True, WoStatus.IN_PROGRESS)
    # still held: the device does not go back by hand, and a completion still needs its checks
    r = _to_service(client, vent)
    assert r.status_code == 400 and f"Complete {wo.number} with its checks after repair" in str(r.json())
    vent.refresh_from_db()
    assert vent.status == AssetStatus.OUT_OF_SERVICE
    r = client.post(f"/api/v1/work-orders/{wo.id}/transition/", {"status": "completed", "resolution": "Fixed"}, content_type="application/json")
    assert r.status_code == 400 and "repair_check" in r.json()


def test_the_api_keeps_an_in_repair_devices_open_repair(client, vent, pump, techs, make_user):
    client.force_login(make_user("technician"))
    Asset.objects.filter(pk=pump.pk).update(status=AssetStatus.IN_REPAIR)
    wo = repair(pump, tech=techs["dana"])  # not tagged out: the device marked in repair is what holds it
    assert not wo.tagged_out
    r = _patch(client, wo, {"type": WoType.SAFETY, "asset": vent.id})
    assert r.status_code == 400 and r.json() == {
        "type": [f"{wo.number} holds CE-10002 out of use: it stays a repair until it is completed with its checks after repair, or as not "
                 "returned to use."],
        "asset": [f"{wo.number} holds CE-10002 out of use: it stays with CE-10002 until it is completed with its checks after repair, or as "
                  "not returned to use."]}
    wo.refresh_from_db()
    assert (wo.type, wo.asset_id) == (WoType.REPAIR, pump.id)
    r = _to_service(client, pump)
    assert r.status_code == 400 and f"CE-10002 is in repair ({wo.number})" in str(r.json())


def test_the_api_still_edits_a_repair_that_holds_nothing(client, vent, pump, techs, make_user):
    client.force_login(make_user("technician"))
    wo = repair(vent, tech=techs["dana"])  # the device stays in service: nothing held
    r = _patch(client, wo, {"asset": pump.id})
    assert r.status_code == 200 and r.json()["asset"] == str(pump.id)
    r = _patch(client, wo, {"type": WoType.SAFETY})
    assert r.status_code == 200 and r.json()["type"] == WoType.SAFETY
    # out of service by hand with an untagged repair: the repair is not why it is out, so it still changes
    eq.set_status(vent, AssetStatus.OUT_OF_SERVICE)
    other = repair(vent, tech=techs["dana"])
    r = _patch(client, other, {"type": WoType.SAFETY})
    assert r.status_code == 200 and r.json()["type"] == WoType.SAFETY


# --- 3. the drawer's results for a repair not returned to use ------------------------------------------------------------------------


def _drawer(client, wo) -> str:
    return client.get(f"/work-orders/{wo.number}/", **HX).content.decode()


def test_reopening_returns_it_is_said_only_while_the_device_is_out_of_service_or_in_repair(client, vent, techs, make_user):
    wo = repair(vent, tech=techs["dana"])
    complete_work_order(wo, resolution="Cracked chassis: beyond economical repair", repair_check=RepairCheck.NOT_RETURNED, repair_finding=FINDING)
    client.force_login(make_user("manager"))
    vent.refresh_from_db()
    assert vent.status == AssetStatus.OUT_OF_SERVICE
    body = _drawer(client, wo)
    assert "CE-10001 did not go back into use after this repair" in body and REOPEN_RETURNS in body
    Asset.objects.filter(pk=vent.pk).update(status=AssetStatus.IN_REPAIR)
    assert REOPEN_RETURNS in _drawer(client, wo)
    Asset.objects.filter(pk=vent.pk).update(status=AssetStatus.OUT_OF_SERVICE)
    eq.set_status(vent, AssetStatus.MISSING)
    body = _drawer(client, wo)
    assert "CE-10001 did not go back into use after this repair" in body and REOPEN_RETURNS not in body
    eq.set_status(vent, AssetStatus.IN_SERVICE)  # found, and back in service by hand: nothing holds it
    body = _drawer(client, wo)
    assert "CE-10001 did not go back into use after this repair" in body and REOPEN_RETURNS not in body


def test_reopening_returns_it_is_never_said_for_a_retired_device(client, vent, techs, make_user):
    """The seed's smart bed: not returned to use, then retired, the repair closed."""
    wo = repair(vent, tech=techs["dana"])
    complete_work_order(wo, resolution="Frame cracked: replace the bed", repair_check=RepairCheck.NOT_RETURNED, repair_finding=FINDING)
    eq.set_status(vent, AssetStatus.RETIRED)
    change_status(wo, WoStatus.CLOSED)
    client.force_login(make_user("manager"))
    body = _drawer(client, wo)
    assert "CE-10001 did not go back into use after this repair" in body and REOPEN_RETURNS not in body
    assert REOPEN_RETURNS not in client.get(f"/work-orders/{wo.number}/").content.decode()  # the full page too


# --- 4. a device's last repair is the one completed last ----------------------------------------------------------------------------


def _section(today):
    return binder.build(period(today - timedelta(days=200), today, today), None)


def _figures(section) -> dict:
    return {f.label: f.value for f in section.figures}


def _device_checks(section) -> dict:
    return {g.record: g.text for g in section.gaps if g.record}


def _back(device, loaner) -> tuple[bool, bool]:
    device.refresh_from_db()
    return eq.loaner_device_back(device), Asset.objects.filter(eq.loaner_back_q(), pk=loaner.pk).exists()


@pytest.fixture
def loaner(vent, vent_model, dept, today):
    return eq.add_temporary_device(tag="L-1", device_model=vent_model, department=dept, kind=Ownership.LOANER, owner="Hamilton Medical",
                                   serial="HM-L-1", arrived_on=today - timedelta(days=3), stands_in_for=vent, added_as=AddedAs.EXISTING,
                                   today=today)


def _two_tagged_out(vent, techs):
    first = repair(vent, tech=techs["dana"], tag_out=True)
    second = repair(vent, tech=techs["dana"], tag_out=True)
    assert first.number < second.number
    return first, second


def test_the_lower_number_completed_second_not_returned_is_the_last(vent, techs, today, loaner):
    first, second = _two_tagged_out(vent, techs)
    complete_work_order(second, resolution="Replaced the flow sensor", **CHECKED)  # the other repair still holds it out
    complete_work_order(first, resolution="Blower failed: beyond repair", repair_check=RepairCheck.NOT_RETURNED, repair_finding=FINDING)
    vent.refresh_from_db()
    assert first.completed_on == second.completed_on == today and vent.status == AssetStatus.OUT_OF_SERVICE
    done = WorkOrder.objects.filter(asset=vent, type=WoType.REPAIR)
    assert list(repair_checks.last_completed_first(done).values_list("number", flat=True)) == [first.number, second.number]
    s = _section(today)
    assert _figures(s)["Still out of use after not returned"] == 1 and _figures(s)["Back in use with no check since"] == 0
    assert _device_checks(s)["CE-10001"].startswith(f"CE-10001 has been out of use since {first.number} ended not returned to use")
    eq.set_status(vent, AssetStatus.IN_SERVICE)  # by hand: no repair is open, so nothing holds it
    s = _section(today)
    assert _figures(s)["Still out of use after not returned"] == 0 and _figures(s)["Back in use with no check since"] == 1
    assert _device_checks(s)["CE-10001"].startswith(f"CE-10001 is in service, but its last repair, {first.number}, ended not returned to use")
    assert _back(vent, loaner) == (False, False)


def test_the_lower_number_completed_second_checked_is_the_last(vent, techs, today, loaner):
    first, second = _two_tagged_out(vent, techs)
    complete_work_order(second, resolution="Blower failed: sent to the depot", repair_check=RepairCheck.NOT_RETURNED, repair_finding=FINDING)
    vent.refresh_from_db()
    assert vent.status == AssetStatus.OUT_OF_SERVICE
    done = complete_work_order(first, resolution="Replaced the blower", **CHECKED)
    vent.refresh_from_db()
    assert done.returned and vent.status == AssetStatus.IN_SERVICE and first.completed_on == second.completed_on == today
    done_qs = WorkOrder.objects.filter(asset=vent, type=WoType.REPAIR)
    assert list(repair_checks.last_completed_first(done_qs).values_list("number", flat=True)) == [first.number, second.number]
    s = _section(today)
    assert _figures(s)["Still out of use after not returned"] == 0 and _figures(s)["Back in use with no check since"] == 0
    assert "CE-10001" not in _device_checks(s)
    assert _back(vent, loaner) == (True, True)


def test_an_earlier_day_still_comes_first_whatever_was_recorded_when(vent, techs, today):
    """The completed day decides first (a completion recorded today for an earlier day is not the last); the moment only breaks a tie."""
    late = repair(vent, tech=techs["dana"])
    complete_work_order(late, resolution="Fixed", **CHECKED)
    early = create_work_order(asset=vent, type=WoType.REPAIR, priority=Priority.NORMAL, problem="Alarm", opened_on=today - timedelta(days=3))
    assign(early, technician=techs["dana"])
    change_status(early, WoStatus.IN_PROGRESS, as_of=today - timedelta(days=2))
    complete_work_order(early, resolution="Fixed", repair_check=RepairCheck.NOT_RETURNED, repair_finding=FINDING, today=today - timedelta(days=1))
    assert (late.completed_on, early.completed_on) == (today, today - timedelta(days=1))
    done = WorkOrder.objects.filter(asset=vent, type=WoType.REPAIR)
    assert list(repair_checks.last_completed_first(done).values_list("number", flat=True)) == [late.number, early.number]


# --- under the policies -----------------------------------------------------------------------------------------------------------


@needs_postgres
def test_the_review_fixes_under_the_policies(client, tenant, vent, vent_model, techs, director, today, loaner):
    device = in_use_device(vent_model, by=director)
    held = repair(device, tech=techs["dana"])
    keep_open(held, results=post_repair_results(*FAILED_LEAKAGE))
    first, second = _two_tagged_out(vent, techs)
    complete_work_order(second, resolution="Replaced the flow sensor", **CHECKED)
    complete_work_order(first, resolution="Beyond repair", repair_check=RepairCheck.NOT_RETURNED, repair_finding=FINDING)
    client.force_login(director)
    as_app_role()
    device.refresh_from_db()
    assert refused_use_before(device, director) == [held_words(device, held)]
    assert incoming_banner(device, director)["use_before"] is False
    r = _patch(client, held, {"type": WoType.SAFETY})
    assert r.status_code == 400 and list(r.json()) == ["type"]
    body = _drawer(client, first)
    assert REOPEN_RETURNS in body
    with tenant_context(tenant):
        s = _section(today)
        assert _figures(s)["Still out of use after not returned"] == 1
        eq.set_status(vent, AssetStatus.IN_SERVICE)
        assert _back(vent, loaner) == (False, False)
