"""
Slice 37 review fixes, F3: the API's words, the history's words, and deleting a device model, each on its finding's own scenario.

- C17 / C30 (the API side): the facility's own arrangement (who installs updates here, the segment) reads rules.FACILITY_SOURCE, "the
  model's, for every version", the drawer's words, in GET software-releases/ (the profile and a version on it) and GET device-security/,
  never "the model's disclosure, version not stated"; the disclosure groups keep rules.PROFILE.
- C5 / C9: deleting a device model that the device security register holds (a profile, a release, one entered in error) is refused in
  words that say so, never "has devices" when it has none; the model is never deleted (the register keeps its rows and their history),
  with devices both are named; the words name no version (a user with Equipment Full and no Device security level reads them too); a
  model with nothing recorded still deletes. On PostgreSQL as the runtime role under the policies too.
- C19: a device's network fact set back to blank reads "As the model discloses" in its history and the change log (never "—"); a cleared
  release, its day, and a release's cleared answer read "Not recorded".
"""
import re
from datetime import timedelta

import pytest
from cyber_fixtures import release
from django.utils import timezone
from pg_helpers import as_app_role, needs_postgres

from apps.accounts.models import Level, Module, Role, User
from apps.core import history
from apps.cybersecurity import rules, services
from apps.cybersecurity.models import Connects, Network, PatientData, Segmented, SoftwareRelease, UpdatesBy
from apps.equipment.models import Asset, DeviceModel
from apps.web import asset_cyber

RELEASES = "/api/v1/software-releases/"
DEVICES = "/api/v1/device-security/"
DAY = timedelta(days=1)
KEEPS = "so it cannot be deleted: the register keeps a model's releases and profile, with their history, even a release entered in error."


