"""
Slice 31, wave D: the survey binder's "Contracted service and vendor staff" section (apps/reports/survey/vendors.py), its place and
access (Reports, Work orders, Contracts, and Equipment View), the cover's reworded "not covered", the staff section's vendor note, and
the demo's deliberate gap.

The section: per vendor (merged by apps.vendors.rules.vendor_key) its contracts in the period, attestations, engineers with training on
file, and the period's jobs by evidence; the engineers and their training; the attestations; and each vendor job with the engineer named
and the evidence on its completed day (the named engineer's training naming the model, else the vendor's or the engineer's company's
attestation, else none). Each record by its own dates whenever entered ("on file since"), a renewal a new row, one entered in error
removed. A GAP per vendor whose jobs no evidence covers (linking to the Vendor staff tab filtered to it), a CHECK per vendor for
manufacturer-schedule jobs naming no engineer; imported jobs apart, never judged; never a person in a gap; never requester text; a
fixed number of queries; another facility's records never read; under row-level security as the runtime role.
"""
from datetime import datetime, time, timedelta
from io import StringIO

import pytest
from csvutil import csv_rows
from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from pg_helpers import as_app_role, needs_postgres
from survey_helpers import gaps_of, period, rows_of
from vendor_fixtures import attestation, engineer, training, vendor_job

from apps.accounts.models import Level, Module, Role, User
from apps.contracts.models import Contract, ContractType, Coverage
from apps.demo.management.commands import seed_demo
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel, RiskClass
from apps.reports import survey
from apps.reports.permissions import SURVEY_NEEDS, survey_refusal
from apps.reports.survey import CHECK, DEVICE, GAP, NOT_COVERED, WORK_ORDER, staff, vendors
from apps.tenants.context import tenant_context
from apps.tenants.models import Tenant
from apps.vendors import services as vs
from apps.vendors.models import Covers, TrainingSource
from apps.vendors.rules import fmt
from apps.workorders.models import PmResult, Priority, Source, WorkOrder, WoType
from apps.workorders.services import assign, create_work_order, set_vendor_engineer

DAY = timedelta(days=1)
SECRET = "Zq7Canary bed 4"  # what a requester typed: never in the binder
VENDOR = "Philips"


@pytest.fixture
def today(ctx):
    return timezone.localdate()


@pytest.fixture
def monitor_model(ctx):
    return DeviceModel.objects.create(manufacturer="Philips", model="IntelliVue MX750", description="Patient monitor", category="Patient monitoring",
                                      risk_class=RiskClass.HIGH, oem_pm_interval_months=12)


@pytest.fixture
def ct_model(ctx):
    """A Philips model of another category: a monitor's training never covers it."""
    return DeviceModel.objects.create(manufacturer="Philips", model="Incisive CT", description="CT scanner", category="Imaging",
                                      risk_class=RiskClass.HIGH, oem_schedule_required=True)


@pytest.fixture
def carm_model(ctx):
    return DeviceModel.objects.create(manufacturer="Siemens Healthineers", model="Cios Spin", description="Mobile C-arm", category="Imaging",
                                      risk_class=RiskClass.HIGH, oem_schedule_required=True)


@pytest.fixture
def monitor(ctx, dept, monitor_model):
    return Asset.objects.create(tag="CE-20001", device_model=monitor_model, department=dept)


@pytest.fixture
def ct(ctx, dept, ct_model):
    return Asset.objects.create(tag="CE-20002", device_model=ct_model, department=dept)


@pytest.fixture
def carm(ctx, dept, carm_model):
    return Asset.objects.create(tag="CE-20003", device_model=carm_model, department=dept)


def build(today, start=None, end=None):
    return vendors.build(period(start or today - 200 * DAY, end or today, today), None)


def figures(section) -> dict:
    return {f.label: f.value for f in section.figures}


def jobs_of(section) -> dict:
    """{work order number: its row} of the jobs table."""
    return {r[0]: r for r in rows_of(section, "jobs")}


def evidence(section, wo) -> str:
    return jobs_of(section)[wo.number][8]


