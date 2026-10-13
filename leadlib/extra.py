"""Extra sources for Canada: 15 new business types and the provider-complaint source (bad Google Maps reviews
that business customers left about their POS, payment, website, marketing or bookkeeping provider).
Everything editable lives in config_extra.yaml. Self-contained: it only reuses stable helpers from
validate.py, scraper.py (queries and Docker run) and enrich.py. It does not change the Canada pipeline."""
import csv
import hashlib
import itertools
import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import yaml

from . import validate

csv.field_size_limit(10_000_000)

LEAD_COLS = ["business_name", "phone", "email", "address", "city", "province", "business_type", "score",
             "services_to_pitch", "reason", "google_rating", "review_count", "website", "maps_url", "source",
             "source_query", "scraped_date"]
REJECT_COLS = ["business_name", "phone", "business_type", "city", "reject_reason", "score", "scraped_date"]
COMPLAINT_COLS = ["reviewer_name", "reviewer_profile_url", "rating", "review_date", "provider_name", "provider_kind",
                  "provider_city", "topics", "quote", "business_hint", "reviewer_name_looks_like_business",
                  "provider_maps_url", "source", "scraped_date"]
SUMMARY_COLS = ["provider_name", "provider_kind", "provider_city", "google_rating", "review_count",
                "complaints_found", "top_topics", "provider_maps_url"]
SHARED_HOSTS = {"facebook.com", "m.facebook.com", "instagram.com", "linktr.ee", "linktree.com", "business.site",
                "square.site", "google.com", "tiktok.com", "youtube.com", "linkedin.com", "twitter.com", "x.com",
                "yelp.com", "yelp.ca", "ubereats.com", "doordash.com", "skipthedishes.com", "toasttab.com"}


def load_config(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ------------------------------------------------------------------ small helpers
def site_key(url):
    if not url:
        return ""
    u = url.strip().lower()
    p = urlparse(u if "://" in u else "http://" + u)
    host = p.netloc.split("@")[-1].split(":")[0]
    host = host[4:] if host.startswith("www.") else host
    if host in SHARED_HOSTS:
        seg = [s for s in p.path.split("/") if s][:1]
        return f"{host}/{seg[0]}" if seg else ""
    return host


def _name_key(name, city):
    n = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower().replace("&", " and "))
    n = " ".join(w for w in n.split() if w not in {"inc", "ltd", "the", "corp", "co"})
    c = re.sub(r"[^a-z0-9]", "", (city or "").lower())
    return f"name:{n}|{c}" if n and c else ""


def _json(text, default):
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        return default


def _num(x):
    try:
        return float(str(x).replace(",", ""))
    except Exception:  # noqa: BLE001
        return 0.0


def _g(d, *names):
    """Case-insensitive dictionary read, so slightly different key spellings still work."""
    low = {str(k).lower(): v for k, v in d.items()}
    for n in names:
        v = low.get(n.lower())
        if v not in (None, ""):
            return v
    return ""


def _when(s):
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return None


def _age_months(published, when_text, now):
    if published:
        return (now - published).days / 30.4
    m = re.search(r"(\d+|an?)\s+(day|week|month|year)s?\s+ago", str(when_text or ""), re.I)
    if not m:
        return None
    n = 1 if m.group(1).lower() in ("a", "an") else int(m.group(1))
    return n * {"day": 1 / 30.4, "week": 7 / 30.4, "month": 1, "year": 12}[m.group(2).lower()]


def _phrase_regexes(phrases):
    out = []
    for p in phrases or []:
        p = str(p).strip()
        if p:
            out.append((p, re.compile(r"(?<!\w)" + re.escape(p).replace(r"\ ", r"\s+") + r"(?!\w)", re.I)))
    return out


# ------------------------------------------------------------------ queries
def _interleave(types, locs):
    combos = []
    for kwi in range(max((len(t["keywords"]) for t in types), default=0)):
        for loc in locs:
            for t in types:
                if kwi < len(t["keywords"]):
                    combos.append((t, t["keywords"][kwi], loc))
    return combos


def build_type_queries(cfg, rotation, stamp):
    types = [t for t in cfg["business_types"] if t.get("enabled", True)]
    combos = _interleave(types, cfg["market"]["cities"])
    if not combos:
        return [], {}
    n = min(int(cfg["market"].get("queries_per_run", 20)), len(combos))
    start = int(rotation.get("types", 0)) % len(combos)
    rotation["types"] = (start + n) % len(combos)
    queries, meta = [], {}
    for i in range(n):
        t, kw, loc = combos[(start + i) % len(combos)]
        qid = f"xt{stamp}x{i}"
        queries.append({"id": qid, "text": f"{kw} in {loc}"})
        meta[qid] = {"type_name": t["name"], "needs": t.get("needs", []), "query": f"{kw} in {loc}"}
    return queries, meta


