# Canada POS Leads: daily pipeline (100% free, no card)

Roman Urdu mein khulasa: ye project aap ke PC par chalta hai. Pehle free Google Maps scraper (gosom) business dhoondta hai,
phir har business ki website se email aur payment system dhoondta hai, Canada/phone/email/chain ke rules lagata hai,
score deta hai, aur sab se achhi leads ko CSV mein aur aap ki Google Sheet mein daal deta hai. Is mein koi paid API ya card nahi lagta.

## What it does

```
config.yaml targets ─┐
Vancouver new-licence ├─> free Maps scraper (Docker, on your PC, with website email crawl)
open data (+ more)   ─┘            │
                                   ▼
        Gate 1 (rules): Canada, open, valid phone, has website, not a chain, not already seen
                                   ▼
        Website visit: public email + where it was found, current payment provider, online ordering, "cash only"
                                   ▼
        Gate 2: email valid (syntax, junk filter, MX lookup), category fits a POS
                                   ▼
        Score 0-100 with a written reason for every point
                                   ▼
   Qualified_Leads (score >= 50, name + phone + address)  |  Review_Queue  |  Rejected_Log  ->  CSV + Make webhook -> Google Sheet
```

Every qualified lead carries `source`, `source_url`, `email_source_url` and an `evidence` text, so you can check any lead in under a minute.

## Where to keep this folder
Keep it at `C:\pos-leads` (not in Downloads, which people clean out). Your first scrape is included in `out\2026-10-06`, so you can process it without scraping again:
`py run_daily.py --reuse-dir out\2026-10-06 --ignore-seen`

## One-time setup (about 30 minutes)

1. **Python 3.10+** from python.org (free). Then in this folder: `pip install -r requirements.txt`
2. **Docker Desktop** (free for personal and small-business use) so the scraper can run. On Windows it needs WSL2, which Docker Desktop offers to install.
   No Docker? Download the scraper binary from https://github.com/gosom/google-maps-scraper/releases and set `scraper.mode: binary` and `scraper.binary_path` in `config.yaml`.
3. **Google Sheet receiver**: already built in your Make account as the scenario *POS Leads: Sheet receiver* and its URL is in `config.yaml` (`sheets.make_webhook_url`).
   Each push costs 2 Make operations whatever the number of rows, so the free plan is plenty. Test it: `python run_daily.py --test-push`, then look for a "connection-test" row in the Run_Log tab.
   Keep that URL private. (Apps Script in `apps_script/Code.gs` is only a fallback.)
4. **Edit `config.yaml`**: categories, cities, `queries_per_run`.
5. **Self-test, no internet needed**: `python -m unittest discover -s tests -v` (11 tests should pass).

## First real run (start small)

Set `queries_per_run: 3` and run `python run_daily.py --no-push`. Open `out/<today>/qualified_leads.csv` and check 10 leads by hand
(open `source_url`, `email_source_url`, call one number). If they look right, remove `--no-push` and raise `queries_per_run`.

Daily: `py run_daily.py` (or double-click `run_daily.bat`, which also starts Docker Desktop for you).

### Automatic daily run (Windows)
Open a normal PowerShell and run this once, changing the time and the folder path if needed (the PC must be on and logged in at that time):
`schtasks /Create /SC DAILY /TN "POS Leads" /ST 02:00 /TR "\"C:\pos-leads\run_daily.bat\""`
Results appear in the Google Sheet, and a log is written to the `logs` folder.

### Re-process an earlier scrape without scraping again
`py run_daily.py --reuse-dir out\2026-10-06 --ignore-seen`
Leads that qualified but did not fit under the daily cap are kept in `state/backlog.json` and are offered first on the next run (for up to 14 days).

## How a lead is judged

