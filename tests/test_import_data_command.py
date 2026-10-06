"""
`manage.py import_data` (slice 23, part D): the screen's steps from the command line. A dry run checks and saves nothing; without
it the import follows; both print the counts and the notes grouped; required columns it cannot find by name stop it, naming them and
the file's columns; no row or size limit; a file that stops before its import is discarded; the facility is read before its
tenant_context (the row-level security stand-in here, the real policies on PostgreSQL). A fake importer (departments by name)
stands in for the real kinds, which parts A to C build.
"""
from io import StringIO

import pytest
from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command
from pg_helpers import as_app_role, needs_postgres

from apps.accounts.models import Level, Module
from apps.equipment.models import Department
from apps.imports import base, kinds, services
from apps.imports.models import ImportRun
from apps.tenants.context import tenant_context


class FakeImporter(base.Importer):
    kind = "fake"
    label = "Departments"
    module = Module.EQUIPMENT
    level = Level.EDIT
    key = "name"
    order = 5
    chunk = 2
    columns = [base.Column("name", "Department", ("department", "dept", "unit"), required=True, max_length=80, example="ICU"),
               base.Column("status", "Status", ("status",))]

    def apply(self, ctx, row, result):
        name = row["name"]
        if name.startswith("skip"):
            raise base.RowSkip("Skipped on purpose")
        if name == "invalid":
            raise ValidationError("Not a department")
        if row.get("status"):
            ctx.read_as("Status", row["status"], "Active")
        if Department.objects.filter(name__iexact=name).exists():
            return
        Department.objects.create(name=name)
        result.outcome = result.CREATE
        ctx.total("Departments", "added")
        ctx.add_created("Departments", name)


@pytest.fixture(autouse=True)
def fake(monkeypatch):
    monkeypatch.setitem(kinds.KINDS, "fake", FakeImporter())


@pytest.fixture
def depts(tmp_path):
    path = tmp_path / "depts.csv"
    path.write_bytes(b"\xef\xbb\xbfDept;Status;Phone\r\nICU;Active;x\r\nOR;Open;\r\nskip-me;;\r\ninvalid;;\r\nPACU;;\r\nicu;;\r\n")
    return path


def run_command(*args):
    out, err = StringIO(), StringIO()
    call_command("import_data", *args, stdout=out, stderr=err)
    return out.getvalue()


def runs(tenant):
    with tenant_context(tenant):
        return list(ImportRun.objects.all())


def test_a_dry_run_checks_prints_and_saves_nothing(tenant, depts):
    out = run_command("--tenant", "riverside", "--kind", "fake", str(depts), "--dry-run")
    assert "Columns: Department from 'Dept'; Status from 'Status'" in out and "Not read: 'Phone'" in out
    assert "Checked depts.csv (departments, 6 rows): 3 to add, 0 to update, 0 unchanged, 3 to skip." in out
    assert "  Skipped, 1 row: Department icu is also on line 2: one row per record\n    line 7 (icu)" in out
    assert "  Skipped, 1 row: Not a department\n    line 5 (invalid)" in out
    assert out.index("Skipped, 1 row") < out.index("Status read:")
    assert "  Status read: 'Active' as Active (1); 'Open' as Active (1)" in out
    assert "  Departments: added 3" in out and "  Adds departments (3): ICU, OR, PACU" in out
    assert out.rstrip().endswith("Dry run: nothing was imported.")
    with tenant_context(tenant):
        assert not Department.objects.exists()
    [run] = runs(tenant)
    assert run.status == "discarded" and run.rows == [] and run.uploaded_by is None