def build_provider_queries(cfg, rotation, stamp):
    c = cfg["complaints"]
    combos = [(name, "Named provider", f"{name} {loc}") for loc in c.get("provider_cities", []) for name in c.get("provider_names", [])]
    for grp in c.get("provider_keywords", []):
        for kw in grp["keywords"]:
            for loc in cfg["market"]["cities"]:
                combos.append((kw, grp["kind"], f"{kw} in {loc}"))
    if not combos:
        return [], {}
    n = min(int(c.get("provider_queries_per_run", 24)), len(combos))
    start = int(rotation.get("providers", 0)) % len(combos)
    rotation["providers"] = (start + n) % len(combos)
    queries, meta = [], {}
    for i in range(n):
        _, kind, text = combos[(start + i) % len(combos)]
        qid = f"xp{stamp}x{i}"
        queries.append({"id": qid, "text": text})
        meta[qid] = {"kind": kind, "query": text}
    return queries, meta


def write_queries(queries, path):
    with open(path, "w", encoding="utf-8") as f:
        for q in queries:
            f.write(f"{q['text']} #!#{q['id']}\n")


def run_extended(cfg, queries_path, out_dir, results_name, depth):
    """Docker run that also collects extra reviews. Needs JSON output. The JSON layout was NOT tested here."""
    s = cfg["scraper"]
    out_dir, queries_path = Path(out_dir).resolve(), Path(queries_path).resolve()
    if not shutil.which("docker") and s.get("mode", "docker") != "binary":
        raise RuntimeError("Docker was not found. Start Docker Desktop first.")
    flags = ["-depth", str(depth), "-c", str(s.get("concurrency", 4)), "-exit-on-inactivity",
             str(s.get("exit_on_inactivity", "3m")), "-lang", s.get("lang", "en"), "-extra-reviews", "-json"]
    if s.get("mode", "docker") == "binary":
        cmd = [s["binary_path"], "-input", str(queries_path), "-results", str(out_dir / results_name)] + flags
    else:
        cmd = ["docker", "run", "--rm", "-v", "gmaps-playwright-cache:/opt", "-v", f"{queries_path}:/queries.txt:ro",
               "-v", f"{out_dir}:/out", "gosom/google-maps-scraper", "-input", "/queries.txt",
               "-results", f"/out/{results_name}"] + flags
    print("Running scraper (extended reviews):", " ".join(cmd))
    subprocess.run(cmd, check=True)
    return out_dir / results_name


# ------------------------------------------------------------------ reading scraper output (CSV, or JSON for extended)
def _reviews_from(items):
    out = []
    for rv in items or []:
        if not isinstance(rv, dict):
            continue
        texts = [str(_g(rv, k) or "") for k in ("Description", "text_original", "text_translated", "text")]
        out.append({"name": str(_g(rv, "Name", "author", "author_name") or ""),
                    "url": str(_g(rv, "author_url", "profile_url") or ""),
                    "rating": _num(_g(rv, "rating_float", "Rating", "stars")),
                    "texts": [t for t in dict.fromkeys(texts) if t],
                    "published": _when(_g(rv, "published_at")), "when": str(_g(rv, "When") or ""),
                    "id": str(_g(rv, "review_id") or "")})
    return out


def _row_from(raw):
    addr = str(_g(raw, "address") or "").strip()
    comp = _g(raw, "complete_address")
    comp = comp if isinstance(comp, dict) else _json(str(comp or ""), {})
    comp = comp if isinstance(comp, dict) else {}
    postal = validate.parse_postal(str(_g(comp, "postal_code", "postalCode") or "")) or validate.parse_postal(addr)
    prov = str(_g(comp, "state", "province") or "").strip().upper()
    if prov not in validate.PROVINCES:
        prov = validate.province_from_text(str(_g(comp, "state", "province") or "")) or validate.province_from_text(addr) or ""
    city = str(_g(comp, "city") or "")
    if not city:
        parts = [p.strip() for p in addr.split(",")]
        city = parts[-3] if len(parts) >= 3 else ""
    reviews = _g(raw, "user_reviews")
    reviews = reviews if isinstance(reviews, list) else _json(str(reviews or ""), [])
    ext = _g(raw, "user_reviews_extended")
    ext = ext if isinstance(ext, list) else _json(str(ext or ""), [])
    emails = _g(raw, "emails")
    emails = emails if isinstance(emails, list) else [e for e in re.split(r"[,;|\s\[\]\"']+", str(emails or "")) if "@" in e]
    return {"input_id": str(_g(raw, "input_id") or "").strip(),
            "place_id": str(_g(raw, "place_id", "cid", "link") or ""), "name": str(_g(raw, "title") or "").strip(),
            "category": str(_g(raw, "category") or "").strip(), "address": addr, "city": city, "province": prov,
            "postal": postal or "", "country": str(_g(comp, "country") or ""),
            "phone_raw": str(_g(raw, "phone") or "").strip(), "website": str(_g(raw, "website") or "").strip(),
            "rating": _num(_g(raw, "review_rating")), "review_count": int(_num(_g(raw, "review_count"))),
            "status": str(_g(raw, "status") or "").strip(), "emails": emails, "maps_url": str(_g(raw, "link") or ""),
            "reviews": _reviews_from(list(reviews or []) + list(ext or []))}


