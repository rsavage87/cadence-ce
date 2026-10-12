"""
Slice 31, naming the vendor's engineer on a work order, the screens (wave B): the completion modal's "Who did the work" on vendor
service of any type (one select: not named on the report, the vendor's engineers, other vendors' engineers for CE only, someone else
with a name box; each option's standing for CE, names only for a vendor's own account) beside the vendor's report number; each choice
through it, the stored engineer prefilled on a reopen, and the refusals in words with what was chosen and typed kept; the drawer's
"Field engineer" row (the evidence on file for CE) and its Change (levels, Approve once closed, never a scoped user); the print; My
work's Complete for a vendor's account. The last test runs them under the row-level security policies.
"""
import json
import re
import uuid
from datetime import date, timedelta
from pathlib import Path

import pytest
from django.utils import timezone
from pg_helpers import as_app_role, needs_postgres
from repair_fixtures import CHECKED, checked_post
from vendor_fixtures import VENDOR, attestation, engineer, training, vendor_job

from apps.equipment.models import Asset, DeviceModel, RiskClass
from apps.tenants.context import tenant_context
from apps.vendors import rules
from apps.vendors.models import VendorEngineer
from apps.web.forms_wo_complete import KEEP_CHOICE, OTHER
from apps.workorders.completion import complete_work_order
from apps.workorders.models import Priority, RepairCheck, Source, WorkOrder, WoStatus, WoType
from apps.workorders.services import assign, change_status, create_work_order

HX = {"HTTP_HX_REQUEST": "true"}
CSS = Path(__file__).resolve().parent.parent / "apps/web/static/web/cadence.css"
WHO_HINT = "The engineer responsible, as the vendor's report names them; when a trainee came with a trained engineer, the trained engineer."


def vendor_wo(asset, *, vendor=VENDOR, type_=WoType.REPAIR, start=True, tech=None):
    wo = create_work_order(asset=asset, type=type_, priority=Priority.NORMAL, problem="Alarm fault")
    if tech is not None:
        assign(wo, technician=tech)
    elif vendor:
        assign(wo, vendor_name=vendor)
    if start:
        change_status(wo, WoStatus.IN_PROGRESS)
    return wo


def url(wo, what="complete") -> str:
    return f"/work-orders/{wo.number}/{what}/"


def triggers(r, header="HX-Trigger") -> dict:
    return json.loads(r[header]) if header in r else {}


def select_of(body: str) -> str:
    """The engineer select's markup."""
    return re.search(r'<select id="w[ce]-vendor_engineer".*?</select>', body, re.S).group(0)


def options(body: str) -> list[tuple[str, str, bool]]:
    """(value, text, selected) of each option of the engineer select, in order."""
    return [(v, t.replace("&#x27;", "'"), bool(s)) for v, s, t in re.findall(r'<option value="([^"]*)"( selected)?>([^<]*)</option>', select_of(body))]


def groups(body: str) -> list[str]:
    return [g.replace("&#x27;", "'") for g in re.findall(r'<optgroup label="([^"]*)">', select_of(body))]


def chosen(body: str) -> str:
    return next(v for v, _t, s in options(body) if s)


@pytest.fixture
def monitor_model(ctx):
    return DeviceModel.objects.create(manufacturer="Philips", model="IntelliVue MX750", description="Patient monitor", category="Patient monitors",
                                      risk_class=RiskClass.HIGH, oem_pm_interval_months=12, expected_life_years=8, list_cost=21000)


@pytest.fixture
def monitor(ctx, dept, monitor_model):
    return Asset.objects.create(tag="CE-20001", device_model=monitor_model, department=dept, acquisition_cost=21000,
                                next_pm_on=date.today() + timedelta(days=60))


@pytest.fixture
def today(ctx):
    return timezone.localdate()


@pytest.fixture
def staff(ctx, monitor_model, today):
    """Philips' Dana Kim (training on file on the monitor through a year ahead) and Lee Park (none), TechCare's Sam Ortiz."""
    dana, lee, sam = engineer("Dana Kim"), engineer("Lee Park"), engineer("Sam Ortiz", company="TechCare")
    training(dana, monitor_model, expires_on=today + timedelta(days=365))
    return {"dana": dana, "lee": lee, "sam": sam}


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug, **fields):
        user = make_user(role_slug)
        for k, v in fields.items():
            setattr(user, k, v)
        user.save()
        client.force_login(user)
        return user

    return _as


def done_job(asset, eng=None, *, reference="FSO-118204", vendor=VENDOR, **extra):
    """A vendor repair completed checked and ready for use, naming `eng`, with the vendor's report number."""
    wo = vendor_wo(asset, vendor=vendor)
    complete_work_order(wo, resolution="Replaced the display board", vendor_engineer=eng, checks_reference=reference, **{**CHECKED, **extra})
    wo.refresh_from_db()
    return wo


