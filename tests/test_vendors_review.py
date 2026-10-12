"""
Slice 31 review fixes (review31), each pinned by a test that fails without it:

1. An apostrophe typed any way (' ’ ‘ ʼ: a phone's smart punctuation) is one engineer: rules.engineer_key folds them, NAME_MARKS takes
   them, the name keeps what was typed; add_engineer and update_engineer refuse the other spelling of a name on file, engineer_named
   (completion's and Change's typed name, the API's engineer_name) reuses it, and his training covers the job in the binder.
2. A name or vendor that folds longer than the stored key's column ("İ" lowercases to two code points, "ß" casefolds to "ss") is
   refused keyed by field through every writer and the API, never a DataError on PostgreSQL.
3. The vendor's report number goes with the engineer when a reopened job moves in-house or to another vendor (workorders.services
   .assign), stays when it goes to the same vendor spelled another way; a completion then sends no number and records none, checked by
   the vendor asks for the new vendor's, and the new vendor's account never reads the old one (drawer, modal, API).
4. The merge fixes: a completion that leaves the number out keeps the one on file ("" clears it, unless required); the completion modal
   takes a vendor's own account's report number; vendors.services.may_name refuses a vendor account another company's engineer, the
   one CE named on its job too.
5. Add attestation dated in year 9999 with the period's end left blank answers the modal with a refusal, never a 500.
6. The history of trainings and attestations names their device models once per page (the change log's areas, the engineer drawer).
7. The API's vendor staff lists read a page in a fixed number of queries when the models a record names were deleted.
8. "Someone else" with no name is refused by the service, keyed engineer_name, with every other problem at once.
9. cadence.css balances its braces and the slice 31 field engineer rules are not inside a phone-only @media block.
"""
import re
import uuid
from datetime import date, timedelta
from pathlib import Path

import pytest
from django.core.exceptions import ValidationError
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from repair_fixtures import CHECKED, POST_REPAIR_ALL_PASS, checked_post
from survey_helpers import period, rows_of
from vendor_fixtures import VENDOR, attestation, engineer, training, vendor_job

from apps.equipment.models import Asset, DeviceModel, RiskClass
from apps.reports.survey import vendors as survey_vendors
from apps.vendors import rules
from apps.vendors import services as vs
from apps.vendors.models import Covers, VendorAttestation, VendorEngineer
from apps.web.forms_wo_complete import OTHER
from apps.workorders.completion import complete_work_order
from apps.workorders.models import Priority, RepairCheck, WorkOrder, WoStatus, WoType
from apps.workorders.services import assign, change_status, create_work_order, set_vendor_engineer

DAY = timedelta(days=1)
HX = {"HTTP_HX_REQUEST": "true"}
API = "/api/v1/"
ENG, TRN, ATT, WOS = f"{API}vendor-engineers/", f"{API}vendor-trainings/", f"{API}vendor-attestations/", f"{API}work-orders/"
CSS = Path(__file__).resolve().parent.parent / "apps/web/static/web/cadence.css"

LONG_NAME = "İ" * 60 + " " + "İ" * 59  # 120 letters and a space: within NAME_MAX as typed, 239 characters once lowercased
LONG_VENDOR = "ß" * 120  # within COMPANY_MAX as typed, 240 characters once casefolded
NAME_TOO_LONG = "Shorten the name: it is too long to store."
VENDOR_TOO_LONG = "Shorten the vendor's name: it is too long to store."
NAME_REQUIRED = "Enter the engineer's name."
NUMBER_REQUIRED = "Enter the vendor's service report or field service order number."
SAY_WHAT = "Say what was found and done."


@pytest.fixture
def today(ctx):
    return timezone.localdate()


@pytest.fixture
def mx750(ctx):
    return DeviceModel.objects.create(manufacturer="Philips", model="IntelliVue MX750", description="Patient monitor", category="Patient monitors",
                                      risk_class=RiskClass.HIGH, oem_pm_interval_months=12)


@pytest.fixture
def mon(ctx, dept, mx750):
    return Asset.objects.create(tag="CE-20001", device_model=mx750, department=dept, next_pm_on=date.today() + timedelta(days=60))


@pytest.fixture
def dana(ctx):
    return engineer("Dana Kim")


@pytest.fixture
def signed_in(client, make_user):
    """Sign in as a default role, with `fields` (company, department) set."""
    n = iter(range(1000))

    def _as(role_slug, **fields):
        user = make_user(role_slug, username=f"{role_slug}-{next(n)}@riverside.example")
        for k, v in fields.items():
            setattr(user, k, v)
        user.save()
        client.force_login(user)
        return user

    return _as


