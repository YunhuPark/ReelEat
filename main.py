from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from dotenv import load_dotenv
import uvicorn
import os
import re

load_dotenv()
import json
import glob
import shutil
import tempfile
import subprocess
import time
import threading
import requests
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from google import genai
from google.genai import types

api_key = os.environ.get("GEMINI_API_KEY")
client = None
if api_key:
    client = genai.Client(api_key=api_key)

NAVER_SEARCH_CLIENT_ID = os.environ.get("NAVER_CLIENT_ID", "")
NAVER_SEARCH_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")

app = FastAPI()

# ── 동시 요청 제한 (영상 분석 최대 3개 동시) ──────────────────────────────────────
_analyze_semaphore = threading.Semaphore(3)

# ── URL 결과 캐시 (24시간 TTL, 최대 100개) ────────────────────────────────────────
_analyze_cache: dict[str, tuple[dict, datetime]] = {}
_CACHE_TTL = timedelta(hours=24)
_cache_lock = threading.Lock()

def _get_cache(url: str) -> dict | None:
    with _cache_lock:
        entry = _analyze_cache.get(url)
        if entry:
            result, ts = entry
            if datetime.now() - ts < _CACHE_TTL:
                return result
            del _analyze_cache[url]
    return None

def _set_cache(url: str, result: dict):
    with _cache_lock:
        if len(_analyze_cache) >= 100:
            oldest = min(_analyze_cache, key=lambda k: _analyze_cache[k][1])
            del _analyze_cache[oldest]
        _analyze_cache[url] = (result, datetime.now())

# ── Instagram URL 검증 ────────────────────────────────────────────────────────
_INSTAGRAM_URL_RE = re.compile(
    r'https?://(?:www\.)?instagram\.com/(?:reel|p|tv)/[A-Za-z0-9_-]+'
)

def _is_valid_instagram_url(url: str) -> bool:
    return bool(_INSTAGRAM_URL_RE.match(url.strip()))

class AnalysisRequest(BaseModel):
    url: str
    user_lat: float | None = None
    user_lng: float | None = None

class ReviewRequest(BaseModel):
    name: str
    address: str | None = None

import math
import base64
import yt_dlp

COOKIE_FILE = os.path.join(os.path.dirname(__file__), "cookies.txt")

# Railway 배포 시 환경변수에서 쿠키 복원
_cookies_b64 = os.environ.get("INSTAGRAM_COOKIES_B64")
if _cookies_b64 and not os.path.exists(COOKIE_FILE):
    try:
        with open(COOKIE_FILE, "w", encoding="utf-8") as _f:
            _f.write(base64.b64decode(_cookies_b64).decode("utf-8"))
        print("cookies.txt restored from env var")
    except Exception as _e:
        print(f"cookies.txt restore error: {_e}")

CATEGORY_MAP = {
    "한식": "한식", "분식": "분식", "국밥": "한식", "삼겹살": "한식", "치킨": "한식",
    "일식": "일식", "초밥": "일식", "라멘": "일식", "스시": "일식", "돈카츠": "일식",
    "중식": "중식", "중국": "중식",
    "양식": "양식", "이탈리안": "양식", "파스타": "양식", "피자": "양식",
    "스테이크": "양식", "멕시칸": "양식", "햄버거": "양식",
    "카페": "카페/디저트", "디저트": "카페/디저트", "베이커리": "카페/디저트", "빵집": "카페/디저트",
    "술집": "술집/바", "주점": "술집/바", "이자카야": "술집/바", "포차": "술집/바",
    "패스트푸드": "패스트푸드",
}

def map_naver_category(raw: str) -> str | None:
    if not raw:
        return None
    for key, val in CATEGORY_MAP.items():
        if key in raw:
            return val
    return None

def extract_place_id(link: str) -> str | None:
    if not link:
        return None
    match = re.search(r'/(?:place|restaurant|cafe|hotel|beauty|hospital)/(\d{7,15})', link)
    return match.group(1) if match else None

def _yt_dlp_attempts() -> list[dict]:
    attempts = []
    if os.path.exists(COOKIE_FILE):
        attempts.append({"cookiefile": COOKIE_FILE, "_label": "cookies.txt"})
    attempts.append({"cookiesfrombrowser": ("chrome",), "_label": "chrome"})
    attempts.append({"cookiesfrombrowser": ("edge",), "_label": "edge"})
    return attempts


# ── 1. 캡션 + 위치 태그 + 썸네일 추출 ────────────────────────────────────────────

def get_reel_info(url: str) -> tuple[str | None, str | None, str | None]:
    """Returns (text_content, location_tag, thumbnail_url)"""
    for attempt in _yt_dlp_attempts():
        label = attempt.pop("_label")
        try:
            print(f"Info extraction: {url} (via {label})")
            ydl_opts = {"quiet": True, "no_warnings": True, **attempt}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                description = info.get('description', '')
                title = info.get('title', '')
                text = f"Title: {title}\nDescription: {description}"

                location_tag = info.get('location')
                if isinstance(location_tag, dict):
                    location_tag = location_tag.get('name')

                thumbnail_url = info.get('thumbnail')
                if not thumbnail_url:
                    thumbs = info.get('thumbnails', [])
                    if thumbs:
                        thumbnail_url = thumbs[-1].get('url')

                print(f"Text len={len(text)}, geotag={location_tag}, thumb={'yes' if thumbnail_url else 'no'}")
                return text, location_tag or None, thumbnail_url or None
        except Exception as e:
            print(f"yt-dlp error ({label}): {e}")
            continue
    return None, None, None


# ── 2. 캡션 → Gemini 분석 (카테고리 포함) ────────────────────────────────────────

def _extract_location_from_hashtags(text: str) -> str | None:
    """#연남동맛집, #홍대카페 등 위치 해시태그에서 동네명 추출"""
    # 위치 + 맛집/카페/식당 패턴: #연남동맛집 → 연남동
    matches = re.findall(r'#([가-힣]{2,8})(?:맛집|카페|레스토랑|식당|맛스타그램|먹스타그램|음식|핫플)', text)
    if matches:
        return matches[0]
    # 단독 동네 해시태그: #연남동, #성수동, #강남구 등
    matches2 = re.findall(r'#([가-힣]{2,6}[동구로])\b', text)
    if matches2:
        return matches2[0]
    return None


