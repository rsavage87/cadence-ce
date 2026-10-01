"""PM procedures (slice 14, part D): apps.pm.procedures. Every rule for writing and revising a procedure, the checklist's line format
and its round trip, choosing a model's procedure through equipment.services.update_device_model, the history each change leaves, and
tenant isolation (codes are unique per facility; another facility's procedure or model is refused)."""
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from apps.accounts.models import create_default_roles
from apps.equipment.models import DeviceModel
from apps.pm import procedures as svc
from apps.pm.models import PmProcedure
from apps.pm.schedule import DEFAULT_PM_HOURS, pm_hours
from apps.tenants.context import tenant_context

CHECKLIST = ["Visual inspection and cleaning", {"text": "Ground resistance", "measure": "Ω, limit 0.3"}, {"text": "Battery runtime", "measure": True}]


def make(**over) -> PmProcedure:
    fields = {"code": "HA-G5-PM6", "name": "ICU ventilator 6-month PM", "source_kind": PmProcedure.SourceKind.OEM, "source_reference": "G5 Service Manual",
              "source_url": "https://docs.example.com/g5.pdf", "revision": "Rev K", "estimated_hours": "1.5", "checklist": CHECKLIST}
    fields.update(over)
    return svc.create_procedure(**fields)


def error(exc_info, field) -> str:
    return " ".join(exc_info.value.message_dict.get(field, []))


@pytest.fixture
def theirs(tenant, other_tenant):
    """Another facility's procedure (code HA-G5-PM6, as ours will be) and a model using it."""
    create_default_roles(other_tenant)
    with tenant_context(other_tenant):
        proc = PmProcedure.objects.create(code="HA-G5-PM6", name="Theirs", estimated_hours=Decimal("9"), checklist=["x"])
        dm = DeviceModel.objects.create(manufacturer="X", model="Y", description="Their vent", category="Ventilators", pm_procedure=proc)
    return {"procedure": proc, "model": dm}


# --- creating ----------------------------------------------------------------------------------------------------------------------

def test_create_stores_the_cleaned_fields_and_records_history(ctx, make_user):
    tech = make_user("technician")
    p = svc.create_procedure(code="  HA-G5-PM6 ", name="  ICU   ventilator 6-month PM ", source_kind="ecri", source_reference=" IPM  454 ",
                             source_url=" https://docs.example.com/g5.pdf ", revision=" Rev K ", estimated_hours="1.50",
                             checklist=[" Visual   inspection ", {"text": "Leakage", "measure": " µA,  limit 100 "}, {"text": "Alarms", "measure": True},
                                        {"text": "Flow", "measure": None}, {"text": "Door", "measure": ""}],
                             by=tech)
    p.refresh_from_db()
    assert (p.code, p.name, p.source_kind, p.source_reference, p.source_url, p.revision, p.estimated_hours) == (
        "HA-G5-PM6", "ICU ventilator 6-month PM", "ecri", "IPM 454", "https://docs.example.com/g5.pdf", "Rev K", Decimal("1.50"))
    # a step is its text unless something is recorded
    assert p.checklist == ["Visual inspection", {"text": "Leakage", "measure": "µA, limit 100"}, {"text": "Alarms", "measure": True}, "Flow", "Door"]
    h = p.history.get()
    assert h.history_change_reason == "Added" and h.history_user == tech and h.history_type == "+"


def test_create_takes_the_editors_text_too(ctx):
    p = make(checklist="Inspect\n\n  Ground resistance |  Ω, limit 0.3 \nBattery runtime |\n")
    assert p.checklist == ["Inspect", CHECKLIST[1], CHECKLIST[2]]


def test_optional_fields_may_be_blank(ctx):
    p = make(source_reference="", source_url="  ", revision="")
    assert (p.source_reference, p.source_url, p.revision) == ("", "", "")


