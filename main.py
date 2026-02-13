import os
from fastapi import FastAPI, HTTPException, Header
from fastapi.openapi.utils import get_openapi
from cachetools import TTLCache
from datetime import date
import httpx
from dotenv import load_dotenv
from datetime import datetime

SAFE_MODE = False

import json
import logging

USAGE_FILE = "odds_usage.json"
WARNING_THRESHOLD = 450

logging.basicConfig(level=logging.INFO)

load_dotenv()

BALLDONTLIE_API_KEY = os.getenv("BALLDONTLIE_API_KEY")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY")

BASE_NBA = "https://api.balldontlie.io/v1"
BASE_ODDS = "https://api.the-odds-api.com/v4"

# Cache odds for 30 seconds
odds_cache = TTLCache(maxsize=10, ttl=60)

ODDS_MONTHLY_LIMIT = 480
odds_call_count = 0
odds_call_month = date.today().month

app = FastAPI(title="Sports API", version="1.0.0", description="Sports data backend")

def verify_key(x_api_key: str | None):
    if x_api_key != INTERNAL_API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    openapi_schema = get_openapi(
        title="Sports API",
        version="1.0.0",
        description="Sports data backend",
        routes=app.routes,
    )
    openapi_schema["servers"] = [
        {"url": "https://sports-server-a18t.onrender.com"}
    ]
    app.openapi_schema = openapi_schema
    return app.openapi_schema

app.openapi = custom_openapi

@app.get("/")
def root():
    return {"message": "Sports server running"}

@app.get("/nba/player/search")
async def search_player(name: str, x_api_key: str | None = Header(default=None)):
    verify_key(x_api_key)

    headers = {"Authorization": BALLDONTLIE_API_KEY}
    params = {"search": name, "per_page": 10}

    async with httpx.AsyncClient() as client:
        r = await client.get(f"{BASE_NBA}/players", headers=headers, params=params)

    return r.json()

@app.get("/nba/player/{player_id}/last5")
async def last5(player_id: int, season: int, x_api_key: str | None = Header(default=None)):
    verify_key(x_api_key)

    headers = {"Authorization": BALLDONTLIE_API_KEY}
    params = {
        "player_ids[]": player_id,
        "seasons[]": season,
        "per_page": 50
    }

    async with httpx.AsyncClient() as client:
        r = await client.get(f"{BASE_NBA}/game_player_stats", headers=headers, params=params)

    data = r.json().get("data", [])
    data = sorted(data, key=lambda x: x["game"]["date"])
    last_5 = data[-5:]

    return last_5

@app.get("/nba/player/{player_id}/last10")
async def last10(player_id: int, season: int, x_api_key: str | None = Header(default=None)):
    verify_key(x_api_key)

    headers = {"Authorization": BALLDONTLIE_API_KEY}
    params = {
        "player_ids[]": player_id,
        "seasons[]": season,
        "per_page": 100
    }

    async with httpx.AsyncClient() as client:
        r = await client.get(f"{BASE_NBA}/game_player_stats", headers=headers, params=params)

    data = r.json().get("data", [])
    data = sorted(data, key=lambda x: x["game"]["date"])
    last_10 = data[-10:]

    totals = {"pts": 0, "reb": 0, "ast": 0}
    for g in last_10:
        totals["pts"] += g.get("pts", 0)
        totals["reb"] += g.get("reb", 0)
        totals["ast"] += g.get("ast", 0)

    n = max(len(last_10), 1)
    averages = {k: round(v / n, 2) for k, v in totals.items()}

    return {
        "player_id": player_id,
        "season": season,
        "games": last_10,
        "averages": averages
    }

@app.get("/nba/injuries")
async def injuries(x_api_key: str | None = Header(default=None)):
    verify_key(x_api_key)

    headers = {"Authorization": BALLDONTLIE_API_KEY}

    async with httpx.AsyncClient() as client:
        r = await client.get(f"{BASE_NBA}/player_injuries", headers=headers)

    return r.json()

def load_usage():
    if not os.path.exists(USAGE_FILE):
        return {"month": date.today().month, "count": 0}
    with open(USAGE_FILE, "r") as f:
        return json.load(f)

def save_usage(data):
    with open(USAGE_FILE, "w") as f:
        json.dump(data, f)

@app.get("/nba/player/{player_id}/vs")
async def player_vs_team(player_id: int, team_abbr: str, season: int, x_api_key: str | None = Header(default=None)):
    verify_key(x_api_key)

    headers = {"Authorization": BALLDONTLIE_API_KEY}
    params = {
        "player_ids[]": player_id,
        "seasons[]": season,
        "per_page": 100
    }

    async with httpx.AsyncClient() as client:
        r = await client.get(f"{BASE_NBA}/game_player_stats", headers=headers, params=params)

    data = r.json().get("data", [])
    filtered = [g for g in data if g["game"]["home_team"]["abbreviation"] == team_abbr
                or g["game"]["visitor_team"]["abbreviation"] == team_abbr]

    return filtered

@app.get("/odds/nba")
async def nba_odds(x_api_key: str | None = Header(default=None)):

    verify_key(x_api_key)

    today = date.today()
    usage = load_usage()

    # Reset if month changed
    if usage["month"] != today.month:
        usage = {"month": today.month, "count": 0}
        save_usage(usage)
    if SAFE_MODE:
         return {"message": "Safe mode enabled — odds API disabled"}

    # Cache check
    if "nba_odds" in odds_cache:
        return odds_cache["nba_odds"]

    # Hard limit
    if usage["count"] >= ODDS_MONTHLY_LIMIT:
        raise HTTPException(
            status_code=429,
            detail=f"Monthly odds API limit reached ({ODDS_MONTHLY_LIMIT})"
        )

    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "us",
        "markets": "h2h,spreads,totals"
    }

    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{BASE_ODDS}/sports/basketball_nba/odds",
            params=params
        )

    data = r.json()

    # Increment and persist
    usage["count"] += 1
    save_usage(usage)

    logging.info(f"Odds API call #{usage['count']} this month")

    response = {
        "calls_used_this_month": usage["count"],
        "limit": ODDS_MONTHLY_LIMIT,
        "remaining": ODDS_MONTHLY_LIMIT - usage["count"],
        "data": data
    }

    if usage["count"] >= WARNING_THRESHOLD:
        response["warning"] = "Approaching monthly odds API limit"

    odds_cache["nba_odds"] = response

    return response

@app.get("/odds/usage")
def odds_usage(x_api_key: str | None = Header(default=None)):
    verify_key(x_api_key)

    usage = load_usage()

    return {
        "month": usage["month"],
        "calls_used": usage["count"],
        "limit": ODDS_MONTHLY_LIMIT,
        "remaining": ODDS_MONTHLY_LIMIT - usage["count"]
    }

@app.get("/system/status")
async def system_status(x_api_key: str | None = Header(default=None)):
    verify_key(x_api_key)

    return {
        "balldontlie_configured": BALLDONTLIE_API_KEY is not None,
        "odds_api_configured": ODDS_API_KEY is not None,
        "internal_auth_enabled": INTERNAL_API_KEY is not None
    }