def vendor_row(section, name) -> dict:
    rows = {r[0]: r for r in rows_of(section, "vendors")}
    return dict(zip(vendors.VENDOR_COLUMNS, rows[name]))


def gap_texts(section, kind=None) -> list[str]:
    return [g.text for g in gaps_of(section, kind)]


def entered(record, day):
    """A record as if it was entered in Cadence on `day` (the tests make every record today, after the jobs they cover)."""
    type(record).objects.filter(pk=record.pk).update(created_at=timezone.make_aware(datetime.combine(day, time(12))))
    record.refresh_from_db()
    return record


def secret(wo) -> WorkOrder:
    """Plant the requester-text canary in everything a requester (or the work order's free text) carries."""
    WorkOrder.objects.filter(pk=wo.pk).update(problem=SECRET, requester=SECRET, callback=SECRET, reported_location=SECRET, resolution=SECRET)
    return wo


# --- the evidence, each path and its words ----------------------------------------------------------------------------------------


def test_the_named_engineers_training_on_the_model(today, vent, vent_model, monitor_model):
    dana = engineer("Dana Kim", "Hamilton Medical field service")
    t = training(dana, vent_model, monitor_model, issued_on=today - 300 * DAY, expires_on=today + 100 * DAY, certificate="HM-G5-0042")
    entered(t, today - 290 * DAY)
    wo = vendor_job(vent, "Hamilton Medical field service", engineer=dana, completed_on=today - 10 * DAY)
    s = build(today)
    row = jobs_of(s)[wo.number]
    assert row[:8] == [wo.number, "Corrective repair", today - 10 * DAY, "CE-10001", "Hamilton Medical Hamilton-G5",
                       "Hamilton Medical field service", "Dana Kim (Hamilton Medical field service)", wo.checks_reference]
    names = {str(vent_model.pk): "Hamilton Medical Hamilton-G5", str(monitor_model.pk): "Philips IntelliVue MX750"}
    covers = ", ".join(names[i] for i in t.model_ids)  # the record's models, as stored
    assert row[8] == f"Training on file: {covers}, Manufacturer's (OEM) factory training, through {fmt(t.expires_on)}"
    assert figures(s)["Training on file"] == 1 and figures(s)["Nothing on file"] == 0 and s.gaps == []
    assert figures(s)["Evidence entered after the work"] == 0
    # the engineers table: the training as of the period's end, with the day it was entered
    assert rows_of(s, "engineers") == [["Hamilton Medical field service", "Dana Kim", True, covers, "Manufacturer's (OEM) factory training",
                                        "HM-G5-0042", today - 300 * DAY, today + 100 * DAY, today - 290 * DAY, "In force"]]


def test_a_product_category_never_a_whole_manufacturer(today, monitor, ct, monitor_model):
    dana = engineer()
    entered(training(dana, covers=Covers.LINE, manufacturer="Philips", category="Patient monitoring", source=TrainingSource.VENDOR),
            today - 300 * DAY)
    on_monitor = vendor_job(monitor, engineer=dana, completed_on=today - 5 * DAY)
    on_ct = vendor_job(ct, engineer=dana, completed_on=today - 4 * DAY)
    s = build(today)
    assert evidence(s, on_monitor) == "Training on file: Philips Patient monitoring, The vendor's own training program, no expiry"
    assert evidence(s, on_ct) == "No training or attestation on file"  # a monitor training never covers the CT
    assert gap_texts(s, GAP) == ["Philips: 1 job in the period with no training or attestation on file covering it"]


