"""Offline tests. Run: py -m unittest discover -s tests"""
import csv
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from leadlib import validate  # noqa: E402
from leadlib.enrich import digital_signals  # noqa: E402
from leadlib.score import category_fit, review_pain, score_lead  # noqa: E402

HEADER = ["input_id", "link", "title", "category", "address", "website", "phone", "review_count",
          "review_rating", "status", "place_id", "complete_address", "user_reviews", "emails", "order_online",
          "credit_cards_accepted"]
PAIN = json.dumps([{"Name": "A", "Description": "Great food but cash only and the card machine was down."}])


def row(**kw):
    base = {h: "" for h in HEADER}
    base.update(kw)
    return base


def base_lead(**kw):
    d = {"category": "Cafe", "name": "X", "phone": "+19055550142", "phone_status": "ok", "email_status": "ok_own_domain",
         "review_count": 120, "rating": 4.5, "city": "Toronto", "province": "ON", "website": "https://x.ca",
         "pos_detected": [], "online_ordering": True, "reviews": [], "signal_type": "", "signal_date": "",
         "site_checked": False, "digital": {}}
    d.update(kw)
    return d


class UnitTests(unittest.TestCase):
    def test_phone(self):
        self.assertEqual(validate.normalize_phone("(905) 555-0142"), "+19055550142")
        self.assertIsNone(validate.normalize_phone("555-0142"))
        self.assertEqual(validate.phone_status("+12125550100"), "non_canadian_area_code")

    def test_canada(self):
        self.assertTrue(validate.is_canadian("ON", "L5B 1A1", "1 Main St, Mississauga, ON L5B 1A1", ""))
        self.assertFalse(validate.is_canadian("", "", "1 Main St, Brooklyn, NY 11201, United States", ""))

    def test_email(self):
        self.assertIsNone(validate.clean_email("logo@2x.png"))
        self.assertIsNone(validate.clean_email("hi@mystore.com"))
        self.assertEqual(validate.email_status("info@cafe.ca", "https://www.cafe.ca", False), ("ok_own_domain", True))

    def test_chain_and_category(self):
        self.assertEqual(validate.chain_match("Tim Hortons #123", ["tim hortons"]), "tim hortons")
        self.assertEqual(category_fit("Coffee shop")[0], "high")
        self.assertEqual(category_fit("Law firm")[0], "excluded")
        self.assertEqual(category_fit("Barber supply store")[0], "excluded")

    def test_review_false_positives(self):
        for text in ["Restyle is such a huge staple in downtown Brampton for small businesses.",
                     "Very nice convenience store near Brampton Terminal at Downtown.",
                     "Avoid this location for slow service.", "No credit card surcharge. Take out only."]:
            self.assertIsNone(review_pain([text]), text)
        self.assertIsNotNone(review_pain(["he doesn't accept debit never smiling"]))
        self.assertIsNotNone(review_pain(["The card machine was down so we left"]))

    def test_digital_signals(self):
        old = "<html><head><title>Joe</title></head><body>Joe's Diner &copy; 2017 <a href='/menu'>Menu</a></body></html>"
        d = digital_signals("http://joes.ca/", old)
        self.assertTrue(d["no_viewport"] and d["no_https"])
        self.assertEqual(d["copyright_year"], 2017)
        self.assertFalse(d["has_facebook"] or d["has_pixel"] or d["has_analytics"])
        modern = ("<html><head><meta name=\"viewport\" content=\"width=device-width\">"
                  "<script src='https://connect.facebook.net/en_US/fbevents.js'></script></head>"
                  "<body><a href='https://www.instagram.com/joes'>ig</a>&copy; 2026 "
                  "<form><input type=\"email\"></form></body></html>")
        m = digital_signals("https://joes.ca/", modern)
        self.assertFalse(m["no_viewport"] or m["no_https"])
        self.assertTrue(m["has_pixel"] and m["has_instagram"] and m["has_form"])

    def test_tier_fit_only_is_c(self):
        s = score_lead(base_lead())
        self.assertEqual(s["tier"], "C")
        self.assertEqual(s["evidence"], "No clear service need found")

    def test_tier_a_from_payment_complaint(self):
        s = score_lead(base_lead(reviews=["card machine was down again"]))
        self.assertEqual(s["tier"], "A")
        self.assertIn("POS & payments", s["service_needs"])
        self.assertGreaterEqual(s["score"], 70)

    def test_tier_b_from_two_digital_gaps(self):
        d = digital_signals("http://x.ca/", "<html><body>&copy; 2016 hello</body></html>")
        s = score_lead(base_lead(site_checked=True, digital=d))
        self.assertEqual(s["tier"], "B")
        for need in ("Website", "Social media", "Marketing & ads"):
            self.assertIn(need, s["service_needs"])

    def test_no_website_active_business_is_strong(self):
        s = score_lead(base_lead(website="", review_count=60))
        self.assertEqual(s["tier"], "A")
        self.assertIn("Website", s["service_needs"])

    def test_new_licence(self):
        recent = (date.today() - timedelta(days=12)).isoformat()
        s = score_lead(base_lead(signal_type="new_business_licence", signal_date=recent,
                                 signal_text="Business licence issued"))
        self.assertEqual(s["tier"], "A")
        self.assertIn("Finance & bookkeeping", s["service_needs"])