# --- the modal: who did the work -------------------------------------------------------------------------------------------------


def test_ce_chooses_among_the_vendors_engineers_then_others_each_with_its_standing(client, signed_in, monitor, staff, today):
    wo = vendor_wo(monitor)
    signed_in("manager")
    body = client.get(url(wo), **HX).content.decode()
    through = rules.fmt(today + timedelta(days=365))
    assert options(body) == [
        ("", "Not named on the report", True),  # chosen to begin with: nobody is preselected
        (str(staff["dana"].pk), f"Dana Kim · training on file through {through}", False),
        (str(staff["lee"].pk), "Lee Park · nothing on file for this model", False),
        (str(staff["sam"].pk), "Sam Ortiz (TechCare) · nothing on file for this model", False),
        (OTHER, "Someone else: type the name", False),
    ]
    assert groups(body) == ["Philips's engineers", "Other vendors' engineers"]
    assert "Who did the work" in body and WHO_HINT in body and 'name="engineer_name"' in body and 'class="fld wc-eng-name"' in body
    # the vendor's report number beside it, offered on every vendor job, after the repair's outcome and before its checks
    assert 'class="full wc-reference"' in body and "Optional, but required with Checked by the vendor" in body
    assert body.index("After the repair") < body.index("Who did the work") < body.index('name="checks_reference"') < body.index("Checks after repair")
    for word in ("trained", "qualified", "verified", "certified"):
        assert word not in select_of(body).lower()


def test_an_attestation_and_an_expired_training_read_as_what_is_on_file(client, signed_in, monitor, monitor_model, staff, today):
    lapsed = engineer("Ira Stone")
    training(lapsed, monitor_model, issued_on=today - timedelta(days=800), expires_on=today - timedelta(days=30))
    wo = vendor_wo(monitor)
    signed_in("manager")
    texts = dict((v, t) for v, t, _s in options(client.get(url(wo), **HX).content.decode()))
    assert texts[str(lapsed.pk)] == f"Ira Stone · nothing on file for this model (training expired {rules.fmt(today - timedelta(days=30))})"
    attestation("Philips Healthcare", "Philips field service")  # the vendor's letter, under its contract's name and its own
    texts = dict((v, t) for v, t, _s in options(client.get(url(wo), **HX).content.decode()))
    assert texts[str(staff["lee"].pk)] == "Lee Park · Philips Healthcare's attestation on file"
    assert texts[str(lapsed.pk)] == "Ira Stone · Philips Healthcare's attestation on file"
    assert texts[str(staff["dana"].pk)].startswith("Dana Kim · training on file through")  # her own training first


def test_an_inactive_engineer_is_offered_only_when_named_already(client, signed_in, monitor, staff):
    from apps.vendors.services import update_engineer

    wo = done_job(monitor, staff["lee"])
    update_engineer(staff["lee"], is_active=False)
    fresh = vendor_wo(monitor)
    signed_in("manager")
    assert str(staff["lee"].pk) not in [v for v, _t, _s in options(client.get(url(fresh), **HX).content.decode())]
    change_status(wo, WoStatus.IN_PROGRESS)  # reopened: Lee is named on it
    body = client.get(url(wo), **HX).content.decode()
    assert (str(staff["lee"].pk), "Lee Park · no longer active · nothing on file for this model", True) in options(body)


@pytest.mark.parametrize("type_", [WoType.PM, WoType.INSPECTION, WoType.RECALL])
def test_every_vendor_job_asks_who_did_the_work_and_takes_the_report_number(client, signed_in, monitor, staff, type_):
    if type_ == WoType.INSPECTION:
        Asset.objects.filter(pk=monitor.pk).update(awaiting_inspection=True, next_pm_on=None)
    wo = vendor_wo(monitor, type_=type_)
    signed_in("manager")
    body = client.get(url(wo), **HX).content.decode()
    assert "Who did the work" in body and 'name="checks_reference"' in body and "As printed on the report they left. Optional." in body
    data = {"resolution": "Vendor visit", "vendor_engineer": str(staff["dana"].pk), "checks_reference": "FSO-77"}
    data.update({WoType.PM: {"pm_result": "pass"}, WoType.INSPECTION: {"inspection_result": "passed"}}.get(type_, {}))
    r = client.post(url(wo), data, **HX)
    assert r["HX-Retarget"] == "#drawer", r.content.decode()[:2000]
    wo.refresh_from_db()
    assert (wo.status, wo.vendor_engineer, wo.checks_reference) == (WoStatus.COMPLETED, staff["dana"], "FSO-77")


