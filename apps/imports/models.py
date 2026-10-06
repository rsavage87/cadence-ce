"""
An import of one CSV file (slice 23). The run is the record of who imported what and when, and holds the file while it is being
checked and imported: its header and rows (every column until the person confirms which columns to use, then only those), cleared
once the import is done, discarded, or left unfinished for EXPIRE_DAYS. Never history (HistoricalRecords) or Admin: the rows are
the facility's data in transit, not a record to keep. See apps.imports.services for the steps.
"""
from django.conf import settings
from django.db import models

from apps.core.models import TenantModel


class ImportRun(TenantModel):
    class Status(models.TextChoices):
        MAPPING = "mapping", "Choosing columns"
        CHECKING = "checking", "Checking"
        CHECKED = "checked", "Checked"
        IMPORTING = "importing", "Importing"
        IMPORTED = "imported", "Imported"
        DISCARDED = "discarded", "Discarded"
        EXPIRED = "expired", "Expired"

    kind = models.CharField(max_length=30, help_text="Which importer reads the file (apps.imports.kinds)")
    file_name = models.CharField(max_length=200)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.MAPPING)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    imported_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    # Review fix: who the current pass acts as (who started the check, or the import), whoever's browser asks for its next chunk, so
    # a row's per-row levels (retiring a device, the CMS mark) are always that person's. None only for a run of the command line.
    pass_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    checked_at = models.DateTimeField(null=True, blank=True)
    imported_at = models.DateTimeField(null=True, blank=True)
    header = models.JSONField(default=list, blank=True, help_text="The file's column names, as written")
    columns = models.JSONField(default=list, blank=True, help_text="Once confirmed: the importer's column keys, in the order rows hold them")
    rows = models.JSONField(default=list, blank=True, help_text="The file's rows (lists of cells); cleared when the run ends")
    lines = models.JSONField(default=list, blank=True, help_text="The line of the file each row starts on (blank lines are left out)")
    row_count = models.PositiveIntegerField(default=0)
    offset = models.PositiveIntegerField(default=0, help_text="Rows done by the current pass (the check, or the import)")
    file_problems = models.JSONField(default=dict, blank=True, help_text="Row index -> why the whole row is refused (a repeat in the file)")
    results = models.JSONField(default=list, blank=True, help_text="[line, key, outcome, [notes]] for every row with a note or skipped")
    counts = models.JSONField(default=dict, blank=True, help_text="Outcome -> rows, for the current pass")
    summary = models.JSONField(default=dict, blank=True, help_text="The importer's totals, new names, and value mappings")

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # One import at a time per facility: two would race for the same tags, numbers, and names.
            models.UniqueConstraint(fields=["tenant"], condition=models.Q(status="importing"), name="one_import_running_per_facility"),
        ]

    def __str__(self):
        return f"{self.kind}: {self.file_name} ({self.get_status_display()})"

    @property
    def done(self) -> bool:
        return self.status in (self.Status.IMPORTED, self.Status.DISCARDED, self.Status.EXPIRED)
