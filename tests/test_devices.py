"""Device services (slice 12), tested as a skeptic: every rule in apps/equipment/services.py's "adding and changing devices" section
(create_asset, create_device_model, create_department, update_asset, set_status, status_actions) and apps/equipment/permissions.py,
plus tenant isolation for each new path. Bugs the first run of these tests found in the scaffold's services (an unknown status, the "." and ".." tags,
an install-date error on the wrong field, a missing cost or condition) were fixed before merging; the tests keep them fixed."""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from apps.accounts.models import Level, Module, Role, User
from apps.equipment import permissions as perms
from apps.equipment import services as svc
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel, RiskClass
from apps.pm.dates import add_months
from apps.tenants.context import tenant_context
from apps.workorders.models import WorkOrderStatusHistory, WoStatus, WoType
from apps.workorders.services import change_status, create_work_order

TODAY = date(2026, 9, 29)  # passed to the services as `today`, so the date rules hold on any calendar day

S = AssetStatus


def add(model, dept, tag="CE-20001", **kw):
    return svc.create_asset(tag=tag, device_model=model, department=dept, today=kw.pop("today", TODAY), **kw)


def field_errors(excinfo) -> dict:
    return excinfo.value.message_dict


def history(asset):
    return list(Asset.history.filter(id=asset.id).order_by("history_date", "history_id"))


@pytest.fixture
def vent(vent):
    """conftest's ventilator, its dates moved onto TODAY: conftest dates it from the real calendar, which would drift past the
    fixed TODAY these tests pass to the services (an install date after TODAY from late 2028)."""
    vent.installed_on, vent.next_pm_on = TODAY - timedelta(days=800), TODAY + timedelta(days=10)
    vent.save(update_fields=["installed_on", "next_pm_on"])
    return vent


@pytest.fixture
def theirs(other_tenant):
    """Another facility with a department, a model, and a device named like ours."""
    with tenant_context(other_tenant):
        d = Department.objects.create(name="ICU")
        m = DeviceModel.objects.create(manufacturer="Hamilton Medical", model="Hamilton-G5", description="ICU ventilator", category="Ventilators",
                                       risk_class=RiskClass.LIFE_SUPPORT, oem_pm_interval_months=6)
        a = Asset.objects.create(tag="CE-10001", device_model=m, department=d, next_pm_on=TODAY + timedelta(days=10))
    return {"dept": d, "model": m, "asset": a}


def device(model, dept, tag, status=S.IN_SERVICE, **kw):
    """A device straight in the given status (fixtures for set_status; the service under test is the one after this)."""
    return Asset.objects.create(tag=tag, device_model=model, department=dept, status=status, next_pm_on=kw.pop("next_pm_on", TODAY + timedelta(days=20)),
                                **kw)


def wo(asset, type_=WoType.REPAIR, to=None):
    w = create_work_order(asset=asset, type=type_, priority="normal", problem=f"{type_} work")
    for step in to or []:
        change_status(w, step)
    return w


# --- create_asset -----------------------------------------------------------------------------------------------------------

def test_create_asset_trims_and_records_added(ctx, dept, vent_model):
    a = add(vent_model, dept, tag="  CE-20001\t", serial="  SN   4411 ", room=" 4 West  ", notes="  Came with the spare battery.\n")
    a.refresh_from_db()
    assert (a.tag, a.serial, a.room, a.notes) == ("CE-20001", "SN 4411", "4 West", "Came with the spare battery.")
    assert a.tenant_id == ctx.id and a.status == S.IN_SERVICE and a.condition == 3 and a.contract_id is None
    rows = history(a)
    assert len(rows) == 1 and rows[0].history_type == "+" and rows[0].history_change_reason == "Added"


@pytest.mark.parametrize("tag, message", [
    ("", "Enter the asset tag"),
    ("   ", "Enter the asset tag"),
    (None, "Enter the asset tag"),
    ("CE 20001", "spaces or slashes"),
    ("CE\t20001", "spaces or slashes"),
    ("CE/20001", "spaces or slashes"),
    ("A" * 41, "at most 40 characters"),
    ("new", "cannot be used"),
    ("NEW", "cannot be used"),
    ("  New ", "cannot be used"),
])
def test_tag_rules(ctx, dept, vent_model, tag, message):
    with pytest.raises(ValidationError) as e:
        add(vent_model, dept, tag=tag)
    assert list(field_errors(e)) == ["tag"] and message in field_errors(e)["tag"][0]
    assert not Asset.objects.exists()


def test_tag_of_exactly_forty_characters_is_fine(ctx, dept, vent_model):
    assert add(vent_model, dept, tag="A" * 40).tag == "A" * 40


@pytest.mark.parametrize("tag", ["ce-10001", "CE-10001", " Ce-10001 "])
def test_tags_are_unique_in_the_facility_in_any_letter_case(ctx, vent, vent_model, dept, tag):
    with pytest.raises(ValidationError) as e:
        add(vent_model, dept, tag=tag)
    assert field_errors(e) == {"tag": [f"{tag.strip()} is already on another device."]}
    assert Asset.objects.count() == 1


def test_the_same_tag_is_fine_in_another_facility(ctx, vent, theirs, dept, vent_model):
    with tenant_context(theirs["asset"].tenant):
        other = svc.create_asset(tag="CE-20001", device_model=theirs["model"], department=theirs["dept"], today=TODAY)
        assert Asset.objects.filter(tag__iexact="ce-10001").count() == 1  # theirs; ours does not block it or show up
    mine = add(vent_model, dept, tag="ce-20001")
    assert other.tenant_id != mine.tenant_id
    assert sorted(Asset.objects.values_list("tag", flat=True)) == ["CE-10001", "ce-20001"]


