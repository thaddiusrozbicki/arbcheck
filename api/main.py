"""ArbCheck web server: the JSON API under /api, plus the website pages.

Run it from the repo root, next to a running poller:

    uvicorn api.main:app --reload --port 8000

Then open http://localhost:8000. On a server, drop --reload.
"""

import os
from contextlib import contextmanager

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from api import service
from poller import db

SITE_DIR = os.environ.get("ARBCHECK_SITE", "mockups")

app = FastAPI(title="ArbCheck", description="Kalshi and Polymarket price gap scanner")


@contextmanager
def database():
    con = db.connect()
    try:
        yield con
    finally:
        con.close()


@app.get("/api/health")
def health():
    """Is the poller feeding the database? Shows how long since the last order book."""
    with database() as con:
        return service.health(con)


@app.get("/api/pairs")
def pairs():
    """Every active pair with both exchanges' best prices, their ages, and the current edge."""
    with database() as con:
        return service.list_pairs(con)


@app.get("/api/pairs/{pair_id}")
def pair(pair_id: str):
    """One pair: both order books, the best trade plan, and the rules."""
    with database() as con:
        detail = service.pair_detail(con, pair_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"No pair called {pair_id}")
    return detail


@app.get("/api/opportunities")
def opportunities(limit: int = 100):
    """The opportunity log, newest first, including gaps that are open right now."""
    with database() as con:
        return service.list_opportunities(con, limit)


@app.get("/api/results")
def results():
    """How many gaps showed up, how many survived fees, and how big the survivors were."""
    with database() as con:
        return service.results(con)


# The website pages. This has to come after the API routes.
app.mount("/", StaticFiles(directory=SITE_DIR, html=True), name="site")
