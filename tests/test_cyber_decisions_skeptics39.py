"""
Slice 39 skeptics' fixes (the rules, the decision service, the decision pages), on SQLite and PostgreSQL:

- S3: a refusal says what it means (decisions.Refusal.kind): End answers a decision being renewed only when nothing in its scope needs
  one now (gone, not needed), never when a fact is to be recorded first (unknown). The renew modal offers End only then; a decision's
  page says why Review and renew is not offered (End primary only then); Needs attention offers neither for a decision whose devices'
  support or connection is not recorded (rules.need_unknown), as refusal_for reads it.
- S5, S6: a MAC is refused within a token (joined on by - # _ / . :), and a ticket of decimal digits in a MAC's layout is not (a MAC needs
  a hex letter in the layouts a digits-only ticket takes); a UUID in its own layout is a record's identifier.
- S7: a release's or a model's devices whose manufacturer's updates continue past 12 months (the OS ended) are refused in those words.
- S8: a model whose parts ended (its support continuing) is never worded as its support ended: the heading and the reason name parts.
- S9: Needs attention's row links to the model's panel only where the panel lists the row's devices and no other (a device under its
  own decision would be listed; the final round's T1, test_cyber_decisions_final39: any release's); a reader of decisions who may not
  end one is never told to "end it first".
"""
from datetime import date, timedelta
from html import unescape

import pytest
from cyber_fixtures import facts, release
from test_cyber_decisions_web import custom_user, decide, floor, signed_in  # noqa: F401 (fixtures)

from apps.accounts.models import Level, Module
from apps.cybersecurity import decisions, filters, rules, services
from apps.cybersecurity.models import DeviceSecurityDecision, Network, OsFamily
from apps.equipment.models import Asset, AssetStatus, DeviceModel

MODAL = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "modal-card"}
HX = {"HTTP_HX_REQUEST": "true"}
DAY = timedelta(days=1)


def body_of(r) -> str:
    assert r.status_code == 200
    return unescape(r.content.decode())


def lapsed(floor, **scope) -> DeviceSecurityDecision:  # noqa: F811
    """A decision decided 400 days ago, past its review (10 days ago)."""
    return decide(**scope, decided_on=floor.today - 400 * DAY, review_by=floor.today - 10 * DAY)


def attention(today, user=None) -> dict:
    return {h.key: h for h in rules.needs_attention(today, user=user).headings}


def end_url(d) -> str:
    return f"/security/decisions/{d.number}/end/"


# --- S3: End only when nothing needs a decision; never for a fact not recorded ------------------------------------------------------