def analyze_text_with_gemini(text: str, location_hint: str = None) -> dict:
    if not client:
        return {"error": "Gemini API Key not set"}

    hint_line = (f'\nLOCATION HINT (from hashtags): "{location_hint}" — '
                 f'use as location for any restaurant if no explicit address is found'
                 if location_hint else "")
    prompt = f"""
ACT AS A DATA EXTRACTION TOOL. DO NOT USE YOUR OWN KNOWLEDGE.
EXTRACT INFORMATION ONLY FROM THE TEXT BELOW. DO NOT INFER OR GUESS.

TEXT:
"{text}"{hint_line}

STRICT RULES:
- ONLY include a restaurant if its NAME is EXPLICITLY written in the text (e.g., after "매장명:", "식당:", or as a clearly identified store name)
- Location hashtags like #연남동맛집 or #홍대맛집 are NOT restaurant names — ignore them
- If the text shows "매장명: X", extract ONLY that restaurant (unless text also explicitly names others separately)
- DO NOT use your own knowledge to add restaurants that are not named in the text
- When uncertain whether something is a restaurant name, leave it out

For each EXPLICITLY NAMED restaurant:
1. name: exact name as written (from "매장명:", store sign text, or explicit naming)
2. location: Korean street address if stated; use LOCATION HINT if no address given; district/landmark otherwise
3. menu: food/drink items and prices ONLY if explicitly listed in the text
4. category: ONE of [한식, 일식, 중식, 양식, 카페/디저트, 술집/바, 분식, 패스트푸드, 기타]
   - 양식: 피자, 파스타, 스테이크, 버거, 샌드위치, 브런치, 이탈리안, 멕시칸, 양식 요리
   - 한식: 삼겹살, 갈비, 비빔밥, 된장찌개, 순두부, 한정식, 치킨, 삼계탕
   - 카페/디저트: 카페, 커피숍, 케이크, 빵집, 베이커리, 아이스크림, 디저트 카페
   - 분식: 떡볶이, 순대, 튀김, 김밥, 라볶이
   - 기타: 위 어느 카테고리에도 해당하지 않는 경우만

Return JSON:
{{ "restaurants": [ {{ "name": "...", "location": "...", "menu": [...], "category": "..." }}, ... ] }}

- Use null if info is missing
- JSON only, no markdown
"""
    hit_rate_limit = False
    for model in ["gemini-2.5-flash", "gemini-2.5-flash-lite"]:
        for attempt in range(2):
            try:
                response = client.models.generate_content(model=model, contents=prompt)
                print(f"Gemini ({model}): {response.text[:200]}")
                return _parse_gemini_json(response.text)
            except Exception as e:
                err = str(e)
                print(f"Gemini error ({model} attempt {attempt+1}): {err[:120]}")
                if "429" in err or "quota" in err.lower() or "rate" in err.lower():
                    hit_rate_limit = True
                    if attempt == 0:
                        time.sleep(5)
                        continue
                elif attempt == 0 and ("503" in err or "unavailable" in err.lower()):
                    time.sleep(3)
                    continue
                break  # 이 모델 실패 → 다음 모델 시도
    if hit_rate_limit:
        return {"error": "rate_limited", "message": "AI 분석 서버가 잠시 과부하 상태예요. 1분 후 다시 시도해주세요.", "retry_after": 60}
    return {"error": "unavailable", "message": "AI 서버에 일시적으로 연결할 수 없어요. 잠시 후 다시 시도해주세요.", "retry_after": 30}

def _parse_gemini_json(text: str) -> dict:
    content = text.replace("```json", "").replace("```", "").strip()
    start = content.find('{')
    end = content.rfind('}')
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON object")
    parsed = json.loads(content[start:end+1])
    if "restaurants" not in parsed and ("name" in parsed or "location" in parsed):
        parsed = {"restaurants": [parsed]}
    return parsed


# ── 3. 영상 프레임 분석 (폴백) ────────────────────────────────────────────────────

def extract_frames_from_video(url: str, n: int = 6) -> list[bytes]:
    tmpdir = tempfile.mkdtemp(prefix="instaeat_")
    frames = []
    try:
        video_file = None
        for attempt in _yt_dlp_attempts():
            label = attempt.pop("_label")
            try:
                ydl_opts = {
                    "quiet": True, "no_warnings": True,
                    "outtmpl": os.path.join(tmpdir, "video.%(ext)s"),
                    "format": "worstvideo[ext=mp4]/worst[ext=mp4]/worst",
                    **attempt
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])
                files = glob.glob(os.path.join(tmpdir, "video.*"))
                if files:
                    video_file = files[0]
                    print(f"Video downloaded ({label}): {os.path.getsize(video_file):,} bytes")
                    break
            except Exception as e:
                print(f"Video download failed ({label}): {e}")

        if not video_file:
            return []

        for i in range(n):
            frame_path = os.path.join(tmpdir, f"frame_{i}.jpg")
            try:
                subprocess.run(
                    ["ffmpeg", "-ss", str(i * 3), "-i", video_file,
                     "-vframes", "1", "-q:v", "3", frame_path, "-y"],
                    capture_output=True, timeout=20
                )
                if os.path.exists(frame_path) and os.path.getsize(frame_path) > 0:
                    with open(frame_path, "rb") as f:
                        frames.append(f.read())
            except FileNotFoundError:
                print("ffmpeg not found")
                break
            except subprocess.TimeoutExpired:
                print(f"ffmpeg timeout at frame {i}")
    except Exception as e:
        print(f"Frame extraction error: {e}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return frames


def analyze_frames_with_gemini(frames: list[bytes]) -> dict:
    if not client or not frames:
        return {}
    try:
        contents = []
        for frame_bytes in frames[:6]:
            contents.append(types.Part(
                inline_data=types.Blob(mime_type="image/jpeg", data=frame_bytes)
            ))
        contents.append(types.Part(text="""이 이미지들은 인스타그램 음식 릴스의 프레임입니다.
다음을 꼼꼼히 찾아주세요:
- 간판, 로고, 상호명 텍스트
- 자막/텍스트 오버레이에 나오는 주소, 위치, 매장명
- 메뉴판, 영수증에 표시된 음식 이름과 가격
- 지역명이 들어간 텍스트

추출 규칙:
- name: 식당/카페 이름 (한글 또는 영문 상호명)
- location: 도로명 주소 우선, 없으면 동/구/시 단위 위치
- menu: 음식 이름과 가격 목록 (명확히 보이는 것만)
- category: [한식/일식/중식/양식/카페/디저트/술집/바/분식/패스트푸드/기타] 중 하나 (피자·파스타·버거·스테이크→양식)

JSON만 반환: {"name":"...","location":"...","menu":[...],"category":"..."}
확실하지 않으면 null 사용."""))

        for model in ["gemini-2.5-flash", "gemini-2.5-flash-lite"]:
            try:
                response = client.models.generate_content(model=model, contents=contents)
                print(f"Frame analysis ({model}): {response.text[:200]}")
                content = response.text.replace("```json", "").replace("```", "").strip()
                start = content.find('{')
                end = content.rfind('}')
                if start != -1 and end != -1:
                    return json.loads(content[start:end+1])
                break
            except Exception as e:
                print(f"Frame Gemini error ({model}): {e}")
    except Exception as e:
        print(f"Frame analysis error: {e}")
    return {}


# ── Geocoding ─────────────────────────────────────────────────────────────────


def _naver_search_items(query: str, display: int = 5) -> list:
    """Naver local search. API 1회 최대 5건 → display>5이면 페이지네이션."""
    _PER_PAGE = 5
    all_items: list = []
    pages = math.ceil(display / _PER_PAGE)
    for page in range(pages):
        start = page * _PER_PAGE + 1
        try:
            res = requests.get(
                "https://openapi.naver.com/v1/search/local.json",
                headers={
                    "X-Naver-Client-Id": NAVER_SEARCH_CLIENT_ID,
                    "X-Naver-Client-Secret": NAVER_SEARCH_CLIENT_SECRET,
                },
                params={"query": query, "display": _PER_PAGE, "start": start},
                timeout=10
            )
            if res.status_code == 200:
                items = res.json().get("items", [])
                all_items.extend(items)
                if len(items) < _PER_PAGE:
                    break
            elif res.status_code == 429:
                print(f"Naver API 할당량 초과 (429). 잠시 대기 후 재시도.")
                time.sleep(2)
                break
            else:
                print(f"Naver search HTTP {res.status_code} ({query})")
                break
        except Exception as e:
            print(f"Naver search error ({query}, start={start}): {e}")
            break
    return all_items


def get_city_from_gps(lat: float, lng: float) -> str | None:
    """GPS → 시/군/구 이름 (OSM Nominatim, 무료·무키)"""
    try:
        res = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"lat": lat, "lon": lng, "format": "json", "accept-language": "ko"},
            headers={"User-Agent": "InstaEat-App/1.0"},
            timeout=5
        )
        if res.status_code == 200:
            addr = res.json().get("address", {})
            city = (addr.get("city") or addr.get("county") or
                    addr.get("town") or addr.get("village") or "")
            city = re.sub(r'[시군구]$', '', city).strip()
            print(f"City from GPS ({lat:.4f},{lng:.4f}): {city!r}")
            return city or None
    except Exception as e:
        print(f"Reverse geocoding error: {e}")
    return None