def test_in_house_work_asks_neither(client, signed_in, monitor, techs, staff):
    wo = vendor_wo(monitor, tech=techs["dana"])
    signed_in("manager")
    body = client.get(url(wo), **HX).content.decode()
    assert "Who did the work" not in body and 'name="vendor_engineer"' not in body and 'name="checks_reference"' not in body
    # what a form not offered sends is never read: no refusal, nothing kept
    r = client.post(url(wo), checked_post(resolution="Fixed", vendor_engineer=str(staff["dana"].pk), checks_reference="FSO-1"), **HX)
    assert r["HX-Retarget"] == "#drawer"
    wo.refresh_from_db()
    assert (wo.status, wo.vendor_engineer, wo.checks_reference) == (WoStatus.COMPLETED, None, "")


# --- each choice ------------------------------------------------------------------------------------------------------------------


def test_naming_an_engineer_saves_them_and_the_drawer_shows_the_evidence(client, signed_in, monitor, monitor_model, staff, today):
    wo = vendor_wo(monitor)
    signed_in("manager")
    r = client.post(url(wo), checked_post(resolution="Replaced the display board", vendor_engineer=str(staff["dana"].pk),
                                          checks_reference="FSO-118204"), **HX)
    assert r["HX-Retarget"] == "#drawer"
    wo.refresh_from_db()
    assert (wo.vendor_engineer, wo.checks_reference, wo.repair_check) == (staff["dana"], "FSO-118204", RepairCheck.PASSED)
    body = r.content.decode()
    row = body[body.index('<div id="wo-eng"'):]
    row = row[:row.index("</dd></div>")]
    assert ">Dana Kim (Philips)</a>" in row and f'href="/contracts/vendors/engineers/{staff["dana"].pk}/"' in row
    assert (f"Training on file: Philips IntelliVue MX750, Manufacturer&#x27;s (OEM) factory training, through "
            f"{rules.fmt(today + timedelta(days=365))}") in row
    assert "<dt>Vendor's report</dt><dd class=\"mono\">FSO-118204</dd>" in body
    assert "Checked by Dana Kim (Philips) on" in body  # the drawer's checks after repair name who did them (checked_by)


def test_someone_else_adds_the_vendors_engineer_named_from_this_work_order(client, signed_in, monitor, staff):
    wo = vendor_wo(monitor)
    user = signed_in("manager")
    r = client.post(url(wo), checked_post(resolution="Fixed", vendor_engineer=OTHER, engineer_name="  Jo   Rivera "), **HX)
    assert r["HX-Retarget"] == "#drawer"
    wo.refresh_from_db()
    jo = wo.vendor_engineer
    assert (jo.name, jo.company, jo.created_by) == ("Jo Rivera", VENDOR, user)
    assert jo.history.earliest().history_change_reason == f"Added at completion of {wo.number}"
    # a name already on file (any letter case) is that engineer, not a second one
    again = vendor_wo(monitor)
    client.post(url(again), checked_post(resolution="Fixed", vendor_engineer=OTHER, engineer_name="dana KIM"), **HX)
    again.refresh_from_db()
    assert again.vendor_engineer == staff["dana"] and VendorEngineer.objects.filter(company_key="philips").count() == 3


def test_not_named_on_the_report_saves_nobody_and_never_blocks(client, signed_in, monitor, staff):
    wo = vendor_wo(monitor)
    signed_in("manager")
    r = client.post(url(wo), checked_post(resolution="Fixed", vendor_engineer=""), **HX)
    assert r["HX-Retarget"] == "#drawer"
    wo.refresh_from_db()
    assert wo.status == WoStatus.COMPLETED and wo.vendor_engineer is None and wo.checks_reference == ""
    row = r.content.decode().split('<div id="wo-eng"')[1].split("</dd></div>")[0]
    assert '<span class="muted">Not named</span>' in row and "No training or attestation on file" in row


def test_a_reopened_job_prefills_its_engineer_and_choosing_again_replaces_it(client, signed_in, monitor, staff):
    wo = done_job(monitor, staff["dana"])
    change_status(wo, WoStatus.IN_PROGRESS)
    signed_in("manager")
    body = client.get(url(wo), **HX).content.decode()
    assert chosen(body) == str(staff["dana"].pk)
    assert '<input type="text" name="checks_reference" value="FSO-118204"' in body  # the report number stored, too
    client.post(url(wo), checked_post(resolution="Fixed again", vendor_engineer=str(staff["sam"].pk), checks_reference="FSO-2"), **HX)
    wo.refresh_from_db()
    assert (wo.status, wo.vendor_engineer, wo.checks_reference) == (WoStatus.COMPLETED, staff["sam"], "FSO-2")
    change_status(wo, WoStatus.IN_PROGRESS)
    client.post(url(wo), checked_post(resolution="Fixed a third time", vendor_engineer=""), **HX)
    wo.refresh_from_db()
    assert wo.vendor_engineer is None  # "Not named on the report" chosen: cleared


