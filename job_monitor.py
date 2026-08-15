#!/usr/bin/env python3
"""
Job Radar - polls company ATS APIs directly and publishes matching roles.

Add a company by pasting its careers page URL into config.json. The ATS and its
parameters are worked out from the URL:

    https://cat.wd5.myworkdayjobs.com/en-US/CaterpillarCareers
    https://boards.greenhouse.io/postman
    https://jobs.lever.co/zepto
    https://jobs.smartrecruiters.com/Visa
    https://jobs.ashbyhq.com/ramp

Usage:
    python job_monitor.py --check-config       # parse URLs, no network calls
    python job_monitor.py --only Caterpillar --debug
    python job_monitor.py --dry-run            # fetch and filter, write nothing
    python job_monitor.py                      # full run
    python job_monitor.py --rebuild-html       # regenerate page from database

Email is sent only when these environment variables are set:
    SMTP_HOST SMTP_PORT SMTP_USER SMTP_PASS EMAIL_TO [EMAIL_FROM] [SITE_URL]

Standard library only. No dependencies.
"""

import argparse
import html
import json
import os
import re
import smtplib
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

UA = "Mozilla/5.0 (compatible; job-radar/2.0)"
LOCALE_SEG = re.compile(r"^[a-z]{2}([-_][A-Za-z]{2})?$")
WD_SEG = re.compile(r"^wd\d+$")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULTS = {
    "site": {
        "title": "Job Radar",
        "subtitle": "",
        "html_path": "docs/index.html",
        "misses_path": "docs/misses.md",
    },
    "filters": {
        "title_keywords": [],
        "exclude_keywords": [],
        "location_keywords": [],
        "max_posted_days": 30,
    },
    "search_terms": ["engineer"],
    "runtime": {
        "request_delay": 0.4,
        "timeout": 30,
        "max_offset": 200,
        "resolve_vague_locations": True,
        "dedupe_reposts": True,
        "drop_after_days": 45,
    },
    "companies": [],
}


def deep_merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path):
    p = Path(path)
    if not p.exists():
        sys.exit(f"Config not found: {p}. Put config.json next to this script.")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.exit(f"Config is not valid JSON: {e}")
    cfg = deep_merge(DEFAULTS, raw)
    if not cfg["companies"]:
        sys.exit("No companies in config. Add at least one name and url.")
    return cfg


# ---------------------------------------------------------------------------
# URL parsing - work out the ATS and its parameters from a careers page URL
# ---------------------------------------------------------------------------

class ParseError(Exception):
    pass


def parse_careers_url(url, ats_hint=None):
    """Return a source dict describing how to call this company's ATS."""
    if not url or "://" not in url:
        raise ParseError("URL must start with https://")

    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    query = urllib.parse.parse_qs(parsed.query)
    segs = [s for s in parsed.path.split("/") if s]
    real = [s for s in segs if not LOCALE_SEG.match(s)]

    ats = ats_hint
    if not ats:
        if "myworkdayjobs.com" in host or "myworkdaysite.com" in host:
            ats = "workday"
        elif "greenhouse.io" in host:
            ats = "greenhouse"
        elif "lever.co" in host:
            ats = "lever"
        elif "smartrecruiters.com" in host:
            ats = "smartrecruiters"
        elif "ashbyhq.com" in host:
            ats = "ashby"
        else:
            raise ParseError(
                "Unrecognised ATS for host '" + host + "'. Supported: "
                "myworkdayjobs.com, greenhouse.io, lever.co, smartrecruiters.com, "
                "ashbyhq.com. Set \"ats\" explicitly if you know it."
            )

    if ats == "workday":
        parts = host.split(".")
        tenant = parts[0]
        wd = next((x for x in parts if WD_SEG.match(x)), "wd1")
        if not real:
            raise ParseError("Workday URL needs the career site segment, "
                             "e.g. .../en-US/CaterpillarCareers")
        site = real[0]
        return {"ats": "workday", "host": host, "tenant": tenant, "wd": wd,
                "site": site, "label": tenant + "/" + site + " (" + wd + ")"}

    if ats == "greenhouse":
        board = query.get("for", [None])[0]
        if not board:
            board = next((s for s in real if s not in ("embed", "job_board")), None)
        if not board:
            raise ParseError("Could not find the Greenhouse board name in the URL")
        return {"ats": "greenhouse", "board": board, "label": board}

    if ats == "lever":
        if not real:
            raise ParseError("Could not find the Lever company name in the URL")
        return {"ats": "lever", "company": real[0], "label": real[0]}

    if ats == "smartrecruiters":
        if not real:
            raise ParseError("Could not find the SmartRecruiters company name")
        return {"ats": "smartrecruiters", "company": real[0], "label": real[0]}

    if ats == "ashby":
        if not real:
            raise ParseError("Could not find the Ashby organisation name")
        return {"ats": "ashby", "org": real[0], "label": real[0]}

    raise ParseError("Unsupported ats '" + str(ats) + "'")


