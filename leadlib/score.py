"""Rule-based POS-fit scoring. Free, deterministic and explainable: every point has a stated reason."""
import re
from datetime import date, datetime

HIGH = ["restaurant", "cafe", "café", "coffee", "bakery", "pizza", "bistro", "diner", "pub", "bar", "grill",
        "eatery", "food truck", "ice cream", "juice", "tea", "bubble tea", "dessert", "sushi", "kitchen",
        "deli", "butcher", "hair salon", "hair", "barber", "beauty", "nail", "spa", "salon", "boutique",
        "clothing", "gift", "convenience", "grocery", "florist", "pet store", "book store", "bookstore",
        "jewelry", "vape", "smoke", "market", "bakery", "shop", "store", "brewery", "bar & grill", "catering"]
MEDIUM = ["gym", "fitness", "yoga", "pilates", "tattoo", "laundromat", "dry clean", "phone repair", "print",
          "photo", "car wash", "grooming", "massage", "studio", "optical", "furniture", "hardware"]
EXCLUDE = ["lawyer", "attorney", "law firm", "accountant", "accounting", "tax", "real estate", "realtor",
           "insurance", "bank", "credit union", "church", "mosque", "temple", "synagogue", "school",
           "university", "college", "daycare", "hospital", "dentist", "dental", "doctor", "physician",
           "government", "city hall", "library", "police", "fire station", "hotel", "motel", "apartment",
           "condo", "storage", "manufacturer", "wholesaler", "contractor", "plumber", "electrician",
           "engineer", "consultant", "software", "agency", "cemetery", "funeral", "gas station",
           "car dealer", "auto dealer", "pharmacy", "clinic", "wholesale", "wholesaler", "supplier",
           "supply store", "distributor", "property", "developer", "importer", "exporter"]

POS_PAIN = re.compile(
    r"\bcash[- ]only\b|\bonly (accepts? |takes? )?cash\b|\bno (debit|credit cards?|cards?)\b(?! (surcharge|surcharges|fee|fees|minimum|required|needed))|"
    r"\b(wouldn'?t|won'?t|don'?t|doesn'?t|couldn'?t|can'?t|did not|does not|do not) (take|accept|use) (a |any )?(card|cards|debit|credit|tap)\b|"
    r"\b(card|cards|tap|interac|debit|payment|payments|terminal)\b[^.!?]{0,30}\b(not working|isn'?t working|wasn'?t working|broken|declined|doesn'?t work|didn'?t work|glitch\w*|was down|is down|were down)\b|"
    r"\b(card machine|debit machine|payment machine|pos machine|machine)\b[^.!?]{0,20}\b(down|broken|not working|wasn'?t working)\b|"
    r"\bslow (checkout|payment|payments)\b",
    re.I,
)

SERVICE_ORDER = ["POS & payments", "Finance & bookkeeping", "Website", "Social media", "Marketing & ads", "CRM & follow-up"]
DELIVERY = {"SkipTheDishes", "DoorDash", "Uber Eats"}

ANGLE = {
    "food": "Angle: faster table/counter payments, tipping and online-order fees.",
    "salon": "Angle: payments plus bookings and tip handling in one system.",
    "retail": "Angle: inventory sync between the shop and online, simpler reporting.",
    "default": "Angle: lower card fees and a simpler checkout.",
}


def _kw(text, words):
    t = (text or "").lower()
    return [w for w in words if re.search(r"(?<![a-z])" + re.escape(w) + r"s?(?![a-z])", t)]


def category_fit(category, name=""):
    blob = f"{category or ''}"
    if _kw(blob, EXCLUDE):
        return "excluded", 0
    if _kw(blob, HIGH):
        return "high", 25
    if _kw(blob, MEDIUM):
        return "medium", 12
    # fall back to the business name when Google gives a vague category
    if _kw(name, ["cafe", "café", "bakery", "restaurant", "kitchen", "salon", "barber", "boutique", "grill", "pizza"]):
        return "medium", 12
    return "unknown", 4


def _angle_key(category):
    t = (category or "").lower()
    if _kw(t, ["restaurant", "cafe", "café", "coffee", "bakery", "pizza", "bar", "pub", "tea", "juice", "kitchen", "deli", "catering", "food truck", "grill", "bistro", "diner", "dessert", "ice cream", "sushi"]):
        return "food"
    if _kw(t, ["salon", "barber", "hair", "beauty", "nail", "spa", "massage"]):
        return "salon"
    if _kw(t, ["boutique", "clothing", "gift", "store", "shop", "convenience", "grocery", "market", "florist"]):
        return "retail"
    return "default"


def _days_since(iso):
    try:
        d = datetime.fromisoformat(str(iso)[:10]).date()
        return (date.today() - d).days
    except Exception:  # noqa: BLE001
        return None


def review_pain(reviews):
    """Return the first short quote that mentions a payment problem, or None."""
    for text in reviews or []:
        m = POS_PAIN.search(text or "")
        if m:
            start = max(0, m.start() - 50)
            return re.sub(r"\s+", " ", text[start:m.end() + 70]).strip()
    return None


