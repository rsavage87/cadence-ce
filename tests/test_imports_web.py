"""
Import data (slice 23, part D): the Settings panel, the page of kinds and runs, a kind's template, an upload and its refusals, the
column choice with its samples, the check's progress (the bar asking for each chunk until the result), what the check found, the
import, a stopped import's Continue, discard, the notes CSV, every step refused through the services for a kind the user may not
import, other facilities' runs, scoped users, the stylesheet's rules, and the whole walk as the runtime role under row-level
security. Fake importers (departments by name; a second kind needing Users Edit) stand in for the real kinds, which parts A to C build.
"""
import json
import pathlib
import re
from datetime import timedelta

import pytest
from csvutil import csv_rows
from django.utils import timezone
from pg_helpers import as_app_role, needs_postgres

from apps.accounts.models import Level, Module, Role, User
from apps.equipment.models import Department
from apps.imports import base, kinds, services
from apps.imports.models import ImportRun
from apps.tenants.context import tenant_context
from apps.tenants.models import Tenant

HX = {"HTTP_HX_REQUEST": "true"}
PAGE = "/settings/import/"
CSS = pathlib.Path("apps/web/static/web/cadence.css")


class FakeImporter(base.Importer):
    kind = "fake"
    label = "Departments"
    module = Module.EQUIPMENT
    level = Level.EDIT
    key = "name"
    order = 5
    chunk = 2
    description = "Your departments, by name."
    columns = [base.Column("name", "Department", ("department", "dept", "unit"), required=True, max_length=80, help="As staff know it", example="ICU"),
               base.Column("code", "Cost center", ("cost center", "cc"), max_length=20, example="4410"),
               base.Column("status", "Status", ("status", "state"), example="Active")]

    def apply(self, ctx, row, result):
        name = row["name"]
        if name.startswith("skip"):
            raise base.RowSkip("Skipped on purpose")
        if row.get("status"):
            ctx.read_as("Status", row["status"], "Active")
        if row.get("code"):
            result.warn("Cost center not read: left blank")
        if Department.objects.filter(name__iexact=name).exists():
            return
        Department.objects.create(name=name)
        result.outcome = result.CREATE
        ctx.total("Departments", "added")
        ctx.add_created("Departments", name)


class StaffImporter(FakeImporter):
    kind = "staff"
    label = "Staff"
    module = Module.USERS
    level = Level.EDIT
    order = 6
    description = "Your staff, by name."


@pytest.fixture(autouse=True)
def fake_kinds(monkeypatch):
    """Only the fakes: the page and its tests do not depend on the real kinds (parts A to C)."""
    monkeypatch.setattr(kinds, "KINDS", {"fake": FakeImporter(), "staff": StaffImporter()})


def csv_bytes(*lines):
    return ("\r\n".join(lines) + "\r\n").encode()


DEPTS = csv_bytes("Dept,CC,Status,Extra", "ICU,4410,Active,x", "OR,,Open,y", "skip-me,,,", "PACU,,,z", "icu,,,")


@pytest.fixture
def kim(client, make_user):
    user = make_user("director")
    client.force_login(user)
    return user


