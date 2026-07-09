"""Load the sanitized candidate profile used by role and match evaluation."""

from __future__ import annotations

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 development environments
    import tomli as tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any


@lru_cache(maxsize=1)
def load_profile() -> dict[str, Any]:
    path = Path(__file__).resolve().parent.parent / "profile.toml"
    with path.open("rb") as handle:
        return tomllib.load(handle)


def profile_list(section: str, key: str) -> list[str]:
    values = load_profile().get(section, {}).get(key, [])
    return [str(value).lower() for value in values]


def profile_value(section: str, key: str, default: Any = None) -> Any:
    return load_profile().get(section, {}).get(key, default)
