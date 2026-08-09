"""Residence optimizer backend.

Serves the static frontend and proxies geocoding (Nominatim) and travel-time
matrix computation (OpenRouteService) so the API key stays server-side.

Both proxies are guarded: this runs in public with one shared key and one
shared allowance on free services, so every request is rate limited per IP, the
day's routing calls are capped, and answers are cached durably.
"""
import os
from pathlib import Path
from typing import Literal

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.limits import RateLimiter, Throttle, client_ip
from app.store import UNREACHABLE, Store

load_dotenv()

ORS_API_KEY = os.getenv("ORS_API_KEY", "").strip()
TRANSIT_SPEED_KMH = float(os.getenv("TRANSIT_SPEED_KMH", "20") or "20")

# How this deployment identifies itself to Nominatim. Their policy requires a
# User-Agent that names the application and lets them reach a human; a bare
# product name is the bare minimum and easy to get blocked on.
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip()
CONTACT_EMAIL = os.getenv("CONTACT_EMAIL", "").strip()

# The routing key is shared by everyone who visits, so the day's calls are a
# common resource. Stay under whatever the ORS plan allows, with headroom.
DAILY_ORS_BUDGET = int(os.getenv("DAILY_ORS_BUDGET", "1500") or 1500)

# Hard caps on a single request. ORS itself allows a 3500-cell matrix, which is
# far more than this UI can produce and far more than we want a stranger to be
# able to ask for in one shot.
MAX_LIVING = int(os.getenv("MAX_LIVING", "25") or 25)
MAX_TARGETS = int(os.getenv("MAX_TARGETS", "25") or 25)

# Analytics: cookieless Umami, configured per deployment so nothing
# analytics-related is baked into the repo. Empty = no analytics at all.
UMAMI_SRC = os.getenv("UMAMI_SRC", "").strip()
UMAMI_WEBSITE_ID = os.getenv("UMAMI_WEBSITE_ID", "").strip()
# Optional comma-separated hostnames to report from, so local development and
# preview deployments do not land in the production numbers.
UMAMI_DOMAINS = os.getenv("UMAMI_DOMAINS", "").strip()

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

store = Store()

# One-way travel times are cached by (mode, home lat/lng, target lat/lng). How
# often a target is visited is deliberately *not* part of the key: changing the
# frequency only rescales an already-known travel time, so it must never
# trigger a routing call.
_COORD_PRECISION = 6

# Per-IP budgets: a burst window and a daily one. A person comparing homes fires
# a handful of computes a minute at most; anything above this is a script.
compute_limiter = RateLimiter([
    (int(os.getenv("COMPUTE_PER_MIN", "20") or 20), 60),
    (int(os.getenv("COMPUTE_PER_DAY", "400") or 400), 86400),
])
geocode_limiter = RateLimiter([
    (int(os.getenv("GEOCODE_PER_MIN", "15") or 15), 60),
    (int(os.getenv("GEOCODE_PER_DAY", "300") or 300), 86400),
])
# Nominatim allows one request per second across all of our users combined.
nominatim_throttle = Throttle(float(os.getenv("NOMINATIM_MIN_INTERVAL", "1.1") or 1.1))


def user_agent() -> str:
    bits = [b for b in (f"+{PUBLIC_URL}" if PUBLIC_URL else "",
                        f"contact: {CONTACT_EMAIL}" if CONTACT_EMAIL else "") if b]
    return "residence-optimizer/1.0" + (f" ({'; '.join(bits)})" if bits else "")


def enforce(limiter: RateLimiter, request: Request) -> None:
    retry = limiter.check(client_ip(request))
    if retry is not None:
        raise HTTPException(
            429,
            "Too many requests from your connection. Please slow down and try again shortly.",
            headers={"Retry-After": str(retry)},
        )


class GeocodeRequest(BaseModel):
    query: str = Field(min_length=1, max_length=200)


