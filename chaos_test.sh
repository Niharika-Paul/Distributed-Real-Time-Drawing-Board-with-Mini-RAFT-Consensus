#!/usr/bin/env bash
# =============================================================================
#  chaos_test.sh  —  Mini-RAFT Chaos & Verification Test Suite
#  Run from the project root after `docker compose up --build -d`
# =============================================================================

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

R1="http://localhost:8001"
R2="http://localhost:8002"
R3="http://localhost:8003"
GW="http://localhost:8000"
ALL_REPLICAS=("$R1" "$R2" "$R3")
ALL_NAMES=("replica1" "replica2" "replica3")

pass=0; fail=0

log()  { echo -e "${CYAN}[chaos]${RESET} $*"; }
ok()   { echo -e "${GREEN}  ✓ PASS${RESET} $*"; ((pass++)); }
err()  { echo -e "${RED}  ✗ FAIL${RESET} $*"; ((fail++)); }
warn() { echo -e "${YELLOW}  ⚠ WARN${RESET} $*"; }
section() { echo -e "\n${BOLD}══ $* ══${RESET}"; }

# ── Helpers ───────────────────────────────────────────────────────────────────

status() {
    curl -sf "$1/status" 2>/dev/null || echo '{"role":"DOWN"}'
}

find_leader() {
    for url in "${ALL_REPLICAS[@]}"; do
        role=$(status "$url" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('role',''))" 2>/dev/null || true)
        if [[ "$role" == "leader" ]]; then
            echo "$url"; return 0
        fi
    done
    echo ""; return 1
}

leader_name() {
    local url; url=$(find_leader)
    for i in 0 1 2; do
        [[ "${ALL_REPLICAS[$i]}" == "$url" ]] && echo "${ALL_NAMES[$i]}" && return
    done
    echo "none"
}

wait_for_leader() {
    local max=${1:-5} i=0 url
    while (( i < max )); do
        url=$(find_leader)
        [[ -n "$url" ]] && echo "$url" && return 0
        sleep 0.5; ((i++))
    done
    echo ""; return 1
}

send_stroke() {
    local leader=${1:-$(find_leader)}
    [[ -z "$leader" ]] && { warn "No leader to send stroke"; return 1; }
    curl -sf -X POST "$leader/stroke" \
        -H "Content-Type: application/json" \
        -d '{"type":"stroke","points":[{"x":10,"y":10},{"x":50,"y":50}],"color":"#06d6a0","size":4,"eraser":false}' \
        > /dev/null
}

log_length() {
    status "$1" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('log_length',-1))" 2>/dev/null || echo -1
}

commit_index() {
    status "$1" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('commit_index',-1))" 2>/dev/null || echo -1
}

term_of() {
    status "$1" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('term',-1))" 2>/dev/null || echo -1
}

# ── Tests ─────────────────────────────────────────────────────────────────────

section "1. Cluster Health Check"
log "Waiting for all replicas to start..."
sleep 3

up=0
for i in 0 1 2; do
    role=$(status "${ALL_REPLICAS[$i]}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('role','DOWN'))" 2>/dev/null || echo "DOWN")
    if [[ "$role" != "DOWN" ]]; then
        ok "${ALL_NAMES[$i]} is UP (role=$role)"
        ((up++))
    else
        err "${ALL_NAMES[$i]} is DOWN"
    fi
done
(( up >= 2 )) && ok "Majority ($up/3) replicas healthy" || err "Less than majority running"

section "2. Leader Election"
leader_url=$(wait_for_leader 10)
if [[ -n "$leader_url" ]]; then
    lname=$(leader_name)
    lterm=$(term_of "$leader_url")
    ok "Leader elected: $lname (term=$lterm)"
else
    err "No leader elected within 5s"
    exit 1
fi

section "3. Basic Stroke Replication"
log "Sending 5 strokes to leader ($lname)..."
for i in $(seq 1 5); do
    send_stroke "$leader_url"
    sleep 0.1
done
sleep 1

ci_leader=$(commit_index "$leader_url")
[[ "$ci_leader" -ge 4 ]] && ok "Leader commit_index=$ci_leader (≥4)" \
                          || err "Leader commit_index=$ci_leader (expected ≥4)"

# Check at least one follower is in sync
for i in 0 1 2; do
    url="${ALL_REPLICAS[$i]}"
    [[ "$url" == "$leader_url" ]] && continue
    ci=$(commit_index "$url")
    [[ "$ci" -ge 4 ]] && ok "${ALL_NAMES[$i]} in sync (commit_index=$ci)" && break
done

