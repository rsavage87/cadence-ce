"""
Device incidents (slice 28, beyond the mock): a device in Cadence suspected in an event, held as evidence, investigated, and decided
under the Safe Medical Devices Act (21 CFR 803), with the reports sent against the 10-work-day clock (apps.core.workdays).

Privacy (CLAUDE.md non-negotiable 6): incidents are the one area of Cadence holding facts about a health event of an identifiable
person (the day, the outcome, a device on a named unit). No field here is free text: the narrative and the deliberations on whether
it was reportable live in the facility's own event report, which `event_reference` points to (803.18 lets the event file reference
other records). Outcome and affected are shown only to Incidents View; emails carry the number, the device tag, and the due date.

Every write goes through apps.incidents.services. Incidents are never deleted (803.18 keeps an event file two years from the event);
one recorded on the wrong device is marked "recorded in error".
"""
from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models
from simple_history.models import HistoricalRecords

from apps.core.models import TenantModel
from apps.equipment.models import AssetStatus

# The facility's event report number: no spaces, so never a name (the form says never a patient's record number either).
EVENT_REFERENCE_VALIDATOR = RegexValidator(r"^[A-Za-z0-9][A-Za-z0-9._/#-]*$",
                                           "Enter the event report's number as your event reporting system shows it, without spaces.")
# 803.3(x): the facility's 10-digit CMS (or FDA-assigned) number, the 4-digit year the report was sent, a 4-digit sequence.
REPORT_NUMBER_VALIDATOR = RegexValidator(r"^\d{10}-\d{4}-\d{4}$",
                                         "Enter the report number as 21 CFR 803 forms it: the facility's 10-digit number, the year, and a "
                                         "4-digit sequence (0123456789-2026-0001).")


class Outcome(models.TextChoices):
    DEATH = "death", "Death"
    SERIOUS_INJURY = "serious_injury", "Serious injury or illness (life-threatening, permanent damage, or needed treatment to prevent it)"
    UNKNOWN = "unknown", "Not known yet (treated as serious until decided)"
    INJURY = "injury", "Injury, not serious"
    NO_HARM = "no_harm", "No harm (malfunction or near miss)"


# The outcomes that start the 10-work-day clock (for a patient of the facility), those that harmed someone, and their order (a raise
# needs Edit, a lowering Approve).
CLOCK_OUTCOMES = (Outcome.DEATH, Outcome.SERIOUS_INJURY, Outcome.UNKNOWN)
HARM_OUTCOMES = CLOCK_OUTCOMES + (Outcome.INJURY,)
OUTCOME_RANK = {Outcome.NO_HARM: 0, Outcome.INJURY: 1, Outcome.UNKNOWN: 2, Outcome.SERIOUS_INJURY: 3, Outcome.DEATH: 4}
SHORT_OUTCOMES = {Outcome.DEATH: "Death", Outcome.SERIOUS_INJURY: "Serious injury", Outcome.UNKNOWN: "Not known yet",
                  Outcome.INJURY: "Injury, not serious", Outcome.NO_HARM: "No harm"}


class Affected(models.TextChoices):
    """Who was harmed, as a category; never who. 803.3(t): a patient of the facility includes its staff on duty."""
    PATIENT = "patient", "Patient"
    STAFF = "staff", "Staff member on duty"
    OTHER = "other", "Visitor or another person"
    NONE = "none", "No one"


FACILITY_PATIENTS = (Affected.PATIENT, Affected.STAFF)


class Basis(models.TextChoices):
    """The reportability decision's basis (803.18: the deliberations themselves are in the facility's event report)."""
    MAY_HAVE = "may_have", ("The information reasonably suggests the device may have caused or contributed (use error included), "
                            "or it cannot be ruled out")
    NOT_SERIOUS = "not_serious", "Not a death or serious injury as 21 CFR 803 defines it"
    NO_SUGGESTION = "no_suggestion", "The information does not suggest the device caused or contributed"
    NOT_PATIENT = "not_patient", "The person was not a patient of the facility or staff on duty"


class DecidedBy(models.TextChoices):
    RISK = "risk", "Risk management or patient safety"
    COMMITTEE = "committee", "A safety or MDR committee"
    CE = "ce", "Clinical Engineering"


class Finding(models.TextChoices):
    """The device evaluation's result: what was found on the device, never who was at fault (the root cause is the facility's
    analysis, in its event report)."""
    DEVICE_FAILURE = "device_failure", "Device failure or malfunction found"
    ACCESSORY_FAILURE = "accessory_failure", "Failure found in an accessory, disposable, or connected device"
    UTILITY = "utility", "A power, medical gas, or other utility problem found"
    MET_SPECS = "met_specs", "The device met its specifications (no device fault found)"
    MANUFACTURER = "manufacturer", "Evaluated by the manufacturer"
    NOT_EVALUATED = "not_evaluated", "Could not be evaluated"


FAILURE_FINDINGS = (Finding.DEVICE_FAILURE, Finding.ACCESSORY_FAILURE)


class Accessories(models.TextChoices):
    KEPT = "kept", "Kept with the device"
    NONE_USED = "none_used", "None were in use"
    NOT_AVAILABLE = "not_available", "Not available (discarded or lost)"


class EventLog(models.TextChoices):
    SAVED = "saved", "Saved"
    NO_LOG = "no_log", "The device keeps no log"
    LOST = "lost", "Lost or could not be retrieved"