def resolve_companies(cfg, only=None):
    """Attach parsed source info to each company. Returns (ok, failed)."""
    ok, failed = [], []
    wanted = {n.strip().lower() for n in only} if only else None

    for c in cfg["companies"]:
        name = c.get("name") or c.get("url", "?")
        if c.get("enabled") is False:
            continue
        if wanted and name.lower() not in wanted:
            continue
        try:
            src = parse_careers_url(c.get("url", ""), c.get("ats"))
        except ParseError as e:
            failed.append((name, str(e)))
            continue
        ok.append({**c, "name": name, "source": src})
    return ok, failed


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Http:
    def __init__(self, delay, timeout):
        self.delay = delay
        self.timeout = timeout
        self.calls = 0

    def json(self, url, payload=None):
        if not url:
            return None
        time.sleep(self.delay)
        self.calls += 1
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"User-Agent": UA, "Accept": "application/json"}
        if data:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except (urllib.error.HTTPError, urllib.error.URLError,
                json.JSONDecodeError, TimeoutError, ValueError) as e:
            print("     ! " + type(e).__name__ + ": " + url[:90])
            return None


# ---------------------------------------------------------------------------
# Age normalisation
# ---------------------------------------------------------------------------

def days_from_workday_text(text):
    if not text:
        return None
    t = text.lower()
    if "today" in t or "just posted" in t:
        return 0
    if "yesterday" in t:
        return 1
    m = re.search(r"(\d+)\+?\s*day", t)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\+?\s*week", t)
    if m:
        return int(m.group(1)) * 7
    m = re.search(r"(\d+)\+?\s*month", t)
    if m:
        return int(m.group(1)) * 30
    return None


def days_from_iso(value):
    if not value:
        return None
    try:
        s = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0, (datetime.now(timezone.utc) - dt).days)
    except (ValueError, TypeError):
        return None


def days_from_epoch_ms(value):
    try:
        dt = datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
        return max(0, (datetime.now(timezone.utc) - dt).days)
    except (ValueError, TypeError, OSError):
        return None


# ---------------------------------------------------------------------------
# Fetchers - each returns a list of job dicts
# ---------------------------------------------------------------------------

def fetch_workday(http, src, cfg, terms):
    base = "https://" + src["host"]
    cxs = base + "/wday/cxs/" + src["tenant"] + "/" + src["site"]
    api = cxs + "/jobs"
    max_offset = cfg["runtime"]["max_offset"]
    seen, out = set(), []

    for term in terms:
        offset, total = 0, None
        while True:
            body = {"appliedFacets": {}, "limit": 20,
                    "offset": offset, "searchText": term}
            data = http.json(api, body)
            if not data or not data.get("jobPostings"):
                break
            if total is None:
                total = data.get("total", 0)

            for j in data["jobPostings"]:
                path = j.get("externalPath", "")
                bullets = j.get("bulletFields") or [path]
                jid = bullets[0] if bullets else path
                if jid in seen:
                    continue
                seen.add(jid)
                posted = j.get("postedOn", "")
                out.append({
                    "id": jid,
                    "title": (j.get("title") or "").strip(),
                    "location": (j.get("locationsText") or "").strip(),
                    "url": base + "/en-US/" + src["site"] + path,
                    "posted": posted,
                    "age_days": days_from_workday_text(posted),
                    "_detail": cxs + path,
                })

            offset += 20
            if not total or offset >= total or offset >= max_offset:
                break
    return out