def load_results(path):
    p = Path(path)
    rows = []
    if p.suffix.lower() == ".json":
        text = p.read_text(encoding="utf-8-sig").strip()
        data = _json(text, None)
        if data is None:  # one JSON object per line
            data = [_json(line, None) for line in text.splitlines() if line.strip()]
        for obj in (data if isinstance(data, list) else [data]):
            if isinstance(obj, dict):
                rows.append(_row_from(obj))
        return rows
    with open(p, newline="", encoding="utf-8-sig") as f:
        return [_row_from(r) for r in csv.DictReader(f)]


# ------------------------------------------------------------------ source 2: provider complaints
class ComplaintFinder:
    def __init__(self, ccfg):
        self.max_rating = float(ccfg.get("max_rating", 2))
        self.max_age = float(ccfg.get("max_age_months", 24))
        self.topics = [(t["topic"], _phrase_regexes(t.get("phrases"))) for t in ccfg.get("topics", [])]
        self.owner = _phrase_regexes(ccfg.get("owner_clues"))
        self.biz_words = {w.lower() for w in ccfg.get("business_name_words", [])}

    def name_looks_like_business(self, name):
        return bool(set(re.findall(r"[a-z]+", (name or "").lower())) & self.biz_words)

    def scan(self, provider, now=None):
        """One record per bad review that matches at least one complaint topic."""
        now = now or datetime.now(timezone.utc)
        found = []
        for rv in provider["reviews"]:
            if rv["rating"] and rv["rating"] > self.max_rating:
                continue
            age = _age_months(rv["published"], rv["when"], now)
            if age is not None and age > self.max_age:
                continue
            text = " ".join(rv["texts"])
            topics = []
            for topic, pats in self.topics:
                if any(rx.search(text) for _, rx in pats):
                    topics.append(topic)
            if not topics:
                continue
            hint = next((m.group(0) for _, rx in self.owner for m in [rx.search(text)] if m), "")
            first = next((rx.search(text) for _, rx in sum((p for t, p in self.topics if t == topics[0]), []) if rx.search(text)), None)
            start = max(0, (first.start() if first else 0) - 80)
            found.append({"rv": rv, "topics": topics, "hint": hint,
                          "quote": re.sub(r"\s+", " ", text[start:start + 240]).strip(),
                          "id": rv["id"] or hashlib.sha1(f"{provider['place_id']}|{rv['name']}|{text[:80]}".encode()).hexdigest()[:16]})
        return found


# ------------------------------------------------------------------ source 1: scoring business-type leads
def _site_gaps(d):
    gaps = []
    if d.get("no_viewport"):
        gaps.append("not mobile-friendly")
    if d.get("no_https"):
        gaps.append("no secure https")
    cy = d.get("copyright_year") or 0
    if cy and cy <= datetime.now().year - 3:
        gaps.append(f"footer still says {cy}")
    if d.get("builder"):
        gaps.append(f"basic {d['builder']} template")
    return gaps


def score_type_lead(lead, cfg):
    sc, w = cfg["scoring"], cfg["scoring"]["weights"]
    parts, need = [], False
    parts.append((w["business_type_fit"], f"Business type: {lead['business_type']}"))
    if lead.get("phone_status") == "ok":
        parts.append((w["phone"], "Valid Canadian phone number"))
    elif lead.get("phone"):
        parts.append((w["phone"] // 2, "Phone number present"))
    if lead.get("email"):
        parts.append((w["email"], "Email found"))
    rc = lead.get("review_count") or 0
    proper = bool(lead.get("website")) and "/" not in site_key(lead["website"])
    if not proper and rc >= sc.get("min_review_count_for_website_gap", 10):
        parts.append((w["no_proper_website"], "No website" if not lead.get("website") else "Only a social page, no real website"))
        need = True
    elif proper and lead.get("site_checked") and lead.get("digital"):
        gaps = _site_gaps(lead["digital"])
        if gaps:
            parts.append((w["dated_website"], "Website looks dated: " + ", ".join(gaps)))
            need = True
    if 10 <= rc <= 1000:
        parts.append((w["active_business"], f"Active business ({rc} Google reviews)"))
    services = list(lead.get("needs") or [])
    if not proper and "Website" not in services:
        services.append("Website")
    return {"score": min(100, sum(p for p, _ in parts)), "need": need,
            "reason": "; ".join(t for _, t in sorted(parts, key=lambda x: -x[0])), "services_to_pitch": ", ".join(services)}


# ------------------------------------------------------------------ duplicate memory
class Seen:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.keys = set(_json(self.path.read_text(encoding="utf-8"), []) if self.path.exists() else [])

    @staticmethod
    def keys_for(lead):
        k = []
        if lead.get("place_id"):
            k.append(f"place:{lead['place_id']}")
        if lead.get("phone"):
            k.append(f"phone:{lead['phone']}")
        if site_key(lead.get("website") or ""):
            k.append(f"site:{site_key(lead['website'])}")
        nk = _name_key(lead.get("name"), lead.get("city"))
        if nk:
            k.append(nk)
        return k

    def has(self, keys):
        return any(x in self.keys for x in keys)

    def add(self, keys):
        self.keys.update(keys)

    def save(self):
        self.path.write_text(json.dumps(sorted(self.keys)), encoding="utf-8")
