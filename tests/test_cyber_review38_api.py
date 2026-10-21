"""
Slice 38 review fixes, the API (apps/api/views.py), on SQLite and PostgreSQL:

- C5, C8, C16: an edit (PATCH or PUT) of a work order that sends software_outcome, software_release, software_reference, or
  software_basis is refused, keyed by each field sent, in SOFTWARE_RECORDED's words, as create and every other move refuse them: never a
  200 that records nothing. Sending back what a GET returned (software_update, software_choices) is fine: no GET returns those fields.
- C6, C19: a transition to completed sending software_* fields but no software_outcome answers in the API's words, never the modal's
  ("Choose what this update did, or Not confirmed now."): a user who may record what the update did is told what to send (SEND_OUTCOME),
  on a first completion and on a re-completion; anyone else is told why nothing is recorded, in the words an outcome sent would get
  (RECORD_PERMISSION for a user without Device security Edit and for a scoped vendor, who learns nothing more; ONLY_UPDATES on another
  type; NOT_FROM_REGISTER on an update imported from another system). Nothing is recorded and the work order stays as it was.
- Round 2 (skeptics' "api"): on a work order that cannot be completed now (completed, completed with the version not confirmed,
  cancelled, not assigned), software fields with no software_outcome, or a software_outcome not one of the values, answer why it cannot
  be completed ({"detail": completion.blocker's words}), as the same request with no software fields and with a valid outcome do: never
  SEND_OUTCOME, which software_choices (null there) would not offer. A software_outcome not one of the values answers OUTCOME_WORDS only
  to a user who may record; anyone else gets the reason there is none, as a missing one does (RECORD_PERMISSION for a scoped vendor and
  for a user without Device security Edit, ONLY_UPDATES on another type, NOT_FROM_REGISTER on an imported update).
"""
import pytest
from cyber_fixtures import update_wo
from test_cyber_updates_api import WORK_ORDERS, custom_user, never, ok, today, transition, world  # noqa: F401

from apps.accounts.models import Level, Module
from apps.api.views import SOFTWARE_RECORDED
from apps.cybersecurity import rules, updates
from apps.cybersecurity.models import SoftwareUpdate
from apps.equipment.models import Asset
from apps.workorders import services as wo_services
from apps.workorders.models import Priority, Source, WorkOrder, WoStatus, WoType

SOFTWARE = {"software_outcome": "installed", "software_release": None, "software_reference": "ICAR-2026-09",
            "software_basis": "manufacturer_release"}


def software(w) -> dict:
    return {**SOFTWARE, "software_release": str(w["r56"].pk)}


def unchanged(wo, release):
    """Nothing recorded on an open update: still open, no outcome on its record, and the device's release as it was."""
    assert WorkOrder.objects.get(pk=wo.pk).status == WoStatus.OPEN
    assert SoftwareUpdate.objects.get(work_order=wo).outcome == ""
    assert rules.release_of(Asset.objects.get(pk=wo.asset_id)) == release


# --- C5, C8, C16: an edit refuses the software fields --------------------------------------------------------------------------------

@pytest.mark.parametrize("field", list(SOFTWARE))
def test_a_patch_sending_a_software_field_is_refused_keyed_by_it(client, make_user, world, field):  # noqa: F811
    wo = world["wos"]["CE-10002"]
    client.force_login(make_user("director"))
    r = client.patch(f"{WORK_ORDERS}{wo.pk}/", {field: software(world)[field]}, content_type="application/json")
    assert r.status_code == 400 and r.json() == {field: [SOFTWARE_RECORDED]}
    assert not never(r.content.decode())
    row = SoftwareUpdate.objects.get(work_order=wo)
    assert (row.outcome, WorkOrder.objects.get(pk=wo.pk).status) == ("", WoStatus.OPEN)
    assert rules.release_of(Asset.objects.get(pk=wo.asset_id)) == world["r52"]


