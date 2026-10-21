"""
Security decisions (slice 39, beyond the mock): every write of apps.cybersecurity.models.DeviceSecurityDecision. HHS 405(d) HICP 2023
recommends documenting mitigating controls and a timeline for resolving the risk, through the organization's risk acceptance or
exception process, when a system past its support stays in use; no rule requires any of it, and Cadence never says one does. IT's
exception stays in IT's system: a decision holds its number (a token), never its text.

- record_decision: a decision for devices past the manufacturer's or the OS vendor's support (or whose end is announced within 12
  months), for one device, a model's devices of ours on one release, or every device of ours of a model; superseding the live decision
  for the same scope (Review and renew: the old one is ended "superseded" in the same transaction, then the new one inserted).
- end_decision: ended as no longer needed (updated, replaced, retired, or returned). A decision is superseded only by recording a new one.
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
(rules.support_need over the scope's devices); the reasons those that hold now (none that does not, every one that ended); return it to
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
"""
from __future__ import annotations

import ipaddress
import re
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
    AcceptedBy,
    AppliesTo,
    DecisionEnd,
    DecisionPlan,
    DecisionResponse,
    DeviceSecurityDecision,
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
NOT_NEEDED = "{name} is not past its support, and no end is announced within 12 months."
EXTENDED = ("{name}: the manufacturer's security updates continue (announced to end {day}): a decision can be recorded once their end is "
            "within 12 months.")
NO_DEVICE_OURS = "No device of ours on site runs {version}."
NO_MODEL_DEVICE = "No {model} of ours is on site."
NONE_NEEDED = "No device of ours on site in this scope is past its support, connected or able to connect, or has an end announced within 12 months."
IN_FORCE = "{number} is in force for this scope: review and renew it instead."
SUPERSEDES_OTHER = "{number} is for another scope: renew a decision for this one."
ALREADY_ENDED = "{number} ended on {day}: it stays as recorded."
END_REASON_WORDS = ("Choose No longer needed: a decision is superseded by recording a new one, and marked entered in error on its own.")
RACE = "This decision's devices or scope were being changed or recorded meanwhile: try again."

# History reasons (fixed sentences, Cadence numbers only)
RECORDED = "Recorded"
RECORDED_SUPERSEDING = "Recorded, superseding {number}"
SUPERSEDED_BY = "Superseded by {number}"
ENDED_NO_LONGER_NEEDED = "Ended: no longer needed"
ENTERED_IN_ERROR = "Entered in error"

_MAC = re.compile(r"^(?:[0-9a-f]{2}[-.:]){5}[0-9a-f]{2}$|^(?:[0-9a-f]{4}\.){2}[0-9a-f]{4}$|^(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{12}$",
                  re.IGNORECASE)
_DOTTED = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,3})?$")
_PORT = re.compile(r"^(?:port(?![a-z])|(?:tcp|udp)[-/_.#]?\d+$|\d+[-/_.#](?:tcp|udp)$)", re.IGNORECASE)


def network_identifier(text: str) -> bool:
    """A token that reads as a network identifier (build39 section 2), refused in every cyber token field this slice adds: an address or
    a block (ipaddress.ip_network(strict=False) parses it, or four dotted numbers), a MAC (six pairs, three dotted fours, or twelve hex
    digits with a letter and a digit), one starting "VLAN" in any case, or a port ("port...", "tcp-443", "443/udp")."""
    text = (text or "").strip()
    if not text:
        return False
    try:
        ipaddress.ip_network(text, strict=False)
        return True
    except ValueError:
        pass
    return bool(_DOTTED.match(text) or _MAC.match(text) or text.casefold().startswith("vlan") or _PORT.match(text))


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


def _same_scope(old, *, asset_id, model_id, release_id) -> bool:
    if old.applies_to != AppliesTo.SUPPORT_ENDED:
        return False
    if asset_id is not None:
        return old.asset_id == asset_id
    return old.asset_id is None and old.device_model_id == model_id and old.release_id == release_id


def _live_for(*, asset_id, model_id, release_id):
    """The live decision for devices past support with this scope, or None (under the register lock)."""
    live = DeviceSecurityDecision.objects.filter(applies_to=AppliesTo.SUPPORT_ENDED, ended_on__isnull=True)
    if asset_id is not None:
        return live.filter(asset_id=asset_id).first()
    if release_id is not None:
        return live.filter(release_id=release_id).first()
    return live.filter(device_model_id=model_id, asset__isnull=True, release__isnull=True).first()


# --- record ------------------------------------------------------------------------------------------------------------------------

