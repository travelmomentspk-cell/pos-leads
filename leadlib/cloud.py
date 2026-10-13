"""Helpers for the cloud runner: Make webhook posting, status cell, decision-maker lookup through the official
Google Programmable Search API (public snippets only), and a short memory of recently rejected businesses."""
import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from .quality import NOT_NAME, TITLES, classify_title

NEW_LEAD_COLS = ["Business", "Decision Maker", "Role", "Phone", "Email", "Website", "Score", "Reason", "Source", "Date",
                 "Services to pitch", "Status"]
MASTER_COLS = ["Business", "Decision Maker", "Role", "Phone", "Email", "Website", "Score", "Source", "Date", "Run ID"]
REJECT_COLS = ["Business", "City", "Reason", "Score", "Source", "Date", "Run ID"]
RUNLOG_COLS = ["Run ID", "Started", "Finished", "Category", "City", "Min score", "Scraped", "Duplicate", "Rejected",
               "Final", "Status", "Notes"]


# ------------------------------------------------------------------ Make webhooks
def _post(url, payload, tries=3):
    last = None
    for i in range(tries):
        try:
            r = requests.post(url, json=payload, timeout=60)
            if r.status_code == 200:
                return True
            last = f"HTTP {r.status_code}"
        except Exception as exc:  # noqa: BLE001
            last = str(exc)
        time.sleep(3 * (i + 1))
    raise RuntimeError(f"Make webhook failed: {last}")


def push_rows(url, tab, cols, rows):
    """rows are dicts keyed by the column names. One Make call (2 operations) per 400 rows."""
    if not url or not rows:
        return False
    for i in range(0, len(rows), 400):
        chunk = [["" if r.get(c) is None else r.get(c) for c in cols] for r in rows[i:i + 400]]
        _post(url, {"tab": tab, "rows_json": json.dumps(chunk, ensure_ascii=False)})
        time.sleep(2)
    return True


def write_range(url, a1, rows):
    if not url:
        return False
    return _post(url, {"range": a1, "rows_json": json.dumps(rows, ensure_ascii=False)})


def clean(s, n=180):
    return re.sub(r'["\\\n\r\t]+', " ", str(s or "")).strip()[:n]


def set_status(url, status, detail="", run_id=""):
    """The Control tab keeps Status, Detail, Updated and Run ID in B8:B11."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return write_range(url, "Control!B8:B11", [[clean(status, 40)], [clean(detail)], [stamp], [clean(run_id, 40)]])


# ------------------------------------------------------------------ decision maker through Google search snippets
def cse_search(query, key, cx, num=5):
    r = requests.get("https://www.googleapis.com/customsearch/v1", params={"key": key, "cx": cx, "q": query, "num": num}, timeout=20)
    r.raise_for_status()
    return r.json().get("items", [])


def _tokens(business):
    stop = {"inc", "ltd", "the", "corp", "co", "and", "of", "restaurant", "cafe", "shop", "store"}
    return [w for w in re.findall(r"[a-z0-9]+", (business or "").lower()) if w not in stop][:3]


def linkedin_decision_maker(business, city, key, cx, search=cse_search):
    """Looks for a public LinkedIn result like 'Jane Doe - Owner - Maple Cafe | LinkedIn'. We never open LinkedIn;
    only the title and snippet that Google's official API returns are read."""
    toks = _tokens(business)
    if not toks:
        return None
    q = f'"{business}" {city.split()[0] if city else ""} (owner OR founder OR CEO OR director OR manager) site:linkedin.com/in'
    for it in search(q, key, cx):
        link, title, snippet = it.get("link", ""), it.get("title", ""), it.get("snippet", "")
        if "linkedin.com/in/" not in link:
            continue
        blob = f"{title} {snippet}".lower()
        if not all(t in blob for t in toks):
            continue
        segs = [s.strip() for s in re.split(r"\s[-–|·]\s", title)]
        name = segs[0] if segs else ""
        words = name.split()
        if not (2 <= len(words) <= 3) or not all(re.fullmatch(r"[A-Za-z'’\-\.]+", w) for w in words) or any(w.lower() in NOT_NAME for w in words):
            continue
        role = next((s for s in segs[1:] if "linkedin" not in s.lower() and classify_title(s)), None)
        if not role:
            m = re.search(r"\b(owner|co-?founder|founder|ceo|president|director|general manager|manager|proprietor)\b[^.\n·]{0,30}?\bat\b\s+([^.\n·]{0,50})", snippet, re.I)
            if m and all(t in m.group(2).lower() for t in toks[:1]):
                role = m.group(1)
        cls = classify_title(role)
        if cls:
            return {"name": name, "title": cls[0], "source_url": link}
    return None


# ------------------------------------------------------------------ recently rejected businesses
class RejectedCache:
    def __init__(self, path, retry_days):
        self.path, self.days = Path(path), int(retry_days)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        except Exception:  # noqa: BLE001
            self.data = {}

    def recent(self, keys, today=None):
        today = today or date.today()
        for k in keys:
            d = self.data.get(k)
            if d and (today - date.fromisoformat(d)).days < self.days:
                return True
        return False

    def add(self, keys, today=None):
        for k in keys:
            self.data[k] = (today or date.today()).isoformat()

    def save(self, today=None):
        today = today or date.today()
        self.data = {k: d for k, d in self.data.items() if (today - date.fromisoformat(d)).days < max(self.days, 1) * 4}
        self.path.write_text(json.dumps(self.data), encoding="utf-8")


# ------------------------------------------------------------------ choosing what to search
def pick(items, wanted, getter=lambda x: x):
    """wanted: 'ALL', one name or several separated by commas. Matches whole names, case-insensitive."""
    wanted = (wanted or "ALL").strip()
    if wanted.upper() == "ALL":
        return list(items)
    names = [w.strip().lower() for w in wanted.split(",") if w.strip()]
    return [i for i in items if getter(i).lower() in names or any(getter(i).lower().startswith(n) for n in names)]