def test_the_report_number_is_kept_whatever_the_outcome(client, signed_in, monitor):
    wo = vendor_wo(monitor)
    signed_in("manager")
    client.post(url(wo), {"repair_check": "not_returned", "resolution": "Beyond repair", "checks_reference": "FSO-9"}, **HX)
    wo.refresh_from_db()
    assert (wo.repair_check, wo.checks_reference) == (RepairCheck.NOT_RETURNED, "FSO-9")


def test_mark_the_rest_pass_keeps_the_choice_and_the_name(client, signed_in, monitor):
    wo = vendor_wo(monitor)
    signed_in("manager")
    body = client.get(url(wo), {"fill": "pass", "repair_check": "passed", "vendor_engineer": OTHER, "engineer_name": "Jo Rivera",
                                "checks_reference": "FSO-3"}, **HX).content.decode()
    assert chosen(body) == OTHER and 'name="engineer_name" value="Jo Rivera"' in body and 'value="FSO-3"' in body


def test_keep_open_names_no_engineer_and_is_never_stopped_by_the_select(client, signed_in, monitor, staff):
    wo = vendor_wo(monitor)
    signed_in("manager")
    values = ["pass"] * 7
    values[2] = "fail"
    r = client.post(url(wo, "keep-open"), checked_post(*values, reading_3="640 µA", vendor_engineer=OTHER, engineer_name="", shown_hold="1",
                                                       hold="on"), **HX)
    assert triggers(r)["toast"]["value"].startswith(f"{wo.number} kept open: failed checks recorded")
    wo.refresh_from_db()
    assert wo.status == WoStatus.IN_PROGRESS and wo.vendor_engineer is None


# --- the refusals -----------------------------------------------------------------------------------------------------------------


def test_someone_else_with_no_name_is_refused_with_the_choice_kept(client, signed_in, monitor):
    wo = vendor_wo(monitor)
    signed_in("manager")
    body = client.post(url(wo), checked_post(resolution="Fixed", vendor_engineer=OTHER, engineer_name="  ", checks_reference="FSO-4"),
                       **HX).content.decode()
    assert "Enter the engineer&#x27;s name." in body and '<span class="err" id="wc-engineer_name_error">' in body
    assert chosen(body) == OTHER and 'value="FSO-4"' in body and 'aria-invalid="true" autofocus' in body
    wo.refresh_from_db()
    assert wo.status == WoStatus.IN_PROGRESS and not VendorEngineer.objects.exists()


@pytest.mark.parametrize("name", ["Badge 4471", "dana@philips.com", "J/R Smith", "x"])
def test_a_name_that_is_not_a_name_is_refused_in_the_rules_words(client, signed_in, monitor, name):
    wo = vendor_wo(monitor)
    signed_in("manager")
    body = client.post(url(wo), checked_post(resolution="Fixed", vendor_engineer=OTHER, engineer_name=name), **HX).content.decode()
    assert rules.NAME_RULE.replace("'", "&#x27;") in body and f'name="engineer_name" value="{name}"' in body
    assert '<span class="err" id="wc-engineer_name_error">' in body and chosen(body) == OTHER
    wo.refresh_from_db()
    assert wo.status == WoStatus.IN_PROGRESS and not VendorEngineer.objects.exists()


def test_an_engineer_not_listed_is_refused_and_the_rest_reported_with_it(client, signed_in, monitor, other_tenant):
    with tenant_context(other_tenant):
        theirs = engineer("Dana Kim")  # another facility's
    wo = vendor_wo(monitor)
    signed_in("manager")
    for value in (str(theirs.pk), str(uuid.uuid4()), "not-an-id"):
        body = client.post(url(wo), checked_post(resolution="", vendor_engineer=value), **HX).content.decode()
        assert "Choose one of the engineers listed, or type the name." in body and '<span class="err" id="wc-vendor_engineer_error">' in body
        assert "Say what was found and done." in body  # every problem at once
        assert chosen(body) == ""  # an id not offered cannot stay chosen: the select shows not named, the refusal says why
    wo.refresh_from_db()
    assert wo.status == WoStatus.IN_PROGRESS and wo.vendor_engineer is None


def test_the_report_numbers_rule_is_said_on_a_pm_too(client, signed_in, monitor):
    wo = vendor_wo(monitor, type_=WoType.PM)
    signed_in("manager")
    body = client.post(url(wo), {"pm_result": "pass", "checks_reference": "FSO 12"}, **HX).content.decode()
    assert "as it is printed, without spaces" in body and 'value="FSO 12"' in body and '<span class="err" id="wc-checks_reference_error">' in body
    wo.refresh_from_db()
    assert wo.status == WoStatus.IN_PROGRESS


# --- a vendor's own account ---------------------------------------------------------------------------------------------------------


