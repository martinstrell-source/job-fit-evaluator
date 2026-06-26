"""Poll target companies' ATS boards, auto-evaluate genuinely new product
postings with the dual-model evaluator, store them, and alert on strong fits.

Run once:
    ./.venv/bin/python poller.py --dry-run     # discover only, no model calls
    ./.venv/bin/python poller.py               # evaluate new postings + alert

Schedule it with cron / launchd / the schedule skill to run every few hours.
The resume is read from the same local cache the app uses, so paste your resume
in the app once before the first run.
"""
import argparse

from evaluator import (
    get_keys,
    load_resume,
    evaluate_fit,
    save_evaluation,
    evaluation_exists,
    research_company,
)
from sources import fetch_source, is_relevant_title, dedupe_by_role, is_bay_or_remote
from notify import notify

# Named targets. Add a company by dropping in its ATS + board slug.
TARGETS = [
    {"ats": "ashby", "slug": "harvey", "company": "Harvey"},
    {"ats": "greenhouse", "slug": "gleanwork", "company": "Glean"},
    {"ats": "greenhouse", "slug": "liberate", "company": "Liberate"},
    {"ats": "greenhouse", "slug": "faire", "company": "Faire"},
    {"ats": "ashby", "slug": "zip", "company": "Zip"},
]


def discover() -> list[dict]:
    """Fetch all targets and return relevant product postings."""
    candidates = []
    for t in TARGETS:
        try:
            postings = fetch_source(t["ats"], t["slug"], t["company"])
        except Exception as e:
            print(f"  ! {t['company']} fetch failed: {e}")
            continue
        relevant = [p for p in postings if is_relevant_title(p["title"])]
        print(f"{t['company']}: {len(postings)} postings, {len(relevant)} product roles")
        candidates.extend(relevant)
    return candidates


def main() -> None:
    ap = argparse.ArgumentParser(description="Poll target ATS boards and auto-evaluate new product postings.")
    ap.add_argument("--threshold", type=float, default=7.0, help="Alert when the Claude score is >= this (default 7.0)")
    ap.add_argument("--limit", type=int, default=0, help="Max new postings to evaluate this run (0 = no limit)")
    ap.add_argument("--dry-run", action="store_true", help="List new matching postings but make no model calls")
    ap.add_argument("--no-notify", action="store_true", help="Skip notifications (still prints)")
    ap.add_argument("--bay-area-only", action="store_true", help="Only evaluate Bay Area / remote postings")
    args = ap.parse_args()

    keys = get_keys()
    if not keys["OPENAI_API_KEY"] or not keys["ANTHROPIC_API_KEY"]:
        raise SystemExit("Missing OPENAI_API_KEY / ANTHROPIC_API_KEY (set env vars or .streamlit/secrets.toml).")
    resume = load_resume()
    if not resume.strip():
        raise SystemExit("No cached resume. Paste your resume in the app once, then re-run the poller.")

    candidates = discover()

    before = len(candidates)
    candidates = dedupe_by_role(candidates)
    print(f"\nCollapsed {before} -> {len(candidates)} after de-duping the same role across locations.")
    if args.bay_area_only:
        candidates = [p for p in candidates if is_bay_or_remote(p["location"])]
        print(f"{len(candidates)} remain after the Bay Area / remote filter.")

    new = [p for p in candidates if not evaluation_exists(p["source_id"])]
    print(f"\n{len(new)} new (not yet evaluated) product postings:")
    for p in new:
        print(f"  - {p['company']}: {p['title']}  ({p['location']})")

    if args.dry_run or not new:
        return
    if args.limit:
        new = new[: args.limit]

    print(f"\nEvaluating {len(new)} posting(s)...\n")
    research_cache: dict[str, str] = {}
    alerts = 0
    for p in new:
        comp = p["company"]
        if comp not in research_cache:
            research = ""
            if keys["TAVILY_API_KEY"]:
                try:
                    research = research_company(comp, keys["TAVILY_API_KEY"])
                except Exception:
                    research = ""
            research_cache[comp] = research
        research = research_cache[comp]

        try:
            res = evaluate_fit(
                resume, p["description"],
                keys["OPENAI_API_KEY"], keys["ANTHROPIC_API_KEY"],
                company_research=research,
            )
        except Exception as e:
            print(f"  ! eval failed for {comp} — {p['title']}: {e}")
            continue

        save_evaluation(
            company=comp,
            job_title=p["title"],
            gpt_verdict=res["gpt_verdict"],
            claude_verdict=res["claude_verdict"],
            synthesis=res["synthesis"],
            job_description=p["description"],
            source_id=p["source_id"],
            job_url=p["url"],
        )

        gs, cs = res["gpt_score"], res["claude_score"]
        gap = (gs - cs) if (gs is not None and cs is not None) else None
        gap_str = f" (gap {gap:+.1f})" if gap is not None else ""
        print(f"  • {comp} — {p['title']}: GPT {gs} / Claude {cs}{gap_str}")

        if cs is not None and cs >= args.threshold:
            alerts += 1
            if args.no_notify:
                print(f"    [strong fit] Claude {cs}, GPT {gs}  {p['url']}")
            else:
                notify(f"Strong fit: {comp}", f"Claude {cs}, GPT {gs} — {p['title']}", p["url"])

    print(f"\nDone. {len(new)} evaluated, {alerts} above threshold ({args.threshold}).")


if __name__ == "__main__":
    main()