def test_model_and_department_must_be_this_facilitys(ctx, dept, vent_model, theirs):
    with pytest.raises(ValidationError) as e:
        add(theirs["model"], dept)
    assert field_errors(e) == {"device_model": ["Choose a device model from this facility."]}
    with pytest.raises(ValidationError) as e:
        add(vent_model, theirs["dept"])
    assert field_errors(e) == {"department": ["Choose a department from this facility."]}
    for model, department, key in ((None, dept, "device_model"), (vent_model, None, "department")):
        with pytest.raises(ValidationError) as e:
            add(model, department)
        assert list(field_errors(e)) == [key]
    assert not Asset.objects.exists()


def test_with_no_facility_in_context_nothing_is_added(ctx, dept, vent_model):
    with tenant_context(None):
        with pytest.raises(ValidationError) as e:
            add(vent_model, dept)
    assert list(field_errors(e)) == ["device_model"]
    assert not Asset.unscoped.exists()  # unscoped: proving no row landed in any tenant


@pytest.mark.parametrize("status", [S.IN_SERVICE, S.OUT_OF_SERVICE])
def test_a_new_device_is_in_service_or_waiting_for_inspection(ctx, dept, vent_model, status):
    assert add(vent_model, dept, status=status).status == status


@pytest.mark.parametrize("status", [S.IN_REPAIR, S.ON_LOAN, S.MISSING, S.RETIRED, "bogus"])
def test_other_statuses_are_refused_for_a_new_device(ctx, dept, vent_model, status):
    with pytest.raises(ValidationError) as e:
        add(vent_model, dept, status=status)
    assert list(field_errors(e)) == ["status"] and not Asset.objects.exists()


def test_acquisition_cost_defaults_to_the_list_cost_but_zero_is_kept(ctx, dept, vent_model):
    assert add(vent_model, dept, tag="A1").acquisition_cost == Decimal("38000")
    donated = add(vent_model, dept, tag="A2", acquisition_cost=Decimal("0"))
    donated.refresh_from_db()
    assert donated.acquisition_cost == 0  # a donated device is free, not list price
    assert add(vent_model, dept, tag="A3", acquisition_cost=Decimal("35100.50")).acquisition_cost == Decimal("35100.50")


def test_negative_cost_is_refused(ctx, dept, vent_model):
    with pytest.raises(ValidationError) as e:
        add(vent_model, dept, acquisition_cost=Decimal("-0.01"))
    assert field_errors(e) == {"acquisition_cost": ["The acquisition cost cannot be negative."]}


@pytest.mark.parametrize("condition, ok", [(0, False), (1, True), (3, True), (5, True), (6, False), (-1, False)])
def test_condition_is_one_to_five(ctx, dept, vent_model, condition, ok):
    if ok:
        assert add(vent_model, dept, condition=condition).condition == condition
    else:
        with pytest.raises(ValidationError) as e:
            add(vent_model, dept, condition=condition)
        assert field_errors(e) == {"condition": ["Condition is 1 (poor) to 5 (excellent)."]}


@pytest.mark.parametrize("dates, key", [
    ({"installed_on": TODAY + timedelta(days=1)}, "installed_on"),
    ({"installed_on": TODAY - timedelta(days=10), "warranty_end": TODAY - timedelta(days=11)}, "warranty_end"),
    ({"last_pm_on": TODAY + timedelta(days=1)}, "last_pm_on"),
    ({"installed_on": TODAY - timedelta(days=10), "last_pm_on": TODAY - timedelta(days=11)}, "last_pm_on"),
])
def test_date_rules(ctx, dept, vent_model, dates, key):
    with pytest.raises(ValidationError) as e:
        add(vent_model, dept, **dates)
    assert list(field_errors(e)) == [key]


def test_date_rules_allow_the_edges(ctx, dept, vent_model):
    a = add(vent_model, dept, tag="A1", installed_on=TODAY, warranty_end=TODAY, last_pm_on=TODAY)
    assert (a.installed_on, a.warranty_end, a.last_pm_on) == (TODAY, TODAY, TODAY)
    b = add(vent_model, dept, tag="A2", warranty_end=TODAY - timedelta(days=400))  # no install date: nothing to compare the warranty with
    assert b.warranty_end == TODAY - timedelta(days=400)


def test_date_errors_are_reported_together(ctx, dept, vent_model):
    with pytest.raises(ValidationError) as e:
        add(vent_model, dept, installed_on=TODAY + timedelta(days=5), warranty_end=TODAY, last_pm_on=TODAY + timedelta(days=1))
    assert set(field_errors(e)) == {"installed_on", "warranty_end", "last_pm_on"}


# --- first_pm_due -----------------------------------------------------------------------------------------------------------

def test_first_pm_is_one_interval_after_the_last_pm(ctx, dept, vent_model, pump_model):
    last = TODAY - timedelta(days=40)
    assert add(vent_model, dept, tag="V", last_pm_on=last, installed_on=TODAY - timedelta(days=900)).next_pm_on == add_months(last, 6)
    assert add(pump_model, dept, tag="P", last_pm_on=last).next_pm_on == add_months(last, 18)  # the pump's approved AEM interval


def test_first_pm_after_a_long_ago_last_pm_is_already_overdue(ctx, dept, vent_model):
    last = TODAY - timedelta(days=400)
    a = add(vent_model, dept, last_pm_on=last)
    assert a.next_pm_on == add_months(last, 6) < TODAY  # one interval after the last PM, as on record: overdue from day one


