"""FastAPI app: REST snapshot, WebSocket live feed, and static frontend."""
from __future__ import annotations

import asyncio
import os

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, data_feed
from .engine import engine, run_loop

app = FastAPI(title="SMC Bot", version="1.0")

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")

_clients: set[WebSocket] = set()
_loop_task: asyncio.Task | None = None
_broadcast_task: asyncio.Task | None = None


@app.on_event("startup")
async def _startup():
    global _loop_task, _broadcast_task
    _loop_task = asyncio.create_task(run_loop())
    _broadcast_task = asyncio.create_task(_broadcaster())


@app.on_event("shutdown")
async def _shutdown():
    for t in (_loop_task, _broadcast_task):
        if t:
            t.cancel()
    await data_feed.close()


async def _broadcaster():
    """Push the latest snapshot to all websocket clients every few seconds."""
    while True:
        await asyncio.sleep(3)
        if not _clients:
            continue
        snap = engine.snapshot()
        dead = []
        for ws in list(_clients):
            try:
                await ws.send_json(snap)
            except Exception:
                dead.append(ws)
        for ws in dead:
            _clients.discard(ws)


@app.get("/api/snapshot")
async def api_snapshot():
    return JSONResponse(engine.snapshot())


@app.get("/api/backtest")
async def api_backtest():
    path = os.path.join(FRONTEND_DIR, "..", "docs", "data", "backtest.json")
    if os.path.exists(path):
        return FileResponse(path, media_type="application/json")
    return JSONResponse({"summary": {}, "error": "Belum ada hasil backtest."})


@app.get("/api/health")
async def api_health():
    return {"ok": True, "last_scan": engine.last_scan, "scanning": engine.scanning}


@app.post("/api/scan")
async def api_scan():
    await engine.scan()
    return {"ok": True, "last_scan": engine.last_scan}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    _clients.add(ws)
    try:
        await ws.send_json(engine.snapshot())  # immediate first paint
        while True:
            await ws.receive_text()  # keep alive; ignore content
    except WebSocketDisconnect:
        pass
    finally:
        _clients.discard(ws)


# ---- static frontend ----
@app.get("/")
async def index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


app.mount("/", StaticFiles(directory=FRONTEND_DIR), name="static")
