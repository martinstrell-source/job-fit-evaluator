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

`poller.py` checks target companies' ATS boards for new product roles, auto-evaluates the genuinely new ones with the same dual-model logic, and stores them in the pipeline. Review matches in the app's Pipeline tab, which has a minimum-total filter and a Source column showing whether a row came from automation or a manual check. It reuses the resume cached by the app, so paste your resume in the app once before the first run.

Edit `targets.json` to choose companies (each is an ATS plus a board slug; Greenhouse and Ashby are supported). The core logic lives in `evaluator.py` so both the app and the poller share it.

```bash
./.venv/bin/python poller.py --dry-run        # discover + filter only, no model calls
./.venv/bin/python poller.py --bay-area-only  # evaluate new Bay Area / remote postings + alert
./.venv/bin/python poller.py --limit 5        # cap how many new postings to evaluate this run
```

Flags: `--threshold N` (the GPT + Claude total, out of 20, default 12, used to mark strong fits in the run output), `--bay-area-only`, `--limit N`, `--dry-run`, `--no-notify`. Same role posted in several locations is collapsed to one (preferring the Bay Area / remote copy), and a senior-product title filter drops junior and intern roles before any model calls.

Every evaluated posting is saved to the pipeline regardless of score (so dedup works and you keep the full record); the threshold only flags strong fits in the run output. Strong fits print an `[ALERT]` line to stdout / the poller log. Review everything in the app's Pipeline tab and use its minimum-total filter to focus on the strong ones.

Schedule it so it runs on its own (recommended over long manual runs, which can time out). On macOS, use the included launchd agent template `com.jobfitevaluator.poller.plist.example`: edit the paths, copy it to `~/Library/LaunchAgents/`, and `launchctl load -w` it. It runs daily at 8am and logs to `~/.job-fit-evaluator/poller.log`. Trigger a test run with `launchctl start com.jobfitevaluator.poller`. (cron works too: `0 8 * * * cd /path/to/job-fit-evaluator && ./.venv/bin/python poller.py --bay-area-only >> ~/.job-fit-evaluator/poller.log 2>&1`.)

Daily runs are tiny and cheap because dedup means only genuinely new postings get evaluated. The first run after adding several companies is the exception: it evaluates their whole current backlog at once, so run that one manually (optionally in chunks with `--limit`) and watch the Anthropic balance.

## Evaluation Framework

Each evaluation covers: overall verdict with a 1–10 fit score, strong matches, gaps (hard blockers vs. bridgeable vs. preferred), level and culture fit, interview watch-outs, and framing recommendations. The prompt is in `prompt.py`.