def test_the_vendors_attestation_covers_a_job_with_or_without_an_engineer(today, monitor, monitor_model):
    a = entered(attestation(VENDOR, covers=Covers.MODELS, model_ids=[str(monitor_model.pk)], reference="LTR-2026-07",
                            valid_from=today - 100 * DAY, valid_through=today + 265 * DAY), today - 95 * DAY)
    unnamed = vendor_job(monitor, "Philips field service", completed_on=today - 20 * DAY)  # the same vendor by key
    named = vendor_job(monitor, engineer=engineer(), completed_on=today - 10 * DAY)  # an engineer with no training: the letter covers them
    s = build(today)
    words = f"Attestation LTR-2026-07 on file (Philips), valid {fmt(a.valid_from)} to {fmt(a.valid_through)}"
    assert evidence(s, unnamed) == words and evidence(s, named) == words
    assert jobs_of(s)[unnamed.number][6] == "Not named"
    assert figures(s)["Attestation on file"] == 2 and figures(s)["No engineer named"] == 1 and s.gaps == []
    assert rows_of(s, "attestations") == [["Philips", "LTR-2026-07", "Philips IntelliVue MX750", a.received_on, a.valid_from, a.valid_through,
                                           today - 95 * DAY, "In force"]]
    # entered after the work: it still counts, and says since when
    entered(a, today)
    assert evidence(build(today), unnamed) == f"{words} (on file since {fmt(today)})"


def test_a_subcontracted_engineer_is_covered_by_their_companys_attestation(today, monitor):
    """A managed-service vendor sends the maker's engineer: the engineer's company's attestation counts, as the work order's vendor's
    would."""
    oem = engineer("Lee Park", "Philips")
    attestation("Philips", reference="PHL-ATT-1")
    wo = vendor_job(monitor, "Acme Managed Services", engineer=oem, completed_on=today - 3 * DAY)
    s = build(today)
    assert evidence(s, wo).startswith("Attestation PHL-ATT-1 on file (Philips)") and s.gaps == []
    assert jobs_of(s)[wo.number][5:7] == ["Acme Managed Services", "Lee Park (Philips)"]
    # the job counts under the work order's vendor; Philips's row holds its attestation
    assert vendor_row(s, "Acme Managed Services")["Attestation on file"] == 1
    assert vendor_row(s, "Philips")["Attestations in the period"].startswith("PHL-ATT-1, ")
    # without it, the same job has nothing on file, and the gap names the work order's vendor, never the engineer
    vs.remove_attestation(next(iter(vs.attestations_for("Philips"))))
    s = build(today)
    assert gap_texts(s) == ["Acme Managed Services: 1 job in the period with no training or attestation on file covering it"]
    assert all("Lee Park" not in g.text and "Lee Park" not in g.record for g in s.gaps)


def test_nothing_on_file_and_an_expired_training(today, monitor, monitor_model):
    dana = engineer()
    lapsed = training(dana, monitor_model, issued_on=today - 800 * DAY, expires_on=today - 50 * DAY)
    wo = vendor_job(monitor, engineer=dana, completed_on=today - 40 * DAY)
    s = build(today)
    assert evidence(s, wo) == f"No training or attestation on file; the engineer's training on this model expired on {fmt(lapsed.expires_on)}"
    assert figures(s)["Nothing on file"] == 1
    gap = gaps_of(s, GAP)[0]
    assert (gap.text, gap.url, gap.record) == ("Philips: 1 job in the period with no training or attestation on file covering it",
                                               "/contracts/vendors/?vendor=Philips", "Philips")
    assert rows_of(s, "engineers")[0][-1] == "Expired"


# --- each record by its own dates ------------------------------------------------------------------------------------------------


def test_a_record_entered_after_the_work_still_counts_and_says_since_when(today, monitor, monitor_model):
    dana = engineer()
    wo = vendor_job(monitor, engineer=dana, completed_on=today - 30 * DAY)
    assert figures(build(today))["Nothing on file"] == 1
    training(dana, monitor_model, issued_on=today - 200 * DAY)  # issued before the job, typed in today
    s = build(today)
    assert evidence(s, wo).endswith(f", no expiry (on file since {fmt(today)})") and s.gaps == []
    assert figures(s)["Evidence entered after the work"] == 1
    # a job done today with a record entered today: on file that day
    same_day = vendor_job(monitor, engineer=dana, completed_on=today)
    assert "on file since" not in evidence(build(today), same_day)


