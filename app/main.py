import os
import contextlib
import httpx
from typing import Literal, Optional
from urllib.parse import quote
from fastapi import FastAPI, HTTPException, Security, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

OVERSEERR_URL = os.environ["OVERSEERR_URL"]       # e.g. http://overseerr.overseerr.svc.cluster.local:5055
OVERSEERR_API_KEY = os.environ["OVERSEERR_API_KEY"]
BRIDGE_TOKEN = os.environ["BRIDGE_TOKEN"]          # secret token clients send in Authorization header
# Optional. When set, an MCP server (for ChatGPT plugins) is served at /mcp/<token>/.
# ChatGPT custom MCP connections cannot send a bearer header, so the path is the secret.
MCP_PATH_TOKEN = os.environ.get("MCP_PATH_TOKEN", "")

# How many Overseerr search pages to scan when filtering by year. Popular old
# titles (e.g. "Treasure Island") can push a new release off page 1.
SEARCH_PAGES = 3

# Overseerr mediaInfo.status codes
STATUS_NAMES = {
    1: "unknown",
    2: "pending",
    3: "processing",
    4: "partially_available",
    5: "available",
}

mcp = FastMCP(
    "plex-request",
    instructions=(
        "Requests movies and TV shows for the user's Plex server through Seerr/Overseerr. "
        "Always call search_media first, pick the result whose title and year match, then call "
        "request_media with its tmdb_id. If several results plausibly match, ask the user which one. "
        "Never say something was requested unless request_media returned status 'requested'."
    ),
    stateless_http=True,
    streamable_http_path="/",
    # Served behind Traefik/Cloudflare under a public hostname, not localhost.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)
mcp_app = mcp.streamable_http_app() if MCP_PATH_TOKEN else None


@contextlib.asynccontextmanager
async def lifespan(_app):
    if mcp_app is None:
        yield
        return
    async with mcp.session_manager.run():
        yield


app = FastAPI(
    title="ChatGPT-Overseerr Bridge",
    version="1.2.0",
    description="Accepts media requests from ChatGPT (actions or MCP plugin) and Hermes, and forwards them to Overseerr/Seerr.",
    lifespan=lifespan,
)

security = HTTPBearer()


def verify_token(credentials: HTTPAuthorizationCredentials = Security(security)) -> str:
    if credentials.credentials != BRIDGE_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")
    return credentials.credentials


class RequestBody(BaseModel):
    query: Optional[str] = None
    media_type: str = "movie"     # "movie" or "tv"
    year: Optional[int] = None    # release / first-air year, narrows the search match
    tmdb_id: Optional[int] = None  # exact TMDB id; skips search when given


def _overseerr_headers() -> dict:
    return {"X-Api-Key": OVERSEERR_API_KEY, "Content-Type": "application/json"}


def _check_media_type(media_type: str) -> str:
    media_type = media_type.lower()
    if media_type not in ("movie", "tv"):
        raise HTTPException(status_code=400, detail="media_type must be 'movie' or 'tv'")
    return media_type


def _year_of(item: dict) -> Optional[int]:
    date = item.get("releaseDate") or item.get("firstAirDate") or ""
    return int(date[:4]) if date[:4].isdigit() else None


def _status_of(item: dict) -> str:
    code = (item.get("mediaInfo") or {}).get("status")
    return STATUS_NAMES.get(code, "not_requested")


def _summarize(item: dict, media_type: str) -> dict:
    return {
        "tmdb_id": item["id"],
        "media_type": media_type,
        "title": item.get("title") or item.get("name", "Unknown"),
        "year": _year_of(item),
        "status": _status_of(item),
        "overview": (item.get("overview") or "")[:200],
    }


async def _search(client: httpx.AsyncClient, query: str, media_type: str, year: Optional[int]) -> list:
    encoded_query = quote(query, safe="")
    matches = []
    for page in range(1, SEARCH_PAGES + 1):
        search = await client.get(
            f"{OVERSEERR_URL}/api/v1/search?query={encoded_query}&page={page}",
            headers=_overseerr_headers(),
        )
        if search.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Overseerr search error: {search.status_code}")
        data = search.json()
        matches += [
            r for r in data.get("results", [])
            if r.get("mediaType") == media_type and (year is None or _year_of(r) == year)
        ]
        # Without a year filter, page 1 is what we've always used.
        if year is None or matches or page >= data.get("totalPages", 1):
            break
    return matches


async def _details(client: httpx.AsyncClient, tmdb_id: int, media_type: str) -> dict:
    detail = await client.get(f"{OVERSEERR_URL}/api/v1/{media_type}/{tmdb_id}", headers=_overseerr_headers())
    if detail.status_code == 404:
        raise HTTPException(status_code=404, detail=f"No {media_type} with TMDB id {tmdb_id}")
    if detail.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Overseerr detail error: {detail.status_code}")
    return detail.json()


@app.get("/health")
def health():
    return {"status": "ok"}


async def do_search(query: str, media_type: str, year: Optional[int]) -> dict:
    media_type = _check_media_type(media_type)
    async with httpx.AsyncClient(timeout=15) as client:
        matches = await _search(client, query, media_type, year)
    return {"results": [_summarize(r, media_type) for r in matches[:10]]}


async def do_status(tmdb_id: int, media_type: str) -> dict:
    media_type = _check_media_type(media_type)
    async with httpx.AsyncClient(timeout=15) as client:
        item = await _details(client, tmdb_id, media_type)
    return _summarize(item, media_type)


async def do_request(query: Optional[str], media_type: str, year: Optional[int], tmdb_id: Optional[int]) -> dict:
    media_type = _check_media_type(media_type)
    if tmdb_id is None and not query:
        raise HTTPException(status_code=400, detail="Provide query or tmdb_id")

    headers = _overseerr_headers()

    async with httpx.AsyncClient(timeout=15) as client:
        # 1 — Resolve the exact title
        if tmdb_id is not None:
            best = await _details(client, tmdb_id, media_type)
        else:
            results = await _search(client, query, media_type, year)
            if not results:
                year_hint = f" ({year})" if year else ""
                return {
                    "message": f"No {media_type} found matching '{query}'{year_hint}. Try a more specific title.",
                    "status": "not_found",
                }
            best = results[0]

        media_id = best["id"]
        title = best.get("title") or best.get("name", "Unknown")
        year = _year_of(best)
        label = f"{title} ({year})" if year else title

        # 2 — Check if already requested / available
        media_info = best.get("mediaInfo") or {}
        if media_info.get("status") in (2, 3, 4, 5):
            return {
                "message": f"'{label}' is already requested or available in Plex.",
                "title": title,
                "year": year,
                "tmdb_id": media_id,
                "status": "already_exists",
            }

        # 3 — Build request payload
        payload: dict = {"mediaType": media_type, "mediaId": media_id}

        if media_type == "tv":
            # Request every non-special season
            tv = best if "seasons" in best else await _details(client, media_id, "tv")
            payload["seasons"] = [s["seasonNumber"] for s in tv.get("seasons", []) if s["seasonNumber"] > 0]
            tvdb_id = (tv.get("externalIds") or {}).get("tvdbId")
            if tvdb_id:
                payload["tvdbId"] = tvdb_id

        # 4 — Submit request
        req = await client.post(f"{OVERSEERR_URL}/api/v1/request", json=payload, headers=headers)

    result = {"title": title, "year": year, "tmdb_id": media_id}
    if req.status_code in (200, 201):
        return {**result, "message": f"'{label}' has been requested and will be added to Plex shortly.",
                "status": "requested"}
    if req.status_code == 409:
        return {**result, "message": f"'{label}' is already requested.", "status": "duplicate"}

    raise HTTPException(status_code=502, detail=f"Overseerr returned {req.status_code}: {req.text}")


@app.get("/search")
async def search_media(query: str, media_type: str = "movie", year: Optional[int] = None,
                       _: str = Depends(verify_token)):
    return await do_search(query, media_type, year)


@app.get("/status")
async def media_status(tmdb_id: int, media_type: str = "movie", _: str = Depends(verify_token)):
    return await do_status(tmdb_id, media_type)


@app.post("/request")
async def request_media(body: RequestBody, _: str = Depends(verify_token)):
    return await do_request(body.query, body.media_type, body.year, body.tmdb_id)


# — MCP tools (ChatGPT plugin) —

MediaType = Literal["movie", "tv"]


async def _tool_call(coro) -> dict:
    try:
        return await coro
    except HTTPException as e:
        return {"status": "error", "message": str(e.detail)}
    except httpx.HTTPError as e:
        return {"status": "error", "message": f"Cannot reach Overseerr: {e.__class__.__name__}"}


@mcp.tool(name="search_media")
async def search_media_tool(query: str, media_type: MediaType = "movie", year: Optional[int] = None) -> dict:
    """Search Plex/Seerr for a movie or TV show. Returns candidates with tmdb_id, title, year and
    status (not_requested, pending, processing, partially_available, available). Pass year when known."""
    return await _tool_call(do_search(query, media_type, year))


@mcp.tool(name="request_media")
async def request_media_tool(tmdb_id: int, media_type: MediaType) -> dict:
    """Submit a real request to add a movie or TV show (all regular seasons) to Plex. This has a real
    side effect. Use the tmdb_id from search_media. Only report success if status is 'requested'."""
    return await _tool_call(do_request(None, media_type, None, tmdb_id))


@mcp.tool(name="get_request_status")
async def get_request_status_tool(tmdb_id: int, media_type: MediaType) -> dict:
    """Check whether a movie or TV show is not requested, pending, downloading (processing),
    partially available, or available in Plex."""
    return await _tool_call(do_status(tmdb_id, media_type))


if mcp_app is not None:
    app.mount(f"/mcp/{MCP_PATH_TOKEN}", mcp_app)
