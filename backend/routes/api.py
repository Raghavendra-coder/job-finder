from __future__ import annotations

import asyncio
import csv
import io
import math
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import JSONResponse, Response

from backend.ai.jd_analyzer import (
    analyze_job_description,
    extract_required_skills,
    extract_role_keywords,
)
from backend.ai.job_matcher import filter_and_score_jobs
from backend.auth.session_manager import (
    get_authenticated_portals,
    login_portals,
    release_portal_sessions,
)
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
INFINITE_SEARCH_PAGES_PER_CYCLE = 10
INFINITE_SEARCH_SLEEP_SECONDS = 30
MAX_SEARCH_PAGES_PER_RUN = 800

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


def validate_inputs(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []

    phone = str(data.get("phone_number", "")).strip()
    if not re.fullmatch(r"\d{10}", phone):
        errors.append("Phone number must be exactly 10 digits")

    country_code = str(data.get("country_code", "")).strip()
    if not re.fullmatch(r"\+\d{1,4}", country_code):
        errors.append("Country code must be like +91")

    if len(str(data.get("job_description", "")).strip()) < 10:
        errors.append("Job description is required")

    work_modes = [item for item in str(data.get("work_modes", "")).split(",") if item.strip()]
    if not work_modes:
        errors.append("Select at least one work mode")

    portals = [item for item in str(data.get("portals", "")).split(",") if item.strip()]
    if not portals:
        errors.append("Select at least one portal")

    max_applications = data.get("max_applications")
    try:
        if int(max_applications) < 1:
            errors.append("Max applications must be at least 1")
    except (TypeError, ValueError):
        errors.append("Max applications is invalid")

    numeric_rules = (
        ("current_ctc", "Current CTC must be greater than 0", lambda value: value > 0),
        ("expected_ctc", "Expected CTC must be greater than 0", lambda value: value > 0),
        ("total_experience", "Experience must be greater than or equal to 0", lambda value: value >= 0),
        ("notice_days", "Notice period must be greater than or equal to 0", lambda value: value >= 0),
    )
    for field_name, message, predicate in numeric_rules:
        raw = str(data.get(field_name, "")).strip()
        if raw == "":
            errors.append(message)
            continue
        try:
            value = float(raw.replace(",", ""))
        except ValueError:
            errors.append(message)
            continue
        if not predicate(value):
            errors.append(message)

    return errors


def _pages_per_search_run(request: JobSearchRequest) -> int:
    if request.infinite_search:
        return INFINITE_SEARCH_PAGES_PER_CYCLE
    estimated_pages = math.ceil(max(request.max_applications, 25) / 25)
    return max(3, min(estimated_pages, MAX_SEARCH_PAGES_PER_RUN))


def _record_application_result(session: SearchSession, app_log: ApplicationLog) -> None:
    session.applications.append(app_log)
    if app_log.status == "applied":
        session.jobs_applied += 1
    elif app_log.status in ("failed", "error"):
        session.errors += 1
    else:
        session.jobs_skipped += 1


async def _crawl_jobs_batch(
    session: SearchSession,
    request: JobSearchRequest,
    search_query: str,
    seen_job_urls: set[str],
) -> list[JobListing]:
    new_jobs: list[JobListing] = []
    pages_to_scan = _pages_per_search_run(request)

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
            max_pages=pages_to_scan,
            on_status=_on_status,
        )
        jobs = await crawler.run()

        unique_jobs = [job for job in jobs if job.url and job.url not in seen_job_urls]
        for job in unique_jobs:
            seen_job_urls.add(job.url)

        new_jobs.extend(unique_jobs)
        session.jobs_found += len(unique_jobs)
        session.logs.append(
            f"{portal.value}: {len(unique_jobs)} new unique job(s) this cycle"
        )

    return new_jobs


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


@router.post("/login-portals")
async def login_to_portals(portals: str = Form("linkedin")):
    if _active_task and not _active_task.done():
        return JSONResponse(
            {"error": "A search is already running. Stop it before logging in."},
            status_code=409,
        )

    portal_list = [JobPortal(p.strip()) for p in portals.split(",") if p.strip()]
    if not portal_list:
        return JSONResponse({"error": "Select at least one portal"}, status_code=400)

    logs: list[str] = []

    def _on_status(msg: str) -> None:
        logs.append(msg)

    results = await login_portals(portal_list, on_status=_on_status)
    return {
        "results": {portal.value: ok for portal, ok in results.items()},
        "all_success": all(results.values()),
        "logs": logs,
        "authenticated": [p.value for p in get_authenticated_portals()],
    }


@router.get("/login-status")
async def login_status():
    return {"authenticated": [p.value for p in get_authenticated_portals()]}


