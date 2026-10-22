"""
Slice 39 final fixes (the last skeptics' round; labels as in its findings), on SQLite and PostgreSQL:

- T1: a Needs attention row links to the model's panel only where the panel, with exactly the link's filters, lists the row's devices and
  no other (every release's devices of that standing, decided or not, and an announced end past 12 months included); else a one-device
  row links to the device's Security tab, a larger one to nothing.
"""
from datetime import timedelta

import pytest
from cyber_fixtures import facts, release
from django.urls import reverse
from test_cyber_decisions_web import decide, floor, signed_in  # noqa: F401 (fixtures)

from apps.cybersecurity import decisions, rules
from apps.equipment.models import Asset

pytestmark = pytest.mark.django_db

HX = {"HTTP_HX_REQUEST": "true"}
DAY = timedelta(days=1)


def attention(today, user=None) -> dict:
    return {h.key: h for h in rules.needs_attention(today, user=user).headings}


def ours(heading) -> list:
    """A heading's rows per model and release (devices of ours), never a temporary device's own row."""
    return [r for r in heading.rows if r.asset is None]


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
