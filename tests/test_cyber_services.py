"""
Slice 37 wave 1, the register's writers (apps.cybersecurity.services): every refusal, keyed by field and all at once; ids resolved
through the facility (another facility's model, release, or device refused by each writer); the profile and the versions (unique by key,
restored when re-added after entered in error, the create race read back as already on file); a device's facts (the release of its
model, its day, its network fact, checked on and by, history reasons; the create race read back); the bulk set (its cap, one model on
site, one transaction, two queries a device, its history); the levels; and on PostgreSQL the lock order (a release named while it is
being removed: the remove waits or the record refuses, never an IntegrityError).
"""
import threading
import time
import uuid
from datetime import date, datetime, timedelta

import pytest
from cyber_fixtures import release
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from pg_helpers import as_app_role, needs_postgres

from apps.accounts.models import DataScope, Level, Module, Role, User
from apps.cybersecurity import services
from apps.cybersecurity.models import (
    Connects,
    DeviceSecurity,
    Mds2Edition,
    Mds2Status,
    Network,
    OsFamily,
    PatientData,
    Segmented,
    SoftwareRelease,
    UpdatesBy,
)
from apps.cybersecurity.services import KEEP, add_release, record_facts, record_facts_for, remove_release, update_release
from apps.equipment import services as eq
from apps.equipment.models import AddedAs, Asset, AssetStatus, Department, DeviceModel, Ownership, ReturnCleaning, ReturnData
from apps.tenants.context import tenant_context

DAY = timedelta(days=1)


@pytest.fixture
def today(ctx):
    return timezone.localdate()


@pytest.fixture
def kim(make_user):
    return make_user("director")


@pytest.fixture
def tech(make_user):
    return make_user("technician")


def refused(fn, *args, **kwargs) -> dict:
    with pytest.raises(ValidationError) as e:
        fn(*args, **kwargs)
    return {k: v[0] if len(v) == 1 else v for k, v in e.value.message_dict.items()}


def reasons(obj) -> list:
    return list(type(obj).history.filter(id=obj.pk).order_by("history_date", "history_id").values_list("history_change_reason", flat=True))


def device(model, dept, tag, **fields):
    return Asset.objects.create(tag=tag, device_model=model, department=dept, **fields)


@pytest.fixture
def theirs(other_tenant):
    """Another facility's model, release, and device."""
    with tenant_context(other_tenant):
        dept = Department.objects.create(name="Their ICU")
        model = DeviceModel.objects.create(manufacturer="BD", model="Alaris 8015 PCU", description="Infusion pump", category="Infusion pumps")
        rel = release(model, "9.1")
        asset = Asset.objects.create(tag="THEIRS-1", device_model=model, department=dept)
    return {"model": model, "release": rel, "asset": asset}


# --- a model's profile and releases --------------------------------------------------------------------------------------------------

def test_add_a_profile_and_releases(pump_model, kim, today):
    profile = add_release(pump_model, by=kim, connects=Connects.WIRELESS, updates_by=UpdatesBy.CE, segmented=Segmented.SOME,
                          patient_data=PatientData.STORES)
    assert (profile.version, profile.version_key, profile.is_profile, profile.connects, profile.updates_by) == ("", "", True, Connects.WIRELESS,
                                                                                                                UpdatesBy.CE)
    assert reasons(profile) == [services.ADDED] and profile.history.get().history_user == kim
    r = add_release(pump_model, by=kim, version="  V9.19.2 ", os_family=OsFamily.EMBEDDED, security_updates_end_on="2028-03-31",
                    mds2=Mds2Status.ON_FILE, mds2_edition=Mds2Edition.E2019, mds2_document="DEMO-MDS2-0001", mds2_released_on=today - 30 * DAY,
                    mds2_url="https://example.com/disclosures/mds2.pdf?id=77", os_name=None, sbom="")
    assert (r.version, r.version_key, r.security_updates_end_on, r.os_name, r.sbom, r.removed_on) == ("V9.19.2", "9.19.2", date(2028, 3, 31),
                                                                                                    "", "", None)
    assert SoftwareRelease.objects.count() == 2


def test_one_profile_and_one_release_per_version(pump_model, kim):
    add_release(pump_model, by=kim)
    assert refused(add_release, pump_model, by=kim, connects=Connects.WIRED) == {"version": services.PROFILE_EXISTS}
    add_release(pump_model, by=kim, version="Rev. 12.1.2")
    for typed in ("12.1.2", "v12.1.2", "REV 12.1.2", " rev.12.1.2"):
        assert refused(add_release, pump_model, by=kim, version=typed) == {"version": "Version Rev. 12.1.2 is already on file for this model."}


def test_a_versioned_release_refuses_the_facility_group(pump_model, kim):
    assert refused(add_release, pump_model, by=kim, version="9.1", updates_by=UpdatesBy.IT, segmented=Segmented.NONE) == {
        "updates_by": services.FACILITY_ON_PROFILE, "segmented": services.FACILITY_ON_PROFILE}
    assert add_release(pump_model, by=kim, version="9.1", updates_by="", segmented=None).version == "9.1"  # blank: not recorded


def test_another_facilitys_model_is_refused(pump_model, kim, theirs):
    for model in (theirs["model"], theirs["model"].pk, str(theirs["model"].pk), uuid.uuid4(), "not-an-id", None, 7):
        assert refused(add_release, model, by=kim, version="9.1") == {"device_model": services.CHOOSE_MODEL}
    assert add_release(str(pump_model.pk), by=kim, version="9.1").device_model == pump_model


def test_unknown_fields_are_refused(pump_model, kim):
    assert refused(add_release, pump_model, by=kim, notes="admin / admin", removed_on=date(2026, 1, 1)) == {
        "notes": "This is not recorded on a software release.", "removed_on": "This is not recorded on a software release."}
    r = add_release(pump_model, by=kim, version="9.1")
    assert refused(update_release, r, by=kim, version_key="x") == {"version_key": "This is not recorded on a software release."}


