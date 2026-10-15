"""
The survey binder's "Contracted service and vendor staff" section (slice 31): the facility's contracted services, and the evidence it
keeps that the vendor staff who serviced its equipment were trained for it (apps.vendors). A surveyor asks per vendor how the facility
assures a contractor uses qualified people, and for the list of contracted services with their scope; then samples the work.

Tables:
- per vendor (merged by apps.vendors.rules.vendor_key: "Philips" and "Philips field service" are one vendor): its contracts in force on
  any day of the period (reference, OEM or third-party, coverage, term, devices covered today), its attestations in force on any day of
  the period, its engineers with a training in force at the period's end, and the period's vendor jobs by evidence;
- engineers and their training: each engineer named on a vendor job in the period or with a training in force on any day of it, and
  those trainings (coverage, source, certificate, issued, expires, the day entered in Cadence, in force or expired at the period's end);
- attestations in force on any day of the period;
- the period's vendor jobs: work orders on vendor service of any type completed (or closed) in the period, with the engineer named and
  the evidence (apps.vendors.rules.evidence_for, judged on the completed day: the named engineer's training naming the device's model,
  else an attestation from the work order's vendor or the engineer's company naming it, else nothing on file).

Every device counts, ours or temporary: work counts every device (equipment.services.OWNED is the fleet's rule, not the work's).

Gaps (never a person: a vendor's name, or a work order's number):
- GAP one per vendor whose jobs in the period no training or attestation on file covers; its link opens the Vendor staff tab filtered to
  that vendor (web:vendor_staff?vendor=<name>), where adding the vendor's attestation or the engineer's training fixes it. A record
  covers the days its own dates say whenever it was entered, so a GAP is never a FINDING: the record can still be put on file.
- CHECK one per vendor for its jobs on imaging, radiologic, or laser equipment kept on the manufacturer's schedule
  (DeviceModel.oem_schedule_required) that name no engineer: the vendor's report names who did the work. One job links to it.
Jobs imported from the previous system (Source.IMPORTED) are counted and listed apart, never judged.

Slice 34, the review of each contracted service vendor (apps.evaluations.models.VendorReview: Contracts, Reviews), as of today:
- The vendors table ends with the vendor's last review: its number (linked for Evaluations View: the VENDOR_REVIEW link column) and the
  day it was signed off. The last review is the latest signed-off review naming the vendor (any of its names, matched by key) by the
  12 months it covers, as the Reviews tab reads it. Never its ratings, its decision, or its figures: the section's readers (Work orders,
  Contracts, and Equipment View) need not hold Evaluations View, and `binder_gaps` builds the section for nobody in particular.
- Two figures: the vendors with a contract in force at the period's end, and how many of them have a signed-off review.
- CHECKs, never a GAP (the interval is the facility's own, and Cadence never says which standard asks for a review), judged on the
  binder's today: one per vendor with a contract in force at the period's end whose next review is overdue (none signed off
  DUE_AFTER_DAYS after the 12 months after its last one ended, linking to the Reviews tab); once the facility has signed off any review,
  one naming the vendors with a contract in force at the period's end and no signed-off review (linking to the Reviews tab's vendors
  not reviewed); and one per signed-off review whose corrective action plan is past its due day with no result recorded (linking to
  the review: its plan's due day, never its rating). Vendors with jobs but no contract are never checked. The rules for when the next
  review is due are apps.evaluations.vendor_reviews' (next_period_end, DUE_AFTER_DAYS), copied here: this module reads the models only.

Queries: seven whatever the facility's size: the contracts (with their devices counted), the period's vendor jobs, the engineers, the
trainings, the attestations, the device models the records name (for their words), and the signed-off vendor reviews. The evidence is
judged in Python, one pass. Nothing a requester typed is read: the vendor's name and the report number are CE's, the engineer's name is
the vendor's report's; a review's summary is never read.
"""
from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import NamedTuple
from urllib.parse import urlencode

from django.db.models import Count, Q
from django.urls import reverse

