"""PM procedures in the model drawer (slice 14, part D): the Procedure tab, choosing a model's procedure, New procedure, and Edit
procedure. Permissions are checked on the server for every view, GET and POST (PM View sees the tab; PM Edit changes it); another
facility's model or procedure is a 404; each change answers with the drawer on the Procedure tab, toasts, and fires models-changed."""
import json
import re
from datetime import date, timedelta
from decimal import Decimal

import pytest

from apps.accounts.models import create_default_roles
from apps.equipment.models import Asset, Department, DeviceModel
from apps.pm import procedures as svc
from apps.pm.models import PmProcedure
from apps.tenants.context import tenant_context
from apps.web.forms_procedures import LAYOUT
from apps.workorders.models import WoType
from apps.workorders.services import create_work_order

HX = {"HTTP_HX_REQUEST": "true"}
CHECKLIST = ["Visual inspection and cleaning", {"text": "Ground resistance", "measure": "Ω, limit 0.3"}, {"text": "Battery runtime", "measure": True}]


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug):
        user = make_user(role_slug)
        client.force_login(user)
        return user

    return _as


@pytest.fixture
def proc(ctx, vent_model, pump_model):
    """HA-G5-PM6, used by the ventilator and the pump."""
    p = svc.create_procedure(code="HA-G5-PM6", name="ICU ventilator 6-month PM", source_kind="oem", source_reference="G5 Service Manual",
                             source_url="https://docs.example.com/g5.pdf", revision="Rev K", estimated_hours="1.5", checklist=CHECKLIST)
    for m in (vent_model, pump_model):
        svc.set_model_procedure(m, p)
    return p


@pytest.fixture
def spare(ctx):
    return svc.create_procedure(code="ECRI-454", name="Generic infusion pump IPM", source_kind="ecri", estimated_hours="0.75", checklist=["Inspect"])


@pytest.fixture
def theirs(tenant, other_tenant):
    create_default_roles(other_tenant)
    with tenant_context(other_tenant):
        p = PmProcedure.objects.create(code="THEIR-PM", name="Theirs", estimated_hours=Decimal("9"), checklist=["x"])
        dm = DeviceModel.objects.create(manufacturer="X", model="Y", description="Their pump", category="C", pm_procedure=p)
    return {"procedure": p, "model": dm}


def tab_url(dm):
    return f"/pm/models/{dm.pk}/?tab=procedure"


def choose_url(dm):
    return f"/pm/models/{dm.pk}/procedure/"


def new_url(dm=None):
    return "/pm/procedures/new/" + (f"?model={dm.pk}" if dm else "")


def edit_url(p, dm=None):
    return f"/pm/procedures/{p.pk}/edit/" + (f"?model={dm.pk}" if dm else "")


def triggers(r, header="HX-Trigger") -> dict:
    return json.loads(r[header]) if header in r else {}


def field_error(body: str, field: str) -> str:
    marker = f'id="pr-{field}_error">'
    return body.split(marker, 1)[1].split("</span>", 1)[0] if marker in body else ""


def form_post(**over) -> dict:
    data = {"code": "BD-ALARIS-PM12", "revision": "Rev F", "name": "Infusion pump 12-month PM", "source_kind": "oem", "estimated_hours": "0.5",
            "source_reference": "Alaris service manual", "source_url": "https://docs.example.com/alaris.pdf",
            "checklist": "Visual inspection\nFlow rate accuracy | ±5% at 25 mL/h\nOcclusion pressure test |"}
    data.update(over)
    return data


def on_procedure_tab(body: str) -> bool:
    return 'class="tab active" role="tab" aria-selected="true"' in body and re.search(r'class="tab active"[^>]*\?tab=procedure"', body) is not None


# --- the tab ------------------------------------------------------------------------------------------------------------------------

