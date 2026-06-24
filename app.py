import json
import re
import sqlite3
import anthropic
import pandas as pd
import streamlit as st
import openai
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
    # Migrate existing DBs that predate the job_description column
    cols = {row[1] for row in conn.execute("PRAGMA table_info(evaluations)")}
    if "job_description" not in cols:
        conn.execute("ALTER TABLE evaluations ADD COLUMN job_description TEXT")
    conn.commit()
    return conn


def save_evaluation(company: str, job_title: str, gpt_verdict: str, claude_verdict: str,
                    synthesis: str, job_description: str = "") -> None:
    with _db() as conn:
        conn.execute(
            "INSERT INTO evaluations "
            "(created_at, company, job_title, gpt_verdict, claude_verdict, synthesis, status, job_description) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (datetime.now().strftime("%Y-%m-%d %H:%M"), company, job_title,
             gpt_verdict, claude_verdict, synthesis, "N/A", job_description),
        )


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


# --- Helpers ---

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

st.set_page_config(page_title="AI Job Evaluator", layout="wide")
st.title("AI Job Evaluator")

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


# --- Session state init ---
for key in ("job_description_area", "company_name", "company_research", "posting_age"):
    if key not in st.session_state:
        st.session_state[key] = ""

if "resume_area" not in st.session_state:
    st.session_state["resume_area"] = load_resume()


def _on_resume_change() -> None:
    save_resume(st.session_state["resume_area"])


# ============================================================
# TABS
# ============================================================
tab_eval, tab_pipeline = st.tabs(["Evaluator", "Pipeline"])

# ============================================================
# TAB 1 — EVALUATOR
# ============================================================
with tab_eval:
    col1, col2 = st.columns(2)

    with col1:
        resume = st.text_area(
            "Resume",
            height=400,
            placeholder="Paste resume here...",
            key="resume_area",
            on_change=_on_resume_change,
        )

    with col2:
        job_url = st.text_input("Job URL (optional)", placeholder="https://...")
        fetch_clicked = st.button("Fetch Job Description", use_container_width=True)

        if fetch_clicked:
            if not job_url.strip():
                st.warning("Enter a URL first.")
            elif "linkedin.com" in job_url.lower():
                st.error("LinkedIn blocks automated access. Try copying the URL from the company's careers page or the Apply button destination instead.")
            else:
                with st.spinner("Fetching job description..."):
                    text, error, posting_age = fetch_job_description(job_url.strip())
                if error:
                    st.error(error)
                else:
                    st.session_state["job_description_area"] = text
                    st.session_state["posting_age"] = posting_age
                    st.success("Job description fetched." + (f" · {posting_age}" if posting_age else ""))
                    if "TAVILY_API_KEY" in st.secrets:
                        with st.spinner("Researching company..."):
                            try:
                                name = extract_company_name(text, st.secrets["OPENAI_API_KEY"])
                                research = research_company(name, st.secrets["TAVILY_API_KEY"])
                                st.session_state["company_name"] = name
                                st.session_state["company_research"] = research
                            except Exception as e:
                                st.session_state["company_research"] = ""
                                st.caption(f"Company research unavailable: {e}")

        job_description = st.text_area(
            "Job Description",
            height=300,
            placeholder="Paste job description here, or fetch from a URL above...",
            key="job_description_area",
        )

        research_clicked = st.button("Research Company", use_container_width=True)
        if research_clicked:
            if not job_description.strip():
                st.warning("Paste a job description first.")
            elif "TAVILY_API_KEY" not in st.secrets:
                st.error("TAVILY_API_KEY is not set in secrets.toml.")
            else:
                with st.spinner("Researching company..."):
                    try:
                        name = extract_company_name(job_description, st.secrets["OPENAI_API_KEY"])
                        research = research_company(name, st.secrets["TAVILY_API_KEY"])
                        st.session_state["company_name"] = name
                        st.session_state["company_research"] = research
                        st.success(f"Research complete for {name}.")
                    except Exception as e:
                        st.error(f"Company research failed: {e}")

    if st.session_state["company_research"]:
        with st.expander(f"Company Research: {st.session_state['company_name']}", expanded=False):
            st.markdown(st.session_state["company_research"])

    if st.button("Evaluate Fit", type="primary", use_container_width=True):
        if not resume.strip() or not job_description.strip():
            st.warning("Please paste both a resume and a job description.")
        else:
            company_research = st.session_state.get("company_research", "")
            posting_age = st.session_state.get("posting_age", "")
            job_block = f"JOB DESCRIPTION{f' ({posting_age})' if posting_age else ''}:\n{job_description}"
            user_content = f"RESUME:\n{resume}\n\n{job_block}"
            if company_research:
                user_content = f"COMPANY RESEARCH:\n{company_research}\n\n" + user_content

            with st.spinner("Evaluating with GPT-4o and Claude in parallel..."):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    gpt_future = executor.submit(_run_gpt4o, st.secrets["OPENAI_API_KEY"], user_content)
                    claude_future = executor.submit(_run_claude, st.secrets["ANTHROPIC_API_KEY"], user_content)
                    gpt_result = gpt_future.result()
                    claude_result = claude_future.result()

            st.divider()
            eval_col1, eval_col2 = st.columns(2)
            with eval_col1:
                st.subheader("GPT-4o")
                st.markdown(gpt_result)
            with eval_col2:
                st.subheader("Claude Sonnet 4")
                st.markdown(claude_result)

            st.divider()
            with st.spinner("Synthesizing..."):
                synthesis = _run_synthesis(st.secrets["ANTHROPIC_API_KEY"], gpt_result, claude_result)
            st.subheader("Synthesis")
            st.markdown(synthesis)

            # --- Auto-save to pipeline ---
            with st.spinner("Saving to pipeline..."):
                try:
                    company = st.session_state.get("company_name", "").strip()
                    if not company:
                        company = extract_company_name(job_description, st.secrets["OPENAI_API_KEY"])
                    job_title = _extract_job_title(job_description, st.secrets["OPENAI_API_KEY"])
                    save_evaluation(
                        company=company,
                        job_title=job_title,
                        gpt_verdict=_extract_verdict(gpt_result),
                        claude_verdict=_extract_verdict(claude_result),
                        synthesis=synthesis,
                        job_description=job_description,
                    )
                    st.success("Saved to pipeline.")
                except Exception as e:
                    st.caption(f"Pipeline save failed: {e}")