@pytest.mark.django_db
def test_each_refusal_says_what_it_means(floor, dept, vent_model):  # noqa: F811
    p = floor.d
    today = floor.today
    kind = {}
    v1 = Asset.objects.create(tag="V-1", device_model=vent_model, department=dept)  # nothing recorded: its connection unknown
    kind["connection"] = decisions.refusal_for(asset=v1, today=today)
    facts(p["P-4"], None)  # no release: the profile says it can connect, nothing of its support
    kind["no_release"] = decisions.refusal_for(asset=p["P-4"], today=today)
    facts(p["P-4"], release(floor.model, "5.8 (demo)"))  # a release with no date of its own
    kind["no_date"] = decisions.refusal_for(asset=p["P-4"], today=today)
    facts(p["P-4"], floor.r56)
    kind["supported"] = decisions.refusal_for(asset=p["P-4"], today=today)
    facts(p["P-3"], floor.r52, network=Network.NOT_CONNECTED)
    kind["not_connected"] = decisions.refusal_for(asset=p["P-3"], today=today)
    ext = release(floor.model, "6.0 (demo)", os_family=OsFamily.WINDOWS, os_name="Windows 7", os_support_ends_on=date(2020, 1, 14),
                  security_updates_end_on=today + 900 * DAY)
    facts(p["P-2"], ext)
    kind["extended"] = decisions.refusal_for(asset=p["P-2"], today=today)
    on52 = decide(release=floor.r52)
    kind["in_force"] = decisions.refusal_for(release=floor.r52, today=today)
    decisions.end_decision(on52, by=None, today=today)
    kind["ended"] = decisions.refusal_for(release=floor.r52, supersedes=on52.number, today=today)
    Asset.objects.filter(pk=p["P-1"].pk).update(status=AssetStatus.RETIRED)
    kind["retired"] = decisions.refusal_for(asset=p["P-1"], today=today)
    unused = DeviceModel.objects.create(manufacturer="Acme", model="Unused 1", description="Monitor", category="Monitors")
    kind["no_device"] = decisions.refusal_for(device_model=unused, today=today)
    assert {k: r.kind for k, r in kind.items()} == {
        "connection": decisions.KIND_UNKNOWN, "no_release": decisions.KIND_UNKNOWN, "no_date": decisions.KIND_UNKNOWN,
        "supported": decisions.KIND_NOT_NEEDED, "not_connected": decisions.KIND_NOT_NEEDED, "extended": decisions.KIND_NOT_NEEDED,
        "in_force": decisions.KIND_IN_FORCE, "ended": decisions.KIND_ENDED, "retired": decisions.KIND_GONE, "no_device": decisions.KIND_GONE}
    assert [k for k, r in kind.items() if r.ends_it] == ["supported", "not_connected", "extended", "retired", "no_device"]
    assert kind["no_release"].words == decisions.NO_RELEASE_RECORDED.format(tag="P-4", version="")
    assert kind["in_force"].live == on52


@pytest.fixture
def unrecorded(floor):  # noqa: F811
    """The skeptic's scenario: a device decision for P-1 and a release decision for 5.2 (P-2, P-3), both past review; then P-1's release
    record cleared (Record facts) and the end of 5.2's security updates cleared (pending the manufacturer's letter)."""
    own = lapsed(floor, asset=floor.d["P-1"])
    on52 = lapsed(floor, release=floor.r52)
    facts(floor.d["P-1"], None)
    floor.r52.security_updates_end_on = None
    floor.r52.save()
    return own, on52


@pytest.mark.django_db
def test_a_fact_not_recorded_is_never_answered_by_end(floor, unrecorded):  # noqa: F811
    own, on52 = unrecorded
    p1_words = "P-1: its release is not recorded, nor the end of its security updates: record its release (Record facts) first."
    on52_words = ("2 devices of ours in this scope are not recorded enough to tell whether they need a decision (P-2, P-3). P-2: the end "
                  "of the manufacturer's security updates for 5.2 (demo) is not recorded: record it on the release (Models) first.")
    for d, words in ((own, p1_words), (on52, on52_words)):
        refusal = decisions.refusal_for(**({"asset": d.asset} if d.asset_id else {"release": d.release}), supersedes=d.number)
        assert (refusal.words, refusal.kind, refusal.ends_it) == (words, decisions.KIND_UNKNOWN, False)
    found = attention(floor.today)
    assert [(r.decision, r.record, r.end) for r in found["review_due"].rows] == [(own, None, None), (on52, None, None)]
    # recorded again: Review and renew for the one whose need is back; End for the one whose devices need none (updated)
    floor.r52.security_updates_end_on = floor.today - 30 * DAY
    floor.r52.save()
    facts(floor.d["P-1"], floor.r56)
    rows = {r.decision: r for r in attention(floor.today)["review_due"].rows}
    assert (rows[on52].record, rows[on52].end) == (rules.Link("web:security_decision_new", (), (("supersedes", on52.number),)), None)
    assert (rows[own].record, rows[own].end) == (None, rules.Link("web:security_decision_end", (own.number,)))