class Point(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class Target(Point):
    mode: Literal["car", "bike", "foot", "transit"]
    trips: float = Field(default=1, ge=0, le=1000)  # one-way trips per week
    enabled: bool = True  # unchecked targets are left out of the totals


class ComputeRequest(BaseModel):
    living: list[Point]
    targets: list[Target]


@app.get("/api/config")
def config():
    """Expose non-secret config + whether a routing key is present."""
    return {
        "has_key": bool(ORS_API_KEY),
        "transit_speed_kmh": TRANSIT_SPEED_KMH,
        "max_living": MAX_LIVING,
        "max_targets": MAX_TARGETS,
        # The frontend loads Umami only if a deployment configured one.
        "umami_src": UMAMI_SRC,
        "umami_website_id": UMAMI_WEBSITE_ID,
        "umami_domains": UMAMI_DOMAINS,
    }


@app.post("/api/geocode")
async def geocode(req: GeocodeRequest, request: Request):
    """Search place names via Nominatim (OpenStreetMap)."""
    q = " ".join(req.query.split())
    if not q:
        return []

    cached = store.get_geocode(q.lower())
    if cached is not None:
        return cached

    enforce(geocode_limiter, request)
    await nominatim_throttle.wait()
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": q, "format": "jsonv2", "limit": 6, "addressdetails": 0},
            headers={"User-Agent": user_agent()},
        )
    if r.status_code != 200:
        raise HTTPException(502, "The place search is unavailable right now. Try again in a moment.")
    results = [
        {"name": item["display_name"], "lat": float(item["lat"]), "lng": float(item["lon"])}
        for item in r.json()
    ]
    store.set_geocode(q.lower(), results)
    return results


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
    if r.status_code == 429:
        raise HTTPException(429, "The routing service is rate limiting us. Please try again in a minute.")
    if r.status_code != 200:
        raise HTTPException(502, "Could not compute travel times right now. Please try again shortly.")
    data = r.json()
    return data.get("durations"), data.get("distances")


@app.post("/api/compute")
async def compute(req: ComputeRequest, request: Request):
    """Compute weekly commute time for every living place and rank them."""
    if not ORS_API_KEY:
        raise HTTPException(400, "This deployment has no routing key configured.")
    if len(req.living) > MAX_LIVING or len(req.targets) > MAX_TARGETS:
        raise HTTPException(
            400,
            f"Too many places at once — up to {MAX_LIVING} homes and {MAX_TARGETS} targets.",
        )
    if not req.living or not req.targets:
        return {"results": []}

    enforce(compute_limiter, request)

    # Group targets by routing profile so we make at most one matrix call per
    # profile. Disabled targets are never routed — they cost no API call at all.
    by_profile: dict[str, list[int]] = {}
    for ti, t in enumerate(req.targets):
        if t.enabled:
            by_profile.setdefault(MODE_PROFILE[t.mode], []).append(ti)

    # Work out what is missing before calling anything, so a request we cannot
    # afford is refused cleanly instead of half-served.
    todo: dict[str, tuple[list[int], list[int]]] = {}
    for profile, tindices in by_profile.items():
        missing_living, missing_targets = set(), set()
        for li, home in enumerate(req.living):
            for ti in tindices:
                if store.get_time(_cache_key(req.targets[ti].mode, home, req.targets[ti])) is None:
                    missing_living.add(li)
                    missing_targets.add(ti)
        if missing_targets:
            todo[profile] = (sorted(missing_living), sorted(missing_targets))

    # The day's routing calls are shared by every visitor. When they run out,
    # anything already cached still answers instantly — only genuinely new
    # places are refused, and they are told why.
    remaining = DAILY_ORS_BUDGET - store.spent_today()
    if todo and len(todo) > remaining:
        raise HTTPException(
            429,
            "The daily routing allowance for this site is used up. Places you have "
            "already looked up still work — new ones will be available tomorrow.",
            headers={"Retry-After": "3600"},
        )

    transit_mps = TRANSIT_SPEED_KMH * 1000 / 3600
    ors_calls = 0

    for profile, (srcs, dsts) in todo.items():
        durations, distances = await _ors_matrix(
            profile, [req.living[li] for li in srcs], [req.targets[ti] for ti in dsts]
        )
        ors_calls += 1
        store.spend(1)
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
                # Unroutable pairs are cached as unreachable too, so we don't ask again.
                store.set_time(
                    _cache_key(t.mode, req.living[li], t),
                    secs if secs is not None else UNREACHABLE,
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
            secs = store.get_time(_cache_key(t.mode, home, t))
            if secs is None or secs == UNREACHABLE:
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
