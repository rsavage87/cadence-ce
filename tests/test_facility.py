"""Facility settings (slice 8): defaults without a row, validation of every field, the policy reset, audit history,
the target and portal helpers the other screens read, the integration list, risk bands, tenant isolation, and the API."""
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from apps.accounts.models import Level, Role, create_default_roles
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel, RiskClass
from apps.facility import services as fs
from apps.facility.models import POLICY, POLICY_DEFAULTS, FacilitySettings
from apps.recalls.models import Alert
from apps.tenants.context import tenant_context


def test_defaults_come_from_the_model_without_writing_a_row(ctx):
    s = fs.get_settings()
    assert s._state.adding and FacilitySettings.objects.count() == 0
    assert s.portal_require_callback is True and s.portal_hotline == "" and s.repair_budget_monthly is None
    assert fs.kpi_targets() == {"pm_on_time": 95.0, "pm_on_time_life_support": 100.0, "uptime_pct": 99.5, "mttr_days": 3.0, "repair_budget_monthly": None}
    assert [i["text"] for i in fs.policy_items()] == [d for _f, _l, d in POLICY] and all(i["is_default"] for i in fs.policy_items())
    assert FacilitySettings.objects.count() == 0


def test_update_saves_one_row_per_tenant_and_records_who(ctx, make_user):
    kim = make_user("director")
    fs.update_settings(by=kim, portal_hotline="  ext.   4400 ", target_pm_pct="97.5")
    fs.update_settings(by=kim, policy_assignment="Credentialed technicians only")
    assert FacilitySettings.objects.count() == 1
    s = fs.get_settings()
    assert s.portal_hotline == "ext. 4400" and s.target_pm_pct == Decimal("97.5") and s.policy_assignment == "Credentialed technicians only"
    assert [h.history_user for h in s.history.all()] == [kim, kim]


@pytest.mark.parametrize("fields, field", [
    ({"target_pm_pct": "49.9"}, "target_pm_pct"), ({"target_pm_pct": "100.1"}, "target_pm_pct"), ({"target_pm_pct": ""}, "target_pm_pct"),
    ({"target_uptime_pct": "89"}, "target_uptime_pct"), ({"target_mttr_days": "0.4"}, "target_mttr_days"), ({"target_mttr_days": "31"}, "target_mttr_days"),
    ({"target_mttr_days": "abc"}, "target_mttr_days"), ({"target_pm_pct": "NaN"}, "target_pm_pct"),
    ({"repair_budget_monthly": "-1"}, "repair_budget_monthly"), ({"repair_budget_monthly": "1e12"}, "repair_budget_monthly"),
    ({"portal_hotline": "x" * 41}, "portal_hotline"), ({"portal_require_callback": "yes"}, "portal_require_callback"),
    ({"policy_portal": "   "}, "policy_portal"), ({"policy_aem": "x" * 301}, "policy_aem"),
])
def test_invalid_values_are_rejected_whole(ctx, fields, field):
    valid = {"portal_hotline": "ext. 1"} if field != "portal_hotline" else {"target_pm_pct": "97"}
    with pytest.raises(ValidationError) as e:
        fs.update_settings(**valid, **fields)
    assert field in e.value.message_dict
    assert FacilitySettings.objects.count() == 0  # nothing saved, not even the valid field alongside it


def test_unknown_fields_are_refused(ctx):
    with pytest.raises(ValidationError, match="Unknown settings: tenant"):
        fs.update_settings(tenant=None)


def test_blank_budget_clears_it_and_bounds_are_inclusive(ctx):
    fs.update_settings(repair_budget_monthly="52000", target_pm_pct="100", target_uptime_pct="90", target_mttr_days="0.5")
    assert fs.kpi_targets()["repair_budget_monthly"] == 52000.0
    fs.update_settings(repair_budget_monthly="")
    assert fs.get_settings().repair_budget_monthly is None


def test_reset_policy_restores_every_default_and_keeps_the_rest(ctx):
    fs.update_settings(policy_life_support="Stricter", policy_portal="Triage within 15 minutes", portal_hotline="ext. 4400", target_pm_pct="98")
    fs.reset_policy()
    s = fs.get_settings()
    assert {f: getattr(s, f) for f in fs.POLICY_FIELDS} == POLICY_DEFAULTS
    assert s.portal_hotline == "ext. 4400" and s.target_pm_pct == Decimal("98")


