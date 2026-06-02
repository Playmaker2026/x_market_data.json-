Y
"""
X Market Data Pipeline — GitHub Actions version
================================================
Runs hourly in the cloud (no VPN, no local machine needed).
Reads accounts from accounts.json.
Calls Xquik API for each account.
Pushes results to GitHub Gist for the dashboard to read.
 
To add/remove accounts: edit accounts.json and commit.
All secrets come from GitHub Actions environment variables.
"""
 
import json
import os
import re
import sys
import logging
import requests
from datetime import datetime, timezone
 
# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout),
              logging.FileHandler('x_pipeline.log', encoding='utf-8')]
)
log = logging.getLogger(__name__)
 
# ── Config from environment (GitHub Actions secrets) ─────────────────────────
XQUIK_KEY  = os.environ.get('XQUIK_API_KEY', '')
GH_TOKEN   = os.environ.get('GITHUB_TOKEN', '')
GIST_ID    = os.environ.get('GIST_ID', '')
GIST_FILE  = 'x_market_data.json'
XQUIK_BASE = 'https://xquik.com/api/v1'
 
# ── Ticker extraction ─────────────────────────────────────────────────────────
TICKER_RE = re.compile(r'\$([A-Z]{1,5})(?:[^A-Z]|$)')
IGNORE = {
    'THE','AND','FOR','ARE','BUT','NOT','YOU','ALL','CAN','WAS','ONE',
    'OUT','DAY','GET','HAS','HOW','ITS','MAY','NEW','NOW','SEE','TWO',
    'WHO','DID','LET','PUT','SAY','SHE','TOO','USE','WAY','USD','ETF',
    'IPO','ATH','CEO','CFO','EPS','IMO','FYI','EOD','EOW','AKA','ETC',
    'NEXT','WEEK','THIS','THAT','WITH','FROM','INTO','HAVE','MORE','BEEN',
    'WHEN','OVER','LONG','SHORT','SELL','STOP','BULL','BEAR','CALL','PUTS',
    'HOLD','HIGH','LAST','OPEN','STAY','TOOK','PLAY','PLAN','LOOK','LIKE',
    'JUST','BEEN','WILL','SOME','ONLY','THAN','THEN','THEM','THEY','ALSO',
}
 
def extract_tickers(text):
    if not text:
        return []
    found  = TICKER_RE.findall(text.upper())
    unique = []
    seen   = set()
    for t in found:
        if t not in IGNORE and t not in seen and len(t) >= 2:
            unique.append(t)
            seen.add(t)
    return unique
 
 
# ── Xquik API ─────────────────────────────────────────────────────────────────
def xquik_session():
    s = requests.Session()
    s.headers.update({'x-api-key': XQUIK_KEY, 'Content-Type': 'application/json'})
    return s
 
 
def get_full_tweet(session, tweet_id):
    """Fetch full untruncated tweet text by ID."""
    try:
        url = f'{XQUIK_BASE}/x/tweets/{tweet_id}'
        r = session.get(url, timeout=15)
        if r.status_code == 200:
            data = r.json()
            # Try all possible full text fields
            full_text = (data.get('fullText')
                      or data.get('full_text')
                      or data.get('text')
                      or '')
            if full_text and len(full_text) > 200:
                log.info(f'  Full tweet fetched: {len(full_text)} chars')
                return full_text
    except Exception as e:
        log.warning(f'  Could not fetch full tweet: {e}')
    return None
 
 
def search_tweets(session, handle, keywords, max_tweets=10):
    """Search for tweets from @handle containing any keyword."""
    kw_part = ' OR '.join(f'"{kw}"' for kw in keywords)
    query   = f'from:{handle} ({kw_part})'
    log.info(f'  Query: {query}')
 
    url = f'{XQUIK_BASE}/x/tweets/search'
    try:
        r = session.get(url, params={'q': query, 'limit': max_tweets}, timeout=30)
        log.info(f'  Status: {r.status_code}')
        if r.status_code == 400:
            log.warning(f'  400 error: {r.text}')
            r = session.get(url, params={'q': f'from:{handle}', 'limit': max_tweets}, timeout=30)
            log.info(f'  Retry status: {r.status_code}')
        r.raise_for_status()
        data   = r.json()
        tweets = (data.get('tweets')
               or data.get('results')
               or data.get('data')
               or [])
        if keywords:
            kw_lower = [k.lower() for k in keywords]
            filtered = [t for t in tweets
                       if any(kw in (t.get('text') or '').lower() for kw in kw_lower)]
            if filtered:
                tweets = filtered
        log.info(f'  Found {len(tweets)} matching tweets')
        return tweets
    except Exception as e:
        log.error(f'  Xquik error: {e}')
        return []
 
 
def normalize_tweet(raw, handle):
    """Normalize raw Xquik tweet into consistent shape."""
    text     = raw.get('text') or raw.get('fullText') or raw.get('content') or ''
    tweet_id = raw.get('id') or raw.get('tweetId') or raw.get('tweet_id') or ''
    author   = raw.get('author') or {}
    username = author.get('username') or handle
    url      = (raw.get('url')
             or raw.get('tweetUrl')
             or (f'https://x.com/{username}/status/{tweet_id}' if tweet_id else ''))
    attach   = raw.get('attachments') or {}
    media    = (attach.get('mediaUrls')
             or raw.get('mediaUrls')
             or raw.get('media_urls')
             or [])
    metrics  = raw.get('publicMetrics') or raw.get('metrics') or {}
    return {
        'id':         tweet_id,
        'url':        url,
        'text':       text,
        'date':       raw.get('createdAt') or raw.get('created_at') or raw.get('date') or '',
        'likes':      metrics.get('likeCount')    or raw.get('likeCount')    or 0,
        'retweets':   metrics.get('retweetCount') or raw.get('retweetCount') or 0,
        'views':      metrics.get('viewCount')    or raw.get('viewCount')    or 0,
        'media_urls': media,
    }
 
 
