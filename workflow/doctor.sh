#!/usr/bin/env sh
# Doctor probe for winter-service-docker.
#
# Emits NDJSON to stdout per the contract documented in
# context/provider-contract.md#doctor-probe. One object per line:
#   {"name": "...", "status": "pass|warn|fail", "message"?: "...", "remediation"?: "..."}
#
# Two checks:
#   1. docker daemon reachable  (`docker info`)
#   2. compose v2 present       (`docker compose version`)
#
# Each probe emits its own NDJSON; the script exits 0 at the end so per-probe
# statuses surface individually in `winter doctor` output.
# A non-zero exit would be collapsed into a single synthetic fail.
set -u

json_escape() {
  # Minimal JSON-string escaping via sed (POSIX portable).
  printf '%s' "$1" \
    | sed 's/\\/\\\\/g; s/"/\\"/g; s/	/\\t/g' \
    | tr -d '\r' \
    | awk '{printf "%s\\n", $0}' \
    | sed 's/\\n$//'
}

emit() {
  # Usage: emit <name> <status> [<message> [<remediation>]]
  local _name _status _message _remediation
  _name=$(json_escape "$1")
  _status="$2"
  _message="${3:-}"
  _remediation="${4:-}"
  if [ -n "$_remediation" ]; then
    _message_esc=$(json_escape "$_message")
    _remediation_esc=$(json_escape "$_remediation")
    printf '{"name":"%s","status":"%s","message":"%s","remediation":"%s"}\n' \
      "$_name" "$_status" "$_message_esc" "$_remediation_esc"
  elif [ -n "$_message" ]; then
    _message_esc=$(json_escape "$_message")
    printf '{"name":"%s","status":"%s","message":"%s"}\n' \
      "$_name" "$_status" "$_message_esc"
  else
    printf '{"name":"%s","status":"%s"}\n' "$_name" "$_status"
  fi
}

# ---- Probe 1: docker daemon reachable ----------------------------------------

if docker_info=$(docker info 2>&1); then
  emit "docker daemon" pass "docker daemon is reachable"
else
  emit "docker daemon" fail \
    "docker daemon is not reachable: $(echo "$docker_info" | head -1)" \
    "Start the docker daemon, or check permissions on /var/run/docker.sock (e.g. add your user to the docker group: sudo usermod -aG docker \$USER)."
fi

# ---- Probe 2: compose v2 present ---------------------------------------------

if compose_ver=$(docker compose version 2>&1); then
  emit "docker compose v2" pass "$compose_ver"
else
  emit "docker compose v2" fail \
    "docker compose v2 not available: $(echo "$compose_ver" | head -1)" \
    "Install the docker compose v2 plugin (e.g. 'sudo apt-get install docker-compose-plugin' or install Docker Desktop)."
fi

exit 0
