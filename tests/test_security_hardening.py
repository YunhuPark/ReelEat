import importlib
from unittest.mock import patch

from fastapi.testclient import TestClient

with patch.dict("os.environ", {"GEMINI_API_KEY": "", "INSTAGRAM_COOKIES_B64": ""}):
    main = importlib.import_module("main")


def _client():
    return TestClient(main.app)


def test_debug_routes_disabled_by_default():
    paths = {getattr(route, "path", "") for route in main.app.routes}
    assert "/debug_search" not in paths
    assert "/debug_menu/{place_id}" not in paths
    response = _client().post("/debug_search", json={"name": "x"})
    assert response.status_code == 404


def test_health_does_not_expose_cookie_details(monkeypatch):
    monkeypatch.setattr(main, "_check_cookie_status", lambda: {
        "status": "expired", "days_left": -12})
    body = _client().get("/health").json()
    assert body["instagram_cookies"] == "degraded"
    assert body["status"] == "degraded"
    assert "expires_at" not in body and "days_left" not in str(body)


def test_api_token_required_when_configured(monkeypatch):
    monkeypatch.setattr(main, "API_TOKEN", "test-token")
    client = _client()
    body = {"url": "https://www.instagram.com/reel/abc/"}
    assert client.post("/analyze", json=body).status_code == 401
    assert client.post(
        "/analyze", json=body, headers={"X-API-Token": "wrong"}).status_code == 401
    assert client.post(
        "/review_summary", json={"name": "x"}).status_code == 401


def test_rate_limit_per_ip(monkeypatch):
    monkeypatch.setattr(main, "API_TOKEN", "")
    monkeypatch.setattr(main, "RATE_LIMIT_PER_MINUTE", 2)
    monkeypatch.setattr(main, "RATE_LIMIT_GLOBAL_PER_MINUTE", 1000)
    monkeypatch.setattr(main, "get_reel_info", lambda url: (None, None, None))
    main._rate_buckets.clear()
    client = _client()
    body = {"url": "https://www.instagram.com/reel/abc/"}
    statuses = [client.post("/analyze", json=body).status_code for _ in range(4)]
    assert statuses[:2] == [200, 200]
    assert statuses[2:] == [429, 429]
    main._rate_buckets.clear()
