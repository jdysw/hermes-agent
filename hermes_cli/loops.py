"""Recurring in-session wakeups — the /loop command (Claude Code parity).

A loop stops when the agent ends a wakeup reply with ``LOOP_COMPLETE`` on its own line, when
``--times N`` ticks have fired, when the ``--until`` judge rules the condition met, or when the
``loops.max_ticks`` backstop pauses it. State lives in SessionDB ``state_meta`` (same contract as
``hermes_cli/goals.py``); CLI, gateway, and TUI all drive it through :class:`LoopManager`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field, fields, asdict
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# Floor for fixed intervals. Claude Code allows 30s; anything tighter is almost always an
# accident that burns tokens polling unchanged state. Config loops.min_interval_seconds (clamped ≥ 5).
DEFAULT_MIN_INTERVAL_SECONDS = 30
# Backstop tick budget so an unattended loop can't run forever. 0 = unlimited; config loops.max_ticks.
DEFAULT_MAX_TICKS = 100
# Self-paced mode: start at the floor, double while replies are unchanged, cap at the
# ceiling, snap back to the floor on any change.
DEFAULT_SELF_PACED_FLOOR_SECONDS = 60
DEFAULT_SELF_PACED_CEILING_SECONDS = 15 * 60

# Completion sentinel the wakeup prompt teaches the agent to emit.
LOOP_COMPLETE_MARKER = "LOOP_COMPLETE"
# Marker on its own line, tolerating surrounding whitespace / trailing punctuation.
_LOOP_COMPLETE_RE = re.compile(
    r"(?im)^\s*" + re.escape(LOOP_COMPLETE_MARKER) + r"\s*[.!]?\s*$"
)
# Interval token: 30s / 5m / 2h / 1h30m (compound units allowed, at least one).
_INTERVAL_TOKEN_RE = re.compile(
    r"^(?=\d)(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$", re.IGNORECASE
)


WAKEUP_PROMPT_TEMPLATE = (
    "[/loop wakeup #{tick}{cadence}]\n"
    "Recurring task: {prompt}\n\n"
    "This is an automatic wakeup from the /loop the user set. Perform the "
    "task now against the CURRENT state (re-check files, processes, or "
    "services fresh — do not assume anything from earlier iterations still "
    "holds). Report concisely what you found or did this iteration.\n"
    "If the task is now complete, no longer applicable, or the thing you "
    "were watching has finished, say so and end your reply with "
    f"{LOOP_COMPLETE_MARKER} on its own line — that stops the loop."
)

WAKEUP_PROMPT_WITH_UNTIL_TEMPLATE = (
    "[/loop wakeup #{tick}{cadence}]\n"
    "Recurring task: {prompt}\n\n"
    "Stop condition: {until}\n\n"
    "This is an automatic wakeup from the /loop the user set. Perform the "
    "task now against the CURRENT state (re-check files, processes, or "
    "services fresh — do not assume anything from earlier iterations still "
    "holds). Report concisely what you found or did this iteration, and "
    "show concrete evidence of the stop condition's status.\n"
    "If the stop condition is met, or the task is no longer applicable, say "
    f"so and end your reply with {LOOP_COMPLETE_MARKER} on its own line — "
    "that stops the loop."
)


def parse_interval_token(token: str) -> Optional[int]:
    """Total seconds for ``30s``/``5m``/``2h``/``1h30m``, else None.

    A bare number is NOT an interval (it collides with prompt text like ``/loop 3 things``).
    """
    m = _INTERVAL_TOKEN_RE.match(token.strip()) if token else None
    if not m:
        return None
    h, mnt, s = (int(g) if g else 0 for g in m.groups())
    total = h * 3600 + mnt * 60 + s
    return total if total > 0 else None


def parse_loop_args(text: str) -> Dict[str, Any]:
    """Parse ``/loop [interval] <prompt> [--times N] [--until ...]``.

    Returns ``{"interval_seconds": int|None, "prompt", "times", "until", "error"}``;
    ``interval_seconds`` None means self-paced, ``error`` is set for unusable input.
    """
    raw = (text or "").strip()
    result: Dict[str, Any] = {"interval_seconds": None, "prompt": "", "times": 0, "until": "", "error": None}
    if not raw:
        return {**result, "error": "empty"}

    # Pull trailing flags first so an interval-looking token inside the --until clause can't
    # confuse the front parse. --until consumes to end-of-line (or to a following --times).
    times, until = 0, ""
    m_times = re.search(r"\s--times\s+(\S+)", raw)
    if m_times:
        try:
            times = int(m_times.group(1))
            if times < 1:
                raise ValueError
        except ValueError:
            return {**result, "error": f"--times expects a positive integer, got {m_times.group(1)!r}"}
        raw = (raw[: m_times.start()] + raw[m_times.end():]).strip()

    m_until = re.search(r"\s--until\s+(.+)$", raw, re.DOTALL)
    if m_until:
        until = m_until.group(1).strip()
        raw = raw[: m_until.start()].strip()

    # Leading "every" sugar: /loop every 5m <prompt>
    tokens = raw.split(None, 1)
    if tokens and tokens[0].lower() == "every" and len(tokens) > 1:
        raw = tokens[1]
        tokens = raw.split(None, 1)

    interval = parse_interval_token(tokens[0]) if tokens else None
    if interval is not None:
        raw = tokens[1].strip() if len(tokens) > 1 else ""

    if not raw:
        return {**result, "error": "missing prompt (usage: /loop [interval] <prompt>)"}
    return {**result, "interval_seconds": interval, "prompt": raw, "times": times, "until": until}


def format_interval(seconds: float) -> str:
    """Render seconds as a compact human interval (``90`` → ``1m30s``)."""
    h, rem = divmod(int(max(0, round(seconds))), 3600)
    m, s = divmod(rem, 60)
    parts = [f"{h}h"] if h else []
    if m:
        parts.append(f"{m}m")
    if s or not parts:
        parts.append(f"{s}s")
    return "".join(parts)


def _loops_config() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        section = (load_config() or {}).get("loops") or {}
        return section if isinstance(section, dict) else {}
    except Exception:
        return {}


def _config_int(key: str, default: int, floor: int) -> int:
    """``loops.<key>`` as an int clamped to ``floor``; ``default`` on any bad value."""
    try:
        return max(floor, int(_loops_config().get(key, default)))
    except Exception:
        return default


def min_interval_seconds() -> int:
    return _config_int("min_interval_seconds", DEFAULT_MIN_INTERVAL_SECONDS, 5)


def max_ticks_default() -> int:
    return _config_int("max_ticks", DEFAULT_MAX_TICKS, 0)


def self_paced_floor_seconds() -> int:
    return _config_int("self_paced_floor_seconds", DEFAULT_SELF_PACED_FLOOR_SECONDS, 10)


def self_paced_ceiling_seconds() -> int:
    floor = self_paced_floor_seconds()
    return _config_int("self_paced_ceiling_seconds", max(floor, DEFAULT_SELF_PACED_CEILING_SECONDS), floor)


@dataclass
class LoopState:
    """Serializable /loop state stored per session."""

    prompt: str
    status: str = "active"            # active | paused | done | cleared
    mode: str = "interval"            # interval | self_paced
    interval_seconds: float = 0.0     # fixed cadence (mode == "interval")
    current_delay: float = 0.0        # live cadence (self-paced backoff)
    times: int = 0                    # user cap (--times N); 0 = none
    until: str = ""                   # judged stop condition; "" = none
    max_ticks: int = DEFAULT_MAX_TICKS  # config backstop; 0 = unlimited
    ticks_fired: int = 0
    created_at: float = 0.0
    last_fired_at: float = 0.0
    next_due_at: float = 0.0
    # True between "wakeup injected" and "that turn's response evaluated": stops a tick from
    # double-firing mid-turn and tells the post-turn hook the turn that just ended was ours.
    awaiting_response: bool = False
    last_response_digest: str = ""    # self-paced change detection
    paused_reason: Optional[str] = None
    last_stop_reason: Optional[str] = None
    # Gateway routing (platform / chat_id / chat_type / thread_id) captured at creation so the
    # idle watcher can inject ticks into the right chat. Empty for CLI/TUI (own schedulers).
    route: Dict[str, str] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "LoopState":
        data = json.loads(raw)
        route = data.get("route")
        kwargs: Dict[str, Any] = {
            "prompt": data.get("prompt", ""),
            "status": data.get("status", "active"),
            "mode": data.get("mode", "interval"),
            "paused_reason": data.get("paused_reason"),
            "last_stop_reason": data.get("last_stop_reason"),
            "route": route if isinstance(route, dict) else {},
        }
        # Remaining scalar fields: missing key -> dataclass default; present-but-falsy -> type zero.
        casts = {"str": str, "int": int, "float": float, "bool": bool}
        for f in fields(cls):
            if f.name not in kwargs:
                kwargs[f.name] = casts[f.type](data.get(f.name, f.default) or casts[f.type]())
        return cls(**kwargs)

    def cadence_label(self) -> str:
        if self.mode == "self_paced":
            live = f"，当前 {format_interval(self.current_delay)}" if self.current_delay else ""
            return f"自适应{live}"
        return f"每 {format_interval(self.interval_seconds)}"

    def remaining_label(self) -> str:
        if self.status != "active":
            return ""
        remaining = self.next_due_at - time.time()
        return "即将触发" if remaining <= 0 else f"{format_interval(remaining)}后"


_META_PREFIX = "loop:"


def _meta_key(session_id: str) -> str:
    return f"{_META_PREFIX}{session_id}"


def _get_session_db() -> Optional[Any]:
    """The goals module's cached SessionDB, so goals/loops/heartbeats share one connection and
    its off-loop bootstrap (a cold cache on the loop thread never runs ``SessionDB()`` inline).

    The previous copy here did, which froze the loop for the init duration and dropped the first ``loop:*``
    write (the /goal bug class, #88965).
    """
    try:
        from hermes_cli.goals import _get_session_db as _goals_db
    except Exception as exc:  # pragma: no cover
        logger.debug("LoopManager: SessionDB bootstrap failed (%s)", exc)
        return None
    return _goals_db()


def _db_op(label: str, fn, default=None):
    """Run one SessionDB call; any error is logged at debug and yields ``default``."""
    try:
        return fn()
    except Exception as exc:
        logger.debug("LoopManager: %s failed: %s", label, exc)
        return default


def _parse_state(raw: str, session_id: str = "") -> Optional[LoopState]:
    """``LoopState`` from stored JSON; None (warning when *session_id* given) on corrupt data."""
    try:
        return LoopState.from_json(raw)
    except Exception as exc:
        if session_id:
            logger.warning("LoopManager: could not parse stored loop for %s: %s", session_id, exc)
        return None


def load_loop(session_id: str) -> Optional[LoopState]:
    """Load the loop for a session, or None if none exists."""
    db = _get_session_db() if session_id else None
    if db is None:
        return None
    raw = _db_op("get_meta", lambda: db.get_meta(_meta_key(session_id)))
    return _parse_state(raw, session_id) if raw else None


def save_loop(session_id: str, state: LoopState) -> None:
    """Persist a loop to SessionDB. No-op if DB unavailable."""
    if not session_id:
        return
    db = _get_session_db()
    if db is None:
        from hermes_cli.goals import _warn_dropped_write

        _warn_dropped_write("LoopManager", "loop", session_id)
        return
    _db_op("set_meta", lambda: db.set_meta(_meta_key(session_id), state.to_json()))


def clear_loop(session_id: str) -> None:
    """Mark a loop cleared in the DB (preserved for audit, status=cleared)."""
    state = load_loop(session_id)
    if state is not None:
        state.status = "cleared"
        save_loop(session_id, state)


def list_active_loops() -> List[Tuple[str, LoopState]]:
    """``[(session_id, LoopState), ...]`` for every ACTIVE loop; ``[]`` on any DB error.

    Used by the gateway's idle wakeup watcher, which scans for due loops on a coarse tick.
    """
    db = _get_session_db()
    if db is None:
        return []
    out: List[Tuple[str, LoopState]] = []
    for key, raw in _db_op("list_meta_prefix", lambda: db.list_meta_prefix(_META_PREFIX), []):
        session_id = key[len(_META_PREFIX):]
        state = _parse_state(raw) if session_id and raw else None
        if state is not None and state.status == "active":
            out.append((session_id, state))
    return out


def store_has_active_loop(db: Any) -> bool:
    """True when *db* holds an ACTIVE ``loop:*`` row — or a row that cannot be parsed (unknown, so the
    caller keeps its full scan). Unlike :func:`list_active_loops` this takes the store explicitly and
    propagates read errors, so an idle gate can tell "empty" from "unavailable"."""
    for key, raw in db.list_meta_prefix(_META_PREFIX):
        state = _parse_state(raw, key[len(_META_PREFIX):]) if raw else None
        if state is None or state.status == "active":
            return True
    return False


def migrate_loop_to_session(old_session_id: str, new_session_id: str, *, reason: str = "") -> bool:
    """Carry a /loop from a parent session to its continuation. Best-effort, never raises.

    Context compression rotates ``session_id`` to a fresh child; without this the loop silently
    dies at the compaction boundary.

    Copies the loop onto the new session and archives the old row as ``cleared`` so exactly one active loop
    row exists per logical conversation. See #33618.
    """
    if not old_session_id or not new_session_id or old_session_id == new_session_id:
        return False
    try:
        state = load_loop(old_session_id)
        if state is None or state.status == "cleared" or load_loop(new_session_id) is not None:
            return False
        save_loop(new_session_id, state)
        clear_loop(old_session_id)
        logger.debug(
            "LoopManager: migrated loop %s -> %s (%s)",
            old_session_id, new_session_id, reason or "rotation",
        )
        return True
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("LoopManager: loop migration failed: %s", exc)
        return False


def _ticks_label(n: int) -> str:
    return f"{n} 次触发"


def _dash(reason: Optional[str]) -> str:
    return f"——{reason}" if reason else ""


def response_signals_complete(response: str) -> bool:
    """True when the agent ended its reply with the LOOP_COMPLETE marker."""
    return bool(response) and _LOOP_COMPLETE_RE.search(response) is not None


def _digest_response(response: str) -> str:
    """Digest for self-paced change detection; whitespace-normalized with clock/timestamp/duration
    tokens stripped so 'checked at 14:02:33' doesn't defeat the backoff."""
    text = (response or "").strip().lower()
    text = re.sub(r"\d{1,2}:\d{2}(:\d{2})?", "", text)
    text = re.sub(r"\d{4}-\d{2}-\d{2}", "", text)
    text = re.sub(r"\b\d+(\.\d+)?\s*(s|sec|secs|seconds|m|min|mins|minutes|h|hr|hrs|hours)\b", "", text)
    text = re.sub(r"\s+", " ", text)
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


class LoopManager:
    """Per-session /loop state + tick decisions.

    Drivers call ``set``/``pause``/``resume``/``clear`` for user controls, ``is_due()`` (cheap,
    in-memory), ``fire_tick()`` to claim a tick and get the wakeup message, ``complete_tick()`` to
    evaluate the finished turn, and ``status_line()``.
    """

    def __init__(self, session_id: str):
        self.session_id = session_id
        self._state: Optional[LoopState] = load_loop(session_id)

    @property
    def state(self) -> Optional[LoopState]:
        return self._state

    def refresh(self) -> None:
        """Re-read state from the DB (cross-process safety for the gateway)."""
        self._state = load_loop(self.session_id)

    def is_active(self) -> bool:
        return self._state is not None and self._state.status == "active"

    def has_loop(self) -> bool:
        return self._state is not None and self._state.status in {"active", "paused"}

    def _save(self) -> LoopState:
        save_loop(self.session_id, self._state)
        return self._state

    def status_line(self) -> str:
        s = self._state
        if s is None or s.status == "cleared":
            return "未设置循环。用 /loop [interval] <prompt> 启动一个。"
        fired = _ticks_label(s.ticks_fired)
        if s.times:
            caps = [f"已运行 {s.ticks_fired}/{s.times} 次"]
        elif s.max_ticks:
            caps = [f"已用 {s.ticks_fired}/{s.max_ticks} 次预算"]
        else:
            caps = [fired]
        if s.until:
            caps.append(f"直到：{s.until}")
        meta = f"{s.cadence_label()}, {', '.join(caps)}"
        if s.status == "active":
            remaining = s.remaining_label()
            tail = "，唤醒执行中" if s.awaiting_response else (f"，{remaining}" if remaining else "")
            return f"↻ 循环（进行中，{meta}{tail}）：{s.prompt}"
        if s.status == "paused":
            return f"⏸ 循环（已暂停，{meta}{_dash(s.paused_reason)}）：{s.prompt}"
        if s.status == "done":
            return f"✓ 循环已结束（{fired}{_dash(s.last_stop_reason)}）：{s.prompt}"
        return f"循环（{s.status}，{meta}）：{s.prompt}"

    def set(
        self,
        prompt: str,
        *,
        interval_seconds: Optional[int] = None,
        times: int = 0,
        until: str = "",
        route: Optional[Dict[str, str]] = None,
    ) -> LoopState:
        """Start a new loop (replaces any existing one for the session)."""
        prompt = (prompt or "").strip()
        if not prompt:
            raise ValueError("循环提示词为空")

        now = time.time()
        self_paced = interval_seconds is None
        interval = 0.0 if self_paced else float(max(int(interval_seconds), min_interval_seconds()))
        state = LoopState(
            prompt=prompt,
            mode="self_paced" if self_paced else "interval",
            interval_seconds=interval,
            current_delay=float(self_paced_floor_seconds()) if self_paced else interval,
            times=max(0, int(times or 0)),
            until=(until or "").strip(),
            max_ticks=max_ticks_default(),
            created_at=now,
            next_due_at=now,
            route=dict(route or {}),
        )
        self._state = state
        return self._save()

    def pause(self, reason: str = "用户暂停") -> Optional[LoopState]:
        s = self._state
        if not s or s.status not in {"active", "paused"}:
            return None
        s.status, s.paused_reason, s.awaiting_response = "paused", reason, False
        return self._save()

    def resume(self) -> Optional[LoopState]:
        s = self._state
        if not s or s.status == "cleared":
            return None
        s.status, s.paused_reason, s.awaiting_response = "active", None, False
        # Re-arm relative to now so a long pause doesn't fire instantly N times.
        delay = s.current_delay or s.interval_seconds or self_paced_floor_seconds()
        s.next_due_at = time.time() + min(delay, 5.0)
        return self._save()

    def clear(self) -> bool:
        if self._state is None or self._state.status == "cleared":
            return False
        self._state.status = "cleared"
        self._save()
        self._state = None
        return True

    def is_due(self, now: Optional[float] = None) -> bool:
        """Cheap check: active, not mid-wakeup, and the clock has passed."""
        s = self._state
        return (
            s is not None and s.status == "active" and not s.awaiting_response
            and (now if now is not None else time.time()) >= s.next_due_at
        )

    def fire_tick(self) -> Optional[str]:
        """Claim a due tick; returns the message to inject, or None.

        The message is the wakeup-framed prompt, or the raw command when the loop's prompt is
        itself a slash command (``/loop 10m /recap``). Marks ``awaiting_response`` so the tick
        can't double-fire; drivers MUST follow up with ``complete_tick`` (or ``abandon_tick``).
        """
        s = self._state
        if s is None or not self.is_due():
            return None
        s.ticks_fired += 1
        s.last_fired_at = time.time()
        s.awaiting_response = True
        # Provisional schedule from NOW: complete_tick reschedules from turn end, but if the
        # process dies mid-turn this keeps the persisted loop from being 'due' in a tight loop.
        s.next_due_at = s.last_fired_at + (s.current_delay or s.interval_seconds or self_paced_floor_seconds())
        self._save()

        if s.prompt.lstrip().startswith("/"):
            return s.prompt.strip()
        cadence = f", {s.cadence_label()}" if s.mode == "interval" else ", self-paced"
        template = WAKEUP_PROMPT_WITH_UNTIL_TEMPLATE if s.until else WAKEUP_PROMPT_TEMPLATE
        return template.format(tick=s.ticks_fired, cadence=cadence, prompt=s.prompt, until=s.until)

    def abandon_tick(self) -> None:
        """Roll back a fired tick whose injection failed (nothing ran)."""
        s = self._state
        if s is None or not s.awaiting_response:
            return
        s.awaiting_response = False
        s.ticks_fired = max(0, s.ticks_fired - 1)
        self._save()

    def _stop(self, status: str, reason: str, message: str) -> Dict[str, Any]:
        """Persist a terminal (``done``) or recoverable (``paused``) stop and build the result."""
        s = self._state
        s.status = status
        if status == "done":
            s.last_stop_reason = reason
        else:
            s.paused_reason = reason
        self._save()
        return {"status": status, "stopped": True, "reason": reason, "message": message}

    def complete_tick(self, last_response: str) -> Dict[str, Any]:
        """Evaluate the finished wakeup turn and schedule what's next.

        Returns ``{"status": "active|done|paused", "stopped": bool, "reason": str, "message": str}``;
        ``message`` is a user-visible one-liner, "" in the common still-looping case.
        """
        s = self._state
        if s is None or not s.awaiting_response:
            return {"status": s.status if s else None, "stopped": False, "reason": "no tick in flight", "message": ""}
        s.awaiting_response = False
        now = time.time()
        ticks = _ticks_label(s.ticks_fired)

        # 1. Agent self-stop marker.
        if response_signals_complete(last_response):
            return self._stop("done", "代理已发出完成信号",
                              f"✓ 循环已结束（运行 {ticks}）——任务完成。")

        # 2. Evidence-based --until judge (reuses the /goal judge; fail-open).
        if s.until and (last_response or "").strip():
            try:
                from hermes_cli.goals import judge_goal

                verdict, reason, _pf, _wait, _tf = judge_goal(s.until, last_response)
            except Exception as exc:
                verdict, reason = "continue", f"评判器不可用：{type(exc).__name__}"
            if verdict == "done":
                return self._stop("done", f"停止条件已满足：{reason}",
                                  f"✓ 循环已结束（运行 {ticks}）——{reason}")
            if verdict == "blocked":
                # Unachievable stop condition: pause so the user can re-scope, don't spin.
                why = f"停止条件被判定为无法达成：{reason}"
                return self._stop("paused", why,
                                  f"⏸ 循环已暂停——{why}。用 /loop resume 继续，/loop stop 结束。")

        # 3. --times user cap.
        if s.times and s.ticks_fired >= s.times:
            return self._stop("done", f"已完成要求的 {s.times} 次运行",
                              f"✓ 循环已结束——共运行 {s.times}/{s.times} 次。")

        # 4. Config backstop budget → pause (recoverable), not done.
        if s.max_ticks and s.ticks_fired >= s.max_ticks:
            return self._stop(
                "paused", f"唤醒预算已用尽（{s.ticks_fired}/{s.max_ticks}）",
                f"⏸ 循环已暂停——已用 {s.ticks_fired}/{s.max_ticks} 次"
                "（loops.max_ticks）。用 /loop resume 继续，/loop stop 结束。",
            )

        # 5. Still looping — schedule the next tick from turn end.
        if s.mode == "self_paced":
            digest = _digest_response(last_response)
            floor = self_paced_floor_seconds()
            if digest and digest == s.last_response_digest:
                s.current_delay = min(max(s.current_delay, floor) * 2, self_paced_ceiling_seconds())
            else:
                s.current_delay = float(floor)
            s.last_response_digest = digest
        else:
            s.current_delay = s.interval_seconds
        s.next_due_at = now + s.current_delay
        self._save()
        return {"status": "active", "stopped": False, "reason": "loop continues", "message": ""}


def goal_blocks_loop_tick(session_id: str) -> bool:
    """True when an ACTIVE, non-parked /goal should defer this session's /loop tick.

    Both features inject synthetic turns at idle boundaries; interleaving them would burn the
    goal's turn budget. Parked (waiting), paused, or done goals do NOT block the loop.
    """
    try:
        from hermes_cli.goals import GoalManager

        mgr = GoalManager(session_id=session_id)
        return mgr.is_active() and not mgr.is_waiting()
    except Exception:
        return False


LOOP_HELP = (
    "用法：/loop [interval] <prompt> [--times N] [--until <condition>]\n"
    "  /loop 5m 检查部署状态              —— 立即跑一次，之后每 5m\n"
    "  /loop every 10m /recap             —— 循环执行一条斜杠命令\n"
    "  /loop 一直修测试直到通过           —— 自适应节奏（输出没变化时自动放慢）\n"
    "  /loop 2m poll CI --times 30        —— 运行 30 次后停止\n"
    "  /loop 5m watch the queue --until queue is empty\n"
    "控制：/loop status · /loop pause · /loop resume · /loop stop\n"
    "当代理回复以下内容时，循环也会自行停止："
    f"{LOOP_COMPLETE_MARKER}."
)


def _pause_output(mgr: "LoopManager") -> str:
    state = mgr.pause(reason="用户暂停")
    return "未设置循环。" if state is None else f"⏸ 循环已暂停：{state.prompt}\n用 /loop resume 继续。"


def _resume_output(mgr: "LoopManager") -> str:
    state = mgr.resume()
    return "没有可恢复的循环。" if state is None else f"▶ 循环已恢复（{state.cadence_label()}）：{state.prompt}"


# Control words -> handler returning the output text. Anything else is a new loop spec.
_CONTROL_COMMANDS = {
    **dict.fromkeys(("", "status"), lambda mgr: mgr.status_line()),
    "pause": _pause_output,
    "resume": _resume_output,
    **dict.fromkeys(("stop", "clear", "cancel"), lambda mgr: "✓ 循环已停止。" if mgr.clear() else "没有进行中的循环。"),
    **dict.fromkeys(("help", "--help", "-h"), lambda mgr: LOOP_HELP),
}


def dispatch_loop_command(
    mgr: "LoopManager",
    args: str,
    *,
    route: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Surface-agnostic handler for ``/loop <args>`` → ``{"output": str, "created": bool}``.

    ``output`` is printed/sent verbatim by each surface. ``route`` is stored on new loops so the
    gateway's idle watcher can inject wakeups into the right chat; CLI/TUI pass None.
    """
    arg = (args or "").strip()
    control = _CONTROL_COMMANDS.get(arg.lower())
    if control is not None:
        return {"output": control(mgr), "created": False}

    parsed = parse_loop_args(arg)
    if parsed["error"]:
        if parsed["error"] == "empty":
            return {"output": "用法：/loop [interval] <prompt> —— 详见 /loop help。", "created": False}
        return {"output": f"/loop: {parsed['error']}", "created": False}

    replacing = mgr.has_loop()
    try:
        state = mgr.set(
            parsed["prompt"],
            interval_seconds=parsed["interval_seconds"],
            times=parsed["times"],
            until=parsed["until"],
            route=route,
        )
    except ValueError as exc:
        return {"output": f"/loop: {exc}", "created": False}

    lines = [f"↻ 循环已设置（{state.cadence_label()}）：{state.prompt}"]
    if replacing:
        lines.append("（已替换本会话先前的循环）")
    if parsed["interval_seconds"] is not None and parsed["interval_seconds"] < state.interval_seconds:
        lines.append(
            f"（间隔已上调至最小值 {format_interval(state.interval_seconds)} —— "
            "loops.min_interval_seconds）"
        )
    if state.mode == "self_paced":
        lines.append(
            f"自适应节奏：{format_interval(state.current_delay)}后首次检查；"
            f"内容无变化时最多放慢到 {format_interval(self_paced_ceiling_seconds())}。"
        )
    if state.times:
        lines.append(f"运行 {state.times} 次后停止。")
    if state.until:
        lines.append(f"停止条件：{state.until}")
    if not state.times and state.max_ticks:
        lines.append(f"兜底预算：{state.max_ticks} 次触发（loops.max_ticks；0 = 不限）。")
    first = "立即触发，之后按上述节奏" if state.status == "active" else state.remaining_label()
    lines.append(f"首次唤醒：{first}。控制：/loop status · pause · resume · stop。")
    return {"output": "\n".join(lines), "created": True}


__all__ = [
    "LoopState", "LoopManager", "parse_loop_args", "parse_interval_token", "format_interval",
    "response_signals_complete", "goal_blocks_loop_tick", "load_loop", "save_loop", "clear_loop",
    "list_active_loops", "migrate_loop_to_session", "dispatch_loop_command", "LOOP_COMPLETE_MARKER",
    "WAKEUP_PROMPT_TEMPLATE", "WAKEUP_PROMPT_WITH_UNTIL_TEMPLATE", "DEFAULT_MIN_INTERVAL_SECONDS",
    "DEFAULT_MAX_TICKS",
]
