"""
Slice 37 wave 1, the register's rules (apps.cybersecurity.rules): every standing key and its words (and none of the words the register
never says), the group fallback (a release over the profile by group, never by field; the facility group always the profile's; a removed
row ignored), slice 36's model support when the release has no date of its own, the OS line apart, the connection, the batch reader's
fixed queries, the fleet's rows and head figures, and Changes for IT (each kind, the window's edges in a facility's time zone).
"""
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from cyber_fixtures import facts as record
from cyber_fixtures import release
from django.utils import timezone
from pg_helpers import as_app_role, needs_postgres

from apps.cybersecurity import rules, services
from apps.cybersecurity.models import (
    Connects,
    DeviceSecurity,
    Disclosed,
    Mds2Edition,
    Mds2Status,
    Network,
    OsFamily,
    Patching,
    PatientData,
    Sbom,
    Segmented,
    SoftwareRelease,
    UpdatesBy,
)
from apps.equipment import services as eq
from apps.equipment.models import AddedAs, Asset, AssetStatus, Department, DeviceModel, Ownership, ReturnCleaning, ReturnData, RiskClass
from apps.tenants.context import tenant_context
from apps.tenants.models import Tenant

TODAY = date(2026, 10, 20)
DAY = timedelta(days=1)


def words_ok(text: str) -> bool:
    """None of rules.NEVER_WORDS, as whole words in any letter case."""
    return not [w for w in rules.NEVER_WORDS if re.search(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", text, re.IGNORECASE)]


def device(model, dept, tag, **fields):
    return Asset.objects.create(tag=tag, device_model=model, department=dept, **fields)


@pytest.fixture
def sterilizer_model(ctx):
    return DeviceModel.objects.create(manufacturer="Steris", model="Amsco 400", description="Steam sterilizer", category="Sterilizers",
                                      risk_class=RiskClass.MEDIUM)


# --- the release's standing (the manufacturer's security updates) ---------------------------------------------------------------

def test_security_updates_ended_and_announced(vent_model):
    ended = rules.release_standing(rules.facts(release(vent_model, "3.1", security_updates_end_on=TODAY - DAY), None), vent_model, TODAY)
    assert ended == rules.Standing(rules.UPDATES_ENDED, "Security updates ended after Oct 19, 2026", TODAY - DAY)
    last_day = rules.release_standing(rules.facts(release(vent_model, "3.2", security_updates_end_on=TODAY), None), vent_model, TODAY)
    assert last_day == rules.Standing(rules.UPDATES_END_ANNOUNCED, "Security updates end Oct 20, 2026 (announced)", TODAY)
    later = rules.release_standing(rules.facts(release(vent_model, "3.3", security_updates_end_on=date(2028, 3, 31)), None), vent_model, TODAY)
    assert later.words == "Security updates end Mar 31, 2028 (announced)" and later.key == rules.UPDATES_END_ANNOUNCED


@pytest.mark.parametrize("support,parts,key,words,day", [
    (date(2026, 6, 30), None, rules.MODEL_SUPPORT_ENDED, "Manufacturer support ended Jun 30, 2026 (the model's)", date(2026, 6, 30)),
    (None, date(2026, 1, 31), rules.MODEL_SUPPORT_ENDED, "Parts no longer available after Jan 31, 2026 (the model's)", date(2026, 1, 31)),
    (date(2026, 6, 30), date(2026, 1, 31), rules.MODEL_SUPPORT_ENDED, "Parts no longer available after Jan 31, 2026 (the model's)",
     date(2026, 1, 31)),
    (date(2027, 6, 30), None, rules.SECURITY_NOT_RECORDED, "Security updates end: not recorded", None),  # announced only: not ended
    (TODAY, None, rules.SECURITY_NOT_RECORDED, "Security updates end: not recorded", None),  # its last day is today
    (None, None, rules.SECURITY_NOT_RECORDED, "Security updates end: not recorded", None),
])
def test_with_no_security_date_the_models_support_decides(vent_model, support, parts, key, words, day):
    vent_model.support_ends_on, vent_model.parts_end_on = support, parts
    assert rules.release_standing(rules.facts(None, None), vent_model, TODAY) == rules.Standing(key, words, day)


def test_a_sale_ended_is_not_the_models_support_ended(vent_model):
    vent_model.sale_ended_on = TODAY - 300 * DAY
    assert rules.release_standing(rules.facts(None, None), vent_model, TODAY).key == rules.SECURITY_NOT_RECORDED


def test_a_release_date_of_its_own_wins_over_the_models_support(vent_model):
    vent_model.support_ends_on = TODAY - 100 * DAY
    own = rules.facts(release(vent_model, "3.1", security_updates_end_on=TODAY + 400 * DAY), None)
    assert rules.release_standing(own, vent_model, TODAY).key == rules.UPDATES_END_ANNOUNCED


# --- the OS line, apart ---------------------------------------------------------------------------------------------------------

def test_the_os_line(vent_model):
    def os_of(**fields):
        return rules.os_standing(rules.facts(None, release(vent_model, **fields)), TODAY)

    assert os_of(os_family=OsFamily.NONE) == rules.Standing(rules.NO_OS, "No operating system")
    SoftwareRelease.objects.all().delete()
    assert os_of(os_family=OsFamily.WINDOWS, os_name="Windows 10 IoT Enterprise LTSC 2021", os_support_ends_on=date(2032, 1, 13)) == \
        rules.Standing(rules.OS_END_ANNOUNCED, "The OS vendor's published support for Windows 10 IoT Enterprise LTSC 2021 ends Jan 13, 2032",
                       date(2032, 1, 13))
    SoftwareRelease.objects.all().delete()
    assert os_of(os_family=OsFamily.WINDOWS, os_support_ends_on=TODAY - DAY) == rules.Standing(
        rules.OS_ENDED, "The OS vendor's published support for Windows ended after Oct 19, 2026", TODAY - DAY)
    SoftwareRelease.objects.all().delete()
    assert os_of(os_family=OsFamily.EMBEDDED, os_support_ends_on=TODAY).words == (
        "The OS vendor's published support for its embedded or real-time OS ends Oct 20, 2026")
    SoftwareRelease.objects.all().delete()
    assert os_of(os_family=OsFamily.OTHER, os_support_ends_on=TODAY).words == "The OS vendor's published support for its operating system ends Oct 20, 2026"
    SoftwareRelease.objects.all().delete()
    assert os_of(os_family=OsFamily.LINUX) == rules.Standing(rules.OS_NOT_RECORDED, "Operating system support: not recorded")
    assert rules.os_standing(rules.facts(None, None), TODAY).key == rules.OS_NOT_RECORDED


def test_the_os_line_and_the_security_line_never_merge(vent_model):
    """An OS whose vendor's support ended beside a release still getting security updates: two lines, each its own."""
    f = rules.facts(release(vent_model, "3.1", os_family=OsFamily.WINDOWS, os_name="Windows 7", os_support_ends_on=date(2020, 1, 14),
                            security_updates_end_on=date(2028, 3, 31)), None)
    assert rules.release_standing(f, vent_model, TODAY).words == "Security updates end Mar 31, 2028 (announced)"
    assert rules.os_standing(f, TODAY).words == "The OS vendor's published support for Windows 7 ended after Jan 14, 2020"


# --- the connection ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("network,key,words", [
    (Network.WIRED, rules.CONNECTED, "Connected (wired)"),
    (Network.WIRELESS, rules.CONNECTED, "Connected (wireless)"),
    (Network.BOTH, rules.CONNECTED, "Connected (wired and wireless)"),
    (Network.INTERMITTENT, rules.CONNECTED, "Connected now and then"),
    (Network.NOT_CONNECTED, rules.NOT_CONNECTED, "Not connected here"),
])
def test_the_devices_own_fact_wins(vent, vent_model, network, key, words):
    disclosed = rules.facts(None, release(vent_model, connects=Connects.NONE))
    assert rules.connected(record(vent, network=network), disclosed) == rules.Connection(key, words, True)


