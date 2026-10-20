"""
Slice 37 review fixes, F1 (apps/cybersecurity/rules.py and services.py), each on its finding's own scenario:

- C1: Changes for IT reads the history rows near its window (the window's, each device's last row before it, the retired rows of the
  devices added in it), never each device's whole history; the same changes in the same three queries.
- C31: a return is dated the day the device went back (the returned_on its history row holds), in the window and in the words.
- C4 / C10 / C12 / C23 / C27: a release only retired or returned devices name can be marked entered in error and have its version
  corrected; the refusals count the devices on site of the release's model, the count the Models panel shows.
- C13: locking a release (and add_release's lookup) locks the release's own row, never its model's.
- C6: the MDS2 link refuses session and secret keys (sessionid, PHPSESSID, JSESSIONID, a path parameter, client_secret, ...).
- C7: the bulk cap's refusal never asks for a department when the devices are all one department's.
- C2 / C3 (PostgreSQL): a model change in progress is waited for and seen (the old model's release refused, never saved); the bulk set
  locks the devices before their security rows, so it never deadlocks with an import chunk changing models in tag order (rounds 2 and
  3: the device lock's order; the devices are tagged against their pks, so no test leans on random UUIDs); a deadlock it does meet
  answers RACE in words, never a server error.
"""
import threading
import time
import uuid
from datetime import datetime, timedelta
from datetime import time as clock

import psycopg
import pytest
from cyber_fixtures import facts as record
from cyber_fixtures import release
from django.core.exceptions import ValidationError
from django.db import DatabaseError, OperationalError, connection, transaction
from django.db.models import F
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from pg_helpers import needs_postgres

from apps.cybersecurity import rules, services
from apps.cybersecurity.models import Connects, DeviceSecurity, Mds2Edition, Mds2Status, SoftwareRelease
from apps.cybersecurity.services import add_release, record_facts, record_facts_for, remove_release, update_release
from apps.equipment import services as eq
from apps.equipment.models import AddedAs, Asset, AssetStatus, Department, DeviceModel, Ownership, ReturnCleaning, ReturnData
from apps.tenants.context import tenant_context

DAY = timedelta(days=1)
HX = {"HTTP_HX_REQUEST": "true"}


def refused(fn, *args, **kwargs) -> dict:
    with pytest.raises(ValidationError) as e:
        fn(*args, **kwargs)
    return {k: v[0] if len(v) == 1 else v for k, v in e.value.message_dict.items()}


def device(model, dept, tag, **fields):
    return Asset.objects.create(tag=tag, device_model=model, department=dept, **fields)


def at(day, hour=12):
    return timezone.make_aware(datetime.combine(day, clock(hour)))


@pytest.fixture
def today(ctx):
    return timezone.localdate()


@pytest.fixture
def kim(make_user):
    return make_user("director")


# --- C1: Changes for IT reads the rows near its window -----------------------------------------------------------------------------

def _rows_read(monkeypatch) -> list:
    """The history rows changes_for_it hands to _events, as (device id, history_type, status), each call's appended."""
    seen, real = [], rules._events

    def spy(rows, start, today):
        rows = list(rows)
        seen.extend((r[0], r[1], r[2]) for r in rows)
        return real(rows, start, today)

    monkeypatch.setattr(rules, "_events", spy)
    return seen


