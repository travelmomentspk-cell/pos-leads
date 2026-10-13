"""Persistent Master list: every lead ever processed, with duplicate matching on
phone, website, place_id and business name. Stored in state/master.db (SQLite)."""
import csv
import difflib
import re
import sqlite3
import unicodedata
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from .validate import normalize_phone

# Many small businesses list a social or ordering page as their "website". Those hosts are shared by
# thousands of businesses, so for them the first path segment is part of the key
# (facebook.com/maple-cafe, never just facebook.com).
SHARED_HOSTS = {"facebook.com", "m.facebook.com", "instagram.com", "linktr.ee", "linktree.com", "business.site",
                "wixsite.com", "square.site", "weebly.com", "godaddysites.com", "squarespace.com", "google.com",
                "ubereats.com", "doordash.com", "skipthedishes.com", "toasttab.com", "order.online", "yelp.com",
                "yelp.ca", "tiktok.com", "youtube.com", "linkedin.com", "twitter.com", "x.com", "wa.me"}
LEGAL_WORDS = {"inc", "incorporated", "ltd", "limited", "corp", "corporation", "co", "company", "llc", "the"}
NAME_MATCH = 0.90  # name similarity needed (and the city must be the same) to call two names the same business


def site_key(url):
    if not url:
        return ""
    u = url.strip().lower()
    if "://" not in u:
        u = "http://" + u
    p = urlparse(u)
    host = p.netloc.split("@")[-1].split(":")[0]
    host = host[4:] if host.startswith("www.") else host
    if host in SHARED_HOSTS:
        seg = [s for s in p.path.split("/") if s][:1]
        return f"{host}/{seg[0]}" if seg else ""  # a bare facebook.com says nothing about which business
    return host


def norm_name(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower().replace("&", " and ")
    return " ".join(w for w in re.sub(r"[^a-z0-9 ]+", " ", s).split() if w not in LEGAL_WORDS)


def city_key(c):
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", c or "").encode("ascii", "ignore").decode().lower())


def make_keys(name="", phone="", website="", city="", place_id=""):
    return (place_id or "", normalize_phone(phone) or "", site_key(website), norm_name(name), city_key(city))


def _similar(a, b):
    if re.findall(r"\d+", a) != re.findall(r"\d+", b):  # "Barber 1" and "Barber 2" are different shops
        return False
    sm = difflib.SequenceMatcher(None, a, b)
    return sm.real_quick_ratio() >= NAME_MATCH and sm.quick_ratio() >= NAME_MATCH and sm.ratio() >= NAME_MATCH


class _Index:
    def __init__(self):
        self.place, self.phone, self.site, self.names = set(), set(), set(), {}

    def add(self, pid, ph, site, nn, city):
        if pid:
            self.place.add(pid)
        if ph:
            self.phone.add(ph)
        if site:
            self.site.add(site)
        if nn and city:
            self.names.setdefault(city, []).append(nn)

    def match(self, pid, ph, site, nn, city):
        """Return why this is a duplicate ('place_id', 'phone', 'website', 'name') or None."""
        if pid and pid in self.place:
            return "place_id"
        if ph and ph in self.phone:
            return "phone"
        if site and site in self.site:
            return "website"
        if nn and city and len(nn) >= 4:
            for old in self.names.get(city, []):
                if old == nn or _similar(old, nn):
                    return "name"
        return None


class Master:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.execute("""CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT, place_id TEXT, phone TEXT, site TEXT, name_norm TEXT,
            city TEXT, business_name TEXT, status TEXT, added_on TEXT)""")
        self.db.commit()
        self.idx = _Index()
        for row in self.db.execute("SELECT place_id, phone, site, name_norm, city FROM leads"):
            self.idx.add(*row)

    def count(self):
        return self.db.execute("SELECT COUNT(*) FROM leads").fetchone()[0]

    def split(self, rows):
        """rows: scraper dicts (name, phone_raw, website, city, place_id). Returns (new_rows, duplicate_rows).
        Duplicates inside the same batch are caught too."""
        batch, new, dup = _Index(), [], []
        for r in rows:
            k = make_keys(r.get("name"), r.get("phone_raw") or r.get("phone"), r.get("website"), r.get("city"), r.get("place_id"))
            reason = self.idx.match(*k) or batch.match(*k)
            if reason:
                r["duplicate_of"] = reason
                dup.append(r)
            else:
                batch.add(*k)
                new.append(r)
        return new, dup

    def add(self, status, name="", phone="", website="", city="", place_id=""):
        k = make_keys(name, phone, website, city, place_id)
        if not any(k[:3]) and not k[3]:
            return False
        self.db.execute("INSERT INTO leads (place_id, phone, site, name_norm, city, business_name, status, added_on) "
                        "VALUES (?,?,?,?,?,?,?,?)", (*k, name or "", status, date.today().isoformat()))
        self.idx.add(*k)
        return True

    def add_decided(self, groups, raw_by_place_id):
        """groups: [(status, [row dicts with place_id, business_name, website, phone])]. Saves each lead once."""
        n = 0
        for status, rows in groups:
            for d in rows:
                src = raw_by_place_id.get(d.get("place_id"), {})
                k = make_keys(d.get("business_name") or src.get("name"), src.get("phone_raw") or d.get("phone"),
                              d.get("website") or src.get("website"), src.get("city"), d.get("place_id"))
                if self.idx.match(*k):
                    continue
                n += self.add(status, d.get("business_name") or src.get("name") or "", src.get("phone_raw") or d.get("phone") or "",
                              d.get("website") or src.get("website") or "", src.get("city") or "", d.get("place_id") or "")
        self.db.commit()
        return n

    def seed_csvs(self, paths):
        """Import earlier leads from any CSV that has some of: business_name/title, phone, website, place_id, city."""
        n = 0
        for p in paths:
            stem = Path(p).stem
            status = ("qualified" if "qualified" in stem else "review" if "review" in stem
                      else "rejected" if "reject" in stem else "imported")
            with open(p, newline="", encoding="utf-8-sig") as f:
                for r in csv.DictReader(f):
                    name = r.get("business_name") or r.get("title") or r.get("name") or ""
                    phone = r.get("phone") or r.get("phone_raw") or ""
                    web, city, pid = r.get("website") or "", r.get("city") or "", r.get("place_id") or ""
                    k = make_keys(name, phone, web, city, pid)
                    if self.idx.match(*k):
                        continue
                    n += self.add(status, name, phone, web, city, pid)
        self.db.commit()
        return n

    def seed_from_out(self, out_dir):
        files = []
        for name in ("qualified_leads.csv", "review_queue.csv", "rejected_log.csv"):
            files += sorted(Path(out_dir).glob(f"*/{name}"))
        return self.seed_csvs(files)
