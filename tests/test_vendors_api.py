"""
Slice 31, wave C: vendor staff over the API (apps/api/views_vendors.py) and the vendor's engineer on a work order (apps/api/views.py's
work order viewset, serializers.WorkOrderSerializer).

/api/v1/vendor-engineers/, vendor-trainings/, and vendor-attestations/ read at Contracts View and write at Contracts Edit through
apps.vendors.services, refuse every scoped user whatever their levels, filter by a vendor's key (any spelling), answer a refusal as a
400 keyed by field in the service's words, refuse fields they do not take, keep other facilities' records out (404 in the address,
400 in a body), and read a page in a fixed number of queries. A work order reads its engineer as the user may (a scoped user never
learns another company's engineer), offers engineer_choices on a single work order to whoever may name one now (names only for a
scoped user), and takes the engineer at completion (an id, null, left out: kept, or a name) and with POST {id}/vendor-engineer/
afterwards (never a scoped user's; Approve once closed); a PATCH never writes it. Under the policies as the runtime role, by token.
"""
from datetime import timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from pg_helpers import as_app_role, needs_postgres
from repair_fixtures import FINDING, POST_REPAIR_ALL_PASS
from rest_framework.authtoken.models import Token
from vendor_fixtures import attestation, engineer, training, vendor_job

from apps.accounts.models import DataScope, Level, Module, Role
from apps.api.permissions import ModulePermission
from apps.api.views import NAME_ENGINEER
from apps.api.views_vendors import EDIT_REFUSAL, VIEW_REFUSAL, VendorAttestationViewSet, VendorEngineerViewSet, VendorTrainingViewSet
from apps.equipment.models import Asset, DeviceModel, RiskClass
from apps.tenants.context import tenant_context
from apps.vendors import rules
from apps.vendors import services as vs
from apps.vendors.models import Covers, EngineerTraining, VendorAttestation, VendorEngineer
from apps.workorders.completion import REFERENCE_ONLY
from apps.workorders.models import Priority, WorkOrder, WoStatus, WoType
from apps.workorders.services import assign, change_status, create_work_order, set_vendor_engineer

API = "/api/v1/"
ENG, TRN, ATT, WOS = f"{API}vendor-engineers/", f"{API}vendor-trainings/", f"{API}vendor-attestations/", f"{API}work-orders/"
ALL_FULL = {m: Level.FULL for m in Module.values}
CLAIMS = (" trained", "qualified", "verified", "certified")  # never Cadence's own words


def results(r):
    assert r.status_code == 200, r.content
    body = r.json()
    return body["results"] if isinstance(body, dict) and "results" in body else body


def post(client, url, body=None, **headers):
    return client.post(url, body if body is not None else {}, content_type="application/json", **headers)


def patch(client, url, body, **headers):
    return client.patch(url, body, content_type="application/json", **headers)


def complete(client, wo, body, **headers):
    finding = {"repair_finding": FINDING} if wo.type == WoType.REPAIR else {}  # slice 33: what a repair found
    return post(client, f"{WOS}{wo.id}/transition/", {"status": "completed", **finding, **body}, **headers)


def name_engineer(client, wo, body, **headers):
    return post(client, f"{WOS}{wo.id}/vendor-engineer/", body, **headers)


@pytest.fixture
def today(ctx):
    return timezone.localdate()


@pytest.fixture
def world(ctx, dept, today):
    """Philips monitors (two models, one category) and a Philips CT; a monitor in the ICU; Philips' Dana Kim with OEM training on the
    MX750 and Lee Park with none; TechCare's Sam Ortiz under TechCare's attestation covering every device it services."""
    mx750 = DeviceModel.objects.create(manufacturer="Philips", model="IntelliVue MX750", description="Patient monitor", category="Patient monitors",
                                       risk_class=RiskClass.MEDIUM, oem_pm_interval_months=12)
    mx450 = DeviceModel.objects.create(manufacturer="Philips", model="IntelliVue MX450", description="Patient monitor", category="Patient monitors",
                                       risk_class=RiskClass.MEDIUM, oem_pm_interval_months=12)
    ct = DeviceModel.objects.create(manufacturer="Philips", model="Incisive CT", description="CT scanner", category="CT scanners",
                                    risk_class=RiskClass.HIGH, oem_pm_interval_months=6)
    monitor = Asset.objects.create(tag="CE-20001", device_model=mx750, department=dept, next_pm_on=today + timedelta(days=40))
    dana, lee, sam = engineer("Dana Kim"), engineer("Lee Park"), engineer("Sam Ortiz", company="TechCare")
    dana_mx750 = training(dana, mx750, issued_on=today - timedelta(days=200), expires_on=today + timedelta(days=165))
    techcare = attestation("TechCare", reference="TC-2026-01")
    return {"mx750": mx750, "mx450": mx450, "ct": ct, "monitor": monitor, "dana": dana, "lee": lee, "sam": sam, "dana_mx750": dana_mx750,
            "techcare": techcare}


@pytest.fixture
def theirs(other_tenant):
    """Another facility's engineer, training, attestation, and device model."""
    with tenant_context(other_tenant):
        model = DeviceModel.objects.create(manufacturer="Philips", model="IntelliVue MX750", description="Patient monitor",
                                           category="Patient monitors", risk_class=RiskClass.MEDIUM, oem_pm_interval_months=12)
        eng = engineer("Dana Kim")
        made = training(eng, model)
        letter = attestation("Philips", reference="THEIRS-1")
    return {"model": model, "engineer": eng, "training": made, "attestation": letter}


@pytest.fixture
def person(client, tenant, make_user):
    """Sign in as a default role (by slug) or a custom role (a Role), with the company and department given."""
    n = iter(range(1000))

    def _as(role, company="", department=""):
        if isinstance(role, str):
            user = make_user(role, username=f"{role}-{next(n)}@riverside.example")
        else:
            from apps.accounts.models import User

            user = User.objects.create_user(username=f"{role.slug}-{next(n)}@riverside.example", password="Test-Pass-2026-x", tenant=tenant,
                                            role=role)
        user.company, user.department = company, department
        user.save()
        client.force_login(user)
        return user

    return _as


def custom_role(slug, scope, levels):
    role = Role.objects.create(name=slug.replace("-", " ").title(), slug=slug, scope=scope)
    role.set_levels(levels)
    return role


def open_vendor_repair(asset, vendor="Philips", start=True):
    wo = create_work_order(asset=asset, type=WoType.REPAIR, priority=Priority.NORMAL, problem="No ECG trace")
    assign(wo, vendor_name=vendor)
    if start:
        change_status(wo, WoStatus.IN_PROGRESS)
    return wo


