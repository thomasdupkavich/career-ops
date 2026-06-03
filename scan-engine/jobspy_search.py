#!/usr/bin/env python3
"""Self-contained job search for career-ops using JobSpy + public feeds.

Ported from the Hermes daily cron. Scrapes Indeed/LinkedIn/Google/Remotive/etc.,
scores each posting (keyword/salary/location/recency/competition), and emits a
single JSON object to STDOUT:

    {"postings": [{url, company, title, location, portal, score, ...}], "stats": {...}}

All scraper diagnostics go to STDERR so stdout stays pure JSON for the Node
adapter (jobspy-adapter.mjs), which dedups + writes via scan-lib.appendPostings.
"""
from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import html
import json
import logging
import math
import multiprocessing as mp
import os
import queue
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from jobspy import scrape_jobs

# Self-contained: no Hermes paths. Apify token (optional) is read from the
# project's .env (career-ops root) or the live environment — see env_value().
PROJECT_ROOT = Path(__file__).resolve().parent.parent
APIFY_ENV_FILES = [PROJECT_ROOT / '.env']
APIFY_BUILTIN_ACTOR = 'logiover~built-in-tech-jobs-scraper'
APIFY_WELLFOUND_ACTOR = 'blackfalcondata~wellfound-scraper'
APIFY_TIMEOUT_SECS = 150
JOBSPY_TOTAL_TIMEOUT_SECS = 150

CORE = ['c#', '.net', '.net core', 'asp.net', 'asp.net mvc', 'asp.net web api', 'entity framework', 'sql server', 't-sql', 'stored procedures', 'rest api', 'restful']
DB = ['query optimization', 'views', 'indexes', 'ctes', 'window functions', 'complex joins', 'bulk operations', 'transaction management', 'data migration', 'ssms']
FE = ['javascript', 'jquery', 'ajax', 'typescript', 'react', 'html5', 'css3', 'razor', 'kendo ui', 'telerik', 'bootstrap', 'responsive design', 'websockets']
TOOLS = ['azure devops', 'azure pipelines', 'ado', 'git', 'ci/cd', 'visual studio', 'vs code', 'jira', 'postman', 'epplus']
PRACTICES = ['solid', 'oop', 'design patterns', 'repository pattern', 'dependency injection', 'agile', 'scrum', 'unit testing', 'code reviews']
SPECIAL = ['hardware-software integration', 'hardware software integration', 'iot', 'real-time', 'session-based workflows']
ALL_KEYWORDS = CORE + DB + FE + TOOLS + PRACTICES + SPECIAL
NEGATIVE = ['security clearance', 'secret clearance', 'top secret', 'ts/sci', 'internship', 'intern ', 'new grad', 'unpaid']
EXPERIENCE_NEGATIVE_PATTERNS = [
    r'\bintern(ship)?\b',
    r'\bnew\s+grad(uate)?\b',
    r'\bentry[-\s]?level\b',
    r'\bunpaid\b',
]
STAFFING_WORDS = ['recruiter', 'staffing', 'talentify', 'jobot', 'cybercoders', 'motion recruitment', 'dice']
BLOCKED_COMPANY_PATTERNS = [
    ('SCM Products', [r'\bscm\s+products?\b']),
    ('Adept Technology', [r'\badept\s+technolog(?:y|ies)\b', r'\badept\s+technology\s+consulting\b']),
]
UA = 'HermesJobSearch/2.1 (+Thomas Dupkavich daily job digest)'
# Keep cron stdout clean. JobSpy/LinkedIn likes to emit INFO lines even with
# verbose=0; the Telegram digest should contain jobs, not scraper diary entries.
logging.basicConfig(level=logging.WARNING)
logging.getLogger().setLevel(logging.WARNING)
for _logger_name in ('JobSpy', 'JobSpy:Linkedin', 'JobSpy:Indeed', 'JobSpy:Google'):
    logging.getLogger(_logger_name).setLevel(logging.WARNING)
logging.disable(logging.INFO)
HOME_ZIP = '11746'
LOCAL_RADIUS_MILES = 20
LOCAL_SOURCE_RADIUS_MILES = 30  # collect a little wider, then enforce LI/NYC post-filtering
LOCAL_HOURS_OLD = 336           # local/hybrid LI volume is lower than national remote volume
REMOTE_HOURS_OLD = 96
LOCAL_RESULTS_WANTED = 25
REMOTE_RESULTS_WANTED = 25
# Displayed digest is intentionally sectioned. Scores are only compared inside
# their own work-mode profile, not as one blended remote/hybrid/onsite ladder.
DIGEST_MAX_JOBS = 12
REMOTE_SECTION_LIMIT = 6
HYBRID_SECTION_LIMIT = 3
ONSITE_SECTION_LIMIT = 3
REMOTE_MIN_SCORE = 60
HYBRID_MIN_SCORE = 52
ONSITE_MIN_SCORE = 48
LOCAL_MIN_SCORE = HYBRID_MIN_SCORE  # compatibility alias for older debug/report code
LOCAL_LONG_ISLAND_TERMS = [
    'huntington', 'huntington station', 'melville', 'dix hills', 'deer park', 'commack',
    'east northport', 'northport', 'greenlawn', 'centerport', 'woodbury', 'syosset',
    'plainview', 'old bethpage', 'bethpage', 'farmingdale', 'amityville', 'massapequa',
    'hicksville', 'levittown', 'westbury', 'garden city', 'carle place', 'jericho',
    'mineola', 'uniondale', 'lake success', 'new hyde park', 'great neck', 'roslyn',
    'hauppauge', 'smithtown', 'kings park', 'islandia', 'ronkonkoma', 'bohemia',
    'holtsville', 'medford', 'babylon', 'west babylon', 'bay shore', 'islip',
    'central islip', 'farmingville', 'port jefferson', 'patchogue', 'suffolk county',
    'nassau county', 'long island'
]
NYC_TERMS = ['new york, ny', 'nyc', 'manhattan', 'brooklyn', 'queens', 'bronx', 'staten island', 'jersey city', 'hoboken', 'long island city', 'lic, ny']
HYBRID_TERMS = ['hybrid', '#li-hybrid', 'on-site', 'onsite', 'in-office', 'in office', 'office days', 'office 2 days', 'office 3 days', 'commute', 'flexible schedule']
LOCAL_SEARCH_TERMS = ['Software Engineer', 'Software Developer', '.NET Developer', 'C# Developer', 'ASP.NET Developer', 'Full Stack Developer', 'Application Developer', 'SQL Server Developer']
LOCAL_LOCATIONS = ['11746', 'Huntington Station, NY', 'Melville, NY', 'Bethpage, NY', 'Plainview, NY', 'Hauppauge, NY', 'Hicksville, NY', 'Syosset, NY', 'Farmingdale, NY', 'Garden City, NY', 'Long Island, NY']
TARGET_COMPANIES = [
    'zebra technologies', 'canon usa', 'broadridge', 'northwell', 'henry schein',
    'dealertrack', 'cox automotive', 'altice', 'softheon', 'publishers clearing house',
]

REMOTE_QUERIES = [
    # Fully remote searches. Hybrid/onsite roles found here are hard-filtered unless they are local to 11746.
    # ZipRecruiter removed due to 403 blocking (Cloudflare bot detection)
    {'search_term': '.NET Developer', 'location': 'United States', 'is_remote': True, 'site_name': ['indeed','linkedin','google'], 'kind': 'remote'},
    {'search_term': 'C# Software Engineer', 'location': 'United States', 'is_remote': True, 'site_name': ['indeed','linkedin','google'], 'kind': 'remote'},
    {'search_term': 'Full Stack .NET React', 'location': 'United States', 'is_remote': True, 'site_name': ['indeed','google'], 'kind': 'remote'},
    {'search_term': 'ASP.NET SQL Server Developer', 'location': 'United States', 'is_remote': True, 'site_name': ['indeed','google'], 'kind': 'remote'},
    # Add broader remote searches
    {'search_term': 'Senior Software Engineer .NET', 'location': 'United States', 'is_remote': True, 'site_name': ['indeed','linkedin','google'], 'kind': 'remote'},
    {'search_term': 'Backend Developer C#', 'location': 'United States', 'is_remote': True, 'site_name': ['indeed','google'], 'kind': 'remote'},
]