def resolve_workday_location(http, job):
    data = http.json(job.get("_detail", ""))
    if not data:
        return job["location"]
    info = data.get("jobPostingInfo") or {}
    parts = [info.get("location", "")]
    parts += [str(x) for x in (info.get("additionalLocations") or [])]
    return ", ".join(p for p in parts if p) or job["location"]


def fetch_greenhouse(http, src, cfg, terms):
    url = ("https://boards-api.greenhouse.io/v1/boards/"
           + src["board"] + "/jobs?content=false")
    data = http.json(url)
    if not data:
        return []
    out = []
    for j in data.get("jobs", []):
        updated = j.get("updated_at") or j.get("first_published")
        out.append({
            "id": str(j.get("id")),
            "title": (j.get("title") or "").strip(),
            "location": ((j.get("location") or {}).get("name") or "").strip(),
            "url": j.get("absolute_url", ""),
            "posted": str(updated or ""),
            "age_days": days_from_iso(updated),
        })
    return out


def fetch_lever(http, src, cfg, terms):
    url = "https://api.lever.co/v0/postings/" + src["company"] + "?mode=json"
    data = http.json(url)
    if not isinstance(data, list):
        return []
    out = []
    for j in data:
        created = j.get("createdAt")
        out.append({
            "id": str(j.get("id", "")),
            "title": (j.get("text") or "").strip(),
            "location": ((j.get("categories") or {}).get("location") or "").strip(),
            "url": j.get("hostedUrl", ""),
            "posted": str(created or ""),
            "age_days": days_from_epoch_ms(created),
        })
    return out


def fetch_smartrecruiters(http, src, cfg, terms):
    out, offset = [], 0
    max_offset = max(cfg["runtime"]["max_offset"], 100)
    while offset < max_offset:
        url = ("https://api.smartrecruiters.com/v1/companies/" + src["company"]
               + "/postings?limit=100&offset=" + str(offset))
        data = http.json(url)
        if not data or not data.get("content"):
            break
        for j in data["content"]:
            loc = j.get("location") or {}
            bits = [loc.get("city") or "", loc.get("region") or "",
                    loc.get("country") or ""]
            released = j.get("releasedDate")
            out.append({
                "id": str(j.get("id", "")),
                "title": (j.get("name") or "").strip(),
                "location": ", ".join(b for b in bits if b),
                "url": ("https://jobs.smartrecruiters.com/" + src["company"]
                        + "/" + str(j.get("id"))),
                "posted": str(released or ""),
                "age_days": days_from_iso(released),
            })
        offset += 100
        if offset >= data.get("totalFound", 0):
            break
    return out


def fetch_ashby(http, src, cfg, terms):
    url = ("https://api.ashbyhq.com/posting-api/job-board/" + src["org"]
           + "?includeCompensation=true")
    data = http.json(url)
    if not data:
        return []
    out = []
    for j in data.get("jobs", []):
        published = j.get("publishedAt")
        out.append({
            "id": str(j.get("id", "")),
            "title": (j.get("title") or "").strip(),
            "location": (j.get("location") or "").strip(),
            "url": j.get("jobUrl", ""),
            "posted": str(published or ""),
            "age_days": days_from_iso(published),
        })
    return out


FETCHERS = {
    "workday": fetch_workday,
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "smartrecruiters": fetch_smartrecruiters,
    "ashby": fetch_ashby,
}


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

VAGUE_LOCATION = ("location", "multiple", "various", "flexible", "remote")


def title_ok(job, filters):
    t = job["title"].lower()
    if not t:
        return False
    if any(x in t for x in filters["exclude_keywords"]):
        return False
    kws = filters["title_keywords"]
    return (not kws) or any(k in t for k in kws)


def age_ok(job, filters):
    limit = filters.get("max_posted_days") or 0
    if not limit:
        return True
    return job.get("age_days") is None or job["age_days"] <= limit