@pytest.mark.parametrize("code, message", [
    ("", "Enter a short code for the procedure, e.g. HA-G5-PM6."),
    ("   ", "Enter a short code for the procedure, e.g. HA-G5-PM6."),
    ("HA G5 PM6", "A code has no spaces. Use hyphens, e.g. HA-G5-PM6."),
    ("HA-G5\tPM6", "A code has no spaces. Use hyphens, e.g. HA-G5-PM6."),
    ("P" * 41, "Keep the code to 40 characters."),
    ("PM#12", "Use letters, digits, and - . _ / only, starting with a letter or digit."),
    ("-PM12", "Use letters, digits, and - . _ / only, starting with a letter or digit."),
    ("PM<12>", "Use letters, digits, and - . _ / only, starting with a letter or digit."),
    (None, "Enter a short code for the procedure, e.g. HA-G5-PM6."),
])
def test_code_rules(ctx, code, message):
    with pytest.raises(ValidationError) as e:
        make(code=code)
    assert error(e, "code") == message
    assert not PmProcedure.objects.exists()


@pytest.mark.parametrize("code", ["P" * 40, "SM/2024.1_a-B", "9-PM"])
def test_codes_that_are_fine(ctx, code):
    assert make(code=code).code == code


def test_codes_are_unique_in_the_facility_in_any_letter_case(ctx):
    make()
    with pytest.raises(ValidationError) as e:
        make(code="ha-g5-pm6", name="Another")
    assert error(e, "code") == "ha-g5-pm6 is already the code of another procedure here."
    assert PmProcedure.objects.count() == 1


def test_two_facilities_may_share_a_code(ctx, theirs):
    p = make()
    assert p.code == theirs["procedure"].code and PmProcedure.unscoped.filter(code="HA-G5-PM6").count() == 2  # unscoped: both facilities
    assert list(PmProcedure.objects.all()) == [p]  # each facility sees its own


@pytest.mark.parametrize("over, field, message", [
    ({"name": ""}, "name", "Enter the procedure's name."),
    ({"name": "   "}, "name", "Enter the procedure's name."),
    ({"name": "N" * 201}, "name", "Keep the name to 200 characters."),
    ({"name": "PM\x00"}, "name", "Remove the invisible control character from this text."),
    ({"source_kind": "manual"}, "source_kind", "Choose where the procedure comes from."),
    ({"source_kind": ""}, "source_kind", "Choose where the procedure comes from."),
    ({"source_reference": "R" * 201}, "source_reference", "Keep the reference to 200 characters."),
    ({"revision": "R" * 41}, "revision", "Keep the revision to 40 characters."),
    ({"source_url": "javascript:alert(1)"}, "source_url", "Enter a web address that starts with https:// (or http://)."),
    ({"source_url": "ftp://files.example.com/g5.pdf"}, "source_url", "Enter a web address that starts with https:// (or http://)."),
    ({"source_url": "docs.example.com/g5.pdf"}, "source_url", "Enter a web address that starts with https:// (or http://)."),
    ({"source_url": "https://"}, "source_url", "Enter a complete web address, e.g. https://example.com/service-manual.pdf."),
    ({"source_url": "https://docs.example.com/a b.pdf"}, "source_url", "Enter a complete web address, e.g. https://example.com/service-manual.pdf."),
    ({"source_url": "https://example.com/" + "a" * 181}, "source_url", "Keep the link to 200 characters."),
])
def test_field_rules(ctx, over, field, message):
    with pytest.raises(ValidationError) as e:
        make(**over)
    assert error(e, field) == message
    assert not PmProcedure.objects.exists()


def test_too_long_is_refused_never_cut(ctx):
    """What was typed is what is saved: 200 characters fit, 201 are refused rather than trimmed to 200."""
    assert make(name="N" * 200).name == "N" * 200
    with pytest.raises(ValidationError):
        make(code="P2", name="N" * 201)


def test_http_links_are_accepted(ctx):
    assert make(source_url="http://intranet.example.org/manuals/g5").source_url == "http://intranet.example.org/manuals/g5"


