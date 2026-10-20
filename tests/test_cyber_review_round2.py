"""
Slice 37 review fixes, round 2 (commit 6a435b0), each on the skeptic's own scenario:

1. The bulk set (Set devices to this release) holds the devices FOR KEY SHARE in tag order (PostgreSQL): a contract's Add model on the
   same model's devices never waits on it nor it on them (both complete, never RACE); AEM's pull-in (aem.end) and the bulk set wait in
   turn (both complete); a model change in progress (update_asset) is still waited for, and the old model's release refused.
2. The MDS2 link's rule: only key=value parts are keys (a bare anchor is not; a hash route's query is); credential words, parts, and
   keys refused (sid, authorization, api_key, sig, ...), ordinary words accepted (author, keyword, designId, #secure-by-design, ...).
3. Changes for IT: a rental whose whole stay is entered late shows its arrival and its return (a backdated retirement of ours still
   hides the addition).
4. Add release restoring a version entered in error: a group typed in (any field) replaces that group whole, as typed; groups left blank
   keep their facts (an MDS2 status other than the stored one, and no operating system over a stored OS name, both restore).
5. Add release with a blank version and another bad field reports both, on the screen and in the service.
6. A department (or category) with no device on site: the Fleet offers it, keeps it, and answers none, as the API does.
7. Deleting a device model the register holds: a reader without Device security View reads only that the register holds it; a register
   reader reads the detailed words.
"""
import json
import re
import threading
import time
import uuid
from datetime import datetime, timedelta
from datetime import time as clock
from html import unescape

import pytest
from cyber_fixtures import release
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models import F
from django.utils import timezone
from pg_helpers import needs_postgres

from apps.accounts.models import Level, Module, Role, User
from apps.contracts import services as contracts
from apps.contracts.models import Contract
from apps.cybersecurity import rules, services
from apps.cybersecurity.models import Connects, DeviceSecurity, Mds2Edition, Mds2Status, OsFamily, Patching, SoftwareRelease
from apps.cybersecurity.services import add_release, record_facts_for
from apps.equipment import services as eq
from apps.equipment.models import AddedAs, Asset, AssetStatus, Department, DeviceModel, Ownership, ReturnCleaning, ReturnData, RiskClass
from apps.pm import aem
from apps.pm.dates import add_months
from apps.tenants.context import tenant_context

DAY = timedelta(days=1)
HX = {"HTTP_HX_REQUEST": "true"}
KEEPS = "so it cannot be deleted: the register keeps a model's releases and profile, with their history, even a release entered in error."


def refused(fn, *args, **kwargs) -> dict:
    with pytest.raises(ValidationError) as e:
        fn(*args, **kwargs)
    return {k: v[0] if len(v) == 1 else v for k, v in e.value.message_dict.items()}


def at(day, hour=12):
    return timezone.make_aware(datetime.combine(day, clock(hour)))


@pytest.fixture
def today(ctx):
    return timezone.localdate()


@pytest.fixture
def kim(make_user):
    return make_user("director")


# --- 1. PostgreSQL: the bulk set's device lock (FOR KEY SHARE, tag order) ---------------------------------------------------------

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
        t.join(60)
    assert not a.is_alive() and not b.is_alive()


def _another_waits_on_a_lock(timeout: float = 15) -> bool:
    """Until another connection to this database waits on a lock (pg_stat_activity), read on this thread's connection (the statistics
    snapshot cleared before each look: inside a transaction the first one is read again otherwise)."""
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


# The skeptics' arrangement (an import's UUIDs): pk order CE-3, CE-1, CE-2, so tag order (CE-1, CE-2, CE-3) is not pk order
PK_OF = {"CE-1": 2, "CE-2": 3, "CE-3": 1}


