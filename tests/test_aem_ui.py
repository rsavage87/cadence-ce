"""The model drawer's AEM tab and its modals (slice 14, apps/web/views_aem.py): who sees and does what (checked on the server, GET and
POST), the HTMX answers (the drawer on the AEM tab, toasts, models-changed, devices-changed when next PMs moved, modal-close after
settle), refusals shown in the modal, an interval on file without a recorded approval, the device drawer's committee date, and
another facility's models and decisions as 404s."""
import json
from datetime import date, timedelta

import pytest

from apps.accounts.models import create_default_roles
from apps.equipment.models import Asset, Department, DeviceModel, RiskClass
from apps.facility.services import update_settings
from apps.pm import aem
from apps.pm.dates import add_months
from apps.pm.models import AemDecision, AemStatus
from apps.tenants.context import tenant_context
from apps.workorders.models import WoType
from apps.workorders.services import create_work_order

HX = {"HTTP_HX_REQUEST": "true"}
TODAY = date.today()
SHOWN_TODAY = f"{TODAY:%b} {TODAY.day}, {TODAY.year}"  # the templates' "M j, Y"


def months_ago(n: int) -> date:
    return add_months(TODAY, -n)


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug, username=None):
        user = make_user(role_slug, username=username)
        client.force_login(user)
        return user

    return _as


@pytest.fixture
def monitor(ctx, dept):
    """A high-risk model with four years of history: one monitor last serviced 14 months ago."""
    dm = DeviceModel.objects.create(manufacturer="Philips", model="IntelliVue MX750", description="Patient monitor", category="Patient monitoring",
                                    risk_class=RiskClass.HIGH, oem_pm_interval_months=12)
    Asset.objects.create(tag="M-1", device_model=dm, department=dept, installed_on=months_ago(48), last_pm_on=months_ago(14),
                         next_pm_on=add_months(months_ago(14), 12))
    return dm


@pytest.fixture
def users(make_user):
    return {"tech": make_user("technician"), "manager": make_user("manager"), "director": make_user("director")}


@pytest.fixture
def theirs(tenant, other_tenant):
    """Another facility's model, with a device and an open AEM proposal."""
    create_default_roles(other_tenant)
    with tenant_context(other_tenant):
        d = Department.objects.create(name="Their ICU")
        dm = DeviceModel.objects.create(manufacturer="GE", model="B650", description="Monitor", category="Monitors", aem_interval_months=24)
        Asset.objects.create(tag="THEIRS-1", device_model=dm, department=d, installed_on=months_ago(60), next_pm_on=TODAY)
        decision = AemDecision.objects.create(device_model=dm, interval_months=18, oem_interval_months=12, rationale="x", proposed_on=TODAY)
    return {"model": dm, "decision": decision}


def propose(dm, by, months=24):
    return aem.propose(dm, interval_months=months, rationale="Few failures in three years.", by=by)


def approve(decision, by):
    return aem.approve(decision, by=by, decided_on=TODAY, note="EMC minutes, item 4")


def tab_url(dm):
    return f"/pm/models/{dm.pk}/?tab=aem"


def propose_url(dm):
    return f"/pm/models/{dm.pk}/aem/propose/"


def decide_url(decision, choice=""):
    return f"/pm/aem/{decision.pk}/decide/" + (f"?decision={choice}" if choice else "")


def withdraw_url(decision):
    return f"/pm/aem/{decision.pk}/withdraw/"


def end_url(pk):
    return f"/pm/aem/{pk}/end/"


def tab(client, dm):
    r = client.get(tab_url(dm), **HX)
    assert r.status_code == 200
    return r.content.decode()


def triggers(r, header="HX-Trigger") -> dict:
    return json.loads(r[header]) if header in r else {}


def assert_saved(r, dm, toast_text):
    """A modal saved: the drawer on the AEM tab into #drawer, a toast, models-changed, and the modal closed after settle."""
    body = r.content.decode()
    assert r.status_code == 200 and r["HX-Retarget"] == "#drawer"
    assert f'class="tab active" role="tab" aria-selected="true" hx-get="/pm/models/{dm.pk}/?tab=aem"' in body
    t = triggers(r)
    assert "models-changed" in t and t["toast"]["value"] == toast_text
    assert "modal-close" in triggers(r, "HX-Trigger-After-Settle")
    return t