@pytest.mark.django_db
def test_the_renew_modal_offers_end_only_when_nothing_needs_one(client, signed_in, floor, unrecorded):  # noqa: F811
    own, on52 = unrecorded
    signed_in("manager")  # Device security Approve, Equipment Edit
    for d in (own, on52):
        body = body_of(client.get(f"/security/decisions/new/?supersedes={d.number}", **MODAL))
        assert "<form" not in body and "record" in body.split('role="alert">')[1].split("</div>")[0]
        assert end_url(d) not in body and f"End {d.number}" not in body
    facts(floor.d["P-1"], floor.r56)  # updated to a supported release: nothing needs it
    body = body_of(client.get(f"/security/decisions/new/?supersedes={own.number}", **MODAL))
    assert decisions.NOT_NEEDED.format(name="P-1") in body and f'hx-get="{end_url(own)}"' in body and f"End {own.number}" in body


@pytest.mark.django_db
def test_the_decision_page_says_why_review_and_renew_is_not_offered(client, signed_in, floor, unrecorded):  # noqa: F811
    own, _on52 = unrecorded
    signed_in("manager")
    page = body_of(client.get(f"/security/decisions/{own.number}/", **HX))
    words = "P-1: its release is not recorded, nor the end of its security updates: record its release (Record facts) first."
    assert f"Review and renew is not offered now: {words}" in page
    assert "Review and renew</button>" not in page and "supersedes=" not in page
    assert f'<button class="btn" type="button" hx-get="{end_url(own)}"' in page  # End stays, never the main action here
    facts(floor.d["P-1"], floor.r56)  # nothing needs it now: End is the main action
    page = body_of(client.get(f"/security/decisions/{own.number}/", **HX))
    assert f"Review and renew is not offered now: {decisions.NOT_NEEDED.format(name='P-1')}" in page
    assert f'<button class="btn primary" type="button" hx-get="{end_url(own)}"' in page and "Review and renew</button>" not in page
    floor.r52.security_updates_end_on = floor.today - 30 * DAY  # recorded again, and P-1 back on it: past its support
    floor.r52.save()
    facts(floor.d["P-1"], floor.r52)  # Review and renew, no words
    page = body_of(client.get(f"/security/decisions/{own.number}/", **HX))
    assert f"supersedes={own.number}" in page and "Review and renew</button>" in page and "is not offered now" not in page
    assert f'<button class="btn" type="button" hx-get="{end_url(own)}"' in page
    signed_in("technician")  # Device security Edit: reads it, never an action or the refusal
    page = body_of(client.get(f"/security/decisions/{own.number}/", **HX))
    assert "is not offered now" not in page and end_url(own) not in page


# --- S5, S6: network identifiers within a token; tickets of decimal digits ----------------------------------------------------------

MAC_JOINED = ["EX-001A-2B3C-4D5E", "FW-00-1A-2B-3C-4D-5E", "MAC-001A2B3C4D5E", "HOST-001A.2B3C.4D5E", "001A2B3C4D5E#1",
              "00:11:22:33:44:55", "00-11-22-33-44-55"]
DIGIT_TICKETS = ["2026-1021-0042", "202610-000042", "2026_1021_0042", "000123-000456", "100200-300400", "0011-2233-4455",
                 "00_11_22_33_44_55", "123456789012", "20261021-000042", "CHG-2026-1021-0042", "CRQ000000012345", "2026-10-21-01",
                 "INC-2026-0042", "CHG0012345", "RITM0012345", "EX-1042",
                 # a UUID in its own layout, alone or joined on; a longer hex run is never a MAC
                 "3f2504e0-4f89-11d3-9a0c-0305e82c3301", "GRC-3f2504e0-4f89-11d3-9a0c-0305e82c3301", "EX-001A2B3C4D5E6F"]


