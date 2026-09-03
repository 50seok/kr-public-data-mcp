"""kr-public-data-mcp — 한국 공공데이터(날씨·미세먼지)를 MCP 도구로 노출하는 stdio 서버.

지역명 -> 위경도(Open-Meteo, study/lap0/weather.py 이식) -> KMA 격자(LCC 투영, 신규)로
기상청 초단기실황을 조회하고, 시->시도 매핑으로 에어코리아 시도별 실시간 측정정보를 조회한다.
"""
import datetime as dt
import math
import os
import sys
import time
from functools import lru_cache
from pathlib import Path

import httpx
import requests
from dotenv import load_dotenv

# ponytail: Windows 콘솔이 UTF-8이 아니면 한글 에러 메시지가 깨진다 — 실측으로 확인된 문제라 방어해둔다.
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

# claude mcp add로 등록되면 실행 cwd가 이 파일 위치와 다를 수 있다 — 스크립트 옆 .env를 명시적으로 찾는다.
load_dotenv(Path(__file__).parent / ".env")

API_KEY = os.environ.get("PUBLIC_DATA_API_KEY")
if not API_KEY:
    print("PUBLIC_DATA_API_KEY 환경변수가 없습니다. .env.example을 .env로 복사하고 키를 채우세요.", file=sys.stderr)
    sys.exit(1)

CACHE_TTL = 600  # 10분

# --- 지역명 -> 위경도 (study/lap0/weather.py의 geocode() 이식 + 실측 보정) ---
GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"

# ponytail: 실측 확인 — Open-Meteo 한국 지명 색인이 들쭉날쭉하다.
# "성남"처럼 접미사 없이 치면 북한 동명 촌락이 1순위로 잡히는 경우가 있고(→ country=KR로 방어),
# 시 단위는 보통 "OO시"를 붙여야 잡히지만 일부는 거꾸로 접미사를 붙이면 안 잡힌다.
# CITY_TO_PROVINCE 50개 전수 실측 후 아래만 예외로 등록, 나머지는 "+시" 휴리스틱.
ALIASES = {
    "서울": "서울특별시", "부산": "부산광역시", "대구": "대구광역시", "인천": "인천광역시",
    "광주": "광주광역시", "대전": "대전광역시", "울산": "울산광역시", "세종": "세종",
    "용인": "Yongin-si",  # "용인"/"용인시" 둘 다 색인에 없음(실측 확인), 로마자 표기만 잡힘
    # 아래는 접미사 없이 바로 쳐야 잡힘("OO시"로 치면 EMPTY, 실측 확인)
    "파주": "파주", "충주": "충주", "목포": "목포", "순천": "순천", "포항": "포항", "양산": "양산",
}

# 지오코더가 접미사를 붙여도/빼도 못 찾는 극소수 예외 — 좌표를 직접 박아둔다(시청 근방, 5km 격자라 오차 무관)
KNOWN_COORDS = {
    "시흥": (37.3800, 126.8028),
    "서귀포": (33.2541, 126.5601),
}


def geocode(region: str) -> dict:
    """지역명 -> {"name","lat","lon"}. 못 찾으면 ValueError."""
    if region in KNOWN_COORDS:
        lat, lon = KNOWN_COORDS[region]
        return {"name": region, "lat": lat, "lon": lon}

    query = ALIASES.get(region)
    if query is None:
        query = region if region.endswith(("시", "군", "구", "도")) else f"{region}시"
    r = httpx.get(GEO_URL, params={"name": query, "count": 1, "language": "ko", "country": "KR"}, timeout=10)
    r.raise_for_status()
    results = r.json().get("results")
    if not results:
        raise ValueError(f"지역을 찾지 못했습니다: {region} (예: 서울, 성남, 대전)")
    top = results[0]
    return {"name": top["name"], "lat": top["latitude"], "lon": top["longitude"]}


# --- 위경도 -> KMA 격자(nx, ny): Lambert Conformal Conic 투영 ---
# 기상청 공개 변환식(RE·GRID·SLAT1·SLAT2·OLON·OLAT·XO·YO는 기상청 문서 고정값)
_RE, _GRID = 6371.00877, 5.0
_SLAT1, _SLAT2 = 30.0, 60.0
_OLON, _OLAT = 126.0, 38.0
_XO, _YO = 43, 136
_DEGRAD = math.pi / 180.0


