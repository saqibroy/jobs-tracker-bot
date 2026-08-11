"""Provider-neutral Phase 6A1 job-alert ingestion contracts."""

from integrations.job_alerts.contracts import (
    AlertMatch,
    AlertParser,
    AlertParseStatus,
    BoundedMailContent,
    JobAlertItem,
    JobAlertParseResult,
    MailIntent,
    MailIntentDecision,
    MailMessageMetadata,
)
from integrations.job_alerts.registry import AlertParserRegistry

__all__ = [
    "AlertMatch",
    "AlertParser",
    "AlertParseStatus",
    "AlertParserRegistry",
    "BoundedMailContent",
    "JobAlertItem",
    "JobAlertParseResult",
    "MailIntent",
    "MailIntentDecision",
    "MailMessageMetadata",
]
