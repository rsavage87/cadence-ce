"""
The Device security screen (slice 37, beyond the mock), `/security/`, nav "Device security": the register of what each model's software
can do and what its manufacturer discloses, and what this facility knows about each device's software and connection. Views parse
input and call apps.cybersecurity.services; every figure, standing, and word about a device or a release is apps.cybersecurity.rules'
(never composed here from the fields); the levels are apps.cybersecurity.permissions. Scoped users are refused every view here
(web_view's default), whatever their levels.

Tabs (page-level links, each its own address; a filter or a window re-fetches the tab's body and pushes the address):
- Fleet (`/security/`, the default): HICP 2023's recommendation in one small line, the head figures (rules.fleet's), and one row per
  model with devices on site: devices, connected here or able to connect, by release ("Release not recorded" last), past security
  updates, OS past its vendor's support, MDS2, patient data, who installs updates here, segment. Filters: whose, department, category,
  connection, standing, release not recorded (the last three read each device's facts through rules.standings), parsed by
  apps.cybersecurity.filters (the API's device-security list reads the same query). Export carries them. A head figure counted under
  the filters links to the devices it counts with every filter kept (FleetFilters.within), or not at all when the two cannot be one query.
- Models (`/security/models/`): every model with devices on site or anything recorded (a search finds any of the facility's models, to
  start a profile), each with its profile's answers, its releases, and its MDS2.
- Changes for IT (`/security/changes/`): rules.changes_for_it for 7, 30 (the default), or 90 days, and its CSV.

A model's panel (`/security/models/<id>/`, into #drawer; the screen with it open when its address is opened directly): the profile's
card (each group with its source and words, the MDS2 sentence), the releases (each with its standing lines and, behind Details, its
groups merged with the profile by group), and its devices on site (tag, location, release, since, connection, the two standing lines),
narrowed by the Fleet's filters when it was opened from a filtered Fleet. Add release, Record the profile, Edit, Entered in error, and
Set devices to this release are modals; a save answers with the panel into #drawer, the toast, `cyber-changed` (the tab's body
re-fetches itself), and the modal's close (Set devices to this release opened from an advisory's page answers with that page instead:
slice 40, set_release).

Who: Device security View sees the model-level parts (Models, a panel's profile and releases). Every list naming devices (the Fleet, a
panel's devices, devices.csv, Changes for IT) also needs Equipment View (permissions.can_view_devices): without it `/security/` opens
Models and the device lists answer 403. Device security Edit adds, edits, and marks releases entered in error; Set devices to this
release needs Equipment View too (permissions.can_edit_devices). The services check every level again. The device drawer's Record facts
is views_cyber_facts' (the device drawer's Security tab).
"""
from collections import Counter
from datetime import timedelta
from urllib.parse import urlencode

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.db.models.functions import Lower
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import capfirst, slugify
from django_htmx.http import retarget, trigger_client_event

from apps.cybersecurity import filters, rules
from apps.cybersecurity import permissions as cyber_perms
from apps.cybersecurity import services as cyber
from apps.cybersecurity.models import DISCLOSURE_GROUP, FACILITY_GROUP, GROUPS, Mds2Status, OsFamily, SoftwareRelease
from apps.equipment.models import AssetStatus, Department, DeviceModel

from . import cyber_release_actions
from .decorators import web_view
from .exports import csv_response
from .forms import parse_uuid
from .forms_cybersecurity import (
    CONNECTED_FILTERS,
    KEEP_NETWORK,
    QUESTIONS,
    STANDING_FILTERS,
    WHOSE_FILTERS,
    ReleaseForm,
    SetReleaseForm,
    parse_days,
    parse_fleet_filters,
    parse_search,
    release_initial,
)
from .htmx import PAGE_SIZE, is_partial, toast

VIEW, EDIT = cyber_perms.VIEW_LEVEL, cyber_perms.EDIT_LEVEL
MODULE = cyber_perms.MODULE

