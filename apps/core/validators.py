"""
A reference number typed into Cadence as a token (slices 28 to 30): an incident's event report number, a temporary device's agreement,
PO, or RMA number, a vendor's service report number. No spaces, so never a name or a sentence (CLAUDE.md non-negotiable 6: a free-text
box next to a patient-related record invites a patient's name). Each field gives its own words.

Slice 37: a product's own text (a software version as the manufacturer writes it, "Rel. M.00.03"; an operating system with its edition,
"Windows 10 IoT Enterprise LTSC 2021"): letters, digits, spaces, and . _ / ( ) + - only, never @ or : (no address, no account, no URL),
starting with a letter or digit. Close to a name, so it lives only on a model-level record that never touches a patient.
"""
from django.core.validators import RegexValidator

TOKEN_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._/#-]*$"


def reference_token(message: str) -> RegexValidator:
    return RegexValidator(TOKEN_PATTERN, message)


PRODUCT_TEXT_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9 ._/()+-]*$"


def product_text(message: str) -> RegexValidator:
    return RegexValidator(PRODUCT_TEXT_PATTERN, message)