# Local searches are intentionally broad: don't put "hybrid" in the query, because LinkedIn/Indeed often
# keep hybrid in metadata/description rather than title/search terms. Classify and filter after fetch.
QUERIES = REMOTE_QUERIES + [
    {'search_term': term, 'location': loc, 'is_remote': None, 'site_name': ['linkedin'], 'kind': 'local'}
    for term in LOCAL_SEARCH_TERMS[:5] for loc in LOCAL_LOCATIONS[:4]
] + [
    {'search_term': term, 'location': loc, 'is_remote': None, 'site_name': ['indeed','google'], 'kind': 'local'}
    for term in LOCAL_SEARCH_TERMS[:5] for loc in [LOCAL_LOCATIONS[i] for i in (0, 2, 4)]
]


def clean(s: Any) -> str:
    s = '' if s is None else str(s)
    s = re.sub(r'<[^>]+>', ' ', s)
    s = html.unescape(s)
    return re.sub(r'\s+', ' ', s).strip()


def norm(s: str) -> str:
    return re.sub(r'[^a-z0-9]+', '', s.lower())


def sanitize_url(url: str) -> str:
    """Remove apply/session tokens before saving or sending digests."""
    if not url:
        return ''
    # Indeed injects sensitive one-time apply tokens into employer URLs.
    url = re.sub(r'([?&])indeed-apply-token=[^&#\s]+', r'\1indeed-apply-token=[REDACTED]', url, flags=re.I)
    url = re.sub(r'([?&])(jk|vjk|from|tk)=[^&#\s]+', r'\1\2=[REDACTED]', url, flags=re.I)
    return url


NEUTRAL_ATS_HOST_TERMS = [
    'greenhouse.io', 'lever.co', 'workday', 'myworkdayjobs.com', 'icims.com',
    'smartrecruiters.com', 'bamboohr.com', 'adp.com', 'workforcenow.adp.com',
    'paycomonline.net', 'ceipal.com', 'ashbyhq.com', 'jobvite.com',
]


def company_url_tokens(company: str) -> set[str]:
    stop = {'inc', 'llc', 'ltd', 'corp', 'corporation', 'company', 'co', 'the', 'group', 'solutions', 'services'}
    return {t for t in re.findall(r'[a-z0-9]+', clean(company).lower()) if len(t) >= 3 and t not in stop}


def direct_url_looks_mismatched(company: str, direct_url: str) -> bool:
    """Catch scraper-provided direct links that clearly belong to another employer."""
    if not direct_url:
        return False
    try:
        host = urllib.parse.urlparse(direct_url).netloc.lower()
    except Exception:
        return False
    if not host:
        return False
    if any(term in host for term in NEUTRAL_ATS_HOST_TERMS):
        return False
    tokens = company_url_tokens(company)
    if not tokens:
        return False
    host_words = set(re.findall(r'[a-z0-9]+', host))
    return not bool(tokens & host_words)


def best_job_url(job: dict, company: str) -> str:
    direct = clean(job.get('job_url_direct'))
    board = clean(job.get('job_url') or job.get('url'))
    if direct and not direct_url_looks_mismatched(company, direct):
        return sanitize_url(direct)
    return sanitize_url(board or direct)


def parse_date(v: Any) -> str | None:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    s = str(v)
    if not s or s == 'NaT':
        return None
    return s[:10]


def days_old(posted: str | None) -> float | None:
    if not posted:
        return None
    try:
        d = dt.date.fromisoformat(posted[:10])
        return (dt.date.today() - d).days
    except Exception:
        return None


def salary_text(job: dict) -> str:
    parts = []
    for k in ['min_amount','max_amount','interval','currency','salary','compensation']:
        v = job.get(k)
        if v is not None and str(v) not in ('nan','NaN','None',''):
            parts.append(str(v))
    if job.get('min_amount') or job.get('max_amount'):
        lo = job.get('min_amount'); hi = job.get('max_amount')
        try:
            if lo and hi: return f"${int(float(lo)/1000)}k-${int(float(hi)/1000)}k"
            if hi: return f"up to ${int(float(hi)/1000)}k"
            if lo: return f"${int(float(lo)/1000)}k+"
        except Exception:
            pass
    return clean(' '.join(parts)) or 'Unlisted'


def salary_points(job: dict, blob: str) -> tuple[int, bool]:
    nums = []
    for k in ['min_amount','max_amount']:
        v = job.get(k)
        try:
            if v and not math.isnan(float(v)):
                nums.append(float(v))
        except Exception:
            pass
    if not nums:
        # parse $120k, 120,000, etc. conservatively from text
        for m in re.finditer(r'\$?([1-2]\d{2})(?:,?000|k)\b', blob, re.I):
            nums.append(float(m.group(1))*1000)
    if nums:
        mx = max(nums)
        if mx < 120000:
            return 0, True
        if mx >= 160000: return 15, False
        if mx >= 140000: return 11, False
        return 7, False
    return 5, False


def keyword_points(blob: str) -> tuple[int, list[str]]:
    b = blob.lower().replace('c sharp', 'c#')
    approx = {
        '.net core': ['.net core','.net 6','.net 7','.net 8','.net 9','.net 10','dotnet'],
        'entity framework': ['entity framework','ef core','ef6'],
        'rest api': ['rest api','restful','web api'],
        'sql server': ['sql server','mssql','ms sql'],
        'azure devops': ['azure devops','azure pipelines','ado'],
        'react': ['react','react.js','reactjs'],
        'asp.net': ['asp.net','asp.net core','asp net'],
    }
    matched = set()
    for kw in ALL_KEYWORDS:
        variants = approx.get(kw, [kw])
        if any(v in b for v in variants):
            matched.add(kw)
    core_matches = [kw for kw in CORE if kw in matched or any(v in b for v in approx.get(kw, [kw]))]
    n = len(matched)
    if n >= 12 and len(core_matches) >= 3: pts = 35
    elif n >= 9 and len(core_matches) >= 2: pts = 28
    elif n >= 6: pts = 21
    elif n >= 4: pts = 14
    elif n >= 2: pts = 7
    else: pts = 2
    if not core_matches:
        pts = min(pts, 14)
    return pts, sorted(matched)


def has_any(blob: str, terms: list[str]) -> bool:
    return any(x in blob for x in terms)


def blocked_company_reason(blob: str) -> str | None:
    """Return a hard-skip reason for companies Thomas explicitly banned."""
    b = clean(blob).lower()
    for label, patterns in BLOCKED_COMPANY_PATTERNS:
        if any(re.search(pattern, b, flags=re.I) for pattern in patterns):
            return f'blocked company: {label}'
    return None


