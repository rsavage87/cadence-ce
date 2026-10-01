"""The CSV importer (import_assets) and the devices added on screen (slice 12) share one rule: a tag names one device in any
letter case, and the tags that collide with the app's links are refused."""
from io import StringIO

from django.core.management import call_command

from apps.equipment import services
from apps.equipment.models import Asset


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
