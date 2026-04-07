# Distributed Real-Time Drawing Board · Mini-RAFT

A fault-tolerant collaborative whiteboard backed by a RAFT-like consensus
protocol. Built with Python (aiohttp), WebSockets, Docker, and an HTML5 canvas
frontend.

---

## Architecture

```
Browser clients
     │  WebSocket
     ▼
  ┌─────────┐
  │ Gateway │  :8000  — manages WS clients, routes strokes, broadcasts commits
  └────┬────┘
       │ HTTP RPCs
  ┌────┴────────────────────────────────┐
  │                                     │
replica1:8001   replica2:8002   replica3:8003
  │                                     │
  └─────────── RAFT consensus ──────────┘
```

### RAFT Implementation

| Parameter          | Value         |
|--------------------|---------------|
| Election timeout   | 500–800 ms (random) |
| Heartbeat interval | 150 ms        |
| Quorum (3 nodes)   | ≥ 2 votes     |
| RPC endpoints      | /request-vote, /append-entries, /sync-log, /stroke, /status, /log |

---

## Quick Start

```bash
# 1. Clone / enter the project
cd miniraft

# 2. Build and start everything
docker compose up --build

# 3. Open the board
open http://localhost        # frontend
```

Services exposed on your host:
- `http://localhost`     — Drawing board
- `http://localhost:8000` — Gateway (WebSocket at /ws)
- `http://localhost:8001` — Replica 1 status
- `http://localhost:8002` — Replica 2 status
- `http://localhost:8003` — Replica 3 status

---

## Testing Scenarios

### 1. Basic multi-user drawing
Open `http://localhost` in two browser tabs. Draw on one — strokes appear on both.

### 2. Kill the leader
```bash
# Find which replica is leader
curl http://localhost:8001/status | python3 -m json.tool

# Kill it (e.g., if replica1 is leader)
docker compose stop replica1

# Watch the sidebar — remaining replicas elect a new leader in <800ms
# Keep drawing — zero downtime!

# Bring it back — it catches up automatically
docker compose start replica1
```

### 3. Hot-reload (edit any replica's code)
```bash
# Edit a replica's source code
echo "# changed" >> replica2/replica.py

# Container auto-restarts via watchfiles
# RAFT election fires — new leader elected
# Drawing continues without client disconnection
```

### 4. Stress test (kill and restart rapidly)
```bash
# Run in a loop
for i in 1 2 3; do
  docker compose restart replica$i
  sleep 0.5
done
```

### 5. Inspect individual replica state
```bash
curl http://localhost:8001/status   # role, term, leader_id, log_length
curl http://localhost:8001/log      # all committed log entries
curl http://localhost:8001/sync-log?from=0  # full committed log from index 0
```

---

## Project Structure

```
miniraft/
├── docker-compose.yml
├── frontend/
│   ├── index.html          # Canvas UI + cluster status dashboard
│   ├── nginx.conf
│   └── Dockerfile
├── gateway/
│   ├── gateway.py          # WebSocket gateway, leader routing
│   ├── requirements.txt
│   └── Dockerfile
├── replica1/               # ← bind-mounted: edit here → hot-reload
│   ├── replica.py          # Full Mini-RAFT state machine
│   ├── requirements.txt
│   └── Dockerfile
├── replica2/               # identical code, different REPLICA_ID env var
└── replica3/
```

---

## RAFT Protocol — Key Flows

### Leader Election
1. Follower misses heartbeat within 500–800 ms timeout
2. Becomes Candidate, increments `current_term`, votes for self
3. Sends `POST /request-vote` to all peers
4. On receiving ≥2 votes → becomes Leader
5. Immediately starts sending heartbeats every 150 ms

### Log Replication
1. Client stroke → Gateway → Leader's `POST /stroke`
2. Leader appends to local log
3. Broadcasts `POST /append-entries` with full log to both followers
4. On ≥2 acknowledgments → marks entry as committed
5. Leader calls `POST /committed` on Gateway → Gateway broadcasts to all WebSocket clients

### Catch-Up (Restarted Node)
1. Restarted follower starts with empty log
2. Leader's next `AppendEntries` overwrites follower's log with full committed history
3. Follower advances `commit_index` to match leader's
4. Follower participates normally in next election

---


## Bonus Challenges

- **Dashboard**: Replica cards in the sidebar show live role/term/log size (polling `/status`)
- **Snapshot on join**: New browser tabs receive the full committed stroke history immediately
- **Deploy to AWS EC2**: Replace `localhost` with your EC2 public IP in `index.html`'s `GATEWAY_WS`

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| No leader elected | Check all 3 replicas are healthy: `docker compose ps` |
| Canvas blank after restart | Refresh browser — snapshot is sent on WS connect |
| Port already in use | `lsof -ti:8000,8001,8002,8003 \| xargs kill` |
| Replica stuck as follower | Restart it: `docker compose restart replica1` |
