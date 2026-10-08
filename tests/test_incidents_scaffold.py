"""
Slice 28 scaffold: the incident models stay in their facility; a role's levels read the same everywhere, with the Incidents module's
default for roles made before it existed; the hold flag's one writer pair and set_status's refusal; the read helpers (the due date,
the clock, what needs action, the hold's words); the nav's Incidents entry and badge.
"""
from datetime import date

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from incident_fixtures import held_incident, make_incident

from apps.accounts.models import Level, Module, Role, RolePermission
from apps.api.serializers_users import RoleSerializer
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel
from apps.equipment.services import HELD_LABEL, clear_incident_hold, set_incident_hold, set_status, status_actions, status_label
from apps.incidents import services as inc
from apps.incidents.models import Affected, Incident, IncidentHold, Outcome
from apps.tenants.context import tenant_context
from apps.workorders.models import WoStatus


def test_incidents_and_holds_stay_in_their_facility(vent, other_tenant):
    held_incident(vent)
    with tenant_context(other_tenant):
        assert not Incident.objects.exists() and not IncidentHold.objects.exists()
        dept = Department.objects.create(name="Their ICU")
        model = DeviceModel.objects.create(manufacturer="GE", model="B650", description="Monitor", category="Monitors")
        theirs = Asset.objects.create(tag="THEIRS-1", device_model=model, department=dept)
        make_incident(theirs, outcome=Outcome.NO_HARM, affected=Affected.NONE, hold=False, open_work_order=False)
        assert Incident.objects.count() == 1
    assert Incident.objects.count() == 1 and Incident.objects.get().asset == vent
    assert IncidentHold.objects.count() == 1


# --- the Incidents module and the levels in force ---------------------------------------------------------------------------------

def _readers(role):
    """Every reader of a role's levels: level_for, the matrix, the API serializer (the PATCH's held levels read role.levels())."""
    from apps.web.views_users import ROLE_MATRIX_MODULES

    assert Module.INCIDENTS in ROLE_MATRIX_MODULES
    return {"level_for": {m: role.level_for(m) for m in Module.values}, "levels": role.levels(),
            "serializer": RoleSerializer().get_levels(role)}


@pytest.mark.parametrize("slug,level", [("director", Level.FULL), ("manager", Level.APPROVE), ("technician", Level.EDIT), ("analyst", Level.VIEW),
                                        ("requester", Level.NONE), ("vendor", Level.NONE)])
def test_a_role_made_before_incidents_takes_its_default(ctx, slug, level):
    role = Role.objects.get(slug=slug)
    RolePermission.objects.filter(role=role, module=Module.INCIDENTS).delete()  # a facility from before slice 28
    role = Role.objects.get(pk=role.pk)
    seen = _readers(role)
    assert seen["level_for"] == seen["levels"] == seen["serializer"]
    assert seen["levels"][Module.INCIDENTS] == level


def test_a_custom_role_without_a_row_has_none_and_a_saved_none_stays(ctx):
    custom = Role.objects.create(name="Quality", slug="quality")
    custom.set_levels({Module.REPORTS: Level.VIEW})
    assert _readers(custom)["levels"][Module.INCIDENTS] == Level.NONE
    tech = Role.objects.get(slug="technician")
    tech.set_levels({Module.INCIDENTS: Level.NONE})
    assert Role.objects.get(pk=tech.pk).level_for(Module.INCIDENTS) == Level.NONE


def test_the_role_matrix_shows_the_level_in_force(ctx, client, make_user):
    RolePermission.objects.filter(module=Module.INCIDENTS).delete()
    client.force_login(make_user("director"))
    html = client.get(reverse("web:roles")).content.decode()
    assert "Incidents" in html


# --- the hold flag ---------------------------------------------------------------------------------------------------------------

def test_holding_takes_a_device_out_and_set_status_refuses_every_move(vent, make_user):
    set_incident_hold(vent)
    assert vent.incident_hold and vent.status == AssetStatus.OUT_OF_SERVICE
    assert status_label(vent) == HELD_LABEL
    assert status_actions(vent, make_user("director")) == []
    for to in (AssetStatus.IN_SERVICE, AssetStatus.MISSING, AssetStatus.RETIRED):
        with pytest.raises(ValidationError, match="held as evidence"):
            set_status(vent, to)
    clear_incident_hold(vent, to_status=AssetStatus.IN_SERVICE)
    assert not vent.incident_hold and vent.status == AssetStatus.IN_SERVICE
    reasons = list(vent.history.order_by("history_date").values_list("history_change_reason", flat=True))
    assert reasons[-2:] == ["Held for an incident investigation", "Hold released"]  # never the incident's number


