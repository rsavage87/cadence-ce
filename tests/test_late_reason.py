"""
Slice 25, part D: why a PM was late, the doors (the survey binder reads WorkOrder.late_reason). Completing a PM after its due date takes
an optional reason (apps.workorders.completion, the modal from the drawer and from My work, the API's transition) recorded through
set_late_reason in the same transaction; the work order drawer's "Why late" row on a PM that missed its due date, with a select that
saves itself for whoever may record it (Edit; Approve once closed; a scoped user on their share only), a life-support or high-risk PM
with no reason highlighted; POST /api/v1/work-orders/{id}/late-reason/, and the field read-only everywhere else; the History section's
words.
"""
import json
import re
from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone
from pg_helpers import as_app_role, needs_postgres

from apps.core.history import record_history
from apps.equipment.models import Asset, Department, DeviceModel, RiskClass
from apps.tenants.context import tenant_context
from apps.web import views_wo_late
from apps.web.forms_wo_complete import LATE_KEEP
from apps.workorders import completion
from apps.workorders.models import LateReason, Source, WorkOrder, WoStatus, WoType
from apps.workorders.services import assign, change_status, create_work_order, set_late_reason

HX = {"HTTP_HX_REQUEST": "true"}
API = "/api/v1/work-orders/"
VENDOR = "Hamilton Medical field service"


@pytest.fixture
def today(ctx):
    return timezone.localdate()  # the facility's day, as the views and the services read it


@pytest.fixture
def monitor_model(ctx):
    return DeviceModel.objects.create(manufacturer="GE", model="B650", description="Patient monitor", category="Monitors",
                                      risk_class=RiskClass.MEDIUM, oem_pm_interval_months=12)


@pytest.fixture
def monitor(ctx, dept, monitor_model):
    return Asset.objects.create(tag="CE-30001", device_model=monitor_model, department=dept)


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug, **fields):
        user = make_user(role_slug)
        for name, value in fields.items():
            setattr(user, name, value)
        user.save()
        client.force_login(user)
        return user

    return _as


def pm_on(asset, *, due_in: int = -5, tech=None, vendor: str = "", source=Source.PM_PLANNER, start: bool = True):
    """A PM on `asset` due `due_in` days from today (negative: past due), opened a month ago, assigned and started unless told not to."""
    today = timezone.localdate()
    wo = create_work_order(asset=asset, type=WoType.PM, priority="normal", problem="Scheduled preventive maintenance", source=source,
                           opened_on=today - timedelta(days=30), due_on=today + timedelta(days=due_in))
    if tech is not None or vendor:
        assign(wo, technician=tech, vendor_name=vendor)
    if start:
        change_status(wo, WoStatus.IN_PROGRESS)
    return wo


def toast_of(r) -> str:
    return json.loads(r["HX-Trigger"])["toast"]["value"] if "HX-Trigger" in r else ""


# --- completing late (apps.workorders.completion) ----------------------------------------------------------------------------------


def test_a_late_pm_records_its_reason_with_the_completion(vent, techs, today, make_user):
    pm = pm_on(vent, tech=techs["dana"])
    tech = make_user("technician")
    assert completion.completes_late(pm, today) and not completion.completes_late(pm, pm.due_on)
    completion.complete_work_order(pm, pm_result="pass", late_reason=LateReason.DEVICE_IN_USE, by=tech, today=today)
    pm.refresh_from_db()
    assert (pm.status, pm.completed_on, pm.late_reason) == (WoStatus.COMPLETED, today, LateReason.DEVICE_IN_USE)
    rows = list(pm.history.order_by("-history_date", "-history_id")[:2])
    assert rows[0].late_reason == "device_in_use" and rows[0].status == WoStatus.COMPLETED  # recorded after the completion, same transaction
    assert rows[0].history_change_reason == "Why late: Device in use, not available" and rows[1].late_reason == ""


def test_the_reason_is_optional_and_blank_keeps_one_already_recorded(vent, pump, techs, today):
    plain = pm_on(vent, tech=techs["dana"])
    completion.complete_work_order(plain, pm_result="pass", today=today)  # an API client or a hurried technician still completes
    plain.refresh_from_db()
    assert plain.status == WoStatus.COMPLETED and plain.late_reason == ""
    kept = pm_on(pump, tech=techs["dana"])
    set_late_reason(kept, LateReason.NOT_LOCATED, today=today)  # recorded from the drawer while it was still open
    completion.complete_work_order(kept, pm_result="pass", late_reason="  ", today=today)
    kept.refresh_from_db()
    assert kept.status == WoStatus.COMPLETED and kept.late_reason == LateReason.NOT_LOCATED