def test_a_patch_sending_every_software_field_is_refused_keyed_by_each(client, make_user, world):  # noqa: F811
    wo = world["wos"]["CE-10002"]
    client.force_login(make_user("director"))
    r = client.patch(f"{WORK_ORDERS}{wo.pk}/", {"priority": "high", **software(world)}, content_type="application/json")
    assert r.status_code == 400 and r.json() == {f: [SOFTWARE_RECORDED] for f in SOFTWARE}
    assert WorkOrder.objects.get(pk=wo.pk).priority == Priority.NORMAL  # the edit is refused whole, never half saved
    assert SoftwareUpdate.objects.get(work_order=wo).outcome == ""


def test_a_patch_on_a_completed_update_refuses_them_too(client, make_user, world):  # noqa: F811
    """A completed update: no other field changes, so the "Reopen it first" refusal never fires; the software fields are still refused."""
    wo = world["wos"]["CE-10004"]  # completed, version not confirmed
    client.force_login(make_user("director"))
    r = client.patch(f"{WORK_ORDERS}{wo.pk}/", {"software_outcome": "installed", "software_release": str(world["r56"].pk),
                                                "software_basis": "not_stated"}, content_type="application/json")
    assert r.status_code == 400 and r.json() == {f: [SOFTWARE_RECORDED] for f in ("software_outcome", "software_release", "software_basis")}
    assert SoftwareUpdate.objects.get(work_order=wo).outcome == ""
    assert rules.release_of(Asset.objects.get(pk=wo.asset_id)) == world["r52"]


def test_a_put_sending_software_fields_is_refused(client, make_user, world):  # noqa: F811
    wo = world["wos"]["CE-10002"]
    client.force_login(make_user("director"))
    body = {"asset": str(wo.asset_id), "type": "software_update", "problem": wo.problem, "due_on": wo.due_on.isoformat()}
    r = client.put(f"{WORK_ORDERS}{wo.pk}/", {**body, **software(world)}, content_type="application/json")
    assert r.status_code == 400 and r.json() == {f: [SOFTWARE_RECORDED] for f in SOFTWARE}
    assert SoftwareUpdate.objects.get(work_order=wo).outcome == ""
    assert ok(client.put(f"{WORK_ORDERS}{wo.pk}/", body, content_type="application/json"))["status"] == "open"  # the same edit without them


def test_sending_back_what_a_get_returned_is_fine(client, make_user, world):  # noqa: F811
    wo = world["wos"]["CE-10002"]
    client.force_login(make_user("director"))
    got = ok(client.get(f"{WORK_ORDERS}{wo.pk}/"))
    assert got["software_update"] is not None and got["software_choices"] is not None
    sent_back = {"priority": "high", "software_update": got["software_update"], "software_choices": got["software_choices"],
                 "software_outcome": "", "software_release": None}  # blank or null: nothing sent
    data = ok(client.patch(f"{WORK_ORDERS}{wo.pk}/", sent_back, content_type="application/json"))
    assert data["priority"] == "high" and data["software_update"]["state"] == "open"


# --- C6, C19: software fields with no software_outcome --------------------------------------------------------------------------------

@pytest.mark.parametrize("field", ["software_release", "software_reference", "software_basis"])
def test_a_user_who_may_record_is_told_what_to_send(client, world, field):  # noqa: F811
    wo = world["wos"]["CE-10002"]
    client.force_login(world["tech"])
    r = transition(client, wo, resolution="Loaded 5.6", **{field: software(world)[field]})
    assert r.status_code == 400 and r.json() == {"software_outcome": [updates.SEND_OUTCOME]}
    assert updates.CHOOSE_OUTCOME not in r.content.decode() and not never(r.content.decode())
    unchanged(wo, world["r52"])


def test_a_re_completion_names_what_to_send_too(client, make_user, world):  # noqa: F811
    wo = world["wos"]["CE-10003"]  # completed as installed (5.6-DEMO)
    wo_services.change_status(wo, WoStatus.IN_PROGRESS)  # reopened: what it recorded stays
    client.force_login(make_user("director"))
    r = transition(client, wo, resolution="Checked again", software_basis="not_stated")
    assert r.status_code == 400 and r.json() == {"software_outcome": [updates.SEND_OUTCOME]}
    assert WorkOrder.objects.get(pk=wo.pk).status == WoStatus.IN_PROGRESS
    assert SoftwareUpdate.objects.get(work_order=wo).outcome == "installed"
    assert ok(transition(client, wo, resolution="Checked again"))["status"] == "completed"  # left out: what was recorded stays