def errors_of(excinfo) -> dict:
    return {k: " ".join(v) for k, v in excinfo.value.message_dict.items()}


def refused(call, *args, **kwargs) -> dict:
    with pytest.raises(ValidationError) as e:
        call(*args, **kwargs)
    return errors_of(e)


def vendor_work(asset, vendor=VENDOR, *, type_=WoType.REPAIR):
    wo = create_work_order(asset=asset, type=type_, priority=Priority.NORMAL, problem="Vendor visit")
    assign(wo, vendor_name=vendor)
    change_status(wo, WoStatus.IN_PROGRESS)
    return wo


def in_house_work(asset, tech):
    wo = create_work_order(asset=asset, type=WoType.REPAIR, priority=Priority.NORMAL, problem="Alarm fault", assigned_to=tech)
    change_status(wo, WoStatus.IN_PROGRESS)
    return wo


def finish(wo, **kwargs):
    """Complete `wo`, a repair checked and ready for use unless `kwargs` say otherwise."""
    return complete_work_order(wo, **{"resolution": "Serviced by the vendor", **CHECKED, **kwargs})


def not_completed(wo, **kwargs) -> dict:
    errors = refused(finish, wo, **kwargs)
    wo.refresh_from_db()
    assert wo.status == WoStatus.IN_PROGRESS
    return errors


def reopen(wo):
    wo.refresh_from_db()
    change_status(wo, WoStatus.IN_PROGRESS)
    wo.refresh_from_db()


def stored(wo) -> tuple:
    wo.refresh_from_db()
    return wo.vendor_name, wo.vendor_engineer, wo.checks_reference


def post(client, url, body):
    return client.post(url, body, content_type="application/json")


def complete_api(client, wo, body):
    return post(client, f"{WOS}{wo.id}/transition/", {"status": "completed", **body})


def html(r) -> str:
    assert r.status_code == 200, r.status_code
    return r.content.decode()


def chosen(body: str) -> str:
    """The value chosen in the engineer select (the completion modal's wc-, Change's we-)."""
    select = re.search(r'<select id="w[ce]-vendor_engineer".*?</select>', body, re.S).group(0)
    return next(v for v, s in re.findall(r'<option value="([^"]*)"( selected)?>', select) if s)


def outside_timeline(drawer: str) -> str:
    """The work order drawer without its timeline: the status history's notes are the record of each visit as it happened (slice 30's
    completion note, "Checked by the vendor, report FSO-..."), not the work order's current report."""
    return re.sub(r'<ul class="tl">.*?</ul>', "", drawer, flags=re.S)


def queries(client, url, **headers) -> int:
    with CaptureQueriesContext(connection) as q:
        assert client.get(url, **headers).status_code == 200
    return len(q)


# --- 1. one apostrophe, any way it is typed -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("typed", ["Sean O’Brien", "Sean O‘Brien", "Sean OʼBrien", "SEAN o'brien", "O’Brien, Sean", "  Sean   O’Brien "])
def test_an_apostrophe_typed_any_way_is_one_name_and_the_name_keeps_what_was_typed(typed):
    assert rules.engineer_key(typed) == rules.engineer_key("Sean O'Brien") == "sean o'brien"
    assert rules.clean_engineer_name(typed) == " ".join(typed.split())  # accepted (NAME_MARKS), kept as typed


def test_add_and_change_refuse_the_other_spelling_of_a_name_on_file(ctx):
    sean = vs.add_engineer(name="Sean O'Brien", company="Philips")
    exists = {"name": "Sean O'Brien is already an engineer of Philips."}
    for typed in ("Sean O’Brien", "Sean O‘Brien", "Sean OʼBrien"):
        assert refused(vs.add_engineer, name=typed, company="PHILIPS field service") == exists
    curly = vs.add_engineer(name="Mary-Jo O’Neil", company="Philips")  # typed on an iPad
    assert (curly.name, curly.name_key) == ("Mary-Jo O’Neil", "mary-jo o'neil")  # the name as typed, the key folded
    assert refused(vs.add_engineer, name="mary-jo o'neil", company="Philips") == {"name": "Mary-Jo O’Neil is already an engineer of Philips."}
    lee = vs.add_engineer(name="Lee Park", company="Philips")
    assert refused(vs.update_engineer, lee, name="Sean O’Brien") == exists  # the other spelling of another engineer's name
    assert VendorEngineer.objects.get(pk=lee.pk).name == "Lee Park"
    vs.update_engineer(sean, name="Sean O’Brien")  # his own name retyped the other way: still him
    sean.refresh_from_db()
    assert (sean.name, sean.name_key) == ("Sean O’Brien", "sean o'brien")
    assert list(vs.engineers_for("Philips")) == [lee, curly, sean]