def classify_location(location: str, is_remote: bool, blob: str, query_kind: str = '') -> tuple[str, bool, str]:
    """Return category, hard-skip flag, and display label.

    Categories:
    - remote: completely remote only
    - local_hybrid: Long Island hybrid/flexible/onsite within Thomas's acceptable 11746 commute area
    - local: local onsite-ish role that does not explicitly say hybrid
    """
    b = (location + ' ' + blob).lower()
    loc = clean(location)
    loc_b = loc.lower()
    mentions_remote = is_remote or 'remote' in b or 'work from home' in b or 'wfh' in b
    mentions_hybrid = has_any(b, HYBRID_TERMS)
    location_local_li = has_any(loc_b, LOCAL_LONG_ISLAND_TERMS) or HOME_ZIP in loc_b
    mentions_local_li = location_local_li or has_any(b, LOCAL_LONG_ISLAND_TERMS) or HOME_ZIP in b
    location_nyc = has_any(loc_b, NYC_TERMS)
    mentions_nyc = has_any(b, NYC_TERMS)
    from_local_query = query_kind == 'local'

    # NYC/the city is excluded for local/hybrid. Only tolerate NYC text when the actual listed
    # location is an LI town (e.g. a multi-office description with Melville as the job location).
    if location_nyc or (mentions_nyc and not location_local_li):
        return 'skip', True, loc or 'NYC/on-site'

    if mentions_local_li:
        if mentions_hybrid or mentions_remote or from_local_query:
            return 'local_hybrid', False, f"{loc or 'Long Island'} · LI hybrid/local ≤{LOCAL_RADIUS_MILES}mi target from {HOME_ZIP}"
        return 'local', False, f"{loc or 'Long Island'} · local ≤{LOCAL_RADIUS_MILES}mi target from {HOME_ZIP}"

    # Hybrid is never treated as fully remote. It must be local to Huntington Station / Long Island.
    if mentions_hybrid:
        return 'skip', True, loc or 'Hybrid not local to 11746'

    if mentions_remote:
        return 'remote', False, 'Remote'

    # Onsite/unclear NY without a Long Island signal is too risky for Thomas's commute preference.
    if 'ny' in b or 'new york' in b:
        return 'skip', True, loc or 'NY location not confirmed Long Island'

    return 'skip', True, loc or 'Location unclear/non-remote'


def work_mode_profile(category: str) -> str:
    """Normalize location category into the scoring profile used for ranking."""
    if category == 'remote':
        return 'remote'
    if category == 'local_hybrid':
        return 'hybrid'
    if category == 'local':
        return 'onsite'
    return 'skip'


def location_points(location: str, is_remote: bool, blob: str, query_kind: str = '') -> tuple[int, bool, str, str]:
    """Compatibility wrapper: location is now a profile gate, not one shared score bucket."""
    category, loc_skip, loc_label = classify_location(location, is_remote, blob, query_kind)
    return 0, loc_skip, loc_label, category


def scale_component(value: int, old_max: int, new_max: int) -> int:
    return int(round((max(0, min(old_max, int(value))) / float(old_max or 1)) * new_max))


def seniority_points(blob: str) -> int:
    b = blob.lower()
    if re.search(r'10\+|10 years|12\+|principal|staff engineer|architect', b): return 0
    if re.search(r'4\+|5\+|6\+|7\+|8\+|senior|sr\.?|lead', b): return 5
    if re.search(r'3\+|mid[- ]?senior|mid level|mid-level', b): return 4
    if re.search(r'2\+|2 years|junior|entry', b): return 2
    return 4


def posted_hours_old(posted_raw: Any) -> float | None:
    if posted_raw is None or (isinstance(posted_raw, float) and math.isnan(posted_raw)):
        return None
    s = str(posted_raw).strip()
    if not s or s == 'NaT':
        return None
    try:
        cleaned = s.replace('Z', '+00:00')
        if 'T' in cleaned or (len(cleaned) > 10 and s[10] == ' '):
            parsed = dt.datetime.fromisoformat(cleaned)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return max(0.0, (dt.datetime.now(tz=dt.timezone.utc) - parsed).total_seconds() / 3600)
    except Exception:
        pass
    try:
        days = (dt.date.today() - dt.date.fromisoformat(s[:10])).days
        return max(0.0, float(days) * 24.0)
    except Exception:
        return None


def recency_points(posted_raw: Any, profile: str) -> tuple[int, int, str, float | None]:
    """Profile-specific recency. Remote decays fastest because applicant pools explode."""
    hours = posted_hours_old(posted_raw)
    if profile == 'remote':
        max_pts = 18
        if hours is None: return 4, max_pts, 'date unknown', None
        if hours <= 12: return 18, max_pts, 'posted ≤12h', hours
        if hours <= 24: return 16, max_pts, 'posted ≤24h', hours
        if hours <= 48: return 13, max_pts, 'posted ≤2d', hours
        if hours <= 72: return 9, max_pts, 'posted ≤3d', hours
        if hours <= 120: return 4, max_pts, 'posted ≤5d', hours
        return 0, max_pts, 'remote stale >5d', hours
    if profile == 'hybrid':
        max_pts = 12
        if hours is None: return 4, max_pts, 'date unknown', None
        if hours <= 24: return 12, max_pts, 'posted ≤24h', hours
        if hours <= 72: return 10, max_pts, 'posted ≤3d', hours
        if hours <= 168: return 7, max_pts, 'posted ≤7d', hours
        if hours <= 336: return 3, max_pts, 'posted ≤14d', hours
        return 0, max_pts, 'hybrid stale >14d', hours
    max_pts = 8
    if hours is None: return 3, max_pts, 'date unknown', None
    if hours <= 48: return 8, max_pts, 'posted ≤2d', hours
    if hours <= 120: return 6, max_pts, 'posted ≤5d', hours
    if hours <= 336: return 3, max_pts, 'posted ≤14d', hours
    return 0, max_pts, 'onsite stale >14d', hours


def freshness_points(posted_raw: Any) -> int:
    return recency_points(posted_raw, 'remote')[0]


def direct_apply_url(url: str) -> bool:
    u = clean(url).lower()
    return bool(u) and not any(s in u for s in ['indeed.com','linkedin.com','ziprecruiter.com','glassdoor.com','google.com'])


def applicant_count(job: dict, blob: str) -> int | None:
    for key in ['applicants', 'applicant_count', 'num_applicants', 'applications', 'application_count']:
        v = job.get(key)
        try:
            if v is not None and str(v).strip() and str(v).lower() not in {'nan', 'none', 'null'}:
                return int(float(str(v).replace(',', '').replace('+', '')))
        except Exception:
            pass
    b = blob.lower()
    for pattern in [
        r'(?:over|more than|>\s*)\s*(\d[\d,]*)\+?\s+(?:applicants|applications)',
        r'(\d[\d,]*)\+?\s+(?:applicants|applications|people applied)',
        r'(\d[\d,]*)\+?\s+(?:applied)',
    ]:
        m = re.search(pattern, b)
        if m:
            try:
                return int(m.group(1).replace(',', ''))
            except Exception:
                continue
    return None


def competition_points(job: dict, source_count: int, profile: str = 'remote', blob: str = '', posted_hours: float | None = None) -> tuple[int, int, str, int | None]:
    """Interview-realism proxy. Remote gets harsher competition penalties."""
    b = blob.lower()
    company_blob = clean(job.get('company') or '').lower()
    source = clean(job.get('site') or job.get('source') or '').lower()
    url = best_job_url(job, clean(job.get('company') or ''))
    direct = direct_apply_url(url)
    applicants = applicant_count(job, b)
    if profile == 'remote':
        max_pts, pts = 22, 14
    elif profile == 'hybrid':
        max_pts, pts = 18, 12
    else:
        max_pts, pts = 12, 9
    reasons: list[str] = []
    if direct:
        pts += 4 if profile == 'remote' else 3
        reasons.append('direct apply')
    else:
        pts -= 3 if profile == 'remote' else 1
        reasons.append('aggregator apply')
    if source_count >= 3:
        pts -= 3 if profile == 'remote' else 1
        reasons.append('multi-board repost')
    if any(w in company_blob for w in STAFFING_WORDS):
        pts -= 4 if profile == 'remote' else 3
        reasons.append('staffing/recruiter')
    if applicants is not None:
        if applicants >= 1000:
            pts -= 12 if profile == 'remote' else 5
            reasons.append('1000+ applicants')
        elif applicants >= 500:
            pts -= 9 if profile == 'remote' else 4
            reasons.append('500+ applicants')
        elif applicants >= 100:
            pts -= 5 if profile == 'remote' else 2
            reasons.append('100+ applicants')
        elif applicants >= 50:
            pts -= 3 if profile == 'remote' else 1
            reasons.append('50+ applicants')
    elif profile == 'remote' and not direct and posted_hours is not None and posted_hours > 72:
        pts -= 3
        reasons.append('older remote aggregator')
    if profile == 'remote' and ('easy apply' in b or source in {'linkedin', 'indeed'}):
        pts -= 2
        reasons.append('crowded easy-apply proxy')
    if profile == 'remote' and direct and posted_hours is not None and posted_hours <= 24:
        pts += 2
        reasons.append('fresh direct opening')
    pts = max(0, min(max_pts, pts))
    return pts, max_pts, '; '.join(dict.fromkeys(reasons)) or 'normal competition', applicants