def record_decision(*, asset=None, device_model=None, release=None, reasons, response, controls=(), controls_reference="",
                    plan=DecisionPlan.NONE, plan_by=None, plan_owner=None, accepted_by, exception_reference="", decided_on=None,
                    review_by=None, supersedes=None, by, today: date | None = None) -> DeviceSecurityDecision:
    """Record a decision for devices past support (the module docstring's rules and lock order): its scope (`asset`; or `release`, with
    its `device_model` or alone; or `device_model` alone), the reasons that hold now (kinds: rules.REASONS), the response, the controls
    (rules.SUPPORT_CONTROLS slugs) and IT's reference for recorded_elsewhere, the plan with its day and owner, who accepted it, IT's
    exception number, the day decided (today by default), the review (rules.review_default by default), and the decision it supersedes
    (the live one of the same scope: ended "Superseded by <number>" first, in the same transaction). Stores the devices on site it governs
    now (governed_ids: rules' "joined since"). History "Recorded" (", superseding <number>"). Returns the new decision."""
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
    reason_list = _kinds(reasons, rules.REASONS, "reasons", errors, words=REASONS_WORDS)
    if "reasons" not in errors and not reason_list:
        errors["reasons"] = REASONS_NONE
    response = _choice(response, DecisionResponse, "response", RESPONSE_WORDS, errors)
    given_controls = controls if controls is not None else ()
    if not isinstance(given_controls, str) and rules.NETWORK_REMOVED in list(given_controls or ()):
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
            locked_release = None
            if kind == rules.RELEASE_SCOPE:  # 3
                locked_release = services._locked_release(release_id, field="release", message=CHOOSE_RELEASE)
                if not locked_release.version_key or locked_release.removed_on is not None:
                    raise ValidationError({"release": CHOOSE_RELEASE})
                if model_id is not None and locked_release.device_model_id != model_id:
                    raise ValidationError({"release": NOT_OF_MODEL.format(version=locked_release.version)})
                model_id = locked_release.device_model_id
            if old is not None and not _same_scope(old, asset_id=asset_id, model_id=model_id, release_id=release_id):
                raise ValidationError({"supersedes": SUPERSEDES_OTHER.format(number=old.number)})
            number = _number(decided)  # 4
            device = None
            if kind == rules.DEVICE_SCOPE:  # 5
                held = services._locked_devices([asset_id])
                if not held:
                    raise ValidationError({"asset": services.CHOOSE_DEVICE})
                device = held[0]
                if device.status == AssetStatus.RETIRED:  # read on its row (a retire in flight was waited for)
                    words = RETURNED if device.ownership != Ownership.OWNED else RETIRED
                    raise ValidationError({"asset": words.format(tag=device.tag)})
            live = _live_for(asset_id=asset_id, model_id=model_id, release_id=release_id)
            if live is not None and (old is None or live.pk != old.pk):
                raise ValidationError({field: IN_FORCE.format(number=live.number)})
            members = _scope_members(kind, device=device, model_id=model_id, release=locked_release)
            needs = _scope_needs(kind, members, device=device, model_id=model_id, release=locked_release, today=today)
            _check_reasons(needs, reason_list)
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
                number=number, applies_to=AppliesTo.SUPPORT_ENDED, asset=device, device_model_id=model_id if device is None else None,
                release=locked_release, reasons=reason_list, response=response, controls=control_list, controls_reference=reference,
                plan=plan, plan_by=plan_day, plan_owner=owner, accepted_by=accepted, exception_reference=exception, recorded_by=by,
                decided_on=decided, review_by=review, governed_ids=[str(a.pk) for a in members], supersedes=old)
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


class Refusal(NamedTuple):
    """Why record_decision would refuse a scope now (refusal_for): its words, and the live decision in force for the scope when that is
    why (the door offers Review and renew for it)."""

    words: str
    live: object = None