def test_every_field_check_at_once(pump_model, kim, today):
    far = services._years_ahead(today, 51)
    problems = refused(add_release, pump_model, by=kim, version="admin@device", os_family="mac", os_name="Win:10",
                       os_support_ends_on="2032-13-01", security_updates_end_on=far, support_checked_on=today + DAY, mds2="on_file",
                       mds2_edition="2020", mds2_document="DEMO 1", mds2_released_on=date(1949, 12, 31), patient_data="yes", connects=3,
                       remote_service="maybe", patching="it", sbom="spdx", mds2_url="http://example.com/mds2.pdf")
    assert set(problems) == {"version", "os_family", "os_name", "os_support_ends_on", "security_updates_end_on", "support_checked_on",
                             "mds2_edition", "mds2_document", "mds2_released_on", "patient_data", "connects", "remote_service", "patching",
                             "sbom", "mds2_url"}
    assert problems["version"].startswith("Enter the version as the manufacturer writes it")
    assert problems["os_name"].startswith("Enter the operating system with its edition")
    assert problems["mds2_document"] == "Enter the MDS2's document number as the form shows it, without spaces."
    assert problems["os_support_ends_on"] == "The OS vendor's last day of support: enter a day."
    assert problems["security_updates_end_on"] == ("The last day of security updates: leave it blank when no day is announced (a day more "
                                                   "than 50 years ahead looks wrong).")
    assert problems["support_checked_on"] == "The day support was checked cannot be after today."
    assert problems["mds2_released_on"] == "The MDS2's release day: enter the day in full (the year looks wrong)."
    assert {problems[k] for k in ("os_family", "mds2_edition", "patient_data", "connects", "remote_service", "patching", "sbom")} == {
        services.CHOICE_WORDS}
    assert problems["mds2_url"] == services.URL_WORDS
    assert not SoftwareRelease.objects.exists()


def test_dates_are_days(pump_model, kim, today):
    for value in (datetime(2028, 1, 1, 12), "31/03/2028", 20280331, "2028-02-30"):
        assert refused(add_release, pump_model, by=kim, version="9.1", security_updates_end_on=value) == {
            "security_updates_end_on": "The last day of security updates: enter a day."}
    limit = today.replace(year=today.year + 50) if (today.month, today.day) != (2, 29) else today.replace(year=today.year + 50, day=28)
    r = add_release(pump_model, by=kim, version="9.1", security_updates_end_on=limit, os_support_ends_on=date(1950, 1, 1),
                    support_checked_on=today, os_family=OsFamily.WINDOWS)
    assert (r.security_updates_end_on, r.os_support_ends_on, r.support_checked_on) == (limit, date(1950, 1, 1), today)
    assert refused(update_release, r, by=kim, security_updates_end_on=limit + DAY)["security_updates_end_on"].endswith("looks wrong).")
    assert update_release(r, by=kim, security_updates_end_on="", support_checked_on=None).security_updates_end_on is None


def test_the_mds2_group(pump_model, kim, today):
    assert refused(add_release, pump_model, by=kim, version="9.1", mds2_edition=Mds2Edition.E2013, mds2_document="DEMO-1",
                   mds2_released_on=today, mds2_url="https://example.com/m.pdf", mds2_requested_on=today) == {
        "mds2_edition": services.ON_FILE_ONLY, "mds2_document": services.ON_FILE_ONLY, "mds2_released_on": services.ON_FILE_ONLY,
        "mds2_url": services.ON_FILE_ONLY, "mds2_requested_on": services.REQUESTED_ONLY}
    assert refused(add_release, pump_model, by=kim, version="9.1", mds2=Mds2Status.ON_FILE, mds2_document="DEMO-1") == {
        "mds2_edition": services.EDITION_NEEDED}
    assert refused(add_release, pump_model, by=kim, version="9.1", mds2=Mds2Status.ON_FILE, mds2_edition=Mds2Edition.LATER,
                   mds2_requested_on=today - DAY) == {"mds2_requested_on": services.REQUESTED_ONLY}
    assert refused(add_release, pump_model, by=kim, version="9.1", mds2=Mds2Status.REQUESTED, mds2_requested_on=today + DAY) == {
        "mds2_requested_on": "The day the MDS2 was requested cannot be after today."}
    asked = add_release(pump_model, by=kim, version="9.1", mds2=Mds2Status.REQUESTED, mds2_requested_on=today - 10 * DAY)
    # on the row with the change applied: the day requested stays until it is cleared
    assert refused(update_release, asked, by=kim, mds2=Mds2Status.ON_FILE, mds2_edition=Mds2Edition.E2019) == {
        "mds2_requested_on": services.REQUESTED_ONLY}
    on_file = update_release(asked, by=kim, mds2=Mds2Status.ON_FILE, mds2_edition=Mds2Edition.E2019, mds2_requested_on=None,
                             mds2_released_on=today - 40 * DAY)
    assert (on_file.mds2, on_file.mds2_edition, on_file.mds2_requested_on) == (Mds2Status.ON_FILE, Mds2Edition.E2019, None)
    assert refused(update_release, on_file, by=kim, mds2="") == {"mds2_edition": services.ON_FILE_ONLY,
                                                                  "mds2_released_on": services.ON_FILE_ONLY}
    none = update_release(on_file, by=kim, mds2=Mds2Status.NONE_FROM_MANUFACTURER, mds2_edition="", mds2_released_on=None)
    assert (none.mds2, none.mds2_edition) == (Mds2Status.NONE_FROM_MANUFACTURER, "")


def test_no_operating_system(pump_model, kim):
    assert refused(add_release, pump_model, by=kim, version="9.1", os_family=OsFamily.NONE, os_name="Windows 10",
                   os_support_ends_on=date(2030, 1, 1)) == {"os_name": services.NO_OS_NAME, "os_support_ends_on": services.NO_OS_DAY}
    r = add_release(pump_model, by=kim, version="9.1", os_family=OsFamily.WINDOWS, os_name="Windows 10 IoT Enterprise LTSC 2021")
    assert refused(update_release, r, by=kim, os_family=OsFamily.NONE) == {"os_name": services.NO_OS_NAME}
    assert update_release(r, by=kim, os_family=OsFamily.NONE, os_name="").os_family == OsFamily.NONE


