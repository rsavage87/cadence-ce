"""
What a repair found (slice 33): the one rule for WorkOrder.repair_finding (RepairFinding) that every door offers and every reader
counts by. One finding per repair, a choice, never free text; "use error" as FDA and IEC 62366-1 word it, never "user error"; the cause
wins over the part (a dropped battery is damage), decided in ORDER.

Groups partition every value and "" (a test checks it):
- NO_FAULT: no problem found, use error, power / gas / network / room: the device had no fault;
- USE_RELATED (use error, damage): the program evaluation's "use-related" line (DNV PE.7, The Joint Commission's use errors);
- RELIABILITY (battery, software, wear, PM could have caught it, caused by earlier service): device failures, what MTBF and the AEM
  evidence count (CMS A-0724: never failures due to operator error); PM_RELATED (PM could have caught it, caused by earlier service);
- UNKNOWN ("", not stated by the vendor): counted as failures where failures are counted, as every repair was before slice 33; an open
  repair counts as unknown whatever is stored (it is being worked on again).
Damage, accessory, power and room conditions, use error, and no problem found are never failures; each is shown on its own.

Who chooses what (choices_for, which completion and services.set_repair_finding enforce): a scoped user (a vendor's account) only the
observations (OBSERVATIONS: never use error, PM could have caught it, or caused by earlier service, the facility's judgments, nor not
stated); not stated only on vendor service; an incident's investigation never use error (who erred stays in the facility's event report);
a failed PM's repair (follow_up_of a PM) never "PM could have caught it" (the PM caught it: it is counted as found on PM).
"""
from __future__ import annotations

from django.db.models import Q

from .models import OPEN_STATUSES, RepairFinding, WoType

F = RepairFinding

NO_FAULT = (F.NO_PROBLEM_FOUND, F.USE_ERROR, F.ENVIRONMENT)
USE_RELATED = (F.USE_ERROR, F.DAMAGE)
RELIABILITY = (F.BATTERY, F.SOFTWARE, F.WEAR, F.PM_PREVENTABLE, F.SERVICE_INDUCED)
PM_RELATED = (F.PM_PREVENTABLE, F.SERVICE_INDUCED)
OTHER_FAULT = (F.DAMAGE, F.ACCESSORY)  # a device fault that is not a reliability failure: shown on its own
UNKNOWN = ("", F.NOT_STATED)
# The partition (every value and "" in exactly one): NO_FAULT, OTHER_FAULT, RELIABILITY, UNKNOWN.
PARTITION = (NO_FAULT, OTHER_FAULT, RELIABILITY, UNKNOWN)

# What a scoped user (a vendor's account) may record: what it sees on the device, never the facility's judgments.
OBSERVATIONS = (F.NO_PROBLEM_FOUND, F.ENVIRONMENT, F.DAMAGE, F.BATTERY, F.ACCESSORY, F.SOFTWARE, F.WEAR)

# The order to decide by when more than one fits (on screen, as the hospitals' flowcharts do): the cause before the part.
ORDER = (F.SERVICE_INDUCED, F.DAMAGE, F.ENVIRONMENT, F.PM_PREVENTABLE, F.BATTERY, F.ACCESSORY, F.SOFTWARE, F.WEAR)
ORDER_WORDS = ("When more than one fits, the first of: caused by earlier service, damaged in handling, power or room conditions, PM could "
               "have caught it, battery, accessory, software, wore out.")

# The select's groups (the modal and the drawer's Change), in this order.
GROUPS = (
    ("No device fault", (F.NO_PROBLEM_FOUND, F.USE_ERROR, F.ENVIRONMENT)),
    ("Device fault", (F.DAMAGE, F.BATTERY, F.ACCESSORY, F.SOFTWARE, F.WEAR, F.PM_PREVENTABLE, F.SERVICE_INDUCED)),
    ("The vendor's report", (F.NOT_STATED,)),
)

HINTS = {
    F.NO_PROBLEM_FOUND: "You could not make it fail (could not duplicate).",
    F.USE_ERROR: "No device fault, and you found how it was operated: settings, clamp, loading. If you could not make it fail, it is No "
                 "problem found.",
    F.ENVIRONMENT: "Power, medical gas, network, or room conditions; no device fault.",
    F.DAMAGE: "Dropped, impact, or fluid.",
    F.BATTERY: "The battery failed before it was due for replacement.",
    F.ACCESSORY: "A cable, probe, lead, or disposable.",
    F.SOFTWARE: "A software, firmware, or configuration fault in the device.",
    F.WEAR: "Wore out or failed in normal use, and no other cause was found.",
    F.PM_PREVENTABLE: "Scheduled maintenance could have caught or prevented it.",
    F.SERVICE_INDUCED: "A recent repair or PM caused it, or a part just replaced failed early.",
    F.NOT_STATED: "The vendor's report names no cause.",
}

FAILURE_RULE = ("Failures are device faults (battery, software, wore out, PM could have caught it, caused by earlier service) and repairs "
                "with no finding recorded; never use error, damage, an accessory, power or room conditions, or no problem found.")

