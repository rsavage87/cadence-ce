"""
The import framework (slice 23 scaffold, apps/imports): reading files (encodings, workbooks, NUL, separators, sizes), matching
columns, the steps of a run (columns, a check that changes nothing, an import committed chunk by chunk, one import per facility,
discard, expiry), a bad row never stopping the file, permissions per kind, and the parsers that never turn an unreadable value into
a default. A fake importer (departments by name) stands in for the real kinds.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.utils import timezone
from pg_helpers import as_app_role, needs_postgres

from apps.accounts.models import Level, Module
from apps.core.csvtext import guard, unguard
from apps.equipment.models import Department
from apps.imports import base, kinds, parse, services
from apps.imports.models import ImportRun
from apps.imports.permissions import can_import, importable
from apps.tenants.context import tenant_context


class FakeImporter(base.Importer):
    kind = "fake"
    label = "Departments"
    module = Module.EQUIPMENT
    level = Level.EDIT
    key = "name"
    order = 99
    chunk = 3
    columns = [base.Column("name", "Department", ("department", "dept", "unit"), required=True, max_length=80, example="ICU"),
               base.Column("note", "Note", ("note", "comment"))]

    def apply(self, ctx, row, result):
        name = row["name"]
        if name == "skip-me":
            raise base.RowSkip("Skipped on purpose")
        if name == "invalid":
            raise ValidationError("Not a department")
        if name == "denied":
            raise PermissionDenied("Needs Equipment Approve")
        if name == "db-error":
            with connection.cursor() as cur:
                cur.execute("SELECT * FROM no_such_table_at_all")
        if row.get("note"):
            result.warn("Note ignored")
        existing = Department.objects.filter(name__iexact=name).first()
        if existing:
            return
        Department.objects.create(name=name)
        result.outcome = result.CREATE
        ctx.total("Departments", "added")
        ctx.add_created("Departments", name)
        ctx.read_as("Note", row.get("note") or "", "kept")


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setitem(kinds.KINDS, "fake", FakeImporter())
    return kinds.KINDS["fake"]


def csv_bytes(*lines, encoding="utf-8"):
    return ("\r\n".join(lines) + "\r\n").encode(encoding)


def run_through(run, user, *, importing=False):
    run = services.process(run, user)
    while run.status in (ImportRun.Status.CHECKING, ImportRun.Status.IMPORTING):
        run = services.process(run, user)
    return run


# --- reading files --------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("data, message", [
    (b"PK\x03\x04rest-of-a-zip", "Excel workbook"),
    (b"\xd0\xcf\x11\xe0old-excel", "Excel workbook"),
    (b"Department\r\nI\x00CU\r\n", "NUL"),
])
def test_files_that_are_not_csv_are_refused_in_words(data, message):
    with pytest.raises(ValidationError) as e:
        base.decode(data)
    assert message in " ".join(e.value.messages)


def test_encodings_and_separators_are_read():
    for encoding in ("utf-8-sig", "utf-16", "utf-32", "cp1252"):
        text = base.decode(csv_bytes("Department;Note", "Médecine;café", encoding=encoding))
        header, rows = base.read_table(text)
        assert header == ["Department", "Note"] and rows == [["Médecine", "café"]], encoding
    header, rows = base.read_table("Department\tNote\n\nICU\n\t\nOR\tx\textra\n")
    assert rows == [["ICU", ""], ["OR", "x"]]  # blank rows left out, short ones padded, long ones cut


def test_sizes_and_shapes_are_refused():
    with pytest.raises(ValidationError, match="more than 2 rows"):
        base.read_table("Department\na\nb\nc\n", max_rows=2)
    with pytest.raises(ValidationError, match="no header"):
        base.read_table("\n\n")
    with pytest.raises(ValidationError, match="no rows"):
        base.read_table("Department\n")
    with pytest.raises(ValidationError, match="longer than"):
        base.read_table("Department\n" + "x" * (base.MAX_CELL + 1) + "\n")


def test_columns_match_by_alias_whatever_the_spelling(fake):
    assert base.auto_map(fake.columns, ["ID", " DEPT. ", "Comment:"]) == {"name": 1, "note": 2}
    assert base.duplicate_headers(["Dept", "dept", "Note"]) == ["Dept", "dept"]
    assert base.samples([["a"], ["a"], ["b"], [""], ["c"], ["d"]], 0) == ["a", "b", "c"]
    for importer in kinds.KINDS.values():  # every template maps itself
        header, example = importer.template()
        assert base.auto_map(importer.columns, header) == {c.key: i for i, c in enumerate(importer.columns)}, importer.kind


def test_cadences_own_formula_guard_comes_off():
    for value in ("-12", "=SUM(A1)", "@x", "plain", "a;=b", "Pump alarm;=WEBSERVICE(x)"):
        assert unguard(guard(value)) == value
    assert base.clean("  '-12 ") == "-12"


# --- the steps of a run -----------------------------------------------------------------------------------------------------------

def test_a_check_changes_nothing_and_the_import_does_what_it_said(ctx, fake, make_user):
    kim = make_user("director")
    data = csv_bytes("Dept,Comment", "ICU,", "OR,late", "skip-me,", "invalid,", "denied,", "db-error,", "icu,", "PACU,")
    run = services.upload(kim, "fake", "depts.csv", data)
    assert run.status == "mapping" and run.row_count == 8
    view = services.mapping_view(run)
    assert [(c["column"].key, c["index"]) for c in view["columns"]] == [("name", 0), ("note", 1)] and view["unused"] == []
    run = services.confirm_columns(run, kim, {"name": 0, "note": 1})
    assert run.status == "checking" and run.file_problems == {"6": "Department icu is also on line 2: one row per record"}
    run = run_through(run, kim)
    assert run.status == "checked" and not Department.objects.exists()  # the check rolled every chunk back
    checked = run.counts
    assert checked == {"create": 3, "skip": 5}
    groups = {g["note"]: g for g in services.problem_groups(run)}
    assert groups["Skipped on purpose"]["lines"] == [4] and groups["Not a department"]["skipped"]
    assert groups["Needs Equipment Approve"]["count"] == 1 and "could not be saved" in " ".join(groups)
    assert groups["Note ignored"] == {"skipped": False, "note": "Note ignored", "count": 1, "lines": [3], "keys": ["OR"]}
    assert run.summary["created"] == {"Departments": ["ICU", "OR", "PACU"]} and run.summary["totals"] == {"Departments": {"added": "3"}}
    run = services.start_import(run, kim)
    run = run_through(run, kim)
    assert run.status == "imported" and run.counts == checked and run.rows == [] and run.imported_by == kim
    assert sorted(Department.objects.values_list("name", flat=True)) == ["ICU", "OR", "PACU"]


def test_an_import_carries_on_where_it_stopped(ctx, fake, make_user):
    kim = make_user("director")
    run = services.confirm_columns(services.upload(kim, "fake", "d.csv", csv_bytes("Dept", "A", "B", "C", "D", "E")), kim, {"name": 0})
    run = services.start_import(run_through(run, kim), kim)
    run = services.process(run, kim)  # one chunk of 3, then the tab is closed
    assert run.status == "importing" and run.offset == 3 and Department.objects.count() == 3
    assert services.progress(run) == 60
    run = run_through(run, kim)
    assert run.status == "imported" and Department.objects.count() == 5


def test_one_import_at_a_time_per_facility(ctx, fake, make_user):
    kim = make_user("director")
    first, second = (services.confirm_columns(services.upload(kim, "fake", "d.csv", csv_bytes("Dept", name)), kim, {"name": 0}) for name in "AB")
    first, second = run_through(first, kim), run_through(second, kim)
    services.start_import(first, kim)
    with pytest.raises(ValidationError, match=r"Another import \(departments, d.csv\) is running"):
        services.start_import(second, kim)
    with pytest.raises(ValidationError, match="already importing"):
        services.start_import(first, kim)


def test_the_columns_step_needs_the_required_ones_once_each(ctx, fake, make_user):
    kim = make_user("director")
    run = services.upload(kim, "fake", "d.csv", csv_bytes("Dept,Note", "ICU,x"))
    with pytest.raises(ValidationError, match="Department"):
        services.confirm_columns(run, kim, {"note": 1})
    with pytest.raises(ValidationError, match="more than once"):
        services.confirm_columns(run, kim, {"name": 0, "note": 0})
    with pytest.raises(ValidationError, match="Choose a column"):
        services.confirm_columns(run, kim, {"name": 9})
    run = services.confirm_columns(run, kim, {"name": 0, "note": None})
    assert run.columns == ["name"] and run.rows == [["ICU"]] and run.header == ["Dept"]  # the unused column is gone


def test_a_value_too_long_for_its_column_skips_the_row_before_any_service(ctx, fake, make_user):
    kim = make_user("director")
    run = services.confirm_columns(services.upload(kim, "fake", "d.csv", csv_bytes("Dept", "x" * 81, "ICU")), kim, {"name": 0})
    run = run_through(run, kim)
    assert run.counts == {"skip": 1, "create": 1} and run.results[0][3] == ["Department is longer than 80 characters"]


def test_discard_and_expiry_clear_the_rows(ctx, fake, make_user):
    kim = make_user("director")
    run = services.upload(kim, "fake", "d.csv", csv_bytes("Dept", "ICU"))
    run = services.discard(run, kim)
    assert run.status == "discarded" and run.rows == []
    with pytest.raises(ValidationError):
        services.discard(run, kim)
    stale = services.upload(kim, "fake", "d.csv", csv_bytes("Dept", "OR"))
    ImportRun.objects.filter(pk=stale.pk).update(updated_at=timezone.now() - timedelta(days=services.EXPIRE_DAYS + 1))
    assert services.expire_stale() == 1
    stale.refresh_from_db()
    assert stale.status == "expired" and stale.rows == []


def test_each_kind_needs_its_modules_level_and_scoped_users_none(ctx, fake, make_user):
    tech, director, analyst = make_user("technician"), make_user("director"), make_user("analyst")
    vendor = make_user("vendor")
    vendor.company = "Hamilton Medical"
    vendor.save()
    assert can_import(director, "work_orders") and not can_import(tech, "work_orders")  # Work orders Approve
    assert can_import(tech, "devices") and not can_import(analyst, "devices")  # Equipment Edit
    assert not can_import(vendor, "fake") and not can_import(director, "nope") and can_import(None, "devices")
    assert [i.kind for i in importable(director)][:4] == ["devices", "contracts", "technicians", "work_orders"]
    with pytest.raises(PermissionDenied):
        services.upload(analyst, "fake", "d.csv", csv_bytes("Dept", "ICU"))
    run = services.upload(director, "fake", "d.csv", csv_bytes("Dept", "ICU"))
    for step in (lambda: services.confirm_columns(run, analyst, {"name": 0}), lambda: services.process(run, analyst),
                 lambda: services.discard(run, analyst)):
        with pytest.raises(PermissionDenied):
            step()
    assert list(services.runs_for(analyst)) == [] and list(services.runs_for(director)) == [run]


def test_runs_stay_in_their_facility(ctx, fake, make_user, other_tenant):
    run = services.upload(make_user("director"), "fake", "d.csv", csv_bytes("Dept", "ICU"))
    with tenant_context(other_tenant):
        assert not ImportRun.objects.filter(pk=run.pk).exists()
    assert ImportRun.objects.filter(pk=run.pk).exists()


def test_an_upload_too_large_is_refused_before_it_is_read(client, settings, make_user):
    settings.UPLOAD_MAX_BYTES = 1000
    client.force_login(make_user("director"))
    r = client.post("/settings/", {"file": ("x" * 2000)}, format="multipart")  # any URL: the middleware answers first
    assert r.status_code == 413


# --- parsers ----------------------------------------------------------------------------------------------------------------------

def test_dates_in_the_shapes_exports_write():
    for value in ("2024-01-15", "01/15/2024", "1/15/24", "15-Jan-2024", "15-Jan-24", "Jan 15, 2024", "01/15/2024 00:00:00",
                  "1/15/2024 10:32 AM", "2024-01-15T08:00:00Z", "45306"):
        assert parse.parse_date(value) == date(2024, 1, 15), value
    assert parse.parse_date("") is None and parse.parse_date("Mar 2024", month_end=True) == date(2024, 3, 31)
    for placeholder in ("1/1/1900", "12/31/2099", "9999-12-31"):
        with pytest.raises(parse.Placeholder):
            parse.parse_date(placeholder)
    with pytest.raises(parse.Unreadable):
        parse.parse_date("next Tuesday")
    with pytest.raises(parse.Unreadable):
        parse.parse_date("2024-03")  # a month alone only where a month is meant


def test_numbers_money_choices_and_intervals():
    assert parse.parse_money("$1,250.005") == Decimal("1250.01") and parse.parse_money("USD 12") == Decimal("12.00")
    assert parse.parse_decimal("(1,250.00)") == Decimal("-1250.00") and parse.parse_money("") is None
    for bad in ("(5)", "1e400", "nan", "twelve"):
        with pytest.raises(parse.Unreadable):
            parse.parse_money(bad)
    assert [parse.parse_interval_months(v) for v in ("12", "6 mo", "2 yr", "Semi-annual", "Quarterly", "")] == [12, 6, 24, 6, 3, None]
    with pytest.raises(parse.Unreadable):
        parse.parse_interval_months("0")
    assert parse.parse_yes_no("Y") is True and parse.parse_yes_no("") is None
    assert parse.parse_choice("In_Service", {"in service": "in_service"}, what="status") == "in_service"
    with pytest.raises(parse.Unreadable, match="not a status"):
        parse.parse_choice("Surplus", {"in service": "in_service"}, what="status")


@needs_postgres
def test_a_run_under_the_policies(ctx, fake, make_user):
    kim = make_user("director")
    as_app_role()
    run = services.confirm_columns(services.upload(kim, "fake", "d.csv", csv_bytes("Dept", "ICU", "db-error")), kim, {"name": 0})
    run = services.start_import(run_through(run, kim), kim)
    run = run_through(run, kim)  # the refused row rolls back to its savepoint; the transaction carries on
    assert run.counts == {"create": 1, "skip": 1} and Department.objects.filter(name="ICU").exists()
