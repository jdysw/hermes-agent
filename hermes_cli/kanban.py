"""``hermes kanban …`` — dispatch (``kanban_command``), task-verb handlers, ``run_slash`` for ``/kanban``.
DB work lives in ``kanban_db``; siblings: ``kanban_parser`` (argparse, re-exported ``build_parser``),
``kanban_output`` (text/--json), ``kanban_boards`` (``boards …``), ``kanban_ops`` (dispatch/daemon/
tail/watch/gc/repair).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shlex
import sys
import time
from pathlib import Path
from typing import Optional

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli import kanban_db_notify as kbn
from hermes_cli import kanban_swarm as ks
from hermes_cli.kanban_output import (
    _ATTACHMENT_FIELDS, _RUNS_RUN_FIELDS, _SHOW_RUN_FIELDS, _bulk_apply, _err,
    _fmt_counts, _fmt_task_line, _fmt_ts, _json_out, _obj_dict, _print_json,
    _task_to_dict,
)
from hermes_cli.kanban_boards import _dispatch_boards
from hermes_cli.kanban_ops import (
    _cmd_daemon, _kanban_config, _cmd_dispatch, _cmd_gc, _cmd_repair, _cmd_tail, _cmd_watch,
)
from hermes_cli.kanban_parser import build_parser  # noqa: F401  (re-exported: hermes_cli.main, run_slash)


# --- Flag parsing helpers ---

def _none_profile(value: str) -> Optional[str]:
    """``none`` / ``-`` / ``null`` mean "unassign"."""
    return None if value.lower() in {"none", "-", "null"} else value


def _parse_metadata_flag(raw: Optional[str]) -> tuple[Optional[dict], int]:
    """Parse ``--metadata`` JSON; returns ``(dict|None, rc)`` with rc=2 on error."""
    if not raw:
        return None, 0
    try:
        metadata = json.loads(raw)
        if not isinstance(metadata, dict):
            raise ValueError("必须是 JSON 对象")
    except (ValueError, json.JSONDecodeError) as exc:
        return None, _err(f"kanban: --metadata：{exc}", 2)
    return metadata, 0


def _run_state_kwargs(args: argparse.Namespace, cmd: str) -> tuple[Optional[dict[str, str]], int]:
    """``--state-type``/``--state-name`` must be given together: ``(kwargs, 0)`` or ``(None, 2)``."""
    st = getattr(args, "state_type", None)
    sn = getattr(args, "state_name", None)
    if (st is None) != (sn is None):
        return None, _err(f"kanban {cmd}: 请同时传入 --state-type 和 --state-name，或都省略", 2)
    return ({} if st is None else {"state_type": st, "state_name": sn}), 0


def _parse_workspace_flag(value: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """``--workspace`` -> ``(kind, path|None)``: ``scratch``, ``worktree``, ``worktree:<p>``, ``dir:<p>``.
    Omitted -> ``(None, None)`` so ``create_task`` can tell "default" from an explicit scratch."""
    if not value:
        return (None, None)
    v = value.strip()
    if v in {"scratch", "worktree"}:
        return (v, None)
    for prefix, kind in (("dir:", "dir"), ("worktree:", "worktree")):
        if not v.startswith(prefix):
            continue
        path = v[len(prefix):].strip()
        if not path:
            raise argparse.ArgumentTypeError(f"--workspace {prefix} 需要在冒号后跟一个路径")
        return (kind, os.path.expanduser(path))
    raise argparse.ArgumentTypeError(f"未知的 --workspace 取值 {value!r}：请使用 scratch、worktree、"
                                     "worktree:<path> 或 dir:<path>")


def _parse_branch_flag(value: Optional[str]) -> Optional[str]:
    """Normalize an optional branch name from ``kanban create --branch``."""
    if value is None:
        return None
    branch = value.strip()
    if not branch:
        raise argparse.ArgumentTypeError("--branch 需要非空名称")
    if branch.startswith("-"):
        raise argparse.ArgumentTypeError("--branch 不能以 '-' 开头")
    if any(ch.isspace() for ch in branch):
        raise argparse.ArgumentTypeError("--branch 不能包含空白字符")
    return branch


def _check_dispatcher_presence(hermes_home: Optional[Path] = None) -> tuple[bool, str]:
    """``(running, message)`` for the "will anything dispatch this?" warning: True when a gateway is
    alive for this HERMES_HOME with ``kanban.dispatch_in_gateway`` on, else False + human guidance.
    Fails OPEN (probe/config errors -> ``(True, "")``) — a missed warning beats crying wolf.
    ``hermes_home`` scopes the probe to a profile dir (dashboard backend); CLI callers pass None.

    The dashboard plugin API passes it because the dashboard backend process can be running under a
    different HERMES_HOME than the profile the request targets, which otherwise produced a "no gateway is
    running" warning against a perfectly healthy profile gateway (#71211). CLI callers leave it ``None`` and
    keep the existing process-level behavior.
    """
    try:
        from gateway.status import resolve_gateway_liveness  # type: ignore

        # Same ladder as the dashboard status endpoints so PID-file-less / cross-container gateways
        # aren't misreported; use_cache=False because this one-shot probe must see the state now.
        liveness = resolve_gateway_liveness(profile_dir=hermes_home, use_cache=False)
    except Exception:
        return (True, "")  # can't probe — silent
    if liveness.probe_error:  # resolver swallows per-rung failures; "can't tell" != "no gateway"
        return (True, "")
    pid = liveness.pid
    # Even if the gateway is up, dispatch_in_gateway may be off (can't tell -> assume default).
    if pid and bool(_kanban_config().get("dispatch_in_gateway", True)):
        return (True, f"gateway pid={pid}, dispatch enabled")
    if pid:
        return (False, "网关正在运行，但 config.yaml 中 kanban.dispatch_in_gateway=false"
                "——任务会一直停留在 'ready'，直到你把它重新打开并重启网关，"
                "或者运行旧版独立守护进程（`hermes kanban daemon --force`）。")
    return (False, "没有网关在运行——任务会一直停留在 'ready'，直到你启动网关。运行：\n"
            "    hermes gateway start\n"
            "网关内置了调度器（默认触发间隔 60s）；网关启动后，"
            "你的任务会在下一次触发时被接管。")


# --- Command dispatch ---

def kanban_command(args: argparse.Namespace) -> int:
    """Entry point from ``hermes kanban …``; returns a shell-style exit code."""
    action = getattr(args, "kanban_action", None)
    if not action:
        parser = getattr(args, "_kanban_parser", None)
        if parser is not None:
            parser.print_help()
        else:
            print("用法：hermes kanban <action> [options]\n"
                  "运行 'hermes kanban --help' 查看完整动作列表。", file=sys.stderr)
        return 0

    # Fast-fail for UX only; the durable trust boundary is in kanban_db, since children can
    # import DB mutators directly.
    if _is_delegated_child_cli_mutation(args):
        return _err("kanban: delegate_task 子任务上下文不能通过 CLI 修改 Kanban 任务")

    # `boards …` manages board metadata and the current-board pointer itself, so it must ignore
    # the `--board` routing override (else `--board beta boards show` reports beta).
    if action == "boards":
        return _dispatch_boards(args)

    # `--board <slug>` pins HERMES_KANBAN_BOARD for the duration of this call so it inherits the
    # exact resolution the dispatcher uses for workers.
    board_override = getattr(args, "board", None)
    board_scope = contextlib.nullcontext()
    if board_override:
        try:
            normed = kb._normalize_board_slug(board_override)
        except ValueError as exc:
            return _err(f"kanban: {exc}", 2)
        if not normed:
            return _err("kanban: --board 需要一个 slug", 2)
        # Boards other than 'default' must already exist — typoed slugs would otherwise silently
        # create an empty board.
        if normed != kb.DEFAULT_BOARD and not kb.board_exists(normed):
            return _err(f"kanban: 看板 {normed!r} 不存在。"
                        f"请用 `hermes kanban boards create {normed}` 创建它。")
        board_scope = kb.scoped_current_board(normed)

    with board_scope:
        # `repair` dispatches BEFORE auto-init: on a corrupt DB init_db() itself raises
        # KanbanDbCorruptError, which would turn every repair into "could not initialize database".
        if action == "repair":
            return _cmd_repair(args)
        # init_db is idempotent (one sqlite_master SELECT when tables exist) and prevents
        # "no such table: tasks" on first use from a fresh HERMES_HOME.
        try:
            kb.init_db()
        except Exception as exc:
            return _err(f"kanban: 无法初始化数据库：{exc}")

        handler = _HANDLERS.get(action)
        if not handler:
            return _err(f"kanban: 未知动作 {action!r}", 2)
        try:
            return int(handler(args) or 0)
        except (ValueError, RuntimeError, PermissionError) as exc:
            return _err(f"kanban: {exc}")


# --- Handlers ---

def _profile_author() -> str:
    """Best-effort author name for an interactive CLI call."""
    for env in ("HERMES_PROFILE_NAME", "HERMES_PROFILE"):
        v = os.environ.get(env)
        if v:
            return v
    try:
        from hermes_cli.profiles import get_active_profile_name
        return get_active_profile_name() or "user"
    except Exception:
        return "user"


_DELEGATED_CHILD_DENIED_ACTIONS: frozenset[str] = frozenset({
    "init", "create", "swarm", "assign", "reclaim", "reassign", "link", "unlink",
    "claim", "comment", "attach", "attach-rm", "complete", "edit", "block",
    "schedule", "unblock", "promote", "archive", "dispatch", "daemon", "repair",
    "heartbeat", "notify-subscribe", "notify-unsubscribe", "specify", "decompose",
    "request-review", "request-changes", "reopen-review",
    "gc",
})

_DELEGATED_CHILD_DENIED_BOARD_ACTIONS: frozenset[str] = frozenset({
    "create", "new", "rm", "remove", "delete", "switch", "use", "rename",
    "set-default-workdir", "import",
})


def _is_delegated_child_cli_mutation(args: argparse.Namespace) -> bool:
    action = getattr(args, "kanban_action", None)
    if action == "boards":
        if (getattr(args, "boards_action", None) or "list") not in _DELEGATED_CHILD_DENIED_BOARD_ACTIONS:
            return False
    elif action not in _DELEGATED_CHILD_DENIED_ACTIONS:
        return False
    from agent.delegation_context import kanban_path_is_fenced

    return kanban_path_is_fenced(kb.kanban_home()) or kanban_path_is_fenced(kb.kanban_db_path())


def _joined_words(words) -> Optional[str]:
    """Free-text positional ``nargs="*"`` words -> stripped string, or None when absent."""
    return " ".join(words).strip() if words else None


def _stripped_or_none(value: Optional[str]) -> Optional[str]:
    """``None`` stays ``None``; otherwise strip, and treat the empty string as ``None``."""
    return None if value is None else (value.strip() or None)


def _ok_or_err(ok, fail: str, done: str) -> int:
    """Single-mutation handlers: print ``done`` (rc 0) or ``fail`` to stderr (rc 1)."""
    if not ok:
        return _err(fail)
    print(done)
    return 0


def _bulk_ids(args: argparse.Namespace) -> list[str]:
    """Positional ``task_id`` plus ``--ids`` extras (bulk verbs)."""
    return [args.task_id] + list(getattr(args, "ids", None) or [])


def _require_ids(args: argparse.Namespace) -> tuple[list[str], int]:
    """``args.task_ids`` -> ``(ids, 0)`` or ``([], 1)`` after printing the standard error."""
    ids = list(args.task_ids or [])
    if not ids:
        return ids, _err("至少需要一个 task_id")
    return ids, 0


def _parse_duration(val) -> Optional[int]:
    """``30s`` / ``5m`` / ``2h`` / ``1d`` or a raw integer → seconds; None for empty input;
    ValueError on malformed input."""
    if val is None or val == "":
        return None
    s = str(val).strip().lower()
    try:
        return int(s)  # bare integer → seconds
    except ValueError:
        pass
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if not (s and s[-1] in units):
        raise ValueError(f"时长格式错误 {val!r}（应为 30s、5m、2h、1d 或一个数字）")
    try:
        n = float(s[:-1])
    except ValueError as exc:
        raise ValueError(f"时长格式错误 {val!r}") from exc
    return int(n * units[s[-1]])


def _cmd_init(args: argparse.Namespace) -> int:
    path = kb.init_db()
    print(f"Kanban 数据库已初始化于 {path}")
    print()
    # Profiles on disk == assignees already addressable.
    try:
        profiles = kb.list_profiles_on_disk()
    except Exception:
        profiles = []
    if profiles:
        print(f"磁盘上发现 {len(profiles)} 个 profile；其中任意一个都可以作为 --assignee：")
        for name in profiles:
            print(f"  {name}")
    else:
        print("在 ~/.hermes/profiles/ 下未找到 profile。\n"
              "分配任务前请先用 `hermes -p <name> setup` 创建一个。")
    print(
        "\n下一步：启动网关，让 ready 状态的任务真正被接管。\n"
        "  hermes gateway start\n\n"
        "网关内置调度器，默认每 60 秒触发一次\n"
        "（配置：kanban.dispatch_interval_seconds）。若没有运行中的网关，\n"
        "任务会永远停留在 'ready'。"
    )
    return 0


def _cmd_heartbeat(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        ok = kbd.heartbeat_worker(conn, args.task_id, note=getattr(args, "note", None),
                                 expected_run_id=_worker_run_id_for(args.task_id))
    return _ok_or_err(ok, f"无法为 {args.task_id} 记录心跳（未在运行？）",
                      f"已为 {args.task_id} 记录心跳")


def _cmd_assignees(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        data = kb.known_assignees(conn)
    if _json_out(args, data):
        return 0
    if not data:
        print("（无 assignee——用 `hermes -p <name> setup` 创建一个 profile）")
        return 0
    print(f"{'名称':20s}  {'在磁盘':8s}  计数")
    for entry in data:
        on_disk = "是" if entry["on_disk"] else "否"
        print(f"{entry['name']:20s}  {on_disk:8s}  {_fmt_counts(entry['counts'] or {}, '(idle)')}")
    return 0


def _cmd_create(args: argparse.Namespace) -> int:
    from agent.delegation_context import is_dispatcher_owned_worker_context

    body = args.body
    body_file = getattr(args, "body_file", None)
    if body is not None and body_file is not None:
        return _err("kanban: --body and --body-file are mutually exclusive", 2)
    if body_file is not None:
        try:
            body = sys.stdin.read() if body_file == "-" else Path(body_file).read_text(encoding="utf-8")
        except OSError as exc:
            return _err(f"kanban: --body-file: {exc}", 2)

    try:
        ws_kind, ws_path = _parse_workspace_flag(args.workspace)
        branch_name = _parse_branch_flag(getattr(args, "branch", None))
    except argparse.ArgumentTypeError as exc:
        return _err(f"kanban: {exc}", 2)
    if branch_name and ws_kind != "worktree":
        return _err("kanban: --branch 仅在配合 --workspace worktree 时有效", 2)
    try:
        max_runtime = _parse_duration(getattr(args, "max_runtime", None))
    except ValueError as exc:
        return _err(f"kanban: --max-runtime：{exc}", 2)
    max_retries = getattr(args, "max_retries", None)
    if max_retries is not None and max_retries < 1:
        return _err(f"kanban: --max-retries 必须 >= 1（当前为 {max_retries}）；"
                    "设为 1 表示首次失败即触发。", 2)
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(
            conn, title=args.title, body=body, assignee=args.assignee,
            created_by=args.created_by or _profile_author(),
            workspace_kind=ws_kind, workspace_path=ws_path, branch_name=branch_name,
            project_id=getattr(args, "project", None), tenant=args.tenant, priority=args.priority,
            parents=tuple(args.parent or ()), triage=bool(getattr(args, "triage", False)),
            idempotency_key=getattr(args, "idempotency_key", None),
            max_runtime_seconds=max_runtime, skills=getattr(args, "skills", None) or None,
            max_retries=max_retries, model_override=getattr(args, "model_override", None),
            provider_override=getattr(args, "provider_override", None),
            goal_mode=bool(getattr(args, "goal_mode", False)),
            goal_max_turns=getattr(args, "goal_max_turns", None),
            completion_contract=getattr(args, "completion_contract", None),
            initial_status=getattr(args, "initial_status", "running"),
            creator_task_id=(os.environ.get("HERMES_KANBAN_TASK")
                             if is_dispatcher_owned_worker_context() else None),
        )
        task = kb.get_task(conn, task_id)
    if getattr(args, "json", False):
        _print_json(_task_to_dict(task))
    else:
        print(f"Created {task_id}  ({task.status}, assignee={task.assignee or '-'})")
        # Warn only for ready+assigned tasks that would sit without a dispatcher (triage/todo idle
        # by design, unassigned can't dispatch); skipped under --json so stdout stays parseable.
        if task.status == "ready" and task.assignee:
            running, message = _check_dispatcher_presence()
            if not running and message:
                print(f"\n⚠  {message}", file=sys.stderr)
    return 0


def _cmd_swarm(args: argparse.Namespace) -> int:
    try:
        workers = [ks.parse_worker_arg(raw) for raw in (args.worker or [])]
    except ValueError as exc:
        return _err(f"kanban swarm: {exc}", 2)
    if not workers:
        return _err("kanban swarm: 至少需要一个 --worker", 2)
    with kbc.connect_closing() as conn:
        created = ks.create_swarm(
            conn, goal=args.goal, workers=workers, verifier_assignee=args.verifier,
            synthesizer_assignee=args.synthesizer, tenant=args.tenant,
            created_by=args.created_by or _profile_author(), priority=args.priority,
            idempotency_key=getattr(args, "idempotency_key", None),
        )
    if getattr(args, "json", False):
        _print_json(created.as_dict())
    else:
        print(f"Swarm 根任务：{created.root_id}\n"
              "工作节点：" + ", ".join(created.worker_ids) + "\n"
              f"验证者：{created.verifier_id}\n"
              f"综合者：{created.synthesizer_id}")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    assignee = args.assignee
    if args.mine and not assignee:
        assignee = _profile_author()
    with kbc.connect_closing() as conn:
        # Cheap mini-dispatch so list reflects dependencies cleared since the last tick.
        kb.recompute_ready(conn)
        tasks = kb.list_tasks(
            conn, assignee=assignee, status=args.status, tenant=args.tenant, session_id=args.session,
            include_archived=args.archived, order_by=getattr(args, "sort", None),
            workflow_template_id=args.workflow_template_id, current_step_key=args.current_step_key,
        )
    if _json_out(args, [_task_to_dict(t) for t in tasks]):
        return 0
    # Passive discoverability: only multi-board users see which board this is.
    try:
        all_boards = kb.list_boards(include_archived=False)
    except Exception:
        all_boards = []
    if len(all_boards) > 1:
        other_count = len(all_boards) - 1
        print(f"看板：{kb.get_current_board()}（另有 {other_count} 个看板——"
              f"`hermes kanban boards list`）\n")
    if not tasks:
        print("（没有匹配的任务）")
        return 0
    for t in tasks:
        print(_fmt_task_line(t))
    return 0


def _print_diagnostics(diags, indent: str, *, with_kind: bool) -> None:
    """Shared human rendering for ``show`` and ``diagnostics`` (suggested actions only)."""
    sev_marker = {"warning": "⚠", "error": "!!", "critical": "!!!"}
    for d in diags:
        head = f"{d.kind}: {d.title}" if with_kind else d.title
        print(f"{indent}{sev_marker.get(d.severity, '?')} [{d.severity}] {head}")
        if d.data:
            bits = [f"{k}={','.join(str(x) for x in v)}" if isinstance(v, list) else f"{k}={v}"
                    for k, v in d.data.items()]
            if bits:
                print(f"{indent}   数据：{' | '.join(bits)}")
        for a in d.actions:
            if a.suggested:
                print(f"{indent}   → {a.label}")


def _print_section(title: str, lines) -> None:
    """Blank line, ``title``, then each line (``show`` body sections)."""
    print()
    print(title)
    for line in lines:
        print(line)


def _cmd_show(args: argparse.Namespace) -> int:
    rsk, rc = _run_state_kwargs(args, "show")
    if rc:
        return rc
    graph = None
    want_json = getattr(args, "json", False)
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, args.task_id)
        if not task:
            return _err(f"无此任务：{args.task_id}")
        comments = kb.list_comments(conn, args.task_id)
        events = kb.list_events(conn, args.task_id)
        parents = kb.parent_ids(conn, args.task_id)
        children = kb.child_ids(conn, args.task_id)
        runs = kb.list_runs(conn, args.task_id, **rsk)
        # Workers hand off via task_runs.summary; tasks.result stays NULL unless set.
        latest_summary = kb.latest_summary(conn, args.task_id)
        if not want_json:
            graph = kb.task_graph_context(conn, task.id)

    if want_json:
        _print_json({
            "task": _task_to_dict(task), "latest_summary": latest_summary, "parents": parents, "children": children,
            "comments": [_obj_dict(c, ("author", "body", "created_at")) for c in comments],
            "events": [_obj_dict(e, ("kind", "payload", "created_at", "run_id")) for e in events],
            "runs": [_obj_dict(r, _SHOW_RUN_FIELDS) for r in runs],
        })
        return 0

    def field(label: str, value) -> None:
        print(f"  {label + ':':<11}{value}")

    print(f"Task {task.id}: {task.title}")
    field("status", task.status)
    field("assignee", task.assignee or "-")
    if task.tenant:
        field("tenant", task.tenant)
    field("workspace", f"{task.workspace_kind}" + (f" @ {task.workspace_path}" if task.workspace_path else ""))
    if task.branch_name:
        field("branch", task.branch_name)
    if task.skills:
        field("skills", ", ".join(task.skills))
    if task.model_override:
        _prov = f" (provider: {task.provider_override})" if task.provider_override else ""
        field("model", f"{task.model_override}{_prov}")
    # Effective retry threshold (task > config > default) explains auto-blocks.
    if task.max_retries is not None:
        print(f"  max-retries: {task.max_retries} (task)")
    else:
        cfg_val = _kanban_config().get("failure_limit")
        if cfg_val is not None and int(cfg_val) != kb.DEFAULT_FAILURE_LIMIT:
            print(f"  max-retries: {int(cfg_val)} (config kanban.failure_limit)")
        else:
            print(f"  max-retries: {kb.DEFAULT_FAILURE_LIMIT} (default)")
    field("created", f"{_fmt_ts(task.created_at)} by {task.created_by or '-'}")

    # Diagnostics up top so CLI users see distress signals before scrolling.
    from hermes_cli import kanban_diagnostics as kd
    diags = kd.compute_task_diagnostics(task, events, runs, graph=graph)
    if diags:
        print(f"\n  诊断（{len(diags)}）：")
        _print_diagnostics(diags, "    ", with_kind=False)
    if task.started_at:
        field("started", _fmt_ts(task.started_at))
    if task.completed_at:
        field("completed", _fmt_ts(task.completed_at))
    if parents:
        field("parents", ", ".join(parents))
    if children:
        field("children", ", ".join(children))
    if task.body:
        _print_section("正文：", [task.body])
    if task.result:
        _print_section("结果：", [task.result])
    elif latest_summary:
        _print_section("最新摘要：", [latest_summary])
    if comments:
        _print_section(f"评论（{len(comments)}）：",
                       (f"  [{_fmt_ts(c.created_at)}] {c.author}: {c.body}" for c in comments))
    if events:
        _print_section(f"事件（{len(events)}）：", (
            f"  [{_fmt_ts(e.created_at)}]{f' [run {e.run_id}]' if e.run_id else ''} {e.kind}"
            f"{f' {e.payload}' if e.payload else ''}" for e in events[-20:]))
    if runs:
        print()
        print(f"运行记录（{len(runs)}）：")
        for r in runs:
            # Clamp to 0 so NTP backward-jumps don't print negative seconds.
            elapsed = max(0, r.ended_at - r.started_at) if r.ended_at else None
            el = f"{elapsed}s" if elapsed is not None else "active"
            outcome = r.outcome or r.status or "active"
            print(f"  #{r.id:<3} {outcome:<12} @{r.profile or '-'}  {el}  {_fmt_ts(r.started_at)}")
            if r.summary:
                print(f"        → {r.summary.splitlines()[0][:160]}")
            if r.error:
                print(f"        ! {r.error.splitlines()[0][:160]}")
    return 0


def _cmd_assign(args: argparse.Namespace) -> int:
    profile = _none_profile(args.profile)
    with kbc.connect_closing() as conn:
        ok = kb.assign_task(conn, args.task_id, profile)
    return _ok_or_err(ok, f"无此任务：{args.task_id}",
                      f"已将 {args.task_id} 分配给 {profile or '(未分配)'}")


def _cmd_set_model(args: argparse.Namespace) -> int:
    model = args.model
    if model is not None and model.lower() in {"none", "-", "null", ""}:
        model = None
    provider = getattr(args, "provider", None)
    try:
        with kbc.connect_closing() as conn:
            ok = kb.set_model_override(conn, args.task_id, model, provider=provider)
    except (ValueError, RuntimeError) as exc:
        return _err(f"kanban: {exc}", 2)
    if not ok:
        return _err(f"无此任务：{args.task_id}")
    if model:
        label = f"{provider}:{model}" if provider else model
        print(f"已为 {args.task_id} 设置模型覆盖：{label}（下次调度时生效）")
    else:
        print(f"已清除 {args.task_id} 的模型覆盖（工作节点将使用其 profile 默认值）")
    return 0


def _cmd_reclaim(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        ok = kb.reclaim_task(conn, args.task_id, reason=getattr(args, "reason", None))
    return _ok_or_err(ok, f"无法回收 {args.task_id}（未在运行或未知 id）",
                      f"已回收 {args.task_id}")


def _cmd_reassign(args: argparse.Namespace) -> int:
    profile = _none_profile(args.profile)
    reclaim = bool(getattr(args, "reclaim", False))
    with kbc.connect_closing() as conn:
        ok = kb.reassign_task(conn, args.task_id, profile, reclaim_first=reclaim, reason=getattr(args, "reason", None))
    return _ok_or_err(
        ok,
        f"无法重新分配 {args.task_id}（未知 id，或仍在运行——先传 --reclaim 释放）",
        f"已将 {args.task_id} 重新分配给 {profile or '(未分配)'}" + ("（认领已回收）" if reclaim else ""),
    )


def _rows_by_task(conn, table: str, ids: list[str]) -> dict[str, list]:
    """``{task_id: [rows ordered by id]}`` for every id (empty list when none)."""
    by = {i: [] for i in ids}
    placeholders = ",".join(["?"] * len(ids))
    for row in conn.execute(f"SELECT * FROM {table} WHERE task_id IN ({placeholders}) ORDER BY id", tuple(ids)):
        by.setdefault(row["task_id"], []).append(row)
    return by


def _cmd_diagnostics(args: argparse.Namespace) -> int:
    """List active diagnostics on the board via the same rule engine the dashboard uses."""
    from hermes_cli import kanban_diagnostics as kd
    # Honour kanban.default_assignee as the fallback for unassigned ready tasks (#27145),
    # kanban.max_in_progress as the global concurrency cap (#33488), kanban.max_in_progress_per_profile as
    # the per-profile cap (#21582), and kanban.max_spawn as the per-tick spawn limit (#28805). Same
    # semantics as the gateway dispatch path so behavior matches whether the user runs the CLI directly or
    # relies on the gateway-embedded dispatcher.
    from hermes_cli.config import load_config

    diag_config = kd.config_from_runtime_config(load_config())

    with kbc.connect_closing() as conn:
        # Either one-task mode or fleet mode.
        if getattr(args, "task", None):
            task = kb.get_task(conn, args.task)
            if task is None:
                return _err(f"无此任务：{args.task}")
            diags_by_task = {args.task: kd.compute_task_diagnostics(
                task, kb.list_events(conn, args.task), kb.list_runs(conn, args.task),
                graph=kb.task_graph_context(conn, args.task), config=diag_config)}
        else:
            # Fleet mode: pull all non-archived tasks + their events/runs.
            rows = list(conn.execute("SELECT * FROM tasks WHERE status != 'archived'").fetchall())
            ids = [r["id"] for r in rows]
            diags_by_task = {}
            if ids:
                ev_by = _rows_by_task(conn, "task_events", ids)
                run_by = _rows_by_task(conn, "task_runs", ids)
                graph_by = kb.task_graph_contexts(conn, ids)
                for r in rows:
                    tid = r["id"]
                    dl = kd.compute_task_diagnostics(r, ev_by.get(tid, []), run_by.get(tid, []),
                                                     graph=graph_by.get(tid), config=diag_config)
                    if dl:
                        diags_by_task[tid] = dl

        sev = getattr(args, "severity", None)
        if sev:
            floor = kd.SEVERITY_ORDER.index(sev)
            diags_by_task = {tid: kept for tid, dl in diags_by_task.items()
                             if (kept := [d for d in dl if kd.SEVERITY_ORDER.index(d.severity) >= floor])}

        # Map task_id → title/status/assignee for the table output.
        meta: dict[str, dict] = {}
        if diags_by_task:
            placeholders = ",".join(["?"] * len(diags_by_task))
            for r in conn.execute(f"SELECT id, title, status, assignee FROM tasks WHERE id IN ({placeholders})",
                                  tuple(diags_by_task.keys())):
                meta[r["id"]] = {k: r[k] for k in ("title", "status", "assignee")}

    # What this home believes it may claim on a shared board (#113620).
    allowlist = kbd.dispatch_profile_allowlist_summary()

    if getattr(args, "json", False):
        # Per-task rows unchanged; the home-scope allowlist rides as a trailing row
        # (task_id null) so existing `payload[0]["diagnostics"]` consumers keep working.
        _print_json([{"task_id": tid, **meta.get(tid, {}), "diagnostics": [d.to_dict() for d in dl]}
                     for tid, dl in diags_by_task.items()]
                    + [{"task_id": None, "dispatch_profiles": allowlist, "diagnostics": []}])
        return 0

    print(f"kanban.dispatch_profiles: {allowlist}")
    if not diags_by_task:
        print("此看板上没有活跃诊断。")
        return 0

    total = sum(len(dl) for dl in diags_by_task.values())
    print(f"{total} 条活跃诊断，涉及 {len(diags_by_task)} 个任务：\n")
    for tid, dl in diags_by_task.items():
        m = meta.get(tid, {})
        print(f"  {tid}  {m.get('status') or '?':8s}  @{m.get('assignee') or '(未分配)':18s}  "
              f"{m.get('title') or '(无标题)'}")
        _print_diagnostics(dl, "    ", with_kind=True)
        print()
    return 0


def _cmd_link(args: argparse.Namespace) -> int:
    # A worker linking its own running card (dependency-block handoff) proves
    # ownership with its run id; linking a foreign task never needs one.
    expected_child_run_id = (
        _worker_run_id_for(args.child_id)
        if args.child_id == os.environ.get("HERMES_KANBAN_TASK") else None)
    with kbc.connect_closing() as conn:
        gated = kb.link_tasks(conn, args.parent_id, args.child_id,
                              expected_child_run_id=expected_child_run_id)
    print(f"Linked {args.parent_id} -> {args.child_id}")
    if gated:
        print(
            f"Note: {args.child_id} was ready and is now todo — parent "
            f"{args.parent_id} is not done yet. The ready -> running claim "
            f"re-checks parents, so the child only runs after the parent "
            f"completes; use `hermes kanban unlink {args.parent_id} {args.child_id}` "
            f"to run it now."
        )
    return 0


def _cmd_unlink(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        ok = kb.unlink_tasks(conn, args.parent_id, args.child_id)
    return _ok_or_err(ok, f"无此链接：{args.parent_id} -> {args.child_id}",
                      f"已解除链接 {args.parent_id} -> {args.child_id}")


def _cmd_claim(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        task = kb.claim_task(conn, args.task_id, ttl_seconds=args.ttl)
        if task is None:
            existing = kb.get_task(conn, args.task_id)
            if existing is None:
                return _err(f"无此任务：{args.task_id}")
            return _err(f"无法认领 {args.task_id}：status={existing.status} "
                        f"lock={existing.claim_lock or '(none)'}")
        workspace = kbw.resolve_workspace(task)
        kbw.set_workspace_path(conn, task.id, str(workspace))
    print(f"已认领 {task.id}\n工作区：{workspace}")
    return 0


def _cmd_comment(args: argparse.Namespace) -> int:
    body = " ".join(args.text).strip()
    if args.max_len is not None:
        if args.max_len < 1:
            return _err("kanban: --max-len 必须为正数", 2)
        if len(body) > args.max_len:
            suffix = f"\n\n[因 --max-len 截断至 {args.max_len} 个字符]"
            body = body[: max(0, args.max_len - len(suffix))].rstrip() + suffix
    author = args.author or _profile_author()
    with kbc.connect_closing() as conn:
        kb.add_comment(conn, args.task_id, author, body)
    print(f"已向 {args.task_id} 添加评论")
    return 0


def _cmd_attach(args: argparse.Namespace) -> int:
    """Attach a local file via the shared ``store_attachment_bytes`` path (same 25 MB cap and name
    sanitisation as the dashboard upload and agent tool)."""
    import mimetypes
    _worker_run_id_for(args.task_id)

    src = Path(args.path).expanduser()
    if not src.is_file():
        return _err(f"kanban: 无此文件：{src}")
    data = src.read_bytes()
    name = args.name or src.name
    content_type = args.content_type or mimetypes.guess_type(name)[0]
    uploaded_by = args.author or _profile_author()
    try:
        with kbc.connect_closing() as conn:
            att_id = kb.store_attachment_bytes(conn, args.task_id, name, data, content_type=content_type,
                                               uploaded_by=uploaded_by)
    except kb.AttachmentTooLarge as exc:
        return _err(f"kanban: {exc}")
    print(f"已将 {name} 附加到 {args.task_id}（附件 {att_id}，{len(data)} 字节）")
    return 0


def _cmd_attachments(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        if kb.get_task(conn, args.task_id) is None:
            return _err(f"无此任务：{args.task_id}")
        atts = kb.list_attachments(conn, args.task_id)
    if _json_out(args, [_obj_dict(a, _ATTACHMENT_FIELDS) for a in atts], ascii=True):
        return 0
    if not atts:
        print(f"{args.task_id} 上没有附件")
        return 0
    print(f"{args.task_id} 的附件：")
    for a in atts:
        ct = a.content_type or "-"
        print(f"  [{a.id}] {a.filename}  （{a.size} 字节，{ct}，上传者 {a.uploaded_by or '-'}）")
        print(f"        {a.stored_path}")
    return 0


def _cmd_attach_rm(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        removed = kb.delete_attachment(conn, args.attachment_id)
    if removed is None:
        return _err(f"无此附件：{args.attachment_id}")
    print(f"已从 {removed.task_id} 删除附件 {args.attachment_id}（{removed.filename}）")
    return 0


def _worker_run_id_for(task_id: str) -> Optional[int]:
    env_tid = os.environ.get("HERMES_KANBAN_TASK")
    if env_tid and env_tid != task_id:
        raise ValueError(f"工作节点被限定于任务 {env_tid}；拒绝修改 {task_id}")
    raw = os.environ.get("HERMES_KANBAN_RUN_ID")
    if os.environ.get("HERMES_KANBAN_TASK") != task_id or not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _goal_mode_handoff_rejection(task: Optional[kb.Task], evidence: str):
    """Goal judge for every terminal worker handoff (including review).

    Returns ``(verdict, reason_or_None)``: ``"done"`` allows; ``"blocked"`` = judge ruled the goal
    unachievable; ``"continue"``/``"wait"`` reject with the judge's reason. Judge failures allow
    the handoff (logged).

    See #100954.
    ``{"done", None}`` means the judge allows the handoff; anything else is a rejection whose verdict
    disambiguates the guidance the caller gives the worker (``continue`` = not done yet, ``blocked`` =
    judged unachievable — see #100954).
    """
    if task is None or not task.goal_mode:
        return ("done", None)
    try:
        from agent.auxiliary_client import get_text_auxiliary_client

        client, model = get_text_auxiliary_client("goal_judge")
    except Exception:
        client, model = None, None
    if client is None or not model:
        return ("done", None)

    from hermes_cli.goals import judge_goal

    verdict, reason, transport_failed = "done", "", False
    try:
        # Headless handoff checks run outside any agent turn: bind the per-task relay-affinity
        # scope (mirrors kanban_specify) so the relay does not reject the judge call (#113669).
        from agent.portal_tags import get_affinity_scope, reset_affinity_scope, set_affinity_scope
        affinity_token = None if get_affinity_scope() else set_affinity_scope(f"kanban:{task.id}")
        try:
            verdict, reason, _, _, transport_failed = judge_goal(
                goal=f"{task.title}\n\n{task.body or ''}".strip(),
                last_response=evidence.strip())
        finally:
            if affinity_token is not None:
                reset_affinity_scope(affinity_token)
    except Exception as judge_exc:
        import logging as _logging

        _logging.getLogger(__name__).warning("goal judge check failed, allowing lifecycle handoff: %s",
                                             judge_exc, exc_info=True)
    if transport_failed:
        # ``judge_goal`` fails open to ``continue`` on transport errors (relay 400, auth, timeout);
        # an unreachable judge is not a human "not done" and must not reject the handoff (#83610).
        import logging as _logging

        _logging.getLogger(__name__).warning("goal judge unreachable (%s), allowing lifecycle handoff", reason)
        return ("done", None)
    return (verdict, None if verdict == "done" else reason)


def _goal_gate_error(conn, tid: str, evidence: str, handoff: str, blocked_hint: str,
                     continue_hint: str) -> Optional[str]:
    """Goal-mode judge gate shared by ``complete`` / ``request-review`` (mirrors tools/kanban_tools.py);
    applied to every terminal handoff so request-review can't bypass it. Returns the error line, or
    None to allow."""
    verdict, rejection = _goal_mode_handoff_rejection(kb.get_task(conn, tid), evidence)
    if verdict == "blocked":
        return (f"kanban: {tid} 的{handoff}被拒绝：评判器判定"
                f"目标不可达成——{rejection}。{blocked_hint}")
    if rejection is not None:
        return f"kanban: {tid} 的{handoff}被评判器拒绝：{rejection}。{continue_hint}"
    return None


def _cmd_complete(args: argparse.Namespace) -> int:
    """Mark one or more tasks done. Supports a single id or a list."""
    ids, rc = _require_ids(args)
    if rc:
        return rc
    summary = getattr(args, "summary", None)
    raw_meta = getattr(args, "metadata", None)
    # Handoff fields are per-run; refuse to copy them across N runs.
    if len(ids) > 1 and (summary or raw_meta):
        return _err("kanban: --summary / --metadata 是按任务独立的，不能与多个 id 一起使用"
                    "（否则会把同一份交接套用到每个任务）。"
                    "请逐个完成任务，或在批量关闭时去掉这些标志。", 2)
    metadata, rc = _parse_metadata_flag(raw_meta)
    if rc:
        return rc
    fail_msg: dict[str, str] = {}
    with kbc.connect_closing() as conn:
        def op(tid):
            gate_err = _goal_gate_error(
                conn, tid, (summary or args.result or "").strip(), "完成",
                "用 kanban edit 重新界定范围，或改用 kanban block 记录阻塞，而不是完成。",
                "请提供与任务验收标准相符的证据。")
            if gate_err:
                fail_msg[tid] = gate_err
                return False
            fail_msg[tid] = f"无法完成 {tid}（未知 id 或已处于终态）"
            try:
                done = kb.complete_task(conn, tid, result=args.result, summary=summary, metadata=metadata,
                                        expected_run_id=_worker_run_id_for(tid),
                                        force=bool(getattr(args, "force", False)))
            except kb.LiveClaimError:
                fail_msg[tid] = (f"无法完成 {tid}：有 worker 正在运行它。请等该 worker 结束、"
                                 f"用 `hermes kanban reclaim {tid}` 释放认领，或加 `--force` "
                                 f"关闭其运行并强制完成。")
                return False
            except kb.EmptyCompletionError as empty_err:
                fail_msg[tid] = (f"cannot complete {tid}: {empty_err}. Pass --result/--summary "
                                 f"describing what was done (an empty completion is not evidence).")
                return False
            if not done:
                # complete_task returns bare False for a dependency refusal too;
                # name the open parents instead of claiming the id is unknown.
                blockers = kb.unsatisfied_parents(conn, tid)
                if blockers:
                    detail = ", ".join(f"{pid} ({status})" for pid, status in blockers)
                    fail_msg[tid] = (f"cannot complete {tid}: unsatisfied parent dependencies: {detail}; "
                                     f"complete the parents first, or `hermes kanban unlink <parent> {tid}`.")
            return done

        return _bulk_apply(ids, op, lambda tid: f"已完成 {tid}", fail_msg.__getitem__)


def _cmd_edit(args: argparse.Namespace) -> int:
    result = getattr(args, "result", None)
    raw_metadata = getattr(args, "metadata", None)
    summary = getattr(args, "summary", None)
    title = getattr(args, "title", None)
    body = getattr(args, "body", None)
    priority = getattr(args, "priority", None)
    if result is None and (summary is not None or raw_metadata is not None):
        return _err("kanban edit: --summary and --metadata require --result", 2)
    if all(value is None for value in (title, body, priority, result)):
        return _err("kanban edit: provide --title, --body, --priority, or --result", 2)
    metadata, rc = _parse_metadata_flag(raw_metadata)
    if rc:
        return rc
    with kbc.connect_closing() as conn:
        ok = kb.edit_task(
            conn, args.task_id, title=title, body=body, priority=priority,
            result=result, summary=summary, metadata=metadata,
        )
    return _ok_or_err(
        ok,
        f"cannot edit {args.task_id} (unknown id, or --result used on a task that is not done)",
        f"Edited {args.task_id}",
    )


def _commented(conn, reason: Optional[str], author, prefix: str, op):
    """Wrap a per-task ``op`` so a ``reason`` is first recorded as a ``PREFIX: reason`` comment."""
    def run(tid):
        if reason:
            kb.add_comment(conn, tid, author, f"{prefix}: {reason}")
        return op(tid)
    return run


def _cmd_block(args: argparse.Namespace) -> int:
    reason = _joined_words(args.reason)
    kind = getattr(args, "kind", None)
    author = _profile_author()
    ids = _bulk_ids(args)
    suffix = f": {reason}" if reason else ""
    with kbc.connect_closing() as conn:
        def ok_msg(tid):
            # Report where it landed: dependency blocks -> todo, tripped unblock-loop breaker -> triage.
            landed = kb.get_task(conn, tid)
            where = landed.status if landed else "blocked"
            if where == "todo":
                return f"{tid} → todo (dependency wait){suffix}"
            if kind == "dependency" and where == "blocked":
                return f"Blocked {tid} as needs_input (no open parent to wait on){suffix}"
            if where == "triage":
                # Only a typed owner-input block carries a question for a human.
                verdict = ("needs a human decision" if (landed.block_kind if landed else kind) == "needs_input"
                           else "orchestration attention needed")
                return f"{tid} → triage (unblock loop detected — {verdict}){suffix}"
            return f"Blocked {tid}{suffix}"

        op = _commented(conn, reason, author, "BLOCKED", lambda tid: kb.block_task(
            conn, tid, reason=reason, kind=kind, expected_run_id=_worker_run_id_for(tid)))
        return _bulk_apply(ids, op, ok_msg, lambda tid: f"cannot block {tid}")


def _cmd_schedule(args: argparse.Namespace) -> int:
    reason = _joined_words(args.reason)
    author = _profile_author()
    ids = _bulk_ids(args)
    suffix = f": {reason}" if reason else ""
    with kbc.connect_closing() as conn:
        op = _commented(conn, reason, author, "SCHEDULED", lambda tid: kb.schedule_task(
            conn, tid, reason=reason, expected_run_id=_worker_run_id_for(tid)))
        return _bulk_apply(ids, op, lambda tid: f"已安排 {tid}{suffix}", lambda tid: f"无法安排 {tid}")


def _cmd_unblock(args: argparse.Namespace) -> int:
    if os.environ.get("HERMES_KANBAN_TASK"):
        return _err("kanban unblock 仅限编排器使用；工作节点必须交接其被分配的任务")
    ids, rc = _require_ids(args)
    if rc:
        return rc
    reason = _stripped_or_none(getattr(args, "reason", None))
    author = _profile_author() if reason else None
    suffix = f": {reason}" if reason else ""
    with kbc.connect_closing() as conn:
        op = _commented(conn, reason, author, "UNBLOCK", lambda tid: kb.unblock_task(conn, tid))
        return _bulk_apply(ids, op, lambda tid: f"已解除阻塞 {tid}{suffix}",
                           lambda tid: f"无法解除阻塞 {tid}（未处于阻塞/已安排状态？）")


def _cmd_request_review(args: argparse.Namespace) -> int:
    tid = args.task_id
    summary = _stripped_or_none(getattr(args, "summary", None))
    metadata, rc = _parse_metadata_flag(getattr(args, "metadata", None))
    if rc:
        return rc
    with kbc.connect_closing() as conn:
        gate_err = _goal_gate_error(
            conn, tid, summary or "", "评审交接",
            "改用 kanban block 记录阻塞，而不是请求评审。",
            "请提供与该任务相符的验收证据。")
        if gate_err:
            return _err(gate_err)
        ok, reason = kb.request_review(
            conn, tid, summary=summary, metadata=metadata, reviewer=getattr(args, "reviewer", None),
            expected_run_id=_worker_run_id_for(tid), force=bool(getattr(args, "force", False)), with_reason=True)
        if not ok:
            return _err(f"无法为 {tid} 请求评审：{reason or '未在运行/未就绪？'}")
        persisted_run = kb.latest_run(conn, tid)
        display_summary = persisted_run.summary if persisted_run else None
        print(f"已为 {tid} 请求评审" + (f"：{display_summary}" if display_summary else ""))
    return 0


def _cmd_request_changes(args: argparse.Namespace) -> int:
    tid = args.task_id
    reason = " ".join(args.reason).strip()
    with kbc.connect_closing() as conn:
        ok, detail = kb.request_changes(conn, tid, reason=reason, expected_run_id=_worker_run_id_for(tid))
        if not ok:
            return _err(f"无法为 {tid} 请求修改：{detail or '无效的评审状态'}")
        print(f"已为 {tid} 请求修改" + (f"；已转给 {detail}" if detail else ""))
    return 0


def _cmd_reopen_review(args: argparse.Namespace) -> int:
    ids, rc = _require_ids(args)
    if rc:
        return rc
    reason = getattr(args, "reason", None)
    if reason is not None:
        reason = str(kb.redact_review_value(reason.strip())).strip() or None
    author = _profile_author() if reason else None
    suffix = f": {reason}" if reason else ""
    with kbc.connect_closing() as conn:
        def op(tid):
            if not kb.reopen_review_task(conn, tid):
                return False
            if reason:
                kb.add_comment(conn, tid, author or "operator", f"CHANGES REQUESTED: {reason}")
            return True

        return _bulk_apply(ids, op, lambda tid: f"已重新打开 {tid}{suffix}",
                           lambda tid: f"无法重新打开 {tid}（不在评审中？）")


def _cmd_promote(args: argparse.Namespace) -> int:
    reason = _joined_words(args.reason)
    author = _profile_author()
    # Dedupe while preserving order; positional task_id always first.
    ids = list(dict.fromkeys(_bulk_ids(args)))
    dry_run = bool(args.dry_run)

    results: list[dict[str, object]] = []
    with kbc.connect_closing() as conn:
        for tid in ids:
            ok, err = kb.promote_task(conn, tid, actor=author, reason=reason, dry_run=dry_run)
            results.append({"task_id": tid, "promoted": ok, "dry_run": dry_run,
                            "reason": reason, "error": err})

    failed = [r for r in results if not r["promoted"]]
    if getattr(args, "json", False):
        # Single-id stays a flat object for back-compat; bulk emits a list.
        _print_json(results[0] if len(results) == 1 else results)
        return 0 if not failed else 1

    tag = "（试运行）" if dry_run else ""
    label = "将提升" if dry_run else "已提升"
    suffix = f": {reason}" if reason else ""
    for r in results:
        if r["promoted"]:
            print(f"{label} {r['task_id']} -> ready{tag}{suffix}")
        else:
            print(f"无法提升 {r['task_id']}：{r['error']}", file=sys.stderr)
    return 0 if not failed else 1


def _cmd_archive(args: argparse.Namespace) -> int:
    ids = list(args.task_ids or [])
    purge_ids = list(getattr(args, "purge_ids", None) or [])
    if ids and purge_ids:
        return _err("请选择要归档的 task_ids，或用 --rm 指定已归档的 task_ids")
    if not ids and not purge_ids:
        return _err("at least one task_id is required")
    with kbc.connect_closing() as conn:
        if purge_ids:
            return _bulk_apply(purge_ids, lambda tid: kb.delete_archived_task(conn, tid), lambda tid: f"已删除 {tid}",
                               lambda tid: f"无法删除 {tid}（必须已归档）")
        return _bulk_apply(ids, lambda tid: kb.archive_task(conn, tid),
                           lambda tid: f"已归档 {tid}", lambda tid: f"无法归档 {tid}")


def _cmd_stats(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        stats = kb.board_stats(conn)
    if _json_out(args, stats):
        return 0
    print("按状态：")
    for k in ("triage", "todo", "scheduled", "ready", "running", "blocked", "done"):
        print(f"  {k:8s}  {stats['by_status'].get(k, 0)}")
    if stats["by_assignee"]:
        print("\n按 assignee：")
        for who, counts in sorted(stats["by_assignee"].items()):
            print(f"  {who:20s}  {_fmt_counts(counts)}")
    age = stats["oldest_ready_age_seconds"]
    if age is not None:
        print(f"\n最早的 ready 任务已等待：{int(age)}s")
    return 0


def _cmd_notify_subscribe(args: argparse.Namespace) -> int:
    delivery_metadata = {
        key: value
        for key, value in (
            ("parent_chat_id", getattr(args, "parent_chat_id", None)),
            ("guild_id", getattr(args, "guild_id", None)),
        )
        if value
    }
    with kbc.connect_closing() as conn:
        if kb.get_task(conn, args.task_id) is None:
            return _err(f"无此任务：{args.task_id}")
        kbn.add_notify_sub(
            conn, task_id=args.task_id, platform=args.platform, chat_id=args.chat_id,
            chat_type=args.chat_type, thread_id=args.thread_id, user_id=args.user_id,
            user_id_alt=getattr(args, "user_id_alt", None),
            notifier_profile=args.notifier_profile or _profile_author(),
            delivery_mode=getattr(args, "delivery_mode", None),
            delivery_metadata=delivery_metadata or None,
        )
    print(f"已订阅 {args.platform}:{args.chat_id}" + (f":{args.thread_id}" if args.thread_id else "")
          + f" to {args.task_id}")
    return 0


def _cmd_notify_list(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        subs = kbn.list_notify_subs(conn, args.task_id)
    if _json_out(args, subs):
        return 0
    if not subs:
        print("（无订阅）")
        return 0
    for s in subs:
        thr = f":{s['thread_id']}" if s.get("thread_id") else ""
        dmode, ctype = s.get("delivery_mode") or "notify", s.get("chat_type") or "dm"
        extras = "".join((
            f"  owner={s['notifier_profile']}" if s.get("notifier_profile") else "",
            "" if ctype == "dm" else f"  chat_type={ctype}",
            f"  user_id_alt={s['user_id_alt']}" if s.get("user_id_alt") else "",
            "" if dmode == "notify" else f"  mode={dmode}",
        ))
        print(f"  {s['task_id']:10s}  {s['platform']}:{s['chat_id']}{thr}  （自事件 {s['last_event_id']} 起）{extras}")
    return 0


def _cmd_notify_unsubscribe(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        ok = kbn.remove_notify_sub(conn, task_id=args.task_id, platform=args.platform, chat_id=args.chat_id,
                                  thread_id=args.thread_id)
    return _ok_or_err(ok, "(无此订阅)", f"已取消订阅 {args.task_id}")


def _cmd_log(args: argparse.Namespace) -> int:
    content = kb.read_worker_log(args.task_id, tail_bytes=args.tail)
    if content is None:
        return _err(f"(没有 {args.task_id} 的日志——任务可能尚未启动)")
    sys.stdout.write(content)
    if not content.endswith("\n"):
        sys.stdout.write("\n")
    return 0


def _cmd_runs(args: argparse.Namespace) -> int:
    """Show attempt history for a task."""
    rsk, rc = _run_state_kwargs(args, "runs")
    if rc:
        return rc
    with kbc.connect_closing() as conn:
        runs = kb.list_runs(conn, args.task_id, **rsk)
    if _json_out(args, [_obj_dict(r, _RUNS_RUN_FIELDS) for r in runs]):
        return 0
    if not runs:
        print(f"（{args.task_id} 尚无运行记录）")
        return 0
    print(f"{'#':3s}  {'结果':12s}  {'PROFILE':16s}  {'耗时':>8s}  开始时间")
    for i, r in enumerate(runs, 1):
        end = r.ended_at or int(time.time())
        # Clamp to 0 so NTP backward-jumps don't print negative durations.
        elapsed = max(0, end - r.started_at)
        el = f"{elapsed}s" if elapsed < 60 else f"{elapsed // 60}m" if elapsed < 3600 else f"{elapsed / 3600:.1f}h"
        outcome = r.outcome or ("(运行中)" if not r.ended_at else r.status)
        print(f"{i:3d}  {outcome:12s}  {(r.profile or '-'):16s}  {el:>8s}  {_fmt_ts(r.started_at)}")
        if r.summary:
            print(f"     → {r.summary.splitlines()[0][:100]}")
        if r.error:
            print(f"     ✖ {r.error[:100]}")
    return 0


def _cmd_context(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        text = kb.build_worker_context(conn, args.task_id)
    print(text)
    return 0


def _run_triage_sweep(args: argparse.Namespace, verb: str, mod, run_one, json_key: str,
                      json_fields: tuple[str, ...], human_ok) -> int:
    """Shared driver for ``specify`` / ``decompose``: validate ids (one task id XOR ``--all``), run
    ``run_one(tid, author=...)`` per id, print JSON or human lines, exit code."""
    all_flag = bool(getattr(args, "all_triage", False))
    author = getattr(args, "author", None) or _profile_author()
    want_json = bool(getattr(args, "json", False))
    tenant = getattr(args, "tenant", None)
    if args.task_id and all_flag:
        return _err("kanban: 请传入一个 task id 或 --all，二者只能选其一", 2)
    if all_flag:
        ids = mod.list_triage_ids(tenant=tenant)
        if not ids:
            if want_json:
                print(json.dumps({json_key: 0, "total": 0}))
            else:
                print("没有 triage 任务" + (f"（tenant {tenant!r}）" if tenant else "") + "。")
            return 0
    elif args.task_id:
        ids = [args.task_id]
    else:
        return _err(f"kanban: {verb} 需要一个 task id 或 --all", 2)

    ok_count = 0
    for tid in ids:
        outcome = run_one(tid, author=author)
        if outcome.ok:
            ok_count += 1
        if want_json:
            print(json.dumps(_obj_dict(outcome, json_fields)))
        elif outcome.ok:
            print(human_ok(outcome))
        else:
            print(f"kanban: {verb} {outcome.task_id}：{outcome.reason}", file=sys.stderr)
    if not all_flag:
        return 0 if ok_count == 1 else 1
    # --all: exit 1 only when every candidate failed (honest signal for scripts).
    return 0 if (ok_count > 0 or not ids) else 1


def _retitled_suffix(outcome) -> str:
    return f"——已改标题：{outcome.new_title!r}" if outcome.new_title else ""


def _cmd_specify(args: argparse.Namespace) -> int:
    """Spec a triage task (or all) via the auxiliary LLM, promote to todo."""
    from hermes_cli import kanban_specify as spec

    return _run_triage_sweep(args, "specify", spec, spec.specify_task, "specified",
                             ("task_id", "ok", "reason", "new_title"),
                             lambda o: f"已细化 {o.task_id} → todo{_retitled_suffix(o)}")


def _decompose_ok_line(o) -> str:
    if o.fanout and o.child_ids:
        return (f"已分解 {o.task_id} → {len(o.child_ids)} "
                f"个子任务（{', '.join(o.child_ids)}）；根任务已提升为 todo")
    return f"已细化 {o.task_id} → todo（无扇出）{_retitled_suffix(o)}"


def _cmd_decompose(args: argparse.Namespace) -> int:
    """Fan a triage task (or all) out into child tasks via the auxiliary LLM."""
    from hermes_cli import kanban_decompose as decomp

    return _run_triage_sweep(args, "decompose", decomp, decomp.decompose_task, "decomposed",
                             ("task_id", "ok", "reason", "fanout", "child_ids", "new_title"), _decompose_ok_line)


_HANDLERS = {
    "init": _cmd_init, "create": _cmd_create, "swarm": _cmd_swarm,
    "list": _cmd_list, "ls": _cmd_list, "show": _cmd_show,
    "assign": _cmd_assign, "set-model": _cmd_set_model,
    "reclaim": _cmd_reclaim, "reassign": _cmd_reassign,
    "diagnostics": _cmd_diagnostics, "diag": _cmd_diagnostics,
    "link": _cmd_link, "unlink": _cmd_unlink, "claim": _cmd_claim,
    "comment": _cmd_comment, "attach": _cmd_attach,
    "attachments": _cmd_attachments, "attach-rm": _cmd_attach_rm,
    "complete": _cmd_complete, "edit": _cmd_edit, "block": _cmd_block,
    "schedule": _cmd_schedule, "unblock": _cmd_unblock,
    "request-review": _cmd_request_review, "request-changes": _cmd_request_changes,
    "reopen-review": _cmd_reopen_review, "promote": _cmd_promote,
    "archive": _cmd_archive, "tail": _cmd_tail, "dispatch": _cmd_dispatch,
    "daemon": _cmd_daemon, "watch": _cmd_watch, "stats": _cmd_stats,
    "log": _cmd_log, "runs": _cmd_runs, "heartbeat": _cmd_heartbeat,
    "assignees": _cmd_assignees, "notify-subscribe": _cmd_notify_subscribe,
    "notify-list": _cmd_notify_list, "notify-unsubscribe": _cmd_notify_unsubscribe,
    "context": _cmd_context, "specify": _cmd_specify, "decompose": _cmd_decompose,
    "gc": _cmd_gc,
}


# --- Slash-command entry point (used by /kanban from CLI and gateway) ---

_SLASH_KANBAN_HELP = """\
**/kanban** —— 管理共享任务看板。

常用子命令：
  `list`（别名 `ls`）   列出当前看板上的任务
  `show <id>`           任务详情 + 评论 + 事件
  `stats`               按状态 / 按 assignee 计数
  `create <title>…`     创建任务（自动为你订阅事件）
  `comment <id> <msg>`  追加评论
  `attach <id> <path>`  附加本地文件；`attachments <id>` 列出
  `complete <id>…`      将任务标记为完成
  `request-review <id>` 进入正式评审；`request-changes <id> <reason>` 将进行中的评审退回给实现者
  `block <id> [reason]` 标记阻塞；`schedule <id> [reason]` 暂存延时工作；`unblock <id>` 复活
  `assign <id> <profile>`  重新分配
  `boards list`         显示所有看板
  `assignees`           已知 profile + 计数
  `context <id>`        完整的工作节点上下文转储
  `runs <id>`           尝试历史
  `log <id>`            工作节点日志

运行 `/kanban <subcommand> -h` 查看参数。 \
只读命令在代理运行期间也可安全使用。\
"""


def run_slash(rest: str) -> str:
    """Execute a ``/kanban …`` string (``rest`` = everything after ``/kanban``) and return captured
    stdout/stderr. Shared by the interactive CLI and the gateway so formatting is identical."""
    import io

    tokens = shlex.split(rest) if rest and rest.strip() else []
    # Bare ``/kanban`` / ``help`` / ``-h``: curated short block, not argparse's full tree (garbage
    # in a chat bubble). ``/kanban foo -h`` still works.
    if not tokens or tokens[0] in {"help", "--help", "-h", "?"}:
        return _SLASH_KANBAN_HELP
    # build_parser() needs a subparsers action to attach to: build a throwaway one and drive
    # kanban_parser directly so usage/error text reads ``/kanban``.
    _wrap = argparse.ArgumentParser(prog="/kanban-wrap", add_help=False)
    _wrap.exit_on_error = False  # type: ignore[attr-defined]
    kanban_parser = build_parser(_wrap.add_subparsers(dest="_top"))
    kanban_parser.prog = "/kanban"
    kanban_parser.exit_on_error = False  # type: ignore[attr-defined]
    subparsers = [a for a in kanban_parser._actions if isinstance(a, argparse._SubParsersAction)]
    for _action in subparsers:
        for _name, _choice in _action.choices.items():
            _choice.prog = f"/kanban {_name}"
            _choice.exit_on_error = False  # type: ignore[attr-defined]

    def _usage_for_error() -> str:
        if tokens:
            for _action in subparsers:
                subparser = _action.choices.get(tokens[0])
                if subparser is not None:
                    return subparser.format_usage().rstrip()
        return kanban_parser.format_usage().rstrip()

    buf_out, buf_err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            args = kanban_parser.parse_args(tokens)
    except SystemExit as exc:
        out, err = buf_out.getvalue().rstrip(), buf_err.getvalue().rstrip()
        if exc.code in {0, None} and out:  # ``-h`` help dump
            return out
        body = err or out
        return f"⚠ /kanban 用法错误\n{body}" if body else "⚠ /kanban 用法错误"
    except argparse.ArgumentError as exc:
        return f"⚠ /kanban 用法错误\n{_usage_for_error()}\n{exc}"

    with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
        try:
            kanban_command(args)
        except SystemExit:
            pass
        except Exception as exc:
            print(f"错误：{exc}", file=sys.stderr)

    out, err = buf_out.getvalue().rstrip(), buf_err.getvalue().rstrip()
    if err and out:
        return f"{out}\n{err}"
    return err if err else (out or "(无输出)")


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
from typing import Any  # noqa: F401,E402
# ---- END PLUGIN-COMPAT ----