REQUIRED = "Choose what the repair found."
NOT_OFFERED = "Choose one of the findings listed."
INVESTIGATION = ("This repair is an incident's investigation: use error is never recorded on it (who erred stays in the facility's event "
                 "report). Choose what was found on the device.")
FOUND_ON_PM = "This repair was opened from a failed PM: the PM caught it. Choose what failed."
NOT_A_REPAIR = "Only a repair records what it found."
FACILITY_RECORDED = "The facility's CE staff recorded what this repair found: it stays as they recorded it."  # a scoped user's change (merge fix)


def is_investigation(wo) -> bool:
    """Whether `wo` is the investigation of an incident not recorded in error (any status: a closed incident's investigation stays one)."""
    from apps.incidents.models import Incident, Status

    return bool(wo.pk) and Incident.objects.filter(work_order_id=wo.pk).exclude(status=Status.IN_ERROR).exists()


def found_on_pm(wo) -> bool:
    """Whether `wo` was opened from a failed PM (completion's follow-up repair)."""
    from .models import WorkOrder

    return bool(wo.follow_up_of_id) and WorkOrder.objects.filter(pk=wo.follow_up_of_id, type=WoType.PM).exists()


def choices_for(wo, user=None, *, investigation: bool | None = None, from_pm: bool | None = None) -> list[str]:
    """The findings `user` may record on repair `wo`, in GROUPS' order: every door offers these and the service enforces them. Pass
    `investigation` / `from_pm` when already known (else read: one query each)."""
    from . import scoping

    if wo.type != WoType.REPAIR:
        return []
    investigation = is_investigation(wo) if investigation is None else investigation
    from_pm = found_on_pm(wo) if from_pm is None else from_pm
    scoped = scoping.is_scoped(user)
    out = []
    for _label, values in GROUPS:
        for value in values:
            if scoped and value not in OBSERVATIONS:
                continue
            if value == F.NOT_STATED and not wo.vendor_service:
                continue
            if investigation and value == F.USE_ERROR:
                continue
            if from_pm and value == F.PM_PREVENTABLE:
                continue
            out.append(str(value))
    return out


# Said only where "PM could have caught it" is offered (review fix: never send a door to a choice it does not have).
BATTERY_PM = " One past due, or one the PM's battery test should have caught, is PM could have caught it."


def hint(value, choices) -> str:
    """A finding's hint as the door offering `choices` words it."""
    text = HINTS.get(value, "")
    if value == F.BATTERY and F.PM_PREVENTABLE in choices:
        text += BATTERY_PM
    return text


# Each choice as ORDER_WORDS names it (a phrase with no comma: the order reads as one list).
ORDER_PHRASES = {F.SERVICE_INDUCED: "caused by earlier service", F.DAMAGE: "damaged in handling", F.ENVIRONMENT: "power or room conditions",
                 F.PM_PREVENTABLE: "PM could have caught it", F.BATTERY: "battery", F.ACCESSORY: "accessory", F.SOFTWARE: "software",
                 F.WEAR: "wore out"}


def order_words(choices) -> str:
    """ORDER_WORDS for the choices a door offers: the first of those that fits (review fix: never a choice it does not have). Every
    choice offered: ORDER_WORDS itself."""
    named = [ORDER_PHRASES[v] for v in ORDER if v in choices]
    return f"When more than one fits, the first of: {', '.join(named)}." if len(named) > 1 else ""


def facility_holds(wo, user, *, investigation: bool, from_pm: bool) -> bool:
    """Whether the finding stored on `wo` is the facility's and stays as recorded whatever a scoped `user` sends: one they cannot choose
    that the repair's own limits still allow (review fix: completion answers them the same whatever they send, so a refusal never tells
    what is stored; the API's checklist and words, and the web's select, read this too)."""
    from . import scoping

    stored = wo.repair_finding
    return (bool(stored) and scoping.is_scoped(user)
            and stored not in choices_for(wo, user, investigation=investigation, from_pm=from_pm)
            and stored in choices_for(wo, None, investigation=investigation, from_pm=from_pm))


def label(value) -> str:
    return dict(RepairFinding.choices).get(value, "Not recorded" if not value else str(value))


def failure_q(prefix: str = "") -> Q:
    """Repairs that count as failures (FAILURE_RULE): reliability findings, no finding recorded or not stated, and every open repair.
    Positive conditions only; `prefix` reaches the work order from another model ("work_orders__")."""
    p = prefix
    return Q(**{f"{p}type": WoType.REPAIR}) & (Q(**{f"{p}status__in": OPEN_STATUSES})
                                               | Q(**{f"{p}repair_finding__in": [str(v) for v in (*RELIABILITY, *UNKNOWN)]}))


def recorded_q(prefix: str = "") -> Q:
    """Repairs with a finding recorded (never blank, never not stated): the coverage's numerator."""
    p = prefix
    return Q(**{f"{p}type": WoType.REPAIR}) & Q(**{f"{p}repair_finding__in": [str(v) for v in RepairFinding.values if v != F.NOT_STATED]})