def _name_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


VAGUE_LOCATION_KEYWORDS = {"전국", "방방곡곡", "전지점", "전매장", "전국각지", "어디서나", "전국방방"}

def is_vague_location(location: str | None) -> bool:
    if not location:
        return True
    loc = location.replace(" ", "")
    return any(kw in loc for kw in VAGUE_LOCATION_KEYWORDS)


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat/2)**2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlng/2)**2)
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(a)))


def naver_local_search_best(name: str, location: str = None,
                            user_lat: float = None, user_lng: float = None) -> dict | None:
    """이름 퍼지매칭 + GPS 최근접 지점 + 주소 폴백"""

    # GPS 기반 최근접 지점 (위치가 전국/모호한 가맹점)
    if is_vague_location(location) and name and user_lat is not None and user_lng is not None:
        def _dist(item):
            try:
                return haversine_km(user_lat, user_lng,
                                    int(item["mapy"]) / 1e7,
                                    int(item["mapx"]) / 1e7)
            except Exception:
                return 9999.0

        city = get_city_from_gps(user_lat, user_lng)
        items: list = []

        # 1) 도시명 포함 검색 → 해당 도시 지점 우선 노출
        if city:
            city_items = _naver_search_items(f"{name} {city}", display=10)
            name_ns = re.sub(r'\s', '', re.sub(r'[^가-힣a-zA-Z0-9\s]', '', name)).lower()
            items = [it for it in city_items
                     if name_ns in re.sub(r'\s', '', re.sub(r'<[^>]+>', '', it.get('title', ''))).lower()]
            print(f"City-specific search '{name} {city}': {len(items)} matching items")

        # 2) 도시 검색 실패 시 전국 25개 → GPS 최근접
        if not items:
            items = _naver_search_items(name, display=25)
            print(f"Nationwide search '{name}': {len(items)} items")

        if items:
            nearest = min(items, key=_dist)
            d = _dist(nearest)
            nearest_name = re.sub(r'<[^>]+>', '', nearest.get('title', ''))
            print(f"GPS nearest ({city or 'nationwide'}): {nearest_name} ({d:.1f}km)")
            return _item_to_dict(nearest)
        return None

    name_clean = re.sub(r'[^가-힣a-zA-Z0-9\s]', '', name or '').strip().lower()

    # 1단계: 이름 기반 쿼리 (완전일치 → 퍼지매칭)
    name_queries = []
    if name and location:
        name_queries = [f"{name} {location}", name]
    elif name:
        name_queries = [name]

    for query in name_queries:
        items = _naver_search_items(query, display=10)
        if not items:
            continue

        exact_matches = []
        best_item = None
        best_score = 0.0

        for item in items:
            item_name = re.sub(r'<[^>]+>', '', item.get("title", "")).strip().lower()
            item_name_clean = re.sub(r'[^가-힣a-zA-Z0-9\s]', '', item_name)
            # 공백 제거 버전으로 비교 (불도장 중화요리전문점 vs 불도장중화요리전문점 동일 처리)
            nc_ns = name_clean.replace(' ', '')
            ic_ns = item_name_clean.replace(' ', '')

            # 결과가 검색어의 부분문자열 → 키워드 오매칭 차단
            if (ic_ns and nc_ns and ic_ns != nc_ns and ic_ns in nc_ns):
                print(f"Skipping keyword-only match: {item_name}")
                continue

            # 완전 포함 일치 (공백 무시): 모두 수집
            if nc_ns and nc_ns in ic_ns:
                exact_matches.append(item)
                continue

            # 퍼지 유사도 (공백 제거 버전 비교)
            if nc_ns and ic_ns:
                score = _name_similarity(nc_ns, ic_ns)
                if score > best_score:
                    best_score = score
                    best_item = item

        # 정확 일치가 있으면 GPS/주소로 최적 지점 선택 (동일 이름 여러 지점 → 가장 가까운 것)
        if exact_matches:
            return _pick_best_match(exact_matches, location, user_lat, user_lng)

        # 유사도 0.85 이상만 채택
        if best_item and best_score >= 0.85:
            item_name = re.sub(r'<[^>]+>', '', best_item.get("title", ""))
            print(f"Naver fuzzy match ({best_score:.2f}): {item_name}")
            return _item_to_dict(best_item)

    return None