@pytest.mark.parametrize("url,ok", [
    ("https://example.com/disclosures/mds2.pdf", True),
    ("https://example.com/mds2.pdf?id=77&lang=en", True),
    ("https://example.com/mds2.pdf?keyword=pump", True),  # review round 2: an ordinary word containing "key" is not a credential
    ("http://example.com/mds2.pdf", False),
    ("ftp://example.com/mds2.pdf", False),
    ("https://user:pass@example.com/mds2.pdf", False),
    ("https://user@example.com/mds2.pdf", False),
    ("https://example.com/mds2.pdf?token=abc", False),
    ("https://example.com/mds2.pdf?Token=abc", False),
    ("https://example.com/mds2.pdf?access_token=abc", False),
    ("https://example.com/mds2.pdf?apikey=abc", False),
    ("https://example.com/mds2.pdf?api-key=abc", False),
    ("https://example.com/mds2.pdf?sig=abc", False),
    ("https://example.com/mds2.pdf?Signature=abc&Expires=1", False),
    ("https://bucket.s3.amazonaws.com/mds2.pdf?X-Amz-Credential=abc", False),
    ("https://example.com/mds2.pdf?session=abc", False),
    ("https://example.com/mds2.pdf?auth=abc", False),
    ("https://example.com/mds2.pdf?pwd=abc", False),
    ("https://example.com/mds2.pdf#access_token=abc", False),
    ("https://example.com/" + "a" * 290, False),
    ("not a link", False),
])
def test_the_mds2_link(pump_model, kim, url, ok):
    fields = {"version": "9.1", "mds2": Mds2Status.ON_FILE, "mds2_edition": Mds2Edition.E2019, "mds2_url": url}
    if ok:
        assert add_release(pump_model, by=kim, **fields).mds2_url == url
    else:
        assert refused(add_release, pump_model, by=kim, **fields) == {"mds2_url": services.URL_WORDS}


def test_text_is_stripped_and_kept_to_its_length(pump_model, kim):
    r = add_release(pump_model, by=kim, version=" Rel. M.00.03 ", os_family=OsFamily.WINDOWS, os_name="  Windows 10 IoT Enterprise LTSC 2021 ",
                    mds2=Mds2Status.ON_FILE, mds2_edition=Mds2Edition.E2019, mds2_document=" DEMO-MDS2-0001 ")
    assert (r.version, r.os_name, r.mds2_document) == ("Rel. M.00.03", "Windows 10 IoT Enterprise LTSC 2021", "DEMO-MDS2-0001")
    assert refused(add_release, pump_model, by=kim, version="9" * 61, os_name="W" * 81, mds2_document="D" * 41) == {
        "version": "Keep it to 60 characters.", "os_name": "Keep it to 80 characters.", "mds2_document": "Keep it to 40 characters."}
    assert refused(add_release, pump_model, by=kim, version="9.1\n2") == {
        "version": "Enter the version as the manufacturer writes it: letters, digits, spaces, and . _ / ( ) + - only."}
    assert refused(add_release, pump_model, by=kim, version=9.1) == {"version": "Enter it as text."}


def test_restore_a_version_entered_in_error(pump_model, kim, tech, today):
    r = add_release(pump_model, by=kim, version="9.19.2", connects=Connects.WIRELESS, security_updates_end_on=date(2028, 3, 31))
    remove_release(r, by=kim)
    assert refused(update_release, r, by=kim, connects=Connects.WIRED) == {"release": services.REMOVED_RELEASE}
    back = add_release(pump_model, by=tech, version="V9.19.2", connects=Connects.WIRED)
    assert back.pk == r.pk and SoftwareRelease.objects.count() == 1
    assert (back.version, back.removed_on, back.removed_by, back.connects, back.security_updates_end_on) == (
        "V9.19.2", None, None, Connects.WIRED, date(2028, 3, 31))  # the given fields over what it had
    assert reasons(back) == [services.ADDED, services.ENTERED_IN_ERROR, services.RESTORED]
    assert back.history.order_by("-history_date", "-history_id").first().history_user == tech
    assert refused(add_release, pump_model, by=kim, version="9.19.2") == {"version": "Version V9.19.2 is already on file for this model."}


def test_a_restore_is_checked_as_the_row_it_makes(pump_model, kim):
    r = add_release(pump_model, by=kim, version="9.1", mds2=Mds2Status.ON_FILE, mds2_edition=Mds2Edition.E2019)
    remove_release(r, by=kim)
    assert refused(add_release, pump_model, by=kim, version="9.1", mds2="") == {"mds2_edition": services.ON_FILE_ONLY}
    assert SoftwareRelease.objects.get().removed_on is not None


def test_update_on_the_fresh_row_and_its_history(pump_model, kim, tech):
    r = add_release(pump_model, by=kim, version="9.1")
    stale = SoftwareRelease.objects.get(pk=r.pk)
    update_release(r, by=kim, os_family=OsFamily.LINUX, os_name="Debian 12")
    out = update_release(stale, by=tech, connects=Connects.WIRED)
    assert (out.os_name, out.connects, stale.os_name, stale.connects) == ("Debian 12", Connects.WIRED, "Debian 12", Connects.WIRED)
    assert reasons(r) == [services.ADDED, services.EDITED, services.EDITED]
    update_release(stale, by=tech, connects=Connects.WIRED, os_name=" Debian 12 ")  # nothing changes: no history
    assert reasons(r) == [services.ADDED, services.EDITED, services.EDITED]


def test_the_version_changes_only_while_no_device_names_it(pump, pump_model, kim):
    profile = add_release(pump_model, by=kim)
    r = add_release(pump_model, by=kim, version="9.1")
    other = add_release(pump_model, by=kim, version="9.2")
    gone = add_release(pump_model, by=kim, version="9.0")
    remove_release(gone, by=kim)
    assert refused(update_release, profile, by=kim, version="9.3") == {"version": services.PROFILE_VERSIONLESS}
    assert update_release(profile, by=kim, version="", connects=Connects.WIRED).version == ""
    assert refused(update_release, r, by=kim, version=" ") == {"version": services.VERSION_NEEDED}
    assert refused(update_release, r, by=kim, version="v9.2") == {"version": "Version 9.2 is already on file for this model."}
    assert refused(update_release, r, by=kim, version="9.0") == {"version": "Version 9.0 was entered in error for this model: add it again to "
                                                                            "restore it."}
    assert update_release(r, by=kim, version="9.1a").version_key == "9.1a"
    record_facts(pump, by=kim, release=r)
    assert refused(update_release, r, by=kim, version="9.1b") == {"version": services.VERSION_NAMED}
    respelled = update_release(r, by=kim, version="V9.1A")  # the same version, by its key: any time
    assert (respelled.version, respelled.version_key) == ("V9.1A", "9.1a")
    assert refused(update_release, other, by=kim, updates_by=UpdatesBy.IT) == {"updates_by": services.FACILITY_ON_PROFILE}
    assert update_release(profile, by=kim, updates_by=UpdatesBy.IT, segmented=Segmented.ALL).segmented == Segmented.ALL


