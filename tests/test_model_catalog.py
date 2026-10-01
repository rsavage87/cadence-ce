"""The device model catalog's services (slice 14, part A): risk scoring with the Settings rubric (set_risk_score, clear_risk_score,
the bands, the yearly review) and update_device_model's rule that a scored model's risk class follows its score. Every model
change takes one path (_save_model), which tells apps.pm.aem when the risk class or the OEM interval changed."""
import re
from datetime import date

import pytest
from django.core.exceptions import ValidationError

from apps.equipment import services as eq
from apps.equipment.models import DeviceModel, RiskClass
from apps.facility import services as fs
from apps.tenants.context import tenant_context

TODAY = date(2026, 10, 1)
HIGH = {"function": 7, "physical": 3, "maintenance": 3, "incidents": 1}  # 14, high
LIFE = {"function": 10, "physical": 5, "maintenance": 3, "incidents": 0}  # 18, life support
LOW = {"function": 2, "physical": 1, "maintenance": 2, "incidents": 0}  # 5, low


@pytest.fixture
def heard(monkeypatch):
    """Every call apps.pm.aem.model_changed receives, as (model, changed)."""
    calls = []
    monkeypatch.setattr("apps.pm.aem.model_changed", lambda dm, changed, by=None, previous=None: calls.append((dm.pk, sorted(changed))))
    return calls


def parts(dm) -> tuple:
    return (dm.risk_function, dm.risk_physical, dm.risk_maintenance, dm.risk_incidents)


# --- the bands ------------------------------------------------------------------------------------------------------------

def _settings_band(score: int) -> str:
    """The class a score falls in, read from the words on the Settings screen (apps.facility.services.RISK_BANDS)."""
    for words, rc in fs.RISK_BANDS:
        if m := re.fullmatch(r"(\d+) and above", words):
            ok = score >= int(m.group(1))
        elif m := re.fullmatch(r"(\d+) and below", words):
            ok = score <= int(m.group(1))
        else:
            low, high = map(int, re.fullmatch(r"(\d+) to (\d+)", words).groups())
            ok = low <= score <= high
        if ok:
            return rc.value
    raise AssertionError(f"no Settings band for {score}")


def test_the_numeric_bands_agree_with_the_settings_rubric():
    lowest = sum(low for *_rest, low, _high in eq.RISK_PARTS)
    highest = sum(high for *_rest, high in eq.RISK_PARTS)
    assert (lowest, highest) == (3, 22)
    for score in range(lowest, highest + 1):
        assert eq.risk_band(score) == _settings_band(score), score
    assert [rc for _low, rc in eq.RISK_SCORE_BANDS] == [rc for _words, rc in fs.RISK_BANDS]  # same classes, same order


@pytest.mark.parametrize("score, band", [(3, "low"), (8, "low"), (9, "medium"), (11, "medium"), (12, "high"), (15, "high"),
                                         (16, "life_support"), (22, "life_support")])
def test_band_edges(score, band):
    assert eq.risk_band(score) == band


def test_the_rubric_ranges_match_the_model_and_the_settings_text():
    assert [(key, low, high) for key, _f, _l, low, high in eq.RISK_PARTS] == [("function", 1, 10), ("physical", 1, 5), ("maintenance", 1, 5),
                                                                              ("incidents", 0, 2)]
    for _key, field, label, low, high in eq.RISK_PARTS:
        assert f"{label.lower()} ({low} to {high})" in fs.RISK_RUBRIC.lower()
        assert DeviceModel._meta.get_field(field).help_text.endswith(f"{low} to {high}")


# --- set_risk_score ---------------------------------------------------------------------------------------------------------

def test_scoring_stores_the_parts_marks_the_review_and_sets_the_class(ctx, pump_model, heard):
    eq.set_risk_score(pump_model, **LOW, today=TODAY)
    pump_model.refresh_from_db()
    assert parts(pump_model) == (2, 1, 2, 0) and pump_model.risk_score == 5
    assert pump_model.risk_reviewed_on == TODAY and pump_model.risk_class == RiskClass.LOW
    assert heard == [(pump_model.pk, ["risk_class", "risk_function", "risk_incidents", "risk_maintenance", "risk_physical", "risk_reviewed_on"])]
    assert pump_model.history.first().history_change_reason == "Risk scored 5 (Low)"
    assert pump_model.history.count() == 2  # created, then one save for the whole score


def test_a_score_in_the_same_class_does_not_tell_aem(ctx, pump_model, heard):
    eq.set_risk_score(pump_model, **HIGH, today=TODAY)  # 14: high, as it already is
    pump_model.refresh_from_db()
    assert pump_model.risk_class == RiskClass.HIGH and pump_model.risk_score == 14 and heard == []


