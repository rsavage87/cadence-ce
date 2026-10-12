"""
Naming the vendor's engineer on a work order (slice 31, apps.vendors): the select the completion modal and the drawer's Change share,
the drawer's "Field engineer" row, and Change itself. Views parse input and call the services (completion.complete_work_order and
workorders.services.set_vendor_engineer, the two writers of WorkOrder.vendor_engineer); the rule is apps.vendors.

The select (web/_wo_engineer_select.html), on vendor service of any type: "Who did the work" (the engineer responsible, as the
vendor's report names them; when a trainee came with a trained engineer, the trained engineer). One native select, so it needs no
round trip on a phone: "Not named on the report" (blank, first), an optgroup of the work order's vendor's engineers, then, for a user
who sees the whole facility, "Other vendors' engineers" (vendors.services.engineer_options: active ones, and the one named already even
when inactive), then "Someone else", whose name box shows with it (CSS :has; always shown without :has support). For a user who sees
the whole facility each option carries its standing for this device on the day judged (today when completing; a completed job's day
in Change), from vendors.rules.evidence_for: "training on file through Mar 31, 2027", "Philips's attestation on file", "nothing on
file for this model". A scoped vendor account sees names only, of its own company's engineers; the engineer stored, when it is
another company's, is an option reading "Another company's engineer" (KEEP_CHOICE) that keeps it. Nothing is preselected but the
engineer stored (a reopened job, or Change).

The drawer's row (drawer_context, under "field_engineer"), on vendor service not cancelled: who is named (services.engineer_shown;
"Not named"), a link to the engineer's drawer for Contracts View, and for a user who sees the whole facility the evidence on file for
the job (rules.evidence_words: on its completion day, today while open; an imported job is never judged, as the binder counts it
apart); "Change" when workorders.permissions.can_set_vendor_engineer (Work orders Edit, Approve once closed, never a scoped user);
and the vendor's report number when one is recorded.

Change (wo_engineer, work-orders/<number>/engineer/): a modal with the select, posting to set_vendor_engineer. Never a scoped user's
(web_view refuses them: a vendor names its engineer only as it completes its own work). A work order that is not vendor service, is
cancelled, or is closed for a user without Approve, gets the modal saying why. A refusal re-renders it with what was chosen and typed;
a save answers with the drawer, a toast, wo-changed, and the modal's close after settle.
"""
from collections import defaultdict

from django.core.exceptions import ValidationError
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django_htmx.http import retarget, trigger_client_event

from apps.equipment.models import DeviceModel
from apps.vendors import permissions as vendor_perms
from apps.vendors import rules
from apps.vendors import services as vendor_services
from apps.vendors.models import Covers, EngineerTraining, VendorAttestation
from apps.workorders import completion, scoping
from apps.workorders import permissions as wo_perms
from apps.workorders import services as wo_services
from apps.workorders.models import Source, WoStatus

from .decorators import web_view
from .forms_wo_complete import KEEP_CHOICE, OTHER, EngineerForm
from .htmx import toast
from .views import _render_wo_drawer, get_wo

MODAL = "web/_wo_engineer.html"
NOT_NAMED = "Not named"
CLOSED = "{number} is closed: naming its engineer now needs Work orders Approve."
CANCELLED = "{number} is cancelled: a cancelled work order names no engineer."
IN_HOUSE = "{number} is in-house work: only vendor service names the vendor's engineer."


# --- the select ----------------------------------------------------------------------------------------------------------------

def standing_words(evidence) -> str:
    """An option's standing, short (vendors.rules.evidence_for's answer): what is on file, never "trained"."""
    if evidence.kind == rules.TRAINING:
        training = evidence.record
        return f"training on file through {rules.fmt(training.expires_on)}" if training.expires_on else "training on file, no expiry"
    if evidence.kind == rules.ATTESTATION:
        names = evidence.record.vendor_names or ["The vendor"]
        return f"{names[0]}'s attestation on file"
    words = "nothing on file for this model"
    if evidence.expired_on:
        words += f" (training expired {rules.fmt(evidence.expired_on)})"
    return words


