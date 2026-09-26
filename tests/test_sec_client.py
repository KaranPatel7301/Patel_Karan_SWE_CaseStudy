import httpx

from app.ingest.sec_client import SecClient


def test_client_rejects_blank_user_agent() -> None:
    try:
        SecClient("   ")
    except ValueError as exc:
        assert "SEC_USER_AGENT" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_client_sends_user_agent_and_retries_429() -> None:
    calls = {"n": 0}
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["User-Agent"] == "Karan Patel test@example.com"
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429)
        return httpx.Response(200, json={"cik": 1})

    with SecClient(
        "Karan Patel test@example.com",
        min_interval=0,
        transport=httpx.MockTransport(handler),
        sleeper=sleeps.append,
    ) as client:
        payload = client.company_facts("0000000001")

    assert payload == {"cik": 1}
    assert calls["n"] == 2
    assert sleeps == [1.0]


def test_filing_document_url() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text="<html></html>")

    with SecClient(
        "Karan Patel test@example.com",
        min_interval=0,
        transport=httpx.MockTransport(handler),
        sleeper=lambda _delay: None,
    ) as client:
        body = client.filing_document(
            "0001045810",
            "0001045810-26-000021",
            "nvda-20260125.htm",
        )

    assert body == "<html></html>"
    assert seen == [
        "https://www.sec.gov/Archives/edgar/data/1045810/000104581026000021/nvda-20260125.htm"
    ]