# --- reading -----------------------------------------------------------------------------------------------------------------------

def test_contracts_view_reads_engineers_trainings_and_attestations(client, person, world, today):
    person("technician")  # Contracts View
    engineers = results(client.get(ENG))
    assert [(e["name"], e["company"], e["is_active"]) for e in engineers] == [("Dana Kim", "Philips", True), ("Lee Park", "Philips", True),
                                                                             ("Sam Ortiz", "TechCare", True)]
    dana = engineers[0]
    expires = today + timedelta(days=165)
    assert [t["words"] for t in dana["trainings"]] == [
        f"Training on file: Philips IntelliVue MX750, Manufacturer's (OEM) factory training, through {rules.fmt(expires)}"]
    assert engineers[1]["trainings"] == []
    trainings = results(client.get(TRN))
    assert trainings == dana["trainings"]
    assert trainings[0] | {} == {
        "id": str(world["dana_mx750"].id), "engineer": str(world["dana"].id), "engineer_name": "Dana Kim", "company": "Philips",
        "covers": "models", "covers_label": "Device models", "model_ids": [str(world["mx750"].id)], "manufacturer": "", "category": "",
        "coverage": "Philips IntelliVue MX750", "source": "oem", "source_label": "Manufacturer's (OEM) factory training", "certificate": "",
        "issued_on": (today - timedelta(days=200)).isoformat(), "expires_on": expires.isoformat(), "in_force": True,
        "words": trainings[0]["words"], "entered_on": today.isoformat()}
    letters = results(client.get(ATT))
    assert letters == [{
        "id": str(world["techcare"].id), "vendor_names": ["TechCare"], "reference": "TC-2026-01",
        "received_on": (today - timedelta(days=365)).isoformat(), "valid_from": (today - timedelta(days=365)).isoformat(),
        "valid_through": (today + timedelta(days=365)).isoformat(), "covers": "all", "covers_label": "Every device this vendor services for us",
        "model_ids": [], "manufacturer": "", "category": "", "coverage": "Every device this vendor services for us", "in_force": True,
        "entered_on": today.isoformat()}]
    assert client.get(f"{ENG}{world['lee'].id}/").json()["name"] == "Lee Park"
    assert client.get(f"{TRN}{world['dana_mx750'].id}/").json()["coverage"] == "Philips IntelliVue MX750"
    assert client.get(f"{ATT}{world['techcare'].id}/").json()["reference"] == "TC-2026-01"
    text = (str(engineers) + str(trainings) + str(letters)).lower()
    assert not any(word in text for word in CLAIMS)


def test_the_browsable_api_renders_them(client, person, world):
    person("manager")
    wo = open_vendor_repair(world["monitor"])
    for url in (ENG, f"{ENG}{world['dana'].id}/", TRN, f"{TRN}{world['dana_mx750'].id}/", ATT, f"{WOS}{wo.id}/"):
        r = client.get(url, HTTP_ACCEPT="text/html")
        assert r.status_code == 200, url
    assert b"Dana Kim" in client.get(f"{WOS}{wo.id}/", HTTP_ACCEPT="text/html").content  # its engineer_choices


def test_a_training_that_lapsed_reads_as_not_in_force(client, person, world, today):
    old = training(world["lee"], covers=Covers.LINE, manufacturer="Philips", category="Patient monitors",
                   issued_on=today - timedelta(days=800), expires_on=today - timedelta(days=1), certificate="PH-77")
    person("analyst")  # Contracts View
    data = client.get(f"{TRN}{old.id}/").json()
    assert (data["in_force"], data["coverage"], data["covers_label"], data["certificate"]) == (False, "Philips Patient monitors",
                                                                                               "A manufacturer's product category", "PH-77")


def test_the_filters(client, person, world, today):
    person("manager")
    gone = engineer("Ann Lee", company="PHILIPS  field service")
    vs.update_engineer(gone, is_active=False)

    def names(url):
        return [e["name"] for e in results(client.get(url))]

    # one vendor, any spelling (by company as written, then name: the database's collation orders "PHILIPS" and "Philips")
    assert sorted(names(f"{ENG}?vendor=philips%20FIELD%20service")) == ["Ann Lee", "Dana Kim", "Lee Park"]
    assert names(f"{ENG}?vendor=Philips&active=true") == ["Dana Kim", "Lee Park"]
    assert names(f"{ENG}?active=false") == ["Ann Lee"]
    assert names(f"{ENG}?vendor=") == names(ENG) and len(names(ENG)) == 4
    assert names(f"{ENG}?search=ortiz") == ["Sam Ortiz"]
    r = client.get(f"{ENG}?active=maybe")
    assert r.status_code == 400 and r.json() == {"active": ["Send true or false."]}
    training(world["sam"], world["mx450"])
    assert [t["engineer_name"] for t in results(client.get(f"{TRN}?engineer={world['dana'].id}"))] == ["Dana Kim"]
    assert [t["engineer_name"] for t in results(client.get(f"{TRN}?vendor=techcare"))] == ["Sam Ortiz"]
    r = client.get(f"{TRN}?engineer=dana")
    assert r.status_code == 400 and r.json() == {"engineer": ["An engineer's id."]}
    attestation("Philips Healthcare", "Philips field service", reference="LTR-1")
    assert [a["reference"] for a in results(client.get(f"{ATT}?vendor=PHILIPS"))] == ["LTR-1"]  # "Philips field service" is Philips
    assert [a["reference"] for a in results(client.get(f"{ATT}?vendor=philips%20healthcare"))] == ["LTR-1"]
    assert [a["reference"] for a in results(client.get(f"{ATT}?vendor=TechCare%20Field%20Service"))] == ["TC-2026-01"]
    assert results(client.get(f"{ATT}?vendor=GE%20Healthcare")) == []
    assert sorted(a["reference"] for a in results(client.get(ATT))) == ["LTR-1", "TC-2026-01"]


# --- who may --------------------------------------------------------------------------------------------------------------------

def writes(world):
    """One write per endpoint, each valid for Contracts Edit (so a refusal is the level's, not the body's)."""
    return [
        ("post", ENG, {"name": "Ana Ruiz", "company": "GE Healthcare"}),
        ("patch", f"{ENG}{world['lee'].id}/", {"is_active": False}),
        ("post", TRN, {"engineer": str(world["lee"].id), "covers": "models", "model_ids": [str(world["mx450"].id)], "source": "vendor",
                       "issued_on": "2026-01-05"}),
        ("delete", f"{TRN}{world['dana_mx750'].id}/", None),
        ("post", ATT, {"vendor_names": ["Philips"], "covers": "all", "reference": "LTR-9", "received_on": "2026-01-05",
                       "valid_from": "2026-01-01", "valid_through": "2026-12-31"}),
        ("delete", f"{ATT}{world['techcare'].id}/", None),
    ]