def test_completion_and_change_name_the_engineer_on_file_and_his_training_covers_the_job(mon, mx750, today, make_user):
    sean = engineer("Sean O'Brien")
    training(sean, mx750, issued_on=today - 100 * DAY, expires_on=today + 100 * DAY)
    assert vs.engineer_named("Philips field service", "Sean O’Brien") == sean
    typed = vendor_job(mon, engineer_name="Sean O’Brien", completed_on=today - DAY)  # completion's "Someone else"
    assert typed.vendor_engineer == sean
    later = vendor_job(mon, completed_on=today)
    set_vendor_engineer(later, engineer_name="Sean OʼBrien", by=make_user("manager"))  # the drawer's Change
    assert WorkOrder.objects.get(pk=later.pk).vendor_engineer == sean
    assert VendorEngineer.objects.count() == 1
    s = survey_vendors.build(period(today - 200 * DAY, today, today), None)
    jobs = {r[0]: r for r in rows_of(s, "jobs")}
    for wo in (typed, later):
        assert jobs[wo.number][6] == "Sean O'Brien (Philips)"
        assert jobs[wo.number][8].startswith("Training on file: Philips IntelliVue MX750"), jobs[wo.number][8]
    assert {f.label: f.value for f in s.figures}["Nothing on file"] == 0 and s.gaps == []


def test_the_api_and_the_screens_take_a_curly_apostrophe_as_the_engineer_on_file(client, signed_in, mon, dana):
    sean = engineer("Sean O'Brien")
    signed_in("manager")
    r = post(client, ENG, {"name": "Sean O’Brien", "company": "Philips"})
    assert r.status_code == 400 and r.json() == {"name": ["Sean O'Brien is already an engineer of Philips."]}
    r = client.post(reverse("web:vendor_engineer_new"), {"name": "Sean O’Brien", "company": "Philips"}, **HX)
    assert "Sean O&#x27;Brien is already an engineer of Philips." in html(r) and 'value="Sean O’Brien"' in html(r)
    wo = vendor_work(mon)
    r = complete_api(client, wo, {"resolution": "Replaced the board", "repair_check": "passed", "results": POST_REPAIR_ALL_PASS,
                                  "engineer_name": "Sean O’Brien"})
    assert r.status_code == 200 and r.json()["vendor_engineer"] == str(sean.id), r.content
    modal = vendor_work(mon)
    r = client.post(f"/work-orders/{modal.number}/complete/", checked_post(resolution="Fixed", vendor_engineer=OTHER,
                                                                             engineer_name="Sean O‘Brien"), **HX)
    assert r["HX-Retarget"] == "#drawer"
    assert stored(modal)[1] == sean and VendorEngineer.objects.count() == 2  # Dana and Sean


# --- 2. a key longer than its column ------------------------------------------------------------------------------------------------

def test_a_name_or_vendor_folding_past_the_column_is_refused_by_every_writer(mon, ctx):
    assert len(LONG_NAME) == 120 and len(rules.engineer_key(LONG_NAME)) > rules.KEY_MAX
    assert len(LONG_VENDOR) == 120 and len(rules.vendor_key(LONG_VENDOR)) > rules.KEY_MAX
    assert refused(rules.clean_engineer_name, LONG_NAME) == {"name": NAME_TOO_LONG}
    assert refused(vs.add_engineer, name=LONG_NAME, company="Philips") == {"name": NAME_TOO_LONG}
    assert refused(vs.add_engineer, name="Lee Park", company=LONG_VENDOR) == {"company": VENDOR_TOO_LONG}
    lee = engineer("Lee Park")
    assert refused(vs.update_engineer, lee, name=LONG_NAME) == {"name": NAME_TOO_LONG}
    assert refused(vs.update_engineer, lee, company=LONG_VENDOR) == {"company": VENDOR_TOO_LONG}
    assert refused(vs.engineer_named, "Philips", LONG_NAME) == {"engineer_name": NAME_TOO_LONG}
    assert refused(vs.engineer_named, LONG_VENDOR, "Lee Park") == {"engineer_name": VENDOR_TOO_LONG}
    # completion's typed name: refused with every other problem
    wo = vendor_work(mon)
    assert not_completed(wo, resolution="", engineer_name=LONG_NAME) == {"resolution": SAY_WHAT, "engineer_name": NAME_TOO_LONG}
    # a work order whose vendor folds past the column: a typed name has no vendor key to go under
    long_wo = vendor_work(mon, LONG_VENDOR)
    assert not_completed(long_wo, engineer_name="Pat Morgan") == {"engineer_name": VENDOR_TOO_LONG}
    assert refused(set_vendor_engineer, long_wo, engineer_name="Pat Morgan") == {"engineer_name": VENDOR_TOO_LONG}
    assert stored(long_wo)[1:] == (None, "")
    assert list(VendorEngineer.objects.values_list("name", "company")) == [("Lee Park", "Philips")]


