"""Input parsing for the Technician credentials tab. Technician and "Covers" choices depend on the tenant, so they are built in __init__."""
from datetime import date

from django import forms

from apps.credentials.models import Credential, Scope, Technician
from apps.credentials.services import credential_options

from .forms import parse_uuid

# Stored as the credential's free-text `source`; the mock offers these four.
SOURCES = ["OEM training", "In-house sign-off", "Third-party course", "Certification (CBET, CRES, CLES)"]


def active_technicians():
    return Technician.objects.filter(is_active=True)


def technician_filter(params):
    """`?technician=<uuid>` narrows the tab. Returns (given, technician or None); an unknown id yields (True, None) so the tab shows nothing."""
    raw = params.get("technician", "")
    if not raw:
        return False, None
    pk = parse_uuid(raw)
    return True, (active_technicians().filter(pk=pk).first() if pk else None)


class CredentialForm(forms.Form):
    technician = forms.ChoiceField()
    covers = forms.ChoiceField()
    source = forms.ChoiceField(choices=[(s, s) for s in SOURCES])
    status = forms.ChoiceField(choices=Credential.Status.choices)
    issued_on = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}), label="Issued")
    expires_on = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}), required=False, label="Expires, if any")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.technicians = {str(t.id): t for t in active_technicians()}
        self.fields["technician"].choices = [(pk, t.name) for pk, t in self.technicians.items()]
        self.fields["covers"].choices = credential_options()
        self.fields["issued_on"].initial = date.today()

    def clean_technician(self):
        return self.technicians[self.cleaned_data["technician"]]

    def clean_covers(self):
        scope, _, value = self.cleaned_data["covers"].partition("|")
        if scope not in Scope.values or not value:
            raise forms.ValidationError("Choose what the credential covers.")
        return scope, value

    def clean(self):
        d = super().clean()
        if d.get("issued_on") and d.get("expires_on") and d["expires_on"] < d["issued_on"]:
            self.add_error("expires_on", "The expiry date cannot be before the issue date.")
        return d
