"""
Slice 40 review fixes, the advisory doors (apps/web/views_cyber_advisories.py, forms_cyber_advisories.py, their templates):

- #6: a model row's "Record decision for <release>" and "Record decision for every <model> of ours" are offered only for a device of ours
  that a decision for that scope would govern (decisions._would_govern): never one a more specific decision governs, past its review or
  not, read from the governing decision's scope. Every scope button offered leads to the form, or to the scope's own decision in force
  (review and renew it), never to a refusal of the scope.
- #8 and #19: Reopen's confirmation says whether the models stay confirmed or are then to confirm, from the advisory as stored; a revision
  that clears the confirmation of a closed advisory says to reopen it to confirm them (its Check's words), never to confirm them there.
- #13: an advisory entered in error reads no count: its page and the list say nothing about the devices on site (never "No device of
  this model on site" or "None on site"), and its CSV row's count cells are blank, never 0.
- #15: Set models sends only what its choices show: a fix, a release ticked, or a patch list its choice hides is never refused out of
  sight; the writer keeps those refusals.
- #16: Record advisory keeps the CVE IDs' format refusal when the reference is on another advisory: an overlap is noted only for CVE IDs
  that read as such.
- #17: a version typed in "Add a version not on file" and sent by Save (Enter in the box) is added, never dropped by a save.

The GET doors these touch (the page, the list, its CSV, the reopen modal) also run on PostgreSQL outside any transaction (GET doors never
lock); every word added here is scanned with rules.DECISION_NEVER_WORDS.
"""
import re
from datetime import timedelta
from html import unescape
from urllib.parse import parse_qs, urlsplit

import pytest
from csvutil import csv_rows
from cyber_fixtures import make_advisory, model_row
from django.utils import timezone
from test_cyber_advisories_web import (  # noqa: F401 (fixtures)
    MODAL,
    blocker_of,
    decide,
    errors,
    floor,
    never,
    page,
    saved_page,
    signed_in,
    toast_of,
)

from apps.cybersecurity import advisories, decisions, rules
from apps.cybersecurity.models import (
    AdvisoryModel,
    AdvisorySource,
    AdvisoryStatus,
    AppliesTo,
    DeviceSecurityDecision,
    SecurityAdvisory,
    SoftwareRelease,
)
from apps.web import forms_cyber_advisories as forms_ad
from apps.web import views_cyber_advisories as views_ad

DAY = timedelta(days=1)
SEEN: list = []  # every body, CSV, and toast read here (never what a person typed), for the never-words test at the end


def text(r) -> str:
    body = unescape(r.content.decode())
    SEEN.append(body)
    return body


def lapse(d) -> DeviceSecurityDecision:
    """A decision past its review date (decided two days ago, review by yesterday)."""
    today = timezone.localdate()
    DeviceSecurityDecision.objects.filter(pk=d.pk).update(decided_on=today - 2 * DAY, review_by=today - DAY)
    d.refresh_from_db()
    return d


# --- #6: the scope buttons of a model row ------------------------------------------------------------------------------------------

def scope_buttons(body: str) -> dict:
    """{"release": [release ids], "model": [model ids]}: the Record decision buttons of the model rows on an advisory's page."""
    out = {"release": [], "model": []}
    for url in re.findall(r'<button class="link" type="button" hx-get="(/security/decisions/new/\?[^"]+)" hx-target="#modal-card">'
                          r'Record decision for ', body):
        query = parse_qs(urlsplit(url).query)
        for key in out:
            out[key] += query.get(key, [])
    return out


def leads_somewhere(adv, buttons: dict) -> None:
    """Every scope button offered leads to the form (refusal_for None) or to the scope's own decision in force (review and renew it)."""
    for key, ids in buttons.items():
        for pk in ids:
            found = decisions.refusal_for(applies_to=AppliesTo.ADVISORY, advisory=adv,
                                          **({"release": pk} if key == "release" else {"device_model": pk}))
            assert found is None or found.kind == decisions.KIND_IN_FORCE, (key, found)


