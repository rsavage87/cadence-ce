"""
Which test equipment a work order recorded (slice 35, apps.testequipment): the completion modal's "Test equipment used" picker and the
drawer's Change (both web/_wo_test_equipment_picker.html), the drawer's "Test equipment" section (web/_wo_test_equipment_row.html,
through views_wo_complete.results_context), the work order print's line (print_context, views_print.wo_print), and Change itself
(work-orders/<number>/test-equipment/, web/_wo_test_equipment.html). Views parse input and call the services
(completion.complete_work_order and workorders.services.set_test_equipment, the writers of the uses); the rule is
apps.testequipment.rules, and every standing's words come from it. Never a scoped user's: no picker, no section, no print line, and
Change refuses them (web_view).

The picker (offer, then picked): only where rules.offered holds (in-house work of any type, or a repair whose outcome is not the
vendor's report; never imported; never a scoped user), and shown only when it has options (testequipment.services.picker: none means no
picker and no marker, so a facility that keeps no test equipment in Cadence sees nothing) or a refusal to show. The form takes the
ticks wherever it is offered, so an item taken out of use while the modal was open is refused in the service's words, never dropped. "Yours"
first (the items held by costs.default_technician, the technician assigned or the user's own profile, then those that technician
recorded in the last 30 days, at most 8), then "Other test equipment (N)", a <details> grouped by kind, open by default only when Yours
is empty and N is at most 12 (and whenever one in it is ticked, or a refusal points at it). Each row: the tag, the make and model, the
status when it is not in use, and a chip with its standing (judged today while completing; on the day of the work in Change), in the
warning style when flagged (anything but a certificate in force). Nothing is ticked for the user: only what the work order already
recorded (a reopened work order's, or Change's), or what the form sent back. Completing offers only items in use or whose status changed
today, and those already on the work order; Change, every item not retired before the work's day. An item kept on the work order (the
kept-use rule: a certificate dated on or after the work's day is on file, rules.may_remove) is ticked and disabled, "kept: calibrated
since", with a hidden input carrying its id (a disabled box posts nothing, and the service refuses a list that leaves it out). The
hidden `shown_test_equipment` marker says the picker was on screen: without it nothing is sent (kept as recorded); with it and nothing
ticked, [] (forms_wo_complete.ticked). On a repair the picker hides with "Checked by the vendor" (CSS :has), and the form drops ticks
left under that choice.

The drawer's section (drawer_context, under "test_equipment_row") on a completed or closed work order, for a user who sees the whole
facility: each item the work order recorded in rules' words on the day of the work ("TE-01 (Fluke Biomedical ESA612): calibration due
Mar 31, 2027 on the day of this work", then any "found out of tolerance on <day>; <its review>"), flagged ones marked, the tag a link
to the item for Equipment View (testequipment.permissions.can_view); "None recorded" when it is offered and recorded none (shown only
when the facility has test equipment); Change ("Record" when none) for workorders.permissions.can_set_test_equipment (Work orders Edit,
Approve once closed). judged_for reads the uses in at most four queries whatever their number.

Change (wo_test_equipment): a modal with the picker as of the day of the work, posting to services.set_test_equipment. Open, cancelled,
not offered (imported, vendor work), or closed for a user without Approve: the modal says why. A refusal (keyed test_equipment, in the
service's words) re-renders it with what was ticked; a save answers with the drawer, a toast, wo-changed, and the modal's close after
settle. A direct visit opens the work order.
"""
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django_htmx.http import retarget, trigger_client_event

from apps.testequipment import permissions as te_perms
from apps.testequipment import rules
from apps.testequipment import services as te_services
from apps.testequipment.models import Reason, Status, TestEquipment, TestEquipmentUse
from apps.vendors.rules import fmt
from apps.workorders import permissions as wo_perms
from apps.workorders import services as wo_services
from apps.workorders.models import OPEN_STATUSES, RepairCheck, Source, WoStatus

from .decorators import web_view
from .forms_wo_complete import TestEquipmentForm, ticked
from .htmx import toast
from .views import _render_wo_drawer, get_wo

MODAL = "web/_wo_test_equipment.html"
LEGEND = "Test equipment used"
YOURS = "Yours"
YOURS_HINT = "{name}'s kit, and what {name} recorded in the last {days} days"
OTHERS = "Other test equipment ({count})"
HINT_TODAY = ("Tick what was used. Each shows its certificate as of today; one past due or with none on file can still be recorded, and "
              "the record says so.")
HINT_DAY = "Tick what was used on {day}, the day of this work. Each shows its certificate as it stood that day."
VENDOR_HINT = "Not recorded with Checked by the vendor: their report names the test equipment they used."
KEPT_WORDS = "kept: calibrated since"
NONE_RECORDED = "None recorded"
CANCELLED = "{number} was cancelled: it records no test equipment."
CLOSED = "{number} is closed: recording the test equipment used now needs Work orders Approve."
IMPORTED = "{number} was imported from another system: it records no test equipment."
VENDOR_CHECKED = "{number} was checked by the vendor: their report names the test equipment they used."
VENDOR_WORK = "{number} is vendor service: the vendor's report names the test equipment it used."
NOTHING_ON_FILE = "No test equipment is on file in this facility yet: it is added on the Equipment screen's Test equipment tab."
SAVED = "Test equipment on {number}: {tags}"