def test_changes_for_it_reads_only_the_rows_near_its_window(ctx, dept, pump_model, today, monkeypatch, django_assert_num_queries):
    """C1's scenario: a device with 50 history rows years old (PM completions, edits) and one change this month contributes its last
    row before the window and the change, never its 50 rows; a device with only old rows contributes none. A device added this month
    whose retirement is dated years before it (the previous system's) still reads as never on site."""
    release(pump_model, connects=Connects.WIRELESS)
    busy = eq.create_asset(tag="P-BUSY", device_model=pump_model, department=dept, installed_on=today - 3000 * DAY)
    quiet = eq.create_asset(tag="P-QUIET", device_model=pump_model, department=dept, installed_on=today - 3000 * DAY)
    for a in (busy, quiet):
        for i in range(50):
            a.last_pm_on = today - (400 - i) * DAY
            a.save()
        Asset.history.filter(id=a.pk).update(history_date=at(today - 400 * DAY))
    eq.set_status(busy, AssetStatus.MISSING)  # this month
    back = eq.create_asset(tag="P-BACK", device_model=pump_model, department=dept, installed_on=today - 3000 * DAY)
    eq.set_status(back, AssetStatus.RETIRED, changed_on=today - 500 * DAY)  # added today, its retirement dated before: never on site
    seen = _rows_read(monkeypatch)
    with django_assert_num_queries(3):  # the history; the devices with their rows; the profiles
        changes = rules.changes_for_it(today)
    assert [(c.asset.tag, c.kind, c.day) for c in changes] == [("P-BUSY", "missing", today)]
    by_device = {pk: [(kind, status) for p, kind, status in seen if p == pk] for pk in (busy.pk, quiet.pk, back.pk)}
    assert by_device[busy.pk] == [("~", AssetStatus.IN_SERVICE), ("~", AssetStatus.MISSING)]  # its last row before, and the change
    assert by_device[quiet.pk] == []
    assert by_device[back.pk] == [("~", AssetStatus.RETIRED), ("+", AssetStatus.IN_SERVICE)]  # the backdated retirement is read
    assert len(seen) == 4


def test_changes_for_it_status_before_the_window_decides_a_change(ctx, dept, pump_model, today, monkeypatch):
    """The last row before the window carries the status then: a device missing since long ago and saved again this month is no new
    change; one found and lost again this month is."""
    release(pump_model, connects=Connects.WIRELESS)
    lost = eq.create_asset(tag="P-LOST", device_model=pump_model, department=dept, installed_on=today - 3000 * DAY)
    eq.set_status(lost, AssetStatus.MISSING)
    lost = Asset.objects.get(pk=lost.pk)
    for i in range(10):
        lost.notes = f"searched {i}"
        lost.save()
    Asset.history.filter(id=lost.pk).update(history_date=at(today - 300 * DAY))
    lost.notes = "still looking"
    lost.save()  # this month, still missing
    seen = _rows_read(monkeypatch)
    assert rules.changes_for_it(today) == []
    assert len(seen) == 2  # its last row before the window (missing), and this month's
    eq.set_status(lost, AssetStatus.IN_SERVICE)
    eq.set_status(lost, AssetStatus.MISSING)
    assert [(c.asset.tag, c.kind) for c in rules.changes_for_it(today)] == [("P-LOST", "missing")]


# --- C31: a return is dated the day the device went back ---------------------------------------------------------------------------

def test_a_return_entered_late_is_dated_the_day_it_went_back(ctx, dept, pump_model, today):
    """C31's scenario: a rental returned 45 days ago, entered today. Its history row is dated today; Changes for IT dates it by its
    returned_on, in the window (not in the last 7 days, in the last 90) and in the words."""
    release(pump_model, connects=Connects.WIRELESS)
    late = eq.add_temporary_device(tag="R-LATE", device_model=pump_model, department=dept, kind=Ownership.RENTAL, owner="Acme Rentals",
                                   serial="SN-1", added_as=AddedAs.EXISTING, arrived_on=today - 60 * DAY)
    soon = eq.add_temporary_device(tag="R-SOON", device_model=pump_model, department=dept, kind=Ownership.RENTAL, owner="Acme Rentals",
                                   serial="SN-2", added_as=AddedAs.EXISTING, arrived_on=today - 60 * DAY)
    Asset.history.filter(id__in=[late.pk, soon.pk]).update(history_date=at(today - 60 * DAY))  # added when they arrived
    eq.return_to_owner(late, cleaning=ReturnCleaning.DECONTAMINATED, data=ReturnData.NONE_STORED, on=today - 45 * DAY)
    eq.return_to_owner(soon, cleaning=ReturnCleaning.DECONTAMINATED, data=ReturnData.NONE_STORED, on=today - 3 * DAY)
    went = Asset.objects.get(pk=late.pk).returned_on
    assert went == today - 45 * DAY
    assert local_days(late) == [today - 60 * DAY, today]  # the return's row is dated the day it was entered
    assert [(c.asset.tag, c.kind, c.day, c.words) for c in rules.changes_for_it(today, 7)] == [
        ("R-SOON", "returned", today - 3 * DAY, f"Returned to its owner {rules.day_words(today - 3 * DAY)}")]
    assert [(c.asset.tag, c.kind, c.day, c.words) for c in rules.changes_for_it(today, 90) if c.kind == "returned"] == [
        ("R-SOON", "returned", today - 3 * DAY, f"Returned to its owner {rules.day_words(today - 3 * DAY)}"),
        ("R-LATE", "returned", went, f"Returned to its owner {rules.day_words(went)}")]