@pytest.mark.django_db(transaction=True)
def test_a_device_decision_past_review_offers_neither_scope_button(client, signed_in, floor):  # noqa: F811
    """The finding's scenario: P-1's own decision past review, the other devices of ours needing a response under their own decisions.
    A release's decision would govern none of them, nor would the model's: neither is offered; the line offers Review and renew."""
    own = lapse(decide(floor.a, asset=floor.d["P-1"]))
    decide(floor.a, asset=floor.d["P-2"])
    decide(floor.a, asset=floor.d["P-5"])
    signed_in("manager")
    n = floor.a.number
    body = text(page(client, n))
    assert scope_buttons(body) == {"release": [], "model": []}
    assert f"/security/decisions/new/?supersedes={own.number}&from=advisory" in body  # the line's own Review and renew
    for scope in ({"release": floor.r52}, {"device_model": floor.model}):  # what the buttons would have led to
        assert decisions.refusal_for(applies_to=AppliesTo.ADVISORY, advisory=floor.a, **scope).kind == decisions.KIND_NOT_NEEDED
    r = client.get(f"/security/advisories/{n}/")  # the full page reads the same
    assert r.status_code == 200 and scope_buttons(text(r)) == {"release": [], "model": []}


@pytest.mark.django_db(transaction=True)
def test_a_release_decision_past_review_leaves_its_release_button_never_the_models(client, signed_in, floor):  # noqa: F811
    """The release's own decision past review: its button leads to it (review and renew it); the model's would govern no device needing
    a response (P-1 and P-2 under the release's, P-5 under its own): not offered."""
    d = lapse(decide(floor.a, release=floor.r52))
    decide(floor.a, asset=floor.d["P-5"])
    signed_in("manager")
    body = text(page(client, floor.a.number))
    buttons = scope_buttons(body)
    assert buttons == {"release": [str(floor.r52.pk)], "model": []}
    leads_somewhere(floor.a, buttons)
    assert decisions.refusal_for(applies_to=AppliesTo.ADVISORY, advisory=floor.a, release=floor.r52).live == d


@pytest.mark.django_db(transaction=True)
def test_under_the_models_decision_past_review_both_buttons_lead_somewhere(client, signed_in, floor):  # noqa: F811
    """The model's decision past review: a release's decision would govern its devices on 5.2 (more specific), so its button stays, and
    the model's button leads to the model's decision (review and renew it)."""
    lapse(decide(floor.a, device_model=floor.model))
    signed_in("manager")
    buttons = scope_buttons(text(page(client, floor.a.number)))
    assert buttons == {"release": [str(floor.r52.pk)], "model": [str(floor.model.pk)]}
    leads_somewhere(floor.a, buttons)
    assert decisions.refusal_for(applies_to=AppliesTo.ADVISORY, advisory=floor.a, release=floor.r52) is None


@pytest.mark.django_db
def test_nothing_decided_offers_both_buttons_and_a_covering_decision_neither(client, signed_in, floor):  # noqa: F811
    signed_in("manager")
    buttons = scope_buttons(text(page(client, floor.a.number)))
    assert buttons == {"release": [str(floor.r52.pk)], "model": [str(floor.model.pk)]}
    leads_somewhere(floor.a, buttons)
    decide(floor.a, device_model=floor.model)  # in force over every device of ours: nothing waits
    assert scope_buttons(text(page(client, floor.a.number))) == {"release": [], "model": []}


def test_the_predicate_reads_the_governing_decisions_scope():
    """_more_specific from the decision's scope alone: a device's is more specific than a release's and the model's, a release's than
    the model's only."""
    from types import SimpleNamespace as N

    device, rel, model = N(asset_id=1, release_id=None), N(asset_id=None, release_id=2), N(asset_id=None, release_id=None)
    assert [views_ad._more_specific(d, rules.RELEASE_SCOPE) for d in (None, device, rel, model)] == [False, True, False, False]
    assert [views_ad._more_specific(d, rules.MODEL_SCOPE) for d in (None, device, rel, model)] == [False, True, True, False]


# --- #8, #19: Reopen's words and the revision's toast follow the confirmation ------------------------------------------------------

def revision(client, adv, **fields):
    src = AdvisorySource.objects.filter(advisory=adv, removed_on__isnull=True).order_by("created_at").first()
    data = {"revision": "2", "updated_on": timezone.localdate().isoformat(), "last_change": ["products_changed"], **fields}
    return client.post(f"/security/advisories/{adv.number}/sources/{src.pk}/revision/", data, **MODAL)


