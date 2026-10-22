"""
Slice 39 final fixes (the last skeptics' round; labels as in its findings), on SQLite and PostgreSQL:

- T2: "not recorded" is judged against the reasons of the decision being renewed (rules.need_unknown(standing, reasons)): a security
  updates reason while the end of the manufacturer's security updates is not recorded, an OS support reason while the OS vendor's support
  is not recorded, whatever the other line records (a profile recording the OS for every version). refusal_for (supersedes given), Needs
  attention, and the decision's page never offer End for it: they say what to record first. A new decision keeps the both-blank rule.
- T0: a release's or a model's decision reads its reasons (those the modal offers and the service requires) and "What holds now" (the
  modal's and the page's) from the devices it would govern (decisions.would_govern: with the new decision among the live ones), never
  from devices a more specific live decision governs; a scope whose devices that need one are all governed elsewhere is refused in words
  (End then answers a decision being renewed).
- T1: a Needs attention row links to the model's panel only where the panel, with exactly the link's filters, lists the row's devices and
  no other (every release's devices of that standing, decided or not, and an announced end past 12 months included); else a one-device
  row links to the device's Security tab, a larger one to nothing.
"""
from datetime import date, timedelta
from html import unescape

import pytest
from cyber_fixtures import facts, release
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone
from test_cyber_decisions_web import decide, floor, signed_in  # noqa: F401 (fixtures)

from apps.cybersecurity import decisions, rules
from apps.cybersecurity.models import AcceptedBy, DecisionResponse, DeviceSecurityDecision, Network, OsFamily
from apps.equipment.models import Asset

pytestmark = pytest.mark.django_db

MODAL = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "modal-card"}
HX = {"HTTP_HX_REQUEST": "true"}
DAY = timedelta(days=1)
OS_REASON = "the OS vendor's published support ended, or its end is announced"


def body_of(r) -> str:
    assert r.status_code == 200
    return unescape(r.content.decode())


def lapsed(**scope) -> DeviceSecurityDecision:
    """A decision decided 400 days ago, past its review (10 days ago)."""
    today = timezone.localdate()
    return decide(**scope, decided_on=today - 400 * DAY, review_by=today - 10 * DAY)


def attention(today, user=None) -> dict:
    return {h.key: h for h in rules.needs_attention(today, user=user).headings}


def scope_of(d) -> dict:
    if d.asset_id:
        return {"asset": d.asset}
    return {"release": d.release} if d.release_id else {"device_model": d.device_model}


def ours(heading) -> list:
    """A heading's rows per model and release (devices of ours), never a temporary device's own row."""
    return [r for r in heading.rows if r.asset is None]


def end_url(d) -> str:
    return f"/security/decisions/{d.number}/end/"


# --- T2: a reason's fact not recorded ---------------------------------------------------------------------------------------------

@pytest.fixture
def cleared(floor):  # noqa: F811
    """The skeptic's scenario: the pump profile records the OS once for every version (Windows 10 IoT, support to Jan 1, 2031); 5.2
    records its own security end (a month ago). A device decision for P-1 and a release decision for 5.2, both past review, for the
    manufacturer's security updates; then P-1's release cleared (Record facts) and 5.2's security end cleared (waiting for the
    manufacturer's letter): both fall back to the profile's support group, its OS recorded and supported."""
    floor.profile.os_family, floor.profile.os_name, floor.profile.os_support_ends_on = OsFamily.WINDOWS, "Windows 10 IoT", date(2031, 1, 1)
    floor.profile.save()
    own = lapsed(asset=floor.d["P-1"])
    on52 = lapsed(release=floor.r52)
    facts(floor.d["P-1"], None)
    floor.r52.security_updates_end_on = None
    floor.r52.save()
    return own, on52