def test_the_tab_shows_the_procedure_its_checklist_and_who_uses_it(client, signed_in, proc, vent_model):
    signed_in("analyst")  # PM View
    r = client.get(tab_url(vent_model), **HX)
    body = r.content.decode()
    assert r.status_code == 200 and on_procedure_tab(body)
    assert "<h3>PM procedure <span>HA-G5-PM6 · 1.5 h estimated</span></h3>" in body and "ICU ventilator 6-month PM" in body
    assert "<dd>OEM service manual</dd>" in body and "<dd>Rev K</dd>" in body and "<dd>G5 Service Manual</dd>" in body
    assert '<a class="link" href="https://docs.example.com/g5.pdf" target="_blank" rel="noopener noreferrer">' in body
    assert "<dd>1.5 h per PM</dd>" in body
    assert "<h3>Checklist <span>3 steps</span></h3>" in body
    rows = re.findall(r'<tr><td class="num muted">(\d+)</td><td style="white-space:normal">(.*?)</td><td style="white-space:normal">(.*?)</td></tr>', body)
    assert rows == [("1", "Visual inspection and cleaning", '<span class="muted">—</span>'), ("2", "Ground resistance", "Ω, limit 0.3"),
                    ("3", "Battery runtime", "A reading")]
    assert "<h3>Used by <span>2 models</span></h3>" in body and "This model, BD Alaris 8015 PCU." in body
    assert "changes the PM for all of them" in body


def test_view_only_sees_no_actions(client, signed_in, proc, spare, vent_model):
    signed_in("analyst")
    body = client.get(tab_url(vent_model), **HX).content.decode()
    for action in ("Edit procedure", "New procedure", "Use for this model", "Change procedure", "/procedure/", "/procedures/"):
        assert action not in body


def test_pm_edit_sees_the_actions(client, signed_in, proc, spare, vent_model):
    signed_in("technician")  # PM Edit
    body = client.get(tab_url(vent_model), **HX).content.decode()
    assert f'hx-get="{edit_url(proc, vent_model)}" hx-target="#modal-card">Edit procedure</button>' in body
    assert f'hx-get="{new_url(vent_model)}" hx-target="#modal-card">New procedure</button>' in body
    assert f'hx-post="{choose_url(vent_model)}" hx-target="#drawer"' in body
    assert '<option value="">No procedure (1 h estimate)</option>' in body
    assert f'<option value="{proc.pk}" selected>HA-G5-PM6 · ICU ventilator 6-month PM · 1.5 h</option>' in body
    assert f'<option value="{spare.pk}">ECRI-454 · Generic infusion pump IPM · 0.75 h</option>' in body
    assert "<h3>Change procedure <span>2 in the library</span></h3>" in body


def test_without_a_procedure_the_tab_says_how_its_pms_are_estimated(client, signed_in, vent_model, spare):
    signed_in("technician")
    body = client.get(tab_url(vent_model), **HX).content.decode()
    assert "No PM procedure on file for this model." in body
    assert "A PM for this model is estimated at 1 hour, and its work orders name no procedure." in body
    assert "<h3>Choose a procedure <span>1 in the library</span></h3>" in body and '<option value="" selected>' in body
    assert "Edit procedure" not in body and "New procedure" in body


def test_an_empty_library_points_to_new_procedure(client, signed_in, vent_model):
    signed_in("technician")
    body = client.get(tab_url(vent_model), **HX).content.decode()
    assert "The library has no procedures yet. Write the first one with New procedure." in body and "<select" not in body


def test_the_tab_says_open_pm_work_orders_print_a_change(client, signed_in, proc, vent_model, vent):
    create_work_order(asset=vent, type=WoType.PM, priority="high", problem="PM", estimated_hours=Decimal("1.5"))
    signed_in("technician")
    body = client.get(tab_url(vent_model), **HX).content.decode()
    assert "Hours apply to PM work orders created from now on." in body
    assert "so this model's 1 open PM work order will print a change too." in body