def local_days(asset) -> list:
    from apps.core.days import local_day

    return [local_day(d) for d in Asset.history.filter(id=asset.pk).order_by("history_date").values_list("history_date", flat=True)]


# --- C4 / C10 / C12 / C23 / C27: a release only retired or returned devices name --------------------------------------------------

@pytest.fixture
def typo(ctx, dept, pump_model, kim, today):
    """The scenario: 9.3.31 added by mistake (for 9.33.1), recorded on pump P-1 and on the rental R-1; P-1 retired, R-1 returned."""
    release(pump_model, connects=Connects.WIRELESS)
    wrong = add_release(pump_model, by=kim, version="9.3.31")
    p1 = device(pump_model, dept, "P-1")
    rental = eq.add_temporary_device(tag="R-1", device_model=pump_model, department=dept, kind=Ownership.RENTAL, owner="Acme Rentals",
                                     serial="SN-1", added_as=AddedAs.EXISTING, arrived_on=today - 5 * DAY)
    record_facts(p1, by=kim, release=wrong)
    record_facts(rental, by=kim, release=wrong)
    eq.set_status(p1, AssetStatus.RETIRED)
    eq.return_to_owner(rental, cleaning=ReturnCleaning.DECONTAMINATED, data=ReturnData.NONE_STORED)
    return {"wrong": wrong, "p1": p1, "rental": rental}


def test_a_release_only_retired_and_returned_devices_name_can_be_entered_in_error(typo, pump_model, kim, today):
    wrong, p1, rental = typo["wrong"], typo["p1"], typo["rental"]
    assert refused(record_facts, p1, by=kim, release=None) == {"asset": "P-1 is retired: its facts stay as they were."}
    assert services.removal_refusal(wrong) == "" and services.running(wrong) == 0
    histories = {a.pk: DeviceSecurity.objects.get(asset=a).history.count() for a in (p1, rental)}
    assert remove_release(wrong, by=kim).removed_on == today
    for a in (p1, rental):  # their records keep naming it, as they were (no history written), read as not recorded
        sec = DeviceSecurity.objects.get(asset=a)
        assert sec.release_id == wrong.pk and sec.history.count() == histories[a.pk]
        assert sec.history.order_by("-history_date", "-history_id").first().release_id == wrong.pk
        assert rules.release_of(Asset.objects.get(pk=a.pk)) is None
    assert rules.releases(pump_model) == []  # out of every picker
    back = add_release(pump_model, by=kim, version="9.3.31")  # adding it again restores it: the records name it again
    assert back.pk == wrong.pk and rules.release_of(Asset.objects.get(pk=p1.pk)) == back


def test_a_version_only_retired_and_returned_devices_name_can_be_corrected(typo, kim):
    fixed = update_release(typo["wrong"], by=kim, version="9.33.1")
    assert (fixed.version, fixed.version_key) == ("9.33.1", "9.33.1")
    assert DeviceSecurity.objects.get(asset=typo["p1"]).release.version == "9.33.1"  # the retired record follows the correction


