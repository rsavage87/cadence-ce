"""
The technicians import (slice 23, apps/imports/kinds/technicians.py) and the services it writes through
(apps.credentials.services.create_technician, update_technician): names matched in any letter case and in either order, a blank
cell keeping what is here, two technicians with one name a skip, former staff inactive, a linked account kept, values it cannot
read noted rather than defaulted silently, and a re-run that changes nothing.
"""
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from pg_helpers import as_app_role, needs_postgres

from apps.credentials import services as credentials
from apps.credentials.models import Technician
from apps.imports import services
from apps.imports.kinds.technicians import display_name
from apps.imports.models import ImportRun
from apps.tenants.context import tenant_context

HEADER = "Name,Title,Certification,Weekly hours,Active"


def csv_bytes(*lines):
    return ("\r\n".join(lines) + "\r\n").encode()


def run_through(run, user):
    run = services.process(run, user)
    while run.status in (ImportRun.Status.CHECKING, ImportRun.Status.IMPORTING):
        run = services.process(run, user)
    return run


def checked(user, *lines):
    run = services.upload(user, "technicians", "technicians.csv", csv_bytes(*lines))
    return run_through(services.confirm_columns(run, user, services.mapping_of(run)), user)


def imported(user, *lines):
    return run_through(services.start_import(checked(user, *lines), user), user)


def notes(run) -> dict:
    return {line: (outcome, row_notes) for line, key, outcome, row_notes in run.results}


def test_technicians_are_matched_in_either_order_updated_and_added(ctx, make_user, techs):
    kim = make_user("director")
    lines = (HEADER, '"Whitfield, Dana",,CBET,36,yes', "tom okafor,BMET II,,,", '"Reyes,  Ana",BMET I,,24,no', "Lee   Park,,,,")
    run = checked(kim, *lines)
    assert run.status == "checked" and run.counts == {"update": 2, "create": 2} and run.results == []
    assert Technician.objects.count() == 2 and techs["dana"].certification == ""  # the check changed nothing
    run = services.start_import(run, kim)
    run = run_through(run, kim)
    assert run.status == "imported" and run.counts == {"update": 2, "create": 2}
    dana, tom = (Technician.objects.get(pk=techs[k].pk) for k in ("dana", "tom"))
    assert (dana.name, dana.title, dana.certification, dana.weekly_capacity_hours) == ("Dana Whitfield", "Lead BMET", "CBET", Decimal("36.0"))
    assert (tom.name, tom.title) == ("Tom Okafor", "BMET II")  # the name in the file never renames
    ana, lee = Technician.objects.get(name="Ana Reyes"), Technician.objects.get(name="Lee Park")  # written First Last
    assert (ana.title, ana.weekly_capacity_hours, ana.is_active, ana.user) == ("BMET I", Decimal("24.0"), False, None)
    assert (lee.weekly_capacity_hours, lee.is_active) == (Decimal("32.0"), True)
    assert run.summary["created"] == {"Technicians": ["Ana Reyes", "Lee Park"]}
    assert run.summary["totals"] == {"Changes": {"Certification": "1", "Weekly hours": "1", "Title": "1"},
                                     "New technicians": {"inactive": "1", "active": "1"}}
    assert run.summary["values"] == {"Active": {"yes": ["Active", 1], "no": ["Inactive", 1]}}
    again = imported(kim, *lines)  # a re-run is safe: nothing doubles, nothing changes
    assert again.counts == {"unchanged": 4} and Technician.objects.count() == 4


def test_a_technician_stored_last_first_is_found_by_first_last(ctx, make_user):
    kim = make_user("director")
    stored = Technician.objects.create(name="Okafor, Tom")
    run = imported(kim, "Technician,Title", "Tom Okafor,BMET II")
    stored.refresh_from_db()
    assert run.counts == {"update": 1} and stored.title == "BMET II" and stored.name == "Okafor, Tom"


def test_a_name_two_technicians_share_is_skipped_and_the_file_names_each_once(ctx, make_user):
    kim = make_user("director")
    Technician.objects.create(name="Dana Whitfield")
    Technician.objects.create(name="Whitfield, Dana")
    run = imported(kim, "Name,Title", "dana whitfield,BMET", "Sam Ito,BMET", '"Ito, Sam",BMET II')
    assert run.counts == {"skip": 2, "create": 1}
    assert notes(run) == {2: ("skip", ["Two technicians have this name"]),
                          4: ("skip", ["Name Ito, Sam is also on line 3: one row per technician"])}
    assert Technician.objects.get(name="Sam Ito").title == "BMET"
    assert not Technician.objects.filter(title="BMET").exclude(name="Sam Ito").exists()


def test_values_it_cannot_read_are_noted_never_defaulted_silently(ctx, make_user, techs):
    kim = make_user("director")
    run = imported(kim, "Name,Weekly hours,Active", "Dana Whitfield,ninety,maybe", "New Person,81,maybe", "Former Tech,8,Terminated",
                   "Part Timer,-1,")
    assert run.counts == {"unchanged": 1, "create": 3}
    assert notes(run) == {
        2: ("unchanged", ["Weekly hours not read (0 to 80): left as it was", "Active not read: left as it was"]),
        3: ("create", ["Weekly hours not read (0 to 80): 32 used", "Active not read: imported as active"]),
        5: ("create", ["Weekly hours not read (0 to 80): 32 used"]),
    }
    dana = Technician.objects.get(pk=techs["dana"].pk)
    assert dana.weekly_capacity_hours == Decimal("32.0") and dana.is_active
    new, former = Technician.objects.get(name="New Person"), Technician.objects.get(name="Former Tech")
    assert (new.weekly_capacity_hours, new.is_active) == (Decimal("32.0"), True)
    assert (former.weekly_capacity_hours, former.is_active) == (Decimal("8.0"), False)
    assert run.summary["values"] == {"Active": {"Terminated": ["Inactive", 1]}}


