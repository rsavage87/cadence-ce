"""
The PM completion window (slice 27 scaffold: apps/pm/windows.py, the PM services on it, Settings' window fields). The database rule and the
Python rule agree over month ends, year ends, and Feb 29, for every kind and a mixed pair (on SQLite and, in CI, PostgreSQL); the
default window is the SQL the code had before; the services count by the window (open PMs never dropped: the negation trap); Settings
checks a window on the saved row with the change applied, and the default PM policy lines follow it.
"""
from datetime import date, timedelta

import pytest
from django.core.exceptions import ValidationError
from django.db.models import F, Q

from apps.api.serializers import FacilitySettingsSerializer
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel, RiskClass
from apps.facility import services as fac
from apps.facility.models import PmWindow
from apps.pm import windows as W
from apps.pm.services import assets_past_window, missed_pms, overdue_assets, pm_due_queryset, pm_on_time_rate, pm_on_time_series, pm_pending
from apps.workorders.models import Priority, WorkOrder, WoStatus, WoType
from apps.workorders.services import create_work_order

K = PmWindow
KINDS = [W.Window(), W.Window(K.DAYS_AFTER, 1), W.Window(K.DAYS_AFTER, 14), W.Window(K.DAYS_AFTER, 45), W.Window(K.DUE_MONTH),
         W.Window(K.NEXT_MONTH)]
PAIRS = [W.Windows(k, k) for k in KINDS] + [W.Windows(W.Window(K.DUE_MONTH), W.Window(K.NEXT_MONTH)),
                                            W.Windows(W.Window(), W.Window(K.DAYS_AFTER, 14))]
DUES = [date(2026, 1, 31), date(2026, 2, 28), date(2026, 12, 10), date(2026, 12, 31), date(2028, 2, 29), date(2027, 3, 3)]
AFTER = [0, 1, 14, 15, 28, 29, 30, 31, 45, 46, 60, 62]


def test_the_default_window_is_the_old_sql():
    d = W.DEFAULT
    assert d.is_default and d.uniform
    assert d.on_time_q() == Q(completed_on__lte=F("due_on"))
    assert d.past_window_q(date(2026, 10, 8)) == Q(due_on__lt=date(2026, 10, 8))
    assert d.not_on_time_q() == Q(completed_on__isnull=True) | Q(completed_on__gt=F("due_on"))


@pytest.mark.parametrize("w", KINDS, ids=lambda w: f"{w.kind}-{w.days}")
def test_cutoff_inverts_end(w):
    for due in DUES:
        for k in range(-5, 120):
            today = due + timedelta(days=k)
            assert (due < w.cutoff(today)) == (w.end(due) < today), (due, today)


@pytest.fixture
def grid(ctx, vent_model):
    medium = DeviceModel.objects.create(manufacturer="Philips", model="MX450", description="Monitor", category="Monitors",
                                        risk_class=RiskClass.MEDIUM, oem_pm_interval_months=12)
    dept = Department.objects.create(name="Grid")
    devices = [Asset.objects.create(tag=f"G-{m.pk.hex[:4]}", device_model=m, department=dept) for m in (vent_model, medium)]
    rows = []
    for device in devices:
        for due in DUES:
            for after in AFTER:
                wo = create_work_order(asset=device, type=WoType.PM, priority=Priority.NORMAL, problem="PM", opened_on=due - timedelta(days=3),
                                       due_on=due)
                WorkOrder.objects.filter(pk=wo.pk).update(status=WoStatus.COMPLETED, completed_on=due + timedelta(days=after))
                rows.append((wo.pk, due, due + timedelta(days=after), device.device_model.risk_class))
            wo = create_work_order(asset=device, type=WoType.PM, priority=Priority.NORMAL, problem="PM", opened_on=due - timedelta(days=3),
                                   due_on=due)  # still open
            rows.append((wo.pk, due, None, device.device_model.risk_class))
    return rows