def refusal_for(*, asset=None, device_model=None, release=None, supersedes=None, today: date | None = None) -> Refusal | None:
    """Why record_decision would refuse this scope now, in its words and its order, read before the person tries (the Record decision
    modal), without its locks: the decision to renew already ended; a device retired or returned; a live decision in force for the
    scope; a release or model with no device of ours on site; nothing past its support or announced within 12 months (a device not
    connected here, its connection not recorded, the manufacturer's updates continuing). None when the scope itself would not be refused
    (the fields are the service's to check). record_decision decides again under its locks."""
    today = services._today(today)
    old = _decision_row(supersedes, "supersedes", lock=False) if supersedes is not None else None
    if old is not None and old.ended_on is not None:
        return Refusal(ALREADY_ENDED.format(number=old.number, day=rules.day_words(old.ended_on)))
    device = release_row = None
    model_id = services._model(device_model).pk if device_model is not None else None
    if asset is not None:
        kind, device = rules.DEVICE_SCOPE, Asset.objects.get(pk=services._pk(asset, Asset))
        if device.status == AssetStatus.RETIRED:
            return Refusal((RETURNED if device.ownership != Ownership.OWNED else RETIRED).format(tag=device.tag))
    elif release is not None:
        kind, release_row = rules.RELEASE_SCOPE, SoftwareRelease.objects.get(pk=services._pk(release, SoftwareRelease))
        model_id = release_row.device_model_id
    else:
        kind = rules.MODEL_SCOPE
    live = _live_for(asset_id=device.pk if device is not None else None, model_id=model_id,
                     release_id=release_row.pk if release_row is not None else None)
    if live is not None and (old is None or live.pk != old.pk):
        return Refusal(IN_FORCE.format(number=live.number), live)
    members = _scope_members(kind, device=device, model_id=model_id, release=release_row)
    try:
        _scope_needs(kind, members, device=device, model_id=model_id, release=release_row, today=today)
    except ValidationError as e:
        return Refusal(e.messages[0])
    return None


def _scope_members(kind: str, *, device, model_id, release) -> list:
    """The devices on site the scope holds now (rules.scope_devices), with their rows read: the device itself (its row as locked, a
    device scope); a release's or a model's devices of ours. Ordered by tag. One query (none for a device scope)."""
    if kind == rules.DEVICE_SCOPE:
        return [device]
    scope = rules.scope_devices(device_model=model_id, release=release)
    return list(scope.select_related(*rules._WITH_RECORD).order_by("tag"))


def _scope_needs(kind: str, members: list, *, device, model_id, release, today: date) -> list:
    """The decidable needs of the scope's devices (rules.support_need), refused in words when it has none: a device not connected here,
    its connection not recorded, the manufacturer's updates continuing (EXTENDED), nothing past support or announced within 12 months;
    a release or model with no device of ours on site."""
    if not members:
        if kind == rules.RELEASE_SCOPE:
            raise ValidationError({"release": NO_DEVICE_OURS.format(version=release.version)})
        model = DeviceModel.objects.filter(pk=model_id).first()
        raise ValidationError({"device_model": NO_MODEL_DEVICE.format(model=model.model if model else "device of this model")})
    standings = rules.standings(members, today)
    needs = [rules.support_need(standings[a.pk], today) for a in members]
    decidable = [n for n in needs if n is not None and n.decidable]
    if decidable:
        return decidable
    if kind == rules.DEVICE_SCOPE:
        s, need = standings[device.pk], needs[0]
        if s.connection.key == rules.CONNECTION_NOT_RECORDED:
            raise ValidationError({"asset": CONNECTION_UNKNOWN.format(tag=device.tag)})
        if s.connection.key not in rules.ON_SITE_KEYS:
            raise ValidationError({"asset": NOT_CONNECTED.format(tag=device.tag)})
        if need is not None and need.key == rules.EXTENDED:
            raise ValidationError({"asset": EXTENDED.format(name=device.tag, day=rules.day_words(need.day))})
        raise ValidationError({"asset": NOT_NEEDED.format(name=device.tag)})
    raise ValidationError({_scope_field(kind): NONE_NEEDED})


def _check_reasons(needs: list, given: list) -> None:
    """The reasons given hold now for the scope (ended, or announced to end within 12 months, on a device it holds), and every one that
    ended is named (REASONS_NOW, keyed reasons)."""
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
    """End a live decision as no longer needed (updated, replaced, retired, or returned): ended_on today, ended_by, end_reason; history
    "Ended: no longer needed". Superseded only by record_decision, entered in error only by mark_entered_in_error (END_REASON_WORDS, keyed
    end_reason); an ended one is refused (ALREADY_ENDED). Locks: the register, the decision's own row. Returns the row (the caller's copy
    follows it)."""
    services._check(by, perms.can_decide, END_PERMISSION)
    if reason != DecisionEnd.NO_LONGER_NEEDED:
        raise ValidationError({"end_reason": END_REASON_WORDS})
    return _end(decision, reason=DecisionEnd.NO_LONGER_NEEDED, history=ENDED_NO_LONGER_NEEDED, by=by, today=today)


def mark_entered_in_error(decision, *, by, today: date | None = None) -> DeviceSecurityDecision:
    """Mark a live decision entered in error: ended_on today, ended_by, end_reason entered_in_error; history "Entered in error". Never
    deleted (its history keeps it); an ended one is refused (ALREADY_ENDED). Locks: the register, the decision's own row."""
    services._check(by, perms.can_decide, END_PERMISSION)
    return _end(decision, reason=DecisionEnd.ENTERED_IN_ERROR, history=ENTERED_IN_ERROR, by=by, today=today)
