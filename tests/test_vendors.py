"""
Slice 31, contracted service and vendor staff, through the services: the one rule (apps.vendors.rules: which vendor a name is, the
name rule, what a record covers, each record by its own dates, the evidence behind a vendor job and its words, never "trained"), the
records' writes (apps.vendors.services: engineers one per vendor and name, an inactive one reused and never reactivated, a company
fixed once a work order names them; trainings for one vendor's engineers; attestations; removal as entered in error), and every
writer of WorkOrder.vendor_engineer: completion.complete_work_order (an engineer, an id, someone else, not named, kept on a reopen; a
scoped vendor account among its own company's engineers, anything else answering as an unknown id), workorders.services
.set_vendor_engineer, and assign's clear (only open work; the engineer cleared when the work moves, kept on the same vendor). The
vendor's report number on any vendor job. The paths that never write the engineer (a failed PM's repair, a re-inspection, a recall
batch, the legacy importer, keep_open, reopening). checked_by's words and masking. Tenant isolation, PROTECT, the levels. On
PostgreSQL: two completions typing one new name make one engineer, and the services under the row-level security policies.
"""
import re
import threading
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

import pytest
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.db import connection
from django.db.models import ProtectedError, QuerySet
from django.utils import timezone
from incoming_fixtures import INCOMING_ALL_PASS, incoming_results, waiting_device
from pg_helpers import as_app_role, needs_postgres
from repair_fixtures import CHECKED, post_repair_results
from vendor_fixtures import VENDOR, attestation, engineer, training, vendor_job

from apps.accounts.models import DataScope, Level, Module, Role, User
from apps.equipment import services as eq
from apps.equipment.models import AddedAs, Asset, Department, DeviceModel, Ownership, RiskClass
from apps.recalls import services as recalls
from apps.tenants.context import tenant_context
from apps.vendors import permissions as vendor_perms
from apps.vendors import rules, services
from apps.vendors.admin import EngineerTrainingAdmin, VendorAttestationAdmin, VendorEngineerAdmin
from apps.vendors.models import Covers, EngineerTraining, TrainingSource, VendorAttestation, VendorEngineer
from apps.workorders import completion, inspections, legacy, repair_checks
from apps.workorders import permissions as wo_perms
from apps.workorders.completion import complete_work_order, keep_open
from apps.workorders.models import InspectionResult, PmResult, Priority, RepairCheck, WorkOrder, WoStatus, WoType
from apps.workorders.services import assign, change_status, create_work_order, set_vendor_engineer

DAY = timedelta(days=1)
NEVER = re.compile(r"\b(trained|qualified|verified|certified)\b", re.IGNORECASE)  # Cadence keeps documents, never judges competence


@pytest.fixture
def today(ctx):
    return timezone.localdate()


@pytest.fixture
def catalog(ctx):
    """Philips' two monitors (one product category), Philips' CT (another category, kept on the manufacturer's schedule), GE's monitor."""
    def model(manufacturer, name, category, **extra):
        return DeviceModel.objects.create(manufacturer=manufacturer, model=name, description=category[:-1], category=category,
                                          risk_class=RiskClass.HIGH, **extra)

    return SimpleNamespace(mx750=model("Philips", "IntelliVue MX750", "Patient monitors"),
                           mx450=model("Philips", "IntelliVue MX450", "Patient monitors"),
                           ct=model("Philips", "Ingenuity CT", "CT scanners", oem_schedule_required=True),
                           ge=model("GE", "B650", "Patient monitors"))


@pytest.fixture
def mon(catalog, dept):
    return Asset.objects.create(tag="CE-20001", device_model=catalog.mx750, department=dept)


@pytest.fixture
def ct(catalog, dept):
    return Asset.objects.create(tag="CE-20002", device_model=catalog.ct, department=dept)


@pytest.fixture
def dana(ctx):
    return engineer("Dana Kim")


@pytest.fixture
def vendor_user(make_user):
    user = make_user("vendor")
    user.company = VENDOR
    user.save()
    return user


def errors_of(excinfo) -> dict:
    return {k: " ".join(v) for k, v in excinfo.value.message_dict.items()}


def refused(call, *args, **kwargs) -> dict:
    with pytest.raises(ValidationError) as e:
        call(*args, **kwargs)
    return errors_of(e)


def vendor_work(asset, vendor=VENDOR, *, type_=WoType.REPAIR, start=True):
    wo = create_work_order(asset=asset, type=type_, priority=Priority.NORMAL, problem="Vendor visit")
    assign(wo, vendor_name=vendor)
    if start:
        change_status(wo, WoStatus.IN_PROGRESS)
    return wo


def in_house_work(asset, tech, *, type_=WoType.REPAIR):
    wo = create_work_order(asset=asset, type=type_, priority=Priority.NORMAL, problem="Alarm fault", assigned_to=tech)
    change_status(wo, WoStatus.IN_PROGRESS)
    return wo


def finish(wo, **kwargs):
    """Complete `wo` (a repair checked and ready for use, a PM passed) with `kwargs`."""
    extra = dict(CHECKED) if wo.type == WoType.REPAIR else {"pm_result": PmResult.PASS} if wo.type == WoType.PM else {}
    return complete_work_order(wo, resolution="Serviced by the vendor", **{**extra, **kwargs})


def not_completed(wo, **kwargs) -> dict:
    """finish refused, and nothing saved: the work order still in progress and naming whom it named."""
    named = WorkOrder.objects.get(pk=wo.pk).vendor_engineer_id
    errors = refused(finish, wo, **kwargs)
    wo.refresh_from_db()
    assert wo.status == WoStatus.IN_PROGRESS and wo.vendor_engineer_id == named
    return errors


def on_file_since(record, day):
    """Backdate when `record` was entered in Cadence (created_at is set once; the evidence reads its facility day)."""
    type(record).objects.filter(pk=record.pk).update(created_at=timezone.make_aware(datetime.combine(day, time(9))))
    record.refresh_from_db()
    return record


def evidence(day, device_model, vendor=VENDOR, eng=None):
    return rules.evidence_for(day=day, device_model=device_model, vendor_name=vendor, engineer=eng,
                              trainings=list(EngineerTraining.objects.all()), attestations=list(VendorAttestation.objects.all()))


def models_by_id():
    return {str(m.pk): m for m in DeviceModel.objects.all()}


def custom_scoped_role(levels):
    role = Role.objects.create(name="Vendor lead", slug="vendor-lead", scope=DataScope.COMPANY)
    role.set_levels(levels)
    return role


# --- which vendor a name is, and a person's name ---------------------------------------------------------------------------------

@pytest.mark.parametrize("name, key", [
    ("Philips", "philips"), ("PHILIPS  field service", "philips"), ("  philips ", "philips"), ("Philips Field Service", "philips"),
    ("Philips Healthcare", "philips healthcare"),  # another name: an attestation names both
    ("field service", "field service"),  # nothing before it: a name, not a suffix
    ("Acme field service field service", "acme field service"),  # one dropped
    ("Dräger", "dräger"), ("DRÄGER", "dräger"), ("Dräger", "dräger"),  # any letter case, beyond ASCII, typed composed or not
    ("STRASSE", "strasse"), ("Straße", "strasse"), ("", ""), (None, ""),
])
def test_vendor_key(name, key):
    assert rules.vendor_key(name) == key