def test_a_refused_reason_is_reported_with_the_rest_and_nothing_is_saved(vent, monitor, techs, today):
    on_time = pm_on(vent, due_in=3, tech=techs["dana"])
    with pytest.raises(ValidationError) as e:
        completion.complete_work_order(on_time, pm_result="pass", late_reason=LateReason.STAFFING, today=today)
    assert e.value.message_dict == {"late_reason": [f"{on_time.number} is done by its due date, so it has no reason to record."]}
    late = pm_on(monitor, tech=techs["dana"])
    with pytest.raises(ValidationError) as e:
        completion.complete_work_order(late, late_reason="because", hours="1", today=today)  # no result either: both at once
    assert set(e.value.message_dict) == {"pm_result", "late_reason"} and e.value.message_dict["late_reason"] == ["Choose one of the reasons listed."]
    repair = create_work_order(asset=monitor, type=WoType.REPAIR, priority="normal", problem="No display", opened_on=today - timedelta(days=20),
                               due_on=today - timedelta(days=2))
    assign(repair, technician=techs["dana"])
    change_status(repair, WoStatus.IN_PROGRESS)
    with pytest.raises(ValidationError) as e:
        completion.complete_work_order(repair, resolution="Replaced the display", late_reason=LateReason.STAFFING, today=today)
    assert e.value.message_dict == {"late_reason": ["Only a PM records why it was late."]}
    for wo in (on_time, late, repair):
        wo.refresh_from_db()
        assert wo.status == WoStatus.IN_PROGRESS and wo.late_reason == "" and not wo.labor_lines.exists()


def test_a_refusal_when_recording_takes_the_completion_back(vent, techs, today, monkeypatch):
    pm = pm_on(vent, tech=techs["dana"])

    def refuse(*args, **kwargs):
        raise ValidationError({"late_reason": "Not now."})

    monkeypatch.setattr("apps.workorders.services.set_late_reason", refuse)
    with pytest.raises(ValidationError) as e:
        completion.complete_work_order(pm, pm_result="pass", late_reason=LateReason.STAFFING, today=today)
    assert e.value.message_dict == {"late_reason": ["Not now."]}
    pm.refresh_from_db()
    assert pm.status == WoStatus.IN_PROGRESS and pm.completed_on is None


def test_an_open_pm_completed_late_in_one_step_records_its_reason(vent, techs, today):
    pm = pm_on(vent, tech=techs["dana"], start=False)  # slice 24: started as it is completed
    done = completion.complete_work_order(pm, pm_result="pass", late_reason=LateReason.SCHEDULING, today=today)
    pm.refresh_from_db()
    assert done.started and pm.status == WoStatus.COMPLETED and pm.late_reason == LateReason.SCHEDULING


# --- the Mark completed modal ------------------------------------------------------------------------------------------------------


def complete_url(wo) -> str:
    return f"/work-orders/{wo.number}/complete/"


def test_the_modal_asks_why_a_life_support_pm_was_late_before_the_result(client, signed_in, vent, techs):
    pm = pm_on(vent, tech=techs["dana"])
    signed_in("technician")
    body = client.get(complete_url(pm), **HX).content.decode()
    assert 'class="full wc-late flag"' in body and "Why was it late?" in body
    assert body.index('name="late_reason"') < body.index('name="pm_result"')  # prominent: before the result
    assert "the survey binder lists a late life-support or high-risk PM until a reason is recorded" in body
    assert '<option value="" selected>Not recorded</option>' in body
    for value, label in LateReason.choices:
        assert f'<option value="{value}">{label}</option>' in body.replace("&#x27;", "'")


def test_the_modal_asks_a_medium_risk_pm_after_the_hours_and_an_on_time_pm_not_at_all(client, signed_in, monitor, vent, techs):
    late = pm_on(monitor, tech=techs["dana"])
    signed_in("technician")
    body = client.get(complete_url(late), **HX).content.decode()
    assert 'class="wc-late"' in body and "Why was it late? Optional" in body and "wc-late flag" not in body
    assert body.index('name="late_reason"') > body.index('name="hours"') > body.index('name="resolution"')
    on_time = pm_on(vent, due_in=0, tech=techs["dana"])  # due today: done by its due date
    assert 'name="late_reason"' not in client.get(complete_url(on_time), **HX).content.decode()


