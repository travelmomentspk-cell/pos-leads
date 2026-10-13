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


def score_lead(r):
    """r is a lead dict. Returns dict with score, tier, signals, evidence, description, pos_detected."""
    pts, signals, evidence = 0, [], []
    fit, p = category_fit(r.get("category"), r.get("name"))
    pts += p
    if fit == "high":
        signals.append("Category suits a POS (counter/checkout business)")
    elif fit == "medium":
        signals.append("Category can use a POS")

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
    if rc >= 150:
        pts += 12
    elif rc >= 50:
        pts += 8
    elif rc >= 15:
        pts += 4
    if rc >= 15 and rating >= 4.0:
        pts += 3

    tier_hot = False
    # --- new business signal from a licence or similar source ---
    if r.get("signal_type") == "new_business_licence":
        age = _days_since(r.get("signal_date"))
        if age is not None and age <= 120:
            pts += 30 if age <= 45 else 22
            tier_hot = True
            signals.append(f"New business licence issued {age} days ago")
            evidence.append(r.get("signal_text") or "New business licence")
    # --- customer reviews mention payment problems ---
    quote = review_pain(r.get("reviews"))
    if quote:
        pts += 20
        tier_hot = True
        signals.append("Customer review mentions a payment or card-machine problem")
        evidence.append(f'Review: "{quote[:140]}"')
    if r.get("cash_only"):
        pts += 10
        tier_hot = True
        signals.append("Website says cash only")
        evidence.append("Website mentions cash only")

    pos = r.get("pos_detected") or []
    warm = False
    if pos:
        pts += 12
        warm = True
        signals.append("Website uses " + ", ".join(pos))
        evidence.append("Website references: " + ", ".join(pos))
    if fit in ("high",) and _angle_key(r.get("category")) == "food" and r.get("website") and not r.get("online_ordering") and not r.get("order_online"):
        pts += 6
        warm = True
        signals.append("No online ordering found")
    if not r.get("credit_cards_accepted") and fit in ("high", "medium"):
        pass  # Google rarely fills this field, so absence is not a signal

    tier = "A" if tier_hot else ("B" if warm else "C")
    score = max(0, min(100, pts))

    loc = ", ".join(x for x in [r.get("city"), r.get("province")] if x)
    cat = r.get("category") or "Local business"
    bits = [f"{cat} in {loc}." if loc else f"{cat}."]
    if rc >= 100 and rating:
        bits.append(f"Rated {rating} from {rc} Google reviews, a busy and well-known local business.")
    elif rc >= 30 and rating:
        bits.append(f"Rated {rating} from {rc} Google reviews.")
    elif rc:
        bits.append(f"Only {rc} Google reviews so far, so a smaller or newer business.")
    if signals:
        bits.append("POS signals: " + "; ".join(signals[1:] if len(signals) > 1 else signals) + ".")
    bits.append(ANGLE[_angle_key(r.get("category"))])
    if es not in ("ok_own_domain", "ok_other_domain", "ok_freemail"):
        bits.append("No public email found: call and ask for the owner's email.")
    return {
        "score": score,
        "tier": tier,
        "signals": signals,
        "evidence": " | ".join(evidence) if evidence else "Category and contact details only",
        "description": " ".join(bits),
        "pos_detected": ", ".join(pos),
        "category_fit": fit,
    }
