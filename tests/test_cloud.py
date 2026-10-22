import csv
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from argparse import Namespace
from unittest import mock
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import run_cloud  # noqa: E402
from leadlib import cloud, enrich, extra, scraper  # noqa: E402

CFG = extra.load_config(ROOT / "config_cloud.yaml")


class Units(unittest.TestCase):
    def test_config_has_23_categories_and_lists(self):
        self.assertEqual(len(CFG["categories"]), 35)
        for c in CFG["categories"]:
            self.assertTrue(c["keywords"] and c["needs"] and c["name"])
        rows = run_cloud.lists_rows(CFG)
        self.assertEqual(rows[0], ["ALL", "ALL", 60])
        self.assertEqual(rows[1][0], "Restaurants")
        self.assertIn("3D printing shops", [r[0] for r in rows])
        self.assertEqual(len(rows), 59)                                 # always rewrites the whole A2:C60 block
        self.assertEqual(rows[-1], ["", "", ""])
        names = [r[0] for r in rows]
        for new in ("Dry Cleaners & Laundry Services", "Shisha Lounges & Bars", "Billiards, Snooker & Gaming Zones"):
            self.assertIn(new, names)
        self.assertIn("Red Deer AB", [r[1] for r in rows])

    def test_pick(self):
        cats = CFG["categories"]
        self.assertEqual(len(cloud.pick(cats, "ALL", lambda c: c["name"])), 35)
        self.assertEqual([c["name"] for c in cloud.pick(cats, "clinics, Florists", lambda c: c["name"])], ["Clinics", "Florists"])
        self.assertEqual(cloud.pick(CFG["cities"], "toronto on"), ["Toronto ON"])
        # names that contain a comma must still be found as one name
        for name in ("Billiards, Snooker & Gaming Zones", "Tutoring, music and driving schools"):
            self.assertEqual([c["name"] for c in cloud.pick(cats, name, lambda c: c["name"])], [name])

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


class MissingWebhook(unittest.TestCase):
    def test_missing_leads_webhook_fails_loudly_and_saves_nothing(self):
        import os
        hook = HTTPServer(("127.0.0.1", 0), Hook)
        hook.captured = []
        threading.Thread(target=hook.serve_forever, daemon=True).start()
        tmp = Path(tempfile.mkdtemp())
        try:
            header = ["input_id", "link", "title", "category", "address", "website", "phone", "review_count", "review_rating",
                      "status", "place_id", "complete_address", "user_reviews", "emails"]
            r = {h: "" for h in header}
            r.update(input_id="c1", title="Some Cafe", category="Cafe", address="1 King St, Toronto, ON M5V 2T6, Canada",
                     phone="416-555-0101", review_count="50", place_id="M1")
            csvp = tmp / "r.csv"
            with open(csvp, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=header); w.writeheader(); w.writerow(r)
            cfgtext = (ROOT / "config_cloud.yaml").read_text(encoding="utf-8")
            cfgtext = cfgtext.replace("out: out/cloud", f"out: {tmp / 'out'}").replace("state: state/cloud", f"state: {tmp / 'state'}")
            (tmp / "cfg.yaml").write_text(cfgtext, encoding="utf-8")
            env = {k: v for k, v in os.environ.items() if k not in ("MAKE_WEBHOOK_URL", "GOOGLE_CSE_KEY", "GOOGLE_CSE_CX")}
            env["MAKE_STATUS_WEBHOOK_URL"] = f"http://127.0.0.1:{hook.server_port}/s"
            res = subprocess.run([sys.executable, str(ROOT / "run_cloud.py"), "--config", str(tmp / "cfg.yaml"), "run", "--csv", str(csvp),
                                  "--no-enrich", "--run-id", "t9"], capture_output=True, text=True, cwd=ROOT, env=env, encoding="utf-8")
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("MAKE_WEBHOOK_URL is not set", res.stdout + res.stderr)
            last = [p for p in hook.captured if p["range"] == "Control!B8:B11"][-1]
            rows = json.loads(last["rows_json"])
            self.assertEqual(rows[0], ["Failed"])
            self.assertIn("MAKE_WEBHOOK_URL", rows[1][0])
            self.assertFalse((tmp / "state" / "rejected_cache.json").exists())     # nothing remembered
        finally:
            hook.shutdown()
            shutil.rmtree(tmp, ignore_errors=True)


class ScraperTimeout(unittest.TestCase):
    def test_docker_scraper_timeout_kills_its_container(self):
        calls = []

        def run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            if cmd[:2] == ["docker", "run"]:
                raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(scraper.shutil, "which", return_value="docker"), \
                mock.patch.object(scraper.subprocess, "run", side_effect=run):
            with self.assertRaisesRegex(RuntimeError, "exceeded its 1-second timeout"):
                scraper.run_scraper({"scraper": {"mode": "docker", "timeout_seconds": 1}},
                                    Path(tmp) / "queries.txt", tmp, "results.csv", 3)

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1]["timeout"], 1)
        self.assertIn("--name", calls[0][0])
        self.assertEqual(calls[1][0][:2], ["docker", "kill"])


