"""
What a software update did, on the work order's doors (slice 38, apps.cybersecurity.updates): the completion modal's "Software on the
device" section (web/_wo_release_picker.html, through views_wo_complete: offer, then picked), the toast's words (toast_words), the
drawer's "Software update" section (web/_wo_release_row.html, through views_wo_complete.results_context: drawer_context), and the work
order print's line (print_line, views_print.wo_print). Every word about the record comes from apps.cybersecurity.rules (update_words,
update_state, STATE_LABELS, release_name) or the model's own choices; nothing here words a field itself.

The section (offer, then picked): only where rules.release_offer holds (a register-opened update work order, its device on site, a user
who may record: Device security Edit and Equipment View, never scoped; no query for another type), read once in views_wo_complete._setup.
The target above the radios in rules' words (the work order is open while it is completed: "Open: to install 5.6 (demo)"); the outcome
radios, each with its label (UpdateOutcome's, and "Not confirmed now") and what it does: none chosen while a choice is needed; on a
re-completion with an outcome recorded, "Keep as recorded" first and chosen (it sends nothing: the record stays), and "Not confirmed now"
saying it keeps the record too (NOT_CONFIRMED_KEEPS: updates.check_choice records nothing for it, so Confirm version is never offered on
that row; Entered in error at Device security Approve clears the outcome). The release it runs
now (the model's releases, version order, the recorded one marked; the target chosen to begin with on a release target), the reference
(the target's patch list to begin with on a patches target), the basis (UpdateBasis), and the help line (drug libraries are not
recorded here). The hidden `shown_software_outcome` marker says the section was on screen: without it nothing is sent (KEEP), and the
service asks for a choice where one is needed. forms_wo_complete.CompleteForm turns the post into updates.choice_from's choice (the basis
blanked for "already": the service refuses one there). A scoped user, or one without the offer, sees no section, and the form takes
nothing from their post.

The drawer's section and the print's line: on an update work order that has its record (an imported one has none), for
permissions.can_view_devices only (Device security View and Equipment View, never scoped): "Software update" and rules.update_words
(open ones say their target; completed ones what was recorded, or "version not confirmed"), and in the drawer a link to the device's
Security tab, where Confirm version and Entered in error are. One query (the record with its releases), none for another type or a
reader who may not see it.
"""
from django.urls import reverse

from apps.cybersecurity import permissions as cyber_perms
from apps.cybersecurity import rules
from apps.cybersecurity.models import SoftwareUpdate, UpdateBasis, UpdateOutcome, UpdateTarget
from apps.cybersecurity.updates import NOT_CONFIRMED
from apps.workorders.models import WoType

LEGEND = "Software on the device"
KEEP = "keep"  # the re-completion's "Keep as recorded": nothing is sent (cybersecurity.services.KEEP), the recorded outcome stays
KEEP_LABEL = "Keep as recorded"
KEEP_HINT = "Recorded when it was last completed: {label}"
NOT_CONFIRMED_LABEL = "Not confirmed now"
OUTCOME_HINTS = {
    UpdateOutcome.INSTALLED: "The release installed, chosen below; the device's record moves to it from today",
    UpdateOutcome.PATCHES: "The version is unchanged; enter the manufacturer's patch list below",
    UpdateOutcome.ALREADY: "Checked today: it already runs the update",
    NOT_CONFIRMED: "Nothing is recorded now; Confirm version on the device's Security tab records it later. Say why in the resolution",
}
# "Not confirmed now" on a re-completion with an outcome recorded: it records nothing, so what was recorded stays (build38: a correction is
# entered in error at Approve), never the hint above (Confirm version is offered only while nothing is recorded)
NOT_CONFIRMED_KEEPS = ("Keeps what was recorded: {label}; Entered in error on the device's Security tab (Device security Approve) clears it. "
                       "Say why in the resolution")
RELEASE_LABEL = "The release it runs now"
RELEASE_HINT = "Patches applied leave the version as recorded: choose the recorded one, or Not chosen."
RELEASE_BLANK = "Not chosen"
RECORDED_MARK = "{name} · recorded now"
REFERENCE_LABEL = "Reference"
REFERENCE_HINT = "The manufacturer's patch list and its month or revision, e.g. ICAR-2026-09."
BASIS_LABEL = "Basis for this update"
BASIS_BLANK = "Choose…"
HELP = "Drug libraries are not recorded here."
ROW_LABEL = "Software update"
SECURITY_TAB = "On the device's Security tab"
FIELDS = ("software_outcome", "software_release", "software_reference", "software_basis")


# --- the completion modal's section ------------------------------------------------------------------------------------------