@pytest.mark.parametrize("hours, message", [
    ("", "Enter the estimated hours per PM, e.g. 1.5."),
    (None, "Enter the estimated hours per PM, e.g. 1.5."),
    ("abc", "Enter the hours as a number, e.g. 1.5."),
    ("1,5", "Enter the hours as a number, e.g. 1.5."),
    ("NaN", "Enter the hours as a number, e.g. 1.5."),
    ("Infinity", "Enter the hours as a number, e.g. 1.5."),
    (True, "Enter the hours as a number, e.g. 1.5."),
    ("1.555", "Use at most 2 decimal places."),
    ("0.125", "Use at most 2 decimal places."),
    ("0.09", "Estimated hours are 0.1 to 40."),
    ("0", "Estimated hours are 0.1 to 40."),
    ("-1", "Estimated hours are 0.1 to 40."),
    ("40.01", "Estimated hours are 0.1 to 40."),
])
def test_hours_rules(ctx, hours, message):
    with pytest.raises(ValidationError) as e:
        make(estimated_hours=hours)
    assert error(e, "estimated_hours") == message


@pytest.mark.parametrize("hours, stored", [("0.1", Decimal("0.10")), ("40", Decimal("40")), (" 2.50 ", Decimal("2.5")), (1.25, Decimal("1.25")),
                                           (Decimal("0.75"), Decimal("0.75")), ("1.500", Decimal("1.5")), (3, Decimal("3"))])
def test_hours_in_range_are_kept_as_typed(ctx, hours, stored):
    p = make(estimated_hours=hours)
    p.refresh_from_db()
    assert p.estimated_hours == stored


@pytest.mark.parametrize("checklist, message", [
    ([], "Add at least one step, one per line."),
    ("", "Add at least one step, one per line."),
    ("\n  \n", "Add at least one step, one per line."),
    (None, "Write the checklist one step per line."),
    ({"text": "x"}, "Write the checklist one step per line."),
    (["Inspect", ""], "Step 2: write the step."),
    (["Inspect", {"text": "  ", "measure": "V"}], "Step 2: write the step before the |."),
    ("Inspect\n | V", "Step 2: write the step before the |."),
    ([{"text": "a | b"}], "Step 1: a step cannot contain |, which separates the step from what to record."),
    (["a | b"], "Step 1: a step cannot contain |, which separates the step from what to record."),
    (["S" * 301], "Step 1: keep each step to 300 characters."),
    ([{"text": "Leakage", "measure": "M" * 101}], "Step 1: keep what to record to 100 characters."),
    ([{"text": "Leakage", "measure": "µA", "limit": 100}], "Step 1: a step has its text and, for a reading, what to record."),
    ([5], "Step 1: write each step as a line of text."),
    ([{"text": 5}], "Step 1: write each step as a line of text."),
    ([{"text": "Leakage", "measure": ["µA"]}], "Step 1: say what to record as text, e.g. µA, limit 100."),
    (["ok", "Bad\x00step"], "Step 2: remove the invisible control character from this step."),
    ([f"Step {n}" for n in range(61)], "Keep the checklist to 60 steps (this one has 61)."),
])
def test_checklist_rules(ctx, checklist, message):
    with pytest.raises(ValidationError) as e:
        make(checklist=checklist)
    assert error(e, "checklist") == message


def test_checklist_limits_and_reading_shapes(ctx):
    assert len(make(checklist=[f"Step {n}" for n in range(60)]).checklist) == 60
    p = make(code="P2", checklist=["S" * 300, {"text": "Leakage", "measure": "M" * 100}, {"text": "Limit", "measure": 0.3}, {"text": "Zero", "measure": 0},
                                  {"text": "No reading", "measure": False}, {"text": "Blank", "measure": "  "}])
    assert p.checklist == ["S" * 300, {"text": "Leakage", "measure": "M" * 100}, {"text": "Limit", "measure": "0.3"}, {"text": "Zero", "measure": "0"},
                           "No reading", "Blank"]


def test_every_problem_is_reported_at_once_keyed_by_field(ctx):
    with pytest.raises(ValidationError) as e:
        make(code="", name="", estimated_hours="x", checklist=[], source_url="nope")
    assert set(e.value.message_dict) == {"code", "name", "estimated_hours", "checklist", "source_url"}


# --- the line format ------------------------------------------------------------------------------------------------------