def test_the_api_and_the_modal_refuse_them_keyed_by_field(client, signed_in, mon, ctx):
    lee = engineer("Lee Park")
    signed_in("manager")
    for body, answer in [({"name": LONG_NAME, "company": "Philips"}, {"name": [NAME_TOO_LONG]}),
                         ({"name": "Pat Morgan", "company": LONG_VENDOR}, {"company": [VENDOR_TOO_LONG]})]:
        r = post(client, ENG, body)
        assert r.status_code == 400 and r.json() == answer, body
    for body, answer in [({"name": LONG_NAME}, {"name": [NAME_TOO_LONG]}), ({"company": LONG_VENDOR}, {"company": [VENDOR_TOO_LONG]})]:
        r = client.patch(f"{ENG}{lee.id}/", body, content_type="application/json")
        assert r.status_code == 400 and r.json() == answer, body
    done = {"resolution": "Replaced the board", "repair_check": "passed", "results": POST_REPAIR_ALL_PASS}
    wo = vendor_work(mon)
    r = complete_api(client, wo, {**done, "engineer_name": LONG_NAME})
    assert r.status_code == 400 and r.json() == {"engineer_name": [NAME_TOO_LONG]}
    long_wo = vendor_work(mon, LONG_VENDOR)
    r = complete_api(client, long_wo, {**done, "engineer_name": "Pat Morgan"})
    assert r.status_code == 400 and r.json() == {"engineer_name": [VENDOR_TOO_LONG]}
    finish(wo)
    r = post(client, f"{WOS}{wo.id}/vendor-engineer/", {"engineer_name": LONG_NAME})
    assert r.status_code == 400 and r.json() == {"engineer_name": [NAME_TOO_LONG]}
    r = client.post(reverse("web:vendor_engineer_new"), {"name": LONG_NAME, "company": "Philips"}, **HX)
    assert NAME_TOO_LONG in html(r) and "Add engineer" in html(r)
    assert stored(long_wo)[1:] == (None, "") and stored(wo)[1] is None
    assert list(VendorEngineer.objects.values_list("name", "company")) == [("Lee Park", "Philips")]


def test_an_attestation_under_such_a_vendor_name_is_a_json_list_with_nothing_to_overflow(client, signed_in, ctx):
    """vendor_names and vendor_keys are JSON lists, not 120-character columns: the long name is taken as typed (no DataError on
    PostgreSQL), keyed as a work order on that vendor is."""
    signed_in("manager")
    r = post(client, ATT, {"vendor_names": [LONG_VENDOR], "covers": "all", "reference": "LTR-9", "received_on": "2026-01-05",
                           "valid_from": "2026-01-05", "valid_through": "2026-12-31"})
    assert r.status_code == 201, r.content
    assert VendorAttestation.objects.get().vendor_keys == [rules.vendor_key(LONG_VENDOR)]


# --- 3. the report number goes with the work ----------------------------------------------------------------------------------------

def test_the_report_number_goes_when_the_work_moves_and_stays_with_the_same_vendor(mon, dana, techs):
    wo = vendor_job(mon, engineer=dana, repair_check=RepairCheck.VENDOR_REPORT, results=None, checks_reference="FSO-PHILIPS-1")
    reopen(wo)
    assert stored(wo) == (VENDOR, dana, "FSO-PHILIPS-1")  # reopened: who did it and their report stay
    assign(wo, vendor_name="PHILIPS field service")  # the same vendor spelled another way: both stay
    assert stored(wo) == ("PHILIPS field service", dana, "FSO-PHILIPS-1")
    finish(wo, repair_check=RepairCheck.VENDOR_REPORT, results=None)  # nothing sent: Philips' own number is kept
    assert stored(wo) == ("PHILIPS field service", dana, "FSO-PHILIPS-1")
    reopen(wo)
    assign(wo, vendor_name="TechCare")
    assert stored(wo) == ("TechCare", None, "")
    last = wo.history.order_by("-history_date", "-history_id").first()
    assert (last.vendor_name, last.vendor_engineer_id, last.checks_reference) == ("TechCare", None, "")  # audited
    # checked by the vendor now asks for TechCare's number; a completion that sends none records none
    assert not_completed(wo, repair_check=RepairCheck.VENDOR_REPORT, results=None) == {"checks_reference": NUMBER_REQUIRED}
    finish(wo)
    assert stored(wo) == ("TechCare", None, "")
    # in-house, and back to the first vendor: the number went at the first move
    other = vendor_job(mon, engineer=dana, checks_reference="FSO-PHILIPS-2")
    reopen(other)
    assign(other, technician=techs["tom"])
    assert (other.vendor_service, *stored(other)[1:]) == (False, None, "")
    assign(other, vendor_name=VENDOR)
    finish(other)
    assert stored(other) == (VENDOR, None, "")


