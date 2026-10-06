"""
Create a tenant with its default roles and a first director user.

    python manage.py bootstrap_tenant --name "Riverside Regional" --slug riverside --admin-email kim@riverside.example
    python manage.py bootstrap_tenant --name "Riverside Regional" --slug riverside --admin-email kim@riverside.example --invite

With --invite the director gets an invitation email to set their own password instead of a password printed here. The
command also prints the invitation link, so an operator can hand it over when email is not configured yet.

Slice 22: the director's account is added the way Invite user adds one (apps.accounts.services.add_account). An address that
another facility's account already uses gets an account here for that same person, as an invitation, and the other facility's
account is never reused, moved, or changed. A person who already signs in to Cadence CE keeps their one password: no password is
set or printed for them; they sign in as usual and join the new facility (the printed join page, or the facility menu), and
--invite emails them that link.
"""
import secrets

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.accounts import invitations, services
from apps.accounts.models import create_default_roles
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
        email = services.normalize_email(opts["admin_email"])
        self.stdout.write(self.style.SUCCESS(f"Tenant {'created' if created else 'exists'}: {tenant.name} ({tenant.slug})"))
        self.stdout.write(f"Roles: {', '.join(r.slug for r in tenant_roles(tenant))}")
        if services.is_member(tenant, email):  # this facility's own accounts only; another facility's is linked below, never reused
            self.stdout.write(f"User {email} already existed")
            return
        user = self._add_director(tenant, director, email)
        if opts["invite"]:
            self._invite(user)
        elif invitations.joins_signed_in(user):
            self._join(user)
        else:
            password = opts["password"] or secrets.token_urlsafe(12)
            user.set_password(password)  # for a person in other facilities too, who cannot sign in yet: then it is theirs everywhere
            user.is_invited = False
            user.save(update_fields=["password", "is_invited"])
            self.stdout.write(self.style.SUCCESS(f"Director user {email} created, password: {password}"))

    def _add_director(self, tenant, director, email):
        try:
            return services.add_account(tenant, email=email, role=director, is_staff=True)
        except ValidationError as e:
            raise CommandError(f"{email}: {' '.join(e.messages)}") from e

    def _join(self, user):
        self.stdout.write(self.style.SUCCESS(f"Director user {user.email} created as an invitation to join {user.tenant.name}"))
        self.stdout.write(f"{user.email} already signs in to Cadence CE at another facility, so no password is set here: they sign in as "
                          f"usual and join {user.tenant.name} from the facility menu, or at {invitations.join_url(user)}")

    def _invite(self, user):
        self.stdout.write(self.style.SUCCESS(f"Director user {user.email} created as an invitation"))
        if invitations.send_invitation(user):
            self.stdout.write(f"Invitation email sent to {user.email}")
        else:
            self.stdout.write(self.style.WARNING(f"The invitation email to {user.email} could not be sent; hand over the link below instead"))
        if invitations.joins_signed_in(user):
            self.stdout.write(f"{user.email} already signs in to Cadence CE at another facility: they sign in as usual and join here at "
                              f"{invitations.join_url(user)}")
            return
        # Sending replaced any earlier link, so build it now. It works once, for INVITATION_VALID_DAYS days.
        self.stdout.write(f"Invitation link (works for {settings.INVITATION_VALID_DAYS} days): {invitations.invitation_url(user)}")


def tenant_roles(tenant):
    from apps.accounts.models import Role

    return list(Role.unscoped.filter(tenant=tenant))  # unscoped: command runs without a request context

