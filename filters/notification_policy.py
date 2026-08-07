"""Pure notification-policy simulation and deterministic company selection."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, TypeAlias, cast

from models.job import Job, NotificationTier


FreelancePermissionMaxTier: TypeAlias = Literal["immediate", "digest", "explore"]
EmploymentBucket: TypeAlias = Literal[
    "freelance", "part_time", "contract_or_fixed_term", "standard"
]
CompanySelectionMode: TypeAlias = Literal["score_only", "diversity"]

TIER_PRIORITY: tuple[NotificationTier, ...] = (
    "immediate",
    "digest",
    "explore",
    "none",
)
EMPLOYMENT_BUCKET_PRIORITY: tuple[EmploymentBucket, ...] = (
    "freelance",
    "part_time",
    "contract_or_fixed_term",
    "standard",
)
SCORE_BANDS: tuple[tuple[str, int, int], ...] = (
    ("70-100", 70, 100),
    ("45-69", 45, 69),
    ("30-44", 30, 44),
    ("15-29", 15, 29),
    ("0-14", 0, 14),
)


def _require_int(name: str, value: object, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"notifications.{name} must be an integer from {minimum} to {maximum}")
    if not minimum <= value <= maximum:
        raise ValueError(f"notifications.{name} must be an integer from {minimum} to {maximum}")
    return value


@dataclass(frozen=True, slots=True)
class NotificationPolicy:
    """One fully validated policy used by simulation and, after rollout, production."""

    immediate_score: int
    digest_score: int
    explore_score: int
    daily_explore_enabled: bool
    explore_hour_utc: int
    immediate_max_items: int
    digest_max_items: int
    explore_max_items: int
    pending_max_age_days: int
    max_jobs_per_company: int
    freelance_permission_max_tier: FreelancePermissionMaxTier

    def __post_init__(self) -> None:
        immediate = _require_int("immediate_score", self.immediate_score, 0, 100)
        digest = _require_int("digest_score", self.digest_score, 0, 100)
        explore = _require_int("explore_score", self.explore_score, 0, 100)
        if not immediate > digest > explore:
            raise ValueError(
                "notifications thresholds must satisfy "
                "100 >= immediate_score > digest_score > explore_score >= 0"
            )
        if not isinstance(self.daily_explore_enabled, bool):
            raise ValueError("notifications.daily_explore_enabled must be a boolean")
        _require_int("explore_hour_utc", self.explore_hour_utc, 0, 23)
        for name in ("immediate_max_items", "digest_max_items", "explore_max_items"):
            _require_int(name, getattr(self, name), 1, 25)
        _require_int("pending_max_age_days", self.pending_max_age_days, 1, 30)
        _require_int("max_jobs_per_company", self.max_jobs_per_company, 1, 5)
        if self.freelance_permission_max_tier not in {
            "immediate",
            "digest",
            "explore",
        }:
            raise ValueError(
                "notifications.freelance_permission_max_tier must be one of "
                "immediate, digest, explore"
            )


def simulation_policy(
    *,
    digest_score: int,
    explore_score: int,
    daily_explore_enabled: bool,
    max_jobs_per_company: int,
    freelance_permission_max_tier: FreelancePermissionMaxTier = "digest",
) -> NotificationPolicy:
    """Build a candidate policy while keeping the approved bounded constants."""

    return NotificationPolicy(
        immediate_score=70,
        digest_score=digest_score,
        explore_score=explore_score,
        daily_explore_enabled=daily_explore_enabled,
        explore_hour_utc=17,
        immediate_max_items=15,
        digest_max_items=15,
        explore_max_items=10,
        pending_max_age_days=14,
        max_jobs_per_company=max_jobs_per_company,
        freelance_permission_max_tier=freelance_permission_max_tier,
    )


def assign_notification_tier(score: int, policy: NotificationPolicy) -> NotificationTier:
    """Return the policy tier for one hard-eligible score at exact boundaries."""

    if score >= policy.immediate_score:
        return "immediate"
    if score >= policy.digest_score:
        return "digest"
    if score >= policy.explore_score and policy.daily_explore_enabled:
        return "explore"
    return "none"


def apply_freelance_permission_ceiling(
    job: Job,
    tier: NotificationTier,
    policy: NotificationPolicy,
) -> NotificationTier:
    """Demote permission-required freelance routing without rejecting the job."""

    if not (
        job.employment_relationship == "freelance"
        and job.freelance_permission_required
    ):
        return tier
    ceiling = policy.freelance_permission_max_tier
    if ceiling == "immediate":
        return tier
    if ceiling == "digest" and tier == "immediate":
        return "digest"
    if ceiling == "explore" and tier in {"immediate", "digest"}:
        return "explore"
    return tier


def employment_bucket(job: Job) -> EmploymentBucket:
    """Classify a candidate into exactly one diversity/presentation bucket."""

    if job.employment_relationship == "freelance":
        return "freelance"
    if job.work_schedule == "part_time":
        return "part_time"
    if (
        job.employment_relationship == "contract_employee"
        or job.contract_term == "fixed_term"
    ):
        return "contract_or_fixed_term"
    return "standard"


def normalize_company(company: str) -> str:
    return company.lower().strip()


def _aware_timestamp(value: datetime) -> float:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def notification_rank(job: Job) -> tuple[int, float, float, str]:
    """Comparable ascending key for the approved global deterministic rank."""

    return (
        -job.match_score,
        -_aware_timestamp(job.posted_at or job.fetched_at),
        -_aware_timestamp(job.fetched_at),
        job.id,
    )


@dataclass(frozen=True, slots=True)
class CompanySelectionResult:
    selected: tuple[Job, ...]
    excluded: tuple[Job, ...]


def _select_company_tier(
    candidates: list[Job],
    slots: int,
    *,
    diversity: bool,
) -> list[Job]:
    ranked = sorted(candidates, key=notification_rank)
    if slots <= 0 or not ranked:
        return []
    if not diversity or slots == 1:
        return ranked[:slots]

    chosen = [ranked.pop(0)]
    represented = {employment_bucket(chosen[0])}
    while ranked and len(chosen) < slots:
        diverse_pick = next(
            (job for job in ranked if employment_bucket(job) not in represented),
            None,
        )
        if diverse_pick is None:
            break
        ranked.remove(diverse_pick)
        chosen.append(diverse_pick)
        represented.add(employment_bucket(diverse_pick))

    if len(chosen) < slots:
        chosen.extend(ranked[: slots - len(chosen)])
    return chosen


def select_company_candidates(
    jobs: list[Job],
    max_jobs_per_company: int,
    *,
    mode: CompanySelectionMode,
) -> CompanySelectionResult:
    """Apply one overall cap per company while preserving routing-tier priority."""

    _require_int("max_jobs_per_company", max_jobs_per_company, 1, 5)
    if mode not in {"score_only", "diversity"}:
        raise ValueError("company selection mode must be score_only or diversity")
    by_company: defaultdict[str, list[Job]] = defaultdict(list)
    for job in jobs:
        by_company[normalize_company(job.company)].append(job)

    selected_ids: set[str] = set()
    selected: list[Job] = []
    for company in sorted(by_company):
        remaining_slots = max_jobs_per_company
        company_selected: list[Job] = []
        for tier in TIER_PRIORITY:
            if remaining_slots <= 0:
                break
            tier_jobs = [
                job for job in by_company[company] if job.notification_tier == tier
            ]
            tier_selected = _select_company_tier(
                tier_jobs,
                remaining_slots,
                diversity=mode == "diversity",
            )
            company_selected.extend(tier_selected)
            remaining_slots -= len(tier_selected)
        for job in company_selected:
            selected_ids.add(job.id)
            selected.append(job)

    selected.sort(key=notification_rank)
    excluded = sorted(
        (job for job in jobs if job.id not in selected_ids),
        key=notification_rank,
    )
    return CompanySelectionResult(tuple(selected), tuple(excluded))


def _score_bands(jobs: list[Job] | tuple[Job, ...]) -> dict[str, int]:
    counts = {name: 0 for name, _, _ in SCORE_BANDS}
    for job in jobs:
        for name, minimum, maximum in SCORE_BANDS:
            if minimum <= job.match_score <= maximum:
                counts[name] += 1
                break
    return counts


def _distribution(jobs: list[Job], attribute: str) -> dict[str, int]:
    counts = Counter(str(getattr(job, attribute) or "unknown") for job in jobs)
    return dict(sorted(counts.items()))


def _company_counts(jobs: list[Job] | tuple[Job, ...]) -> dict[str, int]:
    counts = Counter(normalize_company(job.company) for job in jobs)
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _visibility(jobs: tuple[Job, ...]) -> dict[str, int]:
    return {
        "part_time": sum(job.work_schedule == "part_time" for job in jobs),
        "contract_employee": sum(
            job.employment_relationship == "contract_employee" for job in jobs
        ),
        "fixed_term": sum(job.contract_term == "fixed_term" for job in jobs),
        "contract_or_fixed_term": sum(
            job.employment_relationship == "contract_employee"
            or job.contract_term == "fixed_term"
            for job in jobs
        ),
        "freelance": sum(job.employment_relationship == "freelance" for job in jobs),
        "freelance_permission_required": sum(
            job.freelance_permission_required for job in jobs
        ),
    }


def _tradeoffs(
    diverse: CompanySelectionResult,
    score_only: CompanySelectionResult,
) -> dict[str, object]:
    diverse_ids = {job.id for job in diverse.selected}
    score_ids = {job.id for job in score_only.selected}
    added = [job for job in diverse.selected if job.id not in score_ids]
    removed = [job for job in score_only.selected if job.id not in diverse_ids]
    return {
        "added_by_tier": dict(sorted(Counter(job.notification_tier for job in added).items())),
        "removed_by_tier": dict(sorted(Counter(job.notification_tier for job in removed).items())),
        "added_score_total": sum(job.match_score for job in added),
        "removed_score_total": sum(job.match_score for job in removed),
        "selected_score_delta": sum(job.match_score for job in diverse.selected)
        - sum(job.match_score for job in score_only.selected),
        "tier_counts_preserved": Counter(job.notification_tier for job in diverse.selected)
        == Counter(job.notification_tier for job in score_only.selected),
    }


@dataclass(frozen=True, slots=True)
class SimulationScenario:
    name: str
    policy: NotificationPolicy
    selection_mode: CompanySelectionMode


def _scenario_jobs(jobs: list[Job], policy: NotificationPolicy) -> list[Job]:
    routed: list[Job] = []
    for job in jobs:
        tier = assign_notification_tier(job.match_score, policy)
        tier = apply_freelance_permission_ceiling(job, tier, policy)
        routed.append(job.model_copy(update={"notification_tier": tier}))
    return routed


def _scenario_result(jobs: list[Job], scenario: SimulationScenario) -> dict[str, object]:
    routed = _scenario_jobs(jobs, scenario.policy)
    selection = select_company_candidates(
        routed,
        scenario.policy.max_jobs_per_company,
        mode=scenario.selection_mode,
    )
    score_only = select_company_candidates(
        routed,
        scenario.policy.max_jobs_per_company,
        mode="score_only",
    )
    tiers = Counter(job.notification_tier for job in selection.selected)
    return {
        "immediate": tiers["immediate"],
        "digest": tiers["digest"],
        "explore": tiers["explore"],
        "none": tiers["none"],
        "cap_exclusions": len(selection.excluded),
        "selected_jobs_per_company": _company_counts(selection.selected),
        "visibility": _visibility(selection.selected),
        "selected_score_distribution": _score_bands(selection.selected),
        "lowest_selected_score_by_tier": {
            tier: min(
                (job.match_score for job in selection.selected if job.notification_tier == tier),
                default=None,
            )
            for tier in TIER_PRIORITY
        },
        "same_tier_diversity_tradeoffs": _tradeoffs(selection, score_only),
    }


def default_simulation_scenarios() -> tuple[SimulationScenario, ...]:
    """Return A plus B/C cap 2..5 candidates without choosing production values."""

    scenarios = [
        SimulationScenario(
            "A-current-cap2",
            simulation_policy(
                digest_score=45,
                explore_score=30,
                daily_explore_enabled=False,
                max_jobs_per_company=2,
                freelance_permission_max_tier="immediate",
            ),
            "score_only",
        )
    ]
    for label, digest, explore in (("B-aggressive", 30, 15), ("C-conservative", 45, 30)):
        for cap in range(2, 6):
            scenarios.append(
                SimulationScenario(
                    f"{label}-cap{cap}",
                    simulation_policy(
                        digest_score=digest,
                        explore_score=explore,
                        daily_explore_enabled=True,
                        max_jobs_per_company=cap,
                    ),
                    "diversity",
                )
            )
    return tuple(scenarios)


def build_notification_simulation(jobs: list[Job]) -> dict[str, object]:
    """Build the bounded aggregate A/B/C report from pre-cap candidates."""

    current = _scenario_jobs(
        jobs,
        simulation_policy(
            digest_score=45,
            explore_score=30,
            daily_explore_enabled=False,
            max_jobs_per_company=2,
            freelance_permission_max_tier="immediate",
        ),
    )
    current_selection = select_company_candidates(current, 2, mode="score_only")
    return {
        "hard_eligible_pre_cap_total": len(jobs),
        "score_bands": _score_bands(jobs),
        "source_distribution": _distribution(jobs, "source"),
        "relationship_distribution": _distribution(jobs, "employment_relationship"),
        "schedule_distribution": _distribution(jobs, "work_schedule"),
        "contract_employee_count": sum(
            job.employment_relationship == "contract_employee" for job in jobs
        ),
        "fixed_term_count": sum(job.contract_term == "fixed_term" for job in jobs),
        "contract_or_fixed_term_count": sum(
            job.employment_relationship == "contract_employee"
            or job.contract_term == "fixed_term"
            for job in jobs
        ),
        "freelance_count": sum(job.employment_relationship == "freelance" for job in jobs),
        "freelance_permission_required_count": sum(
            job.freelance_permission_required for job in jobs
        ),
        "per_company_concentration": _company_counts(jobs),
        "current_score_only_cap2_exclusions": len(current_selection.excluded),
        "scenarios": {
            scenario.name: _scenario_result(jobs, scenario)
            for scenario in default_simulation_scenarios()
        },
    }


def format_notification_simulation(report: dict[str, object]) -> str:
    """Render only aggregate counts; never include posting descriptions."""

    lines = ["NOTIFICATION POLICY SIMULATION (read-only)"]
    for key in (
        "hard_eligible_pre_cap_total",
        "score_bands",
        "source_distribution",
        "relationship_distribution",
        "schedule_distribution",
        "contract_employee_count",
        "fixed_term_count",
        "contract_or_fixed_term_count",
        "freelance_count",
        "freelance_permission_required_count",
        "per_company_concentration",
        "current_score_only_cap2_exclusions",
    ):
        lines.append(f"{key}: {report[key]}")
    lines.append("scenarios:")
    scenarios = cast(dict[str, dict[str, object]], report["scenarios"])
    for name, values in scenarios.items():
        lines.append(f"  {name}: {values}")
    return "\n".join(lines)