def test_over_the_api_the_new_vendor_neither_reads_nor_inherits_the_old_number(client, signed_in, mon, dana):
    signed_in("manager")
    wo = vendor_work(mon)
    r = complete_api(client, wo, {"resolution": "Philips replaced the board", "repair_check": "vendor_report", "checks_reference": "FSO-PHILIPS-1",
                                  "vendor_engineer": str(dana.id)})
    assert r.status_code == 200 and r.json()["checks_reference"] == "FSO-PHILIPS-1", r.content
    reopen(wo)
    r = post(client, f"{WOS}{wo.id}/assign/", {"vendor_name": "TechCare"})
    assert r.status_code == 200 and (r.json()["vendor_engineer"], r.json()["checks_reference"]) == (None, ""), r.content
    r = complete_api(client, wo, {"resolution": "TechCare replaced the board", "repair_check": "vendor_report"})
    assert r.status_code == 400 and r.json() == {"checks_reference": [NUMBER_REQUIRED]}
    signed_in("vendor", company="TechCare")
    shown = client.get(f"{WOS}{wo.id}/")
    assert shown.status_code == 200 and shown.json()["checks_reference"] == "" and "FSO-PHILIPS-1" not in shown.content.decode()
    drawer = html(client.get(f"/work-orders/{wo.number}/", **HX))
    assert "Vendor's report" not in drawer and "FSO-PHILIPS-1" not in outside_timeline(drawer)
    assert "FSO-PHILIPS-1" not in html(client.get(f"/work-orders/{wo.number}/complete/", **HX))
    printed = html(client.get(f"/print/work-orders/{wo.number}/"))  # its row blank, for TechCare's number to be written in
    assert "<div class=\"pr-ref\"><dt>Vendor's report</dt><dd><span class=\"blank\"></span></dd></div>" in printed
    r = complete_api(client, wo, {"resolution": "TechCare replaced the board", "repair_check": "passed", "results": POST_REPAIR_ALL_PASS})
    assert r.status_code == 200 and r.json()["checks_reference"] == "", r.content
    assert stored(wo) == ("TechCare", None, "")


def test_the_modal_prefills_no_earlier_vendors_number(client, signed_in, mon, dana):
    wo = vendor_job(mon, engineer=dana, repair_check=RepairCheck.VENDOR_REPORT, results=None, checks_reference="FSO-PHILIPS-1")
    reopen(wo)
    assign(wo, vendor_name="TechCare")
    signed_in("manager")
    url = f"/work-orders/{wo.number}/complete/"
    body = html(client.get(url, **HX))
    assert 'name="checks_reference"' in body and "FSO-PHILIPS-1" not in body
    body = html(client.post(url, checked_post(resolution="TechCare replaced the board", repair_check="vendor_report", vendor_engineer="",
                                              checks_reference=""), **HX))  # the box as it opened
    assert NUMBER_REQUIRED.replace("'", "&#x27;") in body and 'id="wc-checks_reference_error"' in body
    assert stored(wo) == ("TechCare", None, "") and wo.status == WoStatus.IN_PROGRESS
    drawer = html(client.get(f"/work-orders/{wo.number}/", **HX))
    assert "Vendor's report" not in drawer and "FSO-PHILIPS-1" not in outside_timeline(drawer)


# --- 4. the merge fixes ---------------------------------------------------------------------------------------------------------------