def test_a_reasons_fact_not_recorded_is_unknown_for_the_decision_renewed(floor, cleared):  # noqa: F811
    own, on52 = cleared
    s = rules.device_standing(floor.d["P-1"], floor.today)
    assert (s.security.key, s.os.key) == (rules.SECURITY_NOT_RECORDED, rules.OS_END_ANNOUNCED)
    assert rules.support_need(s, floor.today) is None
    assert not rules.need_unknown(s)  # a new decision: the both-blank rule (its OS support is recorded)
    assert rules.need_unknown(s, [rules.SECURITY_UPDATES]) and rules.need_unknown(s, (rules.SECURITY_UPDATES, rules.OS_SUPPORT))
    assert not rules.need_unknown(s, [rules.OS_SUPPORT])  # its OS support is recorded: that reason is known not to hold
    p1_words = "P-1: its release is not recorded, nor the end of its security updates: record its release (Record facts) first."
    on52_words = ("2 devices of ours in this scope are not recorded enough to tell whether they need a decision (P-2, P-3). P-2: the end "
                  "of the manufacturer's security updates for 5.2 (demo) is not recorded: record it on the release (Models) first.")
    for d, words in ((own, p1_words), (on52, on52_words)):
        refusal = decisions.refusal_for(**scope_of(d), supersedes=d.number, today=floor.today)
        assert (refusal.words, refusal.kind, refusal.ends_it) == (words, decisions.KIND_UNKNOWN, False), d.number
    found = attention(floor.today)
    assert [(r.decision, r.record, r.end) for r in found["review_due"].rows] == [(own, None, None), (on52, None, None)]
    # a new decision keeps the both-blank rule: P-4 with no release recorded reads the profile, its OS supported: none needed
    facts(floor.d["P-4"], None)
    refusal = decisions.refusal_for(asset=floor.d["P-4"], today=floor.today)
    assert (refusal.words, refusal.kind) == (decisions.NOT_NEEDED.format(name="P-4"), decisions.KIND_NOT_NEEDED)
    # recorded again: Review and renew
    floor.r52.security_updates_end_on = floor.today - 30 * DAY
    floor.r52.save()
    facts(floor.d["P-1"], floor.r52)
    rows = {r.decision: r for r in attention(floor.today)["review_due"].rows}
    assert all(r.record is not None and r.end is None for r in rows.values())


def test_a_release_recording_its_os_alone_and_its_security_end_cleared(floor):  # noqa: F811
    """Without a profile's OS: a release records its OS as supported and only its security end is cleared."""
    r53 = release(floor.model, "5.3 (demo)", security_updates_end_on=floor.today - 30 * DAY, os_family=OsFamily.WINDOWS,
                  os_name="Windows 10 IoT", os_support_ends_on=date(2031, 1, 1))
    facts(floor.d["P-4"], r53)
    own = lapsed(asset=floor.d["P-4"])
    r53.security_updates_end_on = None
    r53.save()
    refusal = decisions.refusal_for(asset=floor.d["P-4"], supersedes=own, today=floor.today)
    words = ("P-4: the end of the manufacturer's security updates for 5.3 (demo) is not recorded: record it on the release (Models) "
             "first.")
    assert (refusal.words, refusal.kind, refusal.ends_it) == (words, decisions.KIND_UNKNOWN, False)
    (row,) = attention(floor.today)["review_due"].rows
    assert (row.decision, row.record, row.end) == (own, None, None)


def test_an_os_support_reason_whose_support_is_cleared_is_unknown(floor):  # noqa: F811
    """A decision for the OS vendor's support (the manufacturer's end not recorded then); later the manufacturer's end is recorded years
    away and the OS support's day cleared: the OS reason's fact is not recorded, never End."""
    r70 = release(floor.model, "7.0 (demo)", os_family=OsFamily.WINDOWS, os_name="Windows 7", os_support_ends_on=date(2024, 1, 14))
    for tag in ("P-3", "P-4"):
        facts(floor.d[tag], r70)
    own = lapsed(asset=floor.d["P-4"], reasons=[rules.OS_SUPPORT])
    r70.security_updates_end_on, r70.os_support_ends_on = date(floor.today.year + 4, 3, 31), None
    r70.save()
    s = rules.device_standing(floor.d["P-4"], floor.today)
    assert (s.security.key, s.os.key) == (rules.UPDATES_END_ANNOUNCED, rules.OS_NOT_RECORDED)
    assert not rules.need_unknown(s) and rules.need_unknown(s, [rules.OS_SUPPORT]) and not rules.need_unknown(s, [rules.SECURITY_UPDATES])
    refusal = decisions.refusal_for(asset=floor.d["P-4"], supersedes=own, today=floor.today)
    assert (refusal.words, refusal.kind) == (decisions.OS_SUPPORT_NOT_RECORDED.format(tag="P-4", version="7.0 (demo)"),
                                             decisions.KIND_UNKNOWN)
    assert "record it on the release (Models) first" in refusal.words and not refusal.ends_it
    (row,) = attention(floor.today)["review_due"].rows
    assert (row.decision, row.record, row.end) == (own, None, None)
    # a new decision for P-3, in the same state: the both-blank rule (the manufacturer's end is recorded): none needed
    assert decisions.refusal_for(asset=floor.d["P-3"], today=floor.today).kind == decisions.KIND_NOT_NEEDED


