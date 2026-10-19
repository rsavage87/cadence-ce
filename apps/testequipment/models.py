"""
The CE department's own test equipment and its calibration (slice 35, beyond the mock): the safety analyzers, simulators, and meters a
technician uses on PMs, incoming inspections, safety checks, and checks after repair, the certificates that renew them, the review of
the work done with one later found out of tolerance, and which test equipment each work order recorded.

No US rule (CMS, The Joint Commission's 2026 Physical Environment chapter, NFPA 99) names CE's own test equipment; DNV surveyors apply
ISO 9001's control of measuring equipment to it (calibrated at intervals, traceable, its status identified, the test equipment used on
the records, and the validity of earlier results assessed when one is found out of tolerance). Cadence keeps the records and never says
a standard requires them: its words are the records' ("Calibration due Mar 31, 2027", "Calibration was due Aug 31, 2026", "No
calibration on file", "Failed calibration Oct 3, 2026"), never "calibrated", "in date", or "uncalibrated" as a status. "Test
equipment", never "instrument" (the binder's NOT_COVERED says "loaner surgical instrument sets").

Test equipment is never an equipment.Asset: no fleet, PM, report, recall, portal, contract, or evaluation figure counts it. Its tag
shares the facility's tags with the devices (one sticker, one meaning: equipment.services.lock_tags and both writers' checks).

- TestEquipment: one item (its tag, kind, make, model, serial; its program: calibration or verification certificates; its interval;
  its status with a reason; who holds it). No notes, no free text, no cost.
- Calibration: one certificate from an outside provider (a lab or the manufacturer), judged by its own dates whenever entered; a
  renewal is a new record; one entered in error stays (marked). In-house verification against a reference standard is deferred.
- ToleranceReview: the decision on the work done with an item between its last passed certificate and one that found it out of
  tolerance (or failed it), with the list of work orders it was drawn with, frozen.
- TestEquipmentUse: one work order recorded one item. Standing is never stored: it is computed on the work's completed day (rules).

Every rule is apps.testequipment.rules; every write is apps.testequipment.services (and, for uses, completion.complete_work_order and
workorders.services.set_test_equipment).
"""
from django.conf import settings
from django.db import models
from django.db.models import Q
from simple_history.models import HistoricalRecords

from apps.core.models import TenantModel
from apps.core.validators import reference_token
from apps.equipment.models import TAG_VALIDATOR

CERTIFICATE_VALIDATOR = reference_token("Enter the certificate's number as it is printed, without spaces.")
QUALITY_REFERENCE_VALIDATOR = reference_token("Enter the quality record's number, without spaces.")
TAG_MAX = 40
TEXT_MAX = 120
SERIAL_MAX = 80
INTERVAL_MIN, INTERVAL_MAX, INTERVAL_DEFAULT = 1, 60, 12


class Kind(models.TextChoices):
    ELECTRICAL_SAFETY = "electrical_safety", "Electrical safety analyzer"
    DEFIBRILLATOR = "defibrillator", "Defibrillator and pacer analyzer"
    INFUSION = "infusion", "Infusion device analyzer"
    PATIENT_SIMULATOR = "patient_simulator", "Patient simulator"
    SPO2 = "spo2", "SpO2 simulator"
    NIBP = "nibp", "NIBP simulator"
    GAS_FLOW = "gas_flow", "Gas flow analyzer"
    ESU = "esu", "Electrosurgical (ESU) analyzer"
    PRESSURE = "pressure", "Pressure meter"
    MULTIMETER = "multimeter", "Multimeter"
    THERMOMETER = "thermometer", "Thermometer"
    TORQUE = "torque", "Torque tool"
    TEST_WEIGHTS = "test_weights", "Test weights"
    TACHOMETER = "tachometer", "Tachometer"
    RADIOMETER = "radiometer", "Phototherapy radiometer"
    INCUBATOR = "incubator", "Incubator analyzer"
    ULTRASOUND_POWER = "ultrasound_power", "Ultrasound power meter"
    OXYGEN = "oxygen", "Oxygen analyzer"
    OTHER = "other", "Other"


class Program(models.TextChoices):
    """What its certificates are: a SpO2 simulator is verified, not calibrated (there is no primary reference). The standing's words
    follow it ("Calibration due" / "Verification due")."""
    CALIBRATION = "calibration", "Calibration"
    VERIFICATION = "verification", "Verification"


