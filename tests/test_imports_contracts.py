"""
The service contracts import (slice 23, apps/imports/kinds/contracts.py): a file of contracts or of covered devices (the reference
repeating once per device), contracts found by reference in any letter case and changed through update_contract (a blank keeps),
new ones through create_contract (vendor, start, end needed), devices put on through add_asset (a tag not here or a retired device
noted, a device on a contract that ends later left there, otherwise moved), rows that disagree noted, the same device twice a file
problem, values it cannot read noted rather than defaulted silently, and a re-run that changes nothing.
"""
from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied
from pg_helpers import as_app_role, needs_postgres

from apps.contracts import services as ct
from apps.contracts.models import Contract, ContractType, Coverage
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel, SupportType
from apps.imports import base, kinds, services
from apps.imports.models import ImportRun
from apps.tenants.context import tenant_context

HEADER = "Contract #,Vendor,Contract type,Coverage,Start date,End date,Annual cost,Asset tag"
JAN1, DEC31 = date(2026, 1, 1), date(2026, 12, 31)


def csv_bytes(*lines):
    return ("\r\n".join(lines) + "\r\n").encode()


def run_through(run, user):
    run = services.process(run, user)
    while run.status in (ImportRun.Status.CHECKING, ImportRun.Status.IMPORTING):
        run = services.process(run, user)
    return run


def checked(user, *lines):
    run = services.upload(user, "contracts", "contracts.csv", csv_bytes(*lines))
    return run_through(services.confirm_columns(run, user, services.mapping_of(run)), user)


def imported(user, *lines):
    return run_through(services.start_import(checked(user, *lines), user), user)


def notes(run) -> dict:
    return {line: (outcome, row_notes) for line, key, outcome, row_notes in run.results}


def contract(reference="SC-1", **fields):
    values = {"vendor": "Acme", "type": ContractType.OEM, "coverage": Coverage.FULL, "start_on": JAN1, "end_on": DEC31, "annual_cost": 1000}
    return ct.create_contract(reference=reference, **{**values, **fields})


@pytest.fixture
def device(ctx, dept, pump_model):
    def _make(tag, **fields):
        return Asset.objects.create(tag=tag, device_model=pump_model, department=dept, acquisition_cost=3200, **fields)

    return _make


def test_a_link_file_adds_contracts_and_puts_their_devices_on_them(ctx, make_user, vent, pump, device):
    kim, third = make_user("director"), device("CE-10003")
    lines = (HEADER,
             'SC-1,Hamilton Medical,OEM,Full service,2026-01-01,2026-12-31,"$12,000.00",ce-10001',
             "SC-2,BD,Third party,Parts & labor,01/01/2026,12/31/2026,4000,CE-10002",
             "sc-2,,,,,,,CE-10003")  # only a reference and a tag: the contract an earlier line adds
    run = checked(kim, *lines)
    assert run.status == "checked" and run.counts == {"create": 2, "update": 1} and run.results == []
    assert not Contract.objects.exists() and Asset.objects.get(pk=vent.pk).contract is None  # the check changed nothing
    assert run.summary == {"created": {"Contracts": ["SC-1", "SC-2"]}, "totals": {"Devices": {"put on a contract": "3"}},
                           "values": {"Contract type": {"OEM": ["OEM", 1], "Third party": ["Third-party", 1]},
                                      "Coverage": {"Full service": ["Full service", 1], "Parts & labor": ["Parts and labor", 1]}}}
    run = run_through(services.start_import(run, kim), kim)
    assert run.status == "imported" and run.counts == {"create": 2, "update": 1}
    sc1, sc2 = Contract.objects.get(reference="SC-1"), Contract.objects.get(reference="SC-2")
    assert (sc1.vendor, sc1.type, sc1.coverage, sc1.start_on, sc1.end_on, sc1.annual_cost) == (
        "Hamilton Medical", ContractType.OEM, Coverage.FULL, JAN1, DEC31, Decimal("12000.00"))
    assert (sc2.type, sc2.coverage, sc2.annual_cost) == (ContractType.THIRD_PARTY, Coverage.PARTS_LABOR, Decimal("4000.00"))
    assert {a.tag: (a.contract, a.support_type) for a in Asset.objects.all()} == {
        "CE-10001": (sc1, SupportType.OEM_CONTRACT), "CE-10002": (sc2, SupportType.THIRD_PARTY), third.tag: (sc2, SupportType.THIRD_PARTY)}
    assert sc1.history.first().history_user == kim  # the importer is named in the contract's history
    again = imported(kim, *lines)  # a re-run is safe: nothing doubles, nothing changes
    assert again.counts == {"unchanged": 3} and Contract.objects.count() == 2 and sc1.history.count() == 1


