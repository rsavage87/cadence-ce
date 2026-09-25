import uuid

from django.db import models


class Tenant(models.Model):
    """One customer organization (a hospital or health system). Everything else hangs off this."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=60, unique=True, help_text="Used in the public request portal URL.")
    timezone = models.CharField(max_length=64, default="America/New_York")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name
