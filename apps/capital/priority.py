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
             LIFE_SOURCE: "Where the expected life comes from is not recorded", SUPPORT_UNCHECKED: "Manufacturer support not checked"}
# A device is ranked only when its age and its replacement estimate are known; else it is listed apart, "Not ranked: <why>"
UNRANKED_WORDS = {NO_INSTALL_DATE: "no install date", NO_ESTIMATE: "no replacement estimate"}

TOO_LITTLE_HISTORY = "too little history"
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
    month (read from Settings once when None). Unordered: rank() orders them."""
    raise NotImplementedError


def for_asset(asset, today: date, start: int | None = None) -> Priority | None:
    """One device's priority (the drawer's outlook): score_assets for that device; None when the rule does not score it (not ours, or
    not in use)."""
    raise NotImplementedError


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
