"""
Create a tenant with its default roles and a first director user.

    python manage.py bootstrap_tenant --name "Riverside Regional" --slug riverside --admin-email kim@riverside.example
"""
import secrets

from django.core.management.base import BaseCommand

from apps.accounts.models import User, create_default_roles
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Create a tenant, its default roles, and a director user."

    def add_arguments(self, parser):
        parser.add_argument("--name", required=True)
        parser.add_argument("--slug", required=True)
        parser.add_argument("--admin-email", required=True)
        parser.add_argument("--password", default=None, help="Omit to generate one.")

    def handle(self, *args, **opts):
        tenant, created = Tenant.objects.get_or_create(slug=opts["slug"], defaults={"name": opts["name"]})
        roles = create_default_roles(tenant)
        director = next(r for r in [*roles, *tenant_roles(tenant)] if r.slug == "director")
        password = opts["password"] or secrets.token_urlsafe(12)
        user, user_created = User.objects.get_or_create(
            username=opts["admin_email"],
            defaults={"email": opts["admin_email"], "tenant": tenant, "role": director, "is_staff": True},
        )
        if user_created:
            user.set_password(password)
            user.save()
        self.stdout.write(self.style.SUCCESS(f"Tenant {'created' if created else 'exists'}: {tenant.name} ({tenant.slug})"))
        self.stdout.write(f"Roles: {', '.join(r.slug for r in tenant_roles(tenant))}")
        if user_created:
            self.stdout.write(self.style.SUCCESS(f"Director user {user.username} created, password: {password}"))
        else:
            self.stdout.write(f"User {user.username} already existed")


def tenant_roles(tenant):
    from apps.accounts.models import Role

    return list(Role.unscoped.filter(tenant=tenant))  # unscoped: command runs without a request context
