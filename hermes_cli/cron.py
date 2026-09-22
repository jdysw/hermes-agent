"""Cron subcommand for hermes CLI."""

import contextlib
import json
import re
import sys
import unicodedata
from datetime import timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

from hermes_cli.colors import Colors, color


def _normalize_skills(single_skill=None, skills: Optional[Iterable[str]] = None) -> Optional[List[str]]:
    """Deduped, stripped skill names; None when neither argument was given."""
    if skills is None and single_skill is None:
        return None
    normalized: List[str] = []
    for item in list(skills) if skills is not None else [single_skill]:
        text = str(item or "").strip()
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def _cron_api(**kwargs):
    from tools.cronjob_tools import cronjob as cronjob_tool
    return json.loads(cronjob_tool(**kwargs))


def _active_cron_provider_name() -> str:
    """Resolved cron scheduler provider name ('builtin', 'chronos', …); 'builtin' on failure."""
    try:
        from cron.scheduler_provider import resolve_cron_scheduler
        return resolve_cron_scheduler().name or "builtin"
    except Exception:
        return "builtin"


def _builtin_gateway_liveness() -> Optional[bool]:
    """Tri-state scheduler readiness (None = probe failed).

    Local gateways use process liveness; served satellites also require their own fresh heartbeat. External providers use their own machinery and are exempt.
    """
    try:
        if _active_cron_provider_name() != "builtin":
            return True
        # The runtime lock is held for exactly the gateway's lifetime — more reliable than PID
        # scanning (find_gateway_pids transiently misses the gateway right after a restart, and
        # inside the gateway it must never say "not running"). A crashing probe is "unknown".
        with contextlib.suppress(Exception):
            from gateway.status import is_gateway_runtime_lock_active
            if is_gateway_runtime_lock_active():
                return True
        from hermes_cli.gateway import (
            find_gateway_pids, named_profile_served_by_running_multiplexer)
        if find_gateway_pids():
            return True
        if not named_profile_served_by_running_multiplexer():
            return False
        # List/create and status require a fresh heartbeat from the satellite's own store.
        from cron.jobs import get_ticker_heartbeat_age
        return _ticker_age_is_fresh(get_ticker_heartbeat_age())
    except Exception:
        return None


def _warn_if_gateway_not_running() -> None:
    """Warn at create/list time when the scheduler is not ready; stay silent on an unknown probe result."""
    if _builtin_gateway_liveness() is not False:
        return
    print(color("  ⚠  调度器尚未就绪：没有运行中的网关，或没有新鲜的 profile 心跳。", Colors.YELLOW))
    print(color("     如果网关未运行：hermes gateway install\n"
                "                    sudo hermes gateway install --system  # Linux 服务器\n"
                "     查看状态：hermes cron status", Colors.DIM))


def _format_lateness(seconds: float) -> str:
    """Render a lateness duration compactly: '31m', '2h 30m', '45s'."""
    try:
        seconds = max(0, int(seconds))
    except (TypeError, ValueError):
        return "?"
    if seconds < 60:
        return f"{seconds}s"
    minutes, _ = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    parts = [(days, "d"), (hours, "h"), (minutes if not days else 0, "m")]
    return " ".join(f"{n}{unit}" for n, unit in parts if n) or "0m"


def _dispatch_kind_label(kind) -> Optional[str]:
    return {"catch_up": "catch-up after missed fire", "late": "late"}.get(kind)


