"""Mounted at /contracts/reviews/ inside the `web` namespace (slice 34): the Contracts screen's Reviews tab, the yearly review of each
contracted service vendor. Keep these names: the binder and the change log link to them. `web:vendor_review` takes the review's number
(VR-26-0001) and opens its page; the contracted services summary prints at `web:vendor_reviews_print`."""
from django.urls import path

from . import views_vendor_reviews as v

urlpatterns = [
    path("", v.vendor_reviews, name="vendor_reviews"),
    path("new/", v.vendor_review_new, name="vendor_review_new"),
    path("summary/print/", v.vendor_reviews_print, name="vendor_reviews_print"),
    path("<str:number>/", v.vendor_review, name="vendor_review"),
    path("<str:number>/print/", v.vendor_review_print, name="vendor_review_print"),
]
