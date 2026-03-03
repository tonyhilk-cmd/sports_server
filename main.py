import os
from fastapi import FastAPI, HTTPException, Header
from cachetools import TTLCache
from datetime import date
import httpx
from dotenv import load_dotenv
import json

load_dotenv()

BALLDONTLIE_API_KEY = os.getenv("BALLDONTLIE_API_KEY")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY")

BASE_NBA = "https://api.balldontlie.io/v1"
BASE_ODDS = "https://api.the-odds-api.com/v4"

SAFE_MODE = False
USAGE_FILE = "odds_usage.json"
ODDS_MONTHLY_LIMIT = 480

odds_cache = TTLCache(maxsize=10, ttl=60)

app = FastAPI(title="Sports API", version="2.1.0")

# ---------------- AUTH ---------------- #

def verify_key(x_api_key: str | None):
    if x_api_key != INTERNAL_API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

# ---------------- ROOT ---------------- #

@app.get("/")
def root():
    return {"message": "Sports server running"}

# ---------------- PLAYER SEARCH ---------------- #

@app.get("/nba/player/search")
async def search_player(name: str, x_api_key: str | None = Header(default=None)):
    verify_key(x_api_key)

    headers = {"Authorization": BALLDONTLIE_API_KEY}
    params = {"search": name, "per_page": "10"}

    async with httpx.AsyncClient() as client:
        r = await client.get(f"{BASE_NBA}/players", headers=headers, params=params)

    return r.json()

# ---------------- PLAYER GAME LOGS ---------------- #

async def fetch_player_stats(player_id: int, season: int):
    headers = {"Authorization": BALLDONTLIE_API_KEY}

    params = {
        "player_ids[]": str(player_id),
        "seasons[]": str(season),
        "per_page": "100"
    }

    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{BASE_NBA}/stats",
            headers=headers,
            params=params
        )

    print("=== DEBUG REQUEST URL ===")
    print(r.request.url)
    print("=== DEBUG RESPONSE ===")
    print(r.json())

    return r.json().get("data", [])

@app.get("/nba/player/{player_id}/last5")
async def last5(player_id: int, season: int, x_api_key: str | None = Header(default=None)):
    verify_key(x_api_key)

    data = await fetch_player_stats(player_id, season)
    data = sorted(data, key=lambda x: x["game"]["date"])
    return data[-5:]

@app.get("/nba/player/{player_id}/last10")
async def last10(player_id: int, season: int, x_api_key: str | None = Header(default=None)):
    verify_key(x_api_key)

    data = await fetch_player_stats(player_id, season)
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

# ---------------- INJURIES ---------------- #

@app.get("/nba/injuries")
async def injuries(x_api_key: str | None = Header(default=None)):
    verify_key(x_api_key)

    headers = {"Authorization": BALLDONTLIE_API_KEY}

    async with httpx.AsyncClient() as client:
        r = await client.get(f"{BASE_NBA}/player_injuries", headers=headers)

    return r.json()

# ---------------- GAME DAY ---------------- #

@app.get("/nba/gameday")
async def gameday(include_odds: bool = False, x_api_key: str | None = Header(default=None)):
    verify_key(x_api_key)

    today = date.today().isoformat()
    headers = {"Authorization": BALLDONTLIE_API_KEY}

    async with httpx.AsyncClient() as client:
        games = await client.get(
            f"{BASE_NBA}/games",
            headers=headers,
            params={"dates[]": today}
        )

        injuries = await client.get(
            f"{BASE_NBA}/player_injuries",
            headers=headers
        )

    response = {
        "games": games.json().get("data", []),
        "injuries": injuries.json().get("data", [])
    }

    if include_odds:
        response["odds"] = await nba_odds_internal()

    return response

# ---------------- ODDS SYSTEM ---------------- #

def load_usage():
    if not os.path.exists(USAGE_FILE):
        return {"month": date.today().month, "count": 0}
    with open(USAGE_FILE, "r") as f:
        return json.load(f)

def save_usage(data):
    with open(USAGE_FILE, "w") as f:
        json.dump(data, f)

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

    usage["count"] += 1
    save_usage(usage)

    response = {
        "calls_used_this_month": usage["count"],
        "limit": ODDS_MONTHLY_LIMIT,
        "remaining": ODDS_MONTHLY_LIMIT - usage["count"],
        "data": r.json()
    }

    odds_cache["nba_odds"] = response
    return response

@app.get("/odds/nba")
async def nba_odds(x_api_key: str | None = Header(default=None)):
    verify_key(x_api_key)
    return await nba_odds_internal()

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