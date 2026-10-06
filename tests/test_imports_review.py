"""
Slice 23 review fixes in the import framework: a wide header refused before it fills the memory, a row with more values than the
header skipped rather than shifted, notes pointing at the file's own lines, a decimal comma and a decimal interval read right, every
skipped row kept, a pass acting as whoever started it, a plain save the database refuses skipping only its row, every earlier row
visible to an importer (ctx.rows), and unfinished runs expired by a daily job.
"""
from datetime import timedelta
from io import StringIO

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.utils import timezone
from test_imports_framework import FakeImporter, csv_bytes, run_through

from apps.accounts.models import Level, Module
from apps.equipment.models import Department
from apps.imports import base, kinds, parse, services
from apps.imports.models import ImportRun


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setitem(kinds.KINDS, "fake", FakeImporter())
    return kinds.KINDS["fake"]


def test_a_header_wider_than_any_export_is_refused():
    with pytest.raises(ValidationError, match="columns"):
        base.read_file("a" + "," * base.MAX_COLUMNS + "\nx\n")


def test_a_row_with_more_values_than_the_header_is_skipped_and_lines_are_the_files(ctx, fake, make_user):
    kim = make_user("director")
    data = csv_bytes("Dept,Note", "", "ICU,a", "", "OR,b,stray", '"Multi', 'line",c', "PACU,d,")
    run = services.upload(kim, "fake", "d.csv", data)
    assert run.lines == [3, 5, 6, 8] and run.file_problems == {"1": base.WIDE_ROW}  # a blank trailing cell is no extra value
    run = run_through(services.confirm_columns(run, kim, {"name": 0, "note": 1}), kim)
    assert [(line, outcome) for line, _k, outcome, _n in run.results] == [(3, "create"), (5, "skip"), (6, "create"), (8, "create")]
    assert run.results[1][3] == [base.WIDE_ROW]


def test_numbers_and_intervals_are_read_as_written():
    assert parse.parse_money("3,200.50") == parse.parse_money("3200.50")
    with pytest.raises(parse.Unreadable, match="point for decimals"):
        parse.parse_money("3200,50")
    assert [parse.parse_interval_months(v) for v in ("12.0", "6.0 mo", "1.5 yr", "6 mo.")] == [12, 6, 18, 6]
    with pytest.raises(parse.Unreadable):
        parse.parse_interval_months("6.5")


class NoisyImporter(FakeImporter):
    """Every row gets a note; a name starting with "skip" is skipped; "plain-save-fails" breaks a plain save the database refuses."""

    kind = "noisy"

    def apply(self, ctx, row, result):
        if row["name"].startswith("skip"):
            raise base.RowSkip("Skipped")
        if row["name"] == "plain-save-fails":
            Department.objects.create(name="Twice")
            Department.objects.create(name="Twice")  # a plain save the database refuses: no service's atomic around it
        result.warn("A note")
        result.outcome = result.CREATE
        ctx.total("Seen", "rows before this one", len([r for r in ctx.rows[:ctx.index] if r]))


def test_every_skipped_row_is_kept_and_notes_beyond_the_cap_are_counted(ctx, fake, make_user, monkeypatch):
    monkeypatch.setitem(kinds.KINDS, "noisy", NoisyImporter())
    monkeypatch.setattr(services, "NOTES_KEPT", 2)
    kim = make_user("director")
    run = services.upload(kim, "noisy", "d.csv", csv_bytes("Dept", "a", "b", "c", "skip1", "d", "skip2"))
    run = run_through(services.confirm_columns(run, kim, {"name": 0}), kim)
    assert [r[2] for r in run.results] == ["create", "create", "skip", "skip"] and run.summary["notes_not_kept"] == 2
    assert run.summary["totals"]["Seen"]["rows before this one"] == str(0 + 1 + 2 + 4)  # ctx.rows and ctx.index across chunks


def test_a_plain_save_the_database_refuses_skips_only_its_row(ctx, fake, make_user, monkeypatch):
    """Department names are unique (uniq_department_per_tenant): the second plain create fails, outside any service's atomic block."""
    monkeypatch.setitem(kinds.KINDS, "noisy", NoisyImporter())
    kim = make_user("director")
    run = services.upload(kim, "noisy", "d.csv", csv_bytes("Dept", "a", "plain-save-fails", "b"))
    run = run_through(services.start_import(run_through(services.confirm_columns(run, kim, {"name": 0}), kim), kim), kim)
    assert run.status == "imported" and run.counts == {"create": 2, "skip": 1}
    assert not Department.objects.filter(name="Twice").exists()  # the row's savepoint took its first create back too


def test_a_pass_acts_as_whoever_started_it(ctx, fake, make_user, monkeypatch):
    """Whoever's browser asks for the next chunk, the rows are applied with the levels of the person who started the pass."""
    seen = []
    real = FakeImporter.apply
    monkeypatch.setattr(FakeImporter, "apply", lambda self, c, row, result: (seen.append(c.user), real(self, c, row, result))[1])
    tech, director = make_user("technician"), make_user("director")
    run = services.upload(tech, "fake", "d.csv", csv_bytes("Dept", "a", "b", "c", "d"))
    run = services.confirm_columns(run, tech, {"name": 0})
    run = services.process(run, director)  # the director opened the page and their browser drove a chunk
    run = run_through(run, tech)
    assert set(seen) == {tech} and run.pass_by == tech
    ImportRun.objects.filter(pk=run.pk).update(pass_by=None)
    run = services.start_import(ImportRun.objects.get(pk=run.pk), director)
    seen.clear()
    run_through(run, tech)
    assert set(seen) == {director}


def test_unfinished_runs_expire_daily_and_a_file_never_mapped_sooner(ctx, fake, make_user, tenant):
    kim = make_user("director")
    mapping = services.upload(kim, "fake", "d.csv", csv_bytes("Dept,Free text", "ICU,anything"))
    checked = run_through(services.confirm_columns(services.upload(kim, "fake", "d.csv", csv_bytes("Dept", "OR")), kim, {"name": 0}), kim)
    three_days = timezone.now() - timedelta(days=3)
    ImportRun.objects.filter(pk__in=[mapping.pk, checked.pk]).update(updated_at=three_days)
    call_command("expire_imports", stdout=(out := StringIO()))
    assert "riverside: 1 unfinished import expired" in out.getvalue()
    mapping.refresh_from_db(), checked.refresh_from_db()
    assert (mapping.status, mapping.rows, checked.status) == ("expired", [], "checked")


def test_the_level_of_each_kind_still_applies_to_the_driver(ctx, fake, make_user):
    analyst = make_user("analyst")
    run = services.confirm_columns(services.upload(make_user("director"), "fake", "d.csv", csv_bytes("Dept", "a")), make_user(
        "director", username="kim2@riverside.example"), {"name": 0})
    assert not analyst.has_level(Module.EQUIPMENT, Level.EDIT)
    with pytest.raises(Exception):
        services.process(run, analyst)
