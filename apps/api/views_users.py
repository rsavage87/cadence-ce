"""
Users and access (slice 19, part D): technicians and their credentials, and (part D) users and roles.
"""



from apps.accounts.models import Level
from apps.credentials.models import Credential, Technician

from . import serializers as s
from .base import TenantViewSet


class TechnicianViewSet(TenantViewSet):
    model, module, serializer_class = Technician, "users", s.TechnicianSerializer

    def get_queryset(self):
        return Technician.objects.prefetch_related("credentials")


class CredentialViewSet(TenantViewSet):
    # Removing a credential is part of routine credential upkeep, gated like adding one (the web tab does the same).
    model, module, serializer_class, delete_level = Credential, "users", s.CredentialSerializer, Level.EDIT


def register(router):
    router.register("technicians", TechnicianViewSet, basename="technician")
    router.register("credentials", CredentialViewSet, basename="credential")
