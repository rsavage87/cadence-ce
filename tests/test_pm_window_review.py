"""
Slice 27 review fixes: the database refuses "days after the due date" with no days; an anchored next PM never lands on or before the
day the PM was done, nor pulls the schedule back from a newer PM's date; the device drawer projects the next PMs as completing today
would; an API client that sends back the whole settings body with a new window gets the default PM policy line following it; days
written with digits int() cannot read are a refusal, not a 500; a lost first-save race starts again from what was sent.
"""
from datetime import date, timedelta

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.equipment.models import Asset, DeviceModel, RiskClass
from apps.facility import services as fac
from apps.facility.models import FacilitySettings
from apps.facility.models import PmWindow as K
from apps.pm.dates import add_months
from apps.web.asset_tabs import pm_upcoming
from apps.workorders.models import Priority, WorkOrder, WoStatus, WoType
from apps.workorders.services import change_status, create_work_order, next_pm_after


def test_the_database_refuses_days_after_with_no_days(ctx):
    s = fac.update_settings(portal_hotline="ext. 1")
    with pytest.raises(IntegrityError), transaction.atomic():
        FacilitySettings.objects.filter(pk=s.pk).update(pm_window_high=K.DAYS_AFTER)


@pytest.fixture
def monthly(ctx, dept):
    model = DeviceModel.objects.create(manufacturer="Acme", model="M1", description="Pump", category="Pumps", risk_class=RiskClass.MEDIUM,
                                       oem_pm_interval_months=1)
    return Asset.objects.create(tag="MO-1", device_model=model, department=dept, next_pm_on=date(2026, 8, 1))


def _pm(asset, due, done=None):
    wo = create_work_order(asset=asset, type=WoType.PM, priority=Priority.NORMAL, problem="PM", opened_on=due - timedelta(days=5), due_on=due)
    if done:
        WorkOrder.objects.filter(pk=wo.pk).update(status=WoStatus.COMPLETED, completed_on=done)
        wo.refresh_from_db()
    return wo


def test_an_anchored_next_pm_never_lands_on_or_before_the_day_done(monthly, make_user):
    fac.update_settings(by=make_user("director"), pm_window_other=K.DAYS_AFTER, pm_window_other_days=45)
    wo = _pm(monthly, date(2026, 8, 1))
    done = date(2026, 9, 10)  # 40 days late, inside the window: Aug 1 + 1 month = Sep 1 is already past
    assert next_pm_after(wo, monthly, done) == add_months(done, 1)
    wo2 = _pm(monthly, date(2026, 8, 1))
    assert next_pm_after(wo2, monthly, date(2026, 8, 20)) == date(2026, 9, 1)  # anchored: after the day done


def test_an_older_pm_completed_again_never_pulls_the_schedule_back(monthly, make_user):
    fac.update_settings(by=make_user("director"), pm_window_other=K.NEXT_MONTH)
    a = _pm(monthly, date(2026, 3, 10), done=date(2026, 3, 10))
    Asset.objects.filter(pk=monthly.pk).update(next_pm_on=date(2026, 5, 8))  # a newer PM, done Apr 8, set May 8
    monthly.refresh_from_db()
    change_status(a, WoStatus.IN_PROGRESS)  # reopened to correct a reading
    assert next_pm_after(a, monthly, date(2026, 4, 20)) == date(2026, 5, 20)  # from the day done, never back to Apr 10


def test_the_drawer_projects_from_the_due_date_inside_the_window(ctx, dept, make_user):
    model = DeviceModel.objects.create(manufacturer="Acme", model="Q3", description="Monitor", category="Monitors", risk_class=RiskClass.MEDIUM,
                                       oem_pm_interval_months=3)
    today = date(2026, 10, 8)
    device = Asset.objects.create(tag="PR-1", device_model=model, department=dept, next_pm_on=date(2026, 9, 10))
    assert pm_upcoming(device, None, today)["rows"][1]["on"] == add_months(today, 3)  # the default window: from today
    fac.update_settings(by=make_user("director"), pm_window_other=K.NEXT_MONTH)
    up = pm_upcoming(device, None, today)
    assert up["rows"][1]["on"] == date(2026, 12, 10) and up["from_due"]


def test_a_full_body_patch_with_a_new_window_makes_the_default_line_follow(ctx, client, make_user):
    client.force_login(make_user("director"))
    body = client.get("/api/v1/settings/").json()
    body["pm_window_other"] = "due_month"
    assert client.patch("/api/v1/settings/", body, content_type="application/json").status_code == 200
    assert fac.get_settings().policy_medium_low == "AEM allowed, complete by the end of the due month"
    body = client.get("/api/v1/settings/").json()
    body["pm_window_other"], body["policy_medium_low"] = "due_date", "Our own words"
    client.patch("/api/v1/settings/", body, content_type="application/json")
    assert fac.get_settings().policy_medium_low == "Our own words"  # text the client changed is its own


@pytest.mark.parametrize("days", ["²", "①", "9" * 5000, "٣"])
def test_digits_int_cannot_read_are_refused_in_words(ctx, client, make_user, days):
    with pytest.raises(ValidationError) as e:
        fac.update_settings(pm_window_high=K.DAYS_AFTER, pm_window_high_days=days)
    assert "pm_window_high_days" in e.value.message_dict
    client.force_login(make_user("director"))
    r = client.post("/settings/pm-window/", {"pm_window_high": "days_after", "pm_window_high_days": days, "pm_window_other": "due_date"},
                    HTTP_HX_REQUEST="true")
    assert r.status_code == 200 and "whole number of days" in r.content.decode()


def test_a_lost_first_save_race_starts_again_from_what_was_sent(ctx, monkeypatch, make_user):
    kim = make_user("director")
    fac.update_settings(by=kim, policy_life_support="Our own words about the due date")  # the other director's save won the row
    calls = {"n": 0}
    real = fac._locked_row

    def first_none():
        calls["n"] += 1
        return None if calls["n"] == 1 else real()

    monkeypatch.setattr(fac, "_locked_row", first_none)
    s = fac.update_settings(by=kim, pm_window_high=K.DUE_MONTH)
    assert s.pm_window_high == K.DUE_MONTH and s.policy_life_support == "Our own words about the due date"