def in_target_city(job, filters):
    kws = filters["location_keywords"]
    if not kws:
        return True
    return any(k in (job.get("location") or "").lower() for k in kws)


def location_ok(job, filters, ats, http, cfg):
    kws = filters["location_keywords"]
    if not kws:
        return True
    loc = (job.get("location") or "").lower()
    if any(k in loc for k in kws):
        return True

    vague = (not loc.strip()) or any(v in loc for v in VAGUE_LOCATION)
    if not vague:
        return False

    if ats == "workday" and cfg["runtime"]["resolve_vague_locations"]:
        real = resolve_workday_location(http, job)
        job["location"] = real
        return any(k in real.lower() for k in kws)

    return True  # cannot resolve, keep rather than lose it


def repost_key(company, job, dedupe):
    if not dedupe:
        return company + "::" + str(job["id"])
    title = re.sub(r"[^a-z0-9]+", " ", job["title"].lower()).strip()
    loc = re.sub(r"[^a-z0-9]+", " ", (job.get("location") or "").lower()).strip()
    return company + "::" + title + "::" + loc


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    key        TEXT PRIMARY KEY,
    company    TEXT,
    ats        TEXT,
    title      TEXT,
    location   TEXT,
    url        TEXT,
    posted     TEXT,
    age_days   INTEGER,
    first_seen TEXT,
    last_seen  TEXT,
    active     INTEGER DEFAULT 1,
    notified   INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS runs (
    ts TEXT, companies INTEGER, fetched INTEGER, matched INTEGER, new INTEGER
);
CREATE INDEX IF NOT EXISTS idx_active ON jobs(active, first_seen);
"""


def open_db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def upsert(conn, key, company, ats, job, now):
    row = conn.execute("SELECT key FROM jobs WHERE key=?", (key,)).fetchone()
    if row:
        conn.execute(
            "UPDATE jobs SET last_seen=?, active=1, location=?, age_days=?, "
            "posted=? WHERE key=?",
            (now, job["location"], job.get("age_days"), job["posted"], key))
        return False
    conn.execute(
        "INSERT INTO jobs (key, company, ats, title, location, url, posted, "
        "age_days, first_seen, last_seen, active, notified) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,1,0)",
        (key, company, ats, job["title"], job["location"], job["url"],
         job["posted"], job.get("age_days"), now, now))
    return True


def close_out(conn, companies_done, now):
    """Deactivate jobs from successfully polled companies that no longer appear."""
    if not companies_done:
        return 0
    marks = ",".join("?" * len(companies_done))
    cur = conn.execute(
        "UPDATE jobs SET active=0 WHERE active=1 AND last_seen<? "
        "AND company IN (" + marks + ")",
        (now, *companies_done))
    return cur.rowcount


def prune(conn, drop_after_days):
    if not drop_after_days:
        return 0
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=drop_after_days)).isoformat()
    cur = conn.execute("DELETE FROM jobs WHERE active=0 AND last_seen<?", (cutoff,))
    return cur.rowcount


# ---------------------------------------------------------------------------
# HTML page
# ---------------------------------------------------------------------------

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>__TITLE__</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Archivo:wght@600;700&family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root{
    --paper:#E7EBEE; --card:#FDFDFC; --ink:#111A22; --ink-soft:#4A5A69;
    --line:#C9D2D9; --signal:#1F4FD8; --warm:#A8480B; --quiet:#7A8894;
  }
  *{box-sizing:border-box}
  body{
    margin:0; background:var(--paper); color:var(--ink);
    font-family:"IBM Plex Sans",system-ui,sans-serif; font-size:15px;
    line-height:1.5; -webkit-font-smoothing:antialiased;
  }
  .wrap{max-width:940px; margin:0 auto; padding:32px 20px 72px}
  header{border-bottom:2px solid var(--ink); padding-bottom:18px}
  h1{
    font-family:Archivo,sans-serif; font-weight:700;
    font-size:clamp(30px,6vw,46px); letter-spacing:-.025em; margin:0; line-height:1;
  }
  .sub{color:var(--ink-soft); margin:8px 0 0; max-width:46ch}

  .meters{display:flex; flex-wrap:wrap; border-bottom:1px solid var(--line); margin-bottom:24px}
  .meter{padding:14px 22px 14px 0; margin-right:22px; border-right:1px solid var(--line)}
  .meter:last-child{border-right:none; margin-right:0}
  .meter b{
    display:block; font-family:"IBM Plex Mono",monospace; font-size:26px;
    font-weight:500; letter-spacing:-.02em;
  }
  .meter span{
    font-family:"IBM Plex Mono",monospace; font-size:10px; letter-spacing:.14em;
    text-transform:uppercase; color:var(--quiet);
  }

  .controls{display:flex; flex-wrap:wrap; gap:10px; margin-bottom:22px}
  input,select{
    font-family:inherit; font-size:14px; padding:9px 12px; color:var(--ink);
    background:var(--card); border:1px solid var(--line); border-radius:2px;
  }
  input{flex:1 1 220px}
  input:focus,select:focus{outline:2px solid var(--signal); outline-offset:1px; border-color:var(--signal)}

  .band{
    font-family:"IBM Plex Mono",monospace; font-size:11px; letter-spacing:.16em;
    text-transform:uppercase; color:var(--quiet); margin:28px 0 10px;
    display:flex; align-items:center; gap:12px;
  }
  .band::after{content:""; flex:1; height:1px; background:var(--line)}

  .job{
    display:block; text-decoration:none; color:inherit; background:var(--card);
    border:1px solid var(--line); border-left:3px solid var(--line);
    border-radius:2px; padding:14px 16px; margin-bottom:7px;
    transition:border-left-color .12s ease, transform .12s ease;
  }
  .job:hover,.job:focus-visible{border-left-color:var(--signal); transform:translateX(2px)}
  .job:focus-visible{outline:2px solid var(--signal); outline-offset:2px}
  .job.fresh{border-left-color:var(--warm)}

  .row{display:flex; gap:14px; align-items:baseline}
  .t{font-weight:500; flex:1; letter-spacing:-.005em}
  .age{font-family:"IBM Plex Mono",monospace; font-size:12px; color:var(--quiet); white-space:nowrap}
  .job.fresh .age{color:var(--warm)}
  .m{
    font-family:"IBM Plex Mono",monospace; font-size:12px; color:var(--ink-soft);
    margin-top:5px; display:flex; gap:8px; flex-wrap:wrap;
  }
  .m .co{color:var(--ink); font-weight:500}
  .m .sep{color:var(--line)}

  .decay{height:2px; background:var(--line); margin-top:10px; position:relative}
  .decay i{position:absolute; top:0; bottom:0; left:0; background:var(--signal); display:block}
  .job.fresh .decay i{background:var(--warm)}

  .empty{padding:44px 0; color:var(--ink-soft); font-family:"IBM Plex Mono",monospace; font-size:13px}
  footer{
    margin-top:52px; padding-top:16px; border-top:1px solid var(--line);
    font-family:"IBM Plex Mono",monospace; font-size:11px; color:var(--quiet);
    display:flex; justify-content:space-between; flex-wrap:wrap; gap:8px;
  }
  @media (prefers-reduced-motion:reduce){*{transition:none !important}}
  @media (max-width:560px){
    .meter{padding-right:14px; margin-right:14px}
    .row{flex-direction:column; gap:4px}
  }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>__TITLE__</h1>
    <p class="sub">__SUBTITLE__</p>
  </header>

  <div class="meters">
    <div class="meter"><b id="mTotal">0</b><span>open roles</span></div>
    <div class="meter"><b id="mFresh">0</b><span>this week</span></div>
    <div class="meter"><b>__COMPANIES__</b><span>companies watched</span></div>
    <div class="meter"><b>__WINDOW__</b><span>day window</span></div>
  </div>

  <div class="controls">
    <input id="q" type="search" placeholder="Filter by title or location" aria-label="Filter roles">
    <select id="co" aria-label="Filter by company"><option value="">All companies</option></select>
    <select id="sort" aria-label="Sort roles">
      <option value="age">Newest posted</option>
      <option value="found">Recently found</option>
      <option value="company">Company</option>
    </select>
  </div>

  <div id="list"></div>

  <footer>
    <span>Last checked __GENERATED__</span>
    <span>Polled directly from company ATS</span>
  </footer>
</div>

<script>
const JOBS = __DATA__;
const WINDOW = __WINDOW__;
const list = document.getElementById('list');
const q = document.getElementById('q');
const co = document.getElementById('co');
const sort = document.getElementById('sort');

[...new Set(JOBS.map(j => j.company))].sort().forEach(c => {
  const o = document.createElement('option');
  o.value = c; o.textContent = c; co.appendChild(o);
});

function ageLabel(d){
  if (d === null || d === undefined) return 'date unknown';
  if (d === 0) return 'today';
  if (d === 1) return 'yesterday';
  return d + 'd ago';
}
function band(d){
  if (d === null || d === undefined) return 'Undated';
  if (d <= 1) return 'Today';
  if (d <= 7) return 'This week';
  if (d <= 21) return 'Earlier this month';
  return 'Older';
}

function render(){
  const term = q.value.trim().toLowerCase();
  const company = co.value;
  let rows = JOBS.filter(j =>
    (!company || j.company === company) &&
    (!term || (j.title + ' ' + j.location).toLowerCase().includes(term))
  );

  if (sort.value === 'company')
    rows.sort((a,b) => a.company.localeCompare(b.company) || (a.age ?? 999) - (b.age ?? 999));
  else if (sort.value === 'found')
    rows.sort((a,b) => b.first_seen.localeCompare(a.first_seen));
  else
    rows.sort((a,b) => (a.age ?? 999) - (b.age ?? 999));

  document.getElementById('mTotal').textContent = rows.length;
  document.getElementById('mFresh').textContent =
    rows.filter(j => j.age !== null && j.age <= 7).length;

  if (!rows.length){
    list.innerHTML = '<p class="empty">No roles match this filter. Clear the search to see everything currently open.</p>';
    return;
  }

  let out = '', lastBand = null;
  const grouped = sort.value === 'age';
  for (const j of rows){
    if (grouped){
      const b = band(j.age);
      if (b !== lastBand){ out += '<div class="band">' + b + '</div>'; lastBand = b; }
    }
    const fresh = j.age !== null && j.age <= 7;
    const left = j.age === null ? 0 : Math.max(0, Math.min(100, 100 - (j.age / WINDOW) * 100));
    out += '<a class="job' + (fresh ? ' fresh' : '') + '" href="' + j.url + '" target="_blank" rel="noopener">'
      + '<div class="row"><span class="t">' + j.title + '</span>'
      + '<span class="age">' + ageLabel(j.age) + '</span></div>'
      + '<div class="m"><span class="co">' + j.company + '</span>'
      + '<span class="sep">/</span><span>' + j.location + '</span></div>'
      + '<div class="decay"><i style="width:' + left.toFixed(0) + '%"></i></div></a>';
  }
  list.innerHTML = out;
}

[q, co, sort].forEach(el => el.addEventListener('input', render));
render();
</script>
</body>
</html>
"""