def test_without_dry_run_the_import_follows(tenant, other_tenant, depts):
    out = run_command("--tenant", "riverside", "--kind", "fake", str(depts))
    assert "Checked depts.csv" in out and "Imported depts.csv (departments, 6 rows): 3 added, 0 updated, 0 unchanged, 3 skipped." in out
    assert "  Added departments (3): ICU, OR, PACU" in out
    with tenant_context(tenant):
        assert sorted(Department.objects.values_list("name", flat=True)) == ["ICU", "OR", "PACU"]
    with tenant_context(other_tenant):
        assert not Department.objects.exists()
    [run] = runs(tenant)
    assert run.status == "imported" and run.rows == [] and run.imported_by is None and run.counts == {"create": 3, "skip": 3}
    again = run_command("--tenant", "riverside", "--kind", "fake", str(depts))  # a re-run doubles nothing
    assert "0 added, 0 updated, 3 unchanged, 3 skipped." in again


def test_required_columns_it_cannot_find_stop_it_with_the_files_columns(tenant, tmp_path):
    path = tmp_path / "wrong.csv"
    path.write_text("Ward,Status\nICU,Active\n")
    with pytest.raises(CommandError) as e:
        run_command("--tenant", "riverside", "--kind", "fake", str(path))
    message = str(e.value)
    assert "The file has no column for Department (a column named 'department', 'dept', 'unit')." in message
    assert "Its columns are: 'Ward', 'Status'." in message
    [run] = runs(tenant)
    assert run.status == "discarded" and run.rows == []  # the facility's data is not kept for a run that stopped


def test_what_it_refuses_before_reading_anything(tenant, tmp_path, depts):
    with pytest.raises(CommandError, match="--kind must be one of"):
        run_command("--tenant", "riverside", "--kind", "nope", str(depts))
    with pytest.raises(CommandError, match="No facility has the slug 'nowhere'"):
        run_command("--tenant", "nowhere", "--kind", "fake", str(depts))
    with pytest.raises(CommandError, match="Cannot read"):
        run_command("--tenant", "riverside", "--kind", "fake", str(tmp_path / "missing.csv"))
    workbook = tmp_path / "inventory.xlsx"
    workbook.write_bytes(b"PK\x03\x04rest-of-a-zip")
    with pytest.raises(CommandError, match="This is an Excel workbook"):
        run_command("--tenant", "riverside", "--kind", "fake", str(workbook))
    assert runs(tenant) == []


def test_no_limit_on_rows_or_size(tenant, depts, monkeypatch):
    seen = {}
    real = services.upload

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(services, "upload", spy)
    run_command("--tenant", "riverside", "--kind", "fake", str(depts), "--dry-run")
    assert seen == {"max_rows": None, "max_bytes": None}


def test_a_running_import_stops_it_and_its_check_is_discarded(tenant, make_user, depts):
    with tenant_context(tenant):
        kim = make_user("director")
        running = services.confirm_columns(services.upload(kim, "fake", "d.csv", b"Dept\r\nLab\r\n"), kim, {"name": 0})
        while running.status == "checking":
            running = services.process(running, kim)
        services.start_import(running, kim)
    with pytest.raises(CommandError, match="Another import is running in this facility"):
        run_command("--tenant", "riverside", "--kind", "fake", str(depts))
    with tenant_context(tenant):
        mine = ImportRun.objects.get(file_name="depts.csv")
        assert mine.status == "discarded" and mine.rows == [] and not Department.objects.exists()


def test_it_reads_no_tenant_table_before_the_facility_is_set(tenant, depts, monkeypatch):
    """The stand-in for the policies (tests/test_rls_paths.py's): no query touches a tenant-scoped table while no tenant is set."""
    from test_rls_paths import _Rls

    guard = _Rls()
    monkeypatch.setattr("apps.tenants.context.set_db_tenant", guard.set_db_tenant)
    with guard:
        run_command("--tenant", "riverside", "--kind", "fake", str(depts))
    assert guard.violations == []


@needs_postgres
def test_the_command_under_the_policies(tenant, other_tenant, depts):
    as_app_role()
    out = run_command("--tenant", "riverside", "--kind", "fake", str(depts))
    assert "3 added" in out
    with tenant_context(tenant):
        assert Department.objects.count() == 3 and ImportRun.objects.get().status == "imported"
    with tenant_context(other_tenant):
        assert not Department.objects.exists() and not ImportRun.objects.exists()
