import csv
import json
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

from leadlib.master import Master, norm_name, site_key  # noqa: E402

HEADER = ["input_id", "link", "title", "category", "address", "website", "phone", "review_count",
          "review_rating", "status", "place_id", "complete_address", "user_reviews", "emails"]
PAIN = json.dumps([{"Name": "A", "Description": "Good but cash only and the card machine was down."}])


def lead(name, phone="", website="", city="Toronto", place_id=""):
    return {"name": name, "phone_raw": phone, "website": website, "city": city, "place_id": place_id}


class MasterUnit(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.m = Master(self.tmp / "master.db")
        self.m.add("qualified", "Maple Leaf Cafe Inc.", "+1 (905) 555-0142", "https://www.mapleleafcafe.ca/menu", "Mississauga", "P1")
        self.m.db.commit()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_phone_any_format(self):
        new, dup = self.m.split([lead("Other Name", "905-555-0142"), lead("Other Name 2", "19055550142")])
        self.assertEqual((len(new), len(dup)), (0, 2))
        self.assertEqual(dup[0]["duplicate_of"], "phone")

    def test_website_domain(self):
        new, dup = self.m.split([lead("Totally Else", "416-555-0001", "http://mapleleafcafe.ca/")])
        self.assertEqual(dup[0]["duplicate_of"], "website")

    def test_name_fuzzy_same_city_only(self):
        a, b = self.m.split([lead("Maple Leaf Café", "", "", "Mississauga")])
        self.assertEqual((len(a), len(b)), (0, 1))
        self.assertEqual(b[0]["duplicate_of"], "name")
        a, b = self.m.split([lead("Maple Leaf Cafe", "", "", "Calgary")])  # same name, different city: kept
        self.assertEqual((len(a), len(b)), (1, 0))

    def test_numbered_names_are_different_businesses(self):
        m = Master(self.tmp / "m3.db")
        m.add("qualified", "Elite Barbers 1", "", "", "Toronto")
        new, dup = m.split([lead("Elite Barbers 2", "", "", "Toronto"), lead("Elite Barbers 1", "", "", "Toronto")])
        self.assertEqual([r["name"] for r in new], ["Elite Barbers 2"])

    def test_different_business_kept(self):
        new, dup = self.m.split([lead("Maple Leaf Bakery", "416-555-0777", "https://leafbakery.ca", "Mississauga")])
        self.assertEqual((len(new), len(dup)), (1, 0))

    def test_shared_hosts_do_not_collide(self):
        m = Master(self.tmp / "m2.db")
        m.add("qualified", "A Cafe", "", "https://www.facebook.com/acafe", "Toronto")
        new, dup = m.split([lead("B Bakery", "", "https://www.facebook.com/bbakery"),
                            lead("A Cafe Again", "", "https://facebook.com/acafe/")])
        self.assertEqual([r["name"] for r in new], ["B Bakery"])
        self.assertEqual(site_key("https://facebook.com"), "")

    def test_same_batch_duplicates(self):
        new, dup = self.m.split([lead("Fresh One", "416-555-0900"), lead("Fresh One Copy", "(416) 555-0900")])
        self.assertEqual((len(new), len(dup)), (1, 1))

    def test_persists_and_seeds(self):
        again = Master(self.tmp / "master.db")
        self.assertEqual(again.count(), 1)
        self.assertEqual(len(again.split([lead("x", "905-555-0142")])[1]), 1)
        csvp = self.tmp / "qualified_leads.csv"
        csvp.write_text("business_name,phone,website,city,place_id\nSeeded Bar,16045550123,https://seeded.ca,Vancouver,S1\n", encoding="utf-8")
        self.assertEqual(again.seed_csvs([csvp]), 1)
        self.assertEqual(len(again.split([lead("y", "604-555-0123")])[1]), 1)  # phone had lost its plus: still matches

    def test_norm_name(self):
        self.assertEqual(norm_name("The Maple Leaf Café, Inc."), "maple leaf cafe")


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200); self.end_headers(); self.wfile.write(b"Accepted")

    def log_message(self, *a):
        pass


class EndToEnd(unittest.TestCase):
    def test_lost_seen_file_does_not_repeat_leads(self):
        tmp = Path(tempfile.mkdtemp())
        srv = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            rows = []
            for i in range(1, 6):
                r = {h: "" for h in HEADER}
                r.update(input_id="c", title=f"Cafe Number {i}", category="Cafe", website=f"https://cafe{i}.ca",
                         address=f"{i} King St, Toronto, ON M5V 2T6, Canada", phone=f"416-555-01{i:02d}",
                         review_count="120", review_rating="4.5", place_id=f"Q{i}", user_reviews=PAIN)
                rows.append(r)
            csvp = tmp / "r.csv"
            with open(csvp, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=HEADER); w.writeheader(); w.writerows(rows)
            cfg = (ROOT / "config.yaml").read_text().replace("mx_check: true", "mx_check: false")
            cfg = cfg.replace("lead_quality:\n  enabled: true", "lead_quality:\n  enabled: false")
            cfg = cfg.replace("out: out", f"out: {tmp / 'out'}").replace("state: state", f"state: {tmp / 'state'}")
            import re
            cfg, n = re.subn(r'make_webhook_url:\s*".*"', f'make_webhook_url: "http://127.0.0.1:{srv.server_port}/hook"', cfg)
            self.assertEqual(n, 1, "the test must point the webhook at its own local server, never at a real one")
            (tmp / "cfg.yaml").write_text(cfg)
            cmd = [sys.executable, str(ROOT / "run_daily.py"), "--config", str(tmp / "cfg.yaml"), "--csv", str(csvp), "--no-enrich"]
            r1 = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
            self.assertEqual(r1.returncode, 0, r1.stdout + r1.stderr)
            self.assertIn("5 scraped, 0 duplicate, 5 new", r1.stdout)
            (tmp / "state" / "seen.json").unlink()            # the old memory is lost, as happened to the user
            r2 = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
            self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
            self.assertIn("5 scraped, 5 duplicate, 0 new", r2.stdout)
            print(r1.stdout.strip().splitlines()[-6:], r2.stdout.strip().splitlines()[2:4])
        finally:
            srv.shutdown()
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
