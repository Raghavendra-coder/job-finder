from __future__ import annotations

import re

from backend.logger import logger

ROLE_KEYWORDS = [
    "engineer", "developer", "architect", "analyst", "scientist", "manager",
    "designer", "consultant", "administrator", "devops", "sre", "lead",
    "director", "intern", "associate", "senior", "junior", "principal",
    "full stack", "frontend", "backend", "data", "ml", "ai", "cloud",
    "mobile", "ios", "android", "qa", "test", "security", "product",
]


def extract_required_skills(jd_text: str) -> list[str]:
    """Pull technology / skill keywords from a job description."""
    from backend.parser.resume_parser import SKILL_KEYWORDS

    text_lower = jd_text.lower()
    found: list[str] = []
    for skill in SKILL_KEYWORDS:
        if re.search(rf"\b{re.escape(skill)}\b", text_lower):
            found.append(skill)
    return sorted(set(found))


def extract_role_keywords(jd_text: str) -> list[str]:
    text_lower = jd_text.lower()
    found: list[str] = []
    for kw in ROLE_KEYWORDS:
        if kw in text_lower:
            found.append(kw)
    return sorted(set(found))


def detect_work_mode(jd_text: str) -> str:
    text_lower = jd_text.lower()
    if "remote" in text_lower:
        return "remote"
    if "hybrid" in text_lower:
        return "hybrid"
    return "onsite"


def detect_job_type(jd_text: str) -> str:
    text_lower = jd_text.lower()
    if "part-time" in text_lower or "part time" in text_lower:
        return "Part-time"
    if "contract" in text_lower:
        return "Contract"
    if "internship" in text_lower or "intern" in text_lower:
        return "Internship"
    return "Full-time"


def analyze_job_description(jd_text: str) -> dict:
    logger.info("Analyzing job description (%d chars)", len(jd_text))
    return {
        "required_skills": extract_required_skills(jd_text),
        "role_keywords": extract_role_keywords(jd_text),
        "work_mode": detect_work_mode(jd_text),
        "job_type": detect_job_type(jd_text),
    }
