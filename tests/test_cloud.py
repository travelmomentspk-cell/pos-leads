import csv
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import run_cloud  # noqa: E402
from leadlib import cloud, extra  # noqa: E402

CFG = extra.load_config(ROOT / "config_cloud.yaml")


class Units(unittest.TestCase):
    def test_config_has_23_categories_and_lists(self):
        self.assertEqual(len(CFG["categories"]), 23)
        for c in CFG["categories"]:
            self.assertTrue(c["keywords"] and c["needs"] and c["name"])
        rows = run_cloud.lists_rows(CFG)
        self.assertEqual(rows[0], ["ALL", "ALL", 60])
        self.assertEqual(rows[1][0], "Restaurants")
        self.assertIn("3D printing shops", [r[0] for r in rows])
        self.assertLessEqual(len(rows), 59)

    def test_pick(self):
        cats = CFG["categories"]
        self.assertEqual(len(cloud.pick(cats, "ALL", lambda c: c["name"])), 23)
        self.assertEqual([c["name"] for c in cloud.pick(cats, "clinics, Florists", lambda c: c["name"])], ["Clinics", "Florists"])
        self.assertEqual(cloud.pick(CFG["cities"], "toronto on"), ["Toronto ON"])

    def test_clean_and_status_payload(self):
        sent = []
        orig = cloud._post
        cloud._post = lambda url, payload, tries=3: sent.append((url, payload)) or True
        try:
            cloud.set_status("http://x", "Done", 'a "quoted"\nline with \\ slash', "run-1")
        finally:
            cloud._post = orig
        payload = sent[0][1]
        self.assertEqual(payload["range"], "Control!B8:B11")
        rows = json.loads(payload["rows_json"])
        self.assertEqual(rows[0], ["Done"])
        self.assertNotIn('"', rows[1][0])
        self.assertNotIn("\n", rows[1][0])
        self.assertEqual(rows[3], ["run-1"])

    def test_google_search_decision_maker(self):
        def search(items):
            return lambda q, k, c, num=5: items
        good = [{"link": "https://ca.linkedin.com/in/janedoe", "title": "Jane Doe - Owner - Maple Leaf Cafe | LinkedIn", "snippet": "Toronto"}]
        r = cloud.linkedin_decision_maker("Maple Leaf Cafe", "Toronto ON", "k", "c", search(good))
        self.assertEqual((r["name"], r["title"]), ("Jane Doe", "Owner"))
        snip = [{"link": "https://linkedin.com/in/sam", "title": "Sam Patel - Toronto | LinkedIn", "snippet": "General Manager at Maple Leaf Cafe · Toronto"}]
        self.assertEqual(cloud.linkedin_decision_maker("Maple Leaf Cafe", "Toronto", "k", "c", search(snip))["title"], "Manager")
        wrong_company = [{"link": "https://linkedin.com/in/x", "title": "Al Khan - Owner - Other Bakery | LinkedIn", "snippet": ""}]
        self.assertIsNone(cloud.linkedin_decision_maker("Maple Leaf Cafe", "Toronto", "k", "c", search(wrong_company)))
        staff = [{"link": "https://linkedin.com/in/y", "title": "Ann Lee - Cashier - Maple Leaf Cafe | LinkedIn", "snippet": ""}]
        self.assertIsNone(cloud.linkedin_decision_maker("Maple Leaf Cafe", "Toronto", "k", "c", search(staff)))
        notprofile = [{"link": "https://example.com/post", "title": "Jane Doe - Owner - Maple Leaf Cafe", "snippet": ""}]
        self.assertIsNone(cloud.linkedin_decision_maker("Maple Leaf Cafe", "Toronto", "k", "c", search(notprofile)))

    def test_rejected_cache(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            c = cloud.RejectedCache(tmp / "r.json", 14)
            c.add(["phone:+1"], date.today() - timedelta(days=3))
            c.add(["phone:+2"], date.today() - timedelta(days=30))
            c.save()
            c2 = cloud.RejectedCache(tmp / "r.json", 14)
            self.assertTrue(c2.recent(["phone:+1"]))
            self.assertFalse(c2.recent(["phone:+2"]))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class Site(BaseHTTPRequestHandler):
    OLD = ("<html><head><title>x</title></head><body>&copy; 2016 <a href='/{b}/about'>About</a> "
           "<a href='https://www.facebook.com/{b}'>fb</a> We're hiring a cook! Visit us.</body></html>")
    PAGES = {
        "/biz1/": OLD.format(b="biz1"), "/biz1/about": "<html><body><p>Jane Doe, Owner</p></body></html>",
        "/biz2/": OLD.format(b="biz2") + '<script type="application/ld+json">{"@type":"LocalBusiness","founder":{"@type":"Person","name":"Ravi Shah"}}</script>',
        "/biz2/about": "<html><body>Our story</body></html>",
        "/biz3/": OLD.format(b="biz3"), "/biz3/about": "<html><body>We love food. Family run.</body></html>",
        "/biz4/": "<html><head><meta name=\"viewport\" content=\"x\"></head><body>&copy; 2026 <a href='/biz4/about'>About</a><form><input type=\"email\"></form></body></html>",
        "/biz4/about": "<html><body>Owner: Ali Khan</body></html>",
        "/biz5/": OLD.format(b="biz5"), "/biz5/about": "<html><body><p>Jane Roe, Owner</p></body></html>",
    }

    def do_GET(self):
        body = self.PAGES.get(self.path)
        if body is None:
            self.send_response(404); self.end_headers(); return
        self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers(); self.wfile.write(body.encode())

    def log_message(self, *a):
        pass


class Hook(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.server.captured.append(json.loads(body))
        self.send_response(200); self.end_headers(); self.wfile.write(b"Accepted")

    def log_message(self, *a):
        pass


HEADER = ["input_id", "link", "title", "category", "address", "website", "phone", "review_count", "review_rating",
          "status", "place_id", "complete_address", "user_reviews", "emails"]


class EndToEnd(unittest.TestCase):
    def test_full_cloud_run_and_no_repeat(self):
        sites, hooks = {}, {}
        try:
            for i in (1, 2, 3, 4, 5):
                sites[i] = HTTPServer((f"127.0.0.{i}", 0), Site)
                threading.Thread(target=sites[i].serve_forever, daemon=True).start()
        except OSError as exc:
            for s in sites.values():
                s.shutdown()
            self.skipTest(f"cannot open local test servers here: {exc}")
        for name in ("leads", "status"):
            hooks[name] = HTTPServer(("127.0.0.1", 0), Hook)
            hooks[name].captured = []
            threading.Thread(target=hooks[name].serve_forever, daemon=True).start()
        tmp = Path(tempfile.mkdtemp())
        try:
            rows = []
            for i, name in ((1, "Biz One Cafe"), (2, "Biz Two Cafe"), (3, "Biz Three Cafe"), (4, "Biz Four Cafe"), (5, "Biz Five Cafe")):
                r = {h: "" for h in HEADER}
                r.update(input_id="c1", title=name, category="Cafe", website=f"http://127.0.0.{i}:{sites[i].server_port}/biz{i}/",
                         address=f"{i} King St, Toronto, ON M5V 2T6, Canada", phone=f"416-555-01{i:02d}", review_count="80",
                         review_rating="4.4", place_id=f"Z{i}", emails=f"info@biz{i}.test" if i not in (3, 5) else "")
                rows.append(r)
            rows.append({**rows[0], "place_id": "Z1b"})                      # same business seen twice in one scrape
            csvp = tmp / "r.csv"
            with open(csvp, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=HEADER); w.writeheader(); w.writerows(rows)
            (tmp / "meta.json").write_text(json.dumps({"c1": {"type_name": "Cafes", "needs": ["POS", "Website"], "query": "cafe in Toronto ON"}}))
            cfgtext = (ROOT / "config_cloud.yaml").read_text(encoding="utf-8")
            cfgtext = cfgtext.replace("out: out/cloud", f"out: {tmp / 'out'}").replace("state: state/cloud", f"state: {tmp / 'state'}")
            cfgtext = cfgtext.replace("mx_check: true", "mx_check: false")
            (tmp / "cfg.yaml").write_text(cfgtext, encoding="utf-8")
            env = {"PATH": "/usr/bin:/bin", "MAKE_WEBHOOK_URL": f"http://127.0.0.1:{hooks['leads'].server_port}/a",
                   "MAKE_STATUS_WEBHOOK_URL": f"http://127.0.0.1:{hooks['status'].server_port}/b", "PYTHONIOENCODING": "utf-8"}
            import os
            env = {**os.environ, **{k: v for k, v in env.items() if k != "PATH"}}
            env.pop("GOOGLE_CSE_KEY", None); env.pop("GOOGLE_CSE_CX", None)
            cmd = [sys.executable, str(ROOT / "run_cloud.py"), "--config", str(tmp / "cfg.yaml"), "run", "--csv", str(csvp),
                   "--category", "Cafes", "--city", "Toronto ON", "--min-score", "70", "--run-id", "t1"]
            res = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT, env=env, encoding="utf-8")
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            self.assertIn("6 scraped, 1 duplicate, 3 rejected, 2 final", res.stdout)
            lead_posts = {p["tab"]: json.loads(p["rows_json"]) for p in hooks["leads"].captured}
            self.assertEqual(set(lead_posts), {"New Leads", "Master", "Rejected", "Run Log"})
            names = {r[0]: r for r in lead_posts["New Leads"]}
            self.assertEqual(set(names), {"Biz One Cafe", "Biz Two Cafe"})
            self.assertEqual(names["Biz One Cafe"][1:3], ["Jane Doe", "Owner"])       # from the About page
            self.assertEqual(names["Biz Two Cafe"][1:3], ["Ravi Shah", "Founder"])    # from schema.org data
            self.assertGreaterEqual(names["Biz One Cafe"][6], 70)
            self.assertIn("Hiring", names["Biz One Cafe"][7])
            self.assertEqual(len(lead_posts["New Leads"][0]), len(cloud.NEW_LEAD_COLS))
            self.assertEqual(len(lead_posts["Master"][0]), len(cloud.MASTER_COLS))
            reasons = {r[0]: r[2] for r in lead_posts["Rejected"]}
            self.assertIn("missing decision maker name, email", reasons["Biz Three Cafe"])
            self.assertEqual(reasons["Biz Five Cafe"].split(" (")[0], "missing email")   # owner found, but no email: Rejected
            self.assertIn("below 70", reasons["Biz Four Cafe"])
            self.assertEqual(lead_posts["Run Log"][0][6:10], [6, 1, 3, 2])
            st = [(p["range"], json.loads(p["rows_json"])) for p in hooks["status"].captured]
            self.assertEqual(st[0][1][0], ["Running"])
            self.assertEqual([s for s in st if s[0] == "Control!B8:B11"][-1][1][0], ["Done"])
            self.assertTrue(any(s[0] == "Lists!A2:C60" for s in st))
            # second run: everything delivered is remembered, the rest was rejected a moment ago
            hooks["leads"].captured.clear()
            res2 = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT, env=env, encoding="utf-8")
            self.assertIn("6 scraped, 6 duplicate, 0 rejected, 0 final", res2.stdout)
            # a failing run is reported as Failed in the status cell
            hooks["status"].captured.clear()
            bad = subprocess.run(cmd[:-8] + ["--csv", str(tmp / "missing.csv")], capture_output=True, text=True, cwd=ROOT, env=env, encoding="utf-8")
            self.assertNotEqual(bad.returncode, 0)
            self.assertEqual([json.loads(p["rows_json"])[0] for p in hooks["status"].captured if p["range"] == "Control!B8:B11"][-1], ["Failed"])
        finally:
            for s in list(sites.values()) + list(hooks.values()):
                s.shutdown()
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
