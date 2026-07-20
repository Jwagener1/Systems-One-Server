from __future__ import annotations

import importlib
import os
from typing import Optional


def try_load_dotenv(*, override: bool = False) -> None:
    """Load .env if python-dotenv is installed.

    This keeps python-dotenv optional: production can use real environment variables.
    """

    try:
        dotenv = importlib.import_module("dotenv")
    except ModuleNotFoundError:
        return

    load_dotenv = getattr(dotenv, "load_dotenv", None)
    if callable(load_dotenv):
        load_dotenv(override=override)


def is_truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def getenv_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def getenv_int(name: str, default: int) -> int:
    raw = getenv_str(name, str(default))
    try:
        return int(raw)
    except ValueError:
        return default
