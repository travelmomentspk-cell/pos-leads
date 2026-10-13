"""Gate checks: Canada, phone, email, chain, closed status. No network except the optional MX lookup."""
import re
from functools import lru_cache

PROVINCES = {"AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"}
PROVINCE_NAMES = {
    "alberta": "AB", "british columbia": "BC", "manitoba": "MB", "new brunswick": "NB",
    "newfoundland and labrador": "NL", "newfoundland": "NL", "nova scotia": "NS",
    "northwest territories": "NT", "nunavut": "NU", "ontario": "ON",
    "prince edward island": "PE", "quebec": "QC", "québec": "QC", "saskatchewan": "SK", "yukon": "YT",
}
POSTAL_FIRST = {"A": "NL", "B": "NS", "C": "PE", "E": "NB", "G": "QC", "H": "QC", "J": "QC",
                "K": "ON", "L": "ON", "M": "ON", "N": "ON", "P": "ON", "R": "MB", "S": "SK",
                "T": "AB", "V": "BC", "X": "NT", "Y": "YT"}
POSTAL_RE = re.compile(r"\b([ABCEGHJ-NPRSTVXY]\d[ABCEGHJ-NPRSTV-Z])[ -]?(\d[ABCEGHJ-NPRSTV-Z]\d)\b", re.I)

# Canadian NANP area codes. Used as a soft check only (phone_status), never as the sole reject reason.
CA_AREA_CODES = {
    "204", "226", "236", "249", "257", "263", "289", "306", "343", "354", "365", "367", "368", "382",
    "403", "416", "418", "428", "431", "437", "438", "450", "468", "474", "506", "514", "519", "548",
    "579", "581", "584", "587", "604", "613", "639", "647", "672", "683", "705", "709", "742", "753",
    "778", "780", "782", "807", "819", "825", "867", "873", "879", "902", "905",
}

JUNK_LOCAL = {"noreply", "no-reply", "donotreply", "do-not-reply", "sentry", "wixpress", "example",
              "test", "user", "yourname", "youremail", "email", "name", "firstname", "username",
              "mailer-daemon", "postmaster", "webmaster", "abuse"}
JUNK_DOMAINS = {"sentry.io", "wixpress.com", "example.com", "domain.com", "email.com", "yoursite.com",
                "sentry-next.wixpress.com", "godaddy.com", "squarespace.com", "wix.com", "shopify.com",
                "mystore.com", "mysite.com", "mydomain.com", "yourdomain.com", "yourcompany.com", "company.com",
                "companyname.com", "sample.com", "test.com", "website.com", "site.com", "yourwebsite.com",
                "yourbusiness.com", "business.com", "shop.com", "store.com", "mail.com", "123.com", "abc.com",
                "domain.ca", "example.ca", "email.ca"}
BAD_TLDS = {"png", "jpg", "jpeg", "gif", "webp", "svg", "css", "js", "woff", "woff2", "ico", "pdf"}
FREEMAIL = {"gmail.com", "googlemail.com", "hotmail.com", "outlook.com", "yahoo.com", "yahoo.ca",
            "icloud.com", "live.com", "live.ca", "aol.com", "protonmail.com", "proton.me", "msn.com",
            "rogers.com", "bell.net", "sympatico.ca", "telus.net", "shaw.ca", "videotron.ca", "hotmail.ca"}
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def normalize_phone(raw):
    """Return +1XXXXXXXXXX or None."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10 or digits[0] in "01" or digits[3] in "01":
        return None
    return "+1" + digits


def phone_status(e164):
    if not e164:
        return "invalid"
    return "ok" if e164[2:5] in CA_AREA_CODES else "non_canadian_area_code"


def province_from_text(text):
    if not text:
        return None
    t = text.lower()
    for name, code in PROVINCE_NAMES.items():
        if re.search(r"\b" + re.escape(name) + r"\b", t):
            return code
    m = re.findall(r"\b(AB|BC|MB|NB|NL|NS|NT|NU|ON|PE|QC|SK|YT)\b", text)
    return m[-1] if m else None


def parse_postal(text):
    m = POSTAL_RE.search(text or "")
    return (m.group(1) + " " + m.group(2)).upper() if m else None


def is_canadian(province, postal, address, country=None):
    """True when the location clearly points to Canada and not elsewhere."""
    blob = f"{address or ''} {country or ''}".lower()
    if re.search(r"\b(usa|united states|u\.s\.a)\b", blob):
        return False
    if country and str(country).strip().lower() in {"ca", "can", "canada"}:
        return True
    if province in PROVINCES and (postal or "canada" in blob):
        return True
    if postal and POSTAL_FIRST.get(postal[0]):
        return True
    return "canada" in blob


def chain_match(name, chains):
    n = (name or "").lower().replace("’", "'")
    for c in chains:
        if c and re.search(r"(?<![a-z0-9])" + re.escape(c) + r"(?![a-z0-9])", n):
            return c
    return None


def domain_of(url):
    if not url:
        return ""
    d = re.sub(r"^https?://", "", url.strip().lower()).split("/")[0].split("?")[0]
    return d[4:] if d.startswith("www.") else d


def clean_email(e):
    e = (e or "").strip().strip(".,;:<>()[]\"'").lower()
    if not EMAIL_RE.match(e):
        return None
    local, dom = e.rsplit("@", 1)
    if local in JUNK_LOCAL or dom in JUNK_DOMAINS or dom.rsplit(".", 1)[-1] in BAD_TLDS:
        return None
    if local.startswith("your") or local in {"john.doe", "johndoe", "jane.doe", "janedoe", "someone", "info@info"}:
        return None
    if re.search(r"\d+x\d+|@\d+x", e):  # image names like logo@2x.png
        return None
    return e


@lru_cache(maxsize=4096)
def mx_ok(domain):
    """True/False, or None when dnspython is missing or the network is blocked."""
    try:
        import dns.resolver
    except ImportError:
        return None
    try:
        dns.resolver.resolve(domain, "MX", lifetime=4)
        return True
    except Exception as exc:  # noqa: BLE001
        name = type(exc).__name__
        if name in {"NXDOMAIN"}:
            return False
        if name in {"NoAnswer"}:
            try:
                dns.resolver.resolve(domain, "A", lifetime=3)
                return True
            except Exception:  # noqa: BLE001
                return False
        return None


def email_status(email, website, mx_check=True):
    """Return (status, usable). Status strings go straight into the sheet."""
    e = clean_email(email)
    if not e:
        return "invalid", False
    dom = e.rsplit("@", 1)[1]
    if mx_check:
        ok = mx_ok(dom)
        if ok is False:
            return "mx_failed", False
    if dom in FREEMAIL:
        return "ok_freemail", True
    wd = domain_of(website)
    if wd and (dom == wd or dom.endswith("." + wd) or wd.endswith("." + dom)):
        return "ok_own_domain", True
    return "ok_other_domain", True
