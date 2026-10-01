"""
PM procedures (slice 14): writing and revising a procedure (code, name, source, revision, estimated hours, checklist) and
choosing a device model's procedure. Outside the demo seed and the admin, the only code that writes PmProcedure rows.

The rules, the same on create and on change (update_procedure checks only the fields it is given):
- code: required, trimmed, at most 40 characters of letters, digits, and - . _ / (no spaces), starting with a letter or digit;
  unique in the facility in any letter case (two facilities may share a code). A code that does not change is not checked again,
  so one saved before these rules (admin, seed) never blocks an edit of the rest.
- name: required, one line, at most 200 characters. source_reference: one line, at most 200. revision: one line, at most 40.
- source_kind: one of PmProcedure.SourceKind.
- source_url: blank, or a complete http or https address (it becomes a link in the drawer), at most 200 characters.
- estimated_hours: 0.1 to 40 with at most two decimal places. More precise input is refused, not rounded (as Settings does for
  its targets), so what was typed is what plans the PMs.
- checklist: 1 to 60 steps, in order. A step is its text (one line, at most 300 characters, no "|"), or {"text", "measure"} when the
  technician records a reading: `measure` says what to record (at most 100 characters, e.g. "µA, limit 100"), or is True for a
  reading without saying what. Steps are stored in that form: a plain string unless something is recorded.

The editor is a textarea, one step per line (LINE_FORMAT): parse_checklist reads it and checklist_text writes it, and the two
round-trip every checklist these rules accept.

A procedure is shared. Every model using it plans its PM hours from it (schedule.pm_hours) and prints its checklist on its PM work
orders; the print reads the model's procedure when it is printed, so open PM work orders print a change too. A PM work order keeps
the hours estimated when it was created. Each change is in the procedure's (or the model's) history with a reason.
"""
import re
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import IntegrityError, transaction

from apps.equipment import services as eq
from apps.equipment.models import DeviceModel
from apps.tenants.context import get_current_tenant

from .models import PmProcedure

CODE_MAX = PmProcedure._meta.get_field("code").max_length
NAME_MAX = PmProcedure._meta.get_field("name").max_length
REFERENCE_MAX = PmProcedure._meta.get_field("source_reference").max_length
REVISION_MAX = PmProcedure._meta.get_field("revision").max_length
URL_MAX = PmProcedure._meta.get_field("source_url").max_length
HOURS_PLACES = PmProcedure._meta.get_field("estimated_hours").decimal_places
HOURS_MIN, HOURS_MAX = Decimal("0.1"), Decimal("40")
STEPS_MAX = 60
STEP_MAX = 300
MEASURE_MAX = 100
SEPARATOR = "|"
LINE_FORMAT = (f"One step per line, in order (up to {STEPS_MAX}). For a step that records a reading, add {SEPARATOR} and what to record, "
               f"e.g. Ground resistance {SEPARATOR} Ω, limit 0.3. A {SEPARATOR} with nothing after it leaves a blank for the reading.")

EDITABLE = ("code", "name", "source_kind", "source_reference", "source_url", "revision", "estimated_hours", "checklist")
# What the history's change reason calls each field ("Edited: checklist, hours").
LABELS = {"code": "code", "name": "name", "source_kind": "source", "source_reference": "reference", "source_url": "link", "revision": "revision",
          "estimated_hours": "hours", "checklist": "checklist"}
_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
_URL = URLValidator(schemes=["http", "https"])
_CONTROL = "Remove the invisible control character from this text."
_STEP_CONTROL = "remove the invisible control character from this step."


# --- the checklist's line format ---------------------------------------------------------------------------------------------