def bonus_points(blob: str) -> tuple[int, str]:
    b = blob.lower()
    if any(x in b for x in TARGET_COMPANIES): return 5, 'target LI/company list'
    if 'vibe coding' in b: return 5, 'vibe coding'
    if any(x in b for x in ['cursor', 'copilot', 'claude', 'ai assisted', 'ai-assisted']): return 4, 'AI tools'
    if any(x in b for x in ['llm', 'ai-first', 'genai', 'generative ai']): return 3, 'LLM/AI workflow'
    if any(x in b for x in ['cloud-native','microservices','devops','ci/cd','kubernetes','azure']): return 2, 'modern stack'
    return 0, 'traditional/unspecified'


def interview_chance(score: int, breakdown: dict, job: dict) -> str:
    """Plain-English callback/interview odds proxy, not a guarantee."""
    profile = clean(breakdown.get('score_profile') or breakdown.get('profile') or '')
    match = int(breakdown.get('fit', 0))
    match_max = max(1, int(breakdown.get('fit_max', 60)))
    realism = int(breakdown.get('hireability', 0))
    realism_max = max(1, int(breakdown.get('hireability_max', 40)))
    freshness = int(breakdown.get('freshness', 0))
    applicants = breakdown.get('applicants')
    applicant_wall = isinstance(applicants, int) and applicants >= 1000
    match_pct = match / match_max
    realism_pct = realism / realism_max
    if profile == 'remote':
        if score >= 84 and match_pct >= 0.76 and realism_pct >= 0.78 and freshness >= 12 and not applicant_wall:
            return 'high'
        if score >= 74 and match_pct >= 0.68 and realism_pct >= 0.62 and not applicant_wall:
            return 'good'
        if score >= 62 and match_pct >= 0.58 and realism_pct >= 0.45:
            return 'possible'
        return 'low'
    if profile == 'hybrid':
        if score >= 80 and match_pct >= 0.72 and realism_pct >= 0.65:
            return 'high'
        if score >= 68 and match_pct >= 0.62:
            return 'good'
        if score >= 56:
            return 'possible'
        return 'low'
    if profile == 'onsite':
        if score >= 78 and match_pct >= 0.70:
            return 'high'
        if score >= 66:
            return 'good'
        if score >= 54:
            return 'possible'
        return 'low'
    return 'low'


def hard_skip(blob: str, sal_skip: bool, loc_skip: bool) -> str | None:
    b = blob.lower()
    blocked = blocked_company_reason(blob)
    if blocked: return blocked
    if sal_skip: return 'salary below $120k'
    if loc_skip: return 'commute/location hard filter'
    if any(x in b for x in NEGATIVE[:4]): return 'clearance required'
    if any(re.search(pattern, b) for pattern in EXPERIENCE_NEGATIVE_PATTERNS): return 'entry/intern/unpaid role'
    if any(x in b for x in ['europe only','uk only','canada only','non-us','outside us only']): return 'non-US pay/location'
    return None


def score_job(job: dict, source_count: int = 1) -> dict:
    title = clean(job.get('title'))
    company = clean(job.get('company'))
    location = clean(job.get('location'))
    desc = clean(job.get('description') or job.get('snippet') or '')
    url = best_job_url(job, company)
    source = clean(job.get('site') or job.get('source') or 'JobSpy')
    query_kind = clean(job.get('_query_kind') or '')
    query_location = clean(job.get('_query_location') or '')
    query_term = clean(job.get('_query_term') or '')
    posted_raw = job.get('date_posted') or job.get('posted') or job.get('date')
    posted = parse_date(posted_raw)
    # Do not include query_location in the scoring/classification blob; it would make every local search
    # look like an LI job even when the actual listing says NYC/White Plains/CT.
    blob = ' '.join([title, company, location, query_term, desc, salary_text(job)])

    kw_pts, matched = keyword_points(blob)
    sal_pts, sal_skip = salary_points(job, blob)
    _loc_pts, loc_skip, loc_label, category = location_points(location, bool(job.get('is_remote')), blob, query_kind)
    profile = work_mode_profile(category)
    sen_pts = seniority_points(blob)
    recency_pts, recency_max, recency_reason, posted_hours = recency_points(posted_raw, profile)
    comp_pts, comp_max, comp_reason, applicants = competition_points(job, source_count, profile, blob, posted_hours)
    bonus_pts, bonus_reason = bonus_points(blob)
    skip = hard_skip(blob, sal_skip, loc_skip)

    if profile == 'remote':
        match = scale_component(kw_pts, 35, 42) + scale_component(sal_pts, 15, 8) + scale_component(sen_pts, 5, 8) + min(bonus_pts, 2)
        match_max = 60
        location_fit = 0
        realism = comp_pts + recency_pts
        realism_max = comp_max + recency_max
    elif profile == 'hybrid':
        match = scale_component(kw_pts, 35, 36) + scale_component(sal_pts, 15, 10) + scale_component(sen_pts, 5, 7) + min(bonus_pts, 2)
        match_max = 55
        location_fit = 15
        realism = comp_pts + recency_pts
        realism_max = comp_max + recency_max
    elif profile == 'onsite':
        match = scale_component(kw_pts, 35, 35) + scale_component(sal_pts, 15, 10) + scale_component(sen_pts, 5, 7) + min(bonus_pts, 3)
        match_max = 55
        location_fit = 25
        realism = comp_pts + recency_pts
        realism_max = comp_max + recency_max
    else:
        match = 0
        match_max = 60
        location_fit = 0
        realism = 0
        realism_max = comp_max + recency_max

    total = 0 if skip else match + location_fit + realism
    breakdown = {
        'score_profile': profile,
        'fit': match, 'fit_max': match_max,
        'keywords': kw_pts, 'salary': sal_pts, 'location': location_fit, 'seniority': sen_pts,
        'hireability': realism, 'hireability_max': realism_max,
        'competition': comp_pts, 'competition_max': comp_max, 'competition_reason': comp_reason,
        'freshness': recency_pts, 'freshness_max': recency_max, 'freshness_reason': recency_reason,
        'bonus': bonus_pts, 'bonus_reason': bonus_reason,
        'applicants': applicants,
    }
    chance = interview_chance(int(total), breakdown, job)
    return {
        'score': int(total), 'score_profile': profile, 'title': title, 'company': company, 'salary': salary_text(job),
        'location': loc_label, 'category': category, 'interview_chance': chance,
        'source': source, 'url': url, 'posted': posted,
        'query_kind': query_kind, 'query_location': query_location, 'query_term': query_term,
        'keywords_matched': matched[:18], 'snippet': desc[:500], 'skip_reason': skip,
        'breakdown': breakdown,
    }


def fetch_json(url: str, timeout=12):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8', errors='replace'))


