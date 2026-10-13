"""Where candidates come from. Every source returns queries for the free Maps scraper plus metadata that
explains WHY the business is interesting (the 'signal'). More sources can be added here."""
import itertools
import re
from datetime import date, timedelta

import requests

VAN_API = "https://opendata.vancouver.ca/api/explore/v2.1/catalog/datasets/business-licences/records"
VAN_PAGE = "https://opendata.vancouver.ca/explore/dataset/business-licences/"


def category_queries(cfg, state, run_stamp):
    cats = cfg["targets"]["categories"]
    cities = cfg["targets"]["cities"]
    combos = list(itertools.product(cities, cats))
    n = int(cfg.get("queries_per_run", 24))
    start = int(state.rotation.get("next_index", 0)) % len(combos)
    picked = [combos[(start + i) % len(combos)] for i in range(min(n, len(combos)))]
    state.rotation["next_index"] = (start + len(picked)) % len(combos)
    queries, meta = [], {}
    for i, (city, cat) in enumerate(picked):
        qid = f"cat{run_stamp}x{i}"
        text = f"{cat} in {city}"
        queries.append({"id": qid, "text": text})
        meta[qid] = {"source": "google_maps_search", "query": text, "source_url": "",
                     "signal_type": "", "signal_text": "", "signal_date": ""}
    return queries, meta


def _g(rec, *names):
    for n in names:
        v = rec.get(n)
        if v not in (None, ""):
            return str(v).strip()
    return ""


def vancouver_records(cfg_src, limit=None):
    since = (date.today() - timedelta(days=int(cfg_src.get("days_back", 45)))).isoformat()
    want = int(limit or cfg_src.get("max_records", 150))
    params = {"limit": 100, "offset": 0, "order_by": "issueddate desc",
              "where": f"issueddate >= date'{since}'"}
    out = []
    while len(out) < want:
        r = requests.get(VAN_API, params=params, timeout=30)
        r.raise_for_status()
        batch = r.json().get("results", [])
        if not batch:
            break
        out += batch
        params["offset"] += 100
        if len(batch) < 100:
            break
    return out[:want]


def vancouver_queries(cfg, run_stamp):
    src = cfg["sources"]["vancouver_licences"]
    kws = [k.lower() for k in src.get("business_type_keywords", [])]
    queries, meta, seen = [], {}, set()
    for i, rec in enumerate(vancouver_records(src)):
        btype = _g(rec, "businesstype", "business_type")
        if kws and not any(re.search(r"(?<![a-z])" + re.escape(k), btype.lower()) for k in kws):
            continue
        if re.search(r"wholesale|property|real estate|rental|contractor|consult|office|manufactur|storage", btype, re.I):
            continue
        status = _g(rec, "status").lower()
        if status and status not in ("issued", "active"):
            continue
        name = _g(rec, "businesstradename", "business_trade_name") or _g(rec, "businessname", "business_name")
        legal = _g(rec, "businessname", "business_name")
        street = " ".join(x for x in [_g(rec, "house"), _g(rec, "street")] if x)
        if not name or not street:
            continue
        key = (name.lower(), street.lower())
        if key in seen:
            continue
        seen.add(key)
        issued = _g(rec, "issueddate", "issued_date")[:10]
        qid = f"lic{run_stamp}x{i}"
        queries.append({"id": qid, "text": f"{name}, {street}, Vancouver BC"})
        meta[qid] = {
            "source": "licence_vancouver", "query": name, "source_url": VAN_PAGE,
            "signal_type": "new_business_licence", "signal_date": issued,
            "signal_text": f"Business licence issued {issued} ({btype}), City of Vancouver open data",
            "match_names": [n for n in {name, legal} if n],
        }
    return queries, meta


def socrata_queries(cfg, run_stamp):
    """Generic adapter. Each portal entry in config.yaml needs: city, province, dataset_url (the
    https://data.<city>.ca/resource/<id>.json URL), date_field, name_field, address_field, type_field."""
    queries, meta = [], {}
    for p in cfg["sources"].get("socrata_licences", {}).get("portals", []):
        since = (date.today() - timedelta(days=int(p.get("days_back", 45)))).strftime("%Y-%m-%dT00:00:00")
        params = {"$limit": int(p.get("max_records", 150)), "$order": f"{p['date_field']} DESC",
                  "$where": f"{p['date_field']} >= '{since}'"}
        r = requests.get(p["dataset_url"], params=params, timeout=30)
        r.raise_for_status()
        for i, rec in enumerate(r.json()):
            name, addr = _g(rec, p["name_field"]), _g(rec, p["address_field"])
            if not name or not addr:
                continue
            issued = _g(rec, p["date_field"])[:10]
            qid = f"soc{run_stamp}{p['city'][:3].lower()}x{i}"
            queries.append({"id": qid, "text": f"{name}, {addr}, {p['city']} {p['province']}"})
            meta[qid] = {"source": f"licence_{p['city'].lower()}", "query": name, "source_url": p["dataset_url"],
                         "signal_type": "new_business_licence", "signal_date": issued,
                         "signal_text": f"Business licence issued {issued} ({_g(rec, p.get('type_field', ''))}), "
                                        f"{p['city']} open data",
                         "match_names": [name]}
    return queries, meta