from apps.contracts.models import Contract, ContractType
from apps.contracts.models import Coverage as ContractCoverage
from apps.equipment.models import AssetStatus, DeviceModel
from apps.evaluations.models import Decision, ReviewStatus, VendorReview
from apps.pm.dates import add_months
from apps.vendors import rules
from apps.vendors.models import EngineerTraining, TrainingSource, VendorAttestation, VendorEngineer
from apps.vendors.rules import ATTESTATION, NONE, TRAINING
from apps.workorders.models import Source, WorkOrder, WoStatus, WoType

from . import CHECK, DEVICE, GAP, VENDOR_REVIEW, WORK_ORDER, Figure, Gap, Period, Section, Table

KEY, TITLE = "vendors", "Contracted service and vendor staff"
TOPIC = ("The evidence the facility keeps that the vendor staff who serviced its equipment were trained for it, and the contracted "
         "services.")
DONE = (WoStatus.COMPLETED, WoStatus.CLOSED)
IMPORTED = "imported"  # a job's tally next to rules' TRAINING, ATTESTATION, NONE
NOT_JUDGED = "Imported from the previous system: not judged"
NOT_NAMED = "Not named"
NO_VENDOR = "Vendor not named"
NO_TRAINING = "No training on file in the period"
IN_FORCE, EXPIRED, ENDED = "In force", "Expired", "Ended"
OEM_SCHEDULE_WORDS = "imaging, radiologic, or laser equipment kept on the manufacturer's schedule"

VENDOR_COLUMNS = ["Vendor", "Also written", "Contracts in the period", "Attestations in the period", "Engineers with training on file",
                  "Vendor jobs", "Training on file", "Attestation on file", "Nothing on file", "Imported", "Last review", "Last review signed off"]
LAST_REVIEW = VENDOR_COLUMNS.index("Last review")  # slice 34: its number, linked for Evaluations View (VENDOR_REVIEW)
ENGINEER_COLUMNS = ["Vendor", "Engineer", "Active today", "Covers", "Source", "Certificate", "Issued", "Expires", "Entered in Cadence",
                    "At the period's end"]
ATTESTATION_COLUMNS = ["Vendor names", "Reference", "Covers", "Received", "Valid from", "Valid through", "Entered in Cadence",
                       "At the period's end"]
JOB_COLUMNS = ["Work order", "Type", "Completed on", "Device", "Model", "Vendor", "Engineer", "Vendor report number", "Evidence"]

# Slice 34: when a vendor's next review is due (apps.evaluations.vendor_reviews' DUE_AFTER_DAYS and next_period_end, copied: a test
# keeps the two the same), the Reviews tab's filter for vendors not reviewed, and how many names a check lists.
DUE_AFTER_DAYS = 90
NOT_REVIEWED_QUERY = "show=not_reviewed"
NAMES_SHOWN = 5
REVIEWS_RECORD = "Vendor reviews"  # what a check's link opens, as shown: the Contracts screen's Reviews tab
_REVIEW_FIELDS = ("number", "vendor_names", "vendor_keys", "period_start", "period_end", "signed_on", "decision", "plan_due_on", "plan_result")

_TYPE_LABELS = dict(WoType.choices)
_CONTRACT_TYPES = dict(ContractType.choices)
_CONTRACT_COVERAGE = dict(ContractCoverage.choices)
_SOURCES = dict(TrainingSource.choices)
# The job columns read (CE's or chosen, never the problem, the resolution, the requester, or notes).
_JOB_FIELDS = ("number", "type", "completed_on", "source", "vendor_name", "vendor_engineer_id", "checks_reference", "asset__tag",
               "asset__device_model_id", "asset__device_model__manufacturer", "asset__device_model__model",
               "asset__device_model__category", "asset__device_model__oem_schedule_required")


class _Model(NamedTuple):
    """A device model as rules.names_model reads it (id, manufacturer, category), with its name for the table."""
    id: object
    manufacturer: str
    model: str
    category: str

    def __str__(self):
        return f"{self.manufacturer} {self.model}"


@dataclass
class _Job:
    number: str
    type: str
    completed_on: object
    tag: str
    model: _Model
    vendor: str
    key: str
    engineer: VendorEngineer | None
    reference: str
    imported: bool
    oem_schedule: bool
    evidence: rules.Evidence | None = None  # None: imported, never judged