def test_a_contract_here_is_found_in_any_letter_case_and_a_blank_keeps(ctx, make_user, vent):
    kim = make_user("director")
    here = contract("SC-2026-118", vendor="Hamilton", end_on=date(2025, 12, 31), start_on=date(2025, 1, 1), annual_cost=10000)
    here.add_assets([vent])
    run = imported(kim, "Reference,Vendor,Start date,End date,Annual cost,Asset tag", "sc-2026-118,,2026-01-01,2026-12-31,11000,CE-10001")
    here.refresh_from_db()
    assert run.counts == {"update": 1} and run.summary["totals"] == {"Changes": {"Start date": "1", "End date": "1", "Annual cost": "1"}}
    assert (here.reference, here.vendor, here.start_on, here.end_on, here.annual_cost) == ("SC-2026-118", "Hamilton", JAN1, DEC31, Decimal("11000.00"))
    assert here.history.first().history_user == kim and Contract.objects.count() == 1


def test_rows_that_cannot_make_a_contract_are_skipped_in_words(ctx, make_user, vent):
    kim = make_user("director")
    run = imported(kim, "Reference,Vendor,Start date,End date,Asset tag", "SC-9,,,,CE-10001", "SC-10,Acme,someday,2026-12-31,",
                   "SC-11,Acme,2026-01-01,12/31/2099,", "SC-12,Acme,2026-06-01,2026-01-01,", ",Acme,2026-01-01,2026-12-31,")
    assert run.counts == {"skip": 5} and not Contract.objects.exists() and Asset.objects.get(pk=vent.pk).contract is None
    assert {line: n[0] for line, (_, n) in notes(run).items()} == {
        2: "No contract with this reference here; a new one needs Vendor, Start date and End date",
        3: "Start date not read: a new contract needs one",
        4: "End date reads as no date: a new contract needs one",
        5: "The end date must be on or after the start date.",
        6: "Contract reference is blank",
    }


def test_values_it_cannot_read_are_noted_never_defaulted_silently(ctx, make_user):
    kim = make_user("director")
    here = contract("SC-1", type=ContractType.THIRD_PARTY, coverage=Coverage.PARTS, annual_cost=5000)
    run = imported(kim, HEADER, "SC-1,,Bartered,Gold,soon,later,lots,", "SC-2,Acme,Bartered,Gold,2026-01-01,2026-12-31,lots,")
    assert run.counts == {"unchanged": 1, "create": 1}
    assert notes(run) == {
        2: ("unchanged", ["Contract type not read: left as it was", "Coverage not read: left as it was", "Start date not read: left as it was",
                          "End date not read: left as it was", "Annual cost not read: left as it was"]),
        3: ("create", ["Contract type not read: OEM used", "Coverage not read: Full service used", "Annual cost not read: 0 used"]),
    }
    here.refresh_from_db()
    assert (here.type, here.coverage, here.start_on, here.end_on, here.annual_cost) == (
        ContractType.THIRD_PARTY, Coverage.PARTS, JAN1, DEC31, Decimal("5000.00"))
    new = Contract.objects.get(reference="SC-2")
    assert (new.type, new.coverage, new.annual_cost) == (ContractType.OEM, Coverage.FULL, Decimal("0.00"))


