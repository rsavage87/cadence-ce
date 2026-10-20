"""
Slice 36 scaffold: the fiscal year (Settings' start month, apps/facility/fiscal.py), a model's manufacturer support (the DeviceModel
fields through update_device_model only, equipment.services.support_status), their history words, and the priority rule's pure parts
(apps/capital/priority.py: weights, values, the total with points unknown, the suggested fiscal year).
"""
from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from apps.api.serializers import FacilitySettingsSerializer
from apps.capital import priority as P
from apps.core.history import _kind_text, record_history
from apps.equipment import services as eq
from apps.equipment.models import DeviceModel, LifeSource, RiskClass
from apps.facility import fiscal
from apps.facility import services as fac

# --- the fiscal year ---------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("day, start, fy", [
    (date(2026, 6, 30), 7, 2026), (date(2026, 7, 1), 7, 2027), (date(2027, 6, 30), 7, 2027), (date(2026, 1, 1), 1, 2026),
    (date(2026, 12, 31), 1, 2026), (date(2026, 9, 30), 10, 2026), (date(2026, 10, 1), 10, 2027), (date(2026, 12, 31), 12, 2027),
    (date(2026, 11, 30), 12, 2026), (date(2028, 2, 29), 3, 2028), (date(2028, 3, 1), 3, 2029),
])
def test_a_fiscal_year_is_named_by_the_year_it_ends(day, start, fy):
    assert fiscal.fiscal_year_of(day, start) == fy == fiscal.current_fy(day, start)
    first, last = fiscal.fiscal_bounds(fy, start)
    assert first <= day <= last and fiscal.fiscal_year_of(first, start) == fiscal.fiscal_year_of(last, start) == fy


@pytest.mark.parametrize("start", range(1, 13))
def test_fiscal_years_tile_the_calendar(start):
    for fy in range(2025, 2030):
        first, last = fiscal.fiscal_bounds(fy, start)
        assert fiscal.fiscal_bounds(fy + 1, start)[0] == date.fromordinal(last.toordinal() + 1)
        assert first.day == 1 and first.month == start and 365 <= (last - first).days + 1 <= 366


def test_the_words():
    assert fiscal.fy_label(2027) == "FY2027"
    assert fiscal.describe(2027, 7) == "FY2027: Jul 1, 2026 to Jun 30, 2027"
    assert fiscal.describe(2026, 1) == "FY2026: Jan 1, 2026 to Dec 31, 2026"
    assert fiscal.month_name(7) == "July"


def test_settings_keep_the_start_month(ctx):
    assert fiscal.start_month() == 1  # defaults until first saved
    fac.update_settings(fiscal_year_start_month="7")
    assert fac.get_settings().fiscal_year_start_month == 7 == fiscal.start_month()
    for bad in ("0", "13", "7.5", "July", "", "٧", True, -1, "007"):
        with pytest.raises(ValidationError) as e:
            fac.update_settings(fiscal_year_start_month=bad)
        assert "fiscal_year_start_month" in e.value.message_dict
    assert fac.get_settings().fiscal_year_start_month == 7


def test_the_api_offers_what_settings_edit():
    assert "fiscal_year_start_month" in FacilitySettingsSerializer().fields
    assert "fiscal_year_start_month" in fac.EDITABLE


def test_the_history_reads_the_month():
    assert _kind_text("month", 7, None) == "July"
    assert _kind_text("month", 1, None) == "January"


# --- a model's manufacturer support --------------------------------------------------------------------------------------------------

TODAY = date(2026, 10, 19)


def model(**kw):
    defaults = {"sale_ended_on": None, "support_ends_on": None, "parts_end_on": None, "support_checked_on": None}
    return DeviceModel(manufacturer="X", model="Y", description="Z", category="C", risk_class=RiskClass.LOW, **{**defaults, **kw})


@pytest.mark.parametrize("kw, key, value, words", [
    ({}, "unknown", None, "Support not checked"),
    ({"support_checked_on": date(2024, 1, 5)}, "unknown", None, "Support last checked Jan 5, 2024"),
    ({"support_checked_on": date(2026, 3, 1)}, "supported", 0.0, "Supported, checked Mar 1, 2026"),
    ({"support_checked_on": date(2025, 10, 19)}, "supported", 0.0, "Supported, checked Oct 19, 2025"),  # 365 days: still checked
    ({"support_checked_on": date(2025, 10, 18)}, "unknown", None, "Support last checked Oct 18, 2025"),
    ({"support_ends_on": date(2028, 3, 31)}, "announced", 0.2, "Support ends Mar 31, 2028"),
    ({"sale_ended_on": date(2027, 1, 1), "parts_end_on": date(2030, 1, 1)}, "announced", 0.2, "No longer sold from Jan 1, 2027"),
    ({"sale_ended_on": date(2025, 1, 1), "support_ends_on": date(2028, 3, 31)}, "sale_ended", 0.4, "No longer sold since Jan 1, 2025"),
    ({"sale_ended_on": date(2024, 1, 1), "support_ends_on": TODAY}, "sale_ended", 0.4,  # support's last day: still supported
     "No longer sold since Jan 1, 2024"),
    ({"sale_ended_on": date(2024, 1, 1), "support_ends_on": date(2026, 10, 18)}, "support_ended", 0.8,
     "Manufacturer support ended Oct 18, 2026"),
    ({"support_ends_on": date(2025, 1, 1), "parts_end_on": date(2026, 6, 1)}, "parts_ended", 1.0,
     "Parts no longer available after Jun 1, 2026"),
    ({"parts_end_on": date(2020, 1, 1), "support_ends_on": date(2025, 1, 1)}, "parts_ended", 1.0,  # any order: the gravest passed
     "Parts no longer available after Jan 1, 2020"),
])
def test_support_status(kw, key, value, words):
    s = eq.support_status(model(**kw), TODAY)
    assert (s.key, s.value, s.words) == (key, value, words)


