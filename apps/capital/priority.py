"""Replacement priority (slice 36): the one rule the Replacement planning report (its screen, CSV, print, email, and API rows) and the
device drawer's outlook read. Beyond the mock, whose score (age 50%, repairs in six months 30%, condition 20%) slice 33 deferred.

No US rule requires replacement planning; AAMI EQ56:2013 (a recommended practice) says to plan replacements when parts become hard to
get. Published methods (Fennigkoh; Taylor and Jackson; Ridgway; Capuano; ECRI) weigh support, failures, the cost of service against the
device's value, safety, and age, and warn against age alone. The score is a recommendation for a committee, never a decision.

The score, 0 to 100, for our devices in use (equipment.services.OWNED in Asset.ACTIVE_STATUSES; never a temporary device):
- five components (COMPONENTS, WEIGHTS), each a value from 0 to 1 or unknown, with its input in words; an unknown one is never scored as
  0 and the others are never reweighted: its weight is shown as "points unknown" beside the score;
- times the criticality multiplier of the model's risk class (MULTIPLIERS), never added points;
- condition is never in the score (no day says when it was assessed); it is shown beside it.
Weights and thresholds are code constants printed with SCORE_VERSION wherever a score is shown.

Read in a fixed number of queries however many devices (score_assets; for_asset is the same for one). `today` and the fiscal year start
are passed in. Never incidents, never status words, never text a requester typed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from apps.equipment.models import RiskClass

SCORE_VERSION = 2

# --- the components ------------------------------------------------------------------------------------------------------------------

AGE, SUPPORT, RELIABILITY, COST, SAFETY = "age", "support", "reliability", "cost", "safety"
COMPONENTS = (AGE, SUPPORT, RELIABILITY, COST, SAFETY)  # the order every door shows
WEIGHTS = {AGE: 30, SUPPORT: 25, RELIABILITY: 20, COST: 15, SAFETY: 10}  # sums to 100
LABELS = {AGE: "Age against expected life", SUPPORT: "Manufacturer support", RELIABILITY: "Failures",
          COST: "Service cost a year against replacement cost", SAFETY: "Confirmed recall"}

# Criticality by the model's risk class: a multiplier on the total (never risk_incidents or risk_score)
MULTIPLIERS = {RiskClass.LIFE_SUPPORT: 1.0, RiskClass.HIGH: 0.95, RiskClass.MEDIUM: 0.9, RiskClass.LOW: 0.85}

AGE_CAP = 1.5  # age against life counts up to 1.5x the expected life (value 1.0 there)
FAILURES_FULL = 3  # failures a year in use that score 1.0 (0 at none, linear between)
COST_FLOOR, COST_FULL = Decimal("0.05"), Decimal("0.30")  # service cost a year against replacement: 0 at 5% or less, 1.0 at 30% or more
CLASS_ONE_VALUE, RECALL_VALUE = 1.0, 0.6  # a confirmed Class I recall, any other confirmed recall
WINDOW_MONTHS = 36  # failures and cost: the last 36 months, clipped (see the window below)
MIN_HISTORY_MONTHS = 12  # under this much history, failures and cost read "too little history" (unknown)
MIN_YEARS = 1.0  # rates per year in use never divide by less than a year

# --- flags (apart from the score) and gaps (the worklist) -----------------------------------------------------------------------------

PARTS_ENDED, NOT_RETURNED, SUPPORT_ENDED, RECALL = "parts_ended", "not_returned", "support_ended", "recall"
MUST_ACT = (PARTS_ENDED, NOT_RETURNED)
REVIEW = (SUPPORT_ENDED, RECALL)
FLAGS = (*MUST_ACT, *REVIEW)
FLAG_WORDS = {PARTS_ENDED: "Parts no longer available", NOT_RETURNED: "Kept out of use after a repair",
              SUPPORT_ENDED: "Manufacturer support ended", RECALL: "Confirmed recall"}

NO_INSTALL_DATE, NO_ESTIMATE, LIFE_SOURCE, SUPPORT_UNCHECKED = "no_install_date", "no_estimate", "life_source", "support_unchecked"
GAPS = (NO_INSTALL_DATE, NO_ESTIMATE, LIFE_SOURCE, SUPPORT_UNCHECKED)
GAP_WORDS = {NO_INSTALL_DATE: "No install date", NO_ESTIMATE: "No list price or acquisition cost",
             LIFE_SOURCE: "Where the expected life comes from is not recorded",
             SUPPORT_UNCHECKED: "Manufacturer support not checked in the last 12 months"}
# A device is ranked only when its age and its replacement estimate are known; else it is listed apart, "Not ranked: <why>"
UNRANKED_WORDS = {NO_INSTALL_DATE: "no install date", NO_ESTIMATE: "no replacement estimate"}

TOO_LITTLE_HISTORY = "too little history"
CONTRACT_SHARE_UNKNOWN = "its contract share cannot be worked out: no acquisition cost on file"  # review fix: never a silent $0
NOT_RECORDED_THEN = "not recorded then"  # an imported repair's finding
ESTIMATE_LIST, ESTIMATE_ACQUISITION = "list", "acquisition"


@dataclass(frozen=True)
class Component:
    """One component of a device's score: `value` 0 to 1, or None (unknown: its weight counts as points unknown); `input` what it was read
    from, in words ("12.0 of 10 yr", "Support ends Mar 31, 2028", "6 in 36 mo, 2.0 a year"); `note` beside it ("life source not
    recorded", "1 with no finding recorded", "recall under review")."""

    key: str
    value: float | None
    input: str
    note: str = ""

    @property
    def weight(self) -> int:
        return WEIGHTS[self.key]

    @property
    def label(self) -> str:
        return LABELS[self.key]

    @property
    def points(self) -> float | None:
        return None if self.value is None else self.weight * self.value


@dataclass(frozen=True)
class Estimate:
    """The replacement estimate: the model's list price when above 0 (`set_on` the day its list price was last changed, from history),
    else the device's acquisition cost (a historical price), else unknown. To the cent; no markup."""

    amount: Decimal | None
    source: str  # ESTIMATE_LIST, ESTIMATE_ACQUISITION, or ""
    words: str  # "List price, set Mar 4, 2026", "Acquisition cost, 2017: a historical price", "No estimate"
    set_on: date | None = None


@dataclass(frozen=True)
class Priority:
    """A device's replacement priority on a day."""

    asset: object  # equipment.models.Asset, with device_model and department
    components: tuple[Component, ...]
    multiplier: float
    score: int  # round(multiplier * the known components' points), 0 to 100
    points_unknown: int  # the unknown components' weights
    estimate: Estimate
    support: object  # equipment.services.SupportStatus
    flags: tuple[str, ...]  # FLAGS in order
    gaps: tuple[str, ...]  # GAPS in order
    suggested_fy: int | None  # named by the calendar year it ends; None: "not placed"
    age_years: float | None  # None when there is no install date or it is after today
    life_years: int
    life_end: date | None  # installed_on + the expected life
    condition: int | None  # as recorded (1 to 5), shown beside the score, never in it
    window_start: date  # the start of failures' and cost's window
    window_years: float  # its length in years (before the 1-year floor)
    failures: int | None  # in the window; None with too little history
    unrecorded: int  # of those, how many had no finding recorded
    service_cost_year: Decimal | None  # labor, parts, and the contract share a year; None with too little history
    extras: dict = field(default_factory=dict)  # anything else a door needs, named by the rule's body

    @property
    def points_known(self) -> int:
        return 100 - self.points_unknown

    @property
    def ranked(self) -> bool:
        return not self.unranked_reasons

    @property
    def unranked_reasons(self) -> tuple[str, ...]:
        return tuple(g for g in (NO_INSTALL_DATE, NO_ESTIMATE) if g in self.gaps)

    @property
    def must_act(self) -> bool:
        return any(f in MUST_ACT for f in self.flags)

    def component(self, key: str) -> Component:
        return next(c for c in self.components if c.key == key)


# --- the pure parts of the rule (the scaffold's; the body below reads them) -----------------------------------------------------------


def age_value(age_years: float, life_years: int) -> float:
    """Age against expected life, capped at AGE_CAP x life, scaled to 0..1."""
    life = max(life_years or 1, 1)
    return min(max(age_years, 0.0) / life, AGE_CAP) / AGE_CAP


def reliability_value(per_year: float) -> float:
    return min(max(per_year, 0.0) / FAILURES_FULL, 1.0)


def cost_value(ratio: Decimal) -> float:
    """Service cost a year against the replacement estimate: 0 at COST_FLOOR or less, 1.0 at COST_FULL or more, linear between."""
    if ratio <= COST_FLOOR:
        return 0.0
    if ratio >= COST_FULL:
        return 1.0
    return float((ratio - COST_FLOOR) / (COST_FULL - COST_FLOOR))


def contract_covers(coverage: str, wo_type: str, part: bool) -> bool:
    """Whether a live contract's annual cost already pays for a line of its vendor's work (review fix: by what the contract covers):
    full service and parts and labor cover the vendor's work; preventive maintenance only covers its PMs; parts only covers parts;
    time and materials covers nothing (each job is billed). A line it does not cover is counted on top of the device's share."""
    from apps.contracts.models import Coverage
    from apps.workorders.models import WoType

    if coverage in (Coverage.FULL, Coverage.PARTS_LABOR):
        return True
    if coverage == Coverage.PM_ONLY:
        return wo_type == WoType.PM
    if coverage == Coverage.PARTS:
        return part
    return False


def points_rounded(points: float | None) -> Decimal | None:
    """A component's points to one decimal, half up (the CSV's, the API's, the drawer's words and its table): None when unknown."""
    return None if points is None else Decimal(repr(points)).quantize(Decimal("0.1"), rounding="ROUND_HALF_UP")


def multiplier_for(risk_class: str) -> float:
    return MULTIPLIERS.get(risk_class, MULTIPLIERS[RiskClass.LOW])


def total(components, multiplier: float) -> tuple[int, int]:
    """(score, points unknown): the known components' points times the multiplier, rounded half up; the unknown ones' weights."""
    known = sum(c.points for c in components if c.value is not None)
    unknown = sum(c.weight for c in components if c.value is None)
    score = int(Decimal(str(known * multiplier)).quantize(Decimal("1"), rounding="ROUND_HALF_UP"))
    return score, unknown


def suggested_fy(*, must_act: bool, life_end: date | None, support_ends_on: date | None, parts_end_on: date | None, today: date,
                 start: int) -> int | None:
    """The fiscal year to plan for: the current one when a must-act flag is set; else the fiscal year of the earliest of the life's end,
    support's end, and parts' end (never before the current one); None ("not placed") when none is known. Words: "suggested for
    FY2027", never "due" or "scheduled"."""
    from apps.facility.fiscal import current_fy, fiscal_year_of

    now = current_fy(today, start)
    if must_act:
        return now
    days = [d for d in (life_end, support_ends_on, parts_end_on) if d is not None]
    if not days:
        return None
    return max(fiscal_year_of(min(days), start), now)


# --- the rule's body (wave 1) -------------------------------------------------------------------------------------------------------


def scored_assets():
    """The devices the rule scores: ours, in use."""
    from apps.equipment.models import Asset
    from apps.equipment.services import OWNED

    return Asset.objects.filter(OWNED, status__in=Asset.ACTIVE_STATUSES)


def score_assets(qs, today: date, start: int | None = None) -> list[Priority]:
    """Each device of `qs` (narrowed to scored_assets) scored on `today`, in a fixed number of queries; `start` the fiscal year's start
    month (read from Settings once when None). Unordered: rank() orders them.

    QUERIES of them whatever the fleet's size (a test pins it): the devices with their models, departments, and latest completed repair's
    checks; the facility's first work order day; the failures; labor; parts; the live contracts' covered acquisition cost; the recall
    matches; the devices' completed recall work; the models' list price history; and Settings when `start` is None. Every input is read
    for the whole fleet in grouped queries over the earliest window any device can have, then narrowed per device in Python. Each query
    filters on the devices as a subquery, so none is ever skipped (an empty IN list is), and the count never depends on the data."""
    if start is None:
        from apps.facility.fiscal import start_month

        start = start_month()
    devices = scored_assets().filter(pk__in=qs.values("pk"))
    assets = _devices(devices)
    inputs = _read(devices, today)
    return [_score(a, today, start, inputs) for a in assets]


def for_asset(asset, today: date, start: int | None = None) -> Priority | None:
    """One device's priority (the drawer's outlook): score_assets for that device; None when the rule does not score it (not ours, or
    not in use). A device the copy in hand shows is not scored costs no query."""
    from apps.equipment.models import Asset, Ownership

    if getattr(asset, "ownership", Ownership.OWNED) != Ownership.OWNED or asset.status not in Asset.ACTIVE_STATUSES:
        return None
    found = score_assets(Asset.objects.filter(pk=asset.pk), today, start)
    return found[0] if found else None


QUERIES = 9  # score_assets' queries with `start` given; one more (Settings) without it


# --- reading the inputs ----------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Inputs:
    first_day: date | None  # the facility's first work order (imported ones included), None before any
    failures: dict  # asset id: [(opened_on, recorded, imported, still open)], failures opened in the earliest window
    spent: dict  # asset id: [(completed_on, vendor_service, vendor_name, type, a part line, amount)], labor and parts of work completed
    # in the earliest window
    contracts: dict  # live contract id: (annual cost, the acquisition cost of the devices it covers, coverage, the vendor's key)
    matches: dict  # device model id: [(alert id, status, Class I)], confirmed or under review
    recalls_done: set  # (asset id, alert id): the device's own recall work order completed
    list_set_on: dict  # device model id: (the day its list price was last set, that price), from its history


def _done():
    from apps.workorders.models import WoStatus

    return (WoStatus.COMPLETED, WoStatus.CLOSED)


def _devices(devices) -> list:
    """Query 1: the devices with their models and departments, and the checks after their latest completed repair (repair_check)."""
    from django.db.models import OuterRef, Subquery

    from apps.workorders.models import WorkOrder, WoType
    from apps.workorders.repair_checks import last_completed_first

    # "The device's last repair" as the binder and loaner_device_back read it (review fix: never by created_at)
    latest = last_completed_first(WorkOrder.objects.filter(asset=OuterRef("pk"), type=WoType.REPAIR, status__in=_done(),
                                                           completed_on__isnull=False)).values("repair_check")[:1]
    return list(devices.select_related("device_model", "department").annotate(latest_repair_check=Subquery(latest)))


def _read(devices, today: date) -> _Inputs:
    """Queries 2 to 9: every other input, for the whole fleet at once, over the earliest window any device can have."""
    from django.db.models import BooleanField, Case, DecimalField, Min, Q, Sum, Value, When

    from apps.contracts.models import Contract
    from apps.core.days import local_day
    from apps.core.history import _rows
    from apps.equipment.models import AssetStatus, DeviceModel
    from apps.pm.dates import add_months
    from apps.recalls.models import AlertMatch
    from apps.reports.survey.recalls import is_class_one
    from apps.vendors.rules import vendor_key
    from apps.workorders import findings
    from apps.workorders.models import LABOR_AMOUNT, OPEN_STATUSES, PART_AMOUNT, LaborLine, PartLine, Source, WorkOrder, WoType

    ids = devices.values("pk")
    earliest = add_months(today, -WINDOW_MONTHS)
    done = _done()

    # 2. The facility's first work order in Cadence (imported history included): no window starts before it
    first_day = WorkOrder.objects.aggregate(d=Min("opened_on"))["d"]

    # 3. Failures (findings.failure_q; never a cancelled repair) by the day they were opened, with whether a finding is recorded (only a
    # completed repair has one: an open repair is being worked on again) and whether the repair was imported ("not recorded then")
    recorded = Case(When(findings.recorded_q() & Q(status__in=done), then=Value(True)), default=Value(False), output_field=BooleanField())
    failures: dict = {}
    for asset_id, opened_on, rec, source, status in (
            WorkOrder.objects.filter(findings.failure_q(), asset__in=ids, opened_on__gte=earliest, opened_on__lte=today,
                                     status__in=(*OPEN_STATUSES, *done))
            .annotate(recorded=recorded).order_by().values_list("asset_id", "opened_on", "recorded", "source", "status")):
        failures.setdefault(asset_id, []).append((opened_on, bool(rec), source == Source.IMPORTED, status in OPEN_STATUSES))

    # 4, 5. Labor and parts (each line to the cent) of work completed in the window, never a recall's (the manufacturer's correction)
    money = DecimalField(max_digits=14, decimal_places=2)
    cost_types = [t for t in WoType.values if t != WoType.RECALL]
    lines = {"work_order__asset__in": ids, "work_order__completed_on__gte": earliest, "work_order__completed_on__lte": today,
             "work_order__status__in": done, "work_order__type__in": cost_types}
    spent: dict = {}
    for model, amount, part in ((LaborLine, LABOR_AMOUNT, False), (PartLine, PART_AMOUNT, True)):
        for row in (model.objects.filter(**lines).order_by()
                    .values("work_order__asset", "work_order__completed_on", "work_order__vendor_service", "work_order__vendor_name",
                            "work_order__type").annotate(v=Sum(amount, output_field=money))):
            spent.setdefault(row["work_order__asset"], []).append(
                (row["work_order__completed_on"], row["work_order__vendor_service"], row["work_order__vendor_name"], row["work_order__type"],
                 part, Decimal(row["v"] or 0)))

    # 6. Live contracts today with the acquisition cost of the devices each covers (Contract.covered_assets: not retired), for each
    # device's share (Contract.cost_share_for's rule, grouped)
    covered = [s for s in AssetStatus.values if s != AssetStatus.RETIRED]
    contracts = {row["pk"]: (Decimal(row["annual_cost"] or 0), Decimal(row["total"] or 0), row["coverage"], vendor_key(row["vendor"]))
                 for row in Contract.objects.filter(pk__in=devices.values("contract"), start_on__lte=today, end_on__gte=today)
                 .annotate(total=Sum("assets__acquisition_cost", filter=Q(assets__status__in=covered))).order_by()
                 .values("pk", "annual_cost", "total", "coverage", "vendor")}

    # 7. Recall matches on the devices' models a reviewer confirmed (in progress: counted) or is reviewing (shown beside, no points)
    matches: dict = {}
    for dm_id, alert_id, status, classification in (
            AlertMatch.objects.filter(device_model__in=devices.values("device_model"),
                                      status__in=(AlertMatch.Status.IN_PROGRESS, AlertMatch.Status.UNDER_REVIEW))
            .order_by().values_list("device_model_id", "alert_id", "status", "alert__classification")):
        matches.setdefault(dm_id, []).append((alert_id, status, is_class_one(classification)))

    # 8. The devices' own recall work completed: that device is done for that alert
    recalls_done = set(WorkOrder.objects.filter(asset__in=ids, alert__isnull=False, status__in=done).order_by()
                       .values_list("asset_id", "alert_id"))

    # 9. The day each model's list price was last set, from its history (whose manager is not tenant-scoped: core.history._rows). A list
    # price changed outside the services, which history does not see, has no day.
    list_set_on: dict = {}
    last: dict = {}
    for dm_id, cost, when in (_rows(DeviceModel.history.model)
                              .filter(id__in=devices.filter(device_model__list_cost__gt=0).values("device_model"))
                              .order_by("id", "history_date", "history_id").values_list("id", "list_cost", "history_date")):
        if dm_id not in last or last[dm_id] != cost:
            list_set_on[dm_id] = local_day(when)
        last[dm_id] = cost
    list_set_on = {dm_id: (day, last[dm_id]) for dm_id, day in list_set_on.items()}
    return _Inputs(first_day, failures, spent, contracts, matches, recalls_done, list_set_on)


# --- one device ------------------------------------------------------------------------------------------------------------------------


def _money(d: Decimal) -> str:
    return f"${d:,.0f}"


def _day(d: date) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def window_of(asset, today: date, first_day: date | None) -> tuple[date, int, bool]:
    """(start, days, enough): failures' and cost's window, the last WINDOW_MONTHS clipped to the latest of that, the install date, and
    the facility's first work order (no work order yet: none); enough when it covers MIN_HISTORY_MONTHS."""
    from apps.pm.dates import add_months

    start = max(d for d in (add_months(today, -WINDOW_MONTHS), asset.installed_on, first_day or today) if d is not None)
    days = max((today - start).days, 0)
    return start, days, start <= add_months(today, -MIN_HISTORY_MONTHS)


def estimate_for(asset, list_set_on: date | None = None) -> Estimate:
    """The replacement estimate: the model's list price when above 0, else the device's acquisition cost, else unknown. No markup."""
    dm = asset.device_model
    if dm.list_cost and dm.list_cost > 0:
        return Estimate(Decimal(dm.list_cost), ESTIMATE_LIST, f"List price, set {_day(list_set_on)}" if list_set_on else "List price", list_set_on)
    if asset.acquisition_cost and asset.acquisition_cost > 0:
        year = asset.installed_on.year if asset.installed_on else "year unknown"
        return Estimate(Decimal(asset.acquisition_cost), ESTIMATE_ACQUISITION, f"Acquisition cost, {year}: a historical price")
    return Estimate(None, "", "No estimate")


def _score(a, today: date, start: int, inp: _Inputs) -> Priority:
    from apps.equipment.models import AssetStatus
    from apps.equipment.services import support_status
    from apps.pm.dates import add_months
    from apps.recalls.models import AlertMatch
    from apps.vendors.rules import vendor_key
    from apps.workorders.models import RepairCheck

    dm = a.device_model
    flags, gaps = set(), set()
    life = dm.expected_life_years or 1  # a 0 reads as 1: it never divides by zero

    # Age against expected life
    age = life_end = None
    if a.installed_on is None or a.installed_on > today:
        gaps.add(NO_INSTALL_DATE)
        age_c = Component(AGE, None, "no install date" if a.installed_on is None else "install date after today")
    else:
        age = (today - a.installed_on).days / 365.25
        life_end = add_months(a.installed_on, 12 * life)
        age_c = Component(AGE, age_value(age, life), f"{age:.1f} of {life} yr")
    if not dm.life_source:
        gaps.add(LIFE_SOURCE)
        age_c = Component(AGE, age_c.value, age_c.input, "life source not recorded")

    # Manufacturer support
    support = support_status(dm, today)
    if support.key == "unknown":
        gaps.add(SUPPORT_UNCHECKED)
    if support.key == "parts_ended":
        flags.add(PARTS_ENDED)
    if support.key == "support_ended" or (dm.support_ends_on is not None and dm.support_ends_on < today):  # its last day still supported
        flags.add(SUPPORT_ENDED)
    support_c = Component(SUPPORT, support.value, support.words)

    # The window of failures and cost
    w_start, w_days, enough = window_of(a, today, inp.first_day)
    window_years = w_days / 365.25
    years = max(Decimal(w_days) / Decimal("365.25"), Decimal(str(MIN_YEARS)))  # rates a year never divide by less than a year
    months = round(w_days * 12 / 365.25)

    # Failures
    mine = [f for f in inp.failures.get(a.pk, ()) if w_start <= f[0] <= today]
    failures = len(mine) if enough else None
    unrecorded = sum(1 for f in mine if not f[1]) if enough else 0
    still_open = sum(1 for f in mine if f[3]) if enough else 0
    imported = sum(1 for f in mine if f[2] and not f[1] and not f[3]) if enough else 0  # an open one gets its finding when completed
    per_year = None
    if not enough:
        rel_c = Component(RELIABILITY, None, TOO_LITTLE_HISTORY)
    else:
        per_year = failures / max(window_years, MIN_YEARS)
        note = ""
        if unrecorded:
            why = [f"{still_open} still open" if still_open else "", f"{imported} imported: {NOT_RECORDED_THEN}" if imported else ""]
            why = [w for w in why if w]
            note = f"{unrecorded} with no finding recorded" + (f" ({', '.join(why)})" if why else "")
        rel_c = Component(RELIABILITY, reliability_value(per_year), f"{failures} in {months} mo, {per_year:.1f} a year", note)

    # Service cost a year against the replacement estimate: labor and parts in the window a year, plus the live contract's share (its
    # vendor's work is the contract's, so never counted twice)
    estimate = estimate_for(a, _set_on(dm, inp))
    if estimate.amount is None:
        gaps.add(NO_ESTIMATE)
    contract = inp.contracts.get(a.contract_id) if a.contract_id else None
    share = coverage = contract_vendor = None
    unallocated = False  # a live contract with a cost whose share of it cannot be worked out (no acquisition cost to allocate by)
    if contract is not None:
        annual, covered, coverage, contract_vendor = contract
        acquisition = Decimal(a.acquisition_cost or 0)
        if annual <= 0:
            share = Decimal("0")
        elif acquisition > 0 and covered > 0:
            share = annual * acquisition / covered
        else:
            unallocated = True  # as the Costs tab says (asset_tabs.contract_share): never a silent $0

    def paid_by_contract(vendor, vendor_name, wo_type, part) -> bool:
        """A vendor line the live contract's annual cost already pays for (contract_covers, by that contract's vendor)."""
        return (vendor and contract is not None and vendor_key(vendor_name) == contract_vendor
                and contract_covers(coverage, wo_type, part))

    spent = sum((amount for day, vendor, vendor_name, wo_type, part, amount in inp.spent.get(a.pk, ())
                 if w_start <= day <= today and not paid_by_contract(vendor, vendor_name, wo_type, part)), Decimal("0"))
    cost_year = ratio = None
    if not enough:
        cost_c = Component(COST, None, TOO_LITTLE_HISTORY)
    elif unallocated:
        cost_c = Component(COST, None, CONTRACT_SHARE_UNKNOWN)
    else:
        cost_year = (spent / years + (share or 0)).quantize(Decimal("0.01"), rounding="ROUND_HALF_UP")
        if estimate.amount is None:
            cost_c = Component(COST, None, f"{_money(cost_year)} a year; no replacement estimate")
        else:
            ratio = cost_year / estimate.amount
            pct = (ratio * 100).quantize(Decimal("1"), rounding="ROUND_HALF_UP")
            cost_c = Component(COST, cost_value(ratio), f"{pct}% a year of replacement ({_money(cost_year)} a year)",
                               f"with its contract share, {_money(share)} a year" if share else "")

    # A confirmed recall not done on this device
    model_matches = inp.matches.get(dm.pk, ())
    confirmed = [(alert, one) for alert, status, one in model_matches
                 if status == AlertMatch.Status.IN_PROGRESS and (a.pk, alert) not in inp.recalls_done]
    reviewing = any(status == AlertMatch.Status.UNDER_REVIEW for _alert, status, _one in model_matches)
    if confirmed:
        flags.add(RECALL)
        class_one = any(one for _alert, one in confirmed)
        safety_c = Component(SAFETY, CLASS_ONE_VALUE if class_one else RECALL_VALUE,
                             _plural(len(confirmed), "confirmed recall", "confirmed recalls") + " not done on this device"
                             + (", Class I" if class_one else ""), "recall under review" if reviewing else "")
    else:
        done_here = any(status == AlertMatch.Status.IN_PROGRESS for _alert, status, _one in model_matches)
        safety_c = Component(SAFETY, 0.0, "confirmed recall done on this device" if done_here else "no confirmed recall",
                             "recall under review" if reviewing else "")

    # Kept out of use after its latest repair
    if a.latest_repair_check == RepairCheck.NOT_RETURNED and a.status != AssetStatus.IN_SERVICE:
        flags.add(NOT_RETURNED)

    components = (age_c, support_c, rel_c, cost_c, safety_c)
    multiplier = multiplier_for(dm.risk_class)
    score, unknown = total(components, multiplier)
    ordered_flags = tuple(f for f in FLAGS if f in flags)
    fy = suggested_fy(must_act=any(f in MUST_ACT for f in flags), life_end=life_end, support_ends_on=dm.support_ends_on,
                      parts_end_on=dm.parts_end_on, today=today, start=start)
    return Priority(
        asset=a, components=components, multiplier=multiplier, score=score, points_unknown=unknown, estimate=estimate, support=support,
        flags=ordered_flags, gaps=tuple(g for g in GAPS if g in gaps), suggested_fy=fy, age_years=age, life_years=life, life_end=life_end,
        condition=a.condition, window_start=w_start, window_years=window_years, failures=failures, unrecorded=unrecorded,
        service_cost_year=cost_year,
        extras={"window_months": months, "failures_per_year": per_year, "still_open": still_open, "imported": imported,
                "spent": spent if enough else None, "contract_share": share, "cost_ratio": ratio, "recalls": len(confirmed),
                "recall_under_review": reviewing},
    )


def _set_on(dm, inp: _Inputs) -> date | None:
    """The day the model's list price was set, when its history's last price is the one on the model."""
    found = inp.list_set_on.get(dm.pk)
    if found is None:
        return None
    day, cost = found
    return day if cost == dm.list_cost else None


def rank(priorities) -> tuple[list[Priority], list[Priority]]:
    """(ranked, not ranked): the ranked by score, then points known, then tag; the rest (no age or no estimate) by tag."""
    ranked = sorted((p for p in priorities if p.ranked), key=lambda p: (-p.score, -p.points_known, p.asset.tag))
    rest = sorted((p for p in priorities if not p.ranked), key=lambda p: p.asset.tag)
    return ranked, rest


def version_words() -> str:
    """The rule printed beside every score: "Score v2: age 30, support 25, failures 20, service cost 15, recall 10; times 1.0 life
    support, 0.95 high, 0.9 medium, 0.85 low risk"."""
    parts = ", ".join(f"{short} {WEIGHTS[k]}" for k, short in ((AGE, "age"), (SUPPORT, "support"), (RELIABILITY, "failures"),
                                                               (COST, "service cost"), (SAFETY, "recall")))
    mult = ", ".join(f"{_mult(MULTIPLIERS[r])} {w}" for r, w in ((RiskClass.LIFE_SUPPORT, "life support"), (RiskClass.HIGH, "high"),
                                                                 (RiskClass.MEDIUM, "medium"), (RiskClass.LOW, "low risk")))
    return f"Score v{SCORE_VERSION}: {parts}; times {mult}"


def _mult(m: float) -> str:
    return f"{m:g}" if m != 1 else "1.0"