def _pumps(tenant, *, aem_months=None):
    """Model A (BD Alaris A, its release 1.0 and 2.0) with CE-1..CE-3 in pk order CE-3, CE-1, CE-2; model B. With `aem_months`, A has an
    AEM interval on file (OEM 12 months) and each device's next PM is that far after its last PM, a month ago: ending AEM pulls all
    three in."""
    with tenant_context(tenant):
        today = timezone.localdate()
        dept = Department.objects.create(name="ICU")
        a = DeviceModel.objects.create(manufacturer="BD", model="Alaris A", description="Pump", category="Infusion pumps",
                                       risk_class=RiskClass.HIGH, oem_pm_interval_months=12, aem_interval_months=aem_months)
        b = DeviceModel.objects.create(manufacturer="BD", model="Alaris B", description="Pump", category="Infusion pumps",
                                       risk_class=RiskClass.HIGH, oem_pm_interval_months=12)
        rel, rel2 = release(a, "1.0"), release(a, "2.0")
        last = today - 30 * DAY
        devices = {}
        for tag in ("CE-1", "CE-2", "CE-3"):
            pm = {"last_pm_on": last, "next_pm_on": add_months(last, aem_months)} if aem_months else {}
            devices[tag] = Asset.objects.create(id=uuid.UUID(int=PK_OF[tag]), tag=tag, device_model=a, department=dept, **pm)
    assert sorted(devices.values(), key=lambda d: d.pk)[0].tag == "CE-3"  # pk order is not tag order
    return a, b, rel, rel2, devices


def _crossed(tenant) -> list:
    """Devices whose record names a release of another model (never)."""
    with tenant_context(tenant):
        return list(DeviceSecurity.objects.exclude(release=None).exclude(release__device_model_id=F("asset__device_model_id"))
                    .values_list("asset__tag", flat=True))


def _pausing(items, pause):
    """The items, pausing (pause()) after the first was used: a writer's loop holding its first device while another runs."""
    for i, item in enumerate(items):
        if i == 1:
            pause()
        yield item


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_round2_contract_add_model_and_the_bulk_set_never_wait_on_each_other(tenant, monkeypatch):
    """Skeptic core #1 / #5, case 1: Contracts > Add model saves the model's pumps in tag order in one transaction (Contract.add_assets)
    and holds CE-1 (pk order CE-3, CE-1, CE-2); meanwhile Set devices to this release runs on the same model. The bulk set never waits on
    the contract's saves (KEY SHARE does not conflict with a plain update): it completes while the contract still holds CE-1, then the
    contract completes. Both done, never RACE, never a deadlock's 500."""
    a, _b, rel, _rel2, devices = _pumps(tenant)
    with tenant_context(tenant):
        today = timezone.localdate()
        contract = contracts.create_contract(reference="SC-2026-1", vendor="BD", start_on=today - 10 * DAY, end_on=today + 300 * DAY,
                                             annual_cost=1000)
    holding, bulk_done = threading.Event(), threading.Event()
    seen = {}
    real = Contract.add_assets

    def pause():
        holding.set()
        seen["bulk_done_while_held"] = bulk_done.wait(8)  # the bulk set ran to its end while the contract held CE-1

    def add_assets(self, assets):
        return real(self, _pausing(list(assets), pause))

    monkeypatch.setattr(Contract, "add_assets", add_assets)
    errors, results = {}, {}

    def add_model():
        with tenant_context(tenant), transaction.atomic():
            return contracts.add_model(Contract.objects.get(pk=contract.pk), a)

    def bulk():
        assert holding.wait(20)
        try:
            with tenant_context(tenant):
                return record_facts_for(a, list(devices.values()), by=None, release=rel)
        finally:
            bulk_done.set()

    _both(add_model, bulk, errors, results)
    assert errors == {} and results == {"first": 3, "second": 3}, (results, errors)
    assert seen == {"bulk_done_while_held": True}
    with tenant_context(tenant):
        assert sorted(Asset.objects.filter(contract_id=contract.pk).values_list("tag", flat=True)) == ["CE-1", "CE-2", "CE-3"]
        assert sorted(DeviceSecurity.objects.filter(release=rel).values_list("asset__tag", flat=True)) == ["CE-1", "CE-2", "CE-3"]
    assert _crossed(tenant) == []


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_round2_aem_end_and_the_bulk_set_wait_in_turn(tenant, monkeypatch):
    """Skeptic core #3: model A has an AEM interval on file and three pumps whose next PM is too far out (pk order CE-3, CE-1, CE-2). A PM
    approver ends AEM: the pull-in moves each device's next PM in tag order (update_asset, FOR UPDATE), and holds CE-1 while Set devices
    to this release runs on A. The bulk set waits on CE-1 (KEY SHARE in tag order: it holds none of the others), the pull-in goes on to
    CE-2 and CE-3 and commits, then the bulk set completes. Both done, in turn: never a deadlock, never RACE."""
    a, _b, rel, _rel2, devices = _pumps(tenant, aem_months=18)
    real = eq.update_asset
    calls, seen = [], {}
    pulled_one = threading.Event()

    def update_asset(asset, **kwargs):
        out = real(asset, **kwargs)
        if threading.current_thread().name == "first":
            calls.append(asset.tag)
            if len(calls) == 1:
                pulled_one.set()
                seen["bulk_waited"] = _another_waits_on_a_lock(8)  # the bulk set waits for this transaction's device
        return out

    monkeypatch.setattr(eq, "update_asset", update_asset)
    errors, results = {}, {}

    def end_aem():
        with tenant_context(tenant):
            return aem.end(DeviceModel.objects.get(pk=a.pk), by=None, reason="Back to the manufacturer's interval")

    def bulk():
        assert pulled_one.wait(20)
        with tenant_context(tenant):
            return record_facts_for(a, list(devices.values()), by=None, release=rel)

    _both(end_aem, bulk, errors, results)
    assert errors == {} and results == {"first": 3, "second": 3}, (results, errors)
    assert calls == ["CE-1", "CE-2", "CE-3"] and seen == {"bulk_waited": True}
    with tenant_context(tenant):
        today = timezone.localdate()
        assert DeviceModel.objects.get(pk=a.pk).aem_interval_months is None
        due = add_months(today - 30 * DAY, 12)
        assert set(Asset.objects.filter(device_model=a).values_list("next_pm_on", flat=True)) == {due}  # every next PM pulled in
        assert sorted(DeviceSecurity.objects.filter(release=rel).values_list("asset__tag", flat=True)) == ["CE-1", "CE-2", "CE-3"]
    assert _crossed(tenant) == []


