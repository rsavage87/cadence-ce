"""
Slice 35's review, each fix pinned (the confirmed findings, and the fixes made for them):

#F1 Recording a work order's test equipment needs Equipment View on every door: the API's test-equipment action and its transition to
    completed refuse a user without it (VIEW_NEEDED, keyed test_equipment, even for [], naming no item), as the web does; KEEP (nothing
    sent: the web's completion by such a user) never reaches the check, so the work still completes.
#F2 Reinstate (retired -> in use) and Found (missing -> in use) land out of service when the newest live certificate failed (failed
    calibration) or, for Reinstate, when the item was out of service when it was retired (its reason then, from its history), status
    since that day, the history saying so; only a passed calibration puts it back in use, and work completed that day never records it
    (it was not in use that morning). A passed item reinstated or found goes in use.
#F3 Reinstate refuses a second live record of one make, model, and serial (any letter case), keyed status, on the service and the
    drawer, under the facility's tag lock (two reinstates of one unit at once: one wins).
#F4 A repair completed again as checked by the vendor takes our test equipment off (KEEP, on the service, the web's modal, and the API's
    transition), so its drawer never reads "found out of tolerance" or "added since the review" for work the review cannot cover; a use
    on work that does not count is never in a look-back; a counted repair completed again keeps its uses, and the kept-use rule still
    holds there.
#F5 Change refuses a tag or interval locked between opening and saving (the lock's words, nothing saved), never drops it; locked values
    posted unchanged still save.
#F6 The change log opens an item by its current tag: an earlier row of a corrected tag opens the same item, never another item now
    holding the old tag (the service, the page, and the API).
#F7 Add test equipment: a stray click on the accreditation mark leaves the optional certificate optional; a day, the provider, its
    number, the due day, as found, adjusted, or Failed still counts as a certificate entered.
Round 2 (the fix verifiers'):
#R1 The binder's section and the item's Work done with it (and its CSV) read the uses in a fixed number of queries (judge_uses'
    all_counted: never a deferred work order field per use).
#R2 A judged use is never erased (F4(b) reverted): a completion as checked by the vendor takes off only the uses the kept-use rule
    allows, an explicit [] there is refused in KEPT's words, assign to a vendor keeps a kept use, and a use on work that does not count
    obeys the rule too; a kept use is in no window while its work does not count, and counts again, as the review read it, once the
    work is completed again as checked by us.
#R3 Work completed today records only items in use at some moment of today (rules.in_use_today, from the history): an item reinstated
    or found today that lands out of service, or one retired today from out of service, is refused and never offered by the picker; an
    analyzer that failed its self-test at noon is still recorded on work done this morning; one query for the items changed today.
#R4 _held_out judges the certificates on file against how the item left: retired while out of service, it lands in use when its newest
    live certificate passed and is dated on or after its day out (any newer one after a failed calibration), out of service (failed
    calibration) when its newest failed on or after that day, else with the history's reason; Found, or retired from use, a failed
    newest certificate holds it out.
#R6 The change log page (and its API) and the item's History cost the same queries however many test equipment saves; a corrected tag's
    earlier rows link to the item's current tag.
#R7 Each certificate field alone: the mark alone adds the item with no certificate; as found (a select with a blank choice) alone sends
    the certificate, as every other field does.
#R8 Change's reverse race: a lock that goes away between opening and saving never sends the field the modal did not post (a new serial
    saves).
#R9 The API's work order test_equipment reads [] for a cancelled work order with uses left (kept ones), as its drawer shows none.
Round 3 (the round 2 verifier's):
#R10 A later re-completion (on a real later day, never backdated) never lets [] erase a judged use: after a vendor re-completion (the
    API and the service), on an in-house PM completed again (Change, the API, the service, another completion), and on a vendor PM
    whose use assign kept; a decided review keeps its list. (Round 4's U1 replaced R10's rules.work_day with each use's own day.)
#R11 A completion as checked by the vendor takes back the kept ids (the API's transition and the service): any other id is refused.
#R12 An item added today with a failed first certificate is out of service from its first history row (since the certificate's day):
    never usable today, never offered, refused on today's completion (the service and the web's Add).
#R13 Reinstate after the reason was converted and its failed certificate entered in error, or after a failed calibration itself was
    entered in error, lands out of service, never in use on an older pass; a round trip keeps the day out (status_since and the words),
    so a pass after the day out but before the first reinstate releases it.
#R14 A cancel takes off the work order's uses but the kept ones, audited ("Test equipment cleared: cancelled", by whoever cancelled);
    the API's PATCH sending back the field's [] on a cancelled work order succeeds, one sending a hidden kept id is refused.
Round 4 (the round 3 verifier's):
#U1 Each use has its own day (TestEquipmentUse.used_on), written once: a completion's today, Change's the work order's completed_on;
    reopening and completing again (on a real later day) keeps it, and a use added then gets the new day. The verifier's problem 1 (a
    mis-tick at a later completion, or by Change after it, is taken off: nothing judged it) and problem 2 (an undecided review keeps a
    use reopened and completed again: in the window, the drawer, the API's field and review list, the review box and CSV, the decision's
    frozen list); each use judged on its own day (its standing, the drawer, the API); recheck progress and the binder read it too.
    (The drawer's heading dating every use on the last completion was round 4's bug: V2.)
#U2 On work that is not offered (a completion as checked by the vendor) a list may name only kept ids: a free recorded id is refused in
    VENDOR_CHECKED's words (the service, the API's transition sending back its GET's ids); left out, the free use comes off ("Taken off:
    completed as checked by the vendor"); Change never puts it back.
#U3 One release rule (a pass dated on or after the day it is held from: held_since since V1), a failure holding it out from its own day:
    the verifier's problem 4 (a) (a failure entered in error never lets an older pass release a reinstated item) and (b) (after the
    conversion, a pass dated before the damage never releases it), a failure and its passing successor typed in either order, and out for
    calibration (never before the trip).
#U4 (PostgreSQL) Retiring a device whose two reopened PMs hold two items while Change names both on another device's work order: the
    cancel takes no item's row, so both finish (before U4: deadlock detected).
#U5 A cancelled work order's own words: the API's DELETE, its test-equipment action, the service, and the web's Change.
Round 5 (the round 4 verifier's):
#V1 status_since is only the day of the last status change (the day it was made); held_since is the day a passed certificate must be
    dated from to release an item out of service (by hand: the move's day; a failed certificate: its own day, never before a trip out for
    calibration; Reinstate or Found: the history's or the failed certificate's), cleared when it leaves, never set by Change, in the
    drawer's words and the History ("Needs a pass dated on or after"). The verifier's problem 1 (a), (b), (c): Change refuses an item on a
    day it was retired (rules.retired_throughout reads the history right again); problem 2: an item in use this morning stays recordable on
    today's completion after a back-dated failed certificate is typed (rules.in_use_today); U3's release cases hold through held_since.
#V2 The drawer's heading names the day only when every use shares it, else each line names its own (the print likewise); Change's hint
    says an item already on the work order is judged on the day it was used; the uses CSV, the review CSV, and the binder's work table
    head the use's day "Day of the work".
#V3 On work that is not offered with kept uses, the refusal names them as the ones to send (VENDOR_CHECKED_KEPT, VENDOR_WORK_KEPT; with
    "or leave test_equipment out" only while completing), and each answer it names succeeds.
On PostgreSQL: the fixes under the policies as the runtime role (rounds 3, 4, and 5 too), two reinstates of one unit at once, and U4.
"""
import json
import re
import threading
import time
from datetime import datetime, timedelta
from datetime import time as clock
from types import SimpleNamespace

import pytest
import testequipment_fixtures
from csvutil import csv_rows
from django.core.exceptions import ValidationError
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django.utils.html import escape
from pg_helpers import as_app_role, needs_postgres
from repair_fixtures import CHECKED, FINDING
from survey_helpers import gaps_of, period
from testequipment_fixtures import certificate, in_date, item, no_record, past_due, pm_with, repair_with
from vendor_fixtures import vendor_job

from apps.accounts.models import Level, Module, Role, User
from apps.api.views import TEST_EQUIPMENT_RECORDED
from apps.core import history
from apps.reports.survey import FINDING as BINDER_FINDING
from apps.reports.survey import testequipment as section
from apps.tenants.context import tenant_context
from apps.testequipment import rules, services
from apps.testequipment.models import AsFound, Basis, Kind, Mark, Reason, Result, Status, TestEquipment, TestEquipmentUse
from apps.vendors.rules import fmt
from apps.web.views_wo_test_equipment import HINT_DAY
from apps.workorders.completion import complete_work_order
from apps.workorders.models import PmResult, Priority, RepairCheck, WorkOrder, WoStatus, WoType
from apps.workorders.services import assign, change_status, create_work_order, set_test_equipment

te_world = testequipment_fixtures.te_world  # the shared pytest fixture (its docstring says what it holds)
DAY = timedelta(days=1)
HX = {"HTTP_HX_REQUEST": "true"}
WOS = "/api/v1/work-orders/"
VENDOR_DONE = {"resolution": "Serviced by the vendor", "repair_check": RepairCheck.VENDOR_REPORT, "results": None, "checks_reference": "FSO-2"}


# --- helpers -----------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def today(ctx):
    return timezone.localdate()


@pytest.fixture
def users(make_user):
    return SimpleNamespace(**{slug: make_user(slug) for slug in ("director", "manager", "technician")})