@pytest.mark.parametrize("connects,key,words", [
    (Connects.WIRED, rules.CAN_CONNECT, "Can connect (wired), as disclosed"),
    (Connects.WIRELESS, rules.CAN_CONNECT, "Can connect (wireless), as disclosed"),
    (Connects.BOTH, rules.CAN_CONNECT, "Can connect (wired and wireless), as disclosed"),
    (Connects.NONE, rules.NOT_CONNECTED, "Does not connect, as disclosed"),
    ("", rules.CONNECTION_NOT_RECORDED, "Connection not recorded"),
])
def test_else_the_models_disclosure(vent, vent_model, connects, key, words):
    disclosed = rules.facts(None, release(vent_model, connects=connects))
    assert rules.connected(None, disclosed) == rules.Connection(key, words, False)
    assert rules.connected(record(vent), disclosed) == rules.Connection(key, words, False)  # a row with no network fact


# --- the group fallback ------------------------------------------------------------------------------------------------------------

def test_a_release_overrides_the_profile_by_group_never_by_field(vent_model):
    profile = release(vent_model, os_family=OsFamily.WINDOWS, os_name="Windows 10", os_support_ends_on=date(2032, 1, 13),
                      security_updates_end_on=date(2030, 1, 1), mds2=Mds2Status.ON_FILE, mds2_edition=Mds2Edition.E2019,
                      mds2_document="DEMO-1", patient_data=PatientData.STORES, connects=Connects.WIRELESS, updates_by=UpdatesBy.IT,
                      segmented=Segmented.SOME)
    own = release(vent_model, "3.1", security_updates_end_on=date(2027, 5, 1), remote_service=Disclosed.YES)
    f = rules.facts(own, profile)
    assert f.sources == {"support": rules.THIS_RELEASE, "mds2": rules.PROFILE, "disclosure": rules.THIS_RELEASE, "facility": rules.PROFILE}
    # the support group is the release's, whole: its OS fields are not recorded there, never the profile's
    assert (f.get("security_updates_end_on"), f.get("os_family"), f.get("os_name"), f.get("os_support_ends_on")) == (date(2027, 5, 1), "", "", None)
    assert (f.get("mds2"), f.get("mds2_edition"), f.get("mds2_document")) == (Mds2Status.ON_FILE, Mds2Edition.E2019, "DEMO-1")
    assert (f.get("remote_service"), f.get("patient_data"), f.get("connects")) == (Disclosed.YES, "", "")  # the disclosure group too
    assert (f.get("updates_by"), f.get("segmented")) == (UpdatesBy.IT, Segmented.SOME)
    assert set(f.values) == set(rules.ALL_FIELDS) and f.release == own and f.profile == profile
    assert not f.can_connect and not f.patient_data