@needs_postgres
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("with_row", [False, True])
def test_round2_a_model_change_in_progress_is_still_waited_for(tenant, with_row):
    """Round 2's KEY SHARE still waits for a model change (update_asset holds the device FOR UPDATE until its transaction ends): an edit
    moves CE-2 from A to B while Set devices to this release runs on A's three pumps (pk order CE-3, CE-1, CE-2). The bulk set waits on
    CE-2, then reads model B and refuses in words: A's release is never saved on a model-B device (with or without a security row)."""
    a, b, rel, rel2, devices = _pumps(tenant)
    moved = devices["CE-2"]
    if with_row:
        with tenant_context(tenant):
            services.record_facts(moved, by=None, release=rel2)
    changing = threading.Event()
    errors, results = {}, {}

    def change_model():
        with tenant_context(tenant), transaction.atomic():
            eq.update_asset(Asset.objects.get(pk=moved.pk), by=None, device_model=b)
            changing.set()
            assert _another_waits_on_a_lock()
        return "committed"

    def bulk():
        assert changing.wait(20)
        with tenant_context(tenant):
            return record_facts_for(a, list(devices.values()), by=None, release=rel)

    _both(change_model, bulk, errors, results)
    assert results == {"first": "committed"} and isinstance(errors.get("second"), ValidationError), (results, errors)
    assert errors["second"].message_dict == {"assets": ["CE-2 is not BD Alaris A: set one model's devices at a time."]}
    assert _crossed(tenant) == []
    with tenant_context(tenant):
        assert Asset.objects.get(pk=moved.pk).device_model_id == b.pk
        assert not DeviceSecurity.objects.filter(release=rel).exists()
        assert list(DeviceSecurity.objects.values_list("release_id", flat=True)) == ([None] if with_row else [])


def test_round2_race_words_name_no_one_recording_facts():
    """The RACE words fit a deadlock with any writer of the devices (a model change, a status change), not only another record."""
    assert services.RACE == "These devices were being changed or recorded meanwhile: try again."