def reads(world):
    return [ENG, f"{ENG}{world['dana'].id}/", TRN, f"{TRN}{world['dana_mx750'].id}/", ATT, f"{ATT}{world['techcare'].id}/"]


def call(client, method, url, body):
    if method == "delete":
        return client.delete(url)
    return getattr(client, method)(url, body, content_type="application/json")


def counts():
    return VendorEngineer.objects.count(), EngineerTraining.objects.count(), VendorAttestation.objects.count()


def test_view_reads_and_edit_writes(client, person, world):
    person("technician")  # Contracts View
    before = counts()
    for method, url, body in writes(world):
        r = call(client, method, url, body)
        assert r.status_code == 403 and r.json()["detail"] == EDIT_REFUSAL, (method, url)
    assert counts() == before and VendorEngineer.objects.get(pk=world["lee"].pk).is_active
    person(custom_role("no-contracts", DataScope.FACILITY, {**ALL_FULL, Module.CONTRACTS: Level.NONE}))
    for url in reads(world):
        r = client.get(url)
        assert r.status_code == 403 and r.json()["detail"] == VIEW_REFUSAL, url
    person("manager")  # Contracts Edit: every write, removing included
    answers = [call(client, method, url, body).status_code for method, url, body in writes(world)]
    assert answers == [201, 200, 201, 204, 201, 204]
    assert counts() == (before[0] + 1, before[1], before[2])  # one training added, one removed; one letter added, one removed


@pytest.mark.parametrize("who", ["vendor", "requester", "company-full", "unit-full"])
def test_every_vendors_endpoint_refuses_a_scoped_user(client, person, world, who):
    if who == "company-full":
        person(custom_role("company-full", DataScope.COMPANY, ALL_FULL), company="Philips")
    elif who == "unit-full":
        person(custom_role("unit-full", DataScope.DEPARTMENT, ALL_FULL), department="ICU")
    else:
        person(who, company="Philips" if who == "vendor" else "", department="ICU" if who == "requester" else "")
    before = counts()
    for url in reads(world):
        r = client.get(url)
        assert r.status_code == 403, (who, url)
    for method, url, body in writes(world):
        r = call(client, method, url, body)
        assert r.status_code == 403, (who, method, url)
        if who.endswith("full"):
            assert r.json()["detail"] == ModulePermission.SCOPED_REFUSAL
    assert counts() == before


def test_no_vendors_view_opts_in_for_scoped_users():
    assert VendorEngineerViewSet.scoped_actions == VendorTrainingViewSet.scoped_actions == VendorAttestationViewSet.scoped_actions == frozenset()


def test_another_facilitys_records_are_not_here(client, person, world, theirs):
    person("director")
    for url in (f"{ENG}{theirs['engineer'].id}/", f"{TRN}{theirs['training'].id}/", f"{ATT}{theirs['attestation'].id}/"):
        assert client.get(url).status_code == 404, url
    assert patch(client, f"{ENG}{theirs['engineer'].id}/", {"is_active": False}).status_code == 404
    assert client.delete(f"{TRN}{theirs['training'].id}/").status_code == 404
    assert client.delete(f"{ATT}{theirs['attestation'].id}/").status_code == 404
    assert "THEIRS-1" not in str(results(client.get(ATT))) and len(results(client.get(ENG))) == 3
    # in a body: as an unknown id
    r = post(client, TRN, {"engineer": str(theirs["engineer"].id), "covers": "models", "model_ids": [str(world["mx750"].id)], "source": "oem",
                           "issued_on": "2026-01-05"})
    assert r.status_code == 400 and r.json() == {"engineer": ["Choose an engineer on file here."]}
    r = post(client, TRN, {"engineer": str(world["lee"].id), "covers": "models", "model_ids": [str(theirs["model"].id)], "source": "oem",
                           "issued_on": "2026-01-05"})
    assert r.status_code == 400 and r.json() == {"model_ids": ["Choose device models from the catalog."]}
    with tenant_context(theirs["engineer"].tenant):
        assert EngineerTraining.objects.count() == 1 and VendorEngineer.objects.get().is_active


# --- engineers -----------------------------------------------------------------------------------------------------------------

def test_adding_an_engineer(client, person, world):
    person("manager")
    r = post(client, ENG, {"name": "  Mary-Jo   O'Neil ", "company": "TechCare Field Service"})
    assert r.status_code == 201, r.content
    assert r.json() | {"id": None} == {"id": None, "name": "Mary-Jo O'Neil", "company": "TechCare Field Service", "is_active": True, "trainings": []}
    made = VendorEngineer.objects.get(pk=r.json()["id"])
    assert made.company_key == "techcare" and made.created_by.role.slug == "manager"
    before = counts()
    refusals = [
        ({"name": "dana KIM", "company": "philips field service"}, {"name": ["Dana Kim is already an engineer of Philips."]}),
        ({"name": "Kim, Dana", "company": "Philips"}, {"name": ["Dana Kim is already an engineer of Philips."]}),
        ({"name": "Badge 4471", "company": "Philips"}, {"name": [rules.NAME_RULE]}),
        ({"name": "Pat Morgan"}, {"company": ["Enter the vendor as your work orders and contracts name it."]}),
        ({"name": ["Pat"], "company": "Philips"}, {"name": ["Send this as text."]}),
        ({"name": "Pat Morgan", "company": "Philips", "is_active": False}, {"is_active": ["Unknown field."]}),
    ]
    for body, answer in refusals:
        r = post(client, ENG, body)
        assert r.status_code == 400 and r.json() == answer, body
    assert counts() == before
    assert post(client, ENG, ["Pat Morgan"]).json() == {"detail": "Send the fields as a JSON object."}


