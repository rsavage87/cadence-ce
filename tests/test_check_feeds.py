"""
Slice 15, Check FDA feed: the openFDA fetch-and-store (apps.recalls.feeds) shared by the daily import and the Recalls screen,
the import command still matching every facility, and the screen's button: permissions, matching only the signed-in user's
facility, the page head kept current, the error toast, and the cooldown held in the database (a JobRun row) so it holds across
worker processes. No test touches the network: requests.get answers what each test sets, and fails the test otherwise.
"""
import json
from datetime import date, datetime, timedelta
from io import StringIO

import pytest
import requests
from django.core.cache import caches
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.models import F
from django.utils import timezone
from test_rls_paths import rls  # noqa: F401

from apps.accounts.models import create_default_roles
from apps.equipment.models import DeviceModel
from apps.jobs.models import JobRun
from apps.recalls import feeds
from apps.recalls.models import Alert, AlertMatch
from apps.tenants.context import tenant_context
from apps.web import views_recalls

HX = {"HTTP_HX_REQUEST": "true"}
URL = "/recalls/check-feed/"
TODAY = date.today()
PUMP = {"product_res_number": "Z-0101-2026", "res_event_number": "97001", "recalling_firm": "BD", "product_description": "Alaris 8015 PCU infusion pump",
        "reason_for_recall": "Keypad membrane may lift", "action": "Inspect keypad", "event_date_posted": "2026-09-11", "event_date_initiated": "2026-08-10"}
WIDGET = {"product_res_number": "Z-0102-2026", "res_event_number": "97002", "recalling_firm": "Acme Medical", "product_description": "Widget X lamp",
          "reason_for_recall": "Lamp may flicker", "event_date_posted": "2026-09-12"}


class Answer:
    """What requests.get returns: a status, and a JSON payload (or an exception json() raises)."""

    def __init__(self, status=200, payload=None):
        self.status_code = status
        self.payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Server Error")

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class Feed:
    def __init__(self):
        self.calls = []
        self.response = None  # an Answer, or an exception requests.get raises

    def records(self, *records, total=None):
        meta = {"meta": {"results": {"total": total if total is not None else len(records)}}}
        self.response = Answer(200, {**meta, "results": list(records)})


@pytest.fixture(autouse=True)
def openfda(monkeypatch):
    feed = Feed()

    def fake_get(url, params=None, timeout=None):
        feed.calls.append({"url": url, "params": params, "timeout": timeout})
        if feed.response is None:
            raise AssertionError("openFDA was asked, and this test expected no request")
        if isinstance(feed.response, Exception):
            raise feed.response
        return feed.response

    monkeypatch.setattr(feeds.requests, "get", fake_get)  # the requests module itself: the command's path patches the same function
    return feed


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug):
        user = make_user(role_slug)
        client.force_login(user)
        return user

    return _as


@pytest.fixture
def theirs(other_tenant):
    """The other facility owns the same pump model."""
    create_default_roles(other_tenant)
    with tenant_context(other_tenant):
        return DeviceModel.objects.create(manufacturer="BD", model="Alaris 8015 PCU", description="Pump", category="Infusion pumps")


def toast_of(response) -> str:
    return json.loads(response["HX-Trigger"])["toast"]["value"]


def check_row() -> JobRun:
    return JobRun.objects.get(job=feeds.CHECK_JOB)


def matches_of(tenant) -> list[str]:
    with tenant_context(tenant):
        return sorted(AlertMatch.objects.values_list("alert__external_id", flat=True))


# --- fetch and store --------------------------------------------------------------------------------------------------

def test_import_stores_new_alerts_and_counts_only_those(db, openfda):
    openfda.records(PUMP)
    assert feeds.import_recalls() == feeds.FeedResult(new=1, returned=1, total=1)
    openfda.records(PUMP, WIDGET)
    result = feeds.import_recalls()
    assert (result.new, result.returned, result.total, result.truncated) == (1, 2, 2, False)
    pump = Alert.objects.get(external_id="Z-0101-2026")
    assert (pump.source, pump.manufacturer, pump.title, pump.action, pump.classification) == ("fda", "BD", "Keypad membrane may lift", "Inspect keypad", "")
    assert pump.published_on == date(2026, 9, 11) and pump.raw == PUMP and pump.model_terms == []


