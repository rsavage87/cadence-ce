from datetime import date, timedelta

from apps.credentials.models import Credential, Scope
from apps.credentials.services import coverage_by_category, qualification, qualified_technicians


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