PAGE = "web/cybersecurity.html"
FLEET = "web/_cyber_fleet.html"
MODELS = "web/_cyber_models.html"
CHANGES = "web/_cyber_changes.html"
PANEL = "web/_cyber_model.html"
RELEASE_FORM = "web/_cyber_release_form.html"
REMOVE = "web/_cyber_release_remove.html"
SET_RELEASE = "web/_cyber_set_release.html"
CHANGED = "cyber-changed"  # each tab's body re-fetches itself on it
SHOWN = 100  # a panel's devices: the first this many by tag, then the CSV has every one
DEVICES_WORDS = "The devices' facts need Device security View and Equipment View."
NOT_RECORDED = "Not recorded"
SOURCE_WORDS = {rules.THIS_RELEASE: "From this release", rules.PROFILE: "From the model's disclosure, version not stated",
                rules.NOT_RECORDED: "Not recorded"}
FACILITY_SOURCE = capfirst(rules.FACILITY_SOURCE)  # the facility's arrangement: the model's, for every version (never a disclosure)
GROUP_TITLES = {"support": "Operating system and security updates", "mds2": "The manufacturer's disclosure (MDS2)",
                "disclosure": "What the manufacturer discloses", "facility": "This facility's arrangement"}
SECURITY_TONES = {rules.UPDATES_ENDED: "warn", rules.MODEL_SUPPORT_ENDED: "warn", rules.UPDATES_END_ANNOUNCED: "info"}
OS_TONES = {rules.OS_ENDED: "warn", rules.OS_END_ANNOUNCED: "info"}


# --- small parts -------------------------------------------------------------------------------------------------------------------

def _qs(params: dict) -> str:
    return "?" + urlencode(params) if params else ""


def _label(name: str, value) -> str:
    """A choice in words ("Not recorded" for blank): the field's own TextChoices label."""
    return cyber.CHOICES[name](value).label if value else NOT_RECORDED


def _day(value) -> str:
    return rules.day_words(value) if value else NOT_RECORDED


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _model(pk) -> DeviceModel:
    """A device model of this facility (another facility's, or none, is a 404)."""
    found = DeviceModel.objects.filter(pk=pk).first()
    if found is None:
        raise Http404
    return found


def _release(pk) -> SoftwareRelease:
    """A release (or profile) of this facility not entered in error: hidden from lists and never named once marked, so a 404."""
    found = SoftwareRelease.objects.select_related("device_model").filter(pk=pk, removed_on__isnull=True).first()
    if found is None:
        raise Http404
    return found


def _need_devices(user) -> None:
    if not cyber_perms.can_view_devices(user):
        raise PermissionDenied(DEVICES_WORDS)


def _shell(request, tab: str) -> dict:
    user = request.user
    return {"nav_active": "cybersecurity", "cyber_tab": tab, "can_devices": cyber_perms.can_view_devices(user),
            "can_edit": cyber_perms.can_edit(user), "can_edit_devices": cyber_perms.can_edit_devices(user)}


def _mds2_words(dm, held: list) -> str:
    """A model's MDS2 in the Fleet's words ("On file for 9.19.2", "Requested from the manufacturer"): the fleet's own rule over the
    model's profile and releases (not entered in error), so the Models tab reads as the Fleet does."""
    status, versions = rules.mds2_of(held)
    return rules.FleetRow(device_model=dm, devices=0, connected=0, by_release=[], updates_ended=0, os_ended=0, facts=rules.facts(None, None),
                          mds2=status, mds2_versions=versions).mds2_words


# --- Fleet -------------------------------------------------------------------------------------------------------------------------

def _fleet_options() -> dict:
    """The Fleet's filter choices: every department and every model's category in the facility (round 2: a department or category with
    no device on site is kept and answers none, as the API does for the same address, never widened to the whole fleet)."""
    departments = Department.objects.order_by("name").values_list("pk", "name")
    categories = DeviceModel.objects.order_by("category").values_list("category", flat=True).distinct()
    return {"departments": [(str(pk), name) for pk, name in departments], "categories": list(categories), "whose": WHOSE_FILTERS,
            "connected": CONNECTED_FILTERS, "standings": STANDING_FILTERS}


def _with_kept(options: dict, f) -> dict:
    """The filter bar's choices with a department or category kept from the address but not among them (an old bookmark: it matches
    nothing here) shown selected, "(none here)", so the bar says what is in force and the next change keeps it."""
    out = dict(options)
    if f.dept and f.dept not in {v for v, _ in options["departments"]}:
        out["departments"] = [*options["departments"], (f.dept, "A department not in this facility (none here)")]
    if f.category and f.category not in options["categories"]:
        out["categories"] = [*options["categories"], f.category]
        out["category_none"] = f.category
    return out


