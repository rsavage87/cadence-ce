"""
Base classes for tenant-scoped models.

Rules (also in CLAUDE.md):
- Every business table inherits TenantModel.
- Always query through `Model.objects` (scoped). `Model.unscoped` is for tenant bootstrap,
  cross-tenant jobs, and tests only, and each use should say why in a comment.
- Never build a class-level `queryset = Model.objects.all()`: it is evaluated at import time
  when no tenant is in context. Call `Model.objects.all()` inside the request instead.
"""
import uuid

from django.db import models

from apps.tenants.context import get_current_tenant


class TenantManager(models.Manager):
    def get_queryset(self):
        qs = super().get_queryset()
        tenant = get_current_tenant()
        if tenant is None:
            return qs.none()
        return qs.filter(tenant_id=tenant.id)


class TenantModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, editable=False, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantManager()
    unscoped = models.Manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if self.tenant_id is None:
            tenant = get_current_tenant()
            if tenant is None:
                raise RuntimeError(f"{type(self).__name__}.save() called with no tenant in context; use tenant_context() or pass tenant=")
            self.tenant = tenant
        super().save(*args, **kwargs)


class Sequence(TenantModel):
    """Per-tenant counters for human-readable numbers (work orders, requests)."""

    key = models.CharField(max_length=40)
    value = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["tenant", "key"], name="uniq_sequence_per_tenant")]

    @classmethod
    def next(cls, key, tenant=None):
        from django.db import transaction

        tenant = tenant or get_current_tenant()
        if tenant is None:
            raise RuntimeError("Sequence.next() needs a tenant")
        with transaction.atomic():
            # unscoped + explicit tenant: this runs inside jobs that may not have a request context
            row, _ = cls.unscoped.select_for_update().get_or_create(tenant=tenant, key=key)
            row.value += 1
            row.save(update_fields=["value", "updated_at"])
            return row.value
