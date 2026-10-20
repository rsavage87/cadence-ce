"""
Slice 36, wave 2B on the web: the model drawer's Life and support section (the PM program tab: where the expected life comes from, the
manufacturer's support dates and notice, the day CE checked, and equipment.services.support_status's words for the facility's today),
its Record support modal (Equipment Edit, through update_device_model: checked on today when it opens, each refusal under its field with
what was typed kept, the drawer back on PM program with a toast), the History tab reading the new labels; and Settings' Fiscal year
panel (Settings View reads the month and the fiscal years in real dates, Settings Edit changes it through update_settings; a refusal
answers with the panel and its words). The day is pinned (the views read the facility's clock).
"""
import json
from datetime import date

import pytest
from django.utils import timezone

from apps.accounts.models import Level, Role, User
from apps.equipment import services as eq
from apps.equipment.models import DeviceModel, LifeSource
from apps.facility import services as fs
from apps.tenants.context import tenant_context

HX = {"HTTP_HX_REQUEST": "true"}
HX_DRAWER = {**HX, "HTTP_HX_TARGET": "drawer"}
TODAY = date(2026, 10, 19)
FISCAL_URL = "/settings/fiscal-year/"


@pytest.fixture(autouse=True)
def pinned(monkeypatch):
    """The facility's today is TODAY (the views and the services read the clock themselves)."""
    real = timezone.localdate
    monkeypatch.setattr(timezone, "localdate", lambda value=None, tz=None: TODAY if value is None else real(value, tz))


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug):
        user = make_user(role_slug)
        client.force_login(user)
        return user

    return _as


def model_url(dm, part=""):
    return f"/pm/models/{dm.pk}/" + (f"{part}/" if part else "")


def triggers(r, header="HX-Trigger") -> dict:
    return json.loads(r[header]) if header in r else {}


def field_error(body: str, field: str) -> str:
    marker = f'id="sp-{field}_error">'
    return body.split(marker, 1)[1].split("</span>", 1)[0] if marker in body else ""


def section(body: str) -> str:
    start = body.index('<div class="sec sp-sec" id="model-support">')
    return body[start:body.index("<h3>Devices", start)]


def support_post(**over) -> dict:
    return {"life_source": "", "sale_ended_on": "", "support_ends_on": "", "parts_end_on": "", "support_notice": "",
            "support_checked_on": TODAY.isoformat(), **over}


def stored(dm) -> tuple:
    dm = DeviceModel.objects.get(pk=dm.pk)
    return tuple(getattr(dm, name) for name in eq.SUPPORT_FIELDS)


# --- the Life and support section ----------------------------------------------------------------------------------------------------

def test_a_model_with_nothing_recorded_reads_not_checked(client, signed_in, vent_model):
    signed_in("director")
    body = client.get(model_url(vent_model), **HX).content.decode()
    sec = section(body)
    assert "<h3>Life and support <span>for replacement planning</span>" in sec
    assert (f'<button class="link sp-record" type="button" hx-get="{model_url(vent_model, "support")}" hx-target="#modal-card">'
            "Record support</button>") in sec
    assert '<dt>Support today</dt><dd><span class="chip neutral">Support not checked</span></dd>' in sec
    assert '<dt>Expected life from</dt><dd><span class="muted">Not recorded</span></dd>' in sec
    for label in ("No longer sold from", "Manufacturer support ends", "Parts available until"):
        assert f'<dt>{label}</dt><dd><span class="muted">Not announced</span></dd>' in sec
    assert '<dt>Manufacturer\'s notice</dt><dd><span class="muted">None recorded</span></dd>' in sec
    assert '<dt>Support checked</dt><dd><span class="muted">Never</span></dd>' in sec
    assert "its devices' support counts as unknown, never as 0" in sec and "Record where the expected life comes from" in sec
    # the details above keep their expected life
    assert "<dt>Expected life</dt><dd>10 yr</dd>" in body