# --- access ---------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["requester", "vendor"])
def test_roles_without_pm_access_get_403_everywhere(client, signed_in, monitor, users, role):
    d = propose(monitor, users["tech"])
    signed_in(role)
    assert client.get(tab_url(monitor), **HX).status_code == 403
    for url in (propose_url(monitor), decide_url(d), end_url(d.pk)):
        assert client.get(url, **HX).status_code == 403
    assert client.post(propose_url(monitor), {"interval_months": "18", "rationale": "x"}, **HX).status_code == 403
    assert client.post(decide_url(d), {"decision": "approve", "decided_on": TODAY.isoformat(), "note": "x"}, **HX).status_code == 403
    assert client.post(withdraw_url(d), **HX).status_code == 403
    assert client.post(end_url(monitor.pk), {"reason": "x"}, **HX).status_code == 403
    d.refresh_from_db()
    assert d.status == AemStatus.PROPOSED


def test_an_analyst_sees_the_tab_and_nothing_else(client, signed_in, monitor, users):
    d = approve(propose(monitor, users["tech"]), users["manager"])
    p = propose(monitor, users["tech"], 18)
    signed_in("analyst")  # PM View
    body = tab(client, monitor)
    assert "AEM in force: every 24 months instead of the OEM's 12" in body and "Open proposal" in body and "Failure history" in body
    for button in ("Propose an AEM interval", ">End AEM<", ">Approve<", ">Reject<", ">Withdraw<"):
        assert button not in body
    for url in (propose_url(monitor), decide_url(p), end_url(d.pk)):
        assert client.get(url, **HX).status_code == 403
    assert client.post(withdraw_url(p), **HX).status_code == 403
    assert client.post(end_url(d.pk), {"reason": "x"}, **HX).status_code == 403


# --- proposing ------------------------------------------------------------------------------------------------------------


def test_a_technician_proposes_from_the_tab(client, signed_in, monitor):
    signed_in("technician")  # PM Edit
    body = tab(client, monitor)
    assert f'hx-get="{propose_url(monitor)}" hx-target="#modal-card"' in body and "Propose an AEM interval" in body
    assert "Following the OEM interval: every 12 months." in body and "No AEM proposals for this model yet." in body
    modal = client.get(propose_url(monitor), **HX).content.decode()
    assert "<h2>Propose an AEM interval</h2>" in modal and f'hx-post="{propose_url(monitor)}"' in modal
    assert "Facility AEM policy: Equipment Management Committee, 3-year failure history required." in modal and "No patient information." in modal
    r = client.post(propose_url(monitor), {"interval_months": "24", "rationale": "Few failures in three years."}, **HX)
    assert_saved(r, monitor, "AEM of 24 months proposed for Philips IntelliVue MX750; it goes to the committee")
    d = AemDecision.objects.get()
    assert (d.status, d.interval_months, d.proposed_by.role.slug) == (AemStatus.PROPOSED, 24, "technician")
    body = r.content.decode()
    assert "Open proposal" in body and "Few failures in three years." in body and f'hx-post="{withdraw_url(d)}"' in body
    assert ">Approve<" not in body  # PM Edit does not decide


def test_the_propose_modal_shows_the_facilitys_policy(client, signed_in, monitor):
    update_settings(policy_aem="Committee review with three years of records.")
    signed_in("technician")
    assert "Facility AEM policy: Committee review with three years of records." in client.get(propose_url(monitor), **HX).content.decode()


@pytest.mark.parametrize("data, error", [
    ({"interval_months": "12", "rationale": "x"}, "That is the OEM interval (12 months)."),
    ({"interval_months": "abc", "rationale": "x"}, "The interval is a whole number of months, 1 to 120."),
    ({"interval_months": "24", "rationale": "  "}, "Make the case for the new interval"),
])
def test_a_refused_proposal_stays_in_the_modal(client, signed_in, monitor, data, error):
    signed_in("technician")
    r = client.post(propose_url(monitor), data, **HX)
    body = r.content.decode()
    assert r.status_code == 200 and "HX-Retarget" not in r and "HX-Trigger" not in r
    assert "<h2>Propose an AEM interval</h2>" in body and error in body.replace("&#x27;", "'")
    assert not AemDecision.objects.exists()


def test_a_life_support_model_says_why_it_cannot_be_proposed(client, signed_in, dept, vent_model, vent):
    signed_in("director")
    body = tab(client, vent_model)
    assert "Life-support devices are excluded from AEM by policy" in body and "Propose an AEM interval" not in body
    assert body.count("excluded from AEM by policy") == 1  # said once, in the interval section
    modal = client.get(propose_url(vent_model), **HX).content.decode()
    assert "Life-support devices are excluded from AEM by policy" in modal and "hx-post" not in modal
    r = client.post(propose_url(vent_model), {"interval_months": "12", "rationale": "x"}, **HX)
    assert r.status_code == 200 and "HX-Retarget" not in r and "excluded from AEM by policy" in r.content.decode()
    assert not AemDecision.objects.exists()


