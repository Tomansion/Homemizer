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
- **Ranking** — for each home: `Σ (one-way time × 2 × trips_per_week)` over all
  targets, i.e. total round-trip minutes spent commuting each week.

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
2. Click **🏠 Add home**, then place candidate homes.
3. Homes are ranked automatically — number 1 is the shortest total weekly commute.
