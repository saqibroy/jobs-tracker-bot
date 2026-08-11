"""Application-level source catalog and scheduled group definitions."""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass
from typing import Callable, Iterable

import config
from sources.arbeitnow import ArbeitnowSource
from sources.ashby import AshbySource
from sources.bamboohr import BambooHRSource
from sources.base import BaseSource
from sources.devex import DevexSource
from sources.eurobrussels import EuroBrusselsSource
from sources.goodjobs import GoodJobsSource
from sources.greenhouse import GreenhouseSource
from sources.himalayas import HimalayasSource
from sources.hours80k import Hours80kSource
from sources.idealist import IdealistSource
from sources.jsonld import JsonLdCareerSource
from sources.landingjobs import LandingJobsSource
from sources.lever import LeverSource
from sources.linkedin import LinkedInSource
from sources.nofluffjobs import NoFluffJobsSource
from sources.personio import PersonioSource
from sources.reliefweb import ReliefWebSource
from sources.remoteok import RemoteOKSource
from sources.remotive import RemotiveSource
from sources.stepstone import StepstoneSource
from sources.techjobsforgood import TechJobsForGoodSource
from sources.themuse import TheMuseSource
from sources.weworkremotely import WeWorkRemotelySource
from sources.workable import WorkableSource


GROUP_A_ID = "source_group_a"
GROUP_B_ID = "source_group_b"


@dataclass(frozen=True, slots=True)
class SourceDefinition:
    name: str
    adapter_class: type[BaseSource]
    scheduled_group: str | None
    manual_only: bool
    network_budget_exception: str | None = None


@dataclass(frozen=True, slots=True)
class SourceGroup:
    scheduler_id: str
    scan_scope: str
    cadence_minutes: int
    startup_delay_minutes: int
    source_names: tuple[str, ...]


SOURCE_GROUPS: tuple[SourceGroup, ...] = (
    SourceGroup(
        scheduler_id=GROUP_A_ID,
        scan_scope="group_a",
        cadence_minutes=config.SOURCE_GROUP_A_INTERVAL_MINUTES,
        startup_delay_minutes=config.SOURCE_GROUP_A_STARTUP_DELAY_MINUTES,
        source_names=(
            "greenhouse",
            "ashby",
            "personio",
            "lever",
            "workable",
            "jsonld",
        ),
    ),
    SourceGroup(
        scheduler_id=GROUP_B_ID,
        scan_scope="group_b",
        cadence_minutes=config.SOURCE_GROUP_B_INTERVAL_MINUTES,
        startup_delay_minutes=config.SOURCE_GROUP_B_STARTUP_DELAY_MINUTES,
        source_names=(
            "arbeitnow",
            "remotive",
            "himalayas",
            "remoteok",
            "idealist",
            "linkedin",
        ),
    ),
)


def _scheduled(
    name: str,
    adapter_class: type[BaseSource],
    group: str,
) -> SourceDefinition:
    return SourceDefinition(name, adapter_class, group, False)


def _manual(name: str, adapter_class: type[BaseSource]) -> SourceDefinition:
    return SourceDefinition(name, adapter_class, None, True)


SOURCE_CATALOG: tuple[SourceDefinition, ...] = (
    _scheduled("greenhouse", GreenhouseSource, GROUP_A_ID),
    _scheduled("ashby", AshbySource, GROUP_A_ID),
    _scheduled("personio", PersonioSource, GROUP_A_ID),
    _scheduled("lever", LeverSource, GROUP_A_ID),
    _scheduled("workable", WorkableSource, GROUP_A_ID),
    _scheduled("jsonld", JsonLdCareerSource, GROUP_A_ID),
    _scheduled("arbeitnow", ArbeitnowSource, GROUP_B_ID),
    _scheduled("remotive", RemotiveSource, GROUP_B_ID),
    _scheduled("himalayas", HimalayasSource, GROUP_B_ID),
    _scheduled("remoteok", RemoteOKSource, GROUP_B_ID),
    _scheduled("idealist", IdealistSource, GROUP_B_ID),
    _scheduled("linkedin", LinkedInSource, GROUP_B_ID),
    _manual("stepstone", StepstoneSource),
    _manual("weworkremotely", WeWorkRemotelySource),
    _manual("reliefweb", ReliefWebSource),
    _manual("techjobsforgood", TechJobsForGoodSource),
    _manual("eurobrussels", EuroBrusselsSource),
    _manual("hours80k", Hours80kSource),
    _manual("goodjobs", GoodJobsSource),
    _manual("devex", DevexSource),
    _manual("nofluffjobs", NoFluffJobsSource),
    _manual("landingjobs", LandingJobsSource),
    _manual("themuse", TheMuseSource),
    _manual("bamboohr", BambooHRSource),
)

