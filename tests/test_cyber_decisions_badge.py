"""
Slice 39 wave 2: Device security's nav badge (apps/reports/services.py _cyber_badge, spread into nav_counts; build39 section 7), on SQLite
and PostgreSQL.

- The count: live security decisions past their review date that still govern a device on site (apps.cybersecurity.rules.badge, the
  Decisions tab's "Decisions past their review date"), hot while there are any, with its title (rules.BADGE_TITLE) beside the count.
- Its gates: only a reader of decisions (Device security View and Equipment View, never a scoped user): nothing for no user, the analyst,
  the requester, the vendor, a scoped role with Full everywhere, a technician without Device security View, or Device security without
  Equipment View.
- One query as decisions grow (rules.badge's aggregate); nav_counts() with no user has no Device security key; never in the Overview's
  attention list; the nav shows the count and its title.
"""
import re
from datetime import timedelta

import pytest
from cyber_fixtures import facts, release
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from pg_helpers import as_app_role, needs_postgres

from apps.accounts.models import DEFAULT_LEVELS, DataScope, Level, Module, Role, User
from apps.cybersecurity import decisions, rules
from apps.cybersecurity.models import AcceptedBy, Connects, DecisionResponse, Network, Segmented
from apps.equipment import services as eq
from apps.equipment.models import Asset, AssetStatus, DeviceModel
from apps.reports.services import _cyber_badge, attention_items, nav_counts

DAY = timedelta(days=1)
KEYS = {"cybersecurity", "cybersecurity_hot", "cybersecurity_title"}


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
    """The pumps on 5.2 (demo), past its security updates, P-1 to P-3 of ours; a decision for 5.2 past its review 5 days ago (the badge's
    one); the manager (Device security Approve) signed in by the tests that need a reader."""
    release(pump_model, connects=Connects.WIRELESS, segmented=Segmented.ALL)
    r52 = release(pump_model, "5.2 (demo)", security_updates_end_on=today - 30 * DAY)
    pumps = [Asset.objects.create(tag=f"P-{i}", device_model=pump_model, department=dept) for i in (1, 2, 3)]
    for p in pumps:
        facts(p, r52)
    lapsed = decisions.record_decision(release=r52, reasons=["security_updates"], response=DecisionResponse.KEEP_WITHOUT_ADDED_CONTROLS,
                                       accepted_by=AcceptedBy.IT_SECURITY, decided_on=today - 200 * DAY, review_by=today - 5 * DAY, by=None,
                                       today=today)
    return {"r52": r52, "pumps": pumps, "lapsed": lapsed, "kim": make_user("manager"), "model": pump_model, "dept": dept}


def test_the_badge_counts_decisions_past_review_that_still_govern_a_device(floor, make_user, today):
    kim = floor["kim"]
    counts = nav_counts(kim)
    assert (counts["cybersecurity"], counts["cybersecurity_hot"], counts["cybersecurity_title"]) == (1, True, rules.BADGE_TITLE)
    assert rules.BADGE_TITLE == "Decisions past their review date"
    assert counts["cybersecurity"] == rules.badge(today)["count"]
    heading = {h.key: h for h in rules.needs_attention(today, user=kim).headings}["review_due"]  # the Decisions tab counts the same
    assert [r.decision.number for r in heading.rows] == [floor["lapsed"].number]
    for slug in ("director", "technician"):  # every default reader of decisions (Device security View and Equipment View)
        assert nav_counts(make_user(slug))["cybersecurity"] == 1, slug
    # renewed: the new decision's review is ahead, so nothing is past review (no count, never hot, no title)
    decisions.record_decision(release=floor["r52"], reasons=["security_updates"], response=DecisionResponse.KEEP_WITHOUT_ADDED_CONTROLS,
                              accepted_by=AcceptedBy.IT_SECURITY, supersedes=floor["lapsed"], by=kim, today=today)
    counts = nav_counts(kim)
    assert (counts["cybersecurity"], counts["cybersecurity_hot"], "cybersecurity_title" in counts) == (None, False, False)


def test_a_lapsed_decision_that_governs_no_device_on_site_is_not_counted(floor, today):
    for p in floor["pumps"]:
        eq.set_status(p, AssetStatus.RETIRED)
    counts = nav_counts(floor["kim"])
    assert (counts["cybersecurity"], counts["cybersecurity_hot"]) == (None, False)  # Needs attention lists it to end instead