def test_unset_values_leave_the_group_to_the_profile(vent_model):
    profile = release(vent_model, connects=Connects.WIRED, patching=Patching.FACILITY, sbom=Sbom.AVAILABLE, os_family=OsFamily.LINUX)
    blank = release(vent_model, "3.1", os_family="", os_name="", mds2="", connects="", security_updates_end_on=None)
    f = rules.facts(blank, profile)
    assert f.sources == {"support": rules.PROFILE, "mds2": rules.NOT_RECORDED, "disclosure": rules.PROFILE, "facility": rules.NOT_RECORDED}
    assert (f.get("connects"), f.get("patching"), f.get("sbom"), f.get("os_family")) == (Connects.WIRED, Patching.FACILITY, Sbom.AVAILABLE,
                                                                                      OsFamily.LINUX)
    assert f.can_connect and (f.get("mds2"), f.get("mds2_released_on")) == ("", None)


def test_the_facility_group_is_always_the_profiles(vent_model):
    profile = release(vent_model, updates_by=UpdatesBy.CE)
    stray = release(vent_model, "3.1", updates_by=UpdatesBy.MANUFACTURER, segmented=Segmented.ALL)  # never through the services
    f = rules.facts(stray, profile)
    assert (f.get("updates_by"), f.get("segmented"), f.sources["facility"]) == (UpdatesBy.CE, "", rules.PROFILE)
    assert rules.facts(stray, None).sources["facility"] == rules.NOT_RECORDED and rules.facts(stray, None).get("updates_by") == ""


def test_removed_rows_and_misplaced_ones_are_ignored(vent_model, pump_model):
    profile = release(vent_model, connects=Connects.WIRED, removed_on=TODAY)
    assert set(rules.facts(None, profile).sources.values()) == {rules.NOT_RECORDED} and rules.facts(None, profile).profile is None
    removed = release(vent_model, "3.1", connects=Connects.WIRELESS, removed_on=TODAY)
    assert rules.facts(removed, None).release is None and rules.facts(removed, None).get("connects") == ""
    live = release(pump_model, connects=Connects.WIRELESS)
    as_release = rules.facts(live, None)  # a profile passed as the release: no release
    assert as_release.release is None and as_release.get("connects") == ""
    other = rules.facts(release(vent_model, "3.2"), live)  # another model's profile
    assert other.profile is None and other.get("connects") == ""


def test_profile_releases_and_release_of(vent, vent_model, pump_model):
    assert rules.profile(vent_model) is None and rules.releases(vent_model) == [] and rules.release_of(vent) is None
    p = release(vent_model, connects=Connects.WIRED)
    b, a = release(vent_model, "3.10"), release(vent_model, "3.1")
    release(vent_model, "2.0", removed_on=TODAY)
    release(pump_model, "9.1")
    assert rules.profile(vent_model) == p == rules.profile(vent_model.pk)
    assert rules.releases(vent_model) == [a, b]
    record(vent)
    assert rules.release_of(vent) is None
    record(vent, release=a)
    assert rules.release_of(Asset.objects.get(pk=vent.pk)) == a
    cached = Asset.objects.select_related("cyber__release").get(pk=vent.pk)
    assert rules.release_of(cached) == a


def test_a_release_of_another_model_reads_as_none(vent, vent_model, pump_model):
    """A model change racing a record (services re-read the model, update_asset clears it): the register never quotes another model's
    release."""
    theirs = release(pump_model, "9.1", security_updates_end_on=TODAY - DAY)
    record(vent, release=theirs, release_since=TODAY - 10 * DAY)
    assert rules.release_of(vent) is None
    s = rules.device_standing(vent, TODAY)
    assert (s.release, s.release_since, s.release_words, s.security.key) == (None, None, rules.RELEASE_NOT_RECORDED, rules.SECURITY_NOT_RECORDED)


# --- one device, and the batch reader ---------------------------------------------------------------------------------------------

def test_device_standing(vent, vent_model, make_user):
    kim = make_user("director")
    release(vent_model, connects=Connects.WIRED, os_family=OsFamily.WINDOWS, os_support_ends_on=TODAY - DAY)
    nine = release(vent_model, "9.33.1", security_updates_end_on=date(2028, 3, 31))
    s = rules.device_standing(vent, TODAY)
    assert (s.release_words, s.connection.words, s.security.words, s.os.words, s.record, s.checked_on, s.network) == (
        rules.RELEASE_NOT_RECORDED, "Can connect (wired), as disclosed", "Security updates end: not recorded",
        "The OS vendor's published support for Windows ended after Oct 19, 2026", None, None, "")
    record(vent, release=nine, release_since=date(2026, 3, 3), network=Network.WIRELESS, checked_on=TODAY, checked_by=kim)
    s = rules.device_standing(Asset.objects.get(pk=vent.pk), TODAY)
    assert (s.release, s.release_words, s.connection.words, s.security.words, s.os.words) == (
        nine, "9.33.1, since Mar 3, 2026", "Connected (wireless)", "Security updates end Mar 31, 2028 (announced)",
        "Operating system support: not recorded")  # the release's support group, whole: no OS recorded there
    assert (s.checked_on, s.checked_by, s.network, s.record.asset_id) == (TODAY, kim, Network.WIRELESS, vent.pk)
    record(vent, release=nine, release_since=None)
    assert rules.device_standing(Asset.objects.get(pk=vent.pk), TODAY).release_words == "9.33.1"


