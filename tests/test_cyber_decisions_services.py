"""
Slice 39 wave 1, the decisions' writers (apps.cybersecurity.decisions) and the register refusals they bring (services.removal_refusal,
services.update_release), on SQLite and PostgreSQL:

- record_decision for each scope (number, governed ids, history, the review's default), every refusal in words keyed by field, and
  supersede in one transaction (the old one ended first; a failure after it leaves both as they were).
- end_decision and mark_entered_in_error (immutable otherwise: an ended decision stays as recorded).
- Levels: Device security Approve and Equipment View; Edit, Approve without Equipment View, and scoped users refused.
- The lock spy: the register, the decision superseded or ended, the release, the number, the device; a lock conflict or a constraint
  answered in words. Equipment's writers (_retire through set_status and return_to_owner, update_asset, set_status) and
  cybersecurity.services.model_changed never take the register lock.
- The release refusals: Entered in error while a live decision is for it, a version change while any decision names it.
- A decision's history in words (its controls and reasons as the register words them).
"""
import re
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from cyber_fixtures import facts, release
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, OperationalError

from apps.accounts.models import Level, Module, Role, User
from apps.core.history import record_history
from apps.cybersecurity import decisions, rules, services
from apps.cybersecurity.decisions import end_decision, mark_entered_in_error, record_decision
from apps.cybersecurity.models import (
    AcceptedBy,
    Connects,
    DecisionEnd,
    DecisionPlan,
    DecisionResponse,
    DeviceSecurityDecision,
    Network,
    OsFamily,
    Segmented,
)
from apps.equipment import services as eq
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel, Ownership, ReturnCleaning, ReturnData
from apps.tenants.context import tenant_context

TODAY = date(2026, 10, 21)
DAY = timedelta(days=1)
KEEP_WITH, KEEP_WITHOUT, OUT_OF_USE = (DecisionResponse.KEEP_WITH_CONTROLS, DecisionResponse.KEEP_WITHOUT_ADDED_CONTROLS,
                                       DecisionResponse.TAKE_OUT_OF_USE)
BASE = {"reasons": ["security_updates"], "response": KEEP_WITHOUT, "accepted_by": AcceptedBy.CE_LEADER, "by": None, "today": TODAY}


