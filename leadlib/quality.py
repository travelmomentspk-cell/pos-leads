"""Lead quality gate: a 0-100 score, a decision-maker requirement and a plain-language reason.

quality_gate(lead, cfg) -> dict(score, accepted, reject_reason, reason, decision_maker,
                                decision_maker_title, decision_maker_source)
Score budget (max 100): decision maker 25 | phone 10 | email 10 | website gap 15 | bad review 20 |
hiring or expanding 10 | social links but no online ordering/payments 10.
"""
import re

from .score import review_pain

REJECT_COLS = ["business_name", "phone", "website", "score", "reject_reason", "decision_maker", "reason", "scraped_date"]

# title -> (label, points); first match wins, so seniority order matters
TITLES = [
    (r"\bco-?owner\b|\bowner\b|\bproprietor\b", "Owner", 25),
    (r"\bco-?founder\b|\bfounder\b", "Founder", 25),
    (r"\bceo\b|\bchief executive officer\b", "CEO", 25),
    (r"\bpresident\b", "President", 25),
    (r"\bdirector\b|\bmanaging partner\b|\bprincipal\b", "Director", 22),
    (r"\bgeneral manager\b|\bmanager\b", "Manager", 18),
]
STAFF = re.compile(r"\b(cashier|server|waiter|waitress|barista|cook|stylist|receptionist|assistant|intern|clerk|"
                   r"bartender|host|hostess|driver|cleaner|trainee|associate|staff|employee)\b", re.I)
TITLE_WORDS = (r"(?i:co-?owner|owner|proprietor|co-?founder|founder|ceo|chief executive officer|president|"
               r"managing partner|managing director|principal|general manager|director|manager)")
NAME = r"[A-Z][a-zA-Z'’\-]{1,20}(?:\s+[A-Z][a-zA-Z'’\-]{1,20}){1,2}"
NOT_NAME = {"our", "the", "team", "contact", "about", "meet", "welcome", "owner", "founder", "manager", "director",
            "president", "staff", "chef", "home", "menu", "hours", "open", "call", "email", "story", "mission",
            "careers", "order", "online", "reserve", "book", "location", "locations", "info", "us", "and", "of",
            "restaurant", "cafe", "bakery", "salon", "shop", "store", "canada", "toronto", "inc", "ltd", "management",
            "operated", "since", "family", "owned", "run", "local", "proudly", "serving", "locally", "years",
            "certified", "instructor", "instructors", "trainer", "trainers", "therapist", "teacher", "teachers", "coach",
            "coaches", "yoga", "pilates", "licensed", "registered", "senior", "head", "lead", "master", "personal",
            "professional", "expert", "specialist", "technician", "mechanic", "stylist", "hi", "hello", "hey", "dear",
            "thanks", "thank", "greetings", "bio", "biography", "fitness", "dental", "medical", "cleaning", "tutor",
            "tutors", "school", "academy", "centre", "center", "services", "service", "solutions", "group", "team"}
PATTERNS = [
    re.compile(rf"(?P<name>{NAME})(?:\s*[,|:(]\s*|\s+[-–—]\s+)(?:the\s+)?(?:[A-Za-z]+\s*(?:&|and|/)\s*)?(?P<title>{TITLE_WORDS})\b"),
    re.compile(rf"(?:(?i:our)\s+)?(?P<title>{TITLE_WORDS})\s*(?:[:|,]|\s[-–—]\s|(?i:\sis\s))\s*(?P<name>{NAME})"),
    re.compile(rf"(?P<verb>(?i:founded|started|owned|operated|run|managed))\s+(?i:by)\s+(?:(?i:the)\s+)?(?P<name>{NAME})"),
    re.compile(rf"[A-Za-z]+\s*[/&]\s*(?P<title>{TITLE_WORDS})\s+(?P<name>{NAME})"),
    re.compile(rf"(?i:i['’]m|i am|my name is)\s+(?P<name>{NAME})[^.]{{0,60}}?\b(?P<title>(?i:owner|founder|co-?founder|proprietor))\b"),
]
VERB_TITLE = {"founded": "Founder", "started": "Founder", "owned": "Owner", "operated": "Owner",
              "run": "Owner", "managed": "Manager"}

HIRING = re.compile(r"\b(we['’]?re hiring|we are hiring|now hiring|currently hiring|help wanted|join our team|"
                    r"apply now|job openings?|open positions?|we['’]?re expanding|expanding|new location|"
                    r"second location|grand opening|opening soon|coming soon|now open at|we['’]?re growing)\b", re.I)
WEBSITE_PAIN = re.compile(
    r"\bwebsite (is |was )?(down|broken|outdated|not working|doesn'?t work|useless)\b|"
    r"\b(can'?t|couldn'?t|unable to|no way to) (order|book|reserve|pay) online\b|"
    r"\bno online (ordering|booking|payments?|menu)\b|"
    r"\bonline (order|ordering|booking) (is |was |didn'?t |doesn'?t |not )(work|working|available)\b|"
    r"\bwrong (hours|phone number|address)\b", re.I)
SHOP_TOOLS = {"Square", "Clover", "Toast", "Lightspeed", "Shopify store", "Moneris", "Helcim", "Stripe", "PayPal",
              "Zettle", "SumUp", "TouchBistro", "Revel"}
ONLINE_BUY = re.compile(r"\b(add to cart|order online|online ordering|buy now|checkout|book online|book now)\b", re.I)


