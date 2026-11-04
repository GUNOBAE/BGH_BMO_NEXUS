"""Pure recovery policy: no filesystem, commands, network, or real clock."""

from copy import deepcopy
from dataclasses import dataclass

LIMIT = 5
GRACE = 180
INTERVAL = 30
CONFIRMATIONS = 3
STABLE = 1800
REBOOT_DEADLINE = 180


@dataclass(frozen=True)
class Observation:
    boot: str
    uptime: float
    health: str  # healthy, dead, missing, zero_size, unsafe, unknown
    bmo_clear: bool = True


def initial():
    return dict(version=1, phase="DISARMED", attempts=0, total_attempts=0,
                batch=1, incident="", reason="", sample_boot="", sample_at=-1.0,
                bad=0, good_since=None, pending_boot="", pending_at=0.0,
                outbox=[], notify_boot="", notify_at=-1.0,
                notification_test="")


def enqueue(state, kind, message):
    # One entry per event. The caller persists it before performing any action.
    key = f"{state['incident']}:{state['batch']}:{kind}"
    if not any(item["id"] == key for item in state["outbox"]):
        state["outbox"].append(dict(id=key, message=message))


def block(state, reason, message):
    state["phase"] = "BLOCKED"
    state["reason"] = reason
    enqueue(state, reason, message)


def advance(previous, obs):
    """Return new state and intent. Caller MUST durably save before reboot."""
    s = deepcopy(previous)
    if s["phase"] in ("DISARMED", "BLOCKED"):
        return s, "none"
    if not obs.bmo_clear:
        block(s, "bmo_not_clear", "SSD recovery paused: BMO activity or process visibility is uncertain. No reboot was requested. Check locally before resuming.")
        return s, "none"
    if s["pending_boot"]:
        if s["pending_boot"] == obs.boot:
            if obs.uptime - s["pending_at"] >= REBOOT_DEADLINE:
                block(s, "reboot_not_completed", "The requested reboot did not complete. Automatic recovery is paused; inspect the server locally.")
            return s, "none"
        s["pending_boot"] = ""
        s["bad"] = 0
        s["good_since"] = None
    if obs.uptime < GRACE:
        return s, "none"
    if s["sample_boot"] != obs.boot:
        s.update(sample_boot=obs.boot, sample_at=-1.0, bad=0, good_since=None)
    if s["sample_at"] >= 0 and obs.uptime - s["sample_at"] < INTERVAL:
        return s, "none"
    if s["sample_at"] >= 0 and obs.uptime - s["sample_at"] > 120:
        # A long monitoring gap is not evidence of continuous health/failure.
        s.update(bad=0, good_since=None)
    s["sample_at"] = obs.uptime
    if obs.health == "healthy":
        s["bad"] = 0
        if s["phase"] == "RECOVERING":
            if s["good_since"] is None:
                s["good_since"] = obs.uptime
            if obs.uptime - s["good_since"] >= STABLE:
                enqueue(s, "recovered", "The SSD has remained healthy for 30 minutes. Recovery is complete. Server apps were not restarted; check storage and resume them manually.")
                s.update(phase="WATCHING", attempts=0, reason="", good_since=None)
        return s, "none"
    s["good_since"] = None
    if obs.health in ("unsafe", "unknown"):
        block(s, "storage_not_verified", "SSD recovery paused: storage identity, mount, or observation could not be verified. Server containers will be stopped; automatic reboot is disabled until inspection.")
        return s, "stop"
    if obs.health not in ("dead", "missing", "zero_size"):
        raise ValueError("unknown observation")
    s["bad"] += 1
    if s["bad"] < CONFIRMATIONS:
        return s, "none"
    s["phase"] = "RECOVERING"
    if s["attempts"] >= LIMIT:
        block(s, "retry_limit", "The SSD is still unavailable after 5 reboot attempts. Would you like me to try 5 more? Automatic recovery is paused. Approve another batch through your Tailscale SSH session using ssd-guard retry-five and the current incident ID.")
        return s, "stop"
    s["attempts"] += 1
    s["total_attempts"] += 1
    s.update(pending_boot=obs.boot, pending_at=obs.uptime, bad=0, reason=obs.health)
    return s, "reboot"


def approve_five(previous, incident):
    s = deepcopy(previous)
    if (s["phase"] not in ("BLOCKED", "DISARMED") or s["reason"] != "retry_limit"
            or s["attempts"] != LIMIT or not incident or s["incident"] != incident):
        raise ValueError("retry_not_applicable")
    s.update(phase="RECOVERING", attempts=0, batch=s["batch"] + 1,
             bad=0, good_since=None, pending_boot="", sample_at=-1.0, reason="")
    s["incident"] = f"{s['incident'].split(':', 1)[0]}:{s['batch']}"
    # A delayed old failure alert must not ask for approval again after approval.
    s["outbox"] = [e for e in s["outbox"] if not e["id"].endswith(":retry_limit")]
    return s
