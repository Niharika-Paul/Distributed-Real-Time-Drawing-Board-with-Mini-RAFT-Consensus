"""
Gateway Service  —  fixed for Python 3.12 / aiohttp 3.9
- asyncio.Lock() created inside the app (not at module level)
- No module-level event-loop-bound objects
- WebSocket proxy for nginx
"""

import asyncio
import json
import logging
import os

import aiohttp
from aiohttp import web, WSMsgType

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [GATEWAY] %(levelname)s %(message)s",
)
log = logging.getLogger("gateway")

# ── Config ────────────────────────────────────────────────────────────────────
REPLICA_URLS = [
    os.getenv("REPLICA1_URL", "http://replica1:8001"),
    os.getenv("REPLICA2_URL", "http://replica2:8002"),
    os.getenv("REPLICA3_URL", "http://replica3:8003"),
]
LEADER_POLL_INTERVAL = 0.5
FORWARD_TIMEOUT      = 2.0


# ── App state (stored on app[], NOT at module level) ──────────────────────────
# This avoids asyncio event-loop binding issues on Python 3.12

def get_state(app):
    return app["gw_state"]


# ── Leader Discovery ──────────────────────────────────────────────────────────

async def find_leader(session: aiohttp.ClientSession) -> str | None:
    for url in REPLICA_URLS:
        try:
            async with session.get(
                f"{url}/status",
                timeout=aiohttp.ClientTimeout(total=1),
            ) as resp:
                if resp.status != 200:
                    continue
                data = await resp.json()
                if data.get("role") == "leader":
                    return url
                # Follower knows who the leader is
                leader_id = data.get("leader_id")
                if leader_id:
                    for u in REPLICA_URLS:
                        if leader_id in u:
                            return u
        except Exception:
            pass
    return None


async def leader_watcher(app: web.Application):
    state   = get_state(app)
    session = app["session"]
    while True:
        try:
            leader = await find_leader(session)
            async with state["lock"]:
                if leader != state["leader_url"]:
                    log.info(f"Leader: {state['leader_url']} → {leader}")
                    state["leader_url"] = leader
        except Exception as e:
            log.warning(f"Leader watcher: {e}")
        await asyncio.sleep(LEADER_POLL_INTERVAL)


# ── Broadcast ─────────────────────────────────────────────────────────────────

async def broadcast(app: web.Application, message: dict):
    state   = get_state(app)
    clients = state["clients"]
    dead    = set()
    payload = json.dumps(message)
    for ws in list(clients):
        try:
            await ws.send_str(payload)
        except Exception:
            dead.add(ws)
    clients.difference_update(dead)


# ── WebSocket handler ─────────────────────────────────────────────────────────

async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws      = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)

    state   = get_state(request.app)
    session = request.app["session"]
    state["clients"].add(ws)
    log.info(f"Client connected  (total={len(state['clients'])})")

    # Send snapshot of existing committed strokes
    try:
        async with state["lock"]:
            leader = state["leader_url"]
        if leader:
            async with session.get(
                f"{leader}/log",
                timeout=aiohttp.ClientTimeout(total=2),
            ) as resp:
                if resp.status == 200:
                    data      = await resp.json()
                    committed = data.get("committed_entries", [])
                    if committed:
                        await ws.send_str(json.dumps({
                            "type":    "snapshot",
                            "strokes": [e["data"] for e in committed],
                        }))
    except Exception as e:
        log.warning(f"Snapshot error: {e}")

    # Message loop
    async for msg in ws:
        if msg.type == WSMsgType.TEXT:
            try:
                payload = json.loads(msg.data)
                if payload.get("type") == "stroke":
                    ok = await forward_stroke(request.app, session, payload)
                    if not ok:
                        await ws.send_str(json.dumps({"type": "error", "msg": "no leader"}))
            except json.JSONDecodeError:
                pass
        elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
            break

    state["clients"].discard(ws)
    log.info(f"Client disconnected (total={len(state['clients'])})")
    return ws


async def forward_stroke(
    app: web.Application,
    session: aiohttp.ClientSession,
    stroke: dict,
) -> bool:
    state = get_state(app)
    async with state["lock"]:
        leader = state["leader_url"]

    if not leader:
        log.warning("No leader — dropping stroke")
        return False

    try:
        async with session.post(
            f"{leader}/stroke",
            json=stroke,
            timeout=aiohttp.ClientTimeout(total=FORWARD_TIMEOUT),
        ) as resp:
            if resp.status == 200:
                return True
            log.warning(f"Leader rejected stroke: {resp.status}")
            return False
    except Exception as e:
        log.warning(f"Stroke forward failed: {e}")
        async with state["lock"]:
            state["leader_url"] = None   # force re-discovery
        return False


# ── REST endpoints ────────────────────────────────────────────────────────────

async def committed_handler(request: web.Request) -> web.Response:
    data = await request.json()
    await broadcast(request.app, {"type": "stroke", "stroke": data.get("stroke")})
    return web.Response(text="ok")


async def health_handler(request: web.Request) -> web.Response:
    state = get_state(request.app)
    async with state["lock"]:
        leader = state["leader_url"]
    return web.json_response({
        "status":  "ok",
        "clients": len(state["clients"]),
        "leader":  leader,
    })


# ── Lifecycle ─────────────────────────────────────────────────────────────────

async def on_startup(app: web.Application):
    # Create all event-loop-bound objects HERE, inside the running loop
    app["gw_state"] = {
        "lock":       asyncio.Lock(),   # ← created inside the loop, not at import time
        "leader_url": None,
        "clients":    set(),
    }
    app["session"]      = aiohttp.ClientSession()
    app["leader_task"]  = asyncio.create_task(leader_watcher(app))
    log.info("Gateway started")


async def on_cleanup(app: web.Application):
    app["leader_task"].cancel()
    try:
        await app["leader_task"]
    except asyncio.CancelledError:
        pass
    await app["session"].close()
    log.info("Gateway stopped")


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get( "/ws",        ws_handler)
    app.router.add_post("/committed", committed_handler)
    app.router.add_get( "/health",    health_handler)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    web.run_app(build_app(), host="0.0.0.0", port=port)
