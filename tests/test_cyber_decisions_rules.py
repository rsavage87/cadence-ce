"""
Slice 39 wave 1, the decisions' rules (apps.cybersecurity.rules, build39 sections 2 to 4 and 7), on SQLite and PostgreSQL:

- A device's support need: each standing (security updates ended, the model's support ended, the OS ended), the manufacturer's extended
  support (the OS ended, the release's updates continue: information only), an end announced within 12 months (and one further), not
  connected, connection not recorded, retired.
- The governing decision: the most specific live one (device, release, model), no fallback past a lapsed one, an ended one never, a
  temporary device only by its own; covers only on or before its review day.
- The update responses: an open update to a supported release (never to one past support, never patches, never one whose OS ended),
  the version not confirmed, a done or cancelled one never; the Security tab's lines in order with the decision, or "No decision
  recorded"; who may record, and who sees the work order's link.
- The words: each response's sentence, past review, the plan (the fiscal year read at read time), who (with and without IT's exception
  number, the system), the scope, the state, the review defaults and caps.
- Each Check line, Needs attention's headings and rows (links only as data, Record decision and End only for Approve), the badge, and
  fixed query counts for governing_decisions, update_responses, decision_devices, needs_attention, and badge as devices and decisions grow.
- rules.NEVER_WORDS over every new label, slug, words value, and module-level string (rules' slice 39 names, decisions.py).
"""
import re
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from cyber_fixtures import facts, release, update_wo
from django.utils import timezone

from apps.accounts.models import Level, Module, Role, User
from apps.cybersecurity import decisions, rules
from apps.cybersecurity.models import (
    AcceptedBy,
    AppliesTo,
    Connects,
    DecisionEnd,
    DecisionPlan,
    DecisionResponse,
    DeviceSecurityDecision,
    Network,
    OsFamily,
    Segmented,
    UpdateOutcome,
)
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel, Ownership, RiskClass
from apps.facility.services import get_settings
from apps.workorders import services as wo_services
from apps.workorders.models import WorkOrder, WoStatus

TODAY = date(2026, 10, 21)
DAY = timedelta(days=1)
SEEN: list = []  # every sentence the rules gave in these tests, for the never-words scan
KEEP_WITH, KEEP_WITHOUT, OUT_OF_USE = (DecisionResponse.KEEP_WITH_CONTROLS, DecisionResponse.KEEP_WITHOUT_ADDED_CONTROLS,
                                       DecisionResponse.TAKE_OUT_OF_USE)


