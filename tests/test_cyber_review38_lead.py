"""
Slice 38 review round 2, the lead's follow-up: a work order's outcome entered in error keeps what was taken before (release_before,
before_taken) for its next recording, but the API's software-updates row reads none while nothing is recorded.
"""
from cyber_fixtures import release, update_wo

from apps.api.serializers_cybersecurity import update_data
from apps.cybersecurity.models import SoftwareUpdate


def test_a_cleared_outcome_shows_no_release_before(pump, pump_model):
    r52, r56 = release(pump_model, "5.2"), release(pump_model, "5.6")
    _wo, update = update_wo(pump, r56)
    SoftwareUpdate.objects.filter(pk=update.pk).update(release_before=r52, before_taken=True)  # cleared: kept for the next recording
    row = SoftwareUpdate.objects.select_related("asset", "work_order", "target_release", "release_before", "release_after").get(pk=update.pk)
    assert row.outcome == "" and update_data(row)["release_before"] is None