def _fleet_of(n, models, dept, *, with_facts, with_rows=True):
    """n devices spread over `models`, each model with a profile and two releases (with_rows), every other device with facts."""
    by_model = {}
    for m in models:
        if with_rows:
            release(m, connects=Connects.WIRELESS)
            by_model[m.pk] = [release(m, "1.0", security_updates_end_on=TODAY - DAY), release(m, "2.0")]
    out = []
    for i in range(n):
        m = models[i % len(models)]
        a = device(m, dept, f"Q-{i:03d}")
        if with_facts and i % 2 == 0:
            record(a, release=by_model[m.pk][i % 2] if with_rows else None, network=Network.WIRED, release_since=TODAY - DAY)
        out.append(a)
    return out


@pytest.mark.parametrize("n", [1, 25])
@pytest.mark.parametrize("state", ["bare", "facts only", "releases only", "releases and facts"])
def test_standings_reads_in_fixed_queries(n, state, dept, vent_model, pump_model, sterilizer_model, django_assert_num_queries):
    with_facts, with_rows = "facts" in state, "releases" in state
    made = _fleet_of(n, [vent_model, pump_model, sterilizer_model], dept, with_facts=with_facts, with_rows=with_rows)
    with django_assert_num_queries(2):  # the devices joined to their rows; the models' profiles
        out = rules.standings(Asset.objects.filter(tag__startswith="Q-"), TODAY)
    assert len(out) == n
    first = out[made[0].pk]
    if with_facts and with_rows:
        assert first.release_words == "1.0, since Oct 19, 2026" and first.security.key == rules.UPDATES_ENDED
    elif with_rows:
        assert all(s.release is None and s.connection.key == rules.CAN_CONNECT for s in out.values())
    else:
        assert (first.release, first.connection.key, first.facts.sources["disclosure"]) == (
            None, rules.CONNECTED if with_facts else rules.CONNECTION_NOT_RECORDED, rules.NOT_RECORDED)
    plain = list(Asset.objects.filter(tag__startswith="Q-"))  # models not read
    with django_assert_num_queries(3):  # the rows; the models; the profiles
        assert {k: v.release_words for k, v in rules.standings(plain, TODAY).items()} == {k: v.release_words for k, v in out.items()}
    with_models = list(Asset.objects.filter(tag__startswith="Q-").select_related("device_model"))
    with django_assert_num_queries(2):  # the rows; the profiles
        rules.standings(with_models, TODAY)
    with django_assert_num_queries(1):  # all read now: the profiles only
        rules.standings(with_models, TODAY)
    with django_assert_num_queries(0):
        assert rules.standings(Asset.objects.none(), TODAY) == {}
    with django_assert_num_queries(0):
        assert rules.standings([], TODAY) == {}


def test_without_profiles_or_facts(dept, vent_model, django_assert_num_queries):
    a = device(vent_model, dept, "Q-1")
    with django_assert_num_queries(2):
        s = rules.standings(Asset.objects.all(), TODAY)[a.pk]
    assert (s.release_words, s.connection.key, s.security.key, s.os.key) == (rules.RELEASE_NOT_RECORDED, rules.CONNECTION_NOT_RECORDED,
                                                                             rules.SECURITY_NOT_RECORDED, rules.OS_NOT_RECORDED)
    assert set(s.facts.sources.values()) == {rules.NOT_RECORDED}


def test_on_site_leaves_out_retired_and_returned_devices(ctx, dept, vent_model, pump_model):
    here = device(vent_model, dept, "CE-1")
    device(vent_model, dept, "CE-2", status=AssetStatus.RETIRED)
    missing = device(vent_model, dept, "CE-3", status=AssetStatus.MISSING)
    rental = eq.add_temporary_device(tag="R-1", device_model=pump_model, department=dept, kind=Ownership.RENTAL, owner="Acme Rentals",
                                     serial="SN-1", added_as=AddedAs.EXISTING, arrived_on=timezone.localdate() - 5 * DAY)
    gone = eq.add_temporary_device(tag="R-2", device_model=pump_model, department=dept, kind=Ownership.RENTAL, owner="Acme Rentals",
                                   serial="SN-2", added_as=AddedAs.EXISTING, arrived_on=timezone.localdate() - 5 * DAY)
    eq.return_to_owner(gone, cleaning=ReturnCleaning.DECONTAMINATED, data=ReturnData.NONE_STORED)
    assert eq.status_label(Asset.objects.get(pk=gone.pk)) == eq.RETURNED_LABEL
    assert {a.tag for a in rules.on_site()} == {here.tag, missing.tag, rental.tag}