def test_a_vendors_account_sees_its_own_engineers_by_name_only(client, signed_in, monitor, staff):
    attestation()
    wo = vendor_wo(monitor)
    signed_in("vendor", company="Philips")
    body = client.get(url(wo), **HX).content.decode()
    assert options(body) == [("", "Not named on the report", True), (str(staff["dana"].pk), "Dana Kim", False),
                             (str(staff["lee"].pk), "Lee Park", False), (OTHER, "Someone else: type the name", False)]
    assert groups(body) == ["Philips's engineers"]
    for words in ("on file", "Sam Ortiz", "TechCare", "Other vendors", "attestation", "training"):
        assert words not in select_of(body)
    assert 'name="checks_reference"' not in body  # their own completion is the record (slice 30's rule for checked by the vendor)


def test_a_vendors_account_names_its_engineer_or_types_one(client, signed_in, monitor, staff):
    wo = vendor_wo(monitor)
    user = signed_in("vendor", company="Philips")
    r = client.post(url(wo), checked_post(resolution="Fixed", vendor_engineer=str(staff["lee"].pk)), **HX)
    assert r["HX-Retarget"] == "#drawer"
    wo.refresh_from_db()
    assert wo.vendor_engineer == staff["lee"]
    typed = vendor_wo(monitor)
    client.post(url(typed), checked_post(resolution="Fixed", vendor_engineer=OTHER, engineer_name="Mo Reyes"), **HX)
    typed.refresh_from_db()
    assert (typed.vendor_engineer.name, typed.vendor_engineer.company, typed.vendor_engineer.created_by) == ("Mo Reyes", VENDOR, user)


def test_another_companys_engineer_answers_a_vendor_as_unknown(client, signed_in, monitor, staff, other_tenant):
    with tenant_context(other_tenant):
        theirs = engineer("Dana Kim")
    wo = vendor_wo(monitor)
    signed_in("vendor", company="Philips")
    answers = set()
    for value in (str(staff["sam"].pk), str(theirs.pk), str(uuid.uuid4())):
        body = client.post(url(wo), checked_post(resolution="Fixed", vendor_engineer=value), **HX).content.decode()
        answers.add(re.search(r'id="wc-vendor_engineer_error">([^<]*)<', body).group(1))
        assert "Sam Ortiz" not in body and "TechCare" not in body
    assert answers == {"Choose one of the engineers listed, or type the name."}  # the same words: nothing says the record exists
    wo.refresh_from_db()
    assert wo.status == WoStatus.IN_PROGRESS and wo.vendor_engineer is None


def test_a_vendor_completing_again_keeps_ces_choice_of_another_companys_engineer(client, signed_in, monitor, staff):
    wo = done_job(monitor, staff["sam"])  # CE named TechCare's engineer on Philips' job (a subcontractor)
    change_status(wo, WoStatus.IN_PROGRESS)
    signed_in("vendor", company="Philips")
    body = client.get(url(wo), **HX).content.decode()
    assert (KEEP_CHOICE, "Another company's engineer", True) in options(body) and "Sam Ortiz" not in body
    client.post(url(wo), checked_post(resolution="Fixed again", vendor_engineer=KEEP_CHOICE), **HX)
    wo.refresh_from_db()
    assert wo.status == WoStatus.COMPLETED and wo.vendor_engineer == staff["sam"]
    drawer = client.get(f"/work-orders/{wo.number}/", **HX).content.decode()
    assert "Another company&#x27;s engineer" in drawer and "Sam Ortiz" not in drawer and "Change</button>" not in drawer


# --- the drawer's row and Change --------------------------------------------------------------------------------------------------


def test_the_row_shows_on_vendor_service_only_and_never_on_a_cancelled_job(client, signed_in, monitor, techs):
    signed_in("manager")
    open_job = vendor_wo(monitor, start=False)
    body = client.get(f"/work-orders/{open_job.number}/", **HX).content.decode()
    assert "<dt>Field engineer</dt>" in body and "No training or attestation on file" in body and "Vendor's report" not in body
    in_house = vendor_wo(monitor, tech=techs["dana"])
    assert "<dt>Field engineer</dt>" not in client.get(f"/work-orders/{in_house.number}/", **HX).content.decode()
    change_status(open_job, WoStatus.CANCELLED)
    assert "<dt>Field engineer</dt>" not in client.get(f"/work-orders/{open_job.number}/", **HX).content.decode()


