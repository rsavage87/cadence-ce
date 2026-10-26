"""
Security decisions (slice 39, beyond the mock): every write of apps.cybersecurity.models.DeviceSecurityDecision. HHS 405(d) HICP 2023
recommends documenting mitigating controls and a timeline for resolving the risk, through the organization's risk acceptance or
exception process, when a system past its support stays in use; no rule requires any of it, and Cadence never says one does. IT's
exception stays in IT's system: a decision holds its number (a token), never its text.

- record_decision: a decision for devices past the manufacturer's or the OS vendor's support (or whose end is announced within 12
  months), for one device, a model's devices of ours on one release, or every device of ours of a model; superseding the live decision
  for the same scope (Review and renew: the old one is ended "superseded" in the same transaction, then the new one inserted).
- end_decision: ended as no longer needed (its devices updated, replaced, retired, or returned, or each under a decision of its own). A
  decision is superseded only by recording a new one.
- mark_entered_in_error: ended as entered in error.
A decision is immutable once recorded: nothing here changes its fields but its end (ended_on, ended_by, end_reason). Nothing is deleted.
Equipment's writers (retire, return to owner, a model change, a status change) never write here: the readers (rules) re-derive.

Levels (checked here, permissions.can_decide): Device security Approve and Equipment View, never a scoped user; `by` None is the system
(the seed, tests).

Refusals, in words keyed by field (a ValidationError; never "required": "Choose ..." or "Enter ..."), every field problem at once before
any lock: the scope (exactly one: a device; a release, with its model or alone; a model), the reasons (kinds), the response, the controls
(SUPPORT_CONTROLS: never network_removed, rules.NETWORK_REMOVED_HINT; some exactly to keep in use with controls; IT's reference exactly
with recorded_elsewhere), the plan (a day exactly for an update or a replacement planned, not before the decision; an owner with any plan
but none: an active person of this facility, never scoped, with Device security View), who accepted it, IT's exception number, the day
decided (not after today), the review (after the day decided, by rules.review_cap; rules.review_default when not given). Each reference
is a token, never a network identifier (network_identifier: an address, a MAC, a VLAN, or a port: NOT_AN_ADDRESS). Then, under the locks:
the device on site (its row FOR KEY SHARE: a retire in flight is waited for and then read); a release versioned, not entered in error, of
its model; devices of ours on site in a release's or a model's scope; the scope past its support or an end announced within 12 months
(rules.support_need over the devices the decision would govern: never one a more specific live decision governs, would_govern); the
reasons those that hold now for those devices (none that does not, every one that ended); return it to
its owner only for a temporary device's own decision; the model's record not saying "not on a separate segment" when the controls name
`segmented`; no live decision for the scope (IN_FORCE: "review and renew it instead"), but the one superseded; supersedes a live decision
of the same scope.

Lock order (every writer here; build39 section 5):
  1. the facility's register lock (services.lock_register)
  2. the decision superseded or ended, FOR UPDATE of its own row
  3. the release, for a release scope (services._locked_release, its own row)
  4. the decision number (Sequence.next("decision-<yy>"), a row only decision writers take)
  5. the device FOR KEY SHARE, for a device scope (services._locked_devices): its model, status, and ownership read from that row
  6. the insert
An IntegrityError (the live-scope constraint, or a foreign key PostgreSQL checks at COMMIT: Django makes them DEFERRABLE INITIALLY
DEFERRED) or a lock conflict (services._lock_conflict) answers RACE in words, never a 500. Equipment's writers never take the register
lock (a test pins it), so a retire and a decision meet only on the device's row: the decision's KEY SHARE waits for the retire's FOR
UPDATE and then reads the device retired (refused in words), or the retire waits for the decision (which then governs no device on site:
rules' Check "Governs no device here now: end it").

History reasons are fixed sentences with Cadence numbers only: "Recorded", "Recorded, superseding SD-26-0004", "Superseded by
SD-26-0005", "Ended: no longer needed", "Entered in error".

Slice 40, decisions for the devices a security advisory names (build40 section 5; critique40_scope B3): record_decision, refusal_for,
would_govern, and the scope readers take `applies_to` and `advisory` (support and None by default: every slice 39 caller unchanged), and
read only that need key's decisions (rules.key_of: an advisory decision never governs a support need, nor a support decision an
advisory's). HHS 405(d) HICP 2023 recommends identifying a response to each vulnerability an advisory discloses, alongside the
manufacturer (rules.HICP_ADVISORY_DECISIONS); no rule requires it. Before the locks (field shape): an advisory chosen (CHOOSE_ADVISORY;
none for support: ADVISORY_NOT_FOR_SUPPORT), no support reasons (ADVISORY_REASONS), the four responses ("not in use here" for an advisory
only, NOT_IN_USE_ADVISORY_ONLY, with no plan, NOT_IN_USE_PLAN), the controls from rules.CONTROLS (network_removed included). Under the
locks: the advisory neither marked not applicable nor entered in error (ADVISORY_NOT_APPLICABLE, ADVISORY_IN_ERROR: refused here while a
live decision names it, so no live decision ever names one), the day decided not before it was received (DECIDED_BEFORE_RECEIVED), the
scope (_advisory_scope_read: gone as slice 39's; the advisory not naming the model or the release, or naming the release as the fix; a
device on the fix, on a release it does not name, or with the patches recorded that answer it; every device under a more specific
decision of the advisory; none needing a response now), network_removed only when every device it would govern has its own fact "Not
connected here" (network_refusal), return it to its owner for a temporary device's own decision. It stores the advisory's change count
(advisory_changes: the "changed since" Check compares counts). A device with an open update to the fix may still get a decision (interim
controls). There is no "not recorded" refusal for an advisory: a release not recorded is a need.
Lock order with an advisory: the register, the decision superseded, the advisory's row (services._locked_advisory, FOR UPDATE of its own
row), the release, the number, the device FOR KEY SHARE, the insert. refusal_for reads the same without any lock.
"""
from __future__ import annotations

import ipaddress
import re
import uuid
from datetime import date
from typing import NamedTuple

from django.core.exceptions import ValidationError
from django.db import IntegrityError, OperationalError, transaction

from apps.equipment.models import Asset, AssetStatus, DeviceModel, Ownership

from . import permissions as perms
from . import rules, services
from .models import (
    EXCEPTION_REFERENCE_MAX,
    EXCEPTION_REFERENCE_VALIDATOR,
    SUPPORT_RESPONSES,
    AcceptedBy,
    AdvisoryStatus,
    AdvisoryVersions,
    AppliesTo,
    DecisionEnd,
    DecisionPlan,
    DecisionResponse,
    DeviceSecurityDecision,
    SecurityAdvisory,
    Segmented,
    SoftwareRelease,
)

# Refusals (keyed by field)
DECIDE_PERMISSION = "Recording a security decision needs Device security Approve and Equipment View."
END_PERMISSION = "Ending a security decision needs Device security Approve and Equipment View."
SCOPE_WORDS = "Choose one device, one release of a model, or one model."
CHOOSE_DECISION = "Choose a security decision of this facility."
CHOOSE_RELEASE = "Choose a version of this model."
NOT_OF_MODEL = "Choose a version of this model: {version} is another model's."
REASONS_WORDS = "Choose the reasons from the list: the manufacturer's security updates, the OS vendor's support."
REASONS_NONE = "Choose at least one reason: the manufacturer's security updates, or the OS vendor's support."
REASONS_NOW = "Choose the reasons that hold now: {words}."
RESPONSE_WORDS = "Choose keep in use with controls, keep in use without added controls, or take out of use."
CONTROLS_WORDS = "Choose controls from the list."
CONTROLS_NEEDED = "Choose the controls this decision relies on."
CONTROLS_ONLY_WITH = "Only Keep in use with controls names controls: choose it, or leave the controls blank."
SEGMENT_REFUSAL = ("The model's record says its devices are not on a separate segment: record that there first, or choose other "
                   "controls.")