SOURCE_BY_NAME = {entry.name: entry for entry in SOURCE_CATALOG}
GROUP_BY_ID = {group.scheduler_id: group for group in SOURCE_GROUPS}


def validate_catalog() -> None:
    """Fail clearly when catalog/group membership is internally inconsistent."""

    if len(SOURCE_BY_NAME) != len(SOURCE_CATALOG):
        raise ValueError("source catalog contains duplicate names")
    scheduled_seen: set[str] = set()
    for group in SOURCE_GROUPS:
        if group.cadence_minutes < 1:
            raise ValueError(f"{group.scheduler_id} cadence must be positive")
        if not 0 < group.startup_delay_minutes < group.cadence_minutes:
            raise ValueError(
                f"{group.scheduler_id} startup delay must be non-zero and inside cadence"
            )
        for name in group.source_names:
            if name in scheduled_seen:
                raise ValueError(f"source {name} appears in multiple scheduled groups")
            scheduled_seen.add(name)
            entry = SOURCE_BY_NAME.get(name)
            if entry is None or entry.scheduled_group != group.scheduler_id:
                raise ValueError(f"source {name} has inconsistent group metadata")
            if entry.manual_only:
                raise ValueError(f"scheduled source {name} cannot be manual-only")
    for entry in SOURCE_CATALOG:
        if entry.manual_only != (entry.scheduled_group is None):
            raise ValueError(f"source {entry.name} has inconsistent manual-only metadata")
        if entry.network_budget_exception is not None and not entry.network_budget_exception.strip():
            raise ValueError(f"source {entry.name} has an empty network-budget exception")


def source_names_for_group(group_id: str) -> tuple[str, ...]:
    return GROUP_BY_ID[group_id].source_names


def manual_all_source_names() -> tuple[str, ...]:
    """Return the deterministic union of currently scheduled groups."""

    return tuple(name for group in SOURCE_GROUPS for name in group.source_names)


def instantiate_sources(names: Iterable[str]) -> list[BaseSource]:
    return [SOURCE_BY_NAME[name].adapter_class() for name in names]


_DIRECT_NETWORK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("httpx.AsyncClient", re.compile(r"\bhttpx\s*\.\s*AsyncClient\s*\(")),
    ("aiohttp.ClientSession", re.compile(r"\baiohttp\s*\.\s*ClientSession\s*\(")),
    ("requests request", re.compile(r"\brequests\s*\.\s*(?:get|post|put|patch|delete|request)\s*\(")),
    ("urllib.urlopen", re.compile(r"\burllib(?:\.request)?\s*\.\s*urlopen\s*\(")),
    ("feedparser URL", re.compile(r"\bfeedparser\s*\.\s*parse\s*\(\s*[furbFURB]*['\"]https?://")),
)


def direct_network_constructs(source_text: str) -> tuple[str, ...]:
    """Return obvious source-transport bypasses found in one adapter module."""

    return tuple(name for name, pattern in _DIRECT_NETWORK_PATTERNS if pattern.search(source_text))


def unreviewed_scheduled_network_bypasses(
    source_loader: Callable[[SourceDefinition], str] | None = None,
    entries: Iterable[SourceDefinition] | None = None,
) -> dict[str, tuple[str, ...]]:
    """Audit scheduled adapter modules against reviewed catalog exceptions."""

    load = source_loader or (
        lambda entry: inspect.getsource(inspect.getmodule(entry.adapter_class))
    )
    findings: dict[str, tuple[str, ...]] = {}
    for entry in entries or SOURCE_CATALOG:
        if entry.scheduled_group is None or entry.network_budget_exception:
            continue
        constructs = direct_network_constructs(load(entry))
        if constructs:
            findings[entry.name] = constructs
    return findings


validate_catalog()
