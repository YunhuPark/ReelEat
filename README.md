# ReelEat 🍽️

인스타그램 릴스 URL을 분석해 맛집 정보(위치, 메뉴, 리뷰)를 자동으로 추출하는 서비스입니다.

## 주요 기능

- 인스타그램 릴스 URL 입력 → 식당 이름, 위치, 메뉴 자동 추출
- 네이버 지도 연동으로 실제 메뉴 및 리뷰 요약 제공
- GPS 기반 가장 가까운 지점 매칭 (프랜차이즈 대응)
- 캡션 없는 릴스도 영상 프레임 분석으로 위치 추정

## 기술 스택

| 항목 | 내용 |
|------|------|
| Backend | FastAPI (Python) |
| AI 분석 | Google Gemini API |
| 영상 추출 | yt-dlp |
| 맛집 검색 | 네이버 지역 검색 API |
| 배포 | Railway |
| 앱 클라이언트 | Flutter (Android) |

## API 엔드포인트

### `POST /analyze`
릴스 URL 분석 후 식당 정보 반환

**Request**
```json
{
  "url": "https://www.instagram.com/reel/XXXXX/",
  "user_lat": 37.123,
  "user_lng": 127.456
}
```

**Response**
```json
{
  "success": true,
  "name": "태연반점",
  "category": "중식",
  "address": "경기 포천시 소흘읍 이동교리 406",
  "menu": ["짬짜면 13,000원", "볶짜면 15,000원"],
  "lat": 37.89,
  "lng": 127.12,
  "place_id": "12345678",
  "thumbnail_url": "https://..."
}
```

### `POST /review_summary`
네이버 블로그 리뷰 AI 요약

**Request**
```json
{ "place_id": "12345678", "name": "태연반점" }
```

### `GET /health`
서버 상태 확인

## 환경 변수

Railway Variables 탭에서 설정:

| 키 | 설명 |
|----|------|
| `GEMINI_API_KEY` | Google Gemini API 키 |
| `NAVER_CLIENT_ID` | 네이버 검색 API Client ID |
| `NAVER_CLIENT_SECRET` | 네이버 검색 API Secret |
| `INSTAGRAM_COOKIES_B64` | Instagram 쿠키 (Base64 인코딩) |

### 쿠키 갱신 방법 (주기적으로 필요)

Instagram 쿠키는 수개월 주기로 만료됩니다. 만료 시 아래 절차로 갱신하세요.

1. Chrome에서 Instagram 로그인 후 완전히 종료
2. 아래 명령어로 쿠키 추출 및 Base64 인코딩:
```powershell
yt-dlp --cookies-from-browser chrome --cookies cookies.txt "https://www.instagram.com/reel/any/"
$b64 = [Convert]::ToBase64String([System.IO.File]::ReadAllBytes("cookies.txt"))
Write-Output $b64
```
3. 출력된 값을 Railway → Variables → `INSTAGRAM_COOKIES_B64` 에 업데이트

## 로컬 실행

```bash
pip install -r requirements.txt
# .env 파일에 환경변수 설정 후
uvicorn main:app --reload
```
