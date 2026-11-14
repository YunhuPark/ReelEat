import importlib
from unittest.mock import patch

from fastapi.testclient import TestClient

with patch.dict("os.environ", {"GEMINI_API_KEY": "", "INSTAGRAM_COOKIES_B64": ""}):
    main = importlib.import_module("main")


def test_health_never_calls_model(monkeypatch):
    class Models:
        def generate_content(self, **kwargs):
            raise AssertionError("health must not contact a paid provider")
    class Client:
        models = Models()
    monkeypatch.setattr(main, "client", Client())
    response = TestClient(main.app).get("/health")
    assert response.status_code == 200
    assert response.json()["gemini"] == "configured"


def test_cached_extraction_resolves_branch_for_each_location(monkeypatch):
    main._analyze_cache.clear()
    extraction_calls = []
    def info(url):
        extraction_calls.append(url)
        return "맛있는 체인 식당", "", "thumbnail"
    monkeypatch.setattr(main, "get_reel_info", info)
    monkeypatch.setattr(main, "analyze_text_with_gemini", lambda *a, **k: {
        "restaurants": [{"name": "chain", "location": "Korea", "menu": []}]})
    monkeypatch.setattr(main, "get_coordinates", lambda *a, **k: {
        "lat": k["user_lat"], "lng": k["user_lng"], "name": str(k["user_lat"])})
    monkeypatch.setattr(main, "get_naver_place_id_by_search", lambda *a, **k: None)
    url = "https://www.instagram.com/reel/test123/"
    busan = main.analyze_reel(main.AnalysisRequest(url=url, user_lat=35, user_lng=129))
    seoul = main.analyze_reel(main.AnalysisRequest(url=url, user_lat=37, user_lng=127))
    assert busan["restaurants"][0]["coordinates"]["lat"] == 35
    assert seoul["restaurants"][0]["coordinates"]["lat"] == 37
    assert busan["restaurants"][0]["data"]["name"] == "35.0"
    assert extraction_calls == [url]
