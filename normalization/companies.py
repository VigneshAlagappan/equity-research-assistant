"""Company identifier normalization.

Ticker symbols change; company_id (README: Company Master) doesn't. This
module only normalizes raw identifier text into the canonical company_id
form — CompanyRegistry (companies/registry.py) owns creating and looking up
the actual company record.
"""

from __future__ import annotations

import re

_VALID_COMPANY_ID_RE = re.compile(r"^[A-Z0-9&.\-]+$")


class InvalidCompanyIdError(ValueError):
    """Raised when a raw identifier can't be normalized into a valid company_id."""


def normalize_company_id(raw: str) -> str:
    """Normalize a raw identifier (folder name, symbol, user input) into a company_id.

    company_id is uppercase alphanumeric, plus "&", ".", and "-" — covers
    NSE/BSE symbol conventions (e.g. "HDFCBANK") as well as US tickers that
    legitimately contain a dot or hyphen (e.g. "BRK.B", "BF.B"). Whitespace
    is stripped; anything else invalid raises.
    """
    candidate = raw.strip().upper()
    if not candidate or not _VALID_COMPANY_ID_RE.match(candidate):
        raise InvalidCompanyIdError(f"Not a valid company_id: {raw!r}")
    return candidate