def test_first_pm_counts_from_install_when_no_pm_is_on_record(ctx, dept, vent_model):
    recent = TODAY - timedelta(days=30)
    assert add(vent_model, dept, tag="NEW1", installed_on=recent).next_pm_on == add_months(recent, 6)
    old = TODAY - timedelta(days=3 * 365)
    assert add(vent_model, dept, tag="OLD1", installed_on=old).next_pm_on == TODAY  # never PM'd here and past its interval: due today
    exactly = add_months(TODAY, -6)
    assert add(vent_model, dept, tag="EDGE1", installed_on=exactly).next_pm_on == TODAY


def test_first_pm_with_nothing_on_record_is_one_interval_from_today(ctx, dept, vent_model, pump_model):
    assert add(vent_model, dept, tag="V").next_pm_on == add_months(TODAY, 6)
    assert add(pump_model, dept, tag="P").next_pm_on == add_months(TODAY, 18)


def test_a_given_next_pm_wins(ctx, dept, vent_model):
    given = TODAY + timedelta(days=3)
    assert add(vent_model, dept, last_pm_on=TODAY - timedelta(days=10), next_pm_on=given).next_pm_on == given


def test_first_pm_clamps_to_month_end_including_leap_day(ctx, dept, vent_model):
    monthly = DeviceModel.objects.create(manufacturer="Acme", model="M1", description="Monthly device", category="Misc", risk_class=RiskClass.HIGH,
                                         oem_pm_interval_months=1)
    assert svc.first_pm_due(monthly, last_pm_on=date(2028, 1, 31), today=date(2028, 2, 10)) == date(2028, 2, 29)  # leap year
    assert svc.first_pm_due(monthly, last_pm_on=date(2027, 1, 31), today=date(2027, 2, 10)) == date(2027, 2, 28)
    assert svc.first_pm_due(vent_model, last_pm_on=date(2027, 8, 31), today=date(2027, 9, 1)) == date(2028, 2, 29)
    yearly = DeviceModel.objects.create(manufacturer="Acme", model="Y1", description="Yearly device", category="Misc", risk_class=RiskClass.LOW)
    assert svc.first_pm_due(yearly, installed_on=date(2024, 2, 29), today=date(2024, 3, 1)) == date(2025, 2, 28)
    assert svc.first_pm_due(yearly, today=date(2028, 2, 29)) == date(2029, 2, 28)
    a = add(monthly, dept, last_pm_on=date(2028, 1, 31), today=date(2028, 2, 10))
    assert a.next_pm_on == date(2028, 2, 29)


def test_life_support_never_uses_an_aem_interval(ctx, dept, vent_model):
    vent_model.aem_interval_months = 24  # set behind the services' back (admin): policy still keeps life support on OEM
    vent_model.save()
    assert svc.first_pm_due(vent_model, last_pm_on=date(2026, 1, 15), today=TODAY) == date(2026, 7, 15)
    assert add(vent_model, dept, last_pm_on=date(2026, 1, 15)).next_pm_on == date(2026, 7, 15)


# --- create_device_model ----------------------------------------------------------------------------------------------------

def model_args(**kw):
    return {"manufacturer": "Philips", "model": "IntelliVue MX450", "description": "Patient monitor", "category": "Physiologic monitors",
            "risk_class": RiskClass.HIGH, **kw}


def test_create_device_model_collapses_whitespace_and_applies_defaults(ctx):
    m = svc.create_device_model(**model_args(manufacturer="  Philips   Healthcare ", model=" IntelliVue \t MX450 ", description=" Patient  monitor",
                                             category="Physiologic\nmonitors "))
    m.refresh_from_db()
    assert (m.manufacturer, m.model, m.description, m.category) == ("Philips Healthcare", "IntelliVue MX450", "Patient monitor", "Physiologic monitors")
    assert (m.oem_pm_interval_months, m.expected_life_years, m.list_cost, m.aem_interval_months, m.pm_procedure_id) == (12, 8, 0, None, None)
    assert m.tenant_id == ctx.id and m.history.first().history_type == "+"


def test_create_device_model_requires_each_field(ctx):
    with pytest.raises(ValidationError) as e:
        svc.create_device_model(manufacturer=" ", model="", description=None, category="\t", risk_class="")
    assert set(field_errors(e)) == {"manufacturer", "model", "description", "category", "risk_class"}
    assert not DeviceModel.objects.exists()


@pytest.mark.parametrize("risk", ["", None, "critical", "Life support"])
def test_risk_class_must_be_a_known_slug(ctx, risk):
    with pytest.raises(ValidationError) as e:
        svc.create_device_model(**model_args(risk_class=risk))
    assert field_errors(e) == {"risk_class": ["Choose a risk class."]}


@pytest.mark.parametrize("manufacturer, model", [("hamilton medical", "HAMILTON-G5"), ("Hamilton  Medical", " Hamilton-G5 "),
                                                 ("HAMILTON MEDICAL", "hamilton-g5")])
def test_a_model_already_in_the_catalog_is_refused_in_any_case(ctx, vent_model, manufacturer, model):
    with pytest.raises(ValidationError) as e:
        svc.create_device_model(**model_args(manufacturer=manufacturer, model=model))
    assert list(field_errors(e)) == ["model"] and "already in the catalog" in field_errors(e)["model"][0]
    assert DeviceModel.objects.count() == 1


def test_the_same_model_is_fine_in_another_facility(ctx, theirs):
    m = svc.create_device_model(**model_args(manufacturer="Hamilton Medical", model="Hamilton-G5", risk_class=RiskClass.LIFE_SUPPORT))
    assert m.tenant_id == ctx.id and DeviceModel.objects.count() == 1