def test_a_completion_that_leaves_the_number_out_keeps_the_one_on_file(mon, dana):
    wo = vendor_job(mon, engineer=dana, checks_reference="FSO-1")
    reopen(wo)
    finish(wo)  # left out: kept
    assert wo.checks_reference == "FSO-1"
    reopen(wo)
    finish(wo, repair_check=RepairCheck.VENDOR_REPORT, results=None)  # the number on file satisfies checked by the vendor
    assert (wo.repair_check, wo.checks_reference) == (RepairCheck.VENDOR_REPORT, "FSO-1")
    reopen(wo)
    assert not_completed(wo, repair_check=RepairCheck.VENDOR_REPORT, results=None, checks_reference="") == {"checks_reference": NUMBER_REQUIRED}
    finish(wo, checks_reference="  ")  # sent blank: cleared
    assert wo.checks_reference == ""
    pm = vendor_work(mon, type_=WoType.PM)
    complete_work_order(pm, resolution="Annual PM", pm_result="pass", checks_reference="FSO-2")
    reopen(pm)
    complete_work_order(pm, resolution="Annual PM again", pm_result="pass")
    assert pm.checks_reference == "FSO-2"


def test_the_api_passes_the_number_only_when_sent(client, signed_in, mon, dana):
    signed_in("manager")
    wo = vendor_job(mon, engineer=dana, checks_reference="FSO-1")
    done = {"resolution": "Replaced the board", "repair_check": "passed", "results": POST_REPAIR_ALL_PASS}
    for body, kept in [({}, "FSO-1"), ({"checks_reference": "FSO-2"}, "FSO-2"), ({"checks_reference": None}, ""), ({"checks_reference": ""}, "")]:
        reopen(wo)
        r = complete_api(client, wo, {**done, **body})
        assert r.status_code == 200 and r.json()["checks_reference"] == kept, (body, r.content)
    reopen(wo)
    r = complete_api(client, wo, {"resolution": "Fixed", "repair_check": "vendor_report", "checks_reference": ""})
    assert r.status_code == 400 and r.json() == {"checks_reference": [NUMBER_REQUIRED]}


def test_a_vendors_own_account_records_its_report_number_from_the_modal(client, signed_in, mon, dana):
    wo = vendor_work(mon)
    signed_in("vendor", company="Philips")
    url = f"/work-orders/{wo.number}/complete/"
    assert 'name="checks_reference"' in html(client.get(url, **HX))
    r = client.post(url, checked_post(resolution="Replaced the board", vendor_engineer=str(dana.pk), checks_reference="FSO-77"), **HX)
    assert r["HX-Retarget"] == "#drawer"
    wo.refresh_from_db()
    assert (wo.status, wo.repair_check, wo.vendor_engineer, wo.checks_reference) == (WoStatus.COMPLETED, RepairCheck.PASSED, dana, "FSO-77")


def test_a_vendor_account_never_names_another_companys_engineer_even_the_one_ce_named(client, signed_in, mon, dana, make_user):
    sam = engineer("Sam Ortiz", company="TechCare")
    wo = vendor_work(mon)
    set_vendor_engineer(wo, sam, by=make_user("technician"))  # CE names a subcontractor on Philips' job
    wo.refresh_from_db()
    vendor = make_user("vendor", username="philips-vendor@riverside.example")
    vendor.company = "Philips"
    vendor.save()
    assert vs.may_name(wo, sam, make_user("manager")) and vs.may_name(wo, dana, vendor)
    assert not vs.may_name(wo, sam, vendor)
    for value in (sam, str(sam.pk)):
        errors = refused(finish, wo, vendor_engineer=value, by=vendor)
        assert errors == {"vendor_engineer": vs.CHOOSE_LISTED}
    client.force_login(vendor)
    body = html(client.post(f"/work-orders/{wo.number}/complete/", checked_post(resolution="Fixed", vendor_engineer=str(sam.pk)), **HX))
    assert "Choose one of the engineers listed, or type the name." in body and "Sam Ortiz" not in body
    assert stored(wo)[1] == sam and wo.status == WoStatus.IN_PROGRESS
    finish(wo, by=vendor)  # left out: CE's choice stays
    assert wo.vendor_engineer == sam


# --- 5. Add attestation in year 9999 --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("dates, words", [
    ({"received_on": "9999-03-01", "valid_from": "", "valid_through": ""}, "The day it was received cannot be in the future."),
    ({"received_on": "today", "valid_from": "9999-06-01", "valid_through": ""}, "Enter the last day it covers."),
])
def test_add_attestation_in_year_9999_is_refused_with_the_values_kept(client, signed_in, ctx, today, dates, words):
    signed_in("manager")
    dates = {k: today.isoformat() if v == "today" else v for k, v in dates.items()}
    r = client.post(reverse("web:vendor_attestation_new"), {"vendor_names": ["Philips"], "covers": Covers.ALL, "reference": "LTR-1", **dates}, **HX)
    body = html(r)
    assert "Add attestation" in body and words in body
    for value in dates.values():
        if value:
            assert f'value="{value}"' in body
    assert 'value="Philips"' in body and 'value="LTR-1"' in body
    assert not VendorAttestation.objects.exists()