def settings_only_user(tenant, level=Level.VIEW):
    role = Role.objects.create(name="Settings only", slug="settings-only")
    role.set_levels({"settings": level})
    return User.objects.create_user(username="s@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role)


def upload(client, data=DEPTS, kind="fake", name="depts.csv"):
    from django.core.files.uploadedfile import SimpleUploadedFile

    return client.post(PAGE, {"kind": kind, "file": SimpleUploadedFile(name, data, content_type="text/csv")})


def uploaded(client, data=DEPTS, kind="fake") -> ImportRun:
    r = upload(client, data, kind)
    assert r.status_code == 302, r.content.decode()[:500]
    with tenant_context(Tenant.objects.get(slug="riverside")):  # inside the facility again: a request leaves it on its way out (RLS)
        run = ImportRun.objects.get(pk=r["Location"].split("/")[-2])
    assert r["Location"] == f"{PAGE}{run.pk}/"
    return run


def drive(client, run) -> None:
    """Ask for chunks as the progress bar does, until the answer sends the browser to the run's page."""
    for _ in range(50):
        r = client.post(f"{PAGE}{run.pk}/next/", **HX)
        assert r.status_code == 200
        if "HX-Redirect" in r:
            assert r["HX-Redirect"] == f"{PAGE}{run.pk}/"
            return
        body = r.content.decode()
        assert 'hx-trigger="load"' in body and f'hx-post="{PAGE}{run.pk}/next/"' in body
    raise AssertionError("the pass never finished")


def checked(client, data=DEPTS) -> ImportRun:
    run = uploaded(client, data)
    assert client.post(f"{PAGE}{run.pk}/columns/", {"map-name": "0", "map-code": "1", "map-status": "2"}).status_code == 302
    drive(client, run)
    run.refresh_from_db()
    assert run.status == "checked"
    return run


# --- Settings' panel ----------------------------------------------------------------------------------------------------------

def test_the_settings_panel_is_for_whoever_may_import_a_kind(client, ctx, tenant, make_user, kim):
    body = client.get("/settings/").content.decode()
    assert 'id="set-import"' in body and "Bring in departments and staff from CSV files." in body
    assert f'<a class="btn sm" href="{PAGE}">Import data</a>' in body and "not finished" not in body
    uploaded(client)
    assert "1 file not finished." in client.get("/settings/").content.decode()
    client.force_login(make_user("manager"))  # Equipment Edit, Users View: the departments only
    body = client.get("/settings/").content.decode()
    assert "Bring in departments from CSV files." in body and "1 file not finished." in body
    client.force_login(settings_only_user(tenant, Level.FULL))  # Settings Full, nothing to import
    body = client.get("/settings/").content.decode()
    assert 'id="set-import"' not in body and PAGE not in body


# --- the page -----------------------------------------------------------------------------------------------------------------

def test_the_page_lists_the_kinds_in_order_and_the_runs_of_those_kinds(client, ctx, make_user, kim):
    r = client.get(PAGE)
    body = r.content.decode()
    assert r.status_code == 200 and r.context["nav_active"] == "settings"
    assert [k["importer"].kind for k in r.context["kinds"]] == ["fake", "staff"]
    assert body.index("Your departments, by name.") < body.index("Your staff, by name.")
    assert f'href="{PAGE}template/fake.csv" download' in body and f'href="{PAGE}template/staff.csv" download' in body
    assert body.count(f'action="{PAGE}" enctype="multipart/form-data"') == 2 and '<input type="hidden" name="kind" value="staff">' in body
    assert "up to 5 MB and 20,000 rows" in body and "<b>Department</b> <span class=\"muted\">(required)</span>: As staff know it" in body
    assert "No files yet." in body
    mine = uploaded(client)
    theirs = uploaded(client, kind="staff")
    body = client.get(PAGE).content.decode()
    assert f'href="{PAGE}{mine.pk}/"' in body and f'href="{PAGE}{theirs.pk}/"' in body and "Choosing columns" in body
    assert "Departments · 5 rows · Director User" in body
    client.force_login(make_user("manager"))  # may import departments only: sees only those, and only that kind's form
    r = client.get(PAGE)
    body = r.content.decode()
    assert [k["importer"].kind for k in r.context["kinds"]] == ["fake"] and "Your staff, by name." not in body
    assert f'href="{PAGE}{mine.pk}/"' in body and f'href="{PAGE}{theirs.pk}/"' not in body
    assert "<li>Staff: Users and access Edit</li>" in body  # who may import what


def test_a_role_that_may_import_nothing_is_told_so(client, ctx, tenant):
    client.force_login(settings_only_user(tenant))
    body = client.get(PAGE).content.decode()
    assert "Your role cannot import any kind of file." in body and "<li>Departments: Equipment Edit</li>" in body
    assert "multipart/form-data" not in body


def test_a_kinds_template_maps_itself(client, ctx, make_user, kim):
    r = client.get(f"{PAGE}template/fake.csv")
    assert r.status_code == 200 and r["Content-Disposition"] == 'attachment; filename="fake-template.csv"'
    header, *rows = csv_rows(r)
    assert header == ["Department", "Cost center", "Status"] and rows == [["ICU", "4410", "Active"]]
    assert base.auto_map(kinds.KINDS["fake"].columns, header) == {"name": 0, "code": 1, "status": 2}
    assert client.get(f"{PAGE}template/nope.csv").status_code == 404
    client.force_login(make_user("manager"))
    assert client.get(f"{PAGE}template/staff.csv").status_code == 403


def test_stale_runs_expire_when_the_page_is_read(client, ctx, kim):
    run = uploaded(client)
    ImportRun.objects.filter(pk=run.pk).update(updated_at=timezone.now() - timedelta(days=services.EXPIRE_DAYS + 1))
    assert "Expired" in client.get(PAGE).content.decode()
    run.refresh_from_db()
    assert run.status == "expired" and run.rows == []
    body = client.get(f"{PAGE}{run.pk}/").content.decode()
    assert f"Expired: it was left unfinished for {services.EXPIRE_DAYS} days" in body and "Check the file" not in body


# --- uploading ----------------------------------------------------------------------------------------------------------------

def test_an_unreadable_or_missing_file_comes_back_beside_its_form(client, ctx, kim, monkeypatch):
    r = upload(client, b"PK\x03\x04a-workbook")
    body = r.content.decode()
    assert r.status_code == 200 and 'id="imp-err-fake" role="alert">This is an Excel workbook, not a CSV.' in body
    assert 'aria-invalid="true" aria-describedby="imp-err-fake"' in body and 'id="imp-err-staff"' not in body
    r = client.post(PAGE, {"kind": "fake"})
    assert "Choose the CSV file to upload." in r.content.decode()
    monkeypatch.setattr(base, "MAX_BYTES", 10)
    assert "The file is larger than" in upload(client).content.decode()
    r = upload(client, kind="nope")
    assert r.status_code == 200 and 'class="note warn imp-note" role="alert">Choose what the file holds.' in r.content.decode()
    assert not ImportRun.objects.exists()


# --- choosing the columns -----------------------------------------------------------------------------------------------------

def test_the_column_choice_shows_the_guess_the_samples_and_what_is_left_out(client, ctx, kim):
    run = uploaded(client, csv_bytes("Dept,Status,Extra,dept", "ICU,Active,x,a", "OR,Open,,b", "PACU,Active,y,c", "NICU,,,d"))
    r = client.get(f"{PAGE}{run.pk}/")
    body = r.content.decode()
    assert r.status_code == 200 and "Choose the file's columns" in body and "2 of 3 found by name" in body
    rows = {row["column"].key: row for row in r.context["mapping"]}
    assert rows["name"]["index"] == 0 and rows["status"]["index"] == 1 and rows["code"]["index"] is None
    assert '<label for="map-name">Department</label> <span class="chip warn">Required</span>' in body
    assert '<select id="map-name" name="map-name"' in body and f'hx-get="{PAGE}{run.pk}/samples/"' in body
    assert '<option value="0" selected>Dept</option>' in body and '<option value="">Not in the file</option>' in body
    assert '<span class="imp-smp-v">ICU</span> · <span class="imp-smp-v">OR</span> · <span class="imp-smp-v">PACU</span>' in body
    assert "Not matched by name: Extra, dept. A column of the file is left out" in body and "more than one column named Dept, dept" in body
    # a new choice reads that column's values
    r = client.get(f"{PAGE}{run.pk}/samples/?map-code=2", **HX)
    assert '<span class="imp-smp-v">x</span> · <span class="imp-smp-v">y</span>' in r.content.decode()
    assert "No values to show" in client.get(f"{PAGE}{run.pk}/samples/?map-code=", **HX).content.decode()
    assert client.get(f"{PAGE}{run.pk}/samples/?map-code=2").status_code == 302  # only ever a fragment


def test_a_refused_column_choice_comes_back_as_chosen(client, ctx, kim):
    run = uploaded(client)
    r = client.post(f"{PAGE}{run.pk}/columns/", {"map-name": "", "map-code": "1", "map-status": ""})
    body = r.content.decode()
    assert r.status_code == 200 and 'role="alert">Choose the file&#x27;s column for Department: it is needed.' in body
    assert '<option value="1" selected>CC</option>' in body and '<option value="0" selected>' not in body
    assert "Not chosen: Dept, Status, Extra." in body
    r = client.post(f"{PAGE}{run.pk}/columns/", {"map-name": "0", "map-code": "0"})
    assert "Dept is chosen more than once" in r.content.decode()
    run.refresh_from_db()
    assert run.status == "mapping"


# --- the check and the import -------------------------------------------------------------------------------------------------

def test_the_check_runs_chunk_by_chunk_and_saves_nothing(client, ctx, kim):
    run = uploaded(client)
    r = client.post(f"{PAGE}{run.pk}/columns/", {"map-name": "0", "map-code": "1", "map-status": "2"})
    assert r.status_code == 302 and r["Location"] == f"{PAGE}{run.pk}/"
    body = client.get(r["Location"]).content.decode()
    assert f'id="imp-progress" aria-labelledby="imp-progress-h" hx-post="{PAGE}{run.pk}/next/" hx-trigger="load"' in body
    assert "0 of 5 rows" in body and "nothing is saved yet" in body and "Stop the check" in body
    first = client.post(f"{PAGE}{run.pk}/next/", **HX)
    assert "2 of 5 rows" in first.content.decode() and 'aria-valuenow="40"' in first.content.decode()
    drive(client, run)
    run.refresh_from_db()
    assert run.status == "checked" and not Department.objects.exists()


def test_what_the_check_found(client, ctx, kim):
    run = checked(client)
    r = client.get(f"{PAGE}{run.pk}/")
    body = r.content.decode()
    assert [(c["label"], c["n"]) for c in r.context["counts"]] == [("To add", 3), ("To update", 0), ("Unchanged", 0), ("To skip", 2)]
    assert "Checked: nothing is saved yet." in body and "Import adds and updates 3 rows and leaves out the 2 to skip." in body
    assert f'action="{PAGE}{run.pk}/import/"' in body and f'action="{PAGE}{run.pk}/discard/"' in body
    groups = r.context["groups"]
    assert [g["note"] for g in groups] == ["Department icu is also on line 2: one row per record", "Skipped on purpose", "Cost center not read: left blank"]
    assert body.index("Skipped on purpose") < body.index("Cost center not read")  # skipped rows first
    assert '<span class="chip crit">To skip</span>' in body and '<span class="chip warn">Imports with a note</span>' in body
    assert "Line 6 · Department icu" in body and "Line 2 · Department ICU" in body
    assert "<td>Active</td><td>Active</td>" in body and "<td>Open</td><td>Active</td>" in body  # how the Status values were read
    assert "<td>added</td><td class=\"num\">3</td>" in body
    assert "What the import adds" in body and "ICU, OR, PACU" in body
    assert f'href="{PAGE}{run.pk}/notes.csv" download' in body


def test_the_notes_csv_lists_every_row_with_a_note(client, ctx, kim):
    run = checked(client)
    r = client.get(f"{PAGE}{run.pk}/notes.csv")
    assert r["Content-Disposition"].startswith('attachment; filename="cadence-import-fake-notes-')
    assert csv_rows(r) == [["Line", "Department", "Outcome", "Notes"], ["2", "ICU", "To add", "Cost center not read: left blank"],
                           ["4", "skip-me", "To skip", "Skipped on purpose"], ["6", "icu", "To skip", "Department icu is also on line 2: one row per record"]]


def test_import_then_what_it_did_read_only(client, ctx, kim):
    run = checked(client)
    r = client.post(f"{PAGE}{run.pk}/import/")
    assert r.status_code == 302 and r["Location"] == f"{PAGE}{run.pk}/"
    body = client.get(r["Location"]).content.decode()
    assert 'hx-trigger="load"' in body and "Importing" in body and "Keep this page open" in body and "Stop importing" in body
    drive(client, run)
    assert sorted(Department.objects.values_list("name", flat=True)) == ["ICU", "OR", "PACU"]
    r = client.get(f"{PAGE}{run.pk}/")
    body = r.content.decode()
    assert [(c["label"], c["n"]) for c in r.context["counts"]] == [("Added", 3), ("Updated", 0), ("Unchanged", 0), ("Skipped", 2)]
    assert "Imported. The file's rows are no longer kept here" in body and "What the import added" in body
    assert '<span class="chip crit">Skipped</span>' in body and '<span class="chip warn">Imported with a note</span>' in body
    assert f'action="{PAGE}{run.pk}/import/"' not in body and f'action="{PAGE}{run.pk}/discard/"' not in body and "Import another file" in body
    assert "imported by Director User" in body
    assert "3 added, 2 skipped" in client.get(PAGE).content.decode()
    assert csv_rows(client.get(f"{PAGE}{run.pk}/notes.csv"))[1][2] == "Added"


def test_a_stopped_import_waits_for_continue(client, ctx, kim):
    run = checked(client)
    client.post(f"{PAGE}{run.pk}/import/")
    client.post(f"{PAGE}{run.pk}/next/", **HX)  # one chunk, then the tab is closed
    ImportRun.objects.filter(pk=run.pk).update(updated_at=timezone.now() - timedelta(minutes=5))
    body = client.get(f"{PAGE}{run.pk}/").content.decode()
    assert 'hx-trigger="load"' not in body and "This import stopped at 40%" in body
    assert f'<form method="post" action="{PAGE}{run.pk}/next/" hx-post="{PAGE}{run.pk}/next/" hx-target="#imp-progress" hx-swap="outerHTML">' in body
    r = client.post(f"{PAGE}{run.pk}/next/", **HX)  # Continue
    assert 'hx-trigger="load"' in r.content.decode() and "4 of 5 rows" in r.content.decode()
    drive(client, run)
    run.refresh_from_db()
    assert run.status == "imported" and Department.objects.count() == 3


def test_without_javascript_continue_does_a_chunk_a_click(client, ctx, kim):
    run = uploaded(client)
    client.post(f"{PAGE}{run.pk}/columns/", {"map-name": "0"})
    r = client.post(f"{PAGE}{run.pk}/next/")
    assert r.status_code == 302 and r["Location"] == f"{PAGE}{run.pk}/"
    run.refresh_from_db()
    assert run.offset == 2 and run.status == "checking"


def test_the_bar_waits_while_another_request_does_the_chunk(client, ctx, kim, monkeypatch):
    run = uploaded(client)
    client.post(f"{PAGE}{run.pk}/columns/", {"map-name": "0"})
    monkeypatch.setattr(services, "process", lambda run, user: ImportRun.objects.get(pk=run.pk))  # the lock is someone else's
    body = client.post(f"{PAGE}{run.pk}/next/", **HX).content.decode()
    assert 'hx-trigger="load delay:2s"' in body


def test_discard_ends_the_run_and_clears_its_rows(client, ctx, kim):
    run = uploaded(client)
    r = client.post(f"{PAGE}{run.pk}/discard/", {"map-name": "0"})  # the column choice's Discard posts the form's choices too
    assert r.status_code == 302 and r["Location"] == f"{PAGE}{run.pk}/"
    run.refresh_from_db()
    assert run.status == "discarded" and run.rows == []
    body = client.get(f"{PAGE}{run.pk}/").content.decode()
    assert "Discarded: nothing from this file was saved." in body and "Check the file" not in body and "/discard/" not in body
    assert client.post(f"{PAGE}{run.pk}/discard/").status_code == 302  # again: its page says how it ended
    stopped = checked(client)
    client.post(f"{PAGE}{stopped.pk}/import/")
    client.post(f"{PAGE}{stopped.pk}/next/", **HX)
    client.post(f"{PAGE}{stopped.pk}/discard/")  # Stop importing: the first chunk stays
    assert "Discarded while it was importing: the rows imported before it stopped stay in Cadence." in client.get(f"{PAGE}{stopped.pk}/").content.decode()
    assert Department.objects.count() == 2


def test_a_check_stopped_part_way_shows_no_counts(client, ctx, kim):
    run = uploaded(client)
    client.post(f"{PAGE}{run.pk}/columns/", {"map-name": "0", "map-code": "1"})
    client.post(f"{PAGE}{run.pk}/next/", **HX)  # 2 of 5 rows checked
    client.post(f"{PAGE}{run.pk}/discard/")  # Stop the check
    r = client.get(f"{PAGE}{run.pk}/")
    assert not r.context["show_counts"] and 'class="stats imp-stats"' not in r.content.decode()
    assert "Discarded: nothing from this file was saved." in r.content.decode() and "Cost center not read" in r.content.decode()
    assert "Departments · 5 rows · Director User" in client.get(PAGE).content.decode()


def test_import_twice_from_two_tabs(client, ctx, kim):
    run = checked(client)
    assert client.post(f"{PAGE}{run.pk}/import/").status_code == 302
    r = client.post(f"{PAGE}{run.pk}/import/")  # the second tab: the run is importing already, and its page shows the progress
    assert r.status_code == 302 and r["Location"] == f"{PAGE}{run.pk}/"


def test_a_second_import_waits_for_the_first(client, ctx, kim):
    first, second = checked(client), checked(client, csv_bytes("Dept,CC,Status", "Lab,,"))
    client.post(f"{PAGE}{first.pk}/import/")
    r = client.post(f"{PAGE}{second.pk}/import/")
    assert r.status_code == 200 and "Another import is running in this facility." in r.content.decode()
    second.refresh_from_db()
    assert second.status == "checked"


# --- who may -------------------------------------------------------------------------------------------------------------------

def test_every_step_is_refused_for_a_kind_the_user_may_not_import(client, ctx, make_user, kim):
    run = uploaded(client, kind="staff")
    done = checked(client)
    client.force_login(make_user("manager"))  # Settings View, Users View: not staff
    assert upload(client, kind="staff").status_code == 403
    assert client.post(PAGE, {"kind": "staff"}).status_code == 403
    for url in (f"{PAGE}{run.pk}/", f"{PAGE}{run.pk}/samples/?map-name=0", f"{PAGE}{run.pk}/notes.csv"):
        assert client.get(url, **HX).status_code == 404, url  # not a run they may see
    for step, data in (("columns", {"map-name": "0"}), ("next", {}), ("import", {}), ("discard", {})):
        assert client.post(f"{PAGE}{run.pk}/{step}/", data, **HX).status_code == 403, step  # the service refuses
    run.refresh_from_db()
    assert run.status == "mapping" and ImportRun.objects.filter(kind="staff").count() == 1
    assert client.get(f"{PAGE}{done.pk}/").status_code == 200  # departments: theirs to import


@pytest.mark.parametrize("slug", ["technician", "analyst", "vendor", "requester"])
def test_roles_without_settings_and_scoped_users_are_refused(client, ctx, make_user, kim, slug):
    run = uploaded(client)
    client.force_login(make_user(slug))
    assert client.get(PAGE).status_code == 403 and client.get(f"{PAGE}{run.pk}/").status_code == 403
    assert client.post(f"{PAGE}{run.pk}/discard/").status_code == 403 and upload(client).status_code == 403


def test_another_facilitys_run_is_a_404(client, ctx, make_user, kim, other_tenant):
    from apps.accounts.models import create_default_roles

    create_default_roles(other_tenant)
    with tenant_context(other_tenant):
        theirs = services.upload(make_user("director", tenant_=other_tenant, username="dir@other.example"), "fake", "d.csv", csv_bytes("Dept", "ICU"))
    for url in ("", "samples/", "notes.csv"):
        assert client.get(f"{PAGE}{theirs.pk}/{url}", **HX).status_code == 404
    for step in ("columns", "next", "import", "discard"):
        assert client.post(f"{PAGE}{theirs.pk}/{step}/", **HX).status_code == 404
    assert str(theirs.pk) not in client.get(PAGE).content.decode()
    with tenant_context(other_tenant):
        assert ImportRun.objects.get(pk=theirs.pk).status == "mapping"


# --- the stylesheet -----------------------------------------------------------------------------------------------------------

def test_the_screens_rules_use_the_themes_tokens_above_the_print_section():
    css = CSS.read_text()
    start = css.index("/* Slice 23, part D: Import data")
    section = css[start:css.index("/* Slice 11: printing app pages */")]
    assert start < css.index("@media print{")
    assert not re.search(r"#[0-9A-Fa-f]{3,6}\b|rgba?\(", section)  # colours from the tokens, so dark mode follows
    phone = section[section.index("@media (max-width:760px){"):]
    assert ".imp-map-row{grid-template-columns:minmax(0,1fr)}" in phone and ".imp-kinds{grid-template-columns:minmax(0,1fr)}" in phone


def test_totals_keep_the_places_the_importer_gave_them():
    from apps.web.views_imports import _amount

    assert [_amount(v) for v in ("3", "12500", "5002.00", "0.03", "-1250.5", "nonsense")] == ["3", "12,500", "5,002.00", "0.03", "-1,250.5", "nonsense"]


def test_the_progress_bar_answers_with_a_redirect_header_htmx_follows(client, ctx, kim):
    run = checked(client)
    r = client.post(f"{PAGE}{run.pk}/next/", **HX)  # nothing left to do: the page shows the result
    assert r.status_code == 200 and r["HX-Redirect"] == f"{PAGE}{run.pk}/"
    assert "HX-Trigger" not in r or "toast" not in json.loads(r["HX-Trigger"])


# --- under row-level security ----------------------------------------------------------------------------------------------------

@needs_postgres
def test_the_import_screen_as_the_runtime_role(client, ctx, kim, make_user, other_tenant):
    """As the runtime role: the panel, the page, the template, an upload, the column choice and its samples, the check, what it
    found and its CSV, the import, and the list, all inside the facility; another facility's departments stay out of it."""
    with tenant_context(other_tenant):
        Department.objects.create(name="ICU")
    as_app_role()
    assert 'id="set-import"' in client.get("/settings/").content.decode()
    assert client.get(PAGE).status_code == 200 and csv_rows(client.get(f"{PAGE}template/fake.csv"))[0][0] == "Department"
    run = uploaded(client)
    assert client.get(f"{PAGE}{run.pk}/").status_code == 200
    assert "ICU" in client.get(f"{PAGE}{run.pk}/samples/?map-name=0", **HX).content.decode()
    assert client.post(f"{PAGE}{run.pk}/columns/", {"map-name": "0", "map-code": "1", "map-status": "2"}).status_code == 302
    drive(client, run)
    body = client.get(f"{PAGE}{run.pk}/").content.decode()
    assert "Import adds and updates 3 rows" in body  # the other facility's ICU is not "already here"
    assert len(csv_rows(client.get(f"{PAGE}{run.pk}/notes.csv"))) == 4
    assert client.post(f"{PAGE}{run.pk}/import/").status_code == 302
    drive(client, run)
    assert "3 added, 2 skipped" in client.get(PAGE).content.decode()
    with tenant_context(kim.tenant):
        assert sorted(Department.objects.values_list("name", flat=True)) == ["ICU", "OR", "PACU"]