# --- 2. The MDS2 link's rule --------------------------------------------------------------------------------------------------------

ACCEPTED = [
    # anchors (a bare fragment carries no key): the skeptics' "sig", "auth", "sess" inside ordinary words
    "https://vendor.example.com/product-security#secure-by-design",
    "https://vendor.example.com/product-security#authentication",
    "https://www.vendor.example.com/security#authentication-and-access",
    "https://www.vendor.example.com/support/product-security#security-assessments",
    "https://vendor.example.com/mds2#design-documents",
    "https://vendor.example.com/product-security#insights",
    "https://www.vendor.example.com/security#risk-assessment",
    # ordinary query keys
    "https://vendor.example.com/docs?designId=77",
    "https://vendor.example.com/docs?assessmentId=77",
    "https://vendor.example.com/docs?author=acme",
    "https://vendor.example.com/docs?author=psirt",
    "https://vendor.example.com/docs?insight=pumps",
    "https://vendor.example.com/mds2.pdf?keyword=mds2",
    "https://vendor.example.com/mds2.pdf?designation=MDS2",
    "https://vendor.example.com/docs?authorId=5&author_id=5",
    # a hash route's query with an ordinary key
    "https://vendor.example.com/portal#/cdp/search?keyword=MDS2",
    # links the rule always kept: document portals, a page number, tracking keys
    "https://vendor.example.com/mds2.pdf?la=en&hash=0A1B2C3D4E5F",
    "https://vendor.example.com/mds2.pdf?rev=-1&sc_lang=en&hash=0A1B2C3D",
    "https://vendor.example.com/sfc/servlet.shepherd/document/download/069?asPdf=false&operationContext=CHATTER",
    "https://vendor.example.com/mds2.pdf#page=3",
    "https://vendor.example.com/mds2.pdf?utm_source=newsletter&utm_medium=email&_ga=2.1",
    "https://vendor.example.com/doc?docId=123&assetType=pdf",
    "https://vendor.example.com/kb?id=kb_article&sysparm_article=KB0012345",
]

REFUSED = [
    "https://vendor.example.com/mds2?sid=abc123",  # the skeptics' session id
    "https://vendor.example.com/mds2?SID=abc123",
    "https://vendor.example.com/mds2.pdf?id=7&sid=abc123",
    "https://vendor.example.com/mds2.pdf?%73id=abc123",
    "https://vendor.example.com/mds2.pdf?authorization=abc123",
    "https://vendor.example.com/mds2.pdf?Authorization=Bearer%20abc123",
    "https://vendor.example.com/mds2.pdf?api_key=abc123",
    "https://vendor.example.com/mds2.pdf?apiKey=abc123",
    "https://vendor.example.com/mds2.pdf?key=abc123",
    "https://vendor.example.com/mds2.pdf?sig=abc123",
    "https://vendor.example.com/mds2.pdf?sv=2024-01-01&sig=abc123",  # an Azure SAS link
    "https://vendor.example.com/mds2.pdf?Expires=1&Key-Pair-Id=K1",  # a CloudFront signed link's key
    "https://vendor.example.com/mds2.pdf?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Date=20260101",
    "https://vendor.example.com/mds2.pdf?Signature=abc123",
    "https://vendor.example.com/mds2.pdf?auth=abc123",
    "https://vendor.example.com/mds2.pdf?pass=abc123",
    "https://vendor.example.com/mds2.pdf?pwd=abc123",
    "https://vendor.example.com/mds2.pdf?sess=abc123",
    "https://vendor.example.com/mds2.pdf?sessid=abc123",
    "https://vendor.example.com/mds2.pdf?PHPSESSID=abc123",
    "https://vendor.example.com/mds2.pdf?session_id=abc123",
    "https://vendor.example.com/mds2.pdf?accesskey=abc123",
    "https://vendor.example.com/mds2.pdf?authkey=abc123",
    "https://vendor.example.com/mds2.pdf?secretkey=abc123",
    "https://vendor.example.com/mds2.pdf?passkey=abc123",
    "https://vendor.example.com/mds2.pdf?passcode=abc123",
    "https://vendor.example.com/mds2.pdf?passphrase=abc123",
    "https://vendor.example.com/mds2.pdf?user_password=abc123",
    "https://vendor.example.com/mds2.pdf?access_token=abc123",
    "https://vendor.example.com/mds2.pdf?jwt=abc123",
    "https://vendor.example.com/mds2.pdf?Credential=abc123",
    "https://vendor.example.com/mds2.pdf?client_secret=abc123",
    "https://vendor.example.com/mds2.pdf#access_token=abc123",  # a fragment's key=value
    "https://vendor.example.com/portal#/cdp/search?keyword=MDS2&token=abc123",  # a hash route's query
    "https://vendor.example.com/portal#/docs?sid=abc123",
    "https://vendor.example.com/mds2.pdf;jsessionid=abc123",  # a path parameter
]


