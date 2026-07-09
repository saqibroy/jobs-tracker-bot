"""Typed registry for direct employer career boards."""

from __future__ import annotations

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 development environments
    import tomli as tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class CompanyBoard:
    company: str
    provider: str
    slug: str
    enabled: bool = True
    region: str = "global"
    url: str = ""


@lru_cache(maxsize=1)
def load_company_boards(include_disabled: bool = False) -> tuple[CompanyBoard, ...]:
    path = Path(__file__).resolve().parent.parent / "companies.toml"
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    boards = tuple(CompanyBoard(**item) for item in raw.get("companies", []))
    if include_disabled:
        return boards
    return tuple(board for board in boards if board.enabled)


def boards_for(provider: str, include_disabled: bool = False) -> list[CompanyBoard]:
    return [
        board for board in load_company_boards(include_disabled)
        if board.provider == provider
    ]