def test_an_unchanged_alert_is_not_written_again_and_a_changed_one_is_updated(db, openfda, monkeypatch):
    openfda.records(PUMP, WIDGET)
    feeds.import_recalls()
    writes = []
    real = Alert.objects.update_or_create
    monkeypatch.setattr(Alert.objects, "update_or_create", lambda **kw: writes.append(kw["external_id"]) or real(**kw))
    revised = {**PUMP, "reason_for_recall": "Keypad membrane may lift; revised"}
    openfda.records(revised, WIDGET)
    assert feeds.import_recalls().new == 0
    assert writes == ["Z-0101-2026"] and Alert.objects.get(external_id="Z-0101-2026").title == "Keypad membrane may lift; revised"


def test_odd_records_are_kept_or_skipped_never_fatal(db, openfda):
    blank = {"product_res_number": "Z-0103-2026", "recalling_firm": None, "product_description": None, "reason_for_recall": None, "action": None,
             "event_date_posted": "soon", "event_date_initiated": "20260901"}
    no_id = {"recalling_firm": "BD", "product_description": "No recall number"}
    too_long = {**PUMP, "product_res_number": "Z" * 81}
    openfda.records(blank, no_id, too_long, "not a record", total=4)
    result = feeds.import_recalls()
    assert (result.new, result.returned) == (1, 4)
    alert = Alert.objects.get()
    assert (alert.external_id, alert.manufacturer, alert.title, alert.action, alert.published_on) == ("Z-0103-2026", "", "", "", date(2026, 9, 1))


def test_404_is_no_results(db, openfda):
    openfda.response = Answer(404, {"error": {"code": "NOT_FOUND"}})
    assert feeds.import_recalls() == feeds.FeedResult(0, 0, 0) and not Alert.objects.exists()


@pytest.mark.parametrize("response, message", [
    (requests.Timeout("read timed out"), "openFDA did not answer in time"),
    (requests.ConnectionError("no route"), "openFDA could not be reached"),
    (Answer(503, {}), "openFDA answered with an error (HTTP 503)"),
    (Answer(429, {}), "openFDA answered with an error (HTTP 429)"),
    (Answer(200, ValueError("Expecting value")), "openFDA's answer was not JSON"),
    (Answer(200, {"meta": {}}), "openFDA's answer held no recall records"),
    (Answer(200, ["not", "a", "dict"]), "openFDA's answer held no recall records"),
])
def test_feed_failures_raise_a_feed_error_and_store_nothing(db, openfda, response, message):
    openfda.response = response
    with pytest.raises(feeds.FeedError) as e:
        feeds.import_recalls()
    assert str(e.value) == message and not Alert.objects.exists()


def test_total_beyond_what_was_returned_is_reported(db, openfda):
    openfda.records(PUMP, total=2500)
    result = feeds.import_recalls()
    assert (result.returned, result.total, result.truncated) == (1, 2500, True)
    openfda.response = Answer(200, {"results": [PUMP]})  # no meta: nothing to compare
    assert feeds.import_recalls().total is None and feeds.import_recalls().truncated is False


def test_the_query_window_limit_manufacturer_and_timeout(db, openfda):
    openfda.records()
    feeds.import_recalls(days=7, limit=50, manufacturer='Becton "BD" Dickinson', timeout=5, today=date(2026, 9, 30))
    call = openfda.calls[0]
    assert call["url"] == feeds.ENDPOINT and call["timeout"] == 5
    assert call["params"] == {"search": 'event_date_posted:[20260923 TO 20260930] AND recalling_firm:"Becton BD Dickinson"', "limit": 50}


# --- the import command -----------------------------------------------------------------------------------------------

def test_the_command_still_matches_every_facility(tenant, other_tenant, theirs, openfda):
    with tenant_context(tenant):
        DeviceModel.objects.create(manufacturer="BD", model="Alaris 8015 PCU", description="Pump", category="Infusion pumps")
    openfda.records(PUMP, WIDGET)
    out, err = StringIO(), StringIO()
    call_command("import_openfda", days=30, stdout=out, stderr=err)
    assert out.getvalue().splitlines() == ["Imported 2 new alerts", "other: 1 new matches", "riverside: 1 new matches"]  # facilities by name
    assert err.getvalue() == "" and openfda.calls[0]["timeout"] == feeds.IMPORT_TIMEOUT and openfda.calls[0]["params"]["limit"] == 1000
    assert matches_of(tenant) == matches_of(other_tenant) == ["Z-0101-2026"]


