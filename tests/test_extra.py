import csv
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from leadlib import export, extra  # noqa: E402

CFG = extra.load_config(ROOT / "config_extra.yaml")
NOW = datetime.now(timezone.utc)


def prov(*reviews, name="Acme Payments", pid="PR1"):
    return {"place_id": pid, "name": name, "reviews": list(reviews)}


def rv(text, rating=1, days=60, name="Joe Smith", rid=""):
    return {"name": name, "url": "https://maps/contrib/1", "rating": rating, "texts": [text],
            "published": NOW - timedelta(days=days), "when": "", "id": rid}


class Config(unittest.TestCase):
    def test_fifteen_types(self):
        t = CFG["business_types"]
        self.assertEqual(len(t), 15)
        self.assertEqual(len({x["id"] for x in t}), 15)
        for x in t:
            self.assertTrue(x["keywords"] and x["needs"] and x["name"], x)

    def test_queries(self):
        rot = {}
        q1, meta = extra.build_type_queries(CFG, rot, "a")
        q2, _ = extra.build_type_queries(CFG, rot, "b")
        self.assertEqual(len(q1), CFG["market"]["queries_per_run"])
        self.assertTrue({x["text"] for x in q1}.isdisjoint({x["text"] for x in q2}))
        self.assertEqual(len({m["type_name"] for m in meta.values()}), 15)
        p1, pm = extra.build_provider_queries(CFG, rot, "c")
        self.assertEqual(len(p1), CFG["complaints"]["provider_queries_per_run"])
        self.assertTrue(any("Moneris" in q["text"] for q in p1))
        self.assertEqual(pm[p1[0]["id"]]["kind"], "Named provider")

    def test_make_secret_overrides_config_webhook(self):
        response = mock.Mock(status_code=200)
        with mock.patch.dict("os.environ", {"MAKE_WEBHOOK_URL": "http://127.0.0.1/mock-secret"}), \
                mock.patch.object(export.requests, "post", return_value=response) as post, \
                mock.patch.object(export.time, "sleep"):
            self.assertTrue(export.push(
                {"sheets": {"make_webhook_url": "http://old-config-url"}},
                "Provider_Complaints", ["provider_name"], [{"provider_name": "Test Provider"}]))

        post.assert_called_once()
        self.assertEqual(post.call_args.args[0], "http://127.0.0.1/mock-secret")
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["tab"], "Provider_Complaints")
        self.assertEqual(json.loads(payload["rows_json"]), [["Test Provider"]])

    def test_requested_sheet_push_fails_if_no_endpoint_is_configured(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "results were not sent"):
                export.push({"sheets": {}}, "Provider_Complaints", ["provider_name"],
                            [{"provider_name": "Test Provider"}])


class Complaints(unittest.TestCase):
    def setUp(self):
        self.f = extra.ComplaintFinder(CFG["complaints"])

    def test_finds_topics_and_business_clue(self):
        c = self.f.scan(prov(rv("As a small business we got hidden fees and the terminal stopped working twice. Support never called back.")))
        self.assertEqual(len(c), 1)
        self.assertEqual(set(c[0]["topics"]), {"Hidden fees or pricing", "Terminal or system not working", "Poor support"})
        self.assertEqual(c[0]["hint"].lower(), "as a small business")
        self.assertIn("hidden fees", c[0]["quote"])

    def test_french(self):
        c = self.f.scan(prov(rv("Frais cachés et le terminal ne fonctionne pas")))
        self.assertEqual(set(c[0]["topics"]), {"Hidden fees or pricing", "Terminal or system not working"})

    def test_rating_age_and_whole_words(self):
        self.assertEqual(self.f.scan(prov(rv("hidden fees everywhere", rating=5))), [])
        self.assertEqual(self.f.scan(prov(rv("hidden fees everywhere", days=1500))), [])
        self.assertEqual(self.f.scan(prov(rv("Great place, no scammers here, cancelled my fears"))), [])
        self.assertEqual(len(self.f.scan(prov(rv("hidden fees everywhere", rating=2, days=100)))), 1)

    def test_relative_date_fallback(self):
        r = rv("hidden fees", days=0)
        r["published"], r["when"] = None, "3 years ago"
        self.assertEqual(self.f.scan(prov(r)), [])
        r["when"] = "2 months ago"
        self.assertEqual(len(self.f.scan(prov(r))), 1)

    def test_reviewer_name_looks_like_business(self):
        self.assertTrue(self.f.name_looks_like_business("Maple Leaf Cafe"))
        self.assertTrue(self.f.name_looks_like_business("Sunrise Dental Clinic"))
        self.assertFalse(self.f.name_looks_like_business("Joe Smith"))


