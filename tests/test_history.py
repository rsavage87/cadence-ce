"""
The change history reader (apps/core/history.py, slice 20 scaffold): one record's changes in words, newest first, and the facility's
change log across the areas a role can view, inside the request's facility only.
"""
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.accounts.models import AccessEvent, Role
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
    entries, more = history.change_log(kim)
    assert not more and {e.area for e in entries} >= {"devices", "access"}
    tech = make_user("technician")  # Users None: no access entries, nor roles
    assert "access" not in {e.area for e in history.change_log(tech)[0]}
    assert "access" not in {a.key for a in history.readable_areas(tech)}
    analyst = make_user("analyst")  # Equipment View, no Contracts Edit... but reads devices
    assert {e.area for e in history.change_log(analyst, areas=["devices"])[0]} == {"devices"}


def test_the_change_log_pages_filters_and_stays_in_its_facility(device, other_tenant, make_user):
    asset, kim = device
    for room in ("20", "21", "22"):
        eq.update_asset(Asset.objects.get(pk=asset.pk), room=room, by=kim)
    page, more = history.change_log(kim, areas=["devices"], limit=2)
    assert len(page) == 2 and more and page[0].changes[0].after == "22"
    rest, more = history.change_log(kim, areas=["devices"], limit=2, offset=2)
    assert [e.action for e in rest][-1] == "added" and not more
    today = timezone.localdate()
    assert history.change_log(kim, since=today + timedelta(days=1))[0] == []
    assert history.change_log(kim, who=kim.pk + 999)[0] == []
    with tenant_context(other_tenant):
        theirs = DeviceModel.objects.create(manufacturer="GE", model="B650", description="Monitor", category="Monitors")
        Asset.objects.create(tag="THEIRS-1", device_model=theirs, department=Department.objects.create(name="ED"))
    assert "THEIRS-1" not in {e.record for e in history.change_log(kim)[0]}