@pytest.mark.parametrize("who", ["analyst", "requester", "vendor"])
def test_the_default_roles_without_it_get_no_badge(floor, make_user, who):
    user = make_user(who)
    user.company, user.department = "BD", "ICU"
    user.save()
    assert cyber_keys(nav_counts(user)) == set() and _cyber_badge(user) == {}


def test_the_gates(floor, tenant):
    full = {m: Level.FULL for m in Module.values}
    no_device_security = {**DEFAULT_LEVELS["technician"], Module.CYBERSECURITY: Level.NONE}  # a technician without Device security View
    refused = [
        custom_user(tenant, "companyfull", full, scope=DataScope.COMPANY, company="BD"),
        custom_user(tenant, "unitfull", full, scope=DataScope.DEPARTMENT, department="ICU"),
        custom_user(tenant, "tech-no-cyber", no_device_security),
        custom_user(tenant, "models-only", {Module.CYBERSECURITY: Level.FULL}),  # no Equipment View: a decision can name a device
    ]
    for user in refused:
        assert cyber_keys(nav_counts(user)) == set() and _cyber_badge(user) == {}, user.username
    itsec = custom_user(tenant, "itsec", {Module.CYBERSECURITY: Level.VIEW, Module.EQUIPMENT: Level.VIEW})
    assert _cyber_badge(itsec) == {"cybersecurity": 1, "cybersecurity_hot": True, "cybersecurity_title": rules.BADGE_TITLE}


def test_nav_counts_with_no_user_has_no_device_security_badge(floor):
    assert cyber_keys(nav_counts()) == set() and _cyber_badge(None) == {}


def test_one_query_as_decisions_grow(floor, dept, today):
    """The levels read once (the role's permissions prefetched), the badge is one aggregate query however many decisions."""
    user = User.objects.select_related("role").prefetch_related("role__permissions").get(pk=floor["kim"].pk)

    def counted():
        with CaptureQueriesContext(connection) as q:
            found = _cyber_badge(user)
        return len(q.captured_queries), found["cybersecurity"]

    assert counted() == (1, 1)
    for i in range(4):
        model = DeviceModel.objects.create(manufacturer="Demo", model=f"Bed {i}", description="Bed", category="Beds",
                                           support_ends_on=today - 30 * DAY)
        bed = Asset.objects.create(tag=f"B-{i}", device_model=model, department=dept)
        facts(bed, None, network=Network.WIRELESS)
        scope = {"device_model": model} if i % 2 else {"asset": bed}
        decisions.record_decision(**scope, reasons=["security_updates"], response=DecisionResponse.TAKE_OUT_OF_USE,
                                  accepted_by=AcceptedBy.CE_LEADER, decided_on=today - 60 * DAY, review_by=today - DAY, by=None, today=today)
        if not i % 2:  # and the model's own, in force (never counted)
            decisions.record_decision(device_model=model, reasons=["security_updates"], response=DecisionResponse.TAKE_OUT_OF_USE,
                                      accepted_by=AcceptedBy.CE_LEADER, by=None, today=today)
    assert counted() == (1, 5)


def test_never_in_the_overview_attention_list(floor):
    items = attention_items()
    assert not [it for it in items if floor["lapsed"].number in str(it) or rules.BADGE_TITLE in str(it) or "5.2 (demo)" in str(it)]


def test_the_nav_shows_the_count_with_its_title(client, floor, tenant):
    client.force_login(floor["kim"])
    body = client.get("/equipment/").content.decode()
    item = re.search(r'<a href="/security/"[^>]*>.*?</a>', body, re.S)
    assert item and f'<span class="cnt hot" title="{rules.BADGE_TITLE}">1</span>' in item.group(0)
    assert body.count(f'title="{rules.BADGE_TITLE}"') == 1  # no other nav item carries it
    client.force_login(custom_user(tenant, "tech-no-cyber", {**DEFAULT_LEVELS["technician"], Module.CYBERSECURITY: Level.NONE}))
    body = client.get("/equipment/").content.decode()
    assert '<a href="/security/"' not in body and rules.BADGE_TITLE not in body


@needs_postgres
def test_the_badge_under_the_policies(client, floor):
    client.force_login(floor["kim"])
    as_app_role()
    counts = nav_counts(floor["kim"])
    assert (counts["cybersecurity"], counts["cybersecurity_hot"]) == (1, True)
    assert f'title="{rules.BADGE_TITLE}">1</span>' in client.get("/equipment/").content.decode()