# --- the fleet ----------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def floor(ctx, dept, vent_model, pump_model, sterilizer_model):
    """Pumps: wireless, MDS2 requested, not segmented; two releases (9.19.2 past its security updates, its OS's support ended; 9.33.1
    announced), two pumps not recorded (one a rental), one retired. Ventilators: wired, MDS2 on file for release 3.1 only, segment not
    recorded, the model's support ended; one connectable, one not connected here. The sterilizer: does not connect."""
    release(pump_model, connects=Connects.WIRELESS, mds2=Mds2Status.REQUESTED, mds2_requested_on=TODAY - 30 * DAY, segmented=Segmented.NONE,
            updates_by=UpdatesBy.CE)
    old = release(pump_model, "9.19.2", security_updates_end_on=TODAY - 10 * DAY, os_family=OsFamily.EMBEDDED, os_support_ends_on=TODAY - DAY)
    new = release(pump_model, "9.33.1", security_updates_end_on=TODAY + 400 * DAY)
    release(pump_model, "9.0", removed_on=TODAY)
    p1, p2, p3 = device(pump_model, dept, "P-1"), device(pump_model, dept, "P-2"), device(pump_model, dept, "P-3")
    record(p1, release=old)
    record(p2, release=new)
    rental = eq.add_temporary_device(tag="R-1", device_model=pump_model, department=dept, kind=Ownership.RENTAL, owner="Acme Rentals",
                                     serial="SN-1", added_as=AddedAs.EXISTING, arrived_on=timezone.localdate() - 5 * DAY)
    device(pump_model, dept, "P-9", status=AssetStatus.RETIRED)
    vent_model.support_ends_on = TODAY - 100 * DAY
    vent_model.save()
    release(vent_model, connects=Connects.WIRED)
    release(vent_model, "3.1", mds2=Mds2Status.ON_FILE, mds2_edition=Mds2Edition.E2019)
    v1, v2 = device(vent_model, dept, "V-1"), device(vent_model, dept, "V-2")
    record(v2, network=Network.NOT_CONNECTED)
    release(sterilizer_model, connects=Connects.NONE, mds2=Mds2Status.NONE_FROM_MANUFACTURER)
    s1 = device(sterilizer_model, dept, "S-1")
    return {"old": old, "new": new, "p1": p1, "p2": p2, "p3": p3, "rental": rental, "v1": v1, "v2": v2, "s1": s1}


def test_the_fleets_rows(floor, pump_model, vent_model, sterilizer_model, django_assert_num_queries):
    with django_assert_num_queries(2):  # the devices joined to their rows; the models' profiles and releases
        f = rules.fleet(TODAY)
    assert [r.device_model for r in f.rows] == [pump_model, vent_model, sterilizer_model] and f.devices == 7
    pumps, vents, sterilizers = f.rows
    assert (pumps.devices, pumps.connected, pumps.updates_ended, pumps.os_ended, pumps.not_recorded, pumps.can_connect) == (4, 4, 1, 1, 2, True)
    assert pumps.by_release == [(floor["old"], 1), (floor["new"], 1), (None, 2)]
    assert (pumps.mds2, pumps.mds2_words, pumps.facts.get("segmented"), pumps.facts.get("updates_by")) == (
        Mds2Status.REQUESTED, "Requested from the manufacturer", Segmented.NONE, UpdatesBy.CE)
    assert (vents.devices, vents.connected, vents.updates_ended, vents.os_ended, vents.not_recorded) == (2, 1, 2, 0, 2)
    assert (vents.mds2, vents.mds2_versions, vents.mds2_words, vents.by_release) == (Mds2Status.ON_FILE, ("3.1",), "On file for 3.1", [(None, 2)])
    assert (sterilizers.connected, sterilizers.can_connect, sterilizers.mds2_words) == (0, False, "The manufacturer has none")


def test_the_fleets_head_figures(floor):
    figures = rules.fleet(TODAY).figures
    assert list(figures) == ["no_mds2", "updates_ended", "no_release", "not_segmented", "segment_not_recorded"]
    assert {k: v["count"] for k, v in figures.items()} == {
        "no_mds2": 1,  # the pumps (requested); the ventilators have one on file; the sterilizer cannot connect
        "updates_ended": 2,  # P-1 (its release) and V-1 (its model's support); V-2 is not connected here
        "no_release": 3,  # P-3, R-1 (the rental counts), V-1
        "not_segmented": 1,  # the pumps
        "segment_not_recorded": 1,  # the ventilators
    }
    assert figures["no_mds2"]["requested"] == 1
    assert figures["no_mds2"] == {"key": "no_mds2", "count": 1, "words": "Models that can connect with no MDS2 on file",
                                  "counts": "Models with devices on site (ours and temporary) that can connect", "requested": 1}
    assert all(f["words"] and f["counts"] for f in figures.values())


def test_the_fleet_of_filtered_devices(floor, pump_model, django_assert_num_queries):
    with django_assert_num_queries(2):
        f = rules.fleet(TODAY, Asset.objects.filter(device_model=pump_model))  # the retired pump among them: left out
    assert [r.device_model for r in f.rows] == [pump_model] and f.rows[0].devices == 4 and f.devices == 4
    assert {k: v["count"] for k, v in f.figures.items()} == {"no_mds2": 1, "updates_ended": 1, "no_release": 2, "not_segmented": 1,
                                                             "segment_not_recorded": 0}
    ours = rules.fleet(TODAY, Asset.objects.filter(device_model=pump_model, ownership=Ownership.OWNED))
    assert ours.rows[0].devices == 3 and ours.figures["no_release"]["count"] == 1
    listed = rules.fleet(TODAY, [floor["p1"], floor["s1"], Asset.objects.get(tag="P-9")])
    assert [(r.device_model.model, r.devices) for r in listed.rows] == [("Alaris 8015 PCU", 1), ("Amsco 400", 1)]
    assert rules.fleet(TODAY, Asset.objects.none()).rows == []


