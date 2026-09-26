from datetime import date

from django.conf import settings
from django.db import models
from simple_history.models import HistoricalRecords

from apps.core.models import Sequence, TenantModel


class WoType(models.TextChoices):
    PM = "pm", "Preventive maintenance"
    REPAIR = "repair", "Corrective repair"
    INSPECTION = "inspection", "Incoming inspection"
    RECALL = "recall", "Recall action"
    SAFETY = "safety", "Safety check"


class Priority(models.TextChoices):
    CRITICAL = "critical", "Critical"
    HIGH = "high", "High"
    NORMAL = "normal", "Normal"
    LOW = "low", "Low"


DUE_DAYS = {Priority.CRITICAL: 1, Priority.HIGH: 2, Priority.NORMAL: 5, Priority.LOW: 10}


class WoStatus(models.TextChoices):
    OPEN = "open", "Open"
    IN_PROGRESS = "in_progress", "In progress"
    AWAITING_PARTS = "awaiting_parts", "Awaiting parts"
    COMPLETED = "completed", "Completed"
    CLOSED = "closed", "Closed"
    CANCELLED = "cancelled", "Cancelled"


class Source(models.TextChoices):
    MANUAL = "manual", "Entered by staff"
    PORTAL = "portal", "Service request portal"
    PM_PLANNER = "pm_planner", "PM planner"
    RECALL = "recall", "Recall match"


OPEN_STATUSES = (WoStatus.OPEN, WoStatus.IN_PROGRESS, WoStatus.AWAITING_PARTS)

ALLOWED_TRANSITIONS = {
    WoStatus.OPEN: {WoStatus.IN_PROGRESS, WoStatus.AWAITING_PARTS, WoStatus.CANCELLED},
    WoStatus.IN_PROGRESS: {WoStatus.COMPLETED, WoStatus.AWAITING_PARTS, WoStatus.OPEN},
    WoStatus.AWAITING_PARTS: {WoStatus.IN_PROGRESS, WoStatus.CANCELLED},
    WoStatus.COMPLETED: {WoStatus.CLOSED, WoStatus.IN_PROGRESS},
    WoStatus.CLOSED: {WoStatus.IN_PROGRESS},
    WoStatus.CANCELLED: {WoStatus.OPEN},
}


class WorkOrder(TenantModel):
    number = models.CharField(max_length=20, editable=False)
    asset = models.ForeignKey("equipment.Asset", on_delete=models.PROTECT, related_name="work_orders")
    type = models.CharField(max_length=20, choices=WoType.choices, default=WoType.REPAIR)
    priority = models.CharField(max_length=10, choices=Priority.choices, default=Priority.NORMAL)
    status = models.CharField(max_length=20, choices=WoStatus.choices, default=WoStatus.OPEN)
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.MANUAL)
    requester = models.CharField(max_length=120, blank=True)
    callback = models.CharField(max_length=60, blank=True, help_text="Extension or phone for the requester")
    reported_location = models.CharField(max_length=120, blank=True)
    assigned_to = models.ForeignKey("credentials.Technician", on_delete=models.SET_NULL, null=True, blank=True, related_name="work_orders")
    vendor_service = models.BooleanField(default=False, help_text="Dispatched to the vendor rather than an in-house technician")
    vendor_name = models.CharField(max_length=120, blank=True)
    opened_on = models.DateField(default=date.today)
    due_on = models.DateField()
    started_on = models.DateField(null=True, blank=True)
    completed_on = models.DateField(null=True, blank=True)
    problem = models.TextField()
    resolution = models.TextField(blank=True)
    estimated_hours = models.DecimalField(max_digits=6, decimal_places=2, default=1)
    tagged_out = models.BooleanField(default=False, help_text="Requester removed the device from use")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    alert = models.ForeignKey("recalls.Alert", on_delete=models.SET_NULL, null=True, blank=True, related_name="work_orders",
                              help_text="The recall or hazard notice this work order responds to")
    history = HistoricalRecords()

    class Meta:
        ordering = ["-opened_on", "-created_at"]
        constraints = [models.UniqueConstraint(fields=["tenant", "number"], name="uniq_wo_number_per_tenant")]
        indexes = [models.Index(fields=["tenant", "status"]), models.Index(fields=["tenant", "due_on"]),
                   models.Index(fields=["tenant", "type", "completed_on"])]

    def __str__(self):
        return f"{self.number} · {self.get_type_display()}"

    def save(self, *args, **kwargs):
        if not self.number:
            year = self.opened_on.year
            self.number = f"WO-{year % 100:02d}-{Sequence.next(f'wo-{year}', self.tenant if self.tenant_id else None):04d}"
        super().save(*args, **kwargs)

    # --- derived -----------------------------------------------------------
    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    @property
    def is_late(self) -> bool:
        if self.completed_on:
            return self.completed_on > self.due_on
        return self.is_open and self.due_on < date.today()

    @property
    def turnaround_days(self) -> int | None:
        return (self.completed_on - self.opened_on).days if self.completed_on else None

    @property
    def downtime_days(self) -> int:
        """Days the device was unavailable. Repairs count their turnaround; PMs count nothing."""
        return (self.turnaround_days or 0) if self.type == WoType.REPAIR else 0

    def labor_cost(self) -> float:
        return sum(float(line.hours) * float(line.rate) for line in self.labor_lines.all())

    def parts_cost(self) -> float:
        return sum(float(p.quantity) * float(p.unit_cost) for p in self.part_lines.all())

    def total_cost(self) -> float:
        return self.labor_cost() + self.parts_cost()


