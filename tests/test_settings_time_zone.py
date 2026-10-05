"""
Settings, Time zone (slice 21, part C): the facility's time zone (Tenant.timezone) on the Settings screen and /api/v1/settings/,
through apps.facility.services.set_time_zone. Settings Edit changes it (the director by default), Settings View reads it; only a
zone the server knows is taken, offered with the US zones first and each zone's offset now; the change is audited (the facility's
own history: who, before and after), read in Users and access's change log as the Facility area by Settings View holders, and
never another facility's. The clock is pinned at 08:30 UTC on Monday, Oct 5 (04:30 in New York, daylight saving).
"""
import json
import re
from datetime import datetime
from datetime import timezone as dt_timezone

import pytest
from django.utils import timezone
from django.utils.html import escape
from pg_helpers import as_app_role, needs_postgres

from apps.accounts.models import Level, Module, Role, User
from apps.core import history
from apps.facility import services as fs
from apps.tenants.context import tenant_context
from apps.tenants.models import Tenant
from apps.web import views_settings

NOW = datetime(2026, 10, 5, 8, 30, tzinfo=dt_timezone.utc)
PAGE, LOG, API, LOG_API = "/settings/", "/users/log/", "/api/v1/settings/", "/api/v1/change-log/"
HX = {"HTTP_HX_REQUEST": "true"}
US = ["America/New_York", "America/Chicago", "America/Denver", "America/Phoenix", "America/Los_Angeles", "America/Anchorage", "Pacific/Honolulu"]
REFUSED = "is not a time zone. Choose one from the list, like America/Chicago."


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(timezone, "now", lambda: NOW)


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug):
        user = make_user(role_slug)
        client.force_login(user)
        return user

    return _as


def _toast(r) -> str:
    return json.loads(r["HX-Trigger"])["toast"]["value"]


def _select(html: str) -> str:
    start = html.index('<select id="set-time-zone"')
    return html[start:html.index("</select>", start)]


def _options(html: str) -> list[tuple[str, str, bool]]:
    """(zone, label, selected) for each option of the time zone list, in order."""
    return [(value, label, bool(selected)) for value, selected, label in
            re.findall(r'<option value="([^"]*)"( selected)?>([^<]*)</option>', _select(html))]


def _zone(tenant) -> str:
    return Tenant.objects.get(pk=tenant.pk).timezone


# --- the panel --------------------------------------------------------------------------------------------------------------------

def test_the_panel_lists_the_us_zones_first_each_with_its_offset_now(client, signed_in, ctx):
    signed_in("director")
    r = client.get(PAGE)
    html = r.content.decode()
    assert r.status_code == 200 and 'id="set-zone"' in html and "<h2>Time zone</h2>" in html
    options = _options(html)
    assert [zone for zone, _label, _on in options[:7]] == US
    assert options[0] == ("America/New_York", "Eastern (America/New_York) · UTC-4", True)  # the facility's, chosen
    assert [zone for zone, _label, on in options if on] == ["America/New_York"]
    labels = {zone: label for zone, label, _on in options}
    assert labels["America/Phoenix"] == "Arizona (America/Phoenix) · UTC-7"  # no daylight saving: an hour from Denver in summer
    assert labels["America/Denver"] == "Mountain (America/Denver) · UTC-6"
    assert labels["Pacific/Honolulu"] == "Hawaii (Pacific/Honolulu) · UTC-10"
    assert labels["Asia/Kolkata"] == "Asia/Kolkata · UTC+5:30" and labels["Europe/London"] == "Europe/London · UTC+1"
    assert labels["America/Argentina/Buenos_Aires"] == "America/Argentina/Buenos Aires · UTC-3" and labels["UTC"] == "UTC · UTC"
    rest = [zone for zone, _label, _on in options[7:]]
    assert rest == sorted(rest) and not set(US) & set(rest) and len(rest) > 400
    assert "US/Eastern" not in labels and "EST5EDT" not in labels and "Etc/GMT+5" not in labels  # aliases are not offered
    assert '<optgroup label="United States">' in html and '<optgroup label="Other time zones">' in html
    assert "It is 4:30 AM on Monday, October 5 there now." in html
    assert ("It decides what counts as today (overdue PMs and work orders, due dates, contract days left, report periods), the times "
            "shown on every screen, and when the facility's daily jobs and emails run.") in html
    assert "the daily jobs and emails follow it from the next day." in html
    assert f'hx-post="{PAGE}" hx-target="#set-zone"' in html and "Save time zone" in html