# --- 6. the history names device models once per page ---------------------------------------------------------------------------------

def _models(n, prefix="MX"):
    return [DeviceModel.objects.create(manufacturer="Philips", model=f"{prefix}{i:02}", description="Patient monitor", category="Patient monitors",
                                       risk_class=RiskClass.MEDIUM) for i in range(n)]


def test_vendor_history_pages_take_the_same_queries_with_one_record_or_ten(client, signed_in, ctx, today, dana):
    models = _models(11)
    training(dana, models[0], issued_on=today - 100 * DAY)
    attestation("Philips", covers=Covers.MODELS, model_ids=[str(models[0].pk)], reference="LTR-0")
    signed_in("director")
    pages = [(reverse("web:vendor_engineer", args=[dana.pk]) + "?history=1", {**HX, "HTTP_HX_TARGET": "hist"}),
             (reverse("web:change_log") + "?area=engineer_training", {}), (reverse("web:change_log") + "?area=vendor_attestations", {})]
    before = [queries(client, url, **headers) for url, headers in pages]
    for i in range(1, 10):
        training(dana, models[i], models[i + 1], issued_on=today - (100 + i) * DAY)
        attestation("Philips", covers=Covers.MODELS, model_ids=[str(models[i].pk), str(models[i + 1].pk)], reference=f"LTR-{i}")
    assert [queries(client, url, **headers) for url, headers in pages] == before
    assert "Philips MX09, Philips MX10" in html(client.get(pages[1][0]))  # the latest entries, named


def test_vendor_history_lists_models_by_name_and_a_removed_one_last(client, signed_in, ctx, dana):
    def model(n, manufacturer, name):  # ids in the opposite order to the names: stored by id, read by name
        return DeviceModel.objects.create(id=uuid.UUID(int=n), manufacturer=manufacturer, model=name, description="Monitor",
                                          category="Patient monitors", risk_class=RiskClass.MEDIUM)

    zeta, alpha, gone = model(1, "Zeta", "Z1"), model(2, "Alpha", "A1"), model(3, "Gamma", "G1")
    t = training(dana, zeta, alpha, gone)
    assert t.model_ids == [str(zeta.pk), str(alpha.pk), str(gone.pk)]
    attestation("Philips", covers=Covers.MODELS, model_ids=t.model_ids, reference="LTR-1")
    gone.delete()
    signed_in("director")
    words = "Alpha A1, Zeta Z1, a removed model"
    assert words in html(client.get(reverse("web:change_log") + "?area=engineer_training"))
    assert words in html(client.get(reverse("web:change_log") + "?area=vendor_attestations"))
    hist = html(client.get(reverse("web:vendor_engineer", args=[dana.pk]) + "?history=1", **HX, HTTP_HX_TARGET="hist"))
    assert words in hist and "Gamma" not in hist


# --- 7. the API lists when the models they name were deleted ----------------------------------------------------------------------

def test_the_api_lists_read_deleted_models_in_a_fixed_number_of_queries(client, signed_in, ctx, today, dana):
    signed_in("manager")

    def records_naming_a_deleted_model(i):
        m = _models(1, prefix=f"Gone{i}-")[0]
        training(dana, m, issued_on=today - (10 + i) * DAY)
        attestation("Philips", covers=Covers.MODELS, model_ids=[str(m.pk)], reference=f"LTR-{i}")
        m.delete()

    records_naming_a_deleted_model(0)
    before = {url: queries(client, url) for url in (ENG, TRN, ATT)}
    for i in range(1, 6):
        records_naming_a_deleted_model(i)
    assert {url: queries(client, url) for url in (ENG, TRN, ATT)} == before
    assert {t["coverage"] for t in client.get(TRN).json()["results"]} == {"a removed model"}
    assert {a["coverage"] for a in client.get(ATT).json()["results"]} == {"a removed model"}


# --- 8. "Someone else" with no name ---------------------------------------------------------------------------------------------------