@pytest.mark.parametrize("field, value, ok", [
    ("oem_pm_interval_months", 0, False), ("oem_pm_interval_months", 1, True), ("oem_pm_interval_months", 120, True),
    ("oem_pm_interval_months", 121, False), ("oem_pm_interval_months", None, False), ("oem_pm_interval_months", -6, False),
    ("expected_life_years", 0, False), ("expected_life_years", 1, True), ("expected_life_years", 50, True), ("expected_life_years", 51, False),
    ("expected_life_years", None, False),
    ("list_cost", Decimal("-1"), False), ("list_cost", Decimal("0"), True), ("list_cost", None, True),
])
def test_device_model_number_bounds(ctx, field, value, ok):
    if ok:
        assert svc.create_device_model(**model_args(**{field: value})).pk
    else:
        with pytest.raises(ValidationError) as e:
            svc.create_device_model(**model_args(**{field: value}))
        assert list(field_errors(e)) == [field]


def test_create_device_model_takes_no_aem_interval(ctx):
    """AEM is an approved exception, never part of adding a model; life support never goes on it at all."""
    with pytest.raises(TypeError):
        svc.create_device_model(**model_args(aem_interval_months=24))
    m = svc.create_device_model(**model_args(risk_class=RiskClass.LIFE_SUPPORT, oem_pm_interval_months=6))
    m.aem_interval_months = 24  # even if set later behind the services' back
    m.save()
    assert m.pm_interval_months == 6
    m.risk_class = RiskClass.HIGH
    assert m.pm_interval_months == 24  # the same AEM interval applies to a model that is not life support


# --- create_department ------------------------------------------------------------------------------------------------------

def test_create_department_reuses_a_name_in_any_case(ctx, dept):
    assert svc.create_department("icu") == dept
    assert svc.create_department("  ICU ") == dept
    assert Department.objects.count() == 1


def test_create_department_collapses_whitespace_and_adds(ctx, dept):
    cath = svc.create_department("  Cardiac   Cath\tLab ")
    assert cath.name == "Cardiac Cath Lab" and cath.tenant_id == ctx.id
    assert svc.create_department("cardiac cath lab") == cath and Department.objects.count() == 2


@pytest.mark.parametrize("name", ["", "   ", None])
def test_create_department_needs_a_name(ctx, name):
    with pytest.raises(ValidationError) as e:
        svc.create_department(name)
    assert field_errors(e) == {"department": ["Enter the department's name."]}


def test_departments_are_per_facility(ctx, dept, theirs):
    with tenant_context(theirs["dept"].tenant):
        assert svc.create_department("icu") == theirs["dept"]
        new = svc.create_department("Oncology")
    assert new.tenant_id == theirs["dept"].tenant_id
    assert svc.create_department("icu") == dept
    assert list(Department.objects.values_list("name", flat=True)) == ["ICU"]  # their Oncology is not ours
    assert svc.create_department("oncology").tenant_id == ctx.id


# --- update_asset -----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("field, value", [("tag", "CE-99999"), ("status", S.RETIRED), ("contract", None), ("last_pm_on", TODAY),
                                          ("support_type", "oem_contract"), ("tenant", None), ("bogus", 1)])
def test_update_asset_changes_only_the_editable_fields(ctx, vent, field, value):
    before = (vent.tag, vent.status, vent.last_pm_on, len(history(vent)))
    with pytest.raises(ValidationError) as e:
        svc.update_asset(vent, today=TODAY, room="5 East", **{field: value})
    assert not hasattr(e.value, "error_dict") and f"cannot be changed here: {field}" in e.value.messages[0]
    vent.refresh_from_db()
    assert (vent.tag, vent.status, vent.last_pm_on, len(history(vent))) == before and vent.room == ""


def test_editable_fields_are_what_the_docs_say(ctx):
    assert set(svc.EDITABLE_FIELDS) == {"serial", "device_model", "department", "room", "installed_on", "acquisition_cost", "warranty_end",
                                        "condition", "next_pm_on", "notes"}


def test_update_asset_refuses_another_facilitys_model_or_department(ctx, vent, theirs):
    with pytest.raises(ValidationError) as e:
        svc.update_asset(vent, device_model=theirs["model"], today=TODAY)
    assert list(field_errors(e)) == ["device_model"]
    with pytest.raises(ValidationError) as e:
        svc.update_asset(vent, department=theirs["dept"], today=TODAY)
    assert list(field_errors(e)) == ["department"]
    with pytest.raises(ValidationError):
        svc.update_asset(vent, department=None, today=TODAY)
    vent.refresh_from_db()
    assert vent.device_model.tenant_id == ctx.id and vent.department.tenant_id == ctx.id


@pytest.mark.parametrize("status", [S.IN_SERVICE, S.IN_REPAIR, S.OUT_OF_SERVICE, S.ON_LOAN, S.MISSING])
def test_a_device_in_use_needs_a_next_pm(ctx, dept, vent_model, status):
    a = device(vent_model, dept, "CE-30001", status=status)
    with pytest.raises(ValidationError) as e:
        svc.update_asset(a, next_pm_on=None, today=TODAY)
    assert field_errors(e) == {"next_pm_on": ["A device in use needs a next PM date."]}


def test_a_retired_device_may_have_no_next_pm(ctx, dept, vent_model):
    a = device(vent_model, dept, "CE-30001", status=S.RETIRED)
    svc.update_asset(a, next_pm_on=None, today=TODAY)
    a.refresh_from_db()
    assert a.next_pm_on is None


def test_update_asset_with_nothing_changed_saves_nothing(ctx, vent):
    vent.refresh_from_db()
    stamp, rows = vent.updated_at, len(history(vent))
    same = {f: getattr(vent, f) for f in svc.EDITABLE_FIELDS}
    svc.update_asset(vent, today=TODAY, **same)
    svc.update_asset(vent, today=TODAY, serial="  ", room=" ", notes="  \n", acquisition_cost=Decimal("38000.00"))  # cleans to what is stored
    svc.update_asset(vent, today=TODAY)
    vent.refresh_from_db()
    assert vent.updated_at == stamp and len(history(vent)) == rows