def _pick_best_match(items: list, location: str = None,
                     user_lat: float = None, user_lng: float = None) -> dict:
    """동명 여러 지점 중 GPS·주소 기준 최적 선택"""
    if len(items) == 1:
        return _item_to_dict(items[0])

    # GPS 가장 가까운 지점
    if user_lat is not None and user_lng is not None:
        nearest = min(items, key=lambda it: haversine_km(
            user_lat, user_lng, int(it["mapy"]) / 1e7, int(it["mapx"]) / 1e7))
        d = haversine_km(user_lat, user_lng, int(nearest["mapy"]) / 1e7, int(nearest["mapx"]) / 1e7)
        addr = re.sub(r'<[^>]+>', '', nearest.get("roadAddress") or nearest.get("address", ""))
        print(f"Multi-match ({len(items)}) → GPS nearest: {addr} ({d:.1f}km)")
        return _item_to_dict(nearest)

    # 캡션 위치 텍스트로 주소 유사도 비교
    if location and not is_vague_location(location):
        loc_clean = re.sub(r'[^가-힣a-zA-Z0-9\s]', '', location).lower()
        best = max(items, key=lambda it: _name_similarity(
            loc_clean,
            re.sub(r'[^가-힣a-zA-Z0-9\s]', '',
                   re.sub(r'<[^>]+>', '', it.get("roadAddress") or it.get("address", ""))).lower()
        ))
        addr = re.sub(r'<[^>]+>', '', best.get("roadAddress") or best.get("address", ""))
        print(f"Multi-match ({len(items)}) → address match: {addr}")
        return _item_to_dict(best)

    # 기본: Naver 관련도 1순위
    print(f"Multi-match ({len(items)}) → using first result")
    return _item_to_dict(items[0])


def _item_to_dict(item: dict) -> dict:
    lat = int(item.get("mapy", 0) or 0) / 1e7
    lng = int(item.get("mapx", 0) or 0) / 1e7
    address = re.sub(r'<[^>]+>', '', item.get("roadAddress") or item.get("address", ""))
    matched_name = re.sub(r'<[^>]+>', '', item.get("title", ""))
    link = item.get("link", "")
    naver_cat_raw = item.get("category", "")
    place_id = extract_place_id(link)
    category = map_naver_category(naver_cat_raw)
    print(f"Naver hit: {matched_name}, placeId={place_id}, cat={naver_cat_raw}")
    return {
        "lat": lat, "lng": lng,
        "full_address": address,
        "name": matched_name,
        "link": link,
        "place_id": place_id,
        "naver_category": naver_cat_raw,
        "category": category,
        "rating": None,
        "review_count": None,
    }


def nominatim_search(query: str) -> dict | None:
    try:
        res = requests.get(
            "https://nominatim.openstreetmap.org/search",
            headers={"User-Agent": "InstaEat/1.0 (byunhu35@gmail.com)"},
            params={"q": query, "format": "json", "countrycodes": "kr", "limit": 1},
            timeout=10
        )
        if res.status_code == 200 and res.text.strip():
            data = res.json()
            if data:
                return {
                    "lat": float(data[0]["lat"]), "lng": float(data[0]["lon"]),
                    "full_address": data[0].get("display_name", query),
                    "name": None, "link": None, "place_id": None,
                    "naver_category": None, "category": None,
                    "rating": None, "review_count": None,
                }
    except Exception as e:
        print(f"Nominatim error: {e}")
    return None


def get_place_details_by_id(place_id: str) -> dict | None:
    """Place ID → 좌표·이름·주소 조회 (summary API 또는 모바일 홈 페이지)"""
    hdrs = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://map.naver.com/",
    }
    # 1) summary API (x=경도, y=위도)
    try:
        res = requests.get(f"https://map.naver.com/v5/api/sites/summary/{place_id}",
                           headers=hdrs, timeout=8)
        if res.status_code == 200:
            d = res.json()
            lat = d.get("y") or d.get("lat")
            lng = d.get("x") or d.get("lng")
            if lat and lng:
                name = d.get("name", "")
                addr = d.get("roadAddress") or d.get("address", "")
                cat_raw = d.get("category", "")
                print(f"PlaceDetails from summary: {name} ({lat},{lng})")
                return {
                    "lat": float(lat), "lng": float(lng),
                    "full_address": addr, "name": name,
                    "place_id": place_id,
                    "naver_category": cat_raw,
                    "category": map_naver_category(cat_raw),
                    "link": f"https://map.naver.com/v5/entry/place/{place_id}",
                    "rating": None, "review_count": None,
                }
    except Exception as e:
        print(f"PlaceDetails summary error: {e}")

    # 2) 모바일 홈 페이지에서 좌표 파싱 (/restaurant/, /place/ 둘 다 시도)
    for path in [f"restaurant/{place_id}", f"place/{place_id}"]:
        try:
            res = requests.get(
                f"https://m.place.naver.com/{path}/home",
                headers={"User-Agent": "Mozilla/5.0 (Linux; Android 13; SM-S908N) AppleWebKit/537.36"},
                timeout=8)
            if res.status_code != 200:
                continue
            res.encoding = 'utf-8'
            # __APOLLO_STATE__에서 좌표 추출
            apollo_idx = res.text.find('__APOLLO_STATE__')
            if apollo_idx >= 0:
                try:
                    start = res.text.index('{', apollo_idx)
                    depth = 0
                    for ci, ch in enumerate(res.text[start:], start):
                        if ch == '{': depth += 1
                        elif ch == '}':
                            depth -= 1
                            if depth == 0: break
                    apollo = json.loads(res.text[start:ci+1])
                    detail = apollo.get(f"PlaceDetailBase:{place_id}", {})
                    lat_s = detail.get("y") or detail.get("lat")
                    lng_s = detail.get("x") or detail.get("lng")
                    name_s = detail.get("name", "")
                    addr_s = detail.get("roadAddress", "")
                    cat_s = detail.get("category", "")
                    if lat_s and lng_s:
                        print(f"PlaceDetails from Apollo ({path}): {name_s}")
                        return {
                            "lat": float(lat_s), "lng": float(lng_s),
                            "full_address": addr_s, "name": name_s,
                            "place_id": place_id,
                            "naver_category": cat_s,
                            "category": map_naver_category(cat_s),
                            "link": f"https://map.naver.com/v5/entry/place/{place_id}",
                            "rating": None, "review_count": None,
                        }
                except Exception as ae:
                    print(f"Apollo parse error: {ae}")
            # 폴백: 정규식
            lat_m = re.search(r'"(?:lat|y)"\s*:\s*"?(3[3-9]\.\d+)"?', res.text)
            lng_m = re.search(r'"(?:lng|x)"\s*:\s*"?(12[6-9]\.\d+)"?', res.text)
            name_m = re.search(r'"name"\s*:\s*"([^"]{1,60})"', res.text)
            addr_m = re.search(r'"roadAddress"\s*:\s*"([^"]{1,100})"', res.text)
            if lat_m and lng_m:
                name_s = name_m.group(1) if name_m else ""
                addr_s = addr_m.group(1) if addr_m else ""
                print(f"PlaceDetails from regex ({path}): {name_s}")
                return {
                    "lat": float(lat_m.group(1)), "lng": float(lng_m.group(1)),
                    "full_address": addr_s, "name": name_s,
                    "place_id": place_id,
                    "naver_category": "", "category": "",
                    "link": f"https://map.naver.com/v5/entry/place/{place_id}",
                    "rating": None, "review_count": None,
                }
        except Exception as e:
            print(f"PlaceDetails mobile error ({path}): {e}")
    return None