def test_the_line_format_round_trips_every_checklist_the_rules_accept(ctx):
    checklist = ["Visual inspection", {"text": "Ground resistance", "measure": "Ω, limit 0.3"}, {"text": "Battery runtime", "measure": True},
                 {"text": "Pressure", "measure": "a | b"}, "Apply PM sticker"]
    p = make(checklist=checklist)
    text = svc.checklist_text(p.checklist)
    assert text == ("Visual inspection\nGround resistance | Ω, limit 0.3\nBattery runtime |\nPressure | a | b\nApply PM sticker")
    assert svc.parse_checklist(text) == checklist
    # and saving the editor's text unchanged stores exactly the same checklist
    svc.update_procedure(p, checklist=text)
    assert p.checklist == checklist and p.history.count() == 1


def test_parse_skips_blank_lines_collapses_spaces_and_splits_at_the_first_bar():
    assert svc.parse_checklist("  Inspect   case \n\n\tLeakage|µA ,  limit 100\nAlarm test | \r\nA | b | c") == [
        "Inspect case", {"text": "Leakage", "measure": "µA , limit 100"}, {"text": "Alarm test", "measure": True}, {"text": "A", "measure": "b | c"}]
    assert svc.parse_checklist("") == [] and svc.parse_checklist(None) == []


def test_checklist_text_shows_older_shapes_as_the_print_does():
    """Admin or seed data may hold a string checklist, a dict without "text", numbers, or False: the editor shows them all."""
    assert svc.checklist_text("Inspect\n\nTest alarms") == "Inspect\nTest alarms"
    assert svc.checklist_text([{"step": "Leak test", "note": "x"}, {"text": "Limit", "measure": 0}, {"text": "Off", "measure": False}, 7]) == (
        "Leak test; x\nLimit | 0\nOff\n7")
    assert svc.checklist_text([]) == "" and svc.checklist_text(None) == ""


# --- changing ---------------------------------------------------------------------------------------------------------------

def test_update_saves_only_what_changed_with_a_reason(ctx, make_user):
    p = make()
    tech = make_user("technician")
    svc.update_procedure(p, by=tech, name="ICU ventilator 6-month PM", estimated_hours="2", checklist=CHECKLIST + ["Apply PM sticker"])
    p.refresh_from_db()
    assert p.estimated_hours == Decimal("2") and p.checklist[-1] == "Apply PM sticker"
    h = p.history.first()
    assert h.history_change_reason == "Edited: hours, checklist" and h.history_user == tech
    svc.update_procedure(p, name=" ICU  ventilator 6-month PM ", estimated_hours="2.00")  # the same after cleaning: nothing saved
    assert p.history.count() == 2


def test_update_checks_only_the_fields_given(ctx):
    p = make()
    svc.update_procedure(p, revision="Rev L")
    assert p.revision == "Rev L" and p.history.first().history_change_reason == "Edited: revision"


def test_update_refuses_unknown_fields(ctx):
    p = make()
    with pytest.raises(ValidationError) as e:
        svc.update_procedure(p, tenant_id=None, name="x")
    assert e.value.messages == ["These cannot be changed here: tenant_id."]


def test_an_unchanged_older_code_never_blocks_an_edit(ctx):
    """A code saved by the admin before these rules (spaces) stays as it is while the rest is edited."""
    p = PmProcedure.objects.create(code="OLD CODE", name="Old", estimated_hours=1, checklist=["x"])
    svc.update_procedure(p, code=" OLD CODE ", name="Renamed")
    assert (p.code, p.name) == ("OLD CODE", "Renamed")
    with pytest.raises(ValidationError) as e:
        svc.update_procedure(p, code="OLD CODE 2")
    assert error(e, "code") == "A code has no spaces. Use hyphens, e.g. HA-G5-PM6."


def test_update_code_stays_unique_in_any_case_but_may_change_its_own_case(ctx):
    p = make()
    other = make(code="BD-ALARIS-PM12", name="Pump PM")
    with pytest.raises(ValidationError) as e:
        svc.update_procedure(other, code="ha-G5-pm6")
    assert error(e, "code") == "ha-G5-pm6 is already the code of another procedure here."
    svc.update_procedure(p, code="ha-g5-pm6")
    assert PmProcedure.objects.get(pk=p.pk).code == "ha-g5-pm6"


