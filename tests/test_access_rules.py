"""
Who may hand out access (slice 19, apps/accounts/services.py), for the Users and Roles tabs and the API alike: nobody gives a role
more access than their own (inviting, changing a user's role, copying a role, raising a module's level), a facility always keeps a
director who can sign in, deactivating an account deletes its API tokens, and an invited email fits the username it becomes.
"""
import pytest
from django.core.exceptions import ValidationError
from rest_framework.authtoken.models import Token

from apps.accounts import services
from apps.accounts.models import Level, Role, User


@pytest.fixture
def clerk(ctx, make_user):
    """A custom role with Users Full and little else: it manages accounts, but is not the Director."""
    role = Role.objects.create(name="Admin clerk", slug="admin-clerk")
    role.set_levels({"users": Level.FULL, "contracts": Level.EDIT})
    user = make_user("technician", username="clerk@riverside.example")
    user.role = role
    user.save()
    return user


def role(slug):
    return Role.objects.get(slug=slug)


def message(e) -> str:
    return " ".join(e.value.messages)


def test_nobody_gives_a_role_more_access_than_their_own(ctx, clerk, make_user):
    tom = make_user("technician")
    with pytest.raises(ValidationError) as e:
        services.set_user_role(tom, role("director"), by=clerk)
    assert message(e) == "You can give only a role whose access you have yourself: Director has more Equipment access than your role."
    with pytest.raises(ValidationError):
        services.invite_user(ctx, email="new@riverside.example", first_name="N", last_name="P", role=role("manager"), by=clerk)
    with pytest.raises(ValidationError):
        services.create_role(name="Second director", copy_from=role("director"), by=clerk)
    contracts = services.create_role(name="Contracts clerk", copy_from=Role.objects.get(slug="admin-clerk"), by=clerk)
    with pytest.raises(ValidationError) as e:
        services.set_role_level(contracts, "settings", Level.FULL, by=clerk)
    assert message(e) == "You cannot give a role more Settings access than your own role has."
    services.set_role_level(contracts, "users", Level.VIEW, by=clerk)  # lowering, or anything within their own, is fine
    services.set_role_level(contracts, "users", Level.FULL, by=clerk)
    tom.refresh_from_db()
    assert tom.role.slug == "technician" and not User.objects.filter(email="new@riverside.example").exists()
    assert not Role.objects.filter(name="Second director").exists()


def test_the_director_and_a_superuser_give_any_role(ctx, make_user):
    director, tom = make_user("director"), make_user("technician")
    services.set_user_role(tom, role("manager"), by=director)
    root = User.objects.create_superuser(username="root", password="Test-Pass-2026-x", email="root@example.com")
    services.set_user_role(tom, role("director"), by=root)
    tom.refresh_from_db()
    assert tom.role.slug == "director"


def test_a_facility_keeps_a_director_who_can_sign_in(ctx, make_user):
    kim = make_user("director")
    with pytest.raises(ValidationError) as e:
        services.deactivate_user(kim)
    assert message(e) == "Director User is this facility's only Director; give someone else that role first."
    with pytest.raises(ValidationError):
        services.set_user_role(kim, role("manager"))
    invited = services.invite_user(ctx, email="next@riverside.example", first_name="Next", last_name="Director", role=role("director"))
    with pytest.raises(ValidationError):  # an invitation not yet accepted is no director yet
        services.deactivate_user(kim)
    User.objects.filter(pk=invited.pk).update(is_invited=False)
    services.deactivate_user(kim)
    kim.refresh_from_db()
    assert not kim.is_active
    with pytest.raises(ValidationError):  # now the other one is the last
        services.set_user_role(User.objects.get(pk=invited.pk), role("manager"))


def test_deactivating_deletes_the_accounts_tokens(ctx, make_user):
    make_user("director")
    tom = make_user("technician")
    Token.objects.create(user=tom)
    services.deactivate_user(tom)
    assert not Token.objects.filter(user=tom).exists()
    services.reactivate_user(tom)
    assert not Token.objects.filter(user=tom).exists()  # a new token is made for them, never the old one back


def test_an_invited_email_fits_the_username(ctx):
    long = "a" * 140 + "@riverside.example"  # 158 characters
    with pytest.raises(ValidationError) as e:
        services.invite_user(ctx, email=long, first_name="A", last_name="B", role=role("technician"))
    assert message(e) == f"Keep the email to {services.EMAIL_MAX} characters."
