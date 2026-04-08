from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent

UPLOADS_DIR = BASE_DIR / "uploads"
LOGS_DIR = BASE_DIR / "logs"
SESSIONS_DIR = BASE_DIR / "sessions"

for d in (UPLOADS_DIR, LOGS_DIR, SESSIONS_DIR):
    d.mkdir(exist_ok=True)

# --- Ollama -----------------------------------------------------------------
OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "gemma4")

# --- Portal credentials -----------------------------------------------------
LINKEDIN_EMAIL: str = os.getenv("LINKEDIN_EMAIL", "")
LINKEDIN_PASSWORD: str = os.getenv("LINKEDIN_PASSWORD", "")

INDEED_EMAIL: str = os.getenv("INDEED_EMAIL", "")
INDEED_PASSWORD: str = os.getenv("INDEED_PASSWORD", "")

NAUKRI_EMAIL: str = os.getenv("NAUKRI_EMAIL", "")
NAUKRI_PASSWORD: str = os.getenv("NAUKRI_PASSWORD", "")

# --- Applicant defaults ------------------------------------------------------
APPLICANT_NAME: str = os.getenv("APPLICANT_NAME", "")
APPLICANT_EMAIL: str = os.getenv("APPLICANT_EMAIL", "")
APPLICANT_PHONE: str = os.getenv("APPLICANT_PHONE", "")

# --- Server ------------------------------------------------------------------
HOST: str = os.getenv("HOST", "0.0.0.0")
PORT: int = int(os.getenv("PORT", "8000"))

# --- Proxy -------------------------------------------------------------------
PROXY_URL: str = os.getenv("PROXY_URL", "")
BROWSER_CONNECT_OVER_CDP: bool = os.getenv("BROWSER_CONNECT_OVER_CDP", "false").lower() in (
    "1", "true", "yes", "on",
)
CHROME_CDP_URL: str = os.getenv("CHROME_CDP_URL", "http://localhost:9222")

# --- Matching ----------------------------------------------------------------
MATCH_THRESHOLD: float = float(os.getenv("MATCH_THRESHOLD", "0.5"))

# --- Rate limiting -----------------------------------------------------------
MIN_DELAY: float = float(os.getenv("MIN_DELAY", "2"))
MAX_DELAY: float = float(os.getenv("MAX_DELAY", "5"))