def write_html(conn, cfg, generated_at):
    rows = conn.execute(
        "SELECT company, title, location, url, age_days, first_seen "
        "FROM jobs WHERE active=1").fetchall()

    data = [{
        "company": html.escape(r["company"] or ""),
        "title": html.escape(r["title"] or ""),
        "location": html.escape(r["location"] or "Location not stated"),
        "url": r["url"] or "",
        "age": r["age_days"],
        "first_seen": r["first_seen"] or "",
    } for r in rows]

    site = cfg["site"]
    window = cfg["filters"].get("max_posted_days") or 30
    page = (PAGE
            .replace("__TITLE__", html.escape(site["title"]))
            .replace("__SUBTITLE__", html.escape(site.get("subtitle", "")))
            .replace("__COMPANIES__", str(len(cfg["companies"])))
            .replace("__WINDOW__", str(window))
            .replace("__GENERATED__", generated_at.strftime("%d %b %Y, %H:%M UTC"))
            .replace("__DATA__", json.dumps(data)))

    path = Path(site["html_path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(page, encoding="utf-8")
    return path, len(data)


def write_misses(cfg, misses):
    if not misses:
        return None
    path = Path(cfg["site"]["misses_path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    seen, lines = set(), []
    for j in misses:
        k = j["company"] + "::" + j["title"].lower()
        if k in seen:
            continue
        seen.add(k)
        lines.append("- **" + j["company"] + "** - [" + j["title"] + "]("
                     + j["url"] + ")  \n  " + (j["location"] or "") + "\n")
    body = ("# Rejected by the title filter, but in your cities\n\n"
            "Each company files engineering roles under its own job family. "
            "If a title below looks relevant, add its distinctive phrase to "
            "`filters.title_keywords` in config.json.\n\n" + "\n".join(lines))
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def send_email(new_jobs, cfg):
    host = os.environ.get("SMTP_HOST")
    to = os.environ.get("EMAIL_TO")
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    if not all([host, to, user, password]):
        print("Email skipped (SMTP_HOST, SMTP_USER, SMTP_PASS or EMAIL_TO not set)")
        return

    port = int(os.environ.get("SMTP_PORT", "465"))
    sender = os.environ.get("EMAIL_FROM", user)
    site_url = os.environ.get("SITE_URL", "")

    n = len(new_jobs)
    plural = "s" if n != 1 else ""
    msg = EmailMessage()
    msg["Subject"] = str(n) + " new role" + plural + " - " + cfg["site"]["title"]
    msg["From"] = sender
    msg["To"] = to

    plain = [str(n) + " new matching role" + plural + ".", ""]
    for j in new_jobs:
        age = "" if j.get("age_days") is None else " - " + str(j["age_days"]) + "d ago"
        plain.append(j["title"] + "\n  " + j["company"] + " / "
                     + j["location"] + age + "\n  " + j["url"] + "\n")
    if site_url:
        plain.append("All open roles: " + site_url)
    msg.set_content("\n".join(plain))

    items = []
    for j in new_jobs:
        age = "" if j.get("age_days") is None else " &middot; " + str(j["age_days"]) + "d ago"
        items.append(
            '<li style="margin:0 0 16px">'
            '<a href="' + html.escape(j["url"]) + '" style="color:#1F4FD8;'
            'text-decoration:none;font-weight:600">' + html.escape(j["title"]) + '</a><br>'
            '<span style="color:#4A5A69;font-size:13px">'
            + html.escape(j["company"]) + ' &middot; ' + html.escape(j["location"])
            + age + '</span></li>')
    footer = ('<p style="font-size:13px"><a href="' + html.escape(site_url)
              + '" style="color:#1F4FD8">See all open roles</a></p>') if site_url else ""
    msg.add_alternative(
        '<div style="font-family:system-ui,sans-serif;max-width:600px">'
        '<h2 style="font-size:18px;margin:0 0 18px">' + str(n) + ' new role'
        + plural + '</h2><ul style="list-style:none;padding:0;margin:0">'
        + "".join(items) + '</ul>' + footer + '</div>', subtype="html")

    try:
        if port == 587:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.starttls()
                s.login(user, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP_SSL(host, port, timeout=30) as s:
                s.login(user, password)
                s.send_message(msg)
        print("Emailed " + str(n) + " new role(s) to " + to)
    except Exception as e:
        print("! email failed: " + type(e).__name__ + ": " + str(e))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_check_config(cfg, args):
    ok, failed = resolve_companies(cfg, args.only)
    label = "entry" if len(ok) == 1 else "entries"
    print(str(len(ok)) + " company " + label + " parsed\n")
    width = max((len(c["name"]) for c in ok), default=4)
    for c in ok:
        s = c["source"]
        print("  " + c["name"].ljust(width) + "  " + s["ats"].ljust(16) + s["label"])
    if failed:
        print("\n" + str(len(failed)) + " could not be parsed:")
        for name, err in failed:
            print("  " + name + ": " + err)
        return 1
    return 0


def run(cfg, args):
    filters = cfg["filters"]
    rt = cfg["runtime"]
    http = Http(rt["request_delay"], rt["timeout"])
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()

    companies, failed = resolve_companies(cfg, args.only)
    for name, err in failed:
        print("! " + name + ": " + err)
    if not companies:
        print("Nothing to poll.")
        return 1

    conn = open_db(args.db)
    fresh, misses, done = [], [], []
    total_fetched = total_matched = 0

    for c in companies:
        src = c["source"]
        fetcher = FETCHERS[src["ats"]]
        terms = c.get("search_terms") or cfg["search_terms"]
        print("-> " + c["name"] + " [" + src["ats"] + "] " + src["label"])

        try:
            jobs = fetcher(http, src, cfg, terms)
        except Exception as e:
            print("   ! fetch failed, keeping previous results: "
                  + type(e).__name__ + ": " + str(e))
            continue

        if not jobs:
            print("   0 postings returned - check the URL points at the right "
                  "career site")
            continue

        if args.debug:
            for j in jobs:
                print("   [debug] " + j["title"] + " || " + j["location"]
                      + " || " + j["posted"])

        by_title = [j for j in jobs if title_ok(j, filters)]
        by_age = [j for j in by_title if age_ok(j, filters)]
        hits = [j for j in by_age if location_ok(j, filters, src["ats"], http, cfg)]

        for j in jobs:
            if in_target_city(j, filters) and not title_ok(j, filters):
                misses.append({**j, "company": c["name"]})

        print("   " + str(len(jobs)) + " fetched -> " + str(len(by_title))
              + " title -> " + str(len(by_age)) + " age -> " + str(len(hits))
              + " location")

        total_fetched += len(jobs)
        total_matched += len(hits)
        done.append(c["name"])

        if args.dry_run:
            continue

        for j in hits:
            key = repost_key(c["name"], j, rt["dedupe_reposts"])
            if upsert(conn, key, c["name"], src["ats"], j, now):
                fresh.append({**j, "company": c["name"]})

    if args.dry_run:
        print("\nDry run: " + str(total_fetched) + " fetched, "
              + str(total_matched) + " matched, " + str(http.calls)
              + " requests. Nothing written.")
        conn.close()
        return 0

    closed = close_out(conn, done, now)
    pruned = prune(conn, rt["drop_after_days"])
    conn.execute("INSERT INTO runs VALUES (?,?,?,?,?)",
                 (now, len(done), total_fetched, total_matched, len(fresh)))
    conn.commit()

    path, live = write_html(conn, cfg, now_dt)
    mpath = write_misses(cfg, misses)

    print("\n" + str(len(fresh)) + " new | " + str(live) + " open | "
          + str(closed) + " closed | " + str(pruned) + " pruned | "
          + str(http.calls) + " requests")
    print("Wrote " + str(path))
    if mpath:
        print("Wrote " + str(mpath) + " - review to tune title_keywords")

    for j in fresh:
        print("  + " + j["company"] + " - " + j["title"] + " (" + j["location"] + ")")

    if fresh and not args.no_email:
        send_email(fresh, cfg)
        conn.execute("UPDATE jobs SET notified=1 WHERE notified=0")
        conn.commit()

    conn.close()
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="Poll company ATS APIs for matching roles.")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--db", default="jobs.db")
    ap.add_argument("--only", nargs="+", metavar="NAME",
                    help="poll only these companies")
    ap.add_argument("--debug", action="store_true",
                    help="print every raw posting before filtering")
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch and filter but write nothing")
    ap.add_argument("--no-email", action="store_true")
    ap.add_argument("--check-config", action="store_true",
                    help="parse careers URLs and exit, no network calls")
    ap.add_argument("--rebuild-html", action="store_true",
                    help="regenerate the page from the database without polling")
    args = ap.parse_args()

    cfg = load_config(args.config)

    if args.check_config:
        return cmd_check_config(cfg, args)

    if args.rebuild_html:
        conn = open_db(args.db)
        path, live = write_html(conn, cfg, datetime.now(timezone.utc))
        conn.close()
        print("Wrote " + str(path) + " (" + str(live) + " open roles)")
        return 0

    return run(cfg, args)


if __name__ == "__main__":
    sys.exit(main())