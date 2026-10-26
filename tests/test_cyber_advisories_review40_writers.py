"""
Slice 40 review fixes, the writers' refusal words (apps.cybersecurity.advisories, apps.cybersecurity.decisions), on SQLite and PostgreSQL:

- Finding 7: Edit advisory's day received after a decision's day decided (live or ended) reads the earliest decision's number and day
  only to a reader of decisions (permissions.can_view_devices, or the system); anyone else reads RECEIVED_AFTER_DECISION_UNREAD, with no
  number and no day, through the service and through the Edit modal.
- Smoke S1: network_removed's refusal (decisions.network_refusal) words a device read as its model discloses (can connect, or does not
  connect, with no fact of its own) apart from one with nothing recorded at all; connected ones first, then disclosed, then none; up to
  five tags; the same words in the Record decision modal before it is tried, in its POST, and in record_decision's refusal; fixed
  queries, and no lock from the modal's GET (PostgreSQL outside any transaction).
- Every word added scanned with rules.DECISION_NEVER_WORDS.
"""
import re
from datetime import timedelta
from html import unescape
from types import SimpleNamespace

import pytest
from cyber_fixtures import facts, make_advisory, model_row, release
from django.core.exceptions import ValidationError
from django.db import connection
from django.db.models.query import QuerySet
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from pg_helpers import needs_postgres

from apps.accounts.models import Level, Module, Role, User
from apps.cybersecurity import advisories, decisions, rules
from apps.cybersecurity.models import AcceptedBy, AppliesTo, Connects, DecisionResponse, Network
from apps.equipment.models import Asset, DeviceModel

MODAL = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "modal-card"}
DAY = timedelta(days=1)
KEEP_WITH, KEEP_WITHOUT = DecisionResponse.KEEP_WITH_CONTROLS, DecisionResponse.KEEP_WITHOUT_ADDED_CONTROLS
NETWORK = ["network_removed", "physical_controls"]


