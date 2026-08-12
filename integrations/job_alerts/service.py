"""Shared post-fetch processing for bounded Zoho and Gmail messages."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from integrations.job_alerts.contracts import (
    BoundedMailContent,
    JobAlertItem,
    MAX_ALERT_ITEMS_PER_SYNC,
    MailIntent,
    MailMessageMetadata,
)
from integrations.job_alerts.registry import AlertParserRegistry
from integrations.job_alerts.processing import alert_item_to_job
from integrations.job_alerts.routing import route_mail_intent
from integrations.job_alerts.urls import alert_identity_key
from job_ingestion import JobIngestionCandidate, process_discovered_jobs
from storage.zoho_mail import (
    complete_alert_item_results,
    get_pending_alert_input_keys,
    record_alert_provider_health,
    upsert_alert_item_pending,
)

_JOB_EMAIL_KEYWORDS = {
    "application",
    "applied",
    "applying",
    "interview",
    "recruiter",
    "recruiting",
    "talent",
    "job",
    "jobs",
    "position",
    "role",
    "career",
    "careers",
    "candidate",
    "offer",
    "hiring",
    "shortlisted",
    "rejected",
    "unfortunately",
}
_ATS_HINT_KEYWORDS = {
    "ashbyhq.com",
    "greenhouse.io",
    "personio.",
    "m.personio.de",
    "lever.co",
    "workable.com",
    "bamboohr.com",
    "teamtailor.com",
    "smartrecruiters.com",
    "recruitee.com",
    "join.com",
    "onlyfy",
    "softgarden",
    "myworkdayjobs.com",
    "workdayjobs.com",
    "successfactors",
}
_ALERT_SENDER_HINTS = {
    "jobalerts-noreply@linkedin.com",
    "donotreply@match.indeed.com",
    "info@jobagent.stepstone.de",
}
MAIL_PROCESSING_VERSION = 1


@dataclass(frozen=True, slots=True)
class MailTransportEnvelope:
    """One transport-neutral, bounded message ready for intent routing."""

    transport: str
    mailbox_key: str
    message: MailMessageMetadata
    content: BoundedMailContent
    summary_links: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MessageCompletion:
    transport: str
    mailbox_key: str
    message_id: str
    intent: str
    provider: str = ""
    result: str = "handled"
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ApplicationProcessingResult:
    extracted_records: int = 0
    review_records: int = 0


@dataclass(frozen=True, slots=True)
class SharedMailProcessingResult:
    item_results: tuple[object, ...]
    completions: tuple[MessageCompletion, ...]
    provider_health: tuple[str, ...]
    counts: dict[str, int]
    backlog: bool


ApplicationHandler = Callable[
    [MailTransportEnvelope], Awaitable[ApplicationProcessingResult]
]
RoutingWriter = Callable[[str, str, str, str], Awaitable[None]]


def is_likely_job_message(
    message: MailMessageMetadata,
    content: BoundedMailContent | None = None,
    *,
    links: tuple[str, ...] = (),
) -> bool:
    """Apply the established cheap likely-job signals to either transport."""

    text = " ".join(
        (
            message.subject,
            message.sender,
            message.summary,
            " ".join(links),
            content.cleaned_text if content is not None else "",
            " ".join(content.links) if content is not None else "",
        )
    ).lower()
    if any(hint in text for hint in _ATS_HINT_KEYWORDS):
        return True
    if any(sender in message.sender.lower() for sender in _ALERT_SENDER_HINTS):
        return True
    return any(
        re.search(rf"\b{re.escape(keyword)}\b", text) for keyword in _JOB_EMAIL_KEYWORDS
    )


def inside_alert_window(message_date: datetime | None) -> bool:
    if message_date is None:
        return False
    value = (
        message_date
        if message_date.tzinfo is not None
        else message_date.replace(tzinfo=timezone.utc)
    )
    return value >= datetime.now(timezone.utc) - timedelta(days=14)


def is_current_processing_state(state: object) -> bool:
    """Apply the one shared routing generation across mail transports."""

    return (
        bool(getattr(state, "processed", False))
        and int(getattr(state, "processing_version", 0) or 0) >= MAIL_PROCESSING_VERSION
    )


def is_stale_legacy_processing_state(
    state: object,
    message_date: datetime | None,
) -> bool:
    return (
        bool(getattr(state, "processed", False))
        and int(getattr(state, "processing_version", 0) or 0) == 0
        and not inside_alert_window(message_date)
    )


@dataclass(slots=True)
class SharedMailPostFetchProcessor:
    """Route, parse, persist, and batch alert jobs for one transport sync."""

    dry_run: bool
    parser_registry: AlertParserRegistry = field(default_factory=AlertParserRegistry)
    max_alert_items: int = MAX_ALERT_ITEMS_PER_SYNC
    pipeline: Callable[..., Awaitable[Any]] = process_discovered_jobs
    terminal_callback: Callable[..., Awaitable[None]] = complete_alert_item_results
    pending: dict[str, JobIngestionCandidate] = field(default_factory=dict)
    completions: list[MessageCompletion] = field(default_factory=list)
    provider_health: dict[str, str] = field(default_factory=dict)
    counts: dict[str, int] = field(
        default_factory=lambda: {
            "application_messages": 0,
            "alert_messages": 0,
            "unknown_job_messages": 0,
            "parsed_alert_items": 0,
            "valid_alert_items": 0,
            "invalid_alert_items": 0,
            "pending_alert_items": 0,
            "processed_alert_items": 0,
            "provider_failures": 0,
            "pipeline_accepted": 0,
            "pipeline_rejected": 0,
            "backlog_deferred": 0,
            "extracted_records": 0,
            "review_records": 0,
        }
    )
    backlog: bool = False

    def add_completion(
        self,
        *,
        transport: str,
        mailbox_key: str,
        message_id: str,
        intent: str,
        provider: str = "",
        result: str,
        reason: str,
    ) -> None:
        self.completions.append(
            MessageCompletion(
                transport,
                mailbox_key,
                message_id,
                intent,
                provider,
                result,
                reason,
            )
        )

    async def process_message(
        self,
        envelope: MailTransportEnvelope,
        *,
        routing_writer: RoutingWriter,
        application_handler: ApplicationHandler | None = None,
    ) -> bool:
        """Process one fetched message; return False when it is deferred."""

        message = envelope.message
        decision = route_mail_intent(message, envelope.content, self.parser_registry)
        await routing_writer(
            decision.intent.value,
            decision.provider,
            "classified",
            ";".join(decision.evidence),
        )

        if decision.intent == MailIntent.APPLICATION_OR_RECRUITMENT:
            self.counts["application_messages"] += 1
            if application_handler is not None:
                application = await application_handler(envelope)
                self.counts["extracted_records"] += application.extracted_records
                self.counts["review_records"] += application.review_records
                result = "application_processed"
            else:
                # Gmail is alert transport only. Lifecycle mail is completed
                # deterministically but never copied into Zoho application state.
                result = "non_alert_handled"
            self._complete(
                envelope,
                decision.intent.value,
                result=result,
                reason=";".join(decision.evidence),
            )
            return True

        if decision.intent == MailIntent.UNKNOWN_JOB_EMAIL:
            self.counts["unknown_job_messages"] += 1
            self._complete(
                envelope,
                decision.intent.value,
                result="unknown_handled",
                reason=";".join(decision.evidence),
            )
            return True

        self.counts["alert_messages"] += 1
        within_window = inside_alert_window(message.message_date)
        replay_keys = await get_pending_alert_input_keys(
            account_id=envelope.mailbox_key,
            message_id=message.message_id,
            transport=envelope.transport,
            mailbox_key=envelope.mailbox_key,
        )
        if not within_window and not replay_keys:
            reason = (
                "alert_missing_reliable_message_date"
                if message.message_date is None
                else "alert_outside_14_day_window"
            )
            self._complete(
                envelope,
                decision.intent.value,
                provider=decision.provider,
                result="alert_skipped",
                reason=reason,
            )
            return True

        parser = self.parser_registry.get(decision.provider)
        if parser is None:
            self._complete(
                envelope,
                MailIntent.UNKNOWN_JOB_EMAIL.value,
                result="unsupported_alert_provider",
                reason="registered_parser_unavailable",
            )
            return True
        try:
            parsed = parser.parse(message, envelope.content)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._record_failure(
                decision.provider, "parse_error", "provider_parser_exception"
            )
            raise

        self.counts["parsed_alert_items"] += parsed.examined_count
        self.counts["valid_alert_items"] += len(parsed.items)
        self.counts["invalid_alert_items"] += parsed.invalid_count
        self.provider_health[decision.provider] = parsed.status.value
        await record_alert_provider_health(
            provider=decision.provider,
            status=parsed.status.value,
            examined_count=parsed.examined_count,
            valid_count=len(parsed.items),
            invalid_count=parsed.invalid_count,
            issues=parsed.issues,
            dry_run=self.dry_run,
        )

        parsed_keys: set[str] = set()
        stale_new_item = False
        for raw_item in parsed.items:
            item = replace(
                raw_item,
                provider=decision.provider,
                account_id=envelope.mailbox_key,
                message_id=message.message_id,
            )
            input_key = _input_key(item)
            parsed_keys.add(input_key)
            if not within_window and input_key not in replay_keys:
                stale_new_item = True
                continue
            if (
                input_key not in self.pending
                and len(self.pending) >= self.max_alert_items
            ):
                self.backlog = True
                self.counts["backlog_deferred"] += 1
                return False
            persistence = await upsert_alert_item_pending(
                item,
                dry_run=self.dry_run,
                transport=envelope.transport,
                mailbox_key=envelope.mailbox_key,
            )
            if persistence.state == "processed":
                self.counts["processed_alert_items"] += 1
                continue
            if persistence.input_key not in self.pending:
                self.pending[persistence.input_key] = JobIngestionCandidate(
                    persistence.input_key,
                    alert_item_to_job(item),
                )
                self.counts["pending_alert_items"] += 1

        missing = replay_keys - parsed_keys
        if missing:
            await self._record_failure(
                decision.provider,
                "pending_replay_missing",
                "pending_item_not_reproduced",
                examined=parsed.examined_count,
                valid=len(parsed.items),
                invalid=parsed.invalid_count,
            )
            raise RuntimeError("pending alert item was not reproduced by parser")

        issues = list(parsed.issues)
        if stale_new_item:
            issues.append("stale_new_alert_item_ignored")
        self._complete(
            envelope,
            decision.intent.value,
            provider=decision.provider,
            result="alert_items_handled",
            reason=";".join(issues),
        )
        return True

    async def finalize(self) -> SharedMailProcessingResult:
        """Run the one normal job-ingestion batch after all mail is parsed."""

        try:
            ingestion = await self.pipeline(
                list(self.pending.values()),
                persist=not self.dry_run,
                associate_items=True,
                on_terminal=(None if self.dry_run else self.terminal_callback),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            for provider in {key.partition(":")[0] for key in self.pending}:
                await self._record_failure(
                    provider,
                    "processing_error",
                    "normal_job_ingestion_failed",
                )
            raise

        if not self.dry_run:
            terminal_statuses = {
                str(getattr(result.status, "value", result.status))
                for result in ingestion.item_results
            }
            if len(ingestion.item_results) != len(
                self.pending
            ) or not terminal_statuses <= {"saved", "duplicate", "rejected"}:
                raise RuntimeError("mail alert pipeline did not reach terminal results")

        for result in ingestion.item_results:
            status = str(getattr(result.status, "value", result.status))
            key = "pipeline_rejected" if status == "rejected" else "pipeline_accepted"
            self.counts[key] += 1
        if not self.dry_run:
            self.counts["processed_alert_items"] += len(ingestion.item_results)
        return SharedMailProcessingResult(
            item_results=tuple(ingestion.item_results),
            completions=tuple(self.completions),
            provider_health=tuple(
                f"{provider}:{status}"
                for provider, status in sorted(self.provider_health.items())[:10]
            ),
            counts=dict(self.counts),
            backlog=self.backlog,
        )

    def _complete(
        self,
        envelope: MailTransportEnvelope,
        intent: str,
        *,
        provider: str = "",
        result: str,
        reason: str,
    ) -> None:
        self.completions.append(
            MessageCompletion(
                envelope.transport,
                envelope.mailbox_key,
                envelope.message.message_id,
                intent,
                provider,
                result,
                reason,
            )
        )

    async def _record_failure(
        self,
        provider: str,
        status: str,
        issue: str,
        *,
        examined: int = 0,
        valid: int = 0,
        invalid: int = 0,
    ) -> None:
        self.counts["provider_failures"] += 1
        self.provider_health[provider] = status
        await record_alert_provider_health(
            provider=provider,
            status=status,
            examined_count=examined,
            valid_count=valid,
            invalid_count=invalid,
            issues=(issue,),
            processing_failure=True,
            dry_run=self.dry_run,
        )


def _input_key(item: JobAlertItem) -> str:
    identity_key, _ = alert_identity_key(
        provider_item_id=item.provider_item_id,
        canonical_url=item.canonical_url,
        title=item.title,
        company=item.company,
        location=item.location,
    )
    return f"{item.provider}:{identity_key}"
