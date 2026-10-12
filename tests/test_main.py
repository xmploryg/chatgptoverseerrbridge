import json
import os
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("OVERSEERR_URL", "http://seerr.test")
os.environ.setdefault("OVERSEERR_API_KEY", "test-key")
os.environ.setdefault("BRIDGE_TOKEN", "test-token")
os.environ.setdefault("MCP_PATH_TOKEN", "mcp-secret")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import main  # noqa: E402

AUTH = {"Authorization": "Bearer test-token"}

OLD_TI = {"id": 1001, "mediaType": "tv", "name": "Treasure Island", "firstAirDate": "2012-01-01"}
NEW_TI = {"id": 2002, "mediaType": "tv", "name": "Treasure Island", "firstAirDate": "2026-10-04"}
MOVIE_TI = {"id": 3003, "mediaType": "movie", "title": "Treasure Island", "releaseDate": "1950-07-19"}


class FakeSeerr:
    """Records requests and serves canned Overseerr responses."""

    def __init__(self, pages, tv_details=None, request_status=201):
        self.pages = pages
        self.tv_details = tv_details or {}
        self.request_status = request_status
        self.posted = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/search":
            page = int(request.url.params["page"])
            results = self.pages[page - 1] if page <= len(self.pages) else []
            return httpx.Response(200, json={"results": results, "totalPages": len(self.pages)})
        if path.startswith("/api/v1/tv/"):
            tmdb_id = int(path.rsplit("/", 1)[1])
            if tmdb_id not in self.tv_details:
                return httpx.Response(404)
            return httpx.Response(200, json=self.tv_details[tmdb_id])
        if path == "/api/v1/request":
            self.posted.append(json.loads(request.content))
            return httpx.Response(self.request_status, json={})
        return httpx.Response(500)


@pytest.fixture
def seerr(monkeypatch):
    def install(fake):
        real = httpx.AsyncClient

        def patched(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(fake.handler)
            return real(*args, **kwargs)

        monkeypatch.setattr(main.httpx, "AsyncClient", patched)
        return fake

    return install


client = TestClient(main.app)


def test_rejects_bad_token():
    resp = client.post("/request", json={"query": "x"}, headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401


def test_request_without_year_keeps_first_match(seerr):
    fake = seerr(FakeSeerr([[OLD_TI, NEW_TI]], tv_details={1001: {"seasons": [{"seasonNumber": 1}]}}))
    resp = client.post("/request", json={"query": "Treasure Island", "media_type": "tv"}, headers=AUTH)
    assert resp.json()["status"] == "requested"
    assert fake.posted[0]["mediaId"] == 1001


def test_request_with_year_picks_matching_release(seerr):
    fake = seerr(FakeSeerr(
        [[OLD_TI, MOVIE_TI], [NEW_TI]],
        tv_details={2002: {"seasons": [{"seasonNumber": 0}, {"seasonNumber": 1}],
                           "externalIds": {"tvdbId": 555}}},
    ))
    resp = client.post("/request", json={"query": "Treasure Island", "media_type": "tv", "year": 2026},
                       headers=AUTH)
    body = resp.json()
    assert body["status"] == "requested"
    assert body["year"] == 2026
    assert fake.posted == [{"mediaType": "tv", "mediaId": 2002, "seasons": [1], "tvdbId": 555}]


def test_request_by_tmdb_id_skips_search(seerr):
    fake = seerr(FakeSeerr([], tv_details={2002: {**NEW_TI, "seasons": [{"seasonNumber": 1}]}}))
    resp = client.post("/request", json={"tmdb_id": 2002, "media_type": "tv"}, headers=AUTH)
    assert resp.json()["status"] == "requested"
    assert fake.posted[0]["mediaId"] == 2002


def test_request_skips_already_available(seerr):
    fake = seerr(FakeSeerr([[{**NEW_TI, "mediaInfo": {"status": 5}}]]))
    resp = client.post("/request", json={"query": "Treasure Island", "media_type": "tv", "year": 2026},
                       headers=AUTH)
    assert resp.json()["status"] == "already_exists"
    assert fake.posted == []


def test_request_year_not_found(seerr):
    seerr(FakeSeerr([[OLD_TI]]))
    resp = client.post("/request", json={"query": "Treasure Island", "media_type": "tv", "year": 2030},
                       headers=AUTH)
    assert resp.json()["status"] == "not_found"


def test_request_needs_query_or_id():
    resp = client.post("/request", json={"media_type": "tv"}, headers=AUTH)
    assert resp.status_code == 400


def test_search_lists_candidates_with_year_and_status(seerr):
    seerr(FakeSeerr([[OLD_TI, MOVIE_TI, {**NEW_TI, "mediaInfo": {"status": 3}}]]))
    resp = client.get("/search", params={"query": "Treasure Island", "media_type": "tv"}, headers=AUTH)
    results = resp.json()["results"]
    assert [(r["tmdb_id"], r["year"], r["status"]) for r in results] == [
        (1001, 2012, "not_requested"),
        (2002, 2026, "processing"),
    ]


def test_status_reports_media_state(seerr):
    seerr(FakeSeerr([], tv_details={2002: {**NEW_TI, "mediaInfo": {"status": 4}}}))
    resp = client.get("/status", params={"tmdb_id": 2002, "media_type": "tv"}, headers=AUTH)
    assert resp.json()["status"] == "partially_available"


@pytest.fixture(scope="module")
def mcp_client():
    # The MCP session manager can only start once per process, like in production.
    with TestClient(main.app) as c:
        yield c


MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _mcp_call(c, method, params, path="/mcp/mcp-secret/"):
    resp = c.post(path, headers=MCP_HEADERS, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    assert resp.status_code == 200, resp.text
    if resp.headers["content-type"].startswith("text/event-stream"):
        data = [line[5:] for line in resp.text.splitlines() if line.startswith("data:")]
        return json.loads(data[-1])
    return resp.json()


def _tool_result(reply):
    return json.loads(reply["result"]["content"][0]["text"])


def test_mcp_lists_tools(mcp_client):
    reply = _mcp_call(mcp_client, "tools/list", {})
    names = {tool["name"] for tool in reply["result"]["tools"]}
    assert names == {"search_media", "request_media", "get_request_status"}


def test_mcp_request_tool_submits_by_tmdb_id(seerr, mcp_client):
    fake = seerr(FakeSeerr([], tv_details={2002: {**NEW_TI, "seasons": [{"seasonNumber": 1}]}}))
    reply = _mcp_call(mcp_client, "tools/call", {"name": "request_media",
                                                 "arguments": {"tmdb_id": 2002, "media_type": "tv"}})
    assert _tool_result(reply)["status"] == "requested"
    assert fake.posted[0]["mediaId"] == 2002


def test_mcp_tool_reports_errors_without_raising(seerr, mcp_client):
    seerr(FakeSeerr([]))
    reply = _mcp_call(mcp_client, "tools/call", {"name": "get_request_status",
                                                 "arguments": {"tmdb_id": 9999, "media_type": "tv"}})
    assert _tool_result(reply)["status"] == "error"


def test_mcp_wrong_path_is_404(mcp_client):
    resp = mcp_client.post("/mcp/guess/", headers=MCP_HEADERS,
                           json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert resp.status_code == 404
