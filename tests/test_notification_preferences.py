"""Slice 20, part C: choosing the emails (apps.notifications.services, the account menu's Notifications page, and
/api/v1/notification-preferences/).

Defaults until the first save (assignments on, the digest off, contract reminders on), the first save creates the row and later ones
change only what changed, each kind offered exactly to those it may be sent to (contract reminders with Contracts Edit, the work order
emails with Work orders View; a kind not offered is shown off and disabled with its reason, and turning it on refused), so someone with
Contracts Edit and no Work orders View chooses their contract reminders here too; where the emails go (or that there is no address),
the digest's caveat for someone who is not a technician, each switch saving only itself, the account menu's link, and every refusal:
scoped users (whatever their levels), a role offered no kind at all, someone not of the facility, bad values, unknown fields. The rows
stay in their facility, and on PostgreSQL the page and the API work as the runtime role under the policies.
"""
import json

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from pg_helpers import as_app_role, needs_postgres
from rest_framework.authtoken.models import Token

from apps.accounts.models import DataScope, Level, Role, User
from apps.api.tenancy import NO_TENANT
from apps.notifications import services as ns
from apps.notifications.models import NotificationPreference
from apps.tenants.context import tenant_context

URL = "/account/notifications/"
API = "/api/v1/notification-preferences/"
HX = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "ntf-form"}
DEFAULTS = {"assignments": True, "daily_digest": False, "contract_reminders": True}
PART_OF = "part of this facility"  # ModulePermission's refusal of a scoped user
DIGEST_CAVEAT = "The digest lists work assigned to you as a technician"  # the digest's row, for someone without a technician profile


def toast_of(r) -> str:
    return json.loads(r["HX-Trigger"])["toast"]["value"]


@pytest.fixture
def person(client, make_user):
    """Sign in as a default role (by slug), with an email address unless email=False."""

    def _as(slug, email=True, **fields):
        user = make_user(slug)
        if email:
            user.email = user.username
        for name, value in fields.items():
            setattr(user, name, value)
        user.save()
        client.force_login(user)
        return user

    return _as


def custom_role(slug, levels, scope=""):
    role = Role.objects.create(name=slug.replace("-", " ").title(), slug=slug, scope=scope)
    role.set_levels(levels)
    return role


def custom_user(tenant, role, email=True) -> User:
    username = f"{role.slug}@riverside.example"
    return User.objects.create_user(username=username, email=username if email else "", password="Test-Pass-2026-x", tenant=tenant, role=role)


def saved(user) -> dict | None:
    row = NotificationPreference.objects.filter(user=user).first()
    return None if row is None else {k: getattr(row, k) for k in ns.KINDS}


def switch(body: str, kind: str) -> str:
    """The <input type="checkbox"> of one switch."""
    i = body.index(f'id="ntf-{kind}" ')
    return body[body.rindex("<input", 0, i):body.index(">", i) + 1]


# --- the service ----------------------------------------------------------------------------------------------------------------

def test_defaults_until_the_first_save_then_only_what_changed(ctx, make_user):
    tech = make_user("technician")
    assert not NotificationPreference.objects.exists()
    prefs = ns.preferences_for(tech)
    assert prefs._state.adding and {k: getattr(prefs, k) for k in ns.KINDS} == DEFAULTS
    assert ns.shown(tech) == {**DEFAULTS, "contract_reminders": False}  # Contracts View only: never sent, whatever is saved
    ns.set_preferences(tech, daily_digest=True)
    assert saved(tech) == {**DEFAULTS, "daily_digest": True}
    row = NotificationPreference.objects.get()
    stamp = row.updated_at
    assert ns.set_preferences(tech, daily_digest=True).updated_at == stamp  # nothing changes: no save
    ns.set_preferences(tech, assignments=False)
    assert saved(tech) == {"assignments": False, "daily_digest": True, "contract_reminders": True} and NotificationPreference.objects.count() == 1


def test_wants_reads_the_choice_and_needs_an_active_account_with_an_address(ctx, make_user):
    tech = make_user("technician")
    assert not ns.wants(tech, "assignments")  # make_user leaves the address blank
    tech.email = tech.username
    assert ns.wants(tech, "assignments") and not ns.wants(tech, "daily_digest")
    ns.set_preferences(tech, assignments=False, daily_digest=True)
    assert not ns.wants(tech, "assignments") and ns.wants(tech, "daily_digest")
    tech.is_active = False
    assert not ns.wants(tech, "daily_digest")
    with pytest.raises(ValueError):
        ns.wants(tech, "pager")