def test_only_web_links_become_links(client, signed_in, vent_model):
    vent_model.pm_procedure = PmProcedure.objects.create(code="P1", name="PM", checklist=["x"], source_url="javascript:alert(1)")  # admin data
    vent_model.save()
    signed_in("analyst")
    body = client.get(tab_url(vent_model), **HX).content.decode()
    assert "javascript:" not in body and "<dt>Source document</dt><dd>—</dd>" in body


def test_a_procedure_without_steps_says_so(client, signed_in, vent_model):
    vent_model.pm_procedure = PmProcedure.objects.create(code="P1", name="PM", checklist=[])  # admin data
    vent_model.save()
    signed_in("analyst")
    body = client.get(tab_url(vent_model), **HX).content.decode()
    assert "<h3>Checklist <span>0 steps</span></h3>" in body and "This procedure has no steps on file." in body


def test_many_users_are_counted_after_five(client, signed_in, proc, vent_model):
    for n in range(6):
        svc.set_model_procedure(DeviceModel.objects.create(manufacturer="M", model=f"Model {n}", description="d", category="c"), proc)
    signed_in("analyst")
    body = client.get(tab_url(vent_model), **HX).content.decode()
    assert "<h3>Used by <span>8 models</span></h3>" in body
    assert "This model, BD Alaris 8015 PCU, M Model 0, M Model 1, M Model 2, M Model 3, and 2 more." in body


def test_the_tab_opened_directly_renders_the_pm_page_with_the_drawer(client, signed_in, proc, vent_model):
    signed_in("analyst")
    r = client.get(tab_url(vent_model))
    body = r.content.decode()
    assert r.status_code == 200 and "<html" in body and on_procedure_tab(body) and "HA-G5-PM6 · 1.5 h estimated" in body


# --- permissions -----------------------------------------------------------------------------------------------------------------

def test_view_only_is_refused_every_write(client, signed_in, proc, spare, vent_model):
    signed_in("analyst")
    assert client.post(choose_url(vent_model), {"procedure": str(spare.pk)}, **HX).status_code == 403
    assert client.get(new_url(vent_model), **HX).status_code == 403
    assert client.post(new_url(vent_model), form_post(), **HX).status_code == 403
    assert client.get(edit_url(proc, vent_model), **HX).status_code == 403
    assert client.post(edit_url(proc, vent_model), form_post(code="HA-G5-PM6"), **HX).status_code == 403
    assert DeviceModel.objects.get(pk=vent_model.pk).pm_procedure == proc
    assert PmProcedure.objects.count() == 2 and PmProcedure.objects.get(pk=proc.pk).name == "ICU ventilator 6-month PM"


@pytest.mark.parametrize("role", ["requester", "vendor"])
def test_no_pm_access_is_refused_everything(client, signed_in, proc, spare, vent_model, role):
    signed_in(role)  # PM None
    assert client.get(tab_url(vent_model), **HX).status_code == 403
    assert client.post(choose_url(vent_model), {"procedure": str(spare.pk)}, **HX).status_code == 403
    assert client.get(new_url(vent_model), **HX).status_code == 403
    assert client.post(new_url(vent_model), form_post(), **HX).status_code == 403
    assert client.get(edit_url(proc, vent_model), **HX).status_code == 403
    assert client.post(edit_url(proc, vent_model), form_post(), **HX).status_code == 403
    assert DeviceModel.objects.get(pk=vent_model.pk).pm_procedure == proc and PmProcedure.objects.count() == 2


@pytest.mark.parametrize("role", ["technician", "manager", "director"])
def test_pm_edit_and_up_may_change_procedures(client, signed_in, proc, spare, vent_model, role):
    signed_in(role)
    assert client.post(choose_url(vent_model), {"procedure": str(spare.pk)}, **HX).status_code == 200
    assert DeviceModel.objects.get(pk=vent_model.pk).pm_procedure == spare
    assert client.get(new_url(vent_model), **HX).status_code == 200 and client.get(edit_url(proc, vent_model), **HX).status_code == 200


def test_signed_out_is_sent_to_sign_in(client, proc, vent_model):
    r = client.get(new_url(vent_model), **HX)
    assert r.status_code == 302 and "/login" in r["Location"]