def test_scored_into_life_support_the_model_leaves_aem_and_aem_hears_of_it(ctx, pump_model, heard):
    assert pump_model.pm_interval_months == 18  # AEM on file
    eq.set_risk_score(pump_model, **LIFE, today=TODAY)
    pump_model.refresh_from_db()
    assert pump_model.risk_class == RiskClass.LIFE_SUPPORT and pump_model.pm_interval_months == 12  # life support never runs on AEM
    assert heard and "risk_class" in heard[0][1]


def test_the_same_score_again_is_the_yearly_review(ctx, pump_model, heard):
    eq.set_risk_score(pump_model, **HIGH, today=date(2025, 9, 1))
    eq.set_risk_score(pump_model, **HIGH, today=TODAY)
    pump_model.refresh_from_db()
    assert pump_model.risk_reviewed_on == TODAY and parts(pump_model) == (7, 3, 3, 1) and pump_model.risk_class == RiskClass.HIGH
    latest, before = pump_model.history.all()[:2]
    assert latest.history_change_reason == "Risk score reviewed: 14"
    changed = {c.field for c in latest.diff_against(before).changes}
    assert changed == {"risk_reviewed_on"} and heard == []


def test_strings_from_a_form_are_whole_numbers_too(ctx, pump_model):
    eq.set_risk_score(pump_model, function="9", physical="4", maintenance="2", incidents="0", today=TODAY)
    pump_model.refresh_from_db()
    assert parts(pump_model) == (9, 4, 2, 0) and pump_model.risk_class == RiskClass.HIGH


@pytest.mark.parametrize("over, field", [({"function": 0}, "function"), ({"function": 11}, "function"), ({"physical": 0}, "physical"),
                                         ({"physical": 6}, "physical"), ({"maintenance": 6}, "maintenance"), ({"incidents": -1}, "incidents"),
                                         ({"incidents": 3}, "incidents"), ({"function": 2.5}, "function"), ({"function": "7.5"}, "function"),
                                         ({"physical": None}, "physical"), ({"maintenance": ""}, "maintenance"), ({"incidents": "one"}, "incidents"),
                                         ({"function": True}, "function")])
def test_each_part_must_be_a_whole_number_in_its_range(ctx, pump_model, heard, over, field):
    with pytest.raises(ValidationError) as e:
        eq.set_risk_score(pump_model, **{**HIGH, **over}, today=TODAY)
    assert list(e.value.message_dict) == [field]
    label, low, high = next((label, low, high) for key, _f, label, low, high in eq.RISK_PARTS if key == field)
    assert e.value.message_dict[field] == [f"{label} is a whole number from {low} to {high}."]
    pump_model.refresh_from_db()
    assert pump_model.risk_score is None and pump_model.risk_reviewed_on is None and pump_model.risk_class == RiskClass.HIGH and heard == []


def test_every_bad_part_is_reported_at_once(ctx, pump_model):
    with pytest.raises(ValidationError) as e:
        eq.set_risk_score(pump_model, function=0, physical=9, maintenance=3, incidents=5, today=TODAY)
    assert sorted(e.value.message_dict) == ["function", "incidents", "physical"]


def test_scoring_is_per_facility(ctx, pump_model, other_tenant):
    with tenant_context(other_tenant):
        theirs = DeviceModel.objects.create(manufacturer="BD", model="Alaris 8015 PCU", description="Infusion pump", category="Infusion pumps",
                                            risk_class=RiskClass.MEDIUM)
    eq.set_risk_score(pump_model, **LIFE, today=TODAY)
    assert DeviceModel.objects.filter(pk=theirs.pk).count() == 0  # not visible here
    theirs.refresh_from_db()
    assert theirs.risk_score is None and theirs.risk_class == RiskClass.MEDIUM


# --- clear_risk_score -------------------------------------------------------------------------------------------------------

def test_clearing_unscores_and_keeps_the_class(ctx, pump_model, heard):
    eq.set_risk_score(pump_model, **LIFE, today=TODAY)
    heard.clear()
    eq.clear_risk_score(pump_model)
    pump_model.refresh_from_db()
    assert parts(pump_model) == (None,) * 4 and pump_model.risk_reviewed_on is None and pump_model.risk_score is None
    assert pump_model.risk_class == RiskClass.LIFE_SUPPORT and heard == []
    assert pump_model.history.first().history_change_reason == "Risk score cleared"