def test_the_row_judges_the_completion_day_and_says_on_file_since(client, signed_in, monitor, monitor_model, staff, today):
    lee = staff["lee"]
    wo = vendor_job(monitor, engineer=lee, completed_on=today - timedelta(days=5))
    training(lee, monitor_model, issued_on=today - timedelta(days=10), expires_on=today - timedelta(days=1))  # entered after the job
    signed_in("analyst")  # Work orders View reads the row
    row = client.get(f"/work-orders/{wo.number}/", **HX).content.decode().split('<div id="wo-eng"')[1].split("</dd></div>")[0]
    # judged on the day it was completed (the training had not expired yet), and entered since
    assert "Training on file: Philips IntelliVue MX750" in row and f"(on file since {rules.fmt(today)})" in row
    assert "Change</button>" not in row  # Work orders View only
    reopened = vendor_job(monitor, engineer=lee, completed_on=today - timedelta(days=5))
    change_status(reopened, WoStatus.IN_PROGRESS)  # open again: judged today, when that training has expired
    row = client.get(f"/work-orders/{reopened.number}/", **HX).content.decode().split('<div id="wo-eng"')[1].split("</dd></div>")[0]
    assert f"No training or attestation on file; the engineer&#x27;s training on this model expired on {rules.fmt(today - timedelta(days=1))}" in row


def test_imported_vendor_work_is_never_judged(client, signed_in, monitor, staff):
    wo = done_job(monitor, staff["dana"])
    WorkOrder.objects.filter(pk=wo.pk).update(source=Source.IMPORTED)
    signed_in("manager")
    row = client.get(f"/work-orders/{wo.number}/", **HX).content.decode().split('<div id="wo-eng"')[1].split("</dd></div>")[0]
    assert "Imported from the previous system: not judged." in row and "Training on file" not in row


def test_the_engineers_link_needs_contracts_view(client, signed_in, monitor, staff, tenant):
    from apps.accounts.models import Level, Module, Role

    wo = done_job(monitor, staff["dana"])
    role = Role.objects.get(slug="technician")
    role.set_levels({**role.levels(), Module.CONTRACTS: Level.NONE})
    signed_in("technician")
    row = client.get(f"/work-orders/{wo.number}/", **HX).content.decode().split('<div id="wo-eng"')[1].split("</dd></div>")[0]
    assert "Dana Kim (Philips)" in row and "/contracts/vendors/" not in row and "Training on file" in row  # evidence: anyone who sees all


@pytest.mark.parametrize("role, status, offered", [
    ("technician", WoStatus.IN_PROGRESS, True), ("technician", WoStatus.COMPLETED, True), ("technician", WoStatus.CLOSED, False),
    ("manager", WoStatus.CLOSED, True), ("analyst", WoStatus.COMPLETED, False), ("vendor", WoStatus.COMPLETED, False),
])
def test_change_is_offered_at_edit_and_approve_once_closed_never_to_a_vendor(client, signed_in, monitor, staff, role, status, offered):
    wo = vendor_wo(monitor)
    if status != WoStatus.IN_PROGRESS:
        complete_work_order(wo, resolution="Fixed", **CHECKED)
    if status == WoStatus.CLOSED:
        change_status(wo, WoStatus.CLOSED)
    signed_in(role, **({"company": "Philips"} if role == "vendor" else {}))
    body = client.get(f"/work-orders/{wo.number}/", **HX).content.decode()
    assert (f'hx-get="/work-orders/{wo.number}/engineer/"' in body) == offered
    r = client.get(url(wo, "engineer"), **HX)
    if role in ("analyst", "vendor"):
        assert r.status_code == 403
        assert client.post(url(wo, "engineer"), {"vendor_engineer": str(staff["dana"].pk)}, **HX).status_code == 403
    elif not offered:
        assert f"{wo.number} is closed: naming its engineer now needs Work orders Approve." in r.content.decode() and "we-form" not in r.content.decode()
        client.post(url(wo, "engineer"), {"vendor_engineer": str(staff["dana"].pk)}, **HX)
    else:
        assert 'id="we-form"' in r.content.decode()
    wo.refresh_from_db()
    assert wo.vendor_engineer is None  # nothing posted above saved


def test_change_names_the_engineer_after_the_report_arrives(client, signed_in, monitor, staff, today):
    wo = done_job(monitor)
    signed_in("technician")
    body = client.get(url(wo, "engineer"), **HX).content.decode()
    assert chosen(body) == "" and "Field engineer on" in body and "Dana Kim · training on file through" in body
    assert f"what was on file for this model on {wo.completed_on:%b} {wo.completed_on.day}, {wo.completed_on.year}, the day it was completed" in body
    r = client.post(url(wo, "engineer"), {"vendor_engineer": str(staff["dana"].pk)}, **HX)
    assert r["HX-Retarget"] == "#drawer" and triggers(r)["toast"]["value"] == f"Field engineer on {wo.number}: Dana Kim (Philips)"
    assert "wo-changed" in triggers(r) and "modal-close" in triggers(r, "HX-Trigger-After-Settle")
    wo.refresh_from_db()
    assert wo.vendor_engineer == staff["dana"]
    assert wo.history.first().history_change_reason == "Field engineer: Dana Kim"
    # prefilled the next time; Someone else; then cleared
    assert chosen(client.get(url(wo, "engineer"), **HX).content.decode()) == str(staff["dana"].pk)
    client.post(url(wo, "engineer"), {"vendor_engineer": OTHER, "engineer_name": "Jo Rivera"}, **HX)
    wo.refresh_from_db()
    assert wo.vendor_engineer.name == "Jo Rivera" and wo.vendor_engineer.company == VENDOR
    r = client.post(url(wo, "engineer"), {"vendor_engineer": ""}, **HX)
    assert triggers(r)["toast"]["value"] == f"Field engineer on {wo.number}: not named"
    wo.refresh_from_db()
    assert wo.vendor_engineer is None


