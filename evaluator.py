"""Core job-fit evaluation logic, with no Streamlit dependency.

Both the Streamlit UI (app.py) and the headless poller (poller.py) import from
here. Nothing in this module touches streamlit, so it is safe to import from a
plain script or a cron job.
"""
import json
import os
import re
import sqlite3
import tomllib
import anthropic
import openai
import pandas as pd
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from prompt import SYSTEM_PROMPT

# --- Persistent storage paths ---
_DATA_DIR = Path.home() / ".job-fit-evaluator"
RESUME_CACHE = _DATA_DIR / "resume.txt"
DB_PATH = _DATA_DIR / "pipeline.db"

PIPELINE_STATUSES = ["N/A", "Applied", "Phone Screen", "Interview", "Offer", "Rejected"]


# --- Key loading (env first, then .streamlit/secrets.toml) ---

_CONFIG_NAMES = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "TAVILY_API_KEY",
    # Email alerting (used by notify.py); optional.
    "EMAIL_ADDRESS",       # sender, e.g. martin.strell@gmail.com
    "EMAIL_APP_PASSWORD",  # Gmail app password (not your normal password)
    "EMAIL_TO",            # recipient; defaults to EMAIL_ADDRESS if blank
    "SMTP_HOST",           # defaults to smtp.gmail.com
    "SMTP_PORT",           # defaults to 465
)


def get_keys() -> dict:
    """Return config for headless use. Checks environment variables first,
    then falls back to the same .streamlit/secrets.toml the UI uses. Missing
    values come back as empty strings."""
    cfg = {name: os.environ.get(name, "") for name in _CONFIG_NAMES}
    secrets_path = Path(__file__).parent / ".streamlit" / "secrets.toml"
    if secrets_path.exists():
        with secrets_path.open("rb") as f:
            secrets = tomllib.load(f)
        for name in cfg:
            if not cfg[name] and name in secrets:
                cfg[name] = str(secrets[name])
    return cfg


# --- Resume persistence ---

def save_resume(text: str) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    RESUME_CACHE.write_text(text, encoding="utf-8")


def load_resume() -> str:
    try:
        return RESUME_CACHE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


# --- SQLite pipeline ---

def _db() -> sqlite3.Connection:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS evaluations (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at      TEXT NOT NULL,
            company         TEXT,
            job_title       TEXT,
            gpt_verdict     TEXT,
            claude_verdict  TEXT,
            synthesis       TEXT,
            status          TEXT DEFAULT 'N/A',
            job_description TEXT
        )
    """)
    # Migrate older DBs that predate newer columns
    cols = {row[1] for row in conn.execute("PRAGMA table_info(evaluations)")}
    if "job_description" not in cols:
        conn.execute("ALTER TABLE evaluations ADD COLUMN job_description TEXT")
    if "source_id" not in cols:
        conn.execute("ALTER TABLE evaluations ADD COLUMN source_id TEXT")
    if "job_url" not in cols:
        conn.execute("ALTER TABLE evaluations ADD COLUMN job_url TEXT")
    conn.commit()
    return conn


def save_evaluation(company: str, job_title: str, gpt_verdict: str, claude_verdict: str,
                    synthesis: str, job_description: str = "",
                    source_id: str | None = None, job_url: str = "") -> None:
    with _db() as conn:
        conn.execute(
            "INSERT INTO evaluations "
            "(created_at, company, job_title, gpt_verdict, claude_verdict, synthesis, status, "
            "job_description, source_id, job_url) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (datetime.now().strftime("%Y-%m-%d %H:%M"), company, job_title,
             gpt_verdict, claude_verdict, synthesis, "N/A", job_description, source_id, job_url),
        )


def evaluation_exists(source_id: str) -> bool:
    """True if a posting with this source_id has already been evaluated (dedup)."""
    if not source_id:
        return False
    with _db() as conn:
        row = conn.execute(
            "SELECT 1 FROM evaluations WHERE source_id = ? LIMIT 1", (source_id,)
        ).fetchone()
    return row is not None


def load_evaluations() -> pd.DataFrame:
    with _db() as conn:
        df = pd.read_sql_query(
            "SELECT id, created_at, company, job_title, gpt_verdict, claude_verdict, "
            "synthesis, status, job_description "
            "FROM evaluations ORDER BY created_at DESC",
            conn,
        )
    return df


def update_status(row_id: int, status: str) -> None:
    with _db() as conn:
        conn.execute("UPDATE evaluations SET status = ? WHERE id = ?", (status, row_id))


def update_evaluation(row_id: int, gpt_verdict: str, claude_verdict: str, synthesis: str) -> None:
    with _db() as conn:
        conn.execute(
            "UPDATE evaluations SET gpt_verdict=?, claude_verdict=?, synthesis=?, created_at=? WHERE id=?",
            (gpt_verdict, claude_verdict, synthesis, datetime.now().strftime("%Y-%m-%d %H:%M"), row_id),
        )


def delete_evaluation(row_id: int) -> None:
    with _db() as conn:
        conn.execute("DELETE FROM evaluations WHERE id = ?", (row_id,))


# --- Verdict parsing ---

def _extract_score(text: str) -> str:
    """Pull a 1-10 fit score, tolerating '7/10', '7.5/10', '7 out of 10', 'score: 7', or a bare '7.5'."""
    patterns = (
        r"(\d{1,2}(?:\.\d)?)\s*/\s*10",
        r"(\d{1,2}(?:\.\d)?)\s+out of\s+10",
        r"score[:*\s]+(\d{1,2}(?:\.\d)?)",
        r"\b(\d\.\d)\b",
    )
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m and 0 < float(m.group(1)) <= 10:
            return m.group(1)
    return ""


def _extract_verdict(text: str) -> str:
    """Return '<score> · Apply' or '<score> · Don't Apply' (or whichever part is found)."""
    low = text.lower()
    if re.search(r"do\s*not\s*apply|don'?t\s*apply", low):
        label = "Don't Apply"
    elif re.search(r"\bapply\b", low) or any(v in low for v in ("strong fit", "moderate fit", "reach")):
        label = "Apply"
    else:
        label = ""
    score = _extract_score(text)
    if score and label:
        return f"{score} · {label}"
    return score or label or "Unknown"