def never(text: str) -> list:
    return [w for w in rules.DECISION_NEVER_WORDS if re.search(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", unescape(text), re.IGNORECASE)]


def said(text: str) -> str:
    """Every word these tests read, scanned as it is read."""
    assert never(text) == [], text
    return text


def html(r) -> str:
    return said(unescape(r.content.decode()))


def refused(fn, *args, **kwargs) -> dict:
    with pytest.raises(ValidationError) as e:
        fn(*args, **kwargs)
    out = {k: v[0] if len(v) == 1 else v for k, v in e.value.message_dict.items()}
    for words in out.values():
        said(words)
    return out


def errors(body: str, prefix: str) -> dict:
    """{field: its error} from a modal (ids "<prefix>-<name>_error")."""
    return {m.group(1): unescape(m.group(2)) for m in re.finditer(rf'<span class="err" id="{prefix}-(\w+)_error">([^<]*)</span>', body)}


def custom_user(tenant, slug, levels) -> User:
    role = Role.objects.create(name=slug.title(), slug=slug)
    role.set_levels(levels)
    return User.objects.create_user(username=f"{slug}@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role,
                                    first_name=slug.title(), last_name="Person")


@pytest.fixture
def floor(ctx, dept, pump_model):
    """The pump's profile discloses it can connect (wireless); P-1, P-2, P-3 of ours on 5.2 (demo), with no fact of their own; 5.6 (demo)
    the fix. The advisory, received 10 days ago, names the pump on 5.2 (demo)."""
    today = timezone.localdate()
    release(pump_model, "", connects=Connects.WIRELESS)
    r52, r56 = release(pump_model, "5.2 (demo)"), release(pump_model, "5.6 (demo)")
    d = {tag: Asset.objects.create(tag=tag, device_model=pump_model, department=dept) for tag in ("P-1", "P-2", "P-3")}
    for a in d.values():
        facts(a, r52)
    adv = make_advisory(received_on=today - 10 * DAY, reference="DEMO-ADV-0001")
    model_row(adv, pump_model, releases=[r52], fix=r56)
    return SimpleNamespace(today=today, model=pump_model, r52=r52, r56=r56, d=d, dept=dept, adv=adv)


def decide(floor, **fields):
    values = {"applies_to": AppliesTo.ADVISORY, "advisory": floor.adv, "response": KEEP_WITHOUT, "accepted_by": AcceptedBy.CE_LEADER,
              "by": None, "today": floor.today, **fields}
    return decisions.record_decision(**values)


# --- finding 7: the day received after a decision's, by reader -------------------------------------------------------------------------

READER = {Module.CYBERSECURITY: Level.EDIT, Module.EQUIPMENT: Level.VIEW}  # the README's IT security role
NOT_READERS = {"edit": {Module.CYBERSECURITY: Level.EDIT}, "approve": {Module.CYBERSECURITY: Level.APPROVE},
               "full": {Module.CYBERSECURITY: Level.FULL}}


def test_the_day_received_after_a_decision_names_it_only_to_a_reader_of_decisions(floor, tenant):
    on52 = decide(floor, release=floor.r52, decided_on=floor.today - 3 * DAY)
    own = decide(floor, asset=floor.d["P-1"], decided_on=floor.today - 5 * DAY)  # decided earlier: the earliest is named
    first_day = rules.day_words(floor.today - 5 * DAY)
    named = advisories.RECEIVED_AFTER_DECISION.format(decision=own.number, day=first_day)
    assert named == f"{own.number} under this advisory was decided {first_day}: the day received cannot be after it."
    later = {"received_on": floor.today - 4 * DAY, "today": floor.today}
    assert refused(advisories.update_advisory, floor.adv, by=None, **later) == {"received_on": named}
    reader = custom_user(tenant, "itsec", READER)
    assert refused(advisories.update_advisory, floor.adv, by=reader, **later) == {"received_on": named}
    for slug, levels in NOT_READERS.items():
        user = custom_user(tenant, slug, levels)
        got = refused(advisories.update_advisory, floor.adv, by=user, **later)
        assert got == {"received_on": advisories.RECEIVED_AFTER_DECISION_UNREAD}, slug
        assert own.number not in got["received_on"] and on52.number not in got["received_on"] and first_day not in got["received_on"]
        assert not re.search(r"\d", got["received_on"])  # no number, no day
    # an ended decision still holds the day received back, in the same two readers' words
    decisions.mark_entered_in_error(own, by=None, today=floor.today)
    decisions.mark_entered_in_error(on52, by=None, today=floor.today)
    assert refused(advisories.update_advisory, floor.adv, by=reader, **later) == {"received_on": named}
    assert refused(advisories.update_advisory, floor.adv, by=custom_user(tenant, "edit-2", NOT_READERS["edit"]), **later) == {
        "received_on": advisories.RECEIVED_AFTER_DECISION_UNREAD}
    # a day on or before the earliest decision's is saved, whoever saves it
    saved = advisories.update_advisory(floor.adv, received_on=floor.today - 5 * DAY, by=User.objects.get(username="edit@riverside.example"),
                                       today=floor.today)
    assert saved.received_on == floor.today - 5 * DAY


def test_the_edit_modal_words_it_by_reader(client, floor, tenant):
    d = decide(floor, release=floor.r52, decided_on=floor.today - 3 * DAY)
    day = rules.day_words(floor.today - 3 * DAY)
    url = f"/security/advisories/{floor.adv.number}/edit/"
    data = {"received_on": (floor.today - DAY).isoformat(), "cves": ""}
    client.force_login(custom_user(tenant, "edit", NOT_READERS["edit"]))
    r = client.post(url, data, **MODAL)
    body = html(r)
    assert r.status_code == 200 and errors(body, "ad")["received_on"] == advisories.RECEIVED_AFTER_DECISION_UNREAD
    assert d.number not in body and day not in body
    client.force_login(custom_user(tenant, "itsec", READER))
    body = html(client.post(url, data, **MODAL))
    assert errors(body, "ad")["received_on"] == advisories.RECEIVED_AFTER_DECISION.format(decision=d.number, day=day)
    floor.adv.refresh_from_db()
    assert floor.adv.received_on == floor.today - 10 * DAY  # nothing saved


# --- smoke S1: network_removed's refusal by where the connection comes from --------------------------------------------------------

def one_disclosed(tag: str, disclosed: str) -> str:
    return (f"{tag} is recorded as its model discloses ({disclosed}), with no fact of its own here: record Not connected here on it first "
            "(Record facts), or choose other controls.")


def many_disclosed(tags: str, disclosed: str = "") -> str:
    part = f" ({disclosed})" if disclosed else ""
    return (f"{tags} are recorded as their model discloses{part}, with no fact of their own here: record Not connected here on them "
            "first (Record facts), or choose other controls.")


def refusal(devices, today) -> str:
    """network_refusal over the devices read again, in the order given (an instance keeps the record it cached when facts() made it)."""
    rows = Asset.objects.in_bulk([a.pk for a in devices])
    return said(decisions.network_refusal([rows[a.pk] for a in devices], today))


def test_network_refusal_words_a_disclosed_connection_apart_from_none_recorded(floor):
    today, d = floor.today, floor.d
    assert refusal([d["P-1"]], today) == one_disclosed("P-1", "can connect, wireless")
    assert refusal(list(d.values()), today) == many_disclosed("P-1, P-2, P-3", "can connect, wireless")
    assert refusal([d["P-1"]], today) == decisions.NETWORK_DISCLOSED.format(tag="P-1", disclosed="can connect, wireless")
    # a release disclosing otherwise: several readings, no disclosure named
    r54 = release(floor.model, "5.4 (demo)", connects=Connects.WIRED)
    d["P-4"] = Asset.objects.create(tag="P-4", device_model=floor.model, department=floor.dept)
    facts(d["P-4"], r54)
    assert refusal([d["P-4"]], today) == one_disclosed("P-4", "can connect, wired")
    assert refusal([d["P-1"], d["P-4"]], today) == many_disclosed("P-1, P-4")
    both = release(floor.model, "5.5 (demo)", connects=Connects.BOTH)
    d["P-5"] = Asset.objects.create(tag="P-5", device_model=floor.model, department=floor.dept)
    facts(d["P-5"], both)
    assert refusal([d["P-5"]], today) == one_disclosed("P-5", "can connect, wired and wireless")
    # a model disclosing it does not connect is read as disclosed too, never as "not recorded" nor as its own fact
    scale = DeviceModel.objects.create(manufacturer="Welch Allyn", model="Scale 1", description="Scale", category="Scales")
    release(scale, "", connects=Connects.NONE)
    s1 = Asset.objects.create(tag="S-1", device_model=scale, department=floor.dept)
    assert refusal([s1], today) == one_disclosed("S-1", "does not connect")
    # nothing recorded at all: the words as before
    bed = DeviceModel.objects.create(manufacturer="Stryker", model="Bed X", description="Bed", category="Beds")
    b1 = Asset.objects.create(tag="B-1", device_model=bed, department=floor.dept)
    b2 = Asset.objects.create(tag="B-2", device_model=bed, department=floor.dept)
    assert refusal([b1], today) == decisions.NETWORK_NOT_RECORDED.format(tag="B-1") == (
        "B-1 has no connection recorded here: record Not connected here on it first (Record facts), or choose other controls.")
    assert refusal([b1, b2], today) == decisions.NETWORK_NOT_RECORDED_MANY.format(tags="B-1, B-2")
    # the order: connected first, then disclosed, then none recorded
    assert refusal([b1, d["P-1"]], today) == one_disclosed("P-1", "can connect, wireless")
    facts(d["P-2"], floor.r52, network=Network.WIRELESS)
    assert refusal([b1, d["P-1"], d["P-2"]], today) == decisions.NETWORK_CONNECTED.format(tag="P-2", connection="connected (wireless)")
    # a device's own "Not connected here" needs nothing; its own fact is never read as the disclosure
    facts(d["P-3"], floor.r52, network=Network.NOT_CONNECTED)
    facts(s1, network=Network.NOT_CONNECTED)
    assert refusal([d["P-3"], s1], today) == ""
    assert refusal([d["P-3"], d["P-1"]], today) == one_disclosed("P-1", "can connect, wireless")


def test_up_to_five_tags(floor):
    many = [Asset.objects.create(tag=f"Q-{i}", device_model=floor.model, department=floor.dept) for i in range(7)]
    for a in many:
        facts(a, floor.r52)
    assert refusal(many, floor.today) == many_disclosed("Q-0, Q-1, Q-2, Q-3, Q-4 and 2 more", "can connect, wireless")


def test_network_refusal_reads_the_standings_in_fixed_queries(floor):
    more = [Asset.objects.create(tag=f"Q-{i}", device_model=floor.model, department=floor.dept) for i in range(6)]
    for a in more:
        facts(a, floor.r52)
    counts = []
    for devices in ([floor.d["P-1"]], list(Asset.objects.filter(tag__startswith="Q-"))):
        devices = list(Asset.objects.filter(pk__in=[a.pk for a in devices]))
        with CaptureQueriesContext(connection) as q:
            refusal(devices, floor.today)
        counts.append(len(q.captured_queries))
    assert counts[0] == counts[1]


def test_record_decision_refuses_in_the_same_words(floor):
    keep = {"response": KEEP_WITH, "controls": NETWORK}
    assert refused(decide, floor, asset=floor.d["P-1"], **keep) == {"controls": one_disclosed("P-1", "can connect, wireless")}
    assert refused(decide, floor, release=floor.r52, **keep) == {"controls": many_disclosed("P-1, P-2, P-3", "can connect, wireless")}
    for a in floor.d.values():
        facts(a, floor.r52, network=Network.NOT_CONNECTED)
    assert decide(floor, release=floor.r52, **keep).controls == NETWORK


def test_the_modal_says_it_before_it_is_tried_and_after(client, make_user, floor):
    client.force_login(make_user("director"))
    words = many_disclosed("P-1, P-2, P-3", "can connect, wireless")
    url = f"/security/decisions/new/?advisory={floor.adv.number}&release={floor.r52.pk}"
    r = client.get(url, **MODAL)
    body = html(r)
    assert r.status_code == 200 and r.context["network_note"] == words and f"data-network-note>{words}</span>" in body
    body = html(client.post(url, {"response": KEEP_WITH, "controls": ["network_removed"], "accepted_by": AcceptedBy.CE_LEADER,
                                  "decided_on": floor.today.isoformat()}, **MODAL))
    assert errors(body, "sd")["controls"] == words
    r = client.get(f"/security/decisions/new/?advisory={floor.adv.number}&asset=P-1", **MODAL)
    assert said(r.context["network_note"]) == one_disclosed("P-1", "can connect, wireless")


@pytest.mark.django_db
def test_the_modal_takes_no_lock(client, make_user, floor, monkeypatch):
    """Both databases: the Record decision modal's GET reads network_refusal without select_for_update."""
    client.force_login(make_user("director"))
    locked = []
    real = QuerySet.select_for_update
    monkeypatch.setattr(QuerySet, "select_for_update", lambda self, *a, **k: locked.append(self.model.__name__) or real(self, *a, **k))
    r = client.get(f"/security/decisions/new/?advisory={floor.adv.number}&release={floor.r52.pk}", **MODAL)
    assert r.status_code == 200 and said(r.context["network_note"]).startswith("P-1, P-2, P-3 are recorded as their model discloses")
    assert locked == []


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_the_modal_opens_outside_a_transaction(client, make_user, floor):
    """No test transaction wraps the request, as none wraps a GET in production: a FOR UPDATE here would raise on PostgreSQL."""
    client.force_login(make_user("director"))
    for query, words in ((f"release={floor.r52.pk}", many_disclosed("P-1, P-2, P-3", "can connect, wireless")),
                         ("asset=P-1", one_disclosed("P-1", "can connect, wireless"))):
        r = client.get(f"/security/decisions/new/?advisory={floor.adv.number}&{query}", **MODAL)
        assert r.status_code == 200 and said(r.context["network_note"]) == words


# --- words --------------------------------------------------------------------------------------------------------------------------

def test_every_word_added_is_clean():
    """The constants added (every refusal and page read above is scanned as it is read: said)."""
    added = [advisories.RECEIVED_AFTER_DECISION, advisories.RECEIVED_AFTER_DECISION_UNREAD, decisions.NETWORK_DISCLOSED,
             decisions.NETWORK_DISCLOSED_MANY, *decisions.DISCLOSED_WORDS.values()]
    assert [t for t in added if never(t)] == []