# ============================================================
# TAB 2 — PIPELINE
# ============================================================
with tab_pipeline:
    st.header("Pipeline")

    df = load_evaluations()

    if df.empty:
        st.info("No evaluations yet. Run an evaluation to populate the pipeline.")
    else:
        # Keep id for updates but hide it from display
        display_df = df[["id", "created_at", "company", "job_title", "gpt_verdict", "claude_verdict", "status"]].copy()
        gap_series = df["gpt_verdict"].map(_verdict_score) - df["claude_verdict"].map(_verdict_score)
        display_df.insert(6, "gap", gap_series.map(lambda x: f"{x:+.1f}" if pd.notna(x) else ""))
        display_df.columns = ["id", "Date", "Company", "Job Title", "GPT-4o Verdict", "Claude Verdict", "Gap", "Status"]

        edited = st.data_editor(
            display_df,
            column_config={
                "id": None,  # hide the id column
                "Status": st.column_config.SelectboxColumn(
                    "Status",
                    options=PIPELINE_STATUSES,
                    required=True,
                ),
            },
            disabled=["Date", "Company", "Job Title", "GPT-4o Verdict", "Claude Verdict", "Gap"],
            hide_index=True,
            use_container_width=True,
        )

        # Persist any status changes
        changed = edited[edited["Status"] != display_df["Status"]]
        for _, row in changed.iterrows():
            update_status(int(row["id"]), row["Status"])
        if not changed.empty:
            st.toast(f"Updated {len(changed)} status{'es' if len(changed) > 1 else ''}.")

        # Detail view + delete
        st.divider()
        st.subheader("Evaluation details")
        options = {f"{r['company']} — {r['job_title']} ({r['created_at']})": i for i, r in df.iterrows()}
        detail_col, reeval_col, delete_col = st.columns([5, 1.5, 1])
        with detail_col:
            selected_label = st.selectbox("Select an evaluation", list(options.keys()), label_visibility="collapsed")
        with reeval_col:
            reeval_clicked = st.button("🔄 Re-evaluate", use_container_width=True)
        with delete_col:
            delete_clicked = st.button("🗑 Delete", use_container_width=True)

        if selected_label:
            selected_row = df.iloc[options[selected_label]]

            if delete_clicked:
                delete_evaluation(int(selected_row["id"]))
                st.toast(f"Deleted {selected_row['company']} — {selected_row['job_title']}.")
                st.rerun()

            if reeval_clicked:
                jd = selected_row.get("job_description", "") or ""
                resume_text = load_resume()
                if not jd.strip():
                    st.error("No job description stored for this entry — open it in the Evaluator tab and run a fresh evaluation.")
                elif not resume_text.strip():
                    st.error("No resume found. Paste your resume in the Evaluator tab first.")
                else:
                    user_content = f"RESUME:\n{resume_text}\n\nJOB DESCRIPTION:\n{jd}"
                    with st.spinner("Re-evaluating with GPT-4o and Claude in parallel..."):
                        with ThreadPoolExecutor(max_workers=2) as executor:
                            gpt_future = executor.submit(_run_gpt4o, st.secrets["OPENAI_API_KEY"], user_content)
                            claude_future = executor.submit(_run_claude, st.secrets["ANTHROPIC_API_KEY"], user_content)
                            gpt_result = gpt_future.result()
                            claude_result = claude_future.result()
                    with st.spinner("Synthesizing..."):
                        synthesis = _run_synthesis(st.secrets["ANTHROPIC_API_KEY"], gpt_result, claude_result)
                    update_evaluation(
                        row_id=int(selected_row["id"]),
                        gpt_verdict=_extract_verdict(gpt_result),
                        claude_verdict=_extract_verdict(claude_result),
                        synthesis=synthesis,
                    )
                    st.toast(f"Re-evaluated {selected_row['company']} — {selected_row['job_title']}.")
                    st.rerun()

            with st.expander("Full Synthesis", expanded=True):
                st.markdown(selected_row["synthesis"])