def step_parts(step) -> tuple[str, str | bool | None]:
    """(text, measure) of a stored step, whatever shape it has: measure is None (nothing recorded), True (a reading, unspecified),
    or what to record. A dict without "text" gives its other values (as the print does), so nothing on file is hidden."""
    if not isinstance(step, dict):
        return str(step), None
    measure = step.get("measure")
    text = step.get("text")
    if text in (None, ""):
        text = "; ".join(str(v) for k, v in step.items() if k != "measure" and v not in (None, ""))
    if measure is None or measure is False or measure == "":
        return str(text), None
    return str(text), True if measure is True else str(measure)


def _steps_of(checklist) -> list:
    if checklist in (None, "", [], {}):
        return []
    if isinstance(checklist, str):  # a checklist saved as one string: a step per line
        return [line.strip() for line in checklist.splitlines() if line.strip()]
    return list(checklist) if isinstance(checklist, (list, tuple)) else [checklist]


def checklist_text(checklist) -> str:
    """The editor's text for a stored checklist: one line per step, "text | what to record" for a reading."""
    lines = []
    for step in _steps_of(checklist):
        text, measure = step_parts(step)
        text = " ".join(text.split())
        lines.append(text if measure is None else f"{text} {SEPARATOR}" if measure is True else f"{text} {SEPARATOR} {' '.join(measure.split())}")
    return "\n".join(lines)


def parse_checklist(text) -> list:
    """The editor's text as steps, before validation: one step per non-blank line, spaces collapsed. "text | measure" splits at
    the first |; "text |" is a reading without saying what (True)."""
    steps = []
    for line in str(text or "").splitlines():
        if not line.strip():
            continue
        head, sep, tail = line.partition(SEPARATOR)
        step_text, measure = " ".join(head.split()), " ".join(tail.split())
        steps.append({"text": step_text, "measure": measure or True} if sep else step_text)
    return steps


# --- checks ----------------------------------------------------------------------------------------------------------------------

def _line(value, field: str, errors: dict, *, limit: int, required: str = "", label: str = "it") -> str:
    """One line of text, stray whitespace collapsed. Too long is refused, not cut, so what was typed is what is saved."""
    text = " ".join(str(value if value is not None else "").split())
    if "\x00" in text:
        errors[field] = _CONTROL
    elif not text and required:
        errors[field] = required
    elif len(text) > limit:
        errors[field] = f"Keep {label} to {limit} characters."
    return text


def _code(value, errors: dict) -> str:
    code = str(value if value is not None else "").strip()
    if not code:
        errors["code"] = "Enter a short code for the procedure, e.g. HA-G5-PM6."
    elif any(c.isspace() for c in code):
        errors["code"] = "A code has no spaces. Use hyphens, e.g. HA-G5-PM6."
    elif len(code) > CODE_MAX:
        errors["code"] = f"Keep the code to {CODE_MAX} characters."
    elif not _CODE.fullmatch(code):
        errors["code"] = "Use letters, digits, and - . _ / only, starting with a letter or digit."
    return code


def _code_taken(code: str, exclude=None) -> str:
    """The error when another procedure in this facility has `code` in any letter case, else ""."""
    qs = PmProcedure.objects.filter(code__iexact=code)
    if exclude is not None:
        qs = qs.exclude(pk=exclude.pk)
    return f"{code} is already the code of another procedure here." if qs.exists() else ""


def _url(value, errors: dict) -> str:
    url = str(value if value is not None else "").strip()
    if not url:
        return ""
    if len(url) > URL_MAX:
        errors["source_url"] = f"Keep the link to {URL_MAX} characters."
    elif not url.lower().startswith(("https://", "http://")):
        errors["source_url"] = "Enter a web address that starts with https:// (or http://)."
    else:
        try:
            _URL(url)
        except ValidationError:
            errors["source_url"] = "Enter a complete web address, e.g. https://example.com/service-manual.pdf."
    return url


