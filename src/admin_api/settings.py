"""Administration API settings, read from the environment on every call."""

from __future__ import annotations

import os

from customer_api.settings import canonical_origin


def allowed_origins() -> frozenset[str]:
    """SORTVIEW_ADMIN_ALLOWED_ORIGINS, comma-separated: the browser origin(s) of the admin app. Empty by default, so
    every state-changing admin route refuses until a deployment names its admin origin. Normalised exactly as the
    customer list is (customer_api.settings.canonical_origin); an entry that is not a well-formed origin is dropped.

    Separate from SORTVIEW_CUSTOMER_ALLOWED_ORIGINS on purpose: a page served from the customer app's origin can never
    send an admin write. The admin origin still has to be in the customer list too, because the admin app signs in
    and out through /api/auth/login and /api/auth/logout."""
    raw = os.getenv("SORTVIEW_ADMIN_ALLOWED_ORIGINS", "")
    return frozenset(
        origin for origin in (canonical_origin(entry.strip()) for entry in raw.split(",")) if origin is not None
    )
