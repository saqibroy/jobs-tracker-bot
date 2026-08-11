"""Phase 5A HTTP-budget, fan-out, and production-coordinator tests."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import httpx
import pytest

import config
import main
import sources.base as source_base
from models.job import Job
from models.scan import (
    FilterRunSummary,
    SourceFunnelMetrics,
    SourceStatus,
    empty_rejection_counts,
    utc_now,
)
from notifiers.base import DeliverySuccess
from scan_coordinator import ProductionScanCoordinator, ScanBusyResult
from sources.base import BaseSource
from sources.http_budget import SourceHttpBudget
from storage.database import record_delivery_receipts


class _EmptySource(BaseSource):
    name = "empty"

    async def fetch(self):
        return []


@pytest.mark.asyncio
async def test_http_budget_never_exceeds_limit() -> None:
    budget = SourceHttpBudget(3)
    active = 0
    observed = 0

    async def attempt() -> None:
        nonlocal active, observed
        async with budget.attempt():
            active += 1
            observed = max(observed, active)
            await asyncio.sleep(0.005)
            active -= 1

    await asyncio.gather(*(attempt() for _ in range(30)))
    assert observed == 3
    assert budget.observed_peak == 3
    assert budget.current_usage == 0


@pytest.mark.asyncio
async def test_http_budget_releases_on_exception_and_cancellation() -> None:
    budget = SourceHttpBudget(1)
    with pytest.raises(RuntimeError):
        async with budget.attempt():
            raise RuntimeError("boom")
    assert budget.current_usage == 0

    entered = asyncio.Event()

    async def blocked() -> None:
        async with budget.attempt():
            entered.set()
            await asyncio.Future()

    task = asyncio.create_task(blocked())
    await entered.wait()
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    assert budget.current_usage == 0
    async with asyncio.timeout(0.1):
        async with budget.attempt():
            pass


@pytest.mark.asyncio
async def test_retry_releases_http_budget_before_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _EmptySource()
    budget = SourceHttpBudget(1)
    source.bind_http_budget(budget)
    calls = 0

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, **_kwargs):
            nonlocal calls
            calls += 1
            request = httpx.Request("GET", url)
            if calls == 1:
                raise httpx.ConnectError("offline", request=request)
            return httpx.Response(200, request=request, text="ok")

    monkeypatch.setattr(source_base.httpx, "AsyncClient", lambda **_kwargs: Client())
    monkeypatch.setattr(config, "HTTP_MAX_RETRIES", 2)

    async def no_sleep(_seconds: float) -> None:
        assert budget.current_usage == 0

    monkeypatch.setattr(source_base.asyncio, "sleep", no_sleep)
    response = await source._get("https://example.test/jobs")
    assert response.status_code == 200
    assert calls == 2
    assert budget.observed_peak == 1
    assert budget.total_attempts == 2
    assert budget.retry_count == 1
    assert budget.current_usage == 0


@pytest.mark.asyncio
async def test_adapter_concurrency_is_independent_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = 0
    peak = 0

    async def fetch():
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.005)
        active -= 1
        return []

    sources = [
        SimpleNamespace(name=f"legacy-{index}", safe_fetch=fetch)
        for index in range(8)
    ]
    monkeypatch.setattr(config, "MAX_CONCURRENT_SOURCE_ADAPTERS", 2)
    monkeypatch.setattr(config, "MAX_CONCURRENT_SOURCE_COMPONENTS", 7)
    monkeypatch.setattr(config, "MAX_CONCURRENT_HTTP_REQUESTS", 5)
    outcomes, budget = await main._fetch_sources_with_budget(sources)
    assert len(outcomes) == 8
    assert peak == 2
    assert budget.observed_peak == 0


@pytest.mark.asyncio
async def test_component_boundary_uses_fixed_worker_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _EmptySource()
    monkeypatch.setattr(config, "MAX_CONCURRENT_SOURCE_COMPONENTS", 3)
    original_create_task = source_base.asyncio.create_task
    created = 0
    active = 0
    peak = 0

    def counted_create_task(*args, **kwargs):
        nonlocal created
        created += 1
        return original_create_task(*args, **kwargs)

    async def worker(item: int) -> int:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        return item

    monkeypatch.setattr(source_base.asyncio, "create_task", counted_create_task)
    results = await source._map_bounded(list(range(1000)), worker)
    assert results == list(range(1000))
    assert created == 3
    assert peak == 3


@pytest.mark.asyncio
async def test_nested_personio_like_fanout_is_deadlock_safe_and_http_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _EmptySource()
    budget = SourceHttpBudget(2)
    source.bind_http_budget(budget)
    monkeypatch.setattr(config, "MAX_CONCURRENT_SOURCE_COMPONENTS", 2)
    boundary_active = {0: 0, 1: 0}
    boundary_peak = {0: 0, 1: 0}

    async def outer(board: int):
        async def detail(item: int) -> tuple[int, int]:
            boundary_active[board] += 1
            boundary_peak[board] = max(
                boundary_peak[board], boundary_active[board]
            )
            async with budget.attempt():
                await asyncio.sleep(0)
            boundary_active[board] -= 1
            return board, item

        return await source._map_bounded(list(range(50)), detail)

    results = await asyncio.wait_for(source._map_bounded([0, 1], outer), timeout=1)
    assert len(results) == 2
    assert all(len(result) == 50 for result in results)
    assert boundary_peak == {0: 2, 1: 2}
    assert budget.observed_peak <= 2
    assert budget.current_usage == 0


@pytest.mark.asyncio
async def test_shared_http_budget_is_absolute_across_adapters_and_components(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = 0
    peak = 0

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, **_kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.005)
            active -= 1
            request = httpx.Request("GET", url)
            return httpx.Response(200, request=request, text="ok")

    class FanoutSource(BaseSource):
        def __init__(self, name: str) -> None:
            super().__init__()
            self.name = name

        async def fetch(self):
            async def one(index: int):
                await self._get(f"https://example.test/{self.name}/{index}")
                return []

            await self._map_bounded(list(range(8)), one)
            return []

    monkeypatch.setattr(source_base.httpx, "AsyncClient", lambda **_kwargs: Client())
    monkeypatch.setattr(config, "MAX_CONCURRENT_SOURCE_ADAPTERS", 3)
    monkeypatch.setattr(config, "MAX_CONCURRENT_SOURCE_COMPONENTS", 3)
    monkeypatch.setattr(config, "MAX_CONCURRENT_HTTP_REQUESTS", 4)
    outcomes, budget = await main._fetch_sources_with_budget(
        [FanoutSource("a"), FanoutSource("b"), FanoutSource("c")]
    )
    assert len(outcomes) == 3
    assert peak == 4
    assert budget.observed_peak == 4
    assert budget.current_usage == 0


@pytest.mark.asyncio
async def test_scheduled_scans_wait_in_fifo_order_and_never_overlap() -> None:
    coordinator = ProductionScanCoordinator()
    releases = {name: asyncio.Event() for name in ("a", "b", "c")}
    order: list[str] = []
    active = 0
    peak = 0

    async def scan(name: str) -> None:
        nonlocal active, peak
        async with coordinator.scheduled(f"group_{name}"):
            active += 1
            peak = max(peak, active)
            order.append(f"start:{name}")
            await releases[name].wait()
            order.append(f"end:{name}")
            active -= 1

    tasks = [asyncio.create_task(scan("a"))]
    while coordinator.active is None:
        await asyncio.sleep(0)
    tasks.extend([asyncio.create_task(scan("b")), asyncio.create_task(scan("c"))])
    await asyncio.sleep(0)
    releases["a"].set()
    while order != ["start:a", "end:a", "start:b"]:
        await asyncio.sleep(0)
    releases["b"].set()
    while order[-1:] != ["start:c"]:
        await asyncio.sleep(0)
    releases["c"].set()
    await asyncio.gather(*tasks)
    assert order == [
        "start:a", "end:a", "start:b", "end:b", "start:c", "end:c"
    ]
    assert peak == 1
    assert coordinator.active is None


@pytest.mark.asyncio
async def test_manual_scan_is_rejected_with_bounded_active_state() -> None:
    coordinator = ProductionScanCoordinator()
    async with coordinator.scheduled("group_a") as active:
        async with coordinator.manual("manual_all") as result:
            assert isinstance(result, ScanBusyResult)
            assert result.active_scope == "group_a"
            assert result.active_started_at == active.started_at
            assert "group_a" in result.message
            assert "greenhouse" not in result.message
    assert coordinator.active is None


@pytest.mark.asyncio
async def test_coordinator_cleans_up_after_exception_and_cancellation() -> None:
    coordinator = ProductionScanCoordinator()
    with pytest.raises(RuntimeError):
        async with coordinator.scheduled("group_a"):
            raise RuntimeError("failed")
    assert coordinator.active is None

    entered = asyncio.Event()

    async def cancelled_scan() -> None:
        async with coordinator.scheduled("group_b"):
            entered.set()
            await asyncio.Future()

    task = asyncio.create_task(cancelled_scan())
    await entered.wait()
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    assert coordinator.active is None
    async with coordinator.scheduled("group_a"):
        assert coordinator.active is not None


@pytest.mark.asyncio
async def test_run_scan_wraps_complete_lifecycle_and_manual_does_not_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = ProductionScanCoordinator()
    monkeypatch.setattr(main, "PRODUCTION_SCAN_COORDINATOR", coordinator)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def lifecycle(*_args, scan_scope: str, **_kwargs):
        assert coordinator.active is not None
        assert coordinator.active.scope == scan_scope
        entered.set()
        await release.wait()
        return []

    monkeypatch.setattr(main, "_run_scan_lifecycle", lifecycle)
    scheduled = asyncio.create_task(
        main.run_scan(
            [],
            dry_run=False,
            scan_scope="group_a",
            coordinator_mode="scheduled",
        )
    )
    await entered.wait()
    manual = await main.run_scan(
        [], dry_run=False, scan_scope="manual_all", coordinator_mode="manual"
    )
    assert isinstance(manual, ScanBusyResult)
    assert manual.active_scope == "group_a"
    release.set()
    assert await scheduled == []
    assert coordinator.active is None


@pytest.mark.asyncio
async def test_cross_group_url_dedup_preserves_existing_delivery_receipt(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "DATABASE_PATH", str(tmp_path / "groups.db"))
    monkeypatch.setattr(config, "ENABLE_ATS_SNIFFING", False)
    monkeypatch.setattr(main, "PRODUCTION_SCAN_COORDINATOR", ProductionScanCoordinator())
    monkeypatch.setattr(main, "_send_notifications", AsyncMock())

    def accepted_pipeline(jobs, **_kwargs):
        now = utc_now()
        per_source: dict[str, SourceFunnelMetrics] = {}
        for job in jobs:
            metrics = per_source.setdefault(
                job.source,
                SourceFunnelMetrics(
                    source=job.source,
                    started_at=now,
                    completed_at=now,
                    duration_ms=0,
                    status=SourceStatus.HEALTHY,
                ),
            )
            metrics.raw_count += 1
            metrics.accepted_count += 1
        return FilterRunSummary(
            accepted_jobs=list(jobs),
            raw_count=len(jobs),
            rejection_counts=empty_rejection_counts(),
            per_source=per_source,
        )

    monkeypatch.setattr(main, "run_filter_pipeline", accepted_pipeline)

    def duplicate_job(source: str) -> Job:
        return Job(
            title="Frontend Engineer",
            company="Acme",
            location="Germany",
            workplace_type="remote",
            remote_scope="germany",
            eligible_countries=["de"],
            url="https://jobs.example/shared",
            description="React TypeScript",
            source=source,
            notification_tier="immediate",
            match_score=80,
        )

    first_job = duplicate_job("greenhouse")
    first_source = SimpleNamespace(
        name="greenhouse", safe_fetch=AsyncMock(return_value=[first_job])
    )
    first = await main.run_scan(
        [first_source],
        dry_run=False,
        scan_scope="group_a",
        coordinator_mode="scheduled",
    )
    assert first == [first_job]
    assert await record_delivery_receipts(
        "immediate", [DeliverySuccess(first_job.id, "discord_general")]
    ) == 1

    second_source = SimpleNamespace(
        name="linkedin",
        safe_fetch=AsyncMock(return_value=[duplicate_job("linkedin")]),
    )
    second = await main.run_scan(
        [second_source],
        dry_run=False,
        scan_scope="group_b",
        coordinator_mode="scheduled",
    )
    assert second == []

    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        jobs = await (await db.execute("SELECT COUNT(*) FROM jobs")).fetchone()
        receipts = await (
            await db.execute("SELECT COUNT(*) FROM job_delivery_receipts")
        ).fetchone()
        scopes = await (
            await db.execute(
                "SELECT DISTINCT scan_scope FROM source_scan_runs ORDER BY scan_scope"
            )
        ).fetchall()
    assert jobs == (1,)
    assert receipts == (1,)
    assert scopes == [("group_a",), ("group_b",)]