# --- the picker ----------------------------------------------------------------------------------------------------------------

def offer(wo, user, today):
    """The picker's options for `wo` (testequipment.services.Picker), or None where it records no test equipment (rules.offered:
    imported, vendor work other than a repair, a scoped user). The form takes the ticks wherever it is offered (an item taken out of use
    while the modal was open is refused in the service's words, never dropped); the picker shows only when it has options (has_options)
    or a refusal to show."""
    if not rules.offered(wo, user) or not te_perms.can_view(user):  # the items are Equipment View's (as the API's field and History)
        return None
    return te_services.picker(wo, user, today=today)


def has_options(picker) -> bool:
    return picker is not None and bool(picker.yours or picker.others)


def _status_words(item, *, completing: bool) -> str:
    """The item's status when it is not in use ("Out of service (damaged)"); after completion it is today's, so "now ..."."""
    if item.status == Status.IN_USE:
        return ""
    words = Status(item.status).label
    if item.status_reason:
        words += f" ({Reason(item.status_reason).label.lower()})"
    return words if completing else f"now {words[0].lower()}{words[1:]}"


def _option(o, chosen: set, *, completing: bool) -> dict:
    item, standing = o.item, o.standing
    pk = str(item.pk)
    return {"id": pk, "tag": item.tag, "name": f"{item.manufacturer} {item.model}", "words": standing.words, "flagged": standing.flagged,
            "css": "warn" if standing.flagged else "neutral", "status": _status_words(item, completing=completing), "kept": o.kept,
            "checked": o.kept or pk in chosen, "focus": False}


def picked(picker, form, *, prefix: str, day=None, vendor_report: bool = False) -> dict | None:
    """The picker as `form` holds it, under "te" (web/_wo_test_equipment_picker.html): the sections (Yours; Other test equipment by
    kind), each option ticked as the form sent it back (else as the work order recorded it), the error keyed test_equipment and
    whether to focus it (the first box that may be changed), and the hints. None when it has no options and no refusal to show (a
    facility that keeps no test equipment in Cadence: no picker, no marker, nothing sent). `day`: None while completing (the options
    judged today), else the day of the work Change judges them on. `vendor_report`: a repair whose outcome may be the vendor's report
    (the picker hides with it)."""
    completing = day is None
    sent = ticked(form)
    on_work_order = {str(o.item.pk) for o in [*picker.yours, *(o for _label, opts in picker.others for o in opts)] if o.on_work_order}
    chosen = set(sent) if sent is not None else on_work_order
    error = " ".join(form.errors.get("test_equipment", [])) if form.is_bound else ""
    if not has_options(picker) and not error:
        return None
    yours = [_option(o, chosen, completing=completing) for o in picker.yours]
    others = [{"label": label, "options": [_option(o, chosen, completing=completing) for o in opts]} for label, opts in picker.others]
    focus = getattr(form, "first_error", "") == "test_equipment" or (error and not getattr(form, "first_error", ""))
    first = next((o for o in [*yours, *(o for g in others for o in g["options"])] if not o["kept"]), None)
    if focus and first is not None:
        first["focus"] = True
    in_others = any(o["checked"] or o["focus"] for g in others for o in g["options"])
    sections = []
    if yours:
        tech = picker.technician
        sections.append({"key": "yours", "label": YOURS, "collapsible": False, "groups": [{"label": "", "options": yours}],
                         "hint": YOURS_HINT.format(name=tech.name, days=te_services.RECENT_DAYS) if tech is not None else ""})
    if others:
        sections.append({"key": "others", "label": OTHERS.format(count=picker.others_count), "collapsible": True,
                         "open": picker.open_others or in_others, "groups": others, "hint": ""})
    return {"prefix": prefix, "legend": LEGEND, "sections": sections, "error": error,
            "hint": HINT_TODAY if completing else HINT_DAY.format(day=fmt(day)),
            "vendor_hint": VENDOR_HINT if vendor_report else "", "kept_words": KEPT_WORDS}


# --- the drawer's section ------------------------------------------------------------------------------------------------------

def _use(judged, link: bool) -> dict:
    """One use in rules' words (use_words), the item's name apart so the tag can link to the item."""
    name = rules.item_name(judged.item)
    words = judged.words
    rest = words[len(name):] if words.startswith(name) else f": {words}"
    url = reverse("web:test_equipment_item", args=[judged.item.tag]) if link else ""
    return {"name": name, "rest": rest, "words": words, "url": url, "flagged": judged.flagged}


def _shown(wo, user, judged) -> bool:
    """Whether a reader sees the test equipment part at all: something recorded, or it may be recorded and the facility keeps test
    equipment in Cadence (one query, only when nothing is recorded)."""
    return bool(judged) or (rules.offered(wo, user) and TestEquipment.objects.exists())