def test_the_decision_page_and_the_renew_modal_say_what_to_record_first(client, signed_in, floor, cleared):  # noqa: F811
    own, on52 = cleared
    signed_in("manager")  # Device security Approve, Equipment Edit
    words = "P-1: its release is not recorded, nor the end of its security updates: record its release (Record facts) first."
    page = body_of(client.get(f"/security/decisions/{own.number}/", **HX))
    assert f"Review and renew is not offered now: {words}" in page and "Review and renew</button>" not in page
    assert f'<button class="btn" type="button" hx-get="{end_url(own)}"' in page  # End stays, never the main action
    assert f'<button class="btn primary" type="button" hx-get="{end_url(own)}"' not in page
    for d in (own, on52):
        body = body_of(client.get(f"/security/decisions/new/?supersedes={d.number}", **MODAL))
        assert "<form" not in body and "first." in body and end_url(d) not in body and f"End {d.number}" not in body
    r = client.get("/security/decisions/")
    (heading,) = [h for h in r.context["attention"]["headings"] if h["key"] == "review_due"]
    assert [(row["record_url"], row["end_url"]) for row in heading["rows"]] == [("", ""), ("", "")]


# --- T0: the reasons of the devices the decision would govern ---------------------------------------------------------------------

@pytest.fixture
def split(floor):  # noqa: F811
    """The skeptic's scenario: a decision for release 5.2 (security updates ended a month ago: P-1, P-2, P-3); release 6.0 (demo) with
    Windows 7's support ended Jan 14, 2024 and no security-updates date, P-4 on it."""
    on52 = decide(release=floor.r52)
    r60 = release(floor.model, "6.0 (demo)", os_family=OsFamily.WINDOWS, os_name="Windows 7", os_support_ends_on=date(2024, 1, 14))
    facts(floor.d["P-4"], r60)
    return on52, r60


OS_LINE = "The OS vendor's published support for Windows 7 ended after Jan 14, 2024"


def test_a_models_decision_takes_the_reasons_of_the_devices_it_would_govern(floor, split):  # noqa: F811
    assert decisions.refusal_for(device_model=floor.model, today=floor.today) is None
    members = list(rules.scope_devices(device_model=floor.model).select_related(*rules._WITH_RECORD))
    assert [a.tag for a in decisions.would_govern(members, device_model=floor.model)] == ["P-4"]
    for reasons in (["security_updates", "os_support"], ["security_updates"]):
        with pytest.raises(ValidationError) as e:
            decide(device_model=floor.model, reasons=reasons)
        assert e.value.message_dict == {"reasons": [decisions.REASONS_NOW.format(words=OS_REASON)]}, reasons
    d = decide(device_model=floor.model, reasons=["os_support"])
    assert (d.reasons, d.governed_ids) == (["os_support"], [str(floor.d["P-4"].pk)])
    assert rules.holding_lines(d, rules.decision_devices(d, floor.today)) == [OS_LINE]
    assert rules.decision_checks(d, floor.today) == []