@pytest.mark.parametrize("name, kept", [
    ("Dana Kim", "Dana Kim"), ("  Dana   Kim ", "Dana Kim"), ("Mary-Jo O'Neil", "Mary-Jo O'Neil"), ("J. R. Smith, Jr.", "J. R. Smith, Jr."),
    ("Zoë Lefèvre", "Zoë Lefèvre"), ("José Núñez", "José Núñez"), ("D’Arcy Lee", "D’Arcy Lee"), ("Li", "Li"),
])
def test_a_name_as_the_report_gives_it(name, kept):
    assert rules.clean_engineer_name(name) == kept


@pytest.mark.parametrize("name, message", [
    ("", "Enter the engineer's name."), ("   ", "Enter the engineer's name."), (None, "Enter the engineer's name."),
    ("D", rules.NAME_RULE), ("..", rules.NAME_RULE), ("Dana Kim 4471", rules.NAME_RULE), ("dana.kim@philips.com", rules.NAME_RULE),
    ("D/K", rules.NAME_RULE), ("MRN 004471", rules.NAME_RULE), ("Dana (FSE)", rules.NAME_RULE), ("Dana\nKim\t2", rules.NAME_RULE),
    ("A" * 121, "Keep the name to 120 characters."),
])
def test_a_name_never_carries_numbers_addresses_or_records(name, message):
    with pytest.raises(ValidationError) as e:
        rules.clean_engineer_name(name, "engineer_name")
    assert errors_of(e) == {"engineer_name": message}


# --- engineers ------------------------------------------------------------------------------------------------------------------

def test_an_engineer_is_one_per_vendor_and_name(ctx, make_user):
    kim = make_user("manager")
    dana = services.add_engineer(name="  Dana  Kim ", company=" Philips ", by=kim)
    assert (dana.name, dana.company, dana.company_key, dana.name_key, dana.is_active, dana.created_by) == (
        "Dana Kim", "Philips", "philips", "dana kim", True, kim)
    exists = {"name": "Dana Kim is already an engineer of Philips."}
    assert refused(services.add_engineer, name="dana kim", company="Philips") == exists
    assert refused(services.add_engineer, name="Kim, Dana", company="PHILIPS field service") == exists  # the one on file, as written
    services.update_engineer(dana, is_active=False)
    assert refused(services.add_engineer, name="Dana Kim", company="Philips") == {  # still on file: make them active again
        "name": "Dana Kim is already an engineer of Philips (inactive)."}
    other = services.add_engineer(name="Dana Kim", company="GE Healthcare")  # another vendor's Dana Kim
    assert other.company_key == "ge healthcare" and VendorEngineer.objects.count() == 2
    assert refused(services.add_engineer, name="Dana Kim", company="  ") == {"company": "Enter the vendor as your work orders and contracts name it."}
    assert refused(services.add_engineer, name="Dana Kim", company="X" * 121) == {"company": "Keep the vendor's name to 120 characters."}
    assert refused(services.add_engineer, name="Dana 2", company="Philips") == {"name": rules.NAME_RULE}


def test_changing_an_engineer(dana, mon):
    lee = engineer("Lee Park")
    services.update_engineer(dana, name="Dana Kim-Ortiz")
    assert (dana.name, dana.name_key) == ("Dana Kim-Ortiz", "dana kim-ortiz")
    assert refused(services.update_engineer, dana, name="park, lee") == {"name": "Lee Park is already an engineer of Philips."}
    services.update_engineer(dana, company="Philips Healthcare")  # no work order names her yet
    assert (dana.company, dana.company_key) == ("Philips Healthcare", "philips healthcare")
    services.update_engineer(dana, company="Philips")
    assert refused(services.update_engineer, lee, is_active="no") == {"is_active": "Active must be yes or no."}
    services.update_engineer(lee, is_active=False)
    assert not VendorEngineer.objects.get(pk=lee.pk).is_active
    # named on a work order: her company stays (a move to another vendor is a new engineer); her name and standing still change
    vendor_job(mon, engineer=dana)
    assert refused(services.update_engineer, dana, company="TechCare") == {"company": services.COMPANY_FIXED.format(name="Dana Kim-Ortiz")}
    services.update_engineer(dana, company="PHILIPS", name="Dana Kim", is_active=False)  # the same vendor spelled another way: no move
    dana.refresh_from_db()
    assert (dana.company, dana.company_key, dana.name, dana.is_active) == ("PHILIPS", "philips", "Dana Kim", False)
    assert [h.history_type for h in dana.history.order_by("history_date", "history_id")] == ["+", "~", "~", "~", "~"]


def test_engineer_named_reuses_by_vendor_and_name_and_never_reactivates(ctx, dana):
    assert services.engineer_named("PHILIPS field service", "kim, dana") == dana
    services.update_engineer(dana, is_active=False)
    found = services.engineer_named("Philips", "Dana Kim")
    assert found == dana and not VendorEngineer.objects.get(pk=dana.pk).is_active
    lee = services.engineer_named("Philips field service", "Lee  Park")
    assert (lee.name, lee.company, lee.company_key, lee.is_active) == ("Lee Park", "Philips field service", "philips", True)
    assert lee.history.get().history_change_reason is None  # not from a work order
    assert refused(services.engineer_named, "Philips", "Lee 2") == {"engineer_name": rules.NAME_RULE}
    assert refused(services.engineer_named, "", "Lee Park") == {"engineer_name": "Enter the vendor as your work orders and contracts name it."}
    assert list(services.engineers_for("philips")) == [lee] and list(services.engineers_for("")) == []


def test_engineer_named_reads_again_when_the_insert_finds_the_name_taken(ctx, dana, monkeypatch):
    """The lookup found nobody, and the same name was saved a moment later (another completion): the insert fails on the unique key
    and the engineer is read again, the transaction going on."""
    real_first, missed = QuerySet.first, []

    def first(self):
        if self.model is VendorEngineer and not missed:
            missed.append(True)
            return None
        return real_first(self)

    monkeypatch.setattr(QuerySet, "first", first)
    assert services.engineer_named("Philips field service", "Dana Kim") == dana and missed
    assert VendorEngineer.objects.count() == 1


# --- what a record covers -----------------------------------------------------------------------------------------------------

def test_coverage_is_models_or_a_manufacturers_category_never_a_manufacturer(catalog, dana):
    by_models = training(dana, catalog.mx750, catalog.mx450)
    line = training(dana, covers=Covers.LINE, manufacturer="Philips", category="Patient monitors", issued_on=timezone.localdate() - 400 * DAY)
    assert sorted(by_models.model_ids) == sorted([str(catalog.mx750.pk), str(catalog.mx450.pk)])
    assert (line.model_ids, line.manufacturer, line.category) == ([], "Philips", "Patient monitors")
    for record in (by_models, line):
        assert rules.names_model(record, catalog.mx750) and rules.names_model(record, catalog.mx450)
        assert not rules.names_model(record, catalog.ct)  # a certificate on Philips monitors never covers a Philips CT
        assert not rules.names_model(record, catalog.ge)  # nor another maker's monitor
    every = attestation()
    assert all(rules.names_model(every, m) for m in (catalog.mx750, catalog.ct, catalog.ge))


