"""
Contracts over the API (slice 19, part A; apps/api/views_contracts.py). The serializer parses and shows; it never saves:
ContractViewSet hands what it parsed to apps.contracts.services (create_contract, update_contract), which validate (a reference
unique in any letter case, the dates in order, a cost of 0 or more) and record the history.
"""
from rest_framework import serializers

from apps.contracts.services import EDITABLE

from . import serializers as s

NOTES_MAX = 500  # the Contracts screen's form (apps/web/forms_contracts.ContractForm)
READ_BACK = ("id", "status", "device_count", "updated_at")  # what GET returns but a write ignores, so a client can send back what it read


class ContractSerializer(s.ContractSerializer):
    """device_count is the covered (not retired) devices, read from the list's annotation when there is one."""

    notes = serializers.CharField(required=False, allow_blank=True, max_length=NOTES_MAX)

    def get_device_count(self, obj):
        annotated = getattr(obj, "devices", None)
        return annotated if annotated is not None else obj.covered_assets().count()

    def validate(self, attrs):
        # Anything but the fields the services take and READ_BACK is refused, never dropped (the browsable API's form posts its token).
        unknown = sorted(set(self.initial_data) - set(EDITABLE) - set(READ_BACK) - {"csrfmiddlewaretoken"})
        if unknown:
            raise serializers.ValidationError({name: ["Unknown field."] for name in unknown})
        return attrs