def test_contract_reminders_need_contracts_edit(ctx, make_user):
    tech, manager = make_user("technician"), make_user("manager")  # Contracts View, Contracts Edit
    assert not ns.offered(tech, "contract_reminders") and ns.offered(manager, "contract_reminders")
    with pytest.raises(ValidationError) as e:
        ns.set_preferences(tech, contract_reminders=True)
    assert e.value.message_dict == {"contract_reminders": [ns.NOT_OFFERED["contract_reminders"]]} and saved(tech) is None
    ns.set_preferences(tech, contract_reminders=False, daily_digest=True)  # off is what they get already: accepted, nothing stored for it
    assert saved(tech) == {**DEFAULTS, "daily_digest": True}
    ns.set_preferences(manager, contract_reminders=False)
    assert saved(manager)["contract_reminders"] is False and ns.shown(manager)["contract_reminders"] is False
    ns.set_preferences(manager, contract_reminders=True)
    assert ns.shown(manager)["contract_reminders"] is True


@pytest.mark.parametrize("choices, message", [
    ({"pager": True}, "Unknown notification: pager."),
    ({"assignments": "yes"}, "Choose on or off."),
    ({"daily_digest": None}, "Choose on or off."),
])
def test_bad_choices_are_refused(ctx, make_user, choices, message):
    tech = make_user("technician")
    with pytest.raises(ValidationError) as e:
        ns.set_preferences(tech, **choices)
    assert message in e.value.messages and saved(tech) is None


def test_who_may_choose(ctx, tenant, other_tenant, make_user):
    assert ns.refusal(make_user("technician")) is None and ns.refusal(make_user("analyst")) is None
    for slug in ("vendor", "requester"):  # scoped by default
        assert "only part of the facility" in ns.refusal(make_user(slug))
    contracts_only = custom_user(tenant, custom_role("contracts-only", {"contracts": Level.EDIT}))  # offered contract reminders alone
    assert ns.refusal(contracts_only) is None and ns.can_choose(contracts_only)
    for slug, levels in (("contracts-view", {"contracts": Level.VIEW}), ("reports-only", {"reports": Level.VIEW})):  # offered nothing
        why = ns.refusal(custom_user(tenant, custom_role(slug, levels)))
        assert "Work orders View" in why and "Contracts Edit" in why
    root = User.objects.create_superuser(username="root", password="Test-Pass-2026-x", email="root@example.com")
    assert "Only people in this facility" in ns.refusal(root)
    with pytest.raises(ValidationError):
        ns.set_preferences(root, assignments=False)
    with pytest.raises(ValidationError):
        ns.set_preferences(make_user("vendor", username="v2@riverside.example"), assignments=False)
    assert not NotificationPreference.objects.exists()


def test_preferences_stay_in_their_facility(ctx, tenant, other_tenant, make_user):
    """Tenant isolation: a row saved in one facility is not seen from another, which has its own defaults."""
    tech = make_user("technician")
    ns.set_preferences(tech, assignments=False)
    with tenant_context(other_tenant):
        assert not NotificationPreference.objects.exists()
        assert ns.preferences_for(tech)._state.adding and ns.preferences_for(tech).assignments is True
    assert NotificationPreference.objects.get().tenant == tenant


# --- the page -------------------------------------------------------------------------------------------------------------------

def test_the_page_shows_the_switches_and_where_the_emails_go(client, ctx, person):
    user = person("technician")
    r = client.get(URL)
    body = r.content.decode()
    assert r.status_code == 200 and "<h1>Notifications</h1>" in body
    assert f"Emails go to <strong>{user.email}</strong>" in body
    assert " checked" in switch(body, "assignments") and " checked" not in switch(body, "daily_digest")
    contracts = switch(body, "contract_reminders")
    assert " disabled" in contracts and " checked" not in contracts and "hx-post" not in contracts and ns.NOT_OFFERED["contract_reminders"] in body
    assert ns.NOT_OFFERED["assignments"] not in body  # offered: no reason under them
    assert 'id="ntf-contract_reminders-off"' not in body  # a disabled switch posts nothing
    assert "not linked to an active technician profile" in body  # make_user's technician has no profile
    digest = body[body.index('id="ntf-daily_digest-help"'):body.index('id="ntf-contract_reminders-name"')]
    assert DIGEST_CAVEAT in digest  # the digest lists a technician's work: none comes to them yet
    for kind in ("assignments", "daily_digest"):
        assert f'hx-post="{URL}"' in switch(body, kind) and f'hx-include="#ntf-{kind}-off"' in switch(body, kind)