def test_changes_refusals_keep_what_was_chosen(client, signed_in, monitor, staff):
    wo = done_job(monitor)
    signed_in("manager")
    body = client.post(url(wo, "engineer"), {"vendor_engineer": OTHER, "engineer_name": ""}, **HX).content.decode()
    assert "Enter the engineer&#x27;s name." in body and chosen(body) == OTHER and '<span class="err" id="we-engineer_name_error">' in body
    body = client.post(url(wo, "engineer"), {"vendor_engineer": OTHER, "engineer_name": "Badge 12"}, **HX).content.decode()
    assert "letters, spaces, and . &#x27; - , only." in body and 'value="Badge 12"' in body
    body = client.post(url(wo, "engineer"), {"vendor_engineer": str(uuid.uuid4())}, **HX).content.decode()
    assert "Choose one of the engineers listed, or type the name." in body and 'id="we-vendor_engineer_error"' in body
    wo.refresh_from_db()
    assert wo.vendor_engineer is None and not VendorEngineer.objects.filter(name="Badge 12").exists()


def test_change_says_why_not_on_in_house_or_cancelled_work_and_a_visit_opens_the_work_order(client, signed_in, monitor, techs, staff):
    signed_in("manager")
    in_house = vendor_wo(monitor, tech=techs["dana"])
    assert f"{in_house.number} is in-house work" in client.get(url(in_house, "engineer"), **HX).content.decode()
    client.post(url(in_house, "engineer"), {"vendor_engineer": str(staff["dana"].pk)}, **HX)
    cancelled = vendor_wo(monitor, start=False)
    change_status(cancelled, WoStatus.CANCELLED)
    assert f"{cancelled.number} is cancelled" in client.get(url(cancelled, "engineer"), **HX).content.decode()
    r = client.get(url(cancelled, "engineer"))
    assert r.status_code == 302 and r["Location"] == f"/work-orders/{cancelled.number}/"
    assert client.get("/work-orders/WO-99-9999/engineer/", **HX).status_code == 404
    assert not WorkOrder.objects.filter(vendor_engineer__isnull=False).exists()


# --- the print --------------------------------------------------------------------------------------------------------------------


def test_the_print_names_the_engineer_and_the_report(client, signed_in, monitor, staff):
    done = done_job(monitor, staff["dana"])
    signed_in("technician")
    body = client.get(f"/print/work-orders/{done.number}/").content.decode()
    assert '<div class="pr-eng"><dt>Field engineer</dt><dd>Dana Kim (Philips)</dd></div>' in body
    assert "<div class=\"pr-ref\"><dt>Vendor's report</dt><dd><span class=\"mono\">FSO-118204</span></dd></div>" in body
    assert "<b>Checked: ready for use</b> · checked by Dana Kim (Philips)" in body and "Training on file" not in body
    open_job = vendor_wo(monitor)
    body = client.get(f"/print/work-orders/{open_job.number}/").content.decode()
    assert '<div class="pr-eng"><dt>Field engineer</dt><dd><span class="blank"></span></dd></div>' in body
    assert "<div class=\"pr-ref\"><dt>Vendor's report</dt><dd><span class=\"blank\"></span></dd></div>" in body
    nobody = done_job(monitor, reference="")
    body = client.get(f"/print/work-orders/{nobody.number}/").content.decode()
    assert "<dt>Field engineer</dt><dd>Not named</dd>" in body and "<dt>Vendor's report</dt><dd>Not recorded</dd>" in body


def test_the_print_for_a_vendor_masks_another_companys_engineer(client, signed_in, monitor, staff):
    wo = done_job(monitor, staff["sam"])
    signed_in("vendor", company="Philips")
    body = client.get(f"/print/work-orders/{wo.number}/").content.decode()
    assert "<dt>Field engineer</dt><dd>Another company&#x27;s engineer</dd>" in body and "Sam Ortiz" not in body
    assert "checked by Another company&#x27;s engineer" in body