class Status(models.TextChoices):
    IN_USE = "in_use", "In use"
    OUT_FOR_CALIBRATION = "out_for_calibration", "Out for calibration"
    OUT_OF_SERVICE = "out_of_service", "Out of service"
    MISSING = "missing", "Missing"
    RETIRED = "retired", "Retired"


class Reason(models.TextChoices):
    """Why it is out of service or retired (STATUS_REASONS); blank for the other statuses."""
    DAMAGED = "damaged", "Damaged"
    FAILED_CHECK = "failed_check", "Failed a check"
    FAILED_CALIBRATION = "failed_calibration", "Failed calibration"
    WORN_OUT = "worn_out", "Worn out or obsolete"
    RETURNED_TO_OWNER = "returned_to_owner", "Returned to its owner"
    LOST = "lost", "Lost"


STATUS_REASONS = {
    Status.OUT_OF_SERVICE: (Reason.DAMAGED, Reason.FAILED_CHECK, Reason.FAILED_CALIBRATION),
    Status.RETIRED: (Reason.WORN_OUT, Reason.RETURNED_TO_OWNER, Reason.LOST),
}


def _reason_rule() -> Q:
    """A reason exactly when the status takes one, and one of its own."""
    rule = Q(status_reason="") & ~Q(status__in=[s.value for s in STATUS_REASONS])
    for status, reasons in STATUS_REASONS.items():
        rule |= Q(status=status.value, status_reason__in=[r.value for r in reasons])
    return rule


class Mark(models.TextChoices):
    """Whether the certificate carries an ISO/IEC 17025 accreditation mark: Cadence cannot check a lab's scope."""
    SHOWN = "shown", "Carries an ISO/IEC 17025 accreditation mark"
    NOT_SHOWN = "not_shown", "No accreditation mark"


class AsFound(models.TextChoices):
    IN_TOLERANCE = "in_tolerance", "In tolerance as found"
    OUT_OF_TOLERANCE = "out_of_tolerance", "Out of tolerance as found"
    NOT_REPORTED = "not_reported", "As found not reported"


class Result(models.TextChoices):
    PASSED = "passed", "Passed"
    FAILED = "failed", "Failed: could not be brought into tolerance"


class Basis(models.TextChoices):
    """The tolerance review's decision on the work done with the item since it was last known good."""
    WITHIN_MARGIN = "within_margin", "The error was smaller than the margin to the limits used"
    FUNCTION_UNUSED = "function_unused", "The function found out of tolerance was not used for this work"
    RECHECKED = "rechecked", "The affected devices were rechecked"
    NEXT_WORK = "next_work", "Rechecked at each device's next scheduled work"


class TestEquipment(TenantModel):
    """One item of the CE department's test equipment. Never deleted once it has a calibration or a use; its tag may be corrected
    until then (services). The tag is unique per facility in any letter case among devices and test equipment (tag_key here, the
    services' checks under equipment.services.lock_tags)."""
    __test__ = False  # pytest: not a test class

    tag = models.CharField(max_length=TAG_MAX, validators=[TAG_VALIDATOR], help_text="The CE control number on its sticker")
    tag_key = models.CharField(max_length=TAG_MAX, editable=False, help_text="tag.casefold(): services only")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    manufacturer = models.CharField(max_length=TEXT_MAX)
    model = models.CharField(max_length=TEXT_MAX)
    serial = models.CharField(max_length=SERIAL_MAX, help_text="As its certificates name it")
    program = models.CharField(max_length=20, choices=Program.choices, default=Program.CALIBRATION)
    interval_months = models.PositiveSmallIntegerField(default=INTERVAL_DEFAULT)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.IN_USE)
    status_reason = models.CharField(max_length=20, choices=Reason.choices, blank=True)
    status_since = models.DateField(editable=False, help_text="The facility day of the last status change: services only")
    held_by = models.ForeignKey("credentials.Technician", on_delete=models.SET_NULL, null=True, blank=True, related_name="test_equipment",
                                help_text="Whose kit it is")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    history = HistoricalRecords()

    class Meta:
        ordering = ["tag"]
        verbose_name = "test equipment"
        verbose_name_plural = "test equipment"
        constraints = [
            models.UniqueConstraint(fields=["tenant", "tag_key"], name="uniq_test_equipment_tag"),
            models.CheckConstraint(condition=_reason_rule(), name="test_equipment_status_reason"),
            models.CheckConstraint(condition=Q(interval_months__gte=INTERVAL_MIN, interval_months__lte=INTERVAL_MAX),
                                   name="test_equipment_interval"),
        ]
        indexes = [models.Index(fields=["tenant", "status"])]

    def __str__(self):
        return f"{self.tag} ({self.manufacturer} {self.model})"


