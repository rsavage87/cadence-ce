"""
The device drawer's Record facts (slice 37): one device's software release, since when, how it is connected here, and the day someone
checked, through apps.cybersecurity.services.record_facts (which refuses in words keyed by field, and checks the levels itself too).
Mounted at /security/devices/<tag>/facts/ (urls_cybersecurity's "cybersecurity_facts").

Who: Device security Edit and Equipment View (cybersecurity.permissions.can_edit_devices) for the modal and its save; never a scoped user
(web_view, closed by default). The same URL serves the Security tab's history pages (?history=1&older=..., "Show older") to anyone who
reads the device_security area (Device security View and Equipment View): the drawer's own ?older= is its History tab's.

- GET: the modal (web/_asset_cyber_facts.html, into #modal-card): the versions of the device's model on file (never one entered in
  error; "Not recorded" clears it), running it since (blank: today when the release changes, else as recorded), connected here (blank:
  as the model discloses), checked on (today by default). Opened directly (no HTMX), it shows the device's drawer on its Security tab.
- POST: every field as typed to the service. A refusal comes back under its field with what was typed kept (one about the device, such
  as a retired one, at the top); saved, the drawer re-renders on the Security tab, a toast says what changed, "cyber-changed" fires
  for any list showing the register, and the modal closes after settle.

Another facility's device is a 404 (get_asset); another facility's release, or any release not of this device's model, is refused by
the service ("Choose a version of this device's model.").
"""
from django import forms
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django_htmx.http import retarget, trigger_client_event

from apps.cybersecurity import permissions as cyber_perms
from apps.cybersecurity import rules
from apps.cybersecurity import services as cyber
from apps.cybersecurity.models import DeviceSecurity, Network

from . import asset_cyber, history_tabs
from .decorators import web_view
from .htmx import toast
from .views import asset_drawer_context, get_asset

MODAL = "web/_asset_cyber_facts.html"
DRAWER = "web/_asset_drawer.html"
CHANGED = "cyber-changed"  # the register changed: the Device security screen's lists refetch themselves (views_cybersecurity.CHANGED)
DATE = {"type": "date"}


class FactsForm(forms.Form):
    """What Record facts posts, as typed: the service checks every value and words every refusal (release ids resolved through this
    facility, days, the network choice), so the fields here only carry them."""

    release = forms.CharField(label="Software release", required=False, widget=forms.Select)
    release_since = forms.CharField(label="Running it since", required=False, widget=forms.DateInput(attrs=DATE),
                                    help_text="Leave blank: today when the release changes, else as recorded.")
    network = forms.CharField(label="Connected here", required=False, widget=forms.Select)
    checked_on = forms.CharField(label="Checked on", required=False, widget=forms.DateInput(attrs=DATE),
                                 help_text="The day someone checked this unit (today by default).")
    FIELDS = ("release", "release_since", "network", "checked_on")

    def __init__(self, *args, asset, standing: rules.DeviceStanding, today, **kwargs):
        initial = {"release": str(standing.release.pk) if standing.release is not None else "", "network": standing.network,
                   "checked_on": today.isoformat()}
        super().__init__(*args, initial=initial, **kwargs)
        dm = asset.device_model
        versions = rules.releases(asset.device_model_id)
        self.fields["release"].widget.choices = [("", rules.RELEASE_NOT_RECORDED), *((str(r.pk), r.version) for r in versions)]
        self.fields["release"].help_text = (f"The versions of {dm.manufacturer} {dm.model} on file." if versions else
                                            f"No version of {dm.manufacturer} {dm.model} is on file yet: add one on the Device security "
                                            "screen's Models tab.")
        disclosed = rules.connected(None, standing.facts).words
        self.fields["network"].widget.choices = [("", f"As the model discloses ({disclosed})"), *Network.choices]

    def facts(self) -> dict:
        """The service's arguments: the release id or None (Release not recorded clears it), the days and the network as typed."""
        d = self.cleaned_data
        return {"release": d["release"] or None, "release_since": d["release_since"], "network": d["network"], "checked_on": d["checked_on"]}

    def add_service_errors(self, error: ValidationError) -> None:
        """The service's refusals under the fields they name; one about the device (or anything else) at the top."""
        if not hasattr(error, "error_dict"):
            self.add_error(None, error.messages)
            return
        for key, messages in error.message_dict.items():
            self.add_error(key if key in self.FIELDS else None, messages)

    def focus_first_error(self) -> None:
        first = next((name for name in self.FIELDS if name in self.errors), None)
        if first:
            for field in self.fields.values():
                field.widget.attrs.pop("autofocus", None)
            self.fields[first].widget.attrs["autofocus"] = True


def _drawer_url(asset) -> str:
    return reverse("web:asset", args=[asset.tag]) + f"?tab={asset_cyber.TAB}"


def _history(request, asset):
    """A page of the facts' history (the Security tab's Show older): the entries alone, or the drawer opened directly."""
    history_tabs.require(request.user, asset_cyber.AREA)
    if not request.htmx:
        return redirect(_drawer_url(asset))
    record = DeviceSecurity.objects.filter(asset_id=asset.pk).first()
    if record is None:
        return render(request, history_tabs.ENTRIES, {"hist": {"rows": [], "more_url": None, "first": True}})
    return history_tabs.entries_response(request, record, asset_cyber.AREA, asset_cyber.facts_url(asset), tab=False)


def _modal(request, asset, form: FactsForm):
    if form.is_bound:
        form.focus_first_error()
    return render(request, MODAL, {"asset": asset, "form": form})


def _saved(request, asset, message: str):
    """Saved: the device's drawer on its Security tab, freshly read, the toast, then the modal closes (after settle: closing it first
    would detach the form that sent this request and cancel the swap)."""
    request.GET = request.GET.copy()
    request.GET["tab"] = asset_cyber.TAB
    response = retarget(render(request, DRAWER, asset_drawer_context(request, get_asset(request, asset.tag))), "#drawer")
    trigger_client_event(response, CHANGED, {})
    toast(response, message)
    return trigger_client_event(response, "modal-close", {}, after="settle")


def _message(asset, before, sec) -> str:
    if sec.release_id == before:
        return f"Facts recorded for {asset.tag}"
    if sec.release_id is None:
        return f"Facts recorded for {asset.tag}; release cleared"
    return f"Facts recorded for {asset.tag}; release set to {sec.release.version}"


@web_view(cyber_perms.MODULE, cyber_perms.VIEW_LEVEL)
def record_facts(request, tag):
    asset = get_asset(request, tag)
    if history_tabs.asked(request, tab=False):
        return _history(request, asset)
    if not cyber_perms.can_edit_devices(request.user):
        raise PermissionDenied
    if request.method != "POST" and not request.htmx:
        return redirect(_drawer_url(asset))
    today = timezone.localdate()
    standing = rules.device_standing(asset, today)
    if request.method != "POST":
        return _modal(request, asset, FactsForm(asset=asset, standing=standing, today=today))
    form = FactsForm(request.POST, asset=asset, standing=standing, today=today)
    sec = None
    if form.is_valid():
        try:
            sec = cyber.record_facts(asset, by=request.user, today=today, **form.facts())
        except ValidationError as e:
            form.add_service_errors(e)
    if sec is None:
        return _modal(request, asset, form)
    return _saved(request, asset, _message(asset, standing.release.pk if standing.release is not None else None, sec))