def test_another_facilitys_release_is_refused_by_each_writer(pump, pump_model, kim, theirs):
    for rel in (theirs["release"], theirs["release"].pk, str(theirs["release"].pk), "x"):
        assert refused(update_release, rel, by=kim, connects=Connects.WIRED) == {"release": services.CHOOSE_RELEASE}
        assert refused(remove_release, rel, by=kim) == {"release": services.CHOOSE_RELEASE}
        assert refused(record_facts, pump, by=kim, release=rel) == {"release": services.CHOOSE_VERSION}
        assert refused(record_facts_for, pump_model, [pump], by=kim, release=rel) == {"release": services.CHOOSE_MODEL_VERSION}
    with tenant_context(theirs["release"].tenant):
        assert SoftwareRelease.objects.get().connects == "" and SoftwareRelease.objects.get().removed_on is None


def test_remove_a_release(pump, pump_model, vent, kim, tech, today, dept):
    profile = add_release(pump_model, by=kim)
    r = add_release(pump_model, by=kim, version="9.1")
    assert refused(remove_release, profile, by=kim) == {"release": services.PROFILE_KEPT}
    record_facts(pump, by=kim, release=r)
    assert refused(remove_release, r, by=kim) == {"release": "1 device runs this release: set it to another first."}
    second = device(pump_model, dept, "CE-20002")
    record_facts(second, by=kim, release=r)
    assert refused(remove_release, r, by=kim) == {"release": "2 devices run this release: set them to another first."}
    eq.set_status(second, AssetStatus.RETIRED)  # review fix C4: a retired device no longer counts (devices on site, as the panel)
    assert refused(remove_release, r, by=kim) == {"release": "1 device runs this release: set it to another first."}
    record_facts(pump, by=kim, release=None)
    assert remove_release(r, by=kim).removed_on == today  # its record still names it (its facts stay), read as not recorded
    assert DeviceSecurity.objects.get(asset=second).release_id == r.pk
    third = add_release(pump_model, by=kim, version="9.2")
    out = remove_release(third, by=tech, today=today - DAY)
    assert (out.removed_on, out.removed_by, third.removed_on) == (today - DAY, tech, today - DAY)
    assert reasons(third) == [services.ADDED, services.ENTERED_IN_ERROR]
    assert refused(remove_release, third, by=kim) == {"release": services.ALREADY_REMOVED}


# --- a device's facts -------------------------------------------------------------------------------------------------------------

@pytest.fixture
def pumps(pump_model, kim):
    return {"profile": add_release(pump_model, by=kim, connects=Connects.WIRELESS), "old": add_release(pump_model, by=kim, version="9.19.2"),
            "new": add_release(pump_model, by=kim, version="9.33.1")}


def test_record_facts(pump, pumps, kim, tech, today):
    sec = record_facts(pump, by=tech, release=pumps["old"], network=Network.WIRELESS)
    assert (sec.release, sec.release_since, sec.network, sec.checked_on, sec.checked_by) == (pumps["old"], today, Network.WIRELESS, today, tech)
    assert reasons(sec) == ["Facts recorded: release set to 9.19.2"] and sec.history.get().history_user == tech
    sec = record_facts(pump, by=kim, release_since=today - 30 * DAY, checked_on=today - 2 * DAY)  # the same release: a correction
    assert (sec.release, sec.release_since, sec.network, sec.checked_on, sec.checked_by) == (pumps["old"], today - 30 * DAY, Network.WIRELESS,
                                                                                             today - 2 * DAY, kim)
    sec = record_facts(pump, by=kim, release=pumps["old"], network="")  # the same release again: its day kept; as the model discloses
    assert (sec.release_since, sec.network, sec.checked_on) == (today - 30 * DAY, "", today)
    sec = record_facts(pump, by=kim, release=str(pumps["new"].pk), release_since=(today - 3 * DAY).isoformat())
    assert (sec.release, sec.release_since) == (pumps["new"], today - 3 * DAY)
    sec = record_facts(pump, by=kim, release=None)
    assert (sec.release, sec.release_since) == (None, None)
    assert reasons(sec) == ["Facts recorded: release set to 9.19.2", "Facts recorded", "Facts recorded", "Facts recorded: release set to 9.33.1",
                            "Facts recorded: release cleared"]
    assert DeviceSecurity.objects.count() == 1


def test_record_facts_by_id(pump, pumps, kim):
    assert record_facts(str(pump.pk), by=kim, network=Network.NOT_CONNECTED).asset_id == pump.pk
    assert record_facts(pump.pk, by=kim, release=pumps["old"]).release == pumps["old"]


def test_the_release_must_be_a_version_of_the_devices_model(pump, pumps, vent_model, pump_model, kim):
    removed = add_release(pump_model, by=kim, version="9.0")
    remove_release(removed, by=kim)
    vents = add_release(vent_model, by=kim, version="3.1")
    for rel in (pumps["profile"], removed, vents, uuid.uuid4()):
        assert refused(record_facts, pump, by=kim, release=rel) == {"release": services.CHOOSE_VERSION}
    assert not DeviceSecurity.objects.exists()


def test_the_release_since_day(pump, pumps, vent, vent_model, kim, today):
    assert refused(record_facts, pump, by=kim, release=pumps["old"], release_since=today + DAY) == {
        "release_since": "Running it since cannot be after today."}
    installed = vent.installed_on
    three = add_release(vent_model, by=kim, version="3.1")
    assert refused(record_facts, vent, by=kim, release=three, release_since=installed - DAY) == {
        "release_since": f"Running it since cannot be before the device was installed ({installed:%b} {installed.day}, {installed.year})."}
    assert record_facts(vent, by=kim, release=three, release_since=installed).release_since == installed
    assert refused(record_facts, pump, by=kim, release=None, release_since=today) == {"release_since": services.NO_RELEASE_SINCE}
    assert refused(record_facts, pump, by=kim, release_since=today) == {"release_since": services.NO_RELEASE_SINCE}  # none recorded
    assert refused(record_facts, pump, by=kim, release=pumps["old"], release_since="yesterday") == {
        "release_since": "Running it since: enter a day."}
    assert record_facts(pump, by=kim, release=None, release_since=None).release is None


def test_network_and_checked_on(pump, kim, today):
    assert refused(record_facts, pump, by=kim, network="vlan 12", checked_on=today + DAY) == {
        "network": services.NETWORK_WORDS, "checked_on": "The day checked cannot be after today."}
    assert refused(record_facts, pump, by=kim, network=3) == {"network": services.NETWORK_WORDS}
    sec = record_facts(pump, by=kim, network=Network.INTERMITTENT, checked_on="")
    assert (sec.network, sec.checked_on, sec.release) == (Network.INTERMITTENT, today, None)
    assert record_facts(pump, by=kim, network=KEEP, checked_on=today - DAY).network == Network.INTERMITTENT