def offer(wo, user, today=None):
    """The modal's section for `wo`, or None where nothing is offered (rules.release_offer: another type, an update with no record, a
    device not on site, a user who may not record). Queries: none for another type; else release_offer's (the record, the releases,
    the recorded release)."""
    if wo.type != WoType.SOFTWARE_UPDATE:
        return None
    o = rules.release_offer(wo, user)
    if o is None:
        return None
    recorded_pk = o.recorded.pk if o.recorded is not None else None
    releases = [(str(r.pk), RECORDED_MARK.format(name=rules.release_name(r)) if r.pk == recorded_pk else rules.release_name(r))
                for r in o.releases]
    target = o.target_release
    kept = o.update.outcome  # a re-completion's recorded outcome ("" while nothing is recorded: a choice is needed)
    outcomes = [(KEEP, KEEP_LABEL, KEEP_HINT.format(label=UpdateOutcome(kept).label))] if kept else []
    outcomes += [(value, UpdateOutcome(value).label, OUTCOME_HINTS[value]) for value in o.outcomes]
    outcomes.append((NOT_CONFIRMED, NOT_CONFIRMED_LABEL,
                     NOT_CONFIRMED_KEEPS.format(label=UpdateOutcome(kept).label) if kept else OUTCOME_HINTS[NOT_CONFIRMED]))
    return {
        "legend": LEGEND, "target": rules.update_words(o.update), "needed": o.needed, "recorded_outcome": kept,
        "recorded": rules.release_name(o.recorded), "not_recorded": rules.RELEASE_NOT_RECORDED,
        "outcomes": outcomes, "outcome_initial": KEEP if kept else "",
        "release_label": RELEASE_LABEL, "release_hint": RELEASE_HINT, "release_choices": [("", RELEASE_BLANK), *releases], "has_releases": bool(releases),
        "release_initial": str(target.pk) if target is not None and any(r.pk == target.pk for r in o.releases) else "",
        "reference_label": REFERENCE_LABEL, "reference_hint": REFERENCE_HINT,
        "reference_initial": o.target_reference if o.target == UpdateTarget.PATCHES else "",
        "basis_label": BASIS_LABEL, "basis_choices": [("", BASIS_BLANK), *UpdateBasis.choices],
        "bases": {value: tuple(str(b) for b in bases) for value, bases in o.bases.items()},
        "help": HELP,
    }


def picked(offer_ctx, form) -> dict | None:
    """The section as `form` holds it (web/_wo_release_picker.html, under "release"): the outcome radios with the one posted (else the
    one to begin with) chosen, each field's error, and whether the first radio takes the focus (the form's first error). None without
    an offer, or when the form has no section (forms_wo_complete: not offered)."""
    if offer_ctx is None or form is None or "software_outcome" not in form.fields:
        return None
    value = form["software_outcome"].value()
    chosen = "" if value is None else str(value)
    errors = {name: " ".join(form.errors.get(name, [])) if form.is_bound else "" for name in FIELDS}
    return {**offer_ctx, "errors": errors, "focus": getattr(form, "first_error", "") == "software_outcome",
            "outcomes": [{"value": v, "label": label, "hint": hint, "checked": v == chosen} for v, label, hint in offer_ctx["outcomes"]]}


def toast_words(wo, done, user) -> str:
    """What the completion recorded, for its toast: rules.update_words of the record it wrote (Completion.software), or "" when it
    recorded nothing (KEEP, not confirmed now) or the user may not see it (permissions.can_view_devices). `wo` is the work order as the
    completion left it (refreshed), so the words need no query of their own."""
    row = getattr(done, "software", None)
    if row is None or not cyber_perms.can_view_devices(user):
        return ""
    row.work_order = wo
    return rules.update_words(row)


# --- the drawer's section and the print's line ---------------------------------------------------------------------------------

def drawer_line(wo, user, today=None) -> dict | None:
    """The "Software update" line for `wo`, or None: another type, an update with no record (imported), or a reader without
    permissions.can_view_devices (never scoped). One query (the record with its releases); the work order is `wo` itself."""
    if wo.type != WoType.SOFTWARE_UPDATE or not cyber_perms.can_view_devices(user):
        return None
    row = (SoftwareUpdate.objects.filter(work_order_id=wo.pk).select_related("target_release", "release_before", "release_after")
           .order_by("pk").first())
    if row is None:
        return None
    row.work_order = wo
    state = rules.update_state(row)
    return {"label": ROW_LABEL, "words": rules.update_words(row), "state": state, "state_label": rules.STATE_LABELS[state],
            "url": f"{reverse('web:asset', args=[wo.asset.tag])}?tab=security", "link": SECURITY_TAB}


def drawer_context(request, wo) -> dict:
    """The drawer's "Software update" section (web/_wo_release_row.html), under "software_row"."""
    return {"software_row": drawer_line(wo, request.user)}


def print_line(wo, user) -> str:
    """The print's "Software update: <words>" line's words, or "" (drawer_line's rule: never a scoped reader's)."""
    line = drawer_line(wo, user)
    return line["words"] if line is not None else ""
