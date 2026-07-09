"""Language filter — accept English-only job postings.

Uses langdetect on the job title + first 300 chars of description.
If detection fails or is uncertain, defaults to ACCEPT (don't over-filter).
"""

from __future__ import annotations

from langdetect import DetectorFactory, detect
from langdetect.lang_detect_exception import LangDetectException
from loguru import logger

from models.job import Job

# Make langdetect deterministic
DetectorFactory.seed = 0

_ADVANCED_GERMAN_RE = (
    r"\b(?:german|deutsch)\b.{0,40}\b(?:b2|c1|c2|fluent|native|"
    r"professional|business[- ]fluent|verhandlungssicher)\b|"
    r"\b(?:b2|c1|c2|fluent|native|professional|business[- ]fluent|"
    r"verhandlungssicher)\b.{0,40}\b(?:german|deutsch)\b"
)


def passes_language_filter(job: Job) -> bool:
    """Return True if the job appears to be in English (or detection is uncertain)."""
    import re

    full_text = f"{job.title} {job.description or ''}".lower()
    if re.search(_ADVANCED_GERMAN_RE, full_text, flags=re.IGNORECASE):
        logger.debug("Language REJECT (German above B1 required): {}", job.title)
        return False

    text = job.title
    if job.description:
        text += " " + job.description[:300]

    # Very short text — can't detect reliably, accept
    if len(text.strip()) < 20:
        logger.debug("Language ACCEPT (too short to detect): {}", job.title)
        return True

    try:
        lang = detect(text)
    except LangDetectException:
        logger.debug("Language ACCEPT (detection failed): {}", job.title)
        return True

    if lang == "en":
        return True

    logger.debug("Language REJECT (detected '{}'): {}", lang, job.title)
    return False