def test_the_refusals_count_what_the_panel_counts(typo, ctx, dept, pump_model, vent_model, kim):
    """An on-site pump names it too, and so does a ventilator's record (a leftover of a model change racing a record, made directly):
    the refusals count the one on-site pump, as the Models panel does (devices on site of the release's model)."""
    wrong = typo["wrong"]
    p2 = device(pump_model, dept, "P-2")
    record_facts(p2, by=kim, release=wrong)
    stray = device(vent_model, dept, "V-STRAY")
    record(stray, release=wrong)
    panel = sum(1 for s in rules.standings(rules.on_site().filter(device_model=pump_model), timezone.localdate()).values()
                if s.release is not None and s.release.pk == wrong.pk)
    assert panel == services.running(wrong) == 1
    words = "1 device runs this release: set it to another first."
    assert services.removal_refusal(wrong) == words
    assert refused(remove_release, wrong, by=kim) == {"release": words}
    assert refused(update_release, wrong, by=kim, version="9.33.1") == {"version": services.VERSION_NAMED}
    record_facts(p2, by=kim, release=None)
    assert services.removal_refusal(wrong) == "" and remove_release(wrong, by=kim).removed_on is not None
    profile = rules.profile(pump_model)
    assert services.removal_refusal(profile) == services.PROFILE_KEPT and services.removal_refusal(wrong) == services.ALREADY_REMOVED


# --- C13: a release's lock is its own row's ----------------------------------------------------------------------------------------

def _release_selects(queries) -> list:
    return [q["sql"] for q in queries if q["sql"].lstrip().upper().startswith("SELECT") and "cybersecurity_softwarerelease" in
            q["sql"].split(" WHERE ")[0]]


def test_locking_a_release_never_joins_its_model(ctx, pump_model, kim):
    """The locking reads (_locked_release: update, remove, record, the bulk set; add_release's lookup) read the release's table alone:
    the Meta ordering's model columns would join (and on PostgreSQL lock) the model's row."""
    with CaptureQueriesContext(connection) as q:
        r = add_release(pump_model, by=kim, version="9.33.1")
    lookups = _release_selects(q.captured_queries)
    assert len(lookups) == 1 and "equipment_devicemodel" not in lookups[0]
    with transaction.atomic(), CaptureQueriesContext(connection) as q:
        services._locked_release(r)
    (sql,) = q.captured_queries
    assert "equipment_devicemodel" not in sql["sql"]
    if connection.vendor == "postgresql":
        assert 'FOR UPDATE OF "cybersecurity_softwarerelease"' in sql["sql"] and "FOR UPDATE OF" in lookups[0]


# --- C6: the MDS2 link's session and secret keys -----------------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://vendor.example.com/mds2.pdf?PHPSESSID=abc123",
    "https://vendor.example.com/mds2.pdf?sessionid=abc123",
    "https://vendor.example.com/mds2.pdf?JSESSIONID=abc123",
    "https://vendor.example.com/mds2.pdf;jsessionid=abc123",
    "https://vendor.example.com/docs;jsessionid=abc123/mds2.pdf",
    "https://vendor.example.com/mds2.pdf?client_secret=abc123",
    "https://vendor.example.com/mds2.pdf?secret=abc123",
    "https://vendor.example.com/mds2.pdf?authorization=abc123",
    "https://vendor.example.com/mds2.pdf?X-Goog-Credential=abc123",
    "https://vendor.example.com/mds2.pdf?jwt=abc123",
    "https://vendor.example.com/mds2.pdf?passcode=abc123",
    "https://vendor.example.com/mds2.pdf?X-Amz-Date=20260101",
    "https://vendor.example.com/mds2.pdf?id=7;sessionid=abc123",
    "https://vendor.example.com/mds2.pdf?%73ession=abc123",
    "https://vendor.example.com/mds2.pdf#sessionid=abc123",
])
def test_the_mds2_link_refuses_session_and_secret_keys(pump_model, kim, url):
    fields = {"version": "9.1", "mds2": Mds2Status.ON_FILE, "mds2_edition": Mds2Edition.E2019, "mds2_url": url}
    assert refused(add_release, pump_model, by=kim, **fields) == {"mds2_url": services.URL_WORDS}
    assert not SoftwareRelease.objects.exists()


@pytest.mark.parametrize("url", [
    "https://vendor.example.com/mds2.pdf",
    "https://vendor.example.com/mds2.pdf?id=77&lang=en",
    "https://vendor.example.com/docs/mds2;v=2.pdf?doc=mds2&rev=3",
])
def test_the_mds2_link_keeps_plain_keys(pump_model, kim, url):
    fields = {"version": "9.1", "mds2": Mds2Status.ON_FILE, "mds2_edition": Mds2Edition.E2019, "mds2_url": url}
    assert add_release(pump_model, by=kim, **fields).mds2_url == url


