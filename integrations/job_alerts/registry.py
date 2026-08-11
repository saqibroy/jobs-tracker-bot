"""Small static registry for explicitly configured alert parsers."""

from __future__ import annotations

from dataclasses import dataclass

from integrations.job_alerts.contracts import (
    AlertMatch,
    AlertParser,
    BoundedMailContent,
    MailMessageMetadata,
)


@dataclass(frozen=True, slots=True)
class AlertParserRegistry:
    parsers: tuple[AlertParser, ...] = ()

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
