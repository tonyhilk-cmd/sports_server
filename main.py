import hashlib
import json
import logging
import os
from datetime import date
from pathlib import Path

import httpx
from cachetools import TTLCache
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, Security
from fastapi.security import APIKeyHeader


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BALLDONTLIE_API_KEY = os.getenv("BALLDONTLIE_API_KEY")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://sports-server-a18t.onrender.com")

BASE_NBA = "https://api.balldontlie.io/v1"
BASE_ODDS = "https://api.the-odds-api.com/v4"

SAFE_MODE = False
USAGE_FILE = BASE_DIR / "odds_usage.json"
ODDS_MONTHLY_LIMIT = 480
HTTP_TIMEOUT = httpx.Timeout(20.0)

odds_cache = TTLCache(maxsize=10, ttl=60)
api_key_header = APIKeyHeader(name="x-api-key", auto_error=False)
logger = logging.getLogger("sports_api")

app = FastAPI(
    title="Sports API",
    version="2.2.1",
    servers=[{"url": PUBLIC_BASE_URL, "description": "Public deployment"}],
)


def key_fingerprint(value: str | None) -> str:
    if not value:
        return "missing"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def require_setting(name: str, value: str | None) -> str:
    if not value:
        logger.error("%s is not configured", name)
        raise HTTPException(status_code=500, detail=f"{name} not configured")
    return value


def verify_key(
    request: Request,
    x_api_key: str | None = Security(api_key_header),
) -> None:
    expected_key = require_setting("INTERNAL_API_KEY", INTERNAL_API_KEY)
    if x_api_key != expected_key:
        debug_line = (
            "auth_failed "
            f"path={request.url.path} "
            f"method={request.method} "
            f"header_present={x_api_key is not None} "
            f"provided_fp={key_fingerprint(x_api_key)} "
            f"expected_fp={key_fingerprint(expected_key)} "
            f"user_agent={request.headers.get('user-agent')!r}"
        )
        print(debug_line, flush=True)
        logger.warning(
            "auth_failed path=%s method=%s header_present=%s provided_fp=%s expected_fp=%s user_agent=%r",
            request.url.path,
            request.method,
            x_api_key is not None,
            key_fingerprint(x_api_key),
            key_fingerprint(expected_key),
            request.headers.get("user-agent"),
        )
        raise HTTPException(status_code=401, detail="Unauthorized")


async def fetch_json(
    url: str,
    *,
    service_name: str,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
):
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.get(url, headers=headers, params=params)
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=exc.response.status_code,
            detail=f"{service_name} request failed",
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"{service_name} request could not be completed",
        ) from exc

    return response.json()


def nba_headers() -> dict[str, str]:
    return {"Authorization": require_setting("BALLDONTLIE_API_KEY", BALLDONTLIE_API_KEY)}


def odds_params() -> dict[str, str]:
    return {
        "apiKey": require_setting("ODDS_API_KEY", ODDS_API_KEY),
        "regions": "us",
        "markets": "h2h,spreads,totals",
    }


def load_usage():
    if not USAGE_FILE.exists():
        return {"month": date.today().month, "count": 0}
    with USAGE_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_usage(data):
    with USAGE_FILE.open("w", encoding="utf-8") as f:
        json.dump(data, f)


@app.get("/")
def root():
    return {"message": "Sports server running"}


@app.get("/nba/player/search", dependencies=[Depends(verify_key)])
async def search_player(name: str):
    params = {"search": name, "per_page": "10"}
    return await fetch_json(
        f"{BASE_NBA}/players",
        service_name="BallDontLie",
        headers=nba_headers(),
        params=params,
    )


async def fetch_player_stats(player_id: int, season: int):
    params = {
        "player_ids[]": str(player_id),
        "seasons[]": str(season),
        "per_page": "100",
    }
    response = await fetch_json(
        f"{BASE_NBA}/stats",
        service_name="BallDontLie",
        headers=nba_headers(),
        params=params,
    )
    return response.get("data", [])


@app.get("/nba/player/{player_id}/last5", dependencies=[Depends(verify_key)])
async def last5(player_id: int, season: int):
    data = await fetch_player_stats(player_id, season)
    data = sorted(data, key=lambda x: x["game"]["date"])
    return data[-5:]


@app.get("/nba/player/{player_id}/last10", dependencies=[Depends(verify_key)])
async def last10(player_id: int, season: int):
    data = await fetch_player_stats(player_id, season)
    data = sorted(data, key=lambda x: x["game"]["date"])
    last_10 = data[-10:]

    totals = {"pts": 0, "reb": 0, "ast": 0}
    for game in last_10:
        totals["pts"] += game.get("pts", 0)
        totals["reb"] += game.get("reb", 0)
        totals["ast"] += game.get("ast", 0)

    n = max(len(last_10), 1)
    averages = {k: round(v / n, 2) for k, v in totals.items()}

    return {
        "player_id": player_id,
        "season": season,
        "games": last_10,
        "averages": averages,
    }


@app.get("/nba/injuries", dependencies=[Depends(verify_key)])
async def injuries():
    return await fetch_json(
        f"{BASE_NBA}/player_injuries",
        service_name="BallDontLie",
        headers=nba_headers(),
    )


@app.get("/nba/gameday", dependencies=[Depends(verify_key)])
async def gameday(include_odds: bool = False):
    today = date.today().isoformat()
    response = {
        "games": (
            await fetch_json(
                f"{BASE_NBA}/games",
                service_name="BallDontLie",
                headers=nba_headers(),
                params={"dates[]": today},
            )
        ).get("data", []),
        "injuries": (
            await fetch_json(
                f"{BASE_NBA}/player_injuries",
                service_name="BallDontLie",
                headers=nba_headers(),
            )
        ).get("data", []),
    }

    if include_odds:
        response["odds"] = await nba_odds_internal()

    return response


async def nba_odds_internal():
    if SAFE_MODE:
        return {"message": "Safe mode enabled"}

    usage = load_usage()
    today = date.today()

    if usage["month"] != today.month:
        usage = {"month": today.month, "count": 0}
        save_usage(usage)

    if usage["count"] >= ODDS_MONTHLY_LIMIT:
        raise HTTPException(status_code=429, detail="Monthly odds API limit reached")

    if "nba_odds" in odds_cache:
        return odds_cache["nba_odds"]

    response_data = await fetch_json(
        f"{BASE_ODDS}/sports/basketball_nba/odds",
        service_name="The Odds API",
        params=odds_params(),
    )

    usage["count"] += 1
    save_usage(usage)

    response = {
        "calls_used_this_month": usage["count"],
        "limit": ODDS_MONTHLY_LIMIT,
        "remaining": ODDS_MONTHLY_LIMIT - usage["count"],
        "data": response_data,
    }

    odds_cache["nba_odds"] = response
    return response


@app.get("/odds/nba", dependencies=[Depends(verify_key)])
async def nba_odds():
    return await nba_odds_internal()


@app.get("/odds/usage", dependencies=[Depends(verify_key)])
def odds_usage():
    usage = load_usage()
    return {
        "month": usage["month"],
        "calls_used": usage["count"],
        "limit": ODDS_MONTHLY_LIMIT,
        "remaining": ODDS_MONTHLY_LIMIT - usage["count"],
    }