def _mds2(url: str) -> dict:
    return {"version": "9.1", "mds2": Mds2Status.ON_FILE, "mds2_edition": Mds2Edition.E2019, "mds2_url": url}


@pytest.mark.parametrize("url", ACCEPTED)
def test_round2_the_mds2_link_accepts_ordinary_keys_and_anchors(pump_model, kim, url):
    """Skeptic core #2 / #4 / #7: a manufacturer's page with a section anchor, or a document portal's ordinary keys, is saved as typed."""
    assert add_release(pump_model, by=kim, **_mds2(url)).mds2_url == url


@pytest.mark.parametrize("url", REFUSED)
def test_round2_the_mds2_link_refuses_credential_keys(pump_model, kim, url):
    """A session id (?sid=), an authorization, a key, a signature, a password, or a token, in the query, a fragment's key=value, a hash
    route's query, or a path parameter: refused, nothing saved."""
    assert refused(add_release, pump_model, by=kim, **_mds2(url)) == {"mds2_url": services.URL_WORDS}
    assert not SoftwareRelease.objects.exists()


# --- 3. Changes for IT: a rental's whole stay entered late ---------------------------------------------------------------------------

def test_round2_a_rentals_stay_entered_late_shows_its_arrival_and_its_return(ctx, dept, pump_model, today):
    """Skeptic core #6: a rental arrived 20 days ago and was entered 10 days ago (its "+" row); it went back 15 days ago, and Return to
    owner is entered today. Changes for IT lists both its addition and its return (a return dated before the day it was entered never
    makes it "never on site"). A device of ours whose retirement is backdated before its addition still reads as never on site."""
    release(pump_model, connects=Connects.WIRELESS)
    rental = eq.add_temporary_device(tag="R-1", device_model=pump_model, department=dept, kind=Ownership.RENTAL, owner="Acme Rentals",
                                     serial="SN-1", added_as=AddedAs.EXISTING, arrived_on=today - 20 * DAY)
    Asset.history.filter(id=rental.pk).update(history_date=at(today - 10 * DAY))  # entered 10 days ago
    eq.return_to_owner(rental, cleaning=ReturnCleaning.DECONTAMINATED, data=ReturnData.NONE_STORED, on=today - 15 * DAY)
    ours = eq.create_asset(tag="P-BACK", device_model=pump_model, department=dept, installed_on=today - 3000 * DAY)
    eq.set_status(ours, AssetStatus.RETIRED, changed_on=today - 500 * DAY)  # the previous system's retirement, backdated
    changes = [(c.asset.tag, c.kind, c.day, c.words) for c in rules.changes_for_it(today, 30)]
    assert changes == [
        ("R-1", "added", today - 10 * DAY, f"Added {rules.day_words(today - 10 * DAY)} (already in use here)"),
        ("R-1", "returned", today - 15 * DAY, f"Returned to its owner {rules.day_words(today - 15 * DAY)}"),
    ]


# --- 4. Add release restoring a version entered in error ---------------------------------------------------------------------------

