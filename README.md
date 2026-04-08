# AI Job Search Automation System

An AI-powered system that crawls job portals (LinkedIn, Indeed, Naukri), matches jobs to your resume, and automatically fills applications using AI-generated answers.

## Features

- **Resume Parsing** — Extracts skills, experience, and education from PDF/DOCX resumes
- **Job Crawling** — Playwright-based crawlers for LinkedIn, Indeed, and Naukri
- **Smart Matching** — Scores jobs based on skill overlap and experience relevance
- **Auto Apply** — Fills application forms, uploads resume, answers screening questions with AI
- **AI Answers** — Uses local Ollama models to generate optimized screening question answers
- **Session Management** — Persists login cookies, handles manual login fallback
- **Dashboard** — Real-time status, logs, and application tracking
- **Human-like Behavior** — Random delays, realistic user agent, non-headless browser

## Project Structure

```
job-search-ai/
├── backend/
│   ├── ai/
│   │   ├── answer_generator.py   # Ollama-powered answer generation
│   │   ├── jd_analyzer.py        # Job description analysis
│   │   └── job_matcher.py        # Resume-to-job scoring
│   ├── auth/
│   │   └── session_manager.py    # Login flows & cookie persistence
│   ├── crawler/
│   │   ├── auto_apply.py         # Form-filling & application bot
│   │   ├── base_crawler.py       # Abstract crawler base class
│   │   ├── indeed_crawler.py     # Indeed job scraper
│   │   ├── linkedin_crawler.py   # LinkedIn job scraper
│   │   └── naukri_crawler.py     # Naukri job scraper
│   ├── parser/
│   │   └── resume_parser.py      # PDF/DOCX resume extraction
│   ├── routes/
│   │   └── api.py                # FastAPI endpoints
│   ├── config.py                 # Environment & settings
│   ├── logger.py                 # JSON + file logging
│   ├── main.py                   # App entry point
│   └── models.py                 # Pydantic data models
├── frontend/
│   └── index.html                # Single-page dashboard
├── .env.example                  # Environment template
├── requirements.txt              # Python dependencies
└── README.md
```

## Prerequisites

- Python 3.11+
- Ollama installed locally
- `gemma4` model pulled in Ollama
- Accounts on the job portals you want to use

## Setup

### 1. Clone and navigate

```bash
cd job-search-ai
```

### 2. Create virtual environment

```bash
python -m venv venv

# Windows
venv\Scripts\activate

# macOS/Linux
source venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Install Playwright browsers

```bash
playwright install chromium
```

### 5. Download spaCy model (optional — for advanced NLP)

```bash
python -m spacy download en_core_web_sm
```

### 6. Configure environment

```bash
copy .env.example .env
```

Edit `.env` with your credentials:

```env
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=gemma4
BROWSER_CONNECT_OVER_CDP=false
CHROME_CDP_URL=http://localhost:9222
LINKEDIN_EMAIL=you@example.com
LINKEDIN_PASSWORD=your-password
APPLICANT_NAME=Your Name
APPLICANT_EMAIL=you@example.com
APPLICANT_PHONE=+1234567890
```

Pull the model once:

```bash
ollama pull gemma4
```

## Running

Start the server from the `job-search-ai` directory:

```bash
python -m backend.main
```

The app starts at **http://localhost:8000**

Open your browser to see the dashboard.

## Usage

### Sample Test Flow

1. **Upload Resume** — Click "Upload & Parse" and select your PDF or DOCX resume. The parser extracts your name, contact info, and skills.

2. **Set Preferences** — Enter job keywords or paste a full job description, e.g.:
   ```
   Senior Python Developer with AWS, Docker, and FastAPI experience
   ```

3. **Select Filters** — Choose work mode (Remote/Hybrid/Onsite) and portals (LinkedIn/Indeed/Naukri).

4. **Start Search** — Click "Start Job Search". The system will:
   - Open a browser window (non-headless) for each portal
   - Log in automatically or wait for manual login if verification is needed
   - Search for matching full-time jobs
   - Score each job against your resume
   - Apply to jobs above the match threshold
   - Fill forms and answer screening questions using AI

5. **Monitor** — Watch real-time logs, stats, and the applications table in the dashboard.

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/upload-resume` | Upload and parse a resume file |
| POST | `/api/analyze-jd` | Analyze a job description |
| POST | `/api/start-search` | Start the crawl + apply pipeline |
| GET | `/api/status/{id}` | Get session status |
| GET | `/api/status` | Get all sessions |
| POST | `/api/stop` | Stop the active search |
| GET | `/api/logs` | Get application log entries |
| POST | `/api/logs/clear` | Clear all logs |
| GET | `/api/dashboard` | Aggregated dashboard stats |

## Configuration

All settings are in `.env`:

| Variable | Description | Default |
|----------|-------------|---------|
| `OLLAMA_BASE_URL` | Local Ollama server URL | `http://localhost:11434` |
| `OLLAMA_MODEL` | Ollama model used for answer generation | `gemma4` |
| `BROWSER_CONNECT_OVER_CDP` | Connect Playwright to an existing Chrome window | `false` |
| `CHROME_CDP_URL` | Chrome DevTools endpoint when CDP mode is enabled | `http://localhost:9222` |
| `MATCH_THRESHOLD` | Minimum match score to apply (0.0-1.0) | `0.5` |
| `MIN_DELAY` / `MAX_DELAY` | Random delay range in seconds | `2` / `5` |
| `PROXY_URL` | HTTP proxy for browser (optional) | — |

### Use Existing Chrome Window (CDP mode)

If you want login tabs to open in a Chrome window that is already running, start Chrome with remote debugging enabled and turn on CDP mode:

1. Start Chrome with debugging:
   - macOS:
     ```bash
     /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222
     ```
   - Windows:
     ```bash
     "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222
     ```
2. Set in `.env`:
   ```env
   BROWSER_CONNECT_OVER_CDP=true
   CHROME_CDP_URL=http://localhost:9222
   ```
3. Start the app. Playwright will attach to that Chrome instance and open tabs there instead of a separate browser process.

## Authentication Handling

The system attempts automatic login with your credentials. If 2FA or CAPTCHA is detected:

1. The browser window stays open
2. The system pauses and waits up to 120 seconds
3. Complete the verification manually in the browser
4. The system detects the login and continues
5. Cookies are saved for future sessions

## Important Notes

- The browser runs in **non-headless mode** so you can intervene if needed
- Random delays (2-5s) are added between actions to appear human-like
- Only **full-time** jobs are targeted (filtered at the URL level)
- Job portals frequently change their DOM — selectors may need updating
- Use responsibly and in compliance with each portal's terms of service
- Proxy support is available via the `PROXY_URL` environment variable
