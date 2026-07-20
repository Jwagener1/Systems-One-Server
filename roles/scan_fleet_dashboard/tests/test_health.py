from fastapi.testclient import TestClient

import main


def test_health_ok():
    client = TestClient(main.app)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