@pytest.mark.django_db(transaction=True)
def test_a_closed_advisory_whose_models_are_to_confirm_since_a_revision(client, make_user, floor):  # noqa: F811
    """Closed, then a revision naming new products: the toast says to reopen it to confirm them (an Edit reader cannot confirm on a
    closed one), and Reopen says its models are then to confirm, never that they stay confirmed."""
    tech, manager = make_user("technician"), make_user("manager")
    decide(floor.a, device_model=floor.model)
    decide(floor.a, asset=floor.d["T-1"])
    advisories.close(floor.a, by=manager)
    n = floor.a.number
    client.force_login(tech)
    r = revision(client, floor.a)
    body = saved_page(r, n)
    SEEN.append(body)
    assert toast_of(r) == views_ad.REVISION_REOPEN_TO_CONFIRM.format(number=n)
    assert f"Revision recorded on {n}: reopen it to confirm the models it names." == views_ad.REVISION_REOPEN_TO_CONFIRM.format(number=n)
    assert rules.TO_CONFIRM_CHECK_WORDS in body and "Confirm models" not in body
    floor.a.refresh_from_db()
    assert floor.a.status == AdvisoryStatus.CLOSED and floor.a.models_confirmed_on is None
    client.force_login(manager)
    body = text(client.get(f"/security/advisories/{n}/reopen/", **MODAL))
    assert views_ad.REOPEN_WORDS.format(number=n) + views_ad.REOPEN_TO_CONFIRM in body
    assert "stay confirmed" not in body and not blocker_of(body)
    r = client.post(f"/security/advisories/{n}/reopen/", {}, **MODAL)
    assert "Confirm models" in saved_page(r, n)  # as the modal said: to confirm


@pytest.mark.django_db
def test_an_open_advisory_still_says_confirm_them(client, signed_in, floor):  # noqa: F811
    signed_in("technician")
    r = revision(client, floor.a)
    assert toast_of(r) == views_ad.REVISION_TO_CONFIRM.format(number=floor.a.number)


@pytest.mark.django_db(transaction=True)
def test_reopen_says_the_models_stay_confirmed_only_while_they_are(client, make_user, ctx):
    """Not applicable (which confirms the models), then a new day received (which clears it): Reopen says they are then to confirm;
    before that, that they stay confirmed."""
    today = timezone.localdate()
    tech = make_user("technician")
    a = make_advisory(received_on=today - 5 * DAY, reference="DEMO-ADV-0003")
    advisories.mark_not_applicable(a, reason="no_model_here", by=tech, today=today)
    client.force_login(tech)
    url = f"/security/advisories/{a.number}/reopen/"
    body = text(client.get(url, **MODAL))
    assert views_ad.REOPEN_WORDS.format(number=a.number) + views_ad.REOPEN_CONFIRMED in body and views_ad.REOPEN_TO_CONFIRM not in body
    advisories.update_advisory(a, received_on=(today - 4 * DAY).isoformat(), by=tech, today=today)
    a.refresh_from_db()
    assert a.status == AdvisoryStatus.NOT_APPLICABLE and a.models_confirmed_on is None
    body = text(client.get(url, **MODAL))
    assert views_ad.REOPEN_WORDS.format(number=a.number) + views_ad.REOPEN_TO_CONFIRM in body and "stay confirmed" not in body


# --- #13: an advisory entered in error reads no count ------------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
def test_an_advisory_entered_in_error_says_nothing_about_the_devices_on_site(client, make_user, floor, vent_model):  # noqa: F811
    manager = make_user("manager")
    live = make_advisory(reference="DEMO-ADV-0004")
    model_row(live, vent_model, versions="all", remedy="no_fix_yet")  # live, its model with no device on site: the empty words stay
    advisories.mark_entered_in_error(floor.a, by=manager)
    client.force_login(manager)
    n = floor.a.number
    body = text(page(client, n))
    assert '<span class="chip neutral">Entered in error</span>' in body and "BD Alaris 8015 PCU" in body
    assert "No device of this model on site" not in body and 'class="ad-counts"' not in body
    assert "No device of this model on site" in text(page(client, live.number))
    lst = text(client.get("/security/advisories/?status=all"))
    assert set(re.findall(r'<a class="tag" href="/security/advisories/([A-Z0-9-]+)/">', lst)) == {n, live.number}
    row = re.search(rf'href="/security/advisories/{n}/">.*?</tr>', lst, re.S).group(0)
    assert "None on site" not in row and row.count('<td class="muted">—</td>') == 2
    other = re.search(rf'href="/security/advisories/{live.number}/">.*?</tr>', lst, re.S).group(0)
    assert "None on site" in other  # a live advisory whose model has no device here: true
    r = client.get("/security/advisories/export.csv?status=all")
    found = {row[0]: row for row in csv_rows(r)[1:]}
    width = len(views_ad.LIST_COLUMNS) + len(views_ad.DEVICE_COLUMNS_EXTRA)
    assert found[n][5:] == ["Entered in error", *[""] * (width - 6)] and len(found[n]) == width
    assert found[live.number][6:] == ["0"] * (width - 6)