class WorkOrderStatusHistory(TenantModel):
    work_order = models.ForeignKey(WorkOrder, on_delete=models.CASCADE, related_name="status_history")
    from_status = models.CharField(max_length=20, choices=WoStatus.choices, blank=True)
    to_status = models.CharField(max_length=20, choices=WoStatus.choices)
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    note = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["created_at"]


class LaborLine(TenantModel):
    work_order = models.ForeignKey(WorkOrder, on_delete=models.CASCADE, related_name="labor_lines")
    technician = models.ForeignKey("credentials.Technician", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    worked_on = models.DateField(default=date.today)
    hours = models.DecimalField(max_digits=6, decimal_places=2)
    rate = models.DecimalField(max_digits=8, decimal_places=2, help_text="Hourly rate applied (in-house or vendor)")
    description = models.CharField(max_length=200, blank=True)


class PartLine(TenantModel):
    work_order = models.ForeignKey(WorkOrder, on_delete=models.CASCADE, related_name="part_lines")
    description = models.CharField(max_length=200)
    part_number = models.CharField(max_length=80, blank=True)
    quantity = models.DecimalField(max_digits=8, decimal_places=2, default=1)
    unit_cost = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    po_number = models.CharField(max_length=40, blank=True)


class WorkOrderNote(TenantModel):
    work_order = models.ForeignKey(WorkOrder, on_delete=models.CASCADE, related_name="notes")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    author_name = models.CharField(max_length=120, blank=True)
    text = models.TextField()

    class Meta:
        ordering = ["created_at"]


class Urgency(models.TextChoices):
    CRITICAL = "critical", "Device unusable and patient care is affected"
    HIGH = "high", "Device unusable, a backup is in use"
    NORMAL = "normal", "Working with a problem, or routine"


class ServiceRequest(TenantModel):
    """A request from the public portal. Always paired with the work order it created."""

    number = models.CharField(max_length=20, editable=False)
    asset = models.ForeignKey("equipment.Asset", on_delete=models.PROTECT, related_name="service_requests")
    department = models.ForeignKey("equipment.Department", on_delete=models.PROTECT, related_name="+")
    room = models.CharField(max_length=40, blank=True)
    requester_name = models.CharField(max_length=120, blank=True)
    callback = models.CharField(max_length=60, blank=True)
    problem = models.TextField()
    urgency = models.CharField(max_length=10, choices=Urgency.choices, default=Urgency.NORMAL)
    tagged_out = models.BooleanField(default=False)
    work_order = models.OneToOneField(WorkOrder, on_delete=models.CASCADE, related_name="service_request")
    submitted_ip = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if not self.number:
            self.number = f"SR-{Sequence.next('sr', self.tenant if self.tenant_id else None):05d}"
        super().save(*args, **kwargs)