def latlon_to_grid(lat: float, lon: float) -> tuple[int, int]:
    re = _RE / _GRID
    slat1, slat2 = _SLAT1 * _DEGRAD, _SLAT2 * _DEGRAD
    olon, olat = _OLON * _DEGRAD, _OLAT * _DEGRAD

    sn = math.tan(math.pi * 0.25 + slat2 * 0.5) / math.tan(math.pi * 0.25 + slat1 * 0.5)
    sn = math.log(math.cos(slat1) / math.cos(slat2)) / math.log(sn)
    sf = math.tan(math.pi * 0.25 + slat1 * 0.5)
    sf = math.pow(sf, sn) * math.cos(slat1) / sn
    ro = re * sf / math.pow(math.tan(math.pi * 0.25 + olat * 0.5), sn)

    ra = re * sf / math.pow(math.tan(math.pi * 0.25 + lat * _DEGRAD * 0.5), sn)
    theta = lon * _DEGRAD - olon
    if theta > math.pi:
        theta -= 2.0 * math.pi
    if theta < -math.pi:
        theta += 2.0 * math.pi
    theta *= sn

    x = math.floor(ra * math.sin(theta) + _XO + 0.5)
    y = math.floor(ro - ra * math.cos(theta) + _YO + 0.5)
    return int(x), int(y)


# --- 기상청 초단기실황(getUltraSrtNcst) ---
KMA_URL = "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getUltraSrtNcst"
_CATEGORY_NAMES = {"T1H": "기온(℃)", "REH": "습도(%)", "RN1": "1시간강수량(mm)", "WSD": "풍속(m/s)"}
_PTY_NAMES = {"0": "없음", "1": "비", "2": "비/눈", "3": "눈", "5": "빗방울", "6": "빗방울눈날림", "7": "눈날림"}


def _base_datetime(now: dt.datetime | None = None) -> tuple[str, str]:
    """초단기실황은 매시 40분에 생성, 10분 뒤부터 조회 가능 -> 아직 안 나온 시각이면 한 시간 전 정시로."""
    now = now or dt.datetime.now()
    if now.minute < 40:
        now -= dt.timedelta(hours=1)
    base = now.replace(minute=0, second=0, microsecond=0)
    return base.strftime("%Y%m%d"), base.strftime("%H00")


def _kma_weather(nx: int, ny: int) -> dict:
    base_date, base_time = _base_datetime()
    resp = requests.get(
        KMA_URL,
        params={
            "serviceKey": API_KEY,
            "pageNo": 1,
            "numOfRows": 10,
            "dataType": "JSON",
            "base_date": base_date,
            "base_time": base_time,
            "nx": nx,
            "ny": ny,
        },
        timeout=10,
    )
    resp.raise_for_status()
    body = resp.json()["response"]["body"]
    # _search_station과 동일한 이유: 기상청 API도 HTTP 200에 에러 바디를 실어 보낼 때가
    # 있다(서비스키 미승인, 파라미터 오류 등). 그때 "items"가 없거나 빈 문자열이라
    # body["items"]["item"] 그대로 인덱싱하면 KeyError/TypeError가 _safe()를 뚫고 나간다.
    items = (body.get("items") or {}).get("item") or []
    if not items:
        raise ValueError("기상청 API가 관측값을 반환하지 않았습니다 — 서비스키 승인 상태를 확인하세요")
    values = {it["category"]: it["obsrValue"] for it in items}
    out = {_CATEGORY_NAMES[k]: v for k, v in values.items() if k in _CATEGORY_NAMES}
    if "PTY" in values:
        out["강수형태"] = _PTY_NAMES.get(values["PTY"], values["PTY"])
    return out


# --- 에어코리아 시도별 실시간 측정정보(getCtprvnRltmMesureDnsty) ---
AIR_SIDO_URL = "https://apis.data.go.kr/B552584/ArpltnInforInqireSvc/getCtprvnRltmMesureDnsty"
AIR_STATION_URL = "https://apis.data.go.kr/B552584/MsrstnInfoInqireSvc/getMsrstnList"

# ponytail: 주요 시 -> 시도 매핑. 목록에 없는 지역은 시도명 그대로 시도해본다(폴백).
CITY_TO_PROVINCE = {
    "서울": "서울", "부산": "부산", "대구": "대구", "인천": "인천", "광주": "광주",
    "대전": "대전", "울산": "울산", "세종": "세종",
    "수원": "경기", "성남": "경기", "용인": "경기", "고양": "경기", "부천": "경기",
    "안산": "경기", "안양": "경기", "남양주": "경기", "화성": "경기", "평택": "경기",
    "의정부": "경기", "시흥": "경기", "파주": "경기", "김포": "경기", "광명": "경기",
    "춘천": "강원", "원주": "강원", "강릉": "강원",
    "청주": "충북", "충주": "충북",
    "천안": "충남", "아산": "충남", "서산": "충남",
    "전주": "전북", "군산": "전북", "익산": "전북",
    "목포": "전남", "여수": "전남", "순천": "전남",
    "포항": "경북", "구미": "경북", "경주": "경북", "안동": "경북",
    "창원": "경남", "김해": "경남", "진주": "경남", "양산": "경남",
    "제주": "제주", "서귀포": "제주",
}


