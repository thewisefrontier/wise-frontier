# -*- coding: utf-8 -*-
"""
http_retry.py 재시도 설정 회귀 테스트 (2026-09-23 신설).

실사고: 병렬 워커(rss_processor.py)가 텔레그램에 거의 동시 발송하며 429를
대량으로 맞았는데 status_forcelist에 429가 빠져 있어 하나도 재시도가
안 됐다(1000건 중 845건 실패). 실제 HTTP 계층까지 목킹하지 않고, urllib3
Retry 설정 자체가 올바른지만 가볍게 검증한다(과한 목킹은 불필요).

실행: python scripts/test_http_retry.py
"""
import http_retry as hr


def test_429_in_retry_status_forcelist():
    session = hr.get_session()
    adapter = session.get_adapter("https://example.com")
    retry = adapter.max_retries
    assert 429 in retry.status_forcelist, "429가 재시도 대상에서 빠져 있다"


def test_5xx_still_covered():
    """429 추가하면서 기존 5xx 커버리지를 깨지 않았는지."""
    session = hr.get_session()
    retry = session.get_adapter("https://example.com").max_retries
    for code in (500, 502, 503, 504):
        assert code in retry.status_forcelist, f"{code} 누락"


def test_respects_retry_after_header():
    """텔레그램 429 응답의 Retry-After를 실제로 존중하는지(자체 백오프로
    대체돼 실측 대기시간이 안 맞는 사고 방지)."""
    session = hr.get_session()
    retry = session.get_adapter("https://example.com").max_retries
    assert retry.respect_retry_after_header is True


def test_session_reused_singleton():
    """매번 새 세션을 만들면 커넥션 풀 재사용 이점이 없어진다."""
    assert hr.get_session() is hr.get_session()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)}건 통과")
