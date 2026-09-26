from datetime import date, timedelta

import pytest
from django.core.exceptions import ValidationError

from apps.contracts.models import Contract, ContractType
from apps.credentials.models import Credential, Scope
from apps.credentials.services import (
    add_credential,
    coverage_by_category,
    credential_options,
    credential_state,
    qualification,
    qualified_technicians,
    remove_credential,
    renew_credential,
    sign_off_credential,
)
from apps.pm.dates import add_months


def test_model_credential_qualifies_and_outranks_category(vent, pump, techs):
    q = qualification(techs["dana"], vent)
    assert q.ok and q.via == "device model: Hamilton-G5"
    assert qualification(techs["tom"], vent).ok is False


def test_expiring_credential_is_flagged(pump, techs):
    q = qualification(techs["tom"], pump)
    assert q.ok and q.expiring and q.expires_on == date.today() + timedelta(days=30)


def test_expired_credential_does_not_qualify(pump, techs):
    Credential.objects.filter(technician=techs["tom"]).update(expires_on=date.today() - timedelta(days=1))
    q = qualification(techs["tom"], pump)
    assert not q.ok and q.expired_only


def test_qualified_list_puts_stable_credentials_first(pump, techs):
    ranked = [t.name for t, _ in qualified_technicians(pump)]
    assert ranked == ["Dana Whitfield", "Tom Okafor"]


def test_in_training_credential_is_ignored(vent, techs):
    Credential.objects.create(technician=techs["tom"], scope=Scope.CATEGORY, value="Ventilators", status="in_training")
    assert qualification(techs["tom"], vent).ok is False


def test_coverage_report_flags_single_technician(vent, pump, techs):
    rows = {r["category"]: r for r in coverage_by_category()}
    assert rows["Ventilators"]["status"] == "none"          # Dana holds a model-level credential only
    assert rows["Infusion pumps"]["status"] == "covered"     # Dana and Tom both hold category credentials


# --- lifecycle services (slice 5) ---------------------------------------------------------------

def test_add_credential_records_the_fields(techs):
    c = add_credential(techs["tom"], scope=Scope.CATEGORY, value=" Ventilators ", source="OEM training",
                       issued_on=date(2026, 3, 1), expires_on=date(2028, 3, 1))
    assert c.value == "Ventilators" and c.status == "active" and c.technician == techs["tom"] and c.source == "OEM training"


def test_add_credential_rejects_bad_scope_value_and_status(techs):
    with pytest.raises(ValidationError):
        add_credential(techs["tom"], scope="colour", value="Blue")
    with pytest.raises(ValidationError):
        add_credential(techs["tom"], scope=Scope.CATEGORY, value="  ")
    with pytest.raises(ValidationError):
        add_credential(techs["tom"], scope=Scope.CATEGORY, value="Ventilators", status="pending")
    assert techs["tom"].credentials.count() == 1


def test_add_credential_rejects_duplicates(techs):
    with pytest.raises(ValidationError, match="already has"):
        add_credential(techs["tom"], scope=Scope.CATEGORY, value="Infusion pumps")
    assert techs["tom"].credentials.count() == 1


def test_add_credential_rejects_expiry_before_issue(techs):
    with pytest.raises(ValidationError, match="before the issue date"):
        add_credential(techs["tom"], scope=Scope.CATEGORY, value="Ventilators", issued_on=date(2026, 3, 1), expires_on=date(2026, 2, 1))


def test_renew_pushes_expiry_24_months_from_today(techs):
    c = techs["tom"].credentials.get()
    renew_credential(c)
    c.refresh_from_db()
    assert c.expires_on == add_months(date.today(), 24)


def test_renew_rejected_without_expiry_or_in_training(techs):
    no_expiry = Credential.objects.get(technician=techs["dana"], scope=Scope.CATEGORY)
    with pytest.raises(ValidationError, match="no expiry"):
        renew_credential(no_expiry)
    training = Credential.objects.create(technician=techs["tom"], scope=Scope.CATEGORY, value="Ventilators", status="in_training",
                                         expires_on=date.today() + timedelta(days=10))
    with pytest.raises(ValidationError, match="in training"):
        renew_credential(training)
    training.refresh_from_db()
    assert training.expires_on == date.today() + timedelta(days=10)