REFERENCE_NEEDED = "Enter the change or exception number in IT's system (e.g. CHG0012345): the control's details stay there."
REFERENCE_ONLY_WITH = "Choose Controls recorded in IT's system to give its reference, or leave it blank."
REFERENCE_WORDS = "Enter IT's record or ticket number as it is written, without spaces, e.g. EX-1042."
REFERENCE_LENGTH = f"Keep it to {EXCEPTION_REFERENCE_MAX} characters."
NOT_AN_ADDRESS = "Enter IT's record or ticket number, never an address, a VLAN, or a port."
PLAN_WORDS = "Choose a plan from the list, or No plan recorded."
PLAN_DAY = {DecisionPlan.UPDATE_PLANNED: "Enter the day the update is planned by.",
            DecisionPlan.REPLACE_PLANNED: "Enter the day the replacement is planned by."}
PLAN_DAY_ONLY = "Only an update or a replacement planned takes a day: leave it blank."
PLAN_BEFORE = "The plan's day cannot be before the day decided."
RETURN_ONLY_TEMPORARY = "Return it to its owner is a plan for one rental, vendor loaner, or demo unit: choose it on that device's decision."
OWNER_ONLY_WITH = "No plan recorded names no owner: leave the owner blank."
CHOOSE_OWNER = "Choose an active person of this facility with Device security View."
ACCEPTED_WORDS = "Choose who accepted it."
REVIEW_AFTER = "The review day comes after the day decided."
REVIEW_CAP = "Choose a review day by {day}: Cadence keeps a decision's review within {months} months of the day decided."
REVIEW_PASSED = "This review date has passed: the decision will read Review due since {day}."  # Record decision's note (not a refusal)
# Under the locks
RETIRED = "{tag} is retired: a decision is for a device on site."
RETURNED = "{tag} was returned to its owner: a decision is for a device on site."
NOT_CONNECTED = "{tag} is not connected here, as recorded or disclosed: it needs no decision."
CONNECTION_UNKNOWN = "{tag}'s connection is not recorded: record its facts first (Record facts)."
# Said of what is recorded, never more (review 39 F12): "as recorded", and a device whose support nothing records is told so
NOT_NEEDED = "{name} is not past its support as recorded, and no end is announced within 12 months."
SECURITY_END_NOT_RECORDED = "{tag}: the end of the manufacturer's security updates for {version} is not recorded: record it on the release (Models) first."
NO_RELEASE_RECORDED = "{tag}: its release is not recorded, nor the end of its security updates: record its release (Record facts) first."
# A decision being renewed for the OS vendor's support, while that support is not recorded (rules.need_unknown with its reasons)
OS_SUPPORT_NOT_RECORDED = ("{tag}: the OS vendor's support for the operating system of {version} is not recorded: record it on the release "
                           "(Models) first.")
NO_RELEASE_OS = "{tag}: its release is not recorded, nor its operating system's support: record its release (Record facts) first."
EXTENDED = ("{name}: the manufacturer's security updates continue (announced to end {day}): a decision can be recorded once their end is "
            "within 12 months.")
NO_DEVICE_OURS = "No device of ours on site runs {version}."
NO_MODEL_DEVICE = "No {model} of ours is on site."
NONE_NEEDED = ("No device of ours on site in this scope is both connected (or able to connect) and past its support or within 12 months "
               "of an announced end, as recorded.")
# A release's or a model's scope with more than one device whose need is not recorded (rules.need_unknown): the first one's words follow
SCOPE_NOT_RECORDED = "{count} of ours in this scope are not recorded enough to tell whether they need a decision ({tags}). {words}"
# A release's or a model's scope whose devices are under more specific live decisions (would_govern; skeptics T0): a decision for it
# governs only the others, and is read from them
GOVERNED_ELSEWHERE = ("Every device of ours on site in this scope is under a more specific decision ({numbers}): a decision for this scope "
                      "would govern none of them.")
NEEDED_ELSEWHERE = ("No device a decision for this scope would govern ({tags}) is both connected (or able to connect) and past its support "
                    "or within 12 months of an announced end, as recorded: those that are, are under a more specific decision ({numbers}).")
IN_FORCE = "{number} is in force for this scope: review and renew it instead."
SUPERSEDES_OTHER = "{number} is for another scope: renew a decision for this one."
ALREADY_ENDED = "{number} ended on {day}: it stays as recorded."
END_REASON_WORDS = ("Choose No longer needed: a decision is superseded by recording a new one, and marked entered in error on its own.")
RACE = "This decision's devices or scope were being changed or recorded meanwhile: try again."

# Slice 40, a decision for an advisory's devices (build40 section 5.3; critique40_scope B3). Before the locks (field shape):
APPLIES_TO_WORDS = "Choose why it is recorded: support ended, or a security advisory."
ADVISORY_NOT_FOR_SUPPORT = "A decision for devices past support names no advisory: leave it blank."
ADVISORY_REASONS = "An advisory decision names no support reasons."
ADVISORY_RESPONSE_WORDS = ("Choose keep in use with controls, keep in use without added controls, take out of use, or decided the part it "
                           "names is not in use here.")
NOT_IN_USE_ADVISORY_ONLY = "Decided the part it names is not in use here is a response to an advisory only."
NOT_IN_USE_PLAN = "A decision that the part it names is not in use here has no plan."
# Under the locks: the advisory, the day decided
ADVISORY_NOT_APPLICABLE = "{number} is marked not applicable here: reopen it first."
ADVISORY_IN_ERROR = "{number} was entered in error."
DECIDED_BEFORE_RECEIVED = "The day decided is before {number} was received ({day})."
# The scope (keyed by its field; each NOT_NEEDED: End answers a decision being renewed)
MODEL_NOT_NAMED = "{number} does not name the {model}."
DEVICE_MODEL_NOT_NAMED = "{number} does not name {tag}'s model, the {model}."
RELEASE_NOT_NAMED = "{number} does not name {version}."
RELEASE_IS_FIX = "{version} is the release {number} names as the fix."
ON_FIX = "{tag} runs {version}, the release {number} names as the fix."
NOT_NAMED = "{tag} runs {version}, a release {number} does not name."
PATCHED = "{tag} has the manufacturer's patches recorded{day}, since {number} was received."
PATCHED_LIST = "{tag} has the manufacturer's patches recorded{day} ({reference}), the list {number} names."
ADVISORY_NEEDED_ELSEWHERE = ("No device a decision for this scope would govern ({tags}) needs a response to {number} now: those that do "
                             "are under a more specific decision ({numbers}).")
NONE_NEEDS = "No device it would govern needs a response to {number} now."
# network_removed (keyed controls; critique40_rules S8): every device it would govern has its own fact "Not connected here"
NETWORK_CONNECTED = "{tag} is recorded {connection}: record Not connected here on it first (Record facts), or choose other controls."
NETWORK_CONNECTED_MANY = ("{tags} are recorded connected: record Not connected here on them first (Record facts), or choose other "
                          "controls.")
NETWORK_NOT_RECORDED = ("{tag} has no connection recorded here: record Not connected here on it first (Record facts), or choose other "
                        "controls.")
NETWORK_NOT_RECORDED_MANY = ("{tags} have no connection recorded here: record Not connected here on them first (Record facts), or choose "
                             "other controls.")

# History reasons (fixed sentences, Cadence numbers only)
RECORDED = "Recorded"
RECORDED_SUPERSEDING = "Recorded, superseding {number}"
SUPERSEDED_BY = "Superseded by {number}"
ENDED_NO_LONGER_NEEDED = "Ended: no longer needed"
ENTERED_IN_ERROR = "Entered in error"

