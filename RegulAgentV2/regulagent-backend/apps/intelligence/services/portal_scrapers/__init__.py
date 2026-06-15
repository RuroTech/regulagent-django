"""
Portal scraper registry.

Maps agency codes to their concrete scraper classes and provides a
factory function for retrieving scraper instances.

To register a new scraper:
    1. Create a module under this package (e.g. ``rrc.py``).
    2. Implement a class that inherits from ``BasePortalScraper``.
    3. Add it to ``SCRAPER_REGISTRY`` below.
"""

from apps.intelligence.services.portal_scrapers.base import BasePortalScraper
from apps.intelligence.services.portal_scrapers.exceptions import (
    CredentialLockedError,
    InvalidCredentialsError,
)
from apps.intelligence.services.portal_scrapers.rrc import RRCPortalScraper

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
# Maps uppercase agency codes to their concrete scraper *classes* (not
# instances).  get_scraper() instantiates on demand so that scrapers remain
# stateless between calls.
# ---------------------------------------------------------------------------
SCRAPER_REGISTRY: dict[str, type[BasePortalScraper]] = {
    "RRC": RRCPortalScraper,
}


def get_scraper(agency: str) -> BasePortalScraper:
    """
    Return an instantiated scraper for the given agency code.

    Parameters
    ----------
    agency:
        Uppercase agency code, e.g. ``"RRC"`` or ``"NMOCD"``.

    Returns
    -------
    BasePortalScraper
        A fresh instance of the concrete scraper registered for
        ``agency``.

    Raises
    ------
    KeyError
        If no scraper is registered for the requested agency code.
    """
    agency = agency.upper()
    try:
        scraper_cls = SCRAPER_REGISTRY[agency]
    except KeyError:
        available = ", ".join(sorted(SCRAPER_REGISTRY)) or "<none registered>"
        raise KeyError(
            f"No portal scraper registered for agency '{agency}'. "
            f"Available: {available}"
        ) from None
    return scraper_cls()


__all__ = [
    "BasePortalScraper",
    "CredentialLockedError",
    "InvalidCredentialsError",
    "RRCPortalScraper",
    "SCRAPER_REGISTRY",
    "get_scraper",
]