def test_an_alias_set_through_the_api_shows_as_set(client, signed_in, ctx, tenant):
    fs.set_time_zone(tenant, "US/Eastern")
    signed_in("director")
    options = _options(client.get(PAGE).content.decode())
    assert options[7] == ("US/Eastern", "US/Eastern · UTC-4", True)  # heads the other zones


# --- who may change it --------------------------------------------------------------------------------------------------------------

def test_the_director_sets_the_time_zone_and_the_next_request_works_in_it(client, signed_in, ctx, tenant, monkeypatch):
    signed_in("director")
    r = client.post(PAGE, {"time_zone": "America/Los_Angeles"}, **HX)
    html = r.content.decode()
    assert r.status_code == 200 and _toast(r) == "Time zone set to Pacific (America/Los_Angeles)"
    assert html.count('id="set-zone"') == 1 and "<h1>" not in html  # the panel, swapped in place
    assert [zone for zone, _label, on in _options(html) if on] == ["America/Los_Angeles"]
    assert "Pacific (America/Los_Angeles)</span>" in html and "It is 1:30 AM on Monday, October 5 there now." in html
    assert _zone(tenant) == "America/Los_Angeles"
    seen = []
    real = views_settings.fs.risk_summary
    monkeypatch.setattr(views_settings.fs, "risk_summary", lambda: seen.append(timezone.get_current_timezone_name()) or real())
    assert client.get(PAGE).context["zone"] == "America/Los_Angeles" and seen == ["America/Los_Angeles"]
    again = client.post(PAGE, {"time_zone": "America/Los_Angeles"}, **HX)
    assert _toast(again) == "The time zone is already Pacific (America/Los_Angeles)"
    assert Tenant.history.filter(id=tenant.id).count() == 2  # added with the facility, and the one change; the same zone again is none
    assert client.post(PAGE, {"time_zone": "Pacific/Honolulu"}).status_code == 302  # without htmx, back to the page


def test_any_letter_case_names_the_zone(client, signed_in, ctx, tenant):
    signed_in("director")
    r = client.post(PAGE, {"time_zone": "  america/chicago "}, **HX)
    assert _toast(r) == "Time zone set to Central (America/Chicago)" and _zone(tenant) == "America/Chicago"


@pytest.mark.parametrize("posted, message", [
    ("Mars/Olympus_Mons", f"Mars/Olympus_Mons {REFUSED}"),
    ("Eastern", f"Eastern {REFUSED}"),
    ("UTC-5", f"UTC-5 {REFUSED}"),
    ("x" * 61, f"That {REFUSED}"),
    ("Europe/London\x00", f"That {REFUSED}"),
    ("", "Choose a time zone, like America/Chicago."),
    ("   ", "Choose a time zone, like America/Chicago."),
    (None, "Choose a time zone, like America/Chicago."),  # nothing posted at all
])
def test_a_name_that_is_not_a_zone_is_refused_in_plain_words(client, signed_in, ctx, tenant, posted, message):
    signed_in("director")
    r = client.post(PAGE, {} if posted is None else {"time_zone": posted}, **HX)
    html = r.content.decode()
    assert r.status_code == 200 and _toast(r) == message
    assert escape(message) in html and 'aria-invalid="true"' in html and 'aria-describedby="set-zone-err set-zone-now"' in html
    assert [zone for zone, _label, on in _options(html) if on] == ["America/New_York"]  # the saved zone, still chosen
    assert _zone(tenant) == "America/New_York" and Tenant.history.filter(id=tenant.id).count() == 1


def test_settings_view_reads_the_panel_and_cannot_change_it(client, signed_in, ctx, tenant):
    signed_in("manager")  # Settings View
    html = client.get(PAGE).content.decode()
    assert 'name="time_zone" aria-describedby="set-zone-now" disabled>' in html
    assert "Save time zone" not in html and "hx-post" not in html
    assert "It is 4:30 AM on Monday, October 5 there now." in html
    assert client.post(PAGE, {"time_zone": "America/Chicago"}, **HX).status_code == 403
    assert _zone(tenant) == "America/New_York"


@pytest.mark.parametrize("role", ["technician", "analyst", "requester", "vendor"])
def test_roles_without_settings_cannot_change_it(client, signed_in, ctx, tenant, role):
    signed_in(role)
    assert client.post(PAGE, {"time_zone": "America/Chicago"}, **HX).status_code == 403
    assert _zone(tenant) == "America/New_York"


def test_signed_out_goes_to_sign_in(client, ctx, tenant):
    r = client.post(PAGE, {"time_zone": "America/Chicago"})
    assert r.status_code == 302 and "/login/" in r["Location"] and _zone(tenant) == "America/New_York"