# ── GitHub Gist ───────────────────────────────────────────────────────────────
def get_gist():
    """Fetch existing Gist content to preserve history."""
    try:
        r = requests.get(
            f'https://api.github.com/gists/{GIST_ID}',
            headers={'Authorization': f'token {GH_TOKEN}',
                     'Accept': 'application/vnd.github.v3+json'},
            timeout=15
        )
        r.raise_for_status()
        files = r.json().get('files', {})
        if GIST_FILE in files:
            return json.loads(files[GIST_FILE].get('content', '{}'))
    except Exception as e:
        log.warning(f'Could not fetch existing Gist: {e}')
    return {}
 
 
def update_gist(content_str):
    """Push updated JSON to GitHub Gist."""
    try:
        r = requests.patch(
            f'https://api.github.com/gists/{GIST_ID}',
            headers={'Authorization': f'token {GH_TOKEN}',
                     'Accept': 'application/vnd.github.v3+json'},
            json={'files': {GIST_FILE: {'content': content_str}}},
            timeout=30
        )
        r.raise_for_status()
        log.info(f'Gist updated: https://gist.github.com/Playmaker2026/{GIST_ID}')
        return True
    except Exception as e:
        log.error(f'Gist update failed: {e}')
        return False
 
 
# ── Main ──────────────────────────────────────────────────────────────────────
def run():
    log.info('=' * 60)
    log.info('X Market Data Pipeline — GitHub Actions')
    log.info(f'Time: {datetime.now(timezone.utc).isoformat()}')
    log.info('=' * 60)
 
    # Validate secrets
    errors = []
    if not XQUIK_KEY:  errors.append('XQUIK_API_KEY secret not set')
    if not GH_TOKEN:   errors.append('GITHUB_TOKEN / GIST_TOKEN secret not set')
    if not GIST_ID:    errors.append('GIST_ID secret not set')
    if errors:
        for e in errors:
            log.error(f'Config error: {e}')
        sys.exit(1)
 
    # Load accounts
    accounts_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'accounts.json')
    try:
        with open(accounts_path, 'r', encoding='utf-8') as f:
            accounts = json.load(f)
        log.info(f'Loaded {len(accounts)} accounts from accounts.json')
    except Exception as e:
        log.error(f'Could not load accounts.json: {e}')
        sys.exit(1)
 
    # Load existing data to preserve history
    existing = get_gist()
 
    output = {
        '_meta': {
            'updated_at':         datetime.now(timezone.utc).isoformat(),
            'updated_by':         'GitHub Actions / x_pipeline.py',
            'accounts_processed': len(accounts),
            'pipeline_version':   '2.0',
        },
        'accounts': existing.get('accounts', {})
    }
 
    session = xquik_session()
 
    for acct in accounts:
        handle   = acct.get('handle', '').strip()
        label    = acct.get('label', handle)
        keywords = acct.get('keywords', [])
        max_tw   = acct.get('max_tweets', 10)
        do_ticks = acct.get('extract_tickers', True)
 
        if not handle:
            continue
 
        log.info(f'\nProcessing @{handle} ({label})')
        log.info(f'  Keywords: {keywords}')
 
        tweets_raw = search_tweets(session, handle, keywords, max_tw)
 
        if not tweets_raw:
            log.warning(f'  No tweets found — keeping previous data')
            continue
 
        latest    = normalize_tweet(tweets_raw[0], handle)
 
        # Try to get full untruncated text (IBD 50 lists are long)
        tweet_id = latest.get('id') or tweets_raw[0].get('id') or tweets_raw[0].get('tweetId') or ''
        if tweet_id:
            full_text = get_full_tweet(session, tweet_id)
            if full_text:
                latest['text'] = full_text
 
        tickers   = extract_tickers(latest['text']) if do_ticks else []
 
        log.info(f'  Latest date: {latest["date"]}')
        log.info(f'  Tickers extracted: {tickers}')
        log.info(f'  Media URLs: {len(latest["media_urls"])}')
 
        # Keep up to 8 recent posts for history
        prev = output['accounts'].get(handle, {}).get('recent_posts', [])
        seen_ids = {p.get('id') for p in prev}
        if latest['id'] and latest['id'] not in seen_ids:
            prev.insert(0, {**latest, 'tickers': tickers})
        prev = prev[:8]
 
        output['accounts'][handle] = {
            'handle':       handle,
            'label':        label,
            'keywords':     keywords,
            'frequency':    acct.get('frequency', 'hourly'),
            'last_updated': datetime.now(timezone.utc).isoformat(),
            'latest':       {**latest, 'tickers': tickers},
            'recent_posts': prev,
        }
 
    # Push to Gist
    content_str = json.dumps(output, indent=2, ensure_ascii=False)
    log.info('\nPushing to GitHub Gist...')
    update_gist(content_str)
 
    log.info('\n' + '=' * 60)
    log.info('Pipeline completed successfully')
    log.info('=' * 60)
 
 
if __name__ == '__main__':
    run()