def _verdict_score(verdict: str):
    """Parse the leading numeric score from a stored verdict like '8 · Apply' or '7.5 · Apply'."""
    if not verdict:
        return None
    m = re.match(r"\s*(\d{1,2}(?:\.\d)?)", verdict)
    if m and 0 < float(m.group(1)) <= 10:
        return float(m.group(1))
    return None


def _extract_job_title(job_text: str, openai_key: str) -> str:
    client = openai.OpenAI(api_key=openai_key)
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        max_tokens=20,
        temperature=0,
        messages=[
            {"role": "system", "content": "Extract the job title from the job description. Reply with only the job title, nothing else."},
            {"role": "user", "content": job_text[:1500]},
        ],
    )
    return response.choices[0].message.content.strip()


# --- Scraping ---

SCRAPE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_NOISE_TAGS = {"script", "style", "noscript", "header", "footer", "nav", "aside"}
_CONTENT_SELECTORS = [
    "[data-testid*='job-description']",
    "[class*='job-description']",
    "[class*='jobDescription']",
    "[class*='job_description']",
    "[id*='job-description']",
    "[id*='jobDescription']",
    "[class*='description']",
    "article",
    "main",
]


def _extract_text(soup: BeautifulSoup) -> str:
    for tag in soup(_NOISE_TAGS):
        tag.decompose()
    for selector in _CONTENT_SELECTORS:
        node = soup.select_one(selector)
        if node:
            text = node.get_text(separator="\n", strip=True)
            if len(text) > 200:
                return text
    body = soup.find("body")
    return (body or soup).get_text(separator="\n", strip=True)


def _extract_posting_date(soup: BeautifulSoup) -> str | None:
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, list):
                data = data[0]
            date_str = data.get("datePosted")
            if date_str:
                return date_str
        except Exception:
            pass
    time_el = soup.find("time", attrs={"datetime": True})
    if time_el:
        return time_el.get("datetime")
    return None


def _posting_age(date_str: str) -> str | None:
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        days = (datetime.now(timezone.utc) - dt).days
        if days < 1:
            return "posted today"
        if days == 1:
            return "posted 1 day ago"
        if days < 7:
            return f"posted {days} days ago"
        if days < 14:
            return "posted 1 week ago"
        if days < 30:
            return f"posted {days // 7} weeks ago"
        if days < 60:
            return "posted 1 month ago"
        return f"posted {days // 30} months ago"
    except Exception:
        return None


def fetch_job_description(url: str) -> tuple[str, str | None, str | None]:
    try:
        resp = requests.get(url, headers=SCRAPE_HEADERS, timeout=10)
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        if status in (403, 429, 401):
            return "", f"The page blocked automated access (HTTP {status}). Paste the job description manually.", None
        return "", f"HTTP error {status} fetching the URL.", None
    except requests.exceptions.ConnectionError:
        return "", "Could not connect to the URL. Check the address and your internet connection.", None
    except requests.exceptions.Timeout:
        return "", "The request timed out. Try again or paste the job description manually.", None
    except requests.exceptions.RequestException as e:
        return "", f"Failed to fetch URL: {e}", None

    soup = BeautifulSoup(resp.text, "html.parser")
    posting_age = None
    date_str = _extract_posting_date(soup)
    if date_str:
        posting_age = _posting_age(date_str)

    text = _extract_text(soup)
    lines = [ln.strip() for ln in text.splitlines()]
    cleaned = "\n".join(ln for ln in lines if ln)

    if len(cleaned) < 100:
        return "", "Could not extract meaningful text from the page. Paste the job description manually.", None

    return cleaned, None, posting_age


def extract_company_name(job_text: str, openai_key: str) -> str:
    client = openai.OpenAI(api_key=openai_key)
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        max_tokens=20,
        temperature=0,
        messages=[
            {"role": "system", "content": "Extract the company name from the job description. Reply with only the company name, nothing else."},
            {"role": "user", "content": job_text[:1500]},
        ],
    )
    return response.choices[0].message.content.strip()


