"""
Smoke test manual dos endpoints da versão rebuild.

Uso:
    python scripts/test_rebuild_endpoints.py https://SEU-APP.up.railway.app SEGREDO_OPCIONAL
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
        print("uso: python scripts/test_rebuild_endpoints.py BASE_URL [SECRET]")
        raise SystemExit(1)

    base_url = sys.argv[1].rstrip("/")
    secret = sys.argv[2] if len(sys.argv) > 2 else None

    status, body = get(base_url + "/health")
    print("GET /health", status, body[:300])

    status, body = post(
        base_url + "/whatsapp/inbound",
        {
            "text": "teste rebuild whatsapp inbound",
            "chat_id": "pedro-test",
            "message_id": "rebuild-whatsapp-1",
            "source": "whatsapp-test",
        },
        secret,
    )
    print("POST /whatsapp/inbound", status, body[:500])

    status, body = post(
        base_url + "/internal/whatsapp/inbound",
        {
            "text": "teste rebuild internal inbound",
            "chat_id": "pedro-test",
            "message_id": "rebuild-internal-1",
            "source": "whatsapp-web.js",
            "from_me": False,
            "is_group": False,
        },
        secret,
    )
    print("POST /internal/whatsapp/inbound", status, body[:500])

    status, body = post(
        base_url + "/webhook",
        {
            "messages": [
                {
                    "id": "rebuild-webhook-1",
                    "type": "text",
                    "text": {"body": "teste rebuild webhook"},
                    "from": "00000000000@s.whatsapp.net",
                    "from_me": True,
                    "source": "web",
                    "chat_id": "123@g.us",
                }
            ]
        },
    )
    print("POST /webhook", status, body[:500])


if __name__ == "__main__":
    main()
