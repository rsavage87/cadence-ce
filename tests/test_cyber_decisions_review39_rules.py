"""
Slice 39 review fixes (the rules, the decision service, and the register refusals), on SQLite and PostgreSQL:

- F0, F9: a reason ended on a device the decision governs and that it does not name is a Check only from a decidable need (an extended
  one, the OS ended while the manufacturer's updates continue, names no reason a decision could), and its words say what is known, never
  "since this decision" (an end entered later but dated before the decision; a device joining long past its support).
- F1: governed_ids holds only the devices the decision governed when recorded: one under a more specific live decision then reads
  "joined since" once that decision ends.
- F2: Needs attention's row for devices with no release recorded links to the model's panel narrowed to them (norelease).
- F3, F14: the review day's words for a rental, loaner, or demo unit say the rule review_default computes, in each of its branches.
- F5: the references refuse a MAC in its other layouts and an address with a port, a range, or a prefix joined on.
- F7: a release's Edit and Entered in error name a decision's number only to a reader of decisions (the service and both web doors).
- F8: the who sentence says the day decided of the decision, never of the recording (a decision recorded late).
- F10: the decision's "applies to" label holds for an end announced, not yet passed.
- F11: a model whose parts ended (its support continuing) is worded by its own standing, never "security updates ended".
- F12: Record decision's refusals say what is recorded: support not recorded, "as recorded", "both connected and past its support".
- F15: Needs attention offers End, not Review and renew, for a decision to review whose devices no longer need one (and the page's row).
- F20: rules.holds_device_q, the one condition the review headings and the badge count by.
"""
import re
from datetime import date, timedelta
from html import unescape
from types import SimpleNamespace

import pytest
from cyber_fixtures import facts, release
from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.accounts.models import Level, Module, Role, User
from apps.core.history import record_history
from apps.cybersecurity import decisions, filters, rules, services
from apps.cybersecurity.decisions import end_decision, record_decision
from apps.cybersecurity.models import (
    AcceptedBy,
    AppliesTo,
    Connects,
    DecisionResponse,
    DeviceSecurityDecision,
    Network,
    OsFamily,
    Segmented,
)
from apps.equipment.models import Asset, AssetStatus, DeviceModel, Ownership
from apps.pm.dates import add_months

TODAY = date(2026, 10, 21)
DAY = timedelta(days=1)
HX = {"HTTP_HX_REQUEST": "true"}
KEEP_WITH, KEEP_WITHOUT, OUT_OF_USE = (DecisionResponse.KEEP_WITH_CONTROLS, DecisionResponse.KEEP_WITHOUT_ADDED_CONTROLS,
                                       DecisionResponse.TAKE_OUT_OF_USE)
BASE = {"reasons": ["security_updates"], "response": KEEP_WITHOUT, "accepted_by": AcceptedBy.CE_LEADER, "by": None, "today": TODAY}


