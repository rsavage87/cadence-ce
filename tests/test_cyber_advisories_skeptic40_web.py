"""
Slice 40 skeptic fixes, the advisory doors (apps/web/views_cyber_advisories.py):

- S1: Save sends a version left in "Add a version not on file" to services.add_release (review fix #17), so a version it refuses would
  refuse every later Save with nothing saying how to save. Refused from Save, the box says add_release's words and then how to save
  without it (CLEAR_TO_SAVE); refused from its own button, add_release's words alone. The text typed stays in the box either way; Save
  with the box cleared saves. The same on a new row ("Name the model").

Every word added here is scanned with rules.DECISION_NEVER_WORDS.
"""
from html import unescape

import pytest
from cyber_fixtures import release
from test_cyber_advisories_review40_web import edit_form
from test_cyber_advisories_web import (  # noqa: F401 (fixtures)
    MODAL,
    errors,
    floor,
    never,
    saved_page,
    signed_in,
)

from apps.cybersecurity import services as cyber
from apps.cybersecurity.models import AdvisoryModel, SoftwareRelease
from apps.web import views_cyber_advisories as views_ad

SEEN: list = []  # every body read here, for the never-words test at the end


def text(r) -> str:
    body = unescape(r.content.decode())
    SEEN.append(body)
    return body


def on_file(version: str) -> str:
    return cyber.VERSION_ON_FILE.format(version=version)


def box(r) -> str:
    return r.context["form"]["add_version"].value()


@pytest.mark.django_db
def test_a_refused_version_sent_by_save_says_how_to_save(client, signed_in, floor):  # noqa: F811
    """The finding's scenario: 5.4 (demo) is on file. Its button is refused in add_release's words alone, the text kept in the box; the
    person ticks 5.4 (demo) and clicks Save with the box as it is: refused again, now saying to clear the box to save (and still nothing
    saved, however often); Save with the box cleared saves, 5.4 (demo) listed."""
    signed_in("technician")
    n = floor.a.number
    url = f"/security/advisories/{n}/models/?model={floor.model.pk}"
    data = edit_form(client, floor)
    r = client.post(url, {**data, "add_version": "5.4 (demo)", "action": "add_version"}, **MODAL)
    assert r.status_code == 200 and "HX-Retarget" not in r
    assert errors(text(r)) == {"add_version": on_file("5.4 (demo)")} and box(r) == "5.4 (demo)"
    ticked = {**data, "release_ids": [str(floor.r52.pk), str(floor.r54.pk)], "add_version": "5.4 (demo)"}
    for _ in range(2):
        r = client.post(url, ticked, **MODAL)
        assert r.status_code == 200 and "HX-Retarget" not in r
        body = text(r)
        assert errors(body) == {"add_version": f"{on_file('5.4 (demo)')} {views_ad.CLEAR_TO_SAVE}"} and box(r) == "5.4 (demo)"
        assert r.context["form"].fields["add_version"].widget.attrs.get("autofocus")  # the box first: no other field refused
        assert not AdvisoryModel.objects.get(pk=floor.row.pk).releases.filter(release=floor.r54).exists()
    saved_page(client.post(url, {**ticked, "add_version": ""}, **MODAL), n)
    assert set(AdvisoryModel.objects.get(pk=floor.row.pk).releases.values_list("release_id", flat=True)) == {floor.r52.pk, floor.r54.pk}
    assert SoftwareRelease.objects.filter(device_model=floor.model, version_key=floor.r54.version_key).count() == 1


@pytest.mark.django_db
def test_a_new_row_says_how_to_name_the_model_too(client, signed_in, floor, vent_model):  # noqa: F811
    """"Name the model" posts through the same Save: a refused version names the way out, and nothing is named until the box is
    cleared."""
    signed_in("technician")
    n = floor.a.number
    release(vent_model, "1.0 (demo)")
    url = f"/security/advisories/{n}/models/?model={vent_model.pk}"
    data = {"versions": "all", "remedy": "no_fix_yet", "confirm": "on", "add_version": "1.0 (demo)"}
    r = client.post(url, data, **MODAL)
    assert errors(text(r)) == {"add_version": f"{on_file('1.0 (demo)')} {views_ad.CLEAR_TO_SAVE}"} and box(r) == "1.0 (demo)"
    assert not AdvisoryModel.objects.filter(advisory=floor.a, device_model=vent_model).exists()
    r = client.post(url, {**data, "action": "add_version"}, **MODAL)  # its own button: add_release's words alone
    assert errors(text(r)) == {"add_version": on_file("1.0 (demo)")}
    saved_page(client.post(url, {**data, "add_version": ""}, **MODAL), n)
    assert AdvisoryModel.objects.filter(advisory=floor.a, device_model=vent_model).exists()


@pytest.mark.django_db
def test_save_with_a_version_add_release_takes_still_adds_it(client, signed_in, floor):  # noqa: F811
    """Unchanged by S1: a version not on file sent by Save is added, the form back with it ticked and nothing else said."""
    signed_in("technician")
    url = f"/security/advisories/{floor.a.number}/models/?model={floor.model.pk}"
    r = client.post(url, {**edit_form(client, floor), "add_version": "5.8 (demo)"}, **MODAL)
    body = text(r)
    assert views_ad.CLEAR_TO_SAVE not in body and "add_version" not in errors(body) and box(r) == ""
    assert views_ad.VERSION_ADDED.format(version="5.8 (demo)", model=floor.model.model) in body


@pytest.mark.django_db
def test_never_words_in_what_this_fix_added(client, signed_in, floor):  # noqa: F811
    signed_in("technician")
    url = f"/security/advisories/{floor.a.number}/models/?model={floor.model.pk}"
    text(client.post(url, {**edit_form(client, floor), "add_version": "5.4 (demo)"}, **MODAL))
    words = [views_ad.CLEAR_TO_SAVE, views_ad._add_version.__doc__, views_ad.advisory_models.__doc__]
    hits = sorted({(w, t[:80]) for t in [*words, *SEEN] for w in never(t)})
    assert hits == [] and views_ad.CLEAR_TO_SAVE in SEEN[-1] and all(words)