@pytest.mark.parametrize("w", PAIRS, ids=lambda w: f"{w.high.kind}{w.high.days}-{w.other.kind}{w.other.days}")
def test_the_database_and_python_agree(grid, w):
    ids = [r[0] for r in grid]
    qs = WorkOrder.objects.filter(pk__in=ids)
    on_time = set(qs.filter(w.on_time_q()).values_list("pk", flat=True))
    not_on_time = set(qs.filter(w.not_on_time_q()).values_list("pk", flat=True))
    expected = {pk for pk, due, done, rc in grid if done is not None and done <= w.end(due, rc)}
    assert on_time == expected
    assert not_on_time == set(ids) - expected  # every open PM is in it: never dropped by a negated month comparison
    for today in (date(2026, 2, 1), date(2026, 3, 31), date(2027, 1, 1), date(2027, 2, 1), date(2028, 3, 31), date(2028, 4, 1)):
        past = set(qs.filter(w.past_window_q(today)).values_list("pk", flat=True))
        inside = set(qs.filter(w.inside_window_q(today)).values_list("pk", flat=True))
        assert past == {pk for pk, due, _d, rc in grid if w.end(due, rc) < today}, today
        assert inside == {pk for pk, due, _d, rc in grid if due < today <= w.end(due, rc)}, today


@pytest.fixture
def month(ctx, vent_model, pump_model, dept):
    """Today Oct 20; a life-support PM due Oct 5 still open, one due Oct 3 done Oct 15, one due Sep 28 done Oct 2, one due Sep 10 open."""
    today = date(2026, 10, 20)
    vent = Asset.objects.create(tag="M-1", device_model=vent_model, department=dept, next_pm_on=date(2026, 10, 5))
    out = {}
    for key, due, done in (("open", date(2026, 10, 5), None), ("late_in", date(2026, 10, 3), date(2026, 10, 15)),
                           ("next", date(2026, 9, 28), date(2026, 10, 2)), ("old_open", date(2026, 9, 10), None)):
        wo = create_work_order(asset=vent, type=WoType.PM, priority=Priority.HIGH, problem="PM", opened_on=due - timedelta(days=5), due_on=due)
        if done:
            WorkOrder.objects.filter(pk=wo.pk).update(status=WoStatus.COMPLETED, completed_on=done)
        out[key] = wo
    return today, out


def test_the_services_count_by_the_window(month):
    today, wo = month
    default, by_month = W.DEFAULT, W.Windows(W.Window(K.DUE_MONTH), W.Window(K.DUE_MONTH))
    start = date(2026, 9, 1)
    assert pm_on_time_rate(start, today, today, w=default) == {"due": 4, "on_time": 0, "rate": 0.0}
    r = pm_on_time_rate(start, today, today, w=by_month)
    assert (r["due"], r["on_time"]) == (3, 1)  # Oct 5 open is still inside its month; Oct 3 done Oct 15 on time; the two September ones late
    assert pm_pending(start, today, today, w=by_month) == 1 and pm_pending(start, today, today, w=default) == 0
    assert set(missed_pms(today, w=by_month)) == {wo["next"], wo["old_open"]}  # the open September PM is there: the negation trap
    assert set(missed_pms(today, w=default)) == set(wo.values())
    assert set(pm_due_queryset(start, today, today, w=by_month)) == {wo["late_in"], wo["next"], wo["old_open"]}
    points = {(p["year"], p["month"]): p for p in pm_on_time_series(2026, 10, months=2, today=today, w=by_month)}
    assert (points[(2026, 10)]["due"], points[(2026, 10)]["on_time"]) == (1, 1) and (points[(2026, 9)]["due"], points[(2026, 9)]["on_time"]) == (2, 0)


def test_overdue_stays_on_the_due_date_and_compliance_reads_the_window(month):
    today, _wo = month
    by_month = W.Windows(W.Window(K.DUE_MONTH), W.Window(K.DUE_MONTH))
    assert [a.tag for a in overdue_assets(today)] == ["M-1"]  # the schedule: past its due date
    assert list(assets_past_window(today, w=by_month)) == []  # compliance: its month has not ended
    assert [a.tag for a in assets_past_window(date(2026, 11, 1), w=by_month)] == ["M-1"]


# --- Settings --------------------------------------------------------------------------------------------------------------------