def test_settings_edit_is_what_it_takes(client, ctx, tenant):
    role = Role.objects.create(name="Facility admin", slug="facility-admin")
    role.set_levels({Module.SETTINGS: Level.EDIT})
    client.force_login(User.objects.create_user(username="fa@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role))
    assert _toast(client.post(PAGE, {"time_zone": "America/Denver"}, **HX)) == "Time zone set to Mountain (America/Denver)"


def test_the_service_works_only_inside_the_facility_it_changes(tenant, other_tenant):
    with pytest.raises(RuntimeError):
        fs.set_time_zone(tenant, "America/Chicago")  # no facility
    with tenant_context(other_tenant), pytest.raises(RuntimeError):
        fs.set_time_zone(tenant, "America/Chicago")  # another facility's
    assert _zone(tenant) == "America/New_York"


# --- the audit: the change log's Facility area ----------------------------------------------------------------------------------------

def test_the_change_reads_in_the_change_log_with_who_before_and_after(client, signed_in, ctx, tenant, make_user):
    kim = signed_in("director")
    client.post(PAGE, {"time_zone": "Pacific/Honolulu"}, **HX)
    row = Tenant.history.filter(id=tenant.id).latest()
    assert (row.history_type, row.timezone, row.history_user_id) == ("~", "Pacific/Honolulu", kim.pk)
    client.force_login(make_user("manager"))  # Settings View and Users View
    r = client.get(LOG + "?area=facility")
    changed, added = r.context["entries"]
    assert (changed.area, changed.area_label, changed.record, changed.url) == ("facility", "Facility", "Riverside Regional", "/settings/")
    assert (changed.who, changed.who_id, changed.action) == ("Director User", kim.pk, "changed")
    assert [(c.field, c.before, c.after) for c in changed.changes] == [("Time zone", "Eastern (America/New_York)", "Hawaii (Pacific/Honolulu)")]
    assert added.action == "added" and ("Time zone", "", "Eastern (America/New_York)") in [(c.field, c.before, c.after) for c in added.changes]
    html = r.content.decode()
    assert '<span class="clog-old">Eastern (America/New_York)</span>' in html and '<span class="clog-new">Hawaii (Pacific/Honolulu)</span>' in html
    data = client.get(LOG_API + "?area=facility").json()["results"]
    assert data[0]["area"] == "facility" and data[0]["changes"] == [{"field": "Time zone", "before": "Eastern (America/New_York)",
                                                                     "after": "Hawaii (Pacific/Honolulu)"}]
    assert ("facility", "Facility") in client.get(LOG).context["areas"]


def test_a_renamed_facility_reads_in_the_change_log(ctx, tenant, make_user):
    admin = User.objects.create_superuser(username="root@cadence.example", password="Test-Pass-2026-x", first_name="Pat", last_name="Admin")
    tenant.name = "Riverside Regional Medical Center"
    tenant._history_user = admin
    tenant.save()
    entries, _ = history.change_log(make_user("manager"), areas=["facility"])
    assert entries[0].record == "Riverside Regional Medical Center" and entries[0].who == "Pat Admin"
    assert [(c.field, c.before, c.after) for c in entries[0].changes] == [("Name", "Riverside Regional", "Riverside Regional Medical Center")]


def test_only_settings_view_reads_the_facility_area(ctx, tenant, make_user):
    role = Role.objects.create(name="Equipment auditor", slug="equipment-auditor")
    role.set_levels({Module.USERS: Level.VIEW, Module.EQUIPMENT: Level.VIEW})
    auditor = User.objects.create_user(username="audit@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role)
    fs.set_time_zone(tenant, "America/Chicago", by=make_user("director"))
    assert "facility" not in [a.key for a in history.readable_areas(auditor)]
    assert history.change_log(auditor, areas=["facility"]) == ([], None)
    assert "facility" in [a.key for a in history.readable_areas(make_user("manager"))]


def test_another_facilitys_changes_never_show(ctx, tenant, other_tenant, make_user):
    with tenant_context(other_tenant):
        fs.set_time_zone(other_tenant, "Europe/London")
    other_tenant.name = "Their Hospital"
    other_tenant.save()
    fs.set_time_zone(tenant, "America/Chicago")
    manager = make_user("manager")
    entries, _ = history.change_log(manager, areas=["facility"])
    assert {e.record for e in entries} == {"Riverside Regional"} and len(entries) == 2  # added, and its own change
    assert "Europe/London" not in str([c.after for e in entries for c in e.changes])
    with tenant_context(None):
        assert history.change_log(manager, areas=["facility"]) == ([], None)  # no facility, no history


def test_a_facility_with_no_history_yet_gets_its_before(ctx, tenant, make_user):
    """A facility added before its changes were kept: the first change records it as it stood (dated when it was added) first."""
    Tenant.history.filter(id=tenant.id).delete()
    kim = make_user("director")
    fs.set_time_zone(tenant, "America/Denver", by=kim)
    rows = list(Tenant.history.filter(id=tenant.id).order_by("history_id"))
    assert [(r.history_type, r.timezone, r.history_user_id) for r in rows] == [("+", "America/New_York", None), ("~", "America/Denver", kim.pk)]
    assert rows[0].history_date == tenant.created_at and rows[0].history_change_reason == fs.HISTORY_BASELINE_REASON
    changed, added = history.change_log(kim, areas=["facility"])[0]
    assert [(c.field, c.before, c.after) for c in changed.changes] == [("Time zone", "Eastern (America/New_York)", "Mountain (America/Denver)")]
    assert (added.action, added.who, added.reason) == ("added", "Cadence", fs.HISTORY_BASELINE_REASON)
    fs.set_time_zone(tenant, "America/Chicago", by=kim)
    assert Tenant.history.filter(id=tenant.id, history_type="+").count() == 1  # recorded once


# --- the API --------------------------------------------------------------------------------------------------------------------------

def test_the_api_reads_and_patches_the_time_zone(client, ctx, tenant, make_user):
    kim = make_user("director")
    client.force_login(kim)
    assert client.get(API).json()["time_zone"] == "America/New_York"
    r = client.patch(API, {"time_zone": "America/Chicago"}, content_type="application/json")
    assert r.status_code == 200 and r.json()["time_zone"] == "America/Chicago" and _zone(tenant) == "America/Chicago"
    assert Tenant.history.filter(id=tenant.id).latest().history_user_id == kim.pk
    assert fs.get_settings()._state.adding  # only the time zone was sent: the settings row was not written
    refused = client.patch(API, {"time_zone": "Mars/Olympus_Mons"}, content_type="application/json")
    assert refused.status_code == 400 and refused.json() == {"time_zone": [f"Mars/Olympus_Mons {REFUSED}"]}
    both = client.patch(API, {"time_zone": "America/Denver", "target_pm_pct": "20"}, content_type="application/json")
    assert both.status_code == 400 and set(both.json()) == {"target_pm_pct"}
    assert _zone(tenant) == "America/Chicago"  # all or nothing: the refused target undid the time zone
    saved = client.patch(API, {"time_zone": "America/Denver", "target_pm_pct": "97"}, content_type="application/json")
    assert saved.status_code == 200 and saved.json()["time_zone"] == "America/Denver" and saved.json()["target_pm_pct"] == "97.00"
    assert client.patch(API, {"timezone": "America/Denver"}, content_type="application/json").json() == {"detail": "Unknown settings: timezone."}
    client.force_login(make_user("manager"))
    assert client.get(API).json()["time_zone"] == "America/Denver"
    assert client.patch(API, {"time_zone": "America/Chicago"}, content_type="application/json").status_code == 403
    assert _zone(tenant) == "America/Denver"


# --- under row-level security ----------------------------------------------------------------------------------------------------------

@needs_postgres
def test_changing_the_time_zone_on_the_screen_as_the_runtime_role(client, ctx, tenant, other_tenant, make_user):
    with tenant_context(other_tenant):
        fs.set_time_zone(other_tenant, "Europe/London")
    kim = make_user("director")
    client.force_login(kim)
    as_app_role()
    r = client.post(PAGE, {"time_zone": "America/Chicago"}, **HX)
    assert r.status_code == 200 and _toast(r) == "Time zone set to Central (America/Chicago)"
    assert _zone(tenant) == "America/Chicago" and Tenant.history.filter(id=tenant.id).latest().history_user_id == kim.pk
    assert [zone for zone, _label, on in _options(client.get(PAGE).content.decode()) if on] == ["America/Chicago"]
    entries = client.get(LOG + "?area=facility").context["entries"]
    assert {e.record for e in entries} == {"Riverside Regional"}
    assert [(c.field, c.before, c.after) for c in entries[0].changes] == [("Time zone", "Eastern (America/New_York)", "Central (America/Chicago)")]
    api = client.patch(API, {"time_zone": "Pacific/Honolulu"}, content_type="application/json")
    assert api.status_code == 200 and api.json()["time_zone"] == "Pacific/Honolulu" and _zone(tenant) == "Pacific/Honolulu"