class WebsiteTimeout(unittest.TestCase):
    def test_slow_stream_is_abandoned_at_the_page_deadline(self):
        import time

        response = mock.MagicMock()
        response.status_code = 200
        response.headers = {"Content-Type": "text/html"}
        response.url = "https://example.test/"
        response.encoding = "utf-8"
        response.__enter__.return_value = response

        def chunks(_):
            yield b"<html>"
            time.sleep(0.02)
            yield b"too late"

        response.iter_content.side_effect = chunks
        with mock.patch.object(enrich.requests, "get", return_value=response) as get:
            self.assertIsNone(enrich._get("https://example.test/", 0.01, False))

        self.assertLessEqual(get.call_args.kwargs["timeout"][0], 0.01)
        self.assertLessEqual(get.call_args.kwargs["timeout"][1], 0.01)
        response.__exit__.assert_called_once()


class TargetedBatches(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.args = Namespace(target_leads=50, csv=None)
        self.cfg = {"run": {"target_leads_per_run": 50, "max_runtime_minutes": 10},
                    "sources": {"linkedin_via_google_search": {"daily_budget": 80}},
                    "paths": {"state": str(self.tmp / "state")}}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_continues_until_50_and_checkpoints_every_batch(self):
        calls = []

        def batch(args, cfg, run_id, started, seen, remaining, index, cse_budget):
            calls.append((remaining, index, cse_budget))
            count = min(20, remaining)
            query = f"query-{index}"
            seen.add(query)
            return "batch complete", count, [query], 10

        with mock.patch.object(run_cloud, "pipeline", side_effect=batch), \
                mock.patch.object(run_cloud, "checkpoint_state") as checkpoint:
            summary, delivered = run_cloud.run_batches(self.args, self.cfg, "test-run", "start")

        self.assertEqual(delivered, 50)
        self.assertIn("50/50", summary)
        self.assertEqual(calls, [(50, 1, 80), (30, 2, 70), (10, 3, 60)])
        self.assertEqual(checkpoint.call_count, 3)

    def test_stops_when_searches_are_exhausted(self):
        def batch(args, cfg, run_id, started, seen, remaining, index, cse_budget):
            if index == 1:
                seen.add("only-search")
                return "batch complete", 7, ["only-search"], 0
            return "no unused searches", 0, [], 0

        with mock.patch.object(run_cloud, "pipeline", side_effect=batch), \
                mock.patch.object(run_cloud, "checkpoint_state"):
            summary, delivered = run_cloud.run_batches(self.args, self.cfg, "test-run", "start")

        self.assertEqual(delivered, 7)
        self.assertIn("all selected category/city searches have been used", summary)

    def test_failed_batch_does_not_count_or_checkpoint_delivery(self):
        def batch(args, cfg, run_id, started, seen, remaining, index, cse_budget):
            if index == 1:
                seen.add("first")
                return "batch complete", 12, ["first"], 0
            raise RuntimeError("Sheet delivery failed")

        with mock.patch.object(run_cloud, "pipeline", side_effect=batch), \
                mock.patch.object(run_cloud, "checkpoint_state") as checkpoint:
            with self.assertRaisesRegex(RuntimeError, "Sheet delivery failed"):
                run_cloud.run_batches(self.args, self.cfg, "test-run", "start")

        checkpoint.assert_called_once_with()

    def test_pending_master_rows_are_retried_before_search_resumes(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            state = tmp / "state"
            state.mkdir()
            pending = state / "pending_master_rows.json"
            rows = [{"Business": "Already delivered"}]
            pending.write_text(json.dumps(rows), encoding="utf-8")
            cfg = {"sheet": {"tabs": {"master": "Master"}}}
            with mock.patch.object(run_cloud.cloud, "push_rows", return_value=True) as push, \
                    mock.patch.object(run_cloud, "checkpoint_state") as checkpoint:
                run_cloud.flush_pending_master(cfg, state)

            push.assert_called_once_with("", "Master", cloud.MASTER_COLS, rows)
            checkpoint.assert_called_once_with()
            self.assertFalse(pending.exists())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_failed_pending_master_upload_keeps_resume_record(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            state = tmp / "state"
            state.mkdir()
            pending = state / "pending_master_rows.json"
            pending.write_text(json.dumps([{"Business": "Already delivered"}]), encoding="utf-8")
            cfg = {"sheet": {"tabs": {"master": "Master"}}}
            with mock.patch.object(run_cloud.cloud, "push_rows", side_effect=RuntimeError("Sheet offline")):
                with self.assertRaisesRegex(RuntimeError, "Sheet offline"):
                    run_cloud.flush_pending_master(cfg, state)
            self.assertTrue(pending.exists())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_checkpoint_configures_git_identity_before_commit(self):
        calls = []

        def git(*args, **kwargs):
            calls.append(args[0])
            if args[0][:4] == ["git", "diff", "--cached", "--quiet"]:
                return subprocess.CompletedProcess(args[0], 1)
            return subprocess.CompletedProcess(args[0], 0)

        with mock.patch.dict("os.environ", {"GITHUB_ACTIONS": "true"}), \
                mock.patch.object(run_cloud.subprocess, "run", side_effect=git):
            run_cloud.checkpoint_state()

        name_config = ["git", "config", "user.name", "github-actions[bot]"]
        email_config = ["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"]
        commit = ["git", "commit", "-m", "Checkpoint lead run state [skip ci]"]
        self.assertLess(calls.index(name_config), calls.index(commit))
        self.assertLess(calls.index(email_config), calls.index(commit))


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
            scores = [r[3] for r in lead_posts["Rejected"] if isinstance(r[3], (int, float))]
            self.assertEqual(scores, sorted(scores, reverse=True))      # near misses come first
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