def test_what_a_record_covers_is_checked_against_the_catalog(catalog, dana, other_tenant):
    def add(**coverage):
        return refused(services.add_training, [dana], source=TrainingSource.OEM, issued_on=timezone.localdate(), **coverage)

    choose = "Choose what it covers: device models, or a manufacturer's product category."
    assert add(covers=Covers.ALL) == {"covers": choose}  # every device: an attestation's, never one engineer's
    assert add(covers="manufacturer", manufacturer="Philips") == {"covers": choose}
    assert add(covers=Covers.MODELS) == {"model_ids": "Choose at least one device model."}
    assert add(covers=Covers.MODELS, model_ids=["not-an-id"]) == {"model_ids": "Choose device models from the catalog."}
    with tenant_context(other_tenant):
        theirs = DeviceModel.objects.create(manufacturer="Philips", model="IntelliVue MX800", description="Monitor", category="Patient monitors")
    assert add(covers=Covers.MODELS, model_ids=[str(catalog.mx750.pk), str(theirs.pk)]) == {"model_ids": "Choose device models from the catalog."}
    assert add(covers=Covers.LINE, manufacturer="Philips") == {"category": "Choose the product category."}
    assert add(covers=Covers.LINE, category="Patient monitors") == {"manufacturer": "Choose the manufacturer."}
    assert "No device model in the catalog" in add(covers=Covers.LINE, manufacturer="Philips", category="Ventilators")["category"]
    assert "No device model in the catalog" in add(covers=Covers.LINE, manufacturer="philips", category="Patient monitors")["category"]
    # one model sent twice is one model
    t = training(dana, catalog.mx750, catalog.mx750)
    assert t.model_ids == [str(catalog.mx750.pk)]
    assert refused(attestation, covers=Covers.MODELS) == {"model_ids": "Choose at least one device model."}
    a = attestation(covers=Covers.LINE, manufacturer="Philips", category="CT scanners")
    assert rules.names_model(a, catalog.ct) and not rules.names_model(a, catalog.mx750)


# --- each record by its own dates ------------------------------------------------------------------------------------------------

def test_a_training_runs_from_issued_to_expiry_and_a_renewal_is_a_new_record(catalog, dana, today):
    old = training(dana, catalog.mx750, issued_on=today - 700 * DAY, expires_on=today - 300 * DAY)
    renewed = training(dana, catalog.mx750, issued_on=today - 200 * DAY, expires_on=today + 165 * DAY)
    assert EngineerTraining.objects.filter(engineer=dana).count() == 2
    assert (rules.training_in_force(old, today - 700 * DAY), rules.training_in_force(old, today - 300 * DAY)) == (True, True)
    assert (rules.training_in_force(old, today - 701 * DAY), rules.training_in_force(old, today - 299 * DAY)) == (False, False)
    assert evidence(today - 400 * DAY, catalog.mx750, eng=dana).record == old
    assert evidence(today - 30 * DAY, catalog.mx750, eng=dana).record == renewed
    gap = evidence(today - 250 * DAY, catalog.mx750, eng=dana)  # between the two: nothing in force, the old one's expiry said
    assert (gap.kind, gap.record, gap.expired_on) == (rules.NONE, None, today - 300 * DAY)
    no_expiry = training(dana, catalog.mx450, issued_on=today - 10 * DAY)
    assert rules.training_in_force(no_expiry, today + 3650 * DAY) and not rules.training_in_force(no_expiry, today - 11 * DAY)
    # the same coverage issued the same day for the same engineer is on file already; issued another day it is a renewal
    assert refused(training, dana, catalog.mx450, issued_on=today - 10 * DAY) == {
        "issued_on": f"Dana Kim already has this training on file, issued {rules.fmt(today - 10 * DAY)}."}
    training(dana, catalog.mx450, issued_on=today - 9 * DAY)


def test_an_attestation_runs_from_valid_from_through_valid_through_whenever_received(catalog, today):
    letter = attestation(valid_from=today - 300 * DAY, valid_through=today + 65 * DAY, received_on=today - 250 * DAY)
    assert rules.attestation_in_force(letter, today - 300 * DAY) and rules.attestation_in_force(letter, today + 65 * DAY)
    assert not rules.attestation_in_force(letter, today - 301 * DAY) and not rules.attestation_in_force(letter, today + 66 * DAY)
    # received after the period began: the days before it was received are covered (the letter states its period)
    assert evidence(today - 280 * DAY, catalog.mx750).record == letter
    # next year's letter, received early, covers from its own first day
    nxt = attestation(valid_from=today + 66 * DAY, valid_through=today + 430 * DAY, received_on=today, reference="LTR-2027-01")
    assert evidence(today + 100 * DAY, catalog.mx750).record == nxt


def test_dates_never_in_the_future_and_always_in_order(catalog, dana, today):
    tomorrow = today + DAY
    assert refused(training, dana, catalog.mx750, issued_on=tomorrow) == {"issued_on": "The issue date cannot be in the future."}
    assert refused(training, dana, catalog.mx750, issued_on=today, expires_on=today - DAY) == {
        "expires_on": "The expiry date cannot be before the issue date."}
    assert refused(services.add_training, [dana], covers=Covers.MODELS, model_ids=[str(catalog.mx750.pk)], source=TrainingSource.OEM,
                   issued_on=None) == {"issued_on": "Enter the issue date."}
    assert refused(services.add_training, [dana], covers=Covers.MODELS, model_ids=[str(catalog.mx750.pk)], source=TrainingSource.OEM,
                   issued_on="2026-01-01") == {"issued_on": "Enter the issue date as a date."}
    assert refused(training, dana, catalog.mx750, expires_on="2027-01-01") == {"expires_on": "Enter the expiry as a date."}
    assert refused(training, dana, catalog.mx750, source="cousin") == {"source": "Choose where the training came from."}
    assert refused(attestation, received_on=tomorrow, valid_from=today) == {"received_on": "The day it was received cannot be in the future."}
    assert refused(attestation, valid_from=today, valid_through=today - DAY) == {"valid_through": "The last day it covers cannot be before the first."}
    assert refused(services.add_attestation, vendor_names=[VENDOR], covers=Covers.ALL, reference="LTR-1", received_on=today, valid_from=None,
                   valid_through=None) == {"valid_from": "Enter the first day it covers.", "valid_through": "Enter the last day it covers."}
    training(dana, catalog.mx750, issued_on=today, expires_on=today)  # issued today, for one day: fine
    attestation(valid_from=today + 30 * DAY, valid_through=today + 395 * DAY, received_on=today)  # a period starting later: fine


def test_a_record_entered_after_the_job_still_counts_and_says_so(catalog, dana, today):
    job_day = today - 30 * DAY
    late = training(dana, catalog.mx750, issued_on=today - 60 * DAY)  # issued before the job, entered today
    found = evidence(job_day, catalog.mx750, eng=dana)
    assert (found.kind, found.record, found.entered_after) == (rules.TRAINING, late, True)
    assert rules.evidence_words(found, models_by_id()).endswith(f"(on file since {rules.fmt(today)})")
    on_file_since(late, today - 40 * DAY)
    found = evidence(job_day, catalog.mx750, eng=dana)
    assert not found.entered_after and "on file since" not in rules.evidence_words(found, models_by_id())
    # one on file that day is shown before one entered later, then the one running longest
    longer = training(dana, catalog.mx750, catalog.mx450, issued_on=today - 90 * DAY)
    assert evidence(job_day, catalog.mx750, eng=dana).record == late  # longer runs as long (no expiry) but was entered after the job
    on_file_since(longer, today - 80 * DAY)
    EngineerTraining.objects.filter(pk=late.pk).update(expires_on=today + 30 * DAY)
    assert evidence(job_day, catalog.mx750, eng=dana).record == longer  # both on file that day: no expiry runs longest


