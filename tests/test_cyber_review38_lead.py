"""
Slice 38 review round 4, the lead's follow-ups to the final skeptics (skeptics38d.json): an install with no release is refused with the
other problems at Confirm version and Record update (never only after them); the words that answer a correction point to Record facts
first (patches keep the version as recorded, a reinstall the same day); Confirm version's "already" takes the target as it is now.
"""
import test_cyber_review38_services as base
from test_cyber_review38_services import ALREADY, INSTALLED, choice, refused

from apps.cybersecurity import updates
from apps.cybersecurity.services import remove_release
from apps.cybersecurity.updates import confirm, record

# The services review's fixtures, as this module's (assigned, never imported by name: a test's argument of the same name is the fixture)
today, tech, vendor, r56, vendor_job = base.today, base.tech, base.vendor, base.r56, base.vendor_job


def test_an_install_with_no_release_is_refused_with_the_other_problems(pump, vendor_job, tech, today):
    _wo, row = vendor_job
    assert refused(confirm, row, choice(INSTALLED), by=tech) == {"software_release": updates.CHOOSE_RUNS_NOW,
                                                                  "software_basis": updates.BASIS_WORDS}
    assert refused(record, pump, choice(INSTALLED), installed_by="nobody", applied_on=today, by=tech) == {
        "software_release": updates.CHOOSE_RUNS_NOW, "software_basis": updates.BASIS_WORDS, "installed_by": updates.INSTALLER_WORDS}


def test_the_correction_words_point_to_record_facts_first():
    assert "correct it with Record facts first" in updates.PATCHES_NO_RELEASE
    assert "Record facts" in updates.DUPLICATE and updates.DUPLICATE.format(words="x").startswith("This install is already recorded")
    assert updates.CHOOSE_RUNS_NOW == "Choose the release it runs after the update."


def test_already_takes_the_target_as_it_is_now(pump, r56, vendor_job, tech, today):
    _wo, row = vendor_job
    remove_release(r56, by=None)  # entered in error once its work order was completed (no open update names it now)
    assert refused(confirm, row, choice(ALREADY), by=tech) == {"software_release": updates.CHOOSE_RUNS_NOW}
    row.refresh_from_db()
    assert row.outcome == ""  # nothing recorded
