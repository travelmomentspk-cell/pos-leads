import csv
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from leadlib.enrich import digital_signals  # noqa: E402
from leadlib.quality import classify_title, find_decision_maker, quality_gate  # noqa: E402

OLD_HOME = ("<html><head><title>x</title></head><body>&copy; 2016 <a href='/{b}/about'>About</a> "
            "<a href='https://www.facebook.com/{b}'>fb</a> We're hiring a cook! Visit us.</body></html>")
PAGES = {
    "/biz1/": OLD_HOME.format(b="biz1"),
    "/biz1/about": "<html><body><h1>About</h1><p>Jane Doe, Owner</p></body></html>",
    "/biz2/": OLD_HOME.format(b="biz2"),
    "/biz2/about": "<html><body><p>We love food. Family run. Open daily.</p></body></html>",
    "/biz3/": "<html><head><meta name=\"viewport\" content=\"width=device-width\"></head><body>&copy; 2026 <a href='/biz3/about'>About</a> "
              "<form><input type=\"email\"></form> add to cart</body></html>",
    "/biz3/about": "<html><body><p>Owner: Ali Khan</p></body></html>",
}


class Site(BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGES.get(self.path)
        if body is None:
            self.send_response(404); self.end_headers(); return
        self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *a):
        pass


def lead(**kw):
    d = {"name": "Biz", "phone": "+19055550142", "phone_status": "ok", "email_status": "ok_own_domain",
         "website": "https://biz.ca", "digital": {}, "site_checked": False, "reviews": [], "pages": [],
         "pos_detected": [], "online_ordering": False}
    d.update(kw)
    return d


class QualityUnit(unittest.TestCase):
    def test_titles(self):
        self.assertEqual(classify_title("Co-Owner")[0], "Owner")
        self.assertEqual(classify_title("General Manager")[0], "Manager")
        self.assertIsNone(classify_title("Cashier"))
        self.assertIsNone(classify_title("Server"))

    def test_full_marks_example(self):
        d = digital_signals("http://biz.ca/", "<html>&copy; 2015</html>")
        d["has_facebook"] = True
        reviews = ["Nice but their card machine was down again"]
        pages = [("https://biz.ca/about", "Jane Doe, Owner. We're hiring!")]
        q = quality_gate(lead(digital=d, site_checked=True, reviews=reviews, pages=pages))
        self.assertTrue(q["accepted"])
        self.assertEqual(q["score"], 100)
        self.assertEqual((q["decision_maker"], q["decision_maker_title"]), ("Jane Doe", "Owner"))
        for word in ("Decision maker", "Valid Canadian phone", "own domain", "dated", "payment or POS", "Hiring", "Facebook"):
            self.assertIn(word, q["reason"])

    def test_no_website_counts_as_signal(self):
        q = quality_gate(lead(website="", pages=[("u", "Founded by Maria Gonzalez in 2019")]))
        self.assertEqual(q["score"], 25 + 10 + 10 + 15)

    def test_website_pain_review(self):
        q = quality_gate(lead(reviews=["Tried to order but I couldn't order online, site was broken"],
                              pages=[("u", "Sam Patel, Manager")]))
        self.assertIn("website or online ordering", q["reason"])

    def test_staff_only_is_rejected_even_with_high_score(self):
        q = quality_gate(lead(website="", reviews=["cash only and card machine down"],
                              pages=[("u", "Anna Lopez, Cashier")]))
        self.assertFalse(q["accepted"])
        self.assertIn("no decision maker", q["reject_reason"])

    def test_low_score_rejected_and_switchable(self):
        q = quality_gate(lead(email_status="none", pages=[("u", "Priya Nair | Director of Operations")]))
        self.assertFalse(q["accepted"])
        self.assertIn("below 70", q["reject_reason"])
        self.assertTrue(quality_gate(lead(email_status="none", pages=[("u", "Priya Nair | Director")]),
                                      {"min_score": 30})["accepted"])
        self.assertTrue(quality_gate(lead(), {"require_decision_maker": False, "min_score": 20})["accepted"])

    def test_decision_maker_supplied_by_another_source(self):
        q = quality_gate(lead(decision_maker="Lee Wong", decision_maker_title="Founder", website=""))
        self.assertEqual(q["decision_maker"], "Lee Wong")
        q = quality_gate(lead(decision_maker="Lee Wong", decision_maker_title="Waiter", website=""))
        self.assertFalse(q["accepted"])


class QualityEndToEnd(unittest.TestCase):
    def test_pipeline_with_local_websites(self):
        servers = {}
        try:
            for i in (1, 2, 3):
                servers[i] = HTTPServer((f"127.0.0.{i}", 0), Site)
                threading.Thread(target=servers[i].serve_forever, daemon=True).start()
        except OSError as exc:
            for s in servers.values():
                s.shutdown()
            self.skipTest(f"cannot open local test servers here: {exc}")
        tmp = Path(tempfile.mkdtemp())
        try:
            header = ["input_id", "link", "title", "category", "address", "website", "phone", "review_count",
                      "review_rating", "status", "place_id", "complete_address", "user_reviews", "emails"]
            rows = []
            for i, name in ((1, "Biz One Cafe"), (2, "Biz Two Cafe"), (3, "Biz Three Cafe")):
                r = {h: "" for h in header}
                r.update(input_id="c", title=name, category="Cafe", website=f"http://127.0.0.{i}:{servers[i].server_port}/biz{i}/",
                         address=f"{i} King St, Toronto, ON M5V 2T6, Canada", phone=f"416-555-01{i:02d}",
                         review_count="80", review_rating="4.4", place_id=f"Z{i}", emails=f"info@biz{i}.test")
                rows.append(r)
            csvp = tmp / "r.csv"
            with open(csvp, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=header); w.writeheader(); w.writerows(rows)
            cfg = (ROOT / "config.yaml").read_text().replace("mx_check: true", "mx_check: false")
            cfg = cfg.replace("out: out", f"out: {tmp / 'out'}").replace("state: state", f"state: {tmp / 'state'}")
            (tmp / "cfg.yaml").write_text(cfg)
            res = subprocess.run([sys.executable, str(ROOT / "run_daily.py"), "--config", str(tmp / "cfg.yaml"),
                                  "--csv", str(csvp), "--no-push"], capture_output=True, text=True, cwd=ROOT)
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            out = sorted((tmp / "out").glob("*"))[0]
            rd = lambda n: list(csv.DictReader(open(out / n, encoding="utf-8-sig")))  # noqa: E731
            q, qr = rd("qualified_leads.csv"), rd("quality_rejected.csv")
            self.assertEqual([r["business_name"] for r in q], ["Biz One Cafe"], res.stdout)
            self.assertEqual((q[0]["decision_maker"], q[0]["decision_maker_title"]), ("Jane Doe", "Owner"))
            self.assertGreaterEqual(int(q[0]["score"]), 70)
            self.assertIn("hiring", q[0]["reason"].lower())
            self.assertIn("Decision maker", q[0]["reason"])
            self.assertEqual(q[0]["source"], "google_maps_manual")
            rej = {r["business_name"]: r["reject_reason"] for r in qr}
            self.assertIn("no decision maker", rej["Biz Two Cafe"])
            self.assertIn("below 70", rej["Biz Three Cafe"])
            self.assertIn("Quality filter: 2 leads rejected", res.stdout)
        finally:
            for s in servers.values():
                s.shutdown()
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
