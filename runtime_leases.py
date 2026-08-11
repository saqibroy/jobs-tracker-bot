"""Small in-process leases shared by source and mail ingestion."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import AsyncIterator


_ingestion_held: ContextVar[bool] = ContextVar("job_ingestion_held", default=False)
_delivery_held: ContextVar[bool] = ContextVar("immediate_delivery_held", default=False)
_job_ingestion_lock = asyncio.Lock()
_immediate_delivery_lock = asyncio.Lock()


@asynccontextmanager
async def job_ingestion_lease() -> AsyncIterator[None]:
    """Serialize the database dedup/save critical section.

    Delivery must never acquire ingestion in reverse order.  The context flag
    makes that contract fail fast in tests and future call sites.
    """

    if _delivery_held.get():
        raise RuntimeError("job ingestion lease cannot be acquired during delivery")
    await _job_ingestion_lock.acquire()
    token = _ingestion_held.set(True)
    try:
        yield
    finally:
        _ingestion_held.reset(token)
        _job_ingestion_lock.release()


@asynccontextmanager
async def immediate_delivery_lease() -> AsyncIterator[None]:
    """Serialize pending-immediate selection through receipt persistence."""

    if _ingestion_held.get():
        raise RuntimeError("delivery lease cannot be acquired during job ingestion")
    await _immediate_delivery_lock.acquire()
    token = _delivery_held.set(True)
    try:
        yield
    finally:
        _delivery_held.reset(token)
        _immediate_delivery_lock.release()
