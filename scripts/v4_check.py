import json, sys, urllib.request

def post(url, payload, secret=None):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method='POST')
    req.add_header('Content-Type', 'application/json')
    if secret:
        req.add_header('X-Bridge-Secret', secret)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.status, resp.read().decode()

def get(url):
    with urllib.request.urlopen(url, timeout=20) as resp:
        return resp.status, resp.read().decode()

base = sys.argv[1].rstrip('/')
secret = sys.argv[2] if len(sys.argv) > 2 else None
checks = []
checks.append(('GET /health',) + get(base + '/health'))
checks.append(('GET /dashboard/state',) + get(base + '/dashboard/state'))
checks.append(('GET /dashboard/focus',) + get(base + '/dashboard/focus'))
checks.append(('GET /dashboard/tomorrow',) + get(base + '/dashboard/tomorrow'))
checks.append(('POST /whatsapp/inbound',) + post(base + '/whatsapp/inbound', {'text':'teste v4','chat_id':'pedro-test','message_id':'v4-whatsapp-1','source':'whatsapp-test'}, secret))
for name, status, body in checks:
    print(name, status)
    print(body[:600])
    print('-'*80)
