import os
import httpx
from urllib.parse import quote
from fastapi import FastAPI, HTTPException, Security, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

OVERSEERR_URL = os.environ["OVERSEERR_URL"]       # e.g. http://overseerr.overseerr.svc.cluster.local:5055
OVERSEERR_API_KEY = os.environ["OVERSEERR_API_KEY"]
BRIDGE_TOKEN = os.environ["BRIDGE_TOKEN"]          # secret token ChatGPT sends in Authorization header

app = FastAPI(
    title="ChatGPT-Overseerr Bridge",
    version="1.0.0",
    description="Accepts natural-language media requests from a ChatGPT custom action and forwards them to Overseerr.",
)

security = HTTPBearer()


def verify_token(credentials: HTTPAuthorizationCredentials = Security(security)) -> str:
    if credentials.credentials != BRIDGE_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")
    return credentials.credentials


class RequestBody(BaseModel):
    query: str
    media_type: str = "movie"   # "movie" or "tv"


def _overseerr_headers() -> dict:
    return {"X-Api-Key": OVERSEERR_API_KEY, "Content-Type": "application/json"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/request")
async def request_media(body: RequestBody, _: str = Depends(verify_token)):
    media_type = body.media_type.lower()
    if media_type not in ("movie", "tv"):
        raise HTTPException(status_code=400, detail="media_type must be 'movie' or 'tv'")

    headers = _overseerr_headers()
    encoded_query = quote(body.query, safe="")

    async with httpx.AsyncClient(timeout=15) as client:
        # 1 — Search Overseerr
        search = await client.get(
            f"{OVERSEERR_URL}/api/v1/search?query={encoded_query}&page=1",
            headers=headers,
        )
        if search.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Overseerr search error: {search.status_code}")

        results = [r for r in search.json().get("results", []) if r.get("mediaType") == media_type]

        if not results:
            return {"message": f"No {media_type} found matching '{body.query}'. Try a more specific title."}

        best = results[0]
        media_id = best["id"]
        title = best.get("title") or best.get("name", "Unknown")

        # 2 — Check if already requested / available
        media_info = best.get("mediaInfo") or {}
        # Overseerr status: 2=pending, 3=processing, 4=partially_available, 5=available
        if media_info.get("status") in (2, 3, 4, 5):
            return {
                "message": f"'{title}' is already requested or available in Plex.",
                "title": title,
                "status": "already_exists",
            }

        # 3 — Build request payload
        payload: dict = {"mediaType": media_type, "mediaId": media_id}

        if media_type == "tv":
            # Fetch season list so we can request every non-special season
            detail = await client.get(f"{OVERSEERR_URL}/api/v1/tv/{media_id}", headers=headers)
            if detail.status_code == 200:
                tv = detail.json()
                seasons = [s["seasonNumber"] for s in tv.get("seasons", []) if s["seasonNumber"] > 0]
                payload["seasons"] = seasons
                tvdb_id = (tv.get("externalIds") or {}).get("tvdbId")
                if tvdb_id:
                    payload["tvdbId"] = tvdb_id

        # 4 — Submit request
        req = await client.post(f"{OVERSEERR_URL}/api/v1/request", json=payload, headers=headers)

    if req.status_code in (200, 201):
        return {
            "message": f"'{title}' has been requested and will be added to Plex shortly.",
            "title": title,
            "status": "requested",
        }
    if req.status_code == 409:
        return {"message": f"'{title}' is already requested.", "title": title, "status": "duplicate"}

    raise HTTPException(status_code=502, detail=f"Overseerr returned {req.status_code}: {req.text}")