def test_changing_an_engineer(client, person, world, today):
    person("manager")
    url = f"{ENG}{world['lee'].id}/"
    r = patch(client, url, {"name": "Lee Park-Sung", "is_active": False})
    assert r.status_code == 200, r.content
    assert (r.json()["name"], r.json()["is_active"]) == ("Lee Park-Sung", False)
    shown = client.get(url).json()
    assert patch(client, url, {**shown, "is_active": True}).status_code == 200  # what a GET shows may be sent back
    assert patch(client, url, {"company": "TechCare"}).json()["company"] == "TechCare"  # no work order names Lee: it may move
    for body, answer in [({"id": str(world["dana"].id)}, {"id": ["Read only."]}), ({"trainings": [{"id": "x"}]}, {"trainings": ["Read only."]}),
                         ({"is_active": "maybe"}, {"is_active": ["Send true or false."]}), ({"name": None}, {"name": ["Enter the engineer's name."]}),
                         ({"name": "Sam Ortiz"}, {"name": ["Sam Ortiz is already an engineer of TechCare."]}),
                         ({"email": "lee@example.com"}, {"email": ["Unknown field."]})]:
        r = patch(client, url, body)
        assert r.status_code == 400 and r.json() == answer, body
    vendor_job(world["monitor"], engineer=world["dana"], completed_on=today)
    r = patch(client, f"{ENG}{world['dana'].id}/", {"company": "TechCare"})
    assert r.status_code == 400 and r.json() == {"company": [vs.COMPANY_FIXED.format(name="Dana Kim")]}
    assert client.put(url, {"name": "X Y", "company": "Philips"}, content_type="application/json").status_code == 405
    assert client.delete(url).status_code == 405  # never deleted: deactivated
    assert VendorEngineer.objects.filter(pk=world["lee"].pk).exists()


# --- trainings -----------------------------------------------------------------------------------------------------------------

def training_body(world, **extra):
    return {"covers": "models", "model_ids": [str(world["mx750"].id), str(world["mx450"].id)], "source": "oem", "issued_on": "2026-03-02",
            "expires_on": "2027-03-31", **extra}


def test_adding_training_for_one_engineer_or_several(client, person, world):
    person("manager")
    r = post(client, TRN, training_body(world, engineer=str(world["lee"].id), certificate="PH-MX-0042"))
    assert r.status_code == 201, r.content
    one = r.json()
    assert (one["engineer_name"], one["certificate"], sorted(one["coverage"].split(", "))) == (
        "Lee Park", "PH-MX-0042", ["Philips IntelliVue MX450", "Philips IntelliVue MX750"])  # stored in id order
    assert one["words"] == f"Training on file: {one['coverage']}, Manufacturer's (OEM) factory training, through Mar 31, 2027"
    assert sorted(one["model_ids"]) == sorted([str(world["mx750"].id), str(world["mx450"].id)])
    sam = engineer("Sam Lee")
    r = post(client, TRN, {"engineers": [str(world["dana"].id), str(sam.id), str(world["dana"].id)], "covers": "line", "manufacturer": "Philips",
                           "category": "Patient monitors", "source": "vendor", "issued_on": "2026-02-01"})
    assert r.status_code == 201, r.content
    assert [(t["engineer_name"], t["coverage"], t["expires_on"]) for t in r.json()] == [("Dana Kim", "Philips Patient monitors", None),
                                                                                        ("Sam Lee", "Philips Patient monitors", None)]
    assert EngineerTraining.objects.filter(engineer=sam).get().created_by.role.slug == "manager"


def test_a_training_refused_says_why_and_adds_nothing(client, person, world, today, theirs):
    person("manager")
    lee, dana = str(world["lee"].id), str(world["dana"].id)
    tomorrow = (today + timedelta(days=1)).isoformat()
    before = counts()
    refusals = [
        ({"engineer": lee, "engineers": [dana]}, {"engineers": ["Send engineer (one id) or engineers (a list of ids), not both."]}),
        ({"engineer": "nobody"}, {"engineer": ["Choose an engineer on file here."]}),
        ({"engineer": 7}, {"engineer": ["Send the engineer's id as text."]}),
        ({"engineers": [dana, str(theirs["engineer"].id)]}, {"engineers": [f"Not an engineer on file here: {theirs['engineer'].id}."]}),
        ({"engineers": dana + "x"}, {"engineers": [f"Not an engineer on file here: {dana}x."]}),
        ({"engineers": [1, 2]}, {"engineers": ["Send a list of engineers' ids."]}),
        ({}, {"engineers": ["Choose the engineer."]}),
        ({"engineer": lee, "issued_on": tomorrow}, {"issued_on": ["The issue date cannot be in the future."]}),
        ({"engineer": lee, "issued_on": "03/02/2026"}, {"issued_on": ["Enter the day as YYYY-MM-DD."]}),
        ({"engineer": lee, "expires_on": "2026-02-30"}, {"expires_on": ["Enter the day as YYYY-MM-DD."]}),
        ({"engineer": lee, "expires_on": "2026-01-01"}, {"expires_on": ["The expiry date cannot be before the issue date."]}),
        ({"engineer": lee, "covers": "all"}, {"covers": ["Choose what it covers: device models, or a manufacturer's product category."]}),
        ({"engineer": lee, "covers": "line", "model_ids": []}, {"manufacturer": ["Choose the manufacturer."],
                                                               "category": ["Choose the product category."]}),
        ({"engineer": lee, "covers": "line", "manufacturer": "Philips", "category": "Ventilators", "model_ids": []},
         {"category": ["No device model in the catalog is a Philips ventilators."]}),
        ({"engineer": lee, "model_ids": "not-an-id"}, {"model_ids": ["Choose device models from the catalog."]}),
        ({"engineer": lee, "model_ids": [{"id": 1}]}, {"model_ids": ["Send a list of device model ids."]}),
        ({"engineer": lee, "source": "brochure"}, {"source": ["Choose where the training came from."]}),
        ({"engineers": [lee, dana], "certificate": "PH-1"}, {"certificate": ["A certificate number is one engineer's: add it to each "
                                                                            "engineer's own training."]}),
        ({"engineer": lee, "certificate": "PH 1"}, {"certificate": ["Enter the certificate's number as it is printed, without spaces."]}),
        ({"engineer": lee, "notes": "Good with monitors"}, {"notes": ["Unknown field."]}),
        ({"engineer": lee, "source": ["oem"]}, {"source": ["Send this as text."]}),
    ]
    for extra, answer in refusals:
        r = post(client, TRN, training_body(world, **extra))
        assert r.status_code == 400 and r.json() == answer, extra
    assert counts() == before
    post(client, TRN, training_body(world, engineer=lee))
    r = post(client, TRN, training_body(world, engineer=lee))  # the same coverage issued the same day: on file already
    assert r.status_code == 400 and r.json() == {"issued_on": ["Lee Park already has this training on file, issued Mar 2, 2026."]}


def test_removing_a_training_entered_in_error(client, person, world):
    person("manager")
    pk = world["dana_mx750"].pk
    assert client.delete(f"{TRN}{pk}/").status_code == 204
    assert not EngineerTraining.objects.filter(pk=pk).exists()
    gone = EngineerTraining.history.filter(id=pk, history_type="-").get()
    assert gone.history_change_reason == "Removed: entered in error"
    assert client.get(f"{TRN}{pk}/").status_code == 404 and client.delete(f"{TRN}{pk}/").status_code == 404
    assert patch(client, f"{TRN}{world['dana_mx750'].pk}/", {"certificate": "X"}).status_code == 405  # never edited in place


