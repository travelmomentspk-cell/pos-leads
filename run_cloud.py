#!/usr/bin/env python3
"""Cloud lead run (GitHub Actions or your PC). All settings live in config_cloud.yaml.

  py run_cloud.py run --category "Gyms and fitness studios" --city "Toronto ON" --min-score 70
  py run_cloud.py run --category ALL --city ALL            walks through categories and cities, queries_per_run at a time
  py run_cloud.py status --state Failed --detail "why"    writes the status cell of the Control tab
  py run_cloud.py lists                                    refreshes the dropdown lists in the Sheet from the config
Offline tests: --csv results.csv --meta meta.json --no-push --no-enrich --no-cse
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

from leadlib import cloud, enrich as enrich_mod, export, extra, scraper, validate
from leadlib.master import Master
from leadlib.quality import classify_title, quality_gate

ROOT = Path(__file__).parent


def load_cfg(path):
    return extra.load_config(path)


def urls(cfg):
    sh = cfg.get("sheet", {})
    return (os.environ.get("MAKE_WEBHOOK_URL") or sh.get("make_webhook_url") or "",
            os.environ.get("MAKE_STATUS_WEBHOOK_URL") or sh.get("make_status_webhook_url") or "")


def load_chains(cfg):
    p = ROOT / "chains.txt"
    base = [l.strip().lower() for l in p.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")] if p.exists() else []
    return base + [c.lower() for c in cfg.get("exclude_chains", [])]


def lists_rows(cfg):
    """Rows for Lists!A2:C60: category, city and min-score choices that feed the Sheet dropdowns."""
    cats = [c["name"] for c in cfg["categories"] if c.get("enabled", True)]
    cities, scores = list(cfg["cities"]), [int(x) for x in cfg.get("score_choices", [60, 70, 80])]

    def at(lst, i):
        return lst[i] if 0 <= i < len(lst) else ""
    n = max(59, max(len(cats), len(cities), len(scores)) + 1)
    return [["ALL" if i == 0 else at(cats, i - 1), "ALL" if i == 0 else at(cities, i - 1), at(scores, i)] for i in range(min(n, 59))]


def pipeline(args, cfg, run_id, started, seen_queries=None, max_new_leads=None, batch_index=1,
             cse_budget=None):
    run_cfg, qual = cfg["run"], cfg.get("quality", {})
    today = date.today().isoformat()
    min_score = int(args.min_score or run_cfg.get("min_score", 70))
    wanted_cat = args.category or run_cfg.get("default_category", "ALL")
    wanted_city = args.city or run_cfg.get("default_city", "ALL")
    out_dir = ROOT / cfg["paths"]["out"] / today / run_id / f"batch_{batch_index:03d}"
    out_dir.mkdir(parents=True, exist_ok=True)
    state_dir = ROOT / cfg["paths"]["state"]
    state_dir.mkdir(parents=True, exist_ok=True)
    rot_path = state_dir / "rotation.json"
    rotation = json.loads(rot_path.read_text()) if rot_path.exists() else {}
    master = Master(state_dir / "master.db")
    rcache = cloud.RejectedCache(state_dir / "rejected_cache.json", run_cfg.get("rejected_retry_days", 14))

    # ---------- 1. scrape (Google Maps through the free scraper) ----------
    cats = [c for c in cloud.pick([c for c in cfg["categories"] if c.get("enabled", True)], wanted_cat, lambda c: c["name"])]
    if not cats and not args.csv:
        raise RuntimeError(f"No enabled category matches '{wanted_cat}'")
    cities = cloud.pick(cfg["cities"], wanted_city) or ([wanted_city] if wanted_city.upper() != "ALL" else cfg["cities"])
    meta = {}
    if args.csv:
        raw = extra.load_results(args.csv)
        mp = Path(args.meta) if args.meta else Path(args.csv).with_name("meta.json")
        meta = json.loads(mp.read_text(encoding="utf-8")) if mp.exists() else {}
    else:
        stamp = datetime.now().strftime("%m%d%H%M")
        like = {"business_types": [{"name": c["name"], "enabled": True, "keywords": c["keywords"], "needs": c.get("needs", [])} for c in cats],
                "market": {"cities": cities, "queries_per_run": int(run_cfg.get("queries_per_run", 20))}}
        queries, meta = extra.build_type_queries(like, rotation, stamp)
        seen_queries = seen_queries if seen_queries is not None else set()
        queries = [q for q in queries if q["text"] not in seen_queries]
        if not queries:
            master.db.close()
            return "No unused category/city searches remain", 0, [], 0
        seen_queries.update(q["text"] for q in queries)
        meta = {q["id"]: meta[q["id"]] for q in queries}
        extra.write_queries(queries, out_dir / "queries.txt")
        (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        res = scraper.run_scraper(cfg, out_dir / "queries.txt", out_dir, "results.csv", cfg["sources"]["google_maps"].get("depth", 3))
        raw = extra.load_results(res)
    scraped = len(raw)
    print(f"Scraper returned {scraped} records; starting lead qualification", flush=True)

    # ---------- 2. duplicates: Master list, businesses rejected recently, and repeats inside this batch ----------
    rejected, cands, dup = [], [], 0
    for r in raw:
        m = meta.get(r["input_id"], {})
        r.update(category_name=m.get("type_name", ""), needs=m.get("needs", []), source_query=m.get("query", ""))
        r["phone"] = validate.normalize_phone(r["phone_raw"])
    fresh, dups = master.split(raw)
    dup += len(dups)
    survivors = []
    for r in fresh:
        if rcache.recent(extra.Seen.keys_for(r), date.today()):
            dup += 1
        else:
            survivors.append(r)

    def reject(r, reason, score=""):
        rejected.append({"Business": r["name"], "City": r["city"], "Reason": reason, "Score": score,
                         "Source": r.get("source_query", ""), "Date": today, "Run ID": run_id})
        rcache.add(extra.Seen.keys_for(r))

    # ---------- 3. hard rules ----------
    chains, maxrev = load_chains(cfg), int(run_cfg.get("max_review_count", 1500))
    for r in survivors:
        r["phone_status"] = validate.phone_status(r["phone"])
        if "closed" in r["status"].lower():
            reject(r, "business closed")
        elif not validate.is_canadian(r["province"], r["postal"], r["address"], r["country"]):
            reject(r, "not in Canada")
        elif not r["phone"]:
            reject(r, "no valid phone number")
        elif validate.chain_match(r["name"], chains):
            reject(r, "chain or franchise")
        elif r["review_count"] > maxrev:
            reject(r, f"too large ({r['review_count']} reviews)")
        else:
            cands.append(r)

    # ---------- 4. their own website: email, About/Team pages, schema.org people, hiring text ----------
    wp = cfg["sources"]["website_pages"]
    for r in cands:
        r.update(email=(validate.clean_email(r["emails"][0]) if r["emails"] else None), digital={}, site_checked=False,
                 pages=[], people=[], pos_detected=[], online_ordering=False)
    todo = [r for r in cands if r["website"] and "/" not in extra.site_key(r["website"])]
    if wp.get("enabled", True) and not args.no_enrich and todo:
        print(f"Checking {len(todo)} business websites for contact and quality signals", flush=True)
        res = enrich_mod.enrich_many([(i, r["website"], r["emails"]) for i, r in enumerate(todo)], wp.get("workers", 8),
                                     wp.get("timeout_seconds", 10), cfg["enrich"].get("check_robots", True))
        for i, r in enumerate(todo):
            x = res[i]
            r.update(email=x.get("email") or r["email"], digital=x.get("digital") or {}, site_checked=bool(x.get("fetch_ok")),
                     pages=x.get("pages") or [], people=x.get("people") or [], pos_detected=x.get("pos_detected") or [],
                     online_ordering=bool(x.get("online_ordering")))

    # ---------- 5. build the lead, find the decision maker, score ----------
    def qlead(r):
        d = {"name": r["name"], "phone": r["phone"], "phone_status": r["phone_status"], "website": r["website"],
             "email_status": validate.email_status(r["email"], r["website"], cfg["enrich"].get("mx_check", True) and not args.no_enrich)[0] if r["email"] else "none",
             "digital": r["digital"], "site_checked": r["site_checked"], "pages": r["pages"], "pos_detected": r["pos_detected"],
             "online_ordering": r["online_ordering"],
             "reviews": [t for rv in r["reviews"] if (not rv["rating"] or rv["rating"] <= 3) for t in rv["texts"]]}
        best = None
        for p in r["people"]:
            c = classify_title(p["title"])
            if c and (best is None or c[1] > best[1]):
                best = (p, c[1])
        if best:
            d.update(decision_maker=best[0]["name"], decision_maker_title=best[0]["title"], decision_maker_source=best[0].get("source_url", ""))
        elif r.get("dm"):
            d.update(decision_maker=r["dm"]["name"], decision_maker_title=r["dm"]["title"], decision_maker_source=r["dm"]["source_url"])
        return d

    for r in cands:
        r["q"] = qlead(r)
        pre = quality_gate(r["q"], {"min_score": 0, "require_decision_maker": False})
        r["pre"] = pre["score"] + (0 if pre["decision_maker"] else 25)  # best case if a decision maker turns up
    key, cx = os.environ.get("GOOGLE_CSE_KEY"), os.environ.get("GOOGLE_CSE_CX")
    lk, searched = cfg["sources"]["linkedin_via_google_search"], 0
    daily_budget = int(lk.get("daily_budget", 80))
    if cse_budget is not None:
        daily_budget = min(daily_budget, max(0, cse_budget))
    if lk.get("enabled", True) and key and cx and not args.no_cse:
        print("Looking up public decision-maker snippets", flush=True)
        for r in sorted(cands, key=lambda x: -x["pre"]):
            if searched >= daily_budget:
                break
            if r["pre"] < min_score or quality_gate(r["q"], {"min_score": 0})["decision_maker"]:
                continue
            searched += 1
            try:
                found = cloud.linkedin_decision_maker(r["name"], r["city"], key, cx)
            except Exception as exc:  # noqa: BLE001
                print("Google search skipped:", str(exc)[:100]); break
            if found:
                r["dm"] = found
                r["q"] = qlead(r)
            if searched % 10 == 0:
                print(f"Decision-maker search progress: {searched}/{daily_budget}", flush=True)
    good = []
    for r in cands:
        g = quality_gate(r["q"], {"min_score": min_score, "require_decision_maker": False})
        r["g"] = g
        missing = []
        if qual.get("require_decision_maker", True) and not g["decision_maker"]:
            missing.append("decision maker name")
        if qual.get("require_phone", True) and not r["phone"]:
            missing.append("phone")
        if qual.get("require_email", True) and r["q"]["email_status"] not in ("ok_own_domain", "ok_other_domain", "ok_freemail"):
            missing.append("email")
        if missing:
            reject(r, "missing " + ", ".join(missing), g["score"])
        elif g["score"] < min_score:
            reject(r, f"score {g['score']} below {min_score}", g["score"])
        else:
            good.append(r)
    good.sort(key=lambda x: (-x["g"]["score"], -(x["review_count"] or 0)))
    daily_cap = int(run_cfg.get("daily_cap", 100))
    if max_new_leads is not None:
        daily_cap = min(daily_cap, max_new_leads)
    good = good[:daily_cap]

    # ---------- 6. rows, files, Sheet ----------
    leads, masters = [], []
    for r in good:
        g = r["g"]
        src = f"Google Maps: {r['source_query']}" + (f" | decision maker: {g['decision_maker_source']}" if g["decision_maker_source"] else "")
        services = ", ".join(dict.fromkeys(list(r["needs"]) + (["Website"] if not (r["website"] and "/" not in extra.site_key(r["website"])) else [])))
        base = {"Business": r["name"], "Decision Maker": g["decision_maker"], "Role": g["decision_maker_title"], "Phone": r["phone"],
                "Email": r["email"] or "", "Website": r["website"], "Score": g["score"], "Source": src, "Date": today}
        leads.append({**base, "Reason": g["reason"], "Services to pitch": services, "Status": "New"})
        masters.append({**base, "Run ID": run_id})
    final = len(leads)
    finished = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    runlog = {"Run ID": run_id, "Started": started, "Finished": finished, "Category": wanted_cat, "City": wanted_city,
              "Min score": min_score, "Scraped": scraped, "Duplicate": dup, "Rejected": len(rejected), "Final": final,
              "Status": "Done", "Notes": f"google searches used: {searched}"}
    export.write_csv(out_dir / "new_leads.csv", cloud.NEW_LEAD_COLS, leads)
    export.write_csv(out_dir / "rejected.csv", cloud.REJECT_COLS, rejected)
    summary = f"{scraped} scraped, {dup} duplicate, {len(rejected)} rejected, {final} final"
    print(summary, flush=True)
    leads_url, status_url = urls(cfg)
    tabs = cfg["sheet"]["tabs"]
    if not args.no_push:
        if not leads_url:
            # Never report Done when nothing reached the Sheet, and never remember leads that were not delivered.
            raise RuntimeError("MAKE_WEBHOOK_URL is not set (GitHub secret). Nothing was sent to the Sheet and nothing was saved, so the run can simply be repeated.")
        cloud.push_rows(leads_url, tabs["new_leads"], cloud.NEW_LEAD_COLS, leads)
        if leads:
            for r in good:
                master.add("delivered", r["name"], r["phone"], r["website"], r["city"], r["place_id"])
            master.db.commit()
            rcache.save()
            pending_path = state_dir / "pending_master_rows.json"
            pending = json.loads(pending_path.read_text(encoding="utf-8")) if pending_path.exists() else []
            pending.extend(masters)
            temp_path = pending_path.with_suffix(".tmp")
            temp_path.write_text(json.dumps(pending, ensure_ascii=False), encoding="utf-8")
            temp_path.replace(pending_path)
            checkpoint_state()
            flush_pending_master(cfg, state_dir)
        near_misses = sorted(rejected, key=lambda x: -(x["Score"] if isinstance(x["Score"], (int, float)) else -1))
        cloud.push_rows(leads_url, tabs["rejected"], cloud.REJECT_COLS, near_misses[:300])
        cloud.push_rows(leads_url, tabs["run_log"], cloud.RUNLOG_COLS, [runlog])
        if not leads:
            rcache.save()
    if not args.csv:
        rot_path.write_text(json.dumps(rotation))
    master.db.close()
    return summary, final, [q["text"] for q in queries] if not args.csv else [], searched


def checkpoint_state():
    """Persist completed batches on the Actions branch so an interrupted run can resume safely."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
    subprocess.run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], check=True)
    subprocess.run(["git", "add", "state"], check=True)
    staged = subprocess.run(["git", "diff", "--cached", "--quiet", "--", "state"], check=False)
    if staged.returncode == 0:
        return
    if staged.returncode != 1:
        raise RuntimeError("Could not inspect staged lead state changes")
    subprocess.run(["git", "commit", "-m", "Checkpoint lead run state [skip ci]"], check=True)
    subprocess.run(["git", "pull", "--rebase"], check=True)
    subprocess.run(["git", "push"], check=True)