def test_support_is_recorded_through_update_device_model_only(ctx, vent_model, make_user):
    with pytest.raises(TypeError):
        eq.create_device_model(manufacturer="A", model="B", description="C", category="D", risk_class=RiskClass.LOW, support_ends_on=TODAY)
    eq.update_device_model(vent_model, life_source=LifeSource.MANUFACTURER, sale_ended_on="2025-01-01", support_ends_on=date(2028, 3, 31),
                           parts_end_on=None, support_notice="FSN-2025-014", support_checked_on="2026-10-01", by=make_user("director"))
    dm = DeviceModel.objects.get(pk=vent_model.pk)
    assert (dm.life_source, dm.sale_ended_on, dm.support_ends_on, dm.parts_end_on, dm.support_notice, dm.support_checked_on) == (
        "manufacturer", date(2025, 1, 1), date(2028, 3, 31), None, "FSN-2025-014", date(2026, 10, 1))
    entries, _ = record_history(dm)
    changes = {c.field: c.after for c in entries[0].changes}
    assert changes["Expected life from"] == "The manufacturer" and changes["Manufacturer's notice"] == "FSN-2025-014"
    assert "Manufacturer support ends" in changes and "Support checked" in changes and "No longer sold from" in changes


@pytest.mark.parametrize("field, bad", [
    ("life_source", "guess"), ("sale_ended_on", "31/03/2028"), ("support_ends_on", "soon"), ("parts_end_on", 5),
    ("support_notice", "two words"), ("support_notice", "x" * 41), ("support_notice", 7), ("support_checked_on", "2999-01-01"),
])
def test_support_refusals(ctx, vent_model, field, bad):
    with pytest.raises(ValidationError) as e:
        eq.update_device_model(vent_model, **{field: bad})
    assert field in e.value.message_dict


def test_support_clears(ctx, vent_model):
    eq.update_device_model(vent_model, life_source="aha", support_notice="L-1", support_ends_on="2028-01-01")
    eq.update_device_model(vent_model, life_source="", support_notice="", support_ends_on="")
    dm = DeviceModel.objects.get(pk=vent_model.pk)
    assert (dm.life_source, dm.support_notice, dm.support_ends_on) == ("", "", None)


# --- the priority rule's pure parts ----------------------------------------------------------------------------------------------------


def test_the_weights_and_version():
    assert sum(P.WEIGHTS.values()) == 100 and tuple(P.WEIGHTS) == P.COMPONENTS and P.SCORE_VERSION == 2
    assert P.version_words() == ("Score v2: age 30, support 25, failures 20, service cost 15, recall 10; times 1.0 life support, 0.95 high, "
                                 "0.9 medium, 0.85 low risk")
    assert set(P.FLAG_WORDS) == set(P.FLAGS) and set(P.GAP_WORDS) == set(P.GAPS)


def test_the_values():
    assert P.age_value(0, 10) == 0 and P.age_value(15, 10) == 1.0 and P.age_value(30, 10) == 1.0 and P.age_value(7.5, 10) == 0.5
    assert P.age_value(-2, 10) == 0 and P.age_value(1, 0) == pytest.approx(1 / 1.5)
    assert P.reliability_value(0) == 0 and P.reliability_value(1.5) == 0.5 and P.reliability_value(9) == 1.0
    assert P.cost_value(Decimal("0.05")) == 0 and P.cost_value(Decimal("0.175")) == 0.5 and P.cost_value(Decimal("0.4")) == 1.0
    assert P.multiplier_for(RiskClass.LIFE_SUPPORT) == 1.0 and P.multiplier_for(RiskClass.LOW) == 0.85


def test_unknown_points_are_never_scored_or_reweighted():
    comps = (P.Component(P.AGE, 1.0, "15.0 of 10 yr"), P.Component(P.SUPPORT, None, "not checked"), P.Component(P.RELIABILITY, 0.5, ""),
             P.Component(P.COST, None, P.TOO_LITTLE_HISTORY), P.Component(P.SAFETY, 0.0, ""))
    assert P.total(comps, 1.0) == (40, 40)
    assert P.total(comps, 0.9) == (36, 40)
    assert P.total((P.Component(P.AGE, 0.5, ""),), 0.95) == (14, 0)  # 14.25
    assert P.total((P.Component(P.RELIABILITY, 0.125, ""),), 1.0) == (3, 0)  # 2.5 rounds half up


@pytest.mark.parametrize("kw, fy", [
    ({"must_act": True, "life_end": date(2035, 1, 1)}, 2027),
    ({"life_end": date(2029, 8, 1)}, 2030),
    ({"life_end": date(2029, 8, 1), "support_ends_on": date(2028, 3, 31)}, 2028),
    ({"life_end": date(2020, 1, 1)}, 2027),  # past: the current fiscal year, never one gone by
    ({}, None),
])
def test_the_suggested_fiscal_year(kw, fy):
    args = {"must_act": False, "life_end": None, "support_ends_on": None, "parts_end_on": None, **kw}
    assert P.suggested_fy(**args, today=TODAY, start=7) == fy