def _letter_and_digit(span: int) -> str:
    """Lookaheads: the next `span` characters (hex digits and separators) hold a hex letter and a decimal digit."""
    return rf"(?=[\d_.:-]{{0,{span - 1}}}[a-f])(?=[a-f_.:-]{{0,{span - 1}}}\d)"


# A MAC in the layouts switches and tools print (review 39 F5): six pairs joined by - . or :, three groups of four joined by - . or _
# (Cisco's dotted, H3C's and HPE Comware's hyphenated), two groups of six joined by - . _ or : (HP ProCurve's), or twelve bare hex
# digits. The layouts a ticket number of decimal digits also takes (twelve bare, four-four-four, six-six, pairs joined by _: a dated
# sequence such as 2026-1021-0042) need a hex letter and a digit, as a MAC written only in decimal digits is rare (skeptics S6). Found
# anywhere in the token, alone or joined on by a token's joiner (- # _ / . :) on either side, never within a longer run of hex digits:
# "EX-001A-2B3C-4D5E", "FW-00-1A-2B-3C-4D-5E", "001A2B3C4D5E#1" (skeptics S5)
_HEX_JOINED = r"(?:^|(?<=[-#_/.:]))(?:{})(?=$|[-#_/.:])"
_MAC = re.compile(_HEX_JOINED.format("|".join((
    r"(?:[0-9a-f]{2}[-.:]){5}[0-9a-f]{2}",
    _letter_and_digit(17) + r"(?:[0-9a-f]{2}[-._:]){5}[0-9a-f]{2}",
    _letter_and_digit(14) + r"(?:[0-9a-f]{4}[-._]){2}[0-9a-f]{4}",
    _letter_and_digit(13) + r"[0-9a-f]{6}[-._:][0-9a-f]{6}",
    _letter_and_digit(12) + r"[0-9a-f]{12}"))), re.IGNORECASE)