def _air_quality(sido: str) -> list[dict]:
    resp = requests.get(
        AIR_SIDO_URL,
        params={"serviceKey": API_KEY, "returnType": "json", "numOfRows": 100, "pageNo": 1, "sidoName": sido, "ver": "1.3"},
        timeout=10,
    )
    resp.raise_for_status()
    body = resp.json()["response"]["body"]
    items = body.get("items") or []
    if not items:
        raise ValueError(f"'{sido}' 시도의 측정 데이터가 없습니다")
    return items


def _search_station(name: str) -> list[dict]:
    resp = requests.get(
        AIR_STATION_URL,
        params={"serviceKey": API_KEY, "returnType": "json", "addr": name},
        timeout=10,
    )
    resp.raise_for_status()
    body = resp.json()["response"]["body"]
    items = body.get("items") or []
    if not items:
        raise ValueError(f"'{name}'으로 찾은 측정소가 없습니다")
    return items


# --- 10분 TTL 캐시 (functools.lru_cache + 시간버킷 키) ---
def _bucket() -> int:
    return int(time.time() // CACHE_TTL)


@lru_cache(maxsize=128)
def _get_weather_cached(region: str, _bucket_key: int) -> str:
    geo = geocode(region)
    nx, ny = latlon_to_grid(geo["lat"], geo["lon"])
    values = _kma_weather(nx, ny)
    parts = ", ".join(f"{k} {v}" for k, v in values.items())
    return f"{region}({geo['name']}) 현재: {parts}"


@lru_cache(maxsize=128)
def _get_air_quality_cached(region: str, _bucket_key: int) -> str:
    sido = CITY_TO_PROVINCE.get(region, region)
    items = _air_quality(sido)
    it = items[0]
    return (
        f"{region}({it['stationName']} 측정소) 미세먼지: "
        f"PM10 {it.get('pm10Value', '-')}㎍/㎥, PM2.5 {it.get('pm25Value', '-')}㎍/㎥, "
        f"통합대기환경지수 {it.get('khaiGrade', '-')}등급"
    )


@lru_cache(maxsize=128)
def _search_station_cached(name: str, _bucket_key: int) -> str:
    items = _search_station(name)
    names = ", ".join(it["stationName"] for it in items[:5])
    return f"'{name}' 검색 결과 측정소: {names}"


def _safe(fn, *args) -> str:
    try:
        return fn(*args, _bucket())
    except ValueError as e:
        return f"오류: {e}"
    except (requests.RequestException, httpx.HTTPError) as e:
        # 주의: requests/httpx 예외를 str()하면 요청 URL이 그대로 찍히고, 이 API들은
        # 키를 쿼리파라미터로 받으므로 str(e)에 PUBLIC_DATA_API_KEY가 노출된다. 절대 str(e) 반환 금지.
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status in (401, 403):
            return "API 호출 실패: 인증 거부(401/403) — data.go.kr에서 이 서비스 활용신청이 승인됐는지 확인하세요."
        if status:
            return f"API 호출 실패: 상위 서비스 오류(HTTP {status})"
        return "API 호출 실패: 네트워크 오류 또는 상위 서비스 응답 없음"


# --- MCP 도구 3개 ---
from mcp.server.mcpserver import MCPServer  # noqa: E402  (API_KEY 체크가 import보다 먼저여야 함)

mcp = MCPServer("kr-public-data-mcp")


# ponytail: 실측으로 확인된 버그 — 파라미터명이 한글(지역/이름)이면 Anthropic 도구 스키마
# 검증을 통과 못 해 도구 자체가 목록에서 빠진다. 파라미터는 영문, 설명만 한글로 둔다.
@mcp.tool()
def get_weather(region: str) -> str:
    """한국 지역의 실시간 날씨(기온·습도·강수)를 조회한다. 예: region="성남" """
    return _safe(_get_weather_cached, region)


@mcp.tool()
def get_air_quality(region: str) -> str:
    """한국 지역의 실시간 미세먼지(PM10/PM2.5)를 조회한다. 예: region="성남" """
    return _safe(_get_air_quality_cached, region)


@mcp.tool()
def search_station(name: str) -> str:
    """이름/주소 일부로 대기오염 측정소를 검색한다. 예: name="성남" """
    return _safe(_search_station_cached, name)


def _selfcheck() -> None:
    """네트워크 없이 검증 가능한 핵심 로직(격자 변환) 자체 점검. 틀리면 서버 시작 전에 죽는다."""
    nx, ny = latlon_to_grid(37.5665, 126.9780)  # 서울시청
    assert (nx, ny) == (60, 127), f"LCC 격자 변환이 틀렸습니다: 서울 기대값(60,127), 실제 ({nx},{ny})"


if __name__ == "__main__":
    _selfcheck()
    mcp.run()
