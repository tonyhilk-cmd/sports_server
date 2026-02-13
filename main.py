import os
from fastapi import FastAPI, HTTPException, Header
import httpx
from dotenv import load_dotenv

load_dotenv()

BALLDONTLIE_API_KEY = os.getenv("BALLDONTLIE_API_KEY")
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY")

app = FastAPI(
    servers=[
        {"url": "https://sports-server-a18t.onrender.com"}
    ]
)

from fastapi.openapi.utils import get_openapi

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



def verify_key(x_api_key: str | None):
    if x_api_key != INTERNAL_API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

@app.get("/")
def root():
    return {"message": "Sports server running"}

@app.get("/nba/player")
async def get_player(name: str, x_api_key: str | None = Header(default=None)):
    verify_key(x_api_key)

    if not BALLDONTLIE_API_KEY:
        raise HTTPException(status_code=500, detail="Missing API key")

    headers = {
        "Authorization": BALLDONTLIE_API_KEY
    }

    url = "https://api.balldontlie.io/v1/players"
    params = {
        "search": name,
        "per_page": 5
    }

    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=headers, params=params)

    if response.status_code != 200:
        raise HTTPException(status_code=500, detail="API request failed")

    return response.json()
