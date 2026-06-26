"""Job posting sources. Each adapter returns a list of normalized postings:

    {
        "source_id":  "greenhouse:gleanwork:12345",  # stable, used for dedup
        "company":    "Glean",
        "title":      "Senior Product Manager",
        "location":   "San Francisco, CA",
        "url":        "https://...",
        "description": "plain-text job description",
    }

New sources (e.g. an Adzuna open-market feed) only need to return this shape to
plug into poller.py.
"""
import html
import re

import requests
from bs4 import BeautifulSoup

from evaluator import SCRAPE_HEADERS

# --- Title pre-filter (cheap gate before spending model calls) ---

_PRODUCT_RE = re.compile(
    r"\b(product manager|product management|head of product|product lead|"
    r"principal product|group product|director[^,]*product|product[^,]*director|"
    r"vp[^,]*product|chief product)\b",
    re.IGNORECASE,
)
_EXCLUDE_RE = re.compile(
    r"\b(intern|internship|new grad|university grad|early career|"
    r"associate product manager|\bapm\b|working student)\b",
    re.IGNORECASE,
)


def is_relevant_title(title: str) -> bool:
    """True for senior/lead/director-level product roles; drops junior and intern titles."""
    title = title or ""
    return bool(_PRODUCT_RE.search(title)) and not _EXCLUDE_RE.search(title)


# --- Location filter + cross-location de-duplication ---

_BAY_RE = re.compile(
    r"\b(san francisco|bay area|\bsf\b|oakland|palo alto|mountain view|san jose|"
    r"menlo park|sunnyvale|berkeley|redwood city|california|\bca\b|remote)\b",
    re.IGNORECASE,
)


def is_bay_or_remote(location: str) -> bool:
    """True if the location mentions the Bay Area, California, or remote."""
    return bool(_BAY_RE.search(location or ""))


def _norm_title(title: str) -> str:
    return re.sub(r"\s+", " ", (title or "").strip().lower())


def dedupe_by_role(postings: list[dict]) -> list[dict]:
    """Collapse the same role posted in multiple locations down to one entry,
    preferring the Bay Area / remote copy when there is a choice."""
    by_key: dict[tuple[str, str], dict] = {}
    for p in postings:
        key = (p["company"], _norm_title(p["title"]))
        current = by_key.get(key)
        if current is None:
            by_key[key] = p
        elif not is_bay_or_remote(current["location"]) and is_bay_or_remote(p["location"]):
            by_key[key] = p
    return list(by_key.values())


# --- Adapters ---

def fetch_greenhouse(slug: str, company: str) -> list[dict]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    resp = requests.get(url, headers=SCRAPE_HEADERS, timeout=20)
    resp.raise_for_status()
    out = []
    for j in resp.json().get("jobs", []):
        content_html = html.unescape(j.get("content") or "")
        desc = BeautifulSoup(content_html, "html.parser").get_text("\n", strip=True)
        out.append({
            "source_id": f"greenhouse:{slug}:{j.get('id')}",
            "company": company or j.get("company_name") or slug,
            "title": j.get("title") or "",
            "location": (j.get("location") or {}).get("name", ""),
            "url": j.get("absolute_url") or "",
            "description": desc,
        })
    return out


def fetch_ashby(slug: str, company: str) -> list[dict]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
    resp = requests.get(url, headers=SCRAPE_HEADERS, timeout=20)
    resp.raise_for_status()
    out = []
    for j in resp.json().get("jobs", []):
        if j.get("isListed") is False:
            continue
        desc = j.get("descriptionPlain") or BeautifulSoup(
            j.get("descriptionHtml") or "", "html.parser"
        ).get_text("\n", strip=True)
        loc = j.get("location")
        out.append({
            "source_id": f"ashby:{slug}:{j.get('id')}",
            "company": company or slug,
            "title": j.get("title") or "",
            "location": loc if isinstance(loc, str) else "",
            "url": j.get("jobUrl") or j.get("applyUrl") or "",
            "description": desc,
        })
    return out


_FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "ashby": fetch_ashby,
}


def fetch_source(ats: str, slug: str, company: str) -> list[dict]:
    if ats not in _FETCHERS:
        raise ValueError(f"Unknown ATS '{ats}'. Known: {', '.join(_FETCHERS)}")
    return _FETCHERS[ats](slug, company)