def test_the_modal_says_which_reason_blank_keeps(client, signed_in, vent, techs, today):
    pm = pm_on(vent, tech=techs["dana"])
    set_late_reason(pm, LateReason.WAITING_PARTS_VENDOR, today=today)
    signed_in("technician")
    body = client.get(complete_url(pm), **HX).content.decode()
    keep = LATE_KEEP.format(label="Waiting on parts or the vendor")
    assert f'<option value="" selected>{keep}</option>' in body


def test_saving_the_modal_records_the_reason_and_says_so(client, signed_in, vent, techs):
    pm = pm_on(vent, tech=techs["dana"])
    signed_in("technician")
    r = client.post(complete_url(pm), {"pm_result": "pass", "late_reason": LateReason.STAFFING}, **HX)
    pm.refresh_from_db()
    assert r.status_code == 200 and r["HX-Retarget"] == "#drawer"
    assert pm.status == WoStatus.COMPLETED and pm.late_reason == LateReason.STAFFING
    assert toast_of(r) == f"{pm.number} completed: PM passed; why late: Staffing or workload"
    assert '<option value="staffing" selected>Staffing or workload</option>' in r.content.decode()  # the drawer's row now shows it


def test_a_refused_reason_keeps_the_modal_open_with_what_was_typed(client, signed_in, vent, techs):
    pm = pm_on(vent, tech=techs["dana"])
    signed_in("technician")
    r = client.post(complete_url(pm), {"pm_result": "pass", "resolution": "Checked", "late_reason": "lazy"}, **HX)
    body = r.content.decode()
    pm.refresh_from_db()
    assert pm.status == WoStatus.IN_PROGRESS and "HX-Retarget" not in r
    assert "Choose one of the reasons listed." in body and r.context["form"].first_error == "late_reason"
    select = re.search(r'<select name="late_reason"[^>]*>', body).group(0)
    assert "autofocus" in select and 'aria-describedby="wc-late-hint"' in select
    assert '<option value="lazy"' not in body and "\nChecked</textarea>" in body  # the resolution kept; an unknown reason never offered


def test_my_work_completes_late_with_its_reason_and_stays_on_the_list(client, signed_in, vent, techs):
    signed_in("technician")
    pm = pm_on(vent, tech=techs["dana"])
    body = client.get(f"{complete_url(pm)}?from=my_work", **HX).content.decode()
    assert 'name="late_reason"' in body and 'name="from" value="my_work"' in body
    r = client.post(complete_url(pm), {"pm_result": "pass", "late_reason": LateReason.DEVICE_IN_USE, "from": "my_work"}, **HX)
    pm.refresh_from_db()
    assert r["HX-Reswap"] == "none" and pm.late_reason == LateReason.DEVICE_IN_USE
    assert toast_of(r).endswith("why late: Device in use, not available")


# --- the drawer's "Why late" row ---------------------------------------------------------------------------------------------------


def drawer(client, wo) -> str:
    r = client.get(f"/work-orders/{wo.number}/", **HX)
    assert r.status_code == 200
    return r.content.decode()


def test_a_missed_pm_shows_why_late_with_a_select_that_saves_itself(client, signed_in, vent, techs):
    pm = pm_on(vent, tech=techs["dana"])
    signed_in("technician")
    body = drawer(client, pm)
    assert 'id="wo-late" class="wo-late flag"' in body and views_wo_late.FLAG_NOTE in body
    assert (f'<select id="wo-late-select" name="late_reason" hx-post="/work-orders/{pm.number}/late-reason/" hx-trigger="change" '
            'hx-target="#wo-late" hx-swap="outerHTML" aria-describedby="wo-late-note">') in body
    r = client.post(f"/work-orders/{pm.number}/late-reason/", {"late_reason": LateReason.OUT_OF_SERVICE}, **HX)
    row = r.content.decode()
    pm.refresh_from_db()
    assert r.status_code == 200 and pm.late_reason == LateReason.OUT_OF_SERVICE
    assert [t.name for t in r.templates] == ["web/_wo_late.html"] and row.strip().startswith('<div id="wo-late" class="wo-late">')
    assert '<option value="out_of_service" selected>' in row and views_wo_late.FLAG_NOTE not in row  # recorded: no longer highlighted
    assert toast_of(r) == f"Why {pm.number} was late: Out of service or in repair when due"
    r = client.post(f"/work-orders/{pm.number}/late-reason/", {"late_reason": ""}, **HX)
    pm.refresh_from_db()
    assert pm.late_reason == "" and toast_of(r) == f"Why {pm.number} was late: cleared, not recorded" and "wo-late flag" in r.content.decode()


