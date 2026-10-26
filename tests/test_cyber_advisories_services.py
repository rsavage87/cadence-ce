"""
Slice 40 wave 1A, the advisories' writers (apps.cybersecurity.advisories) and the release refusals they bring (services.removal_refusal,
services.update_release), on SQLite and PostgreSQL:

- record_advisory: the number by the year received, the CVE IDs as pasted, the first source; every field problem at once; the same
  publication refused (the constraint its backstop); a CVE ID, or the reference under another source kind, on another live advisory
  offered (refused until `seen` names it), never once seen.
- update_advisory, add_source, update_source, record_revision (the reopening kinds: the confirmation cleared and a change counted, a
  not-applicable advisory moved back to open, a closed one kept closed), remove_source (the last live source kept).
- set_model (one row per model, updated in place, its releases diffed; a change after the confirmation clears it unless confirmed
  again; a live decision's scope kept), remove_model (a second removal in words), confirm_models.
- mark_not_applicable (every reason checked against the records), close (against the lines and what answers them, in words naming the
  devices to a reader of devices), reopen, mark_entered_in_error (the references freed).
- Levels: Edit, Approve, Approve and Equipment View, View only, scoped users. Refusals naming a decision in three readers' words.
- The lock spy (the register, the advisory, the releases), a lock conflict and a deferred key answered in words, and the lock-free
  readers: never select_for_update, the same words as their writers.
- History: fixed sentences, AD numbers only, in words.
"""
import re
from datetime import timedelta
from types import SimpleNamespace

import pytest
from cyber_fixtures import facts, make_advisory, model_row, release, update_wo
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, OperationalError, transaction
from django.utils import timezone

from apps.accounts.models import Level, Module, Role, User
from apps.core.history import record_history
from apps.cybersecurity import advisories, rules, services
from apps.cybersecurity.advisories import (
    add_source,
    close,
    confirm_models,
    mark_entered_in_error,
    mark_not_applicable,
    record_advisory,
    record_revision,
    remove_model,
    remove_source,
    reopen,
    set_model,
    update_advisory,
    update_source,
)
from apps.cybersecurity.models import (
    AcceptedBy,
    AdvisoryModel,
    AdvisoryRelease,
    AdvisoryRemedy,
    AdvisorySource,
    AdvisoryStatus,
    AdvisoryVersions,
    AppliesTo,
    DecisionResponse,
    DeviceSecurityDecision,
    InstalledBy,
    NotApplicableReason,
    SecurityAdvisory,
    SoftwareUpdate,
    UpdateOutcome,
)
from apps.equipment.models import Asset, Department, DeviceModel, Ownership
from apps.tenants.context import tenant_context
from apps.workorders.models import WorkOrder, WoStatus

TODAY = timezone.localdate()
YY = f"{TODAY:%y}"
DAY = timedelta(days=1)
BULLETIN, PORTAL, OTHER = "manufacturer_bulletin", "manufacturer_portal", "other"
LISTED, ALL, NOT_STATED = AdvisoryVersions.LISTED, AdvisoryVersions.ALL, AdvisoryVersions.NOT_STATED
NO_MODEL_HERE, NONE_HERE, RELEASES_NOT_HERE = (NotApplicableReason.NO_MODEL_HERE, NotApplicableReason.NONE_HERE,
                                               NotApplicableReason.RELEASES_NOT_HERE)