# --- C7: the bulk cap's words ------------------------------------------------------------------------------------------------------

def test_the_bulk_cap_never_asks_for_a_department_it_has(ctx, dept, pump_model, kim, monkeypatch):
    """C7's scenario (a central pool: more pumps of one model in one department than the cap): the refusal says they are all one
    department's; devices across departments are still asked to narrow by one."""
    r = add_release(pump_model, by=kim, version="9.33.1")
    pool = [device(pump_model, dept, f"P-{i}") for i in range(3)]
    west = Department.objects.create(name="4 West")
    elsewhere = device(pump_model, west, "P-W")
    monkeypatch.setattr(services, "BULK_MAX", 2)
    one = refused(record_facts_for, pump_model, pool, by=kim, release=r)
    assert one == {"assets": services.TOO_MANY_IN_ONE.format(count=3)} and "choose a department" not in one["assets"]
    assert one["assets"] == ("Set at most 500 devices at once: these 3 are all in one department, so record their facts on each device's "
                             "Security tab.")
    assert refused(record_facts_for, pump_model, [*pool, elsewhere], by=kim, release=r) == {"assets": services.TOO_MANY}
    assert not DeviceSecurity.objects.exists()


def test_the_bulk_cap_on_the_screen_with_a_department_chosen(client, ctx, dept, pump_model, make_user, monkeypatch):
    """The same through Set devices to this release with the department chosen: the refusal under the department never asks for one."""
    tech = make_user("technician")
    r = add_release(pump_model, by=None, version="9.33.1")
    for i in range(3):
        device(pump_model, dept, f"P-{i}")
    device(pump_model, Department.objects.create(name="4 West"), "P-W")
    client.force_login(tech)
    monkeypatch.setattr(services, "BULK_MAX", 2)
    resp = client.post(f"/security/models/{pump_model.pk}/set-release/", {"release": str(r.pk), "department": str(dept.pk), "network": "keep"},
                       **HX)
    body = resp.content.decode()
    assert services.TOO_MANY_IN_ONE.format(count=3).replace("'", "&#x27;") in body and "choose a department to narrow" not in body
    assert not DeviceSecurity.objects.exists()


# --- C3: a deadlock in the bulk set answers in words -------------------------------------------------------------------------------

def _deadlock(*args, **kwargs):
    error = OperationalError("deadlock detected")
    error.__cause__ = psycopg.errors.DeadlockDetected("deadlock detected")
    raise error


def test_a_deadlock_in_the_bulk_set_answers_race(ctx, dept, pump_model, kim, monkeypatch):
    """PostgreSQL ends the bulk set in a deadlock (an import chunk locking the same devices in its own order): RACE in words, nothing
    saved, never a server error. Any other operational error still raises."""
    r = add_release(pump_model, by=kim, version="9.33.1")
    devices = [device(pump_model, dept, f"P-{i}") for i in range(2)]
    monkeypatch.setattr(services, "_save", _deadlock)
    assert refused(record_facts_for, pump_model, devices, by=kim, release=r) == {"assets": services.RACE}

    def gone(*args, **kwargs):
        raise OperationalError("server closed the connection unexpectedly")

    monkeypatch.setattr(services, "_save", gone)
    with pytest.raises(OperationalError):
        record_facts_for(pump_model, devices, by=kim, release=r)
    assert not DeviceSecurity.objects.exists()


# --- PostgreSQL: a model change in progress, the lock order -----------------------------------------------------------------------

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
        t.join(40)
    assert not a.is_alive() and not b.is_alive()


def _another_waits_on_a_lock(timeout: float = 15) -> bool:
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


def _two_models(tenant, tags=("CE-1",)):
    """Models A (its releases 1.0 and 2.0) and B, and A's devices tagged `tags` in order, their pks against it (review round 3: the
    ids are drawn and sorted first, so the first tag has the highest pk: tag order, the device lock's, is never pk order by chance)."""
    ids = sorted((uuid.uuid4() for _ in tags), reverse=True)
    with tenant_context(tenant):
        dept = Department.objects.create(name="ICU")
        a = DeviceModel.objects.create(manufacturer="BD", model="Alaris A", description="Pump", category="Infusion pumps")
        b = DeviceModel.objects.create(manufacturer="BD", model="Alaris B", description="Pump", category="Infusion pumps")
        rel_a, rel_a2 = release(a, "1.0"), release(a, "2.0")
        assets = [Asset.objects.create(id=pk, tag=tag, device_model=a, department=dept) for pk, tag in zip(ids, tags)]
    return a, b, rel_a, rel_a2, assets


