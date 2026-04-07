"""
Mini-RAFT Replica  —  fixed & production-ready
===============================================
Key fixes over v1:
  - Deadlock eliminated: _election_timer never holds the lock while
    spawning elections; lock is always released before IO.
  - _step_down_locked() is a pure state mutation, never awaited.
  - /heartbeat endpoint added (spec requirement).
  - CORS middleware added for browser /status polling.
  - Explicit ack_lock removed (asyncio.gather results collected cleanly).
  - Log rollback only happens if the entry is still the last one.
"""

import asyncio
import logging
import os
import random
import time
from dataclasses import dataclass, asdict
from enum import Enum
from typing import Optional

import aiohttp
from aiohttp import web

# ── Logging ───────────────────────────────────────────────────────────────────
REPLICA_ID = os.getenv("REPLICA_ID", "replica1")
logging.basicConfig(
    level=logging.INFO,
    format=f"%(asctime)s [{REPLICA_ID.upper()}] %(levelname)s %(message)s",
)
log = logging.getLogger(REPLICA_ID)

# ── Configuration ─────────────────────────────────────────────────────────────
PORT        = int(os.getenv("PORT", 8001))
GATEWAY_URL = os.getenv("GATEWAY_URL", "http://gateway:8000")

PEER_URLS: dict[str, str] = {
    k: v
    for k, v in {
        "replica1": os.getenv("REPLICA1_URL", "http://replica1:8001"),
        "replica2": os.getenv("REPLICA2_URL", "http://replica2:8002"),
        "replica3": os.getenv("REPLICA3_URL", "http://replica3:8003"),
    }.items()
    if k != REPLICA_ID
}

ELECTION_TIMEOUT_MIN = 0.500
ELECTION_TIMEOUT_MAX = 0.800
HEARTBEAT_INTERVAL   = 0.150
RPC_TIMEOUT          = 0.400
TIMER_TICK           = 0.050


def rand_timeout() -> float:
    return random.uniform(ELECTION_TIMEOUT_MIN, ELECTION_TIMEOUT_MAX)


# ── Data model ────────────────────────────────────────────────────────────────

class Role(str, Enum):
    FOLLOWER  = "follower"
    CANDIDATE = "candidate"
    LEADER    = "leader"


@dataclass
class LogEntry:
    index: int
    term:  int
    data:  dict


# ── RAFT Node ─────────────────────────────────────────────────────────────────

