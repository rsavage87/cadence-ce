"""
All facilities (slice 22): the Overview's figures for every facility the signed-in person has joined, side by side, with totals.
Reached from the facility menu ("All facilities") while working in any one of them. Each facility's figures are read inside that
facility (its own today, time zone, and rows) with the person's account there, which must give that facility's Overview.

Scaffold stub: part B builds the page (apps.reports.all_facilities).
"""
from django.shortcuts import render

from .decorators import web_view


@web_view(scoped=True)  # shows this facility only through the person's account here, as every other one: see the module docstring
def all_facilities(request):
    return render(request, "web/all_facilities.html", {"nav_active": "overview", "all_facilities_page": True, "rows": [], "totals": None})
