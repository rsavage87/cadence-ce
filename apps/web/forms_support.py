"""
Record support (slice 36), the model drawer's modal: where a model's expected life comes from, and the manufacturer's support as its
notice states it (the three days, the notice's reference) with the day CE last checked. The rules (a choice or not recorded, real days
in any order, the reference a token of up to 40 characters, checked on never after today) live in
apps.equipment.services.update_device_model; this form only reads what was posted, as typed, so a refused value stays in its box with
the service's words under it. Never part of Add model, Edit details, or Add device (DeviceModelForm is unchanged).
"""
from datetime import date

from django import forms

from apps.equipment.models import DeviceModel, LifeSource
from apps.equipment.services import SUPPORT_FIELDS

from .forms_models import ServiceErrorsMixin

NOT_RECORDED = "Not recorded"


def _day_box() -> forms.TextInput:
    """A browser date picker that posts YYYY-MM-DD (a text box where the browser has none). Text, not a DateField: the service reads
    and refuses the value, and a refused one stays as typed."""
    return forms.TextInput(attrs={"type": "date"})


def _words(day: date) -> str:
    return f"{day:%b} {day.day}, {day.year}"


class SupportForm(ServiceErrorsMixin, forms.Form):
    """The six support fields as text (days as the browser's YYYY-MM-DD), parsed and checked by update_device_model. Opened, it shows
    the model's values with checked on set to today: recording support is confirming it now (an earlier day can be chosen)."""

    layout = SUPPORT_FIELDS

    life_source = forms.CharField(label="Expected life comes from", required=False,
                                  widget=forms.Select(choices=[("", NOT_RECORDED), *LifeSource.choices], attrs={"autofocus": True}))
    sale_ended_on = forms.CharField(label="No longer sold from", required=False, widget=_day_box(),
                                    help_text="End of sale, past or announced.")
    support_ends_on = forms.CharField(label="Manufacturer support ends", required=False, widget=_day_box(),
                                      help_text="The last day of service support, past or announced.")
    parts_end_on = forms.CharField(label="Parts available until", required=False, widget=_day_box(),
                                   help_text="The last day parts can be had, past or announced.")
    support_notice = forms.CharField(label="Manufacturer's notice", required=False,
                                     help_text="Its reference as the letter shows it, e.g. FSN-2025-014. Optional.",
                                     widget=forms.TextInput(attrs={"autocomplete": "off", "maxlength": 40, "spellcheck": "false"}))
    support_checked_on = forms.CharField(label="Support checked", required=False, widget=_day_box())

    def __init__(self, *args, device_model: DeviceModel, today: date, **kwargs):
        kwargs.setdefault("auto_id", "sp-%s")
        if not args and not kwargs.get("data"):
            initial = {name: getattr(device_model, name) for name in SUPPORT_FIELDS}
            initial = {name: value.isoformat() if isinstance(value, date) else (value or "") for name, value in initial.items()}
            kwargs["initial"] = {**initial, "support_checked_on": today.isoformat()}
        super().__init__(*args, **kwargs)
        self.device_model = device_model
        life = device_model.expected_life_years
        self.fields["life_source"].help_text = (f"Its expected life is {life} year{'' if life == 1 else 's'} (Edit details changes it). "
                                                "Not recorded: the replacement report lists the model to look up.")
        checked = self.fields["support_checked_on"]
        checked.widget.attrs["max"] = today.isoformat()  # never after today (the service refuses it too)
        last = device_model.support_checked_on
        checked.help_text = ("The day CE confirmed this with the manufacturer: today, unless it was another day."
                             + (f" Last recorded {_words(last)}." if last else ""))

    def service_fields(self) -> dict:
        """update_device_model's keyword arguments: each field as posted (blank clears it), so the service checks exactly what was typed."""
        return {name: self.cleaned_data.get(name) or "" for name in SUPPORT_FIELDS}