| Check | Rule |
|---|---|
| Canada | Canadian postal code or province, no US address |
| Phone | valid 10-digit NANP number; Canadian area code is recorded in `phone_status` |
| Email | optional bonus. If a public email is found (own website or the Maps scraper's crawl) it must be non-junk and its domain must have mail servers; otherwise the lead is kept and the description says to call and ask for it |
| Not a chain | names in `chains.txt` are rejected |
| POS fit | category points: restaurant/cafe/bakery/salon/retail high, gyms and studios medium, law/finance/schools excluded |
| Signals | new licence in the last 120 days, review mentioning card-machine/cash-only problems, "cash only" on the website, a payment provider named on the website, no online ordering |

Tier A = a clear sign they need a POS now. Tier B = a softer sign. Tier C = good fit, no sign yet.
Business name, phone and address are required. A missing email never blocks a lead. Leads scoring between `review_score_floor` and `min_score_qualified` go to `Review_Queue`.

## What to expect (honest numbers)

- Email is optional now, so far more records qualify. Roughly how many raw records you need for 100 per day is unknown until you measure it:
  your first week of `Run_Log` rows gives the real ratio.
  If you fall short, raise `queries_per_run` or `scraper.depth`, add cities, or lower `min_score_qualified`.
- Email crawling makes the scraper slow. A full day's run can take hours. Run it overnight.
- "Authentic" here means verifiable: sources are public, emails are visible on the business's own pages, and each lead has the link to prove it.
  Phone numbers cannot be confirmed as working without calling them.

## Sources

| Source | Status |
|---|---|
| Google Maps search via gosom scraper | Flags and CSV fields checked against the project's README. |
| Vancouver new business licences (open data, Opendatasoft API) | Dataset exists and is updated daily. Field names were not tested from here: run `python run_daily.py --probe-licences` once. If the printed field names differ from `businesstype, businessname, businesstradename, house, street, issueddate, status`, edit `leadlib/sources.py`. |
| Calgary, Edmonton, Winnipeg licences (Socrata) | Adapter included but off. Fill `sources.socrata_licences.portals` in `config.yaml` with `city, province, dataset_url (https://data.<city>.ca/resource/<id>.json), date_field, name_field, address_field, type_field`, then enable. |
| Facebook / Instagram | Not automated: pages are mostly behind login and Meta's terms restrict scraping. Use the public Meta Ad Library by hand to spot active local advertisers. |
| G Maps Extractor (your free API token) | Documented as an MCP server (`https://cloud.gmapsextractor.com/api/mcp`, Bearer token). Not wired into the script because its REST fields could not be verified. Use it through Claude Code in VS Code: `claude mcp add --transport http google-maps-scraper-mcp https://cloud.gmapsextractor.com/api/mcp --header "Authorization: Bearer $env:GMX_TOKEN"`, ask it to save results to a CSV with columns `title, category, address, website, phone, review_count, review_rating, emails, place_id`, then run `python run_daily.py --csv thatfile.csv`. Keep the token in an environment variable, never in a file you share. |
| More (Reddit questions, Kijiji, directories) | Add a function in `leadlib/sources.py` that returns queries plus signal metadata. |

Licence-based candidates are matched back to a Maps listing by name; if the Maps result is a different business it is dropped.

## Legal and safety notes (not legal advice)

- Canada's anti-spam law (CASL) restricts unsolicited commercial emails. Keep `email_source_url` as your record of where each address was published, include your identity and an unsubscribe in every message, and read the CRTC guidance before emailing.
- Scraping Google Maps may breach Google's terms. The scraper runs from your own connection at modest volume; keep it modest.
- Website visits respect `robots.txt` and use short timeouts.
- Never paste secrets into chat or commit them. `LEADS_TOKEN` lives only in your environment.

## Files

`run_daily.py` entry point | `config.yaml` your settings | `chains.txt` excluded chains | `leadlib/` the stages | `apps_script/Code.gs` Sheet receiver | `tests/` offline tests | `state/` remembers seen businesses so nothing repeats | `out/<date>/` CSVs, run log, raw scraper files