def test_the_footers_modal_offers_and_the_page_shows_only_what_holds_for_them(client, signed_in, floor, split):  # noqa: F811
    on52, _r60 = split
    signed_in("director")
    (row,) = attention(floor.today)["no_decision"].rows
    assert row.words.startswith("Alaris 8015 PCU on 6.0 (demo): 1 device of ours; ") and row.tags == ("P-4",)
    url = client.get(f"/security/models/{floor.model.pk}/", **HX).context["decide_model_url"]
    assert url == f"/security/decisions/new/?model={floor.model.pk}&from=panel"
    body = body_of(client.get(url, **MODAL))
    assert 'name="reasons" value="os_support"' in body and 'name="reasons" value="security_updates"' not in body
    assert OS_LINE in body and "Security updates ended after" not in body
    assert rules.REASONS["security_updates"] not in body
    fields = {"response": DecisionResponse.KEEP_WITHOUT_ADDED_CONTROLS, "accepted_by": AcceptedBy.CE_LEADER,
              "decided_on": floor.today.isoformat()}
    body = body_of(client.post(url, {**fields, "reasons": ["security_updates", "os_support"]}, **MODAL))
    assert decisions.REASONS_NOW.format(words=OS_REASON) in body
    assert not DeviceSecurityDecision.objects.filter(release__isnull=True, asset__isnull=True).exists()
    r = client.post(url, {**fields, "reasons": ["os_support"]}, **MODAL)
    assert r.status_code == 200 and r["HX-Retarget"] == "#drawer"
    d = DeviceSecurityDecision.objects.get(release__isnull=True, asset__isnull=True)
    assert (d.reasons, d.governed_ids) == (["os_support"], [str(floor.d["P-4"].pk)])
    page = client.get(f"/security/decisions/{d.number}/", **HX)
    assert page.context["now_lines"] == [OS_LINE] and page.context["checks"] == []
    assert "Security updates ended after" not in body_of(page)
    assert "Security updates ended after" not in body_of(client.get(f"/security/decisions/{d.number}/print/"))
    assert body_of(client.get(f"/security/decisions/{on52.number}/", **HX)).count("Security updates ended after") >= 1


def test_what_holds_now_on_the_page_is_for_the_devices_it_governs(client, signed_in, floor, split):  # noqa: F811
    on52, _r60 = split
    decisions.end_decision(on52, by=None)
    whole = decide(device_model=floor.model, reasons=["security_updates", "os_support"])  # P-1 to P-4: both reasons hold
    sec_line = f"Security updates ended after {rules.day_words(floor.today - 30 * DAY)}"
    assert rules.holding_lines(whole, rules.decision_devices(whole, floor.today)) == [sec_line, OS_LINE]
    decide(release=floor.r52)  # 5.2's own: the model's governs P-4 alone now
    signed_in("director")
    page = client.get(f"/security/decisions/{whole.number}/", **HX)
    assert page.context["now_lines"] == [OS_LINE]
    assert sec_line not in body_of(client.get(f"/security/decisions/{whole.number}/print/"))
    # ended: what it would govern recorded again for its scope now (decisions.would_govern), never 5.2's devices
    decisions.end_decision(whole, by=None)
    whole.refresh_from_db()
    devices = rules.decision_devices(whole, floor.today)
    members = [dd.asset for dd in devices]
    assert [dd.asset.tag for dd in rules.under(whole, devices)] == [a.tag for a in decisions.would_govern(members, device_model=floor.model)]
    assert client.get(f"/security/decisions/{whole.number}/", **HX).context["now_lines"] == [OS_LINE]


def test_a_scope_whose_needs_are_governed_elsewhere_is_refused_and_end_answers_its_renewal(client, signed_in, floor):  # noqa: F811
    whole = lapsed(device_model=floor.model)  # P-1 to P-4 (5.2's past its security updates)
    on52 = decide(release=floor.r52)  # P-1 to P-3 now 5.2's; P-4 (5.6, supported) the model's
    words = decisions.NEEDED_ELSEWHERE.format(tags="P-4", numbers=on52.number)
    refusal = decisions.refusal_for(device_model=floor.model, supersedes=whole, today=floor.today)
    assert (refusal.words, refusal.kind, refusal.ends_it) == (words, decisions.KIND_NOT_NEEDED, True)
    with pytest.raises(ValidationError) as e:
        decide(device_model=floor.model, supersedes=whole)
    assert e.value.message_dict == {"device_model": [words]}
    (row,) = attention(floor.today)["review_due"].rows
    assert (row.decision, row.record, row.end) == (whole, None, rules.Link("web:security_decision_end", (whole.number,)))
    signed_in("director")
    page = body_of(client.get(f"/security/decisions/{whole.number}/", **HX))
    assert f"Review and renew is not offered now: {words}" in page
    assert f'<button class="btn primary" type="button" hx-get="{end_url(whole)}"' in page
    # every device of ours in the scope under a more specific decision
    facts(floor.d["P-4"], floor.r52)
    refusal = decisions.refusal_for(device_model=floor.model, supersedes=whole, today=floor.today)
    assert (refusal.words, refusal.kind) == (decisions.GOVERNED_ELSEWHERE.format(numbers=on52.number), decisions.KIND_NOT_NEEDED)
    assert [w for w in rules.DECISION_NEVER_WORDS if w in (words + refusal.words).casefold()] == []