def _next_run_overdue_seconds(next_run_at: Any) -> Optional[float]:
    """Seconds the stored ``next_run_at`` is already in the past (negative while still
    upcoming); None when it is not a parseable ISO timestamp.

    Parses through the scheduler's own ``_parse_aware`` so the CLI and the ticker agree on
    the instant (mixed UTC offsets, DST folds, legacy naive stamps read as system-local).
    """
    from cron.jobs import _parse_aware
    from hermes_time import now
    dt = _parse_aware(next_run_at)
    if dt is None:
        return None
    # Same-tzinfo subtraction is wall-clock arithmetic in Python; compare instants.
    return (now().astimezone(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds()


def _next_run_row(job: Dict[str, Any]) -> tuple[str, str]:
    """``("Next run" | "Overdue", value)`` for one job.

    A stamp parked past `cron doctor`'s grace on a job that is supposed to fire is the only
    user-visible trace of a dead scheduler; never present it as an upcoming run (#114309).
    """
    stamp = job.get("next_run_at", "?")
    overdue_s = _next_run_overdue_seconds(stamp)
    if (overdue_s is None or overdue_s <= _OVERDUE_GRACE_SECONDS
            or not job.get("enabled", True) or job.get("state") in {"paused", "completed"}):
        return ("Next run", stamp)
    return ("Overdue", color(f"{stamp}  ({_format_lateness(overdue_s)} ago — the job has not fired; "
                                   "is the scheduler running?)", Colors.YELLOW))


def _dispatch_display(dispatch: dict) -> Optional[str]:
    """One-line scheduled-vs-actual dispatch summary; None when the stamp is malformed.

    On-time dispatches render dim; late/catch-up dispatches render loudly so a run fired long
    after gateway downtime doesn't look like an ordinary success.

    See #99879.
    """
    if not isinstance(dispatch, dict):
        return None
    scheduled, actual, kind = (dispatch.get(k) for k in ("scheduled_at", "dispatched_at", "kind"))
    if not scheduled or not actual or not kind:
        return None
    lateness = _format_lateness(dispatch.get("lateness_seconds", 0))
    if kind == "on_time":
        return color(f"准时（计划 {scheduled}）", Colors.DIM)
    label = _dispatch_kind_label(kind) or "late"
    return (color(f"⚠ {label}：", Colors.YELLOW) + f"计划 {scheduled}，实际执行 {actual} "
            + color(f"（延迟 {lateness}）", Colors.YELLOW))


def _display_width(text: str) -> int:
    """Terminal display width: CJK wide/fullwidth chars count as 2 cells."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad_display(text: str, width: int) -> str:
    """Left-align ``text`` padded to ``width`` *display* cells (one CJK char = 2)."""
    return text + " " * max(0, width - _display_width(text))


def _print_banner(title: str) -> None:
    """Boxed cyan section header shared by ``cron list`` and ``cron incidents``."""
    print()
    rule = "─" * 73
    inner = " " * 25 + title
    for line in (f"┌{rule}┐", "│" + _pad_display(inner, 73) + "│", f"└{rule}┘"):
        print(color(line, Colors.CYAN))
    print()


def _unverified_targets(unverified) -> str:
    return ", ".join(map(str, unverified)) if isinstance(unverified, list) else str(unverified)


_STATE_BADGES = {"paused": ("[已暂停]", Colors.YELLOW), "completed": ("[已完成]", Colors.BLUE)}


def cron_list(show_all: bool = False):
    """List all scheduled jobs."""
    from cron.jobs import effective_job_state, list_jobs
    jobs = list_jobs(include_disabled=True)
    if not show_all:
        jobs = [
            job for job in jobs
            if job.get("enabled", True) or effective_job_state(job) == "paused"
        ]

    if not jobs:
        print(color("暂无定时任务。\n可用 'hermes cron create ...' 创建，"
                    "或在对话中使用 /cron 命令。", Colors.DIM))
        return

    _print_banner("定时任务")

    for job in jobs:
        # effective_job_state honours the scheduler flag — never [paused] when enabled=true.
        badge = _STATE_BADGES.get(effective_job_state(job)) or (
            ("[活跃]", Colors.GREEN) if job.get("enabled", True) else ("[已禁用]", Colors.RED))
        print(f"  {color(job.get('id', '?'), Colors.YELLOW)} {color(*badge)}")
        for label, value in _job_rows(job):
            print(f"    {_pad_display(label + '：', 11)}{value}")
        for line in _job_warnings(job):
            print(f"    {line}")
        print()

    _warn_if_gateway_not_running()


def _last_run_display(job: Dict[str, Any]) -> str:
    last_status = job["last_status"]
    if last_status == "ok":
        return color("ok", Colors.GREEN)
    if last_status == "delivery_queued":
        return color("finished; delivery is still in progress", Colors.YELLOW)
    if last_status == "delivery_failed":
        # Agent succeeded but the result never reached the user — not green; last_error is None.
        return color(f"ran, but the result was not delivered ({_short_reason(job.get('last_delivery_error'))}). "
                     f"{_delivery_fix_hint(job)}", Colors.YELLOW)
    display = color(f"{last_status}: {job.get('last_error', '?')}", Colors.RED)
    streak = int(job.get("failure_streak") or 0)
    if streak >= 2:
        display += color(f"  ({streak} 次）", Colors.RED)
    return display


def _job_rows(job: Dict[str, Any]) -> List[tuple[str, str]]:
    """``(label, value)`` detail rows for one job in ``cron list``."""
    # `repeat` / `deliver` may be present-but-null (dict-default only covers a missing key).
    repeat_info = job.get("repeat") or {}
    repeat_times = repeat_info.get("times")
    # `deliver` may be present-but-null in the job record (same pitfall as `repeat` above), so coalesce to
    # the default rather than relying on the dict-default, which only applies to a missing key. A null value
    # would otherwise reach `", ".join(None)` and crash the whole listing (#32896).
    deliver = job.get("deliver") or ["local"]
    skills = job.get("skills") or ([job["skill"]] if job.get("skill") else [])
    monitor_source = job.get("monitor_script") or job.get("monitor_url")
    mon_state = job.get("monitor_state") or {}
    latest_execution = job.get("latest_execution") or {}
    optional = [
        ("技能", ", ".join(skills) if skills else ""),
        ("脚本", job.get("script")),
        ("监控", f"{monitor_source}（仅在输出变化时运行 agent）" if monitor_source
         else ""),
        ("变更", mon_state.get("last_changed_at") if monitor_source else ""),
        ("模式", color("no-agent", Colors.DIM) + "（直接投递脚本 stdout）"
         if job.get("no_agent") else ""),
        ("工作目录", job.get("workdir")),
        ("上次运行", f"{job.get('last_run_at', '?')}  {_last_run_display(job)}"
         if job.get("last_status") else ""),
        ("调度", _dispatch_display(job.get("last_dispatch"))),
        ("执行", f"{latest_execution.get('status', '?')}  {latest_execution.get('id', '?')}"
         if latest_execution else "")]
    return [
        ("名称", job.get("name", "（未命名）")),
        ("计划", job.get("schedule_display", job.get("schedule", {}).get("value", "?"))),
        ("重复", f"{repeat_info.get('completed', 0)}/{repeat_times}" if repeat_times else "∞"),
        _next_run_row(job),
        ("投递", deliver if isinstance(deliver, str) else ", ".join(deliver)),
    ] + [(label, value) for label, value in optional if value]


def _short_reason(text: Any, limit: int = 120) -> str:
    """First line of an adapter/error blob, whitespace-collapsed and capped, or 'no details'."""
    first = str(text or "").strip().splitlines()
    reason = " ".join(first[0].split()) if first else ""
    return (reason[: limit - 1] + "…") if len(reason) > limit else (reason or "no details")


def _delivery_fix_hint(job: Dict[str, Any]) -> str:
    return (f"Check the target with `hermes cron status` or change it with "
            f"`hermes cron edit {job.get('id', '<id>')} --deliver <target>`.")


def _missed_fire_issue(job: Dict[str, Any], fire_err: Dict[str, Any]) -> str:
    return (f"missed scheduled fire at {fire_err.get('at', '?')}: {_short_reason(fire_err['detail'])}. "
            "The messaging gateway was unreachable. Run `hermes gateway restart`, then "
            f"`hermes cron run {job.get('id', '<id>')}` to run it now.")


def _job_warnings(job: Dict[str, Any]) -> List[str]:
    """Delivery / fire warning lines for one job in ``cron list``."""
    lines = []
    if queued := job.get("last_delivery_queued"):
        lines.append(f"Delivery still in progress (the result was handed off but not confirmed yet): {queued}")
    if job.get("last_delivery_error"):
        lines.append(f"{color('⚠ The result was not delivered:', Colors.YELLOW)} "
                     f"{_short_reason(job['last_delivery_error'])}. {_delivery_fix_hint(job)}")
    # A live adapter acked the last send but returned no message_id / raw_response
    # (Slack/Matrix/Mattermost shape): accepted as delivered, but say so here.
    if unverified := job.get("last_delivery_unverified"):
        lines.append(f"{color('⚠ 投递未验证：', Colors.YELLOW)} adapter acked "
                     f"{_unverified_targets(unverified)} without message_id/raw_response")
    fire_err = job.get("last_fire_error")
    if isinstance(fire_err, dict) and fire_err.get("detail"):
        lines.append(color(f"⚠ {_missed_fire_issue(job, fire_err)}", Colors.RED))
    return lines


def cron_tick():
    """Run due jobs once and exit."""
    from cron.scheduler import CronTickYielded, tick
    try:
        tick(verbose=True)
    except CronTickYielded as exc:
        # Inert for a one-shot CLI (no boot fingerprint); report cleanly rather than traceback.
        print(color(f"✗ {exc}", Colors.YELLOW))
        print("  较新的网关进程持有运行时锁，将触发到期任务；"
              "本陈旧进程已让出自己的 tick。")
        return 1
    except OSError as exc:
        # Real lock-acquisition failures (EMFILE, EACCES) propagate; they are not contention.
        # For the one-shot CLI surface, report cleanly instead of dumping a traceback; the gateway ticker
        # loop handles its own retry. See #87644.
        print(color(f"✗ 定时任务 tick 失败：{exc}", Colors.RED))
        print("  请检查 `hermes cron status` 及网关日志以了解详情。")
        return 1
    return 0


def cron_runs(job_id: Optional[str] = None, limit: int = 20):
    """Show indexed durable cron execution history."""
    from cron.executions import list_executions
    records = list_executions(job_id=job_id, limit=limit)
    if not records:
        print("未记录任何定时任务执行尝试。")
        return
    for record in records:
        print(f"{record.get('id', '?')}  {record.get('status', '?'):<9}  "
              f"任务={record.get('job_id', '?')}  来源={record.get('source', '?')}  "
              f"{record.get('claimed_at', '?')}")
        if record.get("error"):
            print(f"    {record['error']}")


_INCIDENT_STATE_COLORS = {"detected": Colors.RED, "alerted": Colors.YELLOW, "resolved": Colors.GREEN,
                          "closed": Colors.DIM}


def cron_incidents(args) -> int:
    """List (``[--state <s>]``) or ``ack <id>`` durable cron failure incidents.

    Acking closes an incident so its failure ping stays silent until the error signature changes.
    """
    from cron.incidents import ack_incident, list_incidents
    action = getattr(args, "incident_action", "list")
    if action == "ack":
        incident_id = getattr(args, "incident_id", None)
        if not incident_id:
            print(color("✗ 需要提供事件 ID：hermes cron incidents ack <incident_id>", Colors.RED))
            return 1
        if ack_incident(incident_id):
            print(color(f"✓ 事件 {incident_id} 已确认（关闭）。", Colors.GREEN))
        else:
            print(color(f"未找到事件 {incident_id}，或该事件已关闭。", Colors.YELLOW))
        return 0

    state = getattr(args, "state", None)
    incidents = list_incidents(state=state)
    if not incidents:
        print(color("未记录任何定时任务失败事件。", Colors.DIM))
        if state:
            print(color(f"  （按状态 '{state}' 过滤）", Colors.DIM))
        return 0

    _print_banner("定时任务失败事件")
    for inc in incidents:
        state_display = color(inc["state"], _INCIDENT_STATE_COLORS.get(inc["state"], Colors.DIM))
        error_text = re.sub(r"\s+", " ", inc.get("error") or "").strip()
        if len(error_text) > 160:
            error_text = error_text[:157].rstrip() + "……"
        rows = [("任务", inc["job_id"]), ("类型", inc.get("failure_type", "unknown")),
                ("首次出现", inc.get("first_seen_at", "?")),
                ("最近出现", inc.get("last_seen_at", "?")), ("错误", error_text),
                ("输出", inc.get("output_file"))]
        print(f"  {color(inc['id'], Colors.YELLOW)}  {state_display}")
        for label, value in rows:
            if label != "输出" or value:
                print(f"    {_pad_display(label + '：', 12)}{value}")
        print()
    print(color(f"  共 {len(incidents)} 个事件  |  确认命令：hermes cron incidents ack <id>",
                Colors.DIM))
    return 0


_PERMISSION_HINT = ("  提示：jobs.json 可能属于其他用户（例如被 root 通过 "
                    "`docker exec hermes hermes cron ...` 重写）。请将属主改为与网关用户一致，"
                    "并优先使用 `docker exec -u <uid>:<gid>`。")
_FD_EXHAUSTION_HINT = ("  提示：ticker 遇到文件描述符耗尽（EMFILE）。调度器现在会退避重试"
                       "并尝试回收 fd，但如果泄漏持续存在，请重启网关以恢复调度。")


def _ticker_age_is_fresh(age: Optional[float]) -> bool:
    from cron.jobs import TICKER_INTERVAL_SECONDS
    return age is not None and age <= TICKER_INTERVAL_SECONDS * 3 + 20


def _print_ticker_health(pids: list, restart_command: str = "hermes gateway restart") -> None:
    """Report builtin-ticker liveness for a gateway process known to be alive.

    The ticker THREAD can die silently or stay alive while every tick fails, so check both
    the liveness heartbeat and the last-successful-tick marker before saying "will fire".
    """
    # See #32612, #32895.
    from cron.jobs import (
        get_ticker_heartbeat_age, get_ticker_last_error, get_ticker_success_age)
    from cron.scheduler import _is_fd_exhaustion_text as _cron_is_fd_exhaustion_text
    from cron.scheduler import stale_code_yield_labels
    hb_age = get_ticker_heartbeat_age()
    ok_age = get_ticker_success_age()
    last_error = get_ticker_last_error()
    pid_line = f"  PID: {', '.join(map(str, pids))}" if pids else None

    def _warn(headline: str) -> None:
        print(color(headline, Colors.YELLOW))
        if pid_line:
            print(pid_line)

    if hb_age is None:
        # Ticker never started (non-cron profile, gateway just started, or a config issue).
        _warn("⚠ 网关正在运行，但定时任务 ticker 尚未上报心跳。")
        print("  在 ticker 写入首次心跳之前，定时任务不会触发。\n"
              "  如果网关刚启动，请等待约 60 秒后重新运行 `hermes cron status`。\n"
              f"  如果始终没有心跳，请重启：{restart_command}")
    elif not _ticker_age_is_fresh(hb_age):  # ticker thread is gone
        _warn("⚠ 网关正在运行，但定时任务 ticker 似乎已停滞 —— "
              f"已 {int(hb_age)}s 无心跳（预期约每 60s 一次）。")
        print(f"  定时任务可能未触发。请重启：{restart_command}")
    elif (skew := stale_code_yield_labels(last_error)) is not None:
        # `hermes update` moved the checkout under a running gateway: its ticker yields every
        # tick (heartbeat stays fresh, nothing dispatches) until the process is restarted (#117275).
        _warn("⚠ 网关正在运行过期的代码 —— 其定时任务 ticker 每次 tick 都会让出，"
              "不会触发任何任务。")
        print(color(f"  启动时的版本为 {skew[0]}，当前代码库已是 {skew[1]} "
                    "（代码在网关运行期间被更新）。", Colors.RED))
        print(f"  请重启以切到新代码：{restart_command}")
    elif (ok_age is not None and not _ticker_age_is_fresh(ok_age)) or (ok_age is None and last_error):
        # Loop alive but every tick fails (or has never succeeded since boot).
        _warn("⚠ 网关和定时任务 ticker 正在运行，但没有任何一次 tick "
              f"{'在 ' + str(int(ok_age)) + 's 内成功' if ok_age is not None else '成功过'} "
              "—— tick 可能在失败。")
        if last_error:
            # WHY ticks fail: root-rewritten jobs.json (PermissionError) or fd exhaustion.
            # Show WHY ticks fail — e.g. a root-rewritten jobs.json (PermissionError) that silently locked
            # out the ticker's uid for ~14h in the field (#68483), or fd exhaustion (EMFILE) that used to
            # stall the scheduler invisibly (#87644).
            print(color(f"  上次 tick 错误：{last_error}", Colors.RED))
            if "Permission denied" in last_error:
                print(color(_PERMISSION_HINT, Colors.YELLOW))
            elif _cron_is_fd_exhaustion_text(last_error):
                print(color(_FD_EXHAUSTION_HINT, Colors.YELLOW))
        print("  请检查网关日志中的 'Cron tick error'。")
    else:
        print(color("✓ 网关正在运行 —— 定时任务将自动触发", Colors.GREEN))
        if pid_line:
            print(pid_line)
        if hb_age is not None:
            print(f"  Ticker 心跳：{int(hb_age)}s 前")


def cron_status():
    """Show cron execution status."""
    from cron.jobs import list_jobs
    from hermes_cli.gateway import find_gateway_pids, named_profile_served_by_running_multiplexer
    from hermes_cli.profiles import get_active_profile_name
    print()

    provider = _active_cron_provider_name()
    if provider != "builtin":
        # External providers fire via webhook: no ticker thread / heartbeat file by design, so
        # the liveness heuristics would always say "stalled".
        print(color(f"✓ Cron provider: {provider} — jobs fire via the managed scheduler, "
                    "not the in-process ticker.", Colors.GREEN))
        print(color("  (No ticker heartbeat is expected for an external provider; "
                    "due jobs are delivered by an authenticated webhook.)", Colors.DIM))
    else:
        from gateway.host_topology import host_gateway_serving
        active = get_active_profile_name()
        # FIRST question under multiplex-only: is the HOST gateway alive and does it tick THIS
        # profile? Starting from find_gateway_pids() (argv `-p <name>`) made every served profile
        # report "not running" and told the user to start a SECOND host process.
        host = None
        with contextlib.suppress(Exception):
            host = host_gateway_serving(active)
        pids = [] if host is not None else find_gateway_pids()
        gateway_alive_via_lock = False
        served_by_multiplexer = False
        if host is None and not pids:
            # The pid scan transiently misses a live gateway right after a restart; the runtime
            # lock proves the process is alive. Declare "not running" only when both agree.
            with contextlib.suppress(Exception):
                # Same false-alarm class the cronjob tool fixed (#95947): the pid scan can transiently miss
                # a live gateway (just after a restart) while the runtime lock — held for exactly the
                # gateway's lifetime — proves the ticker's process is alive.
                from gateway.status import get_running_pid, is_gateway_runtime_lock_active
                gateway_alive_via_lock = is_gateway_runtime_lock_active()
                lock_pid = get_running_pid() if gateway_alive_via_lock else None
                pids = [lock_pid] if lock_pid else pids
            # Multiplexer identity does not establish the active profile's ticker health.
            if not gateway_alive_via_lock:
                served_by_multiplexer = named_profile_served_by_running_multiplexer()
        if host is not None:
            print(f"  Scheduler host: {host.describe()}")
            # `hermes gateway restart` exits 78 for a served NAMED profile
            # (_guard_named_profile_under_multiplexer): the one host process is the default's.
            _print_ticker_health([host.pid], restart_command="hermes --profile default gateway restart")
        elif pids or gateway_alive_via_lock or served_by_multiplexer:
            if served_by_multiplexer:
                print("  Scheduler host: the host gateway (multiplexing this profile)")
                _print_ticker_health([], restart_command="hermes --profile default gateway restart")
            else:
                _print_ticker_health(pids)
        else:
            print(color("✗ 本机上没有运行中的网关 —— 定时任务不会触发", Colors.RED))
            # When scheduling last worked before the host went away: without this, a
            # 7h-overdue job still reads as a normal upcoming "Next run" (#114309).
            with contextlib.suppress(Exception):
                from cron.jobs import TICKER_INTERVAL_SECONDS, get_ticker_heartbeat_age
                hb_age = get_ticker_heartbeat_age()
                if hb_age is not None and hb_age > TICKER_INTERVAL_SECONDS * 3 + 20:
                    print(color("  调度器上次 tick 是在 "
                                f"{_format_lateness(hb_age)} 之前 —— 此后到期的任务 "
                                "均未触发。", Colors.YELLOW))
            print("\n  启动这唯一的主机网关（它多路复用所有 profile，本 profile 也包含在内）：\n"
                  "    hermes --profile default gateway install   # 用户服务\n"
                  "    sudo hermes --profile default gateway install --system  # Linux 服务器：开机自启服务\n"
                  "    hermes --profile default gateway run       # 或在前台运行")
            if active not in ("default", "custom"):
                print("\n  它会自动服务本 profile。如果仍装有旧版本遗留的按 profile 服务或网关，\n"
                      "  请将其合并进来（预检 + 干跑）：\n"
                      "      hermes --profile default gateway migrate --multiplex --dry-run\n"
                      "      hermes --profile default gateway migrate --multiplex\n"
                      "  检查：在本 profile 下运行 hermes cron status 应能看到其 ticker 心跳。\n")

    print()
    _print_active_jobs_summary(list_jobs(include_disabled=False))
    print()


def _print_active_jobs_summary(jobs) -> None:
    """Print the '<N> active job(s)' + next-run line shared by every status path."""
    if not jobs:
        print("  无活跃任务")
        return
    from cron.jobs import _parse_aware

    # Stored stamps carry mixed UTC offsets (an interval job keeps last_run_at's offset, a cron
    # job its configured zone), so order by instant, never by ISO text; display the stored stamp.
    # `_parse_aware` hands back one shared ZoneInfo, and Python compares same-tzinfo datetimes
    # by wall clock (wrong across a DST fold) — normalise to UTC before ordering.
    next_runs = [(parsed.astimezone(timezone.utc), j["next_run_at"]) for j in jobs
                 if (parsed := _parse_aware(j.get("next_run_at"))) is not None]
    print(f"  {len(jobs)} 个活跃任务")
    if next_runs:
        earliest = min(next_runs, key=lambda run: run[0])[1]
        overdue_by = _next_run_overdue_seconds(earliest)
        if overdue_by is not None and overdue_by > _OVERDUE_GRACE_SECONDS:
            # #114309: a dead scheduler leaves next_run_at stranded in the past; presenting it
            # as an upcoming "Next run" hides the outage. Same 15m grace as `cron doctor`
            # (_OVERDUE_GRACE_SECONDS) so a job a few minutes behind the ticker's own
            # cadence doesn't flash OVERDUE here while doctor still calls it healthy.
            print(color(f"  ⚠ 下次运行 {earliest} 已逾期 —— 已过去 "
                        f"{_format_lateness(overdue_by)} 但任务并未触发 "
                        "（调度器是否在运行？）", Colors.YELLOW))
        else:
            print(f"  下次运行：{earliest}")
    # Post-downtime late fires show at status level, not just per-job in `cron list`.
    late = [j for j in jobs if isinstance(j.get("last_dispatch"), dict)
            and j["last_dispatch"].get("kind") in ("late", "catch_up")]
    if late:
        print()
        print(color(f"  ⚠ 有 {len(late)} 个任务上次为延迟触发（错过触发后的补跑）：",
                    Colors.YELLOW))
        for j in late:
            d = j["last_dispatch"]
            late_by = _format_lateness(d.get("lateness_seconds", 0))
            print(f"    {j.get('id', '?')}  {j.get('name', '（未命名）')}：{_dispatch_kind_label(d.get('kind'))}，"
                  f"计划 {d.get('scheduled_at', '?')}，实际执行 {d.get('dispatched_at', '?')} "
                  + color(f"（延迟 {late_by}）", Colors.YELLOW))


def _scripts_dir_for_cron() -> Path:
    """Scripts dir for cron jobs — via ``CRON_DIR`` so monkeypatched cron storage is honoured."""
    from cron.jobs import CRON_DIR
    return CRON_DIR.parent / "scripts"


def _script_health_issue(script: str) -> Optional[str]:
    """Human-readable script issue, or ``None`` when the path is OK."""
    scripts_dir = _scripts_dir_for_cron().resolve()
    raw = Path(script).expanduser()
    path = raw.resolve() if raw.is_absolute() else (scripts_dir / raw).resolve()
    try:
        path.relative_to(scripts_dir)
    except ValueError:
        return f"脚本解析路径超出 {scripts_dir}: {script!r}"
    if not path.exists():
        return f"script not found: {path}"
    if not path.is_file():
        return f"script path is not a file: {path}"
    return None


# A busy tick can push dispatch a few minutes late; only a next_run_at parked well in the past
# means the job is silently not firing (ticker dead, gateway down, wedged fire-claim).
# `cron status`'s OVERDUE line shares this grace so status, list, and doctor tell one
# consistent story about when a job counts as overdue.
_OVERDUE_GRACE_SECONDS = 15 * 60


def _next_run_overdue_issue(next_run: str) -> Optional[str]:
    """Issue string when ``next_run_at`` is parked in the past."""
    overdue_s = _next_run_overdue_seconds(next_run)
    if overdue_s is None:
        return f"next_run_at 不是有效时间戳：{next_run!r}"
    if overdue_s <= _OVERDUE_GRACE_SECONDS:
        return None
    amount = f"{overdue_s / 3600:.1f}h" if overdue_s >= 3600 else f"{overdue_s / 60:.0f}m"
    return f"next_run_at 已逾期 {amount} —— 任务未触发（调度器是否在运行？）"


def _cron_doctor_issues_for_job(job: Dict[str, Any]) -> List[str]:
    issues: List[str] = []
    last_status = str(job.get("last_status") or "").strip().lower()
    # "delivery_failed" = the agent run succeeded; the delivery issue below reports it.
    if last_status and last_status not in {"ok", "delivery_failed", "delivery_queued"}:
        issues.append(f"上次运行失败：{str(job.get('last_error') or 'unknown error').strip()}")
    if delivery_err := str(job.get("last_delivery_error") or "").strip():
        issues.append(f"last run finished but the result was not delivered ({_short_reason(delivery_err)}). "
                      f"{_delivery_fix_hint(job)}")
    if unverified := job.get("last_delivery_unverified"):
        issues.append("last delivery unverified (adapter acked without evidence): "
                      + _unverified_targets(unverified))
    # Dispatch records measure lateness, not whether the scheduler process was running.
    if isinstance(dispatch := job.get("last_dispatch"), dict):
        if label := _dispatch_kind_label(dispatch.get("kind")):
            issues.append(f"last fire was {label} (scheduled {dispatch.get('scheduled_at', '?')}, "
                          f"{_format_lateness(dispatch.get('lateness_seconds', 0))} late). "
                          "This warning clears at the next on-time fire.")
    if isinstance(fire_err := job.get("last_fire_error"), dict) and fire_err.get("detail"):
        # The handoff error survives next_run_at advancing beyond the failed dispatch.
        issues.append(_missed_fire_issue(job, fire_err))
    if job.get("enabled", True) and job.get("state") not in {"paused", "completed"}:
        next_run = str(job.get("next_run_at") or "").strip()
        issue = _next_run_overdue_issue(next_run) if next_run else "active job has no next_run_at"
        if issue:
            issues.append(issue)
    script = str(job.get("script") or "").strip()
    if job.get("no_agent") and not script:
        issues.append("no-agent job has no script")
    if script and (script_issue := _script_health_issue(script)):
        issues.append(script_issue)
    workdir = str(job.get("workdir") or "").strip()
    if workdir and not Path(workdir).expanduser().exists():
        issues.append(f"workdir not found: {workdir}")
    return issues


def cron_doctor() -> int:
    """Run read-only cron health checks and return a shell-friendly status."""
    from cron.jobs import list_jobs
    jobs = list_jobs(include_disabled=False)
    findings = [(job, issues) for job in jobs if (issues := _cron_doctor_issues_for_job(job))]
    if not findings:
        print(color("✓ cron doctor 未发现问题", Colors.GREEN))
        note = f"  已检查 {len(jobs)} 个活跃任务。" if jobs else "  未配置活跃任务。"
        print(color(note, Colors.DIM))
        return 0
    issue_count = sum(len(issues) for _, issues in findings)
    print(color(f"cron doctor 在 {len(findings)} 个任务中发现 {issue_count} 个问题：", Colors.YELLOW))
    print()
    for job, issues in findings:
        print(f"  {color(job.get('id', '?'), Colors.YELLOW)} {job.get('name', '（未命名）')}")
        for issue in issues:
            print(f"    - {issue}")
    print()
    print(color("下一步：修复上述任务配置，然后再次运行 `hermes cron doctor`。", Colors.DIM))
    return 1


_JOB_ARG_FIELDS = (("name", "name"), ("deliver", "deliver"), ("failure_deliver", "failure_deliver"),
                   ("repeat", "repeat"), ("script", "script"), ("workdir", "workdir"),
                   ("model", "model"), ("provider", "model_provider"), ("pinned", "pinned"),
                   ("monitor_script", "monitor_script"), ("monitor_url", "monitor_url"),
                   ("continuity", "continuity"), ("reasoning_effort", "reasoning_effort"))


def _job_api_kwargs(args) -> Dict[str, Any]:
    """Collect the create/update kwargs shared by ``cron create`` and ``cron edit``."""
    return {api_key: getattr(args, attr, None) for api_key, attr in _JOB_ARG_FIELDS}


_JOB_DETAIL_LINES = (
    ("script", "  脚本：{}"),
    ("monitor_script", "  监控：{}（仅在输出变化时运行 agent）"),
    ("monitor_url", "  监控：{}（仅在输出变化时运行 agent）"),
    ("no_agent", "  模式：no-agent（直接投递脚本 stdout）"),
    ("continuity", "  连续性：开启（每次运行都能看到上一次运行的输出）"),
    ("workdir", "  工作目录：{}"))


def _print_job_details(job_data: Dict[str, Any]) -> None:
    """Print the optional Script/Monitor/Mode/Continuity/Workdir lines of a job record."""
    for key, template in _JOB_DETAIL_LINES:
        if job_data.get(key):
            print(template.format(job_data[key]))


def cron_create(args):
    # The gateway-lifecycle guard lives in cron.jobs.create_job (every creation path); a block
    # surfaces as result["error"].
    result = _cron_api(
        action="create", schedule=args.schedule, prompt=args.prompt,
        skill=getattr(args, "skill", None),
        skills=_normalize_skills(getattr(args, "skill", None), getattr(args, "skills", None)),
        no_agent=getattr(args, "no_agent", False) or None,
        **({"paused": args.paused, "paused_reason": getattr(args, "paused_reason", None)}
           if getattr(args, "paused", False) or getattr(args, "paused_reason", None) is not None else {}),
        **_job_api_kwargs(args))
    if not result.get("success"):
        print(color(f"创建任务失败：{result.get('error', '未知错误')}", Colors.RED))
        return 1
    print(color(f"已创建任务：{result['job_id']}", Colors.GREEN))
    print(f"  名称：{result['name']}\n  计划：{result['schedule']}")
    if result.get("skills"):
        print(f"  技能：{', '.join(result['skills'])}")
    _print_job_details(result.get("job", {}))
    if not result.get("job", {}).get("enabled", True):
        print("  已创建为暂停状态 —— 恢复后才会按计划执行，或现在显式运行一次。")
    else:
        print(f"  下次运行：{result['next_run_at']}")
    _warn_if_gateway_not_running()
    return 0


def cron_edit(args):
    from cron.jobs import AmbiguousJobReference, resolve_job_ref
    try:
        job = resolve_job_ref(args.job_id)
    except AmbiguousJobReference as exc:
        print(color(str(exc), Colors.RED))
        for m in exc.matches:
            print(f"  {m['id']}  （名称：{m.get('name')!r}）")
        return 1
    if not job:
        print(color(f"未找到任务：{args.job_id}", Colors.RED))
        return 1
    existing_skills = list(job.get("skills") or ([job["skill"]] if job.get("skill") else []))
    replacement_skills = _normalize_skills(getattr(args, "skill", None), getattr(args, "skills", None))
    add_skills = _normalize_skills(None, getattr(args, "add_skills", None)) or []
    remove_skills = set(_normalize_skills(None, getattr(args, "remove_skills", None)) or [])

    final_skills = None
    if getattr(args, "clear_skills", False):
        final_skills = []
    elif replacement_skills is not None:
        final_skills = replacement_skills
    elif add_skills or remove_skills:
        final_skills = [skill for skill in existing_skills if skill not in remove_skills]
        final_skills += [skill for skill in add_skills if skill not in final_skills]
    result = _cron_api(action="update", job_id=args.job_id,
                       schedule=getattr(args, "schedule", None),
                       prompt=getattr(args, "prompt", None), skills=final_skills,
                       no_agent=getattr(args, "no_agent", None), **_job_api_kwargs(args))
    if not result.get("success"):
        print(color(f"更新任务失败：{result.get('error', '未知错误')}", Colors.RED))
        return 1
    updated = result["job"]
    print(color(f"已更新任务：{updated['job_id']}", Colors.GREEN))
    print(f"  名称：{updated['name']}\n  计划：{updated['schedule']}")
    print(f"  技能：{', '.join(updated['skills'])}" if updated.get("skills") else
          "  技能：无")
    _print_job_details(updated)
    return 0


_ACTION_LABELS = {"pause": "暂停", "resume": "恢复", "run": "触发", "remove": "删除"}


def _job_action(action: str, job_id: str, success_verb: str) -> int:
    _stateless_token = None
    if action == "run":
        # One-shot CLI: a background-dispatched run (daemon thread, triggered when the CLI
        # inherits HERMES_SESSION_KEY) would be orphaned mid-LLM-call, leaving the execution row
        # stuck 'claimed'. Declaring the channel stateless forces a synchronous run; scoped to
        # this call so in-process callers (tests, embedding apps) are not tainted.
        with contextlib.suppress(Exception):
            # The background path in ``_try_dispatch_background_run`` triggers when the CLI inherits a
            # gateway/desktop session env (HERMES_SESSION_KEY); declare the channel stateless so
            # ``async_delivery_supported()`` gates it off and the run executes synchronously to completion
            # instead. See #86721.
            from gateway.session_context import _SESSION_ASYNC_DELIVERY
            _stateless_token = _SESSION_ASYNC_DELIVERY.set(False)
    try:
        result = _cron_api(action=action, job_id=job_id)
    finally:
        if _stateless_token is not None:
            _SESSION_ASYNC_DELIVERY.reset(_stateless_token)
    if not result.get("success"):
        print(color(f"{_ACTION_LABELS.get(action, action)}任务失败：{result.get('error', '未知错误')}", Colors.RED))
        return 1
    job = result.get("job") or result.get("removed_job") or {}
    print(color(f"已{success_verb}任务：{job.get('name', job_id)}（{job_id}）", Colors.GREEN))
    if action in {"resume", "run"} and result.get("job", {}).get("next_run_at"):
        print(f"  下次运行：{result['job']['next_run_at']}")
    if action == "run":
        print(f"  {_run_outcome(result.get('job', {}))}")
    return 0


def _run_outcome(job: Dict[str, Any]) -> str:
    """One-line verdict for a manual run.

    A background-dispatched run (execution_mode="background" / delegation_id) keeps running
    after this CLI exits, so report the dispatch rather than a success/failure verdict.
    """
    if job.get("delegation_id"):
        return f"正在后台运行（委派 {job['delegation_id']}）。"
    if job.get("execution_mode") == "background":
        return "正在后台运行。"
    if job.get("executed"):
        return f"立即运行：{'成功' if job.get('execution_success') else '失败'}。"
    return job.get("execution_skipped") or "将在下一个调度器 tick 时运行。"


def cron_resume(args) -> int:
    """Resume a paused job or explicitly re-arm a completed one-shot."""
    run_at = getattr(args, "run_at", None)
    run_now = getattr(args, "run_now", False)
    if run_at and run_now:
        print(color("只能使用 --at 或 --run-now 其中之一。", Colors.RED))
        return 1
    if not run_at and not run_now:
        return _job_action("resume", args.job_id, "恢复")
    from cron.jobs import AmbiguousJobReference, _hermes_now, rearm_oneshot
    try:
        job = rearm_oneshot(args.job_id, _hermes_now().isoformat() if run_now else run_at)
    except (AmbiguousJobReference, ValueError) as exc:
        print(color(f"重新设置任务失败：{exc}", Colors.RED))
        return 1
    if not job:
        print(color(f"未找到任务：{args.job_id}", Colors.RED))
        return 1
    print(color(f"已重新设置任务：{job.get('name', args.job_id)}（{args.job_id}）", Colors.GREEN)
          + f"\n  下次运行：{job.get('next_run_at')}")
    return 0


def cron_notepad(args) -> int:
    """Handle ``hermes cron notepad <job_id> [get|set|delete|list]`` (per-job durable KV).

    A running cron agent updates its own notepad via its terminal tool; the scheduler injects
    non-empty notepads into the job prompt on each run.
    """
    from cron import notepad
    job_id = str(getattr(args, "job_id", "") or "")
    action = getattr(args, "notepad_action", None) or "list"
    key = getattr(args, "key", None)
    value = getattr(args, "value", None)
    if not job_id:
        print(color("需要提供任务 ID。", Colors.RED))
        return 1
    try:
        if action not in ("set", "get", "delete"):  # list (default)
            notes = notepad.list_notes(job_id)
            if not notes:
                print(color(f"任务 {job_id} 的记事本为空。", Colors.DIM))
            for note in notes:
                print(f"  {color(note['key'], Colors.YELLOW)} = {note['value']}\n"
                      f"    {color('更新时间：' + str(note['updated_at']), Colors.DIM)}")
            return 0
        usage_args = "set <key> <value>" if action == "set" else f"{action} <key>"
        if key is None or (action == "set" and value is None):
            print(color(f"用法：hermes cron notepad <job_id> {usage_args}", Colors.RED))
            return 1
        if action == "set":
            notepad.set_note(job_id, key, value)
            print(color(f"已为任务 {job_id} 设置记事本键 '{key}'。", Colors.GREEN))
            return 0
        if action == "get":
            stored = notepad.get_note(job_id, key)
            if stored is not None:
                print(stored)
                return 0
        elif notepad.delete_note(job_id, key):
            print(color(f"已删除任务 {job_id} 的记事本键 '{key}'。", Colors.GREEN))
            return 0
        print(color(f"任务 {job_id} 没有记事本键 '{key}'。", Colors.YELLOW))
        return 1
    except ValueError as exc:
        print(color(f"记事本错误：{exc}", Colors.RED))
        return 1


# Late-bound lambdas keep module-level monkeypatching working; list/status/runs return None -> 0.
_CRON_SUBCOMMANDS = {
    "list": lambda a: cron_list(getattr(a, "all", False)) or 0,
    "status": lambda a: cron_status() or 0,
    "doctor": lambda a: cron_doctor(),
    "tick": lambda a: cron_tick(),
    "runs": lambda a: cron_runs(getattr(a, "job_id", None), getattr(a, "limit", 20)) or 0,
    "incidents": lambda a: cron_incidents(a),
    "notepad": lambda a: cron_notepad(a),
    "create": lambda a: cron_create(a),
    "edit": lambda a: cron_edit(a),
    "pause": lambda a: _job_action("pause", a.job_id, "暂停"),
    "resume": lambda a: cron_resume(a),
    "run": lambda a: _job_action("run", a.job_id, "Triggered"),
    "remove": lambda a: _job_action("remove", a.job_id, "Removed"),
}
_CRON_SUBCOMMANDS["history"] = _CRON_SUBCOMMANDS["runs"]
_CRON_SUBCOMMANDS["add"] = _CRON_SUBCOMMANDS["create"]
_CRON_SUBCOMMANDS["rm"] = _CRON_SUBCOMMANDS["delete"] = _CRON_SUBCOMMANDS["remove"]


def cron_command(args):
    """Handle cron subcommands."""
    subcmd = getattr(args, 'cron_command', None)
    handler = _CRON_SUBCOMMANDS.get("list" if subcmd is None else subcmd)
    if handler is not None:
        return handler(args)
    print(f"未知的 cron 命令：{subcmd}\n"
          "Usage: hermes cron [list|create|edit|pause|resume|run|remove|status|runs|doctor|tick]")
    sys.exit(1)