def test_the_other_refusals_still_come_with_it(client, world):  # noqa: F811
    wo = world["wos"]["CE-10002"]
    client.force_login(world["tech"])
    r = transition(client, wo, resolution=["Loaded 5.6"], software_release=str(world["r56"].pk))
    assert r.status_code == 400 and r.json() == {"resolution": ["Send this as text."], "software_outcome": [updates.SEND_OUTCOME]}


def _no_offer_answers(client, wo, sent: dict) -> tuple:
    """The answer to software fields with no outcome, and the software_outcome words the same with an outcome gets (what the user
    already learns: the service reports every other problem beside it)."""
    missing = transition(client, wo, resolution="Done", **sent)
    with_outcome = transition(client, wo, resolution="Done", software_outcome="not_confirmed", **sent)
    assert missing.status_code == 400 and with_outcome.status_code == 400, (missing.content, with_outcome.content)
    return missing.json(), {"software_outcome": with_outcome.json()["software_outcome"]}


def test_a_user_without_device_security_edit_is_told_why_never_to_choose(client, tenant, world):  # noqa: F811
    wo = world["wos"]["CE-10002"]
    client.force_login(custom_user(tenant, "woedit", {Module.WORKORDERS: Level.EDIT, Module.EQUIPMENT: Level.VIEW}))
    missing, with_outcome = _no_offer_answers(client, wo, {"software_release": str(world["r56"].pk)})
    assert missing == {"software_outcome": [updates.RECORD_PERMISSION]} == with_outcome
    unchanged(wo, world["r52"])


def test_a_scoped_vendor_learns_nothing_more_than_the_permission(client, make_user, world):  # noqa: F811
    vendor = make_user("vendor")
    vendor.company = "BD"
    vendor.save(update_fields=["company"])
    wo = world["wos"]["CE-10008"]
    client.force_login(vendor)
    missing, with_outcome = _no_offer_answers(client, wo, {"software_reference": "X-1"})
    assert missing == {"software_outcome": [updates.RECORD_PERMISSION]} == with_outcome
    assert "DEMO" not in str(missing) and not never(str(missing))
    unchanged(wo, world["r52"])
    assert ok(transition(client, wo, resolution="Updated per our visit"))["status"] == "completed"  # left out: completes, records nothing
    assert SoftwareUpdate.objects.get(work_order=wo).outcome == ""


def test_another_type_answers_only_updates(client, make_user, world, techs):  # noqa: F811
    repair = wo_services.create_work_order(asset=world["devices"]["CE-10002"], type=WoType.REPAIR, priority=Priority.NORMAL,
                                           problem="Door latch loose", assigned_to=techs["tom"])
    wo_services.change_status(repair, WoStatus.IN_PROGRESS)  # a repair is started before it is completed
    client.force_login(make_user("director"))
    missing, with_outcome = _no_offer_answers(client, repair, {"software_release": str(world["r56"].pk)})
    assert missing == {"software_outcome": [updates.ONLY_UPDATES]} == with_outcome
    assert WorkOrder.objects.get(pk=repair.pk).status == WoStatus.IN_PROGRESS


def test_an_imported_update_answers_that_nothing_is_recorded_on_it(client, make_user, world, techs):  # noqa: F811
    imported = wo_services.create_work_order(asset=world["devices"]["CE-10006"], type=WoType.SOFTWARE_UPDATE, priority=Priority.NORMAL,
                                             problem="Firmware 5.6 loaded", source=Source.IMPORTED, _register_opener=True,
                                             assigned_to=techs["tom"])
    client.force_login(make_user("director"))
    missing, with_outcome = _no_offer_answers(client, imported, {"software_basis": "not_stated"})
    assert missing == {"software_outcome": [updates.NOT_FROM_REGISTER]} == with_outcome
    assert WorkOrder.objects.get(pk=imported.pk).status == WoStatus.OPEN