def _narrowed(f, today):
    """The devices on site the filters keep (apps.cybersecurity.filters, as the API's device list): a queryset, or a list when a filter
    reads each device's facts (rules.standings)."""
    qs = filters.narrow(rules.on_site(), f)
    if not f.by_device:
        return qs
    return [s.asset for s in rules.standings(qs, today).values() if f.matches(s)]


def _releases_words(row) -> list:
    """[(version or "Release not recorded", count)] of a fleet row (rules.fleet's order: versions, then not recorded)."""
    return [(release.version if release is not None else rules.RELEASE_NOT_RECORDED, n) for release, n in row.by_release]


# What each linked head figure counts, as filters: connected here or able to connect, and past security updates or no release recorded
FIGURE_FILTERS = {"updates_ended": {"connected": filters.EITHER, "standing": filters.UPDATES_ENDED},
                  "no_release": {"connected": filters.EITHER, "norelease": True}}


def _figure_url(f, key: str) -> str:
    """A head figure's link: the filters on screen (the figure was counted under them), narrowed to what the figure counts
    (FleetFilters.within), so the list it opens is the devices the figure counted; "" (no link) when the two cannot be one query (a
    connection or standing on screen that the figure's excludes, or another standing beside it)."""
    narrowed = f.within(**FIGURE_FILTERS[key]) if key in FIGURE_FILTERS else None
    return reverse("web:cybersecurity") + _qs(narrowed.params()) if narrowed is not None else ""


def _figures(fl, f) -> list:
    """The head figures in rules.fleet's words, each a link to the devices it counts where a filter shows them (_figure_url)."""
    out = []
    for key in ("no_mds2", "updates_ended", "no_release", "not_segmented", "segment_not_recorded"):
        fig = fl.figures[key]
        extra = ""
        if key == "no_mds2" and fig.get("requested"):
            extra = f"{fig['requested']} of them requested from the manufacturer"
        out.append({**fig, "extra": extra, "url": _figure_url(f, key)})
    return out


def _fleet_context(request) -> dict:
    today = timezone.localdate()
    options = _fleet_options()
    f = parse_fleet_filters(request.GET, options)
    fl = rules.fleet(today, _narrowed(f, today))
    page = Paginator(fl.rows, PAGE_SIZE).get_page(request.GET.get("page"))
    panel_q = _qs(f.params())
    rows = [{"r": r, "releases": _releases_words(r), "patient": _label("patient_data", r.facts.get("patient_data")),
             "updates_by": _label("updates_by", r.facts.get("updates_by")), "segmented": _label("segmented", r.facts.get("segmented")),
             "url": reverse("web:cybersecurity_model", args=[r.device_model.pk]) + panel_q} for r in page.object_list]
    return {**_shell(request, "fleet"), "list_url": reverse("web:cybersecurity"), "f": f, "options": _with_kept(options, f), "fleet": fl,
            "figures": _figures(fl, f), "page": page, "rows": rows, "hicp": rules.HICP_SENTENCE, "counts": rules.COUNTS_ON_SITE}


@web_view(MODULE, VIEW)
def fleet(request):
    """The Fleet tab (the screen's default). A reader without Equipment View opens Models: the Fleet counts devices."""
    if not cyber_perms.can_view_devices(request.user):
        if request.htmx:
            raise PermissionDenied(DEVICES_WORDS)
        return redirect("web:cybersecurity_models")
    ctx = _fleet_context(request)
    if is_partial(request, "cy-fleet"):
        return render(request, FLEET, ctx)
    return render(request, PAGE, ctx)


FLEET_COLUMNS = ["Manufacturer", "Model", "Category", "Devices", "Connected here or able to connect", "By release",
                 "Past security updates or the model's support", "OS past its vendor's published support", "MDS2",
                 "Patient data, as disclosed", "Who installs updates here", "Network segment here"]


@web_view(MODULE, VIEW)
def fleet_csv(request):
    """The Fleet (its filters, every row) as CSV: the table's columns, nothing more."""
    _need_devices(request.user)
    today = timezone.localdate()
    f = parse_fleet_filters(request.GET, _fleet_options())
    fl = rules.fleet(today, _narrowed(f, today))
    rows = ((r.device_model.manufacturer, r.device_model.model, r.device_model.category, r.devices, r.connected,
             "; ".join(f"{words}: {n}" for words, n in _releases_words(r)), r.updates_ended, r.os_ended, r.mds2_words,
             _label("patient_data", r.facts.get("patient_data")), _label("updates_by", r.facts.get("updates_by")),
             _label("segmented", r.facts.get("segmented"))) for r in fl.rows)
    return csv_response(f"cadence-device-security-{today:%Y-%m-%d}.csv", FLEET_COLUMNS, rows)


