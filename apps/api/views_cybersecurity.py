"""
/api/v1/ for the device security register (slice 37), read only (TenantAPIMixin, Module.CYBERSECURITY, no scoped actions; writes answer a
405 in words naming the Device security screen):

GET /api/v1/software-releases/     Each model's profile and releases with their groups, standing words, and device count (Device security View).
GET /api/v1/device-security/       Each device on site with its facts and standing words, by id or tag (Device security View and
                                   Equipment View).

(Scaffold: wave 2 registers the viewsets.) Never nested into the asset or model serializers.
"""


def register(router) -> None:
    """Scaffold: wave 2 adds the two read-only viewsets."""