def never(text: str) -> list:
    return [w for w in rules.DECISION_NEVER_WORDS if re.search(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", text, re.IGNORECASE)]


def refused(fn, *args, **kwargs) -> dict:
    """The refusal's words by field (a ValidationError), checked for never words."""
    with pytest.raises(ValidationError) as e:
        fn(*args, **kwargs)
    out = {k: v[0] if len(v) == 1 else v for k, v in e.value.message_dict.items()}
    assert not [t for t in out.values() if isinstance(t, str) and never(t)], out
    return out


def reasons_of(obj) -> list:
    return list(type(obj).history.filter(id=obj.pk).order_by("history_date", "history_id").values_list("history_change_reason", flat=True))


def custom_user(tenant, slug, levels=None) -> User:
    """A user of a custom role with `levels` (or of the default role `slug` when none), no password (never signs in here: quick)."""
    if levels is None:
        role = Role.objects.get(slug=slug)
    else:
        role = Role.objects.create(name=slug.title(), slug=slug)
        role.set_levels(levels)
    return User.objects.create_user(username=f"{slug}@riverside.example", password=None, tenant=tenant, role=role,
                                    first_name=slug.title(), last_name="User")


def record(**fields):
    values = {"received_on": TODAY - 2 * DAY, "cves": "", "source": BULLETIN, "reference": "DEMO-ADV-0001", "by": None, "today": TODAY}
    return record_advisory(**{**values, **fields})


def decision(adv, n=1, **fields) -> DeviceSecurityDecision:
    """An advisory decision made directly (wave 1B writes them): the slice 39 table's shape for an advisory."""
    values = {"number": f"SD-{YY}-{n:04d}", "applies_to": AppliesTo.ADVISORY, "advisory": adv, "advisory_changes": adv.changes,
              "reasons": [], "response": DecisionResponse.KEEP_WITHOUT_ADDED_CONTROLS, "accepted_by": AcceptedBy.CE_LEADER,
              "decided_on": TODAY, "review_by": TODAY + 365 * DAY, **fields}
    return DeviceSecurityDecision.objects.create(**values)


@pytest.fixture
def floor(ctx, dept, pump_model):
    """The pump model: 5.2 (demo) and 5.4 (demo) named by advisories, 5.6 (demo) the fix; P-1, P-2 of ours on 5.2, P-3 on 5.4, P-4 on 5.6,
    P-5 with no release recorded; the rental T-1 on 5.2 (due back in 20 days)."""
    r52, r54, r56 = release(pump_model, "5.2 (demo)"), release(pump_model, "5.4 (demo)"), release(pump_model, "5.6 (demo)")
    d = {}
    for tag, rel in (("P-1", r52), ("P-2", r52), ("P-3", r54), ("P-4", r56), ("P-5", None)):
        d[tag] = Asset.objects.create(tag=tag, device_model=pump_model, department=dept)
        if rel is not None:
            facts(d[tag], rel)
    d["T-1"] = Asset.objects.create(tag="T-1", device_model=pump_model, department=dept, ownership=Ownership.RENTAL,
                                    owner="Summit Medical Rentals", due_back_on=TODAY + 20 * DAY)
    facts(d["T-1"], r52)
    return SimpleNamespace(model=pump_model, r52=r52, r54=r54, r56=r56, d=d, dept=dept)


@pytest.fixture
def people(tenant):
    """Each reader: the CE manager (Device security Approve, Equipment Edit: can_decide), a technician (Edit, Equipment Edit), IT's
    analyst (Edit, Equipment View), Edit alone, Approve alone (can_correct_advisories, not can_decide), View with Equipment View, View
    alone, and the two scoped roles with every level."""
    out = SimpleNamespace(manager=custom_user(tenant, "manager"), tech=custom_user(tenant, "technician"))
    out.it = custom_user(tenant, "it-security", {Module.CYBERSECURITY: Level.EDIT, Module.EQUIPMENT: Level.VIEW})
    out.edit_only = custom_user(tenant, "edit-only", {Module.CYBERSECURITY: Level.EDIT})
    out.approve_only = custom_user(tenant, "approve-only", {Module.CYBERSECURITY: Level.APPROVE})
    out.viewer = custom_user(tenant, "viewer", {Module.CYBERSECURITY: Level.VIEW, Module.EQUIPMENT: Level.VIEW})
    out.view_only = custom_user(tenant, "view-only", {Module.CYBERSECURITY: Level.VIEW})
    out.scoped = []
    for slug in ("vendor", "requester"):
        Role.objects.get(slug=slug).set_levels({Module.CYBERSECURITY: Level.FULL, Module.EQUIPMENT: Level.FULL})
        out.scoped.append(User.objects.get(pk=custom_user(tenant, slug).pk))
    return out


# --- record_advisory -------------------------------------------------------------------------------------------------------------------

def test_an_advisory_is_recorded_with_its_number_cves_and_first_source(ctx, make_user):
    kim = make_user("director")
    a = record(cves="cve-2026-1111, CVE-2026-2222\nCVE-2026-1111  CVE-2026-33333", url="https://www.example.com/notice/1",
               published_on=TODAY - 5 * DAY, by=kim)
    assert (a.number, a.cves, a.status, a.recorded_by, a.received_on) == (f"AD-{YY}-0001", ["CVE-2026-1111", "CVE-2026-2222",
                                                                                            "CVE-2026-33333"],
                                                                          AdvisoryStatus.OPEN, kim, TODAY - 2 * DAY)
    assert (a.models_confirmed_on, a.changes, a.changed_on, a.removed_on) == (None, 0, None, None)
    src = a.sources.get()
    assert (src.source, src.reference, src.reference_key, src.url, src.published_on) == (
        BULLETIN, "DEMO-ADV-0001", "demo-adv-0001", "https://www.example.com/notice/1", TODAY - 5 * DAY)
    assert reasons_of(a) == ["Recorded"] and reasons_of(src) == ["Recorded"]
    assert record(reference="DEMO-ADV-0002").number == f"AD-{YY}-0002"
    last_year = TODAY.replace(year=TODAY.year - 1, month=12, day=30)
    assert record(reference="DEMO-ADV-0003", received_on=last_year).number == f"AD-{last_year:%y}-0001"  # by the year received
    assert record(reference="", source=OTHER).sources.get().reference_key == ""  # a reference is optional


def test_every_field_problem_at_once_and_never_a_lock(ctx, monkeypatch):
    calls = []
    monkeypatch.setattr(services, "lock_register", lambda: calls.append("register"))
    got = refused(record, received_on=TODAY + DAY, cves="CVE-2026-1111 (Moderate) CVE-2026-2222", source="blog",
                  reference="DEMO ADV 1", url="https://vendor.example.com:8443/notice", published_on="2026-13-01")
    assert got == {"received_on": "The day received cannot be after today.", "cves": "'(Moderate)' is not a CVE ID: paste the IDs only.",
                   "source": advisories.SOURCE_WORDS, "reference": advisories.REFERENCE_WORDS, "url": advisories.LINK_WORDS,
                   "published_on": "The day published: enter a day."}
    assert refused(record, received_on=None, cves=["CVE-2026-1111", 7]) == {"received_on": advisories.RECEIVED_NEEDED,
                                                                            "cves": advisories.CVE_LIST_WORDS}
    assert refused(record, received_on="1949-01-01")["received_on"] == "The day received: enter the day in full (the year looks wrong)."
    for token in ("10.20.30.40", "VLAN-120", "tcp-443", "00-1A-2B-3C-4D-5E"):
        assert refused(record, reference=token) == {"reference": advisories.REFERENCE_WORDS}
    assert refused(record, reference="X" * 41) == {"reference": "Keep it to 40 characters."}
    long_piece = "Z" * 60
    assert refused(record, cves=long_piece)["cves"] == f"'{'Z' * 40}...' is not a CVE ID: paste the IDs only."
    with pytest.raises(ValidationError) as e:  # the piece is the person's own text, named as pasted (critique40_practice N7)
        record(cves="CVE-2026-1111, (Critical)")
    assert e.value.message_dict == {"cves": ["'(Critical)' is not a CVE ID: paste the IDs only."]}
    assert calls == [] and not SecurityAdvisory.objects.exists()


def test_the_same_publication_is_refused_and_another_kind_is_offered(ctx):
    first = record(cves="CVE-2026-1111 CVE-2026-2222")
    assert refused(record, reference="demo-adv-0001") == {"reference": f"demo-adv-0001 is already recorded as a source of {first.number}."}
    # the same reference from another publisher, and a shared CVE ID: offered until the modal showed them
    words = refused(record, source=PORTAL, reference="DEMO-ADV-0009", cves="CVE-2026-9999, CVE-2026-2222")
    assert words == {"cves": f"CVE-2026-2222 is also on {first.number}: add this as a source of {first.number}, or record it as its own "
                             "advisory."}
    assert refused(record, source=PORTAL, reference="DEMO-ADV-0001", cves="CVE-2026-1111 CVE-2026-2222") == {
        "cves": f"CVE-2026-1111 and 2 more are also on {first.number}: add this as a source of {first.number}, or record it as its own "
                "advisory."}
    assert advisories.overlaps(["CVE-2026-2222", "cve-2026-1111"], source=PORTAL, reference="demo-adv-0001") == {
        first.number: ["CVE-2026-2222", "CVE-2026-1111", "demo-adv-0001"]}
    second = record(source=PORTAL, cves="CVE-2026-2222", seen=[first.number.lower()])
    assert second.number == f"AD-{YY}-0002" and second.cves == ["CVE-2026-2222"]
    # a third shares with both: refused naming the one the modal did not show
    assert refused(record, reference="DEMO-ADV-0003", cves="CVE-2026-2222", seen=[first.number]) == {
        "cves": f"CVE-2026-2222 is also on {second.number}: add this as a source of {second.number}, or record it as its own advisory."}
    assert refused(record, reference="DEMO-ADV-0003", cves="CVE-2026-2222")["cves"].endswith(
        f"also on {first.number} and {second.number}: add this as a source of {first.number}, or record it as its own advisory.")
    with pytest.raises(ValidationError):
        record(reference="DEMO-ADV-0003", cves="CVE-2026-2222", seen=first.number)  # a single number is one advisory shown
    third = record(reference="DEMO-ADV-0003", cves="CVE-2026-2222", seen=(first.number, second.number))
    assert third.number == f"AD-{YY}-0003"


def test_the_constraint_backs_the_publication_check(ctx, monkeypatch):
    first = record()
    monkeypatch.setattr(advisories, "_duplicates", lambda *args, **kwargs: {})  # as if another request had checked first
    assert refused(record, reference="DEMO-ADV-0001", seen=[first.number]) == {
        "reference": f"DEMO-ADV-0001 is already recorded as a source of {first.number}."}
    assert SecurityAdvisory.objects.count() == 1  # nothing kept


def test_an_advisory_entered_in_error_shares_nothing(ctx):
    first = record(cves="CVE-2026-1111")
    mark_entered_in_error(first, by=None, today=TODAY)
    assert advisories.overlaps(["CVE-2026-1111"], source=BULLETIN, reference="DEMO-ADV-0001") == {}
    again = record(cves="CVE-2026-1111")  # its reference is free again
    assert again.sources.get().reference_key == "demo-adv-0001"
    assert AdvisorySource.objects.get(advisory=first).removed_on is None  # its own removal day untouched (locks S2)


# --- update_advisory, sources, revisions ---------------------------------------------------------------------------------------------

def test_update_advisory(ctx, floor):
    a = record(cves="CVE-2026-1111")
    other = record(reference="DEMO-ADV-0002", cves="CVE-2026-5555")
    unchanged = update_advisory(a, received_on=a.received_on, cves="cve-2026-1111", by=None, today=TODAY)
    assert reasons_of(unchanged) == ["Recorded"]  # nothing saved
    assert refused(update_advisory, a, cves="CVE-2026-1111 CVE-2026-5555", by=None, today=TODAY) == {
        "cves": f"CVE-2026-5555 is also on {other.number}: add this as a source of {other.number}, or record it as its own advisory."}
    update_advisory(a, cves="CVE-2026-1111 CVE-2026-5555", seen=[other.number], by=None, today=TODAY)
    # an ID it already held is never refused again
    update_advisory(a, cves="CVE-2026-5555 CVE-2026-1111 CVE-2026-7777", by=None, today=TODAY)
    assert a.cves == ["CVE-2026-5555", "CVE-2026-1111", "CVE-2026-7777"]  # the caller's copy follows
    model_row(a, floor.model, releases=[floor.r52], fix=floor.r56)  # confirmed
    decision(a, decided_on=TODAY - 2 * DAY, review_by=TODAY + 30 * DAY, release=floor.r52, device_model=floor.model)
    assert refused(update_advisory, a, received_on=TODAY - DAY, by=None, today=TODAY) == {
        "received_on": f"SD-{YY}-0001 under this advisory was decided {rules.day_words(TODAY - 2 * DAY)}: the day received cannot be "
                       "after it."}
    a = update_advisory(a, received_on=TODAY - 9 * DAY, by=None, today=TODAY)
    assert (a.received_on, a.models_confirmed_on, a.changed_on, a.changes) == (TODAY - 9 * DAY, None, TODAY, 1)  # rules B1
    assert reasons_of(a)[-1] == "Edited"
    mark_entered_in_error_directly(a)
    assert refused(update_advisory, a, received_on=TODAY - 8 * DAY, by=None, today=TODAY) == {
        "advisory": f"{a.number} was entered in error."}


def mark_entered_in_error_directly(a):
    SecurityAdvisory.objects.filter(pk=a.pk).update(removed_on=TODAY)


def test_add_source_merges_the_cves_of_the_locked_row(ctx):
    a = record(cves="CVE-2026-1111")
    stale = SecurityAdvisory.objects.get(pk=a.pk)
    update_advisory(a, cves="CVE-2026-1111 CVE-2026-2222", by=None, today=TODAY)
    src = add_source(stale, source=PORTAL, reference="DEMO-ADV-0001-N", published_on=TODAY - DAY, cves="CVE-2026-3333 cve-2026-2222",
                     by=None, today=TODAY)
    assert SecurityAdvisory.objects.get(pk=a.pk).cves == ["CVE-2026-1111", "CVE-2026-2222", "CVE-2026-3333"]
    assert stale.cves == ["CVE-2026-1111", "CVE-2026-2222", "CVE-2026-3333"]  # the caller's copy follows
    assert reasons_of(src) == ["Source added"] and reasons_of(a)[-1] == "Source added"
    assert refused(add_source, a, source=PORTAL, reference="demo-adv-0001-n", by=None, today=TODAY) == {
        "reference": f"demo-adv-0001-n is already recorded as a source of {a.number}."}
    add_source(a, source=OTHER, by=None, today=TODAY)  # no reference: never a duplicate
    mark_entered_in_error_directly(a)
    assert refused(add_source, a, source=OTHER, by=None, today=TODAY) == {"advisory": f"{a.number} was entered in error."}


def test_update_source_and_its_days(ctx):
    a = record(published_on=TODAY - 10 * DAY)
    src = a.sources.get()
    b = record(reference="DEMO-ADV-0002")
    assert refused(update_source, src, source=BULLETIN, reference="DEMO-ADV-0002", by=None, today=TODAY) == {
        "reference": f"DEMO-ADV-0002 is already recorded as a source of {b.number}."}
    record_revision(src, revision="2", updated_on=TODAY - 4 * DAY, by=None, today=TODAY)
    assert refused(update_source, src, source=BULLETIN, reference="DEMO-ADV-0001", published_on=TODAY - 3 * DAY, by=None,
                   today=TODAY) == {"published_on": f"The day published cannot be after the day updated ({rules.day_words(TODAY - 4 * DAY)})."}
    edited = update_source(str(src.pk), source=OTHER, reference="DEMO-ADV-0001", url="https://www.example.com/a",
                           published_on=TODAY - 10 * DAY, by=None, today=TODAY)
    assert (edited.source, edited.url, edited.revision) == (OTHER, "https://www.example.com/a", "2")  # its revision kept
    assert reasons_of(edited) == ["Recorded", "Revision recorded", "Edited"]
    update_source(src, source=OTHER, reference="DEMO-ADV-0001", url="https://www.example.com/a", published_on=TODAY - 10 * DAY, by=None,
                  today=TODAY)
    assert reasons_of(edited) == ["Recorded", "Revision recorded", "Edited"]  # nothing changed: nothing saved
    add_source(a, source=PORTAL, by=None, today=TODAY)
    remove_source(src, by=None, today=TODAY)
    assert refused(update_source, src, source=OTHER, by=None, today=TODAY) == {"source": f"This source was removed from {a.number}."}
    assert refused(update_source, "not-an-id", source=OTHER, by=None, today=TODAY) == {"source": advisories.CHOOSE_SOURCE}


def test_a_revision_that_changes_what_it_names_asks_to_confirm_again(ctx, floor):
    a = record(published_on=TODAY - 10 * DAY)
    src = a.sources.get()
    model_row(a, floor.model, releases=[floor.r52], fix=floor.r56)
    assert refused(record_revision, src, by=None, today=TODAY) == {"revision": advisories.REVISION_NEEDED}
    assert refused(record_revision, src, revision="Update A", updated_on=TODAY - 11 * DAY, last_change=["typo"], by=None,
                   today=TODAY) == {"revision": advisories.REFERENCE_WORDS, "last_change": advisories.CHANGE_WORDS}
    assert refused(record_revision, src, revision="UpdateA", updated_on=TODAY - 11 * DAY, by=None, today=TODAY) == {
        "updated_on": f"The day updated cannot be before the day published ({rules.day_words(TODAY - 10 * DAY)})."}
    # mitigations changed: recorded, nothing reopens
    record_revision(src, revision="2", updated_on=TODAY - 8 * DAY, last_change=["mitigations_changed"], by=None, today=TODAY)
    a.refresh_from_db()
    assert (a.models_confirmed_on, a.changes, src.revision, src.last_change) == (TODAY, 0, "2", ["mitigations_changed"])
    record_revision(src, revision="UpdateA", updated_on=TODAY - DAY, last_change=["fix_available", "products_changed"], by=None,
                    today=TODAY)
    a.refresh_from_db()
    assert (a.models_confirmed_on, a.changed_on, a.changes, a.status) == (None, TODAY, 1, AdvisoryStatus.OPEN)
    assert src.last_change == ["products_changed", "fix_available"]  # the choices' order
    assert reasons_of(a)[-1] == "Revision recorded" and reasons_of(a).count("Revision recorded") == 1  # mitigations saved none
    assert reasons_of(src) == ["Recorded", "Revision recorded", "Revision recorded"]


def test_a_revision_moves_a_not_applicable_advisory_back_to_open_and_keeps_a_closed_one_closed(ctx, floor, people):
    na = record()
    mark_not_applicable(na, reason=NO_MODEL_HERE, by=people.tech, today=TODAY)
    record_revision(na.sources.get(), revision="2", last_change=["vulnerabilities_added"], updated_on=TODAY, by=people.it, today=TODAY)
    na.refresh_from_db()
    assert (na.status, na.not_applicable_reason, na.status_on, na.status_by, na.models_confirmed_on) == (AdvisoryStatus.OPEN, "", None,
                                                                                                          None, None)
    assert reasons_of(na)[-1] == "Reopened: a revision changed what it names"
    shut = record(reference="DEMO-ADV-0002")
    model_row(shut, floor.model, versions=ALL, remedy=AdvisoryRemedy.NO_FIX_YET)
    for tag in ("P-1", "P-2", "P-3", "P-4", "P-5"):
        decision_for(shut, floor, tag)
    decision(shut, n=9, asset=floor.d["T-1"])
    close(shut, by=people.manager, today=TODAY)
    record_revision(shut.sources.get(), revision="2", last_change=["products_changed"], updated_on=TODAY, by=people.it, today=TODAY)
    shut.refresh_from_db()
    assert (shut.status, shut.models_confirmed_on, shut.changes) == (AdvisoryStatus.CLOSED, None, 1)  # closed stays closed
    assert rules.status_words(shut, devices_visible=False) == (f"Closed ({people.manager}, {rules.day_words(TODAY)}) · models to confirm "
                                                               "since a revision")


def decision_for(adv, floor, tag, n=None):
    """A device's advisory decision (each its own number)."""
    count = DeviceSecurityDecision.objects.count() + 1
    return decision(adv, n=n or count, asset=floor.d[tag])


def test_remove_source_keeps_the_last(ctx):
    a = record()
    src = a.sources.get()
    assert refused(remove_source, src, by=None, today=TODAY) == {"source": advisories.LAST_SOURCE}
    other = add_source(a, source=OTHER, reference="DEMO-ADV-0001-N", by=None, today=TODAY)
    removed = remove_source(src, by=None, today=TODAY)
    assert (removed.removed_on, src.removed_on) == (TODAY, TODAY) and reasons_of(src) == ["Recorded", "Source removed"]
    assert refused(remove_source, src, by=None, today=TODAY) == {"source": f"This source was removed from {a.number}."}
    assert refused(remove_source, other, by=None, today=TODAY) == {"source": advisories.LAST_SOURCE}
    assert rules.advisory_name(a) == f"{a.number} · DEMO-ADV-0001-N · no model of this facility's catalog"  # the first live source
    again = record(reference="DEMO-ADV-0001")  # a removed source frees its reference
    assert again.number == f"AD-{YY}-0002"


# --- set_model, remove_model, confirm_models ---------------------------------------------------------------------------------------------

def test_set_model_names_a_model_with_its_releases_and_fix(ctx, floor, make_user):
    kim = make_user("director")
    a = record()
    later = release(floor.model, "5.8 (demo)")
    row = set_model(a, device_model=floor.model, versions=LISTED, release_ids=[floor.r54.pk, str(floor.r52.pk)], remedy="release",
                    fix_release=floor.r56, by=kim, today=TODAY)
    assert (row.versions, row.remedy, row.fix_release_id, row.patches_reference) == (LISTED, "release", floor.r56.pk, "")
    assert list(row.releases.order_by("release__version_key").values_list("release__version", "advisory_id")) == [
        ("5.2 (demo)", a.pk), ("5.4 (demo)", a.pk)]
    assert sorted(row.seen_release_ids) == sorted(str(r.pk) for r in (floor.r52, floor.r54, floor.r56, later))
    a.refresh_from_db()
    assert a.models_confirmed_on is None and a.changes == 0  # not confirmed: no change counted
    assert reasons_of(row) == ["Models set"] and set(reasons_of(AdvisoryRelease.objects.first())) == {"Models set"}
    # in place: the row kept, its releases diffed, confirmed with "These are all the models it names that are here"
    first_listed = AdvisoryRelease.objects.get(release=floor.r54)
    again = set_model(a, device_model=floor.model, versions=LISTED, release_ids=[floor.r54, later], remedy="release", fix_release=floor.r56,
                      confirm=True, by=kim, today=TODAY)
    assert again.pk == row.pk and AdvisoryModel.objects.count() == 1
    assert set(again.releases.values_list("release_id", flat=True)) == {floor.r54.pk, later.pk}
    assert AdvisoryRelease.objects.filter(pk=first_listed.pk).exists()  # kept, never deleted and added again
    a.refresh_from_db()
    assert (a.models_confirmed_on, a.models_confirmed_by, a.changes) == (TODAY, kim, 0)
    assert reasons_of(a)[-1] == "Models set"
    # after the confirmation: a change clears it and counts; the same values save nothing that counts
    set_model(a, device_model=floor.model, versions=LISTED, release_ids=[floor.r54, later], remedy="release", fix_release=floor.r56,
              by=kim, today=TODAY)
    a.refresh_from_db()
    assert (a.models_confirmed_on, a.changes) == (TODAY, 0)
    set_model(a, device_model=floor.model, versions=ALL, remedy="mitigations", by=kim, today=TODAY)
    a.refresh_from_db()
    assert (a.models_confirmed_on, a.changed_on, a.changes) == (None, TODAY, 1)
    assert not AdvisoryRelease.objects.exists()  # versions all names no release rows
    set_model(a, device_model=floor.model, versions=ALL, remedy="mitigations", confirm=True, by=kim, today=TODAY)
    a.refresh_from_db()
    assert (a.models_confirmed_on, a.changes) == (TODAY, 1) and reasons_of(a)[-1] == "Models confirmed"


def test_set_model_takes_ids_as_a_door_or_the_api_sends_them(ctx, floor):
    a = record()
    row = set_model(str(a.number).lower(), device_model=str(floor.model.pk), versions=LISTED, release_ids=floor.r52.pk, remedy="release",
                    fix_release=floor.r56.pk, by=None, today=TODAY)  # a single UUID each, the advisory by its number
    assert (row.fix_release_id, list(row.releases.values_list("release_id", flat=True))) == (floor.r56.pk, [floor.r52.pk])
    row = set_model(a.pk, device_model=floor.model.pk, versions=LISTED, release_ids=[str(floor.r54.pk)], remedy="release",
                    fix_release=str(floor.r56.pk), by=None, today=TODAY)
    assert list(row.releases.values_list("release_id", flat=True)) == [floor.r54.pk]


def test_set_model_refuses_each_shape_at_once(ctx, floor, vent_model):
    a = record()
    base = {"device_model": floor.model, "by": None, "today": TODAY}
    assert refused(set_model, a, versions="some", remedy="hope", release_ids=["x"], **base) == {
        "versions": advisories.VERSIONS_WORDS, "remedy": advisories.REMEDY_WORDS, "release_ids": advisories.CHOOSE_RELEASES}
    assert refused(set_model, a, versions=ALL, remedy="release", **base) == {"versions": advisories.FIX_ONLY_LISTED,
                                                                             "fix_release": advisories.FIX_NEEDED}
    assert refused(set_model, a, versions=LISTED, remedy="mitigations", fix_release=floor.r56, patches_reference="P-1", **base) == {
        "fix_release": advisories.FIX_ONLY_RELEASE, "release_ids": advisories.CHOOSE_RELEASES, "patches_reference": advisories.PATCHES_ONLY}
    assert refused(set_model, a, versions=NOT_STATED, remedy="patches", release_ids=[floor.r52], patches_reference="10.1.2.3", **base) == {
        "release_ids": advisories.RELEASES_ONLY_LISTED, "patches_reference": advisories.REFERENCE_WORDS}
    assert refused(set_model, a, versions=LISTED, remedy="release", release_ids=[floor.r52, floor.r56], fix_release=floor.r56, **base) == {
        "fix_release": advisories.FIX_LISTED}
    assert refused(set_model, a, device_model="nope", versions=ALL, remedy="mitigations", by=None, today=TODAY) == {
        "device_model": advisories.CHOOSE_MODEL}
    vent_release = release(vent_model, "1.0")
    profile = release(floor.model, "")
    gone = release(floor.model, "4.9 (demo)", removed_on=TODAY)
    words = "Choose a release of the Alaris 8015 PCU on file."
    assert refused(set_model, a, versions=LISTED, remedy="release", release_ids=[vent_release, gone], fix_release=profile, **base) == {
        "release_ids": words, "fix_release": words}
    assert not AdvisoryModel.objects.exists()


def test_set_model_never_reaches_another_facilitys_model(ctx, floor, other_tenant):
    a = record()
    with tenant_context(other_tenant):
        theirs = DeviceModel.objects.create(manufacturer="BD", model="Alaris 8015 PCU", description="Pump", category="Infusion pumps")
        their_release = release(theirs, "5.2 (demo)")
    assert refused(set_model, a, device_model=theirs, versions=ALL, remedy="mitigations", by=None, today=TODAY) == {
        "device_model": advisories.CHOOSE_MODEL}
    assert refused(set_model, a, device_model=floor.model, versions=LISTED, release_ids=[their_release], remedy="mitigations", by=None,
                   today=TODAY) == {"release_ids": "Choose a release of the Alaris 8015 PCU on file."}


def test_set_model_keeps_a_live_decisions_scope(ctx, floor, people):
    a = record()
    set_model(a, device_model=floor.model, versions=LISTED, release_ids=[floor.r52, floor.r54], remedy="release", fix_release=floor.r56,
              confirm=True, by=None, today=TODAY)
    on52 = decision(a, 1, release=floor.r52, device_model=floor.model)
    words = {
        people.manager: f"{on52.number} is decided for 5.2 (demo) under this advisory: end it first.",
        people.it: f"{on52.number} is decided for 5.2 (demo) under this advisory: ending it needs Device security Approve and Equipment View.",
        people.edit_only: advisories.DECISION_UNDER_UNREAD}
    for user, said in words.items():
        assert refused(set_model, a, device_model=floor.model, versions=LISTED, release_ids=[floor.r54], remedy="release",
                       fix_release=floor.r56, by=user, today=TODAY) == {"release_ids": said}
    # 5.2 made the fix no longer reads named either
    assert "release_ids" in refused(set_model, a, device_model=floor.model, versions=LISTED, release_ids=[floor.r54], remedy="release",
                                    fix_release=floor.r52, by=None, today=TODAY)
    # every release names 5.2 still: allowed
    set_model(a, device_model=floor.model, versions=ALL, remedy="no_fix_yet", by=None, today=TODAY)
    # a device's decision: P-4 on 5.6, named under "every release"; listing 5.2 again with 5.6 the fix leaves it without its scope
    own = decision(a, 2, asset=floor.d["P-4"])
    assert refused(set_model, a, device_model=floor.model, versions=LISTED, release_ids=[floor.r52], remedy="release",
                   fix_release=floor.r56, by=None, today=TODAY) == {"release_ids": f"{own.number} is decided for P-4 under this advisory: "
                                                                                   "end it first."}
    DeviceSecurityDecision.objects.filter(pk=own.pk).update(ended_on=TODAY, end_reason="no_longer_needed")
    set_model(a, device_model=floor.model, versions=LISTED, release_ids=[floor.r52], remedy="release", fix_release=floor.r56, by=None,
              today=TODAY)


def test_models_change_only_on_an_open_advisory(ctx, floor, people):
    a = record()
    row = model_row(a, floor.model, versions=ALL, remedy=AdvisoryRemedy.NO_FIX_YET)
    for tag in ("P-1", "P-2", "P-3", "P-4", "P-5", "T-1"):
        decision_for(a, floor, tag)
    close(a, by=people.manager, today=TODAY)
    reopen_words = f"Reopen {a.number} to change the models it names."
    assert refused(set_model, a, device_model=floor.model, versions=ALL, remedy="mitigations", by=None, today=TODAY) == {
        "advisory": reopen_words}
    assert refused(remove_model, row, by=None, today=TODAY) == {"advisory": reopen_words}
    assert refused(confirm_models, a, by=None, today=TODAY) == {"advisory": reopen_words}
    assert advisories.set_model_refusal(a) == reopen_words


def test_remove_model(ctx, floor, people):
    a = record()
    row = model_row(a, floor.model, releases=[floor.r52], fix=floor.r56)
    on52 = decision(a, 1, release=floor.r52, device_model=floor.model)
    assert refused(remove_model, row, by=people.it, today=TODAY) == {
        "device_model": f"{on52.number} is decided for 5.2 (demo) under this advisory: ending it needs Device security Approve and "
                        "Equipment View."}
    DeviceSecurityDecision.objects.filter(pk=on52.pk).update(ended_on=TODAY, end_reason="no_longer_needed")
    own = decision(a, 2, asset=floor.d["P-3"])  # a device of the model
    assert refused(remove_model, str(row.pk), by=None, today=TODAY) == {
        "device_model": f"{own.number} is decided for P-3 under this advisory: end it first."}
    DeviceSecurityDecision.objects.filter(pk=own.pk).update(ended_on=TODAY, end_reason="no_longer_needed")
    listed = row.releases.get()
    remove_model(row, by=people.tech, today=TODAY)
    assert not AdvisoryModel.objects.exists() and not AdvisoryRelease.objects.exists()
    assert AdvisoryRelease.history.filter(id=listed.pk, history_type="-").get().history_change_reason == "Model removed"
    assert AdvisoryModel.history.filter(id=row.pk, history_type="-").get().history_user == people.tech
    a.refresh_from_db()
    assert (a.models_confirmed_on, a.changes) == (None, 1) and reasons_of(a)[-1] == "Model removed"
    assert refused(remove_model, row, by=None, today=TODAY) == {"device_model": f"This model is no longer named on {a.number}."}
    assert advisories.model_removal_refusal(row, user=None) == f"This model is no longer named on {a.number}."
    assert refused(remove_model, "nope", by=None, today=TODAY) == {"device_model": advisories.CHOOSE_ROW}


def test_confirm_models(ctx, floor, make_user):
    kim = make_user("director")
    a = record()
    assert refused(confirm_models, a, by=kim, today=TODAY) == {"advisory": advisories.NAME_A_MODEL}
    model_row(a, floor.model, versions=ALL, remedy=AdvisoryRemedy.MITIGATIONS, confirm=False)
    confirm_models(a, by=kim, today=TODAY - DAY)
    assert (a.models_confirmed_on, a.models_confirmed_by) == (TODAY - DAY, kim) and reasons_of(a)[-1] == "Models confirmed"
    confirm_models(a, by=None, today=TODAY)
    assert a.models_confirmed_on == TODAY - DAY and reasons_of(a).count("Models confirmed") == 1  # already confirmed: nothing saved


# --- not applicable --------------------------------------------------------------------------------------------------------------------

def test_every_not_applicable_reason_is_checked_against_the_records(ctx, floor, vent_model, people):
    a = record()
    assert advisories.not_applicable_refusals(a, TODAY) == {
        NO_MODEL_HERE: None,
        NONE_HERE: "Not marked: name its models first, or choose 'It names no model in this facility's catalog'.",
        RELEASES_NOT_HERE: "Not marked: name its models first, or choose 'It names no model in this facility's catalog'."}
    model_row(a, floor.model, releases=[floor.r52], fix=floor.r56, confirm=False)
    got = advisories.not_applicable_refusals(a, TODAY)
    assert got == {NO_MODEL_HERE: "Not marked: it names the Alaris 8015 PCU, a model in this facility's catalog.",
                   NONE_HERE: "Not marked: 6 Alaris 8015 PCU devices are recorded on site.",
                   RELEASES_NOT_HERE: "Not marked: 3 devices are recorded on a release it names and 1 has no release recorded."}
    for reason, words in got.items():
        assert refused(mark_not_applicable, a, reason=reason, by=people.tech, today=TODAY) == {"not_applicable_reason": words}
    vent = Asset.objects.create(tag="V-1", device_model=vent_model, department=floor.dept)
    model_row(a, vent_model, versions=NOT_STATED, remedy=AdvisoryRemedy.NO_FIX_YET, confirm=False)
    got = advisories.not_applicable_refusals(a, TODAY)
    assert got[NO_MODEL_HERE] == "Not marked: it names the Alaris 8015 PCU and the Hamilton-G5, models in this facility's catalog."
    assert got[NONE_HERE] == "Not marked: 6 Alaris 8015 PCU devices and 1 Hamilton-G5 device are recorded on site."
    assert got[RELEASES_NOT_HERE] == ("Not marked: 3 devices are recorded on a release it names and 1 has no release recorded; it names "
                                      "the Hamilton-G5 without stating releases, and 1 device is recorded on site.")
    # nobody on a named release: releases_not_here holds
    for tag in ("P-1", "P-2", "T-1"):
        facts(floor.d[tag], floor.r54)
    facts(floor.d["P-5"], floor.r56)
    vent.delete()
    got = advisories.not_applicable_refusals(a, TODAY)
    assert got[RELEASES_NOT_HERE] is None and got[NONE_HERE] == "Not marked: 6 Alaris 8015 PCU devices are recorded on site."
    marked = mark_not_applicable(a, reason=RELEASES_NOT_HERE, by=people.edit_only, today=TODAY)  # Edit, no Equipment View
    assert (marked.status, marked.not_applicable_reason, marked.status_on, marked.status_by) == (
        AdvisoryStatus.NOT_APPLICABLE, RELEASES_NOT_HERE, TODAY, people.edit_only)
    assert (marked.models_confirmed_on, marked.models_confirmed_by) == (TODAY, people.edit_only)  # marking is the confirmation
    assert reasons_of(a)[-1] == "Marked not applicable"
    assert refused(mark_not_applicable, a, reason=NONE_HERE, by=None, today=TODAY) == {
        "advisory": f"{a.number} is already marked not applicable here."}
    assert rules.status_words(marked, devices_visible=False) == (f"Not applicable here ({people.edit_only}, {rules.day_words(TODAY)}): "
                                                                 "No device here is recorded on a release it names")
    # no model named: the confirmation of no model, at Edit
    none = record(reference="DEMO-ADV-0002")
    marked = mark_not_applicable(none, reason=NO_MODEL_HERE, by=people.tech, today=TODAY)
    assert marked.models_confirmed_on == TODAY
    assert refused(mark_not_applicable, a, reason="not_tracked", by=None, today=TODAY) == {"not_applicable_reason": advisories.REASON_WORDS}


def test_not_applicable_is_refused_while_a_decision_is_under_it(ctx, floor, people):
    a = record()
    model_row(a, floor.model, releases=[floor.r52], fix=floor.r56)
    d = decision(a, 1, asset=floor.d["P-1"])
    said = {people.manager: f"{d.number} is decided under this advisory: end it first.",
            people.it: f"{d.number} is decided under this advisory: ending it needs Device security Approve and Equipment View.",
            people.edit_only: advisories.DECISION_UNDER_UNREAD}
    for user, words in said.items():
        assert refused(mark_not_applicable, a, reason=NONE_HERE, by=user, today=TODAY) == {"advisory": words}
        assert advisories.not_applicable_refusal(a, user=user) == words
    shut = record(reference="DEMO-ADV-0002")
    SecurityAdvisory.objects.filter(pk=shut.pk).update(status=AdvisoryStatus.CLOSED, status_on=TODAY)
    shut.refresh_from_db()
    assert refused(mark_not_applicable, shut, reason=NO_MODEL_HERE, by=None, today=TODAY) == {
        "advisory": f"{shut.number} is closed: reopen it first."}


# --- close -----------------------------------------------------------------------------------------------------------------------------

def test_close_needs_the_lines_answered(ctx, floor, people):
    a = record()
    assert refused(close, a, by=people.manager, today=TODAY) == {"advisory": advisories.NAME_A_MODEL}
    row = model_row(a, floor.model, releases=[floor.r52], fix=floor.r56, confirm=False)
    assert refused(close, a, by=people.manager, today=TODAY) == {"advisory": advisories.MODELS_TO_CONFIRM}
    confirm_models(a, by=None, today=TODAY)
    words = refused(close, a, by=people.manager, today=TODAY)["advisory"]
    assert words == ("Not closed: 4 devices on a release it names, or with no release recorded, have no response recorded (P-1, P-2, P-5, "
                     "T-1). Open updates to what the advisory offers, or record a decision for them.")
    assert advisories.close_refusal(a, TODAY, user=people.view_only) == (
        "Not closed: 4 devices on a release it names, or with no release recorded, have no response recorded. Open updates to what the "
        "advisory offers, or record a decision for them.")  # counts only for a reader without Equipment View
    wo1, _u = update_wo(floor.d["P-1"], floor.r56)
    wo2, _u = update_wo(floor.d["P-2"], floor.r56)
    WorkOrder.objects.filter(pk=wo2.pk).update(status=WoStatus.COMPLETED, completed_on=TODAY)
    past = decision(a, 1, asset=floor.d["P-5"], decided_on=TODAY - 40 * DAY, review_by=TODAY - DAY)
    assert refused(close, a, by=people.manager, today=TODAY)["advisory"] == (
        "Not closed: 1 device on a release it names, or with no release recorded, has no response recorded (T-1). Open updates to what "
        "the advisory offers, or record a decision for it. 1 device is under a decision past its review date (P-5): review and renew "
        "it. 1 device has an update done with the version not confirmed (P-2): confirm the version on its work order, or record a "
        "decision for it. 1 device has its update open (P-1): close the advisory when it is done, or record a decision for it.")
    DeviceSecurityDecision.objects.filter(pk=past.pk).update(review_by=TODAY + 30 * DAY)
    for tag in ("P-1", "P-2", "T-1"):
        decision_for(a, floor, tag)  # interim decisions beside the updates
    closed = close(a, by=people.manager, today=TODAY)
    assert (closed.status, closed.status_on, closed.status_by) == (AdvisoryStatus.CLOSED, TODAY, people.manager)
    assert reasons_of(a)[-1] == "Closed"
    assert refused(close, a, by=people.manager, today=TODAY) == {"advisory": f"{a.number} is already closed."}
    assert advisories.close_refusal(a, TODAY) is None  # nothing named since
    assert row.pk


def test_close_names_ten_devices_then_counts(ctx, floor, people):
    a = record()
    model_row(a, floor.model, versions=ALL, remedy=AdvisoryRemedy.NO_FIX_YET)
    for i in range(10):
        Asset.objects.create(tag=f"Q-{i:02d}", device_model=floor.model, department=floor.dept)
    words = advisories.close_refusal(a, TODAY, user=people.manager)
    assert words == ("Not closed: 16 devices on a release it names, or with no release recorded, have no response recorded (P-1, P-2, "
                     "P-3, P-4, P-5, Q-00, Q-01, Q-02, Q-03, Q-04 and 6 more). The advisory names no fix: record a decision for them "
                     "(the model's, or each temporary device's).")


def test_close_reads_patches_by_the_list_the_advisory_names(ctx, floor, people):
    """critique40_rules B1: patches on another list block Close in words; on the list named they answer."""
    a = record(received_on=TODAY - 10 * DAY)
    model_row(a, floor.model, versions=ALL, remedy=AdvisoryRemedy.PATCHES, patches_reference="DEMO-PATCH-2026-10")
    for tag in ("P-1", "P-2", "P-3", "P-4", "P-5", "T-1"):
        SoftwareUpdate.objects.create(asset=floor.d[tag], outcome=UpdateOutcome.PATCHES, reference="DEMO-PATCH-2026-10",
                                      applied_on=TODAY - 30 * DAY, installed_by=InstalledBy.MANUFACTURER)
    other = SoftwareUpdate.objects.create(asset=floor.d["P-1"], outcome=UpdateOutcome.PATCHES, reference="DEMO-PATCH-2026-09",
                                          applied_on=TODAY - 2 * DAY, installed_by=InstalledBy.MANUFACTURER)
    assert advisories.close_refusal(a, TODAY) is None  # P-1 has the list named too: patches recorded first
    SoftwareUpdate.objects.filter(asset=floor.d["P-2"], reference="DEMO-PATCH-2026-10").update(reference="DEMO-PATCH-2026-09",
                                                                                                applied_on=TODAY - DAY)
    assert refused(close, a, by=people.manager, today=TODAY)["advisory"] == (
        "Not closed: 1 device has patches recorded that name another list than the one the advisory names (DEMO-PATCH-2026-10) (P-2). "
        "Record the list it names once it is applied, record a revision if the manufacturer now names a later list, or record a decision "
        "for it.")
    assert other.pk


def test_closing_needs_approve_and_equipment_view(ctx, floor, people):
    a = record()
    model_row(a, floor.model, versions=ALL, remedy=AdvisoryRemedy.NO_FIX_YET)
    for user in (people.tech, people.it, people.approve_only, people.viewer, *people.scoped):
        with pytest.raises(PermissionDenied, match=re.escape(advisories.CLOSE_PERMISSION)):
            close(a, by=user, today=TODAY)


# --- reopen, entered in error ---------------------------------------------------------------------------------------------------------

def test_reopen(ctx, floor, people):
    na = record()
    mark_not_applicable(na, reason=NO_MODEL_HERE, by=people.tech, today=TODAY)
    assert advisories.reopen_refusal(na, user=people.view_only) == advisories.REOPEN_PERMISSION
    opened = reopen(na, by=people.edit_only, today=TODAY)  # Edit reopens from not applicable
    assert (opened.status, opened.not_applicable_reason, opened.status_on, opened.status_by) == (AdvisoryStatus.OPEN, "", None, None)
    assert opened.models_confirmed_on == TODAY and reasons_of(na)[-1] == "Reopened"  # the confirmation kept
    assert refused(reopen, na, by=None, today=TODAY) == {"advisory": f"{na.number} is open."}
    shut = record(reference="DEMO-ADV-0002")
    model_row(shut, floor.model, versions=ALL, remedy=AdvisoryRemedy.NO_FIX_YET)
    for tag in ("P-1", "P-2", "P-3", "P-4", "P-5", "T-1"):
        decision_for(shut, floor, tag)
    close(shut, by=people.manager, today=TODAY)
    assert advisories.reopen_refusal(shut, user=people.tech) == advisories.CORRECT_PERMISSION
    for user in (people.tech, people.it, people.edit_only):
        with pytest.raises(PermissionDenied, match=re.escape(advisories.CORRECT_PERMISSION)):
            reopen(shut, by=user, today=TODAY)
    for user in (people.view_only, *people.scoped):
        with pytest.raises(PermissionDenied):
            reopen(shut, by=user, today=TODAY)
    assert reopen(shut, by=people.approve_only, today=TODAY).status == AdvisoryStatus.OPEN  # Approve, without Equipment View


def test_entered_in_error(ctx, floor, people):
    a = record(cves="CVE-2026-1111")
    add_source(a, source=PORTAL, reference="DEMO-ADV-0001-N", by=None, today=TODAY)
    model_row(a, floor.model, releases=[floor.r52], fix=floor.r56)
    d = decision(a, 1, asset=floor.d["P-1"])
    for user in (people.tech, people.it, people.viewer, *people.scoped):
        with pytest.raises(PermissionDenied, match=re.escape(advisories.CORRECT_PERMISSION)):
            mark_entered_in_error(a, by=user, today=TODAY)
    assert refused(mark_entered_in_error, a, by=people.manager, today=TODAY) == {
        "advisory": f"{d.number} is decided under this advisory: end it first."}
    assert refused(mark_entered_in_error, a, by=people.approve_only, today=TODAY) == {"advisory": advisories.DECISION_UNDER_UNREAD}
    assert advisories.entered_in_error_refusal(a, user=people.viewer) == (
        f"{d.number} is decided under this advisory: ending it needs Device security Approve and Equipment View.")
    DeviceSecurityDecision.objects.filter(pk=d.pk).update(ended_on=TODAY, end_reason="no_longer_needed")
    histories = AdvisorySource.history.count()
    marked = mark_entered_in_error(a, by=people.approve_only, today=TODAY)
    assert (marked.removed_on, marked.removed_by) == (TODAY, people.approve_only) and reasons_of(a)[-1] == "Entered in error"
    assert list(AdvisorySource.objects.filter(advisory=a).values_list("advisory_removed", "removed_on")) == [(True, None), (True, None)]
    assert AdvisorySource.history.count() == histories  # one update, no history row (hidden)
    assert refused(mark_entered_in_error, a, by=None, today=TODAY) == {"advisory": f"{a.number} was entered in error."}
    assert refused(close, a, by=None, today=TODAY) == {"advisory": f"{a.number} was entered in error."}
    assert refused(reopen, a, by=None, today=TODAY) == {"advisory": f"{a.number} was entered in error."}
    assert refused(set_model, a, device_model=floor.model, versions=ALL, remedy="mitigations", by=None, today=TODAY) == {
        "advisory": f"{a.number} was entered in error."}
    assert refused(record_revision, a.sources.first(), revision="2", by=None, today=TODAY) == {
        "advisory": f"{a.number} was entered in error."}
    assert record(reference="DEMO-ADV-0001-N", source=PORTAL, cves="CVE-2026-1111").number == f"AD-{YY}-0002"


# --- levels ---------------------------------------------------------------------------------------------------------------------------

def test_recording_needs_device_security_edit(ctx, floor, people):
    a = record()
    src = a.sources.get()
    calls = {
        "record": lambda u: record(reference="DEMO-ADV-0009", by=u),
        "update": lambda u: update_advisory(a, cves="CVE-2026-1111", by=u, today=TODAY),
        "add_source": lambda u: add_source(a, source=OTHER, by=u, today=TODAY),
        "update_source": lambda u: update_source(src, source=BULLETIN, reference="DEMO-ADV-0001", by=u, today=TODAY),
        "revision": lambda u: record_revision(src, revision="2", by=u, today=TODAY),
        "remove_source": lambda u: remove_source(src, by=u, today=TODAY),
        "set_model": lambda u: set_model(a, device_model=floor.model, versions=ALL, remedy="mitigations", by=u, today=TODAY),
        "confirm": lambda u: confirm_models(a, by=u, today=TODAY),
        "not_applicable": lambda u: mark_not_applicable(a, reason=NONE_HERE, by=u, today=TODAY),
    }
    for user in (people.view_only, people.viewer, *people.scoped):
        for name, call in calls.items():
            with pytest.raises(PermissionDenied):
                call(user)
    with pytest.raises(PermissionDenied, match=re.escape(advisories.RECORD_PERMISSION)):
        calls["record"](people.viewer)
    # Edit with or without Equipment View records every one of them
    calls["record"](people.edit_only)
    calls["update"](people.it)
    calls["add_source"](people.edit_only)
    calls["revision"](people.edit_only)
    calls["remove_source"](people.it)
    calls["set_model"](people.edit_only)
    calls["confirm"](people.edit_only)
    assert reasons_of(a)[0] == "Recorded"


# --- the lock spy, conflicts, and the lock-free readers --------------------------------------------------------------------------------

@pytest.fixture
def spy(monkeypatch):
    calls: list = []
    releases: list = []

    def wrap(module, name, label):
        real = getattr(module, name)

        def spied(*args, **kwargs):
            calls.append(label)
            if label == "release":
                releases.append(args[0])
            return real(*args, **kwargs)

        monkeypatch.setattr(module, name, spied)

    wrap(services, "lock_register", "register")
    wrap(services, "_locked_advisory", "advisory")
    wrap(services, "_locked_release", "release")

    def reset():
        calls.clear()
        releases.clear()

    return SimpleNamespace(calls=calls, releases=releases, reset=reset)


def test_every_writer_takes_its_locks_in_the_contracts_order(ctx, floor, people, spy):
    a = record()
    assert spy.calls == ["register"]
    for call in (lambda: update_advisory(a, cves="CVE-2026-1111", by=None, today=TODAY),
                 lambda: add_source(a, source=OTHER, reference="DEMO-ADV-0001-N", by=None, today=TODAY),
                 lambda: update_source(a.sources.first(), source=BULLETIN, reference="DEMO-ADV-0001", by=None, today=TODAY),
                 lambda: record_revision(a.sources.first(), revision="2", by=None, today=TODAY),
                 lambda: remove_source(a.sources.last(), by=None, today=TODAY)):
        spy.reset()
        call()
        assert spy.calls == ["register", "advisory"]
    spy.reset()
    set_model(a, device_model=floor.model, versions=LISTED, release_ids=[floor.r54, floor.r52], remedy="release", fix_release=floor.r56,
              by=None, today=TODAY)
    assert spy.calls == ["register", "advisory", "release", "release", "release"]
    assert spy.releases == sorted([floor.r52.pk, floor.r54.pk, floor.r56.pk])  # in pk order, whatever the order given
    for call in (lambda: confirm_models(a, by=None, today=TODAY),
                 lambda: remove_model(a.model_rows.get(), by=None, today=TODAY)):
        spy.reset()
        call()
        assert spy.calls == ["register", "advisory"]
    na = record(reference="DEMO-ADV-0002")
    for call in (lambda: mark_not_applicable(na, reason=NO_MODEL_HERE, by=None, today=TODAY),
                 lambda: reopen(na, by=None, today=TODAY),
                 lambda: mark_entered_in_error(na, by=None, today=TODAY)):
        spy.reset()
        call()
        assert spy.calls == ["register", "advisory"]
    shut = record(reference="DEMO-ADV-0003")
    model_row(shut, floor.model, versions=ALL, remedy=AdvisoryRemedy.NO_FIX_YET)
    for tag in ("P-1", "P-2", "P-3", "P-4", "P-5", "T-1"):
        decision_for(shut, floor, tag)
    spy.reset()
    close(shut, by=None, today=TODAY)
    assert spy.calls == ["register", "advisory"]


class _Deadlock(Exception):
    sqlstate = "40P01"


def test_a_lock_conflict_or_a_deferred_key_answers_race_in_words(ctx, floor, monkeypatch):
    a = record()

    def deadlocked(*args, **kwargs):
        raise OperationalError("deadlock detected") from _Deadlock()

    real = services._locked_advisory
    monkeypatch.setattr(services, "_locked_advisory", deadlocked)
    assert refused(close, a, by=None, today=TODAY) == {"advisory": advisories.RACE}
    assert refused(set_model, a, device_model=floor.model, versions=ALL, remedy="mitigations", by=None, today=TODAY) == {
        "device_model": advisories.RACE}
    monkeypatch.setattr(services, "_locked_advisory", real)
    real_save = services._save

    def deferred(obj, by, reason):  # a model deleted meanwhile, read at COMMIT
        if isinstance(obj, AdvisoryModel):
            raise IntegrityError("insert or update on table violates foreign key constraint")
        return real_save(obj, by, reason)

    monkeypatch.setattr(services, "_save", deferred)
    assert refused(set_model, a, device_model=floor.model, versions=ALL, remedy="mitigations", by=None, today=TODAY) == {
        "device_model": advisories.RACE}
    assert not AdvisoryModel.objects.exists()


def test_the_readers_take_no_lock_and_say_what_their_writers_say(ctx, floor, people, monkeypatch):
    from django.db.models.query import QuerySet

    a = record()
    row = model_row(a, floor.model, releases=[floor.r52], fix=floor.r56)
    d = decision(a, 1, release=floor.r52, device_model=floor.model)
    lone = record(reference="DEMO-ADV-0002")
    shut = record(reference="DEMO-ADV-0003")
    SecurityAdvisory.objects.filter(pk=shut.pk).update(status=AdvisoryStatus.CLOSED, status_on=TODAY)
    shut.refresh_from_db()
    locked = []
    real = QuerySet.select_for_update
    monkeypatch.setattr(QuerySet, "select_for_update", lambda self, *x, **k: locked.append(1) or real(self, *x, **k))
    read = {
        "close": advisories.close_refusal(a, TODAY, user=people.manager),
        "not_applicable": advisories.not_applicable_refusals(a, TODAY),
        "marking": advisories.not_applicable_refusal(a, user=people.it),
        "reopen": advisories.reopen_refusal(shut, user=people.tech),
        "in_error": advisories.entered_in_error_refusal(a, user=people.manager),
        "model": advisories.model_removal_refusal(row, user=people.manager),
        "source": advisories.source_removal_refusal(lone.sources.get()),
        "set_model": advisories.set_model_refusal(shut),
        "overlaps": advisories.overlaps(["CVE-2026-1111"], source=OTHER, reference="DEMO-ADV-0001"),
    }
    assert locked == []
    monkeypatch.setattr(QuerySet, "select_for_update", real)
    assert refused(close, a, by=people.manager, today=TODAY) == {"advisory": read["close"]}
    assert refused(mark_not_applicable, a, reason=NONE_HERE, by=people.it, today=TODAY) == {"advisory": read["marking"]}
    with pytest.raises(PermissionDenied, match=re.escape(read["reopen"])):
        reopen(shut, by=people.tech, today=TODAY)
    assert refused(mark_entered_in_error, a, by=people.manager, today=TODAY) == {"advisory": read["in_error"]}
    assert refused(remove_model, row, by=people.manager, today=TODAY) == {"device_model": read["model"]}
    assert refused(remove_source, lone.sources.get(), by=None, today=TODAY) == {"source": read["source"]}
    assert refused(set_model, shut, device_model=floor.model, versions=ALL, remedy="mitigations", by=None, today=TODAY) == {
        "advisory": read["set_model"]}
    DeviceSecurityDecision.objects.filter(pk=d.pk).update(ended_on=TODAY, end_reason="no_longer_needed")
    for reason, words in advisories.not_applicable_refusals(a, TODAY).items():
        assert refused(mark_not_applicable, a, reason=reason, by=people.it, today=TODAY) == {"not_applicable_reason": words}
    assert read["overlaps"] == {a.number: ["DEMO-ADV-0001"]}


# --- the release refusals ---------------------------------------------------------------------------------------------------------------

def test_a_release_an_advisory_names_is_not_entered_in_error(ctx, floor, people):
    lone, fix = release(floor.model, "4.0 (demo)"), release(floor.model, "6.0 (demo)")
    a = record()
    model_row(a, floor.model, releases=[lone], fix=fix)
    for r in (lone, fix):
        assert services.removal_refusal(r, by=people.tech) == f"{a.number} names this release: change it there first."
        assert services.removal_refusal(r, by=custom_user_no_view(people)) == "A security advisory names this release."
        assert refused(services.remove_release, r, by=people.tech) == {"release": f"{a.number} names this release: change it there first."}
    SecurityAdvisory.objects.filter(pk=a.pk).update(status=AdvisoryStatus.NOT_APPLICABLE, not_applicable_reason=NONE_HERE, status_on=TODAY)
    assert services.removal_refusal(lone) == f"{a.number} names this release: change it there first."  # whatever its status (locks S4)
    SecurityAdvisory.objects.filter(pk=a.pk).update(removed_on=TODAY)
    assert services.removal_refusal(lone) == "" and services.removal_refusal(fix) == ""  # entered in error: never stops it
    services.remove_release(lone, by=None)


def custom_user_no_view(people):
    """A Work orders reader with no Device security level (a removal refusal read by someone who does not read advisories)."""
    tenant = people.manager.tenant
    found = User.objects.filter(username="no-cyber@riverside.example").first()
    return found or custom_user(tenant, "no-cyber", {Module.WORKORDERS: Level.VIEW})


def test_a_version_an_advisory_names_keeps_its_spelling(ctx, floor, people):
    lone, fix = release(floor.model, "4.0 (demo)"), release(floor.model, "6.0 (demo)")
    a = record()
    model_row(a, floor.model, releases=[lone], fix=fix)
    SecurityAdvisory.objects.filter(pk=a.pk).update(removed_on=TODAY)  # any status, even entered in error
    assert refused(services.update_release, lone, version="4.0.1 (demo)", by=people.tech) == {
        "version": f"{a.number} names this version: add the new version instead."}
    assert refused(services.update_release, fix, version="6.0.1 (demo)", by=None) == {
        "version": f"{a.number} names this version: add the new version instead."}  # as the fix
    with pytest.raises(PermissionDenied):  # only Edit changes a version, and Edit reads advisories: the unread words are the readers'
        services.update_release(fix, version="6.0.1 (demo)", by=custom_user_no_view(people))
    services.update_release(lone, version="V4.0 (demo)", by=people.tech)  # the same version, spelled anew: allowed (version_key)
    other = release(floor.model, "7.0 (demo)")
    services.update_release(other, version="7.0.1 (demo)", by=people.tech)


# --- history ---------------------------------------------------------------------------------------------------------------------------

def test_the_history_reads_in_words_with_ad_numbers_only(ctx, floor, make_user):
    kim = make_user("director")
    a = record(cves="CVE-2026-1111", by=kim)
    src = a.sources.get()
    record_revision(src, revision="UpdateA", updated_on=TODAY, last_change=["products_changed", "fix_available"], by=kim, today=TODAY)
    row = set_model(a, device_model=floor.model, versions=LISTED, release_ids=[floor.r52], remedy="release", fix_release=floor.r56,
                    confirm=True, by=kim, today=TODAY)
    related = {"advisory_sources": {"advisory_id": a.pk}, "advisory_models": {"advisory_id": a.pk},
               "advisory_releases": {"advisory_id": a.pk}}
    entries, _next = record_history(SecurityAdvisory.objects.get(pk=a.pk), related)
    assert {e.area for e in entries} == {"security_advisories", "advisory_sources", "advisory_models", "advisory_releases"}
    revision = next(e for e in entries if e.area == "advisory_sources" and e.reason == "Revision recorded")
    assert {c.field: c.after for c in revision.changes}["What changed"] == "Products or releases changed; The advisory now names a fix"
    added = next(e for e in entries if e.area == "advisory_models")
    changes = {c.field: c.after for c in added.changes}
    assert changes["What the advisory offers"] == "A release from the manufacturer"
    assert changes["The release the advisory names as the fix"].endswith("5.6 (demo)")
    assert "Seen release ids" not in changes and added.record == f"{a.number} · {floor.model}"
    first = next(e for e in entries if e.area == "security_advisories" and e.action == "added")
    assert {c.field: c.after for c in first.changes}["CVE IDs"] == "CVE-2026-1111" and "Number" not in {c.field for c in first.changes}
    allowed = {"Recorded", "Source added", "Source removed", "Revision recorded", "Edited", "Reopened: a revision changed what it names",
               "Models set", "Model removed", "Models confirmed", "Marked not applicable", "Closed", "Reopened", "Entered in error"}
    assert {e.reason for e in entries} <= allowed
    for e in entries:
        assert not never(" ".join([e.reason, *(f"{c.field} {c.before} {c.after}" for c in e.changes)]))
    assert row.pk


def test_the_module_says_no_never_word():
    texts = [v for k, v in vars(advisories).items() if k.isupper() and isinstance(v, str)]
    texts += [v for k, v in vars(services).items() if k.startswith("ADVISORY_") and isinstance(v, str)]
    assert len(texts) > 70 and [t for t in texts if never(t)] == []
    assert not [t for t in texts if re.search(r"CVE-\d{4}-\d", t)]  # never a concrete CVE ID outside tests (critique40_rules N3)


def test_an_advisory_of_another_facility_is_never_reached(ctx, other_tenant):
    with tenant_context(other_tenant):
        dept = Department.objects.create(name="Their ICU")
        theirs = make_advisory(number=f"AD-{YY}-0001")
        assert dept.pk
    assert refused(close, theirs, by=None, today=TODAY) == {"advisory": services.CHOOSE_ADVISORY}
    assert refused(confirm_models, theirs.number, by=None, today=TODAY) == {"advisory": services.CHOOSE_ADVISORY}
    # unscoped: the other facility's row, as a caller holding a foreign copy would pass it
    assert refused(remove_source, AdvisorySource.unscoped.get(advisory_id=theirs.pk), by=None, today=TODAY) == {
        "advisory": services.CHOOSE_ADVISORY}


def test_a_writer_inside_an_outer_transaction_keeps_it(ctx):
    with transaction.atomic():
        a = record()
        with pytest.raises(ValidationError):
            remove_source(a.sources.get(), by=None, today=TODAY)
        assert SecurityAdvisory.objects.filter(pk=a.pk).exists()  # the refusal rolled back only its own savepoint