def test_a_technician_linked_to_an_account_keeps_the_link(ctx, make_user):
    kim, tech_user = make_user("director"), make_user("technician")
    linked = Technician.objects.create(name="Technician User", user=tech_user, title="BMET I")
    run = imported(kim, HEADER, '"User, Technician",BMET II,,,no')
    linked.refresh_from_db()
    assert run.counts == {"update": 1} and (linked.user, linked.title, linked.is_active) == (tech_user, "BMET II", False)


def test_a_technician_in_another_facility_is_never_matched(ctx, make_user, other_tenant):
    kim = make_user("director")
    with tenant_context(other_tenant):
        theirs = Technician.objects.create(name="Dana Whitfield", title="Theirs")
    run = imported(kim, "Name,Title", "Dana Whitfield,Ours")
    assert run.counts == {"create": 1} and Technician.objects.get().title == "Ours"
    assert Technician.unscoped.get(pk=theirs.pk).title == "Theirs"  # unscoped: reading the other facility's row in a test


def test_importing_technicians_needs_users_edit(ctx, make_user):
    with pytest.raises(PermissionDenied):
        services.upload(make_user("manager"), "technicians", "t.csv", csv_bytes("Name", "Dana Whitfield"))  # Users View only
    assert services.upload(make_user("director"), "technicians", "t.csv", csv_bytes("Name", "Dana Whitfield")).status == "mapping"


def test_a_value_too_long_for_its_column_skips_the_row(ctx, make_user):
    run = imported(make_user("director"), "Name,Title,Certification", f"Dana Whitfield,{'x' * 81},", f"Tom Okafor,,{'c' * 81}", "Lee Park,BMET,")
    assert run.counts == {"skip": 2, "create": 1}
    assert [n for _, n in notes(run).values()] == [["Title is longer than 80 characters"], ["Certification is longer than 80 characters"]]


def test_names_are_written_first_last():
    assert [display_name(n) for n in ("Whitfield, Dana", "  Dana   Whitfield ", "Whitfield,Dana M.", "Smith, John, Jr.", "Cher", ",Cher")] == [
        "Dana Whitfield", "Dana Whitfield", "Dana M. Whitfield", "Smith, John, Jr.", "Cher", ",Cher"]


# --- the services ------------------------------------------------------------------------------------------------------------------

def test_create_technician_checks_what_it_writes(ctx):
    t = credentials.create_technician(name="  Ana   Reyes ", title=" BMET I ", weekly_capacity_hours="32.25")
    assert (t.name, t.title, t.certification, t.weekly_capacity_hours, t.is_active, t.user) == ("Ana Reyes", "BMET I", "", Decimal("32.3"), True, None)
    assert credentials.create_technician(name="Lee Park", is_active=False).weekly_capacity_hours == Decimal("32")
    hours = "weekly_capacity_hours"
    for fields, field in (({"name": " "}, "name"), ({"name": "x" * 121}, "name"), ({"name": "A", "title": "t" * 81}, "title"),
                          ({"name": "A", "certification": "c" * 81}, "certification"), ({"name": "A", hours: 81}, hours),
                          ({"name": "A", hours: -1}, hours), ({"name": "A", hours: "lots"}, hours), ({"name": "A", hours: "NaN"}, hours),
                          ({"name": "A", "is_active": "no"}, "is_active")):
        with pytest.raises(ValidationError) as e:
            credentials.create_technician(**fields)
        assert list(e.value.message_dict) == [field], fields
    assert Technician.objects.count() == 2


def test_update_technician_writes_only_what_it_is_given(ctx, make_user):
    user = make_user("technician")
    t = Technician.objects.create(name="Technician User", user=user, title="BMET I", certification="CBET")
    with pytest.raises(ValidationError, match="Unknown field"):
        credentials.update_technician(t, user=None)
    credentials.update_technician(t, title="BMET II", weekly_capacity_hours=0, is_active=False)
    t = Technician.objects.get(pk=t.pk)
    assert (t.user, t.title, t.certification, t.weekly_capacity_hours, t.is_active) == (user, "BMET II", "CBET", Decimal("0.0"), False)
    with pytest.raises(ValidationError):
        credentials.update_technician(t, weekly_capacity_hours=80.1)
    assert Technician.objects.get(pk=t.pk).weekly_capacity_hours == Decimal("0.0")


# --- under the policies --------------------------------------------------------------------------------------------------------------

@needs_postgres
def test_a_technicians_import_under_the_policies(ctx, make_user, techs):
    kim = make_user("director")
    as_app_role()
    run = checked(kim, HEADER, '"Whitfield, Dana",,CBET,,', f"Lee Park,{'x' * 81},,,", "Ana Reyes,BMET I,,24,no")
    assert run.counts == {"update": 1, "skip": 1, "create": 1} and not Technician.objects.filter(name="Ana Reyes").exists()
    run = run_through(services.start_import(run, kim), kim)  # the long title never reaches PostgreSQL, which would refuse it
    assert run.counts == {"update": 1, "skip": 1, "create": 1} and notes(run)[3] == ("skip", ["Title is longer than 80 characters"])
    assert Technician.objects.get(pk=techs["dana"].pk).certification == "CBET" and not Technician.objects.get(name="Ana Reyes").is_active