# --- #15: Set models sends what its choices show -----------------------------------------------------------------------------------

def edit_form(client, floor) -> dict:  # noqa: F811
    """The Set models form for the pumps' row as rendered, as a browser posts it (every field, hidden or not)."""
    r = client.get(f"/security/advisories/{floor.a.number}/models/?model={floor.model.pk}", **MODAL)
    form = r.context["form"]
    data = {name: form[name].value() for name in ("versions", "release_ids", "remedy", "fix_release", "patches_reference")}
    return {**{k: v for k, v in data.items() if v not in (None, "")}, "confirm": "on", "add_version": ""}


@pytest.mark.django_db
def test_a_choice_moved_on_saves_without_what_it_hides(client, signed_in, floor):  # noqa: F811
    """The finding's three cases: remedy to patches with the fix still chosen; every release with releases still ticked; remedy away
    from patches with the list still typed. Each saves, the hidden value left out."""
    signed_in("technician")
    n = floor.a.number
    url = f"/security/advisories/{n}/models/?model={floor.model.pk}"
    data = edit_form(client, floor)
    assert data["fix_release"] == str(floor.r56.pk) and data["release_ids"] == [str(floor.r52.pk)]
    r = client.post(url, {**data, "remedy": "patches", "patches_reference": "DEMO-PATCH-07"}, **MODAL)
    saved_page(r, n)
    row = AdvisoryModel.objects.get(pk=floor.row.pk)
    assert (row.remedy, row.fix_release_id, row.patches_reference) == ("patches", None, "DEMO-PATCH-07")
    data = edit_form(client, floor)
    assert data["patches_reference"] == "DEMO-PATCH-07"
    r = client.post(url, {**data, "remedy": "mitigations"}, **MODAL)  # the list still typed in its hidden box
    saved_page(r, n)
    row.refresh_from_db()
    assert (row.remedy, row.patches_reference) == ("mitigations", "")
    data = edit_form(client, floor)
    r = client.post(url, {**data, "versions": "all"}, **MODAL)  # 5.2 still ticked in the hidden list
    saved_page(r, n)
    row.refresh_from_db()
    assert row.versions == "all" and not row.releases.exists()


@pytest.mark.django_db
def test_a_refusal_lands_where_it_shows(client, signed_in, floor):  # noqa: F811
    """What a choice shows is still refused in sight: the releases listed with none ticked, a release remedy with no fix."""
    signed_in("technician")
    url = f"/security/advisories/{floor.a.number}/models/?model={floor.model.pk}"
    data = edit_form(client, floor)
    body = text(client.post(url, {**data, "release_ids": [], "fix_release": ""}, **MODAL))
    assert set(errors(body)) == {"release_ids", "fix_release"}


def test_the_form_sends_only_what_its_choices_show():
    rel = SoftwareRelease(pk=None, version="5.2 (demo)")
    rel.pk = "11111111-1111-1111-1111-111111111111"

    def sent(**data) -> dict:
        form = forms_ad.ModelForm({"confirm": "on", **data}, releases=[(rel, 1, "")])
        assert form.is_valid()
        return form.service_kwargs()

    hidden = {"release_ids": [rel.pk], "fix_release": rel.pk, "patches_reference": "DEMO-PATCH-07"}
    assert sent(versions="all", remedy="mitigations", **hidden) == {
        "versions": "all", "release_ids": [], "remedy": "mitigations", "fix_release": None, "patches_reference": "", "confirm": True}
    assert sent(versions="listed", remedy="release", **hidden)["release_ids"] == [rel.pk]
    assert sent(versions="listed", remedy="release", **hidden)["fix_release"] == rel.pk
    assert sent(versions="not_stated", remedy="patches", **hidden)["patches_reference"] == "DEMO-PATCH-07"
    assert sent(versions="", remedy="", **hidden) == {"versions": "", "release_ids": [], "remedy": "", "fix_release": None,
                                                      "patches_reference": "", "confirm": True}


@pytest.mark.django_db
def test_the_writer_keeps_its_refusals_for_the_api(floor):  # noqa: F811
    from django.core.exceptions import ValidationError

    with pytest.raises(ValidationError) as e:
        advisories.set_model(floor.a, device_model=floor.model, versions="all", release_ids=[floor.r52.pk], remedy="patches",
                             fix_release=floor.r56.pk, patches_reference="DEMO-PATCH-07", confirm=True, by=None)
    assert set(e.value.message_dict) == {"release_ids", "fix_release"}