def flush_pending_master(cfg, state_dir):
    pending_path = Path(state_dir) / "pending_master_rows.json"
    if not pending_path.exists():
        return
    pending = json.loads(pending_path.read_text(encoding="utf-8"))
    if not pending:
        pending_path.unlink()
        checkpoint_state()
        return
    leads_url, _ = urls(cfg)
    cloud.push_rows(leads_url, cfg["sheet"]["tabs"]["master"], cloud.MASTER_COLS, pending)
    pending_path.unlink()
    checkpoint_state()


def run_batches(args, cfg, run_id, started):
    run_cfg = cfg["run"]
    target = int(getattr(args, "target_leads", None) or run_cfg.get("target_leads_per_run", 50))
    if target < 1:
        raise ValueError("target leads must be at least 1")
    max_minutes = int(run_cfg.get("max_runtime_minutes", 270))
    if max_minutes < 1:
        raise ValueError("max runtime must be at least 1 minute")

    started_at = time.monotonic()
    seen_queries, delivered, searched, batch_index = set(), 0, 0, 0
    reason = "target reached"
    last_summary = ""
    if not getattr(args, "no_push", False):
        flush_pending_master(cfg, ROOT / cfg["paths"]["state"])
    while delivered < target:
        if args.csv and batch_index:
            reason = "CSV input is a single batch"
            break
        if (time.monotonic() - started_at) >= max_minutes * 60:
            reason = f"run safety limit reached ({max_minutes} minutes)"
            break
        batch_index += 1
        remaining_cse = max(0, int(cfg["sources"]["linkedin_via_google_search"].get("daily_budget", 80)) - searched)
        print(f"Starting batch {batch_index}; target remaining: {target - delivered}", flush=True)
        last_summary, added, batch_queries, cse_used = pipeline(
            args, cfg, run_id, started, seen_queries, target - delivered, batch_index, remaining_cse)
        if not batch_queries and not args.csv:
            reason = "all selected category/city searches have been used"
            break
        delivered += added
        searched += cse_used
        checkpoint_state()
        print(f"Progress: {delivered}/{target} new leads delivered; {batch_index} batch(es) completed", flush=True)
        if args.csv:
            reason = "CSV input is a single batch"
            break
    if delivered < target and reason == "target reached":
        reason = "target not reached"
    result = f"{delivered}/{target} new leads delivered in {batch_index} batch(es); {reason}"
    print(result, flush=True)
    return result, delivered


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config_cloud.yaml"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--category"), r.add_argument("--city"), r.add_argument("--min-score"), r.add_argument("--run-id", default="")
    r.add_argument("--target-leads", type=int)
    r.add_argument("--csv"), r.add_argument("--meta")
    for flag in ("--no-enrich", "--no-push", "--no-cse"):
        r.add_argument(flag, action="store_true")
    s = sub.add_parser("status")
    s.add_argument("--state", required=True), s.add_argument("--detail", default=""), s.add_argument("--run-id", default="")
    sub.add_parser("lists")
    args = ap.parse_args(argv)
    cfg = load_cfg(args.config)
    leads_url, status_url = urls(cfg)
    if args.cmd == "status":
        cloud.set_status(status_url, args.state, args.detail, args.run_id)
        print("status sent" if status_url else "no status webhook set")
        return 0
    if args.cmd == "lists":
        ok = cloud.write_range(status_url, "Lists!A2:C60", lists_rows(cfg))
        print("lists sent" if ok else "no status webhook set")
        return 0
    run_id = args.run_id or datetime.now().strftime("run-%Y%m%d-%H%M")
    started = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if not args.no_push:
        cloud.set_status(status_url, "Running", f"{args.category or 'ALL'} / {args.city or 'ALL'}", run_id)
    try:
        summary, final = run_batches(args, cfg, run_id, started)
    except Exception as exc:  # noqa: BLE001
        if not args.no_push:
            cloud.set_status(status_url, "Failed", str(exc)[:150], run_id)
        raise
    if not args.no_push:
        target = int(args.target_leads or cfg["run"].get("target_leads_per_run", 50))
        cloud.set_status(status_url, "Done" if args.csv or final >= target else "Stopped", summary, run_id)
        cloud.write_range(status_url, "Lists!A2:C60", lists_rows(cfg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