# --- attestations --------------------------------------------------------------------------------------------------------------

def attestation_body(**extra):
    return {"vendor_names": ["Philips Healthcare", "philips field service", "PHILIPS HEALTHCARE"], "covers": "all", "reference": "LTR-2026-PH",
            "received_on": "2026-02-15", "valid_from": "2026-01-01", "valid_through": "2026-12-31", **extra}


def test_adding_an_attestation(client, person, world):
    person("manager")
    r = post(client, ATT, attestation_body())
    assert r.status_code == 201, r.content
    data = r.json()
    assert (data["vendor_names"], data["coverage"], data["valid_from"], data["valid_through"]) == (
        ["Philips Healthcare", "philips field service"], "Every device this vendor services for us", "2026-01-01", "2026-12-31")
    letter = VendorAttestation.objects.get(pk=data["id"])
    assert letter.vendor_keys == ["philips healthcare", "philips"]
    r = post(client, ATT, attestation_body(covers="models", model_ids=[str(world["mx750"].id)], reference="LTR-MX", vendor_names="Philips"))
    assert r.status_code == 201 and (r.json()["vendor_names"], r.json()["coverage"]) == (["Philips"], "Philips IntelliVue MX750")


def test_an_attestation_refused_says_why_and_adds_nothing(client, person, world, today):
    person("manager")
    before = counts()
    refusals = [
        ({"vendor_names": []}, {"vendor_names": ["Enter the vendor as your work orders and contracts name it."]}),
        ({"vendor_names": [5]}, {"vendor_names": ["Send a list of the vendor's names."]}),
        ({"reference": "LTR 1"}, {"reference": ["Enter the letter's reference, or the contract and clause, without spaces (SC-2026-118/7.2)."]}),
        ({"reference": ""}, {"reference": ["Enter the letter's reference, or the contract and clause."]}),
        ({"received_on": (today + timedelta(days=1)).isoformat()}, {"received_on": ["The day it was received cannot be in the future."]}),
        ({"valid_through": None}, {"valid_through": ["Enter the last day it covers."]}),
        ({"valid_through": "2025-12-31"}, {"valid_through": ["The last day it covers cannot be before the first."]}),
        ({"valid_from": "Jan 1"}, {"valid_from": ["Enter the day as YYYY-MM-DD."]}),
        ({"covers": "manufacturer"}, {"covers": ["Choose what it covers: device models, or a manufacturer's product category, or every "
                                                 "device this vendor services for us."]}),
        ({"covers": "models"}, {"model_ids": ["Choose at least one device model."]}),
        ({"contract": "SC-1"}, {"contract": ["Unknown field."]}),
    ]
    for extra, answer in refusals:
        r = post(client, ATT, attestation_body(**extra))
        assert r.status_code == 400 and r.json() == answer, extra
    assert counts() == before


def test_removing_an_attestation_entered_in_error(client, person, world):
    person("manager")
    pk = world["techcare"].pk
    assert client.delete(f"{ATT}{pk}/").status_code == 204
    assert not VendorAttestation.objects.filter(pk=pk).exists()
    assert VendorAttestation.history.filter(id=pk, history_type="-").get().history_change_reason == "Removed: entered in error"


# --- fixed queries -------------------------------------------------------------------------------------------------------------

def test_the_lists_read_a_page_in_a_fixed_number_of_queries(client, person, world, today):
    person("manager")
    attestation("Philips", covers=Covers.MODELS, model_ids=[str(world["mx450"].id)], reference="LTR-MX450")  # a page that names models

    def queries(url) -> int:
        with CaptureQueriesContext(connection) as q:
            assert client.get(url).status_code == 200
        return len(q)

    before = {url: queries(url) for url in (ENG, TRN, ATT)}
    for n in range(6):
        e = engineer(f"Extra Person {'abcdef'[n]}", company=f"Vendor {'abcdef'[n]}")
        training(e, world["mx750"], world["mx450"])
        training(e, covers=Covers.LINE, manufacturer="Philips", category="CT scanners", issued_on=today - timedelta(days=10))
        attestation(f"Vendor {'abcdef'[n]}", covers=Covers.MODELS, model_ids=[str(world["ct"].id)], reference=f"LTR-{n}")
    assert {url: queries(url) for url in (ENG, TRN, ATT)} == before


# --- the work order's engineer ---------------------------------------------------------------------------------------------------

def test_a_work_order_reads_its_engineer_as_the_user_may(client, person, world, today):
    job = vendor_job(world["monitor"], engineer=world["dana"], completed_on=today)
    person("manager")
    data = client.get(f"{WOS}{job.id}/").json()
    assert (data["vendor_engineer"], data["vendor_engineer_name"]) == (str(world["dana"].id), "Dana Kim (Philips)")
    person("vendor", company="Philips")  # their own company's engineer: named
    data = client.get(f"{WOS}{job.id}/").json()
    assert (data["vendor_engineer"], data["vendor_engineer_name"]) == (str(world["dana"].id), "Dana Kim (Philips)")
    person("manager")
    set_vendor_engineer(job, world["sam"], by=None)  # CE names TechCare's engineer on Philips' job (a subcontractor)
    person("vendor", company="Philips")
    data = client.get(f"{WOS}{job.id}/").json()
    assert (data["vendor_engineer"], data["vendor_engineer_name"]) == (None, "Another company's engineer")
    listed = [w for w in results(client.get(WOS)) if w["id"] == str(job.id)][0]
    assert (listed["vendor_engineer"], listed["vendor_engineer_name"]) == (None, "Another company's engineer")
    assert "Sam" not in str(listed) and "TechCare" not in str(data)
    person("manager")
    assert client.get(f"{WOS}{job.id}/").json()["vendor_engineer_name"] == "Sam Ortiz (TechCare)"
    inhouse = create_work_order(asset=world["monitor"], type=WoType.REPAIR, priority=Priority.NORMAL, problem="Loose cable")
    data = client.get(f"{WOS}{inhouse.id}/").json()
    assert (data["vendor_engineer"], data["vendor_engineer_name"], data["engineer_choices"]) == (None, None, None)