# --- a model's panel ---------------------------------------------------------------------------------------------------------------

def _os_words(values) -> str:
    family, name = values.get("os_family"), values.get("os_name")
    if not family:
        return name or NOT_RECORDED
    label = OsFamily(family).label
    return f"{label} · {name}" if name and family != OsFamily.NONE else label


def _source(group: str, source: str) -> str:
    """A group's source in words. The facility's arrangement is this facility's, recorded on the model's profile for every version (the
    drawer's and the API's words: rules.FACILITY_SOURCE), never the manufacturer's disclosure."""
    if group == "facility" and source == rules.PROFILE:
        return FACILITY_SOURCE
    return SOURCE_WORDS[source]


def _card(merged, dm, today, *, profile: bool) -> dict:
    """One card's groups in words (the profile's, or a release's merged with the profile by group), each with its source, and the two
    standing lines (rules.release_standing, rules.os_standing), never merged."""
    v = merged.values
    security, os_line = rules.release_standing(merged, dm, today), rules.os_standing(merged, today)
    support = [("Operating system", _os_words(v)), ("Support checked", _day(v.get("support_checked_on")))]
    status = v.get("mds2")
    mds2 = [("MDS2", _label("mds2", status))]
    if status == Mds2Status.ON_FILE:
        mds2 += [("Form", _label("mds2_edition", v.get("mds2_edition"))), ("Document", v.get("mds2_document") or NOT_RECORDED),
                 ("Released", _day(v.get("mds2_released_on")))]
    elif status == Mds2Status.REQUESTED:
        mds2.append(("Requested on", _day(v.get("mds2_requested_on"))))
    groups = [
        {"key": "support", "lines": [security.words, os_line.words], "rows": support},
        {"key": "mds2", "rows": mds2, "url": v.get("mds2_url") if status == Mds2Status.ON_FILE else "", "sentence": rules.MDS2_SENTENCE},
        {"key": "disclosure", "rows": [(QUESTIONS[n], _label(n, v.get(n))) for n in DISCLOSURE_GROUP]},
    ]
    if profile:
        groups.append({"key": "facility", "rows": [(QUESTIONS[n], _label(n, v.get(n))) for n in FACILITY_GROUP]})
    for g in groups:
        g["title"] = GROUP_TITLES[g["key"]]
        g["source"] = _source(g["key"], merged.sources[g["key"]])
    return {"groups": groups, "security": security, "os": os_line, "security_tone": SECURITY_TONES.get(security.key, "neutral"),
            "os_tone": OS_TONES.get(os_line.key, "neutral")}