def test_clearing_an_unscored_model_changes_nothing(ctx, pump_model):
    eq.clear_risk_score(pump_model)
    assert pump_model.history.count() == 1


# --- the yearly review ------------------------------------------------------------------------------------------------------

def test_review_due_never_scored_or_a_year_on(ctx, pump_model):
    assert eq.risk_review_due(pump_model, TODAY) and eq.risk_review_due_on(pump_model) is None
    pump_model.risk_reviewed_on = date(2025, 10, 2)
    assert not eq.risk_review_due(pump_model, TODAY) and eq.risk_review_due_on(pump_model) == date(2026, 10, 2)
    pump_model.risk_reviewed_on = date(2025, 10, 1)
    assert eq.risk_review_due(pump_model, TODAY)  # due on the anniversary
    pump_model.risk_reviewed_on = date(2024, 2, 29)
    assert eq.risk_review_due_on(pump_model) == date(2025, 2, 28)


# --- update_device_model: the class follows the score ------------------------------------------------------------------------

def test_a_scored_models_class_cannot_be_changed_away_from_its_score(ctx, pump_model, heard):
    eq.set_risk_score(pump_model, **HIGH, today=TODAY)
    with pytest.raises(ValidationError) as e:
        eq.update_device_model(pump_model, risk_class=RiskClass.LOW, description="Large-volume pump")
    assert e.value.message_dict == {"risk_class": ["This model's risk class follows its risk score (14, High). Change the risk score to change the class."]}
    pump_model.refresh_from_db()
    assert pump_model.risk_class == RiskClass.HIGH and pump_model.description == "Infusion pump" and heard == []


def test_a_scored_model_takes_back_its_own_class_and_other_changes(ctx, pump_model, heard):
    eq.set_risk_score(pump_model, **HIGH, today=TODAY)
    eq.update_device_model(pump_model, risk_class=RiskClass.HIGH, description="Large-volume pump", oem_pm_interval_months=24)
    pump_model.refresh_from_db()
    assert pump_model.description == "Large-volume pump" and pump_model.oem_pm_interval_months == 24
    assert heard == [(pump_model.pk, ["description", "oem_pm_interval_months"])]


def test_a_stray_class_can_move_onto_its_scores_band(ctx, pump_model):
    eq.set_risk_score(pump_model, **HIGH, today=TODAY)
    DeviceModel.objects.filter(pk=pump_model.pk).update(risk_class=RiskClass.LOW)  # as older data might hold it
    pump_model.refresh_from_db()
    eq.update_device_model(pump_model, risk_class=RiskClass.LOW, list_cost=3300)  # sending back the stored class is fine
    eq.update_device_model(pump_model, risk_class=RiskClass.HIGH)
    pump_model.refresh_from_db()
    assert pump_model.risk_class == RiskClass.HIGH and pump_model.list_cost == 3300
    with pytest.raises(ValidationError):
        eq.update_device_model(pump_model, risk_class=RiskClass.MEDIUM)


def test_an_unscored_models_class_can_change_directly(ctx, pump_model, heard):
    eq.update_device_model(pump_model, risk_class=RiskClass.LIFE_SUPPORT)
    pump_model.refresh_from_db()
    assert pump_model.risk_class == RiskClass.LIFE_SUPPORT and heard == [(pump_model.pk, ["risk_class"])]


def test_update_still_refuses_a_direct_aem_change_and_unknown_fields(ctx, pump_model):
    with pytest.raises(ValidationError) as e:
        eq.update_device_model(pump_model, aem_interval_months=24)
    assert "aem_interval_months" in e.value.message_dict
    with pytest.raises(ValidationError):
        eq.update_device_model(pump_model, risk_function=10)  # the score's parts change only through set_risk_score


# --- the Settings summary ---------------------------------------------------------------------------------------------------

def test_settings_counts_scored_models_and_reviews_due(ctx, pump_model, vent_model, other_tenant):
    assert fs.risk_scoring_summary(TODAY) == {"models": 2, "scored": 0, "reviews_due": 0}
    eq.set_risk_score(pump_model, **HIGH, today=TODAY)
    eq.set_risk_score(vent_model, **LIFE, today=date(2025, 9, 30))
    with tenant_context(other_tenant):
        DeviceModel.objects.create(manufacturer="X", model="Y", description="Z", category="C", risk_function=1, risk_physical=1,
                                   risk_maintenance=1, risk_incidents=0, risk_reviewed_on=date(2020, 1, 1))
    assert fs.risk_scoring_summary(TODAY) == {"models": 2, "scored": 2, "reviews_due": 1}
