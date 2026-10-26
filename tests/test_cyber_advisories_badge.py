"""
Slice 40 wave 2C: Device security's nav badge by halves (apps/reports/services.py _cyber_badge; build40 section 7.9), on SQLite and
PostgreSQL.

- Slice 39's half (live decisions of any need key past their review date that still govern a device on site: rules.badge) for a reader of
  decisions (permissions.can_view_devices); slice 40's (open advisories, not entered in error, with models to confirm:
  rules.advisory_badge, stored rows only) for whoever confirms them (permissions.can_edit, Equipment View or not).
- One count, their sum; hot only from decisions; the title names the halves that count (BADGE_TITLE, ADVISORY_BADGE_TITLE,
  BOTH_BADGE_TITLE); a reader of either half with nothing counted reads no count; nothing for no user, a scoped user, or a role reading
  neither half.
- One aggregate query per half however many advisories and decisions; never in the Overview's attention list; the nav shows the count
  and its title once, never hot from advisories alone.
"""
import re
from datetime import timedelta

import pytest
from cyber_fixtures import facts, make_advisory, model_row, release
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from pg_helpers import as_app_role, needs_postgres

from apps.accounts.models import DataScope, Level, Module, Role, User
from apps.cybersecurity import decisions, rules
from apps.cybersecurity.models import (
    AcceptedBy,
    AdvisoryStatus,
    AppliesTo,
    Connects,
    DecisionResponse,
    DeviceSecurityDecision,
    SecurityAdvisory,
    Segmented,
)
from apps.equipment.models import Asset, DeviceModel
from apps.reports.services import _cyber_badge, attention_items, nav_counts
from apps.tenants.context import tenant_context

DAY = timedelta(days=1)


