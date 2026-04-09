from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import JSONResponse

from backend.ai.jd_analyzer import (
    analyze_job_description,
    extract_required_skills,
    extract_role_keywords,
)
from backend.ai.job_matcher import filter_and_score_jobs
from backend.auth.session_manager import close_browser
from backend.config import MATCH_THRESHOLD, UPLOADS_DIR
from backend.crawler.auto_apply import AutoApplyBot
from backend.crawler.indeed_crawler import IndeedCrawler
from backend.crawler.linkedin_crawler import LinkedInCrawler
from backend.crawler.naukri_crawler import NaukriCrawler
from backend.logger import clear_logs, get_log_entries, log_event, logger
from backend.models import (
    ApplicationLog,
    JobListing,
    JobPortal,
    JobSearchRequest,
    SearchSession,
    WorkMode,
)
from backend.parser.resume_parser import parse_resume

router = APIRouter(prefix="/api")

_sessions: dict[str, SearchSession] = {}
_active_task: asyncio.Task | None = None

CRAWLER_MAP = {
    JobPortal.LINKEDIN: LinkedInCrawler,
    JobPortal.INDEED: IndeedCrawler,
    JobPortal.NAUKRI: NaukriCrawler,
}


def _get_session(session_id: str) -> SearchSession:
    if session_id not in _sessions:
        _sessions[session_id] = SearchSession(session_id=session_id)
    return _sessions[session_id]


def _build_search_query(job_description: str) -> str:
    skills = extract_required_skills(job_description)
    roles = extract_role_keywords(job_description)
    query_parts = (roles[:3] + skills[:5])
    if query_parts:
        return " ".join(dict.fromkeys(query_parts))

    first_line = job_description.splitlines()[0] if job_description.strip() else ""
    return first_line[:80] or job_description[:80]


def _parse_optional_float(raw: str, field_name: str) -> float | None:
    value = raw.strip()
    if not value:
        return None
    try:
        parsed = float(value.replace(",", ""))
    except ValueError as exc:
        raise ValueError(f"Invalid {field_name}") from exc
    if parsed < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return parsed


def _parse_bool(raw: str, field_name: str) -> bool:
    value = raw.strip().lower()
    if value in {"", "0", "false", "no", "off"}:
        return False
    if value in {"1", "true", "yes", "on"}:
        return True
    raise ValueError(f"Invalid {field_name}")


@router.post("/upload-resume")
async def upload_resume(file: UploadFile = File(...)):
    if not file.filename:
        return JSONResponse({"error": "No file provided"}, status_code=400)

    suffix = Path(file.filename).suffix.lower()
    if suffix not in (".pdf", ".docx", ".doc", ".txt"):
        return JSONResponse(
            {"error": "Unsupported file type. Use PDF, DOCX, or TXT."},
            status_code=400,
        )

    save_path = UPLOADS_DIR / f"resume{suffix}"
    content = await file.read()
    save_path.write_bytes(content)

    resume_data = parse_resume(save_path)
    return {
        "message": "Resume parsed successfully",
        "resume_path": str(save_path),
        "data": resume_data.model_dump(),
    }


@router.post("/analyze-jd")
async def analyze_jd(job_description: str = Form(...)):
    analysis = analyze_job_description(job_description)
    return {"analysis": analysis}