@pytest.fixture
def r31(ctx, vent_model, today):
    """Skeptic web #1-#3's ventilator 3.1: its OS (Linux 4.19) with its days, its MDS2 on file (2019 form, DEMO-MDS2-0001, an https link,
    released), and a disclosure answer; then marked entered in error."""
    row = release(vent_model, "3.1", os_family=OsFamily.LINUX, os_name="Linux 4.19", os_support_ends_on=today + 100 * DAY,
                  security_updates_end_on=today + 200 * DAY, support_checked_on=today - 5 * DAY, mds2=Mds2Status.ON_FILE,
                  mds2_edition=Mds2Edition.E2019, mds2_document="DEMO-MDS2-0001", mds2_url="https://manufacturer.example/mds2/demo",
                  mds2_released_on=today - 400 * DAY, patching=Patching.MANUFACTURER, connects=Connects.WIRED)
    services.remove_release(row, by=None)
    return row


SUPPORT = ("os_family", "os_name", "os_support_ends_on", "security_updates_end_on", "support_checked_on")
MDS2 = ("mds2", "mds2_edition", "mds2_document", "mds2_url", "mds2_released_on", "mds2_requested_on")
DISCLOSURE = ("patching", "connects")


def _facts(row, names) -> dict:
    row.refresh_from_db()
    return {f: getattr(row, f) for f in names}


def _restore(client, make_user, vent_model, data):
    client.force_login(make_user("technician"))
    r = client.post(f"/security/models/{vent_model.pk}/releases/new/", data, **HX)
    assert r.status_code == 200
    return r


def _toast(r) -> str:
    out = {}
    for h in ("HX-Trigger", "HX-Trigger-After-Settle"):
        if h in r:
            out.update(json.loads(r[h]))
    return out.get("toast", {}).get("value", "")


RESTORED_OVER = "Version 3.1 restored (it was entered in error), with the facts it had and what you entered over them"


@pytest.mark.parametrize("blanks", [False, True])
def test_round2_restoring_with_another_mds2_status_replaces_the_mds2_group(client, make_user, vent_model, r31, today, blanks):
    """Skeptic web #1-#3: 3.1 restored through Add release with MDS2 "Requested" and the day it was requested (the on-file fields left
    blank, or sent blank): restored, the MDS2 group as typed (form, document, link, release day cleared), the OS and the disclosure
    left blank keep what 3.1 had."""
    support, disclosure = _facts(r31, SUPPORT), _facts(r31, DISCLOSURE)
    data = {"version": "3.1", "mds2": Mds2Status.REQUESTED, "mds2_requested_on": (today - DAY).isoformat()}
    if blanks:
        data.update({"mds2_edition": "", "mds2_document": "", "mds2_url": "", "mds2_released_on": ""})
    r = _restore(client, make_user, vent_model, data)
    assert _toast(r) == RESTORED_OVER, r.content.decode()[:2000]
    r31.refresh_from_db()
    assert r31.removed_on is None
    assert _facts(r31, MDS2) == {"mds2": Mds2Status.REQUESTED, "mds2_edition": "", "mds2_document": "", "mds2_url": "",
                                 "mds2_released_on": None, "mds2_requested_on": today - DAY}
    assert _facts(r31, SUPPORT) == support and _facts(r31, DISCLOSURE) == disclosure


def test_round2_restoring_with_no_operating_system_replaces_the_support_group(client, make_user, vent_model, r31):
    """Skeptic web #2-#3: 3.1 restored with Operating system "None" over its stored "Linux 4.19": restored, the support group as typed
    (no OS name, no OS day, and the other days left blank cleared); the MDS2 and the disclosure keep what 3.1 had."""
    mds2, disclosure = _facts(r31, MDS2), _facts(r31, DISCLOSURE)
    r = _restore(client, make_user, vent_model, {"version": "3.1", "os_family": OsFamily.NONE})
    assert _toast(r) == RESTORED_OVER, r.content.decode()[:2000]
    r31.refresh_from_db()
    assert r31.removed_on is None
    assert _facts(r31, SUPPORT) == {"os_family": OsFamily.NONE, "os_name": "", "os_support_ends_on": None, "security_updates_end_on": None,
                                    "support_checked_on": None}
    assert _facts(r31, MDS2) == mds2 and _facts(r31, DISCLOSURE) == disclosure