@pytest.mark.parametrize("fields, tone, words", [
    ({"support_checked_on": date(2026, 3, 1)}, "ok", "Supported, checked Mar 1, 2026"),
    ({"support_ends_on": date(2028, 3, 31)}, "neutral", "Support ends Mar 31, 2028"),
    ({"sale_ended_on": date(2025, 1, 1)}, "neutral", "No longer sold since Jan 1, 2025"),
    ({"sale_ended_on": date(2024, 1, 1), "support_ends_on": date(2026, 10, 1)}, "warn", "Manufacturer support ended Oct 1, 2026"),
    ({"support_ends_on": date(2025, 1, 1), "parts_end_on": date(2026, 6, 1)}, "crit", "Parts no longer available after Jun 1, 2026"),
])
def test_the_section_reads_support_status_for_today(client, signed_in, vent_model, fields, tone, words):
    eq.update_device_model(vent_model, **fields)
    signed_in("director")
    sec = section(client.get(model_url(vent_model), **HX).content.decode())
    assert f'<dt>Support today</dt><dd><span class="chip {tone}">{words}</span></dd>' in sec
    assert "counts as unknown" not in sec


def test_the_section_shows_what_is_recorded(client, signed_in, vent_model):
    eq.update_device_model(vent_model, life_source=LifeSource.MANUFACTURER, sale_ended_on=date(2024, 1, 1), support_ends_on=date(2026, 10, 1),
                           parts_end_on=date(2029, 6, 30), support_notice="FSN-2025-014", support_checked_on=date(2026, 9, 2))
    signed_in("director")
    sec = section(client.get(model_url(vent_model), **HX).content.decode())
    assert "<dt>Expected life from</dt><dd>The manufacturer</dd>" in sec
    assert "<dt>No longer sold from</dt><dd>Jan 1, 2024</dd>" in sec and "<dt>Manufacturer support ends</dt><dd>Oct 1, 2026</dd>" in sec
    assert "<dt>Parts available until</dt><dd>Jun 30, 2029</dd>" in sec
    assert "<dt>Manufacturer's notice</dt><dd>FSN-2025-014</dd>" in sec and "<dt>Support checked</dt><dd>Sep 2, 2026</dd>" in sec
    assert "Record where the expected life comes from" not in sec


@pytest.mark.parametrize("role", ["analyst", "manager", "technician"])
def test_record_support_is_offered_with_equipment_edit_only(client, signed_in, vent_model, role):
    signed_in(role)  # the analyst has PM View and Equipment View; the manager and the technician Equipment Edit by default
    body = client.get(model_url(vent_model), **HX).content.decode()
    sec = section(body)
    assert "Support today" in sec
    assert ("Record support</button>" in sec) is (role != "analyst")


# --- Record support --------------------------------------------------------------------------------------------------------------------

def test_the_modal_opens_with_what_is_recorded_and_checked_on_today(client, signed_in, vent_model):
    eq.update_device_model(vent_model, life_source=LifeSource.AHA, support_ends_on=date(2028, 3, 31), support_notice="EOL-77",
                           support_checked_on=date(2025, 4, 2))
    signed_in("director")
    r = client.get(model_url(vent_model, "support"), **HX)
    body = r.content.decode()
    assert r.status_code == 200 and "<h2>Life and support · Hamilton Medical Hamilton-G5</h2>" in body
    assert f'<form id="sp-form" hx-post="{model_url(vent_model, "support")}" hx-target="#modal-card" novalidate>' in body
    assert '<option value="aha" selected>AHA estimated useful lives</option>' in body and '<option value="">Not recorded</option>' in body
    assert 'type="date" name="support_ends_on" value="2028-03-31"' in body and 'type="date" name="sale_ended_on"' in body
    assert 'name="support_notice" value="EOL-77"' in body
    assert f'type="date" name="support_checked_on" value="{TODAY.isoformat()}"' in body and f'max="{TODAY.isoformat()}"' in body
    assert "Last recorded Apr 2, 2025." in body
    assert "As recorded: Support ends Mar 31, 2028." in body  # support_status as stored, before this save
    assert "Its expected life is 10 years (Edit details changes it)." in body
    assert "<legend>The manufacturer's notice</legend>" in body and "Save support" in body


