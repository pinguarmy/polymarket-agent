import requests, json, time, csv, sys, os
sys.path.insert(0, '.')

# Load key
key = None
with open('./.env') as f:
    for line in f:
        if line.startswith('DUNE_API_KEY='):
            key = line.split('=', 1)[1].strip()

s = requests.Session()
s.headers.update({'X-Dune-Api-Key': key})

eid = '01KQHQ73NBQQTCA27JZGYMEP0G'
print(f"Downloading results from execution {eid}...")

all_rows = []
offset = 0
page = 1

while True:
    resp = s.get(f'https://api.dune.com/api/v1/execution/{eid}/results',
                 params={'limit': 1000, 'offset': offset}, timeout=30)
    
    if resp.status_code != 200:
        print(f"  Page {page}: HTTP {resp.status_code}")
        break
    
    data = resp.json()
    rows = data.get('result', {}).get('rows', [])
    if not rows:
        break
    
    all_rows.extend(rows)
    total = data.get('result', {}).get('metadata', {}).get('total_row_count', '?')
    print(f"  Page {page}: {len(rows)} rows (total: {len(all_rows)}/{total})", end='\r')
    
    next_uri = data.get('next_uri')
    if not next_uri:
        break
    
    offset += 1000
    page += 1
    time.sleep(0.3)

print(f"\nDownloaded {len(all_rows)} rows total")

# Save
output = './data/dune_april_full.csv'
with open(output, 'w', newline='') as f:
    writer = csv.DictWriter(f, fieldnames=all_rows[0].keys())
    writer.writeheader()
    writer.writerows(all_rows)

print(f"Saved to {output}")
print(f"File size: {os.path.getsize(output)/1024/1024:.1f} MB")
