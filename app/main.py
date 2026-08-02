"""Residence optimizer backend.

Serves the static frontend and proxies geocoding (Nominatim) and travel-time
matrix computation (OpenRouteService) so the API key stays server-side.
"""
import os
from pathlib import Path
from typing import Literal

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

ORS_API_KEY = os.getenv("ORS_API_KEY", "").strip()
TRANSIT_SPEED_KMH = float(os.getenv("TRANSIT_SPEED_KMH", "20") or "20")

STATIC_DIR = Path(__file__).parent / "static"

# Transport mode -> OpenRouteService routing profile.
# "transit" has no free routing profile, so we route it on the car network and
# derive a time from the distance using TRANSIT_SPEED_KMH.
MODE_PROFILE = {
    "car": "driving-car",
    "bike": "cycling-regular",
    "foot": "foot-walking",
    "transit": "driving-car",
}

app = FastAPI(title="Residence Optimizer")


class GeocodeRequest(BaseModel):
    query: str


class Point(BaseModel):
    lat: float
    lng: float


class Target(BaseModel):
    lat: float
    lng: float
    mode: Literal["car", "bike", "foot", "transit"]
    trips: float = 1  # one-way trips per week


class ComputeRequest(BaseModel):
    living: list[Point]
    targets: list[Target]


@app.get("/api/config")
def config():
    """Expose non-secret config + whether a routing key is present."""
    return {"has_key": bool(ORS_API_KEY), "transit_speed_kmh": TRANSIT_SPEED_KMH}


@app.post("/api/geocode")
async def geocode(req: GeocodeRequest):
    """Search place names via Nominatim (OpenStreetMap)."""
    q = req.query.strip()
    if not q:
        return []
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": q, "format": "jsonv2", "limit": 6, "addressdetails": 0},
            headers={"User-Agent": "residence-optimizer/1.0"},
        )
    if r.status_code != 200:
        raise HTTPException(502, "Geocoding service error")
    return [
        {"name": item["display_name"], "lat": float(item["lat"]), "lng": float(item["lon"])}
        for item in r.json()
    ]


async def _ors_matrix(profile: str, living: list[Point], targets: list[Target]):
    """Return duration (s) and distance (m) matrices from each living place to
    each of the given targets, using one OpenRouteService matrix call."""
    # ORS expects [lng, lat]. Sources = living places, destinations = targets.
    locations = [[p.lng, p.lat] for p in living] + [[t.lng, t.lat] for t in targets]
    sources = list(range(len(living)))
    destinations = list(range(len(living), len(living) + len(targets)))
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"https://api.openrouteservice.org/v2/matrix/{profile}",
            headers={"Authorization": ORS_API_KEY, "Content-Type": "application/json"},
            json={
                "locations": locations,
                "sources": sources,
                "destinations": destinations,
                "metrics": ["duration", "distance"],
            },
        )
    if r.status_code != 200:
        raise HTTPException(502, f"Routing error ({r.status_code}): {r.text[:200]}")
    data = r.json()
    return data.get("durations"), data.get("distances")


@app.post("/api/compute")
async def compute(req: ComputeRequest):
    """Compute weekly commute time for every living place and rank them."""
    if not ORS_API_KEY:
        raise HTTPException(400, "No ORS_API_KEY configured. See .env.example.")
    if not req.living or not req.targets:
        return {"results": []}

    n_living = len(req.living)
    # one_way_seconds[living_index][target_index]
    one_way = [[0.0] * len(req.targets) for _ in range(n_living)]

    # Group targets by routing profile so we make one matrix call per profile.
    by_profile: dict[str, list[int]] = {}
    for ti, t in enumerate(req.targets):
        by_profile.setdefault(MODE_PROFILE[t.mode], []).append(ti)

    transit_mps = TRANSIT_SPEED_KMH * 1000 / 3600

    for profile, tindices in by_profile.items():
        sub_targets = [req.targets[ti] for ti in tindices]
        durations, distances = await _ors_matrix(profile, req.living, sub_targets)
        for li in range(n_living):
            for col, ti in enumerate(tindices):
                mode = req.targets[ti].mode
                if mode == "transit":
                    dist = (distances[li][col] if distances else None)
                    secs = (dist / transit_mps) if dist else None
                else:
                    secs = durations[li][col] if durations else None
                one_way[li][ti] = secs if secs is not None else float("inf")

    results = []
    for li in range(n_living):
        breakdown = []
        weekly_minutes = 0.0
        reachable = True
        for ti, t in enumerate(req.targets):
            secs = one_way[li][ti]
            if secs == float("inf"):
                reachable = False
                breakdown.append({"one_way_min": None, "weekly_min": None})
                continue
            ow_min = secs / 60
            # Round trip (there and back) times trips per week.
            wk = ow_min * 2 * t.trips
            weekly_minutes += wk
            breakdown.append({"one_way_min": round(ow_min, 1), "weekly_min": round(wk, 1)})
        results.append({
            "index": li,
            "weekly_minutes": round(weekly_minutes, 1) if reachable else None,
            "reachable": reachable,
            "breakdown": breakdown,
        })

    # Sort: reachable first, then by weekly total ascending.
    results.sort(key=lambda x: (x["weekly_minutes"] is None, x["weekly_minutes"] or 0))
    return {"results": results}


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/", StaticFiles(directory=STATIC_DIR), name="static")