def test_a_window_is_checked_with_the_change_applied(ctx, make_user):
    kim = make_user("director")
    with pytest.raises(ValidationError) as e:
        fac.update_settings(by=kim, pm_window_high=K.DAYS_AFTER)
    assert "pm_window_high_days" in e.value.message_dict
    s = fac.update_settings(by=kim, pm_window_high=K.DAYS_AFTER, pm_window_high_days="14")
    assert (s.pm_window_high, s.pm_window_high_days) == (K.DAYS_AFTER, 14)
    s = fac.update_settings(by=kim, pm_window_high_days=21)  # days alone, the saved kind takes them
    assert s.pm_window_high_days == 21
    s = fac.update_settings(by=kim, pm_window_high=K.DUE_MONTH)  # a kind that takes none: its days are cleared
    assert (s.pm_window_high, s.pm_window_high_days) == (K.DUE_MONTH, None)
    for fields, key in (({"pm_window_high_days": 5}, "pm_window_high_days"), ({"pm_window_other": "soon"}, "pm_window_other"),
                        ({"pm_window_other": K.DAYS_AFTER, "pm_window_other_days": 46}, "pm_window_other_days"),
                        ({"pm_window_other": K.DAYS_AFTER, "pm_window_other_days": True}, "pm_window_other_days"),
                        ({"pm_window_other": K.DAYS_AFTER, "pm_window_other_days": "14.5"}, "pm_window_other_days"),
                        ({"pm_window_other": K.DUE_DATE, "pm_window_other_days": 3}, "pm_window_other_days")):
        with pytest.raises(ValidationError) as e:
            fac.update_settings(by=kim, **fields)
        assert key in e.value.message_dict, fields


def test_the_default_pm_policy_lines_follow_the_window(ctx, make_user):
    kim = make_user("director")
    s = fac.update_settings(by=kim, pm_window_high=K.DUE_MONTH)
    assert s.policy_life_support == "OEM interval, complete by the end of the due month"
    assert s.policy_medium_low == "AEM allowed, complete by the due date"  # its group's window did not change
    items = {i["field"]: i for i in fac.policy_items(s)}
    assert items["policy_life_support"]["is_default"]
    s = fac.update_settings(by=kim, policy_life_support="Our own words: within the scheduled month")
    s = fac.update_settings(by=kim, pm_window_high=K.NEXT_MONTH)
    assert s.policy_life_support == "Our own words: within the scheduled month"  # written by the facility: left alone
    s = fac.reset_policy(by=kim)
    assert s.policy_life_support == "OEM interval, complete by the end of the month after the due month"
    assert not W.policy_disagrees(s.policy_life_support, W.windows(s).high)
    assert W.policy_disagrees("OEM interval, complete by the due date, no grace", W.Window(K.DUE_MONTH))
    assert W.policy_disagrees("complete within due month, no grace", W.Window())
    assert not W.policy_disagrees("complete within due month, no grace", W.Window(K.DUE_MONTH))


def test_the_api_reads_and_writes_every_setting(ctx, client, make_user):
    assert set(FacilitySettingsSerializer().fields) == set(fac.EDITABLE) | {"time_zone", "updated_at"}
    client.force_login(make_user("director"))
    r = client.patch("/api/v1/settings/", {"pm_window_other": "days_after", "pm_window_other_days": 30}, content_type="application/json")
    assert r.status_code == 200, r.content
    got = client.get("/api/v1/settings/").json()
    assert (got["pm_window_other"], got["pm_window_other_days"], got["pm_window_high"], got["pm_window_high_days"]) == ("days_after", 30, "due_date", None)
    assert client.patch("/api/v1/settings/", got, content_type="application/json").status_code == 200  # what GET returned goes back
    r = client.patch("/api/v1/settings/", {"pm_window_other": "due_month", "pm_window_other_days": 30}, content_type="application/json")
    assert r.status_code == 400 and "pm_window_other_days" in r.json()


def test_a_window_change_is_audited(ctx, make_user):
    kim = make_user("director")
    fac.update_settings(by=kim, pm_window_other=K.DAYS_AFTER, pm_window_other_days=10)
    latest = fac.get_settings().history.first()
    assert (latest.pm_window_other, latest.pm_window_other_days, latest.history_user) == (K.DAYS_AFTER, 10, kim)


def test_the_database_refuses_days_without_their_kind(ctx):
    from django.db import IntegrityError, transaction

    s = fac.update_settings(portal_hotline="ext. 1")
    with pytest.raises(IntegrityError), transaction.atomic():
        type(s).objects.filter(pk=s.pk).update(pm_window_high_days=5)  # due_date with days: the check constraint


def test_today_window_and_a_waiting_device(ctx, vent_model, dept):
    """A device waiting for its incoming inspection has no next PM, so neither reader sees it (slice 26)."""
    a = Asset.objects.create(tag="W-1", device_model=vent_model, department=dept, status=AssetStatus.OUT_OF_SERVICE, next_pm_on=None,
                             awaiting_inspection=True)
    assert a not in assets_past_window(date(2030, 1, 1)) and a not in overdue_assets(date(2030, 1, 1))