# --- choosing a model's procedure ----------------------------------------------------------------------------------------------

def test_choosing_a_procedure_answers_with_the_drawer_on_the_tab(client, signed_in, vent_model, spare):
    tech = signed_in("technician")
    r = client.post(choose_url(vent_model), {"procedure": str(spare.pk)}, **HX)
    body = r.content.decode()
    assert r.status_code == 200 and on_procedure_tab(body) and "ECRI-454 · 0.75 h estimated" in body and "HX-Retarget" not in r
    t = triggers(r)
    assert "models-changed" in t and t["toast"] == {"value": "Hamilton Medical Hamilton-G5 now uses ECRI-454"}
    vent_model = DeviceModel.objects.get(pk=vent_model.pk)
    assert vent_model.pm_procedure == spare
    h = vent_model.history.first()
    assert h.history_change_reason == "PM procedure: ECRI-454" and h.history_user == tech


def test_choosing_none_removes_it(client, signed_in, proc, vent_model):
    signed_in("technician")
    r = client.post(choose_url(vent_model), {"procedure": ""}, **HX)
    assert DeviceModel.objects.get(pk=vent_model.pk).pm_procedure is None
    assert triggers(r)["toast"] == {"value": "Hamilton Medical Hamilton-G5 has no PM procedure now"} and "models-changed" in triggers(r)
    assert "A PM for this model is estimated at 1 hour" in r.content.decode()
    assert PmProcedure.objects.filter(pk=proc.pk).exists()  # the procedure stays in the library


def test_choosing_the_same_one_changes_nothing(client, signed_in, proc, vent_model):
    signed_in("technician")
    r = client.post(choose_url(vent_model), {"procedure": str(proc.pk)}, **HX)
    t = triggers(r)
    assert r.status_code == 200 and "models-changed" not in t and t["toast"] == {"value": "No change: that is already this model's procedure"}
    assert vent_model.history.count() == 2  # created, then given the procedure by the fixture


def test_choosing_needs_a_post_with_a_value(client, signed_in, proc, vent_model):
    signed_in("technician")
    assert client.get(choose_url(vent_model), **HX).status_code == 405
    assert client.post(choose_url(vent_model), {}, **HX).status_code == 400
    assert client.post(choose_url(vent_model), {"procedure": "not-an-id"}, **HX).status_code == 404
    assert DeviceModel.objects.get(pk=vent_model.pk).pm_procedure == proc


def test_another_facilitys_procedure_cannot_be_chosen(client, signed_in, vent_model, theirs):
    signed_in("technician")
    r = client.post(choose_url(vent_model), {"procedure": str(theirs["procedure"].pk)}, **HX)
    assert r.status_code == 404 and DeviceModel.objects.get(pk=vent_model.pk).pm_procedure is None
    body = client.get(tab_url(vent_model), **HX).content.decode()
    assert "THEIR-PM" not in body


def test_another_facilitys_model_is_not_found(client, signed_in, spare, theirs):
    signed_in("technician")
    assert client.get(tab_url(theirs["model"]), **HX).status_code == 404
    assert client.post(choose_url(theirs["model"]), {"procedure": str(spare.pk)}, **HX).status_code == 404
    assert client.get(new_url(theirs["model"]), **HX).status_code == 404
    assert client.post(new_url(theirs["model"]), form_post(), **HX).status_code == 404
    assert not PmProcedure.objects.filter(code="BD-ALARIS-PM12").exists()
    assert DeviceModel.unscoped.get(pk=theirs["model"].pk).pm_procedure_id == theirs["procedure"].pk  # unscoped: the other facility's row


# --- New procedure ---------------------------------------------------------------------------------------------------------------

