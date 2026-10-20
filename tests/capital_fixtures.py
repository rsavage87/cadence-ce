"""
Slice 36, the replacement priority (apps.capital.priority): helpers for its inputs. Import from any test file:
`from capital_fixtures import TODAY, device, work, recall_match`.

- TODAY: the day the tests score on (a Monday in October 2026; passed to score_assets, never read from the clock).
- device(model, dept, tag, *, years=None, **fields): a device of ours installed `years` before TODAY (None: no install date).
- work(asset, type, opened, *, completed=None, cancelled=False, labor=(), parts=(), finding=None, check=None, vendor=False,
  vendor_name=None, imported=False, alert=None): a work order opened on `opened` and, when given, completed (or cancelled); labor
  (hours, rate) and part (quantity, unit cost) lines; on vendor service, the vendor's name ("Vendor field service" unless given); the
  stored finding, checks after repair, and source set as completion and the import would (they are not editable by hand).
- recall_match(model, *, classification="Class II", status=IN_PROGRESS): an alert matched to the model, with its status.
"""
from datetime import date, timedelta
from decimal import Decimal

from apps.equipment.models import Asset
from apps.recalls.models import Alert, AlertMatch
from apps.workorders.models import LaborLine, PartLine, Source, WorkOrder
from apps.workorders.services import change_status, create_work_order

TODAY = date(2026, 10, 19)


def years_before(years: float, today: date = TODAY) -> date:
    return today - timedelta(days=round(years * 365.25))


def device(model, dept, tag, *, years=None, **fields) -> Asset:
    values = {"acquisition_cost": Decimal("1000"), "installed_on": years_before(years) if years is not None else None,
              "next_pm_on": TODAY + timedelta(days=30), **fields}
    return Asset.objects.create(tag=tag, device_model=model, department=dept, **values)


def work(asset, type, opened, *, completed=None, cancelled=False, labor=(), parts=(), finding=None, check=None, vendor=False,
         vendor_name=None, imported=False, alert=None) -> WorkOrder:
    wo = create_work_order(asset=asset, type=type, priority="normal", problem="Test", opened_on=opened, vendor_service=vendor,
                           vendor_name=(vendor_name or "Vendor field service") if vendor else "", alert=alert)
    for hours, rate in labor:
        LaborLine.objects.create(work_order=wo, hours=Decimal(str(hours)), rate=Decimal(str(rate)))
    for quantity, unit_cost in parts:
        PartLine.objects.create(work_order=wo, description="Part", quantity=Decimal(str(quantity)), unit_cost=Decimal(str(unit_cost)))
    if cancelled:
        change_status(wo, "cancelled", as_of=opened)
    elif completed:
        change_status(wo, "in_progress", as_of=opened)
        change_status(wo, "completed", as_of=completed)
    fields = {}
    if finding is not None:
        fields["repair_finding"] = finding
    if check is not None:
        fields["repair_check"] = check
    if imported:
        fields["source"] = Source.IMPORTED
    if fields:
        WorkOrder.objects.filter(pk=wo.pk).update(**fields)
        wo.refresh_from_db()
    return wo


def recall_match(model, *, classification="Class II", status=AlertMatch.Status.IN_PROGRESS, external_id=None) -> AlertMatch:
    n = Alert.objects.count() + 1
    alert = Alert.objects.create(source=Alert.Source.FDA, external_id=external_id or f"Z-36-{n}", classification=classification,
                                 manufacturer=model.manufacturer, product=model.model, title=f"Recall {n}", published_on=TODAY - timedelta(days=30))
    return AlertMatch.objects.create(alert=alert, device_model=model, status=status)
