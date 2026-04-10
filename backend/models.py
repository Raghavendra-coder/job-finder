from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class WorkMode(str, Enum):
    ONSITE = "onsite"
    HYBRID = "hybrid"
    REMOTE = "remote"


class JobPortal(str, Enum):
    LINKEDIN = "linkedin"
    INDEED = "indeed"
    NAUKRI = "naukri"


class JobSearchRequest(BaseModel):
    job_description: str = Field(..., min_length=10)
    work_modes: list[WorkMode] = Field(default=[WorkMode.REMOTE])
    portals: list[JobPortal] = Field(default=[JobPortal.LINKEDIN])
    max_applications: int = Field(default=25, ge=1, le=20000)
    infinite_search: bool = False
    phone_number: str = ""
    country_code: str = ""
    current_ctc: Optional[float] = Field(default=None, ge=0)
    expected_ctc: Optional[float] = Field(default=None, ge=0)
    notice_days: Optional[float] = Field(default=None, ge=0)
    total_experience: Optional[float] = Field(default=None, ge=0)
    is_immediate_joiner: bool = False


class ResumeData(BaseModel):
    raw_text: str = ""
    name: str = ""
    email: str = ""
    phone: str = ""
    skills: list[str] = Field(default_factory=list)
    experience: list[ExperienceEntry] = Field(default_factory=list)
    education: list[EducationEntry] = Field(default_factory=list)
    summary: str = ""


class ExperienceEntry(BaseModel):
    title: str = ""
    company: str = ""
    duration: str = ""
    description: str = ""


class EducationEntry(BaseModel):
    degree: str = ""
    institution: str = ""
    year: str = ""


# Rebuild ResumeData so forward refs resolve
ResumeData.model_rebuild()


class JobListing(BaseModel):
    title: str
    company: str
    location: str = ""
    url: str
    portal: JobPortal
    work_mode: WorkMode = WorkMode.ONSITE
    job_type: str = "Full-time"
    description: str = ""
    match_score: float = 0.0
    applied: bool = False
    error: Optional[str] = None


class ApplicationLog(BaseModel):
    job: JobListing
    status: str = "pending"
    answers: dict[str, str] = Field(default_factory=dict)
    error: Optional[str] = None


class SearchSession(BaseModel):
    session_id: str
    status: str = "idle"
    infinite_search: bool = False
    search_cycles: int = 0
    jobs_found: int = 0
    jobs_applied: int = 0
    jobs_skipped: int = 0
    errors: int = 0
    logs: list[str] = Field(default_factory=list)
    applications: list[ApplicationLog] = Field(default_factory=list)