def score_lead(r, quality=None):
    """Need-based score. A lead only reaches tier A or B when there is evidence that the business needs one of
    the services you sell (POS/payments, finance, website, social media, marketing, CRM)."""
    q = {"max_review_count": 1500, "min_need_signals": 2}
    q.update(quality or {})
    pts = 0
    strong, normal = [], []          # (text, [services])
    fit, p = category_fit(r.get("category"), r.get("name"))
    pts += p

    if r.get("phone_status") == "ok":
        pts += 15
    elif r.get("phone"):
        pts += 6
    es = r.get("email_status") or ""
    if es == "ok_own_domain":
        pts += 12
    elif es in ("ok_other_domain", "ok_freemail"):
        pts += 8

    rc, rating = r.get("review_count") or 0, r.get("rating") or 0
    pts += 2 if 15 <= rc < 50 else 5 if 50 <= rc < 500 else 3 if rc >= 500 else 0
    if rc > q["max_review_count"]:
        pts -= 15

    # ---- strong evidence ----
    if r.get("signal_type") == "new_business_licence":
        age = _days_since(r.get("signal_date"))
        if age is not None and age <= 60:
            strong.append((f"New business licence issued {age} days ago, so they are setting up now",
                           ["POS & payments", "Finance & bookkeeping", "Website"]))
        elif age is not None and age <= 150:
            normal.append((f"Business licence issued {age} days ago",
                           ["POS & payments", "Finance & bookkeeping", "Website"]))
    quote = review_pain(r.get("reviews"))
    if quote:
        strong.append((f'A customer review mentions a payment problem: "{quote[:140]}"', ["POS & payments"]))
    if r.get("cash_only"):
        strong.append(("Website says cash only", ["POS & payments", "Finance & bookkeeping"]))
    if not r.get("website") and rc >= 15:
        strong.append((f"No website, yet an active business ({rc} Google reviews)", ["Website", "Social media"]))

    # ---- normal evidence: gaps seen on the business's own website ----
    d = r.get("digital") or {}
    if r.get("site_checked") and d:
        gaps = []
        if d.get("no_viewport"):
            gaps.append("not mobile-friendly")
        if d.get("no_https"):
            gaps.append("no secure https connection")
        cy = d.get("copyright_year") or 0
        if cy and cy <= date.today().year - 3:
            gaps.append(f"footer still says {cy}")
        if d.get("builder"):
            gaps.append(f"basic {d['builder']} template")
        if gaps:
            normal.append(("Website looks dated: " + ", ".join(gaps), ["Website"]))
        if not d.get("has_facebook") and not d.get("has_instagram"):
            normal.append(("No Facebook or Instagram link found on the website", ["Social media"]))
        if d.get("has_pixel"):
            normal.append(("Uses the Meta Pixel, so they likely run Facebook/Instagram ads", ["Social media", "Marketing & ads"]))
        elif not d.get("has_analytics"):
            normal.append(("No analytics or ad tracking found on the website", ["Marketing & ads"]))
        if not (d.get("has_email_tool") or d.get("has_form") or d.get("has_booking") or r.get("online_ordering")):
            normal.append(("No email sign-up, contact form or booking tool found", ["CRM & follow-up"]))
    pos = r.get("pos_detected") or []
    if set(pos) & DELIVERY:
        normal.append(("Relies on delivery apps (" + ", ".join(sorted(set(pos) & DELIVERY)) + "), which charge commission",
                       ["POS & payments", "Finance & bookkeeping"]))
    if (r.get("site_checked") and fit == "high" and _angle_key(r.get("category")) == "food"
            and not r.get("online_ordering") and not r.get("order_online")):
        normal.append(("No online ordering found", ["POS & payments"]))
    if 0 < rc <= 10 and rating >= 4.3 and fit == "high":
        normal.append((f"Only {rc} Google reviews with a high rating, possibly newly opened", ["POS & payments", "Website"]))

    pts += min(36, 18 * len(strong)) + min(32, 8 * len(normal))
    if pos:
        pts += 4

    tier = "A" if strong else ("B" if len(normal) >= q["min_need_signals"] else "C")
    score = max(0, min(100, pts))

    services = []
    for _, sv in strong + normal:
        for x in sv:
            if x not in services:
                services.append(x)
    services.sort(key=SERVICE_ORDER.index)
    texts = [t for t, _ in strong + normal]

    loc = ", ".join(x for x in [r.get("city"), r.get("province")] if x)
    cat = r.get("category") or "Local business"
    bits = [f"{cat} in {loc}." if loc else f"{cat}."]
    if rc >= 100 and rating:
        bits.append(f"Rated {rating} from {rc} Google reviews, a busy and well-known local business.")
    elif rc >= 30 and rating:
        bits.append(f"Rated {rating} from {rc} Google reviews.")
    elif rc:
        bits.append(f"Only {rc} Google reviews so far, so a smaller or newer business.")
    if services:
        bits.append("Likely needs: " + ", ".join(services) + ".")
    if texts:
        bits.append("Why: " + "; ".join(texts[:3]) + ".")
    if pos:
        bits.append("Tools seen on their site: " + ", ".join(pos) + ".")
    if es not in ("ok_own_domain", "ok_other_domain", "ok_freemail"):
        bits.append("No public email found: call and ask for the owner's email.")
    return {
        "score": score, "tier": tier, "signals": texts,
        "evidence": " | ".join(texts) if texts else "No clear service need found",
        "description": " ".join(bits), "pos_detected": ", ".join(pos), "category_fit": fit,
        "service_needs": ", ".join(services), "need_count": len(strong) * 2 + len(normal),
    }