def never(text: str) -> list:
    return [w for w in rules.DECISION_NEVER_WORDS if re.search(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", text, re.IGNORECASE)]


def custom_user(tenant, name, levels, scope="", **extra):
    role = Role.objects.create(name=name.title(), slug=name, scope=scope)
    role.set_levels({m: levels.get(m, Level.NONE) for m in Module.values})
    return User.objects.create_user(username=f"{name}@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role, **extra)


def cyber_keys(counts: dict) -> set:
    return {k for k in counts if k.startswith("cybersecurity")}


@pytest.fixture
def today(ctx):
    return timezone.localdate()


@pytest.fixture
def floor(ctx, dept, pump_model, make_user, today):
    """The pumps on 5.2 (demo), past its security updates, P-1 to P-3 of ours; a support decision for 5.2 past its review 5 days ago (the
    decisions half's one); two open advisories with models to confirm (AD-1 naming no model yet, AD-2 naming the pumps unconfirmed), and
    three that never count: one confirmed, one closed (to confirm since a change: Needs attention's, never the badge's), one entered in
    error."""
    release(pump_model, connects=Connects.WIRELESS, segmented=Segmented.ALL)
    r52 = release(pump_model, "5.2 (demo)", security_updates_end_on=today - 30 * DAY)
    pumps = [Asset.objects.create(tag=f"P-{i}", device_model=pump_model, department=dept) for i in (1, 2, 3)]
    for p in pumps:
        facts(p, r52)
    lapsed = decisions.record_decision(release=r52, reasons=["security_updates"], response=DecisionResponse.KEEP_WITHOUT_ADDED_CONTROLS,
                                       accepted_by=AcceptedBy.IT_SECURITY, decided_on=today - 200 * DAY, review_by=today - 5 * DAY, by=None,
                                       today=today)
    ad1 = make_advisory(reference="DEMO-ADV-0001")
    ad2 = make_advisory(reference="DEMO-ADV-0002")
    model_row(ad2, pump_model, releases=[r52], remedy="mitigations", confirm=False)
    confirmed = make_advisory(reference="DEMO-ADV-0003")
    model_row(confirmed, pump_model, releases=[r52], remedy="mitigations")
    closed = make_advisory(reference="DEMO-ADV-0004", status=AdvisoryStatus.CLOSED, status_on=today)
    removed = make_advisory(reference="DEMO-ADV-0005", removed_on=today)
    return {"r52": r52, "pumps": pumps, "lapsed": lapsed, "ad1": ad1, "ad2": ad2, "confirmed": confirmed, "closed": closed,
            "removed": removed, "kim": make_user("manager"), "model": pump_model, "dept": dept}


def test_the_halves_add_up_with_a_title_for_what_counts(floor, make_user, today):
    assert rules.advisory_badge(today) == {"count": 2} and rules.badge(today) == {"count": 1, "hot": True}
    # a reader of both halves (the CE manager: Approve with Equipment View; the director; the technician at Edit)
    for user in (floor["kim"], make_user("director"), make_user("technician")):
        assert _cyber_badge(user) == {"cybersecurity": 3, "cybersecurity_hot": True, "cybersecurity_title": rules.BOTH_BADGE_TITLE}, user
    assert rules.BOTH_BADGE_TITLE == "Advisories with models to confirm, and decisions past their review date"
    # the decision renewed: only advisories count, never hot
    decisions.record_decision(release=floor["r52"], reasons=["security_updates"], response=DecisionResponse.KEEP_WITHOUT_ADDED_CONTROLS,
                              accepted_by=AcceptedBy.IT_SECURITY, supersedes=floor["lapsed"], by=None, today=today)
    assert _cyber_badge(floor["kim"]) == {"cybersecurity": 2, "cybersecurity_hot": False, "cybersecurity_title": rules.ADVISORY_BADGE_TITLE}
    assert rules.ADVISORY_BADGE_TITLE == "Advisories with models to confirm"
    # the models confirmed (stored state: the confirmation on the advisory's row), and AD-1 marked not applicable: nothing counts
    SecurityAdvisory.objects.filter(pk=floor["ad2"].pk).update(models_confirmed_on=today)
    SecurityAdvisory.objects.filter(pk=floor["ad1"].pk).update(status=AdvisoryStatus.NOT_APPLICABLE, not_applicable_reason="no_model_here",
                                                                status_on=today)
    assert rules.advisory_badge(today) == {"count": 0}
    assert _cyber_badge(floor["kim"]) == {"cybersecurity": None, "cybersecurity_hot": False}


def test_a_decision_alone_keeps_slice_39s_title(floor, today):
    SecurityAdvisory.objects.filter(pk__in=[floor["ad1"].pk, floor["ad2"].pk]).update(models_confirmed_on=today)
    assert _cyber_badge(floor["kim"]) == {"cybersecurity": 1, "cybersecurity_hot": True, "cybersecurity_title": rules.BADGE_TITLE}


def test_an_advisory_decision_past_review_counts_in_the_decisions_half(floor, today):
    """rules.badge counts any need key: an advisory decision past its review whose scope holds a device on site."""
    a = floor["confirmed"]
    DeviceSecurityDecision.objects.create(number=f"SD-{today:%y}-0901", applies_to=AppliesTo.ADVISORY, advisory=a, advisory_changes=a.changes,
                                          asset=floor["pumps"][0], reasons=[], response=DecisionResponse.NOT_IN_USE_HERE,
                                          accepted_by=AcceptedBy.CE_LEADER, decided_on=today - 400 * DAY, review_by=today - DAY)
    assert _cyber_badge(floor["kim"])["cybersecurity"] == 2 + 2 and _cyber_badge(floor["kim"])["cybersecurity_hot"] is True


def test_each_half_for_its_own_readers(floor, tenant, make_user):
    full = {m: Level.FULL for m in Module.values}
    # a reader of decisions who confirms nothing (Device security View, Equipment View): the decisions half only
    itsec_read = custom_user(tenant, "itsec-read", {Module.CYBERSECURITY: Level.VIEW, Module.EQUIPMENT: Level.VIEW})
    assert _cyber_badge(itsec_read) == {"cybersecurity": 1, "cybersecurity_hot": True, "cybersecurity_title": rules.BADGE_TITLE}
    # IT's analyst at Device security Edit without Equipment View (reads no decision): the advisory half only, never hot
    it_edit = custom_user(tenant, "it-edit", {Module.CYBERSECURITY: Level.EDIT})
    assert _cyber_badge(it_edit) == {"cybersecurity": 2, "cybersecurity_hot": False, "cybersecurity_title": rules.ADVISORY_BADGE_TITLE}
    assert nav_counts(it_edit)["cybersecurity"] == 2
    # Device security View alone: neither half
    view_only = custom_user(tenant, "view-only", {Module.CYBERSECURITY: Level.VIEW})
    assert _cyber_badge(view_only) == {} and cyber_keys(nav_counts(view_only)) == set()
    # scoped roles with Full everywhere, and the default roles without Device security
    for user in (custom_user(tenant, "companyfull", full, scope=DataScope.COMPANY, company="BD"),
                 custom_user(tenant, "unitfull", full, scope=DataScope.DEPARTMENT, department="ICU"),
                 make_user("analyst"), make_user("requester"), make_user("vendor")):
        assert _cyber_badge(user) == {} and cyber_keys(nav_counts(user)) == set(), user.username
    assert _cyber_badge(None) == {} and cyber_keys(nav_counts()) == set()


def test_one_query_per_half_as_advisories_and_decisions_grow(floor, tenant, dept, today):
    both = User.objects.select_related("role").prefetch_related("role__permissions").get(pk=floor["kim"].pk)
    advisories_only = custom_user(tenant, "it-edit", {Module.CYBERSECURITY: Level.EDIT})
    advisories_only = User.objects.select_related("role").prefetch_related("role__permissions").get(pk=advisories_only.pk)

    def counted(user):
        with CaptureQueriesContext(connection) as q:
            found = _cyber_badge(user)
        return len(q.captured_queries), found["cybersecurity"]

    assert (counted(both), counted(advisories_only)) == ((2, 3), (1, 2))
    for i in range(4):
        model = DeviceModel.objects.create(manufacturer="Demo", model=f"Bed {i}", description="Bed", category="Beds")
        a = make_advisory(reference=f"DEMO-ADV-01{i}")
        model_row(a, model, versions="not_stated", remedy="no_fix_yet", confirm=False)
        bed = Asset.objects.create(tag=f"B-{i}", device_model=model, department=dept)
        DeviceSecurityDecision.objects.create(number=f"SD-{today:%y}-08{i}0", applies_to=AppliesTo.ADVISORY, advisory=a,
                                              advisory_changes=a.changes, asset=bed, reasons=[], response=DecisionResponse.NOT_IN_USE_HERE,
                                              accepted_by=AcceptedBy.CE_LEADER, decided_on=today - 400 * DAY, review_by=today - DAY)
    assert (counted(both), counted(advisories_only)) == ((2, 3 + 4 + 4), (1, 2 + 4))


def test_never_in_the_overview_attention_list(floor):
    items = str(attention_items())
    for words in (floor["ad1"].number, floor["ad2"].number, rules.ADVISORY_BADGE_TITLE, rules.BOTH_BADGE_TITLE, "DEMO-ADV"):
        assert words not in items


def test_the_nav_shows_one_count_with_its_title(client, floor, tenant, today):
    client.force_login(floor["kim"])
    body = client.get("/equipment/").content.decode()
    item = re.search(r'<a href="/security/"[^>]*>.*?</a>', body, re.S)
    assert item and f'<span class="cnt hot" title="{rules.BOTH_BADGE_TITLE}">3</span>' in item.group(0)
    assert body.count(f'title="{rules.BOTH_BADGE_TITLE}"') == 1 and never(item.group(0)) == []
    # advisories alone: a count, never hot
    client.force_login(custom_user(tenant, "it-edit", {Module.CYBERSECURITY: Level.EDIT}))
    page = client.get("/security/models/")  # the Models tab: Device security View (no Equipment View here)
    assert page.status_code == 200
    item = re.search(r'<a href="/security/"[^>]*>.*?</a>', page.content.decode(), re.S)
    assert item and f'<span class="cnt" title="{rules.ADVISORY_BADGE_TITLE}">2</span>' in item.group(0)


def test_the_titles_never_say_the_never_words():
    assert never(" ".join((rules.BADGE_TITLE, rules.ADVISORY_BADGE_TITLE, rules.BOTH_BADGE_TITLE))) == []


@needs_postgres
def test_the_badge_under_the_policies(client, floor, other_tenant):
    """As the runtime role: this facility's advisories only (another facility's open advisory with models to confirm never counts)."""
    with tenant_context(other_tenant):
        make_advisory(number="AD-26-0777", reference="THEIR-ADV-1")
    client.force_login(floor["kim"])
    as_app_role()
    counts = nav_counts(floor["kim"])
    assert (counts["cybersecurity"], counts["cybersecurity_hot"], counts["cybersecurity_title"]) == (3, True, rules.BOTH_BADGE_TITLE)
    assert f'title="{rules.BOTH_BADGE_TITLE}">3</span>' in client.get("/equipment/").content.decode()