def test_a_record_entered_in_error_is_removed_and_covers_no_day(catalog, dana, mon, today, make_user):
    t = training(dana, catalog.mx750, issued_on=today - 100 * DAY)
    letter = attestation()
    ids = (t.pk, letter.pk)  # delete() clears them
    assert evidence(today, catalog.mx750, eng=dana).kind == rules.TRAINING
    services.remove_training(t, by=make_user("manager"))
    assert evidence(today, catalog.mx750, eng=dana).kind == rules.ATTESTATION
    services.remove_attestation(letter)
    assert evidence(today, catalog.mx750, eng=dana) == rules.Evidence(rules.NONE)
    for model, pk in zip((EngineerTraining, VendorAttestation), ids, strict=True):
        gone = model.history.filter(id=pk).first()
        assert (gone.history_type, gone.history_change_reason) == ("-", "Removed: entered in error")
        assert not model.objects.filter(pk=pk).exists()
    assert VendorEngineer.objects.filter(pk=dana.pk).exists()  # the engineer stays


# --- the evidence for a job -----------------------------------------------------------------------------------------------------

def test_the_engineers_training_first_then_an_attestation_then_none(catalog, dana, today):
    day = today - 10 * DAY
    assert evidence(day, catalog.mx750, eng=dana) == rules.Evidence(rules.NONE)
    assert evidence(day, catalog.mx750) == rules.Evidence(rules.NONE)
    letter = attestation()
    on_file_since(letter, today - 400 * DAY)
    assert evidence(day, catalog.mx750) == rules.Evidence(rules.ATTESTATION, letter)  # no engineer named: the vendor's letter
    assert evidence(day, catalog.mx750, eng=dana) == rules.Evidence(rules.ATTESTATION, letter)
    t = on_file_since(training(dana, catalog.mx750), today - 400 * DAY)
    assert evidence(day, catalog.mx750, eng=dana) == rules.Evidence(rules.TRAINING, t)  # the engineer's own training first
    assert evidence(day, catalog.mx750) == rules.Evidence(rules.ATTESTATION, letter)  # not named: never someone's training
    lee = engineer("Lee Park")
    assert evidence(day, catalog.mx750, eng=lee).kind == rules.ATTESTATION  # another engineer's training is not theirs
    assert evidence(day, catalog.ct, eng=dana).kind == rules.ATTESTATION  # her monitor training does not reach the CT


def test_an_attestation_from_the_work_orders_vendor_or_the_engineers_company(catalog, today):
    day = today - 10 * DAY
    sub = engineer("Lee Park", company="Philips")  # Philips sends its engineer on a job for Acme Biomed (subcontracting)
    letter = on_file_since(attestation("Philips Healthcare", "Philips field service"), today - 400 * DAY)
    assert evidence(day, catalog.mx750, vendor="Philips field service").record == letter
    assert evidence(day, catalog.mx750, vendor="PHILIPS").record == letter  # "Philips field service" is Philips
    assert evidence(day, catalog.mx750, vendor="Philips Healthcare").record == letter
    assert evidence(day, catalog.mx750, vendor="Acme Biomed").kind == rules.NONE
    assert evidence(day, catalog.mx750, vendor="Acme Biomed", eng=sub).record == letter  # the engineer's company's letter
    acme = on_file_since(attestation("Acme Biomed", reference="ACME-2026"), today - 400 * DAY)
    assert evidence(day, catalog.mx750, vendor="Acme Biomed").record == acme
    assert evidence(day, catalog.mx750, vendor="GE Healthcare", eng=engineer("Sam Ruiz", company="TechCare")).kind == rules.NONE


def test_the_expired_training_is_said_when_nothing_covers_the_job(catalog, dana, today):
    training(dana, catalog.mx750, issued_on=today - 800 * DAY, expires_on=today - 400 * DAY)
    training(dana, catalog.mx750, issued_on=today - 380 * DAY, expires_on=today - 20 * DAY)
    training(dana, catalog.ct, issued_on=today - 380 * DAY, expires_on=today - 2 * DAY)  # another model's: not this job's
    found = evidence(today - 10 * DAY, catalog.mx750, eng=dana)
    assert (found.kind, found.expired_on) == (rules.NONE, today - 20 * DAY)
    assert rules.evidence_words(found, models_by_id()) == (
        f"No training or attestation on file; the engineer's training on this model expired on {rules.fmt(today - 20 * DAY)}")
    assert evidence(today - 10 * DAY, catalog.mx750).expired_on is None  # nobody named: no one's training to speak of
    on_file_since(attestation(), today - 400 * DAY)
    assert evidence(today - 10 * DAY, catalog.mx750, eng=dana).kind == rules.ATTESTATION


def test_the_words_are_documents_on_file_never_competence(catalog, dana, today):
    t = training(dana, catalog.mx750, catalog.mx450, issued_on=today - 100 * DAY, expires_on=today + 200 * DAY, certificate="PH-88123")
    line = training(dana, covers=Covers.LINE, manufacturer="Philips", category="Patient monitors", source=TrainingSource.THIRD_PARTY,
                    issued_on=today - 50 * DAY)
    names = ", ".join(f"Philips {models_by_id()[i].model}" for i in t.model_ids)  # in the record's order
    assert rules.training_words(t, models_by_id()) == (
        f"Training on file: {names}, Manufacturer's (OEM) factory training, through {rules.fmt(today + 200 * DAY)}")
    assert rules.training_words(line, models_by_id()) == "Training on file: Philips Patient monitors, Third-party course, no expiry"
    letter = on_file_since(attestation("Philips Healthcare", "Philips field service", reference="LTR-1", valid_from=today - 100 * DAY,
                                       valid_through=today + 265 * DAY), today - 90 * DAY)
    words = rules.evidence_words(rules.Evidence(rules.ATTESTATION, letter), models_by_id())
    assert words == (f"Attestation LTR-1 on file (Philips Healthcare, Philips field service), valid {rules.fmt(today - 100 * DAY)} to "
                     f"{rules.fmt(today + 265 * DAY)}")
    assert rules.coverage_words(letter, {}) == "Every device this vendor services for us"
    catalog.mx450.delete()
    assert rules.coverage_words(t, models_by_id()) == names.replace("Philips IntelliVue MX450", "a removed model")
    every = [rules.training_words(t, models_by_id()), rules.training_words(line, models_by_id()), words,
             rules.evidence_words(evidence(today, catalog.ct, eng=dana), models_by_id()), *TrainingSource.labels, *Covers.labels]
    assert not [w for w in every if NEVER.search(w)]
    assert rules.fmt(date(2027, 3, 1)) == "Mar 1, 2027" and rules.fmt(None) == ""


# --- trainings and attestations: who and which ----------------------------------------------------------------------------------

