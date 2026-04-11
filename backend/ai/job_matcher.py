from __future__ import annotations

from backend.ai.jd_analyzer import extract_required_skills
from backend.logger import logger
from backend.models import JobListing, ResumeData


def compute_match_score(
    resume: ResumeData,
    job: JobListing,
    target_skills: set[str] | None = None,
) -> float:
    """
    Score between 0.0 and 1.0 reflecting how well a resume matches a job.
    Combines skill overlap and keyword presence in experience text.
    """
    resume_skills = {s.lower() for s in resume.skills}
    job_skills = set(extract_required_skills(job.description))

    if not job_skills:
        title_words = {w.lower() for w in job.title.split() if len(w) > 2}
        if target_skills:
            skill_overlap = resume_skills & target_skills
            skill_score = len(skill_overlap) / max(len(target_skills), 1)
            title_overlap = title_words & resume_skills
            title_score = len(title_overlap) / max(len(title_words), 1)
            return min(max(skill_score, title_score, 0.5 * skill_score + 0.5 * title_score), 1.0)

        overlap = title_words & resume_skills
        return min(len(overlap) / max(len(title_words), 1), 1.0)

    skill_overlap = resume_skills & job_skills
    skill_score = len(skill_overlap) / len(job_skills) if job_skills else 0.0

    exp_text = " ".join(
        f"{e.title} {e.company} {e.description}" for e in resume.experience
    ).lower()
    exp_hits = sum(1 for s in job_skills if s in exp_text)
    exp_score = exp_hits / len(job_skills) if job_skills else 0.0

    combined = 0.7 * skill_score + 0.3 * exp_score
    return round(min(combined, 1.0), 3)


def filter_and_score_jobs(
    resume: ResumeData,
    jobs: list[JobListing],
    threshold: float = 0.5,
    search_context: str = "",
) -> tuple[list[JobListing], list[JobListing]]:
    """Return (matched, skipped) lists after scoring each job."""
    scored: list[JobListing] = []
    skipped: list[JobListing] = []
    target_skills = set(extract_required_skills(search_context))
    for job in jobs:
        score = compute_match_score(resume, job, target_skills=target_skills)
        job.match_score = score
        if score >= threshold:
            scored.append(job)
            logger.info(
                "MATCH %.2f — %s @ %s", score, job.title, job.company,
            )
        else:
            skipped.append(job)
            logger.info(
                "SKIP  %.2f — %s @ %s (threshold %.2f)",
                score, job.title, job.company, threshold,
            )
    scored.sort(key=lambda j: j.match_score, reverse=True)
    return scored, skipped