def test_a_renewal_is_a_new_record_and_covers_its_own_days(today, monitor, monitor_model):
    dana = engineer()
    training(dana, monitor_model, issued_on=today - 800 * DAY, expires_on=today - 50 * DAY)
    in_lapse = vendor_job(monitor, engineer=dana, completed_on=today - 40 * DAY)
    after = vendor_job(monitor, engineer=dana, completed_on=today - 20 * DAY)
    training(dana, monitor_model, issued_on=today - 30 * DAY, expires_on=today + 700 * DAY)  # renewed after the lapse
    s = build(today)
    assert evidence(s, in_lapse).startswith("No training or attestation on file; the engineer's training on this model expired")
    assert evidence(s, after).startswith("Training on file: Philips IntelliVue MX750")
    assert gap_texts(s) == ["Philips: 1 job in the period with no training or attestation on file covering it"]
    # both records listed, the newest first, each with its standing at the period's end
    assert [(r[6], r[-1]) for r in rows_of(s, "engineers")] == [(today - 30 * DAY, "In force"), (today - 800 * DAY, "Expired")]
    # a period ending in the lapse: the old record had expired, the renewal not issued yet (not listed)
    early = build(today, end=today - 35 * DAY)
    assert [(r[6], r[-1]) for r in rows_of(early, "engineers")] == [(today - 800 * DAY, "Expired")]
    assert set(jobs_of(early)) == {in_lapse.number}


def test_a_record_entered_in_error_covers_no_day(today, monitor, monitor_model):
    dana = engineer()
    t = training(dana, monitor_model)
    a = attestation(VENDOR, reference="LTR-ERR")
    wo = vendor_job(monitor, engineer=dana, completed_on=today - 5 * DAY)
    assert evidence(build(today), wo).startswith("Training on file")
    vs.remove_training(t)
    assert evidence(build(today), wo).startswith("Attestation LTR-ERR on file")
    vs.remove_attestation(a)
    s = build(today)
    assert evidence(s, wo) == "No training or attestation on file" and len(gaps_of(s, GAP)) == 1
    assert rows_of(s, "attestations") == [] and rows_of(s, "engineers")[0][3] == vendors.NO_TRAINING


def test_an_attestation_counts_on_the_days_it_states(today, monitor):
    attestation(VENDOR, reference="LTR-2026", valid_from=today - 60 * DAY, valid_through=today - 10 * DAY, received_on=today - 60 * DAY)
    before, during, after = (vendor_job(monitor, completed_on=today - n * DAY) for n in (70, 30, 5))
    s = build(today)
    assert [evidence(s, wo).split(" ")[0] for wo in (before, during, after)] == ["No", "Attestation", "No"]
    assert gap_texts(s) == ["Philips: 2 jobs in the period with no training or attestation on file covering them"]
    assert rows_of(s, "attestations")[0][-1] == "Ended"  # at the period's end


# --- vendors, the gap and the check, imported jobs ----------------------------------------------------------------------------------


