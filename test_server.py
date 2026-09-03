"""server.py 방어 로직 검증. 네트워크 호출 없음 (requests.get 모킹).

    python test_server.py
"""
from unittest.mock import patch, MagicMock

import server


def _mock_resp(body_json):
    r = MagicMock()
    r.raise_for_status.return_value = None
    r.json.return_value = {"response": {"body": body_json}}
    return r


def test_kma_weather_missing_items_raises_valueerror():
    """서비스키 미승인 등으로 200 + 에러 바디가 오면 KeyError가 아니라 ValueError여야 _safe()가 잡는다."""
    with patch.object(server.requests, "get", return_value=_mock_resp({})):
        try:
            server._kma_weather(60, 127)
        except ValueError:
            pass
        except KeyError:
            raise AssertionError("KeyError가 새어나감 — _safe()를 뚫고 나가 도구 호출이 죽는다")
        else:
            raise AssertionError("빈 items면 예외가 나야 함")


def test_kma_weather_normal_body_still_parses():
    body = {"items": {"item": [
        {"category": "T1H", "obsrValue": "23.4"},
        {"category": "REH", "obsrValue": "55"},
    ]}}
    with patch.object(server.requests, "get", return_value=_mock_resp(body)):
        out = server._kma_weather(60, 127)
    assert out.get("기온(℃)") == "23.4"
    assert out.get("습도(%)") == "55"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("PASS", t.__name__)
    print("%d/%d PASS" % (len(tests), len(tests)))