def test_saving_records_support_and_answers_with_the_drawer(client, signed_in, vent_model):
    user = signed_in("director")
    r = client.post(model_url(vent_model, "support"), support_post(life_source="manufacturer", sale_ended_on="2024-01-01",
                                                                    support_ends_on="2026-10-01", support_notice=" FSN-2025-014 ",
                                                                    support_checked_on="2026-10-12"), **HX)
    assert r.status_code == 200 and r["HX-Retarget"] == "#drawer", r.content.decode()[:500]
    t = triggers(r)
    assert t["toast"] == {"value": "Life and support saved for Hamilton Medical Hamilton-G5: Manufacturer support ended Oct 1, 2026"}
    assert "models-changed" in t and "devices-changed" not in t
    assert "modal-close" in triggers(r, "HX-Trigger-After-Settle")
    assert stored(vent_model) == ("manufacturer", date(2024, 1, 1), date(2026, 10, 1), None, "FSN-2025-014", date(2026, 10, 12))
    # the drawer is back on PM program with the new words
    body = r.content.decode()
    assert 'aria-selected="true" hx-get="' + model_url(vent_model) + '?tab=program"' in body
    assert '<span class="chip warn">Manufacturer support ended Oct 1, 2026</span>' in section(body)
    # recorded with who: the history tab reads the slice's labels
    hist = client.get(model_url(vent_model) + "?tab=history", **HX_DRAWER).content.decode()
    for label, value in [("Expected life from", "The manufacturer"), ("No longer sold from", "Jan 1, 2024"),
                         ("Manufacturer support ends", "Oct 1, 2026"), ("Manufacturer's notice", "FSN-2025-014"),
                         ("Support checked", "Oct 12, 2026")]:
        assert (f'<dt>{label}</dt><dd><span class="was">—</span> <span class="to" aria-label="changed to">→</span> {value}</dd>'
                in hist.replace("&#x27;", "'")), label
    assert str(user) in hist
    # saving the same again changes nothing, and says so
    r = client.post(model_url(vent_model, "support"), support_post(life_source="manufacturer", sale_ended_on="2024-01-01",
                                                                    support_ends_on="2026-10-01", support_notice="FSN-2025-014",
                                                                    support_checked_on="2026-10-12"), **HX)
    assert triggers(r)["toast"] == {"value": "Life and support for Hamilton Medical Hamilton-G5 unchanged"}


def test_blank_clears_what_was_recorded(client, signed_in, vent_model):
    eq.update_device_model(vent_model, life_source=LifeSource.ECRI, parts_end_on=date(2030, 1, 1), support_notice="L-1",
                           support_checked_on=date(2026, 1, 5))
    signed_in("technician")  # Equipment Edit by default
    r = client.post(model_url(vent_model, "support"), support_post(support_checked_on=""), **HX)
    assert r["HX-Retarget"] == "#drawer"
    assert stored(vent_model) == ("", None, None, None, "", None)
    assert triggers(r)["toast"] == {"value": "Life and support saved for Hamilton Medical Hamilton-G5: Support not checked"}


@pytest.mark.parametrize("field, typed, words", [
    ("support_notice", "two words", "Enter the notice&#x27;s reference as the manufacturer&#x27;s letter shows it, without spaces."),
    ("support_notice", "x" * 41, "Keep the reference to 40 characters."),
    ("support_checked_on", "2026-10-20", "The day support was checked cannot be after today."),
    ("support_ends_on", "31/03/2028", "Enter a day."),
    ("life_source", "guess", "Choose where the expected life comes from, or leave it not recorded."),
])
def test_a_refusal_shows_under_its_field_and_keeps_what_was_typed(client, signed_in, vent_model, field, typed, words):
    signed_in("director")
    r = client.post(model_url(vent_model, "support"), support_post(**{"life_source": "aha", "parts_end_on": "2030-01-01", field: typed}), **HX)
    body = r.content.decode()
    assert r.status_code == 200 and "HX-Retarget" not in r and "toast" not in triggers(r)
    assert field_error(body, field) == words
    assert f'id="sp-{field}"' in body and 'aria-invalid="true"' in body
    if field != "life_source":
        assert f'value="{typed}"' in body  # as typed
        assert '<option value="aha" selected>' in body and 'value="2030-01-01"' in body  # the rest as typed too
    assert stored(vent_model) == ("", None, None, None, "", None)  # nothing saved


def test_every_refusal_at_once_and_the_first_is_focused(client, signed_in, vent_model):
    signed_in("director")
    r = client.post(model_url(vent_model, "support"), support_post(sale_ended_on="soon", support_notice="two words",
                                                                    support_checked_on="2027-01-01"), **HX)
    body = r.content.decode()
    assert field_error(body, "sale_ended_on") == "Enter a day."
    assert field_error(body, "support_notice") and field_error(body, "support_checked_on")
    assert 'name="sale_ended_on" value="soon" autofocus' in body and body.count("autofocus") == 1
    assert stored(vent_model) == ("", None, None, None, "", None)


