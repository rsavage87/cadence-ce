"""
Match alerts to a tenant's catalog. Matching is deliberately simple (manufacturer + model fragment);
a false positive costs a reviewer a minute, a false negative can cost a patient.
"""
from apps.equipment.models import DeviceModel

from .models import Alert, AlertMatch


def _norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


def match_alert(alert: Alert) -> int:
    """Create AlertMatch rows for every device model in the current tenant the alert could apply to."""
    created = 0
    mfr = _norm(alert.manufacturer)
    terms = [_norm(t) for t in alert.model_terms if t] or [_norm(alert.product)]
    for dm in DeviceModel.objects.all():
        if _norm(dm.manufacturer) not in mfr and mfr not in _norm(dm.manufacturer):
            continue
        model_norm = _norm(dm.model)
        if any(t and (t in model_norm or model_norm in t) for t in terms):
            _, was_created = AlertMatch.objects.get_or_create(alert=alert, device_model=dm)
            created += int(was_created)
    return created


def match_all_open_alerts() -> int:
    return sum(match_alert(a) for a in Alert.objects.all())