class RaftNode:
    """
    LOCKING CONVENTION
    ------------------
    self._lock guards ALL mutable fields.
    Methods suffixed _locked() must only be called while _lock is held
    and must contain no awaits.
    All network IO happens outside the lock.
    """

    def __init__(self):
        self.id:           str           = REPLICA_ID
        self.current_term: int           = 0
        self.voted_for:    Optional[str] = None
        self.role:         Role          = Role.FOLLOWER
        self.leader_id:    Optional[str] = None
        self.log:          list[LogEntry] = []
        self.commit_index: int           = -1
        self._last_hb:     float         = time.monotonic()
        self._el_timeout:  float         = rand_timeout()
        self._session:     Optional[aiohttp.ClientSession] = None
        self._hb_task:     Optional[asyncio.Task]          = None
        self._timer_task:  Optional[asyncio.Task]          = None
        self._lock = asyncio.Lock()

    # Lifecycle ----------------------------------------------------------------

    async def start(self, session: aiohttp.ClientSession):
        self._session    = session
        self._timer_task = asyncio.create_task(self._timer_loop())
        log.info(f"Started FOLLOWER term={self.current_term}")

    async def stop(self):
        for t in (self._hb_task, self._timer_task):
            if t:
                t.cancel()

    # Election timer -----------------------------------------------------------

    async def _timer_loop(self):
        while True:
            await asyncio.sleep(TIMER_TICK)
            fire = False
            async with self._lock:
                if self.role != Role.LEADER:
                    if time.monotonic() - self._last_hb >= self._el_timeout:
                        self._last_hb  = time.monotonic()
                        self._el_timeout = rand_timeout()
                        fire = True
            if fire:
                asyncio.create_task(self._run_election())

    # Election -----------------------------------------------------------------

    async def _run_election(self):
        # Snapshot state, transition to Candidate — all under lock
        async with self._lock:
            if self.role == Role.LEADER:
                return
            self.role          = Role.CANDIDATE
            self.current_term += 1
            self.voted_for     = self.id
            self.leader_id     = None
            term               = self.current_term
            lli                = len(self.log) - 1
            llt                = self.log[-1].term if self.log else 0

        log.info(f"[ELECT] term={term}")

        # Parallel vote requests — no lock held
        async def ask(pid: str, purl: str) -> bool:
            try:
                async with self._session.post(
                    f"{purl}/request-vote",
                    json=dict(term=term, candidate_id=self.id,
                              last_log_index=lli, last_log_term=llt),
                    timeout=aiohttp.ClientTimeout(total=RPC_TIMEOUT),
                ) as r:
                    d = await r.json()
                    if d.get("term", 0) > term:
                        async with self._lock:
                            if d["term"] > self.current_term:
                                self._step_down_locked(d["term"])
                        return False
                    return bool(d.get("vote_granted"))
            except Exception as e:
                log.debug(f"[ELECT] vote→{pid}: {e}")
                return False

        granted = await asyncio.gather(*(ask(p, u) for p, u in PEER_URLS.items()))
        votes   = 1 + sum(granted)
        total   = 1 + len(PEER_URLS)
        majority = (total // 2) + 1

        async with self._lock:
            if self.role != Role.CANDIDATE or self.current_term != term:
                return
            if votes >= majority:
                self._become_leader_locked()
            else:
                log.info(f"[ELECT] Lost term={term} ({votes}/{majority})")
                self.role     = Role.FOLLOWER
                self._last_hb = time.monotonic()
                self._el_timeout = rand_timeout()

    # State transitions (locked) -----------------------------------------------

    def _become_leader_locked(self):
        self.role      = Role.LEADER
        self.leader_id = self.id
        log.info(f"★ LEADER term={self.current_term}")
        if self._hb_task:
            self._hb_task.cancel()
        self._hb_task = asyncio.create_task(self._hb_loop())

    def _step_down_locked(self, new_term: int):
        log.info(f"Step-down {self.current_term}→{new_term}")
        self.current_term = new_term
        self.role         = Role.FOLLOWER
        self.voted_for    = None
        self._last_hb     = time.monotonic()
        self._el_timeout  = rand_timeout()
        if self._hb_task:
            self._hb_task.cancel()
            self._hb_task = None

    # Heartbeat loop -----------------------------------------------------------

    async def _hb_loop(self):
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            async with self._lock:
                if self.role != Role.LEADER:
                    return
                term    = self.current_term
                lid     = self.id
                entries = [asdict(e) for e in self.log]
                ci      = self.commit_index

            await asyncio.gather(*(
                self._send_ae(pid, purl, term, lid, entries, ci)
                for pid, purl in PEER_URLS.items()
            ))

    async def _send_ae(self, peer_id, peer_url, term, leader_id, entries, ci):
        try:
            async with self._session.post(
                f"{peer_url}/append-entries",
                json=dict(term=term, leader_id=leader_id,
                          entries=entries, leader_commit=ci),
                timeout=aiohttp.ClientTimeout(total=RPC_TIMEOUT),
            ) as r:
                d = await r.json()
                if d.get("term", 0) > term:
                    async with self._lock:
                        if d["term"] > self.current_term:
                            self._step_down_locked(d["term"])
        except Exception as e:
            log.debug(f"[HB] ae→{peer_id}: {e}")

    # Client stroke replication -----------------------------------------------

    async def append_stroke(self, stroke: dict) -> bool:
        # 1. Append locally
        async with self._lock:
            if self.role != Role.LEADER:
                return False
            entry   = LogEntry(index=len(self.log), term=self.current_term, data=stroke)
            self.log.append(entry)
            term    = self.current_term
            entries = [asdict(e) for e in self.log]
            ci      = self.commit_index

        log.info(f"[REPLICATE] idx={entry.index} term={term}")

        # 2. Replicate to peers
        async def rep(pid: str, purl: str) -> bool:
            try:
                async with self._session.post(
                    f"{purl}/append-entries",
                    json=dict(term=term, leader_id=self.id,
                              entries=entries, leader_commit=ci),
                    timeout=aiohttp.ClientTimeout(total=RPC_TIMEOUT),
                ) as r:
                    d = await r.json()
                    if d.get("term", 0) > term:
                        async with self._lock:
                            if d["term"] > self.current_term:
                                self._step_down_locked(d["term"])
                        return False
                    return bool(d.get("success"))
            except Exception as e:
                log.debug(f"[REPLICATE] →{pid}: {e}")
                return False

        ack_results = await asyncio.gather(*(rep(p, u) for p, u in PEER_URLS.items()))
        acks        = 1 + sum(ack_results)
        majority    = ((1 + len(PEER_URLS)) // 2) + 1

        # 3. Commit or rollback
        async with self._lock:
            if self.role != Role.LEADER or self.current_term != term:
                log.warning(f"[REPLICATE] No longer leader; discarding idx={entry.index}")
                return False
            if acks >= majority:
                self.commit_index = entry.index
                log.info(f"✓ Committed idx={entry.index} ({acks}/{majority})")
            else:
                if self.log and self.log[-1].index == entry.index:
                    self.log.pop()
                log.warning(f"✗ NOT committed idx={entry.index} ({acks}/{majority})")
                return False

        asyncio.create_task(self._notify_gateway(stroke))
        return True

    async def _notify_gateway(self, stroke: dict):
        try:
            async with self._session.post(
                f"{GATEWAY_URL}/committed",
                json={"stroke": stroke},
                timeout=aiohttp.ClientTimeout(total=1.5),
            ):
                pass
        except Exception as e:
            log.warning(f"[NOTIFY] gateway: {e}")


# ── CORS middleware ───────────────────────────────────────────────────────────

@web.middleware
async def cors_mw(request: web.Request, handler):
    if request.method == "OPTIONS":
        return web.Response(headers={
            "Access-Control-Allow-Origin":  "*",
            "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        })
    resp = await handler(request)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


# ── HTTP handlers ─────────────────────────────────────────────────────────────

async def h_request_vote(req: web.Request) -> web.Response:
    node: RaftNode = req.app["node"]
    d  = await req.json()
    term, cid = d["term"], d["candidate_id"]
    lli, llt  = d["last_log_index"], d["last_log_term"]

    async with node._lock:
        granted = False
        if term > node.current_term:
            node._step_down_locked(term)
        if term == node.current_term:
            my_lli = len(node.log) - 1
            my_llt = node.log[-1].term if node.log else 0
            log_ok = llt > my_llt or (llt == my_llt and lli >= my_lli)
            if log_ok and node.voted_for in (None, cid):
                node.voted_for = cid
                node._last_hb  = time.monotonic()
                granted        = True
                log.info(f"[VOTE] → {cid} term={term}")
        t = node.current_term

    return web.json_response({"term": t, "vote_granted": granted})


async def h_append_entries(req: web.Request) -> web.Response:
    node: RaftNode = req.app["node"]
    d   = await req.json()
    term          = d["term"]
    leader_id     = d["leader_id"]
    entries_raw   = d.get("entries", [])
    leader_commit = d.get("leader_commit", -1)

    async with node._lock:
        if term < node.current_term:
            return web.json_response({"term": node.current_term, "success": False})
        if term > node.current_term:
            node._step_down_locked(term)

        node._last_hb  = time.monotonic()
        node.leader_id = leader_id
        if node.role == Role.CANDIDATE:
            node.role = Role.FOLLOWER

        if entries_raw:
            node.log = [LogEntry(e["index"], e["term"], e["data"]) for e in entries_raw]

        if leader_commit > node.commit_index:
            node.commit_index = min(leader_commit, len(node.log) - 1)

        t = node.current_term

    return web.json_response({"term": t, "success": True})


async def h_heartbeat(req: web.Request) -> web.Response:
    """Standalone /heartbeat endpoint — same as empty AppendEntries."""
    node: RaftNode = req.app["node"]
    d    = await req.json()
    term = d.get("term", 0)
    lid  = d.get("leader_id", "")

    async with node._lock:
        if term < node.current_term:
            return web.json_response({"term": node.current_term, "success": False})
        if term > node.current_term:
            node._step_down_locked(term)
        node._last_hb  = time.monotonic()
        node.leader_id = lid
        if node.role == Role.CANDIDATE:
            node.role = Role.FOLLOWER
        t = node.current_term

    return web.json_response({"term": t, "success": True})


async def h_sync_log(req: web.Request) -> web.Response:
    """GET /sync-log?from=N  —  returns committed entries [N, commit_index]."""
    node: RaftNode = req.app["node"]
    from_idx = int(req.rel_url.query.get("from", 0))
    async with node._lock:
        out = [asdict(e) for e in node.log
               if e.index >= from_idx and e.index <= node.commit_index]
        ci  = node.commit_index
    return web.json_response({"entries": out, "commit_index": ci})


async def h_stroke(req: web.Request) -> web.Response:
    node: RaftNode = req.app["node"]
    async with node._lock:
        role = node.role
        lid  = node.leader_id
    if role != Role.LEADER:
        return web.json_response({"error": "not leader", "leader_id": lid}, status=400)
    stroke = await req.json()
    ok = await node.append_stroke(stroke)
    return (web.json_response({"status": "committed"}) if ok
            else web.json_response({"error": "commit failed"}, status=500))


async def h_status(req: web.Request) -> web.Response:
    node: RaftNode = req.app["node"]
    async with node._lock:
        return web.json_response({
            "id":           node.id,
            "role":         node.role.value,
            "term":         node.current_term,
            "leader_id":    node.leader_id,
            "log_length":   len(node.log),
            "commit_index": node.commit_index,
            "voted_for":    node.voted_for,
        })


async def h_log(req: web.Request) -> web.Response:
    node: RaftNode = req.app["node"]
    async with node._lock:
        committed = [asdict(e) for e in node.log if e.index <= node.commit_index]
    return web.json_response({"committed_entries": committed})


# ── App lifecycle ─────────────────────────────────────────────────────────────

async def on_startup(app: web.Application):
    s = aiohttp.ClientSession()
    app["session"] = s
    await app["node"].start(s)
    log.info(f"Listening on :{PORT}")


async def on_cleanup(app: web.Application):
    await app["node"].stop()
    await app["session"].close()


def build_app() -> web.Application:
    app = web.Application(middlewares=[cors_mw])
    app["node"] = RaftNode()
    app.router.add_post("/request-vote",   h_request_vote)
    app.router.add_post("/append-entries", h_append_entries)
    app.router.add_post("/heartbeat",      h_heartbeat)
    app.router.add_get( "/sync-log",       h_sync_log)
    app.router.add_post("/stroke",         h_stroke)
    app.router.add_get( "/status",         h_status)
    app.router.add_get( "/log",            h_log)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    web.run_app(build_app(), host="0.0.0.0", port=PORT)
