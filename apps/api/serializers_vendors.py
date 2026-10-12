"""
Vendor staff over the API (slice 31; apps/api/views_vendors.py, and a work order's engineer_choices in serializers.WorkOrderSerializer).
The serializers show; they never save: the views hand what a body sent to apps.vendors.services, which validate and record the history.
The words are apps.vendors.rules' (coverage_words, training_words, evidence_words): Cadence keeps documents, never judges competence.

A record's coverage reads its device models' names, from one read of the catalog per page (`models_by_id`, put in the context by the
view as `models_by_id`), and whether it is in force today (the facility's day, read once per request, the context's `today`).
"""
from collections import defaultdict

from django.utils import timezone
from rest_framework import serializers

from apps.vendors import rules
from apps.vendors import services as vendor_services
from apps.vendors.models import EngineerTraining, VendorAttestation, VendorEngineer
from apps.workorders import scoping


def models_by_id(records) -> dict:
    """{str(id): DeviceModel} for every catalog model `records` (trainings or attestations) name: one query, none when no record names
    a model. A model no longer in the catalog is left out (coverage_words reads it "a removed model")."""
    from apps.equipment.models import DeviceModel

    ids = {str(i) for r in records for i in (r.model_ids or ())}
    if not ids:
        return {}
    return {str(m.pk): m for m in DeviceModel.objects.filter(pk__in=ids).only("id", "manufacturer", "model", "category")}


def _known(context, record) -> dict:
    """The page's models (the view reads them at once); a record shown alone, outside a view, reads its own."""
    known = context.setdefault("models_by_id", {})
    missing = {str(i) for i in record.model_ids or ()} - known.keys()
    if missing:
        known.update(models_by_id([record]))
    return known


def _today(context):
    if "today" not in context:
        context["today"] = timezone.localdate()
    return context["today"]


class CoverageFields(serializers.ModelSerializer):
    """What a training or an attestation covers: covers (Covers: models, line, or, for an attestation, all) with covers_label,
    model_ids (catalog device model ids), manufacturer and category (a product category), and coverage, in words; and the day it was
    entered. Read only: each subclass names its model and fields."""

    covers_label = serializers.CharField(source="get_covers_display", read_only=True)
    model_ids = serializers.ListField(child=serializers.CharField(), read_only=True)
    coverage = serializers.SerializerMethodField()
    entered_on = serializers.SerializerMethodField()

    def get_coverage(self, record) -> str:
        return rules.coverage_words(record, _known(self.context, record))

    def get_entered_on(self, record):
        """The facility's day the record was entered in Cadence (the binder's "on file since")."""
        return rules.entered_on(record).isoformat()


class TrainingSerializer(CoverageFields):
    """One engineer's training on file: its source, certificate number, issued_on to expires_on (null: no expiry), in_force today, and
    words ("Training on file: Philips IntelliVue MX750, Manufacturer's (OEM) factory training, through Mar 31, 2027")."""

    engineer_name = serializers.CharField(source="engineer.name", read_only=True)
    company = serializers.CharField(source="engineer.company", read_only=True)
    source_label = serializers.CharField(source="get_source_display", read_only=True)
    in_force = serializers.SerializerMethodField()
    words = serializers.SerializerMethodField()

    class Meta:
        model = EngineerTraining
        fields = ["id", "engineer", "engineer_name", "company", "covers", "covers_label", "model_ids", "manufacturer", "category", "coverage",
                  "source", "source_label", "certificate", "issued_on", "expires_on", "in_force", "words", "entered_on"]
        read_only_fields = fields

    def get_in_force(self, training) -> bool:
        return rules.training_in_force(training, _today(self.context))

    def get_words(self, training) -> str:
        return rules.training_words(training, _known(self.context, training))


class EngineerSerializer(serializers.ModelSerializer):
    """A vendor's engineer as their reports name them: name, company (the vendor), is_active, and their trainings on file, newest first."""

    trainings = TrainingSerializer(many=True, read_only=True)

    class Meta:
        model = VendorEngineer
        fields = ["id", "name", "company", "is_active", "trainings"]
        read_only_fields = fields


class AttestationSerializer(CoverageFields):
    """A vendor's attestation on file: the vendor's names, its reference, received_on, valid_from to valid_through, what it covers,
    and in_force today."""

    vendor_names = serializers.ListField(child=serializers.CharField(), read_only=True)
    in_force = serializers.SerializerMethodField()

    class Meta:
        model = VendorAttestation
        fields = ["id", "vendor_names", "reference", "received_on", "valid_from", "valid_through", "covers", "covers_label", "model_ids",
                  "manufacturer", "category", "coverage", "in_force", "entered_on"]
        read_only_fields = fields

    def get_in_force(self, attestation) -> bool:
        return rules.attestation_in_force(attestation, _today(self.context))


# --- a work order's engineer choices -------------------------------------------------------------------------------------------

def engineer_choices(wo, user, today) -> dict:
    """The engineers `user` may name on `wo` (vendor_services.engineer_options): {"vendor": the work order's vendor's (the one named
    now first, even when inactive), "other_vendors": every other vendor's active engineers ([] for a scoped user)}, each {"id",
    "name", "company", "named" (the one the work order names now)}. For a user who sees the whole facility, each also carries on_file:
    the evidence on file for that engineer on this device on the day judged (the day it was completed, else today), in
    rules.evidence_words' words, as the completion modal's options carry it; a scoped user (a vendor account) gets names only. A fixed
    number of queries: the options, then (unscoped) the trainings, the attestations, and the catalog models they name."""
    own, others = vendor_services.engineer_options(wo, user)
    on_file = {}
    if not scoping.is_scoped(user) and (own or others):
        engineers = [*own, *others]
        trainings = list(EngineerTraining.objects.filter(engineer__in=engineers))
        attestations = list(VendorAttestation.objects.all())
        known = models_by_id([*trainings, *attestations])
        mine = defaultdict(list)
        for t in trainings:
            mine[t.engineer_id].append(t)
        day, device_model = wo.completed_on or today, wo.asset.device_model
        for e in engineers:
            evidence = rules.evidence_for(day=day, device_model=device_model, vendor_name=wo.vendor_name, engineer=e,
                                          trainings=mine[e.pk], attestations=attestations)
            on_file[e.pk] = rules.evidence_words(evidence, known)

    def item(e: VendorEngineer) -> dict:
        row = {"id": str(e.pk), "name": e.name, "company": e.company, "named": e.pk == wo.vendor_engineer_id}
        if e.pk in on_file:
            row["on_file"] = on_file[e.pk]
        return row

    return {"vendor": [item(e) for e in own], "other_vendors": [item(e) for e in others]}