def test_another_facilitys_device_is_refused(pump_model, pumps, kim, theirs):
    for asset in (theirs["asset"], theirs["asset"].pk, "x", None):
        assert refused(record_facts, asset, by=kim, network=Network.WIRED) == {"asset": services.CHOOSE_DEVICE}
    assert refused(record_facts_for, pump_model, [theirs["asset"]], by=kim, release=pumps["old"]) == {"assets": services.CHOOSE_DEVICES}
    assert refused(record_facts_for, pump_model, ["x"], by=kim, release=pumps["old"]) == {"assets": services.CHOOSE_DEVICES}
    assert refused(record_facts_for, theirs["model"], [], by=kim, release=pumps["old"]) == {"device_model": services.CHOOSE_MODEL}
    with tenant_context(theirs["asset"].tenant):
        assert not DeviceSecurity.objects.exists()


def test_retired_and_returned_devices_keep_their_facts(ctx, pump, pumps, pump_model, dept, kim, today):
    sec = record_facts(pump, by=kim, release=pumps["old"], network=Network.WIRELESS)
    eq.set_status(pump, AssetStatus.RETIRED)
    assert refused(record_facts, pump, by=kim, network=Network.WIRED) == {"asset": f"{pump.tag} is retired: its facts stay as they were."}
    rental = eq.add_temporary_device(tag="R-1", device_model=pump_model, department=dept, kind=Ownership.RENTAL, owner="Acme Rentals",
                                     serial="SN-1", added_as=AddedAs.EXISTING, arrived_on=today - 5 * DAY)
    record_facts(rental, by=kim, release=pumps["new"])
    eq.return_to_owner(rental, cleaning=ReturnCleaning.DECONTAMINATED, data=ReturnData.NONE_STORED)
    assert refused(record_facts, rental, by=kim, network=Network.WIRED) == {
        "asset": "R-1 was returned to its owner: its facts stay as they were."}
    sec.refresh_from_db()
    assert (sec.release, sec.network) == (pumps["old"], Network.WIRELESS)


def test_a_missing_or_waiting_device_is_still_on_site(ctx, pump_model, pumps, dept, kim):
    waiting = eq.create_asset(tag="N-1", device_model=pump_model, department=dept, incoming_inspection="waiting")
    assert record_facts(waiting, by=kim, release=pumps["new"]).release == pumps["new"]
    lost = eq.create_asset(tag="N-2", device_model=pump_model, department=dept)
    eq.set_status(lost, AssetStatus.MISSING)
    assert record_facts(lost, by=kim, network=Network.WIRELESS).network == Network.WIRELESS


# --- the levels ----------------------------------------------------------------------------------------------------------------------

def _custom(tenant, slug, levels, scope=""):
    role = Role.objects.create(name=slug.title(), slug=slug, scope=scope)
    role.set_levels(levels)
    return User.objects.create_user(username=f"{slug}@x.example", password="Test-Pass-2026-x", tenant=tenant, role=Role.objects.get(pk=role.pk))


def test_the_levels(ctx, pump, pump_model, pumps, make_user):
    analyst = make_user("analyst")  # Device security None
    it = _custom(ctx, "it-security", {Module.CYBERSECURITY: Level.EDIT, Module.EQUIPMENT: Level.VIEW})
    no_equipment = _custom(ctx, "no-equipment", {Module.CYBERSECURITY: Level.FULL})
    viewer = _custom(ctx, "viewer", {Module.CYBERSECURITY: Level.VIEW, Module.EQUIPMENT: Level.FULL})
    scoped = _custom(ctx, "scoped", {Module.CYBERSECURITY: Level.FULL, Module.EQUIPMENT: Level.FULL}, scope=DataScope.COMPANY)
    for user in (analyst, viewer, scoped):
        for call in (lambda: add_release(pump_model, by=user, version="1.0"), lambda: update_release(pumps["old"], by=user, sbom="available"),
                     lambda: remove_release(pumps["new"], by=user), lambda: record_facts(pump, by=user, network=Network.WIRED),
                     lambda: record_facts_for(pump_model, [pump], by=user, release=pumps["old"])):
            with pytest.raises(PermissionDenied):
                call()
    assert add_release(pump_model, by=no_equipment, version="1.0").version == "1.0"  # a model's releases: Device security alone
    with pytest.raises(PermissionDenied, match="Equipment View"):
        record_facts(pump, by=no_equipment, network=Network.WIRED)
    assert record_facts(pump, by=it, release=pumps["old"]).checked_by == it
    assert record_facts_for(pump_model, [pump], by=it, release=pumps["new"]) == 1
    assert record_facts(pump, by=None, network=Network.WIRED).checked_by is None  # the system: the seed


# --- the bulk set ---------------------------------------------------------------------------------------------------------------

def test_set_one_models_devices(ctx, pump_model, pumps, dept, kim, today):
    a, b, c = (device(pump_model, dept, f"P-{i}") for i in (1, 2, 3))
    record_facts(b, by=kim, release=pumps["old"], release_since=today - 100 * DAY, network=Network.WIRED)
    record_facts(c, by=kim, release=pumps["new"], release_since=today - 50 * DAY)
    assert record_facts_for(pump_model, Asset.objects.filter(pk__in=[a.pk, b.pk, c.pk]), by=kim, release=pumps["new"]) == 2
    rows = {s.asset.tag: s for s in DeviceSecurity.objects.select_related("asset")}
    assert {t: (s.release.version, s.release_since, s.network, s.checked_on, s.checked_by) for t, s in rows.items()} == {
        "P-1": ("9.33.1", today, "", today, kim), "P-2": ("9.33.1", today, Network.WIRED, today, kim),
        "P-3": ("9.33.1", today - 50 * DAY, "", today, kim)}  # already on it: left as it was
    assert reasons(rows["P-1"]) == ["Set with 3 devices of BD Alaris 8015 PCU"]
    assert reasons(rows["P-2"])[-1] == "Set with 3 devices of BD Alaris 8015 PCU" and reasons(rows["P-3"]) == ["Facts recorded: release set to 9.33.1"]
    assert rows["P-1"].history.get().history_user == kim
    assert record_facts_for(pump_model, [a, b, c], by=kim, release=pumps["new"]) == 0  # nothing to change
    n = record_facts_for(pump_model, [a.pk, str(b.pk), c], by=kim, release=pumps["new"], release_since=today - 7 * DAY, network=Network.WIRELESS)
    assert n == 3 and set(DeviceSecurity.objects.values_list("release_since", "network")) == {(today - 7 * DAY, Network.WIRELESS)}
    assert record_facts_for(pump_model, [a], by=kim, release=pumps["old"]) == 1
    assert reasons(DeviceSecurity.objects.get(asset=a))[-1] == "Set with 1 device of BD Alaris 8015 PCU"