def test_the_row_reads_only_for_a_view_role_and_is_highlighted_only_for_life_support_and_high(client, signed_in, vent, pump, monitor, techs, today):
    life, high, medium = pm_on(vent, tech=techs["dana"]), pm_on(pump, tech=techs["dana"]), pm_on(monitor, tech=techs["dana"])
    imported = pm_on(vent, tech=techs["dana"], source=Source.IMPORTED)
    set_late_reason(high, LateReason.STAFFING, today=today)
    signed_in("analyst")
    body = drawer(client, life)
    assert '<div id="wo-late" class="wo-late flag">' in body and "<dt>Why late</dt>" in body and "Not recorded" in body
    assert "<select" not in body.split('id="wo-late"')[1].split("</div>")[0]
    assert "Staffing or workload" in drawer(client, high) and "wo-late flag" not in drawer(client, high)
    assert "wo-late flag" not in drawer(client, medium) and 'id="wo-late"' in drawer(client, medium)
    body = drawer(client, imported)
    assert "wo-late flag" not in body and "Imported from the previous system." in body


def test_no_row_on_a_pm_on_time_or_not_yet_due_or_a_repair(client, signed_in, vent, monitor, techs, today):
    not_due = pm_on(vent, due_in=4, tech=techs["dana"])
    done = pm_on(monitor, due_in=-1, tech=techs["dana"])
    completion.complete_work_order(done, pm_result="pass", today=done.due_on)  # completed on its due date
    repair = create_work_order(asset=monitor, type=WoType.REPAIR, priority="normal", problem="No display", opened_on=today - timedelta(days=20),
                               due_on=today - timedelta(days=5))
    signed_in("technician")
    for wo in (not_due, done, repair):
        assert 'id="wo-late"' not in drawer(client, wo), wo.number


def test_a_closed_pm_needs_approve_and_a_refusal_is_said_in_the_row(client, signed_in, vent, techs, today):
    pm = pm_on(vent, tech=techs["dana"])
    completion.complete_work_order(pm, pm_result="pass", today=today)
    change_status(pm, WoStatus.CLOSED)
    signed_in("technician")
    body = drawer(client, pm)
    assert 'id="wo-late"' in body and "wo-late-select" not in body  # Edit: read only once closed
    r = client.post(f"/work-orders/{pm.number}/late-reason/", {"late_reason": LateReason.STAFFING}, **HX)
    pm.refresh_from_db()
    assert r.status_code == 200 and pm.late_reason == "" and views_wo_late.CLOSED.format(number=pm.number) in r.content.decode()
    assert toast_of(r) == ""
    signed_in("manager")
    assert "wo-late-select" in drawer(client, pm)
    client.post(f"/work-orders/{pm.number}/late-reason/", {"late_reason": LateReason.STAFFING}, **HX)
    pm.refresh_from_db()
    assert pm.late_reason == LateReason.STAFFING


def test_the_services_refusals_are_said_in_the_row(client, signed_in, vent, monitor, techs):
    late = pm_on(vent, tech=techs["dana"])
    signed_in("technician")
    r = client.post(f"/work-orders/{late.number}/late-reason/", {"late_reason": "lazy"}, **HX)
    late.refresh_from_db()
    assert late.late_reason == "" and "Choose one of the reasons listed." in r.content.decode() and 'id="wo-late-select"' in r.content.decode()
    not_due = pm_on(monitor, due_in=10, tech=techs["dana"])  # its due date moved later while the drawer was open
    r = client.post(f"/work-orders/{not_due.number}/late-reason/", {"late_reason": LateReason.STAFFING}, **HX)
    body = r.content.decode()
    assert f"{not_due.number} is not a PM that missed its due date" in body and '<div id="wo-late" class="wo-late">' in body
    assert "<select" not in body


@pytest.mark.parametrize("role", ["analyst", "requester"])
def test_roles_without_work_orders_edit_get_a_403(client, signed_in, vent, techs, role):
    pm = pm_on(vent, tech=techs["dana"])
    signed_in(role, **({"department": "ICU"} if role == "requester" else {}))
    assert client.post(f"/work-orders/{pm.number}/late-reason/", {"late_reason": LateReason.STAFFING}, **HX).status_code == 403
    assert client.get(f"/work-orders/{pm.number}/late-reason/", **HX).status_code in (403, 405)
    pm.refresh_from_db()
    assert pm.late_reason == ""