def fetch_text(url: str, timeout=12) -> str:
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/rss+xml, application/xml, text/xml, */*'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode('utf-8', errors='replace')


def source_interesting(blob: str) -> bool:
    b = blob.lower().replace('c sharp', 'c#')
    terms = [
        '.net', 'dotnet', 'c#', 'csharp', 'asp.net', 'sql server', 'mssql',
        'entity framework', 'azure devops', 'react', 'typescript', 'javascript',
        'full stack', 'backend', 'software engineer', 'software developer',
    ]
    return any(term in b for term in terms)


def env_value(name: str) -> str:
    """Read an env var from the live process or Hermes dotenv files without printing secrets."""
    value = os.environ.get(name)
    if value:
        return value.strip().strip('"').strip("'")
    for path in APIFY_ENV_FILES:
        if not path.exists():
            continue
        try:
            for line in path.read_text(encoding='utf-8', errors='ignore').splitlines():
                raw = line.strip()
                if not raw or raw.startswith('#') or '=' not in raw:
                    continue
                key, val = raw.split('=', 1)
                if key.strip() == name:
                    return val.strip().strip('"').strip("'")
        except Exception:
            continue
    return ''


def env_bool(name: str, default: bool = False) -> bool:
    value = env_value(name)
    if not value:
        return default
    return value.lower() in {'1', 'true', 'yes', 'on'}


def env_int(name: str, default: int, min_value: int, max_value: int) -> int:
    try:
        value = int(env_value(name) or default)
    except Exception:
        value = default
    return max(min_value, min(max_value, value))


def scrub_secret(text: Any, secret: str = '') -> str:
    s = clean(text)
    if secret:
        s = s.replace(secret, '[REDACTED]')
    s = re.sub(r'token=[^&\s]+', 'token=[REDACTED]', s, flags=re.I)
    return s[:240]


def apify_enabled() -> bool:
    return bool(env_value('APIFY_TOKEN')) and not env_bool('APIFY_JOB_SOURCES_DISABLED', False)


def apify_actor_items(actor: str, payload: dict, timeout: int | None = None) -> tuple[list[dict], str | None]:
    """Run a small Apify actor query and return dataset items, never leaking the token in errors."""
    token = env_value('APIFY_TOKEN')
    if not token:
        return [], 'APIFY_TOKEN not configured'
    url = f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items?token={urllib.parse.quote(token)}"
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        url,
        data=data,
        method='POST',
        headers={'User-Agent': UA, 'Accept': 'application/json', 'Content-Type': 'application/json'},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout or APIFY_TIMEOUT_SECS) as r:
            body = r.read().decode('utf-8', errors='replace')
        parsed = json.loads(body or '[]')
        if isinstance(parsed, list):
            return [x for x in parsed if isinstance(x, dict)], None
        return [], f'{actor} returned non-list dataset payload'
    except Exception as e:
        return [], f'{actor}: {scrub_secret(e, token)}'


def join_text(value: Any, limit: int = 2000) -> str:
    if isinstance(value, list):
        text = ' · '.join(clean(x) for x in value if clean(x))
    elif isinstance(value, dict):
        text = ' · '.join(f'{clean(k)}: {clean(v)}' for k, v in value.items() if clean(v))
    else:
        text = clean(value)
    return text[:limit]


def parse_amount(value: Any) -> float | None:
    if value is None or value == '':
        return None
    try:
        amount = float(str(value).replace('$', '').replace(',', '').replace('k', '000').replace('K', '000'))
        return amount if amount > 0 else None
    except Exception:
        return None


def posted_label_to_iso(value: Any) -> str | None:
    s = clean(value).lower()
    if not s:
        return None
    today = dt.date.today()
    if any(x in s for x in ['today', 'hour', 'minute', 'just now']):
        return today.isoformat()
    if 'yesterday' in s:
        return (today - dt.timedelta(days=1)).isoformat()
    m = re.search(r'(\d+)\s+day', s)
    if m:
        return (today - dt.timedelta(days=int(m.group(1)))).isoformat()
    m = re.search(r'(\d+)\s+week', s)
    if m:
        return (today - dt.timedelta(days=int(m.group(1)) * 7)).isoformat()
    try:
        return dt.datetime.fromisoformat(str(value).replace('Z', '+00:00')).date().isoformat()
    except Exception:
        return None


def normalize_builtin_job(item: dict, query_label: str) -> dict:
    remote_type = clean(item.get('remoteType') or item.get('remote_policy') or item.get('remote') or '')
    location = clean(item.get('location') or item.get('locations') or remote_type or 'Remote')
    skills = join_text(item.get('skills'), 600)
    industries = join_text(item.get('industries'), 400)
    desc = ' '.join(filter(None, [
        clean(item.get('description') or item.get('snippet') or item.get('summary')),
        f'Remote policy: {remote_type}' if remote_type else '',
        f'Experience: {clean(item.get("experienceLevel"))}' if clean(item.get('experienceLevel')) else '',
        f'Skills: {skills}' if skills else '',
        f'Industries: {industries}' if industries else '',
    ]))
    posted = posted_label_to_iso(item.get('postedLabel') or item.get('posted') or item.get('scrapedAt'))
    remote_b = remote_type.lower()
    is_remote = 'remote' in remote_b and 'hybrid' not in remote_b and 'office' not in remote_b
    query_kind = 'remote' if is_remote else 'local'
    return {
        'source': 'BuiltIn (Apify)', 'site': 'builtin', 'title': item.get('title'),
        'company': item.get('company'), 'location': ' · '.join(x for x in [location, remote_type] if x),
        'url': item.get('url') or item.get('jobUrl'), 'description': desc, 'posted': posted,
        'date_posted': posted, 'salary': item.get('salary'), 'is_remote': is_remote,
        '_query_kind': query_kind, '_query_location': 'Built In', '_query_term': query_label,
    }


def normalize_wellfound_job(item: dict, query_label: str) -> dict:
    locations = join_text(item.get('locationNames') or item.get('locations') or item.get('acceptedRemoteLocations'), 400)
    is_remote = bool(item.get('remote') or item.get('acceptedRemoteLocations'))
    location = locations or ('Remote' if is_remote else '')
    compensation = clean(item.get('compensation') or item.get('salary') or '')
    desc = ' '.join(filter(None, [
        clean(item.get('jobDescription') or item.get('description') or item.get('descriptionMarkdown')),
        clean(item.get('companyHighConcept') or item.get('companyTagline')),
        join_text(item.get('companyBadges'), 400),
        clean(item.get('primaryRole') or item.get('primaryRoleTitle')),
    ]))
    posted = posted_label_to_iso(item.get('datePosted') or item.get('postedAt') or item.get('firstSeenAt') or item.get('lastSeenAt'))
    min_amount = parse_amount(item.get('salaryMin'))
    max_amount = parse_amount(item.get('salaryMax'))
    return {
        'source': 'Wellfound (Apify)', 'site': 'wellfound',
        'title': item.get('jobTitle') or item.get('title'),
        'company': item.get('companyName') or item.get('company'),
        'location': location, 'url': item.get('jobUrl') or item.get('portalUrl') or item.get('detailUrl'),
        'description': desc, 'posted': posted, 'date_posted': posted,
        'salary': compensation, 'compensation': compensation,
        'min_amount': min_amount, 'max_amount': max_amount,
        'is_remote': is_remote, '_query_kind': 'remote' if is_remote else 'local',
        '_query_location': 'Wellfound', '_query_term': query_label,
    }


def apify_feed_jobs() -> tuple[list[dict], list[str]]:
    """Add Built In + Wellfound via Apify when APIFY_TOKEN is configured."""
    if not apify_enabled():
        return [], []
    builtin_max = env_int('APIFY_BUILTIN_MAX_JOBS', 30, 1, 100)
    wellfound_max = env_int('APIFY_WELLFOUND_MAX_RESULTS', 35, 1, 100)
    jobs: list[dict] = []
    errors: list[str] = []

    hybrid_builtin_max = max(10, builtin_max // 2) if builtin_max >= 10 else builtin_max
    builtin_queries = [
        ('.NET Developer remote', {'searchTitle': '.NET Developer', 'remote': ['remote'], 'categories': ['software-engineering'], 'experience': ['mid-level', 'senior'], 'daysSinceUpdated': 7, 'maxJobs': builtin_max}),
        ('C# Software Engineer remote', {'searchTitle': 'C# Software Engineer', 'remote': ['remote'], 'categories': ['software-engineering'], 'experience': ['mid-level', 'senior'], 'daysSinceUpdated': 7, 'maxJobs': builtin_max}),
        ('ASP.NET SQL Server hybrid', {'searchTitle': 'ASP.NET SQL Server', 'remote': ['hybrid'], 'categories': ['software-engineering'], 'experience': ['mid-level', 'senior'], 'daysSinceUpdated': 30, 'maxJobs': hybrid_builtin_max}),
    ]
    for label, payload in builtin_queries:
        items, err = apify_actor_items(APIFY_BUILTIN_ACTOR, payload)
        if err:
            errors.append(err)
            continue
        jobs.extend(normalize_builtin_job(item, label) for item in items[:int(payload.get('maxJobs') or builtin_max)])

    wellfound_payload = {
        'query': 'software engineer',
        'remote': True,
        'maxResults': wellfound_max,
        'maxPages': 2,
        'enrichDetail': False,
        'enrichCompany': False,
        'companyOnlyMode': False,
        'descriptionMaxLength': 2500,
        'customFilters': [{'field': 'title', 'op': 'includes', 'value': 'Engineer'}],
    }
    items, err = apify_actor_items(APIFY_WELLFOUND_ACTOR, wellfound_payload)
    if err:
        errors.append(err)
    else:
        jobs.extend(normalize_wellfound_job(item, 'Wellfound remote software engineer') for item in items)
    return jobs, errors


def public_feed_jobs() -> list[dict]:
    out=[]
    try:
        for q in ['.net','c%23','asp.net','sql server']:
            data=fetch_json(f'https://remotive.com/api/remote-jobs?search={urllib.parse.quote(q)}')
            for j in data.get('jobs', [])[:30]:
                out.append({'source':'Remotive','title':j.get('title'),'company':j.get('company_name'),'location':j.get('candidate_required_location') or 'Remote','url':j.get('url'),'description':j.get('description'),'posted':j.get('publication_date')})
    except Exception as e:
        out.append({'source':'Remotive','error':str(e)[:120]})
    try:
        data=fetch_json('https://www.arbeitnow.com/api/job-board-api')
        for j in data.get('data', [])[:80]:
            out.append({'source':'Arbeitnow','title':j.get('title'),'company':j.get('company_name'),'location':j.get('location'),'url':j.get('url'),'description':j.get('description'),'posted':j.get('created_at')})
    except Exception as e:
        out.append({'source':'Arbeitnow','error':str(e)[:120]})
    try:
        data=fetch_json('https://remoteok.com/api')
        for j in [x for x in data if isinstance(x, dict)][:120]:
            blob = clean(' '.join([
                j.get('position') or '',
                j.get('company') or '',
                ' '.join(j.get('tags') or []),
                j.get('description') or '',
            ]))
            if not source_interesting(blob):
                continue
            out.append({
                'source':'RemoteOK', 'title':j.get('position'), 'company':j.get('company'),
                'location':'Remote', 'url':j.get('url'), 'description':j.get('description') or ' '.join(j.get('tags') or []),
                'posted':j.get('date'), 'is_remote': True,
            })
    except Exception as e:
        out.append({'source':'RemoteOK','error':str(e)[:120]})
    try:
        for page in (1, 2):
            data=fetch_json(f'https://www.themuse.com/api/public/jobs?category=Software%20Engineering&page={page}')
            for j in data.get('results') or data.get('items') or []:
                company = (j.get('company') or {}).get('name') if isinstance(j.get('company'), dict) else j.get('company')
                locations = ', '.join([
                    clean(x.get('name') if isinstance(x, dict) else x)
                    for x in (j.get('locations') or [])
                    if clean(x.get('name') if isinstance(x, dict) else x)
                ])
                contents = clean(j.get('contents') or '')
                blob = ' '.join([clean(j.get('name')), clean(company), locations, contents])
                if not source_interesting(blob):
                    continue
                refs = j.get('refs') or {}
                out.append({
                    'source':'The Muse', 'title':j.get('name'), 'company':company,
                    'location':locations or 'Location unclear',
                    'url':refs.get('landing_page') or refs.get('apply') or j.get('url'),
                    'description':contents, 'posted':j.get('publication_date'),
                    'is_remote':'remote' in locations.lower(),
                })
    except Exception as e:
        out.append({'source':'The Muse','error':str(e)[:120]})
    try:
        root = ET.fromstring(fetch_text('https://weworkremotely.com/remote-jobs.rss'))
        for item in root.findall('./channel/item')[:120]:
            title = clean(item.findtext('title'))
            desc = clean(item.findtext('description'))
            if not source_interesting(' '.join([title, desc])):
                continue
            out.append({
                'source':'We Work Remotely', 'title':title, 'company':'',
                'location':'Remote', 'url':clean(item.findtext('link')),
                'description':desc, 'posted':item.findtext('pubDate'), 'is_remote': True,
            })
    except Exception as e:
        out.append({'source':'We Work Remotely','error':str(e)[:120]})
    try:
        data=fetch_json('https://api.lever.co/v0/postings/softheon?mode=json')
        for j in data if isinstance(data, list) else []:
            categories = j.get('categories') or {}
            loc = clean(categories.get('location') or 'Remote/Hybrid unclear')
            text_blob = clean(' '.join([
                j.get('text') or '',
                j.get('descriptionPlain') or '',
                ' '.join([l.get('text', '') for l in (j.get('lists') or []) if isinstance(l, dict)]),
            ]))
            blob = ' '.join([clean(j.get('text')), 'Softheon', loc, text_blob])
            if not source_interesting(blob):
                continue
            out.append({
                'source':'Softheon Lever', 'title':j.get('text'), 'company':'Softheon',
                'location':loc, 'url':j.get('hostedUrl') or j.get('applyUrl'),
                'description':text_blob, 'posted':j.get('createdAt'),
            })
    except Exception as e:
        out.append({'source':'Softheon Lever','error':str(e)[:120]})
    apify_jobs, apify_errors = apify_feed_jobs()
    out.extend(apify_jobs)
    out.extend({'source': 'Apify', 'error': e} for e in apify_errors)
    return out


def run_jobspy_query(q: dict) -> dict:
    try:
        local = q.get('kind') == 'local'
        kwargs = dict(
            site_name=q['site_name'], search_term=q['search_term'], location=q['location'],
            distance=LOCAL_SOURCE_RADIUS_MILES if local else 50,
            results_wanted=LOCAL_RESULTS_WANTED if local else REMOTE_RESULTS_WANTED,
            hours_old=LOCAL_HOURS_OLD if local else REMOTE_HOURS_OLD,
            # Fetching LinkedIn descriptions for every local query is slow enough to trip
            # JobSpy's batch timeout. Keep LinkedIn in the local search mix, but score from
            # listing metadata/snippets so local coverage does not vanish when LinkedIn drags.
            country_indeed='usa', linkedin_fetch_description=False, verbose=0,
        )
        if q.get('is_remote') is not None:
            kwargs['is_remote'] = q['is_remote']
        if 'google' in q.get('site_name', []):
            if local:
                kwargs['google_search_term'] = f"{q['search_term']} hybrid software jobs near {q['location']} Long Island NY -NYC -Manhattan -Brooklyn -Queens"
            else:
                kwargs['google_search_term'] = f"{q['search_term']} fully remote software jobs United States"
        df = scrape_jobs(**kwargs)
        records=[] if df is None else df.to_dict(orient='records')
        return {'query': q['search_term'], 'kind': q.get('kind',''), 'location': q.get('location',''), 'count': len(records), 'jobs': records, 'error': None}
    except Exception as e:
        return {'query': q['search_term'], 'kind': q.get('kind',''), 'location': q.get('location',''), 'count': 0, 'jobs': [], 'error': str(e)[:200]}


def jobspy_query_worker(idx: int, q: dict, outq: mp.Queue) -> None:
    outq.put((idx, run_jobspy_query(q)))


def run_jobspy_queries() -> list[dict]:
    """Run JobSpy queries with a hard batch timeout so one stuck scrape cannot block cron."""
    timeout = env_int('JOBSPY_TOTAL_TIMEOUT_SECS', JOBSPY_TOTAL_TIMEOUT_SECS, 30, 600)
    max_workers = env_int('JOBSPY_MAX_WORKERS', 5, 1, 8)
    start = time.monotonic()
    outq: mp.Queue = mp.Queue()
    pending = list(enumerate(QUERIES))
    running: dict[int, tuple[mp.Process, dict]] = {}
    results_by_idx: dict[int, dict] = {}

    while pending or running:
        while pending and len(running) < max_workers and time.monotonic() - start < timeout:
            idx, q = pending.pop(0)
            proc = mp.Process(target=jobspy_query_worker, args=(idx, q, outq))
            proc.start()
            running[idx] = (proc, q)

        while True:
            try:
                idx, result = outq.get_nowait()
            except queue.Empty:
                break
            results_by_idx[idx] = result

        for idx, (proc, q) in list(running.items()):
            if proc.is_alive():
                continue
            proc.join(timeout=0.1)
            running.pop(idx, None)
            if idx not in results_by_idx:
                results_by_idx[idx] = {'query': q['search_term'], 'kind': q.get('kind',''), 'location': q.get('location',''), 'count': 0, 'jobs': [], 'error': f'worker exited {proc.exitcode}'}

        if time.monotonic() - start >= timeout:
            for idx, (proc, q) in list(running.items()):
                if proc.is_alive():
                    proc.terminate()
                    proc.join(timeout=2)
                    if proc.is_alive():
                        proc.kill()
                        proc.join(timeout=1)
                results_by_idx.setdefault(idx, {'query': q['search_term'], 'kind': q.get('kind',''), 'location': q.get('location',''), 'count': 0, 'jobs': [], 'error': f'timed out after {timeout}s batch limit'})
            for idx, q in pending:
                results_by_idx.setdefault(idx, {'query': q['search_term'], 'kind': q.get('kind',''), 'location': q.get('location',''), 'count': 0, 'jobs': [], 'error': f'timed out after {timeout}s batch limit'})
            break

        time.sleep(0.2)

    return [results_by_idx[i] for i in sorted(results_by_idx)]


def load_applied_keys() -> set[str]:
    # Dedup against the tracker/pipeline is handled downstream by the Node sink
    # (scan-lib.appendPostings), so the Python engine emits all scored postings.
    return set()


CHANCE_RANK = {'high': 3, 'good': 2, 'possible': 1, 'low': 0}


def digest_sort_key(job: dict) -> tuple[int, ...]:
    """Profile-aware section sort. Remote prioritizes realistic interview odds first."""
    breakdown = job.get('breakdown') or {}
    try:
        score = int(job.get('score') or 0)
    except Exception:
        score = 0
    chance = CHANCE_RANK.get(str(job.get('interview_chance') or '').lower(), 0)
    try:
        fit = int(breakdown.get('fit') or 0)
    except Exception:
        fit = 0
    try:
        hireability = int(breakdown.get('hireability') or 0)
    except Exception:
        hireability = 0
    try:
        freshness = int(breakdown.get('freshness') or 0)
    except Exception:
        freshness = 0
    profile = clean(job.get('score_profile') or breakdown.get('score_profile') or work_mode_profile(clean(job.get('category'))))
    if profile == 'remote':
        return chance, score, hireability, freshness, fit
    return score, chance, fit, hireability, freshness


def score_value(job: dict) -> int:
    try:
        return int(job.get('score') or 0)
    except Exception:
        return 0


def source_name(job: dict) -> str:
    return clean(job.get('source')).lower()


def rank_section(pool: list[dict], limit: int, wanted_non_indeed: int = 1) -> list[dict]:
    """Rank within one work-mode section, with conservative source diversity."""
    ranked = sorted(pool, key=digest_sort_key, reverse=True)
    chosen = ranked[:limit]
    if not chosen:
        return []
    for candidate in ranked[limit:]:
        if len([j for j in chosen if source_name(j) != 'indeed']) >= wanted_non_indeed:
            break
        if source_name(candidate) == 'indeed':
            continue
        if source_name(candidate) in {source_name(j) for j in chosen if source_name(j) != 'indeed'}:
            continue
        replaceable = [
            (idx, job) for idx, job in enumerate(chosen)
            if source_name(job) == 'indeed' and score_value(candidate) >= score_value(job) - 8
        ]
        if not replaceable:
            continue
        idx, _job = min(replaceable, key=lambda pair: score_value(pair[1]))
        chosen[idx] = candidate
    return sorted(chosen, key=digest_sort_key, reverse=True)[:limit]


def selected_digest_sections(remote_pool: list[dict], hybrid_pool: list[dict], onsite_pool: list[dict]) -> tuple[list[dict], dict[str, list[dict]]]:
    """Return sectioned, independently-ranked digest rows. No blended mega-ranking."""
    sections = {
        'remote': rank_section(remote_pool, REMOTE_SECTION_LIMIT, wanted_non_indeed=2),
        'hybrid': rank_section(hybrid_pool, HYBRID_SECTION_LIMIT, wanted_non_indeed=1),
        'onsite': rank_section(onsite_pool, ONSITE_SECTION_LIMIT, wanted_non_indeed=1),
    }
    top = (sections['remote'] + sections['hybrid'] + sections['onsite'])[:DIGEST_MAX_JOBS]
    return top, sections


def source_counts(rows: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        if row.get('error'):
            continue
        source = clean(row.get('site') or row.get('source') or 'unknown')
        if not source:
            source = 'unknown'
        counts[source] = counts.get(source, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0].lower())))


def compact(value: Any, limit: int) -> str:
    text = clean(value)
    if len(text) <= limit:
        return text
    return text[:max(0, limit - 1)].rstrip() + '…'


def mode_label(job: dict) -> str:
    category = job.get('category')
    if category == 'remote':
        return '🌐 Remote'
    if category == 'local_hybrid':
        return '🏝 LI hybrid'
    if category == 'local':
        return '🏢 LI onsite'
    return '📍 ' + compact(job.get('location') or 'Location unclear', 24)


def tier_for(job: dict) -> tuple[str, str]:
    score = int(job.get('score') or 0)
    if score >= 85:
        return 'strong', '🟢'
    if score >= 70:
        return 'good', '🔵'
    if score >= 55:
        return 'possible', '🟡'
    return 'low', '⚪'


def safe_markdown_url(url: str) -> str:
    return clean(url).replace('(', '%28').replace(')', '%29').replace(' ', '%20')


def human_date_label(generated_at: str) -> str:
    raw = clean(generated_at)[:10]
    try:
        d = dt.date.fromisoformat(raw)
        return d.strftime('%a, %b %-d')
    except Exception:
        return dt.date.today().strftime('%a, %b %-d')


def main() -> int:
    jobspy_results=run_jobspy_queries()
    feed = public_feed_jobs()
    raw=[]
    local_raw_count = 0
    for res in jobspy_results:
        for j in res['jobs']:
            jj = dict(j)
            jj['_query_kind'] = res.get('kind','')
            jj['_query_location'] = res.get('location','')
            jj['_query_term'] = res.get('query','')
            if jj['_query_kind'] == 'local':
                local_raw_count += 1
            raw.append(jj)
    raw.extend([j for j in feed if not j.get('error')])

    grouped={}
    for j in raw:
        key=norm(clean(j.get('company'))+'|'+clean(j.get('title')))
        if not key or len(key)<5: continue
        grouped.setdefault(key, []).append(j)

    applied=load_applied_keys()
    scored=[]; skipped=[]
    for key, jobs in grouped.items():
        if key in applied:
            continue
        # Keep richest description/direct-url job, but retain source count for competition proxy.
        # Preserve local-query candidates when tied so LI hits do not get hidden by remote duplicates.
        best=max(jobs, key=lambda x: len(clean(x.get('description') or x.get('snippet') or '')) + (50 if x.get('job_url_direct') else 0) + (25 if x.get('_query_kind') == 'local' else 0))
        s=score_job(best, len(jobs))
        s['sources_seen']=sorted({clean(x.get('site') or x.get('source') or 'JobSpy') for x in jobs if clean(x.get('site') or x.get('source') or 'JobSpy')})
        profile = clean(s.get('score_profile') or s.get('breakdown', {}).get('score_profile') or '')
        if profile == 'remote':
            min_score = REMOTE_MIN_SCORE
        elif profile == 'hybrid':
            min_score = HYBRID_MIN_SCORE
        elif profile == 'onsite':
            min_score = ONSITE_MIN_SCORE
        else:
            min_score = LOCAL_MIN_SCORE if s.get('query_kind') == 'local' else REMOTE_MIN_SCORE
        if s['score'] >= min_score and s.get('url'):
            scored.append(s)
        else:
            skipped.append(s)
    scored.sort(key=digest_sort_key, reverse=True)
    allowed_chances = {'high','good','possible'}
    remote_pool=[j for j in scored if j.get('category') == 'remote' and j.get('interview_chance') in allowed_chances]
    hybrid_pool=[j for j in scored if j.get('category') == 'local_hybrid' and (j.get('interview_chance') in allowed_chances or int(j.get('score') or 0) >= HYBRID_MIN_SCORE)]
    onsite_pool=[j for j in scored if j.get('category') == 'local' and (j.get('interview_chance') in allowed_chances or int(j.get('score') or 0) >= ONSITE_MIN_SCORE)]
    local_skipped_debug=sorted([
        j for j in skipped
        if j.get('query_kind') == 'local' or j.get('category') in ('local_hybrid','local') or 'long island' in (j.get('location','') + ' ' + j.get('snippet','')).lower()
    ], key=lambda x: (x.get('score', 0), x.get('breakdown', {}).get('keywords', 0)), reverse=True)[:15]
    skip_reason_counts={}
    for j in skipped:
        reason=j.get('skip_reason') or 'below score/url threshold'
        skip_reason_counts[reason]=skip_reason_counts.get(reason,0)+1
    top, sectioned_top = selected_digest_sections(remote_pool, hybrid_pool, onsite_pool)
    if not top:
        fallback=sorted([x for x in skipped if x.get('url')], key=digest_sort_key, reverse=True)[:3]
        sectioned_top = {
            'remote': [j for j in fallback if j.get('category') == 'remote'],
            'hybrid': [j for j in fallback if j.get('category') == 'local_hybrid'],
            'onsite': [j for j in fallback if j.get('category') == 'local'],
        }
        top = fallback
    for i,j in enumerate(top,1): j['n']=i
    remote_top=sectioned_top.get('remote') or []
    hybrid_top=sectioned_top.get('hybrid') or []
    onsite_top=sectioned_top.get('onsite') or []
    local_top=hybrid_top + onsite_top
    now=dt.datetime.now().isoformat(timespec='seconds')
    payload={
        'generated_at':now,
        'resume_path':str(RESUME_PATH),
        'preferences':{
            'remote':'completely remote only',
            'local_hybrid':f'Long Island only, target ≤{LOCAL_RADIUS_MILES} miles from {HOME_ZIP}; NYC excluded',
            'ranking':'prioritize good/high interview-chance proxy from fit + direct apply/competition + freshness',
            'blocked_companies':['SCM Products','Adept Technology','Adept Technologies','Adept Technology Consulting'],
            'target_companies':TARGET_COMPANIES,
        },
        'stats':{
            'jobspy_queries':{f"{r['query']} [{r.get('kind','')} {r.get('location','')}]":r['count'] for r in jobspy_results},
            'jobspy_errors':[f"{r['query']}: {r['error']}" for r in jobspy_results if r['error']],
            'jobspy_source_counts':source_counts([j for r in jobspy_results for j in r.get('jobs', [])]),
            'public_feed_count':len([j for j in feed if not j.get('error')]),
            'feed_source_counts':source_counts(feed),
            'candidate_source_counts':source_counts(scored),
            'shown_source_counts':source_counts(top),
            'raw_count':len(raw),'after_dedupe':len(grouped),'shown':len(top),
            'shown_local_hybrid':len(local_top),'shown_remote':len(remote_top),
            'shown_hybrid':len(hybrid_top),'shown_onsite':len(onsite_top),
            'candidate_remote_count':len(remote_pool),'candidate_hybrid_count':len(hybrid_pool),'candidate_onsite_count':len(onsite_pool),
            'local_raw_count':local_raw_count,
            'local_scored_count':len([j for j in scored if j.get('query_kind') == 'local' or j.get('category') in ('local_hybrid','local')]),
            'local_skipped_debug_count':len(local_skipped_debug),
            'skip_reason_counts':skip_reason_counts,
            'skipped_below_threshold':len(skipped)
        },
        'sections':{'remote':remote_top,'hybrid':hybrid_top,'onsite':onsite_top,'local_hybrid':local_top},
        'debug':{'local_skipped_top':local_skipped_debug},
        'jobs':top
    }
    LAST_JSON.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    report=OUTDIR / f"job-search-{dt.date.today().isoformat()}.md"
    lines=[f"# Job Search — {dt.date.today().isoformat()}", '', json.dumps(payload['stats'], indent=2), '']
    for heading, jobs in [
        ('Completely remote — best match + realistic interview odds', remote_top),
        (f'LI hybrid near {HOME_ZIP} — ranked separately', hybrid_top),
        (f'LI onsite near {HOME_ZIP} — ranked separately', onsite_top),
    ]:
        lines += [f"## {heading}", '']
        if not jobs:
            lines += ['No matches in this section today after commute/interview-chance filters.', '']
        for j in jobs:
            b=j['breakdown']
            lines += [
                f"### #{j['n']} — {j['title']} at {j['company']}",
                f"Score: {j['score']}/100",
                f"Interview chance proxy: {j['interview_chance']}",
                f"Profile: {b.get('score_profile', j.get('score_profile', ''))}",
                f"Fit: {b['fit']}/{b.get('fit_max', 60)} (keywords {b['keywords']}, salary {b['salary']}, location {b['location']}, seniority {b['seniority']})",
                f"Realism: {b['hireability']}/{b.get('hireability_max', 40)} (competition {b['competition']}/{b.get('competition_max', '?')}: {b.get('competition_reason', '')}; freshness {b['freshness']}/{b.get('freshness_max', '?')}: {b.get('freshness_reason', '')})",
                f"Bonus: {b['bonus']}/5 ({b['bonus_reason']})",
                f"Salary: {j['salary']}",
                f"Location: {j['location']}",
                f"Source: {j['source']} / {', '.join(j.get('sources_seen', []))}",
                f"Posted: {j.get('posted') or 'unknown'}",
                f"Keywords: {', '.join(j['keywords_matched'])}",
                f"URL: {j['url']}",
                ''
            ]
    report.write_text('\n'.join(lines), encoding='utf-8')

    print(render_media_digest(payload))
    return 0

if __name__ == '__main__':
    if '--preview-from-last' in sys.argv:
        raise SystemExit(preview_from_last())
    raise SystemExit(main())
