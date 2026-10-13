#!/usr/bin/env python3
"""Extra sources for Canada. All settings live in config_extra.yaml. The existing pipeline is untouched.

  py run_extra.py                    both sources (needs Docker running)
  py run_extra.py --only types       only the 15 business types
  py run_extra.py --only providers   only the provider-complaint source
  py run_extra.py --csv-types f.csv --csv-providers g.csv   judge existing scraper files, no scraping
  py run_extra.py --no-enrich        do not visit websites
  py run_extra.py --push             also send results to the Google Sheet tabs named in the config
"""
import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

from leadlib import enrich as enrich_mod
from leadlib import export, scraper, validate
from leadlib import extra

ROOT = Path(__file__).parent


def load_chains(cfg):
    p = ROOT / "chains.txt"
    base = [l.strip().lower() for l in p.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.startswith("#")] if p.exists() else []
    return base + [c.lower() for c in cfg.get("exclude_chains", [])]


def scrape(cfg, queries, meta, out_dir, name, depth, extended=False):
    meta_path = out_dir / f"meta_{name}.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    extra.write_queries(queries, out_dir / f"queries_{name}.txt")
    if extended:
        return extra.run_extended(cfg, out_dir / f"queries_{name}.txt", out_dir, f"results_{name}.json", depth)
    return scraper.run_scraper(cfg, out_dir / f"queries_{name}.txt", out_dir, f"results_{name}.csv", depth)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config_extra.yaml"))
    ap.add_argument("--only", choices=["types", "providers"])
    ap.add_argument("--csv-types"), ap.add_argument("--csv-providers")
    ap.add_argument("--no-enrich", action="store_true")
    ap.add_argument("--push", action="store_true")
    args = ap.parse_args()

    cfg = extra.load_config(args.config)
    today, stamp = date.today().isoformat(), datetime.now().strftime("%m%d%H%M")
    out_dir = ROOT / cfg["paths"]["out"] / today
    out_dir.mkdir(parents=True, exist_ok=True)
    state_dir = ROOT / cfg["paths"]["state"]
    state_dir.mkdir(parents=True, exist_ok=True)
    rot_path = state_dir / "rotation.json"
    rotation = json.loads(rot_path.read_text()) if rot_path.exists() else {}
    seen = extra.Seen(state_dir / "seen.json")
    seen_complaints = extra.Seen(state_dir / "seen_complaints.json")
    push = args.push or cfg["sheets"].get("push")
    summary = []

    # =========================== SOURCE 1: business types ===========================
    if args.only in (None, "types"):
        meta = {}
        if args.csv_types:
            raw = extra.load_results(args.csv_types)
            mp = Path(args.csv_types).with_name("meta.json")
            meta = json.loads(mp.read_text(encoding="utf-8")) if mp.exists() else {}
            n_queries = 0
        else:
            queries, meta = extra.build_type_queries(cfg, rotation, stamp)
            if not queries:
                sys.exit("No business types are enabled in config_extra.yaml")
            n_queries = len(queries)
            raw = extra.load_results(scrape(cfg, queries, meta, out_dir, "types", cfg["scraper"].get("depth", 3)))
        sc, chains = cfg["scoring"], load_chains(cfg)
        rejected, cands, batch, dup = [], [], set(), 0

        def reject(r, reason, score=""):
            rejected.append({"business_name": r["name"], "phone": r.get("phone", ""), "business_type": r.get("business_type", ""),
                             "city": r["city"], "reject_reason": reason, "score": score, "scraped_date": today})

        for r in raw:
            m = meta.get(r["input_id"], {})
            r.update(business_type=m.get("type_name", "Unknown type"), needs=m.get("needs", []), source_query=m.get("query", ""))
            r["phone"] = validate.normalize_phone(r["phone_raw"])
            r["phone_status"] = validate.phone_status(r["phone"])
            keys = extra.Seen.keys_for(r)
            if seen.has(keys) or any(k in batch for k in keys):
                dup += 1
                continue
            batch.update(keys)
            r["keys"] = keys
            if "closed" in r["status"].lower():
                reject(r, "business closed"); continue
            if not validate.is_canadian(r["province"], r["postal"], r["address"], r["country"]):
                reject(r, "not in Canada"); continue
            if not r["phone"]:
                reject(r, "no valid phone number"); continue
            if validate.chain_match(r["name"], chains):
                reject(r, "chain or franchise"); continue
            if r["review_count"] > int(sc.get("max_review_count", 1500)):
                reject(r, f"too large ({r['review_count']} reviews)"); continue
            cands.append(r)
        print(f"[types] {len(raw)} scraped, {dup} duplicate, {len(raw) - dup} new")

        e = cfg.get("enrich", {})
        todo = [r for r in cands if r["website"] and "/" not in extra.site_key(r["website"])]
        for r in cands:
            r.update(email=(validate.clean_email(r["emails"][0]) if r["emails"] else None), digital={}, site_checked=False)
        if e.get("enabled", True) and not args.no_enrich and todo:
            res = enrich_mod.enrich_many([(i, r["website"], r["emails"]) for i, r in enumerate(todo)],
                                         e.get("workers", 8), e.get("timeout_seconds", 10), e.get("check_robots", True))
            for i, r in enumerate(todo):
                r.update(email=res[i].get("email") or r["email"], digital=res[i].get("digital") or {},
                         site_checked=bool(res[i].get("fetch_ok")))
            print(f"[types] visited {len(todo)} websites")

        good = []
        for r in cands:
            s = extra.score_type_lead(r, cfg)
            r.update(s)
            if sc.get("require_need_signal", True) and not s["need"]:
                reject(r, "no sign of need (has a proper, current website)", s["score"]); continue
            if s["score"] < sc.get("min_score", 45):
                reject(r, f"score {s['score']} below {sc.get('min_score', 45)}", s["score"]); continue
            good.append(r)
        good.sort(key=lambda x: (-x["score"], -(x["review_count"] or 0)))
        good = good[:int(cfg["market"].get("daily_cap", 100))]
        rows = [{"business_name": r["name"], "phone": r["phone"], "email": r["email"] or "", "address": r["address"],
                 "city": r["city"], "province": r["province"], "business_type": r["business_type"], "score": r["score"],
                 "services_to_pitch": r["services_to_pitch"], "reason": r["reason"], "google_rating": r["rating"],
                 "review_count": r["review_count"], "website": r["website"], "maps_url": r["maps_url"],
                 "source": "google_maps_business_types", "source_query": r["source_query"], "scraped_date": today} for r in good]
        export.write_csv(out_dir / "type_leads.csv", extra.LEAD_COLS, rows)
        export.write_csv(out_dir / "type_rejected.csv", extra.REJECT_COLS, rejected)
        if push and rows:
            export.push({"sheets": cfg["sheets"]}, cfg["sheets"].get("leads_tab", "Extra_Leads"), extra.LEAD_COLS, rows)
        for r in good:
            seen.add(r["keys"])
        summary.append(f"Business types: {n_queries} searches | {len(rows)} leads saved | {len(rejected)} rejected")

    # =========================== SOURCE 2: provider complaints ===========================
    cc = cfg["complaints"]
    if args.only in (None, "providers") and cc.get("enabled", True):
        meta, extended = {}, cc.get("reviews_mode", "sample") == "extended"
        if args.csv_providers:
            raw = extra.load_results(args.csv_providers)
            mp = Path(args.csv_providers).with_name("meta.json")
            meta = json.loads(mp.read_text(encoding="utf-8")) if mp.exists() else {}
            n_queries = 0
        else:
            queries, meta = extra.build_provider_queries(cfg, rotation, stamp)
            n_queries = len(queries)
            raw = extra.load_results(scrape(cfg, queries, meta, out_dir, "providers", cfg["scraper"].get("provider_depth", 2), extended)) if queries else []
        finder = extra.ComplaintFinder(cc)
        complaints, by_provider, listings, dup = [], defaultdict(list), 0, 0
        for p in raw:
            m = meta.get(p["input_id"], {})
            if not validate.is_canadian(p["province"], p["postal"], p["address"], p["country"]) or "closed" in p["status"].lower():
                continue
            pk = f"listing:{p['place_id']}"
            if pk in by_provider:
                continue
            listings += 1
            by_provider[pk] = []
            for c in finder.scan(p):
                if seen_complaints.has([f"review:{c['id']}"]):
                    dup += 1
                    continue
                seen_complaints.add([f"review:{c['id']}"])
                rv = c["rv"]
                row = {"reviewer_name": rv["name"], "reviewer_profile_url": rv["url"],
                       "rating": int(rv["rating"]) if rv["rating"] else "", "review_date": rv["published"].date().isoformat() if rv["published"] else rv["when"],
                       "provider_name": p["name"], "provider_kind": m.get("kind", ""), "provider_city": p["city"],
                       "topics": "; ".join(c["topics"]), "quote": c["quote"], "business_hint": c["hint"],
                       "reviewer_name_looks_like_business": "yes" if finder.name_looks_like_business(rv["name"]) else "no",
                       "provider_maps_url": p["maps_url"], "source": "google_maps_provider_reviews", "scraped_date": today}
                complaints.append(row)
                by_provider[pk].append((p, row))
        summ = []
        for pk, items in by_provider.items():
            if not items:
                continue
            p = items[0][0]
            top = Counter(t for _, r in items for t in r["topics"].split("; ")).most_common(3)
            summ.append({"provider_name": p["name"], "provider_kind": items[0][1]["provider_kind"], "provider_city": p["city"],
                         "google_rating": p["rating"], "review_count": p["review_count"], "complaints_found": len(items),
                         "top_topics": "; ".join(f"{t} ({n})" for t, n in top), "provider_maps_url": p["maps_url"]})
        summ.sort(key=lambda x: -x["complaints_found"])
        complaints.sort(key=lambda x: str(x["review_date"]), reverse=True)          # newest first
        complaints.sort(key=lambda x: (x["reviewer_name_looks_like_business"] != "yes", not x["business_hint"]))  # business clues first
        export.write_csv(out_dir / "provider_complaints.csv", extra.COMPLAINT_COLS, complaints)
        export.write_csv(out_dir / "provider_summary.csv", extra.SUMMARY_COLS, summ)
        if push and complaints:
            export.push({"sheets": cfg["sheets"]}, cfg["sheets"].get("complaints_tab", "Provider_Complaints"), extra.COMPLAINT_COLS, complaints)
        print(f"[providers] {listings} provider listings read, {dup} already seen, {len(complaints)} new complaints")
        summary.append(f"Provider complaints: {n_queries} searches | {listings} listings | {len(complaints)} complaints "
                       f"({sum(1 for c in complaints if c['business_hint'] or c['reviewer_name_looks_like_business'] == 'yes')} with a business clue)")

    seen.save(); seen_complaints.save()
    rot_path.write_text(json.dumps(rotation))
    print("\n=== Extra sources summary ===")
    for s in summary:
        print(s)
    print(f"Files in: {out_dir}")


if __name__ == "__main__":
    sys.exit(main())
