from fastapi import FastAPI
from pydantic import BaseModel
import uvicorn
import os
import re
import json
import glob
import shutil
import tempfile
import subprocess
import time
import requests
from difflib import SequenceMatcher
from google import genai
from google.genai import types

api_key = os.environ.get("GEMINI_API_KEY", "AIzaSyB-xnB6mz7NK9n5jjyXcSNL6T20gibbULI")
client = None
if api_key:
    client = genai.Client(api_key=api_key)

NAVER_SEARCH_CLIENT_ID = os.environ.get("NAVER_CLIENT_ID", "nQ7TpKNklSAnrsi7ynNe")
NAVER_SEARCH_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "pCnrV5sAoM")

app = FastAPI()

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
    match = re.search(r'/place/(\d+)', link)
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

def analyze_text_with_gemini(text: str) -> dict:
    if not client:
        return {"error": "Gemini API Key not set"}

    model_id = "gemini-1.5-flash"
    prompt = f"""
ACT AS A DATA EXTRACTION TOOL. DO NOT USE YOUR OWN KNOWLEDGE.
EXTRACT INFORMATION ONLY FROM THE TEXT BELOW. DO NOT INFER OR GUESS.

TEXT:
"{text}"

STRICT RULES:
- ONLY include a restaurant if its NAME is EXPLICITLY written in the text (e.g., after "매장명:", "식당:", or as a clearly identified store name)
- Location hashtags like #연남동맛집 or #홍대맛집 are NOT restaurant names — ignore them
- If the text shows "매장명: X", extract ONLY that restaurant (unless text also explicitly names others separately)
- DO NOT use your own knowledge to add restaurants that are not named in the text
- When uncertain whether something is a restaurant name, leave it out

For each EXPLICITLY NAMED restaurant:
1. name: exact name as written (from "매장명:", store sign text, or explicit naming)
2. location: Korean street address if stated; district/landmark only if no address given
3. menu: food/drink items and prices ONLY if explicitly listed in the text
4. category: ONE of [한식, 일식, 중식, 양식, 카페/디저트, 술집/바, 분식, 패스트푸드, 기타]

Return JSON:
{{ "restaurants": [ {{ "name": "...", "location": "...", "menu": [...], "category": "..." }}, ... ] }}

- Use null if info is missing
- JSON only, no markdown
"""
    for model in ["gemini-2.5-flash", "gemini-1.5-flash", "gemini-1.5-pro"]:
        for attempt in range(2):
            try:
                response = client.models.generate_content(model=model, contents=prompt)
                print(f"Gemini ({model}): {response.text[:200]}")
                return _parse_gemini_json(response.text)
            except Exception as e:
                err = str(e)
                print(f"Gemini error ({model} attempt {attempt+1}): {err[:120]}")
                if attempt == 0 and ("503" in err or "429" in err or "unavailable" in err.lower()):
                    time.sleep(3)
                    continue
                break  # 이 모델 실패 → 다음 모델 시도
    return {"error": "All Gemini models unavailable"}

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

def extract_frames_from_video(url: str, n: int = 4) -> list[bytes]:
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
        for frame_bytes in frames[:4]:
            contents.append(types.Part(
                inline_data=types.Blob(mime_type="image/jpeg", data=frame_bytes)
            ))
        contents.append(types.Part(text="""Instagram food reel frames.
Find visible restaurant info: store signs, logos, location text overlays, address text, menu boards.
Extract: name, location (road address preferred), menu items, category [한식/일식/중식/양식/카페/디저트/술집/바/분식/패스트푸드/기타]
JSON only: {"name":"...","location":"...","menu":[...],"category":"..."}
Use null if not clearly visible."""))

        response = client.models.generate_content(model="gemini-1.5-flash", contents=contents)
        print(f"Frame analysis: {response.text[:200]}")
        content = response.text.replace("```json", "").replace("```", "").strip()
        start = content.find('{')
        end = content.rfind('}')
        if start != -1 and end != -1:
            return json.loads(content[start:end+1])
    except Exception as e:
        print(f"Frame Gemini error: {e}")
    return {}


# ── Geocoding ─────────────────────────────────────────────────────────────────