def test_vendors_merge_by_key_with_their_contracts(today, monitor, vent, monitor_model):
    contract = Contract.objects.create(reference="SC-2026-118", vendor="Philips", type=ContractType.OEM, coverage=Coverage.FULL,
                                       start_on=today - 300 * DAY, end_on=today + 65 * DAY)
    Asset.objects.filter(pk=monitor.pk).update(contract=contract)
    Contract.objects.create(reference="SC-2019-001", vendor="Philips", start_on=today - 2000 * DAY, end_on=today - 1000 * DAY)  # long over
    Contract.objects.create(reference="SC-2026-200", vendor="TechCare Biomedical Services", type=ContractType.THIRD_PARTY,
                            coverage=Coverage.TM, start_on=today - 30 * DAY, end_on=today + 300 * DAY)
    grace = engineer("Grace Lindqvist", "PHILIPS")
    training(grace, monitor_model)
    vendor_job(monitor, "Philips field service", engineer=grace, completed_on=today - 9 * DAY)
    vendor_job(monitor, "philips  FIELD service", completed_on=today - 8 * DAY)
    s = build(today)
    assert [r[0] for r in rows_of(s, "vendors")] == ["Philips", "TechCare Biomedical Services"]
    philips = vendor_row(s, "Philips")
    assert philips["Also written"] == "PHILIPS, philips  FIELD service, Philips field service"
    assert philips["Contracts in the period"] == (f"SC-2026-118: OEM, Full service, {fmt(today - 300 * DAY)} to {fmt(today + 65 * DAY)}, "
                                                  "1 device covered")
    assert (philips["Engineers with training on file"], philips["Vendor jobs"], philips["Training on file"], philips["Nothing on file"]) == (1, 2, 1, 1)
    assert vendor_row(s, "TechCare Biomedical Services")["Contracts in the period"].startswith("SC-2026-200: Third-party, Time and materials, ")
    assert gap_texts(s) == ["Philips: 1 job in the period with no training or attestation on file covering it"]
    assert figures(s)["Vendors"] == 2 and figures(s)["Contracts in the period"] == 2
    # a letter under the contract's other spelling and the field service name covers the field service jobs; each name its own row
    attestation("Philips Healthcare", "Philips field service", reference="PHL-HC-1")
    s = build(today)
    assert s.gaps == [] and [r[0] for r in rows_of(s, "vendors")] == ["Philips", "Philips Healthcare", "TechCare Biomedical Services"]
    assert vendor_row(s, "Philips")["Attestation on file"] == 1 and vendor_row(s, "Philips Healthcare")["Vendor jobs"] == 0
    assert vendor_row(s, "Philips")["Also written"] == "PHILIPS, philips  FIELD service, Philips field service"
    # a retired device is not covered
    Asset.objects.filter(pk=monitor.pk).update(status=AssetStatus.RETIRED)
    assert vendor_row(build(today), "Philips")["Contracts in the period"].endswith("0 devices covered")


def test_a_check_for_manufacturer_schedule_jobs_naming_no_engineer(today, carm, ct, monitor):
    attestation("Siemens Healthineers", "Philips", reference="LTR-ALL")  # covered: the check is about the record, not the evidence
    one = vendor_job(carm, "Siemens Healthineers", completed_on=today - 9 * DAY)
    vendor_job(carm, "Siemens Healthineers", engineer=engineer("Ana Ruiz", "Siemens Healthineers"), completed_on=today - 8 * DAY)
    vendor_job(ct, completed_on=today - 7 * DAY)
    vendor_job(ct, completed_on=today - 6 * DAY)
    vendor_job(monitor, completed_on=today - 5 * DAY)  # not on the manufacturer's schedule: never a check
    s = build(today)
    assert gaps_of(s, GAP) == []
    checks = gaps_of(s, CHECK)
    assert [(g.text, g.url, g.record) for g in checks] == [
        ("Philips: 2 jobs on imaging, radiologic, or laser equipment kept on the manufacturer's schedule name no engineer: record who did the "
         "work as the vendor's reports name them (the vendor jobs table lists them).", "", "Philips"),
        (f"Siemens Healthineers: {one.number}, a job on imaging, radiologic, or laser equipment kept on the manufacturer's schedule, names no "
         "engineer: record who did the work as the vendor's report names them.", f"/work-orders/{one.number}/", one.number)]
    # naming the engineer clears it
    set_vendor_engineer(one, engineer("Ben Ortiz", "Siemens Healthineers"))
    assert [g.record for g in gaps_of(build(today), CHECK)] == ["Philips"]


def test_imported_jobs_are_counted_apart_never_judged(today, monitor, carm):
    imported = vendor_job(monitor, completed_on=today - 30 * DAY)
    on_carm = vendor_job(carm, "Siemens Healthineers", completed_on=today - 20 * DAY)
    WorkOrder.objects.filter(pk__in=[imported.pk, on_carm.pk]).update(source=Source.IMPORTED)
    s = build(today)
    assert s.gaps == []  # neither a gap nor a check
    f = figures(s)
    assert (f["Vendor jobs completed"], f["Imported from the previous system"], f["Nothing on file"]) == (0, 2, 0)
    assert evidence(s, imported) == evidence(s, on_carm) == "Imported from the previous system: not judged"
    assert (vendor_row(s, "Philips")["Imported"], vendor_row(s, "Philips")["Vendor jobs"]) == (1, 0)
    assert any("Jobs imported from the previous system (2 in the period)" in n for n in s.notes)