class Runs(unittest.TestCase):
    def _run(self, tmp, rows, meta=None, cap=None, times=1):
        csv_path = tmp / "r.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=HEADER); w.writeheader(); w.writerows(rows)
        if meta:
            (tmp / "meta.json").write_text(json.dumps(meta))
        cfg = (ROOT / "config.yaml").read_text().replace("mx_check: true", "mx_check: false")
        if cap:
            cfg = cfg.replace("target_leads_per_day: 100", f"target_leads_per_day: {cap}")
        cfg = cfg.replace("lead_quality:\n  enabled: true", "lead_quality:\n  enabled: false")
        cfg = cfg.replace("out: out", f"out: {tmp / 'out'}").replace("state: state", f"state: {tmp / 'state'}")
        (tmp / "cfg.yaml").write_text(cfg)
        cmd = [sys.executable, str(ROOT / "run_daily.py"), "--config", str(tmp / "cfg.yaml"), "--csv", str(csv_path),
               "--no-enrich", "--no-push"]
        outs = []
        for _ in range(times):
            res = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            out = sorted((tmp / "out").glob("*"))[0]
            rd = lambda n: list(csv.DictReader(open(out / n, encoding="utf-8-sig")))  # noqa: E731
            outs.append((rd("qualified_leads.csv"), rd("review_queue.csv"), rd("rejected_log.csv"), res.stdout))
        return outs

    def test_full_run(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            recent = (date.today() - timedelta(days=10)).isoformat()
            rows = [
                row(input_id="cat1", title="Maple Leaf Cafe", category="Cafe", website="https://mapleleafcafe.ca",
                    address="12 King St, Mississauga, ON L5B 1A1, Canada", phone="(905) 555-0142", review_count="220",
                    review_rating="4.6", place_id="P1", emails="info@mapleleafcafe.ca", user_reviews=PAIN),
                row(input_id="cat1", title="Tim Hortons", category="Coffee shop", website="https://timhortons.ca",
                    address="5 Queen St, Toronto, ON M5V 2T6, Canada", phone="416-555-0100", place_id="P2"),
                row(input_id="cat1", title="Brooklyn Diner", category="Restaurant", website="https://bd.com",
                    address="1 Main St, Brooklyn, NY 11201, United States", phone="212-555-0100", place_id="P3"),
                row(input_id="cat1", title="Smith & Co Law", category="Law firm", website="https://smithlaw.ca",
                    address="9 Bay St, Toronto, ON M5J 2S1, Canada", phone="416-555-0111", place_id="P4"),
                row(input_id="cat1", title="Sunrise Bakery", category="Bakery", website="https://sunrisebakery.ca",
                    address="3 8 Ave SW, Calgary, AB T2P 1B5, Canada", phone="403-555-0111", place_id="P5",
                    review_count="90", review_rating="4.4"),
                row(input_id="cat1", title="Old Cafe", category="Cafe", website="https://old.ca", status="Permanently closed",
                    address="1 A St, Halifax, NS B3H 1A1, Canada", phone="902-555-0100", place_id="P6"),
                row(input_id="cat1", title="No Phone Barber", category="Barber shop", website="https://npb.ca",
                    address="1 B St, Ottawa, ON K1A 0A1, Canada", phone="", place_id="P7"),
                row(input_id="lic1", title="Licence Test Kitchen", category="Restaurant", website="https://ltk.ca",
                    address="100 Main St, Vancouver, BC V6A 1A1, Canada", phone="604-555-0123", place_id="P8",
                    emails="owner@gmail.com", review_count="12", review_rating="4.7"),
                row(input_id="lic2", title="Totally Different Shop", category="Store", website="https://tds.ca",
                    address="2 Main St, Vancouver, BC V6A 1A1, Canada", phone="604-555-0199", place_id="P9"),
                row(input_id="cat1", title="Corner Kiosk", category="Convenience store", website="",
                    address="7 Elm St, Winnipeg, MB R3C 1A1, Canada", phone="204-555-0188", place_id="P10",
                    review_count="60", review_rating="4.2"),
                row(input_id="cat1", title="Giant Venue", category="Restaurant", website="https://giant.ca",
                    address="1 Big St, Toronto, ON M5V 2T6, Canada", phone="416-555-0777", place_id="P11",
                    review_count="15252", review_rating="4.6", user_reviews=PAIN),
                row(input_id="cat1", title="Maple Leaf Cafe", category="Cafe", website="https://mapleleafcafe.ca",
                    address="12 King St, Mississauga, ON L5B 1A1, Canada", phone="(905) 555-0142", place_id="P1"),
            ]
            meta = {"cat1": {"source": "google_maps_search"},
                    "lic1": {"source": "licence_vancouver", "signal_type": "new_business_licence",
                             "signal_date": recent, "signal_text": f"Business licence issued {recent} (Restaurant)",
                             "match_names": ["Licence Test Kitchen"]},
                    "lic2": {"source": "licence_vancouver", "signal_type": "new_business_licence",
                             "signal_date": recent, "signal_text": "x", "match_names": ["Some Other Name Entirely"]}}
            (qual, rev, rej, out1), (qual2, _, _, _) = self._run(tmp, rows, meta, times=2)
            names = {r["business_name"]: r for r in qual}
            self.assertEqual(set(names), {"Maple Leaf Cafe", "Licence Test Kitchen", "Corner Kiosk"}, set(names))
            self.assertEqual(names["Maple Leaf Cafe"]["intent_tier"], "A")
            self.assertIn("POS & payments", names["Maple Leaf Cafe"]["service_needs"])
            self.assertIn("licence", names["Licence Test Kitchen"]["evidence"].lower())
            self.assertIn("Website", names["Corner Kiosk"]["service_needs"])
            self.assertEqual(names["Corner Kiosk"]["email"], "")
            self.assertTrue(any(r["business_name"] == "Sunrise Bakery" and "no clear service need" in r["review_reason"]
                                for r in rev))
            reasons = " | ".join(r["reject_reason"] for r in rej)
            for expect in ("chain", "not in Canada", "closed", "no valid phone", "not a POS fit", "too large"):
                self.assertIn(expect, reasons)
            self.assertNotIn("Totally Different Shop", [r["business_name"] for r in qual + rev + rej])
            self.assertEqual(len(qual2), 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_overflow_is_kept_and_exported_next_run(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            rows = [row(input_id="c", title=f"Cafe Number {i}", category="Cafe", website=f"https://cafe{i}.ca",
                        address=f"{i} King St, Toronto, ON M5V 2T6, Canada", phone=f"416-555-01{i:02d}",
                        review_count=str(100 + i), review_rating="4.5", place_id=f"Q{i}", user_reviews=PAIN)
                    for i in range(1, 5)]
            names = []
            tmp_runs = self._run(tmp, rows, cap=2, times=3)
            for q, _, _, _ in tmp_runs:
                names += [r["business_name"] for r in q]
            self.assertEqual(len(names), 4)
            self.assertEqual(len(set(names)), 4, names)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
