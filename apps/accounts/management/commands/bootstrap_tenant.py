"""
Create a tenant with its default roles and a first director user.

    python manage.py bootstrap_tenant --name "Riverside Regional" --slug riverside --admin-email kim@riverside.example
    python manage.py bootstrap_tenant --name "Riverside Regional" --slug riverside --admin-email kim@riverside.example --invite

With --invite the director gets an invitation email to set their own password instead of a password printed here. The
command also prints the invitation link, so an operator can hand it over when email is not configured yet.
"""
import secrets

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.accounts import invitations
from apps.accounts.models import User, create_default_roles
from apps.tenants.context import tenant_context
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Create a tenant, its default roles, and a director user."

    def add_arguments(self, parser):
        parser.add_argument("--name", required=True)
        parser.add_argument("--slug", required=True)
        parser.add_argument("--admin-email", required=True)
        how = parser.add_mutually_exclusive_group()
        how.add_argument("--password", default=None, help="Omit to generate one.")
        how.add_argument("--invite", action="store_true", help="Email the director an invitation link to set their own password.")

    def handle(self, *args, **opts):
        if opts["invite"] and opts["password"]:  # call_command() with keyword options skips argparse's group check
            raise CommandError("Use either --invite or --password, not both.")
        tenant, created = Tenant.objects.get_or_create(slug=opts["slug"], defaults={"name": opts["name"]})
        # Inside the new tenant from here on: roles are tenant-scoped, and row-level security only shows (and accepts) a
        # tenant's rows while that tenant is set.
        with tenant_context(tenant):
            self._set_up(tenant, created, opts)

    def _set_up(self, tenant, created, opts):
        roles = create_default_roles(tenant)
        director = next(r for r in [*roles, *tenant_roles(tenant)] if r.slug == "director")
        password = opts["password"] or secrets.token_urlsafe(12)
        defaults = {"email": opts["admin_email"], "tenant": tenant, "role": director, "is_staff": True}
        if opts["invite"]:
            defaults["is_invited"] = True
        user, user_created = User.objects.get_or_create(username=opts["admin_email"], defaults=defaults)
        if user_created:
            if opts["invite"]:
                user.set_unusable_password()
            else:
                user.set_password(password)
            user.save()
        self.stdout.write(self.style.SUCCESS(f"Tenant {'created' if created else 'exists'}: {tenant.name} ({tenant.slug})"))
        self.stdout.write(f"Roles: {', '.join(r.slug for r in tenant_roles(tenant))}")
        if not user_created:
            self.stdout.write(f"User {user.username} already existed")
        elif opts["invite"]:
            self._invite(user)
        else:
            self.stdout.write(self.style.SUCCESS(f"Director user {user.username} created, password: {password}"))

    def _invite(self, user):
        self.stdout.write(self.style.SUCCESS(f"Director user {user.username} created as an invitation"))
        if invitations.send_invitation(user):
            self.stdout.write(f"Invitation email sent to {user.email}")
        else:
            self.stdout.write(self.style.WARNING(f"The invitation email to {user.email} could not be sent; hand over the link below instead"))
        # Sending replaced any earlier link, so build it now. It works once, for INVITATION_VALID_DAYS days.
        self.stdout.write(f"Invitation link (works for {settings.INVITATION_VALID_DAYS} days): {invitations.invitation_url(user)}")


def tenant_roles(tenant):
    from apps.accounts.models import Role

    return list(Role.unscoped.filter(tenant=tenant))  # unscoped: command runs without a request context