def test_new_procedure_modal(client, signed_in, proc, vent_model):
    signed_in("technician")
    r = client.get(new_url(vent_model), **HX)
    body = r.content.decode()
    assert r.status_code == 200 and "<h2>New PM procedure</h2>" in body
    assert f'hx-post="{new_url(vent_model)}" hx-target="#modal-card" novalidate' in body
    assert "Saving makes it the PM procedure for Hamilton Medical Hamilton-G5 in place of HA-G5-PM6." in body
    assert 'value="ICU ventilator 6-month PM"' in body  # the name starts from the model's description and OEM interval
    assert "One step per line, in order (up to 60). For a step that records a reading, add | and what to record" in body
    assert '<option value="oem" selected>OEM service manual</option>' in body
    assert 'aria-describedby="pr-checklist_helptext"' in body and "Add procedure</button>" in body


def test_new_procedure_needs_the_model_it_was_opened_from(client, signed_in):
    signed_in("technician")
    assert client.get(new_url(), **HX).status_code == 400
    assert client.post(new_url(), form_post(), **HX).status_code == 400
    assert client.get("/pm/procedures/new/?model=nope", **HX).status_code == 404
    assert not PmProcedure.objects.exists()


def test_new_procedure_creates_it_and_sets_it_for_the_model(client, signed_in, proc, vent_model):
    tech = signed_in("technician")
    r = client.post(new_url(vent_model), form_post(), **HX)
    body = r.content.decode()
    assert r.status_code == 200 and r["HX-Retarget"] == "#drawer" and on_procedure_tab(body)
    assert "BD-ALARIS-PM12 · 0.5 h estimated" in body
    t = triggers(r)
    assert "models-changed" in t and t["toast"] == {"value": "BD-ALARIS-PM12 added; Hamilton Medical Hamilton-G5 uses it now"}
    assert "modal-close" in triggers(r, "HX-Trigger-After-Settle")
    p = PmProcedure.objects.get(code="BD-ALARIS-PM12")
    assert (p.name, p.revision, p.source_kind, p.estimated_hours, p.source_reference, p.source_url) == (
        "Infusion pump 12-month PM", "Rev F", "oem", Decimal("0.50"), "Alaris service manual", "https://docs.example.com/alaris.pdf")
    assert p.checklist == ["Visual inspection", {"text": "Flow rate accuracy", "measure": "±5% at 25 mL/h"},
                           {"text": "Occlusion pressure test", "measure": True}]
    assert DeviceModel.objects.get(pk=vent_model.pk).pm_procedure == p
    assert p.history.get().history_user == tech
    assert list(svc.models_using(proc).values_list("model", flat=True)) == ["Alaris 8015 PCU"]  # the pump keeps the old one


@pytest.mark.parametrize("over, field, message", [
    ({"code": "ha-g5-pm6"}, "code", "ha-g5-pm6 is already the code of another procedure here."),
    ({"code": "BD ALARIS"}, "code", "A code has no spaces. Use hyphens, e.g. HA-G5-PM6."),
    ({"name": ""}, "name", "Enter the procedure&#x27;s name."),
    ({"source_kind": "manual"}, "source_kind", "Choose where the procedure comes from."),
    ({"estimated_hours": "0.125"}, "estimated_hours", "Use at most 2 decimal places."),
    ({"estimated_hours": "41"}, "estimated_hours", "Estimated hours are 0.1 to 40."),
    ({"source_url": "javascript:alert(1)"}, "source_url", "Enter a web address that starts with https:// (or http://)."),
    ({"checklist": "  \n "}, "checklist", "Add at least one step, one per line."),
    ({"checklist": "Inspect\n| volts"}, "checklist", "Step 2: write the step before the |."),
])
def test_new_procedure_errors_land_on_their_fields(client, signed_in, proc, vent_model, over, field, message):
    signed_in("technician")
    r = client.post(new_url(vent_model), form_post(**over), **HX)
    body = r.content.decode()
    assert r.status_code == 200 and "HX-Retarget" not in r and "HX-Trigger" not in r and "<h2>New PM procedure</h2>" in body
    assert field_error(body, field) == message, body
    assert 'aria-invalid="true"' in body
    assert PmProcedure.objects.count() == 1 and DeviceModel.objects.get(pk=vent_model.pk).pm_procedure == proc