@router.post("/start-search")
async def start_search(
    job_description: str = Form(...),
    work_modes: str = Form("remote"),
    portals: str = Form("linkedin"),
    max_applications: int = Form(25),
    resume_path: str = Form(""),
    current_ctc: str = Form(""),
    expected_ctc: str = Form(""),
    notice_days: str = Form(""),
    total_experience: str = Form(""),
    is_immediate_joiner: str = Form("false"),
):
    global _active_task

    if _active_task and not _active_task.done():
        return JSONResponse(
            {"error": "A search is already running. Wait or stop it first."},
            status_code=409,
        )

    session_id = str(uuid.uuid4())[:8]
    session = _get_session(session_id)
    session.status = "starting"

    wm_list = [WorkMode(m.strip()) for m in work_modes.split(",") if m.strip()]
    portal_list = [JobPortal(p.strip()) for p in portals.split(",") if p.strip()]

    try:
        parsed_current_ctc = _parse_optional_float(current_ctc, "CURRENT_CTC")
        parsed_expected_ctc = _parse_optional_float(expected_ctc, "EXPECTED_CTC")
        parsed_notice_days = _parse_optional_float(notice_days, "NOTICE_DAYS")
        parsed_total_experience = _parse_optional_float(
            total_experience,
            "TOTAL_EXPERIENCE",
        )
        parsed_is_immediate_joiner = _parse_bool(
            is_immediate_joiner,
            "IS_IMMEDIATE_JOINER",
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    if not resume_path:
        for ext in (".pdf", ".docx", ".doc", ".txt"):
            candidate = UPLOADS_DIR / f"resume{ext}"
            if candidate.exists():
                resume_path = str(candidate)
                break

    if not resume_path:
        return JSONResponse(
            {"error": "No resume found. Upload a resume first."},
            status_code=400,
        )

    request = JobSearchRequest(
        job_description=job_description,
        work_modes=wm_list,
        portals=portal_list,
        max_applications=max_applications,
        current_ctc=parsed_current_ctc,
        expected_ctc=parsed_expected_ctc,
        notice_days=parsed_notice_days,
        total_experience=parsed_total_experience,
        is_immediate_joiner=parsed_is_immediate_joiner,
    )

    _active_task = asyncio.create_task(
        _run_search(session, request, Path(resume_path))
    )

    return {"session_id": session_id, "status": "started"}


async def _run_search(
    session: SearchSession,
    request: JobSearchRequest,
    resume_path: Path,
) -> None:
    session.status = "running"

    try:
        resume_data = parse_resume(resume_path)
        session.logs.append("Resume parsed")

        all_jobs: list[JobListing] = []
        search_query = _build_search_query(request.job_description)
        session.logs.append(f"Search query: {search_query}")

        for portal in request.portals:
            crawler_cls = CRAWLER_MAP.get(portal)
            if not crawler_cls:
                session.logs.append(f"Unsupported portal: {portal.value}")
                continue

            def _on_status(msg: str, s=session):
                s.logs.append(msg)

            crawler = crawler_cls(
                search_query=search_query,
                work_modes=request.work_modes,
                on_status=_on_status,
            )
            jobs = await crawler.run()
            all_jobs.extend(jobs)
            session.jobs_found += len(jobs)

        session.logs.append(f"Total jobs found: {len(all_jobs)}")

        matched = filter_and_score_jobs(
            resume_data,
            all_jobs,
            MATCH_THRESHOLD,
            search_context=request.job_description,
        )
        session.logs.append(f"Jobs matching threshold: {len(matched)}")

        apply_limit = min(request.max_applications, len(matched))
        to_apply = matched[:apply_limit]

        bot = AutoApplyBot(
            resume_data=resume_data,
            resume_path=resume_path,
            job_description=request.job_description,
            current_ctc=request.current_ctc,
            expected_ctc=request.expected_ctc,
            notice_days=request.notice_days,
            total_experience=request.total_experience,
            is_immediate_joiner=request.is_immediate_joiner,
            on_status=lambda msg, s=session: s.logs.append(msg),
        )

        for job in to_apply:
            app_log = await bot.apply_to_job(job)
            session.applications.append(app_log)
            if app_log.status == "applied":
                session.jobs_applied += 1
            elif app_log.status in ("failed", "error"):
                session.errors += 1
            else:
                session.jobs_skipped += 1

        session.status = "completed"
        session.logs.append(
            f"Done — Applied: {session.jobs_applied}, "
            f"Errors: {session.errors}, Skipped: {session.jobs_skipped}"
        )

    except Exception as exc:
        session.status = "error"
        session.logs.append(f"Fatal error: {exc}")
        logger.exception("Search task failed")

    finally:
        try:
            await close_browser()
        except Exception:
            pass


@router.get("/status/{session_id}")
async def get_status(session_id: str):
    session = _sessions.get(session_id)
    if not session:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    return session.model_dump()


@router.get("/status")
async def get_all_status():
    if not _sessions:
        return {"sessions": [], "active": False}

    latest = max(_sessions.values(), key=lambda s: s.session_id)
    return {
        "sessions": [s.model_dump() for s in _sessions.values()],
        "active": _active_task is not None and not _active_task.done(),
        "latest": latest.model_dump(),
    }


@router.post("/stop")
async def stop_search():
    global _active_task
    if _active_task and not _active_task.done():
        _active_task.cancel()
        _active_task = None
        try:
            await close_browser()
        except Exception:
            pass
        return {"message": "Search stopped"}
    return {"message": "No active search"}


@router.get("/logs")
async def get_logs():
    return {"logs": get_log_entries()}


@router.post("/logs/clear")
async def clear_all_logs():
    clear_logs()
    return {"message": "Logs cleared"}


@router.get("/dashboard")
async def dashboard():
    """Aggregated stats for the dashboard."""
    total_found = sum(s.jobs_found for s in _sessions.values())
    total_applied = sum(s.jobs_applied for s in _sessions.values())
    total_errors = sum(s.errors for s in _sessions.values())
    total_skipped = sum(s.jobs_skipped for s in _sessions.values())

    recent_apps: list[dict[str, Any]] = []
    for s in _sessions.values():
        for app in s.applications:
            recent_apps.append({
                "title": app.job.title,
                "company": app.job.company,
                "portal": app.job.portal.value,
                "status": app.status,
                "score": app.job.match_score,
                "url": app.job.url,
            })

    return {
        "total_found": total_found,
        "total_applied": total_applied,
        "total_errors": total_errors,
        "total_skipped": total_skipped,
        "active": _active_task is not None and not _active_task.done(),
        "recent_applications": recent_apps[-50:],
    }
