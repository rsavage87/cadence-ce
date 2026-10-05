"""
The openFDA device recall feed: fetch a window of recalls and keep them as global Alert rows, one per recalled product.

Two callers share it. The daily import (`manage.py import_openfda`, run by apps.jobs) fetches and then matches every facility.
The Recalls screen's Check FDA feed (`check_feed`) fetches the last 30 days with a short timeout and matches only the signed-in
user's facility; the others get the new alerts at their next daily import or their own check.

openFDA is free and needs no key for light use (https://open.fda.gov/apis/device/recall/), but keyless use is capped per address
(1,000 requests a day when this was written), and every facility on this server shares the address. So a check fetches at most
once per CHECK_COOLDOWN, counting the daily import: the feed is the same for every facility, so a fetch by anyone serves everyone.
The cooldown is kept in the database, in apps.jobs' JobRun table (a system table, like Alert): the check has one row per local
day (the server's, whichever facility checks: slice 21) whose `started_at` moves to each fetch, taken with a conditional UPDATE, so
it holds across worker processes and servers (the "limits" cache is per process). The row also records what each check found, next
to the daily import's runs.

ECRI alerts need an ECRI membership and API agreement; they would get their own importer.
"""
from datetime import date, datetime, timedelta
from typing import NamedTuple

import requests
from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import dateformat, timezone

from apps.jobs.models import JobRun
from apps.tenants.context import get_current_tenant

from . import services
from .models import Alert

ENDPOINT = "https://api.fda.gov/device/recall.json"
MAX_LIMIT = 1000  # openFDA's maximum per request
IMPORT_TIMEOUT = 30  # seconds: the daily import can wait

CHECK_DAYS = 30  # the same window as the daily import
CHECK_TIMEOUT = (3.05, 10)  # seconds to connect, then to wait for each read: a web request must not hang on a slow openFDA
CHECK_COOLDOWN = timedelta(minutes=15)  # at most 96 checks a day, whatever the number of facilities
CHECK_JOB = "check_fda_feed"  # the check's JobRun rows: one per day, the latest attempt (its status is that attempt's)
CHECK_OK_JOB = "check_fda_feed_ok"  # one per day, the day's last check that succeeded (a later failure never hides it)
DAILY_JOB = "import_openfda"  # the daily import's key in apps.jobs.services.DAILY_JOBS: its runs fetch the feed too
OUTPUT_LIMIT = 20_000  # characters of the day's check log kept, as for the daily jobs


class FeedError(Exception):
    """openFDA did not answer, or answered with something other than recall records. The message says which, for a person."""


class FeedResult(NamedTuple):
    new: int  # alerts stored for the first time
    returned: int  # records in openFDA's answer
    total: int | None  # recalls openFDA says the window holds; None when it did not say

    @property
    def truncated(self) -> bool:
        """openFDA holds more than it returned (it returns at most `limit` records, 1,000 per request)."""
        return self.total is not None and self.total > self.returned


class Check(NamedTuple):
    result: FeedResult | None  # what the fetch brought; None when the cooldown held the fetch back
    matches: int  # new matches for the current facility
    checked_at: datetime  # when the feed was fetched: now, or the last fetch when held back
    again_at: datetime | None  # held back: when the feed may be fetched again
    last_failed: bool = False  # held back: the last fetch did not get an answer


def parse_date(s):
    """openFDA dates: "2026-09-14" in the live feed, "20260914" in older records and in search syntax."""
    digits = str(s or "").replace("-", "")
    try:
        return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8])) if len(digits) == 8 and digits.isdigit() else None
    except ValueError:
        return None


def search_query(days: int, manufacturer: str | None = None, today: date | None = None) -> str:
    today = today or timezone.localdate()
    # By posting date, not initiation date: FDA posts a recall weeks after the firm starts it, so a window on the start date
    # finds almost nothing recent (1 record against 273 for the same 30 days when this was checked). Spaces, not "+":
    # requests encodes the spaces, and a literal "+" makes openFDA answer 500.
    search = f"event_date_posted:[{today - timedelta(days=days):%Y%m%d} TO {today:%Y%m%d}]"
    if manufacturer:
        firm = manufacturer.replace('"', "")  # a quote in the name would end the phrase
        search += f' AND recalling_firm:"{firm}"'
    return search


class _Answer(NamedTuple):
    records: list
    returned: int
    total: int | None


