import os
from fastapi import FastAPI, HTTPException, Header
from fastapi.openapi.utils import get_openapi
import httpx
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

BALLDONTLIE_API_KEY = os.getenv("BALLDONTLIE_API_KEY")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY")

BASE_NBA = "https://api.balldontlie.io/v1"
BASE_ODDS = "https://api.the-odds-api.com/v4"

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

@app.get("/odds/nba")
async def nba_odds(x_api_key: str | None = Header(default=None)):
    verify_key(x_api_key)

    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "us",
        "markets": "h2h,spreads,totals",
        "sport": "basketball_nba"
    }

    async with httpx.AsyncClient() as client:
        r = await client.get(f"{BASE_ODDS}/sports/basketball_nba/odds", params=params)

    return r.json()
