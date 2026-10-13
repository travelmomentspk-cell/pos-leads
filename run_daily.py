#!/usr/bin/env python3
"""Daily lead pipeline: sources -> free Maps scraper -> gates -> enrichment -> scoring -> CSV + Google Sheet.

Usage:
  python run_daily.py                     full run (scraper needs Docker or the binary)
  python run_daily.py --csv results.csv   skip scraping, process an existing scraper CSV
  python run_daily.py --no-push           do not send anything to Google Sheets
  python run_daily.py --no-enrich         skip website visits (offline testing)
  python run_daily.py --probe-licences    print one raw licence record to check field names
"""
import argparse
import difflib
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

import yaml

from leadlib import enrich as enrich_mod
from leadlib import export, scraper, sources, validate
from leadlib.master import Master
from leadlib.quality import REJECT_COLS as QUALITY_REJECT_COLS, quality_gate
from leadlib.score import score_lead
from leadlib.state import State

ROOT = Path(__file__).parent


def norm_name(s):
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower().replace("&", " and ")).strip()


def similar(a, b):
    a, b = norm_name(a), norm_name(b)
    if not a or not b:
        return 0
    if a in b or b in a:
        return 1
    return difflib.SequenceMatcher(None, a, b).ratio()


def load_chains():
    p = ROOT / "chains.txt"
    return [l.strip().lower() for l in p.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.startswith("#")]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--csv", help="process this scraper CSV instead of running the scraper")
    ap.add_argument("--meta", help="queries meta JSON that belongs to --csv")
    ap.add_argument("--reuse-dir", help="re-process the raw scraper files of an earlier run, e.g. out/2026-10-06 (no scraping)")
    ap.add_argument("--ignore-seen", action="store_true", help="do not skip businesses recorded in state/seen.json")
    ap.add_argument("--no-enrich", action="store_true")
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--probe-licences", action="store_true")
    ap.add_argument("--seed-master", nargs="+", metavar="CSV", help="add leads from these CSV files to the Master list and exit")
    ap.add_argument("--test-push", action="store_true", help="send one test row to the Run_Log tab and exit")
    args = ap.parse_args()

    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))
    if args.seed_master:
        m = Master(ROOT / cfg["paths"]["state"] / "master.db")
        n = m.seed_csvs(args.seed_master)
        print(f"Added {n} leads to the Master list. It now holds {m.count()}.")
        return
    if args.test_push:
        row = {"run_id": "connection-test", "run_date": date.today().isoformat(), "notes": "connection test, safe to delete"}
        ok = export.push(cfg, "Run_Log", export.RUNLOG_COLS, [row])
        print("Test row sent. Check the Run_Log tab in a few seconds." if ok else "Nothing sent: set sheets.make_webhook_url in config.yaml")
        return
    if args.probe_licences:
        recs = sources.vancouver_records(cfg["sources"]["vancouver_licences"], limit=2)
        print(json.dumps(recs[:1], indent=2, ensure_ascii=False))
        print("Field names:", sorted(recs[0].keys()) if recs else "no records")
        return

    today = date.today().isoformat()
    stamp = datetime.now().strftime("%m%d%H%M")
    run_id = datetime.now().strftime("%Y%m%d-%H%M")
    out_dir = ROOT / cfg["paths"]["out"] / today
    out_dir.mkdir(parents=True, exist_ok=True)
    state = State(ROOT / cfg["paths"]["state"])
    if args.ignore_seen:
        state.seen = set()
    chains = load_chains()
    quality = cfg.get("quality", {})
    notes = []

    # ---------- 1. acquire raw records ----------
    meta, raw, queries_run = {}, [], 0
    if args.reuse_dir:
        d = Path(args.reuse_dir)
        for name in ("results_categories.csv", "results_licences.csv"):
            if (d / name).exists():
                raw += scraper.load_results(d / name)
        if (d / "meta.json").exists():
            meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        if not raw:
            sys.exit(f"No results_*.csv found in {d}")
    elif args.csv:
        raw = scraper.load_results(args.csv)
        mp = Path(args.meta) if args.meta else Path(args.csv).with_name("meta.json")
        if mp.exists():
            meta = json.loads(mp.read_text(encoding="utf-8"))
    else:
        cat_q, cat_meta = sources.category_queries(cfg, state, stamp)
        lic_q, lic_meta = [], {}
        if cfg["sources"]["vancouver_licences"].get("enabled"):
            try:
                lic_q, lic_meta = sources.vancouver_queries(cfg, stamp)
            except Exception as exc:  # noqa: BLE001
                notes.append(f"vancouver licences skipped: {exc}")
        try:
            if cfg["sources"].get("socrata_licences", {}).get("enabled"):
                q2, m2 = sources.socrata_queries(cfg, stamp)
                lic_q += q2
                lic_meta.update(m2)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"socrata licences skipped: {exc}")
        meta = {**cat_meta, **lic_meta}
        (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        queries_run = len(cat_q) + len(lic_q)
        if cat_q:
            scraper.write_queries(cat_q, out_dir / "queries_categories.txt")
            res = scraper.run_scraper(cfg, out_dir / "queries_categories.txt", out_dir, "results_categories.csv",
                                      cfg["scraper"].get("depth", 3))
            raw += scraper.load_results(res)
        if lic_q:
            scraper.write_queries(lic_q, out_dir / "queries_licences.txt")
            res = scraper.run_scraper(cfg, out_dir / "queries_licences.txt", out_dir, "results_licences.csv", 1)
            raw += scraper.load_results(res)
    print(f"Raw records: {len(raw)}")

    # ---------- 1b. duplicate check against the Master list (runs before anything goes to the Sheet) ----------
    master = Master(ROOT / cfg["paths"]["state"] / "master.db")
    if master.count() == 0:
        n = master.seed_from_out(ROOT / cfg["paths"]["out"])
        if n:
            print(f"Master list started with {n} leads found in earlier output files.")
    fresh, dup = master.split(raw)
    print(f"{len(raw)} scraped, {len(dup)} duplicate, {len(fresh)} new")
    raw = fresh

    # ---------- 2. Gate 1: rules, no network ----------
    rejected, review, cands, batch_keys = [], [], [], set()

    def reject(r, gate, reason, score=""):
        rejected.append({"place_id": r["place_id"], "business_name": r["name"], "website": r["website"],
                         "rejected_at_gate": gate, "reject_reason": reason, "score": score, "scraped_date": today})

    for r in raw:
        m = meta.get(r["input_id"], {})
        r.update(source=m.get("source", "google_maps_manual" if args.csv else "google_maps_search"),
                 source_url=m.get("source_url") or r["link"], signal_type=m.get("signal_type", ""),
                 signal_text=m.get("signal_text", ""), signal_date=m.get("signal_date", ""))
        if m.get("match_names") and max(similar(n, r["name"]) for n in m["match_names"]) < 0.6:
            continue  # licence lookup returned a different business: skip silently
        low = r["status"].lower()
        if "closed" in low:
            reject(r, "gate1", "business closed"); continue
        if not r["canadian"]:
            reject(r, "gate1", "not in Canada"); continue
        r["phone"] = validate.normalize_phone(r["phone_raw"])
        r["phone_status"] = validate.phone_status(r["phone"])
        if not r["phone"]:
            reject(r, "gate1", "no valid phone number"); continue
        if not r["address"]:
            reject(r, "gate1", "no address"); continue
        if r["review_count"] > int(quality.get("max_review_count", 1500)):
            reject(r, "gate1", f"too large or busy for a small-business lead ({r['review_count']} reviews)"); continue
        c = validate.chain_match(r["name"], chains)
        if c:
            reject(r, "gate1", f"chain or franchise ({c})"); continue
        r["domain"] = validate.domain_of(r["website"])
        keys = State.keys_for(r["place_id"], r["phone"], r["domain"])
        if state.is_seen(keys) or any(k in batch_keys for k in keys):
            continue  # already handled before, not a rejection
        batch_keys.update(keys)
        r["keys"] = keys
        cands.append(r)
    print(f"Passed gate 1: {len(cands)}")

    # ---------- 3. enrichment ----------
    with_site = [r for r in cands if r["website"]]
    for r in cands:
        r.update(email=None, email_source_url="", emails=r["emails"], pos_detected=[], online_ordering=False,
                 cash_only=False, snippet="")
    if args.no_enrich:
        for r in with_site:
            r.update(email=(validate.clean_email(r["emails"][0]) if r["emails"] else None), email_source_url=(
                r["website"] + " (found by the Maps scraper's website crawl)" if r["emails"] else ""),
                pos_detected=[], online_ordering=False, cash_only=False, snippet="")
    else:
        e = cfg["enrich"]
        results = enrich_mod.enrich_many([(i, r["website"], r["emails"]) for i, r in enumerate(with_site)],
                                         e.get("workers", 8), e.get("timeout_seconds", 10), e.get("check_robots", True))
        for i, r in enumerate(with_site):
            r.update(results[i])
            r["site_checked"] = bool(results[i].get("fetch_ok"))
    print(f"Enriched (have a website): {len(with_site)}")

    # ---------- 4. Gate 2 + scoring ----------
    qualified, scored, low_quality = [], 0, []
    for r in cands:
        r["email_status"], usable = validate.email_status(r.get("email"), r["website"], cfg["enrich"].get("mx_check", True))
        if not usable:
            r["email_status"] = "none" if not r.get("email") else r["email_status"]
            r["email"] = ""
            r["email_source_url"] = ""
        s = score_lead(r, quality)
        scored += 1
        r.update(s)
        r["model"] = 3
        base = {"place_id": r["place_id"], "business_name": r["name"], "phone": r["phone"],
                "email": r.get("email") or "", "full_address": r["address"], "website": r["website"],
                "score": s["score"], "intent_tier": s["tier"], "scraped_date": today}
        if s["category_fit"] == "excluded":
            reject(r, "gate2", f"category not a POS fit ({r['category']})", s["score"]); state.mark(r["keys"]); continue
        lq = cfg.get("lead_quality", {})
        if lq.get("enabled", False):  # lead quality gate: 0-100 score + decision maker required
            qg = quality_gate(r, lq)
            r.update(decision_maker=qg["decision_maker"], decision_maker_title=qg["decision_maker_title"],
                     reason=qg["reason"], score=qg["score"])
            if not qg["accepted"]:  # never goes to the Sheet; kept only in out/<date>/quality_rejected.csv
                low_quality.append({"business_name": r["name"], "phone": r["phone"], "website": r["website"],
                                    "score": qg["score"], "reject_reason": qg["reject_reason"],
                                    "decision_maker": qg["decision_maker"], "reason": qg["reason"], "scraped_date": today})
                continue
            qualified.append(r)
            continue
        if quality.get("require_need_signal", True) and s["tier"] == "C":
            state.mark(r["keys"])
            review.append({**base, "review_reason": "good fit but no clear service need found yet",
                           "claude_output": json.dumps({"signals": s["signals"], "evidence": s["evidence"]}, ensure_ascii=False)})
        elif s["score"] >= cfg["min_score_qualified"]:
            qualified.append(r)
        elif s["score"] >= cfg["review_score_floor"]:
            state.mark(r["keys"])
            review.append({**base, "review_reason": f"score {s['score']} below {cfg['min_score_qualified']}",
                           "claude_output": json.dumps({"signals": s["signals"], "evidence": s["evidence"]}, ensure_ascii=False)})
        else:
            reject(r, "score", f"score {s['score']} too low", s["score"]); state.mark(r["keys"])

    # leads that qualified on earlier days but did not fit under the daily cap are tried again first
    today_keys = {k for r in qualified for k in r["keys"]}
    for old in state.backlog:
        age = (date.today() - date.fromisoformat(old.get("backlogged_on", today))).days
        if old.get("model") == 3 and age <= 14 and not state.is_seen(old["keys"]) and not any(k in today_keys for k in old["keys"]):
            qualified.append(old)
    qualified.sort(key=lambda x: (-x["score"], {"A": 0, "B": 1, "C": 2}[x["tier"]], -(x.get("review_count") or 0)))
    cap = int(cfg["target_leads_per_day"])
    overflow = qualified[cap:]
    qualified = qualified[:cap]
    state.backlog = [{**o, "backlogged_on": o.get("backlogged_on", today)} for o in overflow]
    for r in qualified:
        state.mark(r["keys"])
    q_rows = [{
        "business_name": r["name"], "phone": r["phone"], "email": r["email"], "full_address": r["address"],
        "description": r["description"], "intent_tier": r["tier"], "score": r["score"],
        "current_pos_detected": r["pos_detected"], "evidence": r["evidence"], "website": r["website"],
        "category": r["category"], "province": r["province"], "city": r["city"],
        "email_source_url": r["email_source_url"], "phone_status": r["phone_status"],
        "email_status": r["email_status"], "place_id": r["place_id"], "scraped_date": today,
        "outreach_status": "new", "source": r["source"], "source_url": r["source_url"],
        "service_needs": r["service_needs"], "decision_maker": r.get("decision_maker", ""),
        "decision_maker_title": r.get("decision_maker_title", ""), "reason": r.get("reason", ""),
    } for r in qualified]
    if overflow:
        notes.append(f"{len(overflow)} more leads qualified but were above the daily cap; kept in state/backlog.json for the next run.")

    # ---------- 5. outputs ----------
    export.write_csv(out_dir / "qualified_leads.csv", export.QUALIFIED_COLS, q_rows)
    export.write_csv(out_dir / "review_queue.csv", export.REVIEW_COLS, review)
    export.write_csv(out_dir / "rejected_log.csv", export.REJECT_COLS, rejected)
    export.write_csv(out_dir / "quality_rejected.csv", QUALITY_REJECT_COLS, low_quality)
    runlog = {"run_id": run_id, "run_date": today, "queries_run": queries_run, "raw_records": len(raw),
              "passed_gate1": len(cands), "enriched": 0 if args.no_enrich else len(with_site), "sent_to_claude": scored,
              "qualified": len(q_rows), "review": len(review), "rejected": len(rejected),
              "make_operations_used": 0, "claude_cost_estimate_usd": 0, "notes": "; ".join(notes)}
    export.write_csv(out_dir / "run_log.csv", export.RUNLOG_COLS, [runlog])

    pushed, delivered = False, False
    if not args.no_push and export.enabled(cfg):
        pushed = export.push(cfg, "Qualified_Leads", export.QUALIFIED_COLS, q_rows)
        export.push(cfg, "Review_Queue", export.REVIEW_COLS, review)
        export.push(cfg, "Rejected_Log", export.REJECT_COLS, rejected)
        export.push(cfg, "Run_Log", export.RUNLOG_COLS, [runlog])
        delivered = True
    if delivered:  # only leads that really reached the Sheet are remembered in the Master list
        added = master.add_decided([("qualified", q_rows), ("review", review), ("rejected", rejected)],
                                   {x["place_id"]: x for x in raw if x["place_id"]})
        print(f"Master list now holds {master.count()} leads (+{added} this run).")
    state.save()

    print("\n=== Run summary ===")
    print(f"Raw {len(raw)} | passed gate 1: {len(cands)} | qualified: {len(q_rows)} | review: {len(review)} | rejected: {len(rejected)}")
    if low_quality:
        print(f"Quality filter: {len(low_quality)} leads rejected (score below the bar or no decision maker). "
              f"They are NOT in the Sheet; see quality_rejected.csv.")
    if len(q_rows) < cap:
        print(f"Only {len(q_rows)} leads met the quality bar today (target {cap}). Quality comes first, so nothing was added to fill the gap.")
    print(f"Files in: {out_dir}")
    print("Pushed to Google Sheet." if pushed else "Not pushed to Google Sheet (no webhook/web app URL or --no-push).")
    for n in notes:
        print("Note:", n)


if __name__ == "__main__":
    sys.exit(main())
