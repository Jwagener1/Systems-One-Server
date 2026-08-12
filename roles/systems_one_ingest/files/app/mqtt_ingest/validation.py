from __future__ import annotations

import logging
import os
import re

# A real serial looks like "018389-01-8" — not a machine name like "DIM5".
# Must contain at least one digit and a non-alpha character (dash), min length 5.
SERIAL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-]{4,}$")
MACHINE_NAME_ONLY_RE = re.compile(r"^(DIM|STATIC)\d+$", re.IGNORECASE)


def _parse_allowlist(value: str) -> set[str]:
    """Comma-separated allowlist -> uppercase set. Empty/unset means no restriction."""
    return {v.strip().upper() for v in value.split(",") if v.strip()}


def validate_device(
    serial_number: str,
    customer: str,
    location: str,
    machine_name: str,
    logger: logging.Logger,
) -> bool:
    """Return True if the device fields look legitimate, False to reject.

    Customer/location are checked against optional allowlists
    (INGEST_ALLOWED_CUSTOMERS / INGEST_ALLOWED_LOCATIONS, comma-separated).
    An unset or empty allowlist means "allow all" — new customers/locations
    must be accepted by default, not silently dropped.
    """

    if MACHINE_NAME_ONLY_RE.match(serial_number):
        logger.warning(
            "Rejecting device: serial_number looks like a machine name (serial=%s customer=%s location=%s machine=%s)",
            serial_number, customer, location, machine_name,
        )
        return False

    if not SERIAL_RE.match(serial_number):
        logger.warning(
            "Rejecting device: serial_number failed format check (serial=%s customer=%s location=%s machine=%s)",
            serial_number, customer, location, machine_name,
        )
        return False

    allowed_customers = _parse_allowlist(os.environ.get("INGEST_ALLOWED_CUSTOMERS", ""))
    if allowed_customers and customer.strip().upper() not in allowed_customers:
        logger.warning(
            "Rejecting device: customer not in INGEST_ALLOWED_CUSTOMERS (serial=%s customer=%s location=%s machine=%s)",
            serial_number, customer, location, machine_name,
        )
        return False

    allowed_locations = _parse_allowlist(os.environ.get("INGEST_ALLOWED_LOCATIONS", ""))
    if allowed_locations and location.strip().upper() not in allowed_locations:
        logger.warning(
            "Rejecting device: location not in INGEST_ALLOWED_LOCATIONS (serial=%s customer=%s location=%s machine=%s)",
            serial_number, customer, location, machine_name,
        )
        return False

    return True
