"""
Slice 40 wave 1B, decisions for the devices a security advisory names (apps.cybersecurity.decisions; build40 section 5.3, critique40_scope
B3), on SQLite and PostgreSQL:

- record_decision with applies_to "advisory" for each scope: its number (the decisions' sequence, shared), the advisory and its change
  count stored, the devices it governs (the advisory's key only), no reasons, the review's default (not in use here as a keep decision),
  history; Review and renew within one advisory's scope.
- Every refusal in words keyed by field: the field shape before the locks (every problem at once), the advisory under its row lock (not
  applicable, entered in error, the day decided before it was received), each scope refusal with its kind (refusal_for gives the same
  words, without any lock), network_removed only on the device's own fact "Not connected here", return it to its owner only for a
  temporary device's own decision.
- Support decisions unchanged: not in use here, an advisory, or network_removed refused for them.
- Levels: Device security Approve and Equipment View; Edit, Approve without Equipment View, View, and scoped users refused.
- The lock spy: the register, the decision superseded, the advisory's row, the release, the number, the device; slice 39's pins for
  support unchanged.
"""
import re
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from cyber_fixtures import facts, make_advisory, model_row, release, update_wo
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models.query import QuerySet

from apps.accounts.models import Level, Module, Role, User
from apps.cybersecurity import decisions, rules, services
from apps.cybersecurity.decisions import record_decision, refusal_for
from apps.cybersecurity.models import (
    AcceptedBy,
    AdvisoryRemedy,
    AdvisoryStatus,
    AdvisoryVersions,
    AppliesTo,
    Connects,
    DecisionEnd,
    DecisionPlan,
    DecisionResponse,
    DeviceSecurityDecision,
    InstalledBy,
    Network,
    NotApplicableReason,
    SecurityAdvisory,
    Segmented,
    SoftwareUpdate,
    UpdateOutcome,
)
from apps.equipment.models import Asset, AssetStatus, DeviceModel, Ownership
from apps.tenants.context import tenant_context

TODAY = date(2026, 10, 21)
DAY = timedelta(days=1)
KEEP_WITH, KEEP_WITHOUT, OUT_OF_USE, NOT_IN_USE = (DecisionResponse.KEEP_WITH_CONTROLS, DecisionResponse.KEEP_WITHOUT_ADDED_CONTROLS,
                                                   DecisionResponse.TAKE_OUT_OF_USE, DecisionResponse.NOT_IN_USE_HERE)
SEEN: list = []  # every refusal's words these tests read, for the never-words scan