def test_the_first_field_with_an_error_gets_the_focus(client, signed_in, vent_model):
    signed_in("technician")
    body = client.post(new_url(vent_model), form_post(estimated_hours="x", checklist=""), **HX).content.decode()
    assert re.search(r'<input type="text" name="estimated_hours"[^>]*autofocus', body)
    assert not re.search(r'<input type="text" name="code"[^>]*autofocus', body)
    assert LAYOUT == ("code", "revision", "name", "source_kind", "estimated_hours", "source_reference", "source_url", "checklist")


def test_a_procedure_the_model_cannot_take_is_not_left_behind(client, signed_in, vent_model, monkeypatch):
    from django.core.exceptions import ValidationError

    def refuse(*args, **kwargs):
        raise ValidationError({"pm_procedure": "Choose a pm procedure from this facility."})

    monkeypatch.setattr(svc, "set_model_procedure", refuse)
    signed_in("technician")
    r = client.post(new_url(vent_model), form_post(), **HX)
    assert "HX-Retarget" not in r and "Choose a pm procedure from this facility." in r.content.decode()
    assert not PmProcedure.objects.exists()


# --- Edit procedure ---------------------------------------------------------------------------------------------------------------

def test_edit_modal_shows_the_procedure_and_who_it_reaches(client, signed_in, proc, vent_model, vent):
    create_work_order(asset=vent, type=WoType.PM, priority="high", problem="PM", estimated_hours=Decimal("1.5"))
    signed_in("technician")
    r = client.get(edit_url(proc, vent_model), **HX)
    body = r.content.decode()
    assert r.status_code == 200 and "<h2>Edit HA-G5-PM6</h2>" in body
    assert f'hx-post="{edit_url(proc, vent_model)}" hx-target="#modal-card" novalidate' in body
    assert "Used by 2 models: BD Alaris 8015 PCU, Hamilton Medical Hamilton-G5. A change applies to all of them." in body
    assert "New hours apply to PM work orders created from now on." in body
    assert "Printed PM work orders show the checklist as it is when printed, including the 1 open now." in body
    assert 'value="HA-G5-PM6"' in body and 'value="Rev K"' in body and 'value="1.5"' in body and 'value="https://docs.example.com/g5.pdf"' in body
    checklist = re.search(r'<textarea name="checklist"[^>]*>(.*?)</textarea>', body, re.S).group(1)
    assert checklist.strip() == "Visual inspection and cleaning\nGround resistance | Ω, limit 0.3\nBattery runtime |"
    assert "Save changes</button>" in body


def test_saving_the_editor_unchanged_changes_nothing(client, signed_in, proc, vent_model):
    """The round trip: what the editor shows, posted back as it is, is the same procedure (no new history row)."""
    signed_in("technician")
    form = client.get(edit_url(proc, vent_model), **HX).context["form"]
    data = {name: form[name].value() for name in LAYOUT}
    r = client.post(edit_url(proc, vent_model), data, **HX)
    assert r["HX-Retarget"] == "#drawer"
    proc.refresh_from_db()
    assert proc.checklist == CHECKLIST and proc.estimated_hours == Decimal("1.5") and proc.history.count() == 1


def test_editing_saves_and_reaches_every_model(client, signed_in, proc, vent_model, pump_model):
    tech = signed_in("manager")
    post = form_post(code="HA-G5-PM6", name="ICU ventilator 6-month PM", revision="Rev L", estimated_hours="2.25",
                     checklist="Visual inspection and cleaning\nGround resistance | Ω, limit 0.2\nBattery runtime |\nApply PM sticker")
    r = client.post(edit_url(proc, vent_model), post, **HX)
    body = r.content.decode()
    assert r.status_code == 200 and r["HX-Retarget"] == "#drawer" and on_procedure_tab(body)
    t = triggers(r)
    assert "models-changed" in t and t["toast"] == {"value": "HA-G5-PM6 saved for all 2 models using it"}
    assert "modal-close" in triggers(r, "HX-Trigger-After-Settle")
    assert "2.25 h estimated" in body and "Ω, limit 0.2" in body and "<h3>Checklist <span>4 steps</span></h3>" in body
    proc.refresh_from_db()
    assert proc.revision == "Rev L" and proc.estimated_hours == Decimal("2.25") and proc.checklist[-1] == "Apply PM sticker"
    h = proc.history.first()
    assert h.history_user == tech and h.history_change_reason.startswith("Edited: ") and "checklist" in h.history_change_reason
    assert DeviceModel.objects.get(pk=pump_model.pk).pm_procedure.estimated_hours == Decimal("2.25")