def _standings(wo, engineers, day) -> dict:
    """Each engineer's standing for `wo`'s device on `day`, by id: two queries (their trainings, the attestations in force)."""
    if not engineers:
        return {}
    trainings = defaultdict(list)
    for t in EngineerTraining.objects.filter(engineer__in=[e.pk for e in engineers]):
        trainings[t.engineer_id].append(t)
    attestations = list(VendorAttestation.objects.filter(valid_from__lte=day, valid_through__gte=day))
    model = wo.asset.device_model
    return {e.pk: standing_words(rules.evidence_for(day=day, device_model=model, vendor_name=wo.vendor_name, engineer=e,
                                                    trainings=trainings[e.pk], attestations=attestations))
            for e in engineers}


def select_context(wo, user, day) -> dict:
    """The select's options for `wo` (vendor service) as `user` may choose them, each with its standing on `day` for a user who sees
    the whole facility: {"groups": [{"label", "options": [{"value", "text"}]}], "keep": "Another company's engineer" when the engineer
    stored is not one this (scoped) user may see, "vendor": the work order's vendor}."""
    scoped = scoping.is_scoped(user)
    own, others = vendor_services.engineer_options(wo, user)
    standing = {} if scoped else _standings(wo, [*own, *others], day)

    def option(engineer, company: bool) -> dict:
        text = f"{engineer.name} ({engineer.company})" if company else engineer.name
        if not scoped:
            text += (" · no longer active" if not engineer.is_active else "") + f" · {standing[engineer.pk]}"
        return {"value": str(engineer.pk), "text": text}

    vendor = wo.vendor_name or "The vendor"
    groups = [g for g in ({"label": f"{vendor}'s engineers", "options": [option(e, False) for e in own]},
                          {"label": "Other vendors' engineers", "options": [option(e, True) for e in others]}) if g["options"]]
    offered = {o["value"] for g in groups for o in g["options"]}
    hidden = bool(wo.vendor_engineer_id) and str(wo.vendor_engineer_id) not in offered
    return {"groups": groups, "keep": vendor_services.ANOTHER_COMPANY if hidden else "", "vendor": vendor}


def initial_choice(wo, sel: dict) -> str:
    """The select's value to begin with: the engineer stored (a reopened job, or Change), else not named. Never anyone else's."""
    if not wo.vendor_engineer_id:
        return ""
    return KEEP_CHOICE if sel["keep"] else str(wo.vendor_engineer_id)


def picked(sel: dict, form, prefix: str) -> dict:
    """The select as `form` holds it: the value chosen, the name typed, their errors, and which to focus (the completion modal's
    first error; Change's)."""
    def value(name):
        v = form[name].value()
        return "" if v is None else str(v)

    def error(name):
        return " ".join(form.errors.get(name, [])) if form.is_bound else ""

    chosen = value("vendor_engineer")
    offered = {o["value"] for g in sel["groups"] for o in g["options"]} | {OTHER} | ({KEEP_CHOICE} if sel["keep"] else set())
    if chosen not in offered:  # an id not offered (another company's, gone, or made up) is shown as not named; its refusal says why
        chosen = ""
    return {**sel, "value": chosen, "name": value("engineer_name"), "error": error("vendor_engineer"),
            "name_error": error("engineer_name"), "focus": getattr(form, "first_error", ""), "prefix": prefix, "name_max": rules.NAME_MAX}


# --- the drawer's row ----------------------------------------------------------------------------------------------------------

def _models_by_id(record) -> dict:
    if record is None or record.covers != Covers.MODELS or not record.model_ids:
        return {}
    return {str(m.pk): m for m in DeviceModel.objects.filter(pk__in=record.model_ids)}


def evidence_on(wo, day) -> str:
    """The evidence on file for vendor job `wo` on `day` (vendors.rules.evidence_words): the engineer's training naming its model,
    else an attestation from its vendor or the engineer's company, else none."""
    engineer = wo.vendor_engineer if wo.vendor_engineer_id else None
    trainings = list(engineer.trainings.all()) if engineer is not None else []
    attestations = list(VendorAttestation.objects.filter(valid_from__lte=day, valid_through__gte=day))
    evidence = rules.evidence_for(day=day, device_model=wo.asset.device_model, vendor_name=wo.vendor_name, engineer=engineer,
                                  trainings=trainings, attestations=attestations)
    return rules.evidence_words(evidence, _models_by_id(evidence.record))


