"""Deterministic mail-intent routing with application-first precedence."""

from __future__ import annotations

import re

from integrations.job_alerts.contracts import (
    BoundedMailContent,
    MailIntent,
    MailIntentDecision,
    MailMessageMetadata,
)
from integrations.job_alerts.registry import AlertParserRegistry

_APPLICATION_PATTERNS = (
    ("application_received", re.compile(r"\b(application received|received your application|we received your application|application (?:was )?submitted)\b", re.I)),
    ("application_thanks", re.compile(r"\bthank(?:s| you) (?:very much )?for (?:your application|applying)\b", re.I)),
    ("application_update", re.compile(r"\b(update|status|feedback) (?:on|about|regarding) your application\b", re.I)),
    ("application_rejection", re.compile(r"\b(not move forward|application (?:was )?rejected|declined your application)\b", re.I)),
    ("application_unfortunate_decision", re.compile(r"\bunfortunately\b.{0,240}\b(?:not (?:be )?(?:moving|move|progress|proceed|continuing|selected)|unable to|decided (?:not|to (?:move|go|continue|proceed) with)|other candidates?|will not|won['’]t|cannot|can't|unsuccessful|not (?:a|the right) (?:match|fit)|your (?:application|candidacy))\b", re.I | re.S)),
    ("application_unfortunate_match", re.compile(r"(?=.*\bunfortunately\b)(?=.*\bapplication\b)(?=.*\binterest\b)(?=.*\bmatch\b)", re.I | re.S)),
    ("application_interview", re.compile(r"\b(your interview|invite you (?:to|for) (?:an )?interview|screening (?:call|interview)|candidate interview)\b", re.I)),
    ("application_offer", re.compile(r"\b(we (?:would like to|are pleased to) offer you|your (?:job )?offer|offer letter)\b", re.I)),
    ("application_german", re.compile(r"\b(deine|ihre|wir haben (?:deine|ihre))\s+bewerbung\b|\bbewerbung (?:eingegangen|abgelehnt)\b", re.I)),
)
_RECRUITER_PATTERNS = (
    ("recruiter_profile_outreach", re.compile(r"\b(came across|found|saw) your profile\b", re.I)),
    ("recruiter_direct_outreach", re.compile(r"\b(recruiter|talent acquisition|sourcer)\b.{0,120}\b(opportunity|role|position|connect|discuss)\b", re.I | re.S)),
    ("recruiter_discussion", re.compile(r"\bwould (?:you|like) (?:be interested|to discuss|to connect)\b", re.I)),
)


def route_mail_intent(
    message: MailMessageMetadata,
    content: BoundedMailContent,
    registry: AlertParserRegistry,
) -> MailIntentDecision:
    text = "\n".join((message.subject, message.summary, content.cleaned_text))
    for code, pattern in _APPLICATION_PATTERNS:
        if pattern.search(text):
            return MailIntentDecision(
                MailIntent.APPLICATION_OR_RECRUITMENT,
                evidence=(code,),
            )

    alert_match = registry.match(message, content)
    if alert_match is not None:
        return MailIntentDecision(
            MailIntent.JOB_ALERT,
            provider=alert_match.provider,
            evidence=("registered_alert_parser_strong", *alert_match.evidence),
        )

    for code, pattern in _RECRUITER_PATTERNS:
        if pattern.search(text):
            return MailIntentDecision(
                MailIntent.APPLICATION_OR_RECRUITMENT,
                evidence=(code,),
            )

    return MailIntentDecision(
        MailIntent.UNKNOWN_JOB_EMAIL,
        evidence=("likely_job_unresolved",),
    )