def test_the_bulk_refusals(ctx, pump, vent, pump_model, pumps, dept, kim, today):
    assert refused(record_facts_for, pump_model, [], by=kim, release=pumps["old"]) == {"assets": services.NO_DEVICES}
    assert refused(record_facts_for, pump_model, [uuid.uuid4() for _ in range(services.BULK_MAX + 1)], by=kim, release=pumps["old"]) == {
        "assets": services.TOO_MANY}
    assert services.TOO_MANY == "Set at most 500 devices at once: choose a department to narrow them."
    assert refused(record_facts_for, pump_model, [uuid.uuid4() for _ in range(services.BULK_MAX)], by=kim, release=pumps["old"]) == {
        "assets": services.CHOOSE_DEVICES}  # at the cap: past it, then each looked up in the facility
    assert refused(record_facts_for, pump_model, [pump, vent], by=kim, release=pumps["old"]) == {
        "assets": "CE-10001 is not BD Alaris 8015 PCU: set one model's devices at a time."}
    assert refused(record_facts_for, pump_model, [pump], by=kim, release=None, release_since=today + DAY, network="lan") == {
        "release": services.CHOOSE_MODEL_VERSION, "release_since": "Running it since cannot be after today.", "network": services.NETWORK_WORDS}
    assert refused(record_facts_for, pump_model, [pump], by=kim, release=pumps["profile"]) == {"release": services.CHOOSE_MODEL_VERSION}
    gone = device(pump_model, dept, "P-GONE", status=AssetStatus.RETIRED)
    assert refused(record_facts_for, pump_model, [pump, gone], by=kim, release=pumps["old"]) == {
        "assets": "P-GONE is no longer on site: their facts stay as they were."}
    late = [device(pump_model, dept, f"P-L{i}", installed_on=today - DAY) for i in range(7)]
    assert refused(record_facts_for, pump_model, [pump, *late], by=kim, release=pumps["old"], release_since=today - 10 * DAY) == {
        "release_since": "P-L0, P-L1, P-L2, P-L3, P-L4 and 2 more were installed after this day: set them apart, or choose a later day."}
    assert not DeviceSecurity.objects.exists()


def test_the_bulk_set_is_one_transaction(ctx, pump_model, pumps, dept, kim, monkeypatch):
    devices = [device(pump_model, dept, f"P-{i}") for i in range(3)]
    real, calls = services._save, []

    def failing(obj, by, reason):
        calls.append(obj)
        if len(calls) == 2:
            raise RuntimeError("the database went away")
        real(obj, by, reason)

    monkeypatch.setattr(services, "_save", failing)
    with pytest.raises(RuntimeError):
        record_facts_for(pump_model, devices, by=kim, release=pumps["old"])
    assert not DeviceSecurity.objects.exists() and not DeviceSecurity.history.exists()


def test_the_bulk_set_costs_two_queries_a_device(ctx, pump_model, pumps, dept, kim):
    """Six fixed queries (the model, the register lock's two, the release locked, the devices, their rows locked), two more for a user's
    level checks, and two a changed device (its row, its history row), new or not."""
    devices = [device(pump_model, dept, f"P-{i:02d}") for i in range(11)]
    record_facts(devices[0], by=None, release=pumps["old"])  # one with a row already

    def counted(assets, release_, by):
        with CaptureQueriesContext(connection) as q:
            changed = record_facts_for(pump_model, assets, by=by, release=release_)
        return changed, len([s for s in q.captured_queries if not s["sql"].startswith(("SAVEPOINT", "RELEASE SAVEPOINT"))])

    assert counted(devices[:1], pumps["new"], None) == (1, 6 + 2)
    assert counted(devices, pumps["old"], None) == (11, 6 + 2 * 11)
    assert counted(devices, pumps["new"], kim) == (11, 8 + 2 * 11)  # a user's level checks: two more
    assert counted(devices, pumps["new"], kim) == (0, 8)  # nothing to change: nothing written


# --- PostgreSQL: the races and the lock order --------------------------------------------------------------------------------------

def _thread(target, errors, results, key):
    def run():
        try:
            results[key] = target()
        except Exception as e:  # noqa: BLE001 - reported by the test
            errors[key] = e
        finally:
            connection.close()
    return threading.Thread(target=run, name=key)


def _both(first, second, errors, results):
    a, b = _thread(first, errors, results, "first"), _thread(second, errors, results, "second")
    a.start()
    b.start()
    for t in (a, b):
        t.join(30)
    assert not a.is_alive() and not b.is_alive()


def _another_waits_on_a_lock(timeout: float = 10) -> bool:
    """Until another connection to this database waits on a lock (pg_stat_activity), read on this thread's connection (inside its
    transaction: the statistics snapshot is cleared before each look, else the first one is read again until the transaction ends)."""
    deadline = time.monotonic() + timeout
    with connection.cursor() as cur:
        while time.monotonic() < deadline:
            cur.execute("SELECT pg_stat_clear_snapshot()")
            cur.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid() "
                        "AND wait_event_type = 'Lock'")
            if cur.fetchone()[0]:
                return True
            time.sleep(0.05)
    return False


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_two_adds_of_one_version_at_once(tenant, monkeypatch):
    """Review round 3 changed this test: every register writer takes the facility's register lock first, so two adds of one version
    queue. The first pauses after its field check (it has looked: no release) until the second waits on a lock (the register's); the
    second then looks, finds the first's release, and reads as already on file, in words, before any insert (no IntegrityError read
    back). (Was: both passed the look behind a barrier, and the second's insert failed on the unique key, read back as on file.)"""
    with tenant_context(tenant):
        model = DeviceModel.objects.create(manufacturer="BD", model="Alaris", description="Pump", category="Infusion pumps")
    started, looked, waited = threading.Event(), {}, {}
    real = services._check_row

    def check_row(values, *, versioned, errors):
        name = threading.current_thread().name
        looked[name] = errors.get("version")  # what its look for the version found
        real(values, versioned=versioned, errors=errors)
        if name == "first":
            started.set()
            waited["first"] = _another_waits_on_a_lock()

    monkeypatch.setattr(services, "_check_row", check_row)
    errors, results = {}, {}

    def add(typed, wait):
        def call():
            if wait:
                assert started.wait(20)
            with tenant_context(tenant):
                return add_release(model, by=None, version=typed).version
        return call

    _both(add("9.33.1", False), add("V9.33.1", True), errors, results)
    assert results == {"first": "9.33.1"} and isinstance(errors.get("second"), ValidationError), (results, errors)
    assert errors["second"].message_dict == {"version": ["Version 9.33.1 is already on file for this model."]}
    assert waited == {"first": True}  # the second waited for the first
    assert looked == {"first": None, "second": "Version 9.33.1 is already on file for this model."}  # refused by its look
    with tenant_context(tenant):
        assert SoftwareRelease.objects.count() == 1