@pytest.mark.django_db
def test_a_mac_within_a_token_is_refused_and_a_ticket_of_digits_is_not(floor):  # noqa: F811
    for text in MAC_JOINED:
        errors: dict = {}
        decisions._token(text, "exception_reference", errors)
        assert decisions.network_identifier(text) and errors == {"exception_reference": decisions.NOT_AN_ADDRESS}, text
    assert decisions.network_identifier("X-3f2504e0-4f89-11d3-9a0c-0305e82c3301-001A2B3C4D5E")  # a MAC after a UUID (over 40 characters)
    for text in DIGIT_TICKETS:
        errors = {}
        assert not decisions.network_identifier(text) and decisions._token(text, "exception_reference", errors) == text, text
        assert errors == {}, text
    with pytest.raises(Exception) as e:
        decide(asset=floor.d["P-1"], response="keep_with_controls", controls=["recorded_elsewhere"], controls_reference="FW-00-1A-2B-3C-4D-5E",
               exception_reference="EX-001A-2B3C-4D5E")
    assert e.value.message_dict == {"controls_reference": [decisions.NOT_AN_ADDRESS], "exception_reference": [decisions.NOT_AN_ADDRESS]}
    kept = decide(asset=floor.d["P-1"], exception_reference="2026-1021-0042")
    assert DeviceSecurityDecision.objects.get(pk=kept.pk).exception_reference == "2026-1021-0042"


# --- S7: the manufacturer's updates continuing, for a release's or a model's devices ------------------------------------------------

@pytest.mark.django_db
def test_a_scope_whose_updates_continue_is_refused_in_those_words(floor):  # noqa: F811
    later = date(floor.today.year + 3, 3, 31)
    r60 = release(floor.model, "6.0 (demo)", os_family=OsFamily.WINDOWS, os_name="Windows 7", os_support_ends_on=date(2020, 1, 14),
                  security_updates_end_on=later)
    for tag in ("P-1", "P-2", "P-3", "P-4"):
        facts(floor.d[tag], r60)
    words = (f"P-1, P-2, P-3, P-4: the manufacturer's security updates continue (announced to end {rules.day_words(later)}): a decision "
             "can be recorded once their end is within 12 months.")
    for scope, field in (({"release": r60}, "release"), ({"device_model": floor.model}, "device_model")):
        refusal = decisions.refusal_for(**scope, today=floor.today)
        assert (refusal.words, refusal.kind) == (words, decisions.KIND_NOT_NEEDED), scope
        with pytest.raises(Exception) as e:
            decide(**scope)
        assert e.value.message_dict == {field: [words]}
    # the earliest announced end, for the devices it is announced for
    even_later = date(floor.today.year + 4, 3, 31)
    facts(floor.d["P-4"], release(floor.model, "6.1 (demo)", os_family=OsFamily.WINDOWS, os_support_ends_on=date(2020, 1, 14),
                                  security_updates_end_on=even_later))
    assert decisions.refusal_for(device_model=floor.model, today=floor.today).words == words.replace("P-1, P-2, P-3, P-4:", "P-1, P-2, P-3:")
    assert not [w for w in rules.DECISION_NEVER_WORDS if w.casefold() in words.casefold()]


# --- S8: parts ended, support continuing ------------------------------------------------------------------------------------------

@pytest.mark.django_db
def test_a_model_whose_parts_ended_is_never_said_past_its_support(floor):  # noqa: F811
    DeviceModel.objects.filter(pk=floor.model.pk).update(parts_end_on=floor.today - 60 * DAY, support_ends_on=floor.today + 1400 * DAY)
    bare = release(floor.model, "2.0 (demo)")  # no date of its own: the model's support (slice 36)
    facts(floor.d["P-4"], bare)
    heading = attention(floor.today)["no_decision"]
    assert heading.words == ("Security updates, manufacturer support or parts, or OS support ended, connected or able to connect: no "
                             "decision recorded")
    on20 = next(r for r in heading.rows if "2.0 (demo)" in r.words)
    assert "parts no longer available after" in on20.words
    d = decide(asset=floor.d["P-4"])
    assert rules.reason_words(d) == ["The manufacturer's security updates, support, or parts ended, or their end is announced"]
    assert rules.ALSO_ENDED[rules.SECURITY_UPDATES] == ("The manufacturer's security updates, support, or parts have ended, and this "
                                                        "decision does not name them")


# --- S9: the row's link lists what the row counts; who may end a decision ---------------------------------------------------------

