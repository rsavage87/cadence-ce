"""Settings screen (slice 8): every panel on the page, read-only for the manager, 403 for roles without Settings, the auto-saving
portal rows, the policy and targets forms with their errors, the department links modal, and tenant isolation."""
from decimal import Decimal
from html.parser import HTMLParser

import pytest

from apps.equipment.models import Department
from apps.facility import services as fs
from apps.facility.models import POLICY, POLICY_DEFAULTS
from apps.tenants.context import tenant_context

HX = {"HTTP_HX_REQUEST": "true"}
BASE = "https://cadence.example"
PORTAL = f"{BASE}/r/riverside/"
POSTS = ["/settings/portal/", "/settings/policy/", "/settings/policy/reset/", "/settings/targets/"]


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug):
        user = make_user(role_slug)
        client.force_login(user)
        return user

    return _as


@pytest.fixture(autouse=True)
def portal_base(settings):
    settings.PORTAL_BASE_URL = BASE


def _policy_post(**changes):
    return {**POLICY_DEFAULTS, **changes}


def _targets_post(**changes):
    return {"target_pm_pct": "95", "target_uptime_pct": "99.5", "target_mttr_days": "3", "repair_budget_monthly": "", **changes}


def _toast(r) -> str:
    import json

    return json.loads(r["HX-Trigger"])["toast"]["value"]


# --- the page ---------------------------------------------------------------------------------------------

def test_director_sees_every_panel_with_defaults(client, signed_in, ctx, vent, pump, pump_recall):
    signed_in("director")
    r = client.get("/settings/")
    body = r.content.decode()
    assert r.status_code == 200 and r.context["nav_active"] == "settings" and r.context["can_edit"] is True
    # page head: the mock's sub line, with the two sections linked for a user who can open them
    assert "Integrations, the service request portal, maintenance policy, and risk scoring for Riverside Regional." in body
    assert '<a class="link" href="/contracts/">Contracts</a>' in body and '<a class="link" href="/users/">Users and access</a>' in body
    # integrations: the FDA feed is connected (the pump recall), ECRI needs a license, the rest are not connected
    assert "1 of 8 connected" in body
    assert body.count('<span class="chip ok">Connected</span>') == 1 and body.count('<span class="chip warn">License needed</span>') == 1
    assert body.count('<span class="chip neutral">Not connected</span>') == 6
    assert '<a class="btn sm ghost" href="/recalls/">Open recalls</a>' in body and "1 matched to this inventory" in body
    assert "The FDA feed imports nightly. The other connections are not available yet." in body
    assert "Sync" not in body and ">Connect<" not in body  # no fake connector buttons
    # portal
    assert f'<input readonly value="{PORTAL}" aria-label="Portal link">' in body and f'data-copy="{PORTAL}"' in body
    assert f'href="{PORTAL}" target="_blank" rel="noopener">Preview</a>' in body
    assert 'hx-get="/settings/portal/links/" hx-target="#modal-card"' in body
    assert 'name="portal_require_callback" value="1" aria-label="Requester must give a callback number" checked>' in body
    assert 'name="portal_hotline" value="" maxlength="40" placeholder="e.g. ext. 4400"' in body
    for row, why in [("Page on-call for critical requests, around the clock", "Needs the notifications integration"),
                     ("Allow photo upload with a request", "Not offered: photos can capture patients"),
                     ("Confirmation to the requester", "Email and text need the notifications integration")]:
        assert f"{row}<span class=\"why\">{why}</span>" in body
    assert "Changes save as you make them." in body
    # policy defaults, in the mock's order
    positions = [body.index(f'name="{field}" value="{default}" maxlength="300"') for field, _label, default in POLICY]
    assert positions == sorted(positions)
    assert "Save policy" in body and "Reset to defaults" in body and 'hx-confirm="Restore the default text for all eight policies?"' in body
    # targets, without trailing zeros; no budget
    for field, value in [("target_pm_pct", "95"), ("target_uptime_pct", "99.5"), ("target_mttr_days", "3"), ("repair_budget_monthly", "")]:
        assert f'name="{field}" value="{value}"' in body
    assert "Medium and low risk equipment; life support and high risk are held at 100%" in body and "Save targets" in body
    # risk scoring: one life-support vent, one high-risk pump, linked to the filtered Equipment list
    assert fs.RISK_RUBRIC in body and "A device&#x27;s class comes from its model in the catalog." not in body
    assert "A device's class comes from its model in the catalog." in body
    assert '<a class="link" href="/equipment/?risk=life_support">1</a>' in body and '<a class="link" href="/equipment/?risk=high">1</a>' in body
    assert '<a class="link" href="/equipment/?risk=low">0</a>' in body