def test_a_vendor_records_why_on_their_companys_pm_only(client, signed_in, vent, pump, techs):
    theirs = pm_on(vent, vendor=VENDOR)
    other = pm_on(pump, vendor="BD field service")
    house = pm_on(vent, tech=techs["dana"])
    signed_in("vendor", company="Hamilton Medical")
    assert "wo-late-select" in drawer(client, theirs)
    r = client.post(f"/work-orders/{theirs.number}/late-reason/", {"late_reason": LateReason.DEVICE_IN_USE}, **HX)
    theirs.refresh_from_db()
    assert r.status_code == 200 and theirs.late_reason == LateReason.DEVICE_IN_USE
    for wo in (other, house):
        assert client.post(f"/work-orders/{wo.number}/late-reason/", {"late_reason": LateReason.STAFFING}, **HX).status_code == 404
        wo.refresh_from_db()
        assert wo.late_reason == ""


def test_another_facilitys_work_order_is_a_404(client, signed_in, other_tenant, ctx):
    with tenant_context(other_tenant):
        model = DeviceModel.objects.create(manufacturer="GE", model="B650", description="Monitor", category="Monitors", risk_class=RiskClass.HIGH)
        theirs = pm_on(Asset.objects.create(tag="THEIRS-1", device_model=model, department=Department.objects.create(name="ICU")), vendor="GE")
    signed_in("director")
    assert client.post(f"/work-orders/{theirs.number}/late-reason/", {"late_reason": LateReason.STAFFING}, **HX).status_code == 404
    with tenant_context(other_tenant):
        theirs.refresh_from_db()
        assert theirs.late_reason == ""


def test_the_history_section_reads_the_reason_in_words(vent, techs, today, make_user):
    pm = pm_on(vent, tech=techs["dana"])
    set_late_reason(pm, LateReason.DUPLICATE, by=make_user("manager"), today=today)
    entries, _more = record_history(pm)
    latest = entries[0]
    assert [(c.field, c.before, c.after) for c in latest.changes] == [("Why late", "—", "Duplicate work order (the PM was done on another)")]
    assert latest.reason == ""  # "Why late: <reason>" only repeats the change
    set_late_reason(pm, "", today=today)
    entries, _more = record_history(pm)
    assert [(c.field, c.after) for c in entries[0].changes] == [("Why late", "—")] and entries[0].reason == "Why late cleared"


# --- the API -----------------------------------------------------------------------------------------------------------------------


def post(client, url, body):
    return client.post(url, body, content_type="application/json")


def test_the_api_shows_the_reason_and_records_it_at_late_reason(client, signed_in, vent, techs):
    pm = pm_on(vent, tech=techs["dana"])
    signed_in("technician")
    data = client.get(f"{API}{pm.id}/").json()
    assert (data["late_reason"], data["late_reason_label"]) == ("", "")
    r = post(client, f"{API}{pm.id}/late-reason/", {"late_reason": "not_located"})
    assert r.status_code == 200 and (r.json()["late_reason"], r.json()["late_reason_label"]) == ("not_located", "Device could not be located")
    pm.refresh_from_db()
    assert pm.late_reason == LateReason.NOT_LOCATED
    assert post(client, f"{API}{pm.id}/late-reason/", {"late_reason": None}).json()["late_reason"] == ""  # null clears it


@pytest.mark.parametrize("body, words", [({"late_reason": "lazy"}, "Choose one of the reasons listed."), ({}, "Required"),
                                         ({"late_reason": 5}, "Choose one of the reasons listed."), ([], "Required")])
def test_the_api_refuses_a_bad_reason_in_words(client, signed_in, vent, techs, body, words):
    pm = pm_on(vent, tech=techs["dana"])
    signed_in("technician")
    r = post(client, f"{API}{pm.id}/late-reason/", body)
    assert r.status_code == 400 and words in r.json()["late_reason"][0]


def test_the_api_refuses_a_pm_that_did_not_miss_its_due_date_and_a_closed_one_without_approve(client, signed_in, vent, monitor, techs, today):
    not_due = pm_on(vent, due_in=5, tech=techs["dana"])
    closed = pm_on(monitor, tech=techs["dana"])
    completion.complete_work_order(closed, pm_result="pass", today=today)
    change_status(closed, WoStatus.CLOSED)
    signed_in("technician")
    r = post(client, f"{API}{not_due.id}/late-reason/", {"late_reason": "staffing"})
    assert r.status_code == 400 and "is not a PM that missed its due date" in r.json()["late_reason"][0]
    r = post(client, f"{API}{closed.id}/late-reason/", {"late_reason": "staffing"})
    assert r.status_code == 403 and "needs Work orders Approve" in r.json()["detail"]
    signed_in("analyst")
    assert post(client, f"{API}{closed.id}/late-reason/", {"late_reason": "staffing"}).status_code == 403
    signed_in("manager")
    assert post(client, f"{API}{closed.id}/late-reason/", {"late_reason": "staffing"}).status_code == 200


