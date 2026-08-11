"""Convert validated provider-neutral alert items into normal Job inputs."""

from __future__ import annotations

from models.job import Job
from integrations.job_alerts.contracts import JobAlertItem

_ALERT_SOURCES = {
    "linkedin": "linkedin_alert",
    "indeed": "indeed_alert",
}


def alert_item_to_job(item: JobAlertItem) -> Job:
    """Map only explicit alert evidence; never invent work eligibility."""

    tags = [item.employment_text] if item.employment_text else []
    return Job(
        title=item.title,
        company=item.company,
        location=item.location,
        url=item.job_url or item.canonical_url,
        description=item.summary or None,
        salary=item.salary or None,
        tags=tags,
        source=_ALERT_SOURCES.get(item.provider, item.provider),
        is_remote=bool(item.is_remote) if item.is_remote is not None else False,
        workplace_type=item.workplace_type,
        remote_scope=item.remote_scope,
        eligible_countries=list(item.eligible_countries),
        eligible_regions=list(item.eligible_regions),
        posted_at=item.posted_at,
    )