def never(text: str) -> list:
    return [w for w in rules.NEVER_WORDS if re.search(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", text, re.IGNORECASE)]


def ok(r):
    assert r.status_code == 200, r.content
    return r.json()


@pytest.fixture
def today(ctx):
    return timezone.localdate()


@pytest.fixture
def kim(make_user):
    return make_user("director")


def equipment_only(tenant):
    """Equipment Full and nothing else: deletes a model, never reads the register."""
    role = Role.objects.create(name="Equipment Only", slug="equipment-only")
    role.set_levels({m: Level.FULL if m == Module.EQUIPMENT else Level.NONE for m in Module.values})
    return User.objects.create_user(username="equipment-only@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role)


# --- C17 / C30: the facility's arrangement in the API ---------------------------------------------------------------------------------

@pytest.fixture
def arranged(ctx, pump_model, pump, today):
    """The finding's scenario: the pump model's profile records who installs updates here (CE) and the segment, beside a disclosure;
    CE-10002 runs 9.33.1."""
    profile = release(pump_model, connects=Connects.WIRELESS, patient_data=PatientData.STORES, updates_by=UpdatesBy.CE,
                      segmented=Segmented.ALL)
    new = release(pump_model, "9.33.1")
    services.record_facts(pump, by=None, release=new, release_since=today - 20 * DAY)
    return {"profile": profile, "new": new}


def test_the_facility_group_reads_the_drawers_words_in_the_api(client, make_user, arranged, pump):
    client.force_login(make_user("technician"))
    assert asset_cyber.FACILITY_SOURCE == rules.FACILITY_SOURCE == "the model's, for every version"  # the drawer's words
    rows = {r["version"]: r for r in ok(client.get(RELEASES))["results"]}
    for row in rows.values():  # the profile itself and the version on it
        facility = row["groups"]["facility"]
        assert facility["source"] == rules.FACILITY_SOURCE and facility["updates_by"] == UpdatesBy.CE and facility["segmented"] == "all"
        assert row["groups"]["disclosure"]["source"] == rules.PROFILE  # a disclosure group keeps "the model's disclosure"
    device = ok(client.get(f"{DEVICES}{pump.pk}/"))
    assert device["groups"]["facility"]["source"] == rules.FACILITY_SOURCE
    assert device["groups"]["disclosure"]["source"] == rules.PROFILE and device["groups"]["support"]["source"] == rules.NOT_RECORDED
    for data in (*rows.values(), device):
        assert data["groups"]["facility"]["source"] != rules.PROFILE


def test_no_arrangement_reads_not_recorded(client, make_user, ctx, vent_model, vent):
    release(vent_model, connects=Connects.NONE)
    client.force_login(make_user("technician"))
    assert ok(client.get(f"{DEVICES}{vent.pk}/"))["groups"]["facility"]["source"] == rules.NOT_RECORDED


# --- C5 / C9: deleting a model the register holds -----------------------------------------------------------------------------------

def _delete(client, dm):
    return client.delete(f"/api/v1/device-models/{dm.pk}/")


def test_a_model_with_only_a_profile_is_refused_in_the_registers_words(client, kim, pump_model):
    """C9's scenario: a model with no devices and a profile (never removed): the 400 says what holds it, never "has devices"."""
    release(pump_model, connects=Connects.NONE)
    client.force_login(kim)
    r = _delete(client, pump_model)
    assert not Asset.objects.filter(device_model=pump_model).exists()
    assert r.status_code == 400, r.content
    detail = r.json()["detail"]
    assert detail == f"BD Alaris 8015 PCU has a profile in the Device security register, {KEEPS}"
    assert "has devices" not in detail and not never(detail)
    assert DeviceModel.objects.filter(pk=pump_model.pk).exists()


def test_a_model_with_only_a_release_entered_in_error_is_refused_in_the_registers_words(client, kim, pump_model):
    """C5's scenario: a release recorded by mistake and marked entered in error (never a database delete): the model is kept, and the
    words say why."""
    rel = services.add_release(pump_model, by=kim, version="1.0")
    services.remove_release(rel, by=kim)
    client.force_login(kim)
    r = _delete(client, pump_model)
    assert r.status_code == 400, r.content
    assert r.json()["detail"] == f"BD Alaris 8015 PCU has a software release entered in error in the Device security register, {KEEPS}"
    assert DeviceModel.objects.filter(pk=pump_model.pk).exists() and SoftwareRelease.objects.filter(pk=rel.pk).exists()


def test_several_releases_and_a_profile(client, kim, pump_model):
    release(pump_model, connects=Connects.WIRELESS)
    release(pump_model, "9.19.2")
    services.remove_release(release(pump_model, "9.40"), by=kim)
    client.force_login(kim)
    r = _delete(client, pump_model)
    assert r.status_code == 400
    assert r.json()["detail"] == f"BD Alaris 8015 PCU has software releases and a profile in the Device security register, {KEEPS}"


def test_devices_and_register_rows_are_both_named(client, kim, vent_model, vent):
    release(vent_model, "3.1")
    client.force_login(kim)
    r = _delete(client, vent_model)
    assert r.status_code == 400
    assert r.json()["detail"] == f"Hamilton Medical Hamilton-G5 has devices, and a software release in the Device security register, {KEEPS}"


def test_devices_alone_keep_their_words_and_nothing_recorded_still_deletes(client, kim, vent_model, vent, pump_model):
    client.force_login(kim)
    r = _delete(client, vent_model)
    assert r.status_code == 400 and r.json()["detail"] == "Hamilton Medical Hamilton-G5 has devices, so it cannot be deleted."
    assert _delete(client, pump_model).status_code == 204
    assert not DeviceModel.objects.filter(pk=pump_model.pk).exists()


def test_the_words_name_no_version_for_a_reader_without_the_register(client, tenant, pump_model, kim):
    """Equipment Full with no Device security level: the same words, no version named (the register is its own module)."""
    release(pump_model, "SECRET-VERSION-9", connects=Connects.WIRED)
    client.force_login(equipment_only(tenant))
    assert client.get(RELEASES).status_code == 403  # cannot read the register
    r = _delete(client, pump_model)
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail == f"BD Alaris 8015 PCU has a software release in the Device security register, {KEEPS}"
    assert "SECRET-VERSION-9" not in detail


@needs_postgres
def test_the_refusal_under_the_policies(client, kim, pump_model):
    release(pump_model, connects=Connects.NONE)
    client.force_login(kim)
    as_app_role()
    r = _delete(client, pump_model)
    assert r.status_code == 400 and r.json()["detail"] == f"BD Alaris 8015 PCU has a profile in the Device security register, {KEEPS}"


# --- C19: the history's words for a blank -------------------------------------------------------------------------------------------

def _changes(entries) -> list:
    return [(c.field, c.before, c.after) for e in entries for c in e.changes]


def test_a_network_fact_set_back_reads_as_the_model_discloses(kim, pump, pump_model):
    """C19's scenario: CE-10002 recorded Connected (wired), then set back to "As the model discloses" (blank): its history and the
    change log say so, never "—"."""
    release(pump_model, connects=Connects.WIRELESS)
    services.record_facts(pump, by=kim)  # the row, no network fact of its own
    services.record_facts(pump, by=kim, network=Network.WIRED)
    sec = services.record_facts(pump, by=kim, network="")
    entries, _ = history.record_history(sec)
    changes = _changes(entries)
    assert ("Connected here", "Connected (wired)", "As the model discloses") in changes
    assert ("Connected here", "As the model discloses", "Connected (wired)") in changes
    assert all(history.EMPTY not in (before, after) for field, before, after in changes if field == "Connected here")
    logged = _changes(history.change_log(kim, areas=["device_security"], limit=50)[0])
    assert ("Connected here", "Connected (wired)", "As the model discloses") in logged


def test_a_cleared_release_and_answer_read_not_recorded(kim, pump, pump_model, today):
    rel = release(pump_model, "9.33.1", connects=Connects.WIRELESS)
    services.record_facts(pump, by=kim, release=rel, release_since=today - 3 * DAY)
    sec = services.record_facts(pump, by=kim, release=None)
    changes = _changes(history.record_history(sec)[0])
    assert ("Software release", str(rel), "Not recorded") in changes
    assert ("Running it since", rules.day_words(today - 3 * DAY), "Not recorded") in changes
    services.update_release(rel, by=kim, connects="")
    rel_changes = _changes(history.record_history(rel)[0])
    assert ("Connects, as disclosed", Connects.WIRELESS.label, "Not recorded") in rel_changes