def drawer_context(request, wo) -> dict:
    """The drawer's "Test equipment" section, under "test_equipment_row": None unless `wo` is completed or closed and the reader sees
    the whole facility with Equipment View (testequipment.permissions.can_view: as the API's field and the change log's area) (and
    _shown). Each use judged on the day of the work (testequipment.services.judged_for: at most four queries); Change for
    can_set_test_equipment where it may be recorded."""
    user = request.user
    if wo.status not in rules.DONE or not te_perms.can_view(user):
        return {"test_equipment_row": None}
    judged = te_services.judged_for(wo)
    if not _shown(wo, user, judged):
        return {"test_equipment_row": None}
    link = te_perms.can_view(user)
    return {"test_equipment_row": {
        "uses": [_use(j, link) for j in judged], "none": NONE_RECORDED, "day": wo.completed_on,
        "can_change": rules.offered(wo, user) and wo_perms.can_set_test_equipment(user, wo),
    }}


# --- the print's line ----------------------------------------------------------------------------------------------------------

def print_context(wo, user) -> dict | None:
    """The work order print's test equipment (views_print.wo_print): None for a scoped reader or one without Equipment View, a cancelled
    work order, or where it is not _shown. Open: `open` (lines to write on while it may be recorded) and any uses a reopened work order keeps. Done: each use in
    rules' words on the day of the work, or none recorded."""
    if not te_perms.can_view(user):  # never a scoped reader's; the items are Equipment View's
        return None
    is_open = wo.status in OPEN_STATUSES
    if not is_open and wo.status not in rules.DONE:
        return None
    judged = te_services.judged_for(wo)
    if not _shown(wo, user, judged):
        return None
    return {"open": is_open and rules.offered(wo, user), "uses": [j.words for j in judged], "flagged": [j.flagged for j in judged]}


# --- Change ---------------------------------------------------------------------------------------------------------------------

def _blocker(user, wo) -> str:
    """Why the test equipment `wo` recorded cannot be changed here now, or ""."""
    if wo.status == WoStatus.CANCELLED:
        return CANCELLED.format(number=wo.number)
    if wo.status not in rules.DONE:
        return te_services.NOT_DONE.format(number=wo.number)
    if not wo_perms.can_set_test_equipment(user, wo):  # Edit got the user here; a closed work order needs Approve
        return CLOSED.format(number=wo.number)
    if not rules.offered(wo, user):
        if wo.source == Source.IMPORTED:
            return IMPORTED.format(number=wo.number)
        if wo.repair_check == RepairCheck.VENDOR_REPORT:
            return VENDOR_CHECKED.format(number=wo.number)
        return VENDOR_WORK.format(number=wo.number)
    return ""


def _modal(request, wo, form=None, picker=None, blocker=""):
    ctx = {"wo": wo, "asset": wo.asset, "dm": wo.asset.device_model, "blocker": blocker}
    if form is not None and picker is not None and not blocker:
        ctx.update(form=form, te=picked(picker, form, prefix="wt", day=wo.completed_on), recorded=any(
            o.on_work_order for o in [*picker.yours, *(o for _label, opts in picker.others for o in opts)]))
    return render(request, MODAL, ctx)


def _saved(request, wo):
    tags = list(TestEquipmentUse.objects.filter(work_order=wo).order_by("test_equipment__tag").values_list("test_equipment__tag", flat=True))
    message = SAVED.format(number=wo.number, tags=", ".join(tags) if tags else NONE_RECORDED.lower())
    response = toast(trigger_client_event(retarget(_render_wo_drawer(request, wo), "#drawer"), "wo-changed", {}), message)
    return trigger_client_event(response, "modal-close", {}, after="settle")


@web_view(wo_perms.MODULE, wo_perms.LATE_REASON_LEVEL)  # Work orders Edit (Approve once closed: _blocker); never a scoped user
def wo_test_equipment(request, number):
    wo = get_wo(request, number)  # another facility's number is a 404
    if request.method != "POST" and not request.htmx:  # only ever a modal: a direct visit opens the work order
        return redirect("web:wo", number=wo.number)
    user = request.user
    today = timezone.localdate()
    blocker, picker = _state(user, wo, today)
    if blocker:
        return _modal(request, wo, blocker=blocker)
    if request.method != "POST":
        return _modal(request, wo, TestEquipmentForm(), picker)
    form = TestEquipmentForm(request.POST)
    if form.is_valid():
        try:
            wo_services.set_test_equipment(wo, form.ids(), by=user, today=today)
        except ValidationError as e:
            form.add_service_errors(e)
        except PermissionDenied:
            pass  # closed meanwhile: the work order as it is now says why
        else:
            return _saved(request, get_wo(request, number))
    wo = get_wo(request, number)  # as it is now: reopened, cancelled, or closed meanwhile is the blocker
    blocker, picker = _state(user, wo, today)
    return _modal(request, wo, form, picker, blocker=blocker)


def _state(user, wo, today) -> tuple:
    """(why Change cannot be used now, or ""; the picker's options when it can)."""
    blocker = _blocker(user, wo)
    if blocker:
        return blocker, None
    picker = offer(wo, user, today)
    return ("", picker) if has_options(picker) else (NOTHING_ON_FILE, None)