def test_round2_restoring_with_the_version_alone_keeps_every_group(client, make_user, vent_model, r31):
    """Every group left blank keeps its facts: the version alone brings back all of them."""
    before = _facts(r31, (*SUPPORT, *MDS2, *DISCLOSURE))
    r = _restore(client, make_user, vent_model, {"version": "3.1"})
    assert _toast(r) == "Version 3.1 restored (it was entered in error), with the facts it had"
    assert _facts(r31, (*SUPPORT, *MDS2, *DISCLOSURE)) == before


# --- 5. Add release with a blank version and another bad field -------------------------------------------------------------------

BAD = {"security_updates_end_on": "2025-13-45", "mds2_document": "has space"}


def test_round2_the_service_refuses_a_blank_version_with_every_other_problem(kim, pump_model, vent_model):
    """Skeptic web #5 in the service: add_release(profile=False) with a blank version and an impossible day and a document number with
    a space: one refusal carrying all three; a model with a profile is asked for the version too (never "already has a profile")."""
    others = refused(add_release, vent_model, by=kim, profile=False, version="1.0", **BAD)
    assert set(others) == {"security_updates_end_on", "mds2_document"}
    assert refused(add_release, vent_model, by=kim, profile=False, version="", **BAD) == {**others, "version": services.VERSION_NEEDED}
    assert refused(add_release, vent_model, by=kim, profile=False, version="  ") == {"version": services.VERSION_NEEDED}
    release(pump_model, connects=Connects.WIRELESS)
    assert refused(add_release, pump_model, by=kim, profile=False, version="", os_family="embedded") == {"version": services.VERSION_NEEDED}
    assert not SoftwareRelease.objects.filter(device_model=vent_model).exists()


def test_round2_add_release_with_a_blank_version_reports_every_problem(client, make_user, vent_model):
    """Skeptic web #5's scenario: Add release with Version blank, an impossible security-updates day, and a document number with a space:
    all three come back at once, under their fields; nothing stored."""
    def errors(body: str) -> dict:
        return {m.group(1): unescape(m.group(2)) for m in re.finditer(r'<span class="err" id="cy-(\w+)_error">([^<]*)</span>', body)}

    client.force_login(make_user("technician"))
    url = f"/security/models/{vent_model.pk}/releases/new/"
    others = errors(client.post(url, {"version": "1.0", **BAD}, **HX).content.decode())
    assert set(others) == {"security_updates_end_on", "mds2_document"}
    blank = errors(client.post(url, {"version": "", **BAD}, **HX).content.decode())
    assert blank == {**others, "version": services.VERSION_NEEDED}
    assert not SoftwareRelease.objects.filter(device_model=vent_model).exists()


# --- 6. A department or category with no device on site -------------------------------------------------------------------------

@pytest.fixture
def emptied(ctx, dept, pump_model, today):
    """Skeptic web #4: the pumps in the ICU on site; Storage holds only a retired device (of the sterilizer model, so its category,
    Sterilizers, has none on site either); Spare holds no device at all."""
    release(pump_model, connects=Connects.WIRELESS)
    for tag in ("P-1", "P-2"):
        Asset.objects.create(tag=tag, device_model=pump_model, department=dept)
    storage = Department.objects.create(name="Storage")
    spare = Department.objects.create(name="Spare")
    ster = DeviceModel.objects.create(manufacturer="Steris", model="Amsco 400", description="Steam sterilizer", category="Sterilizers",
                                      risk_class=RiskClass.MEDIUM)
    Asset.objects.create(tag="S-OLD", device_model=ster, department=storage, status=AssetStatus.RETIRED)
    return {"storage": storage, "spare": spare}