def never(text: str) -> list:
    return [w for w in rules.DECISION_NEVER_WORDS if re.search(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", text, re.IGNORECASE)]


def refused(fn, *args, **kwargs) -> dict:
    """The refusal's words by field (each never-word clean)."""
    with pytest.raises(ValidationError) as e:
        fn(*args, **kwargs)
    out = {k: v[0] if len(v) == 1 else v for k, v in e.value.message_dict.items()}
    SEEN.extend(t for t in out.values() if isinstance(t, str))
    assert not [t for t in out.values() if isinstance(t, str) and never(t)], out
    return out


def custom_user(tenant, slug, levels, **fields):
    role = Role.objects.create(name=slug.title(), slug=slug)
    role.set_levels(levels)
    return User.objects.create_user(username=f"{slug}@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role,
                                    first_name=slug.title(), last_name="User", **fields)


@pytest.fixture
def floor(ctx, dept, pump_model):
    """The pump model can connect (wireless, as disclosed), all on a segment; 5.2 (demo) past its security updates (a support need too),
    5.4 (demo), 5.6 (demo) supported. P-1, P-2, P-3 of ours on 5.2, P-4 on 5.6, P-5 on 5.4, P-6 with no release recorded, the rental T-1
    on 5.2 (due back Nov 10, 2026). AD-26-0001, received Oct 1, 2026 (two changes recorded), names the pump on 5.2, the fix 5.6."""
    profile = release(pump_model, "", connects=Connects.WIRELESS, segmented=Segmented.ALL)
    r52 = release(pump_model, "5.2 (demo)", security_updates_end_on=TODAY - 30 * DAY)
    r54 = release(pump_model, "5.4 (demo)", security_updates_end_on=date(2029, 3, 31))
    r56 = release(pump_model, "5.6 (demo)", security_updates_end_on=date(2030, 3, 31))
    devices = {tag: Asset.objects.create(tag=tag, device_model=pump_model, department=dept) for tag in ("P-1", "P-2", "P-3", "P-4", "P-5",
                                                                                                          "P-6")}
    for tag in ("P-1", "P-2", "P-3"):
        facts(devices[tag], r52)
    facts(devices["P-4"], r56)
    facts(devices["P-5"], r54)
    devices["T-1"] = Asset.objects.create(tag="T-1", device_model=pump_model, department=dept, ownership=Ownership.RENTAL,
                                          owner="Summit Medical Rentals", due_back_on=date(2026, 11, 10))
    facts(devices["T-1"], r52)
    adv = make_advisory(number="AD-26-0001", received_on=TODAY - 20 * DAY)
    SecurityAdvisory.objects.filter(pk=adv.pk).update(changes=2, changed_on=TODAY - 5 * DAY)
    adv.refresh_from_db()
    row = model_row(adv, pump_model, releases=[r52], fix=r56)
    return SimpleNamespace(profile=profile, r52=r52, r54=r54, r56=r56, model=pump_model, d=devices, dept=dept, adv=adv, row=row)


BASE = {"response": KEEP_WITHOUT, "accepted_by": AcceptedBy.CE_LEADER, "by": None, "today": TODAY}


def record(adv, **fields):
    return record_decision(**{**BASE, "applies_to": AppliesTo.ADVISORY, "advisory": adv, **fields})


def support(**fields):
    return record_decision(**{**BASE, "reasons": ["security_updates"], **fields})


def reasons_of(obj) -> list:
    return list(type(obj).history.filter(id=obj.pk).order_by("history_date", "history_id").values_list("history_change_reason", flat=True))


def tags(ids) -> list:
    return sorted(Asset.objects.filter(pk__in=ids).values_list("tag", flat=True))


# --- recording ------------------------------------------------------------------------------------------------------------------------

def test_each_scope_is_recorded_for_the_advisory_with_its_count_and_devices(floor, make_user):
    kim = make_user("director")
    on52 = record(floor.adv, release=floor.r52, by=kim)
    assert (on52.number, on52.applies_to, on52.advisory, on52.advisory_changes) == ("SD-26-0001", AppliesTo.ADVISORY, floor.adv, 2)
    assert (on52.reasons, on52.release_id, on52.device_model_id, on52.asset_id) == ([], floor.r52.pk, floor.model.pk, None)
    assert tags(on52.governed_ids) == ["P-1", "P-2", "P-3"]  # devices of ours on 5.2 (never the rental)
    assert (on52.recorded_by, on52.decided_on, on52.review_by) == (kim, TODAY, date(2027, 10, 21)) and reasons_of(on52) == ["Recorded"]
    # every device of ours of the model but those under 5.2's decision: P-6 (no release recorded) needs a response; P-4 and P-5 need none
    model_wide = record(floor.adv, device_model=floor.model, response=NOT_IN_USE)
    assert model_wide.number == "SD-26-0002" and tags(model_wide.governed_ids) == ["P-4", "P-5", "P-6"]
    assert (model_wide.response, model_wide.plan, model_wide.review_by) == (NOT_IN_USE, DecisionPlan.NONE, date(2027, 10, 21))
    own = record(floor.adv, asset=floor.d["T-1"], plan=DecisionPlan.RETURN_TO_OWNER)
    assert (own.asset_id, own.governed_ids, own.review_by) == (floor.d["T-1"].pk, [str(floor.d["T-1"].pk)], date(2026, 11, 10))
    # the decisions' sequence is shared with support decisions
    assert support(release=floor.r52).number == "SD-26-0004"
    assert rules.who_verb(model_wide) == "Decided by" and rules.who_verb(on52) == "Accepted by"


def test_an_advisory_by_its_number_in_any_letter_case(floor):
    d = record("ad-26-0001", asset=floor.d["P-1"])
    assert d.advisory == floor.adv
    d2 = record(str(floor.adv.pk), asset=floor.d["P-2"])
    assert d2.advisory == floor.adv


def test_a_support_decision_and_an_advisory_decision_of_one_scope_are_two_needs(floor):
    """A support decision never stands in for an advisory's need, nor an advisory's for support (rules.key_of)."""
    s = support(release=floor.r52)
    a = record(floor.adv, release=floor.r52)  # never IN_FORCE: another need key
    assert rules.governing_decisions([floor.d["P-1"]], TODAY) == {floor.d["P-1"].pk: s}
    assert rules.governing_decisions([floor.d["P-1"]], TODAY, key=rules.advisory_key(floor.adv)) == {floor.d["P-1"].pk: a}
    other = make_advisory(number="AD-26-0002", reference="DEMO-ADV-0002", received_on=TODAY - 3 * DAY)
    model_row(other, floor.model, versions=AdvisoryVersions.ALL, remedy=AdvisoryRemedy.NO_FIX_YET)
    b = record(other, release=floor.r52)  # another advisory: its own need
    assert b.governed_ids == a.governed_ids == s.governed_ids
    assert refused(record, floor.adv, release=floor.r52) == {"release": decisions.IN_FORCE.format(number=a.number)}
    assert refused(support, release=floor.r52) == {"release": decisions.IN_FORCE.format(number=s.number)}


def test_review_and_renew_within_one_advisorys_scope(floor):
    old = record(floor.adv, release=floor.r52, decided_on=TODAY - 20 * DAY, review_by=TODAY + DAY)
    new = record(floor.adv, release=floor.r52, supersedes=old, response=KEEP_WITH, controls=["traffic_restricted"])
    old.refresh_from_db()
    assert (old.ended_on, old.end_reason, new.supersedes) == (TODAY, DecisionEnd.SUPERSEDED, old)
    assert reasons_of(old)[-1] == f"Superseded by {new.number}" and reasons_of(new) == [f"Recorded, superseding {old.number}"]
    # a support decision of the same scope, or another advisory's, is another scope's
    s = support(release=floor.r52)
    assert refused(record, floor.adv, release=floor.r52, supersedes=s) == {"supersedes": decisions.SUPERSEDES_OTHER.format(number=s.number)}
    other = make_advisory(number="AD-26-0002", reference="DEMO-ADV-0002", received_on=TODAY - 3 * DAY)
    model_row(other, floor.model, versions=AdvisoryVersions.ALL, remedy=AdvisoryRemedy.NO_FIX_YET)
    assert refused(record, other, release=floor.r52, supersedes=new) == {"supersedes": decisions.SUPERSEDES_OTHER.format(number=new.number)}
    assert refused(support, release=floor.r52, supersedes=new, reasons=["security_updates"]) == {
        "supersedes": decisions.SUPERSEDES_OTHER.format(number=new.number)}
    ended = DeviceSecurityDecision.objects.get(pk=old.pk)
    assert refused(record, floor.adv, release=floor.r52, supersedes=ended) == {
        "supersedes": decisions.ALREADY_ENDED.format(number=old.number, day=rules.day_words(TODAY))}


def test_a_device_with_an_open_update_to_the_fix_may_still_be_decided(floor):
    """Interim controls beside an open update (critique39_practice B2): the line still needs a response."""
    update_wo(floor.d["P-1"], floor.r56)
    d = record(floor.adv, asset=floor.d["P-1"], response=KEEP_WITH, controls=["traffic_restricted"])
    assert d.governed_ids == [str(floor.d["P-1"].pk)]


# --- the field shape, before any lock --------------------------------------------------------------------------------------------------

def test_every_field_problem_at_once_before_any_lock(floor, monkeypatch):
    calls = []
    monkeypatch.setattr(services, "lock_register", lambda: calls.append("register"))
    assert refused(record, None, asset=floor.d["P-1"], reasons=["security_updates"], response="maybe", controls=["nope"]) == {
        "advisory": services.CHOOSE_ADVISORY, "reasons": decisions.ADVISORY_REASONS, "response": decisions.ADVISORY_RESPONSE_WORDS,
        "controls": decisions.CONTROLS_WORDS}
    assert refused(record, floor.adv, asset=floor.d["P-1"], reasons="security_updates") == {"reasons": decisions.ADVISORY_REASONS}
    assert refused(record, floor.adv, asset=floor.d["P-1"], response=NOT_IN_USE, plan=DecisionPlan.WAIT_FOR_MANUFACTURER) == {
        "plan": decisions.NOT_IN_USE_PLAN}
    assert refused(record, floor.adv, asset=floor.d["P-1"], response=NOT_IN_USE, controls=["network_removed"]) == {
        "controls": decisions.CONTROLS_ONLY_WITH}
    assert refused(record, floor.adv, asset=floor.d["P-1"], response=NOT_IN_USE, review_by=TODAY + 1200 * DAY) == {
        "review_by": decisions.REVIEW_CAP.format(day="Oct 21, 2029", months=36)}  # a keep decision's cap
    assert refused(record, floor.adv, asset=floor.d["P-1"], applies_to="something") == {"applies_to": decisions.APPLIES_TO_WORDS}
    assert calls == []


def test_support_decisions_keep_their_own_shape(floor):
    assert refused(support, asset=floor.d["P-1"], response=NOT_IN_USE) == {"response": decisions.NOT_IN_USE_ADVISORY_ONLY}
    assert refused(support, asset=floor.d["P-1"], advisory=floor.adv) == {"advisory": decisions.ADVISORY_NOT_FOR_SUPPORT}
    assert refused(support, asset=floor.d["P-1"], response=KEEP_WITH, controls=["network_removed"]) == {
        "controls": rules.NETWORK_REMOVED_HINT}
    assert refused(support, asset=floor.d["P-1"], reasons=[]) == {"reasons": decisions.REASONS_NONE}
    assert refused(support, asset=floor.d["P-1"], response="maybe") == {"response": decisions.RESPONSE_WORDS}
    with pytest.raises(ValidationError) as e:
        refusal_for(asset=floor.d["P-1"], advisory=floor.adv)
    assert e.value.message_dict == {"advisory": [decisions.ADVISORY_NOT_FOR_SUPPORT]}


# --- the advisory, under its row lock ------------------------------------------------------------------------------------------------

def test_the_advisorys_state_and_day_are_read_under_its_lock(floor, other_tenant):
    SecurityAdvisory.objects.filter(pk=floor.adv.pk).update(status=AdvisoryStatus.NOT_APPLICABLE, status_on=TODAY,
                                                           not_applicable_reason=NotApplicableReason.RELEASES_NOT_HERE)
    words = decisions.ADVISORY_NOT_APPLICABLE.format(number="AD-26-0001")
    assert words == "AD-26-0001 is marked not applicable here: reopen it first."
    assert refused(record, floor.adv, asset=floor.d["P-1"]) == {"advisory": words}
    assert refusal_for(asset=floor.d["P-1"], applies_to=AppliesTo.ADVISORY, advisory=floor.adv) == decisions.Refusal(
        words, decisions.KIND_ADVISORY)
    SecurityAdvisory.objects.filter(pk=floor.adv.pk).update(removed_on=TODAY)
    assert refused(record, floor.adv, asset=floor.d["P-1"]) == {"advisory": "AD-26-0001 was entered in error."}
    assert not decisions.refusal_for(asset=floor.d["P-1"], applies_to=AppliesTo.ADVISORY, advisory=floor.adv).ends_it
    SecurityAdvisory.objects.filter(pk=floor.adv.pk).update(removed_on=None, status=AdvisoryStatus.CLOSED, not_applicable_reason="")
    assert record(floor.adv, asset=floor.d["P-1"]).advisory == floor.adv  # a closed advisory still has needs
    assert refused(record, floor.adv, asset=floor.d["P-2"], decided_on=TODAY - 21 * DAY) == {
        "decided_on": "The day decided is before AD-26-0001 was received (Oct 1, 2026)."}
    record(floor.adv, asset=floor.d["P-2"], decided_on=TODAY - 20 * DAY)  # the day received
    with tenant_context(other_tenant):
        theirs = make_advisory(number="AD-26-0009", reference="DEMO-ADV-0009")
    assert refused(record, theirs.pk, asset=floor.d["P-3"]) == {"advisory": services.CHOOSE_ADVISORY}
    assert refused(record, "AD-26-0009", asset=floor.d["P-3"]) == {"advisory": services.CHOOSE_ADVISORY}


# --- the scope -------------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def both(floor):
    """check(field, words, kind, advisory=..., **scope): record_decision refuses the scope keyed `field` in `words`, and refusal_for says
    the same, with `kind`, without any lock."""

    def check(field: str, words: str, kind: str, *, advisory=None, **scope):
        adv = advisory or floor.adv
        assert refused(record, adv, **scope) == {field: words}
        got = refusal_for(**scope, applies_to=AppliesTo.ADVISORY, advisory=adv, today=TODAY)
        assert (got.words, got.kind) == (words, kind)
    return check


def test_a_scope_with_no_device_of_ours_is_gone(floor, both, vent_model):
    r58 = release(floor.model, "5.8 (demo)")
    both("release", "No device of ours on site runs 5.8 (demo).", decisions.KIND_GONE, release=r58)
    both("device_model", "No Hamilton-G5 of ours is on site.", decisions.KIND_GONE, device_model=vent_model)
    Asset.objects.filter(pk=floor.d["P-1"].pk).update(status=AssetStatus.RETIRED)
    both("asset", "P-1 is retired: a decision is for a device on site.", decisions.KIND_GONE, asset=floor.d["P-1"])
    Asset.objects.filter(pk=floor.d["T-1"].pk).update(status=AssetStatus.RETIRED)
    both("asset", "T-1 was returned to its owner: a decision is for a device on site.", decisions.KIND_GONE, asset=floor.d["T-1"])


def test_a_model_or_release_the_advisory_does_not_name(floor, both, vent):
    both("device_model", "AD-26-0001 does not name the Hamilton-G5.", decisions.KIND_NOT_NEEDED, device_model=vent.device_model)
    both("asset", "AD-26-0001 does not name CE-10001's model, the Hamilton-G5.", decisions.KIND_NOT_NEEDED, asset=vent)
    both("release", "5.6 (demo) is the release AD-26-0001 names as the fix.", decisions.KIND_NOT_NEEDED, release=floor.r56)
    both("release", "AD-26-0001 does not name 5.4 (demo).", decisions.KIND_NOT_NEEDED, release=floor.r54)


def test_a_device_on_the_fix_or_on_a_release_it_does_not_name(floor, both):
    both("asset", "P-4 runs 5.6 (demo), the release AD-26-0001 names as the fix.", decisions.KIND_NOT_NEEDED, asset=floor.d["P-4"])
    both("asset", "P-5 runs 5.4 (demo), a release AD-26-0001 does not name.", decisions.KIND_NOT_NEEDED, asset=floor.d["P-5"])
    record(floor.adv, asset=floor.d["P-6"])  # no release recorded: a need (never "record it first")


def _hand_patches(asset, reference, applied_on):
    return SoftwareUpdate.objects.create(asset=asset, outcome=UpdateOutcome.PATCHES, reference=reference, applied_on=applied_on,
                                         installed_by=InstalledBy.MANUFACTURER)


def test_a_device_with_the_patches_that_answer_it(floor, both):
    listed = make_advisory(number="AD-26-0002", reference="DEMO-ADV-0002", received_on=TODAY - 10 * DAY)
    model_row(listed, floor.model, versions=AdvisoryVersions.ALL, remedy=AdvisoryRemedy.PATCHES, patches_reference="DEMO-PATCH-2026-10")
    unlisted = make_advisory(number="AD-26-0003", reference="DEMO-ADV-0003", received_on=TODAY - 10 * DAY)
    model_row(unlisted, floor.model, versions=AdvisoryVersions.ALL, remedy=AdvisoryRemedy.PATCHES)
    _hand_patches(floor.d["P-1"], "demo-patch-2026-10", TODAY - 40 * DAY)  # the list named, before it was received
    _hand_patches(floor.d["P-2"], "DEMO-PATCH-2026-09", TODAY - 2 * DAY)  # another list since
    both("asset", "P-1 has the manufacturer's patches recorded Sep 11, 2026 (demo-patch-2026-10), the list AD-26-0002 names.",
         decisions.KIND_NOT_NEEDED, advisory=listed, asset=floor.d["P-1"])
    record(listed, asset=floor.d["P-2"])  # another list: it still needs a response
    both("asset", "P-2 has the manufacturer's patches recorded Oct 19, 2026, since AD-26-0003 was received.", decisions.KIND_NOT_NEEDED,
         advisory=unlisted, asset=floor.d["P-2"])
    record(unlisted, asset=floor.d["P-1"])  # patched before AD-26-0003 was received: a need


def test_a_scope_each_of_whose_devices_is_under_a_more_specific_decision(floor, both):
    on52 = [record(floor.adv, asset=floor.d[t]) for t in ("P-1", "P-2", "P-3")]
    numbers = ", ".join(d.number for d in on52)
    both("release", decisions.GOVERNED_ELSEWHERE.format(numbers=numbers), decisions.KIND_NOT_NEEDED, release=floor.r52)
    # the model's: P-4 and P-5 need none; P-6 (no release recorded) needs one, under a decision of its own
    own = record(floor.adv, asset=floor.d["P-6"])
    words = ("No device a decision for this scope would govern (P-4, P-5) needs a response to AD-26-0001 now: those that do are under a "
             f"more specific decision ({', '.join(sorted([*(d.number for d in on52), own.number]))}).")
    both("device_model", words, decisions.KIND_NOT_NEEDED, device_model=floor.model)
    # support decisions of the scope never count here
    support(asset=floor.d["P-1"])
    both("device_model", words, decisions.KIND_NOT_NEEDED, device_model=floor.model)


def test_a_scope_none_of_whose_devices_needs_a_response(floor, both):
    for tag in ("P-1", "P-2", "P-3", "P-6"):
        facts(floor.d[tag], floor.r56)
    both("device_model", "No device it would govern needs a response to AD-26-0001 now.", decisions.KIND_NOT_NEEDED,
         device_model=floor.model)


def test_in_force_and_its_renewal(floor):
    d = record(floor.adv, release=floor.r52)
    got = refusal_for(release=floor.r52, applies_to=AppliesTo.ADVISORY, advisory=floor.adv, today=TODAY)
    assert (got.words, got.kind, got.live) == (decisions.IN_FORCE.format(number=d.number), decisions.KIND_IN_FORCE, d)
    assert refusal_for(release=floor.r52, supersedes=d, applies_to=AppliesTo.ADVISORY, advisory=floor.adv, today=TODAY) is None
    assert refusal_for(release=floor.r52, today=TODAY) is None  # the support need's scope: no support decision yet


def test_refusal_for_and_would_govern_take_no_lock(floor, monkeypatch):
    d = record(floor.adv, release=floor.r52)
    own = record(floor.adv, asset=floor.d["P-6"])
    locked = []
    real = QuerySet.select_for_update
    monkeypatch.setattr(QuerySet, "select_for_update", lambda self, *x, **k: locked.append(self.model.__name__) or real(self, *x, **k))
    kw = {"applies_to": AppliesTo.ADVISORY, "today": TODAY}
    assert refusal_for(release=floor.r52, supersedes=d.number, advisory=floor.adv.number, **kw) is None
    assert refusal_for(asset=floor.d["P-6"], supersedes=own, advisory=floor.adv, **kw) is None
    assert refusal_for(device_model=floor.model, advisory=floor.adv.pk, **kw).kind == decisions.KIND_NOT_NEEDED
    assert refusal_for(asset=floor.d["P-4"], advisory=floor.adv, **kw).kind == decisions.KIND_NOT_NEEDED
    members = list(rules.scope_devices(device_model=floor.model).select_related(*rules._WITH_RECORD))
    governed = decisions.would_govern(members, device_model=floor.model, applies_to=AppliesTo.ADVISORY, advisory=floor.adv.number)
    assert [a.tag for a in governed] == ["P-4", "P-5"]  # P-1 to P-3 under 5.2's, P-6 under its own
    assert [a.tag for a in decisions.would_govern(members, device_model=floor.model)] == [a.tag for a in members]  # support: none live
    assert decisions.network_refusal(governed, TODAY) and locked == []


# --- network_removed and return it to its owner ----------------------------------------------------------------------------------------

def test_network_removed_needs_each_devices_own_fact(floor):
    keep = {"response": KEEP_WITH, "controls": ["network_removed", "physical_controls"]}
    assert refused(record, floor.adv, asset=floor.d["P-1"], **keep) == {
        "controls": "P-1 has no connection recorded here: record Not connected here on it first (Record facts), or choose other controls."}
    facts(floor.d["P-1"], floor.r52, network=Network.WIRELESS)
    assert refused(record, floor.adv, asset=floor.d["P-1"], **keep) == {
        "controls": "P-1 is recorded connected (wireless): record Not connected here on it first (Record facts), or choose other controls."}
    facts(floor.d["P-1"], floor.r52, network=Network.NOT_CONNECTED)
    d = record(floor.adv, asset=floor.d["P-1"], **keep)
    assert d.controls == ["network_removed", "physical_controls"]
    # a release's devices: every one it would govern (P-1's own decision governs P-1)
    for tag in ("P-2", "P-3"):
        facts(floor.d[tag], floor.r52, network=Network.INTERMITTENT)
    assert refused(record, floor.adv, release=floor.r52, **keep) == {
        "controls": "P-2, P-3 are recorded connected: record Not connected here on them first (Record facts), or choose other controls."}
    model = DeviceModel.objects.create(manufacturer="Stryker", model="Bed X", description="Bed", category="Beds")
    beds = [Asset.objects.create(tag=f"B-{i}", device_model=model, department=floor.dept) for i in range(7)]
    model_row(floor.adv, model, versions=AdvisoryVersions.NOT_STATED, remedy=AdvisoryRemedy.NO_FIX_YET)
    assert refused(record, floor.adv, device_model=model, **keep) == {
        "controls": "B-0, B-1, B-2, B-3, B-4 and 2 more have no connection recorded here: record Not connected here on them first (Record "
                    "facts), or choose other controls."}
    for b in beds:
        facts(b, network=Network.NOT_CONNECTED)
    record(floor.adv, device_model=model, **keep)


def test_return_to_its_owner_only_for_a_temporary_devices_own_decision(floor):
    record(floor.adv, asset=floor.d["T-1"], plan=DecisionPlan.RETURN_TO_OWNER)
    assert refused(record, floor.adv, asset=floor.d["P-1"], plan=DecisionPlan.RETURN_TO_OWNER) == {"plan": decisions.RETURN_ONLY_TEMPORARY}
    assert refused(record, floor.adv, release=floor.r52, plan=DecisionPlan.RETURN_TO_OWNER) == {"plan": decisions.RETURN_ONLY_TEMPORARY}


def test_segmented_reads_the_models_record_for_an_advisory_too(floor):
    floor.profile.segmented = Segmented.NONE
    floor.profile.save()
    assert refused(record, floor.adv, asset=floor.d["P-1"], response=KEEP_WITH, controls=["segmented"]) == {
        "controls": decisions.SEGMENT_REFUSAL}


# --- levels -------------------------------------------------------------------------------------------------------------------------------

def test_only_approve_with_equipment_view_records_an_advisory_decision(tenant, floor, make_user):
    for slug, levels in (("editor", {Module.CYBERSECURITY: Level.EDIT, Module.EQUIPMENT: Level.FULL}),
                         ("approver", {Module.CYBERSECURITY: Level.APPROVE}),
                         ("viewer", {Module.CYBERSECURITY: Level.VIEW, Module.EQUIPMENT: Level.VIEW})):
        with pytest.raises(PermissionDenied):
            record(floor.adv, asset=floor.d["P-1"], by=custom_user(tenant, slug, levels))
    with pytest.raises(PermissionDenied):
        record(floor.adv, asset=floor.d["P-1"], by=make_user("technician"))
    for slug in ("vendor", "requester"):
        Role.objects.get(slug=slug).set_levels({Module.CYBERSECURITY: Level.FULL, Module.EQUIPMENT: Level.FULL})
        with pytest.raises(PermissionDenied):
            record(floor.adv, asset=floor.d["P-1"], by=User.objects.get(pk=make_user(slug).pk))
    assert not DeviceSecurityDecision.objects.exists()
    record(floor.adv, asset=floor.d["P-1"], by=make_user("manager"))
    it = custom_user(tenant, "it-security", {Module.CYBERSECURITY: Level.APPROVE, Module.EQUIPMENT: Level.VIEW})
    record(floor.adv, asset=floor.d["P-2"], by=it)


# --- the lock spy -------------------------------------------------------------------------------------------------------------------------

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
    wrap(services, "_locked_advisory", "advisory")
    wrap(services, "_locked_release", "release")
    wrap(decisions, "_number", "number")
    wrap(services, "_locked_devices", "devices")
    return SimpleNamespace(calls=calls, reset=calls.clear)


def test_an_advisory_decision_takes_its_locks_in_the_contracts_order(floor, spy):
    on52 = record(floor.adv, release=floor.r52)
    assert spy.calls == ["register", "advisory", "release", "number"]
    spy.reset()
    record(floor.adv, release=floor.r52, supersedes=on52)
    assert spy.calls == ["register", "decision", "advisory", "release", "number"]
    spy.reset()
    record(floor.adv, asset=floor.d["P-6"])
    assert spy.calls == ["register", "advisory", "number", "devices"]
    spy.reset()
    other = make_advisory(number="AD-26-0002", reference="DEMO-ADV-0002", received_on=TODAY - DAY)
    model_row(other, floor.model, versions=AdvisoryVersions.ALL, remedy=AdvisoryRemedy.NO_FIX_YET)
    spy.reset()
    record(other, device_model=floor.model)
    assert spy.calls == ["register", "advisory", "number"]
    spy.reset()
    support(release=floor.r52)  # slice 39's pins unchanged
    assert spy.calls == ["register", "release", "number"]
    spy.reset()
    support(asset=floor.d["P-1"])
    assert spy.calls == ["register", "number", "devices"]


def test_a_refusal_under_the_locks_records_nothing(floor):
    old = record(floor.adv, release=floor.r52)
    refused(record, floor.adv, release=floor.r52, supersedes=old, response=KEEP_WITH, controls=["network_removed"])
    old.refresh_from_db()
    assert old.ended_on is None and DeviceSecurityDecision.objects.count() == 1


# --- words -------------------------------------------------------------------------------------------------------------------------------

def test_not_in_use_here_reviews_as_a_keep_decision():
    assert rules.review_cap(NOT_IN_USE, TODAY) == rules.review_cap(KEEP_WITHOUT, TODAY) == date(2029, 10, 21)
    assert rules.review_default(NOT_IN_USE, TODAY) == date(2027, 10, 21)
    assert rules.review_rule_words(NOT_IN_USE) == rules.review_rule_words(KEEP_WITH) == (
        "By default 12 months after the day decided (at most 36 months)")


def test_the_new_words_say_no_never_word():
    texts = [v for k, v in vars(decisions).items() if k.isupper() and isinstance(v, str)]
    texts += [rules.ADVISORY_CHANGED_WORDS, rules.ADVISORY_CHANGED_NO_DAY, rules.FIX_NAMED_WORDS, rules.NETWORK_BACK_WORDS,
              rules.NO_NEED_WORDS, rules.FOR_ADVISORY, *SEEN]
    assert len(texts) > 70 and [t for t in texts if never(t)] == []