def test_compliance_targets_hold_life_support_and_high_risk_at_100(ctx):
    fs.update_settings(target_pm_pct="92")
    assert fs.compliance_targets() == {RiskClass.LIFE_SUPPORT: 100.0, RiskClass.HIGH: 100.0, RiskClass.MEDIUM: 92.0, RiskClass.LOW: 92.0}


def test_portal_url_with_and_without_a_department(ctx, settings):
    settings.PORTAL_BASE_URL = "https://cadence.example/"
    assert fs.portal_url(ctx) == "https://cadence.example/r/riverside/"
    assert fs.portal_url(ctx, "Med/Surg 3E") == "https://cadence.example/r/riverside/?dept=Med%2FSurg+3E"


def test_settings_are_per_tenant(ctx, tenant, other_tenant):
    fs.update_settings(portal_hotline="ext. 4400", target_pm_pct="97")
    with tenant_context(other_tenant):
        assert fs.get_settings()._state.adding and fs.get_settings().portal_hotline == ""
        fs.update_settings(portal_hotline="ext. 9")
    assert fs.get_settings().portal_hotline == "ext. 4400" and FacilitySettings.objects.count() == 1
    assert FacilitySettings.unscoped.count() == 2  # unscoped: checking both tenants' rows exist side by side


def test_integrations_report_real_state(ctx, pump_recall):
    rows = {i["key"]: i for i in fs.integrations()}
    assert [i["key"] for i in fs.integrations()] == ["fda", "ecri", "oem", "ehr", "rtls", "erp", "sso", "msg"]
    assert rows["fda"]["status"] == fs.CONNECTED and "1 matched to this inventory" in rows["fda"]["detail"]
    assert rows["ecri"]["status"] == fs.LICENSE and rows["sso"]["status"] == fs.NOT_CONNECTED
    assert rows["oem"]["detail"] == "0 PM procedures entered in Cadence"


def test_integrations_without_any_fda_import(ctx):
    Alert.objects.all().delete()
    fda = fs.integrations()[0]
    assert fda["status"] == fs.NOT_CONNECTED and fda["detail"] == "No FDA notices imported yet"


def test_risk_summary_counts_active_devices_by_class(ctx, dept, vent, pump, pump_model):
    Asset.objects.create(tag="CE-RET", device_model=pump_model, department=dept, status=AssetStatus.RETIRED)
    rows = fs.risk_summary()
    assert [(r["band"], r["risk"], r["devices"]) for r in rows] == [("16 and above", "life_support", 1), ("12 to 15", "high", 1),
                                                                  ("9 to 11", "medium", 0), ("8 and below", "low", 0)]


def test_risk_summary_ignores_other_tenants(ctx, vent, other_tenant):
    with tenant_context(other_tenant):
        dm = DeviceModel.objects.create(manufacturer="X", model="Y", description="Z", category="C", risk_class=RiskClass.LOW)
        Asset.objects.create(tag="THEIRS", device_model=dm, department=Department.objects.create(name="ICU"))
    assert [r["devices"] for r in fs.risk_summary()] == [1, 0, 0, 0]


# --- API ---------------------------------------------------------------------------------------------------

@pytest.fixture
def api_as(client, make_user):
    def _as(role):
        client.force_login(make_user(role))
        return client

    return _as


def test_api_get_returns_defaults_then_saved_values(api_as, ctx):
    client = api_as("director")
    data = client.get("/api/v1/settings/").json()
    assert data["portal_require_callback"] is True and data["target_pm_pct"] == "95.00" and data["updated_at"] is None
    r = client.patch("/api/v1/settings/", {"portal_hotline": "ext. 4400", "target_mttr_days": "2.5"}, content_type="application/json")
    assert r.status_code == 200 and r.json()["portal_hotline"] == "ext. 4400" and r.json()["target_mttr_days"] == "2.50"
    assert r.json()["updated_at"] is not None


def test_api_rejects_bad_values_and_unknown_fields(api_as, ctx):
    client = api_as("director")
    r = client.patch("/api/v1/settings/", {"target_pm_pct": "40"}, content_type="application/json")
    assert r.status_code == 400 and "between" in r.json()["target_pm_pct"][0]
    r = client.patch("/api/v1/settings/", {"tenant": "x"}, content_type="application/json")
    assert r.status_code == 400 and "Unknown settings" in r.json()["detail"]
    assert FacilitySettings.objects.count() == 0