def test_seeded_values_show_plainly(client, signed_in, ctx):
    fs.update_settings(portal_hotline="ext. 4400", repair_budget_monthly="52000", target_uptime_pct="99.50", portal_require_callback=False)
    signed_in("director")
    body = client.get("/settings/").content.decode()
    assert 'name="portal_hotline" value="ext. 4400"' in body and 'name="repair_budget_monthly" value="52000"' in body
    assert 'name="target_uptime_pct" value="99.5"' in body
    assert 'aria-label="Requester must give a callback number"><span class="sw"></span>Off' in body


def test_no_fda_import_means_nothing_connected(client, signed_in, ctx):
    signed_in("director")
    body = client.get("/settings/").content.decode()
    assert "0 of 8 connected" in body and "No notices imported yet" in body and "Open recalls" not in body


def test_manager_sees_everything_read_only(client, signed_in, ctx, vent, pump_recall):
    signed_in("manager")  # settings: View
    r = client.get("/settings/")
    body = r.content.decode()
    assert r.status_code == 200 and r.context["can_edit"] is False
    assert "Save policy" not in body and "Reset to defaults" not in body and "Save targets" not in body
    assert "hx-post" not in body  # nothing on the page writes
    assert 'aria-label="Requester must give a callback number" checked disabled>' in body
    assert 'placeholder="e.g. ext. 4400" autocomplete="off" disabled>' in body
    for field, _label, default in POLICY:
        assert f'name="{field}" value="{default}" maxlength="300" autocomplete="off" disabled>' in body
    assert body.count(' disabled>') >= 1 + 1 + 8 + 4
    assert "Changing settings needs Settings Edit access." in body and "Changes save as you make them." not in body
    # the manager can view Contracts, Users, Recalls, and Equipment, so the links stay
    assert '<a class="link" href="/users/">Users and access</a>' in body and "Open recalls" in body
    assert '<a class="link" href="/equipment/?risk=life_support">1</a>' in body
    # the modal is a read: the manager can open it
    assert client.get("/settings/portal/links/", **HX).status_code == 200


def test_manager_gets_403_on_every_post(client, signed_in, ctx):
    signed_in("manager")
    for url in POSTS:
        assert client.post(url, {"portal_hotline": "x"}).status_code == 403, url
    assert fs.get_settings()._state.adding  # nothing saved


@pytest.mark.parametrize("role", ["technician", "analyst", "requester", "vendor"])
def test_roles_without_settings_get_403(client, signed_in, ctx, role):
    signed_in(role)
    assert client.get("/settings/").status_code == 403
    assert client.get("/settings/portal/links/", **HX).status_code == 403
    for url in POSTS:
        assert client.post(url).status_code == 403, url


def test_signed_out_goes_to_login(client, ctx):
    r = client.get("/settings/")
    assert r.status_code == 302 and "/login/" in r["Location"]


def test_posts_need_post(client, signed_in, ctx):
    signed_in("director")
    for url in POSTS:
        assert client.get(url).status_code == 405, url