def test_the_modals_words_stay_the_modals():
    """choice_from (the service the modal's form shares) keeps CHOOSE_OUTCOME: only the API rewords it."""
    errors: dict = {}
    assert updates.choice_from({"software_release": "x"}, errors) is updates.KEEP
    assert errors == {"software_outcome": updates.CHOOSE_OUTCOME}


# --- Round 2: a work order that cannot be completed now, and a software_outcome not one of the values ---------------------------------

BLOCKED = {"CE-10003": "{number} is already completed.", "CE-10004": "{number} is already completed.", "CE-10005": "{number} was cancelled."}


@pytest.mark.parametrize("sent", [{"software_basis": "not_stated"}, {"software_reference": "ICAR-2026-09"}, {"software_outcome": "bogus"}],
                         ids=["basis", "reference", "bogus"])
@pytest.mark.parametrize("tag", list(BLOCKED))
def test_a_work_order_that_cannot_be_completed_answers_why_never_what_to_send(client, make_user, world, tag, sent):  # noqa: F811
    """CE-10003 completed (installed), CE-10004 completed (version not confirmed), CE-10005 cancelled: software_choices is null on each,
    so the director is never told to send software_outcome; the answer is the one with no software fields, and with a valid outcome."""
    wo = world["wos"][tag]
    before = (wo.status, SoftwareUpdate.objects.get(work_order=wo).outcome, rules.release_of(Asset.objects.get(pk=wo.asset_id)))
    client.force_login(make_user("director"))
    assert ok(client.get(f"{WORK_ORDERS}{wo.pk}/"))["software_choices"] is None
    words = {"detail": BLOCKED[tag].format(number=wo.number)}
    r = transition(client, wo, resolution="Done", **sent)
    assert r.status_code == 400 and r.json() == words, r.content
    assert updates.SEND_OUTCOME not in r.content.decode() and updates.OUTCOME_WORDS not in r.content.decode()
    assert transition(client, wo, resolution="Done").json() == words  # no software fields
    assert transition(client, wo, resolution="Done", software_outcome="not_confirmed").json() == words  # a valid outcome
    after = (WorkOrder.objects.get(pk=wo.pk).status, SoftwareUpdate.objects.get(work_order=wo).outcome,
             rules.release_of(Asset.objects.get(pk=wo.asset_id)))
    assert after == before


def test_an_unassigned_update_answers_that_it_is_not_assigned(client, make_user, world):  # noqa: F811
    """software_choices offers the outcomes here (open, Work orders Edit), but completing it is refused until it is assigned: that is the
    answer, whatever software fields were sent."""
    wo, _row = update_wo(world["devices"]["CE-10007"], world["r56"])
    client.force_login(make_user("director"))
    words = {"detail": f"{wo.number} is not assigned. A CE manager assigns it to whoever does the work before it is completed."}
    for sent in ({"software_release": str(world["r56"].pk)}, {"software_outcome": "bogus"}, {}, {"software_outcome": "not_confirmed"}):
        r = transition(client, wo, resolution="Done", **sent)
        assert r.status_code == 400 and r.json() == words, (sent, r.content)
    unchanged(wo, world["r52"])


def test_another_refusal_comes_first_as_with_no_software_fields(client, make_user, world):  # noqa: F811
    """A text field sent as a list is refused before the work order's state is read, with or without software fields."""
    wo = world["wos"]["CE-10005"]
    client.force_login(make_user("director"))
    words = {"resolution": ["Send this as text."]}
    assert transition(client, wo, resolution=["Done"]).json() == words
    assert transition(client, wo, resolution=["Done"], software_basis="not_stated").json() == words