def test_any_type_of_vendor_job_and_only_completed_ones_in_the_period(today, vent, vent_model):
    dana = engineer("Dana Kim", "Hamilton Medical")
    training(dana, vent_model)
    pm = vendor_job(vent, "Hamilton Medical", engineer=dana, type_=WoType.PM, pm_result=PmResult.PASS, completed_on=today - 3 * DAY)
    old = vendor_job(vent, "Hamilton Medical", completed_on=today - 300 * DAY)  # before the period
    open_wo = create_work_order(asset=vent, type=WoType.REPAIR, priority=Priority.NORMAL, problem=SECRET, opened_on=today - DAY)
    assign(open_wo, vendor_name="Hamilton Medical")
    in_house = vendor_job(vent, "Hamilton Medical", completed_on=today - 2 * DAY)
    WorkOrder.objects.filter(pk=in_house.pk).update(vendor_service=False, vendor_name="")
    s = build(today)
    assert set(jobs_of(s)) == {pm.number} and jobs_of(s)[pm.number][1] == "Preventive maintenance"
    assert old.number not in jobs_of(s) and open_wo.number not in jobs_of(s)


def test_an_empty_period(today):
    s = build(today)
    assert s.gaps == [] and all(rows_of(s, t.key) == [] for t in s.tables)
    assert figures(s)["Vendor jobs completed"] == 0 and s.key == "vendors" and s.title == "Contracted service and vendor staff"
    assert s.topic == ("The evidence the facility keeps that the vendor staff who serviced its equipment were trained for it, and the "
                       "contracted services.")
    assert s.table("jobs").links == {0: WORK_ORDER, 3: DEVICE}
    joined = " ".join(s.notes)
    assert "day it was completed" in joined and "on file since" in joined and "never judges competence" in joined


# --- what it never shows, its cost, and another facility ---------------------------------------------------------------------------


def test_nothing_a_requester_typed_and_a_fixed_number_of_queries(today, monitor, carm, monitor_model):
    vendor_engineers = [engineer(f"Engineer {chr(65 + i)}", VENDOR) for i in range(3)]
    for i, e in enumerate(vendor_engineers):
        training(e, monitor_model, issued_on=today - (300 + i) * DAY)
    attestation("Siemens Healthineers", covers=Covers.MODELS, model_ids=[str(carm.device_model_id)])

    def read() -> int:
        with CaptureQueriesContext(connection) as q:
            s = build(today)
            rows = [r for t in s.tables for r in rows_of(s, t.key)]
        text = " ".join([*(g.text + g.record for g in s.gaps), *(str(f.value) + f.hint for f in s.figures), *s.notes,
                         *(str(c) for r in rows for c in r)])
        assert SECRET not in text
        return len(q)

    def jobs(n):
        for i in range(n):
            asset = carm if i % 3 == 0 else monitor
            secret(vendor_job(asset, VENDOR if asset == monitor else "Siemens Healthineers",
                              engineer=vendor_engineers[i % 3] if i % 2 else None, completed_on=today - (i % 90 + 1) * DAY))

    jobs(5)
    small = read()
    jobs(45)
    WorkOrder.objects.filter(pk__in=WorkOrder.objects.order_by("number").values("pk")[:4]).update(source=Source.IMPORTED)
    assert read() == small <= 6


def test_another_facilitys_records_stay_there(today, ctx, monitor, other_tenant):
    with tenant_context(other_tenant):
        dept = Department.objects.create(name="OR")
        model = DeviceModel.objects.create(manufacturer="Philips", model="IntelliVue MX750", description="Monitor", category="Patient monitoring",
                                           risk_class=RiskClass.HIGH)
        theirs = Asset.objects.create(tag="OT-1", device_model=model, department=dept)
        e = engineer("Pat Lee", VENDOR)
        training(e, model)
        attestation(VENDOR, reference="THEIRS-1")
        vendor_job(theirs, engineer=e, completed_on=timezone.localdate() - 5 * DAY)
        Contract.objects.create(reference="OT-SC-1", vendor=VENDOR, start_on=today - 100 * DAY, end_on=today + 100 * DAY)
        s = build(timezone.localdate())
        assert figures(s)["Training on file"] == 1 and len(rows_of(s, "vendors")) == 1
    wo = vendor_job(monitor, completed_on=today - 5 * DAY)
    s = build(today)
    assert set(jobs_of(s)) == {wo.number} and evidence(s, wo) == "No training or attestation on file"
    assert rows_of(s, "engineers") == [] and rows_of(s, "attestations") == []
    assert vendor_row(s, "Philips")["Contracts in the period"] == ""