def test_contracts_edit_is_offered_contract_reminders(client, ctx, person, techs):
    user = person("manager")
    techs["dana"].user = user
    techs["dana"].save(update_fields=["user", "updated_at"])
    body = client.get(URL).content.decode()
    contracts = switch(body, "contract_reminders")
    assert " checked" in contracts and " disabled" not in contracts and not any(why in body for why in ns.NOT_OFFERED.values())
    assert "not linked to an active technician profile" not in body and DIGEST_CAVEAT not in body


def test_the_page_says_when_there_is_no_address(client, ctx, person):
    person("technician", email=False)
    body = client.get(URL).content.decode()
    assert "Your account has no email address, so Cadence CE cannot email you" in body and "Emails go to" not in body


def test_each_switch_saves_itself(client, ctx, person):
    user = person("technician")
    r = client.post(URL, {"assignments": "0"}, **HX)
    assert r.status_code == 200 and toast_of(r) == "Assignment emails off" and saved(user) == {**DEFAULTS, "assignments": False}
    body = r.content.decode()
    assert body.lstrip().startswith('<div class="set-rows ntf-rows" id="ntf-form">') and "<h1>" not in body  # the switches only
    assert " checked" not in switch(body, "assignments")
    r = client.post(URL, {"daily_digest": ["1", "0"]}, **HX)  # a ticked box posts its 1 with the hidden 0, in either order
    assert toast_of(r) == "Daily digest on" and saved(user) == {**DEFAULTS, "assignments": False, "daily_digest": True}
    r = client.post(URL, {"daily_digest": ["0", "1"]}, **HX)
    assert toast_of(r) == "Daily digest on"
    r = client.post(URL, {"assignments": "maybe"}, **HX)
    assert toast_of(r) == "Choose on or off." and saved(user)["assignments"] is False
    r = client.post(URL, {"contract_reminders": "1"}, **HX)
    assert toast_of(r) == ns.NOT_OFFERED["contract_reminders"] and saved(user)["contract_reminders"] is True  # stored default, still not offered
    assert toast_of(client.post(URL, {}, **HX)) == "Nothing to save"


def test_a_post_without_htmx_saves_and_comes_back(client, ctx, person):
    user = person("manager")
    r = client.post(URL, {"contract_reminders": "0", "daily_digest": "1"})
    assert r.status_code == 302 and r["Location"] == URL
    assert saved(user) == {"assignments": True, "daily_digest": True, "contract_reminders": False}


def test_the_account_menu_offers_it_to_those_it_is_for(client, ctx, tenant, person):
    person("technician")
    menu = client.get("/").content.decode().split('class="user-menu"', 1)[1].split("</details>", 1)[0]
    assert f'href="{reverse("web:notifications")}">Notifications</a>' in menu
    assert menu.index("Change password") < menu.index(">Notifications<")
    person("vendor", company="Hamilton Medical")
    menu = client.get("/work-orders/").content.decode().split('class="user-menu"', 1)[1].split("</details>", 1)[0]
    assert "Notifications" not in menu and "Change password" in menu


@pytest.mark.parametrize("slug, fields", [("vendor", {"company": "Hamilton Medical"}), ("requester", {"department": "ICU"})])
def test_scoped_users_are_refused_the_page(client, ctx, person, slug, fields):
    user = person(slug, **fields)
    assert client.get(URL).status_code == 403
    assert client.post(URL, {"assignments": "0"}, **HX).status_code == 403
    assert saved(user) is None


def test_a_scoped_role_with_full_levels_and_a_role_offered_nothing_are_refused(client, ctx, tenant):
    full = custom_role("scoped-full", {m: Level.FULL for m in ("equipment", "workorders", "contracts", "users", "settings")}, DataScope.COMPANY)
    nothing = (custom_role("reports-only", {"reports": Level.VIEW}), custom_role("contracts-view", {"contracts": Level.VIEW, "reports": Level.FULL}))
    for role in (full, *nothing):
        client.force_login(custom_user(tenant, role))
        assert client.get(URL).status_code == 403, role.slug
        assert client.post(URL, {"assignments": "0"}, **HX).status_code == 403 and client.post(URL, {"contract_reminders": "0"}, **HX).status_code == 403
        if role in nothing:  # the account menu does not offer it either
            menu = client.get("/reports/")
            assert menu.status_code == 200 and ">Notifications</a>" not in menu.content.decode()
    assert not NotificationPreference.objects.exists()