# --- #16: Record advisory keeps the CVE IDs' format refusal ------------------------------------------------------------------------

@pytest.mark.django_db
def test_a_bad_cve_list_is_refused_even_when_the_reference_is_on_another_advisory(client, signed_in, floor):  # noqa: F811
    signed_in("technician")
    url = "/security/advisories/new/"
    data = {"received_on": timezone.localdate().isoformat(), "cves": "(Critical)", "source": "cisa_icsma", "reference": "DEMO-ADV-0001",
            "url": "", "published_on": ""}
    body = unescape(client.post(url, data, **MODAL).content.decode())  # echoes what was typed: never read into SEEN
    assert errors(body)["cves"] == advisories.CVE_WORDS.format(piece="(Critical)")
    assert "Record as its own advisory" not in body and 'name="seen"' not in body
    body = text(client.post(url, {**data, "cves": "CVE-2026-3001"}, **MODAL))  # the list reads: the reference's overlap noted
    assert f"DEMO-ADV-0001 is on {floor.a.number} (Manufacturer bulletin DEMO-ADV-0001)." in body and "cves" not in errors(body)
    assert SecurityAdvisory.objects.count() == 1


@pytest.mark.django_db
def test_edit_advisory_keeps_the_format_refusal_too(client, signed_in, floor):  # noqa: F811
    signed_in("technician")
    url = f"/security/advisories/{floor.a.number}/edit/"
    body = unescape(client.post(url, {"received_on": floor.a.received_on.isoformat(), "cves": "CVE-2026-1001 nope"}, **MODAL)
                    .content.decode())
    assert errors(body)["cves"] == advisories.CVE_WORDS.format(piece="nope")


# --- #17: a version typed is added, never dropped by a save -----------------------------------------------------------------------

@pytest.mark.django_db
def test_enter_in_the_version_box_adds_the_version(client, signed_in, floor):  # noqa: F811
    """What a browser sends on Enter in the box: the form through Save, with no `action`. The version is added and the form comes back
    with it (nothing saved); Save then saves."""
    signed_in("technician")
    n = floor.a.number
    url = f"/security/advisories/{n}/models/?model={floor.model.pk}"
    data = edit_form(client, floor)
    r = client.post(url, {**data, "add_version": "5.8 (demo)"}, **MODAL)
    assert r.status_code == 200 and "HX-Retarget" not in r
    new = SoftwareRelease.objects.get(device_model=floor.model, version="5.8 (demo)")
    body = text(r)
    assert views_ad.VERSION_ADDED.format(version="5.8 (demo)", model="Alaris 8015 PCU") in body
    form = r.context["form"]
    assert form["add_version"].value() == "" and str(new.pk) in form["release_ids"].value()
    assert not AdvisoryModel.objects.get(pk=floor.row.pk).releases.filter(release=new).exists()  # nothing saved yet
    again = {k: (v if k == "release_ids" else v[0]) for k, v in form.data.lists()}
    r = client.post(url, again, **MODAL)
    saved_page(r, n)
    assert AdvisoryModel.objects.get(pk=floor.row.pk).releases.filter(release=new).exists()
    # refused: its words on the box, nothing saved
    body = unescape(client.post(url, {**data, "add_version": "5.2 (demo)"}, **MODAL).content.decode())
    assert "add_version" in errors(body)
    # only spaces typed: an ordinary save
    saved_page(client.post(url, {**data, "add_version": "   "}, **MODAL), n)


# --- words ---------------------------------------------------------------------------------------------------------------------------

@pytest.mark.django_db
def test_never_words_in_what_this_round_added(client, signed_in, floor):  # noqa: F811
    words = [views_ad.REOPEN_WORDS + views_ad.REOPEN_CONFIRMED, views_ad.REOPEN_WORDS + views_ad.REOPEN_TO_CONFIRM,
             views_ad.REVISION_REOPEN_TO_CONFIRM, views_ad._more_specific.__doc__, views_ad._ours_waiting.__doc__,
             views_ad._parsed_cves.__doc__, forms_ad.ModelForm.service_kwargs.__doc__]
    signed_in("manager")
    SEEN.append(text(page(client, floor.a.number)))
    hits = sorted({(w, t[:80]) for t in [*words, *SEEN] for w in never(t)})
    assert hits == [] and len(SEEN) > 1