def test_devices_move_unless_another_contract_ends_later(ctx, make_user, device):
    kim = make_user("director")
    old, later = contract("OLD", end_on=date(2026, 6, 30)), contract("LATER", end_on=date(2028, 12, 31))
    a1, a2, a3, a4 = device("A1"), device("A2", contract=old), device("A3", contract=later), device("A4", status=AssetStatus.RETIRED)
    a5 = device("A5", contract=contract("SAME", end_on=date(2027, 12, 31)))  # ends the same day: it moves
    tags = ("A1", "A2", "A3", "A4", "CE-404", "A5")
    run = imported(kim, "Reference,Vendor,Start date,End date,Asset tag", *(f"NEW,Acme,2026-01-01,2027-12-31,{tag}" for tag in tags))
    assert run.counts == {"create": 1, "update": 2, "unchanged": 3}
    assert notes(run) == {4: ("unchanged", ["The device is on another contract that ends later: left there"]),
                          5: ("unchanged", ["The device is retired: not put on the contract"]),
                          6: ("unchanged", ["No device with this asset tag here: the contract is imported without it"])}
    assert run.summary["totals"] == {"Devices": {"put on a contract": "1", "moved from another contract": "2"}}
    new = Contract.objects.get(reference="NEW")
    assert {a.tag: a.contract for a in Asset.objects.filter(pk__in=[a1.pk, a2.pk, a3.pk, a4.pk, a5.pk])} == {
        "A1": new, "A2": new, "A3": later, "A4": None, "A5": new}


def test_an_import_finds_a_contract_an_earlier_chunk_added(ctx, make_user, monkeypatch, device):
    kim = make_user("director")
    monkeypatch.setattr(kinds.get("contracts"), "chunk", 2, raising=False)
    for tag in ("A1", "A2", "A3", "A4"):
        device(tag)
    run = checked(kim, "Reference,Vendor,Start date,End date,Asset tag", "SC-1,Acme,2026-01-01,2026-12-31,A1", "SC-1,Acme,2026-01-01,2026-12-31,A2",
                  "SC-1,,,,A3", "sc-1,,,,A4")
    run = run_through(services.start_import(run, kim), kim)  # the second chunk's lines find SC-1, committed with the first
    assert run.counts == {"create": 1, "update": 3} and run.results == []
    assert Contract.objects.get().assets.count() == 4


def test_rows_of_one_contract_that_disagree_are_noted_and_the_last_stays(ctx, make_user, vent, pump):
    kim = make_user("director")
    run = imported(kim, "Reference,Vendor,Start date,End date,Annual cost,Asset tag", "SC-1,Acme,2026-01-01,2026-12-31,1000,CE-10001",
                   "SC-1,Acme,2026-01-01,2026-12-31,2000,CE-10002")
    assert run.counts == {"create": 1, "update": 1}
    assert notes(run) == {3: ("update", ["Annual cost differs from an earlier line of this contract: this line's value is used"])}
    assert Contract.objects.get().annual_cost == Decimal("2000.00") and run.summary["totals"]["Changes"] == {"Annual cost": "1"}


def test_the_same_device_twice_under_one_contract_is_a_problem_of_the_file(ctx, make_user):
    kim = make_user("director")
    run = services.upload(kim, "contracts", "c.csv", csv_bytes("Reference,Vendor,Start date,End date,Asset tag", "SC-1,Acme,2026-01-01,2026-12-31,CE-10001",
                                                                "sc-1,,,,ce-10001", "SC-2,Acme,2026-01-01,2026-12-31,", "SC-2,Acme,2026-01-01,2026-12-31,",
                                                                "SC-2,,,,CE-10001"))
    run = services.confirm_columns(run, kim, services.mapping_of(run))
    assert run.file_problems == {"1": "Asset tag ce-10001 is also on line 2 for this contract: one row per covered device",
                                 "3": "This contract is also on line 4 without a device: one row per contract, or per covered device"}