def _tavily_search(query: str, api_key: str, max_results: int = 3, depth: str = "basic", include_answer: bool = False) -> dict:
    resp = requests.post(
        "https://api.tavily.com/search",
        json={
            "api_key": api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": depth,
            "include_answer": include_answer,
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def research_company(company_name: str, tavily_key: str) -> str:
    name_lower = company_name.lower()

    def snippets_by_title(results: list[dict]) -> list[str]:
        return [
            r["content"] for r in results
            if r.get("content") and name_lower in (r.get("title") or "").lower()
        ]

    answered_searches = {
        "Funding & stage": f"{company_name} funding stage valuation investors",
        "Team size": f"How many employees does {company_name} have?",
        "Recent news": f"{company_name} news announcement 2025",
    }
    sections = []
    for label, query in answered_searches.items():
        try:
            data = _tavily_search(query, tavily_key, max_results=3, include_answer=True)
            answer = data.get("answer", "")
            if answer and name_lower in answer.lower():
                sections.append(f"**{label}:** {answer}")
            else:
                snippets = snippets_by_title(data.get("results", []))
                if snippets:
                    sections.append(f"**{label}:** " + " | ".join(snippets[:2]))
        except Exception:
            pass

    try:
        data = _tavily_search(
            f"{company_name} tech company layoffs job cuts",
            tavily_key,
            max_results=5,
            depth="advanced",
            include_answer=True,
        )
        answer = data.get("answer", "")
        title_snippets = snippets_by_title(data.get("results", []))
        layoff_text = answer or (" | ".join(title_snippets[:2]) if title_snippets else "")
        if layoff_text and name_lower in layoff_text.lower():
            sections.append(f"**Layoffs:** {layoff_text}")
        else:
            sections.append(f"**Layoffs:** No specific layoff information found for {company_name}.")
    except Exception:
        pass

    return "\n\n".join(sections)


# --- Model evaluation ---

def _run_gpt4o(openai_key: str, user_content: str) -> str:
    client = openai.OpenAI(api_key=openai_key)
    response = client.chat.completions.create(
        model="gpt-4o",
        max_tokens=2000,
        temperature=0.2,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
    )
    return response.choices[0].message.content


def _run_claude(anthropic_key: str, user_content: str) -> str:
    client = anthropic.Anthropic(api_key=anthropic_key)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        temperature=0.2,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )
    return response.content[0].text


SYNTHESIS_PROMPT = """You are comparing two independent AI evaluations of the same resume and job description: one from GPT-4o and one from Claude.

Write a concise synthesis with three sections:

**Where they agree** — Points both evaluations reach the same conclusion. Note what the convergence signals.

**Where they disagree** — Specific claims or verdicts that differ between the two. Quote or closely paraphrase each model's position.

**What explains the differences** — Identify what drove the divergence: different weightings, different readings of ambiguous experience, one model catching something the other missed, etc.

Be direct and specific. Reference actual content from both evaluations."""


def _run_synthesis(anthropic_key: str, gpt_result: str, claude_result: str) -> str:
    client = anthropic.Anthropic(api_key=anthropic_key)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        system=SYNTHESIS_PROMPT,
        messages=[{
            "role": "user",
            "content": f"GPT-4o Evaluation:\n{gpt_result}\n\nClaude Evaluation:\n{claude_result}",
        }],
    )
    return response.content[0].text


def build_user_content(resume: str, job_description: str, company_research: str = "", posting_age: str = "") -> str:
    job_block = f"JOB DESCRIPTION{f' ({posting_age})' if posting_age else ''}:\n{job_description}"
    user_content = f"RESUME:\n{resume}\n\n{job_block}"
    if company_research:
        user_content = f"COMPANY RESEARCH:\n{company_research}\n\n" + user_content
    return user_content


def evaluate_fit(resume: str, job_description: str, openai_key: str, anthropic_key: str,
                 company_research: str = "", posting_age: str = "") -> dict:
    """Run GPT-4o and Claude in parallel against the same prompt, then synthesize.
    Returns the raw evaluations, the synthesis, and parsed verdicts/scores."""
    user_content = build_user_content(resume, job_description, company_research, posting_age)
    with ThreadPoolExecutor(max_workers=2) as executor:
        gpt_future = executor.submit(_run_gpt4o, openai_key, user_content)
        claude_future = executor.submit(_run_claude, anthropic_key, user_content)
        gpt_result = gpt_future.result()
        claude_result = claude_future.result()
    synthesis = _run_synthesis(anthropic_key, gpt_result, claude_result)
    gpt_verdict = _extract_verdict(gpt_result)
    claude_verdict = _extract_verdict(claude_result)
    return {
        "gpt_result": gpt_result,
        "claude_result": claude_result,
        "synthesis": synthesis,
        "gpt_verdict": gpt_verdict,
        "claude_verdict": claude_verdict,
        "gpt_score": _verdict_score(gpt_verdict),
        "claude_score": _verdict_score(claude_verdict),
    }