def test_a_training_for_several_of_one_vendors_engineers(catalog, dana, today, other_tenant):
    lee, sam = engineer("Lee Park", company="Philips field service"), engineer("Sam Ruiz", company="TechCare")
    made = services.add_training([dana, lee, dana], covers=Covers.MODELS, model_ids=[str(catalog.mx750.pk)], source=TrainingSource.OEM,
                                 issued_on=today - 5 * DAY)
    assert sorted(t.engineer.name for t in made) == ["Dana Kim", "Lee Park"]  # one each, dana sent twice
    assert refused(services.add_training, [dana, sam], covers=Covers.MODELS, model_ids=[str(catalog.mx750.pk)], source=TrainingSource.OEM,
                   issued_on=today) == {"engineers": "Choose engineers of one vendor: add each vendor's training on its own."}
    assert refused(services.add_training, [dana, lee], covers=Covers.MODELS, model_ids=[str(catalog.mx750.pk)], source=TrainingSource.OEM,
                   certificate="PH-1", issued_on=today) == {
        "certificate": "A certificate number is one engineer's: add it to each engineer's own training."}
    assert refused(services.add_training, [], covers=Covers.MODELS, model_ids=[str(catalog.mx750.pk)], source=TrainingSource.OEM,
                   issued_on=today) == {"engineers": "Choose the engineer."}
    assert refused(services.add_training, [str(dana.pk)], covers=Covers.MODELS, model_ids=[str(catalog.mx750.pk)], source=TrainingSource.OEM,
                   issued_on=today) == {"engineers": "Choose engineers on file."}
    assert "without spaces" in refused(training, dana, catalog.mx750, certificate="PH 1")["certificate"]
    assert refused(training, dana, catalog.mx750, certificate="P" * 41) == {"certificate": "Keep the certificate number to 40 characters."}
    with tenant_context(other_tenant):
        theirs = engineer("Dana Kim")
    assert refused(services.add_training, [theirs], covers=Covers.MODELS, model_ids=[str(catalog.mx750.pk)], source=TrainingSource.OEM,
                   issued_on=today) == {"engineers": "Choose engineers on file."}
    assert EngineerTraining.unscoped.filter(engineer=theirs).count() == 0  # unscoped: nothing crossed facilities


def test_an_attestation_names_the_vendor_as_work_orders_do(catalog, today):
    a = attestation(" Philips Healthcare ", "Philips field service", "PHILIPS", "", reference="SC-2026-118/7.2")
    assert (a.vendor_names, a.vendor_keys) == (["Philips Healthcare", "Philips field service"], ["philips healthcare", "philips"])
    assert services.attestations_for("philips") == [a] and services.attestations_for("Philips Healthcare field service") == [a]
    assert services.attestations_for("GE") == [] and services.attestations_for("") == []
    assert refused(attestation, "", " ") == {"vendor_names": "Enter the vendor as your work orders and contracts name it."}
    assert refused(attestation, "X" * 121) == {"vendor_names": "Keep each vendor name to 120 characters."}
    assert refused(attestation, reference="") == {"reference": "Enter the letter's reference, or the contract and clause."}
    assert "without spaces" in refused(attestation, reference="LTR 1")["reference"]
    assert refused(attestation, reference="L" * 41) == {"reference": "Keep the reference to 40 characters."}
    assert services.add_attestation(vendor_names="TechCare", covers=Covers.ALL, reference="TC-1", received_on=today, valid_from=today,
                                    valid_through=today + 364 * DAY).vendor_names == ["TechCare"]


# --- the engineer at completion ---------------------------------------------------------------------------------------------------

def test_completion_names_an_engineer_an_id_someone_else_or_nobody(mon, dana, today, make_user):
    manager = make_user("manager")
    assert vendor_job(mon, engineer=dana).vendor_engineer == dana
    assert vendor_job(mon, engineer=str(dana.pk)).vendor_engineer == dana
    assert vendor_job(mon, engineer_name="KIM, Dana").vendor_engineer == dana  # the name on file, in any letter case or order
    wo = vendor_work(mon, "Philips field service")
    finish(wo, engineer_name="Lee  Park", by=manager)
    lee = wo.vendor_engineer
    assert (lee.name, lee.company, lee.company_key, lee.created_by) == ("Lee Park", "Philips field service", "philips", manager)
    assert lee.history.get().history_change_reason == f"Added at completion of {wo.number}"
    assert vendor_job(mon).vendor_engineer is None  # nothing sent: not named
    wo = vendor_work(mon)
    finish(wo, vendor_engineer=None)
    assert wo.vendor_engineer is None
    wo = vendor_work(mon, type_=WoType.PM)
    finish(wo, vendor_engineer="")
    assert wo.vendor_engineer is None and wo.pm_result == PmResult.PASS


def test_a_reopened_job_keeps_its_engineer_unless_the_completion_says_otherwise(mon, dana):
    wo = vendor_job(mon, engineer=dana)
    change_status(wo, WoStatus.IN_PROGRESS)  # reopened: who did the work stays
    wo.refresh_from_db()
    assert wo.vendor_engineer == dana
    finish(wo)  # KEEP: an API completion that sends no engineer
    assert wo.vendor_engineer == dana
    services.update_engineer(dana, is_active=False)
    change_status(wo, WoStatus.IN_PROGRESS)
    finish(wo, vendor_engineer=str(dana.pk))  # inactive since, still the one this work order names
    assert wo.vendor_engineer == dana
    change_status(wo, WoStatus.IN_PROGRESS)
    finish(wo, vendor_engineer=None)  # the report names nobody after all
    assert wo.vendor_engineer is None
    hist = [h.vendor_engineer_id for h in wo.history.order_by("history_date", "history_id")]
    assert hist[-1] is None and dana.pk in hist


def test_completion_refuses_what_it_cannot_name(mon, dana, techs, other_tenant):
    wo = vendor_work(mon)
    assert not_completed(wo, vendor_engineer=dana, engineer_name="Lee Park") == {"vendor_engineer": services.NOT_BOTH}
    assert not_completed(wo, vendor_engineer="not-an-id") == {"vendor_engineer": services.CHOOSE_LISTED}
    assert not_completed(wo, vendor_engineer=str(VendorEngineer(name="x").pk)) == {"vendor_engineer": services.CHOOSE_LISTED}
    assert not_completed(wo, engineer_name="Lee 2") == {"engineer_name": rules.NAME_RULE}
    with tenant_context(other_tenant):
        theirs = engineer("Dana Kim")
    assert not_completed(wo, vendor_engineer=str(theirs.pk)) == {"vendor_engineer": services.CHOOSE_LISTED}
    assert not_completed(wo, vendor_engineer=theirs) == {"vendor_engineer": services.CHOOSE_LISTED}
    lee = engineer("Lee Park")
    stale = VendorEngineer.objects.get(pk=lee.pk)
    services.update_engineer(lee, is_active=False)
    assert not_completed(wo, vendor_engineer=stale) == {"vendor_engineer": services.CHOOSE_LISTED}  # a copy from before it was deactivated
    assert not_completed(wo, vendor_engineer=str(lee.pk)) == {"vendor_engineer": services.CHOOSE_LISTED}
    # every problem at once, and a typed name made no engineer when the completion was refused
    errors = refused(complete_work_order, wo, resolution="Fixed", engineer_name="Sam Ruiz")
    assert "repair_check" in errors and not VendorEngineer.objects.filter(name="Sam Ruiz").exists()
    # in-house work names no vendor engineer
    own = in_house_work(mon, techs["dana"])
    assert not_completed(own, vendor_engineer=dana) == {"vendor_engineer": services.ENGINEER_ONLY}
    assert not_completed(own, engineer_name="Dana Kim") == {"vendor_engineer": services.ENGINEER_ONLY}
    finish(own, vendor_engineer=None)
    assert own.vendor_engineer is None and own.status == WoStatus.COMPLETED