def test_update_asset_saves_changes_with_edited_in_history(ctx, vent, pump_model):
    icu_west = Department.objects.create(name="ICU West")
    rows = len(history(vent))
    next_pm = vent.next_pm_on
    svc.update_asset(vent, today=TODAY, serial="  SN  77 ", room=" 12 ", department=icu_west, device_model=pump_model, condition=5,
                     acquisition_cost=Decimal("1234.56"), installed_on=TODAY - timedelta(days=100), warranty_end=TODAY + timedelta(days=265),
                     notes="  Battery replaced in March. ")
    vent.refresh_from_db()
    assert (vent.serial, vent.room, vent.department, vent.device_model, vent.condition, vent.acquisition_cost, vent.notes) == (
        "SN 77", "12", icu_west, pump_model, 5, Decimal("1234.56"), "Battery replaced in March.")
    assert vent.next_pm_on == next_pm  # a new model does not move the next PM by itself
    new_rows = history(vent)
    assert len(new_rows) == rows + 1 and new_rows[-1].history_change_reason == "Edited" and new_rows[-1].history_type == "~"


def test_update_asset_checks_dates_against_what_is_stored(ctx, vent):
    installed = vent.installed_on
    with pytest.raises(ValidationError) as e:
        svc.update_asset(vent, warranty_end=installed - timedelta(days=1), today=TODAY)  # against the stored install date
    assert list(field_errors(e)) == ["warranty_end"]
    with pytest.raises(ValidationError) as e:
        svc.update_asset(vent, installed_on=TODAY + timedelta(days=1), today=TODAY)
    assert list(field_errors(e)) == ["installed_on"]
    svc.update_asset(vent, installed_on=None, warranty_end=TODAY - timedelta(days=5000), today=TODAY)  # no install date: any warranty end
    vent.refresh_from_db()
    assert vent.installed_on is None


def test_update_asset_checks_numbers(ctx, vent):
    with pytest.raises(ValidationError) as e:
        svc.update_asset(vent, acquisition_cost=Decimal("-5"), condition=0, today=TODAY)
    assert set(field_errors(e)) == {"acquisition_cost", "condition"}


def test_moving_the_install_date_past_the_last_pm_is_an_install_date_error(ctx, vent):
    vent.last_pm_on = TODAY - timedelta(days=30)
    vent.save()
    with pytest.raises(ValidationError) as e:
        svc.update_asset(vent, installed_on=TODAY - timedelta(days=10), today=TODAY)
    assert list(field_errors(e)) == ["installed_on"]


@pytest.mark.parametrize("call", ["create_condition", "update_condition", "update_cost"])
def test_missing_numbers_are_validation_errors(ctx, vent, dept, vent_model, call):
    with pytest.raises(ValidationError):
        if call == "create_condition":
            add(vent_model, dept, condition=None)
        elif call == "update_condition":
            svc.update_asset(vent, condition=None, today=TODAY)
        else:
            svc.update_asset(vent, acquisition_cost=None, today=TODAY)


# --- set_status -------------------------------------------------------------------------------------------------------------

EXPECTED_CHANGES = {
    S.IN_SERVICE: {S.OUT_OF_SERVICE, S.ON_LOAN, S.MISSING, S.RETIRED},
    S.ON_LOAN: {S.IN_SERVICE, S.OUT_OF_SERVICE, S.MISSING, S.RETIRED},
    S.OUT_OF_SERVICE: {S.IN_SERVICE, S.MISSING, S.RETIRED},
    S.IN_REPAIR: {S.IN_SERVICE, S.OUT_OF_SERVICE, S.MISSING, S.RETIRED},
    S.MISSING: {S.IN_SERVICE, S.RETIRED},
    S.RETIRED: {S.IN_SERVICE},
}
ALLOWED = [(f, t) for f, tos in EXPECTED_CHANGES.items() for t in sorted(tos)]
REFUSED = [(f, t) for f in S.values for t in S.values if t not in EXPECTED_CHANGES[f]]


def test_status_changes_table_is_pinned():
    assert svc.STATUS_CHANGES == EXPECTED_CHANGES
    assert set(svc.STATUS_CHANGES) == set(S.values)  # every status has an entry, so none is a dead end by omission
    assert not any(S.IN_REPAIR in tos for tos in svc.STATUS_CHANGES.values())  # in repair comes from work orders only


@pytest.mark.parametrize("from_, to", ALLOWED)
def test_every_allowed_status_change(ctx, dept, vent_model, from_, to):
    a = device(vent_model, dept, "CE-40001", status=from_, next_pm_on=None if from_ == S.RETIRED else TODAY + timedelta(days=20))
    svc.set_status(a, to, today=TODAY)
    a.refresh_from_db()
    assert a.status == to and history(a)[-1].history_change_reason == f"Status: {S(to).label}"


@pytest.mark.parametrize("from_, to", REFUSED)
def test_every_refused_status_change(ctx, dept, vent_model, from_, to):
    a = device(vent_model, dept, "CE-40001", status=from_)
    rows = len(history(a))
    with pytest.raises(ValidationError) as e:
        svc.set_status(a, to, today=TODAY)
    assert e.value.messages == [f"CE-40001 cannot go from {S(from_).label.lower()} to {S(to).label.lower()}."]
    a.refresh_from_db()
    assert a.status == from_ and len(history(a)) == rows


@pytest.mark.parametrize("to", ["bogus", "", None])
def test_an_unknown_status_is_a_validation_error(ctx, vent, to):
    with pytest.raises(ValidationError):
        svc.set_status(vent, to, today=TODAY)


