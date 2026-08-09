# Residence Optimizer

A web app to find the best place to live. Drop the places you regularly go
(work, gym, family…), set **how you travel** to each (car, bike, foot, transit)
and **how often per week**, then drop candidate homes on the map. Each home is
ranked by **total weekly commute time**.

![map with targets and ranked homes](https://via.placeholder.com/1) <!-- optional screenshot -->

## How it works

- **Map & tiles** — Leaflet + OpenStreetMap (no key).
- **Search** — Nominatim geocoding (no key).
- **Travel times** — [OpenRouteService](https://openrouteservice.org) matrix API
  for car / bike / foot (needs a free API key).
- **Transit** — no free real-time transit API exists, so transit time is
  estimated from the car-network distance at an average door-to-door speed
  (`TRANSIT_SPEED_KMH`, default 20 km/h).
- **Scoring** — for each home: `Σ (one-way time × 2 × trips_per_week)` over all
  targets, i.e. total round-trip minutes spent commuting each week. That total
  becomes a percentage against the best home (`100 × best / total`), so homes
  within a few minutes of each other read as equivalent. Each home lists the
  per-target detail: one-way time and the cumulated weekly time it contributes.
- **Centre of gravity** — a dotted-line hub on the map, at the weighted average
  of the counted targets. Each target's weight is `trips_per_week ÷ speed of its
  mode` (car 50, transit `TRANSIT_SPEED_KMH`, bike 15, foot 5 km/h), i.e. how
  much travel time it costs per kilometre — so a daily walk pulls the centre far
  harder than a monthly drive. It is straight-line geometry, not routing: a free
  hint of where to look, computed in the browser with no API call.
- **Caching** — one-way times are cached server-side per
  `(home, target, transport mode)`. Only never-seen pairs are sent to
  OpenRouteService, so changing a target's frequency (or re-ranking existing
  places) costs no API call.

Your places are saved in the browser (localStorage).

## Setup

1. Get a free API key at <https://openrouteservice.org/dev/#/signup>.
2. Copy the env file and paste your key:
   ```bash
   cp .env.example .env
   # edit .env → ORS_API_KEY=...
   ```

### Run with Docker

```bash
docker compose up --build
```

### Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open <http://localhost:8000>.

## Usage

1. Click **📍 Add target**, then click the map (or use search) to place a spot
   you regularly go to. Set its transport mode and trips per week.
2. The purple **◎** marker is the centre of gravity of your targets, linked to
   each of them by a dotted line. Use it as a starting point for where to hunt.
3. Click **🏠 Add home**, then place candidate homes.
4. Homes are listed best first, each with a score: **100%** is the shortest
   total weekly commute, 50% means twice as long. Homes a few minutes apart
   score almost the same. Hover a home in the list to make its marker pop out
   on the map.
5. Uncheck a target to leave it out of the totals without deleting it
   (it stays on the map, greyed out, drops out of the centre of gravity, and
   costs no routing call).
6. Clicking any marker opens its sidebar card right on the map — same controls,
   same travel details (score, weekly total, per-target legs for a home; mode,
   frequency, counted-or-not and remove for a target). Editing from the popup
   updates the sidebar, and vice versa: it is literally the same card.
