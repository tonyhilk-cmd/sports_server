import json
import os
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

import httpx
from cachetools import TTLCache
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Security
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
SUMMARY_FIELDS = ("pts", "reb", "ast", "stl", "blk", "turnover", "fg3m", "pra", "pr", "pa", "ra", "stocks", "min")

odds_cache = TTLCache(maxsize=10, ttl=60)
api_key_header = APIKeyHeader(name="x-api-key", auto_error=False)

app = FastAPI(
    title="Sports API",
    version="2.3.0",
    servers=[{"url": PUBLIC_BASE_URL, "description": "Public deployment"}],
)


def require_setting(name: str, value: str | None) -> str:
    if not value:
        raise HTTPException(status_code=500, detail=f"{name} not configured")
    return value


def verify_key(x_api_key: str | None = Security(api_key_header)) -> None:
    expected_key = require_setting("INTERNAL_API_KEY", INTERNAL_API_KEY)
    if x_api_key != expected_key:
        raise HTTPException(status_code=401, detail="Unauthorized")


def add_array_params(params: list[tuple[str, str]], name: str, values: list[int | str]) -> None:
    for value in values:
        params.append((f"{name}[]", str(value)))


async def fetch_json(
    url: str,
    *,
    service_name: str,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | list[tuple[str, str]] | None = None,
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


async def fetch_paginated_data(
    url: str,
    *,
    service_name: str,
    headers: dict[str, str] | None = None,
    params: list[tuple[str, str]] | None = None,
):
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    base_params = list(params or [])

    while True:
        page_params = list(base_params)
        page_params.append(("per_page", "100"))
        if cursor is not None:
            page_params.append(("cursor", cursor))

        response = await fetch_json(
            url,
            service_name=service_name,
            headers=headers,
            params=page_params,
        )

        page_items = response.get("data", [])
        if not isinstance(page_items, list):
            raise HTTPException(status_code=502, detail=f"{service_name} returned an unexpected response shape")

        items.extend(page_items)
        cursor = response.get("meta", {}).get("next_cursor")
        if not cursor or not page_items:
            break

    return items


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


def sort_game_logs(games: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(games, key=lambda game: (game["game"]["date"], game["game"]["id"]))


def parse_minutes(value: str | None) -> float:
    if not value:
        return 0.0
    if ":" not in value:
        return float(value)
    minutes, seconds = value.split(":", maxsplit=1)
    return int(minutes) + (int(seconds) / 60)


def canonical_stat_name(stat: str) -> str:
    normalized = stat.strip().lower().replace(" ", "").replace("-", "").replace("_", "")
    aliases = {
        "pts": "pts",
        "points": "pts",
        "reb": "reb",
        "rebounds": "reb",
        "ast": "ast",
        "assists": "ast",
        "stl": "stl",
        "steals": "stl",
        "blk": "blk",
        "blocks": "blk",
        "turnover": "turnover",
        "turnovers": "turnover",
        "tov": "turnover",
        "fg3m": "fg3m",
        "3pm": "fg3m",
        "threes": "fg3m",
        "3ptm": "fg3m",
        "pra": "pra",
        "pr": "pr",
        "pa": "pa",
        "ra": "ra",
        "stocks": "stocks",
        "minutes": "min",
        "min": "min",
    }
    if normalized not in aliases:
        raise HTTPException(status_code=400, detail=f"Unsupported stat '{stat}'")
    return aliases[normalized]


def stat_value(game: dict[str, Any], stat: str) -> float:
    if stat == "pra":
        return float(game.get("pts", 0) + game.get("reb", 0) + game.get("ast", 0))
    if stat == "pr":
        return float(game.get("pts", 0) + game.get("reb", 0))
    if stat == "pa":
        return float(game.get("pts", 0) + game.get("ast", 0))
    if stat == "ra":
        return float(game.get("reb", 0) + game.get("ast", 0))
    if stat == "stocks":
        return float(game.get("stl", 0) + game.get("blk", 0))
    if stat == "min":
        return parse_minutes(game.get("min"))
    return float(game.get(stat, 0) or 0)


def summarize_games(games: list[dict[str, Any]]) -> dict[str, Any]:
    if not games:
        return {
            "games": 0,
            "first_game_date": None,
            "last_game_date": None,
            "averages": {field: None for field in SUMMARY_FIELDS},
        }

    averages = {
        field: round(sum(stat_value(game, field) for game in games) / len(games), 2)
        for field in SUMMARY_FIELDS
    }
    return {
        "games": len(games),
        "first_game_date": games[0]["game"]["date"],
        "last_game_date": games[-1]["game"]["date"],
        "averages": averages,
    }


def summarize_delta(primary: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    delta: dict[str, Any] = {}
    for field in ("pts", "reb", "ast", "fg3m", "pra", "pr", "pa", "ra", "stocks", "min"):
        primary_value = primary["averages"].get(field)
        baseline_value = baseline["averages"].get(field)
        if primary_value is None or baseline_value is None:
            delta[field] = None
        else:
            delta[field] = round(primary_value - baseline_value, 2)
    return delta


def build_hit_rate(games: list[dict[str, Any]], stat: str, line: float) -> dict[str, Any]:
    if not games:
        return {
            "games": 0,
            "line": line,
            "stat": stat,
            "over": 0,
            "under": 0,
            "push": 0,
            "over_rate": None,
        }

    over = under = push = 0
    for game in games:
        value = stat_value(game, stat)
        if value > line:
            over += 1
        elif value < line:
            under += 1
        else:
            push += 1

    return {
        "games": len(games),
        "line": line,
        "stat": stat,
        "over": over,
        "under": under,
        "push": push,
        "over_rate": round(over / len(games), 3),
    }


def compact_game_log(game: dict[str, Any]) -> dict[str, Any]:
    team_id = game["team"]["id"]
    home_team_id = game["game"]["home_team_id"]
    home = team_id == home_team_id
    opponent_id = game["game"]["visitor_team_id"] if home else home_team_id
    opponent_side = "visitor_team" if home else "home_team"

    return {
        "game_id": game["game"]["id"],
        "date": game["game"]["date"],
        "season": game["game"]["season"],
        "home": home,
        "opponent_id": opponent_id,
        "opponent": game["game"].get(opponent_side, {}).get("abbreviation"),
        "team_score": game["game"]["home_team_score"] if home else game["game"]["visitor_team_score"],
        "opponent_score": game["game"]["visitor_team_score"] if home else game["game"]["home_team_score"],
        "stats": {
            "min": round(parse_minutes(game.get("min")), 2),
            "pts": game.get("pts", 0),
            "reb": game.get("reb", 0),
            "ast": game.get("ast", 0),
            "stl": game.get("stl", 0),
            "blk": game.get("blk", 0),
            "turnover": game.get("turnover", 0),
            "fg3m": game.get("fg3m", 0),
            "pra": round(stat_value(game, "pra"), 2),
        },
    }


def is_impact_absence(status: str | None) -> bool:
    if not status:
        return False
    normalized = status.lower()
    return any(token in normalized for token in ("out", "doubt", "inactive", "suspend"))


def compact_injury(injury: dict[str, Any]) -> dict[str, Any]:
    player = injury["player"]
    return {
        "player": {
            "id": player["id"],
            "first_name": player["first_name"],
            "last_name": player["last_name"],
            "team_id": player["team_id"],
        },
        "status": injury.get("status"),
        "return_date": injury.get("return_date"),
        "description": injury.get("description"),
    }


async def fetch_player_stats(
    *,
    player_ids: list[int],
    season: int,
    game_ids: list[int] | None = None,
):
    params: list[tuple[str, str]] = []
    add_array_params(params, "player_ids", player_ids)
    add_array_params(params, "seasons", [season])
    if game_ids:
        add_array_params(params, "game_ids", game_ids)

    stats = await fetch_paginated_data(
        f"{BASE_NBA}/stats",
        service_name="BallDontLie",
        headers=nba_headers(),
        params=params,
    )
    return sort_game_logs(stats)


async def fetch_team_injuries(team_id: int):
    params: list[tuple[str, str]] = []
    add_array_params(params, "team_ids", [team_id])
    return await fetch_paginated_data(
        f"{BASE_NBA}/player_injuries",
        service_name="BallDontLie",
        headers=nba_headers(),
        params=params,
    )


async def build_injury_context(
    *,
    player_id: int,
    season: int,
    player_games: list[dict[str, Any]],
    stat: str | None,
    line: float | None,
    max_injured_teammates: int,
):
    if not player_games:
        return {"impact_absences": [], "splits": []}

    team_id = player_games[-1]["team"]["id"]
    injuries = await fetch_team_injuries(team_id)
    impact_absences = [
        injury
        for injury in injuries
        if injury["player"]["id"] != player_id and is_impact_absence(injury.get("status"))
    ]

    if not impact_absences:
        return {"impact_absences": [], "splits": []}

    selected = impact_absences[:max_injured_teammates]
    game_ids = [game["game"]["id"] for game in player_games]
    teammate_ids = [injury["player"]["id"] for injury in selected]
    teammate_stats = await fetch_player_stats(player_ids=teammate_ids, season=season, game_ids=game_ids)

    presence_by_game: dict[int, set[int]] = defaultdict(set)
    for teammate_stat in teammate_stats:
        presence_by_game[teammate_stat["game"]["id"]].add(teammate_stat["player"]["id"])

    splits = []
    for injury in selected:
        teammate_id = injury["player"]["id"]
        with_teammate = [game for game in player_games if teammate_id in presence_by_game.get(game["game"]["id"], set())]
        without_teammate = [game for game in player_games if teammate_id not in presence_by_game.get(game["game"]["id"], set())]

        split: dict[str, Any] = {
            "teammate": compact_injury(injury)["player"],
            "status": injury.get("status"),
            "return_date": injury.get("return_date"),
            "description": injury.get("description"),
            "with_teammate": summarize_games(with_teammate),
            "without_teammate": summarize_games(without_teammate),
        }
        split["delta_without_minus_with"] = summarize_delta(split["without_teammate"], split["with_teammate"])

        if stat is not None and line is not None:
            split["line_analysis"] = {
                "with_teammate": build_hit_rate(with_teammate, stat, line),
                "without_teammate": build_hit_rate(without_teammate, stat, line),
            }

        splits.append(split)

    return {
        "impact_absences": [compact_injury(injury) for injury in selected],
        "splits": splits,
    }


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


@app.get("/nba/player/{player_id}/last5", dependencies=[Depends(verify_key)])
async def last5(player_id: int, season: int):
    data = await fetch_player_stats(player_ids=[player_id], season=season)
    return data[-5:]


@app.get("/nba/player/{player_id}/last10", dependencies=[Depends(verify_key)])
async def last10(player_id: int, season: int):
    data = await fetch_player_stats(player_ids=[player_id], season=season)
    last_10 = data[-10:]

    return {
        "player_id": player_id,
        "season": season,
        "games": last_10,
        "averages": summarize_games(last_10)["averages"],
    }


@app.get("/nba/player/{player_id}/analysis", dependencies=[Depends(verify_key)])
async def player_analysis(
    player_id: int,
    season: int,
    recent_games: int = Query(default=10, ge=3, le=20),
    stat: str | None = None,
    line: float | None = None,
    include_injury_context: bool = True,
    max_injured_teammates: int = Query(default=3, ge=1, le=6),
):
    player_games = await fetch_player_stats(player_ids=[player_id], season=season)
    if not player_games:
        raise HTTPException(status_code=404, detail="No game stats found for that player and season")

    normalized_stat = canonical_stat_name(stat) if stat is not None else None
    if normalized_stat is None and line is not None:
        raise HTTPException(status_code=400, detail="A stat is required when a line is provided")

    recent_slice = player_games[-recent_games:]
    player_info = player_games[-1]["player"]
    team_info = player_games[-1]["team"]

    response: dict[str, Any] = {
        "player": player_info,
        "team": team_info,
        "season": season,
        "games_played": len(player_games),
        "season_summary": summarize_games(player_games),
        "last_5_summary": summarize_games(player_games[-5:]),
        "last_10_summary": summarize_games(player_games[-10:]),
        "recent_summary": summarize_games(recent_slice),
        "recent_games": [compact_game_log(game) for game in recent_slice],
    }

    if normalized_stat is not None and line is not None:
        response["line_analysis"] = {
            "stat": normalized_stat,
            "line": line,
            "season": build_hit_rate(player_games, normalized_stat, line),
            "last_10": build_hit_rate(player_games[-10:], normalized_stat, line),
            "last_5": build_hit_rate(player_games[-5:], normalized_stat, line),
            "recent_window": build_hit_rate(recent_slice, normalized_stat, line),
        }

    if include_injury_context:
        response["injury_context"] = await build_injury_context(
            player_id=player_id,
            season=season,
            player_games=player_games,
            stat=normalized_stat,
            line=line,
            max_injured_teammates=max_injured_teammates,
        )

    return response


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