@pytest.mark.parametrize("role", ["analyst", "requester", "vendor"])
def test_record_support_needs_equipment_edit(client, signed_in, vent_model, role):
    signed_in(role)  # the analyst: PM View, Equipment View; the requester and the vendor: no PM View
    assert client.get(model_url(vent_model, "support"), **HX).status_code == 403
    assert client.post(model_url(vent_model, "support"), support_post(support_ends_on="2028-01-01"), **HX).status_code == 403
    assert stored(vent_model) == ("", None, None, None, "", None)


def test_a_role_with_pm_but_not_equipment_edit_is_refused(client, tenant, vent_model):
    with tenant_context(tenant):
        role = Role.objects.create(name="PM lead", slug="pm-lead")
        role.set_levels({"pm": Level.APPROVE, "equipment": Level.VIEW})
    client.force_login(User.objects.create_user(username="pmlead@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role))
    assert "Record support" not in section(client.get(model_url(vent_model), **HX).content.decode())
    assert client.post(model_url(vent_model, "support"), support_post(support_ends_on="2028-01-01"), **HX).status_code == 403


def test_another_facilitys_model_is_a_404(client, signed_in, other_tenant):
    from apps.accounts.models import create_default_roles

    create_default_roles(other_tenant)
    with tenant_context(other_tenant):
        theirs = DeviceModel.objects.create(manufacturer="X", model="Y", description="Their pump", category="C")
    signed_in("director")
    assert client.get(model_url(theirs, "support"), **HX).status_code == 404
    assert client.post(model_url(theirs, "support"), support_post(support_ends_on="2028-01-01"), **HX).status_code == 404
    with tenant_context(other_tenant):
        assert DeviceModel.objects.get(pk=theirs.pk).support_ends_on is None


def test_edit_details_and_add_model_never_take_support(client, signed_in, vent_model):
    signed_in("director")
    body = client.get(model_url(vent_model, "edit"), **HX).content.decode()
    assert "support_ends_on" not in body and "life_source" not in body
    post = {"manufacturer": vent_model.manufacturer, "model": vent_model.model, "description": vent_model.description, "category": vent_model.category,
            "oem_pm_interval_months": "6", "expected_life_years": "10", "list_cost": "38000", "support_ends_on": "2028-01-01", "life_source": "aha"}
    assert client.post(model_url(vent_model, "edit"), post, **HX)["HX-Retarget"] == "#drawer"
    assert stored(vent_model) == ("", None, None, None, "", None)


# --- Settings' Fiscal year panel ----------------------------------------------------------------------------------------------------------

def fiscal_panel(body: str) -> str:
    start = body.index('<div class="panel mt" id="set-fiscal">')
    return body[start:body.index('id="set-risk"', start) if 'id="set-risk"' in body[start:] else len(body)]


def toast_of(r) -> str:
    return triggers(r)["toast"]["value"]


def test_an_editor_sees_the_month_select_and_the_dates(client, signed_in, ctx):
    signed_in("director")
    body = client.get("/settings/").content.decode()
    assert body.index('id="set-pm-window"') < body.index('id="set-fiscal"') < body.index('id="set-risk"')  # after the PM window
    panel = fiscal_panel(body)
    assert "<h2>Fiscal year</h2><span class=\"sub\">Starts in January</span>" in panel
    assert f'<form hx-post="{FISCAL_URL}" hx-target="#set-fiscal" hx-swap="outerHTML" hx-disinherit="hx-swap hx-target">' in panel
    assert '<option value="1" selected>January (today in FY2026)</option>' in panel
    assert '<option value="7">July (today in FY2027)</option>' in panel and '<option value="10">October (today in FY2027)</option>' in panel
    assert '<option value="11">November (today in FY2026)</option>' in panel
    assert panel.count("<option") == 12
    assert "A fiscal year is named by the calendar year it ends: starting in January, it is the calendar year." in panel
    assert '<span class="fy-dates">FY2026: Jan 1, 2026 to Dec 31, 2026</span>' in panel
    assert '<span class="fy-dates">FY2027: Jan 1, 2027 to Dec 31, 2027</span>' in panel
    assert "the replacement outlook re-buckets; nothing stored moves." in panel and "Save fiscal year" in panel
    assert "Changing settings needs Settings Edit access." not in panel


def test_saving_the_month(client, signed_in, ctx):
    user = signed_in("director")
    r = client.post(FISCAL_URL, {"fiscal_year_start_month": "7"}, **HX)
    body = r.content.decode()
    assert r.status_code == 200 and toast_of(r) == "The fiscal year now starts in July: FY2027 runs Jul 1, 2026 to Jun 30, 2027"
    assert fs.get_settings().fiscal_year_start_month == 7
    assert body.lstrip().startswith('<div class="panel mt" id="set-fiscal">')  # the panel alone, swapping itself
    assert "Starts in July" in body and '<option value="7" selected>July (today in FY2027)</option>' in body
    assert '<span class="fy-dates">FY2027: Jul 1, 2026 to Jun 30, 2027</span>' in body
    assert '<span class="fy-dates">FY2028: Jul 1, 2027 to Jun 30, 2028</span>' in body
    assert "starting in January" not in body
    r = client.post(FISCAL_URL, {"fiscal_year_start_month": "7"}, **HX)
    assert toast_of(r) == "The fiscal year already starts in July"
    # recorded with who, the month by name (the Settings history, as the change log reads it)
    from apps.core.history import record_history

    entries, _ = record_history(fs.get_settings())
    entry = next(e for e in entries if any(c.field == "Fiscal year starts in" for c in e.changes))
    assert next(c.after for c in entry.changes if c.field == "Fiscal year starts in") == "July" and entry.who == str(user)
    r = client.post(FISCAL_URL, {"fiscal_year_start_month": "1"}, **HX)
    assert toast_of(r) == "The fiscal year now starts in January: FY2026 runs Jan 1, 2026 to Dec 31, 2026"
    entries, _ = record_history(fs.get_settings())
    change = next(c for c in entries[0].changes if c.field == "Fiscal year starts in")
    assert (change.before, change.after) == ("July", "January")


@pytest.mark.parametrize("bad", ["13", "0", "", "July", "7.5"])
def test_a_refused_month_answers_with_the_panel_and_its_words(client, signed_in, ctx, bad):
    fs.update_settings(fiscal_year_start_month=10)
    signed_in("director")
    r = client.post(FISCAL_URL, {"fiscal_year_start_month": bad}, **HX)
    body = r.content.decode()
    assert r.status_code == 200 and toast_of(r) == "Choose the month the fiscal year starts in."
    assert '<span class="hint err" id="set-fiscal-err">Choose the month the fiscal year starts in.</span>' in body
    assert 'aria-invalid="true"' in body and '<option value="10" selected>October (today in FY2027)</option>' in body  # as saved
    assert fs.get_settings().fiscal_year_start_month == 10
    assert client.post(FISCAL_URL, {}, **HX).status_code == 200 and fs.get_settings().fiscal_year_start_month == 10  # nothing sent


def test_a_viewer_reads_the_fiscal_year_and_cannot_change_it(client, signed_in, ctx):
    fs.update_settings(fiscal_year_start_month=7)
    signed_in("manager")  # Settings View
    body = client.get("/settings/").content.decode()
    panel = fiscal_panel(body)
    assert "Starts in July" in panel and '<span class="fy-dates">FY2027: Jul 1, 2026 to Jun 30, 2027</span>' in panel
    assert "<form" not in panel and "<select" not in panel and "hx-post" not in panel
    assert "Changing settings needs Settings Edit access." in panel
    assert client.post(FISCAL_URL, {"fiscal_year_start_month": "1"}, **HX).status_code == 403
    assert fs.get_settings().fiscal_year_start_month == 7


def test_no_settings_view_no_panel(client, signed_in, ctx):
    signed_in("technician")  # Settings: none
    assert client.get("/settings/").status_code == 403
    assert client.post(FISCAL_URL, {"fiscal_year_start_month": "7"}, **HX).status_code == 403
    assert fs.get_settings().fiscal_year_start_month == 1


def test_another_facility_keeps_its_own_fiscal_year(client, signed_in, ctx, other_tenant):
    signed_in("director")
    client.post(FISCAL_URL, {"fiscal_year_start_month": "7"}, **HX)
    with tenant_context(other_tenant):
        assert fs.get_settings().fiscal_year_start_month == 1
