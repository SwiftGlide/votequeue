#!/usr/bin/env python3
"""
VoteQueue SA — Nominatim Geocoder
Fetches stations where lat IS NULL and last_checked IS NULL (never attempted),
stamps every row with last_checked regardless of outcome, and writes lat/lng
on a hit.  Re-runs only touch stations that haven't been attempted yet, so
the loop is safe to interrupt and restart.

Usage:
  python3 geocode.py                          # all provinces, never-checked only
  python3 geocode.py --province "Gauteng"     # single province
  python3 geocode.py --recheck                # retry all null-lat rows
  python3 geocode.py --recheck --province "Western Cape"

~40 min total when run as 9 parallel GitHub Actions jobs (one per province).
"""
import time, json, urllib.request, urllib.parse, sys
from datetime import datetime, timezone

import os
SUPABASE_URL = os.environ.get('SUPABASE_URL', 'https://fzuvrpbeogazshukhvll.supabase.co')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY', 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImZ6dXZycGJlb2dhenNodWtodmxsIiwicm9sZSI6ImFub24iLCJpYXQiOjE3OTM2Mzk3NzgsImV4cCI6MjEwOTIxNTc3OH0.WRZpGjgxmLkx2v7HWirhhmOG2XopK1Q-nDWv89uOsQs')
NOMINATIM    = 'https://nominatim.openstreetmap.org/search'

NOM_HEADERS = {
    'User-Agent': 'VoteQueueSA/1.0 (votequeue.co.za)',
    'Accept': 'application/json',
}

RECHECK  = '--recheck' in sys.argv

# --province "Gauteng"  (optional)
PROVINCE = None
if '--province' in sys.argv:
    idx = sys.argv.index('--province')
    if idx + 1 < len(sys.argv):
        PROVINCE = sys.argv[idx + 1]

# Module-level consecutive error counter
nom_errors = 0


# ── Nominatim ────────────────────────────────────────────────────────────────

def _nom_query(q):
    """Fire one Nominatim query; returns (lat, lng) or (None, None)."""
    global nom_errors
    params = urllib.parse.urlencode({
        'q': q, 'format': 'json', 'limit': 1, 'countrycodes': 'za'
    })
    req = urllib.request.Request(f'{NOMINATIM}?{params}', headers=NOM_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
            nom_errors = 0   # reset on any successful response
            if data:
                return float(data[0]['lat']), float(data[0]['lon'])
    except Exception as e:
        nom_errors += 1
        print(f'  nominatim error (#{nom_errors}): {e}')
    return None, None


def geocode(name, province):
    """
    Try two queries in order, stopping at first hit:
      1. Station name + province
      2. Station name alone
    Each costs 1 req/s — caller must sleep between stations.
    """
    attempts = []
    if name and province:
        attempts.append(f'{name}, {province}, South Africa')
    if name:
        attempts.append(f'{name}, South Africa')

    for i, q in enumerate(attempts):
        if i > 0:
            time.sleep(1)   # Nominatim ToS: ≤ 1 req/s between queries
        lat, lng = _nom_query(q)
        if lat is not None:
            print(f'  ✓ hit on attempt {i+1}: {q[:60]}')
            return lat, lng

    return None, None


# ── Supabase helpers ─────────────────────────────────────────────────────────

def _sb_headers(extra=None):
    h = {
        'apikey': SUPABASE_KEY,
        'Authorization': f'Bearer {SUPABASE_KEY}',
        'Content-Type': 'application/json',
        'Prefer': 'return=minimal',
    }
    if extra:
        h.update(extra)
    return h


def fetch_stations(recheck=False, province=None):
    """
    Fetch stations where lat IS NULL.
    Normal mode:  also require last_checked IS NULL.
    Recheck mode: all null-lat rows including prev failures.
    Province:     filter to one province when set.
    Sorted Z–A by name. Paginates in batches of 1000.
    """
    filters = 'lat=is.null'
    if not recheck:
        filters += '&last_checked=is.null'
    if province:
        filters += f'&province=eq.{urllib.parse.quote(province)}'

    all_stations = []
    page, page_size = 0, 1000
    while True:
        start = page * page_size
        end   = start + page_size - 1
        url   = (f'{SUPABASE_URL}/rest/v1/voting_stations'
                 f'?{filters}&select=id,name,province&order=name.desc')
        req   = urllib.request.Request(url, headers={
            **_sb_headers(),
            'Accept': 'application/json',
            'Range-Unit': 'items',
            'Range': f'{start}-{end}',
        })
        with urllib.request.urlopen(req, timeout=30) as r:
            batch = json.loads(r.read())
        if not batch:
            break
        all_stations.extend(batch)
        print(f'  Page {page+1}: {len(batch)} rows (total so far: {len(all_stations)})')
        if len(batch) < page_size:
            break
        page += 1
    return all_stations


def patch(station_id, lat, lng):
    """Write coords and stamp last_checked = now."""
    now  = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    body = {'last_checked': now}
    if lat is not None:
        body['lat'] = lat
        body['lng'] = lng

    url = f'{SUPABASE_URL}/rest/v1/voting_stations?id=eq.{station_id}'
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method='PATCH',
        headers=_sb_headers()
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status in (200, 204)
    except Exception as e:
        print(f'  supabase patch error: {e}')
        return False


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    mode = 'RECHECK (all null-lat)' if RECHECK else 'NORMAL (never-checked only)'
    scope = f'province={PROVINCE}' if PROVINCE else 'all provinces'
    print(f'Mode: {mode} | Scope: {scope}\n')

    stations = fetch_stations(recheck=RECHECK, province=PROVINCE)
    total    = len(stations)
    print(f'\n{total} stations to process\n')

    if total == 0:
        print('Nothing to do.')
        return

    success = 0
    fail    = 0
    start_t = time.time()

    for i, s in enumerate(stations):
        sid  = s['id']
        name = (s.get('name')     or '').strip()
        prov = (s.get('province') or '').strip()

        lat, lng = geocode(name, prov)

        time.sleep(1)   # Nominatim ToS: ≤ 1 req/s between stations

        ok = patch(sid, lat, lng)   # always stamps last_checked
        if lat is not None and ok:
            success += 1
        else:
            fail += 1

        done = i + 1
        if done % 50 == 0 or done == total:
            elapsed  = time.time() - start_t
            rate     = done / elapsed
            eta_min  = ((total - done) / rate / 60) if rate > 0 else 0
            pct      = done / total * 100
            ts       = datetime.now(timezone.utc).strftime('%H:%M:%SZ')
            print(
                f'[{ts}] [{done}/{total}] {pct:.1f}% | '
                f'found: {success} | failed: {fail} | nom_errors: {nom_errors} | '
                f'ETA: {eta_min:.0f} min',
                flush=True
            )

            # Bail if Nominatim looks blocked
            if nom_errors >= 10:
                print('\n⚠️  10+ consecutive Nominatim errors — likely rate-limited or blocked.')
                print('Wait a few minutes then restart. last_checked is stamped up to this point.')
                return

    elapsed = time.time() - start_t
    print(f'\nDone in {elapsed/60:.1f} min — found: {success} | not found: {fail}')


if __name__ == '__main__':
    main()