@dataclass
class _Vendor:
    key: str
    contract_names: list = field(default_factory=list)
    job_names: Counter = field(default_factory=Counter)
    other_names: list = field(default_factory=list)  # attestations' and engineers' spellings
    contracts: list = field(default_factory=list)
    attestations: list = field(default_factory=list)
    trained: set = field(default_factory=set)  # engineers with a training in force at the period's end
    tally: Counter = field(default_factory=Counter)  # TRAINING, ATTESTATION, NONE, IMPORTED
    unnamed_oem: list = field(default_factory=list)  # job numbers on manufacturer-schedule equipment naming no engineer

    @property
    def name(self) -> str:
        """The name shown: a contract's (the contracted-services list's), else the spelling on most of the period's jobs, else an
        attestation's or an engineer's company."""
        if self.contract_names:
            return self.contract_names[0]
        if self.job_names:
            return sorted(self.job_names.items(), key=lambda kv: (-kv[1], kv[0].casefold(), kv[0]))[0][0]
        if self.other_names:
            return self.other_names[0]
        return NO_VENDOR

    @property
    def also(self) -> str:
        shown = self.name
        seen = {n for n in (*self.contract_names, *self.job_names, *self.other_names) if n != shown}
        return ", ".join(sorted(seen, key=lambda n: (n.casefold(), n)))


def _n(n: int, what: str) -> str:
    return f"{n} {what}{'' if n == 1 else 's'}"


def _vendor_url(vendor: _Vendor) -> str:
    """The Vendor staff tab filtered to the vendor (it matches the name by key, so any spelling finds it)."""
    url = reverse("web:vendor_staff")
    return f"{url}?{urlencode({'vendor': vendor.name})}" if vendor.key else url


def _overlaps(start, end, period: Period) -> bool:
    """Whether a record running start..end (end None: no end) is in force on any day of the period."""
    return start <= period.end and (end is None or end >= period.start)


def _contracts(period: Period) -> list:
    """The contracts in force on any day of the period, with their devices covered today (not retired, as the Contracts table counts
    them). One query."""
    return list(Contract.objects.filter(start_on__lte=period.end, end_on__gte=period.start)
                .annotate(devices=Count("assets", filter=~Q(assets__status=AssetStatus.RETIRED))).order_by("reference"))


def _jobs(period: Period, engineers: dict) -> list[_Job]:
    """The period's vendor jobs, in the order completed. One query."""
    done = (WorkOrder.objects.filter(vendor_service=True, status__in=DONE, completed_on__gte=period.start, completed_on__lte=period.end)
            .order_by("completed_on", "number").values(*_JOB_FIELDS))
    jobs = []
    for w in done:
        model = _Model(w["asset__device_model_id"], w["asset__device_model__manufacturer"], w["asset__device_model__model"],
                       w["asset__device_model__category"])
        vendor = w["vendor_name"] or ""
        jobs.append(_Job(number=w["number"], type=_TYPE_LABELS.get(w["type"], w["type"]), completed_on=w["completed_on"], tag=w["asset__tag"],
                         model=model, vendor=vendor, key=rules.vendor_key(vendor), engineer=engineers.get(w["vendor_engineer_id"]),
                         reference=w["checks_reference"], imported=w["source"] == Source.IMPORTED,
                         oem_schedule=bool(w["asset__device_model__oem_schedule_required"])))
    return jobs


def _judge(jobs: list[_Job], trainings_of: dict, attestations: list) -> None:
    """Each job's evidence (rules.evidence_for, on its completed day); imported jobs are never judged."""
    for job in jobs:
        if job.imported:
            continue
        job.evidence = rules.evidence_for(day=job.completed_on, device_model=job.model, vendor_name=job.vendor, engineer=job.engineer,
                                          trainings=trainings_of.get(job.engineer.pk, ()) if job.engineer is not None else (),
                                          attestations=attestations)