def test_a_model_without_enough_history_says_so(client, signed_in, ctx, dept):
    dm = DeviceModel.objects.create(manufacturer="GE", model="B40", description="Monitor", category="Monitors")
    Asset.objects.create(tag="B-1", device_model=dm, department=dept, installed_on=months_ago(12), next_pm_on=TODAY)
    signed_in("technician")
    body = tab(client, dm)
    assert "Propose an AEM interval" not in body and "The facility&#x27;s AEM policy" in body and "a proposal needs 3 years" in body


# --- the committee's decision ---------------------------------------------------------------------------------------------


def test_a_manager_approves_with_the_committee_date(client, signed_in, monitor, users):
    d = propose(monitor, users["tech"])
    client.force_login(users["manager"])  # PM Approve
    body = tab(client, monitor)
    assert f'hx-get="{decide_url(d, "approve")}"' in body and f'hx-get="{decide_url(d, "reject")}"' in body and f'hx-post="{withdraw_url(d)}"' in body
    modal = client.get(decide_url(d, "approve"), **HX).content.decode()
    assert "<h2>Approve AEM proposal</h2>" in modal and f'value="{TODAY.isoformat()}"' in modal and 'name="decision" value="approve"' in modal
    assert "Few failures in three years." in modal and "A longer interval moves no device's next PM" in modal
    r = client.post(decide_url(d), {"decision": "approve", "decided_on": TODAY.isoformat(), "note": "EMC minutes, item 4"}, **HX)
    t = assert_saved(r, monitor, "AEM of 24 months approved for Philips IntelliVue MX750")
    assert "devices-changed" not in t  # a longer interval moves no next PM
    monitor.refresh_from_db()
    assert monitor.aem_interval_months == 24
    body = r.content.decode()
    assert f"approved by the Equipment Management Committee on {SHOWN_TODAY}" in body and "EMC minutes, item 4" in body and ">End AEM<" in body


def test_a_shorter_approval_says_how_many_next_pms_move(client, signed_in, monitor, users):
    approve(propose(monitor, users["tech"]), users["manager"])  # 24 months: M-1's next PM stays 10 months out
    Asset.objects.filter(tag="M-1").update(next_pm_on=add_months(months_ago(14), 24))
    d = propose(monitor, users["tech"], 18)
    client.force_login(users["manager"])
    modal = client.get(decide_url(d, "approve"), **HX).content.decode()
    assert "1 device&#x27;s next PM will move earlier" in modal
    r = client.post(decide_url(d), {"decision": "approve", "decided_on": TODAY.isoformat(), "note": "EMC minutes"}, **HX)
    t = assert_saved(r, monitor, "AEM of 18 months approved for Philips IntelliVue MX750; 1 device's next PM moved earlier")
    assert "devices-changed" in t and "wo-changed" in t
    assert Asset.objects.get(tag="M-1").next_pm_on == add_months(months_ago(14), 18)


def test_a_shorter_approval_that_moves_nothing_says_so(client, signed_in, monitor, users):
    approve(propose(monitor, users["tech"]), users["manager"])
    d = propose(monitor, users["tech"], 18)  # M-1's next PM is already within 18 months of its last PM
    client.force_login(users["manager"])
    modal = client.get(decide_url(d, "approve"), **HX).content.decode()
    assert "This interval is shorter than the one in use, and every device's next PM is already within it." in modal
    assert "A longer interval" not in modal and "will move earlier" not in modal


def test_the_proposal_shows_the_history_it_was_made_on_when_it_differs(client, signed_in, monitor, users):
    d = propose(monitor, users["tech"])
    client.force_login(users["manager"])
    body = tab(client, monitor)
    assert "Proposed on the failure history below, unchanged since." in body and "Failure history when proposed" not in body
    create_work_order(asset=Asset.objects.get(tag="M-1"), type=WoType.REPAIR, priority="normal", problem="Alarm fault")
    body = tab(client, monitor)
    assert "Failure history when proposed" in body and "unchanged since" not in body
    assert d.evidence["repairs"] == 0 and '<div class="v">1</div><div class="l">corrective repair,' in body  # today's figures below the snapshot


def test_a_manager_rejects(client, signed_in, monitor, users):
    d = propose(monitor, users["tech"])
    client.force_login(users["manager"])
    assert "<h2>Reject AEM proposal</h2>" in client.get(decide_url(d, "reject"), **HX).content.decode()
    r = client.post(decide_url(d), {"decision": "reject", "decided_on": TODAY.isoformat(), "note": "Another year of data first"}, **HX)
    assert_saved(r, monitor, "AEM proposal for Philips IntelliVue MX750 rejected")
    d.refresh_from_db()
    assert d.status == AemStatus.REJECTED and "Rejected by the committee on" in r.content.decode()


