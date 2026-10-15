"""Mounted at /contracts/reviews/ inside the `web` namespace (slice 34): the Contracts screen's Reviews tab, the yearly review of each
contracted service vendor. Keep these names: the binder and the change log link to them. `web:vendor_review` takes the review's number
(VR-26-0001) and opens its page; the contracted services summary prints at `web:vendor_reviews_print`. The tab takes ?show=not_reviewed
(the vendors with no signed-off review) and ?vendor=<name> (one vendor, by its key). Every action names the review by its number."""
from django.urls import path

from . import views_vendor_reviews as v

urlpatterns = [
    path("", v.vendor_reviews, name="vendor_reviews"),
    path("new/", v.vendor_review_new, name="vendor_review_new"),
    path("summary/print/", v.vendor_reviews_print, name="vendor_reviews_print"),
    path("committee/", v.vendor_reviews_report, name="vendor_reviews_report"),  # Record a report to committee, for several reviews
    path("<str:number>/", v.vendor_review, name="vendor_review"),
    path("<str:number>/print/", v.vendor_review_print, name="vendor_review_print"),
    path("<str:number>/refresh/", v.vendor_review_refresh, name="vendor_review_refresh"),
    path("<str:number>/fields/<str:key>/", v.vendor_review_field, name="vendor_review_field"),
    path("<str:number>/sign-off/", v.vendor_review_sign_off, name="vendor_review_sign_off"),
    path("<str:number>/withdraw/", v.vendor_review_withdraw, name="vendor_review_withdraw"),
    path("<str:number>/discard/", v.vendor_review_discard, name="vendor_review_discard"),
    path("<str:number>/plan/", v.vendor_review_plan, name="vendor_review_plan"),
    path("<str:number>/reports/new/", v.vendor_review_report_new, name="vendor_review_report_new"),
    path("<str:number>/reports/<uuid:pk>/", v.vendor_review_report, name="vendor_review_report"),
    path("<str:number>/reports/<uuid:pk>/remove/", v.vendor_review_report_remove, name="vendor_review_report_remove"),
]
