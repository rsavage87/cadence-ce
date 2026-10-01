"""Mounted at /pm/ inside the `web` namespace (slice 14): the PM program. A device model's drawer (?tab=program|procedure|aem)
opens from the PM library and the device drawer; the same URL renders the PM schedule with the drawer open when opened directly.
Model ids are UUIDs, so "new" can never be one."""
from django.urls import path

from . import views_aem, views_models, views_pm_week, views_procedures

urlpatterns = [
    # part A: the catalog (views_models)
    path("models/new/", views_models.model_new, name="pm_model_new"),
    path("models/<uuid:pk>/", views_models.model_detail, name="pm_model"),
    path("models/<uuid:pk>/edit/", views_models.model_edit, name="pm_model_edit"),
    path("models/<uuid:pk>/risk/", views_models.model_risk, name="pm_model_risk"),
    # part D: procedures (views_procedures)
    path("models/<uuid:pk>/procedure/", views_procedures.model_procedure, name="pm_model_procedure"),
    path("procedures/new/", views_procedures.procedure_new, name="pm_procedure_new"),
    path("procedures/<uuid:pk>/edit/", views_procedures.procedure_edit, name="pm_procedure_edit"),
    # part B: AEM (views_aem)
    path("models/<uuid:pk>/aem/propose/", views_aem.aem_propose, name="pm_aem_propose"),
    path("aem/<uuid:pk>/decide/", views_aem.aem_decide, name="pm_aem_decide"),
    path("aem/<uuid:pk>/withdraw/", views_aem.aem_withdraw, name="pm_aem_withdraw"),
    path("aem/<uuid:pk>/end/", views_aem.aem_end, name="pm_aem_end"),
    # part C: Auto-assign week (views_pm_week)
    path("week/assign/", views_pm_week.week_assign, name="pm_week_assign"),
]
