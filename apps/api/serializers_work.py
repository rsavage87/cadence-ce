"""
A work order's labor, parts, and notes over the API (slice 19, part A; apps/api/views_work.py).

The body serializers parse types only: every field is optional text (a JSON number is read as its text), so the rules and their
wording stay in apps.workorders.costs and apps.workorders.services, as the drawer's forms leave them there
(apps/web/forms_wo_costs.py). A list, an object, or true/false where text belongs is refused here ("Not a valid string."), as are
NUL characters. Nothing saves through these: the views hand the parsed values to the services.

The line serializers show what the drawer's Cost section shows for each line (apps/web/views_wo_costs.costs_context): who did the
work (the technician, else the vendor), and the amount, worked to the cent half up (apps.workorders.costs.labor_amount, part_amount).
"""
from rest_framework import serializers

from apps.workorders import costs, scoping

from . import serializers as s


def _text(**kwargs):
    return serializers.CharField(required=False, allow_blank=True, allow_null=True, **kwargs)


def money(value) -> str:
    """A Decimal amount as the API writes money: "123.00"."""
    return f"{costs.cents(value):.2f}"


class BodySerializer(serializers.Serializer):
    """Refuses a field it does not know (400, keyed by the field) rather than dropping it, so a client never thinks it was saved."""

    def validate(self, attrs):
        unknown = sorted(set(self.initial_data) - set(self.fields) - {"csrfmiddlewaretoken"})  # the browsable API's HTML form posts its token
        if unknown:
            raise serializers.ValidationError({name: ["Unknown field."] for name in unknown})
        return attrs


class LaborBodySerializer(BodySerializer):
    """POST .../labor/. worked_on is YYYY-MM-DD (the view reads it); hours, rate, and the technician's id are text or numbers."""

    worked_on = _text()
    hours = _text()
    technician = _text()
    rate = _text()
    description = _text()


class PartBodySerializer(BodySerializer):
    description = _text()
    part_number = _text()
    quantity = _text()
    unit_cost = _text()
    po_number = _text()


class NoteBodySerializer(BodySerializer):
    text = _text(trim_whitespace=False)  # add_note trims; the API leaves the text as sent until then


class LaborLineSerializer(s.LaborLineSerializer):
    """A labor line as the drawer lists it. `who` is the technician's name, else the work order's vendor ("Vendor service" when it
    names none), as the drawer says it."""

    who = serializers.SerializerMethodField()
    amount = serializers.SerializerMethodField()

    class Meta(s.LaborLineSerializer.Meta):
        fields = ["id", "technician", "who", "worked_on", "hours", "rate", "amount", "description", "created_at"]
        read_only_fields = fields

    def get_who(self, line) -> str:
        if line.technician_id and line.technician is not None:
            return line.technician.name
        return line.work_order.vendor_name or "Vendor service"

    def get_amount(self, line) -> str:
        return money(costs.labor_amount(line))


class PartLineSerializer(s.PartLineSerializer):
    amount = serializers.SerializerMethodField()

    class Meta(s.PartLineSerializer.Meta):
        fields = ["id", "description", "part_number", "quantity", "unit_cost", "po_number", "amount", "created_at"]
        read_only_fields = fields

    def get_amount(self, line) -> str:
        return money(costs.part_amount(line))


class TimelineEntrySerializer(serializers.Serializer):
    """One line of the drawer's timeline (apps.workorders.services.timeline): status changes, notes, and labor and parts, each with
    when and who. As a scoped user may read it, the number of a work order outside their share reads "another work order"
    (apps.workorders.scoping.shown_text), as in the drawer."""

    at = serializers.DateTimeField()
    who = serializers.CharField()
    text = serializers.CharField()

    def to_representation(self, entry):
        data = super().to_representation(entry)
        request = self.context.get("request")
        user = getattr(request, "user", None)
        if user is not None and scoping.is_scoped(user):
            data["text"] = scoping.shown_text(user, data["text"], self.context.setdefault("_scoped_numbers", {}))
        return data