def _hours(value, errors: dict):
    if value is None or str(value).strip() == "":
        errors["estimated_hours"] = "Enter the estimated hours per PM, e.g. 1.5."
        return None
    try:
        d = Decimal(str(value).strip()) if not isinstance(value, bool) else None
    except (InvalidOperation, ValueError):
        d = None
    if d is None or not d.is_finite():
        errors["estimated_hours"] = "Enter the hours as a number, e.g. 1.5."
        return None
    exponent = d.normalize().as_tuple().exponent
    if isinstance(exponent, int) and -exponent > HOURS_PLACES:
        errors["estimated_hours"] = f"Use at most {HOURS_PLACES} decimal places."
        return None
    if not HOURS_MIN <= d <= HOURS_MAX:
        errors["estimated_hours"] = f"Estimated hours are {HOURS_MIN} to {HOURS_MAX}."
        return None
    return d + 0  # drops a negative zero's sign


def _step(step) -> tuple[str, object]:
    """(problem, the step as stored). problem is "" when the step is fine."""
    if isinstance(step, str):
        text, measure = step, None
    elif isinstance(step, dict):
        if set(step) - {"text", "measure"}:
            return "a step has its text and, for a reading, what to record.", None
        text, measure = step.get("text"), step.get("measure")
    else:
        return "write each step as a line of text.", None
    if not isinstance(text, str):
        return "write each step as a line of text.", None
    text = " ".join(text.split())
    if not text:
        return f"write the step before the {SEPARATOR}." if measure not in (None, False, "") else "write the step.", None
    if "\x00" in text:
        return _STEP_CONTROL, None
    if SEPARATOR in text:
        return f"a step cannot contain {SEPARATOR}, which separates the step from what to record.", None
    if len(text) > STEP_MAX:
        return f"keep each step to {STEP_MAX} characters.", None
    if measure is None or measure is False:
        return "", text
    if measure is True:
        return "", {"text": text, "measure": True}
    if isinstance(measure, (int, float, Decimal)):
        measure = str(measure)
    if not isinstance(measure, str):
        return "say what to record as text, e.g. µA, limit 100.", None
    measure = " ".join(measure.split())
    if not measure:
        return "", text
    if "\x00" in measure:
        return _STEP_CONTROL, None
    if len(measure) > MEASURE_MAX:
        return f"keep what to record to {MEASURE_MAX} characters.", None
    return "", {"text": text, "measure": measure}


def _checklist(value, errors: dict) -> list:
    steps = parse_checklist(value) if isinstance(value, str) else value
    if not isinstance(steps, (list, tuple)):
        errors["checklist"] = "Write the checklist one step per line."
        return []
    out = []
    for n, step in enumerate(steps, 1):
        problem, clean = _step(step)
        if problem:
            errors["checklist"] = f"Step {n}: {problem}"
            return []
        out.append(clean)
    if not out:
        errors["checklist"] = "Add at least one step, one per line."
    elif len(out) > STEPS_MAX:
        errors["checklist"] = f"Keep the checklist to {STEPS_MAX} steps (this one has {len(out)})."
    return out


def _clean(raw: dict, current: PmProcedure | None = None) -> dict:
    """Check and normalize the fields in `raw` (form order). Raises one ValidationError with every problem, keyed by field."""
    errors, out = {}, {}
    for field, value in raw.items():
        if field == "code":
            code = str(value if value is not None else "").strip()
            if current is not None and code == current.code:
                out["code"] = code
                continue
            out["code"] = _code(value, errors)
            if "code" not in errors and (taken := _code_taken(out["code"], exclude=current)):
                errors["code"] = taken
        elif field == "name":
            out["name"] = _line(value, field, errors, limit=NAME_MAX, required="Enter the procedure's name.", label="the name")
        elif field == "source_kind":
            if value not in PmProcedure.SourceKind.values:
                errors[field] = "Choose where the procedure comes from."
            out[field] = value
        elif field == "source_reference":
            out[field] = _line(value, field, errors, limit=REFERENCE_MAX, label="the reference")
        elif field == "revision":
            out[field] = _line(value, field, errors, limit=REVISION_MAX, label="the revision")
        elif field == "source_url":
            out[field] = _url(value, errors)
        elif field == "estimated_hours":
            out[field] = _hours(value, errors)
        elif field == "checklist":
            out[field] = _checklist(value, errors)
    if errors:
        raise ValidationError(errors)
    return out