def fetch(days: int = CHECK_DAYS, limit: int = MAX_LIMIT, manufacturer: str | None = None, timeout=IMPORT_TIMEOUT,
          today: date | None = None) -> _Answer:
    """One request to openFDA for the recalls posted in the last `days`. A 404 is openFDA's "no results"."""
    params = {"search": search_query(days, manufacturer, today), "limit": limit}
    try:
        resp = requests.get(ENDPOINT, params=params, timeout=timeout)
    except requests.Timeout as e:
        raise FeedError("openFDA did not answer in time") from e
    except requests.RequestException as e:
        raise FeedError("openFDA could not be reached") from e
    if resp.status_code == 404:
        return _Answer([], 0, 0)
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        raise FeedError(f"openFDA answered with an error (HTTP {resp.status_code})") from e
    try:
        payload = resp.json()
    except ValueError as e:  # requests' JSONDecodeError is a ValueError
        raise FeedError("openFDA's answer was not JSON") from e
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        raise FeedError("openFDA's answer held no recall records")
    meta = payload.get("meta")
    counts = meta.get("results") if isinstance(meta, dict) else None
    total = counts.get("total") if isinstance(counts, dict) else None
    total = total if isinstance(total, int) and not isinstance(total, bool) else None
    return _Answer([r for r in results if isinstance(r, dict)], len(results), total)


def _text(value, size: int | None = None) -> str:
    s = "" if value is None else str(value)
    return s[:size] if size else s


def _alert_fields(rec: dict) -> dict:
    return {
        # device/recall.json carries no recall class (the enforcement feed does); the root cause stays in `raw`.
        "classification": "",
        "manufacturer": _text(rec.get("recalling_firm"), 160),
        "product": _text(rec.get("product_description"), 300),
        # FDA product codes (e.g. "FRN") are not model names; matching falls back to the product description.
        "model_terms": [],
        "title": _text(rec.get("reason_for_recall") or rec.get("product_description"), 300),
        "action": _text(rec.get("action")),
        # When FDA made it public, which is when a CE department could have received it; the start date if missing.
        "published_on": parse_date(rec.get("event_date_posted")) or parse_date(rec.get("event_date_initiated")),
        "raw": rec,
    }


@transaction.atomic
def store(records: list[dict]) -> int:
    """Keep each record as an FDA Alert; returns how many are new. A record already stored as it is now is not written again:
    the 30-day window re-reads a month of notices each time, nearly all of them unchanged."""
    keyed = {}
    for rec in records:
        # One record per recalled product: key on the product's recall number (Z-1234-2026), not the event, which covers
        # several products; keyed by event, every product but the last would be lost, with its device description.
        ext = rec.get("product_res_number") or rec.get("cfres_id") or rec.get("res_event_number")
        if ext and len(str(ext)) <= Alert._meta.get_field("external_id").max_length:
            keyed[str(ext)] = _alert_fields(rec)
    stored = {a.external_id: a for a in Alert.objects.filter(source=Alert.Source.FDA, external_id__in=list(keyed))}
    new = 0
    for ext, fields in keyed.items():
        current = stored.get(ext)
        if current is not None and all(getattr(current, name) == value for name, value in fields.items()):
            continue
        _, created = Alert.objects.update_or_create(source=Alert.Source.FDA, external_id=ext, defaults=fields)
        new += int(created)
    return new


def import_recalls(days: int = CHECK_DAYS, limit: int = MAX_LIMIT, manufacturer: str | None = None, timeout=IMPORT_TIMEOUT,
                   today: date | None = None) -> FeedResult:
    """Fetch the recalls posted in the last `days` and store them. Raises FeedError when openFDA fails; nothing is stored then."""
    answer = fetch(days=days, limit=limit, manufacturer=manufacturer, timeout=timeout, today=today)
    return FeedResult(store(answer.records), answer.returned, answer.total)


# --- the Recalls screen's check ------------------------------------------------------------------------

def last_fetch() -> datetime | None:
    """When the feed was last fetched (a fetch started), by a check or by the daily import."""
    return JobRun.objects.filter(job__in=(CHECK_JOB, DAILY_JOB)).aggregate(at=Max("started_at"))["at"]


def last_checked() -> datetime | None:
    """When the feed last answered: the latest check or daily import that finished well (the Recalls page head). A check's own
    row says how its latest attempt went, so a failed or running attempt would hide the day's earlier success: the successes
    are kept on a row of their own (CHECK_OK_JOB)."""
    return JobRun.objects.filter(job__in=(CHECK_OK_JOB, DAILY_JOB), status=JobRun.Status.SUCCEEDED).aggregate(at=Max("finished_at"))["at"]


def last_fetch_failed() -> bool:
    """Whether the latest fetch (a check or the daily import) failed."""
    row = JobRun.objects.filter(job__in=(CHECK_JOB, DAILY_JOB)).order_by("-started_at").values("status").first()
    return bool(row) and row["status"] == JobRun.Status.FAILED


def _row_day(now: datetime) -> date:
    """The day a check's row is for: the server's (TIME_ZONE), not the checking facility's. The rows are everyone's (one a day, no
    facility), and checks from facilities in different time zones at the same moment must land on the same row (slice 21)."""
    return timezone.localdate(now, timezone=timezone.get_default_timezone())


