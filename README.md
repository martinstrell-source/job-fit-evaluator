# Job Fit Evaluator

A Streamlit app that evaluates how well a resume matches a job description using GPT-4o and Claude in parallel, synthesizes where the two models agree and disagree, and tracks your application pipeline.

![Job Fit Evaluator screenshot](docs/screenshot.png)

## The Problem

Reading job descriptions and honestly assessing fit is time-consuming and easy to get wrong — candidates over-index on surface matches and miss hard blockers, or talk themselves into applying when they shouldn't. This app gives a structured, direct evaluation from two independent AI models, then surfaces where they diverge and why.

## Features

- **URL fetching** — Paste a job URL instead of the full description. The app scrapes the page, extracts the relevant content, and detects the posting date.
- **Company research** — Automatically searches for funding stage, headcount, recent news, and layoffs via Tavily, and injects the context into the evaluation.
- **Dual model comparison** — GPT-4o and Claude Sonnet run in parallel against the same prompt. Results are displayed side by side.
- **Synthesis** — A third Claude call compares both evaluations: where they agree, where they disagree, and what explains the difference.
- **Pipeline tracker** — Every evaluation is saved automatically to a local SQLite database. Track application status (N/A → Applied → Phone Screen → Interview → Offer → Rejected), view full syntheses, re-evaluate with updated models, and delete stale entries.
- **Resume persistence** — The last pasted resume is saved locally and pre-filled on next launch.

## Tech Stack

| Layer | Tool |
|---|---|
| UI | Streamlit |
| Language models | OpenAI GPT-4o, Anthropic Claude Sonnet 4.6 |
| Company research | Tavily Search API |
| Web scraping | Requests, BeautifulSoup4 |
| Database | SQLite (via Python stdlib) |
| Language | Python 3.11+ |

## Running Locally

**1. Clone and install dependencies**

```bash
git clone <repo-url>
cd job-fit-evaluator
pip install -r requirements.txt
```

**2. Add API keys**

Create `.streamlit/secrets.toml` (already in `.gitignore`):

```toml
OPENAI_API_KEY    = "sk-..."
ANTHROPIC_API_KEY = "sk-ant-..."
TAVILY_API_KEY    = "tvly-..."
```

**3. Run**

```bash
./run.sh
```

`run.sh` launches the app using the project's own `.venv`, regardless of which environment is active in your shell. Running `streamlit run app.py` directly can pick up a system-wide Streamlit that lacks the dependencies (`ModuleNotFoundError: No module named 'openai'`); `./run.sh` avoids that.

The app opens at `http://localhost:8501`. The SQLite database and resume cache are stored in `~/.job-fit-evaluator/`.

## Automated polling

`poller.py` checks target companies' ATS boards for new product roles, auto-evaluates the genuinely new ones with the same dual-model logic, stores them in the pipeline, and alerts you on strong fits. It reuses the resume cached by the app, so paste your resume in the app once before the first run.

Edit the `TARGETS` list in `poller.py` to choose companies (each is an ATS plus a board slug; Greenhouse and Ashby are supported). The core logic lives in `evaluator.py` so both the app and the poller share it.

```bash
./.venv/bin/python poller.py --dry-run        # discover + filter only, no model calls
./.venv/bin/python poller.py --bay-area-only  # evaluate new Bay Area / remote postings + alert
./.venv/bin/python poller.py --limit 5        # cap how many new postings to evaluate this run
```

Flags: `--threshold N` (alert when the Claude score is ≥ N, default 7.0), `--bay-area-only`, `--limit N`, `--dry-run`, `--no-notify`. Same role posted in several locations is collapsed to one (preferring the Bay Area / remote copy), and a senior-product title filter drops junior and intern roles before any model calls.

Schedule it to run every few hours with cron or launchd, e.g.:

```cron
0 */4 * * * cd /path/to/job-fit-evaluator && ./.venv/bin/python poller.py --bay-area-only >> ~/.job-fit-evaluator/poller.log 2>&1
```

**Email alerts (optional).** Add to `.streamlit/secrets.toml`:

```toml
EMAIL_ADDRESS      = "you@gmail.com"
EMAIL_APP_PASSWORD = "abcd efgh ijkl mnop"   # Gmail App Password, not your login password
EMAIL_TO           = "you@gmail.com"          # optional; defaults to EMAIL_ADDRESS
```

Gmail needs an App Password (Google Account → Security → 2-Step Verification → App passwords). Without email configured, alerts fall back to a macOS desktop notification; either way they print to stdout.

## Evaluation Framework

Each evaluation covers: overall verdict with a 1–10 fit score, strong matches, gaps (hard blockers vs. bridgeable vs. preferred), level and culture fit, interview watch-outs, and framing recommendations. The prompt is in `prompt.py`.
