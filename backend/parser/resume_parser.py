from __future__ import annotations

import re
from pathlib import Path

import pdfplumber
from docx import Document

from backend.logger import logger
from backend.models import EducationEntry, ExperienceEntry, ResumeData

# Common skill keywords (extensible)
SKILL_KEYWORDS: set[str] = {
    "python", "java", "javascript", "typescript", "c++", "c#", "go", "rust",
    "ruby", "php", "swift", "kotlin", "scala", "r", "matlab", "sql", "nosql",
    "html", "css", "react", "angular", "vue", "next.js", "node.js", "express",
    "django", "flask", "fastapi", "spring", "rails", ".net",
    "aws", "azure", "gcp", "docker", "kubernetes", "terraform", "ansible",
    "jenkins", "ci/cd", "git", "linux", "nginx", "apache",
    "postgresql", "mysql", "mongodb", "redis", "elasticsearch", "dynamodb",
    "kafka", "rabbitmq", "graphql", "rest", "grpc", "microservices",
    "machine learning", "deep learning", "nlp", "computer vision",
    "tensorflow", "pytorch", "scikit-learn", "pandas", "numpy",
    "agile", "scrum", "jira", "confluence", "figma", "tableau", "power bi",
    "selenium", "playwright", "cypress", "jest", "pytest",
}

EDUCATION_PATTERNS = [
    r"(?i)(bachelor|master|ph\.?d|b\.?tech|m\.?tech|b\.?sc|m\.?sc|b\.?e|m\.?e|mba|b\.?a|m\.?a|diploma)",
]

SECTION_HEADERS = {
    "experience": [
        r"(?i)(?:work\s+)?experience",
        r"(?i)employment\s+history",
        r"(?i)professional\s+experience",
    ],
    "education": [
        r"(?i)education",
        r"(?i)academic",
        r"(?i)qualifications",
    ],
    "skills": [
        r"(?i)skills",
        r"(?i)technical\s+skills",
        r"(?i)core\s+competencies",
    ],
}


def extract_text_from_pdf(file_path: Path) -> str:
    text_parts: list[str] = []
    with pdfplumber.open(file_path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text)
    return "\n".join(text_parts)


def extract_text_from_docx(file_path: Path) -> str:
    doc = Document(str(file_path))
    return "\n".join(para.text for para in doc.paragraphs if para.text.strip())


def extract_text(file_path: Path) -> str:
    suffix = file_path.suffix.lower()
    if suffix == ".pdf":
        return extract_text_from_pdf(file_path)
    if suffix in (".docx", ".doc"):
        return extract_text_from_docx(file_path)
    return file_path.read_text(encoding="utf-8")


def extract_email(text: str) -> str:
    match = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
    return match.group(0) if match else ""


def extract_phone(text: str) -> str:
    match = re.search(r"[\+]?[\d\s\-().]{7,15}", text)
    return match.group(0).strip() if match else ""


def extract_name(text: str) -> str:
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if lines:
        candidate = lines[0]
        if len(candidate) < 60 and not re.search(r"@|http|www\.|\.com", candidate):
            return candidate
    return ""


def extract_skills(text: str) -> list[str]:
    text_lower = text.lower()
    found: list[str] = []
    for skill in SKILL_KEYWORDS:
        pattern = rf"\b{re.escape(skill)}\b"
        if re.search(pattern, text_lower):
            found.append(skill)
    return sorted(set(found))


def _find_section(text: str, section: str) -> str:
    """Extract a rough section of text between known headers."""
    patterns = SECTION_HEADERS.get(section, [])
    start_idx = -1
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            start_idx = m.end()
            break
    if start_idx == -1:
        return ""

    all_headers = []
    for sec, pats in SECTION_HEADERS.items():
        if sec == section:
            continue
        for pat in pats:
            for m2 in re.finditer(pat, text):
                if m2.start() > start_idx:
                    all_headers.append(m2.start())

    end_idx = min(all_headers) if all_headers else len(text)
    return text[start_idx:end_idx].strip()


def extract_experience(text: str) -> list[ExperienceEntry]:
    section = _find_section(text, "experience")
    if not section:
        return []

    entries: list[ExperienceEntry] = []
    blocks = re.split(r"\n{2,}", section)
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        lines = block.split("\n")
        title = lines[0].strip() if lines else ""
        company = lines[1].strip() if len(lines) > 1 else ""
        duration = ""
        for line in lines:
            if re.search(r"\d{4}", line) and re.search(r"[-–]", line):
                duration = line.strip()
                break
        description = " ".join(lines[2:]).strip() if len(lines) > 2 else ""
        if title:
            entries.append(ExperienceEntry(
                title=title, company=company, duration=duration, description=description,
            ))
    return entries


def extract_education(text: str) -> list[EducationEntry]:
    section = _find_section(text, "education")
    if not section:
        return []

    entries: list[EducationEntry] = []
    for pat in EDUCATION_PATTERNS:
        for m in re.finditer(pat, section):
            start = max(0, m.start() - 5)
            end = min(len(section), m.end() + 120)
            snippet = section[start:end].strip()
            lines = snippet.split("\n")
            degree = lines[0].strip()
            institution = lines[1].strip() if len(lines) > 1 else ""
            year_match = re.search(r"(19|20)\d{2}", snippet)
            year = year_match.group(0) if year_match else ""
            entries.append(EducationEntry(degree=degree, institution=institution, year=year))
    return entries


def parse_resume(file_path: Path) -> ResumeData:
    logger.info("Parsing resume: %s", file_path)
    raw_text = extract_text(file_path)
    if not raw_text.strip():
        logger.warning("Resume appears empty: %s", file_path)
        return ResumeData(raw_text="")

    return ResumeData(
        raw_text=raw_text,
        name=extract_name(raw_text),
        email=extract_email(raw_text),
        phone=extract_phone(raw_text),
        skills=extract_skills(raw_text),
        experience=extract_experience(raw_text),
        education=extract_education(raw_text),
        summary=raw_text[:500],
    )
