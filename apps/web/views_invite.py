"""
Accepting an invitation (slice 10): the link in the invitation email sets the first password and signs the account in.

Django's PasswordResetConfirmView does the work with the invitation's own token generator: a valid link moves the token
into the session and redirects to .../set-password/, so the token never sits in the address bar of the page with the form
(and the layout sends no Referer). Only a pending invitation of an active facility can be opened; anything else, garbage
included, renders the "link no longer works" page. Setting the password changes the hashed state, so the link is used up.
"""
from django.conf import settings
from django.contrib.auth import password_validation
from django.contrib.auth import views as auth_views
from django.urls import reverse_lazy

from apps.accounts import invitations


class InviteAcceptView(auth_views.PasswordResetConfirmView):
    token_generator = invitations.invitation_tokens
    post_reset_login = True
    post_reset_login_backend = "apps.accounts.backends.UsernameOrEmailBackend"
    success_url = reverse_lazy("web:overview")
    template_name = "web/invite_accept.html"
    title = "Set your password"

    def get_user(self, uidb64):
        return invitations.pending_user_from_uid(uidb64)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if context.get("validlink"):
            context.update(invitee=self.user, facility=self.user.tenant.name if self.user.tenant_id else "",
                           password_help=password_validation.password_validators_help_texts())
        else:
            context["title"] = "This link no longer works"
        context["valid_days"] = settings.INVITATION_VALID_DAYS
        return context


invite_accept = InviteAcceptView.as_view()