def _shorten_address(address: str) -> str:
    """주소에서 시/도 + 시/군/구만 추출 (검색 쿼리용)"""
    if not address:
        return ''
    parts = address.split()
    keep = []
    for p in parts:
        if re.search(r'[시도군구]$', p):
            keep.append(p)
        if len(keep) >= 2:
            break
    return ' '.join(keep)


def get_naver_place_id_by_search(name: str, lat: float = None, lng: float = None,
                                  address: str = None) -> str | None:
    """모바일 Naver 검색(where=nexearch)으로 PlaceID 조회. 이름 유사도 검증 포함."""
    short_addr = _shorten_address(address) if address else ''
    query = f"{name} {short_addr}" if short_addr else name
    name_ns = re.sub(r'[^가-힣a-zA-Z0-9]', '', name).lower()

    try:
        res = requests.get(
            "https://search.naver.com/search.naver",
            params={"query": query, "sm": "tab_hty.top", "where": "nexearch"},
            headers={
                "User-Agent": "Mozilla/5.0 (Linux; Android 13; SM-S908N) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
                "Accept-Language": "ko-KR,ko;q=0.9",
                "Referer": "https://www.naver.com/",
            },
            timeout=10
        )
        if res.status_code != 200:
            print(f"NaverMobileSearch HTTP {res.status_code}")
            return None
        res.encoding = 'utf-8'
        html = res.text

        def _verify_and_return(pid: str, ctx: str) -> str | None:
            """place_id 후보가 이름과 일치하면 반환, 아니면 None."""
            # 1단계: HTML 컨텍스트에서 이름 추출 (빠른 체크)
            pname_m = re.search(
                r'<(?:span|a|strong|h[1-6]|p)[^>]*>\s*([가-힣][가-힣0-9\s·]{1,40})\s*</(?:span|a|strong|h[1-6]|p)>',
                ctx[:500]
            )
            if pname_m:
                pname = pname_m.group(1).strip()
                pname_ns = re.sub(r'[^가-힣a-zA-Z0-9]', '', pname).lower()
                if name_ns in pname_ns or _name_similarity(name_ns, pname_ns) >= 0.80:
                    print(f"NaverMobileSearch: {pid} ({pname})")
                    return pid
                # 컨텍스트 이름이 짧거나 UI 텍스트이면 Apollo로 재확인
                if len(pname_ns) >= 4:
                    print(f"NaverMobileSearch: ctx mismatch {pid} ({pname}), trying Apollo")
            # 2단계: Apollo 호출로 직접 확인 (이름 추출 실패 or 컨텍스트 불일치)
            try:
                r2 = requests.get(
                    f"https://m.place.naver.com/restaurant/{pid}/home",
                    headers={"User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36"},
                    timeout=5
                )
                if r2.status_code == 200:
                    r2.encoding = 'utf-8'
                    a_idx = r2.text.find(f'"PlaceDetailBase:{pid}"')
                    if a_idx >= 0:
                        nm = re.search(r'"name"\s*:\s*"([^"]{1,60})"', r2.text[a_idx:a_idx+200])
                        if nm:
                            pname = nm.group(1)
                            pname_ns = re.sub(r'[^가-힣a-zA-Z0-9]', '', pname).lower()
                            if name_ns in pname_ns or _name_similarity(name_ns, pname_ns) >= 0.80:
                                print(f"NaverMobileSearch (Apollo verify): {pid} ({pname})")
                                return pid
                            print(f"NaverMobileSearch: Apollo skip {pid} ({pname})")
            except Exception:
                pass
            return None

        # 방법 1: data-loc_plc-doc-id="ID" 패턴 (검색 결과 목록형)
        segments = re.split(r'data-loc_plc-doc-id="(\d+)"', html)
        if len(segments) > 1:
            for i in range(1, len(segments), 2):
                pid = segments[i]
                ctx = segments[i + 1] if i + 1 < len(segments) else ""
                result = _verify_and_return(pid, ctx)
                if result:
                    return result

        # 방법 2: place.naver.com/restaurant(cafe 등)/ID 패턴 (단일 결과 패널형)
        place_segs = re.split(r'place\.naver\.com/(?:restaurant|cafe|hotel|beauty|hospital|[a-z]+)/(\d{7,15})', html)
        seen: set = set()
        if len(place_segs) > 1:
            for i in range(1, len(place_segs), 2):
                pid = place_segs[i]
                if pid in seen:
                    continue
                seen.add(pid)
                ctx = place_segs[i + 1] if i + 1 < len(place_segs) else ""
                result = _verify_and_return(pid, ctx)
                if result:
                    return result

    except Exception as e:
        print(f"NaverMobileSearch error: {e}")
    return None


def get_coordinates(address: str, name: str = None,
                    user_lat: float = None, user_lng: float = None) -> dict | None:
    # 1) Naver Local Search API
    if name:
        result = naver_local_search_best(name, address, user_lat, user_lng)
        if result:
            return result

    # 2) Naver 검색 웹 스크래핑 → Place ID → 좌표
    #    Local API에서 못 찾는 소규모 식당 대응
    if name:
        city = None
        if user_lat and user_lng:
            city = get_city_from_gps(user_lat, user_lng)
        search_addr = address if (address and address != "null" and not is_vague_location(address)) else (city or "")
        pid = get_naver_place_id_by_search(name, lat=user_lat, lng=user_lng, address=search_addr)
        if pid:
            details = get_place_details_by_id(pid)
            if details:
                print(f"Coordinates via web scrape: {details['name']}")
                return details

    # 3) 주소 지오코딩 (이름 못 찾았을 때 위치만 반환)
    if address and address != "null":
        return nominatim_search(address)
    return None


def get_naver_place_rating(place_id: str) -> dict:
    if not place_id:
        return {}
    try:
        res = requests.get(
            f"https://map.naver.com/v5/api/sites/summary/{place_id}",
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://map.naver.com/",
            },
            timeout=8
        )
        if res.status_code == 200:
            data = res.json()
            visitor = data.get("visitorReview") or {}
            rating = visitor.get("avgRating")
            count = visitor.get("count")
            rating_float = float(rating) if rating else None
            if rating_float is not None and rating_float <= 0:
                rating_float = None
            if rating_float or count:
                print(f"Rating from API: {rating_float} ({count} reviews)")
                return {"rating": rating_float,
                        "review_count": int(count) if count else None}
    except Exception as e:
        print(f"Naver Place API error: {e}")

    try:
        res = requests.get(
            f"https://m.place.naver.com/place/{place_id}/home",
            headers={"User-Agent": "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36"},
            timeout=8
        )
        if res.status_code == 200:
            res.encoding = 'utf-8'
            text = res.text
            # 별점: 0보다 큰 값만 채택
            rating_match = re.search(r'"avgRating"\s*:\s*"?([\d.]+)"?', text)
            count_match = re.search(r'"visitorReviewCount"\s*:\s*(\d+)', text)
            if not count_match:
                count_match = re.search(r'"totalCount"\s*:\s*(\d+)', text)
            if not count_match:
                count_match = re.search(r'"count"\s*:\s*(\d+)', text)
            rating = float(rating_match.group(1)) if rating_match else None
            if rating is not None and rating <= 0:
                rating = None
            count = int(count_match.group(1)) if count_match else None
            if rating or count:
                print(f"Rating scraped: {rating} ({count} reviews)")
                return {"rating": rating, "review_count": count}
    except Exception as e:
        print(f"Naver Place scrape error: {e}")
    return {}


