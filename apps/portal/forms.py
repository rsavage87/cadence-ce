from django import forms

from apps.equipment.models import Asset, Department
from apps.facility.services import at_domains, domains_text
from apps.workorders.models import Urgency


class ServiceRequestForm(forms.Form):
    asset_tag = forms.CharField(max_length=40, label="Asset tag (on the CE sticker)")
    department = forms.ModelChoiceField(queryset=Department.objects.none(), label="Department")
    room = forms.CharField(max_length=40, required=False, label="Room or bed")
    requester_name = forms.CharField(max_length=120, required=False, label="Your name")
    callback = forms.CharField(max_length=60, label="Callback extension")
    problem = forms.CharField(widget=forms.Textarea(attrs={"rows": 4}), label="What is happening?")
    urgency = forms.ChoiceField(choices=Urgency.choices, initial=Urgency.NORMAL, widget=forms.RadioSelect)
    tagged_out = forms.BooleanField(required=False, label="I have tagged the device and removed it from use")

    def __init__(self, *args, require_callback=True, email_domains=(), **kwargs):
        """`email_domains`: the facility's work email domains when it confirms requests by email (slice 13); empty when it does
        not, and then the form has no email field at all, so nothing typed into one can be stored."""
        super().__init__(*args, **kwargs)
        # Department.objects is tenant-scoped; the view wraps this form in tenant_context().
        self.fields["department"].queryset = Department.objects.all()
        self.fields["callback"].required = require_callback
        if not require_callback:
            self.fields["callback"].label = "Callback extension (optional)"
        self.email_domains = list(email_domains)
        if self.email_domains:
            self.fields["requester_email"] = forms.EmailField(
                required=False, max_length=254, label="Work email for updates (optional)",
                help_text=f"For a confirmation now and a note when the work is done. Use your {domains_text(self.email_domains)} address.",
                widget=forms.EmailInput(attrs={"autocomplete": "email", "spellcheck": "false"}))
            names = list(self.fields)
            names.remove("requester_email")
            names.insert(names.index("callback") + 1, "requester_email")
            self.order_fields(names)

    def clean_asset_tag(self):
        tag = self.cleaned_data["asset_tag"].strip().upper()
        asset = Asset.objects.filter(tag__iexact=tag).exclude(status="retired").select_related("device_model").first()
        if asset is None:
            raise forms.ValidationError("We could not find that tag. Check the CE sticker, or call the shop.")
        self.asset = asset
        return tag

    def clean_requester_email(self):
        """Lowercased, and only at one of the facility's domains (exactly): the public form can never send mail anywhere else."""
        email = (self.cleaned_data.get("requester_email") or "").strip().lower()
        if email and not at_domains(email, self.email_domains):
            raise forms.ValidationError(f"Use your {domains_text(self.email_domains)} work email, or leave this blank.")
        return email
