"""Runs the free gosom/google-maps-scraper on this PC and reads its CSV."""
import csv
import json
import re
import shutil
import subprocess
from pathlib import Path

from .validate import PROVINCES, is_canadian, parse_postal, province_from_text


def write_queries(queries, path):
    with open(path, "w", encoding="utf-8") as f:
        for q in queries:
            f.write(f"{q['text']} #!#{q['id']}\n")


def run_scraper(cfg, queries_path, out_dir, results_name, depth):
    s = cfg["scraper"]
    out_dir = Path(out_dir).resolve()
    queries_path = Path(queries_path).resolve()
    results = out_dir / results_name
    if results.exists():
        results.unlink()
    flags = ["-depth", str(depth), "-c", str(s.get("concurrency", 4)),
             "-exit-on-inactivity", str(s.get("exit_on_inactivity", "3m")), "-lang", s.get("lang", "en")]
    if s.get("email", True):
        flags.append("-email")
    if s.get("mode", "docker") == "binary":
        if not s.get("binary_path"):
            raise RuntimeError("scraper.mode is 'binary' but scraper.binary_path is empty in config.yaml")
        cmd = [s["binary_path"], "-input", str(queries_path), "-results", str(results)] + flags
    else:
        if not shutil.which("docker"):
            raise RuntimeError(
                "Docker was not found. Install Docker Desktop (free), or download the scraper binary from "
                "https://github.com/gosom/google-maps-scraper/releases and set scraper.mode: binary in config.yaml."
            )
        cmd = ["docker", "run", "--rm", "-v", "gmaps-playwright-cache:/opt",
               "-v", f"{queries_path}:/queries.txt:ro", "-v", f"{out_dir}:/out",
               "gosom/google-maps-scraper", "-input", "/queries.txt", "-results", f"/out/{results_name}"] + flags
    print("Running scraper:", " ".join(cmd))
    subprocess.run(cmd, check=True)
    return results


def _json_or(text, default):
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        return default


def _split_emails(raw):
    if not raw:
        return []
    data = _json_or(raw, None)
    if isinstance(data, list):
        return [str(x) for x in data]
    return [p for p in re.split(r"[,;|\s]+", raw) if "@" in p]


def _review_texts(raw):
    data = _json_or(raw, [])
    out = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                t = item.get("Description") or item.get("description") or item.get("text") or ""
                if t:
                    out.append(str(t))
            elif isinstance(item, str):
                out.append(item)
    return out


def _num(x):
    try:
        return float(str(x).replace(",", ""))
    except Exception:  # noqa: BLE001
        return 0


def load_results(csv_path):
    """Normalise gosom CSV rows into lead dicts."""
    rows = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        for raw in csv.DictReader(f):
            addr = raw.get("address") or ""
            comp = _json_or(raw.get("complete_address") or "", {})
            comp = comp if isinstance(comp, dict) else {}
            city = comp.get("city") or comp.get("City") or ""
            postal = comp.get("postal_code") or comp.get("PostalCode") or comp.get("postalCode") or ""
            prov = comp.get("state") or comp.get("State") or comp.get("province") or ""
            country = comp.get("country") or comp.get("Country") or ""
            postal = parse_postal(postal) or parse_postal(addr)
            prov_u = (prov or "").strip().upper()
            if prov_u not in PROVINCES:
                prov_u = province_from_text(prov) or province_from_text(addr) or ""
            if not city:
                parts = [p.strip() for p in addr.split(",")]
                city = parts[-3] if len(parts) >= 3 else (parts[1] if len(parts) > 1 else "")
            full = addr
            if postal and postal.replace(" ", "") not in addr.replace(" ", "").upper():
                full = f"{addr} {postal}".strip()
            rows.append({
                "input_id": (raw.get("input_id") or "").strip(),
                "place_id": raw.get("place_id") or raw.get("cid") or raw.get("data_id") or raw.get("link") or "",
                "name": (raw.get("title") or "").strip(),
                "category": (raw.get("category") or "").strip(),
                "address": full,
                "city": city, "province": prov_u, "postal": postal or "", "country": country,
                "phone_raw": (raw.get("phone") or "").strip(),
                "website": (raw.get("website") or "").strip(),
                "rating": _num(raw.get("review_rating")),
                "review_count": int(_num(raw.get("review_count"))),
                "status": (raw.get("status") or "").strip(),
                "emails": _split_emails(raw.get("emails") or ""),
                "link": raw.get("link") or "",
                "order_online": (raw.get("order_online") or "").strip(),
                "credit_cards_accepted": (raw.get("credit_cards_accepted") or "").strip(),
                "reviews": _review_texts(raw.get("user_reviews") or ""),
                "canadian": is_canadian(prov_u, postal, addr, country),
            })
    return rows
