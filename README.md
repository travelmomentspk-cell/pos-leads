# Canada leads for POS, finance, website, social media and CRM services

Roman Urdu mein khulasa: GET_LEADS.bat par double-click karein, laptop on ho, aur ye free scraper se businesses dhoondta hai,
unki website check karta hai, aur sirf unhein Qualified_Leads mein daalta hai jin mein aap ki services ki zaroorat ki nishani mile
(nayi licence, website purani ya nahi hai, social media nahi, payment ki shikayat wagera). Quantity nahi, quality: agar sirf 30 achhi
leads milen to sirf 30 aayen gi.

## The "button"
Double-click `GET_LEADS.bat`. It starts Docker if needed, runs the pipeline, writes the leads to the Google Sheet and opens it.
Right-click it, Send to, Desktop (create shortcut) to keep a button on your desktop. Nothing runs by itself: the laptop can stay off.

## What it can and cannot know
Nobody can see who searched for what on Google or Meta; that data is private. This tool uses public signs that a business needs your services:

| Sign | Where it comes from | Service it points to |
|---|---|---|
| New business licence in the last 60 days | City open data (Vancouver now; more cities can be added) | POS, finance setup, website |
| Customer review mentions card machine, debit or cash-only trouble | Google reviews | POS and payments |
| Website says cash only | Business website | POS, finance |
| Active business with no website | Google Maps listing | Website, social media |
| Website looks dated: not mobile-friendly, no https, old copyright year, basic template | Business website | Website |
| No Facebook or Instagram link on the website | Business website | Social media |
| Uses the Meta Pixel (likely runs Facebook/Instagram ads) | Business website | Social media, marketing |
| No analytics or ad tracking | Business website | Marketing |
| No email sign-up, contact form or booking tool | Business website | CRM and follow-up |
| Relies on delivery apps (commission fees) | Business website | POS, finance |

"Not found" signs come from the homepage text; a site that loads everything with JavaScript can hide a pixel or a social link, so treat them as hints and check a lead before pitching.

## Quality bar (config.yaml, section `quality`)
- Tier A = strong sign of need (new licence, payment complaint, cash only, active business with no website).
- Tier B = at least `min_need_signals` (2) separate website or marketing gaps.
- Tier C = right kind of business but no sign of need. Goes to `Review_Queue`, never to `Qualified_Leads`.
- Businesses with more than `max_review_count` (1500) Google reviews are skipped as too big or chain-like.
- Chains in `chains.txt` are skipped. Head-office emails (pr@, marketing@, careers@) are ignored; call instead.
- Required for a qualified lead: business name, valid Canadian phone, address, a service need. Email is a bonus.
Every lead has `service_needs`, `evidence`, `source_url` and `email_source_url` so you can verify it in a minute.

## Files and tabs
`Qualified_Leads` (ready to call), `Review_Queue` (borderline or no clear need), `Rejected_Log` (with reasons), `Run_Log` (numbers per run).
Leads that qualify but do not fit under the daily cap wait in `state/backlog.json` and come first next time (up to 14 days).

## First run after this update
Your earlier scrape is in `out\2026-10-06`. Re-judge it with the strict rules, no scraping needed:
`py run_daily.py --reuse-dir out\2026-10-06`
(Leads you already pitched are skipped automatically.) After that, just use `GET_LEADS.bat`.

## Setup (done once)
Python, `py -m pip install -r requirements.txt`, Docker Desktop with the engine running, and the Make webhook URL in `config.yaml`
(`sheets.make_webhook_url`). Self-test: `py -m unittest discover -s tests` (13 tests should pass). Test the sheet link: `py run_daily.py --test-push`.

## Adding places to look
Edit `targets.categories` and `targets.cities` in `config.yaml`. The tool rotates through them, `queries_per_run` at a time.
Facebook and Instagram pages are not scraped (login walls and Meta's terms). The public Meta Ad Library can be browsed by hand to spot active local advertisers.

## Legal notes (not legal advice)
Canada's anti-spam law (CASL) restricts unsolicited commercial emails: keep `email_source_url` as proof of where an address was published, identify yourself and
include an unsubscribe. Calling businesses is generally less restricted, but follow CRTC rules and keep your own do-not-call list. Scraping Google Maps may breach Google's terms; keep volumes modest.
Website visits respect robots.txt. Never share your Make webhook URL.

## What changed in v4
- **Duplicate Master list** (`leadlib/master.py`, file `state/master.db`): every lead that reached the Sheet is remembered. New leads are matched on phone, website, place id and business name (same city), before anything is written to the Sheet. `py run_daily.py --seed-master file.csv` adds old leads from any CSV.
- **Lead quality gate** (`leadlib/quality.py`, section `lead_quality` in config.yaml): 0-100 score, needs a named decision maker (owner, CEO, founder, director, manager), minimum score 70. Rejected leads are only saved in `out/<date>/quality_rejected.csv`. Set `lead_quality.enabled: false` to go back to the old rules.
- **Extra sources** (`run_extra.py`, `config_extra.yaml`, `leadlib/extra.py`): 15 more business types and the provider-complaint source. Run `py run_extra.py`.
- Sheet columns added to `Qualified_Leads`: service_needs, decision_maker, decision_maker_title, reason.

## What changed in v5 (laptop-free, Sheet control panel)
- **`run_cloud.py` + `config_cloud.yaml`**: one config with 23 categories, cities, thresholds and sources. Every lead needs a decision-maker name (owner, founder, CEO, president, director or manager), a valid phone and a usable email, and a score of at least 70. Anything else goes to the Rejected tab with the reason. The Master list (`state/cloud/master.db`) is kept in the repository, so nothing is ever delivered twice.
- **`.github/workflows/leads.yml`**: runs the scraper on GitHub Actions (no laptop). Start it from the Sheet, from the Actions tab, or on a schedule.
- **`apps_script/ControlPanel.gs`**: the Run button for the Sheet's Control tab.
- Decision makers come from the business's own About/Team pages, schema.org data, and (optional) public LinkedIn snippets through Google's official Programmable Search API.
- Cloud runs now search in 10-query batches, upload each batch's qualified leads to the Sheet, and continue until 50 new leads are delivered. Each completed batch checkpoints the duplicate and search-rotation state to GitHub; a repeat run skips leads already delivered. Runs stop after 270 minutes or when the selected searches are exhausted, and the Sheet status reports the final count.
