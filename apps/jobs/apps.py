from django.apps import AppConfig


class JobsConfig(AppConfig):
    """The daily jobs (PM work-order generation, the openFDA import) and the scheduler that runs them."""

    name = "apps.jobs"
    verbose_name = "Scheduled jobs"