def _models_named(records) -> dict:
    """{str(id): device model} for the models the records name (rules.coverage_words), one query."""
    ids = {str(i) for r in records for i in (r.model_ids or ())}
    if not ids:
        return {}
    valid = [i for i in ids if _is_uuid(i)]
    return {str(m.pk): m for m in DeviceModel.objects.filter(pk__in=valid).only("id", "manufacturer", "model")}


def _is_uuid(value) -> bool:
    try:
        uuid.UUID(str(value))
    except ValueError:
        return False
    return True


def _engineer_words(engineer: VendorEngineer | None) -> str:
    return f"{engineer.name} ({engineer.company})" if engineer is not None else NOT_NAMED


def _job_evidence_words(job: _Job, models_by_id: dict) -> str:
    return NOT_JUDGED if job.evidence is None else rules.evidence_words(job.evidence, models_by_id)


def _vendor_gaps(vendors: list[_Vendor]) -> list[Gap]:
    """A GAP per vendor with jobs no evidence covers, then a CHECK per vendor with manufacturer-schedule jobs naming no engineer."""
    gaps = []
    for v in vendors:
        n = v.tally[NONE]
        if n:
            gaps.append(Gap(GAP, f"{v.name}: {_n(n, 'job')} in the period with no training or attestation on file covering "
                                 f"{'it' if n == 1 else 'them'}", _vendor_url(v), v.name))
    for v in vendors:
        numbers = v.unnamed_oem
        if not numbers:
            continue
        n = len(numbers)
        if n == 1:
            gaps.append(Gap(CHECK, f"{v.name}: {numbers[0]}, a job on {OEM_SCHEDULE_WORDS}, names no engineer: record who did the work "
                                   "as the vendor's report names them.", reverse("web:wo", args=[numbers[0]]), numbers[0]))
        else:
            gaps.append(Gap(CHECK, f"{v.name}: {n} jobs on {OEM_SCHEDULE_WORDS} name no engineer: record who did the work as the vendor's "
                                   "reports name them (the vendor jobs table lists them).", record=v.name))
    return gaps


# --- slice 34: the vendor reviews -------------------------------------------------------------------------------------------------


def signed_off_reviews() -> list:
    """The facility's signed-off vendor reviews, the latest 12 months first (one query; never the summary, the ratings, or the figures)."""
    return list(VendorReview.objects.filter(status=ReviewStatus.APPROVED).order_by("-period_end", "-number").only(*_REVIEW_FIELDS))


def last_reviews(reviews: list) -> dict:
    """{vendor key: its last signed-off review}: the first of `reviews` (latest first) naming the key."""
    last: dict = {}
    for r in reviews:
        for key in r.vendor_keys or ():
            last.setdefault(key, r)
    return last


def next_period_end(end: date) -> date:
    """The last day of the 12 months after a review's period ending on `end` (apps.evaluations.vendor_reviews.next_period_end)."""
    return add_months(end + timedelta(days=1), 12) - timedelta(days=1)


def review_due(review) -> date:
    """The day the vendor's next review is overdue after: DUE_AFTER_DAYS after the 12 months after this one's period."""
    return next_period_end(review.period_end) + timedelta(days=DUE_AFTER_DAYS)


def _reviews_url(query: str = "") -> str:
    url = reverse("web:vendor_reviews")
    return f"{url}?{query}" if query else url


def _names(names: list[str], most: int | None = None) -> str:
    """"Hamilton Medical, Steris, and TechCare": the names (the first `most`, then how many more)."""
    shown = list(names[:most] if most else names)
    if len(names) > len(shown):
        shown.append(f"{len(names) - len(shown)} more")
    return shown[0] if len(shown) == 1 else f"{', '.join(shown[:-1])}{',' if len(shown) > 2 else ''} and {shown[-1]}"