def _listed(link, floor, today) -> list:  # noqa: F811
    shown = filters.parse(dict(link.query))
    read = rules.standings(rules.on_site().filter(device_model=floor.model), today)
    return sorted(s.asset.tag for s in read.values() if shown.keeps(s))


@pytest.mark.django_db
def test_a_rows_link_never_lists_a_device_it_does_not_count(floor):  # noqa: F811
    floor.profile.security_updates_end_on = floor.today - 30 * DAY  # what a device with no release reads
    floor.profile.save()
    for tag in ("P-3", "P-4"):
        facts(floor.d[tag], None)
    own = decide(asset=floor.d["P-4"])  # its own decision
    on52 = decide(release=floor.r52)  # P-1, P-2
    (row,) = attention(floor.today)["no_decision"].rows
    assert row.words.startswith("Alaris 8015 PCU, release not recorded: 1 device of ours; ") and row.tags == ("P-3",)
    assert row.link == rules.Link("web:asset", ("P-3",), (("tab", "security"),))  # the panel would list P-4 too
    p5 = Asset.objects.create(tag="P-5", device_model=floor.model, department=floor.dept)
    facts(p5, None)
    (row,) = attention(floor.today)["no_decision"].rows
    assert row.tags == ("P-3", "P-5") and row.link is None
    decisions.end_decision(own, by=None, today=floor.today)  # P-4 counted again: the panel lists them
    (row,) = attention(floor.today)["no_decision"].rows
    assert row.tags == ("P-3", "P-4", "P-5") and _listed(row.link, floor, floor.today) == ["P-3", "P-4", "P-5"]
    # a release's row: one of its devices under its own decision
    facts(floor.d["P-4"], floor.r56)
    p6 = Asset.objects.create(tag="P-6", device_model=floor.model, department=floor.dept)
    facts(p6, floor.r52)
    for tag in ("P-3", "P-5"):
        facts(Asset.objects.get(tag=tag), floor.r52)
    decisions.end_decision(on52, by=None, today=floor.today)
    (row,) = attention(floor.today)["no_decision"].rows
    assert row.tags == ("P-1", "P-2", "P-3", "P-5", "P-6") and _listed(row.link, floor, floor.today) == list(row.tags)
    decide(asset=floor.d["P-2"])
    (row,) = attention(floor.today)["no_decision"].rows
    assert row.tags == ("P-1", "P-3", "P-5", "P-6") and row.link is None


@pytest.mark.django_db
def test_the_decisions_tab_shows_an_unlinked_row_as_words(client, signed_in, floor):  # noqa: F811
    decide(asset=floor.d["P-2"])
    signed_in("manager")
    r = client.get("/security/decisions/")
    (heading,) = [h for h in r.context["attention"]["headings"] if h["key"] == "no_decision"]
    (row,) = heading["rows"]
    assert row["url"] == "" and row["record_url"] == f"/security/decisions/new/?release={floor.r52.pk}"
    body = unescape(r.content.decode())
    assert f'<div class="cy-up-s">{row["words"]}</div>' in body


@pytest.mark.django_db
def test_a_reader_who_may_not_end_a_decision_is_never_told_to_end_it(tenant, floor, make_user):  # noqa: F811
    lone = release(floor.model, "4.0 (demo)", security_updates_end_on=floor.today - DAY)
    facts(floor.d["P-4"], lone)
    d = decide(release=lone)
    facts(floor.d["P-4"], floor.r56)
    from django.test import Client

    it = custom_user(Client(), tenant, "it-security", {Module.CYBERSECURITY: Level.EDIT, Module.EQUIPMENT: Level.VIEW}, login=False)
    assert services.removal_refusal(lone, by=it) == (f"{d.number} is decided for this release: ending it needs Device security Approve and "
                                                    "Equipment View.")
    assert services.removal_refusal(lone, by=make_user("manager")) == f"{d.number} is decided for this release: end it first."
    with pytest.raises(Exception) as e:
        services.remove_release(lone, by=it)
    assert e.value.message_dict == {"release": [services.DECISION_LIVE_READ.format(number=d.number)]}