def test_contracts_edit_without_work_orders_view_chooses_contract_reminders(client, ctx, tenant):
    """Someone who gets contract reminders and no work order emails (Contracts Edit, no Work orders View) can turn them off: the page
    opens with the reminders offered and the work order emails disabled with their reason."""
    user = custom_user(tenant, custom_role("contracts-only", {"contracts": Level.EDIT}))
    assert [k for k in ns.KINDS if ns.offered(user, k)] == ["contract_reminders"]
    assert ns.shown(user) == {"assignments": False, "daily_digest": False, "contract_reminders": True}
    client.force_login(user)
    r = client.get(URL)
    body = r.content.decode()
    assert r.status_code == 200 and f"Emails go to <strong>{user.email}</strong>" in body
    contracts = switch(body, "contract_reminders")
    assert " checked" in contracts and " disabled" not in contracts and f'hx-post="{URL}"' in contracts
    for kind in ("assignments", "daily_digest"):
        assert " disabled" in switch(body, kind) and " checked" not in switch(body, kind) and f'id="ntf-{kind}-off"' not in body
        help_text = body[body.index(f'id="ntf-{kind}-help"'):]
        assert help_text.index(ns.NOT_OFFERED[kind]) < help_text.index("</span>")  # each disabled row says why
    assert ns.NOT_OFFERED["contract_reminders"] not in body and DIGEST_CAVEAT not in body
    assert f'href="{URL}">Notifications</a>' in client.get("/contracts/").content.decode()  # the account menu offers it
    r = client.post(URL, {"contract_reminders": "0"}, **HX)
    assert toast_of(r) == "Contract reminders off" and saved(user)["contract_reminders"] is False and ns.shown(user)["contract_reminders"] is False
    r = client.post(URL, {"assignments": "1"}, **HX)
    assert toast_of(r) == ns.NOT_OFFERED["assignments"] and saved(user)["assignments"] is True  # refused: the stored default, not offered
    r = client.post(URL, {"daily_digest": "0"}, **HX)  # off is what they get already: accepted, nothing stored for it
    assert r.status_code == 200 and saved(user)["daily_digest"] is False
    # The service says the same
    with pytest.raises(ValidationError) as e:
        ns.set_preferences(user, daily_digest=True)
    assert e.value.message_dict == {"daily_digest": [ns.NOT_OFFERED["daily_digest"]]}
    ns.set_preferences(user, contract_reminders=True, assignments=False)
    assert saved(user) == {"assignments": True, "daily_digest": False, "contract_reminders": True}


def test_someone_not_of_the_facility_is_told_so(client, ctx, tenant):
    root = User.objects.create_superuser(username="root", password="Test-Pass-2026-x", email="root@example.com")
    client.force_login(root)
    session = client.session
    session["tenant_id"] = str(tenant.id)
    session.save()
    body = client.get(URL).content.decode()
    assert "Only people in this facility choose the emails it sends them." in body and 'id="ntf-assignments"' not in body
    r = client.post(URL, {"assignments": "0"}, **HX)
    assert toast_of(r) == "Only people in this facility choose the emails it sends them." and not NotificationPreference.objects.exists()
    assert ">Notifications</a>" not in client.get("/").content.decode()
    assert client.get(API).status_code == 403


# --- the API --------------------------------------------------------------------------------------------------------------------

def put(client, body, **extra):
    return client.put(API, body, content_type="application/json", **extra)


def test_the_api_reads_and_sets_your_own(client, ctx, person):
    user = person("technician")
    shown = {"email": user.email, "assignments": True, "daily_digest": False, "contract_reminders": False, "contract_reminders_offered": False}
    assert client.get(API).json() == shown
    r = put(client, {"daily_digest": True, "assignments": "false"})
    assert r.status_code == 200 and r.json() == {**shown, "daily_digest": True, "assignments": False}
    assert saved(user) == {"assignments": False, "daily_digest": True, "contract_reminders": True}
    again = put(client, client.get(API).json())  # what GET showed, sent back as is
    assert again.status_code == 200 and again.json() == r.json() and saved(user)["contract_reminders"] is True


def test_the_api_with_contracts_edit_and_a_token(client, ctx, make_user):
    manager = make_user("manager")
    token = {"HTTP_AUTHORIZATION": f"Token {Token.objects.create(user=manager).key}"}
    assert client.get(API, **token).json() == {"email": "", **DEFAULTS, "contract_reminders_offered": True}
    r = put(client, {"contract_reminders": False}, **token)
    assert r.status_code == 200 and r.json()["contract_reminders"] is False and saved(manager)["contract_reminders"] is False