def test_the_fleets_queries_do_not_grow(dept, vent_model, pump_model, sterilizer_model, django_assert_num_queries):
    _fleet_of(25, [vent_model, pump_model, sterilizer_model], dept, with_facts=True)
    with django_assert_num_queries(2):
        f = rules.fleet(TODAY)
    assert sum(r.devices for r in f.rows) == 25 and f.figures["updates_ended"]["count"] > 0


def test_the_mds2_words():
    row = rules.FleetRow(device_model=None, devices=0, connected=0, by_release=[], updates_ended=0, os_ended=0, facts=rules.facts(None, None))
    assert row.mds2_words == "Not recorded"
    assert rules.FleetRow(**{**row.__dict__, "mds2": Mds2Status.ON_FILE, "mds2_versions": ("", "3.1")}).mds2_words == "On file"
    assert rules.FleetRow(**{**row.__dict__, "mds2": Mds2Status.ON_FILE, "mds2_versions": ("3.1", "3.2")}).mds2_words == "On file for 3.1, 3.2"


# --- Changes for IT ----------------------------------------------------------------------------------------------------------------

def _connectable(pump_model, vent_model, sterilizer_model):
    release(pump_model, connects=Connects.WIRELESS)
    release(vent_model, patient_data=PatientData.HANDLES)  # handles patient data, connection not disclosed
    release(sterilizer_model, connects=Connects.NONE)


def test_changes_for_it_each_kind(ctx, dept, vent_model, pump_model, sterilizer_model, django_assert_num_queries):
    _connectable(pump_model, vent_model, sterilizer_model)
    today = timezone.localdate()
    long_ago = today - 2000 * DAY
    added = eq.create_asset(tag="P-NEW", device_model=pump_model, department=dept)
    lost = eq.create_asset(tag="P-LOST", device_model=pump_model, department=dept, installed_on=long_ago)
    eq.set_status(lost, AssetStatus.MISSING)
    old = eq.create_asset(tag="V-OLD", device_model=vent_model, department=dept, installed_on=long_ago)
    eq.set_status(old, AssetStatus.RETIRED)
    rental = eq.add_temporary_device(tag="R-1", device_model=pump_model, department=dept, kind=Ownership.RENTAL, owner="Acme Rentals",
                                     serial="SN-1", added_as=AddedAs.EXISTING, arrived_on=today - 5 * DAY)
    eq.return_to_owner(rental, cleaning=ReturnCleaning.DECONTAMINATED, data=ReturnData.NONE_STORED)
    eq.create_asset(tag="S-QUIET", device_model=sterilizer_model, department=dept)  # cannot connect, no patient data: not IT's
    said = eq.create_asset(tag="S-WIRED", device_model=sterilizer_model, department=dept)  # its own fact says connected
    record(said, network=Network.WIRED)
    imported = eq.create_asset(tag="P-IMP", device_model=pump_model, department=dept, added_as=AddedAs.IMPORTED, installed_on=long_ago,
                               added_on=long_ago + 300 * DAY)
    eq.set_status(imported, AssetStatus.RETIRED, changed_on=long_ago + 300 * DAY)  # imported retired years ago: never on site
    for a, moment in ((added, 1), (lost, 2), (old, 3), (rental, 4), (said, 5)):  # an order within the day
        Asset.history.filter(id=a.pk).update(history_date=timezone.make_aware(datetime.combine(today, time(1, moment))))
    Asset.history.filter(id=lost.pk, status=AssetStatus.MISSING).update(history_date=timezone.make_aware(datetime.combine(today, time(9))))
    Asset.history.filter(id=old.pk, status=AssetStatus.RETIRED).update(history_date=timezone.make_aware(datetime.combine(today, time(10))))
    with django_assert_num_queries(3):  # the history; the devices with their rows; the profiles
        changes = rules.changes_for_it(today)
    d = rules.day_words(today)
    assert [(c.asset.tag, c.kind, c.words) for c in changes] == [  # newest first
        ("V-OLD", "retired", f"Retired {d}"),
        ("P-LOST", "missing", f"Marked missing {d}"),
        ("S-WIRED", "added", f"Added {d}"),
        ("R-1", "returned", f"Returned to its owner {d}"),
        ("R-1", "added", f"Added {d} (already in use here)"),
        ("V-OLD", "added", f"Added {d}"),
        ("P-LOST", "added", f"Added {d}"),
        ("P-NEW", "added", f"Added {d}"),
    ]
    assert all(c.day == today for c in changes)
    assert changes[0].connection.key == rules.CONNECTION_NOT_RECORDED and changes[2].connection.words == "Connected (wired)"
    assert changes[3].asset.status == AssetStatus.RETIRED and changes[3].connection.words == "Can connect (wireless), as disclosed"