def test_contracts_and_devices_stay_in_their_facility(ctx, make_user, other_tenant):
    kim = make_user("director")
    with tenant_context(other_tenant):
        model = DeviceModel.objects.create(manufacturer="BD", model="Alaris", description="Pump", category="Infusion pumps")
        theirs = Asset.objects.create(tag="CE-77", device_model=model, department=Department.objects.create(name="ICU"))
        their_contract = contract("SC-1", vendor="Theirs")
    run = imported(kim, "Reference,Vendor,Start date,End date,Asset tag", "SC-1,Ours,2026-01-01,2026-12-31,CE-77")
    assert run.counts == {"create": 1} and notes(run)[2][1] == ["No device with this asset tag here: the contract is imported without it"]
    assert Contract.objects.get().vendor == "Ours"
    # unscoped: reading the other facility's rows in a test
    assert Contract.unscoped.get(pk=their_contract.pk).vendor == "Theirs" and Asset.unscoped.get(pk=theirs.pk).contract_id is None


def test_importing_contracts_needs_contracts_edit(ctx, make_user):
    data = csv_bytes("Reference", "SC-1")
    with pytest.raises(PermissionDenied):
        services.upload(make_user("technician"), "contracts", "c.csv", data)  # Contracts View only
    assert services.upload(make_user("manager"), "contracts", "c.csv", data).status == "mapping"


def test_the_command_line_imports_with_no_user(ctx, vent):
    run = imported(None, "Reference,Vendor,Start date,End date,Asset tag", "SC-1,Acme,2026-01-01,2026-12-31,CE-10001")
    assert run.counts == {"create": 1} and Asset.objects.get(pk=vent.pk).contract.reference == "SC-1"


def test_a_value_too_long_for_its_column_skips_the_row(ctx, make_user):
    run = imported(make_user("director"), "Reference,Vendor,Start date,End date", f"{'R' * 61},Acme,2026-01-01,2026-12-31",
                   f"SC-2,{'v' * 121},2026-01-01,2026-12-31", "SC-3,Acme,2026-01-01,2026-12-31")
    assert run.counts == {"skip": 2, "create": 1}
    assert [n for _, n in notes(run).values()] == [["Contract reference is longer than 60 characters"], ["Vendor is longer than 120 characters"]]


def test_columns_are_matched_by_the_names_exports_give_them():
    importer = kinds.get("contracts")
    header = ["Contract No.", "Service Vendor", "Total value", "Yearly Cost", "Expiration Date", "Effective Date", "ID", "Control #", "Notes", "Comments"]
    assert base.auto_map(importer.columns, header) == {"reference": 0, "vendor": 1, "annual_cost": 3, "end": 4, "start": 5, "tag": 7}


# --- under the policies --------------------------------------------------------------------------------------------------------------

@needs_postgres
def test_a_contracts_import_under_the_policies(ctx, make_user, vent, pump):
    kim = make_user("director")
    later = contract("LATER", end_on=date(2028, 12, 31))
    later.add_assets([pump])
    as_app_role()
    lines = ("Reference,Vendor,Start date,End date,Annual cost,Asset tag", "SC-1,Acme,2026-01-01,2026-12-31,1000,CE-10001",
             "SC-1,,,,,CE-10002", f"SC-2,{'v' * 121},2026-01-01,2026-12-31,1000,")
    run = checked(kim, *lines)
    assert run.counts == {"create": 1, "unchanged": 1, "skip": 1} and not Contract.objects.filter(reference="SC-1").exists()
    run = run_through(services.start_import(run, kim), kim)  # the long vendor never reaches PostgreSQL, which would refuse it
    assert run.counts == {"create": 1, "unchanged": 1, "skip": 1} and notes(run)[4] == ("skip", ["Vendor is longer than 120 characters"])
    assert Asset.objects.get(pk=vent.pk).contract.reference == "SC-1" and Asset.objects.get(pk=pump.pk).contract == later