def _panel_context(request, dm) -> dict:
    """The panel: the profile's card, the releases, and (Equipment View too) the devices on site narrowed by the Fleet's filters the
    panel was opened with (the actions keep them: their addresses carry the same query)."""
    user = request.user
    today = timezone.localdate()
    held = list(SoftwareRelease.objects.filter(device_model=dm, removed_on__isnull=True))
    profile_row = next((r for r in held if r.is_profile), None)
    versioned = sorted((r for r in held if not r.is_profile), key=lambda r: (r.version_key, str(r.pk)))
    can_devices, can_edit = cyber_perms.can_view_devices(user), cyber_perms.can_edit(user)
    f = parse_fleet_filters(request.GET, _fleet_options()) if can_devices else parse_fleet_filters({}, {"departments": [], "categories": []})
    params = f.params()
    base = reverse("web:cybersecurity_model", args=[dm.pk])
    read = rules.standings(rules.on_site().filter(device_model=dm), today) if can_devices else {}
    running = Counter(s.release.pk if s.release is not None else None for s in read.values())
    shown = sorted((s for s in read.values() if f.keeps(s)), key=lambda s: s.asset.tag)
    releases = []
    for r in versioned:
        merged = rules.facts(r, profile_row)
        releases.append({"r": r, "card": _card(merged, dm, today, profile=False), "running": running.get(r.pk, 0),
                         "edit_url": reverse("web:cybersecurity_release_edit", args=[r.pk]) + _qs(params),
                         "remove_url": reverse("web:cybersecurity_release_remove", args=[r.pk]) + _qs(params),
                         "set_url": reverse("web:cybersecurity_set_release", args=[dm.pk]) + _qs({**params, "release": r.pk})})
    # slice 38: each release's updates (x["update"]); slice 39: its decision (x["decision"]) and the footer's, on the devices read above
    campaigns = cyber_release_actions.annotate(request, dm, releases, params, read)
    return {
        **campaigns,
        "dm": dm, "today": today, "profile": profile_row, "profile_card": _card(rules.facts(None, profile_row), dm, today, profile=True),
        "releases": releases, "mds2_words": _mds2_words(dm, held), "can_devices": can_devices, "can_edit": can_edit,
        "can_edit_devices": cyber_perms.can_edit_devices(user), "can_view_asset": can_devices,
        # Slice 39: a decision for every device of ours of the model, offered only while a device of ours of the model on site waits for
        # one (cyber_release_actions._waiting, as Needs attention reads it; review 39 S1), its address carrying the panel's filters so
        # the save, and Review and renew from its modal, answer with the panel as it was opened (S0)
        "decide_model_url": (reverse("web:security_decision_new") + _qs({"model": dm.pk, "from": "panel", **params})
                             if campaigns["decide_model"] else ""),
        "on_site": len(read), "not_recorded": running.get(None, 0), "devices": shown[:SHOWN], "devices_count": len(shown),
        "narrowed": f.any, "all_url": base, "f": f, "query": _qs(params),
        "csv_url": reverse("web:cybersecurity_devices_csv") + _qs({"model": dm.pk, **params}),
        "profile_url": (reverse("web:cybersecurity_release_edit", args=[profile_row.pk]) + _qs(params) if profile_row is not None
                        else reverse("web:cybersecurity_release_new", args=[dm.pk]) + _qs({**params, "profile": 1})),
        "new_url": reverse("web:cybersecurity_release_new", args=[dm.pk]) + _qs(params),
        "set_url": reverse("web:cybersecurity_set_release", args=[dm.pk]) + _qs(params),
    }


def _panel(request, dm):
    return render(request, PANEL, _panel_context(request, dm))


@web_view(MODULE, VIEW)
def model_panel(request, pk):
    """A model's panel (HTMX, into #drawer), or the screen with it open when its address is opened directly (the Fleet for a reader of
    the devices, else Models)."""
    dm = _model(pk)
    ctx = _panel_context(request, dm)
    if request.htmx and not request.htmx.history_restore_request:
        return render(request, PANEL, ctx)
    page = _fleet_context(request) if ctx["can_devices"] else _models_context(request)
    return render(request, PAGE, {**page, **ctx, "drawer_template": PANEL})


DEVICE_COLUMNS = ["Tag", "Department", "Room", "Release", "Running it since", "Connected here", "Security updates", "Operating system support"]


@web_view(MODULE, VIEW)
def devices_csv(request):
    """A model's devices on site (`?model=<id>`, with the Fleet's filters when given) as CSV: the panel's columns, nothing more."""
    _need_devices(request.user)
    pk = parse_uuid(request.GET.get("model", ""))
    if pk is None:
        raise Http404
    dm = _model(pk)
    today = timezone.localdate()
    f = parse_fleet_filters(request.GET, _fleet_options())
    read = rules.standings(rules.on_site().filter(device_model=dm), today)
    rows = ((s.asset.tag, s.asset.department.name, s.asset.room, s.release.version if s.release is not None else rules.RELEASE_NOT_RECORDED,
             s.release_since, s.connection.words, s.security.words, s.os.words)
            for s in sorted((s for s in read.values() if f.keeps(s)), key=lambda s: s.asset.tag))
    return csv_response(f"cadence-device-security-{_file_part(dm)}-{today:%Y-%m-%d}.csv", DEVICE_COLUMNS, rows)


def _file_part(dm) -> str:
    """The model in a download's file name: ASCII letters, digits, and dashes only (slugify: "Dräger Evita V500 Œ" reads
    "drager-evita-v500"), else its id. A letter outside Latin-1 would have the whole Content-Disposition header MIME-encoded, which
    browsers cannot read."""
    return slugify(f"{dm.manufacturer} {dm.model}")[:60].strip("-") or str(dm.pk)


