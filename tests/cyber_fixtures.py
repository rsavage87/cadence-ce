"""
Slice 37 fixtures: the device security register's rows made directly (the scaffold's: the services are wave 1's), so rule and screen
tests can set up any state. Each is saved with history, as the services would.

- release(model, version="", **fields): a SoftwareRelease (version blank: the model's profile), version_key as services.version_key.
- facts(asset, release=None, **fields): the device's DeviceSecurity row (created or updated).
- Slice 40: make_advisory(...) and model_row(...): an advisory, its first source, and the models and releases it names, made directly
  (test-only; the writers are apps.cybersecurity.advisories), so rule and door tests never wait on the writers.
"""
from apps.cybersecurity.models import DeviceSecurity, SoftwareRelease
from apps.cybersecurity.services import version_key


def release(model, version: str = "", **fields) -> SoftwareRelease:
    return SoftwareRelease.objects.create(device_model=model, version=version, version_key=version_key(version), **fields)


def facts(asset, release=None, **fields) -> DeviceSecurity:
    sec, _ = DeviceSecurity.objects.get_or_create(asset=asset)
    sec.release = release
    for name, value in fields.items():
        setattr(sec, name, value)
    sec.save()
    return sec


def update_wo(asset, target_release=None, *, reference: str = "", by=None, **wo_fields):
    """Slice 38: an update work order as updates.open_update makes one (made directly: the scaffold's; the service is wave 1's): the work
    order through create_work_order with the opener's keyword, the fixed problem, and its record holding the target (a release, or the
    patches with `reference`). Returns (work order, its SoftwareUpdate)."""
    from django.utils import timezone

    from apps.cybersecurity.models import SoftwareUpdate, UpdateTarget
    from apps.cybersecurity.updates import PATCHES_PROBLEM, UPDATE_PROBLEM
    from apps.workorders.models import Priority, WoType
    from apps.workorders.services import create_work_order

    wo = create_work_order(asset=asset, type=WoType.SOFTWARE_UPDATE, priority=wo_fields.pop("priority", Priority.NORMAL),
                           problem=UPDATE_PROBLEM if target_release is not None else PATCHES_PROBLEM, requester="Clinical engineering",
                           created_by=by, _register_opener=True, **wo_fields)
    update = SoftwareUpdate.objects.create(asset=asset, work_order=wo, target=UpdateTarget.RELEASE if target_release else UpdateTarget.PATCHES,
                                           target_release=target_release, target_reference=reference, opened_on=timezone.localdate(),
                                           opened_by=by)
    return wo, update


def make_advisory(*, number: str = "", received_on=None, cves=(), source: str = "manufacturer_bulletin", reference: str = "DEMO-ADV-0001",
                  published_on=None, by=None, **fields):
    """Slice 40: a SecurityAdvisory (status open unless `fields` say otherwise) with one source, made directly (test-only). The number
    from the advisory sequence of received_on's year unless given."""
    from django.utils import timezone

    from apps.core.models import Sequence
    from apps.cybersecurity.models import AdvisorySource, SecurityAdvisory

    received = received_on or timezone.localdate()
    if not number:
        yy = received.year % 100
        number = f"AD-{yy:02d}-{Sequence.next(f'advisory-{yy:02d}'):04d}"
    advisory = SecurityAdvisory.objects.create(number=number, received_on=received, cves=list(cves), recorded_by=by, **fields)
    AdvisorySource.objects.create(advisory=advisory, source=source, reference=reference, reference_key=reference.casefold(),
                                  published_on=published_on)
    return advisory


def model_row(advisory, device_model, *, versions: str = "listed", releases=(), remedy: str = "release", fix=None,
              patches_reference: str = "", confirm: bool = True):
    """Slice 40: an AdvisoryModel with its AdvisoryRelease rows (versions listed), made directly (test-only). `confirm` marks the
    advisory's models confirmed today (as Set models' checkbox does)."""
    from django.utils import timezone

    from apps.cybersecurity.models import AdvisoryModel, AdvisoryRelease, SoftwareRelease

    seen = [str(pk) for pk in SoftwareRelease.objects.filter(device_model=device_model, removed_on__isnull=True)
            .exclude(version_key="").values_list("pk", flat=True)]
    row = AdvisoryModel.objects.create(advisory=advisory, device_model=device_model, versions=versions, remedy=remedy, fix_release=fix,
                                       patches_reference=patches_reference, seen_release_ids=seen)
    for r in releases:
        AdvisoryRelease.objects.create(advisory=advisory, advisory_model=row, release=r)
    if confirm and advisory.models_confirmed_on is None:
        advisory.models_confirmed_on = timezone.localdate()
        advisory.save(update_fields=["models_confirmed_on"])
    return row
