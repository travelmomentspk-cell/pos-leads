"""Offline tests: unit checks plus a full run on made-up scraper output. Run: python -m unittest -v"""
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
from leadlib.score import review_pain, score_lead, category_fit  # noqa: E402

HEADER = ["input_id", "link", "title", "category", "address", "website", "phone", "review_count",
          "review_rating", "status", "place_id", "complete_address", "user_reviews", "emails", "order_online",
          "credit_cards_accepted"]


def row(**kw):
    base = {h: "" for h in HEADER}
    base.update(kw)
    return base


class UnitTests(unittest.TestCase):
    def test_phone(self):
        self.assertEqual(validate.normalize_phone("(905) 555-0142"), "+19055550142")
        self.assertEqual(validate.normalize_phone("+1 416-555-0100"), "+14165550100")
        self.assertIsNone(validate.normalize_phone("555-0142"))
        self.assertEqual(validate.phone_status("+19055550142"), "ok")
        self.assertEqual(validate.phone_status("+12125550100"), "non_canadian_area_code")

    def test_canada(self):
        self.assertTrue(validate.is_canadian("ON", "L5B 1A1", "1 Main St, Mississauga, ON L5B 1A1", ""))
        self.assertFalse(validate.is_canadian("", "", "1 Main St, Brooklyn, NY 11201, United States", ""))
        self.assertEqual(validate.parse_postal("x m5v 2t6 y"), "M5V 2T6")

    def test_email(self):
        self.assertIsNone(validate.clean_email("logo@2x.png"))
        self.assertIsNone(validate.clean_email("noreply@shop.com"))
        self.assertEqual(validate.clean_email(" Info@Cafe.CA "), "info@cafe.ca")
        self.assertEqual(validate.email_status("info@cafe.ca", "https://www.cafe.ca", False), ("ok_own_domain", True))
        self.assertEqual(validate.email_status("owner@gmail.com", "https://cafe.ca", False), ("ok_freemail", True))

    def test_chain(self):
        self.assertEqual(validate.chain_match("Tim Hortons #123", ["tim hortons"]), "tim hortons")
        self.assertIsNone(validate.chain_match("Subwaytiles Studio", ["subway"]))

    def test_category(self):
        self.assertEqual(category_fit("Coffee shop")[0], "high")
        self.assertEqual(category_fit("Barber shop")[0], "high")
        self.assertEqual(category_fit("Law firm")[0], "excluded")
        self.assertEqual(category_fit("Bar")[0], "high")

    def test_review_pain(self):
        self.assertIn("cash only", review_pain(["Great food but sadly cash only and the machine was down."]).lower())
        self.assertIsNone(review_pain(["Lovely staff and great coffee."]))

    def test_review_false_positives(self):
        # real examples from the first live run that must NOT count as payment problems
        for text in ["Restyle is such a huge staple in downtown Brampton for small businesses.",
                     "Very nice convenience store near Brampton Terminal at Downtown. Staff were friendly.",
                     "Avoid this location for slow service. the drinks are very good.",
                     "Appetizers came out late and slow service overall.",
                     "Prices are okay. No credit card surcharge. Take out only."]:
            self.assertIsNone(review_pain([text]), text)
        self.assertIsNotNone(review_pain(["he is rude and doesn't accept debit never smiling"]))
        self.assertIsNotNone(review_pain(["The card machine was down so we had to leave"]))

    def test_placeholder_email_and_supplier(self):
        self.assertIsNone(validate.clean_email("hi@mystore.com"))
        self.assertIsNone(validate.clean_email("yourname@company.com"))
        self.assertEqual(category_fit("Barber supply store")[0], "excluded")
        self.assertEqual(category_fit("Wholesaler")[0], "excluded")

    def test_score_signals(self):
        lead = {"category": "Cafe", "name": "X", "phone": "+19055550142", "phone_status": "ok",
                "email_status": "ok_own_domain", "review_count": 200, "rating": 4.5, "city": "Toronto",
                "province": "ON", "website": "https://x.ca", "pos_detected": ["Square"], "online_ordering": False,
                "reviews": ["card machine was down again"], "signal_type": "", "signal_date": ""}
        s = score_lead(lead)
        self.assertEqual(s["tier"], "A")
        self.assertGreaterEqual(s["score"], 80)
        lead.update(reviews=[], pos_detected=[])
        self.assertIn(score_lead(lead)["tier"], ("B", "C"))