def test_engineer_choices_on_a_single_work_order(client, person, world, today):
    wo = open_vendor_repair(world["monitor"])
    gone = engineer("Old Hand")
    vs.update_engineer(gone, is_active=False)
    person("manager")
    choices = client.get(f"{WOS}{wo.id}/").json()["engineer_choices"]
    expires = rules.fmt(today + timedelta(days=165))
    assert choices == {
        "vendor": [
            {"id": str(world["dana"].id), "name": "Dana Kim", "company": "Philips", "named": False,
             "on_file": f"Training on file: Philips IntelliVue MX750, Manufacturer's (OEM) factory training, through {expires}"},
            {"id": str(world["lee"].id), "name": "Lee Park", "company": "Philips", "named": False, "on_file": "No training or attestation on file"},
        ],
        "other_vendors": [
            {"id": str(world["sam"].id), "name": "Sam Ortiz", "company": "TechCare", "named": False,  # the engineer's company's letter
             "on_file": f"Attestation TC-2026-01 on file (TechCare), valid {rules.fmt(today - timedelta(days=365))} to "
                        f"{rules.fmt(today + timedelta(days=365))}"},
        ],
    }
    assert "engineer_choices" not in results(client.get(WOS))[0]  # a list's rows read none
    person("vendor", company="Philips")  # names only, their company's only
    assert client.get(f"{WOS}{wo.id}/").json()["engineer_choices"] == {
        "vendor": [{"id": str(world["dana"].id), "name": "Dana Kim", "company": "Philips", "named": False},
                   {"id": str(world["lee"].id), "name": "Lee Park", "company": "Philips", "named": False}],
        "other_vendors": []}
    person("requester", department="ICU")  # sees the work order, never names an engineer
    data = client.get(f"{WOS}{wo.id}/").json()
    assert data["engineer_choices"] is None
    person("analyst")  # Work orders View
    assert client.get(f"{WOS}{wo.id}/").json()["engineer_choices"] is None


def test_engineer_choices_after_completion_follow_who_may_change_it(client, person, world, today):
    job = vendor_job(world["monitor"], engineer=world["lee"], completed_on=today)
    vs.update_engineer(world["lee"], is_active=False)  # named already: still offered, first, as named
    person("technician")  # Edit: a completed one
    choices = client.get(f"{WOS}{job.id}/").json()["engineer_choices"]
    assert [(c["name"], c["named"]) for c in choices["vendor"]] == [("Lee Park", True), ("Dana Kim", False)]
    person("vendor", company="Philips")  # never after completion
    assert client.get(f"{WOS}{job.id}/").json()["engineer_choices"] is None
    change_status(job, WoStatus.CLOSED)
    person("technician")
    assert client.get(f"{WOS}{job.id}/").json()["engineer_choices"] is None  # closed: Approve
    person("manager")
    assert client.get(f"{WOS}{job.id}/").json()["engineer_choices"]["vendor"][0]["name"] == "Lee Park"
    cancelled = open_vendor_repair(world["monitor"], start=False)
    change_status(cancelled, WoStatus.CANCELLED)
    assert client.get(f"{WOS}{cancelled.id}/").json()["engineer_choices"] is None


@pytest.mark.parametrize("who", ["manager", "vendor"])  # the scoped reading compares companies: still from the joined row
def test_the_work_order_list_reads_no_row_for_the_engineer(client, person, world, today, who):
    person(who, company="Philips" if who == "vendor" else "")
    vendor_job(world["monitor"], engineer=world["dana"], completed_on=today)

    def queries() -> int:
        with CaptureQueriesContext(connection) as q:
            assert client.get(WOS).status_code == 200
        return len(q)

    one = queries()
    vendor_job(world["monitor"], engineer=world["lee"], completed_on=today)
    job = vendor_job(world["monitor"], engineer=world["dana"], completed_on=today)
    set_vendor_engineer(job, world["sam"])
    assert queries() == one


# --- naming the engineer at completion -------------------------------------------------------------------------------------------

def vendor_report(**extra):
    return {"resolution": "Vendor replaced the ECG board", "repair_check": "vendor_report", "checks_reference": "FSR-26-1182", **extra}


def reopen(wo):
    wo.refresh_from_db()
    change_status(wo, WoStatus.IN_PROGRESS)


def test_the_transition_names_keeps_and_clears_the_engineer(client, person, world):
    person("manager")
    wo = open_vendor_repair(world["monitor"])
    r = complete(client, wo, vendor_report(vendor_engineer=str(world["dana"].id)))
    assert r.status_code == 200, r.content
    assert (r.json()["vendor_engineer"], r.json()["vendor_engineer_name"], r.json()["checks_reference"]) == (
        str(world["dana"].id), "Dana Kim (Philips)", "FSR-26-1182")
    reopen(wo)
    r = complete(client, wo, vendor_report())  # left out: kept
    assert r.status_code == 200 and r.json()["vendor_engineer"] == str(world["dana"].id)
    reopen(wo)
    r = complete(client, wo, vendor_report(vendor_engineer=None))  # null: not named on the report
    assert r.status_code == 200 and (r.json()["vendor_engineer"], r.json()["vendor_engineer_name"]) == (None, None)
    reopen(wo)
    r = complete(client, wo, vendor_report(engineer_name="Pat  Morgan"))  # someone else: the vendor's engineer of that name
    assert r.status_code == 200, r.content
    pat = VendorEngineer.objects.get(name="Pat Morgan")
    assert (pat.company, pat.company_key, r.json()["vendor_engineer"]) == ("Philips", "philips", str(pat.id))
    assert pat.history.get().history_change_reason == f"Added at completion of {wo.number}"
    reopen(wo)
    r = complete(client, wo, vendor_report(engineer_name="pat morgan"))  # the same one, any letter case
    assert r.status_code == 200 and r.json()["vendor_engineer"] == str(pat.id) and VendorEngineer.objects.filter(name__iexact="pat morgan").count() == 1


def test_the_report_number_on_any_vendor_job(client, person, world, pump, techs):
    person("manager")
    pm = create_work_order(asset=world["monitor"], type=WoType.PM, priority=Priority.NORMAL, problem="Annual PM")
    assign(pm, vendor_name="Philips field service")
    r = complete(client, pm, {"pm_result": "pass", "checks_reference": "FSR-26-2001", "vendor_engineer": str(world["lee"].id)})
    assert r.status_code == 200, r.content
    assert (r.json()["checks_reference"], r.json()["vendor_engineer_name"]) == ("FSR-26-2001", "Lee Park (Philips)")
    ours = create_work_order(asset=pump, type=WoType.PM, priority=Priority.NORMAL, problem="Annual PM")
    assign(ours, technician=techs["tom"])
    r = complete(client, ours, {"pm_result": "pass", "checks_reference": "FSR-26-2002"})
    assert r.status_code == 400 and r.json() == {"checks_reference": [REFERENCE_ONLY]}
    r = complete(client, ours, {"pm_result": "pass", "vendor_engineer": str(world["lee"].id)})
    assert r.status_code == 400 and r.json() == {"vendor_engineer": [vs.ENGINEER_ONLY]}
    ours.refresh_from_db()
    assert ours.status == WoStatus.OPEN and ours.vendor_engineer_id is None