class Status(models.TextChoices):
    OPEN = "open", "Open"
    CLOSED = "closed", "Closed"
    IN_ERROR = "in_error", "Recorded in error"


class Release(models.TextChoices):
    RETURN_TO_USE = "return_to_use", "Returned to use"
    KEEP_OUT = "keep_out", "Kept out of service"
    KEPT_BY_MANUFACTURER = "kept_by_manufacturer", "Kept by the manufacturer"
    IN_ERROR = "in_error", "Recorded in error"


# The releases a person chooses (in_error comes only with "recorded in error").
RELEASE_CHOICES = (Release.RETURN_TO_USE, Release.KEEP_OUT, Release.KEPT_BY_MANUFACTURER)


class AwareChange(models.TextChoices):
    """Why aware_on moved later (Approve): the history's reason, not a field."""
    SERIOUS_LATER = "serious_later", "The death or serious injury became known later"
    INVOLVEMENT_LATER = "involvement_later", "The device's possible part became known later"
    CORRECTION = "correction", "Correction"


class Incident(TenantModel):
    number = models.CharField(max_length=20, editable=False, help_text="IN-26-0004 (Sequence 'incident-<yy>')")
    asset = models.ForeignKey("equipment.Asset", on_delete=models.PROTECT, related_name="incidents", help_text="The device suspected")
    work_order = models.ForeignKey("workorders.WorkOrder", on_delete=models.PROTECT, null=True, blank=True, related_name="incidents",
                                   help_text="The investigation: the request it was reported as (adopted), or one the incident opened")
    opened_work_order = models.BooleanField(default=False, help_text="The incident opened its work order (else it adopted an open one)")
    occurred_on = models.DateField()
    aware_on = models.DateField(help_text="When the facility's clinical staff first knew: the 10-work-day clock starts the day after")
    outcome = models.CharField(max_length=20, choices=Outcome.choices)
    affected = models.CharField(max_length=10, choices=Affected.choices)
    event_reference = models.CharField(max_length=40, blank=True, validators=[EVENT_REFERENCE_VALIDATOR],
                                       help_text="The number in the facility's event reporting system, where the narrative and the "
                                                 "deliberations are")
    report_due_on = models.DateField(null=True, blank=True, editable=False,
                                     help_text="The 10th work day after aware_on while the incident has a clock (services only)")
    # The reportability decision (803.18): reportable (null until decided), its basis, the day and the body that decided, who typed it.
    reportable = models.BooleanField(null=True, blank=True)
    basis = models.CharField(max_length=20, choices=Basis.choices, blank=True)
    decided_on = models.DateField(null=True, blank=True)
    decided_by = models.CharField(max_length=20, choices=DecidedBy.choices, blank=True)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
                                    help_text="Who recorded the decision in Cadence")
    # The reports sent (one report number, one or two copies).
    fda_reported_on = models.DateField(null=True, blank=True)
    manufacturer_reported_on = models.DateField(null=True, blank=True)
    report_number = models.CharField(max_length=20, blank=True, validators=[REPORT_NUMBER_VALIDATOR])
    finding = models.CharField(max_length=20, choices=Finding.choices, blank=True)
    accessories = models.CharField(max_length=20, choices=Accessories.choices, blank=True,
                                   help_text="The disposables and accessories in use, kept with the device as evidence")
    event_log = models.CharField(max_length=20, choices=EventLog.choices, blank=True, help_text="The device's event log")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    closed_on = models.DateField(null=True, blank=True)
    closed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    history = HistoricalRecords()

    class Meta:
        ordering = ["-occurred_on", "-number"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "number"], name="uniq_incident_number_per_tenant"),
            models.UniqueConstraint(fields=["tenant", "report_number"], condition=~models.Q(report_number=""),
                                    name="uniq_incident_report_number_per_tenant"),
        ]
        indexes = [models.Index(fields=["tenant", "status"]), models.Index(fields=["tenant", "occurred_on"])]

    def __str__(self):
        return self.number


class IncidentHold(TenantModel):
    """One device held as evidence by one incident: the suspect device, and any other part of the system (a pump's channel module,
    the generator an electrosurgical accessory was used with). The device's own flag (Asset.incident_hold) is set while any open incident holds
    it. Custody while held: sent to the manufacturer for evaluation and back, inside the hold (803.32 asks for the date)."""
    incident = models.ForeignKey(Incident, on_delete=models.CASCADE, related_name="holds")
    asset = models.ForeignKey("equipment.Asset", on_delete=models.PROTECT, related_name="incident_holds")
    held_on = models.DateField()
    status_before = models.CharField(max_length=20, choices=AssetStatus.choices, help_text="The device's status when held (in_error restores it)")
    sent_on = models.DateField(null=True, blank=True, help_text="Sent to the manufacturer for evaluation")
    back_on = models.DateField(null=True, blank=True, help_text="Back from the manufacturer")
    released_on = models.DateField(null=True, blank=True)
    release = models.CharField(max_length=30, choices=Release.choices, blank=True)
    released_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    history = HistoricalRecords()

    class Meta:
        ordering = ["held_on", "created_at"]
        constraints = [models.UniqueConstraint(fields=["incident", "asset"], name="uniq_incident_hold_per_device")]

    def __str__(self):
        return f"{self.incident.number} · {self.asset.tag}"

    @property
    def active(self) -> bool:
        return self.released_on is None

    @property
    def with_manufacturer(self) -> bool:
        return self.sent_on is not None and self.back_on is None