@pytest.mark.parametrize("type_, steps", [
    (WoType.REPAIR, []), (WoType.REPAIR, [WoStatus.IN_PROGRESS]), (WoType.REPAIR, [WoStatus.AWAITING_PARTS]),
    (WoType.RECALL, []), (WoType.INSPECTION, []), (WoType.SAFETY, []), (WoType.PM, [WoStatus.IN_PROGRESS]),
])
def test_retiring_refuses_while_other_work_is_open(ctx, vent, type_, steps):
    blocking = wo(vent, type_, steps)
    cancellable = wo(vent, WoType.PM)
    with pytest.raises(ValidationError) as e:
        svc.set_status(vent, S.RETIRED, today=TODAY)
    assert e.value.messages == [f"CE-10001 has open work: {blocking.number}. Complete it, or cancel it (an in-progress work order goes "
                                "back to open first), before retiring the device."]
    vent.refresh_from_db()
    cancellable.refresh_from_db()
    assert vent.status == S.IN_SERVICE and vent.next_pm_on is not None
    assert cancellable.status == WoStatus.OPEN  # nothing was cancelled on the way to the refusal


def test_retiring_names_every_blocking_work_order_in_number_order(ctx, vent):
    first = wo(vent, WoType.REPAIR)
    wo(vent, WoType.PM)  # cancellable: not named
    second = wo(vent, WoType.RECALL, [WoStatus.AWAITING_PARTS])
    third = wo(vent, WoType.PM, [WoStatus.IN_PROGRESS])
    with pytest.raises(ValidationError) as e:
        svc.set_status(vent, S.RETIRED, today=TODAY)
    assert f"open work: {first.number}, {second.number}, {third.number}." in e.value.messages[0]


def test_retiring_cancels_open_pm_work_and_leaves_the_schedule(ctx, vent, pump, make_user):
    director = make_user("director")
    open_pm = wo(vent, WoType.PM)
    waiting_pm = wo(vent, WoType.PM, [WoStatus.AWAITING_PARTS])
    done_pm = wo(vent, WoType.PM, [WoStatus.IN_PROGRESS, WoStatus.COMPLETED])
    done_repair = wo(vent, WoType.REPAIR, [WoStatus.IN_PROGRESS, WoStatus.COMPLETED])
    old_cancelled = wo(vent, WoType.PM, [WoStatus.CANCELLED])
    pump_pm = wo(pump, WoType.PM)  # another device's PM is not touched
    vent.refresh_from_db()
    svc.set_status(vent, S.RETIRED, by=director, note="  Replaced by CE-20001. ", today=TODAY)
    vent.refresh_from_db()
    assert vent.status == S.RETIRED and vent.next_pm_on is None
    assert history(vent)[-1].history_change_reason == "Replaced by CE-20001."
    for w, status in ((open_pm, WoStatus.CANCELLED), (waiting_pm, WoStatus.CANCELLED), (done_pm, WoStatus.COMPLETED),
                      (done_repair, WoStatus.COMPLETED), (old_cancelled, WoStatus.CANCELLED), (pump_pm, WoStatus.OPEN)):
        w.refresh_from_db()
        assert w.status == status, w.number
    for w in (open_pm, waiting_pm):
        last = WorkOrderStatusHistory.objects.filter(work_order=w).order_by("created_at").last()
        assert (last.to_status, last.note, last.changed_by) == (WoStatus.CANCELLED, "Device retired", director)
    assert WorkOrderStatusHistory.objects.filter(work_order=old_cancelled, note="Device retired").count() == 0


def test_retiring_sees_only_this_facilitys_work(ctx, vent, theirs):
    """Their device carries the same tag; its open repair must not block ours, and its open PM must not be cancelled with ours."""
    with tenant_context(theirs["asset"].tenant):
        their_repair, their_pm = wo(theirs["asset"], WoType.REPAIR), wo(theirs["asset"], WoType.PM)
    ours = wo(vent, WoType.PM)
    svc.set_status(vent, S.RETIRED, today=TODAY)
    ours.refresh_from_db()
    assert ours.status == WoStatus.CANCELLED
    with tenant_context(theirs["asset"].tenant):
        for w in (their_repair, their_pm):
            w.refresh_from_db()
            assert w.status == WoStatus.OPEN
        theirs["asset"].refresh_from_db()
        assert theirs["asset"].status == S.IN_SERVICE and theirs["asset"].next_pm_on is not None


def test_retiring_a_device_without_work_just_retires_it(ctx, vent):
    svc.set_status(vent, S.RETIRED, today=TODAY)
    vent.refresh_from_db()
    assert vent.status == S.RETIRED and vent.next_pm_on is None and history(vent)[-1].history_change_reason == "Status: Retired"


def test_reinstating_puts_the_device_back_with_a_pm_due_today(ctx, vent):
    vent.last_pm_on = TODAY - timedelta(days=500)
    vent.save()
    svc.set_status(vent, S.RETIRED, today=TODAY)
    svc.set_status(vent, S.IN_SERVICE, today=TODAY + timedelta(days=40))
    vent.refresh_from_db()
    assert vent.status == S.IN_SERVICE and vent.next_pm_on == TODAY + timedelta(days=40) and vent.last_pm_on == TODAY - timedelta(days=500)


def test_reinstating_defaults_to_the_real_today(ctx, dept, vent_model):
    a = device(vent_model, dept, "CE-40001", status=S.RETIRED, next_pm_on=None)
    svc.set_status(a, S.IN_SERVICE)
    a.refresh_from_db()
    assert a.next_pm_on == date.today()


@pytest.mark.parametrize("from_, to", [(S.IN_SERVICE, S.OUT_OF_SERVICE), (S.OUT_OF_SERVICE, S.IN_SERVICE), (S.IN_SERVICE, S.MISSING),
                                       (S.MISSING, S.IN_SERVICE), (S.IN_SERVICE, S.ON_LOAN), (S.IN_REPAIR, S.IN_SERVICE)])