def _review_gaps(vendors: list[_Vendor], under_contract: set, last: dict, reviews: list, period: Period) -> list[Gap]:
    """The review CHECKs (never a GAP), judged on the binder's today: a vendor under contract at the period's end whose next review is
    overdue (one per review: a review naming two of the rows' names is one check); once any review is signed off, the vendors under
    contract at the period's end with none; a corrective action plan past its due day with no result recorded."""
    gaps = []
    contracted = [v for v in vendors if v.key in under_contract]
    overdue: dict = {}  # review id: (the review, the names of the rows it is the last review of), in the rows' order
    for v in contracted:
        r = last.get(v.key)
        if r is not None and review_due(r) < period.today:
            overdue.setdefault(r.pk, (r, []))[1].append(v.name)
    for r, names in overdue.values():
        gaps.append(Gap(CHECK, f"{_names(names)}: no signed-off review of the 12 months after {rules.fmt(r.period_start)} to "
                               f"{rules.fmt(r.period_end)} ({r.number}, the last), {DUE_AFTER_DAYS} days after they ended on "
                               f"{rules.fmt(next_period_end(r.period_end))}. Review the vendor in Contracts, Reviews, or be ready to explain.",
                        _reviews_url(), REVIEWS_RECORD))
    missing = [v.name for v in contracted if v.key not in last]
    if reviews and missing:
        n = len(missing)
        gaps.append(Gap(CHECK, f"{_n(n, 'vendor')} with a contract in force on {rules.fmt(period.end)} {'has' if n == 1 else 'have'} no "
                               f"signed-off review: {_names(missing, NAMES_SHOWN)}. Review {'it' if n == 1 else 'each'} in Contracts, Reviews, or "
                               "be ready to explain.", _reviews_url(NOT_REVIEWED_QUERY), REVIEWS_RECORD))
    for r in sorted(reviews, key=lambda r: (r.plan_due_on or date.max, r.number)):
        if r.decision == Decision.CORRECTIVE_PLAN and not r.plan_result and r.plan_due_on is not None and r.plan_due_on < period.today:
            gaps.append(Gap(CHECK, f"{r.vendor}: the corrective action plan of {r.number} was due on {rules.fmt(r.plan_due_on)} and no result "
                                   "is recorded. Record its result on the review, or be ready to explain.",
                            reverse("web:vendor_review", args=[r.number]), r.number))
    return gaps