def test_edit_errors_come_back_on_the_form(client, signed_in, proc, spare, vent_model):
    signed_in("technician")
    r = client.post(edit_url(proc, vent_model), form_post(code="ecri-454"), **HX)
    body = r.content.decode()
    assert "HX-Retarget" not in r and "<h2>Edit HA-G5-PM6</h2>" in body
    assert field_error(body, "code") == "ecri-454 is already the code of another procedure here."
    proc.refresh_from_db()
    assert proc.code == "HA-G5-PM6" and proc.name == "ICU ventilator 6-month PM"


def test_a_single_user_edit_says_so(client, signed_in, spare, vent_model):
    svc.set_model_procedure(vent_model, spare)
    signed_in("technician")
    body = client.get(edit_url(spare, vent_model), **HX).content.decode()
    assert "Used by Hamilton Medical Hamilton-G5 only." in body
    r = client.post(edit_url(spare, vent_model), form_post(code="ECRI-454"), **HX)
    assert triggers(r)["toast"] == {"value": "ECRI-454 saved"}


def test_edit_needs_the_model_and_a_procedure_of_this_facility(client, signed_in, proc, vent_model, theirs):
    signed_in("technician")
    assert client.get(edit_url(proc), **HX).status_code == 400
    assert client.post(edit_url(proc), form_post(code="HA-G5-PM6"), **HX).status_code == 400
    assert client.get(edit_url(theirs["procedure"], vent_model), **HX).status_code == 404
    assert client.post(edit_url(theirs["procedure"], vent_model), form_post(code="THEIR-PM"), **HX).status_code == 404
    assert client.get(edit_url(proc, theirs["model"]), **HX).status_code == 404
    assert PmProcedure.unscoped.get(pk=theirs["procedure"].pk).name == "Theirs"  # unscoped: the other facility's row
    assert PmProcedure.objects.get(pk=proc.pk).name == "ICU ventilator 6-month PM"


def test_an_open_pm_work_order_prints_the_revised_checklist(client, signed_in, proc, vent_model, vent):
    """What the tab and the edit form say: the work-order print reads the model's procedure when it is printed."""
    wo = create_work_order(asset=vent, type=WoType.PM, priority="high", problem="PM", estimated_hours=Decimal("1.5"))
    signed_in("technician")
    client.post(edit_url(proc, vent_model), form_post(code="HA-G5-PM6", checklist="Brand new step | V"), **HX)
    printed = client.get(f"/print/work-orders/{wo.number}/").content.decode()
    assert "Brand new step" in printed and "Visual inspection and cleaning" not in printed
    wo.refresh_from_db()
    assert wo.estimated_hours == Decimal("1.5")  # the work order keeps the hours estimated when it was created


def test_the_pm_library_shows_the_new_procedure(client, signed_in, vent_model, dept):
    Asset.objects.create(tag="CE-1", device_model=vent_model, department=dept, next_pm_on=date.today() + timedelta(days=5))
    signed_in("technician")
    client.post(new_url(vent_model), form_post(), **HX)
    panels = client.get("/pm/", **{**HX, "HTTP_HX_TARGET": "pm-panels"}).content.decode()
    assert "BD-ALARIS-PM12<small>OEM service manual</small>" in panels and "1 of 1 model has a PM procedure" in panels
    assert Department.objects.count() == 1