# --- its place, access, the cover, and the staff note -------------------------------------------------------------------------------


def test_the_section_follows_staff_and_needs_work_orders_contracts_and_equipment_view(ctx, make_user):
    keys = survey.section_keys()
    assert keys[-2:] == ["staff", "vendors"] and survey.section_title("vendors") == "Contracted service and vendor staff"
    assert SURVEY_NEEDS["vendors"] == ((Module.WORKORDERS, Level.VIEW), (Module.CONTRACTS, Level.VIEW), (Module.EQUIPMENT, Level.VIEW))
    for slug in ("director", "manager", "technician", "analyst"):
        assert survey_refusal(make_user(slug), "vendors") == "", slug
    assert survey_refusal(make_user("vendor"), "vendors").startswith("Needs Reports View")
    full = {Module.REPORTS: Level.VIEW, Module.WORKORDERS: Level.VIEW, Module.CONTRACTS: Level.VIEW, Module.EQUIPMENT: Level.VIEW}
    for module in (Module.WORKORDERS, Module.CONTRACTS, Module.EQUIPMENT):
        role = Role.objects.create(name=f"Without {module}", slug=f"without-{module}")
        role.set_levels({**full, module: Level.NONE})
        user = User.objects.create_user(username=f"{module}@riverside.example", password="Test-Pass-2026-x", tenant=ctx, role=role)
        assert survey_refusal(user, "vendors") == f"Needs {Module(module).label} View, which your role does not have.", module
        assert "vendors" in [lo.key for lo in survey.binder(user, survey.default_period(timezone.localdate())).left_out]


def test_a_gap_links_to_the_vendor_staff_tab_for_contracts_view(today, ctx, monitor, make_user):
    vendor_job(monitor, completed_on=today - 5 * DAY)
    gap = build(today).gaps[0]
    assert survey.gap_links(make_user("analyst"))(gap) == "/contracts/vendors/?vendor=Philips"
    role = Role.objects.create(name="No contracts", slug="no-contracts")
    role.set_levels({Module.REPORTS: Level.VIEW})
    user = User.objects.create_user(username="nc@riverside.example", password="Test-Pass-2026-x", tenant=ctx, role=role)
    assert survey.gap_links(user)(gap) == ""


def test_the_cover_names_what_is_still_not_covered():
    assert "The competence of contracted vendor staff" not in NOT_COVERED
    assert ("Vendor staff's badges, immunizations, and background checks (the facility's vendor credentialing system), and the vendors' own "
            "training records and competence assessments behind their statements") in NOT_COVERED
    assert "The yearly evaluation of each contracted service, and contracted services outside clinical engineering" in NOT_COVERED


def test_the_staff_section_points_vendor_service_here(today, vent):
    vendor_job(vent, "Hamilton Medical", completed_on=today - 5 * DAY)
    s = staff.build(period(today - 200 * DAY, today, today), None)
    note = next(n for n in s.notes if n.startswith("Left out"))
    assert "vendor service (1), which the Contracted service and vendor staff section covers" in note
    assert "keeps no record of a vendor" not in note