def drawer_context(request, wo, today) -> dict:
    """The drawer's "Field engineer" row, under "field_engineer": None unless `wo` is vendor service and not cancelled."""
    if not wo.vendor_service or wo.status == WoStatus.CANCELLED:
        return {"field_engineer": None}
    user = request.user
    scoped = scoping.is_scoped(user)
    shown = vendor_services.engineer_shown(wo, user)
    engineer = wo.vendor_engineer if wo.vendor_engineer_id else None
    imported = wo.source == Source.IMPORTED
    done = wo.status in completion.DONE_STATUSES and wo.completed_on is not None
    return {"field_engineer": {
        "name": shown, "not_named": NOT_NAMED,
        "inactive": engineer is not None and not engineer.is_active and not scoped,
        "url": reverse("web:vendor_engineer", args=[engineer.pk]) if engineer is not None and vendor_perms.can_view(user) else "",
        "evidence": "" if scoped or imported else evidence_on(wo, wo.completed_on if done else today),
        "imported": imported and not scoped,
        "can_change": wo_perms.can_set_vendor_engineer(user, wo),
    }}


# --- Change ---------------------------------------------------------------------------------------------------------------------

def _blocker(user, wo) -> str:
    if not wo.vendor_service:
        return IN_HOUSE.format(number=wo.number)
    if wo.status == WoStatus.CANCELLED:
        return CANCELLED.format(number=wo.number)
    if not wo_perms.can_set_vendor_engineer(user, wo):  # Edit got the user here; a closed work order needs Approve
        return CLOSED.format(number=wo.number)
    return ""


def _modal(request, wo, form=None, blocker=""):
    ctx = {"wo": wo, "asset": wo.asset, "dm": wo.asset.device_model, "blocker": blocker}
    if form is not None and not blocker:
        day = wo.completed_on if wo.status in completion.DONE_STATUSES and wo.completed_on else timezone.localdate()
        sel = select_context(wo, request.user, day)
        if not form.is_bound:
            form.initial["vendor_engineer"] = initial_choice(wo, sel)
        form.first_error = next((name for name in ("vendor_engineer", "engineer_name") if form.is_bound and name in form.errors), "")
        ctx.update(form=form, engineer=picked(sel, form, "we"), judged_on=day if wo.status in completion.DONE_STATUSES else None,
                   scoped=scoping.is_scoped(request.user))
    return render(request, MODAL, ctx)


def _saved(request, wo):
    shown = vendor_services.engineer_shown(wo, request.user)
    message = f"Field engineer on {wo.number}: {shown}" if shown else f"Field engineer on {wo.number}: not named"
    response = toast(trigger_client_event(retarget(_render_wo_drawer(request, wo), "#drawer"), "wo-changed", {}), message)
    return trigger_client_event(response, "modal-close", {}, after="settle")


@web_view(wo_perms.MODULE, wo_perms.LATE_REASON_LEVEL)  # Work orders Edit (Approve once closed: _blocker); never a scoped user
def wo_engineer(request, number):
    wo = get_wo(request, number)  # another facility's number is a 404
    if request.method != "POST" and not request.htmx:  # only ever a modal: a direct visit opens the work order
        return redirect("web:wo", number=wo.number)
    blocker = _blocker(request.user, wo)
    if blocker:
        return _modal(request, wo, blocker=blocker)
    if request.method != "POST":
        return _modal(request, wo, EngineerForm())
    form = EngineerForm(request.POST)
    if form.is_valid():
        try:
            # KEEP_CHOICE (never offered here, but posted) leaves the engineer stored as it is
            wo_services.set_vendor_engineer(wo, form.choice.get("vendor_engineer", vendor_services.KEEP),
                                            engineer_name=form.choice.get("engineer_name", ""), by=request.user)
        except ValidationError as e:
            form.add_service_errors(e)
        else:
            return _saved(request, get_wo(request, number))
    wo = get_wo(request, number)  # as it is now: closed or cancelled meanwhile is the blocker
    return _modal(request, wo, form, blocker=_blocker(request.user, wo))
