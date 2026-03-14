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


def passes_language_filter(job: Job) -> bool:
    """Return True if the job appears to be in English (or detection is uncertain)."""
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