def test_imported_devices_and_quiet_windows(ctx, dept, pump_model, django_assert_num_queries):
    release(pump_model, connects=Connects.WIRELESS)
    today = timezone.localdate()
    with django_assert_num_queries(1):  # nothing changed: the history only
        assert rules.changes_for_it(today) == []
    eq.create_asset(tag="P-IMP", device_model=pump_model, department=dept, added_as=AddedAs.IMPORTED)
    with django_assert_num_queries(3):
        assert [c.words for c in rules.changes_for_it(today)] == [f"Imported {rules.day_words(today)}"]
    same_day = eq.create_asset(tag="P-GONE", device_model=pump_model, department=dept, added_as=AddedAs.IMPORTED,
                               installed_on=today - 400 * DAY, added_on=today - 2 * DAY)
    eq.set_status(same_day, AssetStatus.RETIRED, changed_on=today - 2 * DAY)  # the importer: both rows on the day it was retired
    years = eq.create_asset(tag="P-YEARS", device_model=pump_model, department=dept, added_as=AddedAs.IMPORTED, installed_on=today - 3000 * DAY,
                            added_on=today - 1000 * DAY)
    eq.set_status(years, AssetStatus.RETIRED, changed_on=today - 1000 * DAY)  # retired long ago: nothing in the window
    back = eq.create_asset(tag="P-BACK", device_model=pump_model, department=dept, installed_on=today - 3000 * DAY)
    eq.set_status(back, AssetStatus.RETIRED, changed_on=today - 500 * DAY)  # added today, its retirement dated before: never on site
    assert [(c.asset.tag, c.kind, c.day) for c in rules.changes_for_it(today)] == [
        ("P-IMP", "added", today), ("P-GONE", "retired", today - 2 * DAY), ("P-GONE", "added", today - 2 * DAY)]


def test_changes_for_it_window_edges_in_the_facilitys_time_zone(db):
    """Kona (UTC-10): the window [today - days + 1, today] by the facility's day of each history row: a device added at 00:30 there on
    the first day is in it, one added at 23:30 the day before (the next day in UTC) is not; the day before for each kind too."""
    kona = Tenant.objects.create(name="Kona Community", slug="kona", timezone="Pacific/Honolulu")
    zone = ZoneInfo("Pacific/Honolulu")
    with tenant_context(kona):
        today = timezone.localdate()
        dept = Department.objects.create(name="ED")
        dm = DeviceModel.objects.create(manufacturer="Zoll", model="R", description="Defibrillator", category="Defibrillators",
                                        risk_class=RiskClass.LIFE_SUPPORT)
        SoftwareRelease.objects.create(device_model=dm, version="", version_key="", connects=Connects.WIRELESS)
        for days in (7, 30):
            first = today - (days - 1) * DAY
            Asset.objects.all().delete()  # each window on its own devices
            Asset.history.all().delete()
            moments = {"DAWN": datetime.combine(first, time(0, 30), zone), "EVE": datetime.combine(first - DAY, time(23, 30), zone),
                       "NOW": datetime.combine(today, time(0, 1), zone)}
            for tag, moment in moments.items():
                a = eq.create_asset(tag=tag, device_model=dm, department=dept, installed_on=today - 3000 * DAY)
                Asset.history.filter(id=a.pk).update(history_date=moment)
            assert [(c.asset.tag, c.day) for c in rules.changes_for_it(today, days)] == [("NOW", today), ("DAWN", first)]
            gone = Asset.objects.get(tag="EVE")
            eq.set_status(gone, AssetStatus.MISSING)
            Asset.history.filter(id=gone.pk, status=AssetStatus.MISSING).update(history_date=datetime.combine(first - DAY, time(23, 59), zone))
            assert "EVE" not in [c.asset.tag for c in rules.changes_for_it(today, days)]
            Asset.history.filter(id=gone.pk, status=AssetStatus.MISSING).update(history_date=datetime.combine(first, time(0, 0), zone))
            assert [(c.asset.tag, c.kind, c.day) for c in rules.changes_for_it(today, days) if c.asset.tag == "EVE"] == [("EVE", "missing", first)]


def test_another_facilitys_history_is_never_read(ctx, dept, pump_model, other_tenant):
    release(pump_model, connects=Connects.WIRELESS)
    with tenant_context(other_tenant):
        their_dept = Department.objects.create(name="Their ED")
        theirs = DeviceModel.objects.create(manufacturer="BD", model="Theirs", description="Pump", category="Infusion pumps")
        SoftwareRelease.objects.create(device_model=theirs, version="", version_key="", connects=Connects.WIRELESS)
        eq.create_asset(tag="THEIRS-1", device_model=theirs, department=their_dept)
        assert [c.asset.tag for c in rules.changes_for_it(timezone.localdate())] == ["THEIRS-1"]
    eq.create_asset(tag="MINE-1", device_model=pump_model, department=dept)
    assert [c.asset.tag for c in rules.changes_for_it(timezone.localdate())] == ["MINE-1"]
    assert {r.device_model.model for r in rules.fleet(timezone.localdate()).rows} == {"Alaris 8015 PCU"}
    assert DeviceSecurity.objects.count() == 0


# --- the words -----------------------------------------------------------------------------------------------------------------------