def test_a_code_taken_at_the_same_moment_is_refused_by_the_database(ctx, monkeypatch):
    """The check and the save are not atomic: the unique constraint is the backstop, and the object goes back to what is stored."""
    make()
    other = make(code="P2")
    monkeypatch.setattr(svc, "_code_taken", lambda code, exclude=None: "")
    with pytest.raises(ValidationError) as e:
        make(code="HA-G5-PM6", name="Duplicate")
    assert error(e, "code") == "HA-G5-PM6 is already the code of another procedure here."
    with pytest.raises(ValidationError):
        svc.update_procedure(other, code="HA-G5-PM6", name="Changed")
    assert (other.code, other.name) == ("P2", "ICU ventilator 6-month PM")
    assert PmProcedure.objects.count() == 2


def test_another_facilitys_procedure_cannot_be_changed(ctx, theirs):
    with pytest.raises(ValidationError) as e:
        svc.update_procedure(theirs["procedure"], name="Mine now")
    assert error(e, "procedure") == "Choose a procedure from this facility."
    assert PmProcedure.unscoped.get(pk=theirs["procedure"].pk).name == "Theirs"  # unscoped: reading the other facility's row


# --- a model's procedure --------------------------------------------------------------------------------------------------

def test_set_model_procedure_goes_through_the_model_service_with_history(ctx, vent_model, make_user):
    p = make()
    tech = make_user("technician")
    assert pm_hours(vent_model) == DEFAULT_PM_HOURS
    svc.set_model_procedure(vent_model, p, by=tech)
    vent_model = DeviceModel.objects.get(pk=vent_model.pk)
    assert vent_model.pm_procedure == p and pm_hours(vent_model) == Decimal("1.5")
    h = vent_model.history.first()
    assert h.history_change_reason == "PM procedure: HA-G5-PM6" and h.history_user == tech
    assert list(svc.models_using(p)) == [vent_model]

    svc.set_model_procedure(vent_model, p)  # the same again: nothing saved
    assert vent_model.history.count() == 2
    svc.set_model_procedure(vent_model, None, by=tech)
    vent_model = DeviceModel.objects.get(pk=vent_model.pk)
    assert vent_model.pm_procedure is None and pm_hours(vent_model) == DEFAULT_PM_HOURS
    assert vent_model.history.first().history_change_reason == "PM procedure removed"
    assert not hasattr(vent_model, "_change_reason")


def test_the_reason_never_reaches_a_later_save(ctx, vent_model):
    svc.set_model_procedure(vent_model, make())
    vent_model.description = "Ventilator"
    vent_model.save()
    assert vent_model.history.first().history_change_reason is None


def test_a_change_to_a_shared_procedure_reaches_every_model_using_it(ctx, vent_model, pump_model):
    p = make()
    svc.set_model_procedure(vent_model, p)
    svc.set_model_procedure(pump_model, p)
    svc.update_procedure(p, estimated_hours="3.25")
    assert [pm_hours(DeviceModel.objects.select_related("pm_procedure").get(pk=m.pk)) for m in (vent_model, pump_model)] == [Decimal("3.25")] * 2
    assert list(svc.models_using(p)) == [pump_model, vent_model]  # by name: BD before Hamilton


def test_another_facilitys_procedure_cannot_be_chosen(ctx, vent_model, theirs):
    with pytest.raises(ValidationError) as e:
        svc.set_model_procedure(vent_model, theirs["procedure"])
    assert "pm_procedure" in e.value.message_dict
    assert DeviceModel.objects.get(pk=vent_model.pk).pm_procedure is None


def test_another_facilitys_model_cannot_be_changed(ctx, theirs):
    p = make()
    with pytest.raises(ValidationError) as e:
        svc.set_model_procedure(theirs["model"], p)
    assert error(e, "device_model") == "Choose a device model from this facility."
    assert DeviceModel.unscoped.get(pk=theirs["model"].pk).pm_procedure_id == theirs["procedure"].pk  # unscoped: the other facility's row


def test_models_using_is_tenant_scoped(ctx, theirs):
    assert list(svc.models_using(theirs["procedure"])) == []