def test_the_command_says_when_the_window_is_empty(db, openfda):
    openfda.response = Answer(404, {})
    out = StringIO()
    call_command("import_openfda", stdout=out)
    assert out.getvalue() == "No recalls in that window.\n"


def test_the_command_fails_clearly_when_openfda_does(tenant, openfda):
    openfda.response = Answer(500, {})
    out = StringIO()
    with pytest.raises(CommandError, match=r"openFDA answered with an error \(HTTP 500\); nothing imported"):
        call_command("import_openfda", stdout=out)
    assert out.getvalue() == "" and not Alert.objects.exists()


def test_the_command_touches_no_facility_table_before_entering_it(rls, tenant, openfda):  # noqa: F811
    with tenant_context(tenant):
        DeviceModel.objects.create(manufacturer="BD", model="Alaris 8015 PCU", description="Pump", category="Infusion pumps")
    openfda.records(PUMP)
    with rls:
        call_command("import_openfda", stdout=StringIO())
    assert rls.violations == [] and matches_of(tenant) == ["Z-0101-2026"]


# --- the button: who may press it ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("role", ["director", "manager"])
def test_the_button_is_shown_at_match_level(client, signed_in, role):
    signed_in(role)
    body = client.get("/recalls/").content.decode()
    assert f'hx-post="{URL}"' in body and "Check FDA feed" in body and 'hx-disabled-elt="this"' in body


@pytest.mark.parametrize("role", ["technician", "analyst"])
def test_view_only_roles_see_no_button_and_get_403(client, signed_in, openfda, role):
    signed_in(role)
    assert "Check FDA feed" not in client.get("/recalls/").content.decode()
    assert client.post(URL, **HX).status_code == 403
    assert openfda.calls == [] and not JobRun.objects.exists()  # refused before anything is fetched or claimed


@pytest.mark.parametrize("role", ["requester", "vendor"])
def test_roles_without_recalls_get_403(client, signed_in, openfda, role):
    signed_in(role)
    assert client.post(URL, **HX).status_code == 403 and openfda.calls == []


def test_get_is_not_allowed_and_signing_in_is_required(client, signed_in, openfda):
    assert client.post(URL).status_code == 302  # signed out: to the sign-in page
    signed_in("director")
    assert client.get(URL).status_code == 405 and openfda.calls == []


# --- the button: what it does -----------------------------------------------------------------------------------------

def test_a_check_fetches_matches_this_facility_only_and_says_so(client, signed_in, tenant, other_tenant, theirs, pump_model, openfda):
    signed_in("manager")
    openfda.records(PUMP, WIDGET)
    r = client.post(URL, **HX)
    assert r.status_code == 200 and toast_of(r) == "Checked the FDA recall feed: 2 new notices, 1 new match for your inventory"
    assert openfda.calls[0]["timeout"] == feeds.CHECK_TIMEOUT and "event_date_posted:[" in openfda.calls[0]["params"]["search"]
    assert matches_of(tenant) == ["Z-0101-2026"]
    assert matches_of(other_tenant) == []  # the other facility gets it at its next daily import or its own check
    body = r.content.decode()
    assert 'id="rc-body"' in body and "BD Alaris 8015 PCU" in body and "All alerts · 1" in body


def test_the_page_head_is_sent_out_of_band_with_the_new_dates(client, signed_in, pump_model, openfda):
    signed_in("director")
    head = client.get("/recalls/").content.decode()
    assert "newest FDA notice imported never" in head and "FDA feed checked" not in head
    openfda.records(PUMP)
    body = client.post(URL, **HX).content.decode()
    checked = timezone.localtime(check_row().finished_at)
    assert '<p class="sub" id="rc-feed" hx-swap-oob="true">' in body
    assert f"newest FDA notice imported {TODAY:%b} {TODAY.day}, {TODAY.year}" in body
    assert f"FDA feed checked {checked:%b} {checked.day}, {feeds.clock(checked)}" in body
    head = client.get("/recalls/").content.decode()
    assert f"FDA feed checked {checked:%b} {checked.day}, {feeds.clock(checked)}" in head and 'hx-swap-oob' not in head