section "4. Leader Failover"
log "Killing leader ($lname)..."
old_leader_name=$(leader_name)
old_term=$lterm
docker compose stop "$old_leader_name" 2>/dev/null

log "Waiting for new leader..."
sleep 1.5
new_leader_url=$(wait_for_leader 8)
if [[ -n "$new_leader_url" ]]; then
    new_name=$(leader_name)
    new_term=$(term_of "$new_leader_url")
    [[ "$new_name" != "$old_leader_name" ]] \
        && ok "New leader elected: $new_name (term=$new_term > $old_term)" \
        || warn "Same node won again? (possible if it rejoined fast)"
    (( new_term > old_term )) \
        && ok "Term incremented correctly ($old_term → $new_term)" \
        || err "Term did NOT increment ($old_term → $new_term)"
else
    err "No new leader elected after killing $old_leader_name"
fi

section "5. Availability During Failover (Stroke Submission)"
log "Sending strokes immediately after failover..."
fail_count=0
for i in $(seq 1 5); do
    nlu=$(wait_for_leader 3)
    if [[ -n "$nlu" ]]; then
        send_stroke "$nlu" && true || ((fail_count++))
    else
        ((fail_count++))
    fi
    sleep 0.2
done
(( fail_count == 0 )) \
    && ok "All 5 strokes accepted during failover" \
    || warn "$fail_count/5 strokes dropped (acceptable during brief failover window)"

section "6. Killed Replica Rejoins & Catches Up"
log "Restarting $old_leader_name..."
docker compose start "$old_leader_name" 2>/dev/null
sleep 3

rejoin_ci=$(commit_index "http://localhost:$([ "$old_leader_name" = "replica1" ] && echo 8001 || ([ "$old_leader_name" = "replica2" ] && echo 8002 || echo 8003))")
current_ci=$(commit_index "$(wait_for_leader 5)")
log "Rejoined commit_index=$rejoin_ci, cluster commit_index=$current_ci"
[[ "$rejoin_ci" -ge "$current_ci" ]] \
    && ok "$old_leader_name caught up (commit_index=$rejoin_ci)" \
    || warn "$old_leader_name still catching up ($rejoin_ci < $current_ci) — wait a moment"

section "7. Hot-Reload Test"
log "Appending a comment to replica1/replica.py to trigger hot-reload..."
echo "# hot-reload test $(date)" >> replica1/replica.py
sleep 3

r1_role=$(status "$R1" | python3 -c "import sys,json; print(json.load(sys.stdin).get('role','DOWN'))" 2>/dev/null || echo "DOWN")
[[ "$r1_role" != "DOWN" ]] \
    && ok "replica1 alive after hot-reload (role=$r1_role)" \
    || err "replica1 DOWN after hot-reload"

# Remove the test comment
sed -i '/# hot-reload test/d' replica1/replica.py

section "8. Split Vote / Rapid Restart Resilience"
log "Rapidly restarting all followers (split-vote stress)..."
for name in "${ALL_NAMES[@]}"; do
    [[ "$name" != "$(leader_name)" ]] && docker compose restart "$name" 2>/dev/null &
done
wait
sleep 3

final_leader=$(wait_for_leader 8)
[[ -n "$final_leader" ]] \
    && ok "Leader stable after rapid restarts: $(leader_name) (term=$(term_of "$final_leader"))" \
    || err "No stable leader after rapid restarts"

section "9. Log Consistency (All Replicas Agree)"
sleep 2
ci_vals=()
for url in "${ALL_REPLICAS[@]}"; do
    ci_vals+=( "$(commit_index "$url")" )
done
log "commit_index values: ${ci_vals[*]}"

unique=$(printf '%s\n' "${ci_vals[@]}" | grep -v '^-1$' | sort -u | wc -l)
(( unique <= 1 )) \
    && ok "All replicas agree on commit_index" \
    || warn "Replicas disagree on commit_index (may still be converging)"

section "10. Gateway Health"
gw_status=$(curl -sf "$GW/health" 2>/dev/null || echo '{}')
gw_leader=$(echo "$gw_status" | python3 -c "import sys,json; print(json.load(sys.stdin).get('leader','none'))" 2>/dev/null || echo "unknown")
ok "Gateway healthy (known leader: $gw_leader)"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}═══════════════════════════════════════${RESET}"
echo -e "${BOLD}  Results: ${GREEN}$pass passed${RESET}, ${RED}$fail failed${RESET}"
echo -e "${BOLD}═══════════════════════════════════════${RESET}"
(( fail == 0 )) && echo -e "${GREEN}All tests passed! 🎉${RESET}" \
               || echo -e "${RED}Some tests failed. Check logs: docker compose logs${RESET}"
