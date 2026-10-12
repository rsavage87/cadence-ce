"""Slice 31, wave A: the Contracts screen's Vendor staff tab, the engineer drawer, its modals, and the contract drawer's line for its
vendor. Who sees and changes what (Contracts View and Edit; scoped users and roles without Contracts View refused), each modal's
refusals with the typed values kept, vendors merged by key, the filters, the drawer's work list and links by level, full page and
partial, removal as entered in error, and the change log's three areas."""
import re
from datetime import timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from vendor_fixtures import attestation, engineer, training, vendor_job

from apps.accounts.models import Level, Module, Role, User
from apps.contracts import services as ct
from apps.contracts.models import ContractType
from apps.equipment.models import Asset, DeviceModel, RiskClass
from apps.vendors import rules
from apps.vendors import services as vs
from apps.vendors.models import Covers, EngineerTraining, TrainingSource, VendorAttestation, VendorEngineer
from apps.workorders.models import WorkOrder

HX = {"HTTP_HX_REQUEST": "true"}
TAB = "/contracts/vendors/"


def hx(target):
    return {**HX, "HTTP_HX_TARGET": target}


def today():
    return timezone.localdate()


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug, **fields):
        user = make_user(role_slug)
        for k, v in fields.items():
            setattr(user, k, v)
        if fields:
            user.save()
        client.force_login(user)
        return user

    return _as


