#!/usr/bin/env python3
"""
Geocode voting stations via Nominatim (OpenStreetMap).
Rate-limited to 1 req/sec as required by Nominatim ToS.
Updates Supabase directly via REST API.
Run: python3 geocode.py
"""
import csv, time, json, urllib.request, urllib.parse, sys, os

SUPABASE_URL = 'https://fzuvrpbeogazshukhvll.supabase.co'
SUPABASE_KEY = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImZ6dXZycGJlb2dhenNodWtodmxsIiwicm9sZSI6ImFub24iLCJpYXQiOjE3OTM2Mzk3NzgsImV4cCI6MjEwOTIxNTc3OH0.WRZpGjgxmLkx2v7HWirhhmOG2XopK1Q-nDWv89uOsQs'
NOMINATIM    = 'https://nominatim.openstreetmap.org/search'
PROGRESS_FILE = 'geocode_progress.json'

HEADERS = {
    'User-Agent': 'VoteQueueSA/1.0 geocoder (votequeue.co.za)',
    'Accept': 'application/json',
}

def nominatim_search(query):
    params = urllib.parse.urlencode({'q': query, 'format': 'json', 'limit': 1, 'countrycodes': 'za'})
    url = f'{NOMINATIM}?{params}'
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            results = json.loads(r.read())
            if results:
                return float(results[0]['lat']), float(results[0]['lon'])
    except Exception as e:
        print(f'  Nominatim error: {e}')
    return None, None

def supabase_patch(station_id, lat, lng):
    url = f'{SUPABASE_URL}/rest/v1/voting_stations?id=eq.{station_id}'
    data = json.dumps({'lat': lat, 'lng': lng}).encode()
    req = urllib.request.Request(url, data=data, method='PATCH', headers={
        'apikey': SUPABASE_KEY,
        'Authorization': f'Bearer {SUPABASE_KEY}',
        'Content-Type': 'application/json',
        'Prefer': 'return=minimal',
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status in (200, 204)
    except Exception as e:
        print(f'  Supabase error: {e}')
        return False

def fetch_stations():
    """Fetch all stations without coords from Supabase."""
    url = f'{SUPABASE_URL}/rest/v1/voting_stations?lat=is.null&select=id,name,address,municipality,province&limit=25000'
    # Tell Supabase we accept more than the default 1000 rows

    req = urllib.request.Request(url, headers={
        'apikey': SUPABASE_KEY,
        'Authorization': f'Bearer {SUPABASE_KEY}',
        'Accept': 'application/json',
        'Range-Unit': 'items',
        'Range': '0-25000',
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

def load_progress():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE) as f:
            return set(json.load(f))
    return set()

def save_progress(done):
    with open(PROGRESS_FILE, 'w') as f:
        json.dump(list(done), f)

def main():
    print('Fetching stations without coordinates from Supabase...')
    stations = fetch_stations()
    print(f'Found {len(stations)} stations to geocode')

    done = load_progress()
    print(f'Already done: {len(done)}')

    total   = len(stations)
    success = 0
    fail    = 0

    for i, s in enumerate(stations):
        sid = s['id']
        if sid in done:
            continue

        # Build search query: "Station Name, Municipality, Province, South Africa"
        name    = s.get('name', '')
        muni    = s.get('municipality', '')
        addr    = s.get('address', '')
        prov    = s.get('province', '')

        # Try full address first, fall back to name + municipality
        query1 = f'{addr}, {muni}, {prov}, South Africa'
        query2 = f'{name}, {muni}, {prov}, South Africa'

        lat, lng = nominatim_search(query1)
        time.sleep(1)  # Nominatim ToS: max 1 req/sec

        if lat is None:
            lat, lng = nominatim_search(query2)
            time.sleep(1)

        if lat is not None:
            if supabase_patch(sid, lat, lng):
                success += 1
                done.add(sid)
            else:
                fail += 1
        else:
            fail += 1
            done.add(sid)  # Mark as attempted so we don't retry

        # Save progress every 100 stations
        if (i + 1) % 100 == 0:
            save_progress(done)
            pct = (i + 1) / total * 100
            eta_hrs = ((total - i - 1) * 2) / 3600  # ~2s per station
            print(f'  [{i+1}/{total}] {pct:.1f}% | found: {success} | failed: {fail} | ETA: {eta_hrs:.1f}h')
            sys.stdout.flush()

    save_progress(done)
    print(f'\nDone! Geocoded: {success} | Not found: {fail}')

if __name__ == '__main__':
    main()
