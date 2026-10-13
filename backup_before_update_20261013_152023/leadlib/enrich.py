"""Fetch a business homepage (and one contact page) to find a public email, the current payment/POS
provider and online-ordering signals. Polite: robots.txt respected, short timeouts, small downloads."""
import html as htmlmod
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests

from .validate import clean_email, domain_of

UA = "Mozilla/5.0 (compatible; POSLeadResearch/1.0; business contact research)"
MAX_BYTES = 600_000

POS_SIGNATURES = {
    "Square": ["squareup.com", "square.site", "squarecdn.com", "web-payments-sdk"],
    "Clover": ["clover.com", "cloverpos"],
    "Toast": ["toasttab.com", "toast-tab"],
    "Lightspeed": ["lightspeedhq", "lightspeedapp", "shoplightspeed", "lsretail"],
    "Shopify store": ["cdn.shopify.com", "myshopify.com"],
    "Moneris": ["moneris"],
    "Helcim": ["helcim"],
    "Stripe": ["js.stripe.com"],
    "PayPal": ["paypalobjects.com", "paypal.com/sdk"],
    "Zettle": ["zettle.com"],
    "SumUp": ["sumup.com"],
    "TouchBistro": ["touchbistro"],
    "Revel": ["revelsystems"],
    "SkipTheDishes": ["skipthedishes"],
    "DoorDash": ["doordash.com"],
    "Uber Eats": ["ubereats.com"],
}
ONLINE_ORDER_HINTS = ["order online", "online ordering", "order now", "add to cart", "toasttab.com",
                      "square.site", "skipthedishes", "doordash.com", "ubereats.com", "chownow", "ordering.", "/order"]
EMAIL_FIND = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
MAILTO = re.compile(r"mailto:([^\"'?>\s]+)", re.I)

_robots_cache = {}
_robots_lock = threading.Lock()


def _robots_ok(url, timeout):
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    with _robots_lock:
        rp = _robots_cache.get(base)
    if rp is None:
        rp = RobotFileParser()
        try:
            r = requests.get(base + "/robots.txt", headers={"User-Agent": UA}, timeout=min(timeout, 5))
            rp.parse(r.text.splitlines() if r.status_code == 200 else [])
        except Exception:  # noqa: BLE001
            rp.parse([])
        with _robots_lock:
            _robots_cache[base] = rp
    return rp.can_fetch(UA, url)


def _get(url, timeout, check_robots):
    if check_robots and not _robots_ok(url, timeout):
        return None
    try:
        r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "en-CA,en;q=0.8"},
                         timeout=timeout, stream=True, allow_redirects=True)
        if r.status_code >= 400 or "html" not in r.headers.get("Content-Type", "html").lower():
            return None
        data = b""
        for chunk in r.iter_content(65536):
            data += chunk
            if len(data) >= MAX_BYTES:
                break
        return r.url, data.decode(r.encoding or "utf-8", errors="ignore")
    except Exception:  # noqa: BLE001
        return None


def _emails(html_text):
    found = []
    for m in MAILTO.findall(html_text):
        found.append(htmlmod.unescape(m))
    found += EMAIL_FIND.findall(htmlmod.unescape(html_text))
    out = []
    for e in found:
        c = clean_email(e)
        if c and c not in out:
            out.append(c)
    return out


def _contact_links(base_url, html_text):
    links = []
    for href in re.findall(r"href=[\"']([^\"'#]+)[\"']", html_text, flags=re.I):
        h = href.lower()
        if any(k in h for k in ("contact", "about", "reach-us", "find-us")) and not h.startswith(("mailto:", "tel:")):
            full = urljoin(base_url, href)
            if domain_of(full) == domain_of(base_url) and full not in links:
                links.append(full)
    return links[:2]


def _text_snippet(html_text, n=1200):
    t = re.sub(r"(?is)<(script|style|noscript|svg).*?</\1>", " ", html_text)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    t = re.sub(r"\s+", " ", htmlmod.unescape(t)).strip()
    return t[:n]


def _pick_email(emails, website):
    wd = domain_of(website)
    own = [e for e in emails if e.rsplit("@", 1)[1].endswith(wd)] if wd else []
    pool = own or emails
    pref = ("info", "hello", "contact", "orders", "order", "sales", "office", "owner", "manager", "admin")
    for p in pref:
        for e in pool:
            if e.startswith(p + "@"):
                return e
    return pool[0] if pool else None


def enrich_one(website, known_emails, timeout=10, check_robots=True):
    res = {"email": None, "email_source_url": "", "emails": [], "pos_detected": [], "online_ordering": False,
           "cash_only": False, "snippet": "", "fetch_ok": False}
    if not website:
        return res
    url = website if website.startswith("http") else "https://" + website
    pages = []
    first = _get(url, timeout, check_robots)
    if first:
        pages.append(first)
        for link in _contact_links(first[0], first[1]):
            second = _get(link, timeout, check_robots)
            if second:
                pages.append(second)
    res["fetch_ok"] = bool(pages)
    emails, source = [], {}
    for pu, ph in pages:
        for e in _emails(ph):
            if e not in emails:
                emails.append(e)
                source[e] = pu
    for e in known_emails or []:
        c = clean_email(e)
        if c and c not in emails:
            emails.append(c)
            source[c] = website + " (found by the Maps scraper's website crawl)"
    best = _pick_email(emails, website)
    res.update(emails=emails, email=best, email_source_url=source.get(best, ""))
    blob = " ".join(ph for _, ph in pages).lower()
    res["pos_detected"] = [n for n, sigs in POS_SIGNATURES.items() if any(s in blob for s in sigs)]
    res["online_ordering"] = any(h in blob for h in ONLINE_ORDER_HINTS)
    res["cash_only"] = bool(re.search(r"cash[ -]only|only (accept|accepting) cash", blob))
    res["snippet"] = _text_snippet(pages[0][1]) if pages else ""
    return res


def enrich_many(items, workers=8, timeout=10, check_robots=True):
    """items: list of (key, website, known_emails). Returns {key: result}."""
    out = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(enrich_one, w, k, timeout, check_robots): key for key, w, k in items}
        for f, key in futs.items():
            try:
                out[key] = f.result()
            except Exception:  # noqa: BLE001
                out[key] = {"email": None, "email_source_url": "", "emails": [], "pos_detected": [],
                            "online_ordering": False, "cash_only": False, "snippet": "", "fetch_ok": False}
    return out