def test_a_vendor_account_names_its_own_companys_engineers_or_types_a_name(mon, dana, vendor_user, make_user, other_tenant):
    sub = engineer("Lee Park", company="Acme Biomed")
    gone = engineer("Pat Moss")
    services.update_engineer(gone, is_active=False)
    wo = vendor_work(mon, "Philips field service")
    assert services.engineer_options(wo, vendor_user) == ([dana], [])  # their company's active engineers, no one else's
    assert services.engineer_options(wo, make_user("manager")) == ([dana], [sub])
    for value in (sub, str(sub.pk), str(gone.pk), "not-an-id"):  # another company's, inactive, unknown: one answer
        assert not_completed(wo, vendor_engineer=value, by=vendor_user) == {"vendor_engineer": services.CHOOSE_LISTED}
    with tenant_context(other_tenant):
        theirs = engineer("Dana Kim")
    assert not_completed(wo, vendor_engineer=str(theirs.pk), by=vendor_user) == {"vendor_engineer": services.CHOOSE_LISTED}
    finish(wo, vendor_engineer=str(dana.pk), by=vendor_user)
    assert wo.vendor_engineer == dana
    typed = vendor_work(mon, "Philips field service")
    finish(typed, engineer_name="Kai Moreno", by=vendor_user)
    kai = typed.vendor_engineer
    assert (kai.company, kai.company_key, kai.created_by) == ("Philips field service", "philips", vendor_user)
    assert kai.history.get().history_change_reason == f"Added at completion of {typed.number}"
    assert not kai.trainings.exists()
    # CE named a subcontractor on a Philips job; reopened, the vendor's account sees "another company's engineer" and keeps them
    ce = vendor_work(mon)
    set_vendor_engineer(ce, sub, by=make_user("technician"))
    assert services.engineer_options(ce, vendor_user) == ([dana, kai], [])
    assert services.engineer_shown(ce, vendor_user) == services.ANOTHER_COMPANY
    finish(ce, by=vendor_user)
    assert ce.vendor_engineer == sub


def test_the_options_put_the_named_engineer_first_even_inactive(mon, dana, make_user, vendor_user):
    lee, sam = engineer("Lee Park"), engineer("Sam Ruiz", company="TechCare")
    wo = vendor_work(mon)
    set_vendor_engineer(wo, lee)
    services.update_engineer(lee, is_active=False)
    wo.refresh_from_db()
    assert services.engineer_options(wo, make_user("manager")) == ([lee, dana], [sam])
    assert services.engineer_options(wo, vendor_user) == ([lee, dana], [])
    set_vendor_engineer(wo, sam)
    wo.refresh_from_db()
    assert services.engineer_options(wo, make_user("technician")) == ([dana], [sam])
    assert services.engineer_options(wo, vendor_user) == ([dana], [])


# --- the vendor's report number --------------------------------------------------------------------------------------------------

def test_the_vendors_report_number_on_any_vendor_job_never_in_house(mon, ct, techs):
    pm = vendor_work(mon, type_=WoType.PM)
    finish(pm, checks_reference=" FSO-26-1 ")
    assert pm.checks_reference == "FSO-26-1"
    repair = vendor_work(mon)
    finish(repair, checks_reference="FSO-26-2")  # checked and ready for use by the vendor's own steps: the report number too
    assert (repair.repair_check, repair.checks_reference) == (RepairCheck.PASSED, "FSO-26-2")
    out = vendor_work(ct)
    finish(out, repair_check=RepairCheck.NOT_RETURNED, results=None, checks_reference="FSO-26-3")
    assert (out.repair_check, out.checks_reference) == (RepairCheck.NOT_RETURNED, "FSO-26-3")
    checked = vendor_work(mon)
    assert not_completed(checked, repair_check=RepairCheck.VENDOR_REPORT, results=None) == {
        "checks_reference": "Enter the vendor's service report or field service order number."}  # still required with checked by the vendor
    assert not_completed(checked, checks_reference="FSO 4") == {
        "checks_reference": "Enter the vendor's service report or field service order number as it is printed, without spaces."}
    assert not_completed(checked, checks_reference="F" * 41) == {"checks_reference": "Keep the report number to 40 characters."}
    assert set(not_completed(checked, repair_check="", results=None, checks_reference="FSO-4")) == {"repair_check"}  # the outcome first
    finish(checked, repair_check=RepairCheck.VENDOR_REPORT, results=None, checks_reference="FSO-4")
    assert checked.checks_reference == "FSO-4"
    inspection = vendor_work(mon, type_=WoType.INSPECTION)
    finish(inspection, checks_reference="FSO-26-5")
    assert inspection.checks_reference == "FSO-26-5"
    finish(vendor_work(mon, type_=WoType.PM))  # optional
    for type_ in (WoType.PM, WoType.REPAIR, WoType.INSPECTION, WoType.SAFETY):
        own = in_house_work(mon, techs["dana"], type_=type_)
        assert not_completed(own, checks_reference="FSO-9") == {"checks_reference": completion.REFERENCE_ONLY}
        finish(own, checks_reference="  ")
        assert own.checks_reference == ""


# --- naming the engineer after (or before) completion ----------------------------------------------------------------------------

def test_set_vendor_engineer_on_vendor_work_not_cancelled(mon, dana, techs, vendor_user, make_user):
    kim = make_user("manager")
    wo = vendor_work(mon, start=False)
    set_vendor_engineer(wo, dana, by=kim)  # open: the engineer is on site
    assert WorkOrder.objects.get(pk=wo.pk).vendor_engineer == dana
    assert wo.history.first().history_change_reason == "Field engineer: Dana Kim"
    rows = wo.history.count()
    set_vendor_engineer(wo, str(dana.pk), by=kim)  # no change: no history row
    assert wo.history.count() == rows
    change_status(wo, WoStatus.IN_PROGRESS)
    finish(wo)
    set_vendor_engineer(wo, None, by=kim)  # completed: the report names nobody
    assert wo.vendor_engineer is None and wo.history.first().history_change_reason == "Field engineer cleared"
    change_status(wo, WoStatus.CLOSED)
    locked = set_vendor_engineer(wo, engineer_name="Lee Park", by=kim)  # closed (Approve: the caller's check), a name typed
    lee = locked.vendor_engineer
    assert (wo.vendor_engineer, lee.company, lee.created_by) == (lee, VENDOR, kim)
    assert lee.history.get().history_change_reason == f"Added as the field engineer of {wo.number}"
    assert wo.history.first().history_change_reason == "Field engineer: Lee Park"
    assert refused(set_vendor_engineer, wo, dana, engineer_name="Lee Park") == {"vendor_engineer": services.NOT_BOTH}
    assert refused(set_vendor_engineer, wo, "nobody") == {"vendor_engineer": services.CHOOSE_LISTED}
    assert refused(set_vendor_engineer, wo, engineer_name="Lee 2") == {"engineer_name": rules.NAME_RULE}
    # never a vendor account's, even on its own company's work
    assert refused(set_vendor_engineer, wo, dana, by=vendor_user) == {
        "vendor_engineer": "The facility's CE staff record who did the vendor's work after it is completed."}
    cancelled = vendor_work(mon, start=False)
    change_status(cancelled, WoStatus.CANCELLED)
    assert refused(set_vendor_engineer, cancelled, dana) == {"vendor_engineer": "A cancelled work order names no engineer."}
    own = in_house_work(mon, techs["dana"])
    assert refused(set_vendor_engineer, own, dana) == {"vendor_engineer": services.ENGINEER_ONLY}
    assert refused(set_vendor_engineer, own, None) == {"vendor_engineer": services.ENGINEER_ONLY}  # review fix: was a silent no-op
    assert WorkOrder.objects.get(pk=wo.pk).vendor_engineer == lee


def test_a_long_name_fits_the_historys_reason(mon):
    """Review fix: "Field engineer: <name>" is the work order history's reason, a 100-character column; a name of 120 was a database
    error on PostgreSQL (SQLite stores it whole)."""
    long = engineer("A" * 60 + " " + "B" * 59)
    wo = vendor_work(mon)
    set_vendor_engineer(wo, long)
    reason = wo.history.first().history_change_reason
    assert reason.startswith("Field engineer: AAAA") and len(reason) == 100
    assert WorkOrder.objects.get(pk=wo.pk).vendor_engineer == long