def _succeeded(now: datetime) -> None:
    done = timezone.now()
    JobRun.objects.update_or_create(job=CHECK_OK_JOB, run_on=_row_day(now),
                                    defaults={"started_at": now, "finished_at": done, "status": JobRun.Status.SUCCEEDED})


class _Claim(NamedTuple):
    run_id: int | None  # the day's check row, when this check may fetch
    last: datetime | None  # held back: the last fetch


def _claim(now: datetime) -> _Claim:
    """The right to fetch now, or the last fetch when the cooldown holds this check back. Of two checks at once exactly one
    fetches: the day's row is inserted under the unique (job, run_on) constraint, or moved forward by an UPDATE that only
    matches the row as it was read. Each statement commits on its own, so other processes see the claim before the fetch."""
    last = last_fetch()
    if last is not None and now - last < CHECK_COOLDOWN:
        return _Claim(None, last)
    day = _row_day(now)
    row = JobRun.objects.filter(job=CHECK_JOB, run_on=day).first()
    if row is None:
        try:
            with transaction.atomic():
                return _Claim(JobRun.objects.create(job=CHECK_JOB, run_on=day, started_at=now).pk, None)
        except IntegrityError:  # another check inserted the day's row a moment ago: it fetches
            return _Claim(None, last_fetch() or now)
    taken = (JobRun.objects.filter(pk=row.pk, started_at=row.started_at)
             .update(started_at=now, status=JobRun.Status.RUNNING, finished_at=None))
    return _Claim(row.pk, None) if taken else _Claim(None, last_fetch() or now)


def _record(run_id: int, status: str, line: str) -> None:
    run = JobRun.objects.get(pk=run_id)
    run.status, run.finished_at = status, timezone.now()
    run.output = (run.output + line + "\n")[-OUTPUT_LIMIT:]
    run.save(update_fields=["status", "finished_at", "output"])


def _match_here() -> int:
    with transaction.atomic():
        return services.match_all_open_alerts()


def clock(at: datetime) -> str:
    """"10:42 AM", local time (the facility's, inside one)."""
    return dateformat.format(timezone.localtime(at), "g:i A")


def check_feed(now: datetime | None = None) -> Check:
    """The Recalls screen's check, inside the current facility: fetch the last CHECK_DAYS days (unless the cooldown holds the
    fetch back), store them, and match them to this facility only. Call it outside a transaction (see `_claim`).
    Raises FeedError when openFDA fails; that is recorded on the day's row and nothing is matched."""
    now = now or timezone.now()
    claim = _claim(now)
    if claim.run_id is None:
        return Check(None, _match_here(), claim.last, claim.last + CHECK_COOLDOWN, last_fetch_failed())
    stamp = f"{clock(now)} {get_current_tenant().slug}"
    try:
        result = import_recalls(days=CHECK_DAYS, limit=MAX_LIMIT, timeout=CHECK_TIMEOUT)
        matches = _match_here()
    except Exception as e:
        _record(claim.run_id, JobRun.Status.FAILED, f"{stamp}: {e}" if isinstance(e, FeedError) else f"{stamp}: failed: {e!r}")
        raise
    _record(claim.run_id, JobRun.Status.SUCCEEDED,
            f"{stamp}: {result.new} new alerts, {result.returned} records returned (openFDA total {result.total}); {matches} new matches")
    _succeeded(now)
    return Check(result, matches, now, None)


def _plural(n: int, word: str, plural: str = "") -> str:
    return f"{n} {word if n == 1 else plural or word + 's'}"


def check_message(check: Check) -> str:
    """What a check found, in the words the Recalls screen's toast and the API's check-feed answer use (the mock's "Checked ECRI and
    FDA feeds: no new alerts", for the one feed that is connected)."""
    matches = f"{_plural(check.matches, 'new match', 'new matches')} for your inventory"
    if check.result is None:
        tried = (f"The FDA recall feed did not answer at {clock(check.checked_at)}" if check.last_failed
                 else f"The FDA recall feed was checked at {clock(check.checked_at)}")
        held = f"{tried}; it can be checked again at {clock(check.again_at)}."
        return f"{held} {matches[0].upper()}{matches[1:]}." if check.matches else f"{held} No new matches."
    r = check.result
    if r.new:
        message = f"Checked the FDA recall feed: {_plural(r.new, 'new notice')}, " + (matches if check.matches else "none match your inventory")
    elif check.matches:
        message = f"Checked the FDA recall feed: no new notices, {matches}"
    else:
        message = "Checked the FDA recall feed: no new alerts"
    if r.truncated:
        message += f" (openFDA sent {r.returned:,} of {r.total:,} recalls)"
    return message
