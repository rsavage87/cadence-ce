from django import forms

from apps.equipment.models import Asset, Department
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

    def __init__(self, *args, require_callback=True, **kwargs):
        super().__init__(*args, **kwargs)
        # Department.objects is tenant-scoped; the view wraps this form in tenant_context().
        self.fields["department"].queryset = Department.objects.all()
        self.fields["callback"].required = require_callback

    def clean_asset_tag(self):
        tag = self.cleaned_data["asset_tag"].strip().upper()
        asset = Asset.objects.filter(tag__iexact=tag).exclude(status="retired").select_related("device_model").first()
        if asset is None:
            raise forms.ValidationError("We could not find that tag. Check the CE sticker, or call the shop.")
        self.asset = asset
        return tag
