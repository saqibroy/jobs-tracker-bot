"""Small static registry for explicitly configured alert parsers."""

from __future__ import annotations

from dataclasses import dataclass, field

from integrations.job_alerts.contracts import (
    AlertMatch,
    AlertParser,
    BoundedMailContent,
    MailMessageMetadata,
)


def _production_parsers() -> tuple[AlertParser, ...]:
    # Local imports keep the provider modules independent of registry wiring.
    from integrations.job_alerts.indeed import IndeedAlertParser
    from integrations.job_alerts.linkedin import LinkedInAlertParser

    return (LinkedInAlertParser(), IndeedAlertParser())


@dataclass(frozen=True, slots=True)
class AlertParserRegistry:
    parsers: tuple[AlertParser, ...] = field(default_factory=_production_parsers)

    def match(
        self,
        message: MailMessageMetadata,
        content: BoundedMailContent,
    ) -> AlertMatch | None:
        matches = [parser.matches(message, content) for parser in self.parsers]
        strong = [match for match in matches if match.strong]
        if not strong:
            return None
        return max(strong, key=lambda match: (match.confidence, match.provider))

    def get(self, provider: str) -> AlertParser | None:
        normalized = provider.strip().lower()
        return next(
            (
                parser
                for parser in self.parsers
                if parser.provider.strip().lower() == normalized
            ),
            None,
        )