def test_in_house_work_prints_no_field_engineer(client, signed_in, monitor, techs):
    wo = vendor_wo(monitor, tech=techs["dana"])
    signed_in("technician")
    assert "Field engineer" not in client.get(f"/print/work-orders/{wo.number}/").content.decode()


# --- My work ----------------------------------------------------------------------------------------------------------------------


def test_my_works_complete_for_a_vendor_is_the_same_modal_names_only(client, signed_in, monitor, staff):
    wo = vendor_wo(monitor)
    signed_in("vendor", company="Philips")
    page = client.get("/my-work/", **HX).content.decode()
    assert f'hx-get="/work-orders/{wo.number}/complete/" hx-vals=\'{{"from": "my_work"}}\'' in page
    body = client.get(url(wo), {"from": "my_work"}, **HX).content.decode()
    assert '<input type="hidden" name="from" value="my_work">' in body and "Who did the work" in body
    assert [t for _v, t, _s in options(body)] == ["Not named on the report", "Dana Kim", "Lee Park", "Someone else: type the name"]
    r = client.post(url(wo), checked_post(resolution="Replaced the display board", vendor_engineer=str(staff["dana"].pk), **{"from": "my_work"}), **HX)
    assert r["HX-Reswap"] == "none" and "HX-Retarget" not in r and "modal-close" in triggers(r, "HX-Trigger-After-Settle")
    wo.refresh_from_db()
    assert wo.status == WoStatus.COMPLETED and wo.vendor_engineer == staff["dana"]
    # a refusal from My work keeps the card's from
    again = vendor_wo(monitor)
    body = client.post(url(again), checked_post(resolution="Fixed", vendor_engineer=OTHER, **{"from": "my_work"}), **HX).content.decode()
    assert "Enter the engineer&#x27;s name." in body and 'name="from" value="my_work"' in body


# --- the CSS ----------------------------------------------------------------------------------------------------------------------


def test_the_css_shows_the_name_box_with_someone_else_and_the_report_number_on_every_vendor_job():
    css = CSS.read_text()
    block = css[css.index("/* Slice 31: field engineer"):]
    assert '.wc-eng:has(select[name="vendor_engineer"] option[value="other"]:checked) .wc-eng-name{display:flex}' in block
    assert ".wc-eng .wc-eng-name{display:none}" in block and ".wc-form .wc-vendor .wc-reference{display:flex}" in block


# --- under the policies -----------------------------------------------------------------------------------------------------------


@needs_postgres
def test_the_screens_under_the_policies(client, signed_in, tenant, monitor, staff):
    """As the runtime role: the modal's options and standing, a completion naming someone else, the drawer's row, Change, and the print
    read and write in the request's facility."""
    wo = vendor_wo(monitor)
    done = done_job(monitor)
    signed_in("manager")
    as_app_role()
    body = client.get(url(wo), **HX).content.decode()
    assert "Dana Kim · training on file through" in body and "Sam Ortiz (TechCare)" in body
    r = client.post(url(wo), checked_post(resolution="Fixed", vendor_engineer=OTHER, engineer_name="Jo Rivera", checks_reference="FSO-5"), **HX)
    assert r["HX-Retarget"] == "#drawer" and "Jo Rivera (Philips)" in r.content.decode()
    r = client.post(url(done, "engineer"), {"vendor_engineer": str(staff["dana"].pk)}, **HX)
    assert triggers(r)["toast"]["value"] == f"Field engineer on {done.number}: Dana Kim (Philips)" and "Training on file" in r.content.decode()
    assert "<dt>Field engineer</dt><dd>Dana Kim (Philips)</dd>" in client.get(f"/print/work-orders/{done.number}/").content.decode()
    with tenant_context(tenant):
        wo.refresh_from_db()
        done.refresh_from_db()
        assert (wo.vendor_engineer.name, wo.checks_reference, done.vendor_engineer) == ("Jo Rivera", "FSO-5", staff["dana"])


def test_set_vendor_engineer_is_the_writer_change_calls(client, signed_in, monitor, staff, monkeypatch):
    """Change goes through workorders.services.set_vendor_engineer (the one writer after completion), with the user."""
    from apps.workorders import services

    calls = []
    real = services.set_vendor_engineer

    def spy(wo, engineer=None, **kwargs):
        calls.append((wo.number, engineer, kwargs))
        return real(wo, engineer, **kwargs)

    monkeypatch.setattr(services, "set_vendor_engineer", spy)
    wo = done_job(monitor)
    user = signed_in("manager")
    client.post(url(wo, "engineer"), {"vendor_engineer": str(staff["lee"].pk)}, **HX)
    assert calls == [(wo.number, str(staff["lee"].pk), {"engineer_name": "", "by": user})]
    wo.refresh_from_db()
    assert wo.vendor_engineer == staff["lee"]
