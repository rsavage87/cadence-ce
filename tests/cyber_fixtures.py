"""
Slice 37 fixtures: the device security register's rows made directly (the scaffold's: the services are wave 1's), so rule and screen
tests can set up any state. Each is saved with history, as the services would.

- release(model, version="", **fields): a SoftwareRelease (version blank: the model's profile), version_key as services.version_key.
- facts(asset, release=None, **fields): the device's DeviceSecurity row (created or updated).
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