def test_who_may_change_the_engineer(mon, dana, make_user, vendor_user):
    technician, manager, analyst = make_user("technician"), make_user("manager"), make_user("analyst")
    wo = vendor_job(mon, engineer=dana)
    assert wo_perms.can_set_vendor_engineer(technician, wo) and wo_perms.can_set_vendor_engineer(manager, wo)
    assert not wo_perms.can_set_vendor_engineer(analyst, wo) and not wo_perms.can_set_vendor_engineer(vendor_user, wo)
    change_status(wo, WoStatus.CLOSED)
    assert not wo_perms.can_set_vendor_engineer(technician, wo) and wo_perms.can_set_vendor_engineer(manager, wo)
    lead = User.objects.create_user(username="lead@riverside.example", password="x", tenant=wo.tenant, company=VENDOR,
                                    role=custom_scoped_role({m: Level.FULL for m in Module.values}))
    assert not wo_perms.can_set_vendor_engineer(lead, wo)


# --- assign: only open work, and the engineer goes with the work -----------------------------------------------------------------

def test_assign_moves_only_open_work(mon, dana, techs):
    done = vendor_job(mon, engineer=dana)
    for status in (WoStatus.COMPLETED, WoStatus.CLOSED):
        if status == WoStatus.CLOSED:
            change_status(done, WoStatus.CLOSED)
        with pytest.raises(ValidationError, match="Only open work orders can be reassigned."):
            assign(done, technician=techs["dana"])
        done.refresh_from_db()
        assert (done.vendor_service, done.vendor_name, done.vendor_engineer) == (True, VENDOR, dana)
    cancelled = vendor_work(mon, start=False)
    change_status(cancelled, WoStatus.CANCELLED)
    with pytest.raises(ValidationError, match="Only open work orders can be reassigned."):
        assign(cancelled, vendor_name="GE Healthcare")
    waiting = vendor_work(mon)
    change_status(waiting, WoStatus.AWAITING_PARTS)
    assign(waiting, vendor_name="TechCare")  # awaiting parts is open
    assert waiting.vendor_name == "TechCare"


def test_the_engineer_goes_when_the_work_moves_and_stays_with_the_same_vendor(mon, dana, techs):
    wo = vendor_work(mon)
    set_vendor_engineer(wo, dana)
    assign(wo, vendor_name="PHILIPS field service")  # the same vendor, another spelling
    assert WorkOrder.objects.get(pk=wo.pk).vendor_engineer == dana
    assign(wo, vendor_name="TechCare")
    assert WorkOrder.objects.get(pk=wo.pk).vendor_engineer is None
    set_vendor_engineer(wo, engineer("Sam Ruiz", company="TechCare"))
    sam = WorkOrder.objects.get(pk=wo.pk).vendor_engineer
    assign(wo, technician=techs["tom"])
    wo.refresh_from_db()
    assert (wo.vendor_service, wo.vendor_engineer) == (False, None)
    last, before = wo.history.order_by("-history_date", "-history_id")[:2]
    assert (last.vendor_engineer_id, before.vendor_engineer_id, last.assigned_to_id) == (None, sam.pk, techs["tom"].pk)  # audited


def test_assign_reads_the_work_order_as_stored(mon, dana, techs):
    """Review fix: assign read the status and the engineer on the caller's copy. A copy read before a Change committed moved the work
    in-house keeping the vendor's engineer; one read before the completion committed moved completed work."""
    wo = vendor_work(mon)
    stale = WorkOrder.objects.get(pk=wo.pk)
    set_vendor_engineer(wo, dana)
    assign(stale, technician=techs["dana"])
    wo.refresh_from_db()
    assert (wo.vendor_service, wo.vendor_engineer) == (False, None)
    other = vendor_work(mon)
    stale = WorkOrder.objects.get(pk=other.pk)
    finish(other, vendor_engineer=dana)
    with pytest.raises(ValidationError, match="Only open work orders can be reassigned."):
        assign(stale, technician=techs["dana"])
    other.refresh_from_db()
    assert (other.status, other.vendor_service, other.vendor_engineer) == (WoStatus.COMPLETED, True, dana)


# --- the paths that never write it -------------------------------------------------------------------------------------------------

def test_a_failed_pms_repair_and_a_reinspection_name_no_engineer(mon, dana, catalog, today):
    pm = vendor_work(mon, type_=WoType.PM)
    done = complete_work_order(pm, pm_result=PmResult.FAIL, resolution="Fails its self-test", vendor_engineer=dana)
    assert pm.vendor_engineer == dana
    fu = done.follow_up
    assert (fu.vendor_service, fu.vendor_name, fu.vendor_engineer_id) == (True, VENDOR, None)
    asset = waiting_device(catalog.mx450, today=today)
    first = inspections.open_inspection(asset)
    assign(first, vendor_name=VENDOR)
    done = complete_work_order(first, inspection_result=InspectionResult.FAILED, resolution="Leakage over the limit", vendor_engineer=dana,
                               results=incoming_results("pass", "fail", "pass", "pass", "pass", reading="640"), today=today)
    again = done.reinspection
    assert first.vendor_engineer == dana and (again.vendor_service, again.vendor_engineer_id) == (True, None)
    change_status(again, WoStatus.IN_PROGRESS)
    complete_work_order(again, inspection_result=InspectionResult.PASSED, results=INCOMING_ALL_PASS, today=today)
    assert again.vendor_engineer is None


def test_a_recall_batch_and_the_legacy_import_name_no_engineer(pump_recall, pump, pump_model, techs, today):
    philips = engineer("Dana Kim")
    rental = eq.create_asset(tag="T-1", device_model=pump_model, department=pump.department, serial="SN-T-1", ownership=Ownership.RENTAL,
                             owner=VENDOR, owner_reference="RA-2026-0042", arrived_on=today, due_back_on=today + 14 * DAY,
                             incoming_inspection="", added_as=AddedAs.EXISTING, today=today)
    recalls.create_recall_work_orders(pump_recall)
    theirs = WorkOrder.objects.get(alert=pump_recall.alert, asset=rental)
    assert (theirs.vendor_service, theirs.vendor_name, theirs.vendor_engineer_id) == (True, VENDOR, None)
    imported = legacy.import_work_order(asset=pump, legacy_number="OLD-118", type=WoType.REPAIR, status=WoStatus.COMPLETED,
                                        opened_on=today - 40 * DAY, completed_on=today - 38 * DAY, vendor_name=VENDOR).work_order
    assert (imported.vendor_service, imported.vendor_engineer_id) == (True, None)
    assert VendorEngineer.objects.get() == philips  # nothing made from either


def test_keep_open_and_reopening_leave_the_engineer_as_it_is(mon, dana):
    wo = vendor_work(mon)
    set_vendor_engineer(wo, dana)
    keep_open(wo, results=post_repair_results("pass", "fail", *["pass"] * 5))
    wo.refresh_from_db()
    assert wo.status == WoStatus.IN_PROGRESS and wo.vendor_engineer == dana
    bare = vendor_work(mon)
    keep_open(bare, results=post_repair_results("pass", "fail", *["pass"] * 5), hold=False)
    bare.refresh_from_db()
    assert bare.vendor_engineer is None
    finish(wo)
    for move in (WoStatus.IN_PROGRESS, WoStatus.COMPLETED):
        if move == WoStatus.IN_PROGRESS:
            change_status(wo, move)
        else:
            finish(wo)
        assert WorkOrder.objects.get(pk=wo.pk).vendor_engineer == dana