def classify_title(title):
    """('Owner', 25) etc., or None for general staff or unknown titles."""
    t = title or ""
    for rx, label, pts in TITLES:
        if re.search(rx, t, re.I):
            return label, pts
    return None


def find_decision_maker(pages, business_name=""):
    """pages: [(url, text)]. Looks for 'Jane Doe, Owner' / 'Owner: Jane Doe' / 'Founded by Jane Doe' / 'I'm Jane, the owner'."""
    best = None
    bn = (business_name or "").lower()
    for item in pages or []:
        url, text = item[0], re.sub(r"\s+", " ", item[1] or "")
        for rx in PATTERNS:
            for m in rx.finditer(text):
                raw_words = m.group("name").split()
                while raw_words and raw_words[0].lower() in NOT_NAME:
                    raw_words.pop(0)
                while raw_words and raw_words[-1].lower() in NOT_NAME:
                    raw_words.pop()
                name = " ".join(raw_words)
                if not (2 <= len(raw_words) <= 3) or any(w.lower() in NOT_NAME for w in raw_words) or (bn and name.lower() in bn):
                    continue
                gd = m.groupdict()
                title = gd.get("title") or VERB_TITLE.get((gd.get("verb") or "").lower())
                cls = classify_title(title)
                if not cls:
                    continue
                cand = {"name": name, "title": cls[0], "points": cls[1], "source_url": url}
                if best is None or cand["points"] > best["points"]:
                    best = cand
        if best and best["points"] >= 25:
            break
    return best


def _site_gaps(d):
    gaps = []
    if d.get("no_viewport"):
        gaps.append("not mobile-friendly")
    if d.get("no_https"):
        gaps.append("no secure https connection")
    cy = d.get("copyright_year") or 0
    if cy and cy <= _this_year() - 3:
        gaps.append(f"footer still says {cy}")
    if d.get("builder"):
        gaps.append(f"basic {d['builder']} template")
    return gaps


def _this_year():
    from datetime import date
    return date.today().year


def quality_gate(lead, cfg=None):
    c = {"min_score": 70, "require_decision_maker": True}
    c.update(cfg or {})
    parts = []  # (points, text)
    pages = lead.get("pages") or []
    page_text = " ".join((p[1] or "") for p in pages)

    # 1. decision maker: supplied by another source, or found on the business's own pages
    dm = None
    if lead.get("decision_maker") and classify_title(lead.get("decision_maker_title")):
        label, pts = classify_title(lead["decision_maker_title"])
        dm = {"name": lead["decision_maker"], "title": label, "points": pts,
              "source_url": lead.get("decision_maker_source") or "provided"}
    else:
        dm = find_decision_maker(pages, lead.get("name"))
    if dm:
        parts.append((dm["points"], f"Decision maker: {dm['name']} ({dm['title']}), named at {dm['source_url']}"))

    # 2. contact details
    if lead.get("phone_status") == "ok":
        parts.append((10, "Valid Canadian phone number"))
    elif lead.get("phone"):
        parts.append((5, "Phone number present"))
    es = lead.get("email_status") or ""
    if es == "ok_own_domain":
        parts.append((10, "Email on the business's own domain"))
    elif es in ("ok_other_domain", "ok_freemail"):
        parts.append((6, "Email present"))

    # 3. website missing or dated
    d = lead.get("digital") or {}
    if not lead.get("website"):
        parts.append((15, "No website"))
    elif lead.get("site_checked") and d:
        gaps = _site_gaps(d)
        if gaps:
            parts.append((15 if len(gaps) >= 2 else 10, "Website looks dated: " + ", ".join(gaps)))

    # 4. bad review about payments, POS, website or ordering
    quote = review_pain(lead.get("reviews"))
    kind = "payment or POS"
    if not quote:
        for t in lead.get("reviews") or []:
            m = WEBSITE_PAIN.search(t or "")
            if m:
                quote, kind = re.sub(r"\s+", " ", t[max(0, m.start() - 40):m.end() + 60]).strip(), "website or online ordering"
                break
    if quote:
        parts.append((20, f'A customer review mentions a {kind} problem: "{quote[:120]}"'))

    # 5. hiring or expanding (own homepage, about and contact pages only)
    h = HIRING.search(page_text)
    if h:
        parts.append((10, f'Hiring or expanding: "{h.group(0)}" on their site'))

    # 6. social links but no online ordering or payments (activity itself is not verified)
    social = d.get("has_facebook") or d.get("has_instagram")
    online_buy = (lead.get("online_ordering") or lead.get("order_online")
                  or (set(lead.get("pos_detected") or []) & SHOP_TOOLS) or ONLINE_BUY.search(page_text))
    if social and lead.get("site_checked") and not online_buy:
        parts.append((10, "Has Facebook/Instagram links but no online ordering or payments on its website"
                          + (" and runs Meta ads" if d.get("has_pixel") else "")))

    score = min(100, sum(p for p, _ in parts))
    reject = None
    if c["require_decision_maker"] and not dm:
        reject = "no decision maker (owner, CEO, founder, director, manager) identified"
    elif score < c["min_score"]:
        reject = f"score {score} below {c['min_score']}"
    reason = "; ".join(t for _, t in sorted(parts, key=lambda x: -x[0])) or "No quality signals found"
    return {"score": score, "accepted": reject is None, "reject_reason": reject or "", "reason": reason,
            "decision_maker": dm["name"] if dm else "", "decision_maker_title": dm["title"] if dm else "",
            "decision_maker_source": dm["source_url"] if dm else ""}
