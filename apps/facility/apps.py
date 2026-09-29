from django.apps import AppConfig


class FacilityConfig(AppConfig):
    """Facility settings (the Settings screen): portal options, maintenance policy, KPI targets. Named `facility`, not
    `settings`, so it never reads like django.conf.settings; the permission module is still Module.SETTINGS."""

    name = "apps.facility"
    verbose_name = "Settings"