def test_a_scoped_vendor_on_its_cancelled_update_learns_only_the_status(client, make_user, world):  # noqa: F811
    vendor = make_user("vendor")
    vendor.company = "BD"
    vendor.save(update_fields=["company"])
    wo = world["wos"]["CE-10008"]
    wo_services.change_status(wo, WoStatus.CANCELLED)
    client.force_login(vendor)
    words = {"detail": f"{wo.number} was cancelled."}
    for sent in ({"software_reference": "X-1"}, {"software_outcome": "bogus"}, {}, {"software_outcome": "installed"}):
        r = transition(client, wo, resolution="Done", **sent)
        assert r.status_code == 400 and r.json() == words, (sent, r.content)


def test_a_user_who_may_record_is_told_the_values(client, world):  # noqa: F811
    wo = world["wos"]["CE-10002"]
    client.force_login(world["tech"])
    r = transition(client, wo, resolution="Loaded 5.6", software_outcome="bogus", software_release=str(world["r56"].pk))
    assert r.status_code == 400 and r.json() == {"software_outcome": [updates.OUTCOME_WORDS]}
    unchanged(wo, world["r52"])


def _bogus_answers(client, wo, sent: dict) -> tuple:
    """The answer to a software_outcome not one of the values, and the software_outcome words a valid one gets."""
    bogus = transition(client, wo, resolution="Done", software_outcome="bogus", **sent)
    valid = transition(client, wo, resolution="Done", software_outcome="not_confirmed", **sent)
    assert bogus.status_code == 400 and valid.status_code == 400, (bogus.content, valid.content)
    return bogus.json(), {"software_outcome": valid.json()["software_outcome"]}


def test_a_scoped_vendor_sending_an_unknown_outcome_learns_only_the_permission(client, make_user, world):  # noqa: F811
    vendor = make_user("vendor")
    vendor.company = "BD"
    vendor.save(update_fields=["company"])
    wo = world["wos"]["CE-10008"]
    client.force_login(vendor)
    bogus, valid = _bogus_answers(client, wo, {})
    assert bogus == {"software_outcome": [updates.RECORD_PERMISSION]} == valid
    assert updates.OUTCOME_WORDS not in str(bogus) and not never(str(bogus))
    unchanged(wo, world["r52"])


def test_a_user_without_device_security_sending_an_unknown_outcome_is_told_why(client, tenant, world):  # noqa: F811
    wo = world["wos"]["CE-10002"]
    client.force_login(custom_user(tenant, "woedit", {Module.WORKORDERS: Level.EDIT, Module.EQUIPMENT: Level.VIEW}))
    bogus, valid = _bogus_answers(client, wo, {"software_release": str(world["r56"].pk)})
    assert bogus == {"software_outcome": [updates.RECORD_PERMISSION]} == valid
    unchanged(wo, world["r52"])


def test_an_unknown_outcome_on_another_type_answers_only_updates(client, make_user, world, techs):  # noqa: F811
    repair = wo_services.create_work_order(asset=world["devices"]["CE-10002"], type=WoType.REPAIR, priority=Priority.NORMAL,
                                           problem="Door latch loose", assigned_to=techs["tom"])
    wo_services.change_status(repair, WoStatus.IN_PROGRESS)
    client.force_login(make_user("director"))
    bogus, valid = _bogus_answers(client, repair, {})
    assert bogus == {"software_outcome": [updates.ONLY_UPDATES]} == valid
    assert WorkOrder.objects.get(pk=repair.pk).status == WoStatus.IN_PROGRESS


def test_an_unknown_outcome_on_an_imported_update_answers_that_nothing_is_recorded(client, make_user, world, techs):  # noqa: F811
    imported = wo_services.create_work_order(asset=world["devices"]["CE-10006"], type=WoType.SOFTWARE_UPDATE, priority=Priority.NORMAL,
                                             problem="Firmware 5.6 loaded", source=Source.IMPORTED, _register_opener=True,
                                             assigned_to=techs["tom"])
    client.force_login(make_user("director"))
    bogus, valid = _bogus_answers(client, imported, {})
    assert bogus == {"software_outcome": [updates.NOT_FROM_REGISTER]} == valid
    assert WorkOrder.objects.get(pk=imported.pk).status == WoStatus.OPEN