_MENU_SKIP = {"서비스", "무료", "기본제공", "기본찬", "무료제공", "서비스제공", "기본서비스"}

def _is_real_menu(name: str) -> bool:
    n = name.replace(" ", "")
    return bool(n) and not any(kw in n for kw in _MENU_SKIP)

def _clean_menu_name(name: str) -> str:
    """[HIT], [강추], 강추! 등 마케팅 태그 제거"""
    # 대괄호 태그 제거: [HIT], [불향가득], [대표] 등
    name = re.sub(r'\[[^\]]{1,20}\]\s*', '', name)
    # 끝의 감탄 태그 제거: 강추!, 추천!, BEST!, NEW! 등
    name = re.sub(r'\s*(강추|추천|인기|BEST|NEW|신메뉴|대표|히트|HOT|스페셜|특선)!?\s*$', '', name, flags=re.IGNORECASE)
    return name.strip()

def _fmt_menu(name: str, price) -> str:
    name = _clean_menu_name(name)
    if price:
        try:
            return f"{name} {int(price):,}원"
        except (ValueError, TypeError):
            pass
    return name


def _extract_menus_from_obj(obj) -> list[str]:
    """JSON 객체(dict/list)에서 메뉴명+가격 리스트 추출. 카테고리 구조 자동 처리."""
    results: list[str] = []

    def _collect(o, depth=0):
        if depth > 12 or len(results) >= 4:
            return
        if isinstance(o, list):
            for item in o:
                _collect(item, depth + 1)
                if len(results) >= 4:
                    return
        elif isinstance(o, dict):
            name = (o.get('name') or o.get('menuName') or '').strip()
            if name and _is_real_menu(name):
                price_raw = o.get('price') or o.get('priceStr') or o.get('priceContent') or ''
                p_num = re.search(r'\d+', str(price_raw).replace(',', ''))
                results.append(_fmt_menu(name, p_num.group() if p_num else None))
                return  # 이름 있는 항목은 더 안 파고듦
            # 이름 없으면 하위 목록 탐색 (카테고리 노드)
            for key in ('menus', 'menuList', 'items', 'menuInfo'):
                sub = o.get(key)
                if isinstance(sub, (list, dict)):
                    _collect(sub, depth + 1)
                    if len(results) >= 4:
                        return

    _collect(obj)
    return results


def _parse_next_data_menu(html: str) -> list[str]:
    """__NEXT_DATA__ JSON에서 대표 메뉴 추출"""
    m = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if not m:
        print("Menu: __NEXT_DATA__ script tag not found")
        return []
    try:
        data = json.loads(m.group(1))

        # 재귀적으로 menus/menuList 배열 노드 찾기
        def find_menu_root(obj, depth=0):
            if depth > 12:
                return None
            if isinstance(obj, dict):
                for key in ('menuList', 'menus', 'menuInfo'):
                    val = obj.get(key)
                    if isinstance(val, list) and val:
                        # 직접 메뉴 항목이거나 카테고리 컨테이너
                        first = val[0] if isinstance(val[0], dict) else {}
                        if ('name' in first or 'menuName' in first or
                                'menus' in first or 'menuList' in first):
                            return val
                for val in obj.values():
                    if isinstance(val, (dict, list)):
                        r = find_menu_root(val, depth + 1)
                        if r is not None:
                            return r
            elif isinstance(obj, list):
                for item in obj:
                    if isinstance(item, (dict, list)):
                        r = find_menu_root(item, depth + 1)
                        if r is not None:
                            return r
            return None

        root = find_menu_root(data)
        if root is not None:
            result = _extract_menus_from_obj(root)
            if result:
                print(f"Menu from __NEXT_DATA__: {result}")
                return result
            print(f"Menu: found menu root but extracted 0 items (root length={len(root)})")
        else:
            print("Menu: no menu root found in __NEXT_DATA__")
    except Exception as e:
        print(f"NEXT_DATA menu error: {e}")
    return []


def get_naver_place_menu(place_id: str) -> list[str]:
    """네이버 플레이스 메뉴탭 상위 4개 (서비스/무료 제외, 가격 포맷 00,000원)"""
    if not place_id:
        return []

    mobile_headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 13; SM-S908N) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
        "Referer": "https://map.naver.com/",
        "Accept-Language": "ko-KR,ko;q=0.9",
    }
    pc_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://map.naver.com/",
        "Accept-Language": "ko-KR,ko;q=0.9",
    }

    def _parse_apollo_menus(html: str) -> list[str]:
        """__APOLLO_STATE__에서 Menu:{place_id}_{n} 키로 메뉴 추출"""
        apollo_idx = html.find('__APOLLO_STATE__')
        if apollo_idx < 0:
            return []
        try:
            start = html.index('{', apollo_idx)
            depth = 0
            for ci, ch in enumerate(html[start:], start):
                if ch == '{': depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0: break
            apollo = json.loads(html[start:ci+1])
        except Exception as e:
            print(f"Apollo parse error: {e}")
            return []
        prefix = f'Menu:{place_id}_'
        entries = []
        for k, v in apollo.items():
            if k.startswith(prefix):
                try:
                    entries.append((int(k[len(prefix):]), v))
                except ValueError:
                    pass
        entries.sort(key=lambda x: x[0])
        with_price: list[str] = []
        without_price: list[str] = []
        for _, m in entries:
            n = (m.get('name') or '').strip()
            if n and _is_real_menu(n):
                formatted = _fmt_menu(n, m.get('price'))
                if m.get('price'):
                    with_price.append(formatted)
                else:
                    without_price.append(formatted)
        combined = with_price + without_price
        return combined[:5]

    # 1. 모바일 메뉴 페이지 → __APOLLO_STATE__ 파싱
    pages = [
        (f"https://m.place.naver.com/restaurant/{place_id}/menu/foods", mobile_headers),
        (f"https://pcmap.place.naver.com/restaurant/{place_id}/menu/foods", pc_headers),
        (f"https://m.place.naver.com/place/{place_id}/menu", mobile_headers),
    ]
    for url, hdrs in pages:
        try:
            res = requests.get(url, headers=hdrs, timeout=8)
            print(f"Menu page {url}: HTTP {res.status_code}")
            if res.status_code != 200:
                continue
            res.encoding = 'utf-8'
            menus = _parse_apollo_menus(res.text)
            if menus:
                print(f"Menu from Apollo ({url}): {menus}")
                return menus
            # 정규식 폴백: "name":"메뉴명" + "price":"가격" 패턴
            pairs = re.findall(
                r'"(?:name|menuName)"\s*:\s*"([^"]{1,40})"(?:(?!"name")[^}]){0,300}'
                r'"price(?:Str|Content)?"\s*:\s*"?(\d[\d,]*)"?',
                res.text)
            if pairs:
                menus = [_fmt_menu(n, p.replace(',', '')) for n, p in pairs
                         if _is_real_menu(n)][:4]
                if menus:
                    print(f"Menu from regex ({url}): {menus}")
                    return menus
        except Exception as e:
            print(f"Menu page error ({url}): {e}")

    # 2. summary API (카테고리 구조 포함 처리)
    try:
        res = requests.get(
            f"https://map.naver.com/v5/api/sites/summary/{place_id}",
            headers=pc_headers, timeout=8
        )
        print(f"Summary API: HTTP {res.status_code}")
        if res.status_code == 200:
            data = res.json()
            raw = data.get("menus", [])
            menus: list[str] = []
            for item in raw:
                if not isinstance(item, dict):
                    continue
                n = item.get("name", "").strip()
                if n and _is_real_menu(n):
                    menus.append(_fmt_menu(n, item.get("price")))
                elif "menus" in item:  # 카테고리 → 하위 메뉴
                    for sub in item.get("menus", []):
                        if not isinstance(sub, dict):
                            continue
                        sn = sub.get("name", "").strip()
                        if sn and _is_real_menu(sn):
                            menus.append(_fmt_menu(sn, sub.get("price")))
                if len(menus) >= 4:
                    break
            if menus:
                result = menus[:4]
                print(f"Menu from summary API: {result}")
                return result
    except Exception as e:
        print(f"Menu summary error: {e}")
    return []