@router.post("/start-search")
async def start_search(
    job_description: str = Form(...),
    work_modes: str = Form("remote"),
    portals: str = Form("linkedin"),
    max_applications: int = Form(25),
    infinite_search: str = Form("false"),
    resume_path: str = Form(""),
    phone_number: str = Form(""),
    country_code: str = Form(""),
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
    session.infinite_search = False
    session.search_cycles = 0
    session.jobs_found = 0
    session.jobs_applied = 0
    session.jobs_skipped = 0
    session.errors = 0
    session.logs = []
    session.applications = []

    normalized_phone_number = re.sub(r"\D", "", phone_number).strip()
    normalized_country_code = country_code.strip()

    validation_errors = validate_inputs({
        "job_description": job_description,
        "work_modes": work_modes,
        "portals": portals,
        "max_applications": max_applications,
        "phone_number": normalized_phone_number,
        "country_code": normalized_country_code,
        "current_ctc": current_ctc,
        "expected_ctc": expected_ctc,
        "notice_days": notice_days,
        "total_experience": total_experience,
    })
    if validation_errors:
        return JSONResponse(
            {"status": "error", "error": validation_errors[0], "errors": validation_errors},
            status_code=400,
        )

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
        parsed_infinite_search = _parse_bool(
            infinite_search,
            "INFINITE_SEARCH",
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
        infinite_search=parsed_infinite_search,
        phone_number=normalized_phone_number,
        country_code=normalized_country_code,
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
    global _active_task
    session.status = "logging_in"
    session.infinite_search = request.infinite_search
    bot: AutoApplyBot | None = None

    try:
        session.logs.append(
            "Step 1: Log in to job portals — complete sign-in in each browser window"
        )
        login_results = await login_portals(
            request.portals,
            on_status=lambda msg, s=session: s.logs.append(msg),
        )
        failed_portals = [portal for portal, ok in login_results.items() if not ok]
        if failed_portals:
            session.status = "error"
            session.logs.append(
                "Login required before search can continue. Failed: "
                + ", ".join(portal.value for portal in failed_portals)
            )
            return

        session.logs.append("All portals logged in — starting job search")
        session.status = "running"

        resume_data = parse_resume(resume_path)
        session.logs.append("Resume parsed")

        search_query = _build_search_query(request.job_description)
        session.logs.append(f"Search query: {search_query}")
        session.logs.append(
            "Infinite search is enabled"
            if request.infinite_search
            else f"Max applications requested: {request.max_applications}"
        )
        session.logs.append(
            f"Pages per search cycle: {_pages_per_search_run(request)}"
        )

        bot = AutoApplyBot(
            resume_data=resume_data,
            resume_path=resume_path,
            job_description=request.job_description,
            phone_number=request.phone_number,
            country_code=request.country_code,
            current_ctc=request.current_ctc,
            expected_ctc=request.expected_ctc,
            notice_days=request.notice_days,
            total_experience=request.total_experience,
            is_immediate_joiner=request.is_immediate_joiner,
            on_status=lambda msg, s=session: s.logs.append(msg),
        )
        seen_job_urls: set[str] = set()
        cycle = 0

        while True:
            cycle += 1
            session.search_cycles = cycle
            session.logs.append(f"Search cycle {cycle} started")

            new_jobs = await _crawl_jobs_batch(session, request, search_query, seen_job_urls)
            session.logs.append(f"New jobs found this cycle: {len(new_jobs)}")

            matched, skipped_by_score = filter_and_score_jobs(
                resume_data,
                new_jobs,
                MATCH_THRESHOLD,
                search_context=request.job_description,
            )
            session.logs.append(
                f"Jobs matching threshold this cycle: {len(matched)} "
                f"(skipped {len(skipped_by_score)} below {MATCH_THRESHOLD:.0%})"
            )

            for job in skipped_by_score:
                skip_log = ApplicationLog(job=job, status="skipped")
                skip_log.error = f"match_score_{job.match_score:.2f}_below_threshold"
                _record_application_result(session, skip_log)

            remaining_slots = max(request.max_applications - session.jobs_applied, 0)
            to_apply = matched if request.infinite_search else matched[:remaining_slots]

            for job in to_apply:
                if not request.infinite_search and session.jobs_applied >= request.max_applications:
                    break
                app_log = await bot.apply_to_job(job)
                _record_application_result(session, app_log)

            if not request.infinite_search:
                session.status = "completed"
                session.logs.append(
                    f"Done — Applied: {session.jobs_applied}, "
                    f"Errors: {session.errors}, Skipped: {session.jobs_skipped}"
                )
                break

            session.logs.append(
                f"Cycle {cycle} complete — waiting {INFINITE_SEARCH_SLEEP_SECONDS}s before next search"
            )
            await asyncio.sleep(INFINITE_SEARCH_SLEEP_SECONDS)

    except asyncio.CancelledError:
        session.status = "stopped"
        session.logs.append("Search stopped by user")

    except Exception as exc:
        session.status = "error"
        session.logs.append(f"Fatal error: {exc}")
        logger.exception("Search task failed")

    finally:
        if bot is not None:
            try:
                await bot.close()
            except Exception:
                pass
        try:
            await release_portal_sessions()
        except Exception:
            pass
        current_task = asyncio.current_task()
        if _active_task is current_task:
            _active_task = None


@router.get("/status/{session_id}")
async def get_status(session_id: str):
    session = _sessions.get(session_id)
    if not session:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    return session.model_dump(mode="json")


@router.get("/status")
async def get_all_status():
    if not _sessions:
        return {"sessions": [], "active": False}

    latest = max(_sessions.values(), key=lambda s: s.session_id)
    return {
        "sessions": [s.model_dump(mode="json") for s in _sessions.values()],
        "active": _active_task is not None and not _active_task.done(),
        "latest": latest.model_dump(mode="json"),
    }


@router.post("/stop")
async def stop_search():
    global _active_task
    if _active_task and not _active_task.done():
        _active_task.cancel()
        _active_task = None
        try:
            await release_portal_sessions()
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


@router.get("/applications/export")
async def export_applications_csv():
    """Export all application records as a CSV download."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Job Title",
        "Company",
        "Portal",
        "Score",
        "Status",
        "URL",
        "Location",
        "Work Mode",
        "Error",
        "Session ID",
    ])

    for session in _sessions.values():
        for app in session.applications:
            job = app.job
            writer.writerow([
                job.title,
                job.company,
                job.portal.value,
                f"{job.match_score * 100:.0f}%",
                app.status,
                job.url,
                job.location,
                job.work_mode.value,
                app.error or "",
                session.session_id,
            ])

    filename = f"applications-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.csv"
    return Response(
        content=output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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