def test_patch_never_writes_the_reason(client, signed_in, vent, techs):
    pm = pm_on(vent, tech=techs["dana"])
    signed_in("technician")
    r = client.patch(f"{API}{pm.id}/", {"late_reason": "staffing"}, content_type="application/json")
    assert r.status_code == 400 and "late-reason" in r.json()["late_reason"][0]
    pm.refresh_from_db()
    assert pm.late_reason == ""
    r = client.patch(f"{API}{pm.id}/", {"late_reason": "", "late_reason_label": "Staffing", "estimated_hours": "2.0"}, content_type="application/json")
    pm.refresh_from_db()
    assert r.status_code == 200 and pm.late_reason == "" and r.json()["late_reason_label"] == ""  # what a GET returned goes back fine


def test_the_api_completes_late_with_a_reason(client, signed_in, vent, monitor, techs):
    pm = pm_on(vent, tech=techs["dana"])
    signed_in("technician")
    r = post(client, f"{API}{pm.id}/transition/", {"status": "completed", "pm_result": "pass", "late_reason": "scheduling"})
    assert r.status_code == 200 and (r.json()["status"], r.json()["late_reason"]) == ("completed", "scheduling")
    on_time = pm_on(monitor, due_in=2, tech=techs["dana"])
    r = post(client, f"{API}{on_time.id}/transition/", {"status": "completed", "pm_result": "pass", "late_reason": "scheduling"})
    assert r.status_code == 400 and "late_reason" in r.json()
    on_time.refresh_from_db()
    assert on_time.status == WoStatus.IN_PROGRESS


def test_a_vendor_uses_the_api_on_their_companys_pm_only(client, signed_in, vent, pump):
    theirs, other = pm_on(vent, vendor=VENDOR), pm_on(pump, vendor="BD field service")
    signed_in("vendor", company="Hamilton Medical")
    assert post(client, f"{API}{theirs.id}/late-reason/", {"late_reason": "device_in_use"}).status_code == 200
    assert post(client, f"{API}{other.id}/late-reason/", {"late_reason": "device_in_use"}).status_code == 404
    signed_in("requester", department="ICU")  # Work orders Request: below Edit
    assert post(client, f"{API}{theirs.id}/late-reason/", {"late_reason": "staffing"}).status_code == 403
    assert WorkOrder.objects.get(pk=other.pk).late_reason == ""


# --- under the policies ------------------------------------------------------------------------------------------------------------


@needs_postgres
def test_why_late_under_the_policies(client, signed_in, vent, techs, tenant):
    """As the runtime role: the drawer's row, its select's save, the modal's reason, and the API's are read and written in the facility
    the request set."""
    drawer_pm, modal_pm = pm_on(vent, tech=techs["dana"]), pm_on(vent, tech=techs["dana"])
    signed_in("technician")
    as_app_role()
    assert 'id="wo-late-select"' in client.get(f"/work-orders/{drawer_pm.number}/", **HX).content.decode()
    r = client.post(f"/work-orders/{drawer_pm.number}/late-reason/", {"late_reason": LateReason.STAFFING}, **HX)
    assert toast_of(r) == f"Why {drawer_pm.number} was late: Staffing or workload"
    r = client.post(complete_url(modal_pm), {"pm_result": "pass", "late_reason": LateReason.NOT_LOCATED}, **HX)
    assert r["HX-Retarget"] == "#drawer"
    r = post(client, f"{API}{drawer_pm.id}/late-reason/", {"late_reason": "scheduling"})
    assert r.status_code == 200 and r.json()["late_reason_label"] == "Scheduling error"
    with tenant_context(tenant):  # the requests restored the connection's facility setting when they finished
        drawer_pm.refresh_from_db()
        modal_pm.refresh_from_db()
        assert drawer_pm.late_reason == LateReason.SCHEDULING
        assert (modal_pm.status, modal_pm.late_reason) == (WoStatus.COMPLETED, LateReason.NOT_LOCATED)
