import json, csv, time, urllib.request, urllib.error, os, sys

SP = os.environ['SP']
TOKEN = open(os.path.join(SP, 'estok2.txt'), encoding='utf-8').read()
TOKEN = TOKEN.strip() if ':' not in TOKEN else [l.split(':',1)[1].strip() for l in TOKEN.splitlines() if l.upper().startswith('ACCESS TOKEN')][0]
BASE = 'https://api.bigcommerce.com/stores/qet21urb3p/v3/catalog/products'

truly   = json.load(open(os.path.join(SP,'truly.json')))
matched = {r[0]: r[3] for r in json.load(open(os.path.join(SP,'matched.json')))}
excluded= set(json.load(open(os.path.join(SP,'excluded.json'))))

plan = []
for p in truly:
    if p['id'] in excluded:
        continue
    plan.append({'id': p['id'], 'sku': p.get('sku') or '', 'name': p['name'],
                 'old': 0, 'new': matched.get(p['id'], 0.2),
                 'src': 'ca/com' if p['id'] in matched else 'placeholder'})

# rollback file BEFORE any write
with open('es-weight-ROLLBACK.csv','w',newline='',encoding='utf-8-sig') as f:
    w = csv.writer(f); w.writerow(['product_id','sku','name','original_weight','written_weight','source'])
    for r in plan: w.writerow([r['id'],r['sku'],r['name'],r['old'],r['new'],r['src']])
print(f'rollback saved: {len(plan)} rows -> es-weight-ROLLBACK.csv', flush=True)

def req(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
        headers={'X-Auth-Token':TOKEN,'Content-Type':'application/json','Accept':'application/json'})
    with urllib.request.urlopen(r, timeout=60) as resp:
        return resp.status, json.loads(resp.read().decode())

ok = 0; failed = []
CHUNK = 10
for i in range(0, len(plan), CHUNK):
    batch = plan[i:i+CHUNK]
    payload = [{'id': r['id'], 'weight': r['new']} for r in batch]
    for attempt in (1,2,3):
        try:
            st, body = req('PUT', BASE, payload)
            ok += len(batch)
            break
        except urllib.error.HTTPError as e:
            txt = e.read().decode()[:160]
            if e.code == 429:
                time.sleep(int(e.headers.get('X-Rate-Limit-Time-Reset-Ms', 30000))/1000 + 1); continue
            if attempt == 3:
                failed.append((batch[0]['id'], e.code, txt))
            else:
                time.sleep(2)
        except Exception as e:
            if attempt == 3: failed.append((batch[0]['id'], 'ERR', str(e)[:160]))
            else: time.sleep(2)
    if (i//CHUNK) % 8 == 0:
        print(f'  ... {min(i+CHUNK,len(plan))}/{len(plan)}', flush=True)
    time.sleep(0.25)

print(f'\nWROTE OK: {ok}/{len(plan)}')
if failed:
    print('FAILURES:')
    for f_ in failed[:20]: print('  ', f_)
