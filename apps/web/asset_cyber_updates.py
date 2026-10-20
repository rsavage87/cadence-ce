"""
The device drawer's Security tab, its "Software updates" section (slice 38; web/_asset_cyber_updates.html): the device's update work
orders' records and its installs recorded by hand (rules.updates_of, one query), newest first, each in rules' words (rules.update_state,
rules.update_words); the work order's number always, a link only with Work orders View; Open software update
(permissions.can_open_updates, a device on site and not held as evidence), Record update (can_edit_devices), Confirm version on one
completed with nothing recorded (can_edit_devices), Entered in error (can_correct_updates). No priority chip ("Critical" is a never
word). A fixed number of queries however many rows. Wave 2 builds it.
"""


def context(request, asset, today) -> dict:
    """The section's context, under cyber["updates"] (the caller offers the tab only to can_view_devices, never scoped)."""
    return {}