def test_a_releases_decision_never_reads_a_device_under_its_own(floor):  # noqa: F811
    own = decide(asset=floor.d["P-1"])
    for tag in ("P-2", "P-3"):
        facts(floor.d[tag], floor.r52, network=Network.NOT_CONNECTED)
    words = decisions.NEEDED_ELSEWHERE.format(tags="P-2, P-3", numbers=own.number)
    refusal = decisions.refusal_for(release=floor.r52, today=floor.today)
    assert (refusal.words, refusal.kind) == (words, decisions.KIND_NOT_NEEDED)
    with pytest.raises(ValidationError) as e:
        decide(release=floor.r52)
    assert e.value.message_dict == {"release": [words]}


# --- T1: a row's link lists what the row counts ------------------------------------------------------------------------------------

def _panel_tags(client, link) -> list:
    """The devices the model's panel lists at the link's address (the view itself)."""
    r = client.get(reverse(link.name, args=list(link.args)), dict(link.query), **HX)
    assert r.status_code == 200 and r.context["devices_count"] == len(r.context["devices"])
    return [s.asset.tag for s in r.context["devices"]]


def _model_link(floor, row):  # noqa: F811
    need = rules.support_need(rules.device_standing(Asset.objects.get(tag=row.tags[0]), floor.today), floor.today)
    return rules._model_link(floor.model.pk, need)


def test_a_rows_link_is_the_panel_only_where_it_lists_the_rows_devices(client, signed_in, floor):  # noqa: F811
    signed_in("director")
    (row,) = attention(floor.today)["no_decision"].rows  # 5.2: P-1, P-2, P-3 (the rental is listed on its own)
    assert row.tags == ("P-1", "P-2", "P-3") and row.link == _model_link(floor, row)
    assert _panel_tags(client, row.link) == list(row.tags)
    # (a) an ending row: the standing filter has no 12 months' horizon, so the panel lists 5.6's P-4 too (announced years away)
    floor.r52.security_updates_end_on = floor.today + 60 * DAY
    floor.r52.save()
    (row,) = ours(attention(floor.today)["ending"])  # the rental T-1 is listed on its own
    assert row.tags == ("P-1", "P-2", "P-3") and row.link is None
    assert _panel_tags(client, _model_link(floor, row)) == ["P-1", "P-2", "P-3", "P-4"]
    facts(floor.d["P-4"], floor.r52)  # every device of that standing is the row's: the panel
    (row,) = ours(attention(floor.today)["ending"])
    assert row.link == _model_link(floor, row) and _panel_tags(client, row.link) == list(row.tags) == ["P-1", "P-2", "P-3", "P-4"]
    r = client.get("/security/decisions/")
    (heading,) = [h for h in r.context["attention"]["headings"] if h["key"] == "ending"]
    assert heading["rows"][0]["words"].startswith("Alaris 8015 PCU on 5.2 (demo): 4 devices of ours")
    assert heading["rows"][0]["url"].startswith(f"/security/models/{floor.model.pk}/?whose=ours&connected=either&standing=")


def test_a_rows_link_never_lists_another_releases_devices(client, signed_in, floor):  # noqa: F811
    signed_in("director")
    r50 = release(floor.model, "5.0 (demo)", security_updates_end_on=floor.today - 400 * DAY)
    p7 = Asset.objects.create(tag="P-7", device_model=floor.model, department=floor.dept)
    facts(p7, r50)
    on50 = decide(release=r50)
    (row,) = attention(floor.today)["no_decision"].rows  # 5.2's three; P-7 is 5.0's decision's
    assert row.tags == ("P-1", "P-2", "P-3") and row.link is None
    assert _panel_tags(client, _model_link(floor, row)) == ["P-1", "P-2", "P-3", "P-7"]
    decisions.end_decision(on50, by=None)  # 5.0's row of one device, 5.2's of three: neither is the panel's whole list
    rows = {r.tags: r.link for r in attention(floor.today)["no_decision"].rows}
    assert rows == {("P-7",): rules.Link("web:asset", ("P-7",), (("tab", "security"),)), ("P-1", "P-2", "P-3"): None}
    r = client.get("/security/decisions/")
    (heading,) = [h for h in r.context["attention"]["headings"] if h["key"] == "no_decision"]
    assert sorted(row["url"] for row in heading["rows"]) == ["", "/equipment/P-7/?tab=security"]
