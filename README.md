# kr-public-data-mcp

한국 공공데이터(날씨·미세먼지)를 Claude Code가 도구로 직접 호출할 수 있게 노출하는 MCP stdio 서버.

## 설치

```bash
git clone <repo-url> && cd kr-public-data-mcp
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env   # PUBLIC_DATA_API_KEY 채우기 (data.go.kr 발급키)
```

## Claude Code에 등록

```bash
claude mcp add kr-public-data-mcp -- python C:\path\to\kr-public-data-mcp\server.py
```

이후 Claude에게 자연어로 물으면 됩니다: "지금 성남 날씨 어때?", "대전 미세먼지는?"

## 도구

| 도구 | 설명 | 예 |
|---|---|---|
| `get_weather(지역)` | 실시간 날씨(기온·습도·강수) — 기상청 초단기실황 | `get_weather("성남")` |
| `get_air_quality(지역)` | 실시간 미세먼지(PM10/PM2.5) — 에어코리아 시도별 실시간 측정정보 | `get_air_quality("성남")` |
| `search_station(이름)` | 이름/주소로 대기오염 측정소 검색 | `search_station("성남")` |

## 동작 방식

- **지역 해석**: Open-Meteo 지오코딩(키 불필요)으로 지역명 → 위경도, 자체 구현한 LCC(Lambert Conformal Conic) 투영식으로 위경도 → 기상청 격자(nx,ny) 변환. 별도 좌표 CSV 없음.
- **캐시**: 10분 TTL(`functools.lru_cache` + 시간버킷 키). 같은 지역을 10분 안에 다시 물으면 API 호출 없이 캐시로 응답.
- **에러**: 키 없으면 시작 즉시 종료 / 지역을 못 찾으면 사람이 읽는 메시지 / 상위 API 실패 시 상태코드만 담은 안전한 메시지(키가 URL 쿼리파라미터에 실리는 API라 원본 예외를 그대로 노출하지 않음).

## 준비물

data.go.kr 계정 키 하나로 아래 두 서비스에 **각각 활용신청**이 돼 있어야 합니다(키는 계정당 1개, 서비스별로 활용신청만 추가):
- 기상청_단기예보 조회서비스 (`getUltraSrtNcst`)
- 한국환경공단_에어코리아_대기오염정보 (`getCtprvnRltmMesureDnsty`, `getMsrstnList`)

## 데모

(GIF 자리 — "성남 날씨/미세먼지" 질의 → 응답, 같은 질문 재요청 시 캐시로 즉시 응답하는 모습)