def _crossed(tenant) -> list:
    """Devices whose record names a release of another model (never: a release names its model)."""
    with tenant_context(tenant):
        return list(DeviceSecurity.objects.exclude(release=None).exclude(release__device_model_id=F("asset__device_model_id"))
                    .values_list("asset__tag", flat=True))


def _model_change(tenant, asset, model, changing: threading.Event):
    """An import chunk's transaction: the device's model changed (update_asset: its row FOR UPDATE, model_changed), held open until
    another connection waits on a lock, then committed."""
    def run():
        with tenant_context(tenant), transaction.atomic():
            eq.update_asset(Asset.objects.get(pk=asset.pk), by=None, device_model=model)
            changing.set()
            assert _another_waits_on_a_lock()
        return "committed"
    return run


@needs_postgres
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("with_row", [True, False])
def test_a_model_change_in_progress_refuses_the_old_models_release(tenant, with_row):
    """C2's scenario: an import chunk changes CE-1 from model A to B; meanwhile Record facts names A's 1.0. The record waits for the
    chunk (the device's row, locked after the release), then sees model B and refuses (CHOOSE_VERSION): nothing of A's is saved."""
    a, b, rel_a, rel_a2, (asset,) = _two_models(tenant)
    if with_row:
        with tenant_context(tenant):
            record_facts(asset, by=None, release=rel_a2)
    changing = threading.Event()
    errors, results = {}, {}

    def name():
        assert changing.wait(20)
        with tenant_context(tenant):
            return record_facts(asset, by=None, release=rel_a).release_id

    _both(_model_change(tenant, asset, b, changing), name, errors, results)
    assert results == {"first": "committed"} and isinstance(errors.get("second"), ValidationError), (results, errors)
    assert errors["second"].message_dict == {"release": [services.CHOOSE_VERSION]}
    assert _crossed(tenant) == []
    with tenant_context(tenant):
        assert Asset.objects.get(pk=asset.pk).device_model_id == b.pk
        assert list(DeviceSecurity.objects.values_list("release_id", flat=True)) == ([None] if with_row else [])


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_a_bulk_set_waits_for_a_model_change_in_progress(tenant):
    """C2's bulk side: Set devices to this release on model A while a chunk moves CE-1 to B: refused in words once the chunk commits."""
    a, b, rel_a, _rel_a2, (asset,) = _two_models(tenant)
    changing = threading.Event()
    errors, results = {}, {}

    def bulk():
        assert changing.wait(20)
        with tenant_context(tenant):
            return record_facts_for(a, [asset], by=None, release=rel_a)

    _both(_model_change(tenant, asset, b, changing), bulk, errors, results)
    assert results == {"first": "committed"} and isinstance(errors.get("second"), ValidationError), (results, errors)
    assert errors["second"].message_dict == {"assets": ["CE-1 is not BD Alaris A: set one model's devices at a time."]}
    assert _crossed(tenant) == []


