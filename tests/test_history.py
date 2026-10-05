"""
The change history reader (apps/core/history.py, slice 20 scaffold): one record's changes in words, newest first, and the facility's
change log across the areas a role can view, inside the request's facility only. Pages continue after the last entry shown (a
cursor), so saves made between two pages never repeat or skip an entry, and a page reads past saves that show nothing. Someone of
another facility is never named.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.accounts.models import AccessEvent, Role, User, create_default_roles
from apps.contracts import services as ct
from apps.contracts.models import ContractType
from apps.core import history
from apps.equipment import services as eq
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel
from apps.tenants.context import tenant_context


@pytest.fixture
def device(ctx, dept, vent_model, make_user):
    kim = make_user("director")
    asset = eq.create_asset(tag="CE-10001", device_model=vent_model, department=dept, by=kim, room="12")
    return asset, kim


def test_a_records_changes_in_words_newest_first(device, pump_model):
    asset, kim = device
    vent_name = str(asset.device_model)
    eq.set_status(asset, AssetStatus.OUT_OF_SERVICE, by=kim)
    eq.update_asset(asset, room="14", device_model=pump_model, by=kim)
    entries = history.entries_for(Asset.objects.get(pk=asset.pk))
    assert [e.action for e in entries][-1] == "added" and entries[0].action == "changed"
    latest = {c.field: (c.before, c.after) for c in entries[0].changes}
    assert latest["Room"] == ("12", "14") and latest["Device model"] == (vent_name, str(pump_model))
    status = next(e for e in entries if any(c.field == "Status" for c in e.changes))
    assert {c.field: (c.before, c.after) for c in status.changes}["Status"] == ("In service", "Out of service")
    assert all(e.record == "CE-10001" and e.url == "/equipment/CE-10001/" for e in entries)
    added = entries[-1]
    assert {c.field: c.after for c in added.changes}["Room"] == "12" and all(c.before == "" for c in added.changes)
    assert "Tenant" not in {c.field for e in entries for c in e.changes}


def test_the_change_log_shows_only_what_a_role_can_view(device, make_user, tenant):
    asset, kim = device
    AccessEvent.objects.create(action=AccessEvent.Action.ROLE_CHANGED, by=kim, user=kim, role=Role.objects.get(slug="director"),
                               detail="Technician → Director")
    entries, following = history.change_log(kim)
    assert following is None and {e.area for e in entries} >= {"devices", "access"}
    tech = make_user("technician")  # Users None: no access entries, nor roles
    assert "access" not in {e.area for e in history.change_log(tech)[0]}
    assert "access" not in {a.key for a in history.readable_areas(tech)}
    analyst = make_user("analyst")  # Equipment View, no Contracts Edit... but reads devices
    assert {e.area for e in history.change_log(analyst, areas=["devices"])[0]} == {"devices"}


def test_the_change_log_pages_filters_and_stays_in_its_facility(device, other_tenant, make_user):
    asset, kim = device
    for room in ("20", "21", "22"):
        eq.update_asset(Asset.objects.get(pk=asset.pk), room=room, by=kim)
    page, following = history.change_log(kim, areas=["devices"], limit=2)
    assert len(page) == 2 and following and page[0].changes[0].after == "22"
    rest, following = history.change_log(kim, areas=["devices"], limit=2, after=following)  # the page after the last one shown
    assert [c.after for e in rest for c in e.changes if c.field == "Room"][0] == "20" and rest[-1].action == "added" and following is None
    today = timezone.localdate()
    assert history.change_log(kim, since=today + timedelta(days=1))[0] == []
    assert history.change_log(kim, who=kim.pk + 999)[0] == []
    with tenant_context(other_tenant):
        theirs = DeviceModel.objects.create(manufacturer="GE", model="B650", description="Monitor", category="Monitors")
        Asset.objects.create(tag="THEIRS-1", device_model=theirs, department=Department.objects.create(name="ED"))
    assert "THEIRS-1" not in {e.record for e in history.change_log(kim)[0]}


def rooms(entries) -> list[str]:
    return [{c.field: c.after for c in e.changes}["Room"] for e in entries]


def test_a_save_between_two_pages_neither_repeats_nor_skips_an_entry(device):
    asset, kim = device
    for room in ("20", "21", "22", "23"):
        eq.update_asset(Asset.objects.get(pk=asset.pk), room=room, by=kim)
    everything, _ = history.change_log(kim, areas=["devices"])
    assert rooms(everything) == ["23", "22", "21", "20", "12"]  # newest first, down to the room it was added with
    page, following = history.change_log(kim, areas=["devices"], limit=2)
    assert rooms(page) == ["23", "22"]
    eq.update_asset(Asset.objects.get(pk=asset.pk), room="99", by=kim)  # saved while page 1 is on screen
    rest, following = history.change_log(kim, areas=["devices"], limit=2, after=following)
    assert rest == everything[2:4]  # counted by an offset, "22" would come again
    last, following = history.change_log(kim, areas=["devices"], limit=2, after=following)
    assert last == everything[4:] and last[-1].action == "added" and following is None
    # One record's history (the History tabs) pages the same way
    page, following = history.record_history(Asset.objects.get(pk=asset.pk), limit=2)
    assert rooms(page) == ["99", "23"]
    eq.update_asset(Asset.objects.get(pk=asset.pk), room="100", by=kim)
    rest, following = history.record_history(Asset.objects.get(pk=asset.pk), limit=2, after=following)
    assert rooms(rest) == ["22", "21"] and following is not None
    # A cursor reads back as the key it was made from; anything else is no cursor (the newest page)
    key = history.parse_cursor(following)
    assert key is not None and history.cursor_text(key) == following
    for bad in ("", "2", "nonsense", "2026-10-05~0~1", "2026-10-05T12:00:00~0~1", "2026-10-05T12:00:00+00:00~-1~1", following + "~1"):
        assert history.parse_cursor(bad) is None, bad
    assert history.change_log(kim, areas=["devices"], limit=2, after="nonsense")[0] == history.change_log(kim, areas=["devices"], limit=2)[0]


def test_the_change_log_reads_past_saves_that_change_only_hidden_fields(device, dept, vent_model):
    """A contract's type change re-saves every device it covers for its support type, which the history never shows (it follows the
    contract): a page reads past those saves, up to MAX_ROUNDS windows of a page, to the entries under them, and is never empty while
    older entries lie within reach. Further back, a page may come back empty, but it still says more follow, never that it is the end."""
    asset, kim = device
    contract = ct.create_contract(reference="SC-1", vendor="Philips", type=ContractType.OEM, start_on=date.today() - timedelta(days=30),
                                  end_on=date.today() + timedelta(days=300), annual_cost=Decimal("1200"), by=kim)
    fleet = [Asset.objects.create(tag=f"CE-2{n:03d}", device_model=vent_model, department=dept) for n in range(12)]
    for a in fleet:
        ct.add_asset(contract, a)  # shown: each device's contract
    ct.update_contract(contract, type=ContractType.THIRD_PARTY, by=kim)  # 12 saves of the devices' support type: nothing shown
    assert Asset.history.filter(support_type="third_party").count() == 12
    page, following = history.change_log(kim, areas=["devices"], limit=5)  # 2 windows and a part of nothing shown, then the contracts
    assert len(page) == 5 and following is not None
    assert all([c.field for c in e.changes] == ["Contract"] for e in page)
    page, following = history.change_log(kim, limit=5)  # every area: the contract's own change among them
    assert len(page) == 5 and page[0].area == "contracts" and following is not None
    # Past MAX_ROUNDS windows (5 of 2 saves here), a page can be empty, and still points on; the next one has the entries
    page, following = history.change_log(kim, areas=["devices"], limit=2)
    assert page == [] and following is not None
    page, following = history.change_log(kim, areas=["devices"], limit=2, after=following)
    assert len(page) == 2 and all([c.field for c in e.changes] == ["Contract"] for e in page) and following is not None
    # One record's history reads past them the same way
    entries, _ = history.record_history(Asset.objects.get(pk=fleet[0].pk), limit=1)
    assert [c.field for c in entries[0].changes] == ["Contract"]


def test_a_change_by_someone_of_another_facility_never_names_them(device, other_tenant, make_user):
    asset, kim = device
    create_default_roles(other_tenant)
    stranger = make_user("director", tenant_=other_tenant, username="sandra@other.example")
    stranger.first_name, stranger.last_name = "Sandra", "Stranger"
    stranger.save()
    eq.update_asset(Asset.objects.get(pk=asset.pk), room="30", by=kim)
    Asset.history.filter(id=asset.pk, room="30").update(history_user=stranger)  # as the public form recorded someone signed in elsewhere
    AccessEvent.objects.create(action=AccessEvent.Action.ROLE_CHANGED, by=stranger, user=kim, role=Role.objects.get(slug="director"),
                               detail="Technician → Director")
    entries, _ = history.change_log(kim)
    outside = [e for e in entries if e.who == history.OUTSIDE]
    assert {e.area for e in outside} == {"devices", "access"} and all(e.who_id is None for e in outside)
    assert not any("Stranger" in e.who or "Sandra" in e.who for e in entries)
    latest = history.entries_for(Asset.objects.get(pk=asset.pk))[0]
    assert (latest.who, latest.who_id) == (history.OUTSIDE, None)
    # This facility's people by name, and a platform superuser (no facility) too
    root = User.objects.create_superuser(username="root", password="Test-Pass-2026-x", email="root@example.com")
    Asset.history.filter(id=asset.pk, room="30").update(history_user=root)
    latest = history.entries_for(Asset.objects.get(pk=asset.pk))[0]
    assert (latest.who, latest.who_id) == ("root", root.pk)
    Asset.history.filter(id=asset.pk, room="30").update(history_user=kim)
    latest = history.entries_for(Asset.objects.get(pk=asset.pk))[0]
    assert (latest.who, latest.who_id) == ("Director User", kim.pk)