@pytest.mark.parametrize("data, error", [
    ({"decision": "approve", "decided_on": TODAY.isoformat(), "note": ""}, "Enter the committee&#x27;s minutes reference."),
    ({"decision": "approve", "decided_on": "", "note": "x"}, "Enter the date of the committee&#x27;s meeting."),
    ({"decision": "approve", "decided_on": (TODAY + timedelta(days=2)).isoformat(), "note": "x"}, "cannot be dated in the future"),
])
def test_a_refused_decision_stays_in_the_modal(client, signed_in, monitor, users, data, error):
    d = propose(monitor, users["tech"])
    client.force_login(users["manager"])
    r = client.post(decide_url(d), data, **HX)
    assert r.status_code == 200 and "HX-Retarget" not in r and error in r.content.decode()
    d.refresh_from_db()
    assert d.status == AemStatus.PROPOSED


def test_the_proposer_is_told_why_they_cannot_decide(client, signed_in, monitor, users):
    d = propose(monitor, users["manager"])  # the CE manager makes the case: someone else signs off
    client.force_login(users["manager"])
    body = tab(client, monitor)
    assert ">Approve<" not in body and ">Reject<" not in body and f'hx-post="{withdraw_url(d)}"' in body
    assert "You proposed this change. The committee signs off on someone else&#x27;s case" in body
    modal = client.get(decide_url(d, "approve"), **HX).content.decode()
    assert "You proposed this change" in modal and "hx-post" not in modal
    r = client.post(decide_url(d), {"decision": "approve", "decided_on": TODAY.isoformat(), "note": "x"}, **HX)
    assert r.status_code == 200 and "HX-Retarget" not in r and "You proposed this change" in r.content.decode()
    d.refresh_from_db()
    assert d.status == AemStatus.PROPOSED
    client.force_login(users["director"])
    assert ">Approve<" in tab(client, monitor)


def test_a_technician_cannot_decide(client, signed_in, monitor, users):
    d = propose(monitor, users["manager"])
    client.force_login(users["tech"])
    assert client.get(decide_url(d), **HX).status_code == 403
    assert client.post(decide_url(d), {"decision": "approve", "decided_on": TODAY.isoformat(), "note": "x"}, **HX).status_code == 403


# --- withdrawing ----------------------------------------------------------------------------------------------------------


def test_the_proposer_or_an_approver_withdraws(client, signed_in, monitor, users, make_user):
    d = propose(monitor, users["tech"])
    client.force_login(make_user("technician", username="other-tech@riverside.example"))
    assert client.post(withdraw_url(d), **HX).status_code == 403  # PM Edit, but not their case
    assert client.get(withdraw_url(d), **HX).status_code == 405
    client.force_login(users["tech"])
    r = client.post(withdraw_url(d), **HX)
    assert r.status_code == 200 and "HX-Retarget" not in r and "models-changed" in triggers(r)
    assert triggers(r)["toast"]["value"] == "AEM proposal for Philips IntelliVue MX750 withdrawn"
    d.refresh_from_db()
    assert d.status == AemStatus.WITHDRAWN and "Withdrawn by the proposer." in r.content.decode()
    again = client.post(withdraw_url(d), **HX)
    assert again.status_code == 200 and triggers(again)["toast"]["value"] == "This proposal is no longer open: it was withdrawn."
    other = propose(monitor, users["tech"], 18)
    client.force_login(users["manager"])
    assert client.post(withdraw_url(other), **HX).status_code == 200
    other.refresh_from_db()
    assert other.status == AemStatus.WITHDRAWN


# --- ending ---------------------------------------------------------------------------------------------------------------


def test_ending_says_how_many_next_pms_move_and_moves_them(client, signed_in, monitor, users):
    d = approve(propose(monitor, users["tech"]), users["manager"])
    m1 = Asset.objects.get(tag="M-1")
    Asset.objects.filter(pk=m1.pk).update(next_pm_on=add_months(months_ago(14), 24))  # on the 24-month schedule
    wo = create_work_order(asset=m1, type=WoType.PM, priority="normal", problem="Scheduled PM", due_on=add_months(months_ago(14), 24))
    client.force_login(users["tech"])
    assert client.get(end_url(d.pk), **HX).status_code == 403  # PM Edit does not end
    client.force_login(users["manager"])
    modal = client.get(end_url(d.pk), **HX).content.decode()
    assert "<h2>End AEM</h2>" in modal and "1 device&#x27;s next PM will move earlier to fit the OEM interval" in modal
    assert f"Approved by the Equipment Management Committee on {SHOWN_TODAY}" in modal
    r = client.post(end_url(d.pk), {"reason": "Repairs rose at 24 months"}, **HX)
    t = assert_saved(r, monitor, "AEM ended for Philips IntelliVue MX750: back on the OEM interval of 12 months; 1 device's next PM moved earlier")
    assert "devices-changed" in t and "wo-changed" in t
    d.refresh_from_db()
    wo.refresh_from_db()
    assert d.status == AemStatus.ENDED and Asset.objects.get(pk=m1.pk).next_pm_on == TODAY and wo.due_on == TODAY
    assert "Following the OEM interval: every 12 months." in r.content.decode() and "Repairs rose at 24 months" in r.content.decode()