def test_a_missing_or_retired_device_is_not_held(vent):
    Asset.objects.filter(pk=vent.pk).update(status=AssetStatus.MISSING)
    vent.refresh_from_db()
    with pytest.raises(ValidationError, match="without holding"):
        set_incident_hold(vent)


def test_holding_keeps_a_device_in_repair_in_repair(vent):
    Asset.objects.filter(pk=vent.pk).update(status=AssetStatus.IN_REPAIR)
    set_incident_hold(vent)
    vent.refresh_from_db()
    assert vent.incident_hold and vent.status == AssetStatus.IN_REPAIR


# --- the read helpers ------------------------------------------------------------------------------------------------------------

def test_the_due_date_is_the_tenth_work_day_for_a_patient_of_the_facility():
    aware = date(2026, 10, 8)
    assert inc.due_date(outcome=Outcome.UNKNOWN, affected=Affected.PATIENT, aware_on=aware, reportable=None) == date(2026, 10, 23)
    assert inc.due_date(outcome=Outcome.DEATH, affected=Affected.STAFF, aware_on=aware, reportable=True) == date(2026, 10, 23)
    assert inc.due_date(outcome=Outcome.SERIOUS_INJURY, affected=Affected.OTHER, aware_on=aware, reportable=None) is None  # a visitor
    assert inc.due_date(outcome=Outcome.INJURY, affected=Affected.PATIENT, aware_on=aware, reportable=None) is None
    assert inc.due_date(outcome=Outcome.DEATH, affected=Affected.PATIENT, aware_on=aware, reportable=False) is None


def test_the_clock_and_what_needs_action(vent, pump):
    death = make_incident(vent, outcome=Outcome.DEATH, aware_on=date(2026, 10, 8), occurred_on=date(2026, 10, 8))
    clock = inc.clock(death, date(2026, 10, 19))
    assert (clock.due, clock.left, clock.overdue, clock.soon, clock.recipients) == (date(2026, 10, 23), 4, False, False, ("fda", "manufacturer"))
    assert inc.clock(death, date(2026, 10, 20)).soon and inc.clock(death, date(2026, 10, 26)).overdue
    assert set(inc.needing_action()) == {death}
    Incident.objects.filter(pk=death.pk).update(reportable=True, basis="may_have", manufacturer_reported_on=date(2026, 10, 20))
    death.refresh_from_db()
    assert inc.reports_missing(death) == ("fda",) and set(inc.needing_action()) == {death}
    Incident.objects.filter(pk=death.pk).update(fda_reported_on=date(2026, 10, 21))
    assert not inc.needing_action().exists()
    serious = make_incident(pump, outcome=Outcome.SERIOUS_INJURY, hold=False, reportable=True, fda_reported_on=date(2026, 10, 9))
    assert inc.reports_missing(serious) == ()  # the FDA's copy counts for the manufacturer's (803.30(a)(2))


def test_hold_words_name_the_incident_only_to_incidents_view(vent, make_user):
    incident = held_incident(vent)
    vent.refresh_from_db()
    words = inc.hold_words(vent, make_user("technician"))
    assert incident.number in words["text"] and words["url"] == reverse("web:incident", args=[incident.number])
    for slug in ("requester", "vendor"):
        assert inc.hold_words(vent, make_user(slug)) == {"text": inc.HELD_WORDS, "url": "", "number": ""}
    assert inc.investigation_of(vent) == {incident.work_order_id}
    assert incident.work_order.status == WoStatus.OPEN


def test_the_nav_shows_incidents_and_its_badge(vent, client, make_user):
    make_incident(vent, outcome=Outcome.UNKNOWN)
    client.force_login(make_user("technician"))
    html = client.get(reverse("web:incidents")).content.decode()
    assert 'href="/incidents/"' in html
    client.force_login(make_user("requester"))
    assert client.get(reverse("web:incidents")).status_code == 403