def never(text: str) -> list:
    return [w for w in rules.DECISION_NEVER_WORDS if re.search(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", text, re.IGNORECASE)]


def refused(fn=None, *args, **kwargs) -> dict:
    """The refusal's words by field: `fn(*args, **kwargs)`, or with no `fn` record() (BASE's fields under `kwargs`)."""
    with pytest.raises(ValidationError) as e:
        record(**kwargs) if fn is None else fn(*args, **kwargs)
    out = {k: v[0] if len(v) == 1 else v for k, v in e.value.message_dict.items()}
    assert not [t for t in out.values() if isinstance(t, str) and never(t)], out
    return out


def record(**fields):
    return record_decision(**{**BASE, **fields})


def reasons_of(obj) -> list:
    return list(type(obj).history.filter(id=obj.pk).order_by("history_date", "history_id").values_list("history_change_reason", flat=True))


def custom_user(tenant, slug, levels, **fields):
    role = Role.objects.create(name=slug.title(), slug=slug)
    role.set_levels(levels)
    return User.objects.create_user(username=f"{slug}@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role,
                                    first_name=slug.title(), last_name="User", **fields)


@pytest.fixture
def floor(ctx, dept, pump_model):
    """As the rules' tests: the pump model can connect (wireless), all on a segment; 5.2 (demo) past its security updates, 5.6 (demo)
    supported; P-1, P-2, P-3 of ours on 5.2, P-4 on 5.6, the rental T-1 on 5.2 (due back Nov 10, 2026)."""
    profile = release(pump_model, "", connects=Connects.WIRELESS, segmented=Segmented.ALL)
    r52 = release(pump_model, "5.2 (demo)", security_updates_end_on=TODAY - 30 * DAY)
    r56 = release(pump_model, "5.6 (demo)", security_updates_end_on=date(2030, 3, 31))
    devices = {tag: Asset.objects.create(tag=tag, device_model=pump_model, department=dept) for tag in ("P-1", "P-2", "P-3", "P-4")}
    for tag in ("P-1", "P-2", "P-3"):
        facts(devices[tag], r52)
    facts(devices["P-4"], r56)
    devices["T-1"] = Asset.objects.create(tag="T-1", device_model=pump_model, department=dept, ownership=Ownership.RENTAL,
                                          owner="Summit Medical Rentals", due_back_on=date(2026, 11, 10))
    facts(devices["T-1"], r52)
    return SimpleNamespace(profile=profile, r52=r52, r56=r56, model=pump_model, d=devices, dept=dept)


# --- recording ------------------------------------------------------------------------------------------------------------------------

def test_each_scope_is_recorded_with_its_number_devices_and_history(floor, make_user):
    kim = make_user("director")
    on52 = record(release=floor.r52, response=KEEP_WITH, controls=["traffic_restricted", "segmented"], by=kim,
                  accepted_by=AcceptedBy.IT_SECURITY, exception_reference="DEMO-EX-1037", plan=DecisionPlan.UPDATE_PLANNED,
                  plan_by=date(2027, 3, 1), plan_owner=kim)
    assert (on52.number, on52.device_model_id, on52.release_id, on52.asset_id) == ("SD-26-0001", floor.model.pk, floor.r52.pk, None)
    assert on52.controls == ["segmented", "traffic_restricted"]  # the form's order
    assert on52.governed_ids == [str(floor.d[t].pk) for t in ("P-1", "P-2", "P-3")]  # devices of ours only, tag order
    assert (on52.recorded_by, on52.decided_on, on52.review_by) == (kim, TODAY, date(2027, 10, 21))
    assert reasons_of(on52) == ["Recorded"]
    # P-4 on a release past its security updates with no decision of its own: a model's decision governing only devices that need none
    # is refused (skeptics T0)
    facts(floor.d["P-4"], release(floor.model, "4.0 (demo)", security_updates_end_on=TODAY - DAY))
    model_wide = record(device_model=floor.model)
    assert model_wide.number == "SD-26-0002" and model_wide.release_id is None
    assert model_wide.governed_ids == [str(floor.d["P-4"].pk)]  # P-1 to P-3 are governed by 5.2's decision (review 39 F1)
    own = record(asset=floor.d["T-1"], plan=DecisionPlan.RETURN_TO_OWNER)
    assert (own.asset_id, own.device_model_id, own.governed_ids) == (floor.d["T-1"].pk, None, [str(floor.d["T-1"].pk)])
    assert own.review_by == date(2026, 11, 10)  # a rental: its due back day
    out = record(asset=floor.d["P-1"], response=OUT_OF_USE, decided_on=TODAY - 10 * DAY)
    assert out.review_by == date(2027, 1, 11)  # take out of use: 3 months
    # a decision recorded in another year counts on its own
    assert record(asset=floor.d["P-2"], decided_on=date(2025, 12, 30)).number == "SD-25-0001"


def test_an_end_announced_within_12_months_may_be_decided_ahead(floor):
    soon = release(floor.model, "5.4 (demo)", security_updates_end_on=date(2027, 6, 30))
    facts(floor.d["P-4"], soon)
    d = record(release=soon)
    assert d.reasons == ["security_updates"]
    later = release(floor.model, "5.5 (demo)", security_updates_end_on=date(2028, 6, 30))
    facts(floor.d["P-4"], later)
    assert refused(release=later) == {"release": decisions.NONE_NEEDED}


def test_the_scope_is_one_shape_of_this_facility(floor, other_tenant, vent_model):
    assert refused(asset=floor.d["P-1"], device_model=floor.model) == {"scope": decisions.SCOPE_WORDS}
    assert refused() == {"scope": decisions.SCOPE_WORDS}
    assert refused(asset=floor.d["P-1"], release=floor.r52) == {"scope": decisions.SCOPE_WORDS}
    with tenant_context(other_tenant):
        dept = Department.objects.create(name="Their ICU")
        model = DeviceModel.objects.create(manufacturer="BD", model="Alaris 8015 PCU", description="Pump", category="Infusion pumps")
        theirs = Asset.objects.create(tag="THEIRS-1", device_model=model, department=dept)
        their_release = release(model, "5.2 (demo)")
    assert refused(asset=theirs) == {"asset": services.CHOOSE_DEVICE}
    assert refused(asset=str(theirs.pk)) == {"asset": services.CHOOSE_DEVICE}
    assert refused(device_model=model) == {"device_model": services.CHOOSE_MODEL}
    assert refused(release=their_release) == {"release": decisions.CHOOSE_RELEASE}
    assert refused(release=floor.profile) == {"release": decisions.CHOOSE_RELEASE}  # the profile is versionless
    assert refused(release=floor.r52, device_model=vent_model) == {"release": "Choose a version of this model: 5.2 (demo) is another model's."}
    gone = release(floor.model, "4.0 (demo)")
    gone.removed_on = TODAY
    gone.save()
    assert refused(release=gone) == {"release": decisions.CHOOSE_RELEASE}


def test_every_field_refusal_at_once(floor, make_user, tenant):
    out = refused(asset=floor.d["P-1"], reasons=["security_updates", "firmware"], response="accept", controls=["segmented"],
                  controls_reference="CHG-1", plan="later", accepted_by="nobody", exception_reference="10.0.0.0/24",
                  decided_on=TODAY + DAY, review_by="not a day")
    assert out == {"reasons": decisions.REASONS_WORDS, "response": decisions.RESPONSE_WORDS, "controls_reference": decisions.REFERENCE_ONLY_WITH,
                   "plan": decisions.PLAN_WORDS, "accepted_by": decisions.ACCEPTED_WORDS, "exception_reference": decisions.NOT_AN_ADDRESS,
                   "decided_on": "The day decided cannot be after today.", "review_by": "The review day: enter a day."}
    assert refused(asset=floor.d["P-1"], reasons=[]) == {"reasons": decisions.REASONS_NONE}
    assert refused(asset=floor.d["P-1"], reasons="security_updates") == {"reasons": decisions.REASONS_WORDS}


def test_the_controls_rules(floor):
    p1 = {"asset": floor.d["P-1"]}
    assert refused(**p1, response=KEEP_WITH) == {"controls": decisions.CONTROLS_NEEDED}
    assert refused(**p1, response=KEEP_WITH, controls=["network_removed"]) == {"controls": rules.NETWORK_REMOVED_HINT}
    assert rules.NETWORK_REMOVED_HINT == "Taken off the network? Record it on the device (Record facts): it then needs no decision."
    assert refused(**p1, response=KEEP_WITH, controls=["hardening"]) == {"controls": decisions.CONTROLS_WORDS}
    assert refused(**p1, response=KEEP_WITHOUT, controls=["segmented"]) == {"controls": decisions.CONTROLS_ONLY_WITH}
    assert refused(**p1, response=OUT_OF_USE, controls=["segmented"]) == {"controls": decisions.CONTROLS_ONLY_WITH}
    assert refused(**p1, response=KEEP_WITH, controls=["recorded_elsewhere"]) == {"controls_reference": decisions.REFERENCE_NEEDED}
    assert refused(**p1, controls_reference="CHG0012345") == {"controls_reference": decisions.REFERENCE_ONLY_WITH}
    for address in ("10.20.30.40", "10.20.0.0/16", "10.20.30.400", "00-1A-2B-3C-4D-5E", "001A.2B3C.4D5E", "001a2b3c4d5e", "VLAN-12",
                    "vlan12", "port-443", "tcp-443", "443/udp"):
        assert refused(**p1, response=KEEP_WITH, controls=["recorded_elsewhere"], controls_reference=address) == {
            "controls_reference": decisions.NOT_AN_ADDRESS}, address
        assert refused(**p1, exception_reference=address) == {"exception_reference": decisions.NOT_AN_ADDRESS}, address
    assert refused(**p1, exception_reference="EX 1042") == {"exception_reference": decisions.REFERENCE_WORDS}
    assert refused(**p1, exception_reference="E" * 41) == {"exception_reference": decisions.REFERENCE_LENGTH}
    for token in ("CHG0012345", "DEMO-EX-1042", "RITM0042", "2026-1021", "PORTAL-7", "123456789012"):
        assert not decisions.network_identifier(token), token
    d = record(**p1, response=KEEP_WITH, controls=["recorded_elsewhere", "segmented"], controls_reference="CHG0012345")
    assert (d.controls, d.controls_reference) == (["segmented", "recorded_elsewhere"], "CHG0012345")


def test_segmented_is_refused_while_the_models_record_says_not_on_a_separate_segment(floor):
    floor.profile.segmented = Segmented.NONE
    floor.profile.save()
    assert refused(release=floor.r52, response=KEEP_WITH, controls=["segmented", "no_internet"]) == {"controls": decisions.SEGMENT_REFUSAL}
    assert record(release=floor.r52, response=KEEP_WITH, controls=["no_internet"]).controls == ["no_internet"]
    floor.profile.segmented = Segmented.SOME
    floor.profile.save()
    record(asset=floor.d["P-1"], response=KEEP_WITH, controls=["segmented"])  # some: allowed (an information line later)


def test_the_plan_rules(floor, tenant, make_user, other_tenant):
    p1 = {"asset": floor.d["P-1"]}
    assert refused(**p1, plan=DecisionPlan.UPDATE_PLANNED) == {"plan_by": decisions.PLAN_DAY[DecisionPlan.UPDATE_PLANNED]}
    assert refused(**p1, plan=DecisionPlan.REPLACE_PLANNED) == {"plan_by": "Enter the day the replacement is planned by."}
    assert refused(**p1, plan=DecisionPlan.WAIT_FOR_MANUFACTURER, plan_by=TODAY + DAY) == {"plan_by": decisions.PLAN_DAY_ONLY}
    assert refused(**p1, plan=DecisionPlan.UPDATE_PLANNED, plan_by=TODAY - DAY) == {"plan_by": decisions.PLAN_BEFORE}
    assert refused(**p1, plan=DecisionPlan.RETURN_TO_OWNER) == {"plan": decisions.RETURN_ONLY_TEMPORARY}
    assert refused(release=floor.r52, plan=DecisionPlan.RETURN_TO_OWNER) == {"plan": decisions.RETURN_ONLY_TEMPORARY}
    manager, tech = make_user("manager"), make_user("technician")
    assert refused(**p1, plan_owner=manager) == {"plan_owner": decisions.OWNER_ONLY_WITH}
    User.objects.filter(pk=tech.pk).update(is_active=False)
    assert refused(**p1, plan=DecisionPlan.WAIT_FOR_MANUFACTURER, plan_owner=tech) == {"plan_owner": decisions.CHOOSE_OWNER}
    no_cyber = custom_user(tenant, "biomed", {Module.EQUIPMENT: Level.EDIT})
    assert refused(**p1, plan=DecisionPlan.WAIT_FOR_MANUFACTURER, plan_owner=no_cyber) == {"plan_owner": decisions.CHOOSE_OWNER}
    vendor = make_user("vendor")
    Role.objects.filter(pk=vendor.role_id).first().set_levels({Module.CYBERSECURITY: Level.FULL})
    assert refused(**p1, plan=DecisionPlan.WAIT_FOR_MANUFACTURER, plan_owner=vendor) == {"plan_owner": decisions.CHOOSE_OWNER}
    theirs = make_user("director", tenant_=_with_roles(other_tenant), username="kim@other.example")
    assert refused(**p1, plan=DecisionPlan.WAIT_FOR_MANUFACTURER, plan_owner=theirs.pk) == {"plan_owner": decisions.CHOOSE_OWNER}
    assert refused(**p1, plan=DecisionPlan.WAIT_FOR_MANUFACTURER, plan_owner="kim") == {"plan_owner": decisions.CHOOSE_OWNER}
    d = record(**p1, plan=DecisionPlan.UPDATE_PLANNED, plan_by=TODAY, plan_owner=str(manager.pk))
    assert (d.plan_owner, d.plan_by) == (manager, TODAY)


def _with_roles(tenant):
    from apps.accounts.models import create_default_roles

    create_default_roles(tenant)
    return tenant


def test_the_review_rules(floor):
    p1 = {"asset": floor.d["P-1"]}
    assert refused(**p1, review_by=TODAY) == {"review_by": decisions.REVIEW_AFTER}
    assert refused(**p1, review_by=date(2029, 10, 22)) == {
        "review_by": "Choose a review day by Oct 21, 2029: Cadence keeps a decision's review within 36 months of the day decided."}
    assert refused(**p1, response=OUT_OF_USE, review_by=date(2027, 10, 22)) == {
        "review_by": "Choose a review day by Oct 21, 2027: Cadence keeps a decision's review within 12 months of the day decided."}
    assert record(**p1, review_by=date(2029, 10, 21)).review_by == date(2029, 10, 21)
    # an exception that already lapsed: decided long ago, its review past
    lapsed = record(asset=floor.d["P-2"], decided_on=date(2025, 1, 15), review_by=date(2026, 1, 15))
    assert rules.decision_state(lapsed, TODAY) == rules.REVIEW_DUE
    assert decisions.REVIEW_PASSED.format(day="Jan 15, 2026") == "This review date has passed: the decision will read Review due since Jan 15, 2026."


def test_the_scope_needs_a_device_past_its_support(floor, dept, vent_model):
    assert refused(asset=floor.d["P-4"]) == {"asset": "P-4 is not past its support as recorded, and no end is announced within 12 months."}
    facts(floor.d["P-1"], floor.r52, network=Network.NOT_CONNECTED)
    assert refused(asset=floor.d["P-1"]) == {"asset": "P-1 is not connected here, as recorded or disclosed: it needs no decision."}
    floor.profile.connects = ""
    floor.profile.save()
    assert refused(asset=floor.d["P-2"]) == {"asset": "P-2's connection is not recorded: record its facts first (Record facts)."}
    floor.profile.connects = Connects.WIRELESS
    floor.profile.save()
    ext = release(floor.model, "6.0 (demo)", os_family=OsFamily.EMBEDDED, os_support_ends_on=TODAY - 100 * DAY,
                  security_updates_end_on=date(2028, 3, 31))
    facts(floor.d["P-3"], ext)
    assert refused(asset=floor.d["P-3"]) == {"asset": "P-3: the manufacturer's security updates continue (announced to end Mar 31, 2028): "
                                                      "a decision can be recorded once their end is within 12 months."}
    assert refused(release=ext) == {"release": "P-3: the manufacturer's security updates continue (announced to end Mar 31, 2028): "
                                               "a decision can be recorded once their end is within 12 months."}  # skeptics S7
    empty = release(floor.model, "4.0 (demo)", security_updates_end_on=TODAY - DAY)
    assert refused(release=empty) == {"release": "No device of ours on site runs 4.0 (demo)."}
    facts(floor.d["T-1"], empty)  # a rental on it: never in a release's scope
    assert refused(release=empty) == {"release": "No device of ours on site runs 4.0 (demo)."}
    assert refused(device_model=vent_model) == {"device_model": "No Hamilton-G5 of ours is on site."}
    Asset.objects.filter(pk=floor.d["P-4"].pk).update(status=AssetStatus.RETIRED)
    assert refused(asset=floor.d["P-4"]) == {"asset": "P-4 is retired: a decision is for a device on site."}
    Asset.objects.filter(pk=floor.d["T-1"].pk).update(status=AssetStatus.RETIRED)
    assert refused(asset=floor.d["T-1"]) == {"asset": "T-1 was returned to its owner: a decision is for a device on site."}


def test_the_reasons_are_those_that_hold_now(floor):
    words = "the manufacturer's security updates, support, or parts ended, or their end is announced"
    assert refused(asset=floor.d["P-1"], reasons=["os_support"]) == {"reasons": f"Choose the reasons that hold now: {words}."}
    assert refused(asset=floor.d["P-1"], reasons=["security_updates", "os_support"]) == {
        "reasons": f"Choose the reasons that hold now: {words}."}
    floor.r52.os_family, floor.r52.os_support_ends_on = OsFamily.LINUX, TODAY - DAY
    floor.r52.save()
    both = ("Choose the reasons that hold now: the manufacturer's security updates, support, or parts ended, or their end is announced; "
            "the OS vendor's published support ended, or its end is announced.")
    assert refused(asset=floor.d["P-1"], reasons=["security_updates"]) == {"reasons": both}  # every reason that ended is named
    floor.r52.os_support_ends_on = TODAY + 100 * DAY
    floor.r52.save()
    assert record(asset=floor.d["P-1"], reasons=["security_updates"]).reasons == ["security_updates"]  # an announced one may be left out
    assert record(asset=floor.d["P-2"], reasons=["os_support", "security_updates"]).reasons == ["security_updates", "os_support"]


def test_one_live_decision_per_scope_and_supersede_in_one_transaction(floor, make_user):
    kim = make_user("director")
    first = record(release=floor.r52, by=kim)
    assert refused(release=floor.r52) == {"release": "SD-26-0001 is in force for this scope: review and renew it instead."}
    other = record(asset=floor.d["P-1"])
    assert refused(release=floor.r52, supersedes=other) == {"supersedes": "SD-26-0002 is for another scope: renew a decision for this one."}
    renewed = record(release=floor.r52, supersedes=first, by=kim, response=KEEP_WITH, controls=["monitored"],
                     exception_reference="DEMO-EX-1037")
    first.refresh_from_db()
    assert (first.ended_on, first.ended_by, first.end_reason) == (TODAY, kim, DecisionEnd.SUPERSEDED)
    assert renewed.supersedes == first and first.superseded_by == renewed and renewed.number == "SD-26-0003"
    assert reasons_of(first) == ["Recorded", "Superseded by SD-26-0003"]
    assert reasons_of(renewed) == ["Recorded, superseding SD-26-0001"]
    assert refused(release=floor.r52, supersedes=first) == {"supersedes": "SD-26-0001 ended on Oct 21, 2026: it stays as recorded."}
    assert refused(release=floor.r52, supersedes=first.number.lower()) == {"supersedes": "SD-26-0001 ended on Oct 21, 2026: it stays as recorded."}


def test_a_failure_after_the_old_one_ended_leaves_both_as_they_were(floor, monkeypatch):
    first = record(asset=floor.d["P-1"])
    real = services._save

    def failing(obj, by, reason):
        if isinstance(obj, DeviceSecurityDecision) and obj._state.adding:
            raise IntegrityError("constraint")
        return real(obj, by, reason)

    monkeypatch.setattr(services, "_save", failing)
    assert refused(asset=floor.d["P-1"], supersedes=first) == {"asset": decisions.RACE}
    first.refresh_from_db()
    assert first.ended_on is None and DeviceSecurityDecision.objects.count() == 1


class _Deadlock(Exception):
    sqlstate = "40P01"


def test_a_lock_conflict_answers_race_in_words(floor, monkeypatch):
    def deadlocked(*args, **kwargs):
        raise OperationalError("deadlock detected") from _Deadlock()

    monkeypatch.setattr(decisions, "_number", deadlocked)
    assert refused(release=floor.r52) == {"release": decisions.RACE}
    first = DeviceSecurityDecision.objects.create(number="SD-26-0009", applies_to="support_ended", asset=floor.d["P-2"],
                                                  reasons=["security_updates"], response=KEEP_WITHOUT, accepted_by=AcceptedBy.CE_LEADER,
                                                  decided_on=TODAY, review_by=TODAY + 30 * DAY)
    monkeypatch.setattr(decisions, "_decision_row", deadlocked)
    assert refused(end_decision, first, by=None, today=TODAY) == {"decision": decisions.RACE}
    assert not DeviceSecurityDecision.objects.exclude(pk=first.pk).exists()


# --- end and entered in error ----------------------------------------------------------------------------------------------------------

def test_end_and_entered_in_error(floor, make_user, other_tenant):
    manager = make_user("manager")
    d = record(asset=floor.d["P-1"])
    assert refused(end_decision, d, reason=DecisionEnd.SUPERSEDED, by=manager) == {"end_reason": decisions.END_REASON_WORDS}
    assert refused(end_decision, d, reason=DecisionEnd.ENTERED_IN_ERROR, by=manager) == {"end_reason": decisions.END_REASON_WORDS}
    ended = end_decision(d, by=manager, today=TODAY + DAY)
    assert (ended.ended_on, ended.ended_by, ended.end_reason) == (TODAY + DAY, manager, DecisionEnd.NO_LONGER_NEEDED)
    assert d.ended_on == TODAY + DAY  # the caller's copy follows
    assert reasons_of(d) == ["Recorded", "Ended: no longer needed"]
    assert refused(end_decision, d, by=manager) == {"decision": "SD-26-0001 ended on Oct 22, 2026: it stays as recorded."}
    assert refused(mark_entered_in_error, d, by=manager) == {"decision": "SD-26-0001 ended on Oct 22, 2026: it stays as recorded."}
    again = record(asset=floor.d["P-1"])  # the scope is free again
    marked = mark_entered_in_error(again.number.lower(), by=manager, today=TODAY)
    assert (marked.end_reason, marked.ended_by) == (DecisionEnd.ENTERED_IN_ERROR, manager)
    assert reasons_of(again) == ["Recorded", "Entered in error"]
    assert DeviceSecurityDecision.objects.count() == 2  # never deleted
    with tenant_context(other_tenant):
        dept = Department.objects.create(name="Their ICU")
        model = DeviceModel.objects.create(manufacturer="BD", model="Alaris 8015 PCU", description="Pump", category="Infusion pumps")
        theirs = Asset.objects.create(tag="THEIRS-1", device_model=model, department=dept)
        their = DeviceSecurityDecision.objects.create(number="SD-26-0001", applies_to="support_ended", asset=theirs,
                                                      reasons=["security_updates"], response=KEEP_WITHOUT,
                                                      accepted_by=AcceptedBy.CE_LEADER, decided_on=TODAY, review_by=TODAY + 30 * DAY)
    assert refused(end_decision, their, by=manager) == {"decision": decisions.CHOOSE_DECISION}
    assert refused(mark_entered_in_error, str(their.pk), by=manager) == {"decision": decisions.CHOOSE_DECISION}
    assert refused(end_decision, "SD-99-0001", by=manager) == {"decision": decisions.CHOOSE_DECISION}


# --- levels ----------------------------------------------------------------------------------------------------------------------------

def test_deciding_needs_approve_and_equipment_view(tenant, floor, make_user):
    manager, director, tech = make_user("manager"), make_user("director"), make_user("technician")
    d = record(asset=floor.d["P-1"], by=manager)
    assert d.recorded_by == manager
    approve_only = custom_user(tenant, "it-no-equipment", {Module.CYBERSECURITY: Level.APPROVE})
    it_security = custom_user(tenant, "it-security", {Module.CYBERSECURITY: Level.APPROVE, Module.EQUIPMENT: Level.VIEW})
    for user in (tech, approve_only):
        with pytest.raises(PermissionDenied, match=re.escape(decisions.DECIDE_PERMISSION)):
            record(asset=floor.d["P-2"], by=user)
        with pytest.raises(PermissionDenied, match=re.escape(decisions.END_PERMISSION)):
            end_decision(d, by=user)
        with pytest.raises(PermissionDenied, match=re.escape(decisions.END_PERMISSION)):
            mark_entered_in_error(d, by=user)
    for slug in ("vendor", "requester"):
        scoped = make_user(slug)
        Role.objects.get(slug=slug).set_levels({Module.CYBERSECURITY: Level.FULL, Module.EQUIPMENT: Level.FULL})
        with pytest.raises(PermissionDenied):
            record(asset=floor.d["P-2"], by=User.objects.get(pk=scoped.pk))
        with pytest.raises(PermissionDenied):
            end_decision(d, by=User.objects.get(pk=scoped.pk))
    record(asset=floor.d["P-2"], by=it_security)
    record(asset=floor.d["P-3"], by=director)
    end_decision(d, by=it_security)


def test_a_view_only_reader_reads_but_never_writes(tenant, floor):
    viewer = custom_user(tenant, "viewer", {Module.CYBERSECURITY: Level.VIEW, Module.EQUIPMENT: Level.VIEW})
    with pytest.raises(PermissionDenied):
        record(asset=floor.d["P-1"], by=viewer)
    assert rules.needs_attention(TODAY, user=viewer).headings
    assert all(row.record is None and row.end is None for h in rules.needs_attention(TODAY, user=viewer).headings for row in h.rows)


# --- the lock spy -----------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def spy(monkeypatch):
    calls: list = []

    def wrap(module, name, label):
        real = getattr(module, name)

        def spied(*args, **kwargs):
            calls.append(label)
            return real(*args, **kwargs)

        monkeypatch.setattr(module, name, spied)

    wrap(services, "lock_register", "register")
    wrap(decisions, "_decision_row", "decision")
    wrap(services, "_locked_release", "release")
    wrap(decisions, "_number", "number")
    wrap(services, "_locked_devices", "devices")
    return SimpleNamespace(calls=calls, reset=calls.clear)


def test_every_writer_takes_its_locks_in_the_contracts_order(floor, spy):
    on52 = record(release=floor.r52)
    assert spy.calls == ["register", "release", "number"]
    spy.reset()
    record(release=floor.r52, supersedes=on52)
    assert spy.calls == ["register", "decision", "release", "number"]
    spy.reset()
    own = record(asset=floor.d["P-1"])
    assert spy.calls == ["register", "number", "devices"]
    spy.reset()
    record(asset=floor.d["P-1"], supersedes=own)
    assert spy.calls == ["register", "decision", "number", "devices"]
    spy.reset()
    facts(floor.d["P-4"], release(floor.model, "4.0 (demo)", security_updates_end_on=TODAY - DAY))  # a device the model's decision governs
    record(device_model=floor.model)
    assert spy.calls == ["register", "number"]
    spy.reset()
    end_decision(DeviceSecurityDecision.objects.get(number="SD-26-0005"), by=None)
    assert spy.calls == ["register", "decision"]
    spy.reset()
    mark_entered_in_error(DeviceSecurityDecision.objects.get(number="SD-26-0004"), by=None)
    assert spy.calls == ["register", "decision"]


def test_equipments_writers_never_take_the_register_lock(floor, monkeypatch, vent_model):
    calls: list = []
    real = services.lock_register
    monkeypatch.setattr(services, "lock_register", lambda: calls.append("register") or real())
    eq.update_asset(floor.d["P-2"], room="ICU 4")
    eq.update_asset(floor.d["P-3"], device_model=vent_model)  # model_changed clears its release
    services.model_changed(Asset.objects.get(pk=floor.d["P-1"].pk))
    eq.set_status(floor.d["P-2"], AssetStatus.OUT_OF_SERVICE)
    eq.set_status(floor.d["P-2"], AssetStatus.RETIRED)  # _retire
    eq.return_to_owner(floor.d["T-1"], cleaning=ReturnCleaning.DECONTAMINATED, data=ReturnData.NONE_STORED)  # _retire
    assert calls == []
    assert Asset.objects.get(pk=floor.d["T-1"].pk).status == AssetStatus.RETIRED


# --- the release's refusals --------------------------------------------------------------------------------------------------------------

def test_a_release_with_a_live_decision_is_not_entered_in_error(floor):
    lone = release(floor.model, "4.0 (demo)", security_updates_end_on=TODAY - DAY)
    facts(floor.d["P-4"], lone)
    d = record(release=lone)
    facts(floor.d["P-4"], floor.r56)  # no device on site runs it now
    assert services.removal_refusal(lone) == "SD-26-0001 is decided for this release: end it first."
    with pytest.raises(ValidationError) as e:
        services.remove_release(lone, by=None)
    assert e.value.message_dict == {"release": ["SD-26-0001 is decided for this release: end it first."]}
    end_decision(d, by=None)
    assert services.removal_refusal(lone) == ""  # an ended decision keeps naming it, as it was
    services.remove_release(lone, by=None)


def test_a_version_named_by_any_decision_never_changes(floor):
    lone = release(floor.model, "4.0 (demo)", security_updates_end_on=TODAY - DAY)
    facts(floor.d["P-4"], lone)
    d = record(release=lone)
    facts(floor.d["P-4"], floor.r56)
    with pytest.raises(ValidationError) as e:
        services.update_release(lone, by=None, version="4.1 (demo)")
    assert e.value.message_dict == {"version": ["SD-26-0001 names this version: add the new version instead."]}
    services.update_release(lone, by=None, version="V4.0 (demo)")  # the same version, spelled otherwise
    end_decision(d, by=None)
    with pytest.raises(ValidationError):
        services.update_release(lone, by=None, version="4.1 (demo)")  # ended: it still names it
    services.update_release(lone, by=None, os_family=OsFamily.LINUX)  # other fields: as before


# --- history in words --------------------------------------------------------------------------------------------------------------------

def test_a_decisions_history_reads_in_words(floor, make_user):
    kim = make_user("director")
    d = record(release=floor.r52, response=KEEP_WITH, controls=["traffic_restricted", "recorded_elsewhere"], controls_reference="CHG0012345",
               by=kim, accepted_by=AcceptedBy.IT_SECURITY, plan=DecisionPlan.UPDATE_PLANNED, plan_by=date(2027, 3, 1))
    end_decision(d, by=kim, today=TODAY)
    entries, _next = record_history(DeviceSecurityDecision.objects.get(pk=d.pk))
    added = next(e for e in entries if e.action == "added")
    changes = {c.field: c.after for c in added.changes}
    assert changes["Controls"] == ("Traffic limited to the ports and hosts it needs (firewall or access list); Controls recorded in IT's "
                                   "system (with its reference)")
    assert changes["Reasons"] == "The manufacturer's security updates, support, or parts ended, or their end is announced"
    assert changes["Decision"] == "Keep in use with controls" and changes["Plan"] == "Update planned, not opened yet"
    assert changes["Accepted by"] == "IT security or the CISO" and changes["IT's change or exception number for the controls"] == "CHG0012345"
    assert "Number" not in changes and "Governed ids" not in changes
    ended = next(e for e in entries if e.action == "changed")
    assert ended.reason == "Ended: no longer needed" and {c.field for c in ended.changes} == {"Ended", "Ended by", "Why it ended"}
    assert added.area == "device_security_decisions" and added.record == "SD-26-0001"
    for e in entries:
        assert not never(" ".join([e.reason, *(f"{c.field} {c.before} {c.after}" for c in e.changes)]))


def test_the_decisions_module_says_no_never_word():
    texts = [v for k, v in vars(decisions).items() if k.isupper() and isinstance(v, str)]
    texts += [w for k, v in vars(decisions).items() if k.isupper() and isinstance(v, dict) for w in v.values() if isinstance(w, str)]
    assert len(texts) > 40 and [t for t in texts if never(t)] == []