def test_api_reset_policy(api_as, ctx):
    client = api_as("director")
    client.patch("/api/v1/settings/", {"policy_portal": "Triage within 15 minutes"}, content_type="application/json")
    r = client.post("/api/v1/settings/reset-policy/")
    assert r.status_code == 200 and r.json()["policy_portal"] == POLICY_DEFAULTS["policy_portal"]
    assert client.get("/api/v1/settings/reset-policy/").status_code == 405


@pytest.mark.parametrize("role, get, write", [("director", 200, 200), ("manager", 200, 403), ("technician", 403, 403), ("analyst", 403, 403),
                                              ("requester", 403, 403), ("vendor", 403, 403)])
def test_api_levels(api_as, ctx, role, get, write):
    client = api_as(role)
    assert client.get("/api/v1/settings/").status_code == get
    assert client.patch("/api/v1/settings/", {"portal_hotline": "x"}, content_type="application/json").status_code == write
    assert client.post("/api/v1/settings/reset-policy/").status_code == write


def test_api_edit_level_is_enough(api_as, ctx, tenant, make_user, client):
    role = Role.objects.create(name="Settings editor", slug="settings-editor")
    role.set_levels({"settings": Level.EDIT})
    from apps.accounts.models import User

    client.force_login(User.objects.create_user(username="ed@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role))
    assert client.patch("/api/v1/settings/", {"portal_hotline": "ext. 1"}, content_type="application/json").status_code == 200


def test_api_is_per_tenant(api_as, ctx, other_tenant, make_user, client):
    fs.update_settings(portal_hotline="ext. 4400")
    create_default_roles(other_tenant)
    client.force_login(make_user("director", tenant_=other_tenant))
    assert client.get("/api/v1/settings/").json()["portal_hotline"] == ""


def test_policy_constants_match_the_mock():
    assert [label for _f, label, _d in POLICY] == ["Life support and high risk", "Medium and low risk", "AEM approval", "Missing devices",
                                                   "Incoming inspection", "Post-repair", "Assignment", "Portal requests"]


# --- review fixes: precision, bounds, control characters, the first save, the tenant guard, FDA honesty ----------------------

@pytest.mark.parametrize("fields, message", [
    ({"repair_budget_monthly": "9999999999.999"}, "Use at most 2 decimal places."),  # would round up past the column
    ({"repair_budget_monthly": "10000000000"}, "That budget is too large."),
    ({"target_pm_pct": "95.55"}, "Use at most 1 decimal place."),
    ({"target_mttr_days": "2.25"}, "Use at most 1 decimal place."),
    ({"target_uptime_pct": "99.555"}, "Use at most 2 decimal places."),
    ({"target_pm_pct": "95,5"}, "Enter a number."),
    ({"policy_portal": "Triage\x00now"}, "Remove the invisible control character from this text."),
    ({"portal_hotline": "ext.\x004400"}, "Remove the invisible control character from this text."),
])
def test_more_precision_than_stored_or_control_characters_are_refused(ctx, fields, message):
    with pytest.raises(ValidationError) as e:
        fs.update_settings(**fields)
    assert list(e.value.message_dict.values())[0] == [message] and FacilitySettings.objects.count() == 0


@pytest.mark.parametrize("field, value", [("target_pm_pct", "50"), ("target_pm_pct", "100"), ("target_uptime_pct", "90"), ("target_uptime_pct", "100"),
                                          ("target_mttr_days", "0.5"), ("target_mttr_days", "30"), ("repair_budget_monthly", "9999999999.99"),
                                          ("repair_budget_monthly", "0"), ("target_pm_pct", "95.00"), ("repair_budget_monthly", "-0")])
def test_every_bound_is_inclusive_and_what_is_confirmed_is_stored(ctx, field, value):
    saved = getattr(fs.update_settings(**{field: value}), field)
    FacilitySettings.objects.get().refresh_from_db()
    assert getattr(FacilitySettings.objects.get(), field) == saved == Decimal(value) and not str(saved).startswith("-")


@pytest.mark.parametrize("field, value", [("target_uptime_pct", "100.01"), ("target_mttr_days", "30.1"), ("target_uptime_pct", "89.99")])
def test_just_outside_each_bound_is_refused(ctx, field, value):
    with pytest.raises(ValidationError, match="between"):
        fs.update_settings(**{field: value})


def test_a_first_save_that_loses_the_race_updates_the_winners_row(ctx, monkeypatch, make_user):
    """Two first saves at once: both see no row, one inserts, the other hits the unique-per-tenant constraint and must land on that row."""
    winner = fs.update_settings(portal_hotline="ext. 1")
    monkeypatch.setattr(fs, "_locked_row", lambda: None)  # this request looked before the winner committed
    kim = make_user("director")
    loser = fs.update_settings(by=kim, target_pm_pct="97")
    assert FacilitySettings.objects.count() == 1 and loser.pk == winner.pk
    s = fs.get_settings()
    assert s.portal_hotline == "ext. 1" and s.target_pm_pct == Decimal("97") and s.history.first().history_user == kim


def test_get_settings_refuses_to_run_without_a_tenant(db):
    with pytest.raises(RuntimeError, match="tenant context"):
        fs.get_settings()


def test_tenant_context_points_row_level_security_at_the_tenant_and_back(tenant, other_tenant, monkeypatch):
    """On Postgres, RLS reads app.tenant_id: the portal and management commands enter tenants through tenant_context, so it must set it."""
    import apps.tenants.context as tc

    calls = []
    monkeypatch.setattr(tc, "set_db_tenant", lambda t: calls.append(t.slug if t else None))
    with tc.tenant_context(tenant):
        with tc.tenant_context(other_tenant):
            pass
    assert calls == ["riverside", "other", "riverside", None]


def _fda_alert(external_id, created_at=None, demo=False):
    alert = Alert.objects.create(source=Alert.Source.FDA, external_id=external_id, manufacturer="BD", product="Pump", title="T",
                                 raw={"demo": True} if demo else {})
    if created_at:
        Alert.objects.filter(pk=alert.pk).update(created_at=created_at)  # auto_now_add ignores a value passed to create()
    return alert


def test_sample_alerts_never_count_as_a_connection(ctx, pump_model):
    from apps.recalls.models import AlertMatch

    AlertMatch.objects.create(alert=_fda_alert("Z-DEMO", demo=True), device_model=pump_model)
    fda = fs.integrations()[0]
    assert fda["status"] == fs.NOT_CONNECTED and fda["detail"] == "Only sample alerts so far; no FDA notices imported yet"
    AlertMatch.objects.create(alert=_fda_alert("Z-REAL"), device_model=pump_model)
    fda = fs.integrations()[0]
    assert fda["status"] == fs.CONNECTED and "1 matched to this inventory" in fda["detail"]  # the sample match is not counted


def test_the_import_date_is_the_local_day(ctx, settings):
    from datetime import datetime
    from datetime import timezone as dt_tz

    settings.TIME_ZONE = "America/New_York"
    _fda_alert("Z-EVENING", created_at=datetime(2026, 9, 29, 2, 0, tzinfo=dt_tz.utc))  # 10 pm on Sep 28 in New York
    assert "Newest notice imported Sep 28, 2026" in fs.integrations()[0]["detail"]


def test_integration_counts_ignore_other_tenants(ctx, pump_model, pump_recall, other_tenant):
    from apps.pm.models import PmProcedure
    from apps.recalls.models import AlertMatch

    with tenant_context(other_tenant):
        dm = DeviceModel.objects.create(manufacturer="BD", model="Alaris 8015 PCU", description="Pump", category="Infusion pumps")
        AlertMatch.objects.create(alert=pump_recall.alert, device_model=dm)
        PmProcedure.objects.create(code="THEIRS", name="Theirs")
    rows = {i["key"]: i for i in fs.integrations()}
    assert "1 matched to this inventory" in rows["fda"]["detail"] and rows["oem"]["detail"] == "0 PM procedures entered in Cadence"


def test_api_needs_a_tenant(client, db, django_user_model):
    root = django_user_model.objects.create_superuser(username="root@example.com", password="Test-Pass-2026-x")
    client.force_login(root)
    assert client.get("/api/v1/settings/").status_code == 403
    assert client.patch("/api/v1/settings/", {"portal_hotline": "x"}, content_type="application/json").status_code == 403
    assert client.post("/api/v1/settings/reset-policy/").status_code == 403


def test_api_accepts_back_what_it_returned_and_refuses_extra_precision(api_as, ctx):
    client = api_as("director")
    data = client.get("/api/v1/settings/").json()
    data["portal_hotline"] = "ext. 4400"
    r = client.patch("/api/v1/settings/", data, content_type="application/json")
    assert r.status_code == 200 and r.json()["portal_hotline"] == "ext. 4400"
    r = client.patch("/api/v1/settings/", {"target_mttr_days": "2.25"}, content_type="application/json")
    assert r.status_code == 400 and r.json()["target_mttr_days"] == ["Use at most 1 decimal place."]
