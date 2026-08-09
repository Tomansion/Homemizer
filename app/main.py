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

# Cache of one-way travel times (seconds), keyed by
# (mode, home lat/lng, target lat/lng). How often a target is visited is
# deliberately *not* part of the key: changing the frequency only rescales an
# already-known travel time, so it must never trigger a routing call.
_COORD_PRECISION = 6
_time_cache: dict[tuple, float] = {}


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
    enabled: bool = True  # unchecked targets are left out of the totals


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


def _cache_key(mode: str, home: Point, target: Target) -> tuple:
    return (
        mode,
        round(home.lat, _COORD_PRECISION), round(home.lng, _COORD_PRECISION),
        round(target.lat, _COORD_PRECISION), round(target.lng, _COORD_PRECISION),
    )


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

    # Group targets by routing profile so we make at most one matrix call per
    # profile. Disabled targets are never routed — they cost no API call at all.
    by_profile: dict[str, list[int]] = {}
    for ti, t in enumerate(req.targets):
        if t.enabled:
            by_profile.setdefault(MODE_PROFILE[t.mode], []).append(ti)

    transit_mps = TRANSIT_SPEED_KMH * 1000 / 3600
    ors_calls = 0

    for profile, tindices in by_profile.items():
        # Only ask the routing API for the (home, target) pairs we have never seen.
        missing_living, missing_targets = set(), set()
        for li, home in enumerate(req.living):
            for ti in tindices:
                if _cache_key(req.targets[ti].mode, home, req.targets[ti]) not in _time_cache:
                    missing_living.add(li)
                    missing_targets.add(ti)
        if not missing_targets:
            continue

        srcs, dsts = sorted(missing_living), sorted(missing_targets)
        durations, distances = await _ors_matrix(
            profile, [req.living[li] for li in srcs], [req.targets[ti] for ti in dsts]
        )
        ors_calls += 1
        # The matrix covers every src×dst pair, so cache them all — including the
        # few that were already known and got recomputed as part of the sub-matrix.
        for row, li in enumerate(srcs):
            for col, ti in enumerate(dsts):
                t = req.targets[ti]
                if t.mode == "transit":
                    dist = distances[row][col] if distances else None
                    secs = (dist / transit_mps) if dist is not None else None
                else:
                    secs = durations[row][col] if durations else None
                # Unroutable pairs are cached as inf too, so we don't ask again.
                _time_cache[_cache_key(t.mode, req.living[li], t)] = (
                    secs if secs is not None else float("inf")
                )

    results = []
    for li, home in enumerate(req.living):
        breakdown = []
        weekly_minutes = 0.0
        reachable = True
        for t in req.targets:
            leg = {"mode": t.mode, "trips": t.trips, "enabled": t.enabled}
            # A disabled target is only reported if its time is already known;
            # it is never counted, and never worth a routing call.
            secs = _time_cache.get(_cache_key(t.mode, home, t), float("inf"))
            if secs == float("inf"):
                if t.enabled:
                    reachable = False
                breakdown.append({**leg, "one_way_min": None, "round_trip_min": None, "weekly_min": None})
                continue
            ow_min = secs / 60
            # Round trip (there and back) times trips per week.
            wk = ow_min * 2 * t.trips
            if t.enabled:
                weekly_minutes += wk
            breakdown.append({
                **leg,
                "one_way_min": round(ow_min, 1),
                "round_trip_min": round(ow_min * 2, 1),
                "weekly_min": round(wk, 1),
            })
        results.append({
            "index": li,
            "weekly_minutes": round(weekly_minutes, 1) if reachable else None,
            "reachable": reachable,
            "breakdown": breakdown,
        })

    # Sort: reachable first, then by weekly total ascending.
    results.sort(key=lambda x: (x["weekly_minutes"] is None, x["weekly_minutes"] or 0))

    # Score each home against the best one: 100% is the shortest total, and a
    # home taking twice as long scores 50%. Unlike a rank, this shows *how much*
    # worse a home is — two homes a few minutes apart both land near 100%.
    best = next((r["weekly_minutes"] for r in results if r["weekly_minutes"] is not None), None)
    for r in results:
        wk = r["weekly_minutes"]
        if wk is None or best is None:
            r["score_pct"] = None
        elif wk <= 0:
            r["score_pct"] = 100  # nothing to commute to (all targets off)
        else:
            r["score_pct"] = round(100 * best / wk)

    return {"results": results, "ors_calls": ors_calls}


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/", StaticFiles(directory=STATIC_DIR), name="static")