# --- releases and the profile ------------------------------------------------------------------------------------------------------

def _saved(request, dm, message: str):
    """A modal saved: the model's panel into #drawer, the tab's body refreshed, the toast, then the modal closed after settle (closing
    it first would detach the form that sent this request, which cancels the swap and the events)."""
    response = retarget(_panel(request, dm), "#drawer")
    trigger_client_event(response, CHANGED, {})
    toast(response, message)
    return trigger_client_event(response, "modal-close", {}, after="settle")


def _release_modal(request, dm, form, release=None, action: str = ""):
    if form.is_bound:
        form.focus_first_error()
    return render(request, RELEASE_FORM, {"dm": dm, "form": form, "release": release, "profile": form.profile,
                                          "action": action or request.get_full_path()})


RESTORED_WORDS = "restored (it was entered in error), with the facts it had"
RESTORED_OVER = " and what you entered over them"


@web_view(MODULE, EDIT)
def release_new(request, pk):
    """Add release (or, with ?profile=1, Record the profile): GET the modal, POST services.add_release with every field as typed. Add
    release needs a version (a blank one is never the profile: Record the profile is its own door; the service refuses it with every other
    problem). Re-adding a version entered in error restores it with the facts it had: a group the user filled in (any of its fields)
    replaces that group whole, as typed, so a kept fact never contradicts a typed one (the toast says so). Refusals come back
    under their fields, with what was typed."""
    dm = _model(pk)
    profile = request.GET.get("profile") == "1"
    if profile:
        existing = SoftwareRelease.objects.filter(device_model=dm, version_key="", removed_on__isnull=True).first()
        if existing is not None and request.method != "POST":  # recorded meanwhile (a panel left open): edit it, posting to Edit
            params = {k: v for k, v in request.GET.items() if k != "profile"}
            action = reverse("web:cybersecurity_release_edit", args=[existing.pk]) + _qs(params)
            return _release_modal(request, dm, ReleaseForm(initial=release_initial(existing), profile=True), existing, action)
    if request.method != "POST":
        return _release_modal(request, dm, ReleaseForm(profile=profile))
    form = ReleaseForm(request.POST, profile=profile)
    row = None
    restoring = filled = False
    if form.is_valid():
        kwargs = form.service_kwargs()
        key = cyber.version_key(kwargs.get("version", ""))
        restoring = bool(key) and SoftwareRelease.objects.filter(device_model=dm, version_key=key, removed_on__isnull=False).exists()
        if restoring:  # the facts it had come back: a group filled in (any of its fields) replaces that group whole, as typed
            kept = {"version"} | {name for group in GROUPS.values() if any(str(kwargs.get(f, "")).strip() for f in group) for name in group}
            kwargs = {name: value for name, value in kwargs.items() if name in kept}
            filled = len(kwargs) > 1
        try:
            row = cyber.add_release(dm, by=request.user, today=timezone.localdate(), profile=profile, **kwargs)
        except (ValidationError, PermissionDenied) as e:
            form.add_service_errors(e)
    if row is None:
        return _release_modal(request, dm, form)
    if row.is_profile:
        return _saved(request, dm, f"{dm.manufacturer} {dm.model}: profile recorded")
    if restoring:
        return _saved(request, dm, f"Version {row.version} {RESTORED_WORDS}{RESTORED_OVER if filled else ''}")
    return _saved(request, dm, f"Version {row.version} added")


@web_view(MODULE, EDIT)
def release_edit(request, pk):
    """Edit a release or the profile: GET the modal with its fields, POST services.update_release with every field as typed (the
    version only while no device names it: the service says so)."""
    release = _release(pk)
    dm = release.device_model
    if request.method != "POST":
        return _release_modal(request, dm, ReleaseForm(initial=release_initial(release), profile=release.is_profile), release)
    form = ReleaseForm(request.POST, profile=release.is_profile)
    saved = None
    if form.is_valid():
        try:
            saved = cyber.update_release(release, by=request.user, today=timezone.localdate(), **form.service_kwargs())
        except (ValidationError, PermissionDenied) as e:
            form.add_service_errors(e)
    if saved is None:
        return _release_modal(request, dm, form, release)
    if saved.is_profile:
        return _saved(request, dm, f"{dm.manufacturer} {dm.model}: profile saved")
    return _saved(request, dm, f"Version {saved.version} saved")


