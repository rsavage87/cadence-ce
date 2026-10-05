import uuid

from django.db import models
from simple_history.models import HistoricalRecords


class Tenant(models.Model):
    """One customer organization (a hospital or health system). Everything else hangs off this."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=60, unique=True, help_text="Used in the public request portal URL.")
    # Slice 21: the facility's IANA time zone (its today, its local times, and when its daily jobs run), chosen in Settings. Blank, as
    # every facility starts, is the server's (settings.TIME_ZONE, DJANGO_TIME_ZONE): what every facility used before it could choose.
    timezone = models.CharField(max_length=64, blank=True, default="",
                                help_text="The facility's IANA time zone; blank uses the server's (DJANGO_TIME_ZONE)")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()  # slice 21: a change of time zone (or name) is audited; the change log reads it as the facility's

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def zone_name(self) -> str:
        """The time zone the facility works in: its own, or the server's while it has chosen none (apps.tenants.context.zone_of
        also falls back to the server's for a name that is not a zone)."""
        from .context import zone_of

        return zone_of(self).key