def test_nothing_new(client, signed_in, pump_model, openfda):
    signed_in("director")
    openfda.records(WIDGET)
    client.post(URL, **HX)
    JobRun.objects.update(started_at=F("started_at") - feeds.CHECK_COOLDOWN)
    assert toast_of(client.post(URL, **HX)) == "Checked the FDA recall feed: no new alerts"
    openfda.records()
    JobRun.objects.update(started_at=F("started_at") - feeds.CHECK_COOLDOWN)
    with tenant_context(pump_model.tenant):
        DeviceModel.objects.create(manufacturer="Acme Medical", model="Widget X", description="Lamp", category="Lamps")
    assert toast_of(client.post(URL, **HX)) == "Checked the FDA recall feed: no new notices, 1 new match for your inventory"


def test_an_openfda_failure_toasts_and_is_recorded_never_a_500(client, signed_in, pump_model, openfda):
    signed_in("director")
    openfda.response = requests.Timeout("read timed out")
    r = client.post(URL, **HX)
    assert r.status_code == 200 and 'id="rc-body"' in r.content.decode()
    assert toast_of(r) == "Could not check the FDA recall feed: openFDA did not answer in time. Nothing changed; the daily import will try again."
    run = check_row()
    assert run.status == "failed" and "riverside: openFDA did not answer in time" in run.output
    assert not Alert.objects.exists() and feeds.last_checked() is None


def test_the_checks_are_logged_on_the_days_row(client, signed_in, pump_model, openfda):
    signed_in("director")
    openfda.records(PUMP)
    client.post(URL, **HX)
    run = check_row()
    assert (run.status, run.run_on) == ("succeeded", timezone.localdate()) and run.finished_at >= run.started_at
    assert run.output.endswith("riverside: 1 new alerts, 1 records returned (openFDA total 1); 1 new matches\n")
    JobRun.objects.update(started_at=F("started_at") - feeds.CHECK_COOLDOWN)
    openfda.response = Answer(502, {})
    client.post(URL, **HX)
    run = check_row()  # still one row for the day: each check moves it forward and adds a line
    assert run.status == "failed" and run.output.count("\n") == 2 and "openFDA answered with an error (HTTP 502)" in run.output


# --- the cooldown -----------------------------------------------------------------------------------------------------

def test_a_second_press_inside_the_cooldown_fetches_nothing_and_says_when(client, signed_in, pump_model, openfda):
    signed_in("director")
    openfda.records(PUMP)
    client.post(URL, **HX)
    for cache in caches.all():
        cache.clear()  # nothing about the cooldown is in a cache (per process in production)
    r = client.post(URL, **HX)
    last = check_row().started_at
    assert len(openfda.calls) == 1 and r.status_code == 200
    assert toast_of(r) == (f"The FDA recall feed was checked at {feeds.clock(last)}; it can be checked again at "
                           f"{feeds.clock(last + feeds.CHECK_COOLDOWN)}. No new matches.")
    JobRun.objects.update(started_at=F("started_at") - feeds.CHECK_COOLDOWN)
    assert toast_of(client.post(URL, **HX)) == "Checked the FDA recall feed: no new alerts" and len(openfda.calls) == 2


def test_the_cooldown_follows_a_failure_too(ctx, pump_model, openfda):
    now = timezone.now()
    openfda.response = Answer(503, {})
    with pytest.raises(feeds.FeedError):
        feeds.check_feed(now)
    check = feeds.check_feed(now + timedelta(minutes=14))
    assert check.result is None and check.checked_at == now and check.again_at == now + feeds.CHECK_COOLDOWN and len(openfda.calls) == 1
    openfda.records(PUMP)
    assert feeds.check_feed(now + timedelta(minutes=15)).result.new == 1 and len(openfda.calls) == 2


def test_another_facilitys_press_inside_the_cooldown_matches_what_is_stored(client, signed_in, tenant, other_tenant, theirs, make_user, openfda):
    """The feed is the same for everyone: the other facility's check fetches nothing yet still gets its matches."""
    signed_in("director")
    openfda.records(PUMP)
    client.post(URL, **HX)
    assert matches_of(other_tenant) == []
    client.force_login(make_user("manager", tenant_=other_tenant))
    r = client.post(URL, **HX)
    assert len(openfda.calls) == 1 and toast_of(r).endswith(". 1 new match for your inventory.") and matches_of(other_tenant) == ["Z-0101-2026"]
    assert "Alaris 8015 PCU" in r.content.decode()