def custom_user(client, tenant, slug, levels, **fields):
    role = Role.objects.create(name=slug.title(), slug=slug)
    role.set_levels(levels)
    user = User.objects.create_user(username=f"{slug}@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role, **fields)
    client.force_login(user)
    return user


@pytest.fixture
def monitor_model(ctx):
    return DeviceModel.objects.create(manufacturer="Philips", model="IntelliVue MX750", description="Patient monitor", category="Patient monitors",
                                      risk_class=RiskClass.HIGH)


@pytest.fixture
def monitor(ctx, dept, monitor_model):
    return Asset.objects.create(tag="CE-20001", device_model=monitor_model, department=dept, acquisition_cost=9000)


@pytest.fixture
def philips(ctx, monitor_model):
    """Dana with current training on the monitor, Lee with an expired one, and Philips' yearly letter."""
    dana = engineer("Dana Kim")
    training(dana, monitor_model, issued_on=today() - timedelta(days=300), expires_on=today() + timedelta(days=400), certificate="PH-MX-1")
    lee = engineer("Lee Park")
    training(lee, monitor_model, issued_on=today() - timedelta(days=800), expires_on=today() - timedelta(days=30))
    letter = attestation("Philips", "Philips Healthcare", reference="LTR-2026-01", valid_through=today() + timedelta(days=200))
    return {"dana": dana, "lee": lee, "letter": letter}


def body_of(r) -> str:
    assert r.status_code == 200, r.status_code
    return r.content.decode()


def section(html: str, name: str) -> str:
    """One vendor's panel on the tab, by its heading."""
    m = re.search(r'<section class="panel vs-sec"[^>]*>\s*<div class="panel-h">\s*<div class="vs-head">\s*<h2[^>]*>' + re.escape(name)
                  + r"</h2>.*?</section>", html, re.S)
    assert m, f"no section {name}"
    return m.group(0)


def headings(html: str) -> list[str]:
    return re.findall(r'<h2 id="vs-h-\d+">([^<]*)</h2>', html)


# --- who -----------------------------------------------------------------------------------------------------------------------

URLS = [("vendor_staff", {}), ("vendor_engineer_new", {}), ("vendor_training_new", {}), ("vendor_attestation_new", {})]


def _record_urls(philips):
    t = philips["dana"].trainings.first()
    return [("vendor_engineer", {"pk": philips["dana"].pk}), ("vendor_engineer_change", {"pk": philips["dana"].pk}),
            ("vendor_training_remove", {"pk": t.pk}), ("vendor_attestation_remove", {"pk": philips["letter"].pk})]


def test_contracts_view_sees_the_tab_without_the_buttons(client, signed_in, philips, monitor):
    signed_in("analyst")  # Contracts View, Work orders View
    html = body_of(client.get(TAB))
    assert "Vendor staff" in html and 'aria-current="page">Vendor staff</a>' in html
    philips_sec = section(html, "Philips")
    assert "Attestation LTR-2026-01 on file" in philips_sec and "Every device this vendor services for us" in philips_sec
    assert "Dana Kim" in philips_sec and "Lee Park" in philips_sec
    assert "Training on file: Philips IntelliVue MX750, Manufacturer&#x27;s (OEM) factory training, through" in philips_sec
    for word in ("Add engineer", "Add attestation", "Remove", "+ Engineer"):
        assert word not in html
    assert reverse("web:vendor_engineer_new") not in html and reverse("web:vendor_training_new") not in html
    # nothing here claims competence: documents on file, by their dates
    assert not re.search(r"\b(qualified|verified|certified)\b", html, re.I) and not re.search(r"\btrained\b(?! on them| for it)", html, re.I)


def test_contracts_edit_sees_the_buttons(client, signed_in, philips):
    signed_in("manager")  # Contracts Edit
    html = body_of(client.get(TAB))
    assert "Add engineer" in html and "Add attestation" in html
    sec = section(html, "Philips")
    assert f"{reverse('web:vendor_training_new')}?vendor=Philips" in sec and "Remove" in sec


@pytest.mark.parametrize("contracts_level", [Level.VIEW, Level.FULL])
def test_a_scoped_user_is_refused_everything_even_with_contracts_view(client, tenant, signed_in, philips, contracts_level):
    user = signed_in("vendor", company="Philips")
    user.role.set_levels({Module.CONTRACTS: contracts_level})
    for name, kwargs in URLS + _record_urls(philips):
        url = reverse(f"web:{name}", kwargs=kwargs)
        assert client.get(url, **HX).status_code in (403, 405), url
        assert client.post(url, {"name": "Pat Doe", "company": "Philips"}, **HX).status_code == 403, url
    assert VendorEngineer.objects.count() == 2 and EngineerTraining.objects.count() == 2 and VendorAttestation.objects.count() == 1


def test_a_role_without_contracts_view_is_refused(client, tenant, philips):
    custom_user(client, tenant, "no-contracts", {Module.CONTRACTS: Level.NONE, Module.WORKORDERS: Level.FULL, Module.EQUIPMENT: Level.FULL})
    for name, kwargs in URLS + _record_urls(philips):
        url = reverse(f"web:{name}", kwargs=kwargs)
        assert client.get(url).status_code in (403, 405), url
        assert client.post(url, **HX).status_code == 403, url


def test_contracts_view_cannot_change_anything(client, signed_in, philips, monitor_model):
    signed_in("technician")  # Contracts View
    for name, kwargs in URLS[1:] + _record_urls(philips)[1:]:
        assert client.post(reverse(f"web:{name}", kwargs=kwargs), {"name": "Pat Doe", "company": "Philips"}, **HX).status_code == 403
    assert client.get(reverse("web:vendor_engineer", args=[philips["dana"].pk]), **HX).status_code == 200
    assert VendorEngineer.objects.count() == 2 and EngineerTraining.objects.count() == 2 and VendorAttestation.objects.count() == 1


def test_the_contracts_page_has_the_tab_strip(client, signed_in, ctx):
    signed_in("analyst")
    html = body_of(client.get("/contracts/"))
    assert f'href="{TAB}"' in html and 'aria-current="page">Contracts</a>' in html


# --- vendors merged by key, the filters ------------------------------------------------------------------------------------------

def test_vendors_merge_by_key_from_contracts_engineers_attestations_and_recent_work(client, signed_in, ctx, monitor, vent):
    ct.create_contract(reference="SC-1", vendor="PHILIPS  field service", type=ContractType.OEM, start_on=today() - timedelta(days=10),
                       end_on=today() + timedelta(days=300))
    engineer("Dana Kim", company="philips")
    attestation("Philips Field Service", reference="LTR-9")
    vendor_job(monitor, vendor_name="Philips field service")
    vendor_job(vent, vendor_name="Hamilton Medical", completed_on=today() - timedelta(days=20))
    old = vendor_job(vent, vendor_name="Old Vendor Inc")  # completed more than 12 months ago: not on the tab
    WorkOrder.objects.filter(pk=old.pk).update(completed_on=today() - timedelta(days=400))
    signed_in("analyst")
    html = body_of(client.get(TAB))
    assert headings(html) == ["Hamilton Medical", "PHILIPS field service"]  # one Philips, spelled as its contract first
    sec = section(html, "PHILIPS field service")
    assert "Also written philips<" in sec and "SC-1" in sec  # spellings that differ only in letter case or spacing show once
    assert "1 vendor job in the last 12 months" in sec and "Dana Kim" in sec and "LTR-9" in sec
    hamilton = section(html, "Hamilton Medical")
    assert "Nothing on file for Hamilton Medical." in hamilton and "1 vendor job in the last 12 months" in hamilton
    assert "Old Vendor" not in html


def test_the_job_count_needs_work_orders_view(client, tenant, ctx, monitor):
    vendor_job(monitor, vendor_name="Philips")
    custom_user(client, tenant, "contracts-only", {Module.CONTRACTS: Level.VIEW})
    html = body_of(client.get(TAB))
    assert "Nothing on file for Philips" not in html and headings(html) == []  # the work order's vendor shows only through work orders
    engineer("Dana Kim")
    html = body_of(client.get(TAB))
    assert headings(html) == ["Philips"] and "vendor job" not in html


def test_the_vendor_filter_and_the_search(client, signed_in, philips, monitor, vent):
    engineer("Sam Ortiz", company="TechCare Biomedical Services")
    signed_in("analyst")
    one = body_of(client.get(TAB + "?vendor=philips  FIELD service"))  # by key
    assert headings(one) == ["Philips"] and "Showing philips FIELD service only" in one and "Show every vendor" in one
    nothing = body_of(client.get(TAB + "?vendor=Acme Imaging"))
    assert headings(nothing) == ["Acme Imaging"] and "Nothing on file for Acme Imaging." in nothing
    by_vendor = body_of(client.get(TAB + "?q=techcare", **hx("vs-body")))
    assert "<html" not in by_vendor and headings(by_vendor) == ["TechCare Biomedical Services"] and "Sam Ortiz" in by_vendor
    by_engineer = body_of(client.get(TAB + "?q=lee", **hx("vs-body")))
    assert headings(by_engineer) == ["Philips"] and "Lee Park" in by_engineer and "Dana Kim" not in by_engineer
    assert "matching “lee”" in by_engineer and "LTR-2026-01" in by_engineer  # the vendor's attestation still shows
    assert "3 vendors · 3 active engineers · 1 attestation in force today" in by_engineer  # the facility's, whatever the search
    assert "No vendor or engineer matches “zzz”." in body_of(client.get(TAB + "?q=zzz", **hx("vs-body")))


def test_chips_follow_each_records_own_dates(client, signed_in, ctx, monitor_model):
    pat = engineer("Pat Doe")
    training(pat, monitor_model, issued_on=today() - timedelta(days=10), expires_on=today() + timedelta(days=30))
    attestation("Philips", reference="OLD-1", valid_from=today() - timedelta(days=400), valid_through=today() - timedelta(days=40),
                received_on=today() - timedelta(days=400))
    attestation("Philips", reference="SOON-1", valid_through=today() + timedelta(days=20))
    attestation("Philips", reference="NEXT-1", valid_from=today() + timedelta(days=5), valid_through=today() + timedelta(days=370),
                received_on=today())
    signed_in("analyst")
    sec = section(body_of(client.get(TAB)), "Philips")
    assert f'chip warn">Expires {rules.fmt(today() + timedelta(days=30))}' in sec
    assert f'chip neutral">Ended {rules.fmt(today() - timedelta(days=40))}' in sec
    assert f'chip warn">Ends {rules.fmt(today() + timedelta(days=20))}' in sec
    assert f'chip info">Starts {rules.fmt(today() + timedelta(days=5))}' in sec


def test_the_tab_reads_a_fixed_number_of_queries(client, signed_in, ctx, monitor_model):
    signed_in("analyst")

    def count():
        with CaptureQueriesContext(connection) as q:
            assert client.get(TAB, **hx("vs-body")).status_code == 200
        return len(q.captured_queries)

    training(engineer("Dana Kim"), monitor_model)
    first = count()
    for i in range(6):
        e = engineer(f"Eng {chr(65 + i)}", company=f"Vendor {chr(65 + i)}")
        training(e, monitor_model)
        attestation(f"Vendor {chr(65 + i)}", reference=f"L-{i}", covers=Covers.MODELS, model_ids=[str(monitor_model.pk)])
    assert count() == first


# --- the engineer drawer ----------------------------------------------------------------------------------------------------------

def test_the_drawer_is_a_partial_or_the_full_page(client, signed_in, philips):
    signed_in("analyst")
    url = reverse("web:vendor_engineer", args=[philips["dana"].pk])
    partial = body_of(client.get(url, **HX))
    assert "<html" not in partial and "Dana Kim" in partial and "PH-MX-1" in partial and "Philips · vendor engineer" in partial
    page = body_of(client.get(url))
    assert "<html" in page and 'class="drawer open"' in page and "Vendor staff" in page and "Dana Kim" in page
    assert headings(page) == ["Philips", "Philips Healthcare"]  # the letter names both: it shows under each
    assert client.get(reverse("web:vendor_engineer", args=["00000000-0000-0000-0000-000000000000"]), **HX).status_code == 404


def test_the_drawers_work_list_needs_work_orders_view_and_links_devices_with_equipment_view(client, tenant, signed_in, philips, monitor):
    wo = vendor_job(monitor, engineer=philips["dana"])
    url = reverse("web:vendor_engineer", args=[philips["dana"].pk])
    signed_in("analyst")  # Work orders View, Equipment View
    html = body_of(client.get(url, **HX))
    assert "Work orders naming Dana Kim" in html and wo.number in html and f'href="{reverse("web:asset", args=[monitor.tag])}"' in html
    custom_user(client, tenant, "contracts-wo", {Module.CONTRACTS: Level.VIEW, Module.WORKORDERS: Level.VIEW})
    html = body_of(client.get(url, **HX))
    assert wo.number in html and monitor.tag in html and f'href="{reverse("web:asset", args=[monitor.tag])}"' not in html
    custom_user(client, tenant, "contracts-only", {Module.CONTRACTS: Level.VIEW})
    html = body_of(client.get(url, **HX))
    assert "Work orders naming" not in html and wo.number not in html and monitor.tag not in html


def test_the_drawer_says_when_a_completion_added_the_engineer(client, signed_in, ctx, monitor):
    wo = vendor_job(monitor, engineer_name="Robin Vale")
    robin = VendorEngineer.objects.get(name="Robin Vale")
    signed_in("director")
    html = body_of(client.get(reverse("web:vendor_engineer", args=[robin.pk]), **HX))
    assert f"Added when {wo.number} was completed" in html and "check it against the report" in html


def test_the_drawers_history_carries_its_trainings_removed_ones_too(client, signed_in, philips):
    signed_in("manager")
    dana = philips["dana"]
    t = dana.trainings.get()
    r = client.post(reverse("web:vendor_training_remove", args=[t.pk]), **HX)
    html = body_of(r)
    assert "No training on file." in html and "vendors-changed" in r.headers["HX-Trigger"] and "entered in error" in r.headers["HX-Trigger"]
    assert not EngineerTraining.objects.filter(pk=t.pk).exists()
    url = reverse("web:vendor_engineer", args=[dana.pk])
    hist = body_of(client.get(url + "?history=1", **hx("hist")))
    assert "Training removed" in hist and "Removed: entered in error" in hist and "Training added" in hist and "Added" in hist
    assert "Show changes" in body_of(client.get(url, **HX))
    custom_user(client, dana.tenant, "contracts-only", {Module.CONTRACTS: Level.VIEW})
    assert client.get(url + "?history=1", **hx("hist")).status_code == 200


# --- Add engineer, Change engineer ------------------------------------------------------------------------------------------------

def test_add_engineer_opens_their_drawer(client, signed_in, ctx):
    signed_in("manager")
    modal = body_of(client.get(reverse("web:vendor_engineer_new") + "?vendor=Philips", **HX))
    assert 'value="Philips"' in modal and 'list="vs-vendors"' in modal and "vendor credentialing system" in modal
    r = client.post(reverse("web:vendor_engineer_new"), {"name": "  Mary-Jo  O'Neil ", "company": "Philips"}, **HX)
    assert r.headers["HX-Retarget"] == "#drawer" and "modal-close" in r.headers["HX-Trigger-After-Settle"]
    assert "vendors-changed" in r.headers["HX-Trigger"] and "Mary-Jo O&#x27;Neil" in body_of(r)
    e = VendorEngineer.objects.get()
    assert (e.name, e.company, e.company_key, e.created_by.role.slug) == ("Mary-Jo O'Neil", "Philips", "philips", "manager")


@pytest.mark.parametrize("data, field, words", [
    ({"name": "Dana 2", "company": "Philips"}, "name", rules.NAME_RULE),
    ({"name": "Dana Kim", "company": ""}, "company", "Enter the vendor as your work orders and contracts name it."),
    ({"name": "kim, dana", "company": "PHILIPS field service"}, "name", "is already an engineer of"),
])
def test_add_engineer_refusals_keep_the_values(client, signed_in, ctx, data, field, words):
    engineer("Dana Kim")
    signed_in("manager")
    html = body_of(client.post(reverse("web:vendor_engineer_new"), data, **HX))
    assert "Add engineer" in html and words.replace("'", "&#x27;") in html
    shown = re.search(rf'<input[^>]*name="{field}"[^>]*><span class="err">([^<]*)</span>', html)  # under its own field
    assert shown and words.replace("'", "&#x27;") in shown.group(1)
    for value in data.values():
        if value:
            assert f'value="{value}"' in html
    assert VendorEngineer.objects.count() == 1


def test_change_engineer_saves_and_keeps_a_named_engineers_vendor(client, signed_in, philips, monitor):
    signed_in("manager")
    lee = philips["lee"]
    url = reverse("web:vendor_engineer_change", args=[lee.pk])
    modal = body_of(client.get(url, **HX))
    assert 'value="Lee Park"' in modal and "checked" in modal and "readonly" not in modal
    r = client.post(url, {"name": "Lee J. Park", "company": "TechCare"}, **HX)  # is_active unticked: inactive
    assert r.headers["HX-Retarget"] == "#drawer" and "Inactive" in body_of(r)
    lee.refresh_from_db()
    assert (lee.name, lee.company, lee.is_active) == ("Lee J. Park", "TechCare", False)
    dana = philips["dana"]
    vendor_job(monitor, engineer=dana)
    url = reverse("web:vendor_engineer_change", args=[dana.pk])
    assert "readonly" in body_of(client.get(url, **HX)) and "Named on work orders" in body_of(client.get(url, **HX))
    html = body_of(client.post(url, {"name": "Dana Kim", "company": "GE Healthcare", "is_active": "on"}, **HX))
    assert "Change engineer" in html and "named on work orders, so their company stays" in html and 'value="GE Healthcare"' in html
    html = body_of(client.post(url, {"name": "Dana @ 5", "company": "Philips", "is_active": "on"}, **HX))
    assert rules.NAME_RULE.replace("'", "&#x27;") in html and 'value="Dana @ 5"' in html
    dana.refresh_from_db()
    assert (dana.name, dana.company) == ("Dana Kim", "Philips")


# --- Add training ------------------------------------------------------------------------------------------------------------------

def _training_post(engineers, **extra):
    return {"engineers": [str(e.pk) for e in engineers], "covers": Covers.MODELS, "source": TrainingSource.OEM,
            "issued_on": (today() - timedelta(days=5)).isoformat(), **extra}


def test_add_training_from_a_vendors_section_for_several_engineers(client, signed_in, philips, monitor_model, pump_model):
    signed_in("manager")
    retired = engineer("Old Hand")
    vs.update_engineer(retired, is_active=False)
    modal = body_of(client.get(reverse("web:vendor_training_new") + "?vendor=philips field service", **HX))
    assert "Dana Kim" in modal and "Lee Park" in modal and "Old Hand" not in modal  # the vendor's active engineers
    assert "Patient monitors" in modal and not re.search(r'name="engineers" value="[^"]+" checked', modal)  # several: none ticked
    models = modal[modal.index('name="model_ids"'):]
    assert models.index('<optgroup label="Philips">') < models.index('<optgroup label="BD">')  # the vendor's own make first
    post = _training_post([philips["dana"], philips["lee"]], model_ids=[str(monitor_model.pk)], certificate="CERT-1",
                          vendor="philips field service")
    r = client.post(reverse("web:vendor_training_new"), post, **HX)
    assert r.status_code == 200 and r.content == b"" and "modal-close" in r.headers["HX-Trigger-After-Settle"]
    assert "Training on file added for 2 engineers of philips field service" in r.headers["HX-Trigger"]
    made = EngineerTraining.objects.filter(issued_on=today() - timedelta(days=5))
    assert made.count() == 2 and set(made.values_list("certificate", flat=True)) == {""}  # one engineer's: dropped for two


def test_add_training_from_an_engineer_returns_their_drawer(client, signed_in, philips, monitor_model):
    signed_in("manager")
    lee = philips["lee"]
    vs.update_engineer(lee, is_active=False)
    url = reverse("web:vendor_training_new") + f"?engineer={lee.pk}"
    modal = body_of(client.get(url, **HX))
    assert "Lee Park (inactive)" in modal and re.search(rf'value="{lee.pk}" checked', modal)
    post = _training_post([lee], covers=Covers.LINE, line="Philips|Patient monitors", certificate="PH-9", engineer=str(lee.pk))
    r = client.post(reverse("web:vendor_training_new"), post, **HX)
    assert r.headers["HX-Retarget"] == "#drawer" and "Lee Park" in body_of(r) and "Philips Patient monitors" in body_of(r)
    t = lee.trainings.get(certificate="PH-9")
    assert (t.covers, t.manufacturer, t.category, t.model_ids) == (Covers.LINE, "Philips", "Patient monitors", [])


@pytest.mark.parametrize("extra, field_words", [
    ({"engineers": []}, "Choose the engineer."),
    ({"issued_on": "2099-01-01"}, "The issue date cannot be in the future."),
    ({"issued_on": ""}, "Enter the issue date."),
    ({"expires_on": "2000-01-01"}, "The expiry date cannot be before the issue date."),
    ({"covers": Covers.LINE, "line": ""}, "Choose a product category."),
    ({"model_ids": []}, "Choose at least one device model."),
    ({"model_ids": ["not-a-model"]}, "Choose device models from the catalog."),
    ({"certificate": "PH 1"}, "without spaces"),
    ({"source": "rumour"}, "Choose where the training came from."),
])
def test_add_training_refusals_keep_the_values(client, signed_in, philips, monitor_model, extra, field_words):
    signed_in("manager")
    before = EngineerTraining.objects.count()
    post = {**_training_post([philips["dana"]], model_ids=[str(monitor_model.pk)], certificate="PH-77", vendor="Philips"), **extra}
    html = body_of(client.post(reverse("web:vendor_training_new"), post, **HX))
    assert "Add training · Philips" in html and field_words in html
    assert EngineerTraining.objects.count() == before
    if post["engineers"]:
        assert re.search(rf'value="{philips["dana"].pk}" checked', html)
    if post.get("model_ids") == [str(monitor_model.pk)]:
        assert re.search(rf'<option value="{monitor_model.pk}"[^>]*selected', html)
    if post.get("issued_on"):
        assert f'value="{post["issued_on"]}"' in html


def test_add_training_drops_the_hidden_pickers_choice(client, signed_in, philips, monitor_model):
    signed_in("manager")
    post = _training_post([philips["dana"]], covers=Covers.LINE, line="Philips|Patient monitors", model_ids=["stale-id"], vendor="Philips")
    r = client.post(reverse("web:vendor_training_new"), post, **HX)
    assert r.content == b"" and [t.model_ids for t in philips["dana"].trainings.filter(covers=Covers.LINE)] == [[]]


def test_add_training_for_a_vendor_without_engineers_says_so(client, signed_in, ctx):
    signed_in("manager")
    html = body_of(client.get(reverse("web:vendor_training_new") + "?vendor=Acme", **HX))
    assert "No active engineer on file for Acme" in html and "<form" not in html
    assert body_of(client.post(reverse("web:vendor_training_new"), {"vendor": "Acme", "covers": "models"}, **HX)).count("<form") == 0


def test_add_training_refuses_another_vendors_engineer(client, signed_in, philips, monitor_model):
    signed_in("manager")
    sam = engineer("Sam Ortiz", company="TechCare")
    post = _training_post([sam], model_ids=[str(monitor_model.pk)], vendor="Philips")
    html = body_of(client.post(reverse("web:vendor_training_new"), post, **HX))
    assert "Choose engineers on file." in html and not sam.trainings.exists()


# --- Add attestation ---------------------------------------------------------------------------------------------------------------

def test_add_attestation_with_several_names_and_the_default_period(client, signed_in, ctx, monitor_model):
    signed_in("manager")
    ct.create_contract(reference="SC-1", vendor="Philips Healthcare", type=ContractType.OEM, start_on=today(), end_on=today() + timedelta(days=30))
    modal = body_of(client.get(reverse("web:vendor_attestation_new") + "?vendor=Philips Healthcare", **HX))
    assert 'value="Philips Healthcare"' in modal and '<option value="Philips Healthcare">' in modal
    assert "on site, at its depot, or remotely" in modal and "vendor credentialing system" in modal
    received = today() - timedelta(days=40)
    r = client.post(reverse("web:vendor_attestation_new"), {
        "vendor_names": ["Philips Healthcare", "philips field service", "PHILIPS HEALTHCARE"], "covers": Covers.MODELS,
        "model_ids": [str(monitor_model.pk)], "reference": "LTR-2026-01", "received_on": received.isoformat(), "valid_from": "",
        "valid_through": ""}, **HX)
    assert r.content == b"" and "modal-close" in r.headers["HX-Trigger-After-Settle"] and "vendors-changed" in r.headers["HX-Trigger"]
    a = VendorAttestation.objects.get()
    assert a.vendor_names == ["Philips Healthcare", "philips field service"] and a.vendor_keys == ["philips healthcare", "philips"]
    from apps.pm.dates import add_months
    assert (a.received_on, a.valid_from, a.valid_through) == (received, received, add_months(received, 12) - timedelta(days=1))
    html = body_of(client.get(TAB))
    assert "LTR-2026-01" in section(html, "Philips Healthcare") and "LTR-2026-01" in section(html, "philips field service")


@pytest.mark.parametrize("extra, words", [
    ({"reference": ""}, "Enter the letter&#x27;s reference, or the contract and clause."),
    ({"reference": "LTR 1"}, "without spaces"),
    ({"received_on": "2099-01-01"}, "The day it was received cannot be in the future."),
    ({"vendor_names": ["", ""]}, "Enter the vendor as your work orders and contracts name it."),
    ({"valid_from": "2026-06-01", "valid_through": "2026-01-01"}, "The last day it covers cannot be before the first."),
    ({"covers": ""}, "Choose what it covers"),
    ({"received_on": ""}, "Enter the day it was received."),
])
def test_add_attestation_refusals_keep_the_values(client, signed_in, ctx, extra, words):
    signed_in("manager")
    post = {"vendor_names": ["Philips", "Philips Healthcare"], "covers": Covers.ALL, "reference": "LTR-7",
            "received_on": (today() - timedelta(days=3)).isoformat(), "valid_from": "", "valid_through": "", **extra}
    html = body_of(client.post(reverse("web:vendor_attestation_new"), post, **HX))
    assert "Add attestation" in html and words in html
    if extra.get("received_on") == "":
        assert "Enter the first day it covers." not in html and "Enter the last day it covers." not in html
    for name in post["vendor_names"]:
        if name:
            assert f'value="{name}"' in html
    if post["reference"]:
        assert f'value="{post["reference"]}"' in html
    assert not VendorAttestation.objects.exists()


def test_remove_an_attestation_entered_in_error_keeps_the_filters(client, signed_in, philips):
    signed_in("manager")
    letter = philips["letter"]
    r = client.post(reverse("web:vendor_attestation_remove", args=[letter.pk]) + "?vendor=Philips", **HX)
    html = body_of(r)
    assert 'id="vs-body"' in html and "<html" not in html and headings(html) == ["Philips"] and "LTR-2026-01" not in html
    assert "entered in error" in r.headers["HX-Trigger"] and "vendors-changed" not in r.headers["HX-Trigger"]
    assert not VendorAttestation.objects.exists()
    assert VendorAttestation.history.filter(id=letter.pk, history_type="-", history_change_reason="Removed: entered in error").exists()


# --- the contract drawer's line, the change log --------------------------------------------------------------------------------------

def test_the_contract_drawer_names_what_is_on_file_for_its_vendor(client, signed_in, philips, vent):
    signed_in("analyst")
    c = ct.create_contract(reference="SC-2026-118", vendor="Philips Healthcare", type=ContractType.OEM, start_on=today(),
                           end_on=today() + timedelta(days=300))
    through = rules.fmt(philips["letter"].valid_through)
    html = body_of(client.get(reverse("web:contract", args=[c.pk]), **HX))
    assert f"Philips Healthcare: attestation LTR-2026-01 on file through {through}" in html  # the letter names Philips Healthcare
    assert f'href="{TAB}?vendor=Philips+Healthcare"' in html
    other = ct.create_contract(reference="SC-2", vendor="Philips field service", type=ContractType.OEM, start_on=today(),
                               end_on=today() + timedelta(days=300))
    html = body_of(client.get(reverse("web:contract", args=[other.pk]), **HX))
    assert f"Philips field service: 1 engineer with training on file; attestation LTR-2026-01 on file through {through}" in html
    acme = ct.create_contract(reference="SC-3", vendor="Acme Imaging", type=ContractType.THIRD_PARTY, start_on=today(),
                              end_on=today() + timedelta(days=300))
    assert "Nothing on file for Acme Imaging" in body_of(client.get(reverse("web:contract", args=[acme.pk]), **HX))
    VendorAttestation.objects.all().delete()
    EngineerTraining.objects.filter(engineer=philips["dana"]).update(expires_on=today() - timedelta(days=1))
    assert "Philips field service: nothing on file in force today" in body_of(client.get(reverse("web:contract", args=[other.pk]), **HX))


def test_the_change_log_lists_the_vendor_areas(client, signed_in, philips):
    signed_in("director")
    html = body_of(client.get(reverse("web:change_log")))
    for label in ("Vendor engineers", "Vendor engineers&#x27; training", "Vendor attestations"):
        assert label in html
    entries = body_of(client.get(reverse("web:change_log") + "?area=vendor_engineers"))
    assert "Dana Kim (Philips)" in entries and reverse("web:vendor_engineer", args=[philips["dana"].pk]) in entries
    attest = body_of(client.get(reverse("web:change_log") + "?area=vendor_attestations"))
    assert "Philips, Philips Healthcare · attestation LTR-2026-01" in attest
