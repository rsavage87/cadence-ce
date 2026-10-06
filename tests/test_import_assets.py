"""The CSV importer (import_assets) and the devices added on screen (slice 12) share one rule: a tag names one device in any
letter case, and the tags that collide with the app's links are refused. Since slice 23 the command is the Devices import
(apps.imports.kinds.devices) from the command line: checked first, then imported chunk by chunk, or only checked with --dry-run."""
from io import StringIO

import pytest
from django.core.management import CommandError, call_command

from apps.equipment import services
from apps.equipment.models import Asset
from apps.imports.models import ImportRun


def _import(tmp_path, body: str) -> str:
    path = tmp_path / "inventory.csv"
    path.write_text("tag,manufacturer,model,department,room\n" + body)
    out, err = StringIO(), StringIO()
    call_command("import_assets", "--tenant", "riverside", str(path), stdout=out, stderr=err)
    return out.getvalue() + err.getvalue()


def test_an_import_updates_a_device_added_on_screen_whatever_the_letter_case(ctx, dept, pump_model, tmp_path):
    services.create_asset(tag="ce-77001", device_model=pump_model, department=dept)
    text = _import(tmp_path, "CE-77001,BD,Alaris 8015 PCU,ICU,12B\n")
    assert "0 created, 1 updated" in text
    assert list(Asset.objects.filter(tag__iexact="ce-77001").values_list("tag", "room")) == [("ce-77001", "12B")]


def test_an_import_skips_the_reserved_tags(ctx, dept, pump_model, tmp_path):
    text = _import(tmp_path, "new,BD,Alaris 8015 PCU,ICU,\n.,BD,Alaris 8015 PCU,ICU,\n..,BD,Alaris 8015 PCU,ICU,\nCE-77002,BD,Alaris 8015 PCU,ICU,\n")
    assert "1 created, 0 updated, 3 skipped" in text
    assert list(Asset.objects.values_list("tag", flat=True)) == ["CE-77002"]


def test_a_file_without_a_status_leaves_a_retired_device_retired(ctx, dept, vent_model, tmp_path):
    """Re-importing an inventory (no status column) must not bring a retired device back into service with no next PM."""
    a = services.create_asset(tag="CE-77003", device_model=vent_model, department=dept)
    services.set_status(a, "retired")
    _import(tmp_path, "CE-77003,Hamilton Medical,Hamilton-G5,ICU,4\n")
    a.refresh_from_db()
    assert a.status == "retired" and a.next_pm_on is None and a.room == "4"


def test_an_imported_status_goes_through_the_device_rules(ctx, dept, vent_model, tmp_path):
    from apps.workorders.models import WoStatus
    from apps.workorders.services import create_work_order

    a = services.create_asset(tag="CE-77004", device_model=vent_model, department=dept)
    pm = create_work_order(asset=a, type="pm", priority="normal", problem="PM")
    path = tmp_path / "status.csv"
    path.write_text("tag,manufacturer,model,department,status\nCE-77004,Hamilton Medical,Hamilton-G5,ICU,Retired\n")
    call_command("import_assets", "--tenant", "riverside", str(path), stdout=StringIO(), stderr=StringIO())
    a.refresh_from_db()
    pm.refresh_from_db()
    assert a.status == "retired" and a.next_pm_on is None and pm.status == WoStatus.CANCELLED  # as retiring from the drawer does
    path.write_text("tag,manufacturer,model,department,status\nCE-77004,Hamilton Medical,Hamilton-G5,ICU,In service\n")
    call_command("import_assets", "--tenant", "riverside", str(path), stdout=StringIO(), stderr=StringIO())
    a.refresh_from_db()
    assert a.status == "in_service" and a.next_pm_on is not None  # reinstated with a PM due


def test_a_new_device_without_a_next_pm_gets_its_first_pm_worked_out(ctx, dept, vent_model, tmp_path):
    _import(tmp_path, "CE-77005,Hamilton Medical,Hamilton-G5,ICU,\n")
    assert Asset.objects.get(tag="CE-77005").next_pm_on is not None


def test_a_dry_run_checks_the_file_and_changes_nothing(ctx, dept, pump_model, tmp_path):
    path = tmp_path / "inventory.csv"
    path.write_text("Control No.,Mfr,Model,Dept\nCE-77006,BD,Alaris 8015 PCU,ICU\nCE 77007,BD,Alaris 8015 PCU,ICU\n")
    out, err = StringIO(), StringIO()
    call_command("import_assets", "--tenant", "riverside", str(path), "--dry-run", stdout=out, stderr=err)
    assert "Dry run (nothing saved): 1 created, 0 updated, 1 skipped, 0 unchanged" in out.getvalue()
    assert "Skipped 1 row: Asset tags cannot contain spaces or slashes." in err.getvalue() and "line 3 (CE 77007)" in err.getvalue()
    assert not Asset.objects.exists()
    run = ImportRun.objects.get()
    assert run.status == "discarded" and run.rows == []  # the facility's rows are not kept after a dry run


def test_a_file_without_a_needed_column_is_refused_by_name(ctx, tmp_path):
    path = tmp_path / "inventory.csv"
    path.write_text("Tag,Department,Vendor\nCE-77008,ICU,BD\n")  # "Vendor" is not the manufacturer
    with pytest.raises(CommandError, match="Could not find a column for Manufacturer, Model. Columns seen: Tag, Department, Vendor"):
        call_command("import_assets", "--tenant", "riverside", str(path), stdout=StringIO(), stderr=StringIO())
    assert ImportRun.objects.get().status == "discarded"