def _chunk(tenant, devices, model, changing: threading.Event):
    """An import chunk: one transaction, each row in its own savepoint catching DatabaseError (apps.imports.services._apply's way),
    changing each device's model in the file's order; after the first, it waits until another connection waits on a lock."""
    def run():
        outcomes = []
        with tenant_context(tenant), transaction.atomic():
            for i, d in enumerate(devices):
                try:
                    with transaction.atomic():
                        eq.update_asset(Asset.objects.get(pk=d.pk), by=None, device_model=model)
                    outcomes.append("ok")
                except DatabaseError as e:
                    outcomes.append(f"db error: {type(e).__name__}")
                if i == 0:
                    changing.set()
                    assert _another_waits_on_a_lock()
        return outcomes
    return run


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_a_bulk_set_and_an_import_chunk_in_tag_order_never_deadlock(tenant):
    """C3's scenario, in tag order (review rounds 2 and 3 changed this test: the device lock runs in tag order, so the chunk's order
    that never deadlocks is tag order; it was pk order, and the tags' random pks made the test pass or fail by chance). CE-1 and CE-2
    (CE-1's pk the higher) are model A, only CE-2 has a security row. A chunk changes CE-1 then CE-2 to model B while Set devices to
    this release runs on A: the bulk waits on CE-1's row before it holds CE-2 or any security row, the chunk changes CE-2 and commits,
    and the bulk refuses in words. Never a deadlock, never A's release on a model-B device."""
    a, b, rel_a, rel_a2, (ce1, ce2) = _two_models(tenant, tags=("CE-1", "CE-2"))
    assert ce1.pk > ce2.pk  # tag order is not pk order
    with tenant_context(tenant):
        record_facts(ce2, by=None, release=rel_a2)
    changing = threading.Event()
    errors, results = {}, {}

    def bulk():
        assert changing.wait(20)
        with tenant_context(tenant):
            return record_facts_for(a, [ce2, ce1], by=None, release=rel_a)

    _both(_chunk(tenant, [ce1, ce2], b, changing), bulk, errors, results)
    assert results.get("first") == ["ok", "ok"], (results, errors)
    assert isinstance(errors.get("second"), ValidationError), (results, errors)  # it saw both moved: no deadlock (RACE), no 500
    assert errors["second"].message_dict == {"assets": ["CE-1, CE-2 are not BD Alaris A: set one model's devices at a time."]}
    assert _crossed(tenant) == []
    with tenant_context(tenant):
        assert set(Asset.objects.values_list("device_model_id", flat=True)) == {b.pk}
        assert list(DeviceSecurity.objects.values_list("release_id", flat=True)) == [None]  # model_changed cleared CE-2's


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_a_bulk_set_that_meets_a_chunk_in_another_order_answers_race(tenant):
    """A chunk changing CE-2 then CE-1 (its file's order, against tag order; review round 3 changed this test: tag order, the tags set
    against their pks, where it was pk order with random pks) holds CE-2 while the bulk holds CE-1 (FOR KEY SHARE) and waits on CE-2;
    the chunk then waits on CE-1: PostgreSQL ends the bulk (it waited first), which answers RACE in words, and the chunk commits both
    rows."""
    a, b, rel_a, _rel_a2, (ce1, ce2) = _two_models(tenant, tags=("CE-1", "CE-2"))
    assert ce1.pk > ce2.pk  # the chunk's order is pk order: tag order alone decides
    changing = threading.Event()
    errors, results = {}, {}

    def bulk():
        assert changing.wait(20)
        with tenant_context(tenant):
            return record_facts_for(a, [ce1, ce2], by=None, release=rel_a)

    _both(_chunk(tenant, [ce2, ce1], b, changing), bulk, errors, results)
    assert results.get("first") == ["ok", "ok"], (results, errors)
    assert isinstance(errors.get("second"), ValidationError), (results, errors)
    assert errors["second"].message_dict == {"assets": [services.RACE]}
    assert _crossed(tenant) == []
    with tenant_context(tenant):
        assert not DeviceSecurity.objects.exists()


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_a_release_lock_leaves_its_models_row_free(tenant):
    """C13 on PostgreSQL: while a writer holds a release's lock (here a bulk set of up to 500 devices would), another connection can
    still lock the release's model's row (an edit of the model, its risk score, an AEM decision)."""
    a, _b, rel_a, _rel_a2, _assets = _two_models(tenant)
    held, done = threading.Event(), threading.Event()
    errors, results = {}, {}

    def hold():
        with tenant_context(tenant), transaction.atomic():
            services._locked_release(rel_a)
            held.set()
            assert done.wait(20)
        return "held"

    def edit_model():
        assert held.wait(20)
        try:
            with transaction.atomic(), connection.cursor() as cur:
                cur.execute("SELECT 1 FROM equipment_devicemodel WHERE id = %s FOR UPDATE NOWAIT", [a.pk])
                return cur.fetchone()[0]
        finally:
            done.set()

    _both(hold, edit_model, errors, results)
    assert results == {"first": "held", "second": 1}, (results, errors)