@pytest.mark.parametrize("query", ["dept={storage}", "dept={spare}", "category=Sterilizers", "dept={storage}&category=Sterilizers"])
def test_round2_a_filter_with_no_device_on_site_answers_none_on_the_screen_and_in_the_api(client, make_user, emptied, query):
    """A bookmarked Fleet address for a department (or category) with no device on site: the screen keeps the filter and lists none, as
    GET /api/v1/device-security/ answers for the same query, never the whole fleet."""
    q = query.format(storage=emptied["storage"].pk, spare=emptied["spare"].pk)
    client.force_login(make_user("technician"))
    api = client.get(f"/api/v1/device-security/?{q}")
    assert api.status_code == 200 and api.json()["results"] == []
    r = client.get(f"/security/?{q}")
    assert r.status_code == 200
    assert r.context["f"].any and r.context["fleet"].devices == 0 and r.context["rows"] == []
    assert "No devices on site match these filters." in r.content.decode()
    assert client.get("/security/").context["fleet"].devices == 2  # the whole fleet, unfiltered


def test_round2_the_fleet_offers_and_keeps_every_department_and_category(client, make_user, emptied):
    client.force_login(make_user("technician"))
    storage, spare = emptied["storage"], emptied["spare"]
    body = client.get(f"/security/?dept={storage.pk}").content.decode()
    assert f'<option value="{storage.pk}" selected>Storage</option>' in body
    assert f'<option value="{spare.pk}">Spare</option>' in body
    body = client.get("/security/?category=Sterilizers").content.decode()
    assert '<option value="Sterilizers" selected>Sterilizers</option>' in body


# --- 7. Deleting a model the register holds: the words by reader ------------------------------------------------------------------

def _reader(tenant, slug, cyber_level):
    role = Role.objects.create(name=slug.title(), slug=slug)
    levels = {m: Level.NONE for m in Module.values}
    levels[Module.EQUIPMENT] = Level.FULL
    levels[Module.CYBERSECURITY] = cyber_level
    role.set_levels(levels)
    return User.objects.create_user(username=f"{slug}@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role)


@pytest.fixture
def held(ctx, pump_model, kim):
    """Skeptic api #1: the BD Alaris 8015 PCU with a profile and release 1.0, later marked entered in error."""
    release(pump_model, connects=Connects.WIRELESS)
    services.remove_release(add_release(pump_model, by=kim, version="1.0"), by=kim)
    return pump_model


@pytest.mark.parametrize("with_devices", [False, True])
def test_round2_the_delete_refusal_tells_a_reader_outside_the_register_only_that_it_holds_it(client, tenant, dept, held, with_devices):
    """Equipment Full and no Device security level (a custom role made before slice 37): DELETE /api/v1/device-models/<id>/ answers only
    that the register holds the model (with its devices, if any): never a profile, a count of releases, or one entered in error."""
    if with_devices:
        Asset.objects.create(tag="P-1", device_model=held, department=dept)
    client.force_login(_reader(tenant, "equipment-only", Level.NONE))
    assert client.get("/api/v1/software-releases/").status_code == 403
    r = client.delete(f"/api/v1/device-models/{held.pk}/")
    assert r.status_code == 400
    held_words = "devices, and records" if with_devices else "records"
    detail = r.json()["detail"]
    assert detail == f"BD Alaris 8015 PCU has {held_words} in the Device security register, so it cannot be deleted."
    assert not any(w in detail for w in ("profile", "release", "entered in error", "1.0"))
    assert DeviceModel.objects.filter(pk=held.pk).exists()


@pytest.mark.parametrize("with_devices", [False, True])
def test_round2_the_delete_refusal_tells_a_register_reader_what_it_holds(client, tenant, dept, held, with_devices):
    """The same delete by a reader with Device security View: the detailed words (a release entered in error and a profile)."""
    if with_devices:
        Asset.objects.create(tag="P-1", device_model=held, department=dept)
    client.force_login(_reader(tenant, "equipment-and-register", Level.VIEW))
    assert client.get("/api/v1/software-releases/").status_code == 200
    r = client.delete(f"/api/v1/device-models/{held.pk}/")
    assert r.status_code == 400
    what = "a software release entered in error and a profile in the Device security register"
    if with_devices:
        what = f"devices, and {what}"
    assert r.json()["detail"] == f"BD Alaris 8015 PCU has {what}, {KEEPS}"
    assert DeviceModel.objects.filter(pk=held.pk).exists()