def never(text: str) -> list:
    """rules.NEVER_WORDS found in `text`, as whole words in any letter case (none may be)."""
    return [w for w in rules.DECISION_NEVER_WORDS if re.search(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", text, re.IGNORECASE)]


def seen(text: str) -> str:
    SEEN.append(text)
    return text


def decide(n=1, **fields) -> DeviceSecurityDecision:
    """A decision made directly (the rules' tests set up any state; the writers' tests go through decisions.py)."""
    values = {"number": f"SD-26-{n:04d}", "applies_to": AppliesTo.SUPPORT_ENDED, "reasons": ["security_updates"],
              "response": KEEP_WITHOUT, "accepted_by": AcceptedBy.CE_LEADER, "decided_on": TODAY - 10 * DAY,
              "review_by": TODAY + 300 * DAY, **fields}
    return DeviceSecurityDecision.objects.create(**values)


def custom_user(tenant, slug, levels, **fields):
    role = Role.objects.create(name=slug.title(), slug=slug)
    role.set_levels(levels)
    return User.objects.create_user(username=f"{slug}@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role,
                                    first_name=slug.title(), last_name="User", **fields)


@pytest.fixture
def floor(ctx, dept, pump_model):
    """The pump model can connect (wireless, as disclosed), all on a medical device segment; 5.2 (demo) past its security updates
    (Sep 21, 2026), 5.6 (demo) supported (announced to end in 2030). P-1, P-2, P-3 of ours on 5.2, P-4 on 5.6, the rental T-1 on 5.2."""
    profile = release(pump_model, "", connects=Connects.WIRELESS, segmented=Segmented.ALL)
    r52 = release(pump_model, "5.2 (demo)", security_updates_end_on=TODAY - 30 * DAY)
    r56 = release(pump_model, "5.6 (demo)", security_updates_end_on=date(2030, 3, 31))
    devices = {tag: Asset.objects.create(tag=tag, device_model=pump_model, department=dept) for tag in ("P-1", "P-2", "P-3", "P-4")}
    for tag in ("P-1", "P-2", "P-3"):
        facts(devices[tag], r52)
    facts(devices["P-4"], r56)
    rental = Asset.objects.create(tag="T-1", device_model=pump_model, department=dept, ownership=Ownership.RENTAL,
                                  owner="Summit Medical Rentals", due_back_on=date(2026, 11, 10))
    facts(rental, r52)
    devices["T-1"] = rental
    return SimpleNamespace(profile=profile, r52=r52, r56=r56, model=pump_model, d=devices, dept=dept)


def need_of(asset, today=TODAY):
    return rules.support_need(rules.device_standing(Asset.objects.get(pk=asset.pk), today), today)


# --- the support need ----------------------------------------------------------------------------------------------------------------

def test_security_updates_ended_is_a_need_with_its_reason(floor):
    need = need_of(floor.d["P-1"])
    assert (need.key, need.reasons, need.ended, need.decidable) == (rules.NEEDS, ("security_updates",), ("security_updates",), True)
    assert seen(need.words) == "Security updates ended after Sep 21, 2026"
    assert need_of(floor.d["P-4"]) is None  # 5.6: supported, its end announced years ahead


def test_the_models_support_ended_is_a_need_when_the_release_has_no_date(floor):
    bare = release(floor.model, "5.8 (demo)")
    facts(floor.d["P-4"], bare)
    assert need_of(floor.d["P-4"]) is None
    DeviceModel.objects.filter(pk=floor.model.pk).update(support_ends_on=TODAY - 5 * DAY)
    need = need_of(floor.d["P-4"])
    assert need.key == rules.NEEDS and need.reasons == ("security_updates",)
    assert seen(need.words) == "Manufacturer support ended Oct 16, 2026 (the model's)"


def test_the_os_ended_is_a_need_unless_the_manufacturers_updates_continue(floor):
    floor.profile.os_family, floor.profile.os_name = OsFamily.EMBEDDED, "Demo embedded OS 7"
    floor.profile.os_support_ends_on = TODAY - 100 * DAY
    floor.profile.save()
    bare = release(floor.model, "5.8 (demo)")  # no support dates of its own: the profile's group
    facts(floor.d["P-4"], bare)
    need = need_of(floor.d["P-4"])
    assert (need.key, need.reasons, need.ended) == (rules.NEEDS, ("os_support",), ("os_support",))
    assert seen(need.words) == "The OS vendor's published support for Demo embedded OS 7 ended after Jul 13, 2026"
    # extended support: the release's own security updates continue (its support group: its own OS fields too)
    ext = release(floor.model, "6.0 (demo)", os_family=OsFamily.EMBEDDED, os_name="Demo embedded OS 7",
                  os_support_ends_on=TODAY - 100 * DAY, security_updates_end_on=date(2028, 3, 31))
    facts(floor.d["P-4"], ext)
    need = need_of(floor.d["P-4"])
    assert (need.key, need.decidable, need.reasons, need.ended) == (rules.EXTENDED, False, (), ("os_support",))
    assert seen(need.words) == ("The OS vendor's support ended; the manufacturer's security updates for 6.0 (demo) end Mar 31, 2028 "
                                "(announced)")
    # ...within 12 months, an end ahead: both reasons, one ended
    ext.security_updates_end_on = TODAY + 100 * DAY
    ext.save()
    need = need_of(floor.d["P-4"])
    assert (need.key, need.reasons, need.ended) == (rules.ENDING, ("security_updates", "os_support"), ("os_support",))
    assert seen(need.words) == ("Ending within 12 months: security updates end Jan 29, 2027 (announced); the OS vendor's published "
                                "support for Demo embedded OS 7 ended after Jul 13, 2026")


def test_an_end_announced_within_12_months_is_information_a_decision_may_answer(floor):
    soon = release(floor.model, "5.4 (demo)", security_updates_end_on=date(2027, 10, 21))  # 12 months to the day
    facts(floor.d["P-4"], soon)
    need = need_of(floor.d["P-4"])
    assert (need.key, need.decidable, need.reasons, need.ended, need.day) == (rules.ENDING, True, ("security_updates",), (),
                                                                              date(2027, 10, 21))
    assert seen(need.words) == "Ending within 12 months: security updates end Oct 21, 2027 (announced)"
    soon.security_updates_end_on = date(2027, 10, 22)
    soon.save()
    assert need_of(floor.d["P-4"]) is None
    os_soon = release(floor.model, "5.5 (demo)", os_family=OsFamily.WINDOWS, os_name="Windows 10 IoT Enterprise LTSC 2019",
                      os_support_ends_on=date(2027, 1, 9))
    facts(floor.d["P-4"], os_soon)
    need = need_of(floor.d["P-4"])
    assert (need.key, need.reasons) == (rules.ENDING, ("os_support",))


def test_only_a_device_on_site_connected_or_able_to_connect_has_a_need(floor):
    facts(floor.d["P-1"], floor.r52, network=Network.NOT_CONNECTED)
    assert need_of(floor.d["P-1"]) is None
    facts(floor.d["P-1"], floor.r52, network=Network.WIRED)
    assert need_of(floor.d["P-1"]).key == rules.NEEDS
    floor.profile.connects = ""
    floor.profile.save()
    assert need_of(floor.d["P-2"]) is None  # connection not recorded
    floor.profile.connects = Connects.NONE
    floor.profile.save()
    assert need_of(floor.d["P-2"]) is None  # does not connect, as disclosed
    floor.profile.connects = Connects.WIRELESS
    floor.profile.save()
    Asset.objects.filter(pk=floor.d["P-2"].pk).update(status=AssetStatus.RETIRED)
    assert need_of(floor.d["P-2"]) is None


def test_both_ended_name_both_reasons(floor):
    floor.r52.os_family, floor.r52.os_support_ends_on = OsFamily.LINUX, TODAY - DAY
    floor.r52.save()
    need = need_of(floor.d["P-1"])
    assert need.reasons == need.ended == ("security_updates", "os_support")
    assert seen(need.words) == ("Security updates ended after Sep 21, 2026; the OS vendor's published support for Linux ended after Oct 20, "
                                "2026")


# --- the governing decision ----------------------------------------------------------------------------------------------------------

def test_the_most_specific_live_decision_governs(floor):
    model_wide = decide(1, device_model=floor.model)
    on52 = decide(2, device_model=floor.model, release=floor.r52)
    own = decide(3, asset=floor.d["P-1"])
    gov = rules.governing_decisions(Asset.objects.all(), TODAY)
    assert gov[floor.d["P-1"].pk] == own and gov[floor.d["P-2"].pk] == on52 and gov[floor.d["P-4"].pk] == model_wide
    DeviceSecurityDecision.objects.filter(pk=own.pk).update(ended_on=TODAY, end_reason=DecisionEnd.NO_LONGER_NEEDED)
    assert rules.governing_decisions(Asset.objects.all(), TODAY)[floor.d["P-1"].pk] == on52  # an ended one never governs


def test_a_lapsed_decision_still_governs_and_covers_nothing_with_no_fallback(floor):
    decide(1, device_model=floor.model)  # in force
    lapsed = decide(2, device_model=floor.model, release=floor.r52, decided_on=TODAY - 400 * DAY, review_by=TODAY - DAY)
    gov = rules.governing_decisions(list(Asset.objects.all()), TODAY)
    assert gov[floor.d["P-1"].pk] == lapsed
    assert not rules.covers(lapsed, TODAY) and rules.covers(lapsed, TODAY - DAY)  # covers on its review day, not after


def test_a_temporary_device_is_governed_only_by_its_own(floor):
    decide(1, device_model=floor.model)
    decide(2, device_model=floor.model, release=floor.r52)
    assert rules.governing_decisions([floor.d["T-1"]], TODAY) == {floor.d["T-1"].pk: None}
    own = decide(3, asset=floor.d["T-1"])
    assert rules.governing_decisions([floor.d["T-1"]], TODAY) == {floor.d["T-1"].pk: own}


def test_a_device_whose_release_is_another_models_reads_its_models_decision(floor, vent_model):
    model_wide = decide(1, device_model=floor.model)
    decide(2, device_model=floor.model, release=floor.r52)
    vent_release = release(vent_model, "1.0")
    facts(floor.d["P-2"], vent_release)  # a model change racing a record: the release names another model (rules.release_of)
    fresh = Asset.objects.get(pk=floor.d["P-2"].pk)  # its security row read again (the fixture's copy caches the first)
    assert rules.governing_decisions([fresh], TODAY)[fresh.pk] == model_wide


# --- the update responses ------------------------------------------------------------------------------------------------------------

def test_an_open_update_to_a_supported_release_responds(floor, techs):
    wo, _row = update_wo(floor.d["P-1"], floor.r56, assigned_to=techs["tom"])
    out = rules.update_responses([floor.d["P-1"], floor.d["P-2"]], TODAY)
    assert out[floor.d["P-2"].pk] == []
    (line,) = out[floor.d["P-1"].pk]
    assert (line.kind, seen(line.words), line.number) == (rules.UPDATE_OPEN, f"Update open to 5.6 (demo): {wo.number}", wo.number)
    wo_services.change_status(wo, WoStatus.IN_PROGRESS)
    assert rules.update_responses([floor.d["P-1"]], TODAY)[floor.d["P-1"].pk][0].kind == rules.UPDATE_OPEN


def test_an_update_to_a_release_past_support_patches_or_an_ended_os_never_responds(floor, techs):
    old = release(floor.model, "5.0 (demo)", security_updates_end_on=TODAY - 400 * DAY)
    update_wo(floor.d["P-1"], old)
    update_wo(floor.d["P-2"], reference="DEMO-PATCH-1")
    os_gone = release(floor.model, "5.9 (demo)", os_family=OsFamily.WINDOWS, os_support_ends_on=TODAY - DAY)
    update_wo(floor.d["P-3"], os_gone)
    out = rules.update_responses([floor.d[t] for t in ("P-1", "P-2", "P-3")], TODAY)
    assert all(lines == [] for lines in out.values())


def test_the_target_reads_its_models_profile_by_group(floor, techs):
    """A target with no support dates of its own reads its model's profile: the profile's OS ended, so it answers nothing."""
    floor.profile.os_family, floor.profile.os_support_ends_on = OsFamily.WINDOWS, TODAY - DAY
    floor.profile.save()
    bare = release(floor.model, "5.8 (demo)")
    update_wo(floor.d["P-1"], bare)
    assert rules.update_responses([floor.d["P-1"]], TODAY)[floor.d["P-1"].pk] == []
    update_wo(floor.d["P-2"], floor.r56)  # 5.6 has its own support group (its security updates date): the profile's OS is not read
    assert [r.kind for r in rules.update_responses([floor.d["P-2"]], TODAY)[floor.d["P-2"].pk]] == [rules.UPDATE_OPEN]


def test_done_with_the_version_not_confirmed_is_its_own_response(floor, techs):
    wo, row = update_wo(floor.d["P-1"], floor.r56, assigned_to=techs["tom"])
    WorkOrder.objects.filter(pk=wo.pk).update(status=WoStatus.COMPLETED, completed_on=date(2026, 10, 20))
    (line,) = rules.update_responses([floor.d["P-1"]], TODAY)[floor.d["P-1"].pk]
    assert (line.kind, seen(line.words)) == (rules.UPDATE_NOT_CONFIRMED,
                                             f"Update done Oct 20, 2026, version not confirmed: Confirm version ({wo.number})")
    assert line.update.pk == row.pk
    row.outcome = UpdateOutcome.INSTALLED
    row.save()
    assert rules.update_responses([floor.d["P-1"]], TODAY)[floor.d["P-1"].pk] == []  # done: the facts say what it runs
    WorkOrder.objects.filter(pk=wo.pk).update(status=WoStatus.CANCELLED)
    row.outcome = ""
    row.save()
    assert rules.update_responses([floor.d["P-1"]], TODAY)[floor.d["P-1"].pk] == []


def test_the_security_tabs_lines_in_order(tenant, floor, techs, make_user):
    manager, tech = make_user("manager"), make_user("technician")
    lines = rules.device_lines(floor.d["P-1"], TODAY, user=manager)
    assert lines.need.key == rules.NEEDS and lines.decision is None and lines.can_record and lines.link_work_orders
    assert [(r.kind, r.words) for r in lines.responses] == [(rules.NO_DECISION, "No decision recorded")]
    wo, _ = update_wo(floor.d["P-1"], floor.r56)
    lapsed = decide(1, device_model=floor.model, release=floor.r52, decided_on=TODAY - 400 * DAY, review_by=TODAY - 5 * DAY)
    lines = rules.device_lines(floor.d["P-1"], TODAY, user=tech)
    assert [r.kind for r in lines.responses] == [rules.UPDATE_OPEN, rules.DECIDED]
    assert lines.responses[1].number == lapsed.number and not lines.covers and not lines.responses[1].covers
    assert seen(lines.responses[1].words).startswith("Review due since Oct 16, 2026: keep in use without added controls.")
    assert not lines.can_record  # Device security Edit: never records a decision
    no_work = custom_user(tenant, "it-security", {Module.CYBERSECURITY: Level.APPROVE, Module.EQUIPMENT: Level.VIEW})
    lines = rules.device_lines(floor.d["P-1"], TODAY, user=no_work)
    assert lines.can_record and not lines.link_work_orders
    # no need, still governed: the decision shows; nothing else
    lines = rules.device_lines(floor.d["P-4"], TODAY, user=manager)
    assert lines.need is None and not lines.can_record and lines.responses == ()
    decide(2, device_model=floor.model)
    lines = rules.device_lines(floor.d["P-4"], TODAY, user=manager)
    assert [r.kind for r in lines.responses] == [rules.DECIDED] and lines.covers


# --- words ---------------------------------------------------------------------------------------------------------------------------

def test_the_sentences(floor, make_user):
    kim = make_user("director")
    kim.first_name, kim.last_name = "Kim", "Lee"
    kim.save()
    dana = make_user("manager")
    dana.first_name, dana.last_name = "Dana", "Ruiz"
    dana.save()
    with_controls = decide(1, device_model=floor.model, release=floor.r52, response=KEEP_WITH, accepted_by=AcceptedBy.IT_SECURITY,
                           controls=["remote_access_controlled", "traffic_restricted"], recorded_by=kim, decided_on=TODAY,
                           review_by=date(2027, 10, 21), exception_reference="DEMO-EX-1042", plan=DecisionPlan.UPDATE_PLANNED,
                           plan_by=date(2027, 3, 1), plan_owner=dana)
    assert seen(rules.decision_words(with_controls, TODAY, fy_start=1)) == (
        "Keep in use with controls (2: traffic limited to the ports and hosts it needs; vendor remote access off, or attended only); "
        "review by Oct 21, 2027. Plan: update by Mar 1, 2027 (owner Dana Ruiz). Accepted by IT security or the CISO, as recorded by "
        "Kim Lee on Oct 21, 2026; IT exception DEMO-EX-1042.")
    without = decide(2, asset=floor.d["P-4"], decided_on=TODAY, review_by=date(2027, 10, 21))
    assert seen(rules.decision_words(without, TODAY, fy_start=1)) == (
        "Keep in use without added controls: risk accepted until Oct 21, 2027. No plan recorded. Accepted by the CE leader, as recorded "
        "on Oct 21, 2026; no IT exception number recorded.")
    out = decide(3, asset=floor.d["P-3"], response=OUT_OF_USE, decided_on=TODAY, review_by=date(2027, 1, 21),
                 accepted_by=AcceptedBy.COMMITTEE, recorded_by=kim, plan=DecisionPlan.REPLACE_PLANNED, plan_by=date(2027, 8, 1))
    assert seen(rules.decision_words(out, TODAY, fy_start=1)) == (
        "Take out of use, decided Oct 21, 2026; review by Jan 21, 2027. Plan: replace, planned for FY2027. Decided by a committee, as "
        "recorded by Kim Lee on Oct 21, 2026; no IT exception number recorded.")
    # past review: read first, replacing the review part; "risk accepted until" dropped
    assert seen(rules.decision_words(without, date(2027, 10, 22), fy_start=1)) == (
        "Review due since Oct 21, 2027: keep in use without added controls. No plan recorded. Accepted by the CE leader, as recorded on "
        "Oct 21, 2026; no IT exception number recorded.")
    assert seen(rules.decision_words(out, date(2027, 1, 22), fy_start=1)).startswith(
        "Review due since Jan 21, 2027: take out of use, decided Oct 21, 2026. Plan: replace")
    assert rules.decision_words(without, date(2027, 10, 21), fy_start=1).startswith("Keep in use without added controls: risk accepted")


def test_the_plan_words_and_the_fiscal_year_read_at_read_time(floor):
    out = decide(1, asset=floor.d["P-3"], plan=DecisionPlan.REPLACE_PLANNED, plan_by=date(2027, 8, 1))
    assert rules.plan_words(out, fy_start=1) == "Plan: replace, planned for FY2027"
    assert rules.plan_words(out, fy_start=7) == "Plan: replace, planned for FY2028"
    settings = get_settings()
    settings.fiscal_year_start_month = 7
    settings.save()
    assert rules.plan_words(out) == "Plan: replace, planned for FY2028"  # read now, never stored
    settings.fiscal_year_start_month = 1
    settings.save()
    assert rules.plan_words(out) == "Plan: replace, planned for FY2027"
    for plan, words in ((DecisionPlan.WAIT_FOR_MANUFACTURER, "Plan: wait for the manufacturer"),
                        (DecisionPlan.RETURN_TO_OWNER, "Plan: return it to its owner"), (DecisionPlan.NONE, "No plan recorded")):
        out.plan, out.plan_by = plan, None
        assert seen(rules.plan_words(out, fy_start=1)) == words


def test_controls_recorded_in_its_system_carry_its_reference(floor):
    d = decide(1, asset=floor.d["P-1"], response=KEEP_WITH, controls=["recorded_elsewhere", "segmented"], controls_reference="CHG0012345")
    assert rules.control_words(d) == ["On a medical device network segment", "Controls recorded in IT's system (CHG0012345)"]
    assert seen(rules.decision_words(d, TODAY, fy_start=1)).startswith(
        "Keep in use with controls (2: on a medical device network segment; controls recorded in IT's system (CHG0012345)); review by")
    assert rules.reason_words(d) == [rules.REASONS["security_updates"]]


def test_scope_and_state_words(floor):
    device = decide(1, asset=floor.d["P-1"], review_by=TODAY + 31 * DAY)
    on52 = decide(2, device_model=floor.model, release=floor.r52, review_by=TODAY + 30 * DAY)
    model_wide = decide(3, device_model=floor.model, review_by=TODAY - DAY, decided_on=TODAY - 100 * DAY)
    assert [rules.scope_words(d) for d in (device, on52, model_wide)] == ["P-1", "Alaris 8015 PCU on 5.2 (demo)", "Every Alaris 8015 PCU of ours"]
    assert rules.scope_words(on52, 11) == "Alaris 8015 PCU on 5.2 (demo): 11 devices of ours on site"
    assert rules.scope_words(on52, 1) == "Alaris 8015 PCU on 5.2 (demo): 1 device of ours on site"
    assert rules.scope_words(model_wide, 23) == "Every Alaris 8015 PCU of ours: 23 on site"
    assert [rules.decision_state(d, TODAY) for d in (device, on52, model_wide)] == [rules.IN_FORCE, rules.REVIEW_SOON, rules.REVIEW_DUE]
    assert rules.review_words(model_wide, TODAY) == "Review due since Oct 20, 2026"
    assert rules.review_words(device, TODAY) == "Review by Nov 21, 2026"
    device.ended_on, device.end_reason = TODAY, DecisionEnd.SUPERSEDED
    assert rules.decision_state(device, TODAY) == rules.ENDED and rules.review_words(device, TODAY) == "Superseded Oct 21, 2026"
    assert rules.DECISION_STATE_WORDS == {"in_force": "In force", "review_soon": "Review within 30 days", "review_due": "Review due",
                                          "ended": "Ended"}


def test_review_defaults_and_caps_are_product_choices(floor):
    for fn in (rules.review_cap, rules.review_default):
        assert "product choices, not a rule" in " ".join(fn.__doc__.split()).casefold()
    assert rules.review_default(KEEP_WITH, TODAY) == date(2027, 10, 21)
    assert rules.review_default(OUT_OF_USE, TODAY) == date(2027, 1, 21)
    assert rules.review_cap(OUT_OF_USE, TODAY) == date(2027, 10, 21) and rules.review_cap(KEEP_WITHOUT, TODAY) == date(2029, 10, 21)
    rental = floor.d["T-1"]
    assert rules.review_default(KEEP_WITHOUT, TODAY, asset=rental) == date(2026, 11, 10)  # its due back day
    rental.due_back_on = None
    assert rules.review_default(KEEP_WITHOUT, TODAY, asset=rental) == date(2027, 1, 21)  # none: 3 months
    rental.due_back_on = TODAY - DAY  # past due back: 3 months
    assert rules.review_default(KEEP_WITHOUT, TODAY, asset=rental) == date(2027, 1, 21)
    assert rules.review_default(KEEP_WITHOUT, TODAY, asset=floor.d["P-1"]) == date(2027, 10, 21)
    assert rules.review_rule_words(KEEP_WITH) == "By default 12 months after the day decided (at most 36 months)"
    assert rules.review_rule_words(OUT_OF_USE) == "By default 3 months after the day decided (at most 12 months)"


# --- Check lines ---------------------------------------------------------------------------------------------------------------------

def checks(decision, today=TODAY) -> dict:
    out = {c.key: c for c in rules.decision_checks(DeviceSecurityDecision.objects.get(pk=decision.pk), today)}
    for c in out.values():
        seen(c.words)
    return out


def test_a_reason_ended_since_the_decision(floor):
    d = decide(1, device_model=floor.model, release=floor.r52, governed_ids=[str(floor.d[t].pk) for t in ("P-1", "P-2", "P-3")])
    assert checks(d) == {}
    floor.r52.os_family, floor.r52.os_support_ends_on = OsFamily.LINUX, TODAY - DAY
    floor.r52.save()
    assert checks(d)[rules.CHECK_ALSO_ENDED].words == "The OS vendor's support also ended since this decision"
    d.reasons = ["security_updates", "os_support"]
    d.save()
    assert rules.CHECK_ALSO_ENDED not in checks(d)
    # an announced end that passed is the same reason (kinds, never states)
    soon = release(floor.model, "5.4 (demo)", security_updates_end_on=TODAY + 10 * DAY)
    facts(floor.d["P-4"], soon)
    own = decide(2, asset=floor.d["P-4"])
    assert rules.CHECK_ALSO_ENDED not in checks(own, TODAY + 20 * DAY)


def test_the_segment_the_decision_relies_on(floor):
    d = decide(1, device_model=floor.model, release=floor.r52, response=KEEP_WITH, controls=["segmented"],
               governed_ids=[str(floor.d[t].pk) for t in ("P-1", "P-2", "P-3")])
    assert checks(d) == {}  # all on a medical device segment
    floor.profile.segmented = Segmented.NONE
    floor.profile.save()
    assert checks(d)[rules.CHECK_SEGMENT].words == rules.SEGMENT_NONE_WORDS
    floor.profile.segmented = Segmented.SOME
    floor.profile.save()
    some = checks(d)[rules.CHECK_SEGMENT_SOME]
    assert some.info and some.words == "Only some of its devices are on a segment, as the model's record says"
    floor.profile.segmented = ""
    floor.profile.save()
    assert checks(d)[rules.CHECK_SEGMENT_BLANK].info


def test_governs_no_device_here_now(floor):
    d = decide(1, asset=floor.d["P-1"])
    Asset.objects.filter(pk=floor.d["P-1"].pk).update(status=AssetStatus.RETIRED)
    found = checks(d)
    assert found[rules.CHECK_NO_DEVICE].words == "Governs no device here now: end it" and found[rules.CHECK_NO_DEVICE].action == "end"
    gone = decide(2, device_model=floor.model, release=release(floor.model, "4.0 (demo)"), response=KEEP_WITH, controls=["segmented"])
    floor.profile.segmented = Segmented.NONE
    floor.profile.save()
    assert set(checks(gone)) == {rules.CHECK_NO_DEVICE, rules.CHECK_SEGMENT}  # the profile read with no device of the model in scope


def test_devices_joined_since_by_identity(floor, dept):
    d = decide(1, device_model=floor.model, release=floor.r52, governed_ids=[str(floor.d[t].pk) for t in ("P-1", "P-2")])
    assert checks(d)[rules.CHECK_JOINED].words == "1 device joined since: P-3"
    # one leaves and another joins in the same month: the count stays, the identity tells
    facts(floor.d["P-3"], floor.r56)
    late = Asset.objects.create(tag="P-9", device_model=floor.model, department=dept)
    facts(late, floor.r52)
    assert checks(d)[rules.CHECK_JOINED].words == "1 device joined since: P-9"
    late2 = Asset.objects.create(tag="P-10", device_model=floor.model, department=dept)
    facts(late2, floor.r52)
    assert checks(d)[rules.CHECK_JOINED].words == "2 devices joined since: P-10, P-9"
    own = decide(2, asset=late2)  # a device under its own decision is not this one's
    assert checks(d)[rules.CHECK_JOINED].words == "1 device joined since: P-9" and rules.CHECK_JOINED not in checks(own)


def test_the_plans_date_passed_take_out_of_use_in_service_and_the_owner(tenant, floor, make_user):
    owner = make_user("manager")
    d = decide(1, asset=floor.d["P-1"], response=OUT_OF_USE, decided_on=TODAY, review_by=TODAY + 90 * DAY,
               plan=DecisionPlan.UPDATE_PLANNED, plan_by=date(2027, 3, 1), plan_owner=owner)
    assert checks(d) == {}  # decided today: still in service is no news yet
    later = checks(d, TODAY + DAY)
    assert later[rules.CHECK_IN_SERVICE].words == "Still in service since the decision to take it out of use (Oct 21, 2026)"
    assert checks(d, date(2027, 3, 2))[rules.CHECK_PLAN_PASSED].words == "The plan's date (Mar 1, 2027) has passed"
    assert rules.CHECK_PLAN_PASSED not in checks(d, date(2027, 3, 1))
    Asset.objects.filter(pk=floor.d["P-1"].pk).update(status=AssetStatus.OUT_OF_SERVICE)
    assert rules.CHECK_IN_SERVICE not in checks(d, TODAY + DAY)
    User.objects.filter(pk=owner.pk).update(is_active=False)
    assert checks(d)[rules.CHECK_OWNER].words == "The plan owner's account is deactivated"
    on52 = decide(2, device_model=floor.model, release=floor.r52, response=OUT_OF_USE, decided_on=TODAY, review_by=TODAY + 90 * DAY,
                  governed_ids=[str(floor.d[t].pk) for t in ("P-2", "P-3")])
    assert checks(on52, TODAY + DAY)[rules.CHECK_IN_SERVICE].words == (
        "P-2, P-3 still in service since the decision to take them out of use (Oct 21, 2026)")
    assert rules.decision_checks(DeviceSecurityDecision.objects.get(pk=d.pk), TODAY) != []
    DeviceSecurityDecision.objects.filter(pk=d.pk).update(ended_on=TODAY, end_reason=DecisionEnd.ENTERED_IN_ERROR)
    assert rules.decision_checks(DeviceSecurityDecision.objects.get(pk=d.pk), TODAY) == []  # an ended decision has none


def test_the_decisions_devices(floor, techs):
    on52 = decide(1, device_model=floor.model, release=floor.r52)
    own = decide(2, asset=floor.d["P-2"])
    wo, _ = update_wo(floor.d["P-3"], floor.r56)
    dds = rules.decision_devices(on52, TODAY)
    assert [dd.asset.tag for dd in dds] == ["P-1", "P-2", "P-3"]  # the rental on 5.2 is not ours: never in a release's scope
    assert [dd.governed for dd in dds] == [True, False, True] and dds[1].governing == own
    assert [seen(dd.words(TODAY)) for dd in dds] == ["This decision", "Governed by SD-26-0002",
                                                    f"Update open to 5.6 (demo): {wo.number}; This decision"]
    assert dds[0].standing.connection.words == "Can connect (wireless), as disclosed" and dds[0].need.key == rules.NEEDS


# --- Needs attention and the badge ---------------------------------------------------------------------------------------------------

def headings(user, today=TODAY) -> dict:
    panel = rules.needs_attention(today, user=user)
    for h in panel.headings:
        seen(h.words)
        for row in h.rows:
            seen(row.words)
    return {h.key: h for h in panel.headings}


def test_needs_attention_lists_each_heading(tenant, floor, techs, make_user):
    manager = make_user("manager")
    found = headings(manager)
    assert list(found) == ["no_decision", "temporary"]
    (row,) = found["no_decision"].rows
    assert row.words == "Alaris 8015 PCU on 5.2 (demo): 3 devices of ours; security updates ended after Sep 21, 2026"
    assert row.count == 3 and row.tags == ("P-1", "P-2", "P-3")
    assert row.link == rules.Link("web:cybersecurity_model", (floor.model.pk,), (("whose", "ours"), ("connected", "either"),
                                                                                 ("standing", "updates_ended")))
    assert row.record == rules.Link("web:security_decision_new", (), (("release", floor.r52.pk),))
    (temp,) = found["temporary"].rows
    assert temp.words == ("T-1 (rental, Summit Medical Rentals; due back Nov 10, 2026): security updates ended; its owner installs "
                          "updates: ask the owner to update it, or return it")
    assert temp.record == rules.Link("web:security_decision_new", (), (("asset", "T-1"),))
    # an update open answers P-1; one done with the version not confirmed lists P-2 apart
    update_wo(floor.d["P-1"], floor.r56)
    wo2, _ = update_wo(floor.d["P-2"], floor.r56)
    WorkOrder.objects.filter(pk=wo2.pk).update(status=WoStatus.COMPLETED, completed_on=TODAY)
    found = headings(manager)
    assert found["no_decision"].rows[0].tags == ("P-3",) and found["no_decision"].count == 1
    (nc,) = found["not_confirmed"].rows
    assert nc.words == f"P-2: Update done Oct 21, 2026, version not confirmed: Confirm version ({wo2.number})"
    assert nc.work_order == rules.Link("web:wo", (wo2.number,))


def test_needs_attention_decisions_and_checks(tenant, floor, make_user):
    manager = make_user("manager")
    lapsed = decide(1, device_model=floor.model, release=floor.r52, decided_on=TODAY - 400 * DAY, review_by=TODAY - DAY,
                    plan=DecisionPlan.UPDATE_PLANNED, plan_by=TODAY - DAY, governed_ids=[str(floor.d[t].pk) for t in ("P-1", "P-2", "P-3")])
    soon = decide(2, asset=floor.d["T-1"], review_by=TODAY + 20 * DAY)
    gone = decide(3, device_model=floor.model, release=release(floor.model, "4.0 (demo)"))
    found = headings(manager)
    assert "no_decision" not in found and "temporary" not in found  # lapsed is listed under its decision, never "no decision recorded"
    (due,) = found["review_due"].rows
    assert due.words == "SD-26-0001 · Alaris 8015 PCU on 5.2 (demo): 3 devices of ours on site; review due since Oct 20, 2026"
    assert due.decision == lapsed and due.record == rules.Link("web:security_decision_new", (), (("supersedes", "SD-26-0001"),))
    assert due.link == rules.Link("web:security_decision", ("SD-26-0001",)) and due.end is None
    assert found["review_soon"].rows[0].words == "SD-26-0002 · T-1; review by Nov 10, 2026" and found["review_soon"].rows[0].decision == soon
    (none_here,) = found["no_device"].rows
    assert none_here.words == "SD-26-0003 · Alaris 8015 PCU on 4.0 (demo); review by Aug 17, 2027"
    assert none_here.end == rules.Link("web:security_decision_end", ("SD-26-0003",)) and none_here.decision == gone
    assert [r.words for r in found["checks"].rows] == ["SD-26-0001 · The plan's date (Oct 20, 2026) has passed"]


def test_needs_attention_ending_and_the_readers(tenant, floor, make_user):
    soon = release(floor.model, "5.4 (demo)", security_updates_end_on=date(2027, 6, 30))
    facts(floor.d["P-4"], soon)
    for tag in ("P-1", "P-2", "P-3", "T-1"):
        facts(floor.d[tag], floor.r56)
    viewer = custom_user(tenant, "viewer", {Module.CYBERSECURITY: Level.VIEW, Module.EQUIPMENT: Level.VIEW})
    found = headings(viewer)
    assert list(found) == ["ending"] and found["ending"].info
    (row,) = found["ending"].rows
    assert row.words == "Alaris 8015 PCU on 5.4 (demo): 1 device of ours; ending within 12 months: security updates end Jun 30, 2027 (announced)"
    assert row.record is None and row.link.query[-1] == ("standing", "updates_end_announced")  # View: no Record decision
    facts(floor.d["P-4"], floor.r56)
    panel = rules.needs_attention(TODAY, user=viewer)
    assert panel.empty and panel.empty_words == "Nothing listed from what is recorded here."
    no_equipment = custom_user(tenant, "cyber-only", {Module.CYBERSECURITY: Level.FULL})
    facts(floor.d["P-4"], floor.r52)
    assert rules.needs_attention(TODAY, user=no_equipment).empty  # a decision can name a device: Equipment View too
    assert rules.needs_attention(TODAY, user=None).headings  # the system


def test_the_badge_counts_lapsed_decisions_that_still_hold_a_device_on_site(floor, vent_model, dept):
    assert rules.badge(TODAY) == {"count": 0, "hot": False}
    past = {"decided_on": TODAY - 400 * DAY, "review_by": TODAY - DAY}
    own = decide(1, asset=floor.d["P-1"], **past)
    decide(2, device_model=floor.model, release=floor.r52, **past)
    decide(3, device_model=floor.model, **past)
    decide(4, asset=floor.d["P-4"], decided_on=TODAY - 400 * DAY, review_by=TODAY)  # its review is today: not past
    assert rules.badge(TODAY) == {"count": 3, "hot": True}
    Asset.objects.filter(pk=floor.d["P-1"].pk).update(status=AssetStatus.RETIRED)
    assert rules.badge(TODAY)["count"] == 2
    for tag in ("P-2", "P-3"):
        facts(floor.d[tag], floor.r56)
    assert rules.badge(TODAY)["count"] == 1  # 5.2 now holds only the rental, never in a release's scope
    Asset.objects.filter(device_model=floor.model, ownership=Ownership.OWNED).update(status=AssetStatus.RETIRED)
    assert rules.badge(TODAY) == {"count": 0, "hot": False}  # the rental alone: no model scope holds it
    DeviceSecurityDecision.objects.filter(pk=own.pk).update(ended_on=TODAY, end_reason=DecisionEnd.NO_LONGER_NEEDED)
    vent_release = release(vent_model, "1.0")
    vent = Asset.objects.create(tag="V-1", device_model=vent_model, department=dept)
    facts(vent, vent_release)
    decide(5, device_model=vent_model, release=vent_release, **past)
    assert rules.badge(TODAY) == {"count": 1, "hot": True}
    assert rules.badge(TODAY)["count"] == len(headings(None)["review_due"].rows)


# --- fixed queries as devices and decisions grow ----------------------------------------------------------------------------------------

def _grow(floor, n: int, start: int):
    """n more pumps of ours on 5.2 (each with its own lapsed decision and an open update), and a decision per new release."""
    for i in range(start, start + n):
        a = Asset.objects.create(tag=f"G-{i:03d}", device_model=floor.model, department=floor.dept)
        facts(a, floor.r52)
        decide(100 + i, asset=a, decided_on=TODAY - 400 * DAY, review_by=TODAY - DAY)
        update_wo(a, floor.r56)
        r = release(floor.model, f"3.{i} (demo)", security_updates_end_on=TODAY - DAY)
        decide(500 + i, device_model=floor.model, release=r)


def test_fixed_queries_as_devices_and_decisions_grow(floor, django_assert_num_queries):
    decide(1, device_model=floor.model, release=floor.r52, controls=["segmented"], response=KEEP_WITH)
    counts = []
    for n, start in ((2, 0), (6, 10)):
        _grow(floor, n, start)
        d = DeviceSecurityDecision.objects.get(number="SD-26-0001")
        with django_assert_num_queries(2):
            rules.governing_decisions(Asset.objects.all(), TODAY)
        devices = list(Asset.objects.all())
        with django_assert_num_queries(1):
            rules.update_responses(devices, TODAY)
        with django_assert_num_queries(4):
            dds = rules.decision_devices(d, TODAY)
        with django_assert_num_queries(0):
            rules.decision_checks(d, TODAY, devices=dds)
        with django_assert_num_queries(4):
            rules.needs_attention(TODAY, user=None)
        with django_assert_num_queries(1):
            rules.badge(TODAY)
        counts.append(len(dds))
    assert counts[1] > counts[0]


def test_every_new_word_and_slug_passes_the_never_words():
    texts = list(SEEN)
    texts += list(rules.CONTROLS) + list(rules.CONTROLS.values()) + list(rules.REASONS) + list(rules.REASONS.values())
    texts += list(rules.ALSO_ENDED.values()) + list(rules.ATTENTION_HEADINGS.values()) + list(rules.DECISION_STATE_WORDS.values())
    texts += list(rules.SCOPE_LABELS.values()) + list(rules.END_WORDS.values())
    for choices in (AppliesTo, DecisionResponse, DecisionPlan, AcceptedBy, DecisionEnd):
        texts += [str(label) for label in choices.labels] + list(choices.values)
    texts += [v for k, v in vars(rules).items() if k.isupper() and isinstance(v, str)]
    texts += [v for k, v in vars(decisions).items() if k.isupper() and isinstance(v, str)]
    texts += [w for k, v in vars(decisions).items() if k.isupper() and isinstance(v, dict) for w in v.values() if isinstance(w, str)]
    texts += [rules.review_rule_words(r) for r in DecisionResponse.values]
    assert len(texts) > 150
    assert [t for t in texts if never(t)] == []
    assert never("Overdue") and never("a DEADLINE") and never("Exempt") and never("waiver") and never("hardened")  # the scan itself
    assert rules.NETWORK_REMOVED not in rules.SUPPORT_CONTROLS and list(rules.SUPPORT_CONTROLS) == [s for s in rules.CONTROLS
                                                                                                    if s != "network_removed"]


def test_another_facilitys_decisions_are_never_read(floor, other_tenant):
    from apps.tenants.context import tenant_context

    with tenant_context(other_tenant):
        dept = Department.objects.create(name="Their ICU")
        model = DeviceModel.objects.create(manufacturer="BD", model="Alaris 8015 PCU", description="Pump", category="Infusion pumps",
                                           risk_class=RiskClass.HIGH)
        theirs = Asset.objects.create(tag="P-1", device_model=model, department=dept)
        decide(1, asset=theirs, decided_on=TODAY - 400 * DAY, review_by=TODAY - DAY)
    assert rules.badge(TODAY)["count"] == 0
    assert rules.governing_decisions(Asset.objects.all(), TODAY)[floor.d["P-1"].pk] is None
    assert "review_due" not in headings(None)


def test_the_clock_is_the_facilitys(floor):
    """The rules take `today`; a door passes timezone.localdate() (inside the facility)."""
    assert rules.badge(timezone.localdate()) == {"count": 0, "hot": False}
