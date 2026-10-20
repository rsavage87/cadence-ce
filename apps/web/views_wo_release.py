"""
What a software update did, in the completion modal (slice 38; web/_wo_release_picker.html, through views_wo_complete): offered only
where apps.cybersecurity.rules.release_offer holds (a register-opened update work order, the device on site, a user who may record:
Device security Edit and Equipment View, never scoped); its outcome radios (Installed, Patches applied, Already on the update, Not
confirmed now: none preselected while a choice is needed; on a re-completion the recorded outcome, "Keep as recorded"), the release the
device runs now (the model's releases, the recorded one marked), the reference, the basis, the target above them. The hidden
`shown_software_outcome` marker says the section was on screen: without it nothing is sent (KEEP). The work order drawer's and print's
"Software update" line (target and state in rules' words) only for permissions.can_view_devices, in fixed queries. Wave 2 builds it.
"""


def offer(wo, user, today):
    """The modal's section, or None where nothing is offered (rules.release_offer)."""
    return None


def picked(offer_ctx, data) -> dict:
    """The section as the form sent it back (a refusal re-renders it with the user's choices)."""
    return {}


def drawer_line(wo, user, today):
    """The work order drawer's and print's "Software update" line, or None (not an update, or a reader without can_view_devices)."""
    return None