# ── 블로그 리뷰 AI 요약 ───────────────────────────────────────────────────────────

def fetch_blog_review_snippets(name: str, address: str = None) -> list[str]:
    # 식당명을 반드시 앞에 두어야 관련 블로그가 잡힘
    query = f"{name} 맛집 후기"
    if address:
        district_match = re.search(r'(\S+[구동시])\b', address)
        if district_match:
            query = f"{name} {district_match.group(1)} 후기"
    print(f"Blog query: {query}")
    try:
        res = requests.get(
            "https://openapi.naver.com/v1/search/blog.json",
            headers={
                "X-Naver-Client-Id": NAVER_SEARCH_CLIENT_ID,
                "X-Naver-Client-Secret": NAVER_SEARCH_CLIENT_SECRET,
            },
            params={"query": query, "display": 10, "sort": "date"},
            timeout=10
        )
        if res.status_code == 200:
            items = res.json().get("items", [])
            snippets = []
            name_words = re.findall(r'[가-힣]{2,}|[a-zA-Z]{3,}', name)
            for item in items:
                title = re.sub(r'<[^>]+>', '', item.get("title", "")).strip()
                desc = re.sub(r'<[^>]+>', '', item.get("description", "")).strip()
                combined = (title + " " + desc).lower()
                # 식당명의 주요 단어가 하나라도 포함된 블로그만 채택
                if name_words and not any(w.lower() in combined for w in name_words):
                    continue
                if desc and len(desc) > 20:
                    snippets.append(desc)
                if len(snippets) >= 5:
                    break
            return snippets
    except Exception as e:
        print(f"Blog search error: {e}")
    return []


# ── API ───────────────────────────────────────────────────────────────────────

@app.get("/")
def read_root():
    return {"message": "InstaEat Backend is running!"}


def _check_cookie_status() -> dict:
    if not os.path.exists(COOKIE_FILE):
        return {"status": "missing"}
    now_ts = int(time.time())
    earliest_expiry = None
    try:
        with open(COOKIE_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) >= 5:
                    try:
                        exp = int(parts[4])
                        if exp > 0:
                            if earliest_expiry is None or exp < earliest_expiry:
                                earliest_expiry = exp
                    except ValueError:
                        pass
    except Exception:
        return {"status": "unreadable"}
    if earliest_expiry is None:
        return {"status": "ok", "expires_at": "unknown"}
    days_left = (earliest_expiry - now_ts) // 86400
    if days_left < 0:
        return {"status": "expired", "days_left": days_left}
    if days_left < 14:
        return {"status": "expiring_soon", "days_left": days_left}
    return {"status": "ok", "days_left": days_left}


@app.get("/health")
def health_check():
    """서비스 상태 및 API 키 유효성 확인"""
    cookie_info = _check_cookie_status()
    status = {
        "status": "ok",
        "gemini": "ok" if client else "missing_key",
        "naver": "ok" if NAVER_SEARCH_CLIENT_ID else "missing_key",
        "instagram_cookies": cookie_info,
        "cache_entries": len(_analyze_cache),
        "analyze_slots_available": _analyze_semaphore._value,
    }
    return status


class DebugSearchRequest(BaseModel):
    name: str
    location: str | None = None
    user_lat: float | None = None
    user_lng: float | None = None

@app.post("/debug_search")
def debug_search(req: DebugSearchRequest):
    """식당 이름+위치 → 좌표+place_id+메뉴 전체 플로우 디버그"""
    result = get_coordinates(
        req.location or "",
        name=req.name,
        user_lat=req.user_lat,
        user_lng=req.user_lng,
    )
    if result and not result.get("place_id") and result.get("lat"):
        pid = get_naver_place_id_by_search(
            result.get("name") or req.name,
            lat=result["lat"], lng=result["lng"],
            address=result.get("full_address"),
        )
        if pid:
            result["place_id"] = pid
    menus = get_naver_place_menu(result["place_id"]) if result and result.get("place_id") else []
    return {"input": req.model_dump(), "result": result, "menus": menus}


