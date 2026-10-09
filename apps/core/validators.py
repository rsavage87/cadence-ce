"""
A reference number typed into Cadence as a token (slices 28 to 30): an incident's event report number, a temporary device's agreement,
PO, or RMA number, a vendor's service report number. No spaces, so never a name or a sentence (CLAUDE.md non-negotiable 6: a free-text
box next to a patient-related record invites a patient's name). Each field gives its own words.
"""
from django.core.validators import RegexValidator

TOKEN_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._/#-]*$"


def reference_token(message: str) -> RegexValidator:
    return RegexValidator(TOKEN_PATTERN, message)