def test_links_to_other_sections_follow_their_permissions(client, ctx, tenant, make_user):
    from apps.accounts.models import Level, Role, User

    role = Role.objects.create(name="Settings only", slug="settings-only")
    role.set_levels({"settings": Level.VIEW})
    client.force_login(User.objects.create_user(username="s@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role))
    body = client.get("/settings/").content.decode()
    assert "under Contracts; users and roles are under Users and access." in body
    assert 'href="/contracts/"' not in body.split('<main')[1] and 'href="/users/"' not in body.split('<main')[1]


def test_risk_counts_are_plain_without_equipment_view(client, ctx, tenant, vent):
    from apps.accounts.models import Level, Role, User

    role = Role.objects.create(name="Settings only", slug="settings-only")
    role.set_levels({"settings": Level.EDIT})
    client.force_login(User.objects.create_user(username="s@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role))
    body = client.get("/settings/").content.decode()
    assert "/equipment/?risk=" not in body and '<td class="num">1</td>' in body
    assert "Open recalls" not in body


# --- portal rows -------------------------------------------------------------------------------------------

def test_portal_toggle_turns_off_and_back_on(client, signed_in, ctx):
    kim = signed_in("director")
    r = client.post("/settings/portal/", {"portal_require_callback": "0", "portal_hotline": ""}, **HX)
    assert r.status_code == 200 and _toast(r) == "Portal setting saved"
    assert fs.get_settings().portal_require_callback is False
    body = r.content.decode()
    assert body.lstrip().startswith('<div class="panel mt" id="set-portal">') and "<html" not in body
    assert 'aria-label="Requester must give a callback number"><span class="sw"></span>Off' in body
    # a checked box posts the hidden 0 and then 1; the last value wins
    r = client.post("/settings/portal/", {"portal_require_callback": ["0", "1"], "portal_hotline": ""}, **HX)
    assert fs.get_settings().portal_require_callback is True and "checked><span class=\"sw\"></span>On" in r.content.decode()
    assert fs.get_settings().history.first().history_user == kim


def test_portal_hotline_saves_and_normalizes(client, signed_in, ctx):
    signed_in("director")
    r = client.post("/settings/portal/", {"portal_require_callback": ["0", "1"], "portal_hotline": "  ext.    4400 "}, **HX)
    assert _toast(r) == "Portal setting saved" and fs.get_settings().portal_hotline == "ext. 4400"
    assert 'name="portal_hotline" value="ext. 4400"' in r.content.decode()


def test_too_long_hotline_toasts_and_keeps_the_saved_value(client, signed_in, ctx):
    signed_in("director")
    fs.update_settings(portal_hotline="ext. 4400")
    r = client.post("/settings/portal/", {"portal_require_callback": "0", "portal_hotline": "x" * 41}, **HX)
    assert r.status_code == 200 and _toast(r) == "Keep the hotline to 40 characters, e.g. ext. 4400."
    s = fs.get_settings()
    assert s.portal_hotline == "ext. 4400" and s.portal_require_callback is True  # all or nothing: the toggle did not change either
    assert 'name="portal_hotline" value="ext. 4400"' in r.content.decode()


def test_a_garbage_toggle_value_is_refused(client, signed_in, ctx):
    signed_in("director")
    r = client.post("/settings/portal/", {"portal_require_callback": "maybe"}, **HX)
    assert _toast(r) == "Choose on or off." and fs.get_settings()._state.adding


def test_the_autosave_form_wraps_only_the_setting_rows(client, signed_in, ctx):
    signed_in("director")
    body = client.get("/settings/").content.decode()
    form = body[body.index('<form class="set-rows"'):]
    form = form[:form.index("</form>")]
    assert 'hx-post="/settings/portal/"' in form and 'hx-disinherit="hx-swap hx-target"' in form
    assert "#modal-card" not in form and "Department links" not in form and "Portal link" not in form
    assert form.index('type="hidden" name="portal_require_callback" value="0"') < form.index('type="checkbox" name="portal_require_callback" value="1"')


# --- maintenance policy ------------------------------------------------------------------------------------

def test_policy_save(client, signed_in, ctx):
    kim = signed_in("director")
    r = client.post("/settings/policy/", _policy_post(policy_portal="  Triage within   15 minutes "), **HX)
    assert r.status_code == 200 and _toast(r) == "Maintenance policy saved"
    s = fs.get_settings()
    assert s.policy_portal == "Triage within 15 minutes" and s.history.first().history_user == kim
    body = r.content.decode()
    assert body.lstrip().startswith('<div class="panel" id="set-policy">') and 'value="Triage within 15 minutes"' in body


def test_blank_policy_is_refused_and_keeps_what_was_typed(client, signed_in, ctx):
    signed_in("director")
    r = client.post("/settings/policy/", _policy_post(policy_aem="   ", policy_missing="Escalate after 1 search"), **HX)
    assert _toast(r) == "Policy text cannot be empty. Use Reset to defaults to restore it."
    assert fs.get_settings()._state.adding  # nothing saved, not even the valid change
    body = r.content.decode()
    assert 'name="policy_missing" value="Escalate after 1 search"' in body  # the other typed value survives
    assert 'name="policy_aem" value="   " maxlength="300" autocomplete="off" aria-invalid="true" aria-describedby="policy_aem-err">' in body
    assert '<span class="hint err" id="policy_aem-err">Policy text cannot be empty.' in body
    assert body.count('aria-invalid="true"') == 1


def test_policy_reset(client, signed_in, ctx):
    signed_in("director")
    fs.update_settings(policy_life_support="Stricter", portal_hotline="ext. 4400")
    r = client.post("/settings/policy/reset/", **HX)
    assert _toast(r) == "Policy reset to defaults"
    s = fs.get_settings()
    assert s.policy_life_support == POLICY_DEFAULTS["policy_life_support"] and s.portal_hotline == "ext. 4400"
    assert f'value="{POLICY_DEFAULTS["policy_life_support"]}"' in r.content.decode()


# --- KPI targets -------------------------------------------------------------------------------------------

def test_targets_save(client, signed_in, ctx):
    signed_in("director")
    r = client.post("/settings/targets/", _targets_post(target_pm_pct="97.5", target_mttr_days="2.50", repair_budget_monthly="$52,000"), **HX)
    assert r.status_code == 200 and _toast(r) == "Targets saved"
    s = fs.get_settings()
    assert s.target_pm_pct == Decimal("97.5") and s.target_mttr_days == Decimal("2.5") and s.repair_budget_monthly == Decimal("52000")
    body = r.content.decode()
    assert body.lstrip().startswith('<div class="panel mt" id="set-targets">')
    assert 'name="target_mttr_days" value="2.5"' in body and 'name="repair_budget_monthly" value="52000"' in body
    # a blank budget clears it
    client.post("/settings/targets/", _targets_post(), **HX)
    assert fs.get_settings().repair_budget_monthly is None


@pytest.mark.parametrize("field, value, message", [
    ("target_pm_pct", "40", "PM completion target must be between 50 and 100."),
    ("target_uptime_pct", "101", "Fleet uptime target must be between 90 and 100."),
    ("target_mttr_days", "abc", "Enter a number."),
    ("target_pm_pct", "", "PM completion target is required."),
    ("repair_budget_monthly", "-5", "The budget cannot be negative."),
])
def test_target_errors_keep_the_typed_values(client, signed_in, ctx, field, value, message):
    signed_in("director")
    fs.update_settings(target_uptime_pct="99.9")
    typed = _targets_post(**{"target_uptime_pct": "98", field: value})
    r = client.post("/settings/targets/", typed, **HX)
    assert _toast(r) == message
    assert fs.get_settings().target_uptime_pct == Decimal("99.9")  # all or nothing
    body = r.content.decode()
    assert f'name="{field}" value="{value}"' in body and body.count('aria-invalid="true"') == 1
    assert f'<span class="hint err">{message}</span>' in body
    if field != "target_uptime_pct":
        assert 'name="target_uptime_pct" value="98"' in body


# --- department links modal --------------------------------------------------------------------------------

def test_department_links_modal(client, signed_in, ctx, other_tenant):
    signed_in("director")
    icu = Department.objects.create(name="ICU")
    Department.objects.create(name="Med/Surg 3E")
    with tenant_context(other_tenant):
        Department.objects.create(name="Their Oncology")
    r = client.get("/settings/portal/links/", **HX, HTTP_HX_TARGET="modal-card")
    body = r.content.decode()
    assert r.status_code == 200 and "<html" not in body and "<h2>Service request link</h2>" in body
    assert "Anyone with the link can submit a request without signing in." in body and "the requester gets a confirmation number." in body
    assert f'<input readonly value="{PORTAL}" aria-label="General link">' in body
    assert r.context["departments"] == [icu, Department.objects.get(name="Med/Surg 3E")] and "Their Oncology" not in body
    assert f'<option value="{icu.pk}" selected>ICU</option>' in body
    assert f'value="{PORTAL}?dept=ICU"' in body
    assert "Device-specific links for QR labels are on each device: Equipment, open the device, Request link." in body
    assert 'data-act="close-modal">Done</button>' in body and f'href="{PORTAL}" target="_blank" rel="noopener">Preview the portal</a>' in body
    assert 'hx-target="#share-dept-box" hx-swap="innerHTML"' in body


def test_department_switch_returns_only_the_matching_link(client, signed_in, ctx, other_tenant):
    signed_in("director")
    Department.objects.create(name="ICU")
    med = Department.objects.create(name="Med/Surg 3E")
    r = client.get(f"/settings/portal/links/?dept={med.pk}", **HX, HTTP_HX_TARGET="share-dept-box")
    body = r.content.decode()
    assert r.templates[0].name == "web/_settings_dept_link.html" and "<h2>" not in body
    assert f'value="{PORTAL}?dept=Med%2FSurg+3E"' in body and f'data-copy="{PORTAL}?dept=Med%2FSurg+3E"' in body


def test_unknown_or_foreign_department_falls_back_to_the_first(client, signed_in, ctx, other_tenant):
    signed_in("director")
    Department.objects.create(name="ICU")
    with tenant_context(other_tenant):
        theirs = Department.objects.create(name="Their Oncology")
    for dept in ["nope", "00000000-0000-0000-0000-000000000000", str(theirs.pk)]:
        r = client.get(f"/settings/portal/links/?dept={dept}", **HX, HTTP_HX_TARGET="share-dept-box")
        body = r.content.decode()
        assert r.status_code == 200 and f'value="{PORTAL}?dept=ICU"' in body and "Oncology" not in body, dept


def test_department_links_without_departments(client, signed_in, ctx):
    signed_in("director")
    body = client.get("/settings/portal/links/", **HX).content.decode()
    assert "No departments yet." in body and "<select" not in body
    assert client.get("/settings/portal/links/?dept=x", **HX, HTTP_HX_TARGET="share-dept-box").status_code == 200


def test_department_links_without_htmx_redirect_to_settings(client, signed_in, ctx):
    signed_in("director")
    r = client.get("/settings/portal/links/")
    assert r.status_code == 302 and r["Location"] == "/settings/"


# --- tenant isolation --------------------------------------------------------------------------------------

def test_other_tenants_settings_never_show(client, signed_in, ctx, other_tenant):
    with tenant_context(other_tenant):
        fs.update_settings(portal_hotline="ext. 9999", policy_portal="Their policy", target_pm_pct="88", repair_budget_monthly="1234")
    signed_in("director")
    body = client.get("/settings/").content.decode()
    assert "ext. 9999" not in body and "Their policy" not in body and 'value="88"' not in body and "1234" not in body
    assert "/r/other/" not in body and PORTAL in body
    # and a save here leaves theirs alone
    client.post("/settings/portal/", {"portal_require_callback": "1", "portal_hotline": "ext. 4400"}, **HX)
    with tenant_context(other_tenant):
        assert fs.get_settings().portal_hotline == "ext. 9999"


# --- HTMX inheritance ----------------------------------------------------------------------------------------

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class _ModalTargets(HTMLParser):
    """Records every element that loads #modal-card or #drawer together with the attributes of its open ancestors."""

    def __init__(self):
        super().__init__()
        self.stack, self.found = [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("hx-target") in ("#modal-card", "#drawer"):
            self.found.append((tag, a, [dict(s) for _t, s in self.stack]))
        if tag not in VOID:
            self.stack.append((tag, attrs))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break


def test_nothing_that_loads_the_modal_inherits_an_outerhtml_swap(client, signed_in, ctx):
    signed_in("director")
    parser = _ModalTargets()
    parser.feed(client.get("/settings/").content.decode())
    assert [a.get("hx-get") for _t, a, _anc in parser.found] == ["/settings/portal/links/"]
    for _tag, attrs, ancestors in parser.found:
        assert attrs.get("hx-swap", "innerHTML") != "outerHTML"
        for anc in ancestors:
            leaks = anc.get("hx-swap") == "outerHTML" or "hx-target" in anc
            if leaks:
                disinherit = anc.get("hx-disinherit", "")
                assert disinherit == "*" or {"hx-swap", "hx-target"} <= set(disinherit.split()), anc


# --- form helpers ------------------------------------------------------------------------------------------

def test_form_helpers():
    from apps.web.forms_settings import TARGET_FORM, plain_number, portal_fields, target_fields

    assert [f for f, _l, _h in TARGET_FORM] == list(fs.TARGET_FIELDS)
    assert [plain_number(Decimal(v)) for v in ["95.0", "99.50", "3.0", "52000.00", "100", "0.5"]] == ["95", "99.5", "3", "52000", "100", "0.5"]
    assert plain_number(None) == ""
    assert target_fields({"target_pm_pct": " 95 % ", "repair_budget_monthly": "$52,000.50"}) == {
        "target_pm_pct": "95", "target_uptime_pct": "", "target_mttr_days": "", "repair_budget_monthly": "52000.50"}
    assert portal_fields({}) == {} and portal_fields({"portal_require_callback": "x"}) == {"portal_require_callback": "x"}