# --- who checked it, as each reader may read it ---------------------------------------------------------------------------------

def test_checked_by_names_the_engineer_and_masks_another_companys_from_a_vendor(mon, dana, techs, vendor_user, make_user):
    named = vendor_job(mon, engineer=dana)
    assert repair_checks.checked_by(named) == "Dana Kim (Philips)" == repair_checks.checked_by(named, vendor_user)
    assert services.engineer_shown(named, vendor_user) == "Dana Kim (Philips)"
    sub = vendor_job(mon, engineer=engineer("Lee Park", company="Acme Biomed"))
    assert repair_checks.checked_by(sub, make_user("manager")) == "Lee Park (Acme Biomed)"
    assert repair_checks.checked_by(sub, vendor_user) == "Another company's engineer"
    nobody = vendor_job(mon, "Philips field service")
    assert repair_checks.checked_by(nobody) == "Philips field service" and services.engineer_shown(nobody, vendor_user) == ""
    own = in_house_work(mon, techs["dana"])
    finish(own)
    assert repair_checks.checked_by(own) == "Dana Whitfield"


# --- facilities, deletes, and levels ----------------------------------------------------------------------------------------------

def test_vendor_staff_records_stay_in_their_facility(catalog, dana, mon, other_tenant):
    training(dana, catalog.mx750)
    attestation()
    with tenant_context(other_tenant):
        assert not VendorEngineer.objects.exists() and not EngineerTraining.objects.exists() and not VendorAttestation.objects.exists()
        assert list(services.engineers_for(VENDOR)) == [] and services.attestations_for(VENDOR) == []
        theirs = engineer("Dana Kim")  # their own Dana Kim of Philips: the unique key is per facility
        model = DeviceModel.objects.create(manufacturer="Philips", model="IntelliVue MX750", description="Monitor", category="Patient monitors")
        training(theirs, model)
        attestation(reference="THEIRS-1")
        assert (VendorEngineer.objects.get(), EngineerTraining.objects.count(), VendorAttestation.objects.get().reference) == (theirs, 1, "THEIRS-1")
    assert VendorEngineer.objects.get() == dana and EngineerTraining.objects.get().engineer == dana
    assert VendorAttestation.objects.get().reference == "LTR-2026-01"
    assert services.engineer_named(VENDOR, "Dana Kim") == dana


def test_an_engineer_named_on_work_or_with_training_is_never_deleted(catalog, dana, mon):
    vendor_job(mon, engineer=dana)
    with pytest.raises(ProtectedError):
        dana.delete()
    lee = engineer("Lee Park")
    training(lee, catalog.mx750)
    with pytest.raises(ProtectedError):
        lee.delete()
    for model_admin in (VendorEngineerAdmin(VendorEngineer, admin.site), EngineerTrainingAdmin(EngineerTraining, admin.site),
                        VendorAttestationAdmin(VendorAttestation, admin.site)):
        assert not (model_admin.has_add_permission(None) or model_admin.has_change_permission(None) or model_admin.has_delete_permission(None))


def test_contracts_view_sees_and_edit_changes_never_a_scoped_user(ctx, make_user, vendor_user):
    seen = {slug: (vendor_perms.can_view(make_user(slug)), vendor_perms.can_edit(make_user(slug, username=f"{slug}2@riverside.example")))
            for slug in ("director", "manager", "technician", "analyst", "requester")}
    assert seen == {"director": (True, True), "manager": (True, True), "technician": (True, False), "analyst": (True, False),
                    "requester": (False, False)}
    assert not vendor_perms.can_view(vendor_user) and not vendor_perms.can_edit(vendor_user)
    lead = User.objects.create_user(username="lead@riverside.example", password="x", tenant=ctx, company=VENDOR,
                                    role=custom_scoped_role({m: Level.FULL for m in Module.values}))
    assert lead.has_level(Module.CONTRACTS, Level.FULL) and not vendor_perms.can_view(lead) and not vendor_perms.can_edit(lead)


# --- PostgreSQL ---------------------------------------------------------------------------------------------------------------------

@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_two_completions_typing_one_new_name_make_one_engineer(tenant, monkeypatch):
    """Two connections complete two vendor jobs at the same moment, each typing a name nobody has on file: both look and find nobody (a
    barrier before the insert), both insert, and the unique key keeps one; the second reads it again in a savepoint and its completion
    goes on, naming the same engineer."""
    with tenant_context(tenant):
        model = DeviceModel.objects.create(manufacturer="Philips", model="IntelliVue MX750", description="Monitor", category="Patient monitors")
        dept = Department.objects.create(name="ICU")
        jobs = [vendor_work(Asset.objects.create(tag=f"CE-3000{n}", device_model=model, department=dept), vendor) for n, vendor in
                enumerate(("Philips field service", "PHILIPS"))]
    barrier = threading.Barrier(2, timeout=20)
    real_save, inserts = VendorEngineer.save, []

    def save_together(self, *args, **kwargs):
        if self._state.adding:
            inserts.append(self.name)
            barrier.wait()  # both found nobody: both insert now
        return real_save(self, *args, **kwargs)

    monkeypatch.setattr(VendorEngineer, "save", save_together)
    errors = []

    def complete(pk):
        try:
            with tenant_context(tenant):
                complete_work_order(WorkOrder.objects.get(pk=pk), resolution="Replaced the module", engineer_name="Dana  Kim", **CHECKED)
        except Exception as e:  # noqa: BLE001 - reported below
            errors.append(e)
        finally:
            connection.close()

    threads = [threading.Thread(target=complete, args=(wo.pk,)) for wo in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors and inserts == ["Dana Kim", "Dana Kim"], errors
    with tenant_context(tenant):
        dana = VendorEngineer.objects.get()
        assert (dana.name, dana.company_key) == ("Dana Kim", "philips")
        assert set(WorkOrder.objects.filter(pk__in=[w.pk for w in jobs]).values_list("status", "vendor_engineer")) == {(WoStatus.COMPLETED, dana.pk)}


@needs_postgres
def test_the_vendor_staff_services_under_the_policies(catalog, mon, other_tenant, make_user, today):
    with tenant_context(other_tenant):
        theirs = engineer("Sam Ruiz")
    as_app_role()
    dana = engineer("Dana Kim")
    services.update_engineer(dana, name="Dana Kim")
    t = training(dana, catalog.mx750, issued_on=today - 30 * DAY)
    letter = attestation("Philips Healthcare", "Philips field service")
    wo = vendor_work(mon, "Philips field service")
    assert not_completed(wo, vendor_engineer=str(theirs.pk)) == {"vendor_engineer": services.CHOOSE_LISTED}
    finish(wo, engineer_name="Lee Park", checks_reference="FSO-1")
    lee = wo.vendor_engineer
    set_vendor_engineer(wo, dana, by=make_user("manager"))
    assert wo.vendor_engineer == dana and VendorEngineer.objects.filter(pk=lee.pk).exists()
    assert evidence(today, catalog.mx750, "Philips field service", dana) == rules.Evidence(rules.TRAINING, t, False)
    services.remove_training(t)
    assert evidence(today, catalog.mx750, "Philips field service", dana).record == letter
    assert sorted(VendorEngineer.objects.values_list("name", flat=True)) == ["Dana Kim", "Lee Park"]
    moved = vendor_work(mon)
    set_vendor_engineer(moved, lee)
    assign(moved, vendor_name="TechCare")
    assert WorkOrder.objects.get(pk=moved.pk).vendor_engineer_id is None
