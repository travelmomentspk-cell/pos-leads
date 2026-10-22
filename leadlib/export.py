"""Writes dated CSV files and (optionally) appends rows to the Google Sheet through an Apps Script web app."""
import csv
import json
import os
import time
from pathlib import Path

import requests

QUALIFIED_COLS = ["business_name", "phone", "email", "full_address", "description", "intent_tier", "score",
                  "current_pos_detected", "evidence", "website", "category", "province", "city",
                  "email_source_url", "phone_status", "email_status", "place_id", "scraped_date",
                  "outreach_status", "source", "source_url", "service_needs", "decision_maker", "decision_maker_title", "reason"]
REVIEW_COLS = ["place_id", "business_name", "phone", "email", "full_address", "website", "score",
               "intent_tier", "review_reason", "claude_output", "human_decision", "scraped_date"]
REJECT_COLS = ["place_id", "business_name", "website", "rejected_at_gate", "reject_reason", "score", "scraped_date"]
RUNLOG_COLS = ["run_id", "run_date", "queries_run", "raw_records", "passed_gate1", "enriched", "sent_to_claude",
               "qualified", "review", "rejected", "make_operations_used", "claude_cost_estimate_usd", "notes"]
TEXT_COLS = {"phone", "postal", "place_id"}


def write_csv(path, cols, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            w.writerow([r.get(c, "") for c in cols])


def _sheet_safe(col, v):
    """Stop Google Sheets from turning phone numbers or text starting with = + - @ into formulas/numbers."""
    if v is None:
        return ""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v
    s = str(v)
    if col in TEXT_COLS or s[:1] in ("=", "+", "-", "@"):
        return "'" + s
    return s


def enabled(cfg):
    sh = cfg["sheets"]
    return bool(sh.get("make_webhook_url") or sh.get("web_app_url"))


def _plain(v):
    return "" if v is None else v


def push(cfg, tab, cols, rows):
    """Append rows to a tab. Preferred route: the Make webhook scenario (2 Make operations per call, any number of rows).
    Fallback: the Apps Script web app."""
    sh = cfg["sheets"]
    make_webhook_url = os.environ.get("MAKE_WEBHOOK_URL") or sh.get("make_webhook_url")
    if not rows:
        return False
    if make_webhook_url:
        for i in range(0, len(rows), 400):
            chunk = [[_plain(r.get(c, "")) for c in cols] for r in rows[i:i + 400]]
            payload = {"tab": tab, "rows_json": json.dumps(chunk, ensure_ascii=False)}
            for attempt in range(3):
                try:
                    resp = requests.post(make_webhook_url, json=payload, timeout=60)
                    if resp.status_code == 200:
                        break
                    raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:120]}")
                except Exception as exc:  # noqa: BLE001
                    if attempt == 2:
                        raise RuntimeError(f"Could not push to {tab} via Make: {exc}") from exc
                    time.sleep(3)
            time.sleep(2)  # let Make finish each run before the next one
        return True
    url = sh.get("web_app_url")
    token = os.environ.get("LEADS_TOKEN", "")
    if not url:
        raise RuntimeError("No Make webhook or Apps Script web app URL is configured; results were not sent.")
    if not token:
        raise RuntimeError("Set the LEADS_TOKEN environment variable to the same secret you put in the Apps Script.")
    for i in range(0, len(rows), 150):
        chunk = [[_sheet_safe(c, r.get(c, "")) for c in cols] for r in rows[i:i + 150]]
        for attempt in range(3):
            try:
                resp = requests.post(url, json={"token": token, "tab": tab, "rows": chunk}, timeout=60)
                data = resp.json()
                if data.get("ok"):
                    break
                raise RuntimeError(data.get("error", "unknown error"))
            except Exception as exc:  # noqa: BLE001
                if attempt == 2:
                    raise RuntimeError(f"Could not push to {tab}: {exc}") from exc
                time.sleep(3)
    return True