class Loaders(unittest.TestCase):
    def test_json_lines_and_array_with_extended_reviews(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            obj = {"input_id": "x", "title": "Acme Pay", "address": "1 King St, Toronto, ON M5V 2T6, Canada", "phone": "416-555-0100",
                   "review_count": 300, "review_rating": 2.1, "link": "https://maps/x", "place_id": "P",
                   "user_reviews": [{"Name": "A", "Rating": 5, "Description": "fine"}],
                   "user_reviews_extended": [{"name": "B", "rating": 1, "text": "Hidden fees!", "review_id": "r9"}]}
            (tmp / "a.json").write_text(json.dumps(obj) + "\n" + json.dumps({**obj, "title": "Second"}) + "\n", encoding="utf-8")
            (tmp / "b.json").write_text(json.dumps([obj]), encoding="utf-8")
            for fn, n in (("a.json", 2), ("b.json", 1)):
                rows = extra.load_results(tmp / fn)
                self.assertEqual(len(rows), n)
                self.assertEqual(rows[0]["province"], "ON")
                self.assertEqual(len(rows[0]["reviews"]), 2)
                self.assertEqual(rows[0]["reviews"][1]["id"], "r9")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class Scoring(unittest.TestCase):
    def lead(self, **kw):
        d = {"business_type": "Gyms", "needs": ["CRM", "POS"], "phone": "+14165550100", "phone_status": "ok", "email": None,
             "website": "", "review_count": 80, "digital": {}, "site_checked": False}
        d.update(kw)
        return d

    def test_need_signal(self):
        s = extra.score_type_lead(self.lead(), CFG)
        self.assertTrue(s["need"])
        self.assertIn("Website", s["services_to_pitch"])
        s = extra.score_type_lead(self.lead(website="https://mygym.ca"), CFG)
        self.assertFalse(s["need"])
        s = extra.score_type_lead(self.lead(website="https://www.facebook.com/mygym"), CFG)
        self.assertTrue(s["need"])
        s = extra.score_type_lead(self.lead(website="https://mygym.ca", site_checked=True,
                                            digital={"no_viewport": True, "copyright_year": 2015}), CFG)
        self.assertTrue(s["need"])
        self.assertIn("dated", s["reason"])


HEADER = ["input_id", "link", "title", "category", "address", "website", "phone", "review_count", "review_rating",
          "status", "place_id", "complete_address", "user_reviews", "emails"]


def crow(**kw):
    r = {h: "" for h in HEADER}
    r.update(kw)
    return r


def reviews_json(*items):
    return json.dumps([{"Name": n, "Rating": str(r), "rating_float": str(r), "Description": t,
                        "author_url": f"https://www.google.com/maps/contrib/{n.replace(' ', '')}",
                        "published_at": (NOW - timedelta(days=45)).isoformat(), "review_id": f"id-{n}-{i}"}
                       for i, (n, r, t) in enumerate(items)])


class EndToEnd(unittest.TestCase):
    def test_both_sources_on_synthetic_files(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            def write(name, rows):
                p = tmp / name
                with open(p, "w", newline="", encoding="utf-8") as f:
                    w = csv.DictWriter(f, fieldnames=HEADER); w.writeheader(); w.writerows(rows)
                return p
            types = write("types.csv", [
                crow(input_id="t1", title="Iron Works Gym", category="Gym", address="5 Main St, Calgary, AB T2P 1B5, Canada", phone="403-555-0111",
                     website="https://www.facebook.com/ironworks", review_count="90", review_rating="4.5", place_id="T1"),
                crow(input_id="t1", title="Modern Studio Pilates", category="Pilates studio", address="9 Bay St, Toronto, ON M5J 2S1, Canada",
                     phone="416-555-0112", website="https://modernpilates.ca", review_count="70", place_id="T2"),
                crow(input_id="t2", title="Brooklyn Gym", category="Gym", address="1 Main St, Brooklyn, NY 11201, United States", phone="212-555-0100", place_id="T3"),
                crow(input_id="t1", title="Goodlife Fitness", category="Gym", address="2 King St, Toronto, ON M5V 2T6, Canada", phone="416-555-0113",
                     website="https://goodlife.com", review_count="400", place_id="T4"),
                crow(input_id="t1", title="Iron Works Gym", category="Gym", address="5 Main St, Calgary, AB T2P 1B5, Canada", phone="403-555-0111", place_id="T1b"),
                crow(input_id="t1", title="No Phone Gym", category="Gym", address="3 Elm St, Ottawa, ON K1A 0A1, Canada", phone="", place_id="T5"),
            ])
            (tmp / "meta.json").write_text(json.dumps({
                "t1": {"type_name": "Gyms and fitness studios", "needs": ["CRM", "POS", "Payment gateway"], "query": "gym in Calgary AB"},
                "t2": {"type_name": "Gyms and fitness studios", "needs": ["CRM"], "query": "gym in New York"}}))
            provs = write("providers.csv", [
                crow(input_id="p1", title="Acme Merchant Services", address="10 Bloor St, Toronto, ON M4W 1A1, Canada", phone="416-555-0150",
                     review_count="210", review_rating="2.4", place_id="PV1", link="https://maps/acme",
                     user_reviews=reviews_json(("Maple Leaf Cafe", 1, "My cafe was charged hidden fees and their terminal stopped working. Support never called back."),
                                               ("Dana Lee", 5, "Great service, very happy"),
                                               ("Sam Roy", 2, "Cancellation fee is a trap, I was locked in for 4 years"))),
                crow(input_id="p2", title="Pixel Web Design", address="3 Queen St, Vancouver, BC V6B 1A1, Canada", phone="604-555-0160",
                     review_count="40", review_rating="3.0", place_id="PV2", link="https://maps/pixel",
                     user_reviews=reviews_json(("Priya N", 1, "Website never finished after 8 months, total waste of money"))),
            ])
            (tmp / "meta.json").write_text(json.dumps({
                "t1": {"type_name": "Gyms and fitness studios", "needs": ["CRM", "POS", "Payment gateway"], "query": "gym in Calgary AB"},
                "p1": {"kind": "POS & payments", "query": "merchant services in Toronto ON"},
                "p2": {"kind": "Website", "query": "web design company in Vancouver BC"}}))
            # the providers file must find its own meta: put both in separate folders
            (tmp / "ptypes").mkdir(); (tmp / "pprov").mkdir()
            shutil.copy(types, tmp / "ptypes" / "types.csv"); shutil.copy(provs, tmp / "pprov" / "providers.csv")
            (tmp / "ptypes" / "meta.json").write_text(json.dumps({
                "t1": {"type_name": "Gyms and fitness studios", "needs": ["CRM", "POS", "Payment gateway"], "query": "gym in Calgary AB"}}))
            (tmp / "pprov" / "meta.json").write_text(json.dumps({
                "p1": {"kind": "POS & payments", "query": "merchant services in Toronto ON"},
                "p2": {"kind": "Website", "query": "web design company in Vancouver BC"}}))
            cfgtext = (ROOT / "config_extra.yaml").read_text(encoding="utf-8")
            cfgtext = cfgtext.replace("out: out/extra", f"out: {tmp / 'out'}").replace("state: state/extra", f"state: {tmp / 'state'}")
            (tmp / "cfg.yaml").write_text(cfgtext, encoding="utf-8")
            cmd = [sys.executable, str(ROOT / "run_extra.py"), "--config", str(tmp / "cfg.yaml"), "--no-enrich",
                   "--csv-types", str(tmp / "ptypes" / "types.csv"), "--csv-providers", str(tmp / "pprov" / "providers.csv")]
            res = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT, encoding="utf-8")
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            self.assertIn("[types] 6 scraped, 1 duplicate, 5 new", res.stdout)
            self.assertIn("[providers] 2 provider listings read, 0 already seen, 3 new complaints", res.stdout)
            out = sorted((tmp / "out").glob("*"))[0]
            rd = lambda n: list(csv.DictReader(open(out / n, encoding="utf-8-sig")))  # noqa: E731
            leads, rej = rd("type_leads.csv"), rd("type_rejected.csv")
            self.assertEqual([r["business_name"] for r in leads], ["Iron Works Gym"])
            self.assertEqual(leads[0]["phone"], "+14035550111")
            self.assertIn("Only a social page", leads[0]["reason"])
            self.assertEqual(leads[0]["source"], "google_maps_business_types")
            reasons = {r["business_name"]: r["reject_reason"] for r in rej}
            self.assertIn("not in Canada", reasons["Brooklyn Gym"])
            self.assertIn("chain", reasons["Goodlife Fitness"])
            self.assertIn("no valid phone", reasons["No Phone Gym"])
            self.assertIn("no sign of need", reasons["Modern Studio Pilates"])
            comp, summ = rd("provider_complaints.csv"), rd("provider_summary.csv")
            self.assertEqual(len(comp), 3)
            self.assertEqual(comp[0]["reviewer_name"], "Maple Leaf Cafe")          # business-like name sorted first
            self.assertEqual(comp[0]["reviewer_name_looks_like_business"], "yes")
            self.assertIn("Hidden fees or pricing", comp[0]["topics"])
            self.assertIn("contrib", comp[0]["reviewer_profile_url"])
            self.assertEqual(comp[0]["provider_name"], "Acme Merchant Services")
            self.assertEqual(comp[0]["provider_kind"], "POS & payments")
            self.assertEqual(summ[0]["provider_name"], "Acme Merchant Services")
            self.assertEqual(summ[0]["complaints_found"], "2")
            # a second run must not repeat anything
            res2 = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT, encoding="utf-8")
            self.assertIn("[types] 6 scraped, 2 duplicate, 4 new", res2.stdout)   # the saved lead (and its twin) are remembered
            self.assertIn("0 new complaints", res2.stdout)
            self.assertEqual(list(csv.DictReader(open(sorted((tmp / "out").glob("*"))[0] / "type_leads.csv", encoding="utf-8-sig"))), [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