def test_sign_off_activates_with_source_and_date(techs):
    c = Credential.objects.create(technician=techs["tom"], scope=Scope.CATEGORY, value="Ventilators", status="in_training",
                                  source="In-house training in progress")
    sign_off_credential(c)
    c.refresh_from_db()
    assert c.status == "active" and c.source == "In-house sign-off" and c.issued_on == date.today()
    with pytest.raises(ValidationError, match="in training"):
        sign_off_credential(c)


def test_remove_credential_deletes_it(techs):
    remove_credential(techs["tom"].credentials.get())
    assert not techs["tom"].credentials.exists()


def test_credential_state_covers_every_branch(techs):
    today = date(2026, 9, 25)

    def cred(**kw):
        return Credential(technician=techs["tom"], scope=Scope.CATEGORY, value="X", **kw)

    assert credential_state(cred(status="in_training", expires_on=date(2020, 1, 1)), today) == {"key": "in_training", "label": "In training", "css": "info"}
    assert credential_state(cred(expires_on=date(2026, 3, 15)), today) == {"key": "expired", "label": "Expired Mar 2026", "css": "crit"}
    assert credential_state(cred(expires_on=today + timedelta(days=60)), today) == {"key": "expiring", "label": "Expires in 60 d", "css": "warn"}
    assert credential_state(cred(expires_on=today + timedelta(days=61)), today) == {"key": "ok", "label": "Through Nov 2026", "css": "ok"}
    assert credential_state(cred(expires_on=date(2028, 3, 1)), today) == {"key": "ok", "label": "Through Mar 2028", "css": "ok"}
    assert credential_state(cred(), today) == {"key": "ok", "label": "No expiry", "css": "ok"}


def test_coverage_lists_partial_coverage_and_vendor_contracts(vent, pump, techs):
    rows = {r["category"]: r for r in coverage_by_category()}
    vents = rows["Ventilators"]
    assert vents["technicians"] == [] and vents["partial"] == [(techs["dana"], ["Hamilton-G5"])] and vents["vendor_contract"] is False
    assert rows["Infusion pumps"]["partial"] == [] and rows["Infusion pumps"]["devices"] == 1
    # A manufacturer credential counts as partial coverage; an in-training one does not.
    Credential.objects.create(technician=techs["tom"], scope=Scope.MANUFACTURER, value="Hamilton Medical")
    Credential.objects.create(technician=techs["tom"], scope=Scope.CATEGORY, value="Ventilators", status="in_training")
    contract = Contract.objects.create(reference="SC-1", vendor="Hamilton Medical", type=ContractType.OEM, start_on=date.today(),
                                       end_on=date.today() + timedelta(days=365))
    contract.add_assets([vent])
    vents = {r["category"]: r for r in coverage_by_category()}["Ventilators"]
    assert [(t.name, v) for t, v in vents["partial"]] == [("Dana Whitfield", ["Hamilton-G5"]), ("Tom Okafor", ["Hamilton Medical"])]
    assert vents["vendor_contract"] is True and vents["status"] == "none"


def test_coverage_keeps_its_ordering(vent, pump, techs):
    assert [r["category"] for r in coverage_by_category()] == ["Ventilators", "Infusion pumps"]  # fewest full technicians first


def test_credential_options_group_by_scope(vent_model, pump_model):
    groups = dict(credential_options())
    assert groups["Category"] == [("category|Infusion pumps", "Infusion pumps"), ("category|Ventilators", "Ventilators")]
    assert groups["Manufacturer"] == [("manufacturer|BD", "BD"), ("manufacturer|Hamilton Medical", "Hamilton Medical")]
    assert groups["Model"] == [("model|Alaris 8015 PCU", "BD Alaris 8015 PCU"), ("model|Hamilton-G5", "Hamilton Medical Hamilton-G5")]