def never(text: str) -> list:
    return [w for w in rules.DECISION_NEVER_WORDS if re.search(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", text, re.IGNORECASE)]


def record(**fields) -> DeviceSecurityDecision:
    return record_decision(**{**BASE, **fields})


def refused(fn=None, *args, **kwargs) -> dict:
    """The refusal's words by field: `fn(*args, **kwargs)`, or with no `fn` record() (BASE's fields under `kwargs`)."""
    with pytest.raises(ValidationError) as e:
        record(**kwargs) if fn is None else fn(*args, **kwargs)
    out = {k: v[0] if len(v) == 1 else v for k, v in e.value.message_dict.items()}
    assert not [t for t in out.values() if isinstance(t, str) and never(t)], out
    return out


def checks(decision, today=TODAY) -> dict:
    out = {c.key: c for c in rules.decision_checks(DeviceSecurityDecision.objects.get(pk=decision.pk), today)}
    assert not [c.words for c in out.values() if never(c.words)]
    return out


def headings(user=None, today=TODAY) -> dict:
    panel = rules.needs_attention(today, user=user)
    for h in panel.headings:
        assert not never(h.words) and not [r.words for r in h.rows if never(r.words)]
    return {h.key: h for h in panel.headings}


def need_of(asset, today=TODAY):
    return rules.support_need(rules.device_standing(Asset.objects.get(pk=asset.pk), today), today)


def ids(*assets) -> list:
    return [str(a.pk) for a in assets]


def custom_user(tenant, slug, levels):
    role = Role.objects.create(name=slug.title(), slug=slug)
    role.set_levels(levels)
    return User.objects.create_user(username=f"{slug}@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role,
                                    first_name=slug.title(), last_name="Person")


@pytest.fixture
def floor(ctx, dept, pump_model):
    """As the rules' tests: the pump model can connect (wireless, as disclosed), all on a medical device segment; 5.2 (demo) past its
    security updates (Sep 21, 2026), 5.6 (demo) supported (announced to end in 2030). P-1, P-2, P-3 of ours on 5.2, P-4 on 5.6, the
    rental T-1 on 5.2 (due back Nov 10, 2026)."""
    profile = release(pump_model, "", connects=Connects.WIRELESS, segmented=Segmented.ALL)
    r52 = release(pump_model, "5.2 (demo)", security_updates_end_on=TODAY - 30 * DAY)
    r56 = release(pump_model, "5.6 (demo)", security_updates_end_on=date(2030, 3, 31))
    d = {tag: Asset.objects.create(tag=tag, device_model=pump_model, department=dept, serial=f"SN-{tag}") for tag in ("P-1", "P-2", "P-3", "P-4")}
    for tag in ("P-1", "P-2", "P-3"):
        facts(d[tag], r52)
    facts(d["P-4"], r56)
    d["T-1"] = Asset.objects.create(tag="T-1", device_model=pump_model, department=dept, ownership=Ownership.RENTAL,
                                    owner="Summit Medical Rentals", due_back_on=date(2026, 11, 10))
    facts(d["T-1"], r52)
    return SimpleNamespace(profile=profile, r52=r52, r56=r56, model=pump_model, d=d, dept=dept)


# --- F0, F9: a reason ended that the decision does not name ------------------------------------------------------------------------

def test_an_extended_device_under_a_model_decision_is_no_check(floor):
    """F0: the OS ended on P-4's release while the manufacturer's security updates for it continue past 12 months (EXTENDED: information,
    no reason a decision may name). A model's decision naming the security updates (P-1 to P-3) has no Check for it, then or later."""
    ext = release(floor.model, "6.0 (demo)", os_family=OsFamily.EMBEDDED, os_name="Demo embedded OS 7",
                  os_support_ends_on=TODAY - 100 * DAY, security_updates_end_on=date(2028, 3, 31))
    facts(floor.d["P-4"], ext)
    need = need_of(floor.d["P-4"])
    assert (need.key, need.reasons, need.ended) == (rules.EXTENDED, (), ("os_support",))
    d = record(device_model=floor.model)
    assert d.governed_ids == ids(*(floor.d[t] for t in ("P-1", "P-2", "P-3", "P-4")))  # the extended one is governed too
    assert checks(d) == {} and checks(d, TODAY + 30 * DAY) == {}
    assert "checks" not in headings()
    # naming the OS is refused, as before (it is no reason that holds): nothing asks for it any more
    assert refused(device_model=floor.model, supersedes=d, reasons=["security_updates", "os_support"]) == {
        "reasons": "Choose the reasons that hold now: the manufacturer's security updates, support, or parts ended, or their end is "
                   "announced."}


def test_an_end_entered_later_is_worded_as_known_never_since(floor):
    """F9 (1): the OS on 5.2 recorded after the decision, its support ended in 2020: the Check says it has ended and the decision does
    not name it, never that it ended since the decision."""
    d = record(asset=floor.d["P-1"])
    assert checks(d) == {}
    floor.r52.os_family, floor.r52.os_name, floor.r52.os_support_ends_on = OsFamily.WINDOWS, "Windows 7", date(2020, 1, 14)
    floor.r52.save()
    found = checks(d)
    assert set(found) == {rules.CHECK_ALSO_ENDED}
    assert found[rules.CHECK_ALSO_ENDED].words == "The OS vendor's support has ended, and this decision does not name it"
    assert need_of(floor.d["P-1"]).lines[1] == "The OS vendor's published support for Windows 7 ended after Jan 14, 2020"
    on52 = record(release=floor.r52, reasons=["security_updates", "os_support"])  # named: no Check
    assert rules.CHECK_ALSO_ENDED not in checks(on52)


def test_a_device_joining_long_past_its_support_is_worded_as_known(floor, dept):
    """F9 (2): a model's decision, then P-9 added on 4.0, whose OS support ended in 2020: the Check names what holds, never "since"; the
    device itself reads "joined since" (it did)."""
    d = record(device_model=floor.model)
    old = release(floor.model, "4.0 (demo)", os_family=OsFamily.WINDOWS, os_name="Windows 7", os_support_ends_on=date(2020, 1, 14))
    late = Asset.objects.create(tag="P-9", device_model=floor.model, department=dept)
    facts(late, old)
    found = checks(d)
    assert {k: c.words for k, c in found.items()} == {
        rules.CHECK_ALSO_ENDED: "The OS vendor's support has ended, and this decision does not name it",
        rules.CHECK_JOINED: "1 device joined since: P-9"}
    assert not [w for w in rules.ALSO_ENDED.values() if "since" in w]
    assert rules.ALSO_ENDED[rules.SECURITY_UPDATES] == ("The manufacturer's security updates, support, or parts have ended, and this "
                                                        "decision does not name them")


# --- F1: the devices a decision governed when recorded ------------------------------------------------------------------------------

def test_a_device_under_a_more_specific_decision_joins_when_that_one_ends(floor):
    p1, p2, p3, p4 = (floor.d[t] for t in ("P-1", "P-2", "P-3", "P-4"))
    own = record(asset=p1)
    on52 = record(release=floor.r52)
    assert on52.governed_ids == ids(p2, p3)  # P-1 is its own decision's
    assert [(dd.asset.tag, dd.governed) for dd in rules.decision_devices(on52, TODAY)] == [("P-1", False), ("P-2", True), ("P-3", True)]
    assert rules.CHECK_JOINED not in checks(on52)
    end_decision(own, by=None, today=TODAY)
    assert checks(on52)[rules.CHECK_JOINED].words == "1 device joined since: P-1"
    assert [r.words for r in headings()["checks"].rows] == [f"{on52.number} · 1 device joined since: P-1"]
    # a model's decision: the devices on a release with its own decision are that one's, until it ends
    whole = record(device_model=floor.model)
    assert whole.governed_ids == ids(p4)
    end_decision(on52, by=None, today=TODAY)
    assert checks(whole)[rules.CHECK_JOINED].words == "3 devices joined since: P-1, P-2, P-3"
    # renewed, the new decision governs them all (the one it supersedes never counts)
    renewed = record(device_model=floor.model, supersedes=whole)
    assert renewed.governed_ids == ids(p1, p2, p3, p4) and rules.CHECK_JOINED not in checks(renewed)
    # a device's own decision governs it whatever is live around it
    assert record(asset=p2).governed_ids == ids(p2)


def test_renewing_a_release_decision_keeps_the_devices_under_their_own(floor):
    p1, p2, p3 = (floor.d[t] for t in ("P-1", "P-2", "P-3"))
    on52 = record(release=floor.r52)
    assert on52.governed_ids == ids(p1, p2, p3)
    record(asset=p3)
    renewed = record(release=floor.r52, supersedes=on52)
    assert renewed.governed_ids == ids(p1, p2)


# --- F2: the row for devices with no release recorded ----------------------------------------------------------------------------

def test_the_no_release_row_links_to_the_devices_with_no_release(floor):
    floor.profile.security_updates_end_on = TODAY - 30 * DAY  # the profile's support group: what a device with no release reads
    floor.profile.save()
    facts(floor.d["P-3"], None)
    record(release=floor.r52)  # P-1 and P-2: decided
    rows = headings()["no_decision"].rows
    (row,) = rows
    assert row.words.startswith("Alaris 8015 PCU, release not recorded: 1 device of ours; ") and row.count == 1 and row.tags == ("P-3",)
    assert row.link.query == (("whose", "ours"), ("connected", "either"), ("standing", "updates_ended"), ("norelease", filters.NORELEASE))
    shown = filters.parse(dict(row.link.query))
    read = rules.standings(rules.on_site().filter(device_model=floor.model), TODAY)
    assert [s.asset.tag for s in read.values() if shown.keeps(s)] == ["P-3"]  # the model's panel, as the row counts
    # a release's row: the panel narrowed by its standing would list 5.2's devices too (decided): its one device's Security tab
    # (skeptics T1: the panel lists every release's devices of that standing)
    facts(floor.d["P-3"], floor.r52)
    facts(floor.d["P-4"], release(floor.model, "4.0 (demo)", security_updates_end_on=TODAY - DAY))
    (row,) = headings()["no_decision"].rows
    assert row.tags == ("P-4",) and row.link == rules.Link("web:asset", ("P-4",), (("tab", "security"),))


# --- F3, F14: the review day's words for a temporary device ------------------------------------------------------------------------

KEEP_RULE = ("By default 12 months after the day decided, or the day it is due back when that is after the day decided and comes first; "
             "3 months when no due back day after the day decided is recorded (at most 36 months)")
OUT_RULE = ("By default 3 months after the day decided, or the day it is due back when that is after the day decided and comes first "
            "(at most 12 months)")


@pytest.mark.parametrize("due_back, keep, out", [
    (TODAY + 20 * DAY, TODAY + 20 * DAY, TODAY + 20 * DAY),  # due back first: both
    (TODAY + 180 * DAY, TODAY + 180 * DAY, date(2027, 1, 21)),  # take out of use: its 3 months come first
    (TODAY + 700 * DAY, date(2027, 10, 21), date(2027, 1, 21)),  # keep in use: its 12 months come first
    (TODAY - 5 * DAY, date(2027, 1, 21), date(2027, 1, 21)),  # due back already passed: 3 months
    (None, date(2027, 1, 21), date(2027, 1, 21)),  # none recorded: 3 months
])
def test_the_review_words_for_a_temporary_device_say_the_rule_it_computes(floor, due_back, keep, out):
    from apps.web.forms_cyber_decisions import review_help

    t = floor.d["T-1"]
    t.due_back_on = due_back
    assert (rules.review_default(KEEP_WITH, TODAY, asset=t), rules.review_default(OUT_OF_USE, TODAY, asset=t)) == (keep, out)
    assert rules.review_rule_words(KEEP_WITH, asset=t) == KEEP_RULE and rules.review_rule_words(OUT_OF_USE, asset=t) == OUT_RULE
    # the rule read as written gives the day computed
    for response, months, day in ((KEEP_WITH, 12, keep), (OUT_OF_USE, 3, out)):
        ahead = due_back is not None and due_back > TODAY
        by_words = min(add_months(TODAY, months), due_back) if ahead else add_months(TODAY, min(months, 3))
        assert by_words == day, response
    help_text = review_help(TODAY, asset=t)
    assert f"Keep in use: {rules.day_words(keep)} if decided today (by default 12 months after the day decided, or the day it is due back " \
           "when that is after the day decided and comes first; 3 months when" in help_text
    assert f"Take out of use: {rules.day_words(out)} if decided today (by default 3 months after the day decided, or the day it is due " \
           "back when that is after the day decided and comes first (at most 12 months))." in help_text
    assert not never(help_text)
    # one of ours: as before
    assert rules.review_rule_words(KEEP_WITH, asset=floor.d["P-1"]) == "By default 12 months after the day decided (at most 36 months)"


# --- F5: network identifiers in IT's references ----------------------------------------------------------------------------------

ADDRESS_LIKE = ["001A-2B3C-4D5E", "001a2b-3c4d5e", "00_1A_2B_3C_4D_5E", "001A_2B3C_4D5E", "10.20.30.40#443",
                "10.20.30.40-443", "10.20.30.40_8080", "10.20.30.0-24", "10.20.30.40.443", "10.20.30.40-10.20.30.50", "FW-10.20.30.40",
                "fw-10.20.30.40#8443",
                # a MAC joined on within the token (skeptics S5)
                "EX-001A-2B3C-4D5E", "FW-00-1A-2B-3C-4D-5E",
                # as before
                "001A2B3C4D5E", "192.168.1.10/32", "00-1A-2B-3C-4D-5E", "001A.2B3C.4D5E", "10.20.30.40", "VLAN120", "tcp-443"]
TOKENS = ["CHG0012345", "DEMO-EX-1042", "EX-1042", "RITM0012345", "2026-0042", "INC-20261021-7", "GRC_EX_2026_17", "123456789012",
          "SR-00017", "EX-1.2",
          # a ticket of decimal digits in a MAC's layout: a MAC needs a hex letter there (skeptics S6)
          "0011-2233-4455", "2026-1021-0042", "202610-000042", "2026_1021_0042", "000123-000456", "100200-300400"]


def test_the_references_refuse_every_layout_of_a_network_identifier(floor):
    for text in ADDRESS_LIKE:
        assert decisions.network_identifier(text), text
        errors: dict = {}
        decisions._token(text, "exception_reference", errors)
        assert errors == {"exception_reference": decisions.NOT_AN_ADDRESS}, text
    for text in TOKENS:
        errors = {}
        assert not decisions.network_identifier(text) and decisions._token(text, "exception_reference", errors) == text and errors == {}, text
    assert refused(asset=floor.d["P-1"], response=KEEP_WITH, controls=["recorded_elsewhere", "segmented"],
                   controls_reference="10.20.30.40#443", exception_reference="001A-2B3C-4D5E") == {
        "controls_reference": decisions.NOT_AN_ADDRESS, "exception_reference": decisions.NOT_AN_ADDRESS}
    assert not DeviceSecurityDecision.objects.exists()


# --- F7: a release's refusals name a decision only to its readers ---------------------------------------------------------------------

@pytest.fixture
def lone(floor):
    """4.0 (demo), with a release decision (SD-26-0001) recorded while P-4 ran it, then P-4 moved to 5.6: no device runs it now."""
    lone = release(floor.model, "4.0 (demo)", security_updates_end_on=TODAY - DAY)
    facts(floor.d["P-4"], lone)
    record(release=lone)
    facts(floor.d["P-4"], floor.r56)
    return lone


def test_a_releases_refusals_name_a_decision_only_to_its_readers(tenant, floor, lone):
    itsec = custom_user(tenant, "itsec", {Module.CYBERSECURITY: Level.APPROVE})  # no Equipment View: reads no decision
    editor = custom_user(tenant, "cyber-editor", {Module.CYBERSECURITY: Level.EDIT})
    reader = custom_user(tenant, "cyber-reader", {Module.CYBERSECURITY: Level.EDIT, Module.EQUIPMENT: Level.VIEW})
    unread_live = "A security decision is in force for this release: ending it needs Device security Approve and Equipment View."
    unread_names = "A security decision names this version: add the new version instead."
    assert (services.DECISION_LIVE_UNREAD, services.DECISION_NAMES_UNREAD) == (unread_live, unread_names)
    for user in (itsec, editor):
        assert services.removal_refusal(lone, by=user) == unread_live
        assert refused(services.remove_release, lone, by=user) == {"release": unread_live}
        assert refused(services.update_release, lone, by=user, version="4.1 (demo)") == {"version": unread_names}
    named_live, named_version = "SD-26-0001 is decided for this release: end it first.", "SD-26-0001 names this version: add the new version instead."
    read_live = "SD-26-0001 is decided for this release: ending it needs Device security Approve and Equipment View."  # skeptics S9
    for user in (reader, None):  # a reader of decisions (who may not end one), and the system
        assert services.removal_refusal(lone, by=user) == (read_live if user is reader else named_live)
        assert refused(services.remove_release, lone, by=user) == {"release": read_live if user is reader else named_live}
        assert refused(services.update_release, lone, by=user, version="4.1 (demo)") == {"version": named_version}
    assert services.removal_refusal(lone) == named_live
    end_decision(DeviceSecurityDecision.objects.get(), by=None, today=TODAY)  # ended: still names the version, for anyone
    assert refused(services.update_release, lone, by=editor, version="4.1 (demo)") == {"version": unread_names}


def test_the_release_doors_name_a_decision_only_to_its_readers(client, tenant, floor, lone):
    from apps.web.forms_cybersecurity import release_initial

    itsec = custom_user(tenant, "itsec", {Module.CYBERSECURITY: Level.APPROVE})
    client.force_login(itsec)
    assert client.get("/security/decisions/", **HX).status_code == 403  # reads no decision
    r = client.get(f"/security/releases/{lone.pk}/remove/", **HX)
    body = unescape(r.content.decode())
    assert r.context["blocker"] == services.DECISION_LIVE_UNREAD and "SD-26-0001" not in body
    body = unescape(client.post(f"/security/releases/{lone.pk}/remove/", **HX).content.decode())
    assert services.DECISION_LIVE_UNREAD in body and "SD-26-0001" not in body
    body = unescape(client.post(f"/security/releases/{lone.pk}/edit/", {**release_initial(lone), "version": "4.1 (demo)"}, **HX).content.decode())
    assert services.DECISION_NAMES_UNREAD in body and "SD-26-0001" not in body
    # a reader of decisions reads its number
    client.force_login(custom_user(tenant, "cyber-approver", {Module.CYBERSECURITY: Level.APPROVE, Module.EQUIPMENT: Level.VIEW}))
    r = client.get(f"/security/releases/{lone.pk}/remove/", **HX)
    assert r.context["blocker"] == "SD-26-0001 is decided for this release: end it first."
    body = unescape(client.post(f"/security/releases/{lone.pk}/edit/", {**release_initial(lone), "version": "4.1 (demo)"}, **HX).content.decode())
    assert "SD-26-0001 names this version: add the new version instead." in body


# --- F8: the day decided, said of the decision ---------------------------------------------------------------------------------------

def test_the_who_sentence_says_the_day_decided_of_the_decision(floor, make_user):
    kim = make_user("director")
    late = record(asset=floor.d["P-1"], by=kim, decided_on=date(2026, 9, 1))  # recorded Oct 21, decided Sep 1
    assert rules.who_words(late) == f"Accepted by the CE leader on Sep 1, 2026, as recorded by {kim}; no IT exception number recorded"
    sentence = rules.decision_words(late, TODAY)
    assert f"as recorded by {kim} on" not in sentence and sentence.endswith(
        f"Accepted by the CE leader on Sep 1, 2026, as recorded by {kim}; no IT exception number recorded.")
    out = record(asset=floor.d["P-2"], by=kim, decided_on=date(2026, 9, 1), response=OUT_OF_USE, accepted_by=AcceptedBy.COMMITTEE,
                 exception_reference="DEMO-EX-7")
    assert rules.decision_words(out, TODAY) == (
        "Take out of use, decided Sep 1, 2026; review by Dec 1, 2026. No plan recorded. Decided by a committee, as recorded by "
        f"{kim}; IT exception DEMO-EX-7.")
    assert rules.who_words(out) == f"Decided by a committee on Sep 1, 2026, as recorded by {kim}; IT exception DEMO-EX-7"
    system = record(asset=floor.d["P-3"], decided_on=date(2026, 9, 1))
    assert rules.who_words(system) == "Accepted by the CE leader on Sep 1, 2026; no IT exception number recorded"


# --- F10: what a decision applies to --------------------------------------------------------------------------------------------------

def test_the_applies_to_label_holds_for_an_end_announced(floor):
    soon = release(floor.model, "5.4 (demo)", security_updates_end_on=TODAY + 120 * DAY)
    facts(floor.d["P-4"], soon)
    assert need_of(floor.d["P-4"]).key == rules.ENDING  # nothing has ended yet
    d = record(asset=floor.d["P-4"])
    assert AppliesTo(d.applies_to).label == "Support ended, or its end announced" and d.applies_to == "support_ended"
    entries, _next = record_history(DeviceSecurityDecision.objects.get(pk=d.pk))
    added = next(e for e in entries if e.action == "added")
    assert {c.field: c.after for c in added.changes}["Applies to"] == "Support ended, or its end announced"
    assert not never(AppliesTo.SUPPORT_ENDED.label)


# --- F11: a model whose parts ended -------------------------------------------------------------------------------------------------

def test_a_model_whose_parts_ended_is_worded_by_its_own_standing(floor):
    DeviceModel.objects.filter(pk=floor.model.pk).update(parts_end_on=TODAY - 60 * DAY, support_ends_on=date(2030, 10, 1))
    bare = release(floor.model, "2.0 (demo)")  # no date of its own: the model's support (slice 36)
    for tag in ("P-4", "T-1"):
        facts(floor.d[tag], bare)
    standing = rules.device_standing(Asset.objects.get(pk=floor.d["T-1"].pk), TODAY).security
    assert (standing.key, standing.words) == (rules.MODEL_SUPPORT_ENDED, "Parts no longer available after Aug 22, 2026 (the model's)")
    found = headings()
    (temp,) = found["temporary"].rows
    assert temp.words == ("T-1 (rental, Summit Medical Rentals; due back Nov 10, 2026): parts no longer available after Aug 22, 2026 "
                          "(the model's); its owner installs updates: ask the owner to update it, or return it")
    assert found["no_decision"].words == ("Security updates, manufacturer support or parts, or OS support ended, connected or able to "
                                          "connect: no decision recorded")
    on20 = next(r for r in found["no_decision"].rows if "2.0 (demo)" in r.words)
    assert on20.words == "Alaris 8015 PCU on 2.0 (demo): 1 device of ours; parts no longer available after Aug 22, 2026 (the model's)"
    d = record(asset=floor.d["P-4"])
    assert rules.reason_words(d) == ["The manufacturer's security updates, support, or parts ended, or their end is announced"]
    assert not [w for w in (temp.words, *rules.REASONS.values(), *rules.ATTENTION_HEADINGS.values()) if never(w)]


# --- F12: Record decision's refusals say what is recorded ---------------------------------------------------------------------------

def test_record_decisions_refusals_say_what_is_recorded(floor, dept):
    p7 = Asset.objects.create(tag="P-7", device_model=floor.model, department=dept)  # can connect, as disclosed; nothing else recorded
    nothing = "P-7: its release is not recorded, nor the end of its security updates: record its release (Record facts) first."
    assert decisions.refusal_for(asset=p7, today=TODAY).words == nothing and refused(asset=p7) == {"asset": nothing}
    facts(p7, release(floor.model, "5.8 (demo)"))
    no_date = "P-7: the end of the manufacturer's security updates for 5.8 (demo) is not recorded: record it on the release (Models) first."
    assert decisions.refusal_for(asset=p7, today=TODAY).words == no_date and refused(asset=p7) == {"asset": no_date}
    supported = "P-4 is not past its support as recorded, and no end is announced within 12 months."
    assert decisions.refusal_for(asset=floor.d["P-4"], today=TODAY).words == supported
    # a release whose devices are past support but not connected here
    for tag in ("P-1", "P-2", "P-3"):
        facts(floor.d[tag], floor.r52, network=Network.NOT_CONNECTED)
    none = ("No device of ours on site in this scope is both connected (or able to connect) and past its support or within 12 months "
            "of an announced end, as recorded.")
    assert decisions.NONE_NEEDED == none
    assert decisions.refusal_for(release=floor.r52, today=TODAY).words == none and refused(release=floor.r52) == {"release": none}
    assert not [w for w in (nothing, no_date, supported, none) if never(w)]


# --- F15: a decision to review that no device needs now -----------------------------------------------------------------------------

def test_a_decision_to_review_that_no_device_needs_offers_end(floor, make_user):
    manager = make_user("manager")
    lapsed = record(asset=floor.d["P-1"], decided_on=TODAY - 400 * DAY, review_by=TODAY - 10 * DAY)
    soon = record(asset=floor.d["P-2"], decided_on=TODAY - 300 * DAY, review_by=TODAY + 10 * DAY)
    found = headings(manager)
    (due,), (near,) = found["review_due"].rows, found["review_soon"].rows
    assert (due.record, due.end) == (rules.Link("web:security_decision_new", (), (("supersedes", lapsed.number),)), None)
    assert (near.record, near.end) == (rules.Link("web:security_decision_new", (), (("supersedes", soon.number),)), None)
    # both updated to a supported release: renewing would be refused, End answers them
    for tag in ("P-1", "P-2"):
        facts(floor.d[tag], floor.r56)
    assert decisions.refusal_for(asset=floor.d["P-1"], supersedes=lapsed.number, today=TODAY) is not None
    found = headings(manager)
    (due,), (near,) = found["review_due"].rows, found["review_soon"].rows
    assert (due.record, due.end) == (None, rules.Link("web:security_decision_end", (lapsed.number,)))
    assert (near.record, near.end) == (None, rules.Link("web:security_decision_end", (soon.number,)))
    assert due.words == f"{lapsed.number} · P-1; review due since Oct 11, 2026"  # its words as before
    viewer = custom_user(manager.tenant, "viewer", {Module.CYBERSECURITY: Level.VIEW, Module.EQUIPMENT: Level.VIEW})
    assert headings(viewer)["review_due"].rows[0].end is None  # End only for Approve
    # an end announced within 12 months is decidable: Review and renew again
    facts(floor.d["P-1"], release(floor.model, "5.4 (demo)", security_updates_end_on=TODAY + 100 * DAY))
    (due,) = headings(manager)["review_due"].rows
    assert due.record is not None and due.end is None


def test_the_decisions_tab_offers_end_for_it(client, floor, make_user):
    from django.utils import timezone

    today = timezone.localdate()
    lapsed = record(asset=floor.d["P-1"], decided_on=today - 400 * DAY, review_by=today - 10 * DAY, today=today)
    facts(floor.d["P-1"], floor.r56)
    client.force_login(make_user("manager"))
    r = client.get("/security/decisions/")
    (heading,) = [h for h in r.context["attention"]["headings"] if h["key"] == "review_due"]
    (row,) = heading["rows"]
    assert (row["record_url"], row["end_url"]) == ("", reverse("web:security_decision_end", args=[lapsed.number]))
    assert f'hx-get="{row["end_url"]}"' in r.content.decode()


# --- F20: the condition the review headings and the badge count by -------------------------------------------------------------------

def test_the_review_headings_and_the_badge_count_by_one_condition(floor):
    held = record(asset=floor.d["P-1"], decided_on=TODAY - 400 * DAY, review_by=TODAY - 10 * DAY)
    gone = record(asset=floor.d["P-2"], decided_on=TODAY - 400 * DAY, review_by=TODAY - 10 * DAY)
    Asset.objects.filter(pk=floor.d["P-2"].pk).update(status=AssetStatus.RETIRED)
    past = DeviceSecurityDecision.objects.filter(ended_on__isnull=True, review_by__lt=TODAY)
    assert set(past.values_list("number", flat=True)) == {held.number, gone.number}  # what ?state=review_due lists today
    holding = list(past.filter(rules.holds_device_q()).values_list("number", flat=True))
    found = headings()
    assert holding == [held.number] and found["review_due"].count == 1 and rules.badge(TODAY)["count"] == 1
    assert [r.decision.number for r in found["no_device"].rows] == [gone.number]