def test_the_page_the_csv_and_the_api_serve_it(client, today, monitor, make_user):
    dana = engineer()
    wo = vendor_job(monitor, engineer=dana, completed_on=today - 5 * DAY)
    client.force_login(make_user("director"))
    body = client.get("/reports/survey/").content.decode()
    assert 'id="sv-vendors"' in body and "Contracted service and vendor staff" in body and "/contracts/vendors/?vendor=Philips" in body
    rows = csv_rows(client.get("/reports/survey/vendors/jobs.csv"))
    assert rows[0] == vendors.JOB_COLUMNS and rows[1][0] == wo.number and rows[1][6:] == ["Dana Kim (Philips)", "", "No training or attestation on file"]
    for key in ("vendors", "engineers", "attestations"):
        assert client.get(f"/reports/survey/vendors/{key}.csv").status_code == 200, key
    data = client.get("/api/v1/survey/vendors/").json()
    assert data["key"] == "vendors" and [g["url"] for g in data["gaps"]] == ["/contracts/vendors/?vendor=Philips&facility=riverside"]
    print_body = client.get("/print/survey/").content.decode()
    assert 'id="sv-vendors"' in print_body and SECRET not in print_body


# --- the demo ---------------------------------------------------------------------------------------------------------------------


def test_the_demo_shows_one_gap_for_philips(db):
    call_command("seed_demo", stdout=StringIO())
    with tenant_context(Tenant.objects.get(slug="riverside")):
        today = timezone.localdate()
        s = vendors.build(survey.default_period(today), None)
        assert [(g.kind, g.text, g.url, g.record) for g in s.gaps] == [
            (GAP, "Philips: 1 job in the period with no training or attestation on file covering it", "/contracts/vendors/?vendor=Philips",
             "Philips")]
        (job,) = rows_of(s, "jobs")
        repair = WorkOrder.objects.get(checks_reference=seed_demo.VENDOR_REPORT_NUMBER)
        expired = today - timedelta(days=seed_demo.TRAINING_EXPIRED_DAYS_AGO)
        assert job[0] == repair.number and job[5:] == ["Philips", f"{seed_demo.VENDOR_REPAIR_ENGINEER} (Philips)", seed_demo.VENDOR_REPORT_NUMBER,
                                                       f"No training or attestation on file; the engineer's training on this model expired on {fmt(expired)}"]
        assert repair.vendor_engineer.name == seed_demo.VENDOR_REPAIR_ENGINEER
        philips, techcare = vendor_row(s, "Philips"), vendor_row(s, seed_demo.TECHCARE)
        assert (philips["Attestations in the period"], philips["Engineers with training on file"], philips["Nothing on file"]) == ("", 1, 1)
        assert techcare["Attestations in the period"].startswith(seed_demo.VENDOR_ATTESTATION_REFERENCE)
        assert (techcare["Engineers with training on file"], techcare["Vendor jobs"]) == (1, 0)
        assert [(r[0], r[1], r[-1]) for r in rows_of(s, "engineers")] == [
            ("Philips", "Grace Lindqvist", "In force"), ("Philips", "Owen Marsh", "Expired"), (seed_demo.TECHCARE, "Luis Ortega", "In force")]
        assert [r[1] for r in rows_of(s, "attestations")] == [seed_demo.VENDOR_ATTESTATION_REFERENCE]
        assert len(rows_of(s, "vendors")) == 6  # the six contracts' vendors (the C-arm's job is still open: no check)


# --- under the policies -----------------------------------------------------------------------------------------------------------


@needs_postgres
def test_the_section_under_the_policies(client, today, monitor, carm, monitor_model, make_user):
    dana = engineer()
    training(dana, monitor_model)
    covered = vendor_job(monitor, engineer=dana, completed_on=today - 9 * DAY)
    bare = vendor_job(carm, "Siemens Healthineers", completed_on=today - 5 * DAY)
    attestation("TechCare Biomedical Services", reference="TC-1")
    client.force_login(make_user("director"))
    as_app_role()
    s = build(today)
    assert evidence(s, covered).startswith("Training on file") and evidence(s, bare) == "No training or attestation on file"
    assert [(g.kind, g.record) for g in s.gaps] == [(GAP, "Siemens Healthineers"), (CHECK, bare.number)]
    assert [r[1] for r in rows_of(s, "attestations")] == ["TC-1"]
    data = client.get("/api/v1/survey/vendors/").json()
    assert data["key"] == "vendors" and [g["record"] for g in data["gaps"]] == ["Siemens Healthineers", bare.number]
    assert client.get("/reports/survey/vendors/jobs.csv").status_code == 200
