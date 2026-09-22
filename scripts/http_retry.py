"""
http_retry.py — 일시적 네트워크 오류(connection reset 등) 자동 재시도 세션.

배경(2026-09-22 실사고): export_articles.py가 Supabase REST 호출 중 일시적
"Connection reset by peer" 하나에 재시도 없이 그대로 죽어, 그 사이클의
웹사이트 반영·커밋/푸시가 통째로 스킵됐다. 같은 시간대 rss_collector.py 등
다른 스크립트는 이미 자체 재시도 로직으로 같은 종류의 일시적 오류를
버텨냈던 것과 대조적 — 스크립트마다 재시도를 새로 짜는 대신 공용화한다.

사용:
    from http_retry import get_session
    res = get_session().get(url, ...)   # requests.get()과 동일하게 쓰되
                                          # connection reset/5xx는 자동 재시도
"""
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_session = None


def get_session() -> requests.Session:
    """모듈 전역에서 세션 하나를 재사용(커넥션 풀 재사용 이점도 겸함)."""
    global _session
    if _session is not None:
        return _session
    s = requests.Session()
    retry = Retry(
        total=3, connect=3, read=3,
        backoff_factor=1.5,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=frozenset(("GET", "POST", "PATCH", "DELETE", "PUT")),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    _session = s
    return s