@app.post("/review_summary")
def get_review_summary(request: ReviewRequest):
    print(f"Review summary request: {request.name} / {request.address}")
    snippets = fetch_blog_review_snippets(request.name, request.address)
    print(f"Snippets found: {len(snippets)}")
    if not snippets:
        return {"success": False, "message": "블로그 후기를 찾을 수 없습니다"}

    review_text = "\n".join(f"- {s}" for s in snippets)
    prompt = f"""
다음은 식당 "{request.name}"에 대한 네이버 블로그 방문 후기 발췌입니다:
{review_text}

위 내용만을 바탕으로, 방문 전 꼭 알아야 할 실용적인 제약사항과 핵심 정보를 한 줄로 요약하세요.
아래 항목이 언급된 경우 우선적으로 포함하세요:
- 주차 가능/불가
- 아기의자/유아시설 유무
- 웨이팅/예약 필요 여부
- 포장 가능 여부
- 반려동물 동반 가능 여부
- 영업시간 주의사항 (브레이크타임, 조기마감 등)
- 현금 전용 여부

30자 이내 한국어로. 없는 정보는 언급하지 마세요.
예시: "주차 가능, 주말 웨이팅 1시간, 아기의자 있음"
요약만 출력:
"""
    hit_rate_limit = False
    for model in ["gemini-2.5-flash", "gemini-2.5-flash-lite"]:
        try:
            response = client.models.generate_content(model=model, contents=prompt)
            summary = response.text.strip().strip('"')
            return {"success": True, "summary": summary}
        except Exception as e:
            err = str(e)
            print(f"Review Gemini error ({model}): {err[:120]}")
            if "429" in err or "quota" in err.lower() or "rate" in err.lower():
                hit_rate_limit = True
                time.sleep(5)
            elif "503" in err or "unavailable" in err.lower():
                time.sleep(3)
            continue
    if hit_rate_limit:
        return {"success": False, "message": "AI 서버 일시 과부하, 잠시 후 재시도", "retry_after": 60}
    return {"success": False, "message": "AI 서버 연결 실패, 잠시 후 재시도"}


@app.get("/debug_menu/{place_id}")
def debug_menu(place_id: str):
    """메뉴 파싱 디버그용"""
    menus = get_naver_place_menu(place_id)
    return {"place_id": place_id, "menus": menus}


@app.post("/analyze")
def analyze_reel(request: AnalysisRequest):
    url = request.url.strip()

    # URL 검증
    if not _is_valid_instagram_url(url):
        return {"success": False, "message": "유효하지 않은 Instagram URL입니다. reel/p/tv 링크를 사용하세요."}

    # 캐시 확인
    cached = _get_cache(url)
    if cached:
        print(f"Cache hit: {url}")
        return cached

    # 동시 요청 제한 (슬롯 없으면 즉시 거절)
    if not _analyze_semaphore.acquire(blocking=False):
        return {"success": False, "message": "현재 분석 요청이 많습니다. 잠시 후 다시 시도해주세요."}

    try:
        result = _analyze_reel_inner(request)
    finally:
        _analyze_semaphore.release()

    # 성공 결과만 캐시 저장
    if result.get("success"):
        _set_cache(url, result)
    return result


def _analyze_reel_inner(request: AnalysisRequest) -> dict:
    print(f"\n{'='*50}\nAnalyzing: {request.url}")

    text_content, location_tag, thumbnail_url = get_reel_info(request.url)
    if not text_content:
        return {"success": False, "message": "릴스 정보 추출 실패. 쿠키가 만료됐거나 Instagram 로그인이 필요합니다. Chrome을 완전히 종료 후 재시도하거나 cookies.txt를 재발급하세요."}

    hashtag_location = _extract_location_from_hashtags(text_content)
    if hashtag_location:
        print(f"Hashtag location hint: {hashtag_location}")
    analysis_result = analyze_text_with_gemini(text_content, location_hint=hashtag_location)
    if not isinstance(analysis_result, dict) or "error" in analysis_result:
        resp = {"success": False, "message": analysis_result.get("message", "AI 분석에 실패했어요. 잠시 후 다시 시도해주세요.")}
        if "retry_after" in analysis_result:
            resp["retry_after"] = analysis_result["retry_after"]
        return resp

    restaurants_raw = analysis_result.get("restaurants", [])

    # 텍스트에서 식당 못 찾으면 프레임 분석으로 폴백
    frame_info = {}
    if not restaurants_raw:
        print("No restaurants in caption — falling back to frame analysis...")
        frames = extract_frames_from_video(request.url)
        if frames:
            frame_info = analyze_frames_with_gemini(frames)
        if frame_info.get("name"):
            restaurants_raw = [frame_info]
            frame_info = {}  # 이미 사용됨
        else:
            return {"success": False, "message": "릴스에서 식당 정보를 찾을 수 없습니다. 캡션에 식당 이름이나 위치를 포함해보세요."}

    # Normalize menu items (Gemini sometimes returns dicts instead of strings)
    for restaurant in restaurants_raw:
        raw_menu = restaurant.get('menu') or []
        normalized = []
        for item in raw_menu:
            if isinstance(item, dict):
                name = str(item.get('name') or item.get('item') or '').strip()
                price = str(item.get('price') or '').strip()
                entry = f"{name} {price}".strip() if price else name
                if entry:
                    normalized.append(entry)
            elif item:
                normalized.append(str(item).strip())
        restaurant['menu'] = normalized

    needs_fallback = any(not r.get("location") for r in restaurants_raw)
    if needs_fallback and not location_tag and not hashtag_location and not frame_info:
        print("Fallback: analyzing video frames for location...")
        frames = extract_frames_from_video(request.url)
        if frames:
            frame_info = analyze_frames_with_gemini(frames)

    restaurants = []
    for restaurant in restaurants_raw:
        if not restaurant.get("location"):
            if location_tag:
                restaurant["location"] = location_tag
            elif frame_info.get("location"):
                restaurant["location"] = frame_info["location"]
            elif hashtag_location:
                restaurant["location"] = hashtag_location
        if not restaurant.get("name") and frame_info.get("name"):
            restaurant["name"] = frame_info["name"]
        if not restaurant.get("category") and frame_info.get("category"):
            restaurant["category"] = frame_info["category"]

        coords = get_coordinates(
            restaurant.get("location") or "",
            name=restaurant.get("name"),
            user_lat=request.user_lat,
            user_lng=request.user_lng,
        )

        if coords:
            if coords.get("name"):
                restaurant["name"] = coords["name"]
            if coords.get("category"):
                restaurant["category"] = coords["category"]

            # PlaceID가 없으면 이름+주소로 정확한 지점 검색
            if not coords.get("place_id") and coords.get("lat") and coords.get("lng"):
                pid = get_naver_place_id_by_search(
                    restaurant.get("name", ""),
                    coords["lat"], coords["lng"],
                    address=coords.get("full_address"),
                )
                if pid:
                    coords["place_id"] = pid

            if coords.get("place_id"):
                rating_data = get_naver_place_rating(coords["place_id"])
                coords.update(rating_data)
                # 네이버 대표메뉴 항상 우선 사용 (캡션 메뉴 덮어씀)
                naver_menu = get_naver_place_menu(coords["place_id"])
                if naver_menu:
                    restaurant["menu"] = naver_menu

        print(f"  {restaurant.get('name')} → coords={'Y' if coords else 'N'}, cat={restaurant.get('category')}, menu={len(restaurant.get('menu', []))}")
        restaurants.append({"data": restaurant, "coordinates": coords})

    return {
        "success": True,
        "original_url": request.url,
        "thumbnail_url": thumbnail_url,
        "restaurants": restaurants,
    }


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
