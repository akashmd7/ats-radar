# Job Radar

Checks company career sites for matching roles, publishes them to a web page,
and emails me when something new turns up. Runs itself daily on GitHub Actions.

Companies don't build their own career pages - they rent an ATS, and those have
public JSON APIs. So this calls APIs instead of scraping HTML. Python standard
library only, nothing to install.

**Works with:** Workday, Greenhouse, Lever, SmartRecruiters, Ashby,
SuccessFactors, Oracle Cloud, amazon.jobs

**Doesn't work with:** iCIMS, Taleo. No public JSON, would need fragile HTML
scraping. Use a LinkedIn alert for those companies instead.

## Adding a company

Open the careers page, let it redirect, paste the URL from the address bar:

```json
{ "name": "Caterpillar", "url": "https://cat.wd5.myworkdayjobs.com/en-US/CaterpillarCareers" }
```

Everything else is read from the URL. To park a company without deleting it:

```json
{ "name": "Atlassian", "url": "...", "enabled": false, "note": "iCIMS - LinkedIn alert" }
```

Custom domain that doesn't redirect anywhere recognisable:

```bash
python job_monitor.py --identify https://jobs.example.com
```

If that finds nothing, open the page in a browser, DevTools, Network tab,
filter to Fetch/XHR, run a job search, and see which host the data comes from.
Not worth more than a minute per company.

## Commands

```bash
python job_monitor.py --check-config     # validate URLs, no network
python job_monitor.py --dry-run          # fetch and filter, write nothing
python job_monitor.py --only Caterpillar --debug
python job_monitor.py                    # full run
python job_monitor.py --mark-read        # clear email queue without sending
python job_monitor.py --rebuild-html     # regenerate page from database
```

Run `--check-config` after every config edit.

## Filters

Under `filters` in config.json. All matching is lowercase substring.

- `title_keywords` - keep if the title contains any of these
- `exclude_keywords` - drop if the title contains any of these, checked first
- `location_keywords` - keep if the location contains any of these
- `max_posted_days` - ignore older postings, also the freshness window on the page

Keep title keywords broad and let excludes do the trimming. A noisy page is easy
to fix; a missed role is invisible.

Skills like `sql` or `aws` don't belong in title keywords - they appear in
descriptions, not titles, so they only add noise.

**`docs/misses.md`** lists postings that were in my cities but rejected by the
title filter. Every company files engineering roles under its own job family
(Caterpillar calls them "IT Analyst Applications"), so this is how I find the
naming I'd never have guessed. Skim it after a run, add anything relevant.

## Speed

`runtime.max_workers` (default 6) polls that many companies at once. Twelve
companies goes from about twelve minutes to two. Each worker still waits
`request_delay` between its own requests, so no single site gets hit harder.

`search_terms` are sent to Workday's search box before local filtering. Every
term costs a full paginated sweep, so fewer is faster. Two broad ones beat five
overlapping ones.

## Automation

Push the repo with `jobs.db` and `docs/` committed - the database is the memory,
without it every run treats everything as new.

- **Pages:** Settings, Pages, branch `main`, folder `/docs`
- **Email:** Settings, Secrets and variables, Actions. Add `SMTP_HOST`,
  `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `EMAIL_TO` as secrets and `SITE_URL` as
  a variable. Gmail needs 2FA on and an app password, not the login password.
  Skip all of this if reading the page is enough.
  The monitor uses the runner's normal IPv4/IPv6 network stack. Set the optional
  `SMTP_IPV4_ONLY=true` secret only if your mail host cannot be reached over IPv6.
- **Run it:** Actions tab, Job Radar, Run workflow. Then leave the cron to it.

Email only goes out when something new appears. If a send fails, those roles stay
queued for the next run rather than being lost. The page is cumulative, so
missing an email doesn't lose anything.

## Notes

- First live run finds every currently open role at once. Expect a long first
  email, then zero to three a day.
- Reposts of the same requisition under different IDs collapse into one. Set
  `runtime.dedupe_reposts` to false to see each.
- Only sees what a company publishes to its own ATS. This is a watcher for
  chosen companies, not a full job search.
- If a company silently returns nothing for several days, it changed its ATS.
  `--check-config` won't catch that - skim the run log weekly.