def _hold_device_lock(monkeypatch, who: str, held: threading.Event, waited: dict):
    """The thread named `who` holds the devices' rows (services._locked_devices: after the register lock and the release, before any
    security row) until another connection waits on a lock (waited[who]: whether one did)."""
    real = services._locked_devices

    def slow(ids):
        found = real(ids)
        if threading.current_thread().name == who:
            held.set()
            waited[who] = _another_waits_on_a_lock()
        return found

    monkeypatch.setattr(services, "_locked_devices", slow)


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_two_records_on_a_new_device_at_once(tenant, monkeypatch):
    """Review round 3 changed this test: every register writer takes the facility's register lock first, so the second record waits
    for the first's commit (on the register lock, not the device's row: round 2's FOR KEY SHARE let both through), then records on the
    row the first created. One row, two history rows, no IntegrityError. (Review fixes C2 and C3: the second waited on the device's row
    FOR UPDATE; before them: both found no row behind a barrier, and the loser's insert read the row back.)"""
    with tenant_context(tenant):
        dept = Department.objects.create(name="ICU")
        model = DeviceModel.objects.create(manufacturer="BD", model="Alaris", description="Pump", category="Infusion pumps")
        rel = release(model, "9.33.1")
        asset = Asset.objects.create(tag="CE-1", device_model=model, department=dept)
    held, waited = threading.Event(), {}
    _hold_device_lock(monkeypatch, "first", held, waited)
    real, calls = services._reason, []

    def counted(*args, **kwargs):
        calls.append(threading.current_thread().name)
        return real(*args, **kwargs)

    monkeypatch.setattr(services, "_reason", counted)
    errors, results = {}, {}

    def first():
        with tenant_context(tenant):
            return record_facts(asset, by=None, release=rel).release_id

    def second():
        assert held.wait(20)
        with tenant_context(tenant):
            return record_facts(asset, by=None, network=Network.WIRELESS).network

    _both(first, second, errors, results)
    assert not errors and results == {"first": rel.pk, "second": Network.WIRELESS}, (results, errors)
    assert waited == {"first": True}  # the second waited for the first (on the register lock)
    assert calls == ["first", "second"]  # one record each: the second found the first's row
    with tenant_context(tenant):
        sec = DeviceSecurity.objects.get()
        assert (sec.release_id, sec.network) == (rel.pk, Network.WIRELESS) and sec.history.count() == 2


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_a_record_that_loses_a_create_race_reads_the_row_back(tenant, monkeypatch):
    """A security row inserted meanwhile by a writer that does not lock the device (none in the app since review fixes C2 and C3; the
    fixtures here): the record's insert fails on the one-to-one key, reads the row back locked, and records on it, never an
    IntegrityError."""
    _model, rel, asset = _setup_release(tenant)
    barrier, inserted = threading.Barrier(2, timeout=20), threading.Event()
    real, calls = services._reason, []

    def meet(*args, **kwargs):
        calls.append(threading.current_thread().name)
        if len(calls) == 1:
            barrier.wait()  # the record has looked: no row
            assert inserted.wait(20)
        return real(*args, **kwargs)

    monkeypatch.setattr(services, "_reason", meet)
    errors, results = {}, {}

    def first():
        with tenant_context(tenant):
            return record_facts(asset, by=None, release=rel).release_id

    def second():
        barrier.wait()
        with tenant_context(tenant):
            DeviceSecurity.objects.create(asset=asset, network=Network.WIRED)
        inserted.set()
        return "inserted"

    _both(first, second, errors, results)
    assert not errors and results == {"first": rel.pk, "second": "inserted"}, (results, errors)
    assert len(calls) == 2  # the insert failed, and it recorded again on the row read back
    with tenant_context(tenant):
        sec = DeviceSecurity.objects.get()
        assert (sec.release_id, sec.network) == (rel.pk, Network.WIRED) and sec.history.count() == 2


def _hold_release_lock(monkeypatch, who: str, held: threading.Event):
    """The thread named `who` sleeps while it holds a release's lock (longer than PostgreSQL's deadlock_timeout)."""
    real = services._locked_release

    def slow(*args, **kwargs):
        found = real(*args, **kwargs)
        if threading.current_thread().name == who:
            held.set()
            time.sleep(1.5)
        return found

    monkeypatch.setattr(services, "_locked_release", slow)