def test_the_service_refuses_someone_else_with_no_name_with_every_other_problem(mon, techs, make_user):
    wo = vendor_work(mon)
    assert not_completed(wo, resolution="", vendor_engineer=vs.SOMEONE_ELSE, engineer_name="  ") == {
        "resolution": SAY_WHAT, "engineer_name": NAME_REQUIRED}
    assert not_completed(wo, vendor_engineer=vs.SOMEONE_ELSE) == {"engineer_name": NAME_REQUIRED}
    assert not VendorEngineer.objects.exists() and stored(wo)[1] is None
    finish(wo, vendor_engineer=vs.SOMEONE_ELSE, engineer_name="Jo Rivera")
    jo = wo.vendor_engineer
    assert (jo.name, jo.company) == ("Jo Rivera", VENDOR)
    assert refused(set_vendor_engineer, wo, vs.SOMEONE_ELSE, engineer_name="", by=make_user("manager")) == {"engineer_name": NAME_REQUIRED}
    assert stored(wo)[1] == jo
    own = in_house_work(mon, techs["dana"])
    assert not_completed(own, vendor_engineer=vs.SOMEONE_ELSE) == {"vendor_engineer": vs.ENGINEER_ONLY}
    assert not_completed(own, vendor_engineer=vs.SOMEONE_ELSE, engineer_name="Jo Rivera") == {"vendor_engineer": vs.ENGINEER_ONLY}


def test_the_modal_refuses_it_with_the_rest_and_keeps_the_values_and_change_refuses_it(client, signed_in, mon):
    wo = vendor_work(mon)
    signed_in("manager")
    body = html(client.post(f"/work-orders/{wo.number}/complete/", checked_post(resolution="", vendor_engineer=OTHER, engineer_name="",
                                                                                checks_reference="FSO-4"), **HX))
    assert "Enter the engineer&#x27;s name." in body and '<span class="err" id="wc-engineer_name_error">' in body
    assert SAY_WHAT in body  # every problem in the same answer
    assert chosen(body) == OTHER and 'value="FSO-4"' in body
    assert stored(wo)[1:] == (None, "") and wo.status == WoStatus.IN_PROGRESS
    finish(wo)
    body = html(client.post(f"/work-orders/{wo.number}/engineer/", {"vendor_engineer": OTHER, "engineer_name": "  "}, **HX))
    assert "Enter the engineer&#x27;s name." in body and '<span class="err" id="we-engineer_name_error">' in body and chosen(body) == OTHER
    assert stored(wo)[1] is None and not VendorEngineer.objects.exists()


# --- 9. the stylesheet ------------------------------------------------------------------------------------------------------------------

def _open_blocks(css: str) -> tuple[str, list[list[str]]]:
    """The stylesheet with its comments blanked (same length), and at each index the preludes of the blocks open there ("@media
    (max-width:760px)", ".wc-eng"). Asserts no closing brace goes below depth 0."""
    text = re.sub(r"/\*.*?\*/", lambda m: " " * len(m.group(0)), css, flags=re.S)
    stack, start, at = [], 0, []
    for i, ch in enumerate(text):
        if ch == "{":
            stack.append(" ".join(text[start:i].split()))
            start = i + 1
        elif ch == "}":
            assert stack, f"a closing brace with nothing open, at character {i}"
            stack.pop()
            start = i + 1
        at.append(list(stack))
    return text, at


def test_the_stylesheet_balances_and_the_field_engineer_rules_are_for_every_screen():
    css = CSS.read_text()
    text, at = _open_blocks(css)
    assert at[-1] == [], f"blocks left open at the end: {at[-1]}"
    marker = css.index("/* Slice 31: field engineer")
    assert at[marker] == [], f"the field engineer section starts inside {at[marker]}"
    for rule in (".wc-vendor{display:grid", ".wc-eng{display:flex", ".dl .wo-eng{grid-column:1/-1", "@supports selector(:has(*)){\n  .wc-form .wc-vendor"):
        i = text.index(rule, marker)
        assert at[i] == [], (rule, at[i])
    for rule in (".wc-form .wc-vendor .wc-reference{display:flex}", ".wc-eng .wc-eng-name{display:none}",
                 '.wc-eng:has(select[name="vendor_engineer"] option[value="other"]:checked) .wc-eng-name{display:flex}'):
        i = text.index(rule, marker) + rule.index("{") + 1
        assert not any(p.startswith("@media") for p in at[i]), (rule, at[i])
        assert at[i][0] == "@supports selector(:has(*))", (rule, at[i])
    # the Vendor staff tab's phone rules stay phone-only, closed before the field engineer section
    i = text.index(".vs-eng-boxes label{min-height:40px}")
    assert at[i] == ["@media (max-width:760px)"]