def test_everyday_changes_leave_the_pm_dates_alone(ctx, dept, vent_model, from_, to):
    due = TODAY - timedelta(days=3)
    a = device(vent_model, dept, "CE-40001", status=from_, next_pm_on=due, last_pm_on=TODAY - timedelta(days=200))
    svc.set_status(a, to, today=TODAY)
    a.refresh_from_db()
    assert (a.next_pm_on, a.last_pm_on) == (due, TODAY - timedelta(days=200))


def test_the_note_lands_in_history_trimmed_and_capped(ctx, vent):
    svc.set_status(vent, S.OUT_OF_SERVICE, note="  Cracked housing; tagged at the nurses' station.  ", today=TODAY)
    assert history(vent)[-1].history_change_reason == "Cracked housing; tagged at the nurses' station."
    svc.set_status(vent, S.IN_SERVICE, note="x" * 250, today=TODAY)
    assert history(vent)[-1].history_change_reason == "x" * 100
    svc.set_status(vent, S.ON_LOAN, note="   ", today=TODAY)
    assert history(vent)[-1].history_change_reason == "Status: On loan"


# --- status_actions and status_action_label ---------------------------------------------------------------------------------

TECH_ACTIONS = {
    S.IN_SERVICE: [(S.OUT_OF_SERVICE, "Tag out of service", "danger"), (S.ON_LOAN, "Lend out", ""), (S.MISSING, "Mark missing", "")],
    S.ON_LOAN: [(S.OUT_OF_SERVICE, "Tag out of service", "danger"), (S.IN_SERVICE, "Back from loan", ""), (S.MISSING, "Mark missing", "")],
    S.OUT_OF_SERVICE: [(S.IN_SERVICE, "Return to service", ""), (S.MISSING, "Mark missing", "")],
    S.IN_REPAIR: [(S.IN_SERVICE, "Return to service", ""), (S.OUT_OF_SERVICE, "Tag out of service", "danger"), (S.MISSING, "Mark missing", "")],
    S.MISSING: [(S.IN_SERVICE, "Found", "")],
    S.RETIRED: [],
}
RETIRE = (S.RETIRED, "Retire", "danger")
DIRECTOR_ACTIONS = {**{k: v + [RETIRE] for k, v in TECH_ACTIONS.items() if k != S.RETIRED}, S.RETIRED: [(S.IN_SERVICE, "Reinstate", "")]}


