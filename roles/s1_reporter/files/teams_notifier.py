import json
import time
import urllib.request
import urllib.error


def post_to_teams(webhook_url, card, max_retries=3, backoff_seconds=2.0):
    """POST an Adaptive Card to a Teams Power Automate webhook.

    Returns True on a 2xx response, False if every attempt fails.
    Never raises — network/HTTP errors are logged and treated as a failed attempt.
    """
    envelope = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": card,
            }
        ],
    }
    payload = json.dumps(envelope).encode("utf-8")

    for attempt in range(1, max_retries + 1):
        req = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if 200 <= resp.status < 300:
                    return True
                print(f"⚠️ Teams webhook returned status {resp.status} (attempt {attempt}/{max_retries})")
        except (urllib.error.URLError, OSError) as e:
            print(f"⚠️ Teams webhook POST failed: {e} (attempt {attempt}/{max_retries})")

        if attempt < max_retries:
            time.sleep(backoff_seconds * attempt)

    print(f"❌ Teams webhook POST failed after {max_retries} attempts, dropping card:")
    print(json.dumps(envelope))
    return False