def naver_local_search(query: str, display: int = 5) -> dict | None:
    try:
        res = requests.get(
            "https://openapi.naver.com/v1/search/local.json",
            headers={
                "X-Naver-Client-Id": NAVER_SEARCH_CLIENT_ID,
                "X-Naver-Client-Secret": NAVER_SEARCH_CLIENT_SECRET,
            },
            params={"query": query, "display": display},
            timeout=10
        )
        if res.status_code == 200:
            items = res.json().get("items", [])
            if items:
                item = items[0]
                lat = int(item["mapy"]) / 1e7
                lng = int(item["mapx"]) / 1e7
                address = re.sub(r'<[^>]+>', '', item.get("roadAddress") or item.get("address", query))
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
        else:
            print(f"Naver Local Search error: {res.status_code}")
    except Exception as e:
        print(f"Naver Local exception: {e}")
    return None


def _naver_search_items(query: str, display: int = 5) -> list:
    try:
        res = requests.get(
            "https://openapi.naver.com/v1/search/local.json",
            headers={
                "X-Naver-Client-Id": NAVER_SEARCH_CLIENT_ID,
                "X-Naver-Client-Secret": NAVER_SEARCH_CLIENT_SECRET,
            },
            params={"query": query, "display": display},
            timeout=10
        )
        if res.status_code == 200:
            return res.json().get("items", [])
    except Exception as e:
        print(f"Naver search error ({query}): {e}")
    return []


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
        items = _naver_search_items(name)
        if items:
            def _dist(item):
                try:
                    return haversine_km(user_lat, user_lng,
                                        int(item["mapy"]) / 1e7,
                                        int(item["mapx"]) / 1e7)
                except Exception:
                    return 9999.0
            nearest = min(items, key=_dist)
            d = _dist(nearest)
            nearest_name = re.sub(r'<[^>]+>', '', nearest.get('title', ''))
            print(f"Naver GPS nearest: {nearest_name} ({d:.1f}km)")
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
        items = _naver_search_items(query)
        if not items:
            continue

        best_item = None
        best_score = 0.0

        for item in items:
            item_name = re.sub(r'<[^>]+>', '', item.get("title", "")).strip().lower()
            item_name_clean = re.sub(r'[^가-힣a-zA-Z0-9\s]', '', item_name)

            # 완전 포함 일치
            if name_clean and (name_clean in item_name_clean or item_name_clean in name_clean):
                print(f"Naver exact match: {item_name}")
                return _item_to_dict(item)

            # 퍼지 유사도
            if name_clean and item_name_clean:
                score = _name_similarity(name_clean, item_name_clean)
                if score > best_score:
                    best_score = score
                    best_item = item

        # 유사도 0.75 이상이면 채택 (오타 허용, 완전 다른 이름은 거부)
        if best_item and best_score >= 0.75:
            item_name = re.sub(r'<[^>]+>', '', best_item.get("title", ""))
            print(f"Naver fuzzy match ({best_score:.2f}): {item_name}")
            return _item_to_dict(best_item)

    return None


def _item_to_dict(item: dict) -> dict:
    lat = int(item["mapy"]) / 1e7
    lng = int(item["mapx"]) / 1e7
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


def get_coordinates(address: str, name: str = None,
                    user_lat: float = None, user_lng: float = None) -> dict | None:
    if name:
        result = naver_local_search_best(name, address, user_lat, user_lng)
        if result:
            return result
    if address and address != "null":
        return nominatim_search(address)
    return None


def get_naver_place_id_by_search(name: str, lat: float = None, lng: float = None,
                                  address: str = None) -> str | None:
    """search.naver.com 장소탭 스크래핑으로 PlaceID 조회 (주소 포함 검색으로 정확한 지점 특정)"""
    # 주소가 있으면 "{이름} {주소}" 로 검색하여 동명 다른 지점과 구분
    query = f"{name} {address}" if address else name
    try:
        res = requests.get(
            "https://search.naver.com/search.naver",
            params={"query": query, "where": "place"},
            headers={
                "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36",
                "Accept-Language": "ko-KR,ko;q=0.9",
            },
            timeout=8
        )
        if res.status_code != 200:
            print(f"NaverSearch HTTP {res.status_code}")
            return None
        html = res.text
        name_clean = re.sub(r'[^가-힣a-zA-Z0-9]', '', name).lower()

        # id + name 쌍 추출 후 이름 매칭
        pairs = re.findall(r'"id"\s*:\s*"([0-9]{5,12})"[^}]{0,300}"name"\s*:\s*"([^"]{1,40})"', html)
        for pid, pname in pairs:
            pname_clean = re.sub(r'[^가-힣a-zA-Z0-9]', '', pname).lower()
            if (name_clean in pname_clean or pname_clean in name_clean or
                    _name_similarity(name_clean, pname_clean) >= 0.55):
                print(f"NaverSearch place_id: {pid} ({pname})")
                return pid

        # 이름 매칭 실패 → place URL에서 첫 번째 ID
        ids = list(dict.fromkeys(re.findall(r'place\.naver\.com/[^"/]+/([0-9]{5,12})', html)))
        if ids:
            print(f"NaverSearch place_id (fallback): {ids[0]}")
            return ids[0]
    except Exception as e:
        print(f"NaverSearch place_id error: {e}")
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