class Backlog(unittest.TestCase):
    def test_overflow_is_kept_and_exported_next_run(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            rows = [row(input_id="c", title=f"Cafe Number {i}", category="Cafe", website=f"https://cafe{i}.ca",
                        address=f"{i} King St, Toronto, ON M5V 2T6, Canada", phone=f"416-555-01{i:02d}",
                        review_count=str(100 + i), review_rating="4.5", place_id=f"Q{i}",
                        emails=f"info@cafe{i}.ca") for i in range(1, 5)]
            csv_path = tmp / "r.csv"
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=HEADER); w.writeheader(); w.writerows(rows)
            cfg = (ROOT / "config.yaml").read_text().replace("mx_check: true", "mx_check: false")
            cfg = cfg.replace("target_leads_per_day: 100", "target_leads_per_day: 2")
            cfg = cfg.replace("out: out", f"out: {tmp / 'out'}").replace("state: state", f"state: {tmp / 'state'}")
            (tmp / "cfg.yaml").write_text(cfg)
            cmd = [sys.executable, str(ROOT / "run_daily.py"), "--config", str(tmp / "cfg.yaml"), "--csv", str(csv_path),
                   "--no-enrich", "--no-push"]
            names = []
            for _ in range(3):
                res = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
                self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
                out = sorted((tmp / "out").glob("*"))[0]
                names += [r["business_name"] for r in csv.DictReader(open(out / "qualified_leads.csv", encoding="utf-8-sig"))]
            self.assertEqual(len(names), 4)
            self.assertEqual(len(set(names)), 4, names)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class EndToEnd(unittest.TestCase):
    def test_full_run(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            recent = (date.today() - timedelta(days=10)).isoformat()
            rows = [
                row(input_id="cat1", title="Maple Leaf Cafe", category="Cafe", website="https://mapleleafcafe.ca",
                    address="12 King St, Mississauga, ON L5B 1A1, Canada", phone="(905) 555-0142",
                    review_count="220", review_rating="4.6", status="", place_id="P1",
                    emails="info@mapleleafcafe.ca",
                    user_reviews=json.dumps([{"Name": "A", "Description": "Great latte but cash only and the card machine was down."}])),
                row(input_id="cat1", title="Tim Hortons", category="Coffee shop", website="https://timhortons.ca",
                    address="5 Queen St, Toronto, ON M5V 2T6, Canada", phone="416-555-0100", place_id="P2"),
                row(input_id="cat1", title="Brooklyn Diner", category="Restaurant", website="https://bd.com",
                    address="1 Main St, Brooklyn, NY 11201, United States", phone="212-555-0100", place_id="P3"),
                row(input_id="cat1", title="Smith & Co Law", category="Law firm", website="https://smithlaw.ca",
                    address="9 Bay St, Toronto, ON M5J 2S1, Canada", phone="416-555-0111", place_id="P4",
                    emails="hello@smithlaw.ca", review_count="30", review_rating="4.8"),
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
                    address="2 Main St, Vancouver, BC V6A 1A1, Canada", phone="604-555-0199", place_id="P9",
                    emails="a@tds.ca"),
                row(input_id="cat1", title="Corner Kiosk", category="Convenience store", website="",
                    address="7 Elm St, Winnipeg, MB R3C 1A1, Canada", phone="204-555-0188", place_id="P10",
                    review_count="60", review_rating="4.2"),
                row(input_id="cat1", title="Maple Leaf Cafe", category="Cafe", website="https://mapleleafcafe.ca",
                    address="12 King St, Mississauga, ON L5B 1A1, Canada", phone="(905) 555-0142", place_id="P1"),
            ]
            csv_path = tmp / "results.csv"
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=HEADER)
                w.writeheader()
                w.writerows(rows)
            (tmp / "meta.json").write_text(json.dumps({
                "cat1": {"source": "google_maps_search", "query": "cafe in Mississauga ON"},
                "lic1": {"source": "licence_vancouver", "source_url": "https://opendata.vancouver.ca/explore/dataset/business-licences/",
                         "signal_type": "new_business_licence", "signal_date": recent,
                         "signal_text": f"Business licence issued {recent} (Restaurant), City of Vancouver open data",
                         "match_names": ["Licence Test Kitchen"]},
                "lic2": {"source": "licence_vancouver", "signal_type": "new_business_licence", "signal_date": recent,
                         "signal_text": "x", "match_names": ["Some Other Name Entirely"]},
            }))
            cfg = (ROOT / "config.yaml").read_text().replace("mx_check: true", "mx_check: false")
            cfg = cfg.replace("out: out", f"out: {tmp / 'out'}").replace("state: state", f"state: {tmp / 'state'}")
            (tmp / "cfg.yaml").write_text(cfg)
            cmd = [sys.executable, str(ROOT / "run_daily.py"), "--config", str(tmp / "cfg.yaml"), "--csv", str(csv_path),
                   "--no-enrich", "--no-push"]
            res = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            out = list((tmp / "out").glob("*"))[0]
            qual = list(csv.DictReader(open(out / "qualified_leads.csv", encoding="utf-8-sig")))
            rev = list(csv.DictReader(open(out / "review_queue.csv", encoding="utf-8-sig")))
            rej = list(csv.DictReader(open(out / "rejected_log.csv", encoding="utf-8-sig")))
            names = {r["business_name"]: r for r in qual}
            self.assertIn("Maple Leaf Cafe", names)
            self.assertEqual(names["Maple Leaf Cafe"]["intent_tier"], "A")
            self.assertEqual(names["Maple Leaf Cafe"]["email_status"], "ok_own_domain")
            self.assertIn("Licence Test Kitchen", names)
            self.assertIn("licence", names["Licence Test Kitchen"]["evidence"].lower())
            self.assertEqual(len([r for r in qual if r["business_name"] == "Maple Leaf Cafe"]), 1)
            self.assertIn("Sunrise Bakery", names)
            self.assertEqual(names["Sunrise Bakery"]["email"], "")
            self.assertIn("call", names["Sunrise Bakery"]["description"].lower())
            self.assertIn("Corner Kiosk", names)
            reasons = " | ".join(r["reject_reason"] for r in rej)
            for expect in ("chain", "not in Canada", "closed", "no valid phone", "not a POS fit"):
                self.assertIn(expect, reasons)
            self.assertNotIn("Totally Different Shop", [r["business_name"] for r in qual + rev + rej])
            # second run must not repeat anything
            res2 = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
            self.assertEqual(res2.returncode, 0, res2.stdout + res2.stderr)
            out2 = sorted((tmp / "out").glob("*"))[0]
            qual2 = list(csv.DictReader(open(out2 / "qualified_leads.csv", encoding="utf-8-sig")))
            self.assertEqual(len(qual2), 0)
            print(res.stdout)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