def test_no_word_the_register_never_says(vent, vent_model, pump_model, dept):
    """Every words string the rules produce, for every key, and every choice label the register shows."""
    from apps.cybersecurity import models as cyber_models

    texts = [rules.MDS2_SENTENCE, rules.HICP_SENTENCE, rules.RELEASE_NOT_RECORDED, rules.THIS_RELEASE, rules.PROFILE, rules.NOT_RECORDED,
             rules.COUNTS_ON_SITE]
    for d in (TODAY - DAY, TODAY, TODAY + DAY):
        texts.append(rules.release_standing(rules.facts(release(vent_model, f"r{d}", security_updates_end_on=d), None), vent_model, TODAY).words)
        for family in OsFamily.values:
            if family != OsFamily.NONE:
                for name in ("", "Windows 10 IoT Enterprise LTSC 2021"):
                    f = rules.facts(None, None)
                    f.values.update(os_family=family, os_name=name, os_support_ends_on=d)
                    texts.append(rules.os_standing(f, TODAY).words)
    for support, parts in ((TODAY - DAY, None), (None, TODAY - DAY), (None, None)):
        vent_model.support_ends_on, vent_model.parts_end_on = support, parts
        texts.append(rules.release_standing(rules.facts(None, None), vent_model, TODAY).words)
    f = rules.facts(None, None)
    f.values.update(os_family=OsFamily.NONE)
    texts.append(rules.os_standing(f, TODAY).words)
    texts.append(rules.os_standing(rules.facts(None, None), TODAY).words)
    sec = record(vent)
    for network in ("", *Network.values):
        sec.network = network
        for connects in ("", *Connects.values):
            disclosed = rules.facts(None, None)
            disclosed.values.update(connects=connects)
            texts.append(rules.connected(sec, disclosed).words)
    for kind in (rules.MISSING, rules.RETIRED, rules.RETURNED):
        texts.append(rules._CHANGE_WORDS[kind].format(day=rules.day_words(TODAY)))
    texts += [rules._added_words(how, TODAY) for how in ("", *AddedAs.values)]
    row = rules.FleetRow(device_model=None, devices=0, connected=0, by_release=[], updates_ended=0, os_ended=0, facts=rules.facts(None, None))
    texts += [rules.FleetRow(**{**row.__dict__, "mds2": s, "mds2_versions": v}).mds2_words for s in ("", *Mds2Status.values) for v in ((), ("3.1",))]
    for figure in rules.fleet(TODAY).figures.values():
        texts += [figure["words"], figure["counts"]]
    for choices in (cyber_models.OsFamily, cyber_models.Mds2Status, cyber_models.Mds2Edition, cyber_models.PatientData, cyber_models.Connects,
                    cyber_models.Disclosed, cyber_models.Patching, cyber_models.Sbom, cyber_models.UpdatesBy, cyber_models.Segmented,
                    cyber_models.Network):
        texts += [str(label) for label in choices.labels]
    assert len(texts) > 80
    bad = [t for t in texts if not words_ok(t)]
    assert bad == []
    assert not words_ok("Not secure") and not words_ok("UNSUPPORTED os") and words_ok("Device security")  # the scan itself


def test_the_services_words_too():
    texts = [v for k, v in vars(services).items() if k.isupper() and isinstance(v, str)]
    assert len(texts) > 25 and [t for t in texts if not words_ok(t)] == []


@needs_postgres
def test_the_rules_under_the_policies(floor, other_tenant, pump_model, django_assert_num_queries):
    """As the runtime role: the batch reader, the fleet, and Changes for IT read this facility's rows only, in their fixed queries."""
    with tenant_context(other_tenant):
        their_dept = Department.objects.create(name="Their ED")
        theirs = DeviceModel.objects.create(manufacturer="BD", model="Alaris 8015 PCU", description="Pump", category="Infusion pumps")
        SoftwareRelease.objects.create(device_model=theirs, version="", version_key="", connects=Connects.WIRELESS)
        their_pump = eq.create_asset(tag="THEIRS-1", device_model=theirs, department=their_dept)
        record(their_pump, network=Network.WIRED)
    as_app_role()
    with django_assert_num_queries(2):
        out = rules.standings(rules.on_site(), TODAY)
    assert {s.asset.tag for s in out.values()} == {"P-1", "P-2", "P-3", "R-1", "V-1", "V-2", "S-1"}
    assert out[floor["p1"].pk].release == floor["old"] and out[floor["v2"].pk].connection.key == rules.NOT_CONNECTED
    with django_assert_num_queries(2):
        f = rules.fleet(TODAY)
    assert f.devices == 7 and {k: v["count"] for k, v in f.figures.items()} == {
        "no_mds2": 1, "updates_ended": 2, "no_release": 3, "not_segmented": 1, "segment_not_recorded": 1}
    with django_assert_num_queries(3):
        changes = rules.changes_for_it(timezone.localdate())
    assert {c.asset.tag for c in changes} == {"P-1", "P-2", "P-3", "R-1", "P-9", "V-1", "V-2"}  # the sterilizer cannot connect
    assert rules.release_of(Asset.objects.get(pk=floor["p2"].pk)) == floor["new"] and rules.profile(pump_model).segmented == Segmented.NONE
