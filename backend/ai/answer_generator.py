from __future__ import annotations

import httpx

from backend.config import OLLAMA_BASE_URL, OLLAMA_MODEL
from backend.logger import logger
from backend.models import ResumeData

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url=OLLAMA_BASE_URL, timeout=90.0)
    return _client


SYSTEM_PROMPT = """\
You are an expert career coach helping a job applicant answer screening \
questions on job application forms. Your answers must be:
- Professional, concise, and directly relevant
- Tailored to maximize the applicant's chances of selection
- Based ONLY on the provided resume data and job description
- Honest — do not fabricate experience the applicant doesn't have
- Between 1-3 sentences unless the question asks for a longer response
"""


def _build_context(resume: ResumeData, job_description: str) -> str:
    skills = ", ".join(resume.skills) if resume.skills else "Not specified"
    experience_lines: list[str] = []
    for exp in resume.experience:
        experience_lines.append(
            f"- {exp.title} at {exp.company} ({exp.duration}): {exp.description}"
        )
    experience_text = "\n".join(experience_lines) if experience_lines else "Not specified"

    education_lines: list[str] = []
    for edu in resume.education:
        education_lines.append(f"- {edu.degree} from {edu.institution} ({edu.year})")
    education_text = "\n".join(education_lines) if education_lines else "Not specified"

    return (
        f"=== APPLICANT RESUME ===\n"
        f"Name: {resume.name}\n"
        f"Skills: {skills}\n"
        f"Experience:\n{experience_text}\n"
        f"Education:\n{education_text}\n"
        f"Summary: {resume.summary[:300]}\n\n"
        f"=== JOB DESCRIPTION ===\n{job_description[:1500]}"
    )


async def generate_answer(
    question: str,
    resume: ResumeData,
    job_description: str,
) -> str:
    """Generate an optimized answer for a screening question."""
    logger.info("Generating AI answer for: %s", question[:80])

    context = _build_context(resume, job_description)
    user_message = (
        f"{context}\n\n"
        f"=== SCREENING QUESTION ===\n{question}\n\n"
        f"Please provide the best possible answer for the applicant."
    )

    try:
        answer = await _chat_completion(
            user_message=user_message,
            temperature=0.4,
        )
        logger.info("AI answer generated (%d chars)", len(answer))
        return answer.strip()
    except Exception as exc:
        logger.error("AI answer generation failed: %s", exc)
        return ""


async def generate_cover_summary(
    resume: ResumeData,
    job_description: str,
) -> str:
    """Generate a short professional summary / cover blurb."""
    context = _build_context(resume, job_description)
    prompt = (
        f"{context}\n\n"
        "Write a 2-3 sentence professional summary explaining why "
        "this applicant is a great fit for this role."
    )

    try:
        return await _chat_completion(
            user_message=prompt,
            temperature=0.5,
        )
    except Exception as exc:
        logger.error("Cover summary generation failed: %s", exc)
        return ""


async def _chat_completion(user_message: str, temperature: float) -> str:
    client = _get_client()
    response = await client.post(
        "/api/chat",
        json={
            "model": OLLAMA_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            "stream": False,
            "options": {"temperature": temperature},
        },
    )
    response.raise_for_status()
    payload = response.json()
    return (payload.get("message", {}).get("content") or "").strip()
