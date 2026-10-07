"""
The survey binder's scaffold (slice 25): the period, the registry and its access, the two new fields, why a PM was late (set_late_reason
and missed_pms), and how a device was added (create_asset's added_as).
"""
from datetime import date, timedelta

import pytest
from django.core.exceptions import ValidationError
from django.http import QueryDict
from django.utils import timezone

from apps.equipment import services as eq
from apps.equipment.models import AddedAs, Asset, AssetStatus
from apps.pm.services import missed_due_date, missed_pms
from apps.reports import survey
from apps.reports.permissions import survey_refusal
from apps.workorders import permissions as wo_perms
from apps.workorders.models import LateReason, Priority, WoStatus, WoType
from apps.workorders.services import create_work_order, set_late_reason

TODAY = date(2026, 10, 7)


def test_the_default_period_is_twelve_months_from_a_first_of_the_month():
    p = survey.default_period(TODAY)
    assert (p.start, p.end, p.today) == (date(2025, 11, 1), TODAY, TODAY)
    assert p.label == "Nov 1, 2025 to Oct 7, 2026"
    assert survey.default_period(date(2026, 11, 30)).start == date(2025, 12, 1)


def test_a_period_is_refused_in_words():
    assert survey.parse_period(QueryDict("from=2026-01-01&to=2026-06-30"), TODAY).start == date(2026, 1, 1)
    for query, key in (("to=2026-10-08", "to"), ("from=2026-05-01&to=2026-04-01", "from"), ("from=2023-01-01", "from"), ("from=junk", "from")):
        with pytest.raises(ValidationError) as e:
            survey.parse_period(QueryDict(query), TODAY)
        assert key in e.value.message_dict, query


def test_sections_need_their_areas_view(ctx, make_user):
    director, analyst, tech = make_user("director"), make_user("analyst"), make_user("technician")
    assert [s.key for s in survey.binder(director, survey.default_period(TODAY)).sections] == survey.section_keys()
    b = survey.binder(analyst, survey.default_period(TODAY))
    assert [lo.key for lo in b.left_out] == ["staff"] and not b.complete
    assert "Users and access View" in b.left_out[0].reason
    assert {lo.key for lo in survey.binder(tech, survey.default_period(TODAY)).left_out} == {"staff"}
    assert survey_refusal(make_user("requester"), "program").startswith("Needs Reports View")


@pytest.fixture
def pm(ctx, vent_model):
    from apps.equipment.models import Department

    dept = Department.objects.create(name="ICU")
    asset = Asset.objects.create(tag="ICU-9", device_model=vent_model, department=dept)
    return create_work_order(asset=asset, type=WoType.PM, priority=Priority.NORMAL, problem="PM", opened_on=TODAY - timedelta(days=30),
                             due_on=TODAY - timedelta(days=3))


def test_a_late_reason_only_on_a_pm_that_missed_its_due_date(pm, make_user):
    assert missed_due_date(pm, TODAY) and list(missed_pms(TODAY)) == [pm]
    set_late_reason(pm, LateReason.DEVICE_IN_USE, today=TODAY)
    pm.refresh_from_db()
    assert pm.late_reason == "device_in_use"
    assert pm.history.first().history_change_reason == "Why late: Device in use, not available"
    with pytest.raises(ValidationError, match="Choose one"):
        set_late_reason(pm, "because", today=TODAY)
    assert not missed_due_date(pm, pm.due_on)  # not yet due on its due date
    with pytest.raises(ValidationError, match="not a PM that missed"):
        set_late_reason(pm, LateReason.OTHER, today=pm.due_on)
    pm.__class__.objects.filter(pk=pm.pk).update(status=WoStatus.COMPLETED, completed_on=pm.due_on)  # done on time
    assert not missed_due_date(pm, TODAY)


def test_a_closed_pms_reason_needs_approve(pm, make_user):
    tech, manager = make_user("technician"), make_user("manager")
    assert wo_perms.can_set_late_reason(tech, pm)
    pm.status = WoStatus.CLOSED
    assert not wo_perms.can_set_late_reason(tech, pm) and wo_perms.can_set_late_reason(manager, pm)


def test_create_asset_records_how_a_device_was_added(ctx, vent_model):
    from apps.equipment.models import Department

    dept = Department.objects.create(name="OR")
    assert eq.create_asset(tag="N-1", device_model=vent_model, department=dept).added_as == AddedAs.NEW
    assert eq.create_asset(tag="N-2", device_model=vent_model, department=dept, added_as=AddedAs.EXISTING).added_as == "existing"
    with pytest.raises(ValidationError):
        eq.create_asset(tag="N-3", device_model=vent_model, department=dept, added_as="borrowed")
    assert Asset.objects.create(tag="N-4", device_model=vent_model, department=dept, status=AssetStatus.IN_SERVICE).added_as == ""
    assert timezone.localdate()  # the facility's clock is set inside ctx