# A UUID in its own layout (8-4-4-4-12 hex digits) is a record's identifier, never read as holding a MAC (its groups take the MAC's layouts)
_UUID = re.compile(_HEX_JOINED.format(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"), re.IGNORECASE)
_DOTTED = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,3})?$")
# Four dotted numbers anywhere in the token, ending it or followed by a token's joiner (an address with a port, a range, or a prefix:
# "10.20.30.40#443", "10.20.30.0-24", "FW-10.20.30.40"); never inside a longer run of digits and dots on its left
_DOTTED_IN = re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?=$|[-#_/.:])")
_PORT = re.compile(r"^(?:port(?![a-z])|(?:tcp|udp)[-/_.#]?\d+$|\d+[-/_.#](?:tcp|udp)$)", re.IGNORECASE)


def network_identifier(text: str) -> bool:
    """A token that reads as a network identifier (build39 section 2), refused in every cyber token field this slice adds: an address or
    a block (ipaddress.ip_network(strict=False) parses it, or four dotted numbers, alone or within the token with a port, a range, or a
    prefix joined on), a MAC (_MAC's layouts, alone or within the token, outside a UUID's own layout), one starting "VLAN" in any case,
    or a port ("port...", "tcp-443", "443/udp")."""
    text = (text or "").strip()
    if not text:
        return False
    try:
        ipaddress.ip_network(text, strict=False)
        return True
    except ValueError:
        pass
    return bool(_DOTTED.match(text) or _DOTTED_IN.search(text) or _MAC.search(_UUID.sub("u", text)) or text.casefold().startswith("vlan")
                or _PORT.match(text))


# --- small parts ---------------------------------------------------------------------------------------------------------------------

def _token(value, field: str, errors: dict) -> str:
    """A reference typed as a token (EXCEPTION_REFERENCE_VALIDATOR), at most EXCEPTION_REFERENCE_MAX characters, never a network
    identifier; "" for none."""
    if value is None:
        return ""
    if not isinstance(value, str):
        errors[field] = REFERENCE_WORDS
        return ""
    text = value.strip()
    if not text:
        return ""
    if len(text) > EXCEPTION_REFERENCE_MAX:
        errors[field] = REFERENCE_LENGTH
    elif network_identifier(text):
        errors[field] = NOT_AN_ADDRESS
    else:
        try:
            EXCEPTION_REFERENCE_VALIDATOR(text)
        except ValidationError:
            errors[field] = REFERENCE_WORDS
    return text


def _choice(value, choices, field: str, words: str, errors: dict) -> str:
    text = value.strip() if isinstance(value, str) else None
    if text not in choices.values:
        errors[field] = words
        return ""
    return text


def _kinds(values, allowed: dict, field: str, errors: dict, *, words: str) -> list:
    """A list of slugs from `allowed`, each once, in `allowed`'s order; refused (keyed `field`) for anything else."""
    if values is None:
        return []
    if isinstance(values, str) or not hasattr(values, "__iter__"):
        errors[field] = words
        return []
    given = [v.strip() if isinstance(v, str) else v for v in values]
    if any(not isinstance(v, str) or v not in allowed for v in given):
        errors[field] = words
        return []
    return [slug for slug in allowed if slug in given]


def _decision_row(decision, field: str = "decision", *, lock: bool = True):
    """The decision (an instance, its id, or its number in any letter case), this facility's only: FOR UPDATE of its own row inside a
    writer's transaction; `lock=False` a plain read (refusal_for, outside any transaction: FOR UPDATE there is an error on PostgreSQL)."""
    pk = services._pk(decision, DeviceSecurityDecision)
    rows = DeviceSecurityDecision.objects.order_by("pk")
    if lock:
        rows = rows.select_for_update(of=("self",))
    if pk is not None:
        found = rows.filter(pk=pk).first()
    elif isinstance(decision, str) and decision.strip():
        found = rows.filter(number__iexact=decision.strip()).first()
    else:
        found = None
    if found is None:
        raise ValidationError({field: CHOOSE_DECISION})
    return found


def _plan_owner(value, errors: dict):
    """The plan's owner: an active person of this facility (Users are not tenant-scoped: the facility is checked here), never scoped, with
    Device security View; None for none."""
    if value in (None, ""):
        return None
    from apps.accounts.models import User
    from apps.tenants.context import get_current_tenant

    if isinstance(value, User):
        value = value.pk
    try:
        pk = int(value)
    except (TypeError, ValueError):
        errors["plan_owner"] = CHOOSE_OWNER
        return None
    tenant = get_current_tenant()
    found = User.objects.filter(pk=pk, tenant_id=getattr(tenant, "pk", None), is_active=True).select_related("role").first()
    if found is None or not perms.can_view(found):
        errors["plan_owner"] = CHOOSE_OWNER
        return None
    return found


def _number(decided_on: date) -> str:
    from apps.core.models import Sequence

    yy = decided_on.year % 100
    return f"SD-{yy:02d}-{Sequence.next(f'decision-{yy:02d}'):04d}"


def _scope_field(kind: str) -> str:
    return {rules.DEVICE_SCOPE: "asset", rules.RELEASE_SCOPE: "release", rules.MODEL_SCOPE: "device_model"}[kind]


def _key(applies_to=AppliesTo.SUPPORT_ENDED, advisory=None) -> tuple:
    """The need key (rules.key_of's shape) of `applies_to` and `advisory` (an instance or its id; None for support)."""
    if applies_to != AppliesTo.ADVISORY:
        return rules.SUPPORT_KEY
    return rules.advisory_key(getattr(advisory, "pk", advisory))


def _same_scope(old, *, asset_id, model_id, release_id, applies_to=AppliesTo.SUPPORT_ENDED, advisory=None) -> bool:
    """Whether `old` is a decision of this scope and need key (support by default; an advisory's with `applies_to` and `advisory`)."""
    if rules.key_of(old) != _key(applies_to, advisory):
        return False
    if asset_id is not None:
        return old.asset_id == asset_id
    return old.asset_id is None and old.device_model_id == model_id and old.release_id == release_id


def _live_for(*, asset_id, model_id, release_id, applies_to=AppliesTo.SUPPORT_ENDED, advisory=None):
    """The live decision for this scope and need key (devices past support by default; an advisory's with `applies_to` and `advisory`),
    or None (under the register lock)."""
    live = rules._live(_key(applies_to, advisory))
    if asset_id is not None:
        return live.filter(asset_id=asset_id).first()
    if release_id is not None:
        return live.filter(release_id=release_id).first()
    return live.filter(device_model_id=model_id, asset__isnull=True, release__isnull=True).first()


def _applies(applies_to, advisory, errors: dict) -> bool:
    """Whether the decision is for an advisory's devices (`applies_to` AppliesTo.ADVISORY), refusing any other value at once (keyed
    applies_to); an advisory decision names its advisory (CHOOSE_ADVISORY), a support decision none (ADVISORY_NOT_FOR_SUPPORT)."""
    text = applies_to.strip() if isinstance(applies_to, str) else None
    if text not in AppliesTo.values:
        raise ValidationError({"applies_to": APPLIES_TO_WORDS})
    blank = advisory is None or (isinstance(advisory, str) and not advisory.strip())
    if text == AppliesTo.ADVISORY and blank:
        errors["advisory"] = services.CHOOSE_ADVISORY
    elif text == AppliesTo.SUPPORT_ENDED and not blank:
        errors["advisory"] = ADVISORY_NOT_FOR_SUPPORT
    return text == AppliesTo.ADVISORY


def _response(value, for_advisory: bool, errors: dict) -> str:
    """The response: keep in use with controls, keep in use without added controls, or take out of use; for an advisory's devices also
    "not in use here" (refused for support: NOT_IN_USE_ADVISORY_ONLY)."""
    text = value.strip() if isinstance(value, str) else None
    if text == DecisionResponse.NOT_IN_USE_HERE and not for_advisory:
        errors["response"] = NOT_IN_USE_ADVISORY_ONLY
        return ""
    if text not in (DecisionResponse.values if for_advisory else [r.value for r in SUPPORT_RESPONSES]):
        errors["response"] = ADVISORY_RESPONSE_WORDS if for_advisory else RESPONSE_WORDS
        return ""
    return text


def _advisory_refusal(advisory) -> str:
    """Why no decision may be recorded under the advisory (its row as read): entered in error, or marked not applicable here; or ""."""
    if advisory.removed_on is not None:
        return ADVISORY_IN_ERROR.format(number=advisory.number)
    if advisory.status == AdvisoryStatus.NOT_APPLICABLE:
        return ADVISORY_NOT_APPLICABLE.format(number=advisory.number)
    return ""


def _advisory_row(advisory, model_id):
    """The advisory's row naming the model (rules.rows_of's, its listed releases read), or None. Two queries."""
    return next((r for r in rules.rows_of([advisory.pk]) if r.device_model_id == model_id), None)


def network_refusal(devices, today: date) -> str:
    """Why an advisory decision governing `devices` (the devices it would govern: decisions.would_govern's) cannot name network_removed,
    or "": each needs its own fact "Not connected here" (critique40_rules S8; a connection as the model discloses, or none recorded, is
    not that fact). Words for those recorded connected first, then for those with none recorded, up to five tags. The modal says it
    before it is tried; record_decision checks it again under its locks. Fixed queries (rules.standings)."""
    devices = list(devices)
    held = rules.standings(devices, today)
    connected = [a for a in devices if held[a.pk].connection.key == rules.CONNECTED]
    if len(connected) == 1:
        return NETWORK_CONNECTED.format(tag=connected[0].tag, connection=rules._lower_first(held[connected[0].pk].connection.words))
    if connected:
        return NETWORK_CONNECTED_MANY.format(tags=rules._tags(a.tag for a in connected))
    missing = [a for a in devices if not (held[a.pk].connection.key == rules.NOT_CONNECTED and held[a.pk].connection.here)]
    if len(missing) == 1:
        return NETWORK_NOT_RECORDED.format(tag=missing[0].tag)
    if missing:
        return NETWORK_NOT_RECORDED_MANY.format(tags=rules._tags(a.tag for a in missing))
    return ""


# --- record ------------------------------------------------------------------------------------------------------------------------

def record_decision(*, asset=None, device_model=None, release=None, reasons=None, response, controls=(), controls_reference="",
                    plan=DecisionPlan.NONE, plan_by=None, plan_owner=None, accepted_by, exception_reference="", decided_on=None,
                    review_by=None, supersedes=None, applies_to=AppliesTo.SUPPORT_ENDED, advisory=None, by,
                    today: date | None = None) -> DeviceSecurityDecision:
    """Record a decision for devices past support (the module docstring's rules and lock order): its scope (`asset`; or `release`, with
    its `device_model` or alone; or `device_model` alone), the reasons that hold now for the devices it would govern (kinds:
    rules.REASONS; _scope_read), the response, the controls (rules.SUPPORT_CONTROLS slugs) and IT's reference for recorded_elsewhere, the
    plan with its day and owner, who accepted it, IT's exception number, the day decided (today by default), the review
    (rules.review_default by default), and the decision it supersedes (the live one of the same scope: ended "Superseded by <number>"
    first, in the same transaction). Stores the devices on site it governs now (governed_ids: rules' "joined since"; never one a more
    specific live decision governs: _would_govern). History "Recorded" (", superseding <number>"). Returns the new decision.
    Slice 40: `applies_to` AppliesTo.ADVISORY with `advisory` (an instance, its id, or its number) records a decision for the devices that
    advisory names (the module docstring): no reasons, the four responses, rules.CONTROLS, the scope read by _advisory_scope_read, the
    advisory's change count stored; superseding only a live decision of the same scope and advisory."""
    services._check(by, perms.can_decide, DECIDE_PERMISSION)
    today = services._today(today)
    errors: dict = {}
    # The scope's shape (resolved through this facility)
    if asset is not None and device_model is None and release is None:
        kind = rules.DEVICE_SCOPE
    elif asset is None and release is not None:
        kind = rules.RELEASE_SCOPE
    elif asset is None and device_model is not None:
        kind = rules.MODEL_SCOPE
    else:
        raise ValidationError({"scope": SCOPE_WORDS})
    asset_id = model_id = release_id = None
    if kind == rules.DEVICE_SCOPE:
        asset_id = services._pk(asset, Asset)
        if asset_id is None or not Asset.objects.filter(pk=asset_id).exists():
            raise ValidationError({"asset": services.CHOOSE_DEVICE})
    else:
        if device_model is not None:
            model_id = services._model(device_model).pk
        if kind == rules.RELEASE_SCOPE:
            release_id = services._pk(release, SoftwareRelease)
            if release_id is None:
                raise ValidationError({"release": CHOOSE_RELEASE})
    # Each field on its own
    for_advisory = _applies(applies_to, advisory, errors)
    if for_advisory:
        given_reasons = () if reasons in (None, "") else reasons
        if isinstance(given_reasons, str) or not hasattr(given_reasons, "__iter__") or list(given_reasons):
            errors["reasons"] = ADVISORY_REASONS
        reason_list = []
    else:
        reason_list = _kinds(reasons, rules.REASONS, "reasons", errors, words=REASONS_WORDS)
        if "reasons" not in errors and not reason_list:
            errors["reasons"] = REASONS_NONE
    response = _response(response, for_advisory, errors)
    given_controls = controls if controls is not None else ()
    if for_advisory:  # every control, network_removed included (checked under the locks: network_refusal)
        control_list = _kinds(given_controls, rules.CONTROLS, "controls", errors, words=CONTROLS_WORDS)
    elif not isinstance(given_controls, str) and rules.NETWORK_REMOVED in list(given_controls or ()):
        errors["controls"] = rules.NETWORK_REMOVED_HINT
        control_list = []
    else:
        control_list = _kinds(given_controls, rules.SUPPORT_CONTROLS, "controls", errors, words=CONTROLS_WORDS)
    if "controls" not in errors and response:
        if response == DecisionResponse.KEEP_WITH_CONTROLS and not control_list:
            errors["controls"] = CONTROLS_NEEDED
        elif response != DecisionResponse.KEEP_WITH_CONTROLS and control_list:
            errors["controls"] = CONTROLS_ONLY_WITH
    reference = _token(controls_reference, "controls_reference", errors)
    if "controls_reference" not in errors and "controls" not in errors:
        if rules.RECORDED_ELSEWHERE in control_list and not reference:
            errors["controls_reference"] = REFERENCE_NEEDED
        elif reference and rules.RECORDED_ELSEWHERE not in control_list:
            errors["controls_reference"] = REFERENCE_ONLY_WITH
    plan = _choice(plan if plan not in (None, "") else DecisionPlan.NONE, DecisionPlan, "plan", PLAN_WORDS, errors)
    if response == DecisionResponse.NOT_IN_USE_HERE and plan and plan != DecisionPlan.NONE:
        errors["plan"] = NOT_IN_USE_PLAN
    accepted = _choice(accepted_by, AcceptedBy, "accepted_by", ACCEPTED_WORDS, errors)
    exception = _token(exception_reference, "exception_reference", errors)
    decided = (services._day(decided_on, "decided_on", "The day decided", errors, today=today, happened=True)
               if decided_on not in (None, "") else today)
    plan_day = services._day(plan_by, "plan_by", "The plan's day", errors, today=today, happened=False)
    if plan and "plan_by" not in errors:
        if plan in PLAN_DAY and plan_day is None:
            errors["plan_by"] = PLAN_DAY[plan]
        elif plan not in PLAN_DAY and plan_day is not None:
            errors["plan_by"] = PLAN_DAY_ONLY
        elif plan_day is not None and decided is not None and "decided_on" not in errors and plan_day < decided:
            errors["plan_by"] = PLAN_BEFORE
    owner = _plan_owner(plan_owner, errors)
    if owner is not None and plan == DecisionPlan.NONE:
        errors["plan_owner"] = OWNER_ONLY_WITH
    review = None
    if review_by not in (None, ""):
        review = services._day(review_by, "review_by", "The review day", errors, today=today, happened=False)
    if review is not None and decided is not None and "decided_on" not in errors and "review_by" not in errors:
        if review <= decided:
            errors["review_by"] = REVIEW_AFTER
        elif response and review > rules.review_cap(response, decided):
            months = rules.OUT_OF_USE_CAP_MONTHS if response == DecisionResponse.TAKE_OUT_OF_USE else rules.REVIEW_CAP_MONTHS
            errors["review_by"] = REVIEW_CAP.format(day=rules.day_words(rules.review_cap(response, decided)), months=months)
    if errors:
        raise ValidationError(errors)
    field = _scope_field(kind)
    try:
        with transaction.atomic():
            services.lock_register()  # 1
            old = None
            if supersedes is not None:  # 2
                old = _decision_row(supersedes, "supersedes")
                if old.ended_on is not None:
                    raise ValidationError({"supersedes": ALREADY_ENDED.format(number=old.number, day=rules.day_words(old.ended_on))})
            locked_advisory, key = None, rules.SUPPORT_KEY
            if for_advisory:  # 3: the advisory's row, its status and day read there
                locked_advisory = services._locked_advisory(advisory)
                words = _advisory_refusal(locked_advisory)
                if words:
                    raise ValidationError({"advisory": words})
                if decided < locked_advisory.received_on:
                    raise ValidationError({"decided_on": DECIDED_BEFORE_RECEIVED.format(
                        number=locked_advisory.number, day=rules.day_words(locked_advisory.received_on))})
                key = rules.advisory_key(locked_advisory)
            keyed = {"applies_to": key[0], "advisory": key[1]}
            locked_release = None
            if kind == rules.RELEASE_SCOPE:  # 4
                locked_release = services._locked_release(release_id, field="release", message=CHOOSE_RELEASE)
                if not locked_release.version_key or locked_release.removed_on is not None:
                    raise ValidationError({"release": CHOOSE_RELEASE})
                if model_id is not None and locked_release.device_model_id != model_id:
                    raise ValidationError({"release": NOT_OF_MODEL.format(version=locked_release.version)})
                model_id = locked_release.device_model_id
            if old is not None and not _same_scope(old, asset_id=asset_id, model_id=model_id, release_id=release_id, **keyed):
                raise ValidationError({"supersedes": SUPERSEDES_OTHER.format(number=old.number)})
            number = _number(decided)  # 5
            device = None
            if kind == rules.DEVICE_SCOPE:  # 6
                held = services._locked_devices([asset_id])
                if not held:
                    raise ValidationError({"asset": services.CHOOSE_DEVICE})
                device = held[0]
                if device.status == AssetStatus.RETIRED:  # read on its row (a retire in flight was waited for)
                    words = RETURNED if device.ownership != Ownership.OWNED else RETIRED
                    raise ValidationError({"asset": words.format(tag=device.tag)})
            live = _live_for(asset_id=asset_id, model_id=model_id, release_id=release_id, **keyed)
            if live is not None and (old is None or live.pk != old.pk):
                raise ValidationError({field: IN_FORCE.format(number=live.number)})
            members = _scope_members(kind, device=device, model_id=model_id, release=locked_release)
            if for_advisory:  # the rows read after the advisory's row (a change of them takes the register first)
                row = _advisory_row(locked_advisory, device.device_model_id if device is not None else model_id)
                read = _advisory_scope_read(kind, members, advisory=locked_advisory, row=row, device=device, model_id=model_id,
                                            release=locked_release, old=old, today=today)
            else:
                read = _scope_read(kind, members, device=device, model_id=model_id, release=locked_release, old=old, today=today)
            if read.refusal is not None:
                raise ValidationError({field: read.refusal.words})
            if for_advisory:
                words = network_refusal(read.governed, today) if rules.NETWORK_REMOVED in control_list else ""
                if words:
                    raise ValidationError({"controls": words})
            else:
                _check_reasons(read.needs, reason_list)
            governed = read.governed
            if plan == DecisionPlan.RETURN_TO_OWNER and not (device is not None and device.ownership != Ownership.OWNED):
                raise ValidationError({"plan": RETURN_ONLY_TEMPORARY})
            if rules.SEGMENTED_CONTROL in control_list:
                profile_row = rules.profile(device.device_model_id if device is not None else model_id)
                if profile_row is not None and profile_row.segmented == Segmented.NONE:
                    raise ValidationError({"controls": SEGMENT_REFUSAL})
            if review is None:
                review = rules.review_default(response, decided, asset=device)
            if old is not None:
                old.ended_on, old.ended_by, old.end_reason = today, by, DecisionEnd.SUPERSEDED
                services._save(old, by, SUPERSEDED_BY.format(number=number))
            row = DeviceSecurityDecision(
                number=number, applies_to=key[0], asset=device, device_model_id=model_id if device is None else None,
                release=locked_release, reasons=reason_list, response=response, controls=control_list, controls_reference=reference,
                plan=plan, plan_by=plan_day, plan_owner=owner, accepted_by=accepted, exception_reference=exception, recorded_by=by,
                decided_on=decided, review_by=review, governed_ids=[str(a.pk) for a in governed], supersedes=old,
                advisory=locked_advisory, advisory_changes=locked_advisory.changes if locked_advisory is not None else None)
            with transaction.atomic():
                services._save(row, by, RECORDED_SUPERSEDING.format(number=old.number) if old is not None else RECORDED)
    except IntegrityError:  # the live-scope constraint, or a deferred key at COMMIT (a device deleted meanwhile)
        raise ValidationError({field: RACE}) from None
    except OperationalError as e:
        if not services._lock_conflict(e):
            raise
        raise ValidationError({field: RACE}) from None
    if old is not None:
        services._sync(supersedes, old)
    return row


# What a refusal of the scope means (Refusal.kind; review 39 skeptics S3): KIND_IN_FORCE a live decision is in force for it (the doors
# offer Review and renew for that one); KIND_ENDED the decision to renew has ended; KIND_GONE no device of the scope is on site (retired,
# returned, none of ours); KIND_NOT_NEEDED none it would govern needs a decision as recorded (not connected here, the manufacturer's
# updates continuing, not past its support, or each under a more specific decision: skeptics T0); KIND_UNKNOWN what a device's need, or
# a reason of the decision being renewed, turns on is not recorded (rules.need_unknown: record it first; skeptics T2). End answers a
# decision being renewed only for GONE and NOT_NEEDED (END_ANSWERS), never for a fact not recorded. Slice 40: KIND_ADVISORY the advisory
# itself refuses a decision (marked not applicable, or entered in error: reopen it, or nothing); an advisory's scope is never UNKNOWN.
KIND_IN_FORCE, KIND_ENDED, KIND_GONE, KIND_NOT_NEEDED, KIND_UNKNOWN = "in_force", "ended", "gone", "not_needed", "unknown"
KIND_ADVISORY = "advisory"
END_ANSWERS = (KIND_GONE, KIND_NOT_NEEDED)


class Refusal(NamedTuple):
    """Why record_decision would refuse a scope now (refusal_for): its words, what it means (`kind`: KIND_*), and the live decision in
    force for the scope when that is why (the door offers Review and renew for it)."""

    words: str
    kind: str
    live: object = None

    @property
    def ends_it(self) -> bool:
        """End answers it (for the decision being renewed): nothing in the scope needs a decision now, as recorded."""
        return self.kind in END_ANSWERS


def refusal_for(*, asset=None, device_model=None, release=None, supersedes=None, today: date | None = None,
                applies_to=AppliesTo.SUPPORT_ENDED, advisory=None) -> Refusal | None:
    """Why record_decision would refuse this scope now, in its words and its order, read before the person tries (the Record decision
    modal, a decision's page), without its locks: the decision to renew already ended; a device retired or returned; a live decision in
    force for the scope; a release or model with no device of ours on site; nothing it would govern past its support or announced within
    12 months (a device not connected here, its connection not recorded, the manufacturer's updates continuing, nothing recording its
    security updates' end, or, for the decision renewed, the fact one of its reasons rests on; each device under a more specific
    decision). Each with its kind (Refusal.kind). None when the scope itself would not be refused (the fields are the service's to
    check). record_decision decides again under its locks.
    Slice 40: with `applies_to` AppliesTo.ADVISORY and `advisory` (an instance, its id, or its number; read without a lock), after the
    decision to renew: the advisory marked not applicable or entered in error (KIND_ADVISORY), then as above for that advisory's need
    (_advisory_scope_read: never UNKNOWN). An advisory with support is refused keyed advisory (ADVISORY_NOT_FOR_SUPPORT)."""
    today = services._today(today)
    for_advisory = applies_to == AppliesTo.ADVISORY
    if not for_advisory and advisory is not None:
        raise ValidationError({"advisory": ADVISORY_NOT_FOR_SUPPORT})
    old = _decision_row(supersedes, "supersedes", lock=False) if supersedes is not None else None
    if old is not None and old.ended_on is not None:
        return Refusal(ALREADY_ENDED.format(number=old.number, day=rules.day_words(old.ended_on)), KIND_ENDED)
    found = None
    if for_advisory:
        found = services._locked_advisory(advisory, lock=False)
        words = _advisory_refusal(found)
        if words:
            return Refusal(words, KIND_ADVISORY)
    keyed = {"applies_to": applies_to, "advisory": found}
    device = release_row = None
    model_id = services._model(device_model).pk if device_model is not None else None
    if asset is not None:
        kind, device = rules.DEVICE_SCOPE, Asset.objects.get(pk=services._pk(asset, Asset))
        if device.status == AssetStatus.RETIRED:
            return Refusal((RETURNED if device.ownership != Ownership.OWNED else RETIRED).format(tag=device.tag), KIND_GONE)
    elif release is not None:
        kind, release_row = rules.RELEASE_SCOPE, SoftwareRelease.objects.get(pk=services._pk(release, SoftwareRelease))
        model_id = release_row.device_model_id
    else:
        kind = rules.MODEL_SCOPE
    live = _live_for(asset_id=device.pk if device is not None else None, model_id=model_id,
                     release_id=release_row.pk if release_row is not None else None, **keyed)
    if live is not None and (old is None or live.pk != old.pk):
        return Refusal(IN_FORCE.format(number=live.number), KIND_IN_FORCE, live)
    members = _scope_members(kind, device=device, model_id=model_id, release=release_row)
    if for_advisory:
        row = _advisory_row(found, device.device_model_id if device is not None else model_id)
        return _advisory_scope_read(kind, members, advisory=found, row=row, device=device, model_id=model_id, release=release_row, old=old,
                                    today=today).refusal
    return _scope_read(kind, members, device=device, model_id=model_id, release=release_row, old=old, today=today).refusal


def _scope_members(kind: str, *, device, model_id, release) -> list:
    """The devices on site the scope holds now (rules.scope_devices), with their rows read: the device itself (its row as locked, a
    device scope); a release's or a model's devices of ours. Ordered by tag. One query (none for a device scope)."""
    if kind == rules.DEVICE_SCOPE:
        return [device]
    scope = rules.scope_devices(device_model=model_id, release=release)
    return list(scope.select_related(*rules._WITH_RECORD).order_by("tag"))


def _would_govern(kind: str, members: list, *, model_id, release, old, key: tuple = rules.SUPPORT_KEY) -> tuple[list, dict]:
    """(the scope's devices on site a decision for it governs recorded now, {device id: the more specific live decision governing it
    instead} for the others): rules' governing over them, with the new decision among the live ones of its need key (`key`: support by
    default; slice 40, an advisory's) in place of the one it supersedes (`old`). A device scope: its device. A release's devices of ours
    but those under their own live decision; a model's devices of ours but those under their own or their recorded release's. What the
    decision is read from (its needs, its reasons, what holds now: skeptics T0) and what it stores (governed_ids: a device that comes
    under it later, when the more specific decision ends, reads "joined since"; review 39 F1). One query (none for a device scope or no
    device)."""
    if kind == rules.DEVICE_SCOPE or not members:
        return list(members), {}
    new = DeviceSecurityDecision(applies_to=key[0], advisory_id=key[1], device_model_id=model_id, release=release)
    live = [d for d in rules._decisions_for(members, key) if old is None or d.pk != old.pk]
    governing = rules._governing(members, [*live, new])
    return ([a for a in members if governing.get(a.pk) is new],
            {a.pk: governing[a.pk] for a in members if governing.get(a.pk) is not None and governing[a.pk] is not new})


def would_govern(members: list, *, asset=None, device_model=None, release=None, old=None, applies_to=AppliesTo.SUPPORT_ENDED,
                 advisory=None) -> list:
    """The devices of `members` (the scope's devices on site, rules.scope_devices, their rows read: rules.standings' assets) that a
    decision for this scope (`asset`; `release`; or `device_model`), recorded now in place of `old` (Review and renew), would govern
    (_would_govern): never one a more specific live decision of its need key governs (support by default; slice 40, `applies_to`
    AppliesTo.ADVISORY with `advisory`: an instance, its id, or its number, read without a lock). Record decision's modal reads its
    reasons and what holds now from them, as record_decision does (skeptics T0). One query (none for a device scope or no device; one more
    for an advisory given by its number)."""
    members = list(members)
    if asset is not None or not members:
        return members
    found = advisory
    if applies_to == AppliesTo.ADVISORY and not isinstance(advisory, (SecurityAdvisory, uuid.UUID)):
        found = services._locked_advisory(advisory, lock=False)
    kind = rules.RELEASE_SCOPE if release is not None else rules.MODEL_SCOPE
    return _would_govern(kind, members, model_id=members[0].device_model_id, release=release, old=old, key=_key(applies_to, found))[0]


def _device_refusal(a, s, need, reasons=None) -> Refusal:
    """Why one device on site with no decidable need has none, in words with its kind: its connection not recorded (UNKNOWN), not
    connected here (NOT_NEEDED), the manufacturer's updates continuing (EXTENDED: NOT_NEEDED), nothing recording the end of its security
    updates (its release's, or with no release recorded: UNKNOWN; rules.need_unknown), or, for a decision being renewed (`reasons`, its
    kinds), the fact one of them rests on (the end of the security updates; the OS vendor's support: UNKNOWN; skeptics T2), nothing past
    support or announced within 12 months as recorded (NOT_NEEDED)."""
    if s.connection.key == rules.CONNECTION_NOT_RECORDED:
        return Refusal(CONNECTION_UNKNOWN.format(tag=a.tag), KIND_UNKNOWN)
    if s.connection.key not in rules.ON_SITE_KEYS:
        return Refusal(NOT_CONNECTED.format(tag=a.tag), KIND_NOT_NEEDED)
    if need is not None and need.key == rules.EXTENDED:
        return Refusal(EXTENDED.format(name=a.tag, day=rules.day_words(need.day)), KIND_NOT_NEEDED)
    if rules.need_unknown(s, reasons):  # connected or able to: what its need, or the decision's reason, turns on is not recorded
        if s.security.key == rules.SECURITY_NOT_RECORDED:
            words = SECURITY_END_NOT_RECORDED if s.release is not None else NO_RELEASE_RECORDED
        else:  # the OS vendor's support, a reason of the decision being renewed
            words = OS_SUPPORT_NOT_RECORDED if s.release is not None else NO_RELEASE_OS
        return Refusal(words.format(tag=a.tag, version=getattr(s.release, "version", "")), KIND_UNKNOWN)
    return Refusal(NOT_NEEDED.format(name=a.tag), KIND_NOT_NEEDED)


class _ScopeRead(NamedTuple):
    governed: list  # the scope's devices on site a decision for it would govern (_would_govern), in tag order
    needs: list  # their decidable needs (rules.support_need), in the same order; an advisory's: their lines needing a response now
    refusal: Refusal | None  # why it has none, or None


def _numbers(decisions) -> str:
    return ", ".join(sorted({d.number for d in decisions}))


def _scope_read(kind: str, members: list, *, device, model_id, release, old, today: date) -> _ScopeRead:
    """A scope read for record_decision and refusal_for (_ScopeRead): the devices a decision for it would govern recorded now in place of
    `old` (_would_govern: never one a more specific live decision governs; skeptics T0), their decidable needs (the reasons offered and
    required are theirs), and why it has none: a release or model with no device of ours on site (GONE); a device's own
    (_device_refusal, with the reasons of `old`); every device of ours in a release's or a model's scope under a more specific decision
    (GOVERNED_ELSEWHERE: NOT_NEEDED); of those it would govern, one whose need, or a reason of `old`, is not recorded (UNKNOWN: the first
    such one's words, in tag order, after how many and which when there are more: SCOPE_NOT_RECORDED), else some whose manufacturer's
    updates continue (EXTENDED, worded for those with the earliest announced end: skeptics S7), else devices in the scope with a need
    under a more specific decision (NEEDED_ELSEWHERE), else none both connected (or able to connect) and past support or announced
    (NONE_NEEDED). Fixed queries (rules.standings, _would_govern)."""
    if not members:
        if kind == rules.RELEASE_SCOPE:
            return _ScopeRead([], [], Refusal(NO_DEVICE_OURS.format(version=release.version), KIND_GONE))
        model = DeviceModel.objects.filter(pk=model_id).first()
        return _ScopeRead([], [], Refusal(NO_MODEL_DEVICE.format(model=model.model if model else "device of this model"), KIND_GONE))
    renewing = old.reasons if old is not None else None
    governed, elsewhere = _would_govern(kind, members, model_id=model_id, release=release, old=old)
    standings = rules.standings(members, today)
    needs = {a.pk: rules.support_need(standings[a.pk], today) for a in members}
    decidable = [needs[a.pk] for a in governed if needs[a.pk] is not None and needs[a.pk].decidable]
    if decidable:
        return _ScopeRead(governed, decidable, None)
    if kind == rules.DEVICE_SCOPE:
        return _ScopeRead(governed, [], _device_refusal(device, standings[device.pk], needs[device.pk], renewing))
    if not governed:
        return _ScopeRead([], [], Refusal(GOVERNED_ELSEWHERE.format(numbers=_numbers(elsewhere.values())), KIND_NOT_NEEDED))
    unknown = [a for a in governed if rules.need_unknown(standings[a.pk], renewing)]
    if unknown:
        words = _device_refusal(unknown[0], standings[unknown[0].pk], needs[unknown[0].pk], renewing).words
        if len(unknown) > 1:
            words = SCOPE_NOT_RECORDED.format(count=f"{len(unknown)} devices", tags=rules._tags(a.tag for a in unknown), words=words)
        return _ScopeRead(governed, [], Refusal(words, KIND_UNKNOWN))
    extended = [a for a in governed if needs[a.pk] is not None and needs[a.pk].key == rules.EXTENDED]
    if extended:
        day = min(needs[a.pk].day for a in extended)
        first = [a.tag for a in extended if needs[a.pk].day == day]
        return _ScopeRead(governed, [], Refusal(EXTENDED.format(name=rules._tags(first), day=rules.day_words(day)), KIND_NOT_NEEDED))
    needed = [d for pk, d in elsewhere.items() if needs[pk] is not None and needs[pk].decidable]
    if needed:
        words = NEEDED_ELSEWHERE.format(tags=rules._tags(a.tag for a in governed), numbers=_numbers(needed))
        return _ScopeRead(governed, [], Refusal(words, KIND_NOT_NEEDED))
    return _ScopeRead(governed, [], Refusal(NONE_NEEDED, KIND_NOT_NEEDED))


def _model_words(model_id) -> str:
    model = DeviceModel.objects.filter(pk=model_id).first()
    return model.model if model is not None else "device of this model"


def _advisory_device_refusal(device, line, updates: dict, number: str) -> Refusal:
    """Why one device needs no response to the advisory now (NOT_NEEDED, its line under the advisory's row): on the release the advisory
    names as the fix (ON_FIX), on a release it does not name (NOT_NAMED), or with the manufacturer's patches recorded that answer it (the
    list the advisory names: PATCHED_LIST; with none named, since it was received: PATCHED)."""
    version = line.release.version if line.release is not None else ""
    if line.kind == rules.ON_FIX:
        return Refusal(ON_FIX.format(tag=device.tag, version=version, number=number), KIND_NOT_NEEDED)
    if line.kind == rules.OTHER_RELEASE:
        return Refusal(NOT_NAMED.format(tag=device.tag, version=version, number=number), KIND_NOT_NEEDED)
    found = updates.get((device.pk, line.row.pk))
    if found is not None and found.patched is not None:
        patched = found.patched
        day = f" {rules.day_words(patched.applied_on)}" if patched.applied_on else ""
        if line.row.patches_reference:
            return Refusal(PATCHED_LIST.format(tag=device.tag, day=day, reference=rules._patches_reference(patched), number=number),
                           KIND_NOT_NEEDED)
        return Refusal(PATCHED.format(tag=device.tag, day=day, number=number), KIND_NOT_NEEDED)
    return Refusal(NONE_NEEDS.format(number=number), KIND_NOT_NEEDED)


def _advisory_scope_read(kind: str, members: list, *, advisory, row, device, model_id, release, old, today: date) -> _ScopeRead:
    """The scope read of a decision for the devices an advisory names (critique40_scope B3), for record_decision (under its locks, the
    advisory's rows read after its row) and refusal_for (without locks), in this order: the devices a decision for it would govern
    recorded now in place of `old` (_would_govern over the advisory's need key: never one a more specific live decision of the advisory
    governs), their lines that need a response now (`needs`: rules.AdvisoryLine; rules.needs_now: the line names the device and no patches
    record answers it), and why there is none:
    - GONE: a release or model with no device of ours on site (slice 39's NO_DEVICE_OURS, NO_MODEL_DEVICE; a device retired or returned
      is refused before, as slice 39's);
    - NOT_NEEDED: the advisory names no row for the scope's model (MODEL_NOT_NAMED; a device's: DEVICE_MODEL_NOT_NAMED); a release it
      names as the fix (RELEASE_IS_FIX) or, listing its releases, does not name (RELEASE_NOT_NAMED); a device on the fix (ON_FIX), on a
      release it does not name (NOT_NAMED), or with the patches recorded that answer it (PATCHED, PATCHED_LIST); every device of ours in
      a release's or a model's scope under a more specific decision of the advisory (GOVERNED_ELSEWHERE); devices in the scope that need
      a response only under a more specific decision (ADVISORY_NEEDED_ELSEWHERE); none needing a response now (NONE_NEEDS).
    Never UNKNOWN: a release not recorded is a need. A device with an open update to the fix still needs a response (interim controls).
    `row`: the advisory's row naming the scope's model (rules.rows_of's), or None. Fixed queries (rules.standings, _would_govern, the
    updates recorded on the lines: rules.line_updates)."""
    number = advisory.number
    if not members:
        if kind == rules.RELEASE_SCOPE:
            return _ScopeRead([], [], Refusal(NO_DEVICE_OURS.format(version=release.version), KIND_GONE))
        return _ScopeRead([], [], Refusal(NO_MODEL_DEVICE.format(model=_model_words(model_id)), KIND_GONE))
    if row is None:
        if kind == rules.DEVICE_SCOPE:
            words = DEVICE_MODEL_NOT_NAMED.format(number=number, tag=device.tag, model=_model_words(device.device_model_id))
        else:
            words = MODEL_NOT_NAMED.format(number=number, model=_model_words(model_id))
        return _ScopeRead([], [], Refusal(words, KIND_NOT_NEEDED))
    if kind == rules.RELEASE_SCOPE:
        if release.pk == row.fix_release_id:
            return _ScopeRead([], [], Refusal(RELEASE_IS_FIX.format(version=release.version, number=number), KIND_NOT_NEEDED))
        if row.versions == AdvisoryVersions.LISTED and release.pk not in row.listed_ids:
            return _ScopeRead([], [], Refusal(RELEASE_NOT_NAMED.format(number=number, version=release.version), KIND_NOT_NEEDED))
    governed, elsewhere = _would_govern(kind, members, model_id=model_id, release=release, old=old, key=rules.advisory_key(advisory))
    held = rules.standings(members, today)
    lines = {pk: line for (pk, _advisory), line in rules.lines_for(members, held, [row]).items()}
    updates = rules.line_updates(lines.values())
    needing = [lines[a.pk] for a in governed if rules.needs_now(lines.get(a.pk), updates)]
    if needing:
        return _ScopeRead(governed, needing, None)
    if kind == rules.DEVICE_SCOPE:
        return _ScopeRead(governed, [], _advisory_device_refusal(device, lines[device.pk], updates, number))
    if not governed:
        return _ScopeRead([], [], Refusal(GOVERNED_ELSEWHERE.format(numbers=_numbers(elsewhere.values())), KIND_NOT_NEEDED))
    needed = [d for pk, d in elsewhere.items() if rules.needs_now(lines.get(pk), updates)]
    if needed:
        words = ADVISORY_NEEDED_ELSEWHERE.format(tags=rules._tags(a.tag for a in governed), number=number, numbers=_numbers(needed))
        return _ScopeRead(governed, [], Refusal(words, KIND_NOT_NEEDED))
    return _ScopeRead(governed, [], Refusal(NONE_NEEDS.format(number=number), KIND_NOT_NEEDED))


def _check_reasons(needs: list, given: list) -> None:
    """The reasons given hold now for the scope (ended, or announced to end within 12 months, on a device the decision would govern:
    `needs` are _scope_read's, never a device a more specific live decision governs; skeptics T0), and every one that ended is named
    (REASONS_NOW, keyed reasons)."""
    holding = {kind for n in needs for kind in n.reasons}
    ended = {kind for n in needs for kind in n.ended}
    if set(given) - holding or ended - set(given):
        words = "; ".join(_lower(rules.REASONS[k]) for k in rules.REASONS if k in holding)
        raise ValidationError({"reasons": REASONS_NOW.format(words=words)})


def _lower(text: str) -> str:
    return text[:1].lower() + text[1:]


# --- end, entered in error -----------------------------------------------------------------------------------------------------------

def _end(decision, *, reason: str, history: str, by, today: date | None):
    today = services._today(today)
    try:
        with transaction.atomic():
            services.lock_register()  # 1
            locked = _decision_row(decision)  # 2
            if locked.ended_on is not None:
                raise ValidationError({"decision": ALREADY_ENDED.format(number=locked.number, day=rules.day_words(locked.ended_on))})
            locked.ended_on, locked.ended_by, locked.end_reason = today, by, reason
            services._save(locked, by, history)
    except OperationalError as e:
        if not services._lock_conflict(e):
            raise
        raise ValidationError({"decision": RACE}) from None
    if isinstance(decision, DeviceSecurityDecision):
        services._sync(decision, locked)
    return locked


def end_decision(decision, *, reason: str = DecisionEnd.NO_LONGER_NEEDED, by, today: date | None = None) -> DeviceSecurityDecision:
    """End a live decision as no longer needed (its devices updated, replaced, retired, or returned, or each under a decision of their
    own): ended_on today, ended_by, end_reason; history "Ended: no longer needed". Superseded only by record_decision, entered in error
    only by mark_entered_in_error (END_REASON_WORDS, keyed end_reason); an ended one is refused (ALREADY_ENDED). Locks: the register,
    the decision's own row. Returns the row (the caller's copy follows it)."""
    services._check(by, perms.can_decide, END_PERMISSION)
    if reason != DecisionEnd.NO_LONGER_NEEDED:
        raise ValidationError({"end_reason": END_REASON_WORDS})
    return _end(decision, reason=DecisionEnd.NO_LONGER_NEEDED, history=ENDED_NO_LONGER_NEEDED, by=by, today=today)


def mark_entered_in_error(decision, *, by, today: date | None = None) -> DeviceSecurityDecision:
    """Mark a live decision entered in error: ended_on today, ended_by, end_reason entered_in_error; history "Entered in error". Never
    deleted (its history keeps it); an ended one is refused (ALREADY_ENDED). Locks: the register, the decision's own row."""
    services._check(by, perms.can_decide, END_PERMISSION)
    return _end(decision, reason=DecisionEnd.ENTERED_IN_ERROR, history=ENTERED_IN_ERROR, by=by, today=today)