def test_ending_needs_a_reason(client, signed_in, monitor, users):
    d = approve(propose(monitor, users["tech"]), users["manager"])
    client.force_login(users["manager"])
    r = client.post(end_url(d.pk), {"reason": " "}, **HX)
    assert r.status_code == 200 and "HX-Retarget" not in r and "Say why the AEM interval ends." in r.content.decode()
    d.refresh_from_db()
    assert d.status == AemStatus.APPROVED
    aem.end(d, by=users["manager"], reason="Done")
    assert "This AEM interval is no longer in force." in client.get(end_url(d.pk), **HX).content.decode()


def test_an_interval_on_file_without_a_recorded_approval(client, signed_in, ctx, dept, pump_model, pump):
    signed_in("manager")
    body = tab(client, pump_model)
    assert "An AEM interval of 18 months is on file without a recorded approval" in body
    assert f'hx-get="{end_url(pump_model.pk)}"' in body  # ended through the model: there is no decision to name
    assert "On file without a recorded approval" in client.get(end_url(pump_model.pk), **HX).content.decode()
    r = client.post(end_url(pump_model.pk), {"reason": "No approval on record"}, **HX)
    assert_saved(r, pump_model, "AEM ended for BD Alaris 8015 PCU: back on the OEM interval of 12 months")
    pump_model.refresh_from_db()
    assert pump_model.aem_interval_months is None
    assert "This model has no AEM interval in force" in client.get(end_url(pump_model.pk), **HX).content.decode()


# --- the device drawer ----------------------------------------------------------------------------------------------------


def test_the_device_drawer_names_the_committee_date(client, signed_in, monitor, users):
    approve(propose(monitor, users["tech"]), users["manager"])
    client.force_login(users["tech"])
    body = client.get("/equipment/M-1/?tab=pm", **HX).content.decode()
    assert (f"AEM program: the PM interval for this model is extended from the OEM&#x27;s 12 months to 24 months, approved by the Equipment "
            f"Management Committee on {SHOWN_TODAY}. Life-support devices are excluded from AEM by policy.") in body


def test_the_history_lists_every_decision(client, signed_in, monitor, users):
    aem.reject(propose(monitor, users["tech"], 36), by=users["manager"], decided_on=TODAY, note="Too long a step")
    aem.withdraw(propose(monitor, users["tech"], 30), by=users["tech"])
    approve(propose(monitor, users["tech"]), users["manager"])
    client.force_login(users["manager"])
    body = tab(client, monitor)
    assert "<h3>Decisions <span>3</span></h3>" in body
    assert "Too long a step" in body and "Withdrawn by the proposer." in body and ">In force</span>" in body and ">Rejected</span>" in body


# --- tenant isolation -----------------------------------------------------------------------------------------------------


def test_another_facilitys_models_and_decisions_are_404(client, signed_in, monitor, theirs):
    signed_in("director")
    dm, d = theirs["model"], theirs["decision"]
    assert client.get(tab_url(dm), **HX).status_code == 404
    assert client.get(propose_url(dm), **HX).status_code == 404
    assert client.post(propose_url(dm), {"interval_months": "18", "rationale": "x"}, **HX).status_code == 404
    assert client.get(decide_url(d), **HX).status_code == 404
    assert client.post(decide_url(d), {"decision": "approve", "decided_on": TODAY.isoformat(), "note": "x"}, **HX).status_code == 404
    assert client.post(withdraw_url(d), **HX).status_code == 404
    for pk in (d.pk, dm.pk):
        assert client.get(end_url(pk), **HX).status_code == 404
        assert client.post(end_url(pk), {"reason": "x"}, **HX).status_code == 404
    with tenant_context(theirs["decision"].tenant):
        assert AemDecision.objects.get().status == AemStatus.PROPOSED
        assert DeviceModel.objects.get().aem_interval_months == 24