def get_naver_place_menu(place_id: str) -> list[str]:
    """네이버 플레이스에서 대표 메뉴 가져오기"""
    if not place_id:
        return []
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36",
        "Referer": "https://map.naver.com/",
    }
    # API 시도
    try:
        res = requests.get(
            f"https://map.naver.com/v5/api/sites/summary/{place_id}",
            headers=headers, timeout=8
        )
        if res.status_code == 200:
            data = res.json()
            menus = []
            for section in data.get("menus", []):
                for item in section.get("menus", []) if isinstance(section, dict) else []:
                    n = item.get("name", "")
                    p = item.get("price")
                    if n:
                        menus.append(f"{n} {int(p):,}원" if p else n)
                if menus:
                    break
            # 직접 menus 리스트인 경우
            if not menus and isinstance(data.get("menus"), list):
                for item in data["menus"][:5]:
                    if isinstance(item, dict):
                        n = item.get("name", "")
                        p = item.get("price")
                        if n:
                            menus.append(f"{n} {int(p):,}원" if p else n)
            if menus:
                print(f"Menu from summary: {menus[:5]}")
                return menus[:5]
    except Exception as e:
        print(f"Menu summary error: {e}")

    # 모바일 페이지 스크래핑
    try:
        for path in [f"restaurant/{place_id}/menu/foods", f"place/{place_id}/menu"]:
            res = requests.get(
                f"https://m.place.naver.com/{path}",
                headers=headers, timeout=8
            )
            if res.status_code != 200:
                continue
            res.encoding = 'utf-8'
            # JSON 데이터에서 메뉴 추출
            matches = re.findall(r'"name"\s*:\s*"([^"]{1,30})"[^}]*?"price"\s*:\s*"?(\d+)"?', res.text)
            if matches:
                menus = [f"{n} {int(p):,}원" for n, p in matches[:5] if n and p]
                if menus:
                    print(f"Menu scraped: {menus}")
                    return menus
            # price 없이 이름만
            name_matches = re.findall(r'"menuName"\s*:\s*"([^"]{1,30})"', res.text)
            if name_matches:
                print(f"Menu names scraped: {name_matches[:5]}")
                return name_matches[:5]
    except Exception as e:
        print(f"Menu scrape error: {e}")
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

위 내용만을 바탕으로 핵심을 한 줄로 요약하세요.
웨이팅, 맛, 가격, 분위기, 주의사항 등 핵심 정보를 담아 30자 이내 한국어로.
예시: "주말 웨이팅 필수, 파스타 맛있고 가성비 좋음"
요약만 출력:
"""
    for model in ["gemini-2.5-flash", "gemini-1.5-flash", "gemini-1.5-pro"]:
        try:
            response = client.models.generate_content(model=model, contents=prompt)
            summary = response.text.strip().strip('"')
            return {"success": True, "summary": summary}
        except Exception as e:
            err = str(e)
            print(f"Review Gemini error ({model}): {err[:120]}")
            if "503" in err or "429" in err or "unavailable" in err.lower():
                time.sleep(2)
                continue
            return {"success": False, "message": err}
    return {"success": False, "message": "Gemini 서버 과부하, 잠시 후 재시도"}


@app.post("/analyze")
def analyze_reel(request: AnalysisRequest):
    print(f"\n{'='*50}\nAnalyzing: {request.url}")

    text_content, location_tag, thumbnail_url = get_reel_info(request.url)
    if not text_content:
        return {"success": False, "message": "릴스 정보 추출 실패. 쿠키가 만료됐거나 Instagram 로그인이 필요합니다. Chrome을 완전히 종료 후 재시도하거나 cookies.txt를 재발급하세요."}

    analysis_result = analyze_text_with_gemini(text_content)
    if not isinstance(analysis_result, dict) or "error" in analysis_result:
        return {"success": False, "message": analysis_result.get("error", "Gemini analysis failed")}

    restaurants_raw = analysis_result.get("restaurants", [])
    if not restaurants_raw:
        return {"success": False, "message": "No restaurants found in reel."}

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
    frame_info = {}
    if needs_fallback and not location_tag:
        print("Fallback: analyzing video frames...")
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
                # 캡션에 메뉴가 없을 때만 네이버 대표메뉴로 채움
                if not restaurant.get("menu"):
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
