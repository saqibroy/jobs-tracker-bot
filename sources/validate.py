"""CLI validation for configured direct employer boards."""

from __future__ import annotations

import asyncio

from loguru import logger

import config
from sources.ashby import AshbySource
from sources.greenhouse import GreenhouseSource
from sources.jsonld import JsonLdCareerSource
from sources.lever import LeverSource
from sources.personio import PersonioSource
from sources.registry import load_company_boards
from sources.workable import WorkableSource

_SOURCES = {
    "ashby": AshbySource,
    "greenhouse": GreenhouseSource,
    "jsonld": JsonLdCareerSource,
    "lever": LeverSource,
    "personio": PersonioSource,
    "workable": WorkableSource,
}


async def validate_sources() -> int:
    boards = load_company_boards()
    print(f"\nValidating {len(boards)} enabled employer boards\n")
    semaphore = asyncio.Semaphore(max(1, config.MAX_CONCURRENT_SOURCES))

    async def validate_one(board):
        cls = _SOURCES.get(board.provider)
        if cls is None:
            return board, None, f"unknown provider '{board.provider}'"
        source = cls()
        fetcher = getattr(source, "_fetch_board", None) or getattr(source, "_fetch_company")
        try:
            async with semaphore:
                jobs = await fetcher(board)
        except Exception as exc:
            return board, None, f"{type(exc).__name__}: {exc}"
        return board, len(jobs), None

    results = await asyncio.gather(*(validate_one(board) for board in boards))
    failures = 0
    for board, count, error in results:
        if error:
            failures += 1
            print(f"  ❌ {board.company:<24} {board.provider:<12} {error}")
        else:
            print(f"  ✅ {board.company:<24} {board.provider:<12} {count:>4} jobs")
    logger.info("Board validation complete: {} healthy, {} failed", len(boards) - failures, failures)
    return failures
