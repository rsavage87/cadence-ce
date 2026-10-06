from django.apps import AppConfig


class ImportsConfig(AppConfig):
    """Onboarding imports (slice 23): a facility's devices, work order history, contracts, and technicians from CSV files of the
    CMMS it is leaving, checked row by row before anything is saved."""

    name = "apps.imports"
    verbose_name = "Imports"