def _setup_release(tenant):
    with tenant_context(tenant):
        dept = Department.objects.create(name="ICU")
        model = DeviceModel.objects.create(manufacturer="BD", model="Alaris", description="Pump", category="Infusion pumps")
        rel = release(model, "9.33.1")
        asset = Asset.objects.create(tag="CE-1", device_model=model, department=dept)
    return model, rel, asset


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_a_release_named_while_it_is_being_removed(tenant, monkeypatch):
    """The remove holds the release's lock: the record waits for it, then finds the release entered in error and refuses in words."""
    _model, rel, asset = _setup_release(tenant)
    held = threading.Event()
    _hold_release_lock(monkeypatch, "first", held)
    errors, results = {}, {}

    def remove():
        with tenant_context(tenant):
            return remove_release(rel, by=None).removed_on

    def name():
        assert held.wait(20)
        with tenant_context(tenant):
            return record_facts(asset, by=None, release=rel).release_id

    _both(remove, name, errors, results)
    assert "first" in results and isinstance(errors.get("second"), ValidationError), (results, errors)
    assert errors["second"].message_dict == {"release": [services.CHOOSE_VERSION]}
    with tenant_context(tenant):
        assert not DeviceSecurity.objects.exists() and SoftwareRelease.objects.get().removed_on is not None


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_a_release_removed_while_it_is_being_named(tenant, monkeypatch):
    """The record holds the release's lock: the remove waits for it, then counts the device and refuses in words."""
    _model, rel, asset = _setup_release(tenant)
    held = threading.Event()
    _hold_release_lock(monkeypatch, "first", held)
    errors, results = {}, {}

    def name():
        with tenant_context(tenant):
            return record_facts(asset, by=None, release=rel).release_id

    def remove():
        assert held.wait(20)
        with tenant_context(tenant):
            return remove_release(rel, by=None).removed_on

    _both(name, remove, errors, results)
    assert results.get("first") == rel.pk and isinstance(errors.get("second"), ValidationError), (results, errors)
    assert errors["second"].message_dict == {"release": ["1 device runs this release: set it to another first."]}
    with tenant_context(tenant):
        assert SoftwareRelease.objects.get().removed_on is None and DeviceSecurity.objects.get().release_id == rel.pk


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_a_bulk_set_while_the_release_is_removed(tenant, monkeypatch):
    model, rel, asset = _setup_release(tenant)
    held = threading.Event()
    _hold_release_lock(monkeypatch, "first", held)
    errors, results = {}, {}

    def remove():
        with tenant_context(tenant):
            return remove_release(rel, by=None).removed_on

    def bulk():
        assert held.wait(20)
        with tenant_context(tenant):
            return record_facts_for(model, [asset], by=None, release=rel)

    _both(remove, bulk, errors, results)
    assert "first" in results and errors["second"].message_dict == {"release": [services.CHOOSE_MODEL_VERSION]}, (results, errors)


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_a_bulk_set_waits_for_a_record_on_a_new_device(tenant, monkeypatch):
    """Review round 3 changed this test: every register writer takes the facility's register lock first, so a record on a new device
    makes the bulk set wait (on the register lock: round 2's FOR KEY SHARE let both through to the create race); it then sets the row the
    record created (never a second insert): both devices set, the record's network kept. (Review fixes C2 and C3: the bulk waited on the
    device's row; before them: both found no row, and the bulk's insert lost the create race: RACE.)"""
    model, rel, asset = _setup_release(tenant)
    with tenant_context(tenant):
        dept = Department.objects.get()
        other = Asset.objects.create(tag="CE-0", device_model=model, department=dept)
    held, waited = threading.Event(), {}
    _hold_device_lock(monkeypatch, "first", held, waited)
    errors, results = {}, {}

    def single():
        with tenant_context(tenant):
            return record_facts(asset, by=None, network=Network.WIRED).network

    def bulk():
        assert held.wait(20)
        with tenant_context(tenant):
            return record_facts_for(model, [other, asset], by=None, release=rel)

    _both(single, bulk, errors, results)
    assert not errors and results == {"first": Network.WIRED, "second": 2}, (results, errors)
    assert waited == {"first": True}  # the bulk set waited for the record (on the register lock)
    with tenant_context(tenant):
        assert sorted(DeviceSecurity.objects.values_list("asset__tag", "release_id", "network")) == [
            ("CE-0", rel.pk, ""), ("CE-1", rel.pk, Network.WIRED)]
        assert reasons(DeviceSecurity.objects.get(asset=asset)) == ["Facts recorded", "Set with 2 devices of BD Alaris"]  # in that order


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_a_bulk_set_that_loses_a_create_race_saves_nothing(tenant, monkeypatch):
    """A security row inserted meanwhile by a writer that does not lock the device (none in the app since review fixes C2 and C3; the
    fixtures here) after the bulk found none: the bulk's insert fails on the one-to-one key, and it refuses in words and saves none of
    its devices (one transaction), never an IntegrityError."""
    model, rel, asset = _setup_release(tenant)
    with tenant_context(tenant):
        dept = Department.objects.get()
        other = Asset.objects.create(tag="CE-0", device_model=model, department=dept)  # set before the race or not: rolled back with it
    barrier, inserted = threading.Barrier(2, timeout=20), threading.Event()
    real_save = services._save

    def bulk_save(obj, by, reason):
        if threading.current_thread().name == "second" and not getattr(bulk_save, "met", False):
            bulk_save.met = True
            barrier.wait()  # the bulk has looked: no rows
            assert inserted.wait(20)
        return real_save(obj, by, reason)

    monkeypatch.setattr(services, "_save", bulk_save)
    errors, results = {}, {}

    def single():
        barrier.wait()
        with tenant_context(tenant):
            DeviceSecurity.objects.create(asset=asset, network=Network.WIRED)
        inserted.set()
        return Network.WIRED

    def bulk():
        with tenant_context(tenant):
            return record_facts_for(model, [other, asset], by=None, release=rel)

    _both(single, bulk, errors, results)
    assert results == {"first": Network.WIRED} and errors["second"].message_dict == {"assets": [services.RACE]}, (results, errors)
    with tenant_context(tenant):
        assert list(DeviceSecurity.objects.values_list("asset__tag", "release_id", "network")) == [("CE-1", None, Network.WIRED)]


@needs_postgres
def test_the_writers_under_the_policies(pump, pump_model, vent, dept, kim, theirs, today):
    """As the runtime role: every writer, and another facility's ids refused."""
    as_app_role()
    profile = add_release(pump_model, by=kim, connects=Connects.WIRELESS, segmented=Segmented.NONE)
    r = add_release(pump_model, by=kim, version="9.33.1", mds2=Mds2Status.ON_FILE, mds2_edition=Mds2Edition.E2019)
    update_release(profile, by=kim, updates_by=UpdatesBy.IT)
    gone = add_release(pump_model, by=kim, version="9.0")
    remove_release(gone, by=kim)
    assert add_release(pump_model, by=kim, version="v9.0").pk == gone.pk
    assert record_facts(pump, by=kim, release=r, network=Network.WIRELESS).release == r
    second = device(pump_model, dept, "CE-20002")
    assert record_facts_for(pump_model, [pump, second], by=kim, release=gone) == 2
    assert refused(add_release, theirs["model"], by=kim, version="1") == {"device_model": services.CHOOSE_MODEL}
    assert refused(update_release, theirs["release"], by=kim, sbom="available") == {"release": services.CHOOSE_RELEASE}
    assert refused(remove_release, theirs["release"], by=kim) == {"release": services.CHOOSE_RELEASE}
    assert refused(record_facts, theirs["asset"], by=kim, network=Network.WIRED) == {"asset": services.CHOOSE_DEVICE}
    assert refused(record_facts, pump, by=kim, release=theirs["release"]) == {"release": services.CHOOSE_VERSION}
    assert refused(record_facts_for, pump_model, [theirs["asset"]], by=kim, release=r) == {"assets": services.CHOOSE_DEVICES}
    changed = eq.update_asset(Asset.objects.get(pk=pump.pk), by=kim, device_model=vent.device_model)
    assert changed.security_release_cleared and DeviceSecurity.objects.get(asset=pump).release is None
