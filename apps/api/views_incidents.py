"""
Device incidents (slice 28; wave 2 builds the endpoints): /api/v1/incidents/ over apps.incidents.services, with the screen's doors
(apps.incidents.permissions). Module.INCIDENTS; no scoped_actions: a vendor's or a requester's account is refused every action.
"""


def register(router) -> None:
    """Wave 2 registers the incidents viewset here."""