def test_an_engineer_refused_at_completion_saves_nothing(client, person, world, theirs):
    person("manager")
    wo = open_vendor_repair(world["monitor"])
    gone = engineer("Old Hand")
    vs.update_engineer(gone, is_active=False)
    from apps.api.views import ENGINEER_ID

    refusals = [
        ({"vendor_engineer": str(world["dana"].id), "engineer_name": "Pat Morgan"}, {"vendor_engineer": [vs.NOT_BOTH]}),
        ({"vendor_engineer": "nobody"}, {"vendor_engineer": [vs.CHOOSE_LISTED]}),
        ({"vendor_engineer": str(theirs["engineer"].id)}, {"vendor_engineer": [vs.CHOOSE_LISTED]}),  # another facility's: as unknown
        ({"vendor_engineer": str(gone.id)}, {"vendor_engineer": [vs.CHOOSE_LISTED]}),  # inactive, and not named already
        ({"vendor_engineer": 12}, {"vendor_engineer": [ENGINEER_ID]}),
        ({"vendor_engineer": [str(world["dana"].id)]}, {"vendor_engineer": [ENGINEER_ID]}),
        ({"engineer_name": ["Pat"]}, {"engineer_name": ["Send this as text."]}),
        ({"engineer_name": "Pat 4471"}, {"engineer_name": [rules.NAME_RULE]}),
        ({"engineer_name": "pat@philips.com"}, {"engineer_name": [rules.NAME_RULE]}),
        ({"checks_reference": 1182}, {"checks_reference": ["Send this as text."]}),
    ]
    before = VendorEngineer.objects.count()
    for extra, answer in refusals:
        r = complete(client, wo, vendor_report(**extra))
        assert r.status_code == 400 and r.json() == answer, extra
        wo.refresh_from_db()
        assert (wo.status, wo.vendor_engineer_id, wo.checks_reference) == (WoStatus.IN_PROGRESS, None, "")
    assert VendorEngineer.objects.count() == before


def test_a_vendor_account_names_only_its_own_companys_engineers(client, person, world, theirs):
    wo = open_vendor_repair(world["monitor"])
    person("vendor", company="Philips")
    body = {"resolution": "Replaced the ECG board", "repair_check": "passed", "results": POST_REPAIR_ALL_PASS}
    for other in (world["sam"], theirs["engineer"]):  # another company's, another facility's: both as an unknown id
        r = complete(client, wo, {**body, "vendor_engineer": str(other.id)})
        assert r.status_code == 400 and r.json() == {"vendor_engineer": [vs.CHOOSE_LISTED]}
    r = complete(client, wo, {**body, "vendor_engineer": str(world["dana"].id), "checks_reference": "FSR-26-77"})
    assert r.status_code == 200, r.content
    assert (r.json()["vendor_engineer_name"], r.json()["checks_reference"], r.json()["engineer_choices"]) == ("Dana Kim (Philips)", "FSR-26-77", None)
    second = open_vendor_repair(world["monitor"], vendor="Philips field service")
    r = complete(client, second, {**body, "engineer_name": "Robin Hale"})  # someone else: added under the work order's vendor
    assert r.status_code == 200 and VendorEngineer.objects.get(name="Robin Hale").company == "Philips field service"


def test_a_vendor_account_cannot_send_another_companys_engineer_named_by_ce(client, person, world):
    """CE names TechCare's Sam on Philips' open repair (a subcontractor); the Philips account never sees Sam (vendor_engineer null,
    engineer_choices without him), so his id is outside its choices and must answer as an unknown one does."""
    wo = open_vendor_repair(world["monitor"])
    set_vendor_engineer(wo, world["sam"])
    person("vendor", company="Philips")
    shown = client.get(f"{WOS}{wo.id}/").json()
    assert shown["vendor_engineer"] is None and str(world["sam"].id) not in str(shown["engineer_choices"])
    r = complete(client, wo, {"resolution": "Replaced the ECG board", "repair_check": "passed", "results": POST_REPAIR_ALL_PASS,
                              "vendor_engineer": str(world["sam"].id)})
    assert r.status_code == 400 and r.json() == {"vendor_engineer": [vs.CHOOSE_LISTED]}


def test_another_move_never_takes_the_engineer(client, person, world):
    person("manager")
    wo = open_vendor_repair(world["monitor"], start=False)
    for field, value in (("vendor_engineer", str(world["dana"].id)), ("engineer_name", "Pat Morgan")):
        r = post(client, f"{WOS}{wo.id}/transition/", {"status": "in_progress", field: value})
        assert r.status_code == 400 and r.json() == {field: [NAME_ENGINEER]}
    wo.refresh_from_db()
    assert wo.status == WoStatus.OPEN
    assert post(client, f"{WOS}{wo.id}/transition/", {"status": "in_progress", "vendor_engineer": None}).status_code == 200


# --- naming the engineer afterwards: POST {id}/vendor-engineer/ --------------------------------------------------------------------

def test_the_vendor_engineer_action(client, person, world, today):
    job = vendor_job(world["monitor"], completed_on=today)
    person("technician")  # Edit: a completed one
    r = name_engineer(client, job, {"vendor_engineer": str(world["dana"].id)})
    assert r.status_code == 200, r.content
    assert r.json()["vendor_engineer_name"] == "Dana Kim (Philips)"
    last = WorkOrder.history.filter(id=job.pk).order_by("-history_date").first()
    assert last.history_change_reason == "Field engineer: Dana Kim" and last.vendor_engineer_id == world["dana"].pk
    r = name_engineer(client, job, {"engineer_name": "Pat Morgan"})
    assert r.status_code == 200 and r.json()["vendor_engineer_name"] == "Pat Morgan (Philips)"
    r = name_engineer(client, job, {"vendor_engineer": None})
    assert r.status_code == 200 and r.json()["vendor_engineer"] is None
    assert WorkOrder.history.filter(id=job.pk).order_by("-history_date").first().history_change_reason == "Field engineer cleared"
    change_status(job, WoStatus.CLOSED)
    r = name_engineer(client, job, {"vendor_engineer": str(world["dana"].id)})
    assert r.status_code == 403 and r.json()["detail"] == f"{job.number} is closed: naming the vendor's engineer now needs Work orders Approve."
    person("manager")  # Approve
    assert name_engineer(client, job, {"vendor_engineer": str(world["sam"].id)}).json()["vendor_engineer_name"] == "Sam Ortiz (TechCare)"