def approver(tenant):
    """A CE manager whose facility gave them Equipment Approve in the Roles matrix."""
    role = Role.objects.create(name="CE manager (approves retirements)", slug="manager-plus")
    role.set_levels({Module.EQUIPMENT: Level.APPROVE})
    return User.objects.create_user(username="plus@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role)


@pytest.mark.parametrize("status", S.values)
def test_status_actions_per_status_and_role(ctx, dept, vent_model, make_user, status):
    a = device(vent_model, dept, "CE-40001", status=status)
    tech, manager, director, plus = make_user("technician"), make_user("manager"), make_user("director"), approver(ctx)
    shape = lambda user: [(x["to"], x["label"], x["style"]) for x in svc.status_actions(a, user)]  # noqa: E731
    assert shape(tech) == shape(manager) == TECH_ACTIONS[status]
    assert shape(director) == shape(plus) == DIRECTOR_ACTIONS[status]
    for slug in ("requester", "analyst", "vendor"):
        assert svc.status_actions(a, make_user(slug)) == []


def test_status_actions_only_offer_allowed_changes(ctx, dept, vent_model, make_user):
    director = make_user("director")
    for status in S.values:
        a = Asset(tag="X", device_model=vent_model, department=dept, status=status)
        assert {x["to"] for x in svc.status_actions(a, director)} == svc.STATUS_CHANGES[status]


def test_status_action_confirms(ctx, vent, make_user):
    actions = {x["to"]: x["confirm"] for x in svc.status_actions(vent, make_user("director"))}
    assert actions == {S.OUT_OF_SERVICE: "", S.ON_LOAN: "",
                       S.MISSING: "Mark CE-10001 missing? It counts as overdue for PM until it is found.",
                       S.RETIRED: "Retire CE-10001? Its open PM work orders are cancelled and it leaves the PM schedule."}


@pytest.mark.parametrize("from_, to, label", [
    (S.IN_SERVICE, S.OUT_OF_SERVICE, "Tag out of service"), (S.ON_LOAN, S.OUT_OF_SERVICE, "Tag out of service"),
    (S.IN_REPAIR, S.OUT_OF_SERVICE, "Tag out of service"), (S.OUT_OF_SERVICE, S.IN_SERVICE, "Return to service"),
    (S.IN_REPAIR, S.IN_SERVICE, "Return to service"), (S.ON_LOAN, S.IN_SERVICE, "Back from loan"), (S.MISSING, S.IN_SERVICE, "Found"),
    (S.RETIRED, S.IN_SERVICE, "Reinstate"), (S.IN_SERVICE, S.ON_LOAN, "Lend out"), (S.OUT_OF_SERVICE, S.MISSING, "Mark missing"),
    (S.MISSING, S.RETIRED, "Retire"),
])
def test_status_action_labels(from_, to, label):
    assert svc.status_action_label(from_, to)[0] == label


def test_every_allowed_change_has_a_label():
    for from_, to in ALLOWED:
        label, style = svc.status_action_label(from_, to)
        assert label and style in ("", "danger") and label[0].isupper() and not label.endswith((".", "!"))


# --- permissions ------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("from_, to", ALLOWED)
def test_status_level(from_, to):
    assert perms.status_level(from_, to) == (Level.APPROVE if S.RETIRED in (from_, to) else Level.EDIT)


@pytest.mark.parametrize("slug, can_change, can_retire", [("director", True, True), ("manager", True, False), ("technician", True, False),
                                                          ("requester", False, False), ("analyst", False, False), ("vendor", False, False)])
def test_equipment_permissions_by_default_role(ctx, make_user, slug, can_change, can_retire):
    user = make_user(slug)
    assert perms.can_add(user) == perms.can_edit(user) == can_change
    assert perms.can_set_status(user, S.IN_SERVICE, S.OUT_OF_SERVICE) == can_change
    assert perms.can_set_status(user, S.IN_SERVICE, S.RETIRED) == perms.can_set_status(user, S.RETIRED, S.IN_SERVICE) == can_retire


def test_approve_in_the_roles_matrix_lets_a_manager_retire(ctx):
    plus = approver(ctx)
    assert perms.can_set_status(plus, S.MISSING, S.RETIRED) and perms.can_set_status(plus, S.RETIRED, S.IN_SERVICE)


# --- bugs at the edges of tags ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("tag", [".", ".."])
def test_dot_segment_tags_are_refused(ctx, dept, vent_model, tag):
    with pytest.raises(ValidationError) as e:
        add(vent_model, dept, tag=tag)
    assert list(field_errors(e)) == ["tag"]


# --- review fixes -----------------------------------------------------------------------------------------------------------

def test_a_completed_repair_returns_a_device_only_when_it_took_it_out(ctx, dept, pump_model):
    """A repair that was opened with the device tagged out brings it back; a device out of service for another reason (incoming
    inspection, a hand tag-out) stays out when an unrelated repair completes."""
    waiting = svc.create_asset(tag="CE-80001", device_model=pump_model, department=dept, status=S.OUT_OF_SERVICE, today=TODAY)
    repair = create_work_order(asset=waiting, type=WoType.REPAIR, priority="normal", problem="Damaged cord", opened_on=TODAY)
    change_status(repair, WoStatus.IN_PROGRESS, as_of=TODAY)
    change_status(repair, WoStatus.COMPLETED, as_of=TODAY)
    waiting.refresh_from_db()
    assert waiting.status == S.OUT_OF_SERVICE
    tagged = svc.create_asset(tag="CE-80002", device_model=pump_model, department=dept, today=TODAY)
    repair = create_work_order(asset=tagged, type=WoType.REPAIR, priority="normal", problem="Alarm", opened_on=TODAY, tag_out=True)
    tagged.refresh_from_db()
    assert tagged.status == S.OUT_OF_SERVICE
    change_status(repair, WoStatus.IN_PROGRESS, as_of=TODAY)
    change_status(repair, WoStatus.COMPLETED, as_of=TODAY)
    tagged.refresh_from_db()
    assert tagged.status == S.IN_SERVICE


def test_moving_the_next_pm_moves_the_open_pm_work_order(ctx, vent):
    pm = create_work_order(asset=vent, type=WoType.PM, priority="normal", problem="PM", opened_on=TODAY, due_on=vent.next_pm_on)
    later = TODAY + timedelta(days=45)
    svc.update_asset(vent, next_pm_on=later, today=TODAY)
    pm.refresh_from_db()
    assert pm.due_on == later
    assert pm.status_history.filter(note__startswith="Due date moved from").exists()


@pytest.mark.parametrize("day", [date(9999, 12, 31), add_months(TODAY, 121), date(1999, 1, 1)])
def test_the_next_pm_must_be_a_real_date_within_ten_years(ctx, dept, pump_model, vent, day):
    with pytest.raises(ValidationError) as e:
        svc.create_asset(tag="CE-80003", device_model=pump_model, department=dept, next_pm_on=day, today=TODAY)
    assert "next_pm_on" in e.value.message_dict
    with pytest.raises(ValidationError) as e:
        svc.update_asset(vent, next_pm_on=day, today=TODAY)
    assert "next_pm_on" in e.value.message_dict
    svc.update_asset(vent, next_pm_on=add_months(TODAY, 120), today=TODAY)  # ten years is fine


def test_a_pm_cancelled_by_retirement_is_not_a_missed_pm(ctx, dept, vent_model):
    from apps.pm.services import pm_on_time_rate

    a = svc.create_asset(tag="CE-80004", device_model=vent_model, department=dept, today=TODAY)
    due = TODAY + timedelta(days=5)
    create_work_order(asset=a, type=WoType.PM, priority="normal", problem="PM", opened_on=TODAY, due_on=due)
    svc.set_status(a, S.RETIRED, today=TODAY)
    period = (due - timedelta(days=1), due + timedelta(days=1), due + timedelta(days=10))  # the PM fell due inside it, uncompleted
    assert pm_on_time_rate(*period)["due"] == 0
    kept = svc.create_asset(tag="CE-80005", device_model=vent_model, department=dept, today=TODAY)
    missed = create_work_order(asset=kept, type=WoType.PM, priority="normal", problem="PM", opened_on=TODAY, due_on=due)
    change_status(missed, WoStatus.CANCELLED)
    assert pm_on_time_rate(*period) == {"due": 1, "on_time": 0, "rate": 0.0}  # a device in use: still a missed PM


def test_a_device_with_an_odd_stored_warranty_stays_editable(ctx, vent):
    Asset.objects.filter(pk=vent.pk).update(warranty_end=vent.installed_on - timedelta(days=30))
    vent.refresh_from_db()
    svc.update_asset(vent, room="5 East", today=TODAY)  # the stored pair is not re-checked when neither date changes
    with pytest.raises(ValidationError) as e:
        svc.update_asset(vent, warranty_end=vent.installed_on - timedelta(days=1), today=TODAY)
    assert "warranty_end" in e.value.message_dict