def build(period: Period, user) -> Section:
    contracts = _contracts(period)
    engineers = {e.pk: e for e in VendorEngineer.objects.all()}
    jobs = _jobs(period, engineers)
    trainings = list(EngineerTraining.objects.all())
    attestations = list(VendorAttestation.objects.order_by("-valid_through", "reference"))
    trainings_of = defaultdict(list)
    for t in trainings:
        trainings_of[t.engineer_id].append(t)
    _judge(jobs, trainings_of, attestations)

    listed_trainings = [t for t in trainings if _overlaps(t.issued_on, t.expires_on, period)]
    listed_attestations = [a for a in attestations if _overlaps(a.valid_from, a.valid_through, period)]
    evidence_records = [j.evidence.record for j in jobs if j.evidence is not None and j.evidence.record is not None]
    models_by_id = _models_named([*listed_trainings, *listed_attestations, *evidence_records])

    # --- per vendor, merged by key
    vendors: dict[str, _Vendor] = {}

    def vendor(key: str) -> _Vendor:
        if key not in vendors:
            vendors[key] = _Vendor(key)
        return vendors[key]

    for c in contracts:
        v = vendor(rules.vendor_key(c.vendor))
        v.contract_names.append(c.vendor)
        v.contracts.append(f"{c.reference}: {_CONTRACT_TYPES.get(c.type, c.type)}, {_CONTRACT_COVERAGE.get(c.coverage, c.coverage)}, "
                           f"{rules.fmt(c.start_on)} to {rules.fmt(c.end_on)}, {_n(c.devices, 'device')} covered")
    for job in jobs:
        v = vendor(job.key)
        if job.vendor:
            v.job_names[job.vendor] += 1
        if job.imported:
            v.tally[IMPORTED] += 1
            continue
        v.tally[job.evidence.kind] += 1
        if job.oem_schedule and job.engineer is None:
            v.unnamed_oem.append(job.number)
    for a in listed_attestations:
        for name, key in zip(a.vendor_names or (), a.vendor_keys or ()):
            v = vendor(key)
            v.other_names.append(name)
            v.attestations.append(f"{a.reference}, {rules.fmt(a.valid_from)} to {rules.fmt(a.valid_through)}")
    for t in listed_trainings:
        engineer = engineers.get(t.engineer_id)
        if engineer is None:
            continue
        v = vendor(engineer.company_key)
        v.other_names.append(engineer.company)
        if rules.training_in_force(t, period.end):
            v.trained.add(engineer.pk)
    ordered = sorted(vendors.values(), key=lambda v: (v.name.casefold(), v.name))

    # --- slice 34: each vendor's last review, and who is under contract at the period's end
    reviews = signed_off_reviews()
    last = last_reviews(reviews)
    under_contract = {rules.vendor_key(c.vendor) for c in contracts if c.start_on <= period.end <= c.end_on} - {""}

    def last_review(v: _Vendor) -> list:
        r = last.get(v.key) if v.key else None
        return [r.number, r.signed_on] if r is not None else ["", None]

    vendor_rows = [[v.name, v.also, "; ".join(v.contracts), "; ".join(v.attestations), len(v.trained),
                    v.tally[TRAINING] + v.tally[ATTESTATION] + v.tally[NONE], v.tally[TRAINING], v.tally[ATTESTATION], v.tally[NONE],
                    v.tally[IMPORTED], *last_review(v)] for v in ordered]

    # --- engineers and their training, as of the period's end
    listed_by_engineer = defaultdict(list)
    for t in listed_trainings:
        listed_by_engineer[t.engineer_id].append(t)
    shown = set(listed_by_engineer) | {j.engineer.pk for j in jobs if j.engineer is not None}
    engineer_rows = []
    for e in sorted((engineers[pk] for pk in shown if pk in engineers), key=lambda e: (e.company.casefold(), e.name.casefold(), e.name)):
        records = sorted(listed_by_engineer.get(e.pk, ()), key=lambda t: (t.issued_on, t.created_at), reverse=True)
        if not records:
            engineer_rows.append([e.company, e.name, e.is_active, NO_TRAINING, "", "", None, None, None, ""])
        for t in records:
            engineer_rows.append([e.company, e.name, e.is_active, rules.coverage_words(t, models_by_id), _SOURCES.get(t.source, t.source),
                                  t.certificate, t.issued_on, t.expires_on, rules.entered_on(t),
                                  IN_FORCE if rules.training_in_force(t, period.end) else EXPIRED])

    attestation_rows = [[", ".join(a.vendor_names or ()), a.reference, rules.coverage_words(a, models_by_id), a.received_on, a.valid_from,
                         a.valid_through, rules.entered_on(a), IN_FORCE if rules.attestation_in_force(a, period.end) else ENDED]
                        for a in listed_attestations]

    job_rows = [[j.number, j.type, j.completed_on, j.tag, str(j.model), j.vendor or NO_VENDOR, _engineer_words(j.engineer), j.reference,
                 _job_evidence_words(j, models_by_id)] for j in jobs]

    judged = [j for j in jobs if j.evidence is not None]
    tally = Counter(j.evidence.kind for j in judged)
    imported = len(jobs) - len(judged)
    figures = [
        Figure("Vendors", len(ordered), "with a contract, an attestation, or training in force in the period, or work completed in it"),
        Figure("Contracts in the period", len(contracts), "in force on any day of the period"),
        Figure("Vendor jobs completed", len(judged), "completed or closed in the period, judged on the day completed"),
        Figure("Training on file", tally[TRAINING], "the named engineer's training naming the device's model"),
        Figure("Attestation on file", tally[ATTESTATION], "the vendor's, or the engineer's company's, naming the device's model"),
        Figure("Nothing on file", tally[NONE], "no training or attestation on file covering the job"),
        Figure("Evidence entered after the work", sum(1 for j in judged if j.evidence.entered_after),
               "a record dated before the work, entered in Cadence after it"),
        Figure("No engineer named", sum(1 for j in judged if j.engineer is None), "the job's record names no engineer"),
        Figure("Imported from the previous system", imported, "counted apart, never judged"),
        Figure("Vendors under contract", len(under_contract), "with a contract in force at the period's end"),
        Figure("Vendors under contract with a signed-off review", sum(1 for key in under_contract if key in last),
               "a review of the vendor signed off in Contracts, Reviews, as of today"),
    ]
    tables = [
        Table("vendors", "Vendors and their contracted services", VENDOR_COLUMNS, lambda: iter(vendor_rows), count=len(vendor_rows),
              links={LAST_REVIEW: VENDOR_REVIEW}, empty="No contracted service and no vendor work in this period."),
        Table("engineers", "Engineers and their training", ENGINEER_COLUMNS, lambda: iter(engineer_rows), count=len(engineer_rows),
              empty="No engineer with training on file in this period, and none named on a vendor job."),
        Table("attestations", "Vendors' attestations", ATTESTATION_COLUMNS, lambda: iter(attestation_rows), count=len(attestation_rows),
              empty="No attestation in force in this period."),
        Table("jobs", "Vendor jobs completed in the period", JOB_COLUMNS, lambda: iter(job_rows), count=len(job_rows),
              links={0: WORK_ORDER, 3: DEVICE}, empty="No vendor job was completed in this period."),
    ]
    notes = [
        "A vendor job is a work order on vendor service, of any type, completed or closed in the period. Each is judged on the day it was "
        "completed (the day it was recorded done, which may be after the vendor's visit): the training on file for the engineer it "
        "names, naming the device's model and in force that day; else an attestation from its vendor, or from the named engineer's "
        "company, naming the model and in force that day; else nothing on file.",
        "Each record covers the days its own dates say, a training from its issue date to its expiry and an attestation from the first to "
        "the last day it states, whenever it was entered in Cadence: one entered after the work still counts, and the evidence says "
        "\"on file since\" the day it was entered. A renewal is a new record; a record entered in error is removed and covers no day.",
        "Cadence keeps the documents and never judges competence: \"training on file\" and \"attestation on file\" are what the facility "
        "holds, not an assessment of anyone's skill, and whether a training's source meets a contract's terms is the facility's call.",
        "A training or an attestation names device models, or one manufacturer's product category; an attestation may cover every device "
        "the vendor services for the facility. Never a whole manufacturer.",
        "Vendors are matched by name in any letter case and spacing, a trailing \"field service\" dropped (\"Philips field service\" is "
        "Philips); a vendor written another way (\"Philips Healthcare\") is its own row, and an attestation covers its jobs only when it "
        "names that spelling too.",
        f"On {OEM_SCHEDULE_WORDS}, a job naming no engineer is a check: the vendor's report names who did the work.",
        f"Jobs imported from the previous system ({imported} in the period) are counted and listed apart, never judged: Cadence's records "
        "of the vendor's staff start after them.",
        "Contracts are those in force on any day of the period, their devices counted today. Trainings and attestations are those in force "
        "on any day of the period, each with its standing at the period's end; an engineer's training on file is counted at the period's "
        "end.",
        "Vendor staff's badges, immunizations, and background checks belong to the facility's vendor credentialing system.",
        "Last review: the latest review of the vendor signed off in Cadence (Contracts, Reviews), by the 12 months it covers, as of today: "
        "its number and the day it was signed off. Its ratings, decision, and figures are on the review itself. A review names the vendor "
        "as its contracts and work orders do; a vendor written another way has a review of its own.",
        f"Once a review is signed off, checks name a vendor with a contract in force at the period's end whose next review is not signed off "
        f"{DUE_AFTER_DAYS} days after the 12 months after its last review, the vendors with a contract in force at the period's end and "
        "no signed-off review, and a corrective action plan past its due day with no result recorded. How often to review a vendor is the "
        "facility's own rule; vendors with jobs but no contract are never checked.",
    ]
    return Section(key=KEY, title=TITLE, topic=TOPIC,
                   covers=f"Contracts, records, and vendor jobs {period.label}; devices covered and vendor reviews {period.as_of_today}",
                   figures=figures, gaps=[*_vendor_gaps(ordered), *_review_gaps(ordered, under_contract, last, reviews, period)], tables=tables,
                   notes=notes)