class Calibration(TenantModel):
    """One certificate from an outside provider (a lab or the manufacturer) in the item's program, judged by its own dates whenever
    entered. due_on is null exactly when it failed. One live record per item and day; one entered in error stays, marked."""
    test_equipment = models.ForeignKey(TestEquipment, on_delete=models.PROTECT, related_name="calibrations")
    calibrated_on = models.DateField()
    due_on = models.DateField(null=True, blank=True)
    provider = models.CharField(max_length=TEXT_MAX, help_text="The lab or manufacturer that issued the certificate")
    certificate = models.CharField("certificate number", max_length=40, validators=[CERTIFICATE_VALIDATOR])
    accreditation = models.CharField(max_length=20, choices=Mark.choices)
    as_found = models.CharField(max_length=20, choices=AsFound.choices)
    adjusted = models.BooleanField(default=False)
    result = models.CharField(max_length=20, choices=Result.choices)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    entered_in_error = models.BooleanField(default=False, editable=False)
    entered_in_error_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
                                            editable=False)
    entered_in_error_on = models.DateField(null=True, blank=True, editable=False)
    history = HistoricalRecords()

    class Meta:
        ordering = ["-calibrated_on", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "test_equipment", "calibrated_on"], condition=Q(entered_in_error=False),
                                    name="uniq_live_calibration_per_day"),
            models.CheckConstraint(condition=Q(result=Result.FAILED.value, due_on__isnull=True) | Q(result=Result.PASSED.value, due_on__isnull=False),
                                   name="calibration_due_exactly_when_passed"),
        ]
        indexes = [models.Index(fields=["tenant", "test_equipment", "calibrated_on"])]

    def __str__(self):
        return f"{self.test_equipment.tag} · {self.certificate}"

    @property
    def needs_review(self) -> bool:
        """Found out of tolerance as found, or failed: the work done with it since it was last known good needs a decision."""
        return self.as_found == AsFound.OUT_OF_TOLERANCE or self.result == Result.FAILED


class ToleranceReview(TenantModel):
    """The decision on the work done with an item in a calibration's look-back window (rules.window): its basis, who decided and when,
    and the window and work orders it was drawn with, frozen (work_orders: [[id, number], ...] by id). It stops counting when its
    calibration is entered in error (its history keeps it)."""
    calibration = models.OneToOneField(Calibration, on_delete=models.PROTECT, related_name="review")
    basis = models.CharField(max_length=20, choices=Basis.choices)
    quality_reference = models.CharField(max_length=40, blank=True, validators=[QUALITY_REFERENCE_VALIDATOR],
                                         help_text="The facility's quality record, if any")
    decided_on = models.DateField()
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    window_start = models.DateField(null=True, blank=True, help_text="The previous passed calibration's day, exclusive (none: every earlier use)")
    window_end = models.DateField(help_text="The calibration's day, inclusive")
    work_orders = models.JSONField(default=list, help_text="[[id, number], ...] as decided")
    work_count = models.PositiveIntegerField(default=0)
    history = HistoricalRecords()

    class Meta:
        ordering = ["-decided_on", "-id"]

    def __str__(self):
        return f"{self.calibration} · review"


class TestEquipmentUse(TenantModel):
    """One work order recorded one item of test equipment. A work order with test equipment recorded is never deleted (PROTECT)."""
    __test__ = False  # pytest: not a test class

    work_order = models.ForeignKey("workorders.WorkOrder", on_delete=models.PROTECT, related_name="test_equipment_uses")
    test_equipment = models.ForeignKey(TestEquipment, on_delete=models.PROTECT, related_name="uses")
    history = HistoricalRecords()

    class Meta:
        ordering = ["work_order_id", "test_equipment__tag"]
        constraints = [models.UniqueConstraint(fields=["work_order", "test_equipment"], name="uniq_test_equipment_use")]
        indexes = [models.Index(fields=["tenant", "test_equipment"])]

    def __str__(self):
        return f"{self.test_equipment.tag} on work order {self.work_order_id}"