@pytest.mark.parametrize("body, errors", [
    ({"pager": True}, {"detail": "Unknown fields: pager. Set any of assignments, daily_digest, contract_reminders: true or false."}),
    ({"assignments": "maybe"}, {"assignments": ["Send true or false."]}),
    ({"assignments": None}, {"assignments": ["Send true or false."]}),
    ({"assignments": 1}, {"assignments": ["Send true or false."]}),
    ({"email": "someone@else.example", "assignments": False}, {"email": ["email is shown, not set here: send it as GET shows it, or leave it out."]}),
    ({"contract_reminders_offered": True}, {"contract_reminders_offered": [
        "contract_reminders_offered is shown, not set here: send it as GET shows it, or leave it out."]}),
    ({}, {"detail": "Send any of assignments, daily_digest, contract_reminders: true or false."}),
    ({"contract_reminders": True}, {"contract_reminders": [ns.NOT_OFFERED["contract_reminders"]]}),
])
def test_the_api_refuses_bad_bodies(client, ctx, person, body, errors):
    user = person("technician")
    r = put(client, body)
    assert r.status_code == 400 and r.json() == errors
    assert saved(user) is None


def test_the_api_refuses_a_list_and_other_methods(client, ctx, person):
    person("technician")
    assert put(client, [{"assignments": False}]).json() == {"detail": "Send a JSON object."}
    assert client.post(API, {"assignments": False}, content_type="application/json").status_code == 405
    assert client.patch(API, {"assignments": False}, content_type="application/json").status_code == 405
    assert client.delete(API).status_code in (403, 405)  # 403 before the method is looked at: deleting needs Full on Work orders


@pytest.mark.parametrize("slug, fields", [("vendor", {"company": "Hamilton Medical"}), ("requester", {"department": "ICU"})])
def test_the_api_refuses_scoped_users(client, ctx, person, slug, fields):
    user = person(slug, **fields)
    for r in (client.get(API), put(client, {"assignments": False})):
        assert r.status_code == 403 and PART_OF in r.json()["detail"]
    assert saved(user) is None


def test_the_api_for_contracts_edit_without_work_orders_view(client, ctx, tenant):
    user = custom_user(tenant, custom_role("contracts-only", {"contracts": Level.EDIT}))
    client.force_login(user)
    shown = {"email": user.email, "assignments": False, "daily_digest": False, "contract_reminders": True, "contract_reminders_offered": True}
    assert client.get(API).json() == shown
    r = put(client, {"contract_reminders": False})
    assert r.status_code == 200 and r.json() == {**shown, "contract_reminders": False} and saved(user)["contract_reminders"] is False
    r = put(client, {"assignments": True})
    assert r.status_code == 400 and r.json() == {"assignments": [ns.NOT_OFFERED["assignments"]]}
    r = put(client, client.get(API).json())  # what GET showed, sent back: the work order kinds are off, which they already are
    assert r.status_code == 200 and r.json() == {**shown, "contract_reminders": False}


def test_the_api_refuses_a_role_offered_nothing_and_a_user_with_no_facility(client, ctx, tenant):
    for role in (custom_role("reports-only", {"reports": Level.VIEW}), custom_role("contracts-view", {"contracts": Level.VIEW})):
        client.force_login(custom_user(tenant, role))
        assert client.get(API).status_code == 403 and put(client, {"assignments": False}).status_code == 403, role.slug
        assert put(client, {"contract_reminders": False}).status_code == 403
    client.logout()
    root = User.objects.create_superuser(username="root", password="Test-Pass-2026-x", email="root@example.com")
    token = {"HTTP_AUTHORIZATION": f"Token {Token.objects.create(user=root).key}"}
    r = client.get(API, **token)
    assert r.status_code == 403 and r.json() == {"detail": NO_TENANT}
    assert not NotificationPreference.objects.exists()


# --- PostgreSQL ------------------------------------------------------------------------------------------------------------------

@needs_postgres
def test_choosing_under_row_level_security(client, ctx, person, other_tenant, make_user):
    """As the runtime role: the page reads the defaults and saves a switch, the API reads and sets, and the row lands in the user's
    facility only."""
    user = person("manager")
    as_app_role()
    assert " checked" in switch(client.get(URL).content.decode(), "contract_reminders")
    assert toast_of(client.post(URL, {"assignments": "0"}, **HX)) == "Assignment emails off"
    r = put(client, {"daily_digest": True, "contract_reminders": False})
    assert r.status_code == 200 and r.json()["daily_digest"] is True
    assert client.get(API).json() == {"email": user.email, "assignments": False, "daily_digest": True, "contract_reminders": False,
                                      "contract_reminders_offered": True}
    with tenant_context(other_tenant):
        assert not NotificationPreference.unscoped.exists()  # unscoped: the policy hides the other facility's row
    assert saved(user) == {"assignments": False, "daily_digest": True, "contract_reminders": False}