def _remove_blocker(release, user) -> str:
    """Why Entered in error cannot be done now: the service's own refusal (devices on site naming it, as the panel counts them), said
    before it is tried, or "" (a security decision's number only for a reader of decisions: the service words it by `user`)."""
    return cyber.removal_refusal(release, by=user)


@web_view(MODULE, EDIT)
def release_remove(request, pk):
    """Entered in error: GET the modal saying what it does (or why it cannot now), POST services.remove_release. The row stays (its
    history keeps it), hidden from the lists and pickers; adding the same version again restores it."""
    release = _release(pk)
    dm = release.device_model
    ctx = {"dm": dm, "release": release, "blocker": _remove_blocker(release, request.user), "error": "", "action": request.get_full_path()}
    if request.method != "POST":
        return render(request, REMOVE, ctx)
    try:
        cyber.remove_release(release, by=request.user, today=timezone.localdate())
    except (ValidationError, PermissionDenied) as e:
        return render(request, REMOVE, {**ctx, "blocker": "", "error": " ".join(getattr(e, "messages", [str(e)]))})
    return _saved(request, dm, f"Version {release.version} marked entered in error")


# --- Set devices to this release ---------------------------------------------------------------------------------------------------

def _set_choices(dm) -> tuple[list, list]:
    """The model's versioned releases, and the departments with devices of the model on site ([(id, name)])."""
    on = rules.on_site().filter(device_model=dm)
    departments = Department.objects.filter(pk__in=on.values("department_id")).order_by("name").values_list("pk", "name")
    return rules.releases(dm), [(str(pk), name) for pk, name in departments]


@web_view(MODULE, EDIT)
def set_release(request, pk):
    """Set devices to this release (services.record_facts_for): every device of the model on site, or one department's, to one of its
    versioned releases, since a day, with a network fact when chosen. Refusals come back under their fields; saved, the toast counts
    the devices that changed. From an advisory's page (`?advisory=<number>&from=advisory`, slice 40: "Record their releases" beside a
    count of devices with none recorded), saved, it answers with the advisory's page (views_cyber_advisories.advisory_saved) instead of
    the panel, and opened directly it shows that page; the advisory is read from the address only (opened_with: without a lock, never
    posted or carried forward; unknown, or entered in error, a 404)."""
    from . import views_cyber_advisories  # imported here: that module reads this one's helpers

    if not cyber_perms.can_edit_devices(request.user):
        raise PermissionDenied(cyber.FACTS_PERMISSION)
    dm = _model(pk)
    advisory = views_cyber_advisories.opened_with(request)
    answer_there = advisory is not None and views_cyber_advisories.answers_here(request)
    if answer_there and request.method != "POST" and not request.htmx:
        return redirect(views_cyber_advisories.advisory_url(advisory.number))
    releases, departments = _set_choices(dm)
    ctx = {"dm": dm, "releases": releases, "action": request.get_full_path(), "on_site": rules.on_site().filter(device_model=dm).count()}
    if request.method != "POST":
        chosen = request.GET.get("release", "")
        initial = {"network": KEEP_NETWORK, "release": chosen if chosen in {str(r.pk) for r in releases} else ""}
        return render(request, SET_RELEASE, {**ctx, "form": SetReleaseForm(initial=initial, releases=releases, departments=departments)})
    form = SetReleaseForm(request.POST, releases=releases, departments=departments)
    changed = total = None
    if form.is_valid():
        dept = form.department_id()
        if dept and dept not in form.department_ids:
            form.add_error("department", "Choose every department, or one from the list.")
        else:
            devices = rules.on_site().filter(device_model=dm)
            if dept:
                devices = devices.filter(department_id=dept)
            ids = list(devices.order_by("pk").values_list("pk", flat=True))
            try:
                changed = cyber.record_facts_for(dm, ids, by=request.user, today=timezone.localdate(), **form.service_kwargs())
                total = len(ids)
            except (ValidationError, PermissionDenied) as e:
                form.add_service_errors(e)
    if changed is None:
        form.focus_first_error()
        return render(request, SET_RELEASE, {**ctx, "form": form})
    version = SoftwareRelease.objects.get(pk=form.service_kwargs()["release"]).version
    if changed == 0:
        message = f"Nothing changed: {'the device was' if total == 1 else f'all {total} devices were'} already on {version} as chosen"
    else:
        message = f"{_plural(changed, 'device')} of {dm.manufacturer} {dm.model} set to {version}"
        if total > changed:
            message += f" ({total - changed} already were)"
    if answer_there:
        return views_cyber_advisories.advisory_saved(request, advisory, message)
    return _saved(request, dm, message)


