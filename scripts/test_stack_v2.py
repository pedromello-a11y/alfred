"""
Smoke test manual da stack v2.

Uso:
    python scripts/test_stack_v2.py https://SEU-APP.up.railway.app SEGREDO_OPCIONAL
"""

import json
import sys
import urllib.request


def post(url: str, payload: dict, secret: str | None = None):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if secret:
        req.add_header("X-Bridge-Secret", secret)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.status, resp.read().decode("utf-8")


def get(url: str):
    with urllib.request.urlopen(url, timeout=20) as resp:
        return resp.status, resp.read().decode("utf-8")


def main():
    if len(sys.argv) < 2:
        print("uso: python scripts/test_stack_v2.py BASE_URL [SECRET]")
        raise SystemExit(1)

    base_url = sys.argv[1].rstrip("/")
    secret = sys.argv[2] if len(sys.argv) > 2 else None

    checks = []

    status, body = get(base_url + "/health")
    checks.append(("GET /health", status, body[:300]))

    status, body = post(
        base_url + "/whatsapp/inbound",
        {
            "text": "teste stack v2 whatsapp inbound",
            "chat_id": "pedro-test",
            "message_id": "stackv2-whatsapp-1",
            "source": "whatsapp-test",
        },
        secret,
    )
    checks.append(("POST /whatsapp/inbound", status, body[:500]))

    status, body = post(
        base_url + "/internal/whatsapp/inbound",
        {
            "text": "teste stack v2 internal inbound",
            "chat_id": "pedro-test",
            "message_id": "stackv2-internal-1",
            "source": "whatsapp-web.js",
            "from_me": False,
            "is_group": False,
        },
        secret,
    )
    checks.append(("POST /internal/whatsapp/inbound", status, body[:500]))

    status, body = post(
        base_url + "/webhook",
        {
            "messages": [
                {
                    "id": "stackv2-webhook-1",
                    "type": "text",
                    "text": {"body": "teste stack v2 webhook"},
                    "from": "00000000000@s.whatsapp.net",
                    "from_me": True,
                    "source": "web",
                    "chat_id": "123@g.us",
                }
            ]
        },
    )
    checks.append(("POST /webhook", status, body[:500]))

    status, body = get(base_url + "/dashboard/state")
    checks.append(("GET /dashboard/state", status, body[:500]))

    status, body = get(base_url + "/dashboard/focus")
    checks.append(("GET /dashboard/focus", status, body[:500]))

    status, body = get(base_url + "/dashboard/tomorrow")
    checks.append(("GET /dashboard/tomorrow", status, body[:500]))

    for name, status, body in checks:
        print(name, status)
        print(body)
        print("-" * 80)


if __name__ == "__main__":
    main()