def test_the_daily_import_counts_as_a_check(ctx, pump_model, openfda):
    now = timezone.now()
    JobRun.objects.create(job=feeds.DAILY_JOB, run_on=timezone.localdate(now), started_at=now - timedelta(minutes=5), status="succeeded",
                          finished_at=now - timedelta(minutes=4))
    check = feeds.check_feed(now)
    assert check.result is None and check.checked_at == now - timedelta(minutes=5) and openfda.calls == []
    assert feeds.last_checked() == now - timedelta(minutes=4)


def test_two_presses_at_once_fetch_once_when_the_days_row_is_new(ctx, pump_model, openfda, monkeypatch):
    """Between this process finding no row for the day and inserting it, another inserts it: the unique (job, run_on) constraint
    makes this one wait."""
    now = timezone.now()
    real_filter = JobRun.objects.filter
    raced = []

    class Late:
        def first(self):
            # The other process inserts the day's row just after this one looked (outside this one's savepoint, as it would be).
            JobRun.objects.bulk_create([JobRun(job=feeds.CHECK_JOB, run_on=timezone.localdate(now), started_at=now - timedelta(seconds=1))])
            raced.append(1)
            return None

    monkeypatch.setattr(JobRun.objects, "filter", lambda *a, **kw: Late() if "run_on" in kw and not raced else real_filter(*a, **kw))
    check = feeds.check_feed(now)
    assert raced and check.result is None and check.checked_at == now - timedelta(seconds=1) and openfda.calls == []
    assert JobRun.objects.count() == 1 and check_row().status == "running"  # the other process's row, untouched


def test_two_presses_at_once_fetch_once_when_the_days_row_exists(ctx, pump_model, openfda, monkeypatch):
    """Between this process reading the day's row and moving it forward, another moves it: the UPDATE matches the row only as it
    was read, so it changes nothing and this one waits."""
    now = timezone.now()
    row = JobRun.objects.create(job=feeds.CHECK_JOB, run_on=timezone.localdate(now), started_at=now - timedelta(hours=1), status="succeeded")
    real_filter = JobRun.objects.filter
    raced = []

    class Stale:
        def first(self):
            stale = real_filter(pk=row.pk).first()
            real_filter(pk=row.pk).update(started_at=now - timedelta(seconds=1))  # the other process claims it
            raced.append(1)
            return stale

    monkeypatch.setattr(JobRun.objects, "filter", lambda *a, **kw: Stale() if "run_on" in kw and not raced else real_filter(*a, **kw))
    check = feeds.check_feed(now)
    assert raced and check.result is None and check.checked_at == now - timedelta(seconds=1) and openfda.calls == []


def test_the_check_touches_no_facility_table_before_the_tenant_is_set(client, rls, signed_in, pump_model, openfda):  # noqa: F811
    signed_in("director")
    openfda.records(PUMP)
    with rls:
        assert client.post(URL, **HX).status_code == 200
    assert rls.violations == []


# --- the toast wording ------------------------------------------------------------------------------------------------

AT = timezone.make_aware(datetime(2026, 10, 2, 10, 42))  # local time (TIME_ZONE)


@pytest.mark.parametrize("check, message", [
    (feeds.Check(feeds.FeedResult(3, 273, 273), 1, AT, None), "Checked the FDA recall feed: 3 new notices, 1 new match for your inventory"),
    (feeds.Check(feeds.FeedResult(1, 273, 273), 0, AT, None), "Checked the FDA recall feed: 1 new notice, none match your inventory"),
    (feeds.Check(feeds.FeedResult(0, 273, 273), 2, AT, None), "Checked the FDA recall feed: no new notices, 2 new matches for your inventory"),
    (feeds.Check(feeds.FeedResult(0, 0, 0), 0, AT, None), "Checked the FDA recall feed: no new alerts"),
    (feeds.Check(feeds.FeedResult(2, 1000, 1250), 0, AT, None),
     "Checked the FDA recall feed: 2 new notices, none match your inventory (openFDA sent 1,000 of 1,250 recalls)"),
    (feeds.Check(None, 0, AT, AT + feeds.CHECK_COOLDOWN), "The FDA recall feed was checked at 10:42 AM; it can be checked again at 10:57 AM. No new matches."),
    (feeds.Check(None, 2, AT, AT + feeds.CHECK_COOLDOWN),
     "The FDA recall feed was checked at 10:42 AM; it can be checked again at 10:57 AM. 2 new matches for your inventory."),
])
def test_toast_wording(check, message):
    assert views_recalls._check_message(check) == message