# --- Models ------------------------------------------------------------------------------------------------------------------------

def _models_context(request) -> dict:
    """Every model with devices on site or anything recorded (a profile, a release not entered in error), by manufacturer and model; a
    search (each word in the manufacturer or the model) finds any of the facility's models, to start a profile. One page's releases in
    one query."""
    q = parse_search(request.GET)
    qs = DeviceModel.objects.all()
    if q:
        for word in q.split():
            qs = qs.filter(Q(manufacturer__icontains=word) | Q(model__icontains=word))
    else:
        recorded = SoftwareRelease.objects.filter(removed_on__isnull=True).values("device_model_id")
        qs = qs.filter(Q(pk__in=rules.on_site().values("device_model_id")) | Q(pk__in=recorded))
    qs = qs.annotate(on_site=Count("assets", filter=~Q(assets__status=AssetStatus.RETIRED))).order_by(Lower("manufacturer"), Lower("model"), "pk")
    page = Paginator(qs, PAGE_SIZE).get_page(request.GET.get("page"))
    held: dict = {}
    for r in SoftwareRelease.objects.filter(device_model__in=list(page.object_list), removed_on__isnull=True).order_by("version_key"):
        held.setdefault(r.device_model_id, []).append(r)
    rows = []
    for dm in page.object_list:
        rows_held = held.get(dm.pk, [])
        profile_row = next((r for r in rows_held if r.is_profile), None)
        v = rules.facts(None, profile_row).values
        rows.append({"dm": dm, "profile": profile_row is not None, "versions": [r.version for r in rows_held if not r.is_profile],
                     "mds2": _mds2_words(dm, rows_held), "connects": _label("connects", v.get("connects")),
                     "patient": _label("patient_data", v.get("patient_data")), "updates_by": _label("updates_by", v.get("updates_by")),
                     "segmented": _label("segmented", v.get("segmented")), "url": reverse("web:cybersecurity_model", args=[dm.pk])})
    return {**_shell(request, "models"), "list_url": reverse("web:cybersecurity_models"), "q": q, "page": page, "rows": rows}


@web_view(MODULE, VIEW)
def models_tab(request):
    ctx = _models_context(request)
    if is_partial(request, "cy-models"):
        return render(request, MODELS, ctx)
    return render(request, PAGE, ctx)


# --- Changes for IT ----------------------------------------------------------------------------------------------------------------

def _changes_context(request) -> dict:
    today = timezone.localdate()
    days = parse_days(request.GET)
    page = Paginator(rules.changes_for_it(today, days), PAGE_SIZE).get_page(request.GET.get("page"))
    return {**_shell(request, "changes"), "list_url": reverse("web:cybersecurity_changes"), "days": days, "windows": rules.CHANGES_DAYS,
            "start": today - timedelta(days=days - 1), "today": today, "page": page}


@web_view(MODULE, VIEW)
def changes(request):
    """Changes for IT: devices whose model can connect, or can store or handle patient data (as disclosed), or recorded as connected
    here, added, marked missing, retired, or returned to their owner in the last 7, 30, or 90 days (rules.changes_for_it)."""
    _need_devices(request.user)
    ctx = _changes_context(request)
    if is_partial(request, "cy-changes"):
        return render(request, CHANGES, ctx)
    return render(request, PAGE, ctx)


CHANGE_COLUMNS = ["Day", "Tag", "Manufacturer", "Model", "Department", "Room", "What changed", "Connection"]


@web_view(MODULE, VIEW)
def changes_csv(request):
    """Changes for IT for the window asked (`?days=`) as CSV: the list's columns, nothing more."""
    _need_devices(request.user)
    today = timezone.localdate()
    days = parse_days(request.GET)
    rows = ((c.day, c.asset.tag, c.asset.device_model.manufacturer, c.asset.device_model.model, c.asset.department.name, c.asset.room, c.words,
             c.connection.words if c.connection is not None else "") for c in rules.changes_for_it(today, days))
    return csv_response(f"cadence-changes-for-it-{days}-days-{today:%Y-%m-%d}.csv", CHANGE_COLUMNS, rows)