def no_equipment_view(tenant):
    """A custom role with Work orders Edit and Equipment None (no default role has this mix; any facility may make it)."""
    role = Role.objects.create(name="Work orders only", slug="wo-only")
    role.set_levels({m: Level.EDIT if m == Module.WORKORDERS else Level.NONE for m in Module.values})
    return User.objects.create_user(username="wo-only@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role)


def errors_of(excinfo) -> dict:
    e = excinfo.value
    return {k: " ".join(v) for k, v in e.message_dict.items()} if hasattr(e, "error_dict") else {"": " ".join(e.messages)}


def refused(call, *args, **kwargs) -> dict:
    with pytest.raises(ValidationError) as e:
        call(*args, **kwargs)
    return errors_of(e)


def tags_on(wo) -> list:
    return sorted(TestEquipmentUse.objects.filter(work_order=wo).values_list("test_equipment__tag", flat=True))


def open_pm(asset, *, day=None):
    day = day or timezone.localdate()
    wo = create_work_order(asset=asset, type=WoType.PM, priority=Priority.NORMAL, problem="Scheduled work", opened_on=day - 2 * DAY,
                           assigned_to=testequipment_fixtures.bench_tech())
    change_status(wo, WoStatus.IN_PROGRESS, as_of=day)
    return wo


def reopen(wo, day=None):
    wo.refresh_from_db()
    change_status(wo, WoStatus.IN_PROGRESS, note="Reopened", as_of=day)
    wo.refresh_from_db()
    return wo


def last_note(te) -> str:
    return TestEquipment.history.filter(id=te.pk).order_by("-history_date", "-history_id").first().history_change_reason


def written_on(te, day):
    """As if every change to `te` so far was written at noon on `day` (its history's rows; the days the services stored stay)."""
    TestEquipment.history.filter(id=te.pk).update(history_date=timezone.make_aware(datetime.combine(day, clock(12))))
    return te


def offered_tags(wo, user, today) -> list:
    """The tags services.picker offers for `wo` (Yours and the others)."""
    pick = services.picker(WorkOrder.objects.get(pk=wo.pk), user, today=today)
    return sorted([o.item.tag for o in pick.yours] + [o.item.tag for _label, options in pick.others for o in options])


def not_usable(te, status) -> str:
    return services.NOT_USABLE.format(tag=te.tag, status=status)


def failed_on(te, day):
    """A failed certificate dated and entered on `day`: out of service (failed calibration) since then."""
    certificate(te, day, result=Result.FAILED, as_found=AsFound.OUT_OF_TOLERANCE, today=day)
    te.refresh_from_db()
    return te


def checked_vendor_repair(asset, te, day):
    """A repair on vendor service that CE checked and ready for use on `day`, recording `te`: counted."""
    wo = vendor_job(asset, completed_on=day, test_equipment=[str(te.pk)])
    assert tags_on(wo) == [te.tag] and rules.counted(wo)
    return wo


def as_left_before_the_fix(wo):
    """A repair holding our test equipment, completed again as checked by the vendor by the code before F4 (which kept the uses): the
    record such a facility has. A queryset update, so nothing else moves."""
    WorkOrder.objects.filter(pk=wo.pk).update(repair_check=RepairCheck.VENDOR_REPORT, checks_reference="FSO-3")
    wo.refresh_from_db()
    assert not rules.counted(wo) and not rules.offered(wo) and tags_on(wo)
    return wo


def post(client, url, body):
    return client.post(url, body, content_type="application/json")


def wo_url(wo, tail=""):
    return f"{WOS}{wo.id}/{tail}"


def toast_of(r) -> str:
    out = {}
    for h in ("HX-Trigger", "HX-Trigger-After-Settle"):
        if h in r:
            out.update(json.loads(r[h]))
    return out.get("toast", {}).get("value", "")


def drawer_row(client, wo) -> str:
    """The work order drawer's "Test equipment" section ("" when there is none)."""
    body = client.get(f"/work-orders/{wo.number}/", **HX).content.decode()
    m = re.search(r'<div class="sec wte-row" id="wo-te">.*?\n</div>', body, re.S)
    return m.group(0) if m else ""


# --- F1: Equipment View on every door ----------------------------------------------------------------------------------------------

def test_f1_the_api_action_refuses_a_user_without_equipment_view(client, tenant, te_world, make_user):
    """The finding's scenario: Work orders Edit, Equipment None, a completed PM that recorded TE-IN. Clearing, adding, and an id the
    services would refuse in words naming the item are all refused in the same words, and nothing changes."""
    w = te_world
    client.force_login(no_equipment_view(tenant))
    pm_url = wo_url(w.pm, "test-equipment/")
    assert client.get(wo_url(w.pm)).json()["test_equipment"] is None
    for ids in ([], [str(w.past_due.pk)], [str(w.in_date.pk), str(w.failed.pk)]):
        r = post(client, pm_url, {"test_equipment": ids})
        assert r.status_code == 400 and r.json() == {"test_equipment": [services.VIEW_NEEDED]}, ids
    assert tags_on(w.pm) == ["TE-IN"]
    assert not TestEquipmentUse.history.filter(work_order_id=w.pm.pk, history_type="-").exists()
    # a kept use: the refusal never names the item (KEPT would)
    kept = w.unreviewed.work[1]
    r = post(client, wo_url(kept, "test-equipment/"), {"test_equipment": []})
    assert r.status_code == 400 and r.json() == {"test_equipment": [services.VIEW_NEEDED]} and "TE-OT1" not in r.content.decode()
    assert tags_on(kept) == ["TE-OT1"]
    # with Equipment View the same action records it
    client.force_login(make_user("technician"))
    r = post(client, pm_url, {"test_equipment": [str(w.in_date.pk), str(w.past_due.pk)]})
    assert r.status_code == 200 and tags_on(w.pm) == ["TE-IN", "TE-PD"]


def test_f1_the_transition_refuses_a_user_without_equipment_view_and_left_out_still_completes(client, tenant, te_world):
    w = te_world
    client.force_login(no_equipment_view(tenant))
    wo = reopen(w.pm)
    body = {"status": "completed", "resolution": "Done per the procedure", "pm_result": PmResult.PASS}
    for ids in ([], [str(w.in_date.pk)], [str(w.past_due.pk)]):
        r = post(client, wo_url(wo, "transition/"), {**body, "test_equipment": ids})
        assert r.status_code == 400 and r.json()["test_equipment"] == [services.VIEW_NEEDED], ids
        wo.refresh_from_db()
        assert wo.status == WoStatus.IN_PROGRESS and tags_on(wo) == ["TE-IN"]
    # left out (KEEP): it completes, and the use stays
    r = post(client, wo_url(wo, "transition/"), body)
    assert r.status_code == 200, r.content
    wo.refresh_from_db()
    assert wo.status == WoStatus.COMPLETED and tags_on(wo) == ["TE-IN"] and r.json()["test_equipment"] is None


def test_f1_the_services_refuse_a_user_without_equipment_view(tenant, te_world, pump, make_user):
    w = te_world
    user = no_equipment_view(tenant)
    assert refused(set_test_equipment, w.pm, [], by=user) == {"test_equipment": services.VIEW_NEEDED}
    assert refused(set_test_equipment, w.pm, [w.past_due.pk], by=user) == {"test_equipment": services.VIEW_NEEDED}
    assert tags_on(w.pm) == ["TE-IN"]
    wo = open_pm(pump)
    errors = refused(complete_work_order, wo, resolution="Done", pm_result=PmResult.PASS, test_equipment=[str(w.in_date.pk)], by=user)
    assert errors == {"test_equipment": services.VIEW_NEEDED}
    wo.refresh_from_db()
    assert wo.status == WoStatus.IN_PROGRESS and tags_on(wo) == []
    complete_work_order(wo, resolution="Done", pm_result=PmResult.PASS, by=user)  # KEEP never reaches the check
    wo.refresh_from_db()
    assert wo.status == WoStatus.COMPLETED and tags_on(wo) == []
    # a reader with Equipment View still records and clears (by=None, the system, too)
    set_test_equipment(wo, [w.in_date.pk], by=make_user("technician"))
    assert tags_on(wo) == ["TE-IN"]
    set_test_equipment(wo, [], by=None)
    assert tags_on(wo) == []


def test_f1_the_webs_completion_by_a_user_without_equipment_view_completes_and_keeps(client, tenant, te_world):
    w = te_world
    client.force_login(no_equipment_view(tenant))
    wo = reopen(w.pm)
    data = {"resolution": "Done per the procedure", "pm_result": PmResult.PASS, "shown_test_equipment": "1"}
    r = client.post(f"/work-orders/{wo.number}/complete/", data, **HX)
    assert r["HX-Retarget"] == "#drawer", r.content.decode()[:500]
    wo.refresh_from_db()
    assert wo.status == WoStatus.COMPLETED and tags_on(wo) == ["TE-IN"]


# --- F2: back from retired or missing, never past a failed calibration ---------------------------------------------------------------

def test_f2_reinstating_an_item_that_failed_calibration_lands_out_of_service(today, pump, users):
    """The finding's scenario: failed, out of service; set_status to in use is refused; retire it, then Reinstate: out of service
    (failed calibration), never in use, so new work refuses it; a passed calibration puts it back."""
    te = failed_on(item("TE-01"), today - 10 * DAY)
    assert (te.status, te.status_reason) == (Status.OUT_OF_SERVICE, Reason.FAILED_CALIBRATION)
    assert refused(services.set_status, te, to=Status.IN_USE, by=users.director) == {"status": services.back_in_use_words(te)}
    services.set_status(te, to=Status.RETIRED, reason=Reason.WORN_OUT, by=users.director, today=today - 5 * DAY)
    back = services.set_status(te, to=Status.IN_USE, by=users.director, today=today - 2 * DAY)
    # V1: status since the reinstate (the day of the change); held since the failure's day (R13, U3), the day a pass must be dated from
    assert (back.status, back.status_reason, back.status_since, back.held_since) == (
        Status.OUT_OF_SERVICE, Reason.FAILED_CALIBRATION, today - 2 * DAY, today - 10 * DAY)
    assert last_note(te) == "Reinstated: out of service (failed calibration)"
    assert not rules.usable_today(back, today)
    wo = open_pm(pump)
    errors = refused(complete_work_order, wo, resolution="Done", pm_result=PmResult.PASS, test_equipment=[str(te.pk)], today=today)
    assert errors == {"test_equipment": services.NOT_USABLE.format(tag="TE-01", status="out of service (failed calibration)")}
    assert refused(services.set_status, te, to=Status.IN_USE, by=users.director) == {"status": services.back_in_use_words(back)}
    certificate(te, today - DAY)  # a passed calibration after the failed one
    te.refresh_from_db()
    assert (te.status, te.status_since, te.held_since) == (Status.IN_USE, today, None)  # leaving out of service clears held_since


def test_f2_found_after_a_failed_calibration_lands_out_of_service(today, users):
    """The variant: a failed certificate typed while the item is missing does not move it; Found then lands out of service."""
    te = in_date("TE-02")
    services.set_status(te, to=Status.MISSING, by=users.technician, today=today - 6 * DAY)
    certificate(te, today - 4 * DAY, result=Result.FAILED, as_found=AsFound.OUT_OF_TOLERANCE, today=today - 4 * DAY)
    te.refresh_from_db()
    assert te.status == Status.MISSING
    back = services.set_status(te, to=Status.IN_USE, by=users.technician, today=today - DAY)
    # V1: status since the day it was found; held since the failed certificate's own day (the lab's: U3)
    assert (back.status, back.status_reason, back.status_since, back.held_since) == (
        Status.OUT_OF_SERVICE, Reason.FAILED_CALIBRATION, today - DAY, today - 4 * DAY)
    assert last_note(te) == "Found: out of service (failed calibration)"
    assert not rules.usable_today(back, today)


@pytest.mark.parametrize("out_reason", [Reason.DAMAGED, Reason.FAILED_CHECK])
@pytest.mark.parametrize("retired_as", [Reason.WORN_OUT, Reason.RETURNED_TO_OWNER])
def test_f2_reinstating_an_item_retired_from_out_of_service_lands_out_of_service_with_its_reason(today, users, out_reason, retired_as):
    te = in_date("TE-03")  # its newest certificate passed (100 days ago)
    services.set_status(te, to=Status.OUT_OF_SERVICE, reason=out_reason, by=users.technician, today=today - 20 * DAY)
    services.set_status(te, to=Status.RETIRED, reason=retired_as, by=users.director, today=today - 10 * DAY)
    back = services.set_status(te, to=Status.IN_USE, by=users.director, today=today)
    # V1: status since the reinstate; held since its day out (20 days ago, kept from its history: R13)
    assert (back.status, back.status_reason, back.status_since, back.held_since) == (Status.OUT_OF_SERVICE, out_reason, today, today - 20 * DAY)
    assert last_note(te) == f"Reinstated: out of service ({Reason(out_reason).label.lower()})"
    assert services.back_in_use_words(back) == (f"TE-03 is out of service ({Reason(out_reason).label.lower()}): it goes back in use when a "
                                                f"passed calibration dated on or after {fmt(today - 20 * DAY)} is recorded.")
    certificate(te, today - 25 * DAY)  # passed, but dated before its day out: it stays out
    te.refresh_from_db()
    assert te.status == Status.OUT_OF_SERVICE
    certificate(te, today - 5 * DAY)  # after its day out, though before the day it came back
    te.refresh_from_db()
    assert (te.status, te.status_since, te.held_since) == (Status.IN_USE, today, None)


def test_f2_a_passed_item_reinstated_or_found_goes_in_use(today, users):
    """Guards against over-correcting: in date and in use when it left, it comes back in use; so does one with no calibration on file,
    and one that failed and then passed before it was retired."""
    te = in_date("TE-04")
    services.set_status(te, to=Status.RETIRED, reason=Reason.WORN_OUT, by=users.director, today=today - 3 * DAY)
    back = services.set_status(te, to=Status.IN_USE, by=users.director)
    assert (back.status, back.status_reason, back.status_since) == (Status.IN_USE, "", today) and last_note(te) == "Reinstated"
    services.set_status(te, to=Status.MISSING, by=users.technician, today=today - DAY)
    services.set_status(te, to=Status.RETIRED, reason=Reason.LOST, by=users.director, today=today - DAY)
    assert services.set_status(te, to=Status.IN_USE, by=users.director).status == Status.IN_USE
    services.set_status(te, to=Status.MISSING, by=users.technician)
    found = services.set_status(te, to=Status.IN_USE, by=users.technician)
    assert found.status == Status.IN_USE and last_note(te) == "Found"
    fresh = item("TE-05")  # no calibration on file
    services.set_status(fresh, to=Status.RETIRED, reason=Reason.WORN_OUT, by=users.director, today=today - DAY)
    assert services.set_status(fresh, to=Status.IN_USE, by=users.director).status == Status.IN_USE
    again = failed_on(item("TE-06"), today - 30 * DAY)
    certificate(again, today - 20 * DAY, today=today - 20 * DAY)  # passed after the failure: back in use
    again.refresh_from_db()
    assert again.status == Status.IN_USE
    services.set_status(again, to=Status.RETIRED, reason=Reason.WORN_OUT, by=users.director, today=today - 10 * DAY)
    assert services.set_status(again, to=Status.IN_USE, by=users.director).status == Status.IN_USE


@pytest.mark.parametrize("retired_days_ago", [0, 5], ids=["retired-today", "retired-earlier"])
def test_f2_an_item_reinstated_today_out_of_service_is_not_recorded_on_todays_work(today, pump, users, retired_days_ago):
    """The finding's own scenario on one day: the failure on file for ten days (its history written then), retired, and reinstated
    today. Reinstate lands it out of service (failed calibration), and work completed today must not record it either (R3): it was not in
    use at any moment of today, and the picker never offers it."""
    te = written_on(failed_on(item("TE-07"), today - 10 * DAY), today - 10 * DAY)
    assert not rules.usable_today(te, today)
    retired_on = today - retired_days_ago * DAY
    services.set_status(te, to=Status.RETIRED, reason=Reason.WORN_OUT, by=users.director, today=retired_on)
    if retired_days_ago:
        written_on(te, retired_on)
    back = services.set_status(te, to=Status.IN_USE, by=users.director, today=today)
    # V1: status since today (so in_use_today reads its history: never in use today); held since its day out (the failure ten days ago)
    assert (back.status, back.status_reason, back.status_since, back.held_since) == (
        Status.OUT_OF_SERVICE, Reason.FAILED_CALIBRATION, today, today - 10 * DAY)
    assert not rules.usable_today(back, today) and rules.in_use_today([back], today) == set()
    wo = open_pm(pump)
    assert "TE-07" not in offered_tags(wo, users.technician, today)
    errors = refused(complete_work_order, wo, resolution="Done", pm_result=PmResult.PASS, test_equipment=[str(te.pk)], today=today)
    assert errors == {"test_equipment": not_usable(te, "out of service (failed calibration)")}
    assert tags_on(wo) == [] and WorkOrder.objects.get(pk=wo.pk).status == WoStatus.IN_PROGRESS


# --- R4: Reinstate and Found judge the certificates on file against how the item left ------------------------------------------------

def _damaged_then_retired(today, users, tag="TE-R4"):
    """In date (passed 100 days ago), out of service (damaged) 30 days ago, retired 20 days ago: its day out is 30 days ago."""
    te = in_date(tag)
    services.set_status(te, to=Status.OUT_OF_SERVICE, reason=Reason.DAMAGED, by=users.technician, today=today - 30 * DAY)
    services.set_status(te, to=Status.RETIRED, reason=Reason.WORN_OUT, by=users.director, today=today - 20 * DAY)
    return te


def _passed(te, days_ago, today):
    certificate(te, today - days_ago * DAY, today=today - min(days_ago, 15) * DAY)


def _failed(te, days_ago, today):
    certificate(te, today - days_ago * DAY, result=Result.FAILED, as_found=AsFound.OUT_OF_TOLERANCE, today=today - min(days_ago, 15) * DAY)


@pytest.mark.parametrize("story, expected", [
    # (status, reason, held_since in days ago: None in use; V1: status_since is the reinstate's day, today, whatever it lands as).
    # Retired while out of service (damaged since 30 days ago): its newest live certificate passed on or after the day out, recorded
    # while retired: in use
    (lambda te, today: _passed(te, 0, today), (Status.IN_USE, "", None)),
    (lambda te, today: _passed(te, 30, today), (Status.IN_USE, "", None)),  # on the day out itself
    # passed, but dated before the day out: the history's reason holds it, held since the day out (R13)
    (lambda te, today: _passed(te, 31, today), (Status.OUT_OF_SERVICE, Reason.DAMAGED, 30)),
    # failed on or after the day out: a failed calibration, held since that certificate's own day (U3)
    (lambda te, today: _failed(te, 25, today), (Status.OUT_OF_SERVICE, Reason.FAILED_CALIBRATION, 25)),
    # failed before the day out (typed after it, so it moved nothing): the history's reason (damaged), never a failed calibration
    (lambda te, today: _failed(te, 40, today), (Status.OUT_OF_SERVICE, Reason.DAMAGED, 30)),
    # failed on or after the day out, then passed after it: in use
    (lambda te, today: (_failed(te, 25, today), _passed(te, 10, today)), (Status.IN_USE, "", None)),
], ids=["passed-today", "passed-on-the-day-out", "passed-before-the-day-out", "failed-since", "failed-before-the-day-out",
        "failed-then-passed"])
def test_r4_reinstating_an_item_retired_while_out_of_service(today, users, story, expected):
    te = _damaged_then_retired(today, users)
    story(te, today)
    te.refresh_from_db()
    assert te.status == Status.RETIRED  # a certificate typed while retired moves nothing
    back = services.set_status(te, to=Status.IN_USE, by=users.director, today=today)
    status, reason, held_days_ago = expected
    held = None if held_days_ago is None else today - held_days_ago * DAY
    assert (back.status, back.status_reason, back.status_since, back.held_since) == (status, reason, today, held)
    note = f": out of service ({Reason(reason).label.lower()})" if status == Status.OUT_OF_SERVICE else ""
    assert last_note(te) == f"Reinstated{note}"


def test_r4_a_failed_calibration_is_cleared_by_any_newer_passed_certificate(today, users):
    """Retired while out of service for a failed calibration (failed 40 days ago, typed 30 days ago: status since the day it was typed,
    held since the failed certificate's own day, 40 days ago: V1, U3), then a passed certificate dated after the failure but before the
    day it was typed, recorded while retired: Reinstate puts it in use, as the passed certificate would have while it was out of
    service."""
    te = item("TE-FC")
    certificate(te, today - 200 * DAY)
    certificate(te, today - 40 * DAY, result=Result.FAILED, as_found=AsFound.OUT_OF_TOLERANCE, today=today - 30 * DAY)
    te.refresh_from_db()
    assert (te.status, te.status_reason, te.status_since, te.held_since) == (
        Status.OUT_OF_SERVICE, Reason.FAILED_CALIBRATION, today - 30 * DAY, today - 40 * DAY)
    services.set_status(te, to=Status.RETIRED, reason=Reason.WORN_OUT, by=users.director, today=today - 20 * DAY)
    _passed(te, 35, today)
    assert services.set_status(te, to=Status.IN_USE, by=users.director, today=today).status == Status.IN_USE


def test_r4_a_round_trip_keeps_damaged_and_a_pass_before_the_damage_never_releases_it(today, users):
    """The verifier's case (b): passed 200 days ago, damaged 30 days ago, a failed certificate dated 40 days ago typed 25 days ago
    (moves nothing), retired 20 days ago, reinstated: out of service (damaged), as without the round trip; a passed certificate dated 35
    days ago (before the damage) leaves it out; one dated today puts it back."""
    te = item("TE-RT")
    certificate(te, today - 200 * DAY)
    services.set_status(te, to=Status.OUT_OF_SERVICE, reason=Reason.DAMAGED, by=users.technician, today=today - 30 * DAY)
    certificate(te, today - 40 * DAY, result=Result.FAILED, as_found=AsFound.OUT_OF_TOLERANCE, today=today - 25 * DAY)
    te.refresh_from_db()
    assert (te.status, te.status_reason) == (Status.OUT_OF_SERVICE, Reason.DAMAGED)
    services.set_status(te, to=Status.RETIRED, reason=Reason.WORN_OUT, by=users.director, today=today - 20 * DAY)
    back = services.set_status(te, to=Status.IN_USE, by=users.director, today=today)
    assert (back.status, back.status_reason) == (Status.OUT_OF_SERVICE, Reason.DAMAGED)
    certificate(te, today - 35 * DAY)
    te.refresh_from_db()
    assert te.status == Status.OUT_OF_SERVICE
    certificate(te, today)
    te.refresh_from_db()
    assert te.status == Status.IN_USE


@pytest.mark.parametrize("way_out", ["retired-from-use", "missing"])
@pytest.mark.parametrize("newest", [Result.FAILED, Result.PASSED])
def test_r4_found_or_retired_from_use_a_failed_newest_certificate_holds_it_out(today, users, way_out, newest):
    """Found, or retired straight from use: a failed newest certificate (typed while it was away, so it moved nothing) holds it out for a
    failed calibration; a passed one lets it back in use."""
    te = in_date("TE-AW")
    if way_out == "missing":
        services.set_status(te, to=Status.MISSING, by=users.technician, today=today - 20 * DAY)
    else:
        services.set_status(te, to=Status.RETIRED, reason=Reason.WORN_OUT, by=users.director, today=today - 20 * DAY)
    if newest == Result.FAILED:
        _failed(te, 10, today)
    else:
        _passed(te, 10, today)
    te.refresh_from_db()
    assert te.status in (Status.MISSING, Status.RETIRED)
    back = services.set_status(te, to=Status.IN_USE, by=users.director, today=today)
    expected = (Status.OUT_OF_SERVICE, Reason.FAILED_CALIBRATION) if newest == Result.FAILED else (Status.IN_USE, "")
    assert (back.status, back.status_reason) == expected


# --- R3: work completed today records only what was in use at some moment of today -------------------------------------------------

def test_r3_found_today_out_of_service_or_retired_today_from_out_of_service_is_never_recorded_today(today, pump, users):
    """An item found today that lands out of service (a failed certificate typed while it was missing), and one retired today while out
    of service (failed calibration ten days ago): neither was in use at any moment of today, so today's completion refuses each in words
    and the picker offers neither; an item retired today straight from use was in use this morning, so it is still offered."""
    found = in_date("TE-FOUND")
    services.set_status(found, to=Status.MISSING, by=users.technician, today=today - 6 * DAY)
    certificate(found, today - 4 * DAY, result=Result.FAILED, as_found=AsFound.OUT_OF_TOLERANCE, today=today - 4 * DAY)
    written_on(found, today - 4 * DAY)
    services.set_status(found, to=Status.IN_USE, by=users.technician, today=today)
    # V1: status since today (in_use_today reads its history: missing before today, out of service since); held since the failed
    # certificate's day (4 days ago)
    assert (found.status, found.status_reason, found.status_since, found.held_since) == (
        Status.OUT_OF_SERVICE, Reason.FAILED_CALIBRATION, today, today - 4 * DAY)
    gone = written_on(failed_on(item("TE-GONE"), today - 10 * DAY), today - 10 * DAY)
    services.set_status(gone, to=Status.RETIRED, reason=Reason.WORN_OUT, by=users.director, today=today)
    worn = written_on(in_date("TE-WORN"), today - 100 * DAY)
    services.set_status(worn, to=Status.RETIRED, reason=Reason.WORN_OUT, by=users.director, today=today)
    good = written_on(in_date("TE-GOOD"), today - 100 * DAY)
    assert rules.in_use_today([found, gone, worn, good], today) == {worn.pk, good.pk}
    wo = open_pm(pump)
    assert offered_tags(wo, users.technician, today) == ["TE-GOOD", "TE-WORN"]
    for te, status in ((found, "out of service (failed calibration)"), (gone, "retired (worn out or obsolete)")):
        errors = refused(complete_work_order, wo, resolution="Done", pm_result=PmResult.PASS, test_equipment=[str(te.pk)], today=today)
        assert errors == {"test_equipment": not_usable(te, status)}, te.tag
    assert tags_on(wo) == []
    complete_work_order(wo, resolution="Done", pm_result=PmResult.PASS, test_equipment=[str(worn.pk), str(good.pk)], today=today)
    assert tags_on(wo) == ["TE-GOOD", "TE-WORN"]


def test_r3_an_analyzer_that_failed_its_self_test_at_noon_is_recorded_on_work_done_this_morning(today, pump, users):
    """In use since long before today, out of service (failed a check) today: it was in use this morning, so the picker offers it and
    today's completion records it truthfully; out for calibration since yesterday, it is neither."""
    analyzer = written_on(in_date("TE-AN"), today - 100 * DAY)
    services.set_status(analyzer, to=Status.OUT_OF_SERVICE, reason=Reason.FAILED_CHECK, by=users.technician, today=today)
    away = in_date("TE-AWAY")
    services.set_status(away, to=Status.OUT_FOR_CALIBRATION, by=users.technician, today=today - DAY)
    written_on(away, today - DAY)
    assert rules.usable_today(analyzer, today) and not rules.usable_today(away, today)
    wo = open_pm(pump)
    assert offered_tags(wo, users.technician, today) == ["TE-AN"]
    assert refused(complete_work_order, wo, resolution="Done", pm_result=PmResult.PASS, test_equipment=[str(away.pk)], today=today) == {
        "test_equipment": not_usable(away, "out for calibration")}
    complete_work_order(wo, resolution="Leakage within limits", pm_result=PmResult.PASS, test_equipment=[str(analyzer.pk)], today=today)
    assert tags_on(wo) == ["TE-AN"]


def test_r3_in_use_today_reads_the_history_once_and_only_for_items_changed_today(today, users, django_assert_num_queries):
    still = [in_date(f"TE-S{n}") for n in range(3)]
    with django_assert_num_queries(0):
        assert rules.in_use_today(still, today) == {te.pk for te in still}
    changed = []
    for n in range(3):
        te = written_on(in_date(f"TE-C{n}"), today - 50 * DAY)
        services.set_status(te, to=Status.OUT_OF_SERVICE, reason=Reason.DAMAGED, by=users.technician, today=today)
        changed.append(te)
    with django_assert_num_queries(1):
        assert rules.in_use_today([*still, *changed], today) == {te.pk for te in [*still, *changed]}


# --- F3: one live record per unit ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("way_out", ["worn_out", "lost"])
def test_f3_reinstate_refuses_a_second_live_record_of_one_unit(today, users, way_out):
    """The finding's scenario: TE-01 retired, TE-03 added with its make, model, and serial (allowed), then Reinstate TE-01: refused."""
    first = item("TE-01", serial="1234567")
    if way_out == "lost":
        services.set_status(first, to=Status.MISSING, by=users.technician)
    services.set_status(first, to=Status.RETIRED, reason=Reason.LOST if way_out == "lost" else Reason.WORN_OUT, by=users.director)
    second = item("TE-03", manufacturer="FLUKE BIOMEDICAL", model="esa612", serial="1234567")  # any letter case is one unit
    errors = refused(services.set_status, first, to=Status.IN_USE, by=users.director)
    assert errors == {"status": "Fluke Biomedical ESA612 serial 1234567 is already TE-03: one item, one tag."}
    first.refresh_from_db()
    assert first.status == Status.RETIRED
    assert [i.tag for i in TestEquipment.objects.filter(status__in=services.LIVE_STATUSES)] == ["TE-03"]
    # once TE-03 is retired, TE-01 comes back
    services.set_status(second, to=Status.RETIRED, reason=Reason.WORN_OUT, by=users.director)
    assert services.set_status(first, to=Status.IN_USE, by=users.director).status == Status.IN_USE


def test_f3_the_drawers_reinstate_says_so(client, ctx, make_user):
    first = item("TE-A", serial="SN1")
    services.set_status(first, to=Status.RETIRED, reason=Reason.WORN_OUT, by=None)
    item("TE-B", serial="SN1")
    client.force_login(make_user("director"))
    r = client.post("/test-equipment/TE-A/status/", {"to": "in_use"}, **HX)
    words = "Fluke Biomedical ESA612 serial SN1 is already TE-B: one item, one tag."
    assert r.status_code == 200 and toast_of(r) == words and escape(words) in r.content.decode()
    first.refresh_from_db()
    assert first.status == Status.RETIRED
    assert TestEquipment.objects.filter(status=Status.IN_USE, serial="SN1").count() == 1


def test_f3_reinstate_takes_the_tag_lock_before_the_items_row(ctx, users, monkeypatch):
    """The duplicate check runs under the facility's tag lock (taken first), as add and update take it."""
    import apps.equipment.services as equipment_services

    first = item("TE-A", serial="SN1")
    services.set_status(first, to=Status.RETIRED, reason=Reason.WORN_OUT, by=None)
    calls = []
    real_lock, real_locked = equipment_services.lock_tags, services._locked
    monkeypatch.setattr(equipment_services, "lock_tags", lambda: calls.append("tags") or real_lock())
    monkeypatch.setattr(services, "_locked", lambda i: calls.append("item") or real_locked(i))
    services.set_status(first, to=Status.IN_USE, by=users.director)
    assert calls == ["tags", "item"]


# --- F4: work that does not count keeps none of ours --------------------------------------------------------------------------------

def test_f4_a_repair_completed_again_as_checked_by_the_vendor_takes_ours_off(today, pump):
    """The finding's scenario: TE-V passed 200 days ago; a vendor repair CE checked records it (counted); reopened and completed again
    as checked by the vendor with the test equipment left out (KEEP); a PM records TE-V; a certificate today finds it out of tolerance.
    The repair keeps nothing of ours, so the review, the binder, and its drawer agree before and after the decision."""
    te = item("TE-V")
    certificate(te, today - 200 * DAY)
    x = checked_vendor_repair(pump, te, today - 30 * DAY)
    reopen(x, today - 5 * DAY)
    complete_work_order(x, today=today - 3 * DAY, **VENDOR_DONE)
    x.refresh_from_db()
    assert x.repair_check == RepairCheck.VENDOR_REPORT and not rules.counted(x)
    assert tags_on(x) == []
    gone = TestEquipmentUse.history.filter(work_order_id=x.pk, history_type="-")
    assert list(gone.values_list("history_change_reason", flat=True)) == ["Taken off: completed as checked by the vendor"]
    pm = pm_with(pump, te, completed_on=today - 2 * DAY)
    found = certificate(te, today, as_found=AsFound.OUT_OF_TOLERANCE, adjusted=True)
    summary = services.review_summary(found)
    assert [w.pk for w in summary.work] == [pm.pk]
    assert services.judged_for(x) == [] and dict(services.judged_by_work_order([x])) == {}
    services.decide_review(found, basis=Basis.WITHIN_MARGIN, decided_on=today, version=summary.version, by=None)
    assert services.review_summary(found).added == [] and services.judged_for(x) == []
    assert "reviewed" in services.judged_for(pm)[0].words


def test_f4_a_use_a_calibration_since_has_judged_stays_when_completed_again_as_checked_by_the_vendor(today, pump):
    """R2 (reverses F4(b)): a judged use is never erased. Completed again as checked by the vendor with the test equipment left out, the
    repair no longer counts, yet TE-V, calibrated since the work's day, stays; nothing is taken off and nothing is written."""
    te = item("TE-V")
    certificate(te, today - 200 * DAY)
    x = checked_vendor_repair(pump, te, today - 30 * DAY)
    certificate(te, today - 20 * DAY)  # on or after the day of the work: the kept-use rule keeps it
    assert services.kept_ids(x) == {te.pk}
    reopen(x, today - 5 * DAY)
    complete_work_order(x, today=today - 3 * DAY, **VENDOR_DONE)
    x.refresh_from_db()
    assert x.status == WoStatus.COMPLETED and not rules.counted(x) and tags_on(x) == ["TE-V"]
    assert not TestEquipmentUse.history.filter(work_order_id=x.pk, history_type="-").exists()


def test_r2_completed_as_checked_by_the_vendor_a_judged_use_stays_out_of_every_window_then_counts_again(today, pump):
    """A vendor repair CE checked 30 days ago recorded TE-V and TE-F; a certificate 10 days ago found TE-V out of tolerance, and its review
    read the repair. Completed again as checked by the vendor (that day, test equipment left out): TE-F comes off ("Taken off: completed
    as checked by the vendor"), TE-V stays, in no window while the work does not count (the review's work, Work done with it, the
    drawer's look-back). Completed again as checked by us: it counts again, with the use the review read."""
    te, free = item("TE-V"), item("TE-F")
    certificate(te, today - 200 * DAY)
    certificate(free, today - 200 * DAY)
    x = vendor_job(pump, completed_on=today - 30 * DAY, test_equipment=[str(te.pk), str(free.pk)])
    assert tags_on(x) == ["TE-F", "TE-V"] and rules.counted(x)
    found = certificate(te, today - 10 * DAY, as_found=AsFound.OUT_OF_TOLERANCE, adjusted=True)
    assert services.kept_ids(x) == {te.pk}
    summary = services.review_summary(found)
    assert [w.pk for w in summary.work] == [x.pk]
    services.decide_review(found, basis=Basis.WITHIN_MARGIN, decided_on=today, version=summary.version, by=None)
    reopen(x, today - 30 * DAY)
    complete_work_order(x, today=today - 30 * DAY, **VENDOR_DONE)
    x.refresh_from_db()
    assert (x.completed_on, x.repair_check) == (today - 30 * DAY, RepairCheck.VENDOR_REPORT) and not rules.counted(x)
    assert tags_on(x) == ["TE-V"]
    gone = TestEquipmentUse.history.filter(work_order_id=x.pk, history_type="-")
    assert [(r.test_equipment_id, r.history_change_reason) for r in gone] == [(free.pk, "Taken off: completed as checked by the vendor")]
    assert services.review_summary(found).work == [] and services.work_done(te) == []
    [judged] = services.judged_for(x)
    assert judged.item == te and judged.later == ()
    reopen(x, today - 30 * DAY)
    complete_work_order(x, resolution="Checked by CE", today=today - 30 * DAY, **CHECKED)
    x.refresh_from_db()
    assert rules.counted(x) and tags_on(x) == ["TE-V"]
    again = services.review_summary(found)
    assert [w.pk for w in again.work] == [x.pk] and again.added == []
    assert services.judged_for(x)[0].later[0].state == rules.REVIEWED
    assert [j.use.work_order_id for j in services.work_done(te)] == [x.pk]


def test_r2_an_empty_list_on_a_completion_as_checked_by_the_vendor_is_refused_for_a_judged_use(client, today, pump, make_user):
    """[] sent with a completion as checked by the vendor is a removal like any other: the kept-use rule refuses it in KEPT's words (the
    service and the API's transition), nothing completes; left out, the completion goes through and the use stays."""
    te = item("TE-V")
    certificate(te, today - 200 * DAY)
    x = checked_vendor_repair(pump, te, today - 30 * DAY)
    certificate(te, today - 20 * DAY)
    reopen(x, today - 5 * DAY)
    words = services.KEPT.format(tag="TE-V", number=x.number, day=fmt(today - 30 * DAY))
    assert refused(complete_work_order, x, today=today - 3 * DAY, test_equipment=[], **VENDOR_DONE) == {"test_equipment": words}
    client.force_login(make_user("manager"))
    r = post(client, wo_url(x, "transition/"), {"status": "completed", **VENDOR_DONE, "test_equipment": []})
    assert r.status_code == 400 and r.json()["test_equipment"] == [words], r.content
    x.refresh_from_db()
    assert x.status == WoStatus.IN_PROGRESS and tags_on(x) == ["TE-V"]
    r = post(client, wo_url(x, "transition/"), {"status": "completed", **VENDOR_DONE})
    assert r.status_code == 200, r.content
    x.refresh_from_db()
    assert x.status == WoStatus.COMPLETED and tags_on(x) == ["TE-V"]


def test_r2_assign_to_a_vendor_keeps_a_judged_use(today, pump):
    """assign's clearing (clear_for_vendor) takes off only what the kept-use rule allows: a reopened repair moved to a vendor keeps TE-K
    (calibrated since its day) and loses TE-F, audited."""
    kept, free = item("TE-K"), item("TE-F")
    certificate(kept, today - 200 * DAY)
    certificate(free, today - 200 * DAY)
    wo = repair_with(pump, kept, free, completed_on=today - 30 * DAY)
    certificate(kept, today - 10 * DAY, as_found=AsFound.OUT_OF_TOLERANCE, adjusted=True)  # judged since
    reopen(wo, today - 5 * DAY)
    assert services.kept_ids(wo) == {kept.pk}
    assign(wo, vendor_name="Philips")
    assert tags_on(wo) == ["TE-K"]
    gone = TestEquipmentUse.history.filter(work_order_id=wo.pk, history_type="-")
    assert [(r.test_equipment_id, r.history_change_reason) for r in gone] == [(free.pk, "Test equipment cleared: assigned to Philips")]
    assert services.clear_for_vendor(WorkOrder.objects.get(pk=wo.pk), "TechCare", by=None) == 0 and tags_on(wo) == ["TE-K"]


@pytest.mark.parametrize("marker", [True, False])
def test_f4_the_webs_completion_as_checked_by_the_vendor_takes_ours_off(client, today, pump, make_user, marker):
    """The finding's web scenario: a vendor repair completed 20 days ago as checked by CE with TE-01, reopened 10 days ago, completed
    again through the modal as checked by the vendor: TE-01 comes off; a later out-of-tolerance certificate never flags the repair."""
    te = in_date("TE-01")
    x = checked_vendor_repair(pump, te, today - 20 * DAY)
    reopen(x, today - 10 * DAY)
    client.force_login(make_user("manager"))
    data = {"resolution": "Vendor replaced the board", "repair_check": RepairCheck.VENDOR_REPORT, "checks_reference": "FSO-77",
            "repair_finding": FINDING, "vendor_engineer": ""}
    if marker:
        data["shown_test_equipment"] = "1"
    r = client.post(f"/work-orders/{x.number}/complete/", data, **HX)
    assert r["HX-Retarget"] == "#drawer", r.content.decode()[:500]
    x.refresh_from_db()
    assert (x.status, x.repair_check) == (WoStatus.COMPLETED, RepairCheck.VENDOR_REPORT) and tags_on(x) == []
    certificate(te, today - 5 * DAY, as_found=AsFound.OUT_OF_TOLERANCE, adjusted=True)
    assert "TE-01" not in drawer_row(client, x) and "found out of tolerance" not in drawer_row(client, x)


def test_f4_the_apis_transition_as_checked_by_the_vendor_takes_ours_off(client, today, pump, make_user):
    te = in_date("TE-01")
    x = checked_vendor_repair(pump, te, today - 20 * DAY)
    reopen(x, today - 10 * DAY)
    client.force_login(make_user("manager"))
    r = post(client, wo_url(x, "transition/"), {"status": "completed", **VENDOR_DONE})
    assert r.status_code == 200, r.content
    assert r.json()["test_equipment"] == [] and tags_on(x) == []


def test_f4_a_counted_repair_completed_again_keeps_its_uses(client, today, pump, make_user):
    """Guards against over-correcting: a vendor repair CE checked, completed again as checked and ready for use (service or API) with
    the test equipment left out, keeps it; the kept-use rule still holds on it."""
    te = in_date("TE-01")
    x = checked_vendor_repair(pump, te, today - 20 * DAY)
    reopen(x, today - 10 * DAY)
    complete_work_order(x, resolution="Checked by CE again", today=today - 8 * DAY, **CHECKED)
    x.refresh_from_db()
    assert x.repair_check == RepairCheck.PASSED and rules.counted(x) and tags_on(x) == ["TE-01"]
    reopen(x, today - 6 * DAY)
    client.force_login(make_user("manager"))
    body = {"status": "completed", "resolution": "Checked by CE", "repair_check": "passed", "results": CHECKED["results"]}
    r = post(client, wo_url(x, "transition/"), body)
    assert r.status_code == 200, r.content
    x.refresh_from_db()
    assert tags_on(x) == ["TE-01"] and not TestEquipmentUse.history.filter(work_order_id=x.pk, history_type="-").exists()
    certificate(te, today)  # on or after the day of the work: calibrated since
    # U1: the kept-use rule reads the use's own day (recorded 20 days ago), not the re-completion's
    assert refused(set_test_equipment, x, [], by=make_user("director")) == {
        "test_equipment": services.KEPT.format(tag="TE-01", number=x.number, day=fmt(today - 20 * DAY))}
    assert tags_on(x) == ["TE-01"]
    in_house = pm_with(pump, te, completed_on=today)
    reopen(in_house)
    complete_work_order(in_house, resolution="Done", pm_result=PmResult.PASS)
    assert tags_on(in_house) == ["TE-01"]


def test_r2_a_use_on_work_that_does_not_count_obeys_the_kept_use_rule(today, pump, make_user):
    """R2 (F4(b) reverted): a record the code before the fix left (a repair completed again as checked by the vendor, still holding ours).
    A use a calibration since has judged stays ([] is refused in KEPT's words, naming only it, and nothing comes off); once that
    calibration is marked entered in error, nothing judges the work with it, and [] takes both off, "Taken off after completion"."""
    te, free = item("TE-V"), item("TE-F")
    certificate(te, today - 200 * DAY)
    certificate(free, today - 200 * DAY)
    x = as_left_before_the_fix(vendor_job(pump, completed_on=today - 30 * DAY, test_equipment=[str(te.pk), str(free.pk)]))
    since = certificate(te, today - 10 * DAY, as_found=AsFound.OUT_OF_TOLERANCE, adjusted=True)
    manager = make_user("manager")
    assert refused(set_test_equipment, x, [], by=manager) == {
        "test_equipment": services.KEPT.format(tag="TE-V", number=x.number, day=fmt(today - 30 * DAY))}
    assert tags_on(x) == ["TE-F", "TE-V"]
    services.mark_entered_in_error(since, by=None)
    set_test_equipment(x, [], by=manager)
    assert tags_on(x) == []
    gone = TestEquipmentUse.history.filter(work_order_id=x.pk, history_type="-")
    assert sorted((r.test_equipment_id, r.history_change_reason) for r in gone) == sorted(
        [(free.pk, "Taken off after completion"), (te.pk, "Taken off after completion")])


def test_f4_a_use_on_work_that_does_not_count_is_never_found_later(client, today, pump, make_user):
    """(c) The same record: its drawer, judged_for, and the API never read "found out of tolerance" or "added since the review" for
    it, while the PM done with the item in the same window does."""
    te = item("TE-V")
    certificate(te, today - 200 * DAY)
    x = as_left_before_the_fix(checked_vendor_repair(pump, te, today - 30 * DAY))
    pm = pm_with(pump, te, completed_on=today - 20 * DAY)
    found = certificate(te, today - 10 * DAY, as_found=AsFound.OUT_OF_TOLERANCE, adjusted=True)
    judged = services.judged_for(x)
    assert len(judged) == 1 and judged[0].later == () and "found out of tolerance" not in judged[0].words
    assert services.judged_by_work_order([x])[x.pk][0].later == ()
    assert services.judged_for(pm)[0].later[0].state == rules.NOT_REVIEWED
    client.force_login(make_user("manager"))
    assert "found out of tolerance" not in drawer_row(client, x)
    assert all("found out of tolerance" not in row["words"] for row in client.get(wo_url(x)).json()["test_equipment"])
    services.decide_review(found, basis=Basis.WITHIN_MARGIN, decided_on=today, version=services.review_summary(found).version, by=None)
    assert services.judged_for(x)[0].later == () and "added since the review" not in drawer_row(client, x)
    assert services.judged_for(pm)[0].later[0].state == rules.REVIEWED



def test_r1_the_binder_and_work_done_with_it_read_in_a_fixed_number_of_queries(client, today, pump, make_user):
    """(c) judge_uses asks whether each use's work counts (rules.counted) unless its caller read only counted uses (all_counted): the
    binder's uses, read with only the fields it shows, and the item's Work done with it (the service, the drawer, the CSV) are judged in
    a fixed number of queries, never one more per use."""
    te = item("TE-PD")
    certificate(te, today - 400 * DAY, due_on=today - 35 * DAY)  # past due: the work done with it is listed
    pm_with(pump, te, completed_on=today - 3 * DAY)
    client.force_login(make_user("manager"))

    def count(call) -> int:
        with CaptureQueriesContext(connection) as q:
            call()
        return len(q)

    def binder():
        s = section.build(period(today - 200 * DAY, today, today), None)
        assert [list(r) for t in s.tables for r in t.rows()]

    def work_done():
        assert services.work_done(te, limit=10)

    def drawer():
        assert client.get("/test-equipment/TE-PD/", **HX).status_code == 200

    def uses_csv():
        r = client.get("/test-equipment/TE-PD/uses.csv")
        body = b"".join(r.streaming_content) if r.streaming else r.content
        assert r.status_code == 200 and b"WO-" in body

    calls = (binder, work_done, drawer, uses_csv)
    for call in calls:
        call()  # warm: the session and anything read once
    one = [count(call) for call in calls]
    assert one[1] <= 4
    for n in range(4):
        pm_with(pump, te, completed_on=today - (4 + n) * DAY)
    assert [count(call) for call in calls] == one


def test_r1_the_binder_and_work_done_never_ask_whether_the_work_counts(today, pump, monkeypatch):
    """They read only counted_uses_q, so they pass all_counted and never call rules.counted (which would load the binder's deferred work
    order fields one use at a time)."""
    te = item("TE-PD")
    certificate(te, today - 400 * DAY, due_on=today - 35 * DAY)
    pm_with(pump, te, completed_on=today - 3 * DAY)

    def boom(wo):
        raise AssertionError("rules.counted called")

    monkeypatch.setattr(rules, "counted", boom)
    s = section.build(period(today - 200 * DAY, today, today), None)
    assert [list(r) for t in s.tables for r in t.rows()]
    assert len(services.work_done(te)) == 1

# --- F5: Change never drops a field locked meanwhile -------------------------------------------------------------------------------

def _change_data(te, **over):
    data = {"tag": te.tag, "kind": te.kind, "manufacturer": te.manufacturer, "model": te.model, "serial": te.serial, "program": te.program,
            "interval_months": str(te.interval_months), "held_by": ""}
    data.update(over)
    return data


TAG_LOCK = "TE-A has calibrations or work on record, so its tag stays."


@pytest.mark.parametrize("over, words", [
    ({"tag": "TE-Z", "interval_months": "6"}, f"{TAG_LOCK} {services.INTERVAL_PERMISSION}"),
    ({"tag": "TE-Z"}, TAG_LOCK),
    ({"interval_months": "6"}, services.INTERVAL_PERMISSION),
    ({"tag": "TE-Z", "model": "ESA620"}, TAG_LOCK),
], ids=["tag-and-interval", "tag", "interval", "tag-with-a-model"])
def test_f5_a_tag_or_interval_locked_meanwhile_is_refused_never_dropped(client, ctx, make_user, today, over, words):
    """The finding's scenario: a technician opens Change on TE-A (no certificate: tag and interval offered); a colleague records its
    first certificate; saving a new tag or interval is refused in the lock's words and nothing is saved."""
    te = item("TE-A")
    client.force_login(make_user("technician"))
    body = client.get("/test-equipment/TE-A/edit/", **HX).content.decode()
    assert 'name="tag"' in body and 'name="interval_months"' in body
    certificate(te, today - 5 * DAY)  # meanwhile
    r = client.post("/test-equipment/TE-A/edit/", _change_data(te, **over), **HX)
    assert r.status_code == 200 and toast_of(r) == ""
    assert list(r.context["form"].non_field_errors()) == [words] and escape(words) in r.content.decode()
    te.refresh_from_db()
    assert (te.tag, te.interval_months, te.model) == ("TE-A", 12, "ESA612")


def test_f5_locked_values_posted_unchanged_still_save(client, ctx, make_user, today):
    te = item("TE-A")
    certificate(te, today - 5 * DAY)
    client.force_login(make_user("technician"))
    r = client.post("/test-equipment/TE-A/edit/", _change_data(te, model="ESA620"), **HX)  # tag and interval as stored
    te.refresh_from_db()
    assert toast_of(r) == "TE-A: details saved" and te.model == "ESA620"
    r = client.post("/test-equipment/TE-A/edit/", _change_data(te, tag=" TE-A ", model="ESA615"), **HX)
    te.refresh_from_db()
    assert toast_of(r) == "TE-A: details saved" and te.model == "ESA615"
    data = _change_data(te, model="ESA612")
    del data["tag"], data["interval_months"]  # what the modal posts when it never offered them
    r = client.post("/test-equipment/TE-A/edit/", data, **HX)
    te.refresh_from_db()
    assert toast_of(r) == "TE-A: details saved" and te.model == "ESA612"
    # Decide: the interval is offered; the tag stays locked
    client.force_login(make_user("manager"))
    r = client.post("/test-equipment/TE-A/edit/", _change_data(te, interval_months="6"), **HX)
    te.refresh_from_db()
    assert toast_of(r) == "TE-A: details saved" and te.interval_months == 6


def test_f5_at_decide_the_interval_is_offered_and_a_tag_locked_meanwhile_still_refused(client, ctx, make_user, today):
    te = item("TE-A")
    client.force_login(make_user("manager"))
    assert 'name="tag"' in client.get("/test-equipment/TE-A/edit/", **HX).content.decode()
    certificate(te, today - 5 * DAY)  # meanwhile
    r = client.post("/test-equipment/TE-A/edit/", _change_data(te, tag="TE-Z", interval_months="24"), **HX)
    te.refresh_from_db()
    assert toast_of(r) == "" and list(r.context["form"].non_field_errors()) == [TAG_LOCK]
    assert (te.tag, te.interval_months) == ("TE-A", 12)


def test_r8_a_lock_gone_between_opening_and_saving_never_blanks_the_field(client, today, pump, users):
    """The reverse race: a field locked when the modal opened is not posted; if the lock is gone by the save, the field is not sent (as
    blank) to the service: a new serial saves, and the tag and interval stay as stored."""
    client.force_login(users.technician)
    # the tag lock (a use) goes: the director takes TE-A off its PM meanwhile
    te = item("TE-A")
    wo = pm_with(pump, te)
    body = client.get("/test-equipment/TE-A/edit/", **HX).content.decode()
    assert 'name="tag"' not in body and 'name="interval_months"' in body
    set_test_equipment(wo, [], by=users.director)
    data = _change_data(te, serial="SN-NEW-A")
    del data["tag"]
    r = client.post("/test-equipment/TE-A/edit/", data, **HX)
    te.refresh_from_db()
    assert toast_of(r) == "TE-A: details saved" and (te.tag, te.serial, te.interval_months) == ("TE-A", "SN-NEW-A", 12)
    # the interval lock (a live certificate, below Decide) goes: a manager marks its only certificate entered in error meanwhile
    other = item("TE-B")
    cal = certificate(other, today - 5 * DAY)
    body = client.get("/test-equipment/TE-B/edit/", **HX).content.decode()
    assert 'name="tag"' not in body and 'name="interval_months"' not in body
    services.mark_entered_in_error(cal, by=users.manager)
    data = _change_data(other, serial="SN-NEW-B")
    del data["tag"], data["interval_months"]
    r = client.post("/test-equipment/TE-B/edit/", data, **HX)
    other.refresh_from_db()
    assert toast_of(r) == "TE-B: details saved" and (other.tag, other.serial, other.interval_months) == ("TE-B", "SN-NEW-B", 12)


# --- F6: the change log opens an item by its current tag ------------------------------------------------------------------------

def test_f6_the_change_log_opens_an_item_by_its_current_tag(client, ctx, make_user):
    """The finding's scenario: TE-01 added, its tag corrected to TE-02, a new TE-01 added: the first item's "added" entry opens the
    first item (TE-02), never the second."""
    first = item("TE-01")
    services.update_test_equipment(first, tag="TE-02", by=None)
    item("TE-01", serial="SN-OTHER")
    director = make_user("director")
    entries, _more = history.change_log(director, areas=["test_equipment"])
    assert sorted((e.record, e.action, e.url) for e in entries) == [
        ("TE-01", "added", "/test-equipment/TE-01/"),  # the new item
        ("TE-01", "added", "/test-equipment/TE-02/"),  # the first item, added as TE-01
        ("TE-02", "changed", "/test-equipment/TE-02/"),
    ]
    client.force_login(director)
    body = client.get("/users/log/?area=test_equipment").content.decode()
    assert body.count('href="/test-equipment/TE-02/') == 2 and body.count('href="/test-equipment/TE-01/') == 1
    rows = client.get("/api/v1/change-log/?area=test_equipment").json()["results"]
    links = sorted((row["record"], row["url"].split("?")[0].split("/test-equipment/")[1]) for row in rows)
    assert links == [("TE-01", "TE-01/"), ("TE-01", "TE-02/"), ("TE-02", "TE-02/")]


def test_r6_the_change_log_and_the_items_history_cost_the_same_however_many_saves(client, ctx, make_user):
    """The current tags are read once for a batch (history.Area.urls), never once per entry: the change log page, its API, and the
    item's History cost the same with 3 and with 33 test equipment saves; every row of the item, the one written under its first tag
    too, links to its current tag."""
    director = make_user("director")
    client.force_login(director)
    first = item("TE-X")
    services.update_test_equipment(first, tag="TE-A", by=director)  # corrected before its first calibration or use
    item("TE-B")  # 3 saves
    pages = (("/users/log/?area=test_equipment", {}), ("/api/v1/change-log/?area=test_equipment", {}),
             ("/test-equipment/TE-A/?history=1", {**HX, "HTTP_HX_TARGET": "hist"}))

    def queries(url, headers) -> int:
        with CaptureQueriesContext(connection) as q:
            assert client.get(url, **headers).status_code == 200
        return len(q)

    for page in pages:
        queries(*page)  # warm
    before = [queries(*page) for page in pages]
    for n in range(30):
        services.update_test_equipment(first, model=f"ESA6{n:02d}", by=director)
    assert [queries(*page) for page in pages] == before
    entries, _more = history.change_log(director, areas=["test_equipment"])
    assert len(entries) == 33
    assert {e.url for e in entries if e.record != "TE-B"} == {"/test-equipment/TE-A/"}
    assert [e.url for e in entries if e.record == "TE-X"] == ["/test-equipment/TE-A/"]
    body = client.get("/users/log/?area=test_equipment").content.decode()
    assert 'href="/test-equipment/TE-X/' not in body and body.count('href="/test-equipment/TE-A/') == 32


# --- R9: a cancelled work order records none -------------------------------------------------------------------------------------

def test_r9_the_apis_field_reads_nothing_for_a_cancelled_work_order_with_uses_left(client, today, pump, make_user):
    """A PM that recorded TE-C, calibrated since (so R14's cancel keeps it: the kept-use rule), reopened, moved back to open, and
    cancelled: the use stays on the record, but the API reads [] for it, as the drawer shows none and Work done with it leaves it out;
    another work order on the same page still lists its own."""
    te, other = in_date("TE-C"), in_date("TE-D")
    wo = pm_with(pump, te, completed_on=today - 30 * DAY)
    done = pm_with(pump, other, completed_on=today - 20 * DAY)
    certificate(te, today - 10 * DAY)  # judged since: kept
    reopen(wo, today - 5 * DAY)
    change_status(wo, WoStatus.OPEN)
    change_status(wo, WoStatus.CANCELLED)
    wo.refresh_from_db()
    assert wo.status == WoStatus.CANCELLED and tags_on(wo) == ["TE-C"]
    client.force_login(make_user("manager"))
    assert client.get(wo_url(wo)).json()["test_equipment"] == []
    assert [row["tag"] for row in client.get(wo_url(done)).json()["test_equipment"]] == ["TE-D"]
    rows = {row["number"]: row["test_equipment"] for row in client.get(f"{WOS}?page_size=100").json()["results"]}
    assert rows[wo.number] == [] and [row["tag"] for row in rows[done.number]] == ["TE-D"]
    assert drawer_row(client, wo) == "" and services.work_done(te) == []


# --- F7: the certificate stays optional --------------------------------------------------------------------------------------------

def new(**over):
    data = {"tag": "TE-NEW", "kind": Kind.ELECTRICAL_SAFETY, "manufacturer": "Fluke Biomedical", "model": "ESA615", "serial": "4455",
            "program": "calibration", "interval_months": "12", "held_by": "", "result": "passed"}
    data.update(over)
    return data


@pytest.mark.parametrize("stray", [{}, {"accreditation": "shown"}, {"accreditation": "not_shown"}, {"as_found": ""},
                                   {"accreditation": "shown", "as_found": ""}],
                         ids=["nothing", "mark", "no-mark", "as-found-blank", "mark-and-as-found-blank"])
def test_f7_a_stray_click_leaves_the_certificate_optional(client, ctx, make_user, stray):
    """The finding's scenario: the item's fields filled, the accreditation mark (a radio a browser cannot untick) clicked by mistake: it
    is added with no calibration on file (R7: the mark alone never counts; as found left on its blank choice is nothing typed)."""
    client.force_login(make_user("technician"))
    r = client.post("/test-equipment/new/", new(**stray), **HX)
    assert TestEquipment.objects.filter(tag="TE-NEW").exists(), re.findall(r'<span class="err"[^>]*>([^<]*)</span>', r.content.decode())
    te = TestEquipment.objects.get(tag="TE-NEW")
    assert toast_of(r) == "TE-NEW added: No calibration on file" and not te.calibrations.exists()


@pytest.mark.parametrize("mark", [None, "shown"], ids=["alone", "with-the-mark"])
@pytest.mark.parametrize("typed", [{"provider": "Fluke Biomedical"}, {"certificate": "CAL-1"}, {"calibrated_on": "2026-01-05"},
                                   {"due_on": "2027-01-05"}, {"as_found": "in_tolerance"}, {"as_found": "out_of_tolerance"},
                                   {"as_found": "not_reported"}, {"adjusted": "on"}, {"result": "failed"}],
                         ids=["provider", "number", "day", "due-day", "in-tolerance", "out-of-tolerance", "not-reported", "adjusted",
                              "failed"])
def test_f7_any_other_certificate_field_counts_as_entered(client, ctx, make_user, typed, mark):
    """Guards against over-correcting, each field alone and beside the mark: a day, the provider, its number, the due day, as found (R7:
    a select with a blank choice, so a choice there is deliberate), adjusted, or Failed sends the certificate, and the service asks for
    the rest; nothing is added."""
    client.force_login(make_user("technician"))
    data = new(**typed) if mark is None else new(accreditation=mark, **typed)
    r = client.post("/test-equipment/new/", data, **HX)
    body = r.content.decode()
    assert r.status_code == 200 and not TestEquipment.objects.exists() and toast_of(r) == ""
    assert re.search(r'<span class="err"[^>]*>[^<]+</span>', body), typed


def test_f7_a_whole_certificate_with_its_mark_is_recorded(client, ctx, make_user, today):
    client.force_login(make_user("technician"))
    day = today - 30 * DAY
    r = client.post("/test-equipment/new/", new(calibrated_on=day.isoformat(), provider="Fluke Biomedical", certificate="CAL-889",
                                                accreditation="not_shown", as_found="in_tolerance"), **HX)
    cal = TestEquipment.objects.get(tag="TE-NEW").calibrations.get()
    assert (cal.accreditation, cal.as_found, cal.certificate) == ("not_shown", AsFound.IN_TOLERANCE, "CAL-889")
    assert toast_of(r) == f"TE-NEW added: Calibration due {fmt(cal.due_on)}"


# --- R10 (as U1 left it): a judged use is never erased by a later re-completion --------------------------------------------------

def uses_on(wo) -> list:
    """[(tag, used_on)] of `wo`'s uses, by tag."""
    return sorted(TestEquipmentUse.objects.filter(work_order=wo).values_list("test_equipment__tag", "used_on"))


def test_u1_a_uses_day_is_the_day_of_the_work_it_was_recorded_on(today, pump, users):
    """U1 (replaces R10's rules.work_day): each use has its own day, used_on, written once. Recorded at a completion 30 days ago, it is
    that day; one added by Change after that completion gets the work order's completed_on; reopened (completed_on cleared) and
    completed again today, on a real later day, any number of times, they stay; a use added at a re-completion gets the new day, and
    one added by Change after it the new completed_on."""
    w, c, x, y = in_date("TE-W"), in_date("TE-C"), in_date("TE-X"), in_date("TE-Y")
    pm = pm_with(pump, w, completed_on=today - 30 * DAY)
    assert uses_on(pm) == [("TE-W", today - 30 * DAY)]
    set_test_equipment(pm, [w.pk, c.pk], by=users.manager)
    assert uses_on(pm) == [("TE-C", today - 30 * DAY), ("TE-W", today - 30 * DAY)]
    for _ in range(2):
        reopen(pm, today)
        assert pm.completed_on is None and uses_on(pm) == [("TE-C", today - 30 * DAY), ("TE-W", today - 30 * DAY)]
        complete_work_order(pm, resolution="Done again", pm_result=PmResult.PASS, today=today)
        pm.refresh_from_db()
        assert pm.completed_on == today and uses_on(pm) == [("TE-C", today - 30 * DAY), ("TE-W", today - 30 * DAY)]
    reopen(pm, today)
    complete_work_order(pm, resolution="Done again", pm_result=PmResult.PASS, test_equipment=[str(w.pk), str(c.pk), str(x.pk)], today=today)
    assert uses_on(pm) == [("TE-C", today - 30 * DAY), ("TE-W", today - 30 * DAY), ("TE-X", today)]
    set_test_equipment(pm, [w.pk, c.pk, x.pk, y.pk], by=users.manager)
    assert uses_on(pm) == [("TE-C", today - 30 * DAY), ("TE-W", today - 30 * DAY), ("TE-X", today), ("TE-Y", today)]
    # each reader reads that day: the drawer's words and the judged day
    assert [(j.item.tag, j.day) for j in services.judged_for(pm)] == uses_on(pm)


def test_r10_a_vendor_recompletion_on_a_later_day_never_lets_an_empty_list_erase_a_judged_use(client, today, pump, users):
    """The verifier's first path, on a real later day: a vendor repair CE checked 30 days ago recorded TE-V; a certificate 10 days ago
    found it out of tolerance and its review froze the repair. Reopened and completed today as checked by the vendor (test equipment
    left out): TE-V stays, on its own day (U1: used_on, 30 days ago). The API's [] (and the service's) is then refused in KEPT's words
    naming that day: it used to be judged on today, after the certificate, and erased. Completed again today as checked by us, the
    repair counts again with TE-V still on it, the decided review keeps its frozen list, and [] is still refused."""
    te = item("TE-V")
    certificate(te, today - 200 * DAY)
    x = checked_vendor_repair(pump, te, today - 30 * DAY)
    found = certificate(te, today - 10 * DAY, as_found=AsFound.OUT_OF_TOLERANCE, adjusted=True)
    summary = services.review_summary(found)
    assert [w.pk for w in summary.work] == [x.pk]
    review = services.decide_review(found, basis=Basis.WITHIN_MARGIN, decided_on=today, version=summary.version, by=None)
    reopen(x, today)
    complete_work_order(x, today=today, **VENDOR_DONE)
    x.refresh_from_db()
    assert (x.completed_on, x.repair_check) == (today, RepairCheck.VENDOR_REPORT) and not rules.counted(x)
    assert uses_on(x) == [("TE-V", today - 30 * DAY)] and services.kept_ids(x) == {te.pk}
    words = services.KEPT.format(tag="TE-V", number=x.number, day=fmt(today - 30 * DAY))
    client.force_login(users.manager)
    r = post(client, wo_url(x, "test-equipment/"), {"test_equipment": []})
    assert r.status_code == 400 and r.json() == {"test_equipment": [words]}, r.content
    assert refused(set_test_equipment, x, [], by=users.director) == {"test_equipment": words}
    assert tags_on(x) == ["TE-V"] and not TestEquipmentUse.history.filter(work_order_id=x.pk, history_type="-").exists()
    reopen(x, today)
    complete_work_order(x, resolution="Checked by CE", today=today, **CHECKED)
    x.refresh_from_db()
    assert x.completed_on == today and rules.counted(x) and tags_on(x) == ["TE-V"]
    review.refresh_from_db()
    assert rules.reviewed_ids(review) == {str(x.pk)}
    r = post(client, wo_url(x, "test-equipment/"), {"test_equipment": []})
    assert r.status_code == 400 and r.json() == {"test_equipment": [words]}
    assert tags_on(x) == ["TE-V"]


def test_r10_an_in_house_pm_completed_again_on_a_later_day_keeps_a_judged_use(client, today, pump, users):
    """The verifier's second path: a PM 30 days ago recorded TE-P; a certificate 10 days ago (in tolerance: any calibration since judges
    the work). Reopened and completed today with the test equipment left out, TE-P stays; [] is refused naming the first day through
    the drawer's Change, the API, the service, and another completion (U1: the use's own day); nothing is ever taken off."""
    te = in_date("TE-P")
    pm = pm_with(pump, te, completed_on=today - 30 * DAY)
    certificate(te, today - 10 * DAY)
    reopen(pm, today)
    complete_work_order(pm, resolution="Done again", pm_result=PmResult.PASS, today=today)
    pm.refresh_from_db()
    assert pm.completed_on == today and tags_on(pm) == ["TE-P"] and services.kept_ids(pm) == {te.pk}
    words = services.KEPT.format(tag="TE-P", number=pm.number, day=fmt(today - 30 * DAY))
    client.force_login(users.technician)
    r = client.post(f"/work-orders/{pm.number}/test-equipment/", {"shown_test_equipment": "1"}, **HX)
    assert r.status_code == 200 and escape(words) in r.content.decode() and "HX-Retarget" not in r
    r = post(client, wo_url(pm, "test-equipment/"), {"test_equipment": []})
    assert r.status_code == 400 and r.json() == {"test_equipment": [words]}, r.content
    assert refused(set_test_equipment, pm, [], by=users.manager) == {"test_equipment": words}
    reopen(pm, today)
    assert refused(complete_work_order, pm, resolution="Done", pm_result=PmResult.PASS, test_equipment=[], today=today) == {
        "test_equipment": words}
    assert tags_on(pm) == ["TE-P"] and not TestEquipmentUse.history.filter(work_order_id=pm.pk, history_type="-").exists()


def test_r10_a_use_assign_kept_on_a_vendor_pm_survives_its_completion_and_the_apis_empty_list(client, today, pump, users):
    """The verifier's third path: a PM 30 days ago recorded TE-K; a certificate 10 days ago; reopened today and assigned to a vendor
    (assign keeps the judged use); completed today by the vendor (vendor PM: not offered, so the completion's KEEP takes off what the rule
    allows, and that is nothing). The API's [] is refused in KEPT's words naming the first day, never erasing it."""
    te = in_date("TE-K")
    pm = pm_with(pump, te, completed_on=today - 30 * DAY)
    certificate(te, today - 10 * DAY)
    reopen(pm, today)
    assign(pm, vendor_name="Philips")
    assert tags_on(pm) == ["TE-K"]
    complete_work_order(WorkOrder.objects.get(pk=pm.pk), resolution="Serviced by the vendor", pm_result=PmResult.PASS, today=today)
    pm.refresh_from_db()
    assert (pm.status, pm.completed_on, pm.vendor_service) == (WoStatus.COMPLETED, today, True) and not rules.offered(pm)
    assert tags_on(pm) == ["TE-K"]
    words = services.KEPT.format(tag="TE-K", number=pm.number, day=fmt(today - 30 * DAY))
    client.force_login(users.manager)
    r = post(client, wo_url(pm, "test-equipment/"), {"test_equipment": []})
    assert r.status_code == 400 and r.json() == {"test_equipment": [words]}, r.content
    assert refused(set_test_equipment, pm, [], by=users.director) == {"test_equipment": words}
    assert tags_on(pm) == ["TE-K"] and not TestEquipmentUse.history.filter(work_order_id=pm.pk, history_type="-").exists()


def test_u1_a_use_first_recorded_at_a_later_completion_is_judged_on_that_day(today, pump, users):
    """U1 reverses R10's rule here: a PM completed 30 days ago with TE-A, reopened and completed again today with TE-B added. TE-B's
    certificate (10 days ago) predates its use (today), so nothing has judged it: the kept-use rule keeps neither (TE-A's certificate
    is 100 days old), and a list leaving TE-B out, or [], is allowed. The readers judge each use on its own day (TE-A 30 days ago, TE-B
    today)."""
    a, b = in_date("TE-A"), in_date("TE-B")
    pm = pm_with(pump, a, completed_on=today - 30 * DAY)
    certificate(b, today - 10 * DAY)
    reopen(pm, today)
    complete_work_order(pm, resolution="Done again", pm_result=PmResult.PASS, test_equipment=[str(a.pk), str(b.pk)], today=today)
    pm.refresh_from_db()
    assert uses_on(pm) == [("TE-A", today - 30 * DAY), ("TE-B", today)] and services.kept_ids(pm) == set()
    assert [(j.item.tag, j.day) for j in services.judged_for(pm)] == [("TE-A", today - 30 * DAY), ("TE-B", today)]
    set_test_equipment(pm, [a.pk], by=users.manager)
    assert tags_on(pm) == ["TE-A"]
    set_test_equipment(pm, [], by=users.manager)
    assert tags_on(pm) == []


# --- R11: a completion as checked by the vendor takes back the kept ids ------------------------------------------------------------

def test_r11_the_apis_transition_as_checked_by_the_vendor_accepts_the_kept_ids_and_refuses_a_new_one(client, today, pump, users):
    """A vendor repair CE checked 30 days ago recorded TE-V (calibrated 20 days ago: kept) and TE-F (free); reopened. Completed as checked
    by the vendor: [] is refused in KEPT's words; a new id, alone or beside the kept one, in VENDOR_CHECKED's (nothing completes); the
    kept id alone completes (200): TE-V stays, TE-F comes off with the completion. The words no longer go in a circle (V3: the refusal
    names TE-V as the one to send)."""
    te, free, new = item("TE-V"), item("TE-F"), in_date("TE-N")
    certificate(te, today - 200 * DAY)
    certificate(free, today - 200 * DAY)
    x = vendor_job(pump, completed_on=today - 30 * DAY, test_equipment=[str(te.pk), str(free.pk)])
    certificate(te, today - 20 * DAY)
    assert services.kept_ids(x) == {te.pk}
    reopen(x, today - 5 * DAY)
    client.force_login(users.manager)
    url = wo_url(x, "transition/")
    r = post(client, url, {"status": "completed", **VENDOR_DONE, "test_equipment": []})
    assert r.status_code == 400 and r.json()["test_equipment"] == [services.KEPT.format(tag="TE-V", number=x.number,
                                                                                        day=fmt(today - 30 * DAY))], r.content
    for ids in ([str(te.pk), str(new.pk)], [str(new.pk)]):
        r = post(client, url, {"status": "completed", **VENDOR_DONE, "test_equipment": ids})
        assert r.status_code == 400 and r.json()["test_equipment"] == [
            services.VENDOR_CHECKED_KEPT.format(number=x.number, tags="TE-V", left_out=services.LEFT_OUT)], (ids, r.content)
        x.refresh_from_db()
        assert x.status == WoStatus.IN_PROGRESS and tags_on(x) == ["TE-F", "TE-V"]
    r = post(client, url, {"status": "completed", **VENDOR_DONE, "test_equipment": [str(te.pk)]})
    assert r.status_code == 200, r.content
    x.refresh_from_db()
    assert (x.status, x.repair_check) == (WoStatus.COMPLETED, RepairCheck.VENDOR_REPORT) and tags_on(x) == ["TE-V"]
    assert [row["tag"] for row in r.json()["test_equipment"]] == ["TE-V"]
    gone = TestEquipmentUse.history.filter(work_order_id=x.pk, history_type="-")
    # U2: the free use comes off as KEEP takes it off, in its words
    assert [(g.test_equipment_id, g.history_change_reason) for g in gone] == [(free.pk, "Taken off: completed as checked by the vendor")]


def test_r11_the_service_completes_as_checked_by_the_vendor_with_the_kept_ids(today, pump, users):
    te = item("TE-V")
    certificate(te, today - 200 * DAY)
    x = checked_vendor_repair(pump, te, today - 30 * DAY)
    certificate(te, today - 20 * DAY)
    reopen(x, today)
    errors = refused(complete_work_order, x, today=today, test_equipment=[str(te.pk), str(in_date("TE-N").pk)], by=users.manager,
                     **VENDOR_DONE)
    assert errors == {"test_equipment": services.VENDOR_CHECKED_KEPT.format(number=x.number, tags="TE-V", left_out=services.LEFT_OUT)}
    complete_work_order(x, today=today, test_equipment=[str(te.pk)], by=users.manager, **VENDOR_DONE)
    x.refresh_from_db()
    assert x.status == WoStatus.COMPLETED and tags_on(x) == ["TE-V"]


# --- R12: an item added with a failed first certificate is never in use -----------------------------------------------------------

def _failed_certificate(today, number="C-F1"):
    return {"calibrated_on": today - 5 * DAY, "due_on": None, "provider": "Fluke Biomedical", "certificate": number,
            "accreditation": Mark.SHOWN, "as_found": AsFound.OUT_OF_TOLERANCE, "adjusted": False, "result": Result.FAILED}


def test_r12_an_item_added_today_with_a_failed_first_certificate_is_never_usable_today(client, today, pump, users):
    """Added today with a failed certificate (the service, and the web's Add test equipment): out of service (failed calibration) from
    its first history row, never in use for a moment today, so in_use_today and usable_today leave it out, the picker never offers it,
    and today's completion refuses it in words. One added today with a passed certificate is in use and offered."""
    bad = item("TE-BAD", calibration=_failed_certificate(today))
    good = item("TE-GOOD", calibration={**_failed_certificate(today, "C-G1"), "as_found": AsFound.IN_TOLERANCE, "result": Result.PASSED})
    # V1: status since today (the day it was added); held since the failed certificate's own day (5 days ago)
    assert (bad.status, bad.status_reason, bad.status_since, bad.held_since) == (
        Status.OUT_OF_SERVICE, Reason.FAILED_CALIBRATION, today, today - 5 * DAY)
    assert set(TestEquipment.history.filter(id=bad.pk).values_list("status", flat=True)) == {Status.OUT_OF_SERVICE}
    assert good.status == Status.IN_USE
    assert rules.in_use_today([bad, good], today) == {good.pk} and not rules.usable_today(bad, today)
    wo = open_pm(pump)
    assert offered_tags(wo, users.technician, today) == ["TE-GOOD"]
    errors = refused(complete_work_order, wo, resolution="Done", pm_result=PmResult.PASS, test_equipment=[str(bad.pk)], today=today)
    assert errors == {"test_equipment": not_usable(bad, "out of service (failed calibration)")}
    assert tags_on(wo) == [] and WorkOrder.objects.get(pk=wo.pk).status == WoStatus.IN_PROGRESS
    # the web's Add test equipment with its failed certificate
    client.force_login(users.technician)
    client.post("/test-equipment/new/", new(tag="TE-WEB", serial="9988", calibrated_on=(today - 5 * DAY).isoformat(),
                                            provider="Fluke Biomedical", certificate="CAL-9", accreditation="shown",
                                            as_found="out_of_tolerance", result="failed"), **HX)
    web = TestEquipment.objects.get(tag="TE-WEB")
    assert (web.status, web.status_reason) == (Status.OUT_OF_SERVICE, Reason.FAILED_CALIBRATION)
    assert set(TestEquipment.history.filter(id=web.pk).values_list("status", flat=True)) == {Status.OUT_OF_SERVICE}
    assert rules.in_use_today([web], today) == set() and "TE-WEB" not in offered_tags(wo, users.technician, today)


# --- R13: back from retired, judged against the day out, kept through a round trip ------------------------------------------------

def _damaged_retired_then_converted(today, users, tag):
    """Passed 300 days ago (its only pass), damaged 30 days ago (its day out), retired 26 days ago; a failed certificate dated two days
    after the damage, typed while retired (moves nothing); reinstated 20 days ago: out of service for a failed calibration, held since
    that certificate's day (R4's conversion; U3: the failed certificate's own day; V1: status since the reinstate). Returns (item, the
    failed certificate, the day out)."""
    te = item(tag)
    certificate(te, today - 300 * DAY)
    day_out = today - 30 * DAY
    services.set_status(te, to=Status.OUT_OF_SERVICE, reason=Reason.DAMAGED, by=users.technician, today=day_out)
    services.set_status(te, to=Status.RETIRED, reason=Reason.WORN_OUT, by=users.director, today=today - 26 * DAY)
    failed = certificate(te, day_out + 2 * DAY, result=Result.FAILED, as_found=AsFound.OUT_OF_TOLERANCE, today=today - 25 * DAY)
    back = services.set_status(te, to=Status.IN_USE, by=users.director, today=today - 20 * DAY)
    assert (back.status, back.status_reason, back.status_since, back.held_since) == (
        Status.OUT_OF_SERVICE, Reason.FAILED_CALIBRATION, today - 20 * DAY, day_out + 2 * DAY)
    return te, failed, day_out


def test_r13_a_converting_certificate_entered_in_error_never_lets_retire_and_reinstate_release_it(today, users):
    """The verifier's case: the failed certificate that converted the reason is marked entered in error; retired again and reinstated,
    the item lands out of service (the history's reason and day: failed calibration since the converting certificate's day), never in
    use on the pass from 300 days ago; a pass dated after that day puts it back."""
    te, failed, day_out = _damaged_retired_then_converted(today, users, "TE-EE")
    services.mark_entered_in_error(failed, by=None, today=today - 18 * DAY)
    services.set_status(te, to=Status.RETIRED, reason=Reason.WORN_OUT, by=users.director, today=today - 15 * DAY)
    again = services.set_status(te, to=Status.IN_USE, by=users.director, today=today - 10 * DAY)
    assert (again.status, again.status_reason, again.status_since, again.held_since) == (
        Status.OUT_OF_SERVICE, Reason.FAILED_CALIBRATION, today - 10 * DAY, day_out + 2 * DAY)
    assert last_note(te) == "Reinstated: out of service (failed calibration)"
    assert not rules.usable_today(again, today)
    certificate(te, today - 5 * DAY, today=today - 5 * DAY)
    te.refresh_from_db()
    assert te.status == Status.IN_USE


def test_r13_after_the_conversion_a_pass_dated_before_the_damage_never_releases_it(today, users):
    """The same item, not retired again: once the converting certificate is marked entered in error, nothing on file says it passed
    since its day out (the damage), so a passed certificate dated the day before the damage (newer than its pass of 300 days ago) must
    leave it out of service, as it would have before the conversion (damaged: a pass dated on or after the day out)."""
    te, failed, day_out = _damaged_retired_then_converted(today, users, "TE-EE2")
    services.mark_entered_in_error(failed, by=None, today=today - 18 * DAY)
    certificate(te, day_out - DAY, today=today - 5 * DAY)
    te.refresh_from_db()
    assert te.status == Status.OUT_OF_SERVICE, (te.status, te.status_reason)


def test_r13_a_failed_calibration_entered_in_error_never_lets_a_retired_item_back_on_an_older_pass(today, users):
    """The verifier's second case: failed 30 days ago (out of service since), retired, then the failed certificate itself is marked
    entered in error: Reinstate lands out of service (failed calibration, since the day out) as the item stood when it was retired, never
    in use on the pass from 300 days ago; a new passed calibration puts it back."""
    te = item("TE-FE")
    certificate(te, today - 300 * DAY)
    failed = certificate(te, today - 30 * DAY, result=Result.FAILED, as_found=AsFound.OUT_OF_TOLERANCE, today=today - 30 * DAY)
    te.refresh_from_db()
    assert (te.status, te.status_reason, te.status_since, te.held_since) == (
        Status.OUT_OF_SERVICE, Reason.FAILED_CALIBRATION, today - 30 * DAY, today - 30 * DAY)
    services.set_status(te, to=Status.RETIRED, reason=Reason.WORN_OUT, by=users.director, today=today - 20 * DAY)
    assert TestEquipment.objects.get(pk=te.pk).held_since is None  # retired: held_since is only for an item out of service
    services.mark_entered_in_error(failed, by=None, today=today - 15 * DAY)
    back = services.set_status(te, to=Status.IN_USE, by=users.director, today=today)
    assert (back.status, back.status_reason, back.status_since, back.held_since) == (
        Status.OUT_OF_SERVICE, Reason.FAILED_CALIBRATION, today, today - 30 * DAY)
    certificate(te, today)
    te.refresh_from_db()
    assert te.status == Status.IN_USE


def test_r13_a_round_trip_keeps_the_day_out(today, users):
    """Damaged 30 days ago, retired and reinstated three times: each time out of service (damaged) held since the day out, the history's
    (V1: status since each reinstate), and the words ask for a pass dated on or after it (never the reinstate's day)."""
    te = in_date("TE-RT2")
    day_out = today - 30 * DAY
    services.set_status(te, to=Status.OUT_OF_SERVICE, reason=Reason.DAMAGED, by=users.technician, today=day_out)
    words = f"TE-RT2 is out of service (damaged): it goes back in use when a passed calibration dated on or after {fmt(day_out)} is recorded."
    for retired, back_on in ((25, 20), (15, 10), (5, 0)):
        services.set_status(te, to=Status.RETIRED, reason=Reason.WORN_OUT, by=users.director, today=today - retired * DAY)
        back = services.set_status(te, to=Status.IN_USE, by=users.director, today=today - back_on * DAY)
        assert (back.status, back.status_reason, back.status_since, back.held_since) == (
            Status.OUT_OF_SERVICE, Reason.DAMAGED, today - back_on * DAY, day_out), back_on
        assert services.back_in_use_words(back) == words


@pytest.mark.parametrize("typed_while", ["retired-again", "out-of-service"])
def test_r13_a_pass_after_the_day_out_but_before_the_first_reinstate_releases_it(today, users, typed_while):
    """Damaged 30 days ago, retired, reinstated 20 days ago (out of service since the day out); a pass dated 22 days ago (after the day
    out, before that reinstate): typed while retired again, the second Reinstate puts it in use; typed while out of service, it goes back
    in use at once. Before R13 the reinstate's day was the day out, and the pass never released it."""
    te = in_date("TE-RT3")
    day_out = today - 30 * DAY
    services.set_status(te, to=Status.OUT_OF_SERVICE, reason=Reason.DAMAGED, by=users.technician, today=day_out)
    services.set_status(te, to=Status.RETIRED, reason=Reason.WORN_OUT, by=users.director, today=today - 25 * DAY)
    assert services.set_status(te, to=Status.IN_USE, by=users.director, today=today - 20 * DAY).held_since == day_out
    if typed_while == "retired-again":
        services.set_status(te, to=Status.RETIRED, reason=Reason.WORN_OUT, by=users.director, today=today - 15 * DAY)
        certificate(te, today - 22 * DAY, today=today - 12 * DAY)
        te.refresh_from_db()
        assert te.status == Status.RETIRED  # typed while retired: it moves nothing
        back = services.set_status(te, to=Status.IN_USE, by=users.director, today=today - 10 * DAY)
        assert (back.status, back.status_reason, back.status_since) == (Status.IN_USE, "", today - 10 * DAY)
        assert last_note(te) == "Reinstated"
    else:
        certificate(te, today - 22 * DAY, today=today - 12 * DAY)
        te.refresh_from_db()
        assert (te.status, te.status_since, te.held_since) == (Status.IN_USE, today - 12 * DAY, None)


# --- R14: a cancel takes off the uses but the kept ones -------------------------------------------------------------------------

def test_r14_a_cancel_takes_off_the_uses_but_kept_ones_audited_and_the_apis_patch_sends_back_its_empty_list(client, today, pump, users):
    """A PM 30 days ago recorded TE-K (calibrated 10 days ago: kept) and TE-F (free); reopened, moved back to open, and cancelled through
    the API's transition: TE-F comes off, "Test equipment cleared: cancelled", by the user who cancelled; TE-K stays (a judged use is
    never erased). The field reads [] (the response, a GET); a PATCH sending that [] back succeeds; one sending the hidden kept id is
    refused, as any id the field does not show."""
    kept, free = in_date("TE-K"), in_date("TE-F")
    wo = pm_with(pump, kept, free, completed_on=today - 30 * DAY)
    certificate(kept, today - 10 * DAY)
    reopen(wo, today - 5 * DAY)
    change_status(wo, WoStatus.OPEN)
    client.force_login(users.manager)
    r = post(client, wo_url(wo, "transition/"), {"status": "cancelled"})
    assert r.status_code == 200, r.content
    assert r.json()["test_equipment"] == []
    wo.refresh_from_db()
    assert wo.status == WoStatus.CANCELLED and tags_on(wo) == ["TE-K"]
    gone = TestEquipmentUse.history.filter(work_order_id=wo.pk, history_type="-")
    assert [(g.test_equipment_id, g.history_change_reason, g.history_user_id) for g in gone] == [
        (free.pk, "Test equipment cleared: cancelled", users.manager.pk)]
    assert client.get(wo_url(wo)).json()["test_equipment"] == []
    r = client.patch(wo_url(wo), {"test_equipment": []}, content_type="application/json")
    assert r.status_code == 200, r.content
    assert r.json()["test_equipment"] == []
    r = client.patch(wo_url(wo), {"test_equipment": [str(kept.pk)]}, content_type="application/json")
    assert r.status_code == 400 and r.json() == {"test_equipment": [TEST_EQUIPMENT_RECORDED]}, r.content
    assert tags_on(wo) == ["TE-K"] and drawer_row(client, wo) == "" and services.work_done(kept) == []


def test_r14_the_services_cancel_clears_every_use_nothing_keeps(today, pump):
    """change_status's cancel through the service (by=None, the system): every use the kept-use rule allows comes off, audited; a
    cancel with none recorded writes nothing."""
    a, b = in_date("TE-A"), in_date("TE-B")
    wo = pm_with(pump, a, b, completed_on=today - 30 * DAY)
    reopen(wo, today - 5 * DAY)
    change_status(wo, WoStatus.OPEN)
    change_status(wo, WoStatus.CANCELLED)
    assert tags_on(wo) == []
    gone = TestEquipmentUse.history.filter(work_order_id=wo.pk, history_type="-")
    assert sorted(g.test_equipment_id for g in gone) == sorted([a.pk, b.pk])
    assert {g.history_change_reason for g in gone} == {"Test equipment cleared: cancelled"}
    bare = create_work_order(asset=pump, type=WoType.REPAIR, priority=Priority.NORMAL, problem="Alarm", opened_on=today)
    change_status(bare, WoStatus.CANCELLED)
    assert services.clear_for_cancel(bare, by=None) == 0


# --- Round 4 (the round 3 verifier's) ---------------------------------------------------------------------------------------------

def _option(wo, user, today, tag):
    """The picker's option for `tag` on `wo` (Yours or the others)."""
    pick = services.picker(WorkOrder.objects.get(pk=wo.pk), user, today=today)
    return next(o for o in [*pick.yours, *(o for _label, options in pick.others for o in options)] if o.item.tag == tag)


def test_u1_a_mis_tick_at_a_later_completion_can_be_taken_off(client, today, pump, users):
    """The verifier's problem 1, on a real later day: a PM first completed 30 days ago with nothing recorded; a routine passed certificate
    of TE-L dated 20 days ago; the PM reopened and completed today with TE-L ticked by mistake. Its use is recorded today (used_on), after
    the certificate, so nothing has judged it: kept_ids is empty, the picker never marks it kept, and the API's [] takes it off. Added
    again by Change after that second completion (on its day, today), the service's [] takes it off too."""
    te = in_date("TE-L")
    pm = pm_with(pump, completed_on=today - 30 * DAY)
    certificate(te, today - 20 * DAY)
    reopen(pm, today)
    complete_work_order(pm, resolution="Done again", pm_result=PmResult.PASS, test_equipment=[str(te.pk)], today=today)
    pm.refresh_from_db()
    assert pm.completed_on == today and uses_on(pm) == [("TE-L", today)] and services.kept_ids(pm) == set()
    option = _option(pm, users.technician, today, "TE-L")
    assert option.on_work_order and not option.kept
    client.force_login(users.manager)
    r = post(client, wo_url(pm, "test-equipment/"), {"test_equipment": []})
    assert r.status_code == 200 and r.json()["test_equipment"] == [], r.content
    assert tags_on(pm) == []
    set_test_equipment(pm, [te.pk], by=users.manager)
    assert uses_on(pm) == [("TE-L", today)] and services.kept_ids(pm) == set()
    set_test_equipment(pm, [], by=users.director)
    assert tags_on(pm) == []
    gone = TestEquipmentUse.history.filter(work_order_id=pm.pk, history_type="-")
    assert sorted(g.history_change_reason for g in gone) == ["Taken off after completion", "Taken off after completion"]


def test_u1_an_undecided_review_keeps_a_use_reopened_and_completed_again(client, today, pump, users):
    """The verifier's problem 2: a PM 30 days ago recorded TE-U; a certificate 10 days ago found it out of tolerance, not reviewed yet.
    Reopened and completed today with the test equipment left out, TE-U stays on its own day (30 days ago), so it stays in the
    certificate's window: the review's work (the API's list, the item's review box, and the review CSV give that day beside completed_on),
    the work order's drawer row and the API's field ("found out of tolerance ...; not reviewed"), Work done with it, and the decision's
    frozen list all read it; [] is refused in KEPT's words naming that day; the item's work since its last calibration leaves it out."""
    te = item("TE-U")
    certificate(te, today - 200 * DAY)
    pm = pm_with(pump, te, completed_on=today - 30 * DAY)
    found = certificate(te, today - 10 * DAY, as_found=AsFound.OUT_OF_TOLERANCE, adjusted=True)
    reopen(pm, today)
    complete_work_order(pm, resolution="Done again", pm_result=PmResult.PASS, today=today)
    pm.refresh_from_db()
    assert pm.completed_on == today and uses_on(pm) == [("TE-U", today - 30 * DAY)] and services.kept_ids(pm) == {te.pk}
    summary = services.review_summary(found)
    assert [(w.pk, w.used_on, w.completed_on) for w in summary.work] == [(pm.pk, today - 30 * DAY, today)]
    [judged] = services.judged_for(pm)
    words = f"found out of tolerance on {fmt(today - 10 * DAY)}; not reviewed"
    assert judged.day == today - 30 * DAY and [f.state for f in judged.later] == [rules.NOT_REVIEWED] and judged.words.endswith(words)
    assert [(j.use.work_order_id, j.day) for j in services.work_done(te)] == [(pm.pk, today - 30 * DAY)]
    assert services.work_since_last_calibration(te) == 0  # its day is before the certificate's, though completed today
    client.force_login(users.manager)
    assert escape(words) in drawer_row(client, pm)
    assert [row["words"] for row in client.get(wo_url(pm)).json()["test_equipment"]] == [judged.words]
    [review] = client.get("/api/v1/test-equipment/TE-U/").json()["reviews"]
    assert [(w["number"], w["used_on"], w["completed_on"]) for w in review["work_orders"]] == [
        (pm.number, (today - 30 * DAY).isoformat(), today.isoformat())]
    box = client.get("/test-equipment/TE-U/", **HX).content.decode()
    assert f"{pm.number}</a> · {fmt(today - 30 * DAY)} · {pump.tag}" in box
    rows = csv_rows(client.get(f"/test-equipment/TE-U/calibrations/{found.pk}/review.csv"))
    assert [(row[0], row[3]) for row in rows[1:]] == [(pm.number, (today - 30 * DAY).isoformat())]
    decided = services.decide_review(found, basis=Basis.WITHIN_MARGIN, decided_on=today, version=summary.version, by=None)
    assert decided.work_orders == [[str(pm.pk), pm.number]] and decided.work_count == 1
    assert services.judged_for(pm)[0].later[0].state == rules.REVIEWED and services.review_summary(found).added == []
    kept = services.KEPT.format(tag="TE-U", number=pm.number, day=fmt(today - 30 * DAY))
    assert refused(set_test_equipment, pm, [], by=users.manager) == {"test_equipment": kept}
    assert tags_on(pm) == ["TE-U"]


def test_u1_each_use_is_judged_on_its_own_day(client, today, pump, users):
    """A PM 30 days ago recorded TE-A, in date that day (due 20 days ago); reopened and completed today with TE-B added: TE-A still reads
    its standing on its own day ("calibration due", never today's "was due"), on the service, the drawer, and the API; TE-B reads today's."""
    a, b = item("TE-A"), in_date("TE-B")
    certificate(a, today - 300 * DAY, due_on=today - 20 * DAY)
    pm = pm_with(pump, a, completed_on=today - 30 * DAY)
    reopen(pm, today)
    complete_work_order(pm, resolution="Done again", pm_result=PmResult.PASS, test_equipment=[str(a.pk), str(b.pk)], today=today)
    judged = services.judged_for(pm)
    assert [(j.item.tag, j.day, j.standing.state) for j in judged] == [("TE-A", today - 30 * DAY, rules.IN_DATE), ("TE-B", today, rules.IN_DATE)]
    assert judged[0].words == (f"TE-A (Fluke Biomedical ESA612): calibration due {fmt(today - 20 * DAY)} on the day of this work (on file "
                               f"since {fmt(today)})")  # its certificate was entered today, after the day of the work
    client.force_login(users.manager)
    row = drawer_row(client, pm)
    assert all(escape(j.words[len(rules.item_name(j.item)):]) in row for j in judged)  # the tag links to the item; its words follow
    assert [row["words"] for row in client.get(wo_url(pm)).json()["test_equipment"]] == [j.words for j in judged]


def test_u1_recheck_progress_reads_the_uses_own_day(today, pump):
    """A use recorded on the affected device before the out-of-tolerance certificate's day is no recheck, even once its work order is
    completed again today; a use added at that re-completion (on today) is."""
    te = item("TE-U")
    certificate(te, today - 200 * DAY)
    pm_with(pump, te, completed_on=today - 30 * DAY)  # the work in question: the pump
    found = certificate(te, today - 10 * DAY, as_found=AsFound.OUT_OF_TOLERANCE, adjusted=True)
    r, s = in_date("TE-R"), in_date("TE-S")
    later = pm_with(pump, r, completed_on=today - 15 * DAY)
    assert services.recheck_progress(found).words == "0 of 1 rechecked"
    reopen(later, today)
    complete_work_order(later, resolution="Done again", pm_result=PmResult.PASS, today=today)
    later.refresh_from_db()
    assert later.completed_on == today and uses_on(later) == [("TE-R", today - 15 * DAY)]
    progress = services.recheck_progress(found)
    assert progress.words == "0 of 1 rechecked" and [d.words for d in progress.devices] == ["not yet"]
    reopen(later, today)
    complete_work_order(later, resolution="Done again", pm_result=PmResult.PASS, test_equipment=[str(r.pk), str(s.pk)], today=today)
    progress = services.recheck_progress(found)
    [device] = progress.devices
    assert progress.words == "1 of 1 rechecked" and (device.work_order.pk, device.day) == (later.pk, today)
    assert device.words == f"rechecked on {fmt(today)} ({later.number})"


def test_u1_the_binder_reads_each_use_on_its_own_day(today, pump):
    """A PM 20 days ago recorded TE-PD (past due since 35 days ago); reopened and completed today with TE-NR (no calibration on file)
    added. The binder reads each use on its own day, though both work order days are today: a period ending 10 days ago lists TE-PD's
    use (its day in the work table) and its FINDING; the last 9 days list only TE-NR's."""
    pd, nr = past_due("TE-PD"), no_record("TE-NR")
    pm = pm_with(pump, pd, completed_on=today - 20 * DAY)
    reopen(pm, today)
    complete_work_order(pm, resolution="Done again", pm_result=PmResult.PASS, test_equipment=[str(pd.pk), str(nr.pk)], today=today)
    assert uses_on(pm) == [("TE-NR", today), ("TE-PD", today - 20 * DAY)]
    earlier = section.build(period(today - 60 * DAY, today - 10 * DAY, today), None)
    assert [(row[0], row[2], row[4]) for row in earlier.table("work").rows()] == [(pm.number, today - 20 * DAY, "TE-PD")]
    [finding] = gaps_of(earlier, BINDER_FINDING)
    assert finding.record == "TE-PD" and f"used on 1 work order while past due, {fmt(today - 20 * DAY)} " in finding.text
    recent = section.build(period(today - 9 * DAY, today, today), None)
    assert [(row[0], row[2], row[4]) for row in recent.table("work").rows()] == [(pm.number, today, "TE-NR")]
    assert [g.record for g in gaps_of(recent, BINDER_FINDING)] == ["TE-NR"]
    for s in (earlier, recent):
        assert {f.label: f.value for f in s.figures}["Work done with test equipment"] == 1


def test_u2_completed_as_checked_by_the_vendor_a_list_names_only_kept_ids(client, today, pump, vent, users):
    """The verifier's problem 3: a vendor repair CE checked 30 days ago recorded TE-F (free: nothing has calibrated it since). Reopened
    and completed today as checked by the vendor, sending TE-F back (the service, and the API's transition with the ids its GET shows)
    is refused in VENDOR_CHECKED's words and nothing completes; left out, it completes and TE-F comes off ("Taken off: completed as
    checked by the vendor", by whoever completed it); afterwards Change (the service and the API's action) never puts it back. Beside a
    kept TE-V, a list naming both is refused; naming only TE-V completes, and the free one comes off in the same words."""
    free = item("TE-F")
    certificate(free, today - 200 * DAY)
    x = checked_vendor_repair(pump, free, today - 30 * DAY)
    reopen(x, today)
    assert refused(complete_work_order, x, today=today, test_equipment=[str(free.pk)], by=users.manager, **VENDOR_DONE) == {
        "test_equipment": services.VENDOR_CHECKED}
    client.force_login(users.manager)
    shown = [row["id"] for row in client.get(wo_url(x)).json()["test_equipment"]]
    assert shown == [str(free.pk)]
    r = post(client, wo_url(x, "transition/"), {"status": "completed", **VENDOR_DONE, "test_equipment": shown})
    assert r.status_code == 400 and r.json()["test_equipment"] == [services.VENDOR_CHECKED], r.content
    x.refresh_from_db()
    assert x.status == WoStatus.IN_PROGRESS and tags_on(x) == ["TE-F"]
    r = post(client, wo_url(x, "transition/"), {"status": "completed", **VENDOR_DONE})
    assert r.status_code == 200 and r.json()["test_equipment"] == [], r.content
    gone = TestEquipmentUse.history.filter(work_order_id=x.pk, history_type="-")
    assert [(g.test_equipment_id, g.history_change_reason, g.history_user_id) for g in gone] == [
        (free.pk, services.VENDOR_TAKEN_OFF, users.manager.pk)]
    assert refused(set_test_equipment, x, [free.pk], by=users.manager) == {"test_equipment": services.VENDOR_CHECKED}
    r = post(client, wo_url(x, "test-equipment/"), {"test_equipment": [str(free.pk)]})
    assert r.status_code == 400 and r.json() == {"test_equipment": [services.VENDOR_CHECKED]}, r.content
    assert tags_on(x) == []
    # beside a kept use
    kept, other = item("TE-V"), item("TE-G")
    certificate(kept, today - 200 * DAY)
    certificate(other, today - 200 * DAY)
    y = vendor_job(vent, completed_on=today - 30 * DAY, test_equipment=[str(kept.pk), str(other.pk)])
    certificate(kept, today - 20 * DAY)
    reopen(y, today)
    assert services.kept_ids(y) == {kept.pk}
    assert refused(complete_work_order, y, today=today, test_equipment=[str(kept.pk), str(other.pk)], by=users.manager, **VENDOR_DONE) == {
        "test_equipment": services.VENDOR_CHECKED_KEPT.format(number=y.number, tags="TE-V", left_out=services.LEFT_OUT)}
    assert tags_on(y) == ["TE-G", "TE-V"]
    complete_work_order(y, today=today, test_equipment=[str(kept.pk)], by=users.manager, **VENDOR_DONE)
    assert uses_on(y) == [("TE-V", today - 30 * DAY)]
    gone = TestEquipmentUse.history.filter(work_order_id=y.pk, history_type="-")
    assert [(g.test_equipment_id, g.history_change_reason) for g in gone] == [(other.pk, services.VENDOR_TAKEN_OFF)]


def test_u3_a_failure_entered_in_error_never_lets_an_older_pass_release_it(today, users):
    """The verifier's problem 4 (a): failed 700 days ago, passed 300 days ago, failed 20 days ago (out of service since that day, the
    certificate's); that failure is marked entered in error: the status stays and by hand it never goes in use; retired and reinstated,
    it lands out of service (failed calibration, since 20 days ago), never in use on the pass of 300 days ago, whatever its earlier
    failure (a control with none reads the same). A pass dated before that day leaves it out; one dated after it puts it back."""
    for tag, old_failure in (("TE-4A", True), ("TE-4C", False)):
        te = item(tag)
        if old_failure:
            certificate(te, today - 700 * DAY, result=Result.FAILED, as_found=AsFound.OUT_OF_TOLERANCE, today=today - 700 * DAY)
        certificate(te, today - 300 * DAY, today=today - 300 * DAY)
        failure = certificate(te, today - 20 * DAY, result=Result.FAILED, as_found=AsFound.OUT_OF_TOLERANCE, today=today - 20 * DAY)
        te.refresh_from_db()
        assert (te.status, te.status_reason, te.status_since, te.held_since) == (
            Status.OUT_OF_SERVICE, Reason.FAILED_CALIBRATION, today - 20 * DAY, today - 20 * DAY), tag
        services.mark_entered_in_error(failure, by=None, today=today - 15 * DAY)
        te.refresh_from_db()
        assert te.status == Status.OUT_OF_SERVICE, tag
        assert refused(services.set_status, te, to=Status.IN_USE, by=users.director) == {"status": services.back_in_use_words(te)}
        services.set_status(te, to=Status.RETIRED, reason=Reason.WORN_OUT, by=users.director, today=today - 10 * DAY)
        back = services.set_status(te, to=Status.IN_USE, by=users.director, today=today - 5 * DAY)
        assert (back.status, back.status_reason, back.status_since, back.held_since) == (
            Status.OUT_OF_SERVICE, Reason.FAILED_CALIBRATION, today - 5 * DAY, today - 20 * DAY), tag
        assert services.back_in_use_words(back) == (f"{tag} is out of service (failed calibration): it goes back in use when a passed "
                                                    f"calibration dated on or after {fmt(today - 20 * DAY)} is recorded.")
        certificate(te, today - 25 * DAY, today=today - 4 * DAY)
        te.refresh_from_db()
        assert te.status == Status.OUT_OF_SERVICE, tag
        certificate(te, today - 3 * DAY, today=today - 3 * DAY)
        te.refresh_from_db()
        assert (te.status, te.status_since, te.held_since) == (Status.IN_USE, today - 3 * DAY, None), tag


def test_u3_after_a_converting_failure_entered_in_error_a_pass_before_the_damage_never_releases_it(today, users):
    """The verifier's problem 4 (b): damaged 50 days ago, retired, a failed certificate dated 40 days ago typed while retired (moves
    nothing), reinstated: out of service for a failed calibration since that certificate's day. That certificate is then marked entered
    in error: a pass dated 55 days ago (before the damage) leaves it out, as it does the same item never retired; one dated after the
    day it is out since puts it back."""
    te = item("TE-4B")
    certificate(te, today - 300 * DAY, today=today - 300 * DAY)
    services.set_status(te, to=Status.OUT_OF_SERVICE, reason=Reason.DAMAGED, by=users.technician, today=today - 50 * DAY)
    services.set_status(te, to=Status.RETIRED, reason=Reason.WORN_OUT, by=users.director, today=today - 48 * DAY)
    failure = certificate(te, today - 40 * DAY, result=Result.FAILED, as_found=AsFound.OUT_OF_TOLERANCE, today=today - 39 * DAY)
    te.refresh_from_db()
    assert te.status == Status.RETIRED
    back = services.set_status(te, to=Status.IN_USE, by=users.director, today=today - 38 * DAY)
    assert (back.status, back.status_reason, back.status_since, back.held_since) == (
        Status.OUT_OF_SERVICE, Reason.FAILED_CALIBRATION, today - 38 * DAY, today - 40 * DAY)
    services.mark_entered_in_error(failure, by=None, today=today - 37 * DAY)
    certificate(te, today - 55 * DAY, today=today - 36 * DAY)
    te.refresh_from_db()
    assert (te.status, te.status_reason) == (Status.OUT_OF_SERVICE, Reason.FAILED_CALIBRATION)
    certificate(te, today - 35 * DAY, today=today - 35 * DAY)
    te.refresh_from_db()
    assert te.status == Status.IN_USE
    control = item("TE-4D")  # the same story, never retired
    certificate(control, today - 300 * DAY, today=today - 300 * DAY)
    services.set_status(control, to=Status.OUT_OF_SERVICE, reason=Reason.DAMAGED, by=users.technician, today=today - 50 * DAY)
    certificate(control, today - 55 * DAY, today=today - 36 * DAY)
    control.refresh_from_db()
    assert (control.status, control.status_reason) == (Status.OUT_OF_SERVICE, Reason.DAMAGED)


@pytest.mark.parametrize("order", ["failed-first", "passed-first"])
def test_u3_a_failure_and_its_passing_successor_release_it_whatever_order_they_are_typed_in(today, users, order):
    """A failed certificate dated 20 days ago and a pass dated 10 days ago, both typed today. Failed first: out of service (status since
    today, V1) held since the failed certificate's own day (the lab's), so the pass, dated after it, puts it back in use (before U3 it
    was held from the day it was typed, today, and that pass never released it). Passed first: the failure typed after is not the newest record and
    moves nothing. Either way it ends in use, the pass governing."""
    te = in_date("TE-ORD")
    if order == "failed-first":
        certificate(te, today - 20 * DAY, result=Result.FAILED, as_found=AsFound.OUT_OF_TOLERANCE)
        te.refresh_from_db()
        assert (te.status, te.status_reason, te.status_since, te.held_since) == (
            Status.OUT_OF_SERVICE, Reason.FAILED_CALIBRATION, today, today - 20 * DAY)
        certificate(te, today - 10 * DAY)
    else:
        certificate(te, today - 10 * DAY)
        certificate(te, today - 20 * DAY, result=Result.FAILED, as_found=AsFound.OUT_OF_TOLERANCE)
    te.refresh_from_db()
    assert (te.status, te.status_reason, te.status_since, te.held_since) == (Status.IN_USE, "", today, None)
    assert rules.standing_on(te, today, te.calibrations.all()).state == rules.IN_DATE


def test_u3_out_for_calibration_a_failure_never_dates_before_the_trip(today, users):
    """Out for calibration since 15 days ago: a failure dated 12 days ago (typed today: status since today, V1) holds it out of service
    from that day, and a pass dated after it releases it; a failure dated before the trip (20 days ago) keeps the day it went out, so a
    pass from before the trip never releases it, while one dated after the trip does."""
    te = in_date("TE-OUT")
    services.set_status(te, to=Status.OUT_FOR_CALIBRATION, by=users.technician, today=today - 15 * DAY)
    certificate(te, today - 12 * DAY, result=Result.FAILED, as_found=AsFound.OUT_OF_TOLERANCE)
    te.refresh_from_db()
    assert (te.status, te.status_reason, te.status_since, te.held_since) == (Status.OUT_OF_SERVICE, Reason.FAILED_CALIBRATION, today, today - 12 * DAY)
    certificate(te, today - 8 * DAY)
    te.refresh_from_db()
    assert te.status == Status.IN_USE
    early = in_date("TE-OUT2")
    services.set_status(early, to=Status.OUT_FOR_CALIBRATION, by=users.technician, today=today - 15 * DAY)
    certificate(early, today - 20 * DAY, result=Result.FAILED, as_found=AsFound.OUT_OF_TOLERANCE)
    early.refresh_from_db()
    assert (early.status, early.status_reason, early.status_since, early.held_since) == (
        Status.OUT_OF_SERVICE, Reason.FAILED_CALIBRATION, today, today - 15 * DAY)
    certificate(early, today - 18 * DAY)
    early.refresh_from_db()
    assert early.status == Status.OUT_OF_SERVICE
    certificate(early, today - 14 * DAY)
    early.refresh_from_db()
    assert early.status == Status.IN_USE


def test_u5_a_cancelled_work_order_has_its_own_words(client, today, pump, users):
    """A PM that recorded TE-C (calibrated since: kept) reopened and cancelled: the API's DELETE refuses in DELETE_CANCELLED's words (never
    "has test equipment recorded"), and the API's action, the service, and the web's Change say it was cancelled and records no test
    equipment (never "is not completed yet"), whatever is sent; the kept use stays."""
    te = in_date("TE-C")
    wo = pm_with(pump, te, completed_on=today - 30 * DAY)
    certificate(te, today - 10 * DAY)
    reopen(wo, today)
    change_status(wo, WoStatus.OPEN)
    change_status(wo, WoStatus.CANCELLED)
    wo.refresh_from_db()
    assert wo.status == WoStatus.CANCELLED and tags_on(wo) == ["TE-C"]
    deleted = (f"{wo.number} was cancelled; the test equipment a calibration since has judged stays on it as the record, so it is not "
               "deleted.")
    words = f"{wo.number} was cancelled: it records no test equipment."
    assert services.delete_refusal(wo) == deleted
    client.force_login(users.director)
    r = client.delete(wo_url(wo))
    assert r.status_code == 400 and r.json() == {"detail": deleted}, r.content
    assert WorkOrder.objects.filter(pk=wo.pk).exists()
    for ids in ([], [str(te.pk)]):
        r = post(client, wo_url(wo, "test-equipment/"), {"test_equipment": ids})
        assert r.status_code == 400 and r.json() == {"test_equipment": [words]}, (ids, r.content)
        assert refused(set_test_equipment, wo, ids, by=users.manager) == {"test_equipment": words}
    assert escape(words) in client.get(f"/work-orders/{wo.number}/test-equipment/", **HX).content.decode()
    assert tags_on(wo) == ["TE-C"]


# --- Round 5 (the round 4 verifier's) ---------------------------------------------------------------------------------------------

def printed_lines(client, wo) -> list:
    """The work order print's test equipment lines, as written in the page."""
    body = client.get(f"/print/work-orders/{wo.number}/").content.decode()
    return re.findall(r"<li>(.*?)</li>", re.search(r'<ul class="te-uses">(.*?)</ul>', body, re.S).group(1), re.S)


def _heading_and_lines(row) -> tuple:
    """The drawer section's heading (its <span>) and its lines' text (tags stripped, spaces collapsed)."""
    heading = re.search(r"<h3>Test equipment <span>(.*?)</span></h3>", row, re.S).group(1)
    lines = [" ".join(re.sub(r"<[^>]+>", "", li).split()) for li in re.findall(r"<li[^>]*>(.*?)</li>", row, re.S)]
    return heading, lines


def test_v2_the_drawers_heading_dates_the_uses_only_when_they_share_a_day(client, today, pump, users):
    """V2 (round 4's bug: "2 used on <the last completion>" though TE-A was used 30 days ago). Uses that share a day: the heading names it
    once ("1 used on <day>") and no line repeats it. A reopened work order completed again keeps TE-A on its own earlier day (U1) and
    adds TE-B today: the heading reads "2 used" with no day, and each line names its own ("used on <day>"); the print's lines likewise
    (shared, the rules' words alone)."""
    a, b = in_date("TE-A"), in_date("TE-B")
    pm = pm_with(pump, a, completed_on=today - 30 * DAY)
    client.force_login(users.manager)
    heading, lines = _heading_and_lines(drawer_row(client, pm))
    assert heading == f"1 used on {fmt(today - 30 * DAY)}" and len(lines) == 1 and "used on" not in lines[0]
    [only] = services.judged_for(pm)
    assert printed_lines(client, pm) == [escape(only.words)]
    reopen(pm, today)
    complete_work_order(pm, resolution="Done again", pm_result=PmResult.PASS, test_equipment=[str(a.pk), str(b.pk)], today=today)
    assert uses_on(pm) == [("TE-A", today - 30 * DAY), ("TE-B", today)]
    heading, lines = _heading_and_lines(drawer_row(client, pm))
    assert heading == "2 used"  # never "2 used on <today>": TE-A was used 30 days ago
    judged = services.judged_for(pm)
    expected = []
    for j in judged:
        name = rules.item_name(j.item)
        expected.append(f"{name} used on {fmt(j.day)}{j.words[len(name):]}")
    assert [j.day for j in judged] == [today - 30 * DAY, today]
    assert lines == expected, lines
    assert lines[0].startswith(f"TE-A (Fluke Biomedical ESA612) used on {fmt(today - 30 * DAY)}: calibration due ")
    assert printed_lines(client, pm) == [escape(line) for line in expected]
    # TE-A (nothing judged it since) taken off by Change: the one use left is today's, so the heading names today once again
    set_test_equipment(pm, [b.pk], by=users.manager)
    heading, lines = _heading_and_lines(drawer_row(client, pm))
    assert heading == f"1 used on {fmt(today)}" and "used on" not in lines[0]


def test_v2_the_change_modal_and_the_csvs_name_the_day_of_the_work(client, today, pump, users):
    """V2: Change's hint says an item already on the work order shows its certificate as it stood on the day it was used (its own day,
    never the last completion's); the item's uses CSV and the review CSV head the use's day "Day of the work" (it is used_on, not the work
    order's completed_on: a reopened PM completed again today keeps 30 days ago), and so does the binder's work table."""
    te = item("TE-U")
    certificate(te, today - 200 * DAY)
    pm = pm_with(pump, te, completed_on=today - 30 * DAY)
    found = certificate(te, today - 10 * DAY, as_found=AsFound.OUT_OF_TOLERANCE, adjusted=True)
    reopen(pm, today)
    complete_work_order(pm, resolution="Done again", pm_result=PmResult.PASS, today=today)
    pm.refresh_from_db()
    assert (pm.completed_on, uses_on(pm)) == (today, [("TE-U", today - 30 * DAY)])
    client.force_login(users.manager)
    modal = client.get(f"/work-orders/{pm.number}/test-equipment/", **HX).content.decode()
    hint = HINT_DAY.format(day=fmt(today))
    assert hint == (f"Tick what was used on {fmt(today)}, the day of this work. Each shows its certificate as it stood that day; one "
                    "already on the work order, as it stood on the day it was used.")
    assert escape(hint) in modal
    uses = csv_rows(client.get("/test-equipment/TE-U/uses.csv"))
    assert uses[0][3] == "Day of the work" and [(r[0], r[3]) for r in uses[1:]] == [(pm.number, (today - 30 * DAY).isoformat())]
    review = csv_rows(client.get(f"/test-equipment/TE-U/calibrations/{found.pk}/review.csv"))
    assert review[0][3] == "Day of the work" and [(r[0], r[3]) for r in review[1:]] == [(pm.number, (today - 30 * DAY).isoformat())]
    work = section.build(period(today - 60 * DAY, today, today), None).table("work")
    assert work.columns[2] == "Day of the work" and [(r[0], r[2]) for r in work.rows()] == [(pm.number, today - 30 * DAY)]
    assert section.UNNAMED_COLUMNS[2] == "Completed on"  # that table shows the work order's own day


def _retired_story(today, users, story):
    """The round 4 verifier's problem 1: an item retired 100 days ago that later lands out of service held since a day before its
    retirement. (a) reinstated in use 80 days ago, then a failed certificate dated 90 days ago typed 10 days ago; (b) retired from use, a
    failed certificate dated 150 days ago typed while retired (60 days ago), reinstated 10 days ago; (c) damaged 120 days ago, retired,
    reinstated 10 days ago. Returns (item, the day it is held from)."""
    te = item(f"TE-{story.upper()}")
    certificate(te, today - 300 * DAY, today=today - 300 * DAY)
    if story == "c":
        services.set_status(te, to=Status.OUT_OF_SERVICE, reason=Reason.DAMAGED, by=users.technician, today=today - 120 * DAY)
    services.set_status(te, to=Status.RETIRED, reason=Reason.WORN_OUT, by=users.director, today=today - 100 * DAY)
    if story == "a":
        assert services.set_status(te, to=Status.IN_USE, by=users.director, today=today - 80 * DAY).status == Status.IN_USE
        certificate(te, today - 90 * DAY, result=Result.FAILED, as_found=AsFound.OUT_OF_TOLERANCE, today=today - 10 * DAY)
        held = today - 90 * DAY
    else:
        if story == "b":
            certificate(te, today - 150 * DAY, result=Result.FAILED, as_found=AsFound.OUT_OF_TOLERANCE, today=today - 60 * DAY)
        services.set_status(te, to=Status.IN_USE, by=users.director, today=today - 10 * DAY)
        held = today - (150 if story == "b" else 120) * DAY
    te.refresh_from_db()
    assert (te.status, te.status_since, te.held_since) == (Status.OUT_OF_SERVICE, today - 10 * DAY, held), story
    return te, held


@pytest.mark.parametrize("story", ["a", "b", "c"])
def test_v1_change_refuses_an_item_on_a_day_it_was_retired(client, today, pump, users, story):
    """V1 (the round 4 verifier's problem 1 (a), (b), (c)): each item is held since a day before its retirement (held_since), but its
    status_since is the day of each change, so rules.retired_throughout reads its history right: Change on a PM completed while it was
    retired (85 and 50 days ago; for (a) only 85, as it was back in use 80 days ago) is refused naming the retirement day (RETIRED_BEFORE,
    the service and the API's action; before V1 each was recorded), and nothing is recorded. Controls: on a day before the retirement it
    was not retired (recorded); after it came back, recorded."""
    te, _held = _retired_story(today, users, story)
    words = services.RETIRED_BEFORE.format(tag=te.tag, day=fmt(today - 100 * DAY))
    retired_days = (85,) if story == "a" else (85, 50)
    client.force_login(users.manager)
    for days_ago in retired_days:
        pm = pm_with(pump, completed_on=today - days_ago * DAY)
        assert rules.retired_throughout(te, today - days_ago * DAY) == today - 100 * DAY, days_ago
        assert refused(set_test_equipment, pm, [te.pk], by=users.manager) == {"test_equipment": words}, days_ago
        r = post(client, wo_url(pm, "test-equipment/"), {"test_equipment": [str(te.pk)]})
        assert r.status_code == 400 and r.json() == {"test_equipment": [words]}, (days_ago, r.content)
        assert tags_on(pm) == []
    later = (50,) if story == "a" else (5,)
    for days_ago in (110, *later):
        pm = pm_with(pump, completed_on=today - days_ago * DAY)
        assert rules.retired_throughout(te, today - days_ago * DAY) is None, days_ago
        set_test_equipment(pm, [te.pk], by=users.manager)
        assert uses_on(pm) == [(te.tag, today - days_ago * DAY)], days_ago


def test_v1_an_item_in_use_this_morning_stays_recordable_after_a_back_dated_failed_certificate_is_typed(today, pump, users):
    """V1 (the round 4 verifier's problem 2): in use since long before today, a failed certificate dated 10 days ago is typed at noon:
    out of service (failed calibration), status since today (the day of the change), held since the certificate's day. It was in use
    this morning, so in_use_today keeps it, the picker offers it, and today's completion records it, flagged by its standing (before V1,
    refused as not in use). A pass dated after the certificate's day (before the day it was typed) releases it. Control: the same
    certificate typed yesterday leaves it out of service since yesterday: never usable today, refused, never offered."""
    te = written_on(in_date("TE-M"), today - 100 * DAY)
    certificate(te, today - 10 * DAY, result=Result.FAILED, as_found=AsFound.OUT_OF_TOLERANCE)
    te.refresh_from_db()
    assert (te.status, te.status_reason, te.status_since, te.held_since) == (
        Status.OUT_OF_SERVICE, Reason.FAILED_CALIBRATION, today, today - 10 * DAY)
    assert rules.in_use_today([te], today) == {te.pk} and rules.usable_today(te, today)
    wo = open_pm(pump)
    assert offered_tags(wo, users.technician, today) == ["TE-M"]
    complete_work_order(wo, resolution="Done", pm_result=PmResult.PASS, test_equipment=[str(te.pk)], today=today)
    assert uses_on(wo) == [("TE-M", today)]
    [judged] = services.judged_for(wo)
    assert judged.flagged and judged.standing.state == rules.FAILED
    earlier = written_on(in_date("TE-Y"), today - 100 * DAY)
    certificate(earlier, today - 10 * DAY, result=Result.FAILED, as_found=AsFound.OUT_OF_TOLERANCE, today=today - DAY)
    earlier.refresh_from_db()
    assert (earlier.status, earlier.status_since, earlier.held_since) == (Status.OUT_OF_SERVICE, today - DAY, today - 10 * DAY)
    assert rules.in_use_today([earlier], today) == set()
    other = open_pm(pump)
    assert "TE-Y" not in offered_tags(other, users.technician, today)
    assert refused(complete_work_order, other, resolution="Done", pm_result=PmResult.PASS, test_equipment=[str(earlier.pk)], today=today) == {
        "test_equipment": not_usable(earlier, "out of service (failed calibration)")}
    certificate(te, today - 5 * DAY)
    te.refresh_from_db()
    assert (te.status, te.status_since, te.held_since) == (Status.IN_USE, today, None)


def test_v1_held_since_is_the_release_rules_day_in_the_drawer_and_the_history(client, today, users):
    """V1: an item in use given a failed certificate dated 20 days ago, typed today: the drawer reads "Out of service (failed calibration)
    since <today>" (the day of the change) and asks for a pass dated on or after the certificate's day (held_since, back_in_use_words), and the item's History
    says "Needs a pass dated on or after" that day. A pass dated 25 days ago leaves it out; one dated 10 days ago (after it, though before
    the day it was typed) releases it, clearing held_since. Out for calibration, held_since stays empty and the trip's day governs: a pass
    from before the trip leaves it out, one on that day releases it. Change never sets held_since."""
    te = in_date("TE-H")
    certificate(te, today - 20 * DAY, result=Result.FAILED, as_found=AsFound.OUT_OF_TOLERANCE)
    te.refresh_from_db()
    assert (te.status_since, te.held_since, services.release_day(te)) == (today, today - 20 * DAY, today - 20 * DAY)
    words = (f"TE-H is out of service (failed calibration): it goes back in use when a passed calibration dated on or after "
             f"{fmt(today - 20 * DAY)} is recorded.")
    assert services.back_in_use_words(te) == words
    client.force_login(users.manager)
    body = client.get("/test-equipment/TE-H/", **HX).content.decode()
    assert f"Out of service (failed calibration) since {fmt(today)}." in body and escape(words) in body
    entries, _following = history.record_history(TestEquipment.objects.get(pk=te.pk))
    changes = [(c.field, c.before, c.after) for e in entries for c in e.changes if c.field == "Needs a pass dated on or after"]
    assert changes == [("Needs a pass dated on or after", history.EMPTY, fmt(today - 20 * DAY))], changes
    errors = refused(services.update_test_equipment, te, held_since=today, by=users.manager)
    assert errors == {"held_since": "Change the status with its own action."}
    certificate(te, today - 25 * DAY)
    te.refresh_from_db()
    assert (te.status, te.held_since) == (Status.OUT_OF_SERVICE, today - 20 * DAY)
    certificate(te, today - 10 * DAY)
    te.refresh_from_db()
    assert (te.status, te.status_since, te.held_since) == (Status.IN_USE, today, None)
    away = in_date("TE-AWAY")
    services.set_status(away, to=Status.OUT_FOR_CALIBRATION, by=users.technician, today=today - 15 * DAY)
    away.refresh_from_db()
    assert (away.status_since, away.held_since, services.release_day(away)) == (today - 15 * DAY, None, today - 15 * DAY)
    certificate(away, today - 16 * DAY)
    away.refresh_from_db()
    assert away.status == Status.OUT_FOR_CALIBRATION
    certificate(away, today - 15 * DAY)
    away.refresh_from_db()
    assert (away.status, away.status_since, away.held_since) == (Status.IN_USE, today, None)


def test_v1_a_status_change_by_hand_holds_from_its_own_day_and_leaving_clears_it(today, users):
    """V1: out of service by hand (damaged, failed a check), held since the day of the move; retired from there, held_since clears (the
    history keeps it, and Reinstate reads it back); every other move leaves it empty."""
    te = in_date("TE-HAND")
    for to, reason, held in ((Status.OUT_OF_SERVICE, Reason.FAILED_CHECK, today - 9 * DAY), (Status.RETIRED, Reason.WORN_OUT, None)):
        services.set_status(te, to=to, reason=reason, by=users.director, today=today - (9 if held else 7) * DAY)
        te.refresh_from_db()
        assert (te.status, te.held_since) == (to, held)
    back = services.set_status(te, to=Status.IN_USE, by=users.director, today=today - 3 * DAY)
    assert (back.status, back.status_reason, back.status_since, back.held_since) == (
        Status.OUT_OF_SERVICE, Reason.FAILED_CHECK, today - 3 * DAY, today - 9 * DAY)
    other = in_date("TE-MOVES")
    for to in (Status.OUT_FOR_CALIBRATION, Status.IN_USE, Status.MISSING, Status.IN_USE):
        services.set_status(other, to=to, by=users.technician, today=today - DAY)
        other.refresh_from_db()
        assert (other.status, other.status_since, other.held_since) == (to, today - DAY, None)


def test_v3_a_refusal_on_work_that_is_not_offered_names_what_to_send_and_each_answer_succeeds(client, today, pump, users):
    """V3 (the round 4 verifier's problem 4): three vendor repairs CE checked 30 days ago, each recording TE-K and TE-L (calibrated since:
    kept) and TE-F (free), reopened. Completed today as checked by the vendor through the API's transition with the ids its GET shows:
    refused in words naming the kept ones as the only ones to send, or leaving test_equipment out, or recording ours with checked and
    ready for use; [] is refused in KEPT's words (the refusal never offered it). Each answer it names then completes: only the kept ids,
    left out (TE-F comes off with the completion either way), or checked by us (with a new one too). After completion, the words name
    the kept ones with no leave-out part, and sending only them saves (nothing changes)."""
    k, kl, free, new = item("TE-K"), item("TE-L"), item("TE-F"), in_date("TE-N")
    for te in (k, kl, free):
        certificate(te, today - 200 * DAY)
    ids = [str(k.pk), str(kl.pk), str(free.pk)]
    only, left_out, ours = [vendor_job(pump, completed_on=today - 30 * DAY, test_equipment=ids) for _ in range(3)]
    certificate(k, today - 20 * DAY)
    certificate(kl, today - 20 * DAY)
    client.force_login(users.manager)
    for x in (only, left_out, ours):
        reopen(x, today)
        assert services.kept_ids(x) == {k.pk, kl.pk}
        shown = [row["id"] for row in client.get(wo_url(x)).json()["test_equipment"]]
        assert sorted(shown) == sorted(ids)
        r = post(client, wo_url(x, "transition/"), {"status": "completed", **VENDOR_DONE, "test_equipment": shown})
        words = services.VENDOR_CHECKED_KEPT.format(number=x.number, tags="TE-K and TE-L", left_out=services.LEFT_OUT)
        assert words == (f"Checked by the vendor: the vendor's report names the test equipment it used, and {x.number} keeps TE-K and "
                         "TE-L (a calibration since has judged the work). Send only TE-K and TE-L, or leave test_equipment out; ours is "
                         "recorded on work completed as checked and ready for use.")
        assert r.status_code == 400 and r.json()["test_equipment"] == [words], r.content
        r = post(client, wo_url(x, "transition/"), {"status": "completed", **VENDOR_DONE, "test_equipment": []})
        kept_words = " ".join(services.KEPT.format(tag=tag, number=x.number, day=fmt(today - 30 * DAY)) for tag in ("TE-K", "TE-L"))
        assert r.status_code == 400 and r.json()["test_equipment"] == [kept_words], r.content
        x.refresh_from_db()
        assert x.status == WoStatus.IN_PROGRESS and tags_on(x) == ["TE-F", "TE-K", "TE-L"]
    # the answers it names
    r = post(client, wo_url(only, "transition/"), {"status": "completed", **VENDOR_DONE, "test_equipment": [str(k.pk), str(kl.pk)]})
    assert r.status_code == 200, r.content
    r = post(client, wo_url(left_out, "transition/"), {"status": "completed", **VENDOR_DONE})
    assert r.status_code == 200, r.content
    for x in (only, left_out):
        x.refresh_from_db()
        assert (x.status, x.repair_check, tags_on(x)) == (WoStatus.COMPLETED, RepairCheck.VENDOR_REPORT, ["TE-K", "TE-L"])
        gone = TestEquipmentUse.history.filter(work_order_id=x.pk, history_type="-")
        assert [(g.test_equipment_id, g.history_change_reason) for g in gone] == [(free.pk, services.VENDOR_TAKEN_OFF)]
    r = post(client, wo_url(ours, "transition/"), {"status": "completed", "resolution": "Checked by CE", "repair_check": "passed",
                                                   "results": CHECKED["results"], "test_equipment": [*ids, str(new.pk)]})
    assert r.status_code == 200, r.content
    ours.refresh_from_db()
    assert ours.repair_check == RepairCheck.PASSED and uses_on(ours) == [
        ("TE-F", today - 30 * DAY), ("TE-K", today - 30 * DAY), ("TE-L", today - 30 * DAY), ("TE-N", today)]
    # after completion: no leave-out part (nothing is being completed), and sending only the kept ones is accepted
    after = services.VENDOR_CHECKED_KEPT.format(number=only.number, tags="TE-K and TE-L", left_out="")
    assert after.endswith("Send only TE-K and TE-L; ours is recorded on work completed as checked and ready for use.")
    assert refused(set_test_equipment, only, [k.pk, kl.pk, new.pk], by=users.manager) == {"test_equipment": after}
    r = post(client, wo_url(only, "test-equipment/"), {"test_equipment": [str(k.pk), str(kl.pk), str(new.pk)]})
    assert r.status_code == 400 and r.json() == {"test_equipment": [after]}, r.content
    r = post(client, wo_url(only, "test-equipment/"), {"test_equipment": [str(k.pk), str(kl.pk)]})
    assert r.status_code == 200, r.content
    assert tags_on(only) == ["TE-K", "TE-L"]


def test_v3_vendor_work_with_a_kept_use_names_it_as_the_one_to_send(today, pump, users):
    """V3 on vendor service (not a repair checked by the vendor): a PM 30 days ago recorded TE-P, calibrated since (kept), reopened and
    assigned to a vendor (assign keeps it). Completing it with a new item is refused in VENDOR_WORK_KEPT's words naming TE-P and the
    leave-out answer; sending only TE-P completes. Afterwards, a new item is refused naming TE-P with no leave-out part. Without a kept
    use, the words are VENDOR_WORK's as before."""
    p, new = in_date("TE-P"), in_date("TE-N")
    pm = pm_with(pump, p, completed_on=today - 30 * DAY)
    certificate(p, today - 10 * DAY)
    reopen(pm, today)
    assign(pm, vendor_name="Philips")
    pm = WorkOrder.objects.get(pk=pm.pk)
    words = services.VENDOR_WORK_KEPT.format(number=pm.number, tags="TE-P", left_out=services.LEFT_OUT)
    assert words == (f"{pm.number} is vendor service: the vendor's report names the test equipment it used, and it keeps TE-P (a "
                     "calibration since has judged the work). Send only TE-P, or leave test_equipment out.")
    assert refused(complete_work_order, pm, resolution="Serviced by the vendor", pm_result=PmResult.PASS,
                   test_equipment=[str(p.pk), str(new.pk)], today=today, by=users.manager) == {"test_equipment": words}
    complete_work_order(pm, resolution="Serviced by the vendor", pm_result=PmResult.PASS, test_equipment=[str(p.pk)], today=today,
                        by=users.manager)
    pm.refresh_from_db()
    assert (pm.status, tags_on(pm)) == (WoStatus.COMPLETED, ["TE-P"])
    assert refused(set_test_equipment, pm, [p.pk, new.pk], by=users.manager) == {
        "test_equipment": services.VENDOR_WORK_KEPT.format(number=pm.number, tags="TE-P", left_out="")}
    bare = vendor_job(pump, completed_on=today - 3 * DAY, type_=WoType.PM, pm_result=PmResult.PASS)
    assert refused(set_test_equipment, bare, [new.pk], by=users.manager) == {
        "test_equipment": services.VENDOR_WORK.format(number=bare.number)}


# --- PostgreSQL ------------------------------------------------------------------------------------------------------------------

@needs_postgres
def test_the_fixes_under_the_policies(today, pump, tenant, make_user, other_tenant):
    with tenant_context(other_tenant):
        item("TE-THEIRS", serial="1234567")  # another facility's unit with the same serial never counts here
    director, manager = make_user("director"), make_user("manager")
    user = no_equipment_view(tenant)
    as_app_role()
    # F2: the history read under the policies
    te = in_date("TE-03")
    services.set_status(te, to=Status.OUT_OF_SERVICE, reason=Reason.DAMAGED, by=manager, today=today - 20 * DAY)
    services.set_status(te, to=Status.RETIRED, reason=Reason.WORN_OUT, by=director, today=today - 10 * DAY)
    assert services.set_status(te, to=Status.IN_USE, by=director).status == Status.OUT_OF_SERVICE
    # F3: under the tag lock
    first = item("TE-01", serial="1234567")
    services.set_status(first, to=Status.RETIRED, reason=Reason.WORN_OUT, by=director)
    item("TE-02", serial="1234567")
    assert refused(services.set_status, first, to=Status.IN_USE, by=director) == {
        "status": "Fluke Biomedical ESA612 serial 1234567 is already TE-02: one item, one tag."}
    # F4 and F1
    tv = in_date("TE-V")
    x = checked_vendor_repair(pump, tv, today - 30 * DAY)
    assert refused(set_test_equipment, x, [], by=user) == {"test_equipment": services.VIEW_NEEDED}
    reopen(x, today - 5 * DAY)
    complete_work_order(x, by=manager, today=today - 3 * DAY, **VENDOR_DONE)
    assert tags_on(x) == []
    # F6
    fresh = item("TE-08")
    services.update_test_equipment(fresh, tag="TE-09", by=manager)
    entries, _more = history.change_log(director, areas=["test_equipment"])
    assert {e.url for e in entries if e.record in ("TE-08", "TE-09")} == {"/test-equipment/TE-09/"}


@needs_postgres
def test_round_3_under_the_policies(today, pump, make_user):
    """Round 3 as the runtime role: R10's judged use kept (on its own day, U1), R12's add, R13's day out from the item's history, and
    R14's cancel."""
    director, manager = make_user("director"), make_user("manager")
    as_app_role()
    # R10
    te = in_date("TE-P")
    pm = pm_with(pump, te, completed_on=today - 30 * DAY)
    certificate(te, today - 10 * DAY)
    reopen(pm, today)
    complete_work_order(pm, resolution="Done again", pm_result=PmResult.PASS, by=manager, today=today)
    pm.refresh_from_db()
    assert uses_on(pm) == [("TE-P", today - 30 * DAY)] and services.kept_ids(pm) == {te.pk}
    assert refused(set_test_equipment, pm, [], by=manager) == {
        "test_equipment": services.KEPT.format(tag="TE-P", number=pm.number, day=fmt(today - 30 * DAY))}
    # R12
    bad = item("TE-BAD", calibration=_failed_certificate(today), by=manager)
    assert bad.status == Status.OUT_OF_SERVICE and rules.in_use_today([bad], today) == set()
    # R13
    rt = in_date("TE-RT")
    services.set_status(rt, to=Status.OUT_OF_SERVICE, reason=Reason.DAMAGED, by=manager, today=today - 30 * DAY)
    services.set_status(rt, to=Status.RETIRED, reason=Reason.WORN_OUT, by=director, today=today - 20 * DAY)
    back = services.set_status(rt, to=Status.IN_USE, by=director, today=today)
    assert (back.status, back.status_since, back.held_since) == (Status.OUT_OF_SERVICE, today, today - 30 * DAY)
    # R14
    free = in_date("TE-F")
    wo = pm_with(pump, free, completed_on=today - 20 * DAY)
    reopen(wo, today - 5 * DAY)
    change_status(wo, WoStatus.OPEN, by=manager)
    change_status(wo, WoStatus.CANCELLED, by=manager)
    assert tags_on(wo) == []


def _thread(target, errors, results, key):
    def run():
        try:
            results[key] = target()
        except Exception as e:  # noqa: BLE001 - reported by the test
            errors[key] = e
        finally:
            connection.close()
    return threading.Thread(target=run, name=key)


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_f3_two_reinstates_of_one_unit_at_once(tenant, monkeypatch):
    """Two retired records of one unit reinstated at once: the first holds the facility's tag lock while it sleeps after its duplicate
    check; the second waits for it, then finds the first live and refuses in words (without the lock both would be live)."""
    with tenant_context(tenant):
        a = item("TE-A", serial="777")
        services.set_status(a, to=Status.RETIRED, reason=Reason.WORN_OUT, by=None)
        b = item("TE-B", serial="777")
        services.set_status(b, to=Status.RETIRED, reason=Reason.WORN_OUT, by=None)
    inside = threading.Event()
    real = services._same_unit

    def slow(*args, **kwargs):
        found = real(*args, **kwargs)
        if threading.current_thread().name == "first":
            inside.set()
            time.sleep(1)
        return found

    monkeypatch.setattr(services, "_same_unit", slow)
    errors, results = {}, {}

    def reinstate(te):
        def call():
            with tenant_context(tenant):
                return services.set_status(te, to=Status.IN_USE, by=None).status
        return call

    first, second = _thread(reinstate(a), errors, results, "first"), _thread(reinstate(b), errors, results, "second")
    first.start()
    assert inside.wait(20)
    second.start()
    for t in (first, second):
        t.join(30)
    assert results.get("first") == Status.IN_USE and isinstance(errors.get("second"), ValidationError), (results, errors)
    assert errors["second"].message_dict == {"status": ["Fluke Biomedical ESA612 serial 777 is already TE-A: one item, one tag."]}
    with tenant_context(tenant):
        assert list(TestEquipment.objects.filter(status__in=services.LIVE_STATUSES).values_list("tag", flat=True)) == ["TE-A"]



@needs_postgres
def test_round_4_under_the_policies(today, pump, users):
    """Round 4 as the runtime role: U1's used_on written and read (a mis-tick at a later completion taken off; a judged use kept, in its
    window, on its own day), U2's refusal, U3's day out, and U5's words."""
    as_app_role()
    # U1
    te = in_date("TE-L")
    pm = pm_with(pump, completed_on=today - 30 * DAY)
    certificate(te, today - 20 * DAY)
    reopen(pm, today)
    complete_work_order(pm, resolution="Done again", pm_result=PmResult.PASS, test_equipment=[str(te.pk)], by=users.manager, today=today)
    assert uses_on(pm) == [("TE-L", today)] and services.kept_ids(pm) == set()
    set_test_equipment(pm, [], by=users.manager)
    u = item("TE-U")
    certificate(u, today - 200 * DAY)
    done = pm_with(pump, u, completed_on=today - 30 * DAY)
    found = certificate(u, today - 10 * DAY, as_found=AsFound.OUT_OF_TOLERANCE, adjusted=True)
    reopen(done, today)
    complete_work_order(done, resolution="Done again", pm_result=PmResult.PASS, by=users.manager, today=today)
    assert services.kept_ids(done) == {u.pk} and [w.used_on for w in services.review_summary(found).work] == [today - 30 * DAY]
    # U2
    free = in_date("TE-F")
    x = checked_vendor_repair(pump, free, today - 30 * DAY)
    reopen(x, today)
    assert refused(complete_work_order, x, today=today, test_equipment=[str(free.pk)], by=users.manager, **VENDOR_DONE) == {
        "test_equipment": services.VENDOR_CHECKED}
    # U3
    bad = in_date("TE-BAD")
    certificate(bad, today - 20 * DAY, result=Result.FAILED, as_found=AsFound.OUT_OF_TOLERANCE, by=users.manager)
    bad.refresh_from_db()
    assert (bad.status_since, bad.held_since) == (today, today - 20 * DAY)
    certificate(bad, today - 10 * DAY, by=users.manager)
    bad.refresh_from_db()
    assert bad.status == Status.IN_USE
    # U5
    reopen(done, today)
    change_status(done, WoStatus.OPEN, by=users.manager)
    change_status(done, WoStatus.CANCELLED, by=users.manager)
    assert services.delete_refusal(WorkOrder.objects.get(pk=done.pk)).startswith(f"{done.number} was cancelled;")
    assert refused(set_test_equipment, done, [], by=users.manager) == {"test_equipment": services.CANCELLED.format(number=done.number)}


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_u4_retiring_a_device_while_change_names_its_items_never_deadlocks(tenant, monkeypatch):
    """The verifier's problem 5: a device with two PMs reopened and back at open, the first (by number) holding TE-hi and the second
    TE-lo (by pk), is retired: its open PMs are cancelled in number order, each cancel taking off its uses. Between the two cancels,
    Change on another device's work order names both items (it locks them in pk order: lo, then hi). The cancel takes no item's row
    (U4), so both finish and nothing deadlocks; before U4 the first cancel held hi, Change held lo and waited for hi, and the second
    cancel waited for lo."""
    from apps.equipment import services as equipment_services
    from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel

    today = timezone.localdate()
    with tenant_context(tenant):
        dept = Department.objects.create(name="ICU")
        model = DeviceModel.objects.create(manufacturer="BD", model="Alaris", description="Pump", category="Infusion pumps")
        retiring = Asset.objects.create(tag="CE-1", device_model=model, department=dept)
        other = Asset.objects.create(tag="CE-2", device_model=model, department=dept)
        lo, hi = sorted([in_date("TE-1"), in_date("TE-2")], key=lambda te: te.pk)
        first = pm_with(retiring, hi, completed_on=today - 30 * DAY)
        second = pm_with(retiring, lo, completed_on=today - 20 * DAY)
        assert first.number < second.number
        for wo in (first, second):
            reopen(wo, today)
            change_status(wo, WoStatus.OPEN)
        target = pm_with(other, completed_on=today - 10 * DAY)
    between = threading.Event()
    real = services.clear_for_cancel
    calls = []

    def slow(wo, *, by):
        n = real(wo, by=by)
        calls.append(wo.number)
        if threading.current_thread().name == "retire" and len(calls) == 1:
            between.set()
            time.sleep(1.5)  # longer than PostgreSQL's deadlock_timeout (1s): a cycle would be found meanwhile
        return n

    monkeypatch.setattr(services, "clear_for_cancel", slow)
    errors, results = {}, {}

    def retire():
        with tenant_context(tenant):
            return equipment_services.set_status(Asset.objects.get(pk=retiring.pk), AssetStatus.RETIRED, by=None, today=today).status

    def change():
        with tenant_context(tenant):
            return set_test_equipment(WorkOrder.objects.get(pk=target.pk), [lo.pk, hi.pk], by=None).number

    first_thread, second_thread = _thread(retire, errors, results, "retire"), _thread(change, errors, results, "change")
    first_thread.start()
    assert between.wait(20)
    second_thread.start()
    for t in (first_thread, second_thread):
        t.join(30)
    assert not errors and results == {"retire": AssetStatus.RETIRED, "change": target.number}, (results, errors)
    assert calls == [first.number, second.number]
    with tenant_context(tenant):
        assert [WorkOrder.objects.get(pk=w.pk).status for w in (first, second)] == [WoStatus.CANCELLED, WoStatus.CANCELLED]
        assert tags_on(first) == [] and tags_on(second) == [] and tags_on(target) == ["TE-1", "TE-2"]
        gone = TestEquipmentUse.history.filter(work_order_id__in=[first.pk, second.pk], history_type="-")
        assert sorted((str(g.work_order_id), g.history_change_reason) for g in gone) == sorted(
            [(str(first.pk), "Test equipment cleared: cancelled"), (str(second.pk), "Test equipment cleared: cancelled")])


@needs_postgres
def test_round_5_under_the_policies(today, pump, users):
    """Round 5 as the runtime role: V1's held_since carried from the history by Reinstate, retired_throughout refusing Change on a day the
    item was retired, in_use_today keeping an item in use this morning after a back-dated failed certificate is typed, and V3's words."""
    morning = written_on(in_date("TE-M"), today - 100 * DAY)  # its history moved as the owner: the test's setup, not the runtime's
    as_app_role()
    # V1: problem 1 (c), then problem 2
    te, held = _retired_story(today, users, "c")
    assert held == today - 120 * DAY
    pm = pm_with(pump, completed_on=today - 50 * DAY, by=users.manager)
    assert refused(set_test_equipment, pm, [te.pk], by=users.manager) == {
        "test_equipment": services.RETIRED_BEFORE.format(tag=te.tag, day=fmt(today - 100 * DAY))}
    certificate(morning, today - 10 * DAY, result=Result.FAILED, as_found=AsFound.OUT_OF_TOLERANCE, by=users.manager)
    morning.refresh_from_db()
    assert (morning.status_since, morning.held_since) == (today, today - 10 * DAY)
    assert rules.in_use_today([morning], today) == {morning.pk}
    wo = open_pm(pump)
    complete_work_order(wo, resolution="Done", pm_result=PmResult.PASS, test_equipment=[str(morning.pk)], by=users.manager, today=today)
    assert tags_on(wo) == ["TE-M"]
    # V3
    kept, new = item("TE-K"), in_date("TE-N")
    certificate(kept, today - 200 * DAY)
    x = checked_vendor_repair(pump, kept, today - 30 * DAY)
    certificate(kept, today - 20 * DAY)
    reopen(x, today)
    assert refused(complete_work_order, x, today=today, test_equipment=[str(kept.pk), str(new.pk)], by=users.manager, **VENDOR_DONE) == {
        "test_equipment": services.VENDOR_CHECKED_KEPT.format(number=x.number, tags="TE-K", left_out=services.LEFT_OUT)}
    complete_work_order(x, today=today, test_equipment=[str(kept.pk)], by=users.manager, **VENDOR_DONE)
    assert tags_on(x) == ["TE-K"]