def test_the_vendor_engineer_action_refuses(client, person, world, today, pump, techs):
    job = vendor_job(world["monitor"], completed_on=today)
    person("vendor", company="Philips")  # never a scoped user's, their own job included
    r = name_engineer(client, job, {"vendor_engineer": str(world["dana"].id)})
    assert r.status_code == 403 and r.json()["detail"] == ModulePermission.SCOPED_REFUSAL
    person("analyst")  # Work orders View
    assert name_engineer(client, job, {"vendor_engineer": str(world["dana"].id)}).status_code == 403
    person("manager")
    cancelled = open_vendor_repair(world["monitor"], start=False)
    change_status(cancelled, WoStatus.CANCELLED)
    ours = create_work_order(asset=pump, type=WoType.REPAIR, priority=Priority.NORMAL, problem="Door latch")
    assign(ours, technician=techs["tom"])
    for wo, body, answer in [
        (job, {}, {"vendor_engineer": ["Required: the engineer's id (one of the work order's engineer_choices), or null: not named on the "
                                       "vendor's report. Or send engineer_name, someone else by name."]}),
        (job, {"engineer_name": "  "}, None),
        (job, {"vendor_engineer": str(world["dana"].id), "note": "x"}, {"note": ["Unknown field."]}),
        (job, {"vendor_engineer": 5}, {"vendor_engineer": ["Send the engineer's id as text (one of the work order's engineer_choices), or null: "
                                                           "not named on the vendor's report."]}),
        (job, {"engineer_name": 5}, {"engineer_name": ["Send this as text."]}),
        (job, {"vendor_engineer": "nobody"}, {"vendor_engineer": [vs.CHOOSE_LISTED]}),
        (job, {"vendor_engineer": str(world["dana"].id), "engineer_name": "Pat Morgan"}, {"vendor_engineer": [vs.NOT_BOTH]}),
        (job, {"engineer_name": "Pat/Morgan"}, {"engineer_name": [rules.NAME_RULE]}),
        (cancelled, {"vendor_engineer": str(world["dana"].id)}, {"vendor_engineer": ["A cancelled work order names no engineer."]}),
        (ours, {"vendor_engineer": str(world["dana"].id)}, {"vendor_engineer": [vs.ENGINEER_ONLY]}),
    ]:
        r = name_engineer(client, wo, body)
        assert r.status_code == 400, (body, r.content)
        if answer is not None:
            assert r.json() == answer, body
        wo.refresh_from_db()
        assert wo.vendor_engineer_id is None
    assert name_engineer(client, job, ["x"]).json() == {"detail": "Send the fields as a JSON object."}


def test_an_edit_never_writes_the_engineer(client, person, world, today):
    person("manager")
    wo = open_vendor_repair(world["monitor"])
    for body, field in (({"vendor_engineer": str(world["dana"].id)}, "vendor_engineer"), ({"engineer_name": "Pat Morgan"}, "engineer_name")):
        r = patch(client, f"{WOS}{wo.id}/", body)
        assert r.status_code == 400 and r.json() == {field: [NAME_ENGINEER]}, body
    r = patch(client, f"{WOS}{wo.id}/", {"vendor_engineer": None, "vendor_engineer_name": None, "engineer_choices": None, "priority": "high"})
    assert r.status_code == 200, r.content  # what a GET returned may be sent back
    job = vendor_job(world["monitor"], engineer=world["dana"], completed_on=today)
    shown = client.get(f"{WOS}{job.id}/").json()
    r = patch(client, f"{WOS}{job.id}/", {"vendor_engineer": shown["vendor_engineer"]})
    assert r.status_code == 200
    r = patch(client, f"{WOS}{job.id}/", {"vendor_engineer": None})
    assert r.status_code == 400 and r.json() == {"vendor_engineer": [NAME_ENGINEER]}
    r = patch(client, f"{WOS}{job.id}/", {"checks_reference": "FSR-9"})
    assert r.status_code == 400 and "recorded when it is completed" in r.json()["checks_reference"][0]
    r = post(client, WOS, {"asset": str(world["monitor"].id), "problem": "Screen flickers", "due_on": today.isoformat(),
                           "vendor_engineer": str(world["dana"].id)})
    assert r.status_code == 400 and r.json() == {"vendor_engineer": [NAME_ENGINEER]}
    job.refresh_from_db()
    assert job.vendor_engineer_id == world["dana"].pk


# --- under the policies --------------------------------------------------------------------------------------------------------

@needs_postgres
def test_vendor_staff_and_the_engineer_under_the_policies(client, ctx, world, theirs, make_user, today):
    """As the runtime role, by token: the lists (another facility's rows hidden), adding a training, completing a vendor job naming
    an engineer, the choices, and naming one afterwards. A request leaves the facility's setting as it found none, so the test reads
    the rows back inside the facility."""
    token = Token.objects.create(user=make_user("manager"))
    auth = {"HTTP_AUTHORIZATION": f"Token {token.key}"}
    wo, other = open_vendor_repair(world["monitor"]), open_vendor_repair(world["monitor"])
    job = vendor_job(world["monitor"], completed_on=today)
    as_app_role()
    assert [e["name"] for e in results(client.get(ENG, **auth))] == ["Dana Kim", "Lee Park", "Sam Ortiz"]
    assert [a["reference"] for a in results(client.get(ATT, **auth))] == ["TC-2026-01"]
    assert client.get(f"{ENG}{theirs['engineer'].id}/", **auth).status_code == 404
    r = post(client, TRN, training_body(world, engineer=str(world["lee"].id)), **auth)
    assert r.status_code == 201, r.content
    choices = client.get(f"{WOS}{wo.id}/", **auth).json()["engineer_choices"]
    assert [c["name"] for c in choices["vendor"]] == ["Dana Kim", "Lee Park"]
    r = complete(client, wo, vendor_report(vendor_engineer=str(world["lee"].id)), **auth)
    assert r.status_code == 200 and r.json()["vendor_engineer_name"] == "Lee Park (Philips)"
    r = complete(client, other, vendor_report(vendor_engineer=str(theirs["engineer"].id)), **auth)  # another facility's: as unknown
    assert r.status_code == 400 and r.json() == {"vendor_engineer": [vs.CHOOSE_LISTED]}
    r = name_engineer(client, job, {"engineer_name": "Pat Morgan"}, **auth)
    assert r.status_code == 200 and r.json()["vendor_engineer_name"] == "Pat Morgan (Philips)"
    with tenant_context(ctx):
        assert WorkOrder.objects.get(pk=wo.pk).vendor_engineer_id == world["lee"].pk
        assert EngineerTraining.objects.filter(engineer=world["lee"]).count() == 1
        assert WorkOrder.objects.get(pk=job.pk).vendor_engineer.name == "Pat Morgan"