def _check_tenant(obj, field: str, message: str) -> None:
    tenant = get_current_tenant()
    if obj is None or tenant is None or obj.tenant_id != tenant.id:
        raise ValidationError({field: message})


def _save(procedure: PmProcedure, by=None) -> None:
    """Save, with the code's uniqueness held by the database too: two people saving the same code at once get the form's error."""
    if by is not None:
        procedure._history_user = by
    try:
        with transaction.atomic():
            procedure.save()
    except IntegrityError:
        raise ValidationError({"code": f"{procedure.code} is already the code of another procedure here."})
    finally:  # simple_history reads these on every save; never let them reach a later, unrelated one
        procedure.__dict__.pop("_change_reason", None)
        procedure.__dict__.pop("_history_user", None)


# --- writing --------------------------------------------------------------------------------------------------------------------

def create_procedure(*, code, name, source_kind, source_reference="", source_url="", revision="", estimated_hours, checklist, by=None) -> PmProcedure:
    """Add a procedure to the facility's library. `checklist` is a list of steps, or the editor's text (LINE_FORMAT)."""
    fields = _clean({"code": code, "name": name, "source_kind": source_kind, "revision": revision, "source_reference": source_reference,
                     "source_url": source_url, "estimated_hours": estimated_hours, "checklist": checklist})
    procedure = PmProcedure(**fields)
    procedure._change_reason = "Added"
    _save(procedure, by)
    return procedure


def update_procedure(procedure: PmProcedure, *, by=None, **fields) -> PmProcedure:
    """Change a procedure with create_procedure's rules; only the fields given are checked, and only what differs is saved. The
    change applies to every model using the procedure (models_using)."""
    unknown = set(fields) - set(EDITABLE)
    if unknown:
        raise ValidationError(f"These cannot be changed here: {', '.join(sorted(unknown))}.")
    _check_tenant(procedure, "procedure", "Choose a procedure from this facility.")
    cleaned = _clean(fields, current=procedure)
    changed = [f for f in EDITABLE if f in cleaned and getattr(procedure, f) != cleaned[f]]
    if not changed:
        return procedure
    for f in changed:
        setattr(procedure, f, cleaned[f])
    procedure._change_reason = "Edited: " + ", ".join(LABELS[f] for f in changed)
    try:
        _save(procedure, by)
    except ValidationError:
        procedure.refresh_from_db()  # the object stays as stored, not as refused
        raise
    return procedure


@transaction.atomic
def set_model_procedure(device_model, procedure: PmProcedure | None, *, by=None):
    """Make `procedure` (or none) the model's PM procedure, through equipment.services.update_device_model, which refuses another
    facility's procedure. With none, the model's PMs are estimated at schedule.DEFAULT_PM_HOURS and name no procedure."""
    _check_tenant(device_model, "device_model", "Choose a device model from this facility.")
    if (procedure.pk if procedure else None) != device_model.pm_procedure_id:
        device_model._change_reason = f"PM procedure: {procedure.code}" if procedure else "PM procedure removed"
    if by is not None:
        device_model._history_user = by
    try:
        return eq.update_device_model(device_model, by=by, pm_procedure=procedure)
    finally:
        device_model.__dict__.pop("_change_reason", None)
        device_model.__dict__.pop("_history_user", None)


# --- reading ---------------------------------------------------------------------------------------------------------------------

def models_using(procedure: PmProcedure):
    """The facility's device models whose PM procedure this is, by name."""
    return DeviceModel.objects.filter(pm_procedure=procedure).order_by("manufacturer", "model")
