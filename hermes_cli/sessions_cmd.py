"""``hermes sessions`` command.

``cmd_sessions`` routes ``args.sessions_action`` through ``_PRE_DB_HANDLERS`` (repair / recover /
import — must run without opening ``SessionDB()``, which a malformed schema prevents) and
``_DB_HANDLERS`` (everything else, sharing one ``SessionDB``). ``get_hermes_home`` is resolved through
``hermes_cli.main`` at call time so monkeypatches keep working. Picker: :mod:`hermes_cli.sessions_cmd_browse`.
"""

import json
import os
import shutil
import sqlite3
import sys
from functools import partial
from pathlib import Path

from hermes_cli.cli_output import print_truncated
from hermes_cli.sessions_cmd_browse import _relative_time, _session_browse_picker
from hermes_cli.timefmt import pad_display as _pad_display


def get_hermes_home():
    from hermes_cli import main
    return main.get_hermes_home()


def _sessions_dir() -> Path:
    return get_hermes_home() / "sessions"


def _size_mb(path) -> float:
    return os.path.getsize(path) / (1024 * 1024) if path.exists() else 0.0


def _size_delta_label(saved_mb: float) -> str:
    """A negative delta means the file GREW (concurrent writes during a long optimize); "reclaimed
    -163.0 MB" reads as data loss, so say "grew by"."""
    return f"已回收 {saved_mb:.1f} MB" if saved_mb >= 0 else f"增长 {-saved_mb:.1f} MB"


def _confirm_prompt(prompt: str) -> bool:
    """Prompt for y/N confirmation, safe against non-TTY environments."""
    try:
        return input(prompt).strip().lower() in {"y", "yes"}
    except (EOFError, KeyboardInterrupt):
        return False


def _not_found(session_id) -> int:
    print(f"No session '{session_id}'. Run: hermes sessions list to find the id.")
    return 1


def _print_dry_run_preview(candidates, filters) -> None:
    from hermes_cli.session_filters import describe_filters
    print(f"将导出 {len(candidates)} 个会话（{describe_filters(filters)}）。")
    for row in candidates[:100]:
        print(f"  {row.get('id')}  {row.get('source', '')}")
    if len(candidates) > 100:
        print_truncated(len(candidates) - 100)


_FILTER_ARGS = (
    "older_than", "newer_than", "before", "after", "source", "title", "end_reason", "cwd", "min_messages",
    "max_messages", "model", "provider", "user", "chat_id", "chat_type", "branch", "min_tokens", "max_tokens",
    "min_cost", "max_cost", "min_tool_calls", "max_tool_calls",
)


def _any_filter_args(args) -> bool:
    return any(getattr(args, a, None) is not None for a in _FILTER_ARGS)


def _export_dir(output) -> Path:
    """``--output`` dir for multi-file exports; ``~/.hermes/session-exports`` when empty or ``-``."""
    return Path(output).expanduser() if output and output != "-" else get_hermes_home() / "session-exports"


def _output_file_in_dir(output, default_name: str):
    """Single-file exports accept a directory too (``--help`` calls the positional a path, and md/qmd take
    one): an existing directory, or one spelled with a trailing separator, means ``<dir>/<default_name>``."""
    if not output or output == "-":
        return output
    if output.endswith(("/", os.sep)) or os.path.isdir(output):
        os.makedirs(output, exist_ok=True)
        return os.path.join(output, default_name)
    return output


def _write_output(output, text, summary) -> None:
    """Write to stdout when *output* is empty or ``-``; else to the file + print *summary*."""
    if not output or output == "-":
        sys.stdout.write(text)
        return
    with open(output, "w", encoding="utf-8") as f:
        f.write(text)
    print(summary)


# -- handlers that must run BEFORE SessionDB() is opened ----------------------

def _cmd_repair(args):
    from hermes_state import DEFAULT_DB_PATH as db_path, SessionDB
    from hermes_state_repair import _db_opens_cleanly, repair_state_db_schema
    if not db_path.exists():
        print(f"No session database at {db_path} (nothing to repair).")
        return
    reason = _db_opens_cleanly(db_path)
    if reason is None:
        print(f"✓ {db_path} opens cleanly — no repair needed.")
        return
    print(f"✗ {db_path} does not open cleanly: {reason}")
    if getattr(args, "check_only", False):
        return 1
    print("Repairing (a backup copy is made first)…")
    report = repair_state_db_schema(db_path, backup=not getattr(args, "no_backup", False))
    if report.get("repaired"):
        if report.get("backup_path"):
            print(f"  backup: {report['backup_path']}")
        print(f"  strategy: {report.get('strategy')}")
        try:
            with SessionDB() as _repair_db:
                n = _repair_db._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            print(f"✓ Repaired — {n} sessions recovered.")
        except Exception:
            print("✓ Repaired.")
        return
    print(f"✗ Repair failed: {report.get('error')}")
    if report.get("backup_path"):
        print(f"  A backup is preserved at: {report['backup_path']}")
    # Without this pointer the user is at a dead end; lead with --inspect-only before writing.
    source_hint = report.get("backup_path") or db_path
    print(
        "  Keep state.db and the backup; do not delete them.\n"
        "\n  Next step — offline recovery (never modifies the source):\n"
        f"    hermes sessions recover --source {source_hint} \\\n"
        "        --inspect-only\n"
        "  If that reports the data is recoverable, rebuild it into\n"
        "  a NEW database (the active one is left untouched):\n"
        f"    hermes sessions recover --source {source_hint} \\\n"
        "        --output recovered-state.db"
    )


def _cmd_recover(args):
    """Offline recovery: works on a disposable copy of the source; never touches the active database."""
    import sqlite3
    from hermes_cli.session_recovery import (
        SessionRecoveryError, inspect_session_database, recover_session_database, write_recovery_report,
    )
    source, output = args.source, getattr(args, "output", None)
    inspect_only = bool(getattr(args, "inspect_only", False))
    allow_partial = bool(getattr(args, "allow_partial", False))
    report_path = getattr(args, "report", None)
    if not inspect_only and output is not None and report_path is None:
        report_path = output.with_name(output.name + ".recovery.json")
    usage_errors = (
        (inspect_only and output is not None, "--output 不能与 --inspect-only 一起使用。"),
        (inspect_only and allow_partial, "--allow-partial 不能与 --inspect-only 一起使用。"),
        (not inspect_only and output is None, "除非使用 --inspect-only，否则必须提供 --output。"),
        (
            report_path is not None and os.path.lexists(report_path.expanduser()),
            f"拒绝覆盖已存在的报告：{report_path}",
        ),
    )
    for bad, msg in usage_errors:
        if bad:
            print(f"错误：{msg}")
            return 2
    work_dir = getattr(args, "work_dir", None)
    try:
        if inspect_only:
            report = inspect_session_database(source, work_dir=work_dir)
        else:
            print("正在将规范会话数据恢复到新数据库……")
            progress = _RecoveryProgress()
            report = recover_session_database(
                source, output, work_dir=work_dir, chunk_size=getattr(args, "chunk_size", 1000),
                progress_cb=progress, allow_partial=allow_partial,
            )
            progress.finish()
    except (SessionRecoveryError, OSError, sqlite3.DatabaseError) as exc:
        print(f"错误：会话恢复失败：{exc}\n提供的源数据库未被替换或删除。")
        return 1
    if report_path is None:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        try:
            print(f"恢复报告：{write_recovery_report(report_path, report)}")
        except (FileExistsError, OSError) as exc:
            print(f"错误：无法写入恢复报告：{exc}")
            return 1
    if inspect_only:
        return 0 if report.get("recoverable") else 1
    return _print_recovery_verdict(report, output, allow_partial)


class _RecoveryProgress:
    """`recover` progress printer: one live-updating ``  <table>: n/total`` line per table."""

    table = None

    def __call__(self, info):
        table = info.get("table")
        if table != self.table:
            self.finish()
            print(f"  {table}: ", end="", flush=True)
            self.table = table
        total = info.get("source_rows")
        suffix = f"/{int(total):,}" if total is not None else ""
        print(f"\r  {table}: {int(info.get('copied_rows') or 0):,}{suffix}", end="", flush=True)

    def finish(self):
        if self.table is not None:
            print()


def _print_recovery_verdict(report, output, allow_partial) -> int:
    if report.get("complete"):
        print(
            f"✓ 已恢复的数据库校验通过：{output}\n"
            "  当前会话数据库未被改动。\n"
            "  安装此数据库前，请先查看 JSON 报告。"
        )
        return 0
    if allow_partial and report.get("verified"):
        counts = report.get("verification", {}).get("table_counts", {})
        if report.get("best_effort"):
            print(
                f"✓ 尽力而为的页级抢救已校验通过：{output}\n"
                "  源表结构不可读；行已从原始页"
                "通过 sqlite3 .recover 重建，并做了启发式映射。"
            )
        else:
            print(f"✓ 部分恢复输出校验通过：{output}")
        sessions_n, messages_n = int(counts.get("sessions") or 0), int(counts.get("messages") or 0)
        print(
            f"  已恢复 {sessions_n:,} 个会话和 {messages_n:,} 条消息。\n"
            "  当前会话数据库未被改动。\n"
            "  此输出并不完整。安装前请查看 JSON 报告中的"
            "每一段跳过区间和孤儿计数。"
        )
        return 0
    print(
        "✗ 恢复输出未通过全部校验。\n"
        "  不要安装它。请检查 JSON 报告，确认是否存在部分数据或错误。"
    )
    return 1


def _cmd_import(args):
    from hermes_cli.foreign_sessions import run_sessions_import
    # Explicit path but nothing imported -> non-zero for scripts. Picker cancel (no path) -> exit 0.
    if run_sessions_import(args) is None and getattr(args, "path", None):
        return 1


# -- handlers that receive an open SessionDB ----------------------------------

def _default_exclude(args):
    """Hide third-party tool sessions by default, but honour explicit --source."""
    return None if getattr(args, "source", None) else ["tool"]


def _cmd_list(db, args):
    from hermes_state_sessions import workspace_key as _ws_key
    # LIMIT lives in the query, so probe one row past the cap: it is the only way to know the
    # page was cut without a second COUNT query (``--limit 0`` is ``LIMIT 0``: no rows, no probe).
    limit = args.limit
    sessions = db.list_sessions_rich(
        source=args.source, exclude_sources=_default_exclude(args), limit=limit + 1 if limit > 0 else limit,
    )
    truncated = limit > 0 and len(sessions) > limit
    sessions = sessions[:limit] if truncated else sessions

    # Workspace filter: workspace key (git repo root, else cwd) — path substring or exact basename.
    _ws_filter = (getattr(args, "workspace", None) or "").strip()
    if _ws_filter:
        _needle = _ws_filter.lower()
        keyed = ((s, (_ws_key(s) or "").lower()) for s in sessions)
        sessions = [
            s for s, key in keyed if key and (_needle in key or _needle == os.path.basename(key.rstrip("/\\")))
        ]
    if not sessions:
        print("未找到任何会话。")
        return

    # Workspace column only when some session carries a key (or when filtering): unbound listings read as before.
    has_ws = bool(_ws_filter) or any(_ws_key(s) for s in sessions)
    has_titles = any(s.get("title") for s in sessions)

    def _ws(s):  # repo/dir basename, "—" when unbound
        key = _ws_key(s)
        return ((os.path.basename(key.rstrip("/\\")) or key) if key else "—")[:16]
    _title = lambda s, n: (s.get("title") or "—")[:n]  # noqa: E731
    _preview = lambda s, n: s.get("preview", "")[:n]  # noqa: E731
    _ago = lambda s: _relative_time(s.get("last_active"), session_id=s["id"])  # noqa: E731
    layouts = {  # (has_ws, has_titles): header, rule width, row formatter
        (True, True): (f"{'Title':<28} {'Workspace':<18} {'Last Active':<13} {'ID'}", 110,
                       lambda s: f"{_title(s, 26):<28} {_ws(s):<18} {_ago(s):<13} {s['id']}"),
        (True, False): (f"{'Preview':<38} {'Workspace':<18} {'Last Active':<13} {'Src':<6} {'ID'}", 100,
                        lambda s: f"{_preview(s, 36):<38} {_ws(s):<18} {_ago(s):<13} {s['source']:<6} {s['id']}"),
        (False, True): (f"{'Title':<32} {'Preview':<40} {'Last Active':<13} {'ID'}", 110,
                        lambda s: f"{_title(s, 30):<32} {_preview(s, 38):<40} {_ago(s):<13} {s['id']}"),
        (False, False): (f"{'Preview':<50} {'Last Active':<13} {'Src':<6} {'ID'}", 95,
                         lambda s: f"{_preview(s, 48):<50} {_ago(s):<13} {s['source']:<6} {s['id']}"),
    }
    header, rule, fmt = layouts[(has_ws, has_titles)]
    print(header + "\n" + "─" * rule)
    for s in sessions:
        print(fmt(s))
    if truncated:
        print_truncated(None, f"use --limit {limit * 2} to see more")


# -- export -----------------------------------------------------------------

def _cmd_export(db, args):
    from hermes_cli.session_filters import build_prune_filters
    filters = None
    if _any_filter_args(args):
        try:
            filters = build_prune_filters(args)
        except ValueError as e:
            print(f"错误：{e}")
            return
        # Unlike prune/archive, export includes archived sessions.
        filters["archived"] = None

    def _redact(data):
        if not args.redact or data is None:
            return data
        from hermes_cli.session_export_md import redact_session_data
        return redact_session_data(data)

    def _collect_sessions():
        """--session-id / filters / bare export -> redacted session dicts, or None after printing an error."""
        if args.session_id:
            resolved = db.resolve_session_id(args.session_id)
            data = _redact(db.export_session(resolved)) if resolved else None
            if not data:
                _not_found(args.session_id)
                return None
            return [data]
        if filters:
            candidates = db.list_prune_candidates(**filters)
            if args.dry_run:
                return _print_dry_run_preview(candidates, filters)
            return [s for s in (_redact(db.export_session(row["id"])) for row in candidates) if s]
        if args.dry_run:
            return print("--dry-run 至少需要一个筛选条件。")
        return [_redact(s) for s in db.export_all(source=None)]
    if getattr(args, "only", None):
        return _export_flat("only", args, _collect_sessions)
    if args.format == "trace":
        return _export_trace(db, args, filters)
    if args.format in _FLAT_EXPORTERS:
        return _export_flat(args.format, args, _collect_sessions)
    return _export_markdown(db, args, filters, _redact)


def _render_only(args, sessions):
    """--only user-prompts: one prompt record per line (jsonl) or headed sections (md)."""
    from hermes_cli.session_export import export_record_count, render_sessions_export
    rendered = render_sessions_export(sessions, fmt="markdown" if args.format == "md" else "jsonl", only=args.only)
    count, noun = export_record_count(sessions, only=args.only)
    unit = "条用户提示词" if noun == "prompt" else "个会话"
    return rendered, f"已导出 {count} {unit} 到 {args.output}"


def _render_html(args, sessions):
    """One self-contained file (single session, or multi-session with sidebar)."""
    from hermes_cli.session_export_html import generate_html_export, generate_multi_session_html_export
    single = len(sessions) == 1
    content = generate_html_export(sessions[0]) if single else generate_multi_session_html_export(sessions)
    return content, f"已导出 {len(sessions)} 个会话到 {args.output}（HTML）"


def _render_jsonl(args, sessions):
    lines = "".join(json.dumps(s, ensure_ascii=False) + "\n" for s in sessions)
    return lines, f"已导出 {len(sessions)} 个会话到 {args.output}"


#: kind -> (usage error when the --output/--format combination is unusable, renderer)
_FLAT_EXPORTERS = {
    "only": (
        lambda a: a.format not in ("jsonl", "md"), "--only user-prompts 仅支持 --format jsonl 或 md。", _render_only
    ),
    "html": (lambda a: not a.output or a.output == "-", "HTML 导出需要指定输出文件路径。", _render_html),
    "jsonl": (lambda a: not a.output, "JSONL 导出需要指定输出路径（用 - 表示标准输出）。", _render_jsonl),
}


def _export_flat(kind, args, collect):
    """Single-file export: validate the output target, collect sessions, render, write."""
    unusable, message, render = _FLAT_EXPORTERS[kind]
    if unusable(args):
        print(message)
        return
    sessions = collect()
    if sessions is not None:
        from hermes_cli.session_export import default_save_filename
        name = (default_save_filename(sessions[0].get("id", ""), args.format) if len(sessions) == 1
                else f"hermes_sessions.{args.format}")
        args.output = _output_file_in_dir(args.output, name)
        _write_output(args.output, *render(args, sessions))


def _export_trace(db, args, filters):
    """Claude Code JSONL trace export — local file or HF upload. Redaction is ON by default (traces
    leave the machine with --upload); --no-redact opts out."""
    session_id = args.session_id
    if not session_id and not filters:  # "the last thing I did"
        rows = db.list_sessions_rich(limit=1, order_by_last_active=True)
        session_id = rows[0].get("id") if rows else None
        if not session_id:
            print("没有可导出的会话。请传入 --session-id。")
            return
    if session_id and not db.resolve_session_id(session_id):
        _not_found(session_id)
        return
    from agent.trace_upload import TraceRedactionError, build_trace_jsonl, upload_session_trace
    redact_trace = not getattr(args, "no_redact", False)
    if getattr(args, "upload", False):
        if not session_id:
            print("--upload 每次导出一个会话：请传入 --session-id（或去掉筛选条件以使用最近的会话）。")
            return
        resolved = db.resolve_session_id(session_id)
        db.close()
        print(upload_session_trace(resolved, cwd="", redact=redact_trace, private=not getattr(args, "public", False)))
        return
    if session_id:
        ids = [db.resolve_session_id(session_id)]
    else:
        candidates = db.list_prune_candidates(**filters)
        if args.dry_run:
            return _print_dry_run_preview(candidates, filters)
        ids = [row["id"] for row in candidates]

    def _render_trace(sid):
        meta = db.get_session(sid) or {}
        messages = db.get_messages_as_conversation(sid)
        if not messages:
            return None
        return build_trace_jsonl(messages, session_id=sid, model=meta.get("model") or "", cwd="", redact=redact_trace)
    try:
        if len(ids) == 1:
            jsonl = _render_trace(ids[0])
            if not jsonl:
                print(f"会话 '{ids[0]}'.")
                return
            args.output = _output_file_in_dir(args.output, f"{ids[0]}.trace.jsonl")
            _write_output(args.output, jsonl, f"Exported 1 session trace to {args.output}")
        else:
            out_dir = _export_dir(args.output)
            out_dir.mkdir(parents=True, exist_ok=True)
            exported = 0
            for sid in ids:
                jsonl = _render_trace(sid)
                if jsonl:
                    (out_dir / f"{sid}.trace.jsonl").write_text(jsonl, encoding="utf-8")
                    exported += 1
            print(f"已导出 {exported} 个会话追踪到 {out_dir}")
    except TraceRedactionError:
        print("脱敏失败；拒绝导出未脱敏的追踪内容。")


def _export_markdown(db, args, filters, redact):
    """Markdown / QMD export: one file per session plus a manifest entry."""
    from hermes_cli.session_export_md import append_manifest_entry, write_session_markdown
    if args.output == "-":
        print("Markdown/QMD 导出会写入文件；标准输出（-）仅在 --format jsonl 时支持。")
        return
    output_dir = _export_dir(args.output)

    def _export_one(session_id: str, *, include_lineage: bool = False):
        data = db.export_session_lineage(session_id) if include_lineage else db.export_session(session_id)
        if not data:
            return None, None
        data = redact(data)
        path = write_session_markdown(data, output_dir, fmt=args.format, force=args.force)
        append_manifest_entry(output_dir, data, path, fmt=args.format)
        return data, path
    if args.delete_after_verified and not args.yes:
        print("--delete-after-verified 需要配合 --yes。")
        return
    if args.delete_after_verified and not args.session_id:
        print("--delete-after-verified 仅在配合 --session-id 时支持。")
        return
    lineage_is_logical = getattr(args, "lineage", "single") == "logical"
    if args.session_id:
        return _export_markdown_single(db, args, _export_one, output_dir, lineage_is_logical)
    if not filters:
        print("拒绝在没有筛选条件的情况下批量导出。请传入 --session-id 或 "
              "至少一个筛选条件（例如 --older-than 90、--source telegram）。")
        return
    candidates = db.list_prune_candidates(**filters)
    if args.dry_run:
        return _print_dry_run_preview(candidates, filters)
    exported = 0
    for row in candidates:
        try:
            data, exported_path = _export_one(row["id"], include_lineage=lineage_is_logical)
        except FileExistsError as e:
            print(f"跳过已存在的导出：{e}。传入 --force 可覆盖。")
            continue
        if data and exported_path:
            exported += 1
    print(f"已导出 {exported} 个会话到 {output_dir}")


def _export_markdown_single(db, args, export_one, output_dir, lineage_is_logical):
    """--session-id markdown export, optionally + verified delete of it and its delegates."""
    from hermes_cli.session_export_md import verify_export_file
    resolved_session_id = db.resolve_session_id(args.session_id)
    if not resolved_session_id:
        _not_found(args.session_id)
        return
    delete_target_ids = (
        db.get_session_delete_targets(resolved_session_id) if args.delete_after_verified else [resolved_session_id]
    )
    exported_items = []
    for target_id in delete_target_ids:
        try:
            data, exported_path = export_one(
                target_id, include_lineage=(target_id == resolved_session_id and lineage_is_logical),
            )
        except FileExistsError as e:
            print(f"导出已存在：{e}。传入 --force 可覆盖。")
            return
        if not data or not exported_path:
            print(f"会话 '{target_id}' 在导出过程中消失；未删除任何内容。")
            return
        exported_items.append((data, exported_path))
    message_count = sum(len(data.get("messages") or []) for data, _path in exported_items)
    n = len(exported_items)
    print(f"已导出 {n} 个会话（{message_count} 条消息）"
          f"到 {exported_items[0][1] if n == 1 else output_dir}")
    if not args.delete_after_verified:
        return
    for data, exported_path in exported_items:
        ok, reason = verify_export_file(exported_path, data)
        if not ok:
            print(f"导出校验失败；不删除会话 '{data.get('id')}'：{reason}")
            return
    if not db.delete_session(
        resolved_session_id, sessions_dir=_sessions_dir(), expected_delete_ids=delete_target_ids
    ):
        print(f"已导出，但会话 '{resolved_session_id}' 未被删除，因为其委托集已变化。")
        return
    delegates = len(delete_target_ids) - 1
    delegate_suffix = f"，以及 {delegates} 个委托会话" if delegates else ""
    print(f"已删除导出会话 '{resolved_session_id}'{delegate_suffix}。")


# -- delete / prune / archive -------------------------------------------------

def _cmd_delete(db, args):
    resolved_session_id = db.resolve_session_id(args.session_id)
    if not resolved_session_id:
        return _not_found(args.session_id)
    # The delete is honored (explicit id), but a pin is a "keep" flag: say so instead of silently destroying it.
    _pinned_note = "（此会话已被固定）" if (db.get_session(resolved_session_id) or {}).get("pinned") else ""
    if not args.yes:
        if not _confirm_prompt(f"删除会话 '{resolved_session_id}'{_pinned_note} 及其所有消息？[y/N] "):
            print("已取消。")
            return
    elif _pinned_note:
        print(f"警告：正在删除已固定的会话 '{resolved_session_id}'。")
    if not db.delete_session(resolved_session_id, sessions_dir=_sessions_dir()):
        return _not_found(args.session_id)
    print(f"已删除会话 '{resolved_session_id}'。")


#: Age floor for `prune --never-active`; generous: a young never-active row may be a chat nobody replied to yet.
_NEVER_ACTIVE_DEFAULT_DAYS = 30.0


def _prune_never_active_keyed(db, args):
    """`prune --never-active`: drop keyed gateway rows opened and never used (mostly escaped test
    fixtures). Separate from the shared prune/archive selector, which is pinned to `ended_at IS NOT
    NULL` — never-closed rows sit outside it by construction.

    The population is dominated by escaped test fixtures (#82770), which the hermetic-isolation guard can
    only stop from being *created* — rows already written to a developer's state.db need a sweep to leave.
    """
    from hermes_cli.session_filters import format_epoch, parse_duration_seconds
    older_than = getattr(args, "older_than", None)
    days = _NEVER_ACTIVE_DEFAULT_DAYS
    if older_than is not None:
        seconds = parse_duration_seconds(str(older_than))
        if seconds is None:
            print(f"错误：--older-than '{older_than}' 不是合法时长。"
                  "请使用天数（纯数字），或用 '2d' / '1w' 这样的形式。")
            return
        days = seconds / 86400.0
    candidates = db.list_never_active_keyed_sessions(older_than_days=days)
    if not candidates:
        print(f"没有早于 {days:g} 天的从未活跃的带键会话。")
        return
    shown = candidates if args.dry_run else candidates[:15]
    print(f"{len(candidates)} 个早于 {days:g} 天的从未活跃的带键会话 "
          "— 没有消息、token、工具调用或标题：")
    for s in shown:
        print(f"  {s['id']}  {format_epoch(s.get('started_at')):<17} {(s.get('source') or '-'):<10} "
              f"{s.get('session_key') or '-'}")
    if len(candidates) > len(shown):
        print_truncated(len(candidates) - len(shown))
    if args.dry_run:
        print("试运行 — 未删除任何内容。")
        return
    if not args.yes and not _confirm_prompt(f"删除 {len(candidates)} 个会话？[y/N] "):
        print("已中止。")
        return
    deleted, routing_deleted = db.prune_never_active_keyed_sessions(
        older_than_days=days, sessions_dir=_sessions_dir()
    )
    print(f"已删除 {deleted} 个从未活跃的会话和 {routing_deleted} 条过期路由记录。")


def _note_pinned_skipped(db, filters, action):
    """Tell the user how many pinned rows bulk prune/archive spared (pin = durable keep; only
    `prune --include-pinned` opts in, archive always spares them)."""
    _base = {k: v for k, v in filters.items() if k != "include_pinned"}
    with_pinned, without = (int(db.count_prune_matches(**_base, include_pinned=flag)) for flag in (True, False))
    skipped = max(with_pinned - without, 0)
    if not skipped:
        return
    if action == "prune":
        verb = "删除"
        optin = "传入 --include-pinned 可一并删除，或先用 `hermes sessions unpin <id>` 取消固定。"
    else:
        verb, optin = "归档", "先用 `hermes sessions unpin <id>` 取消固定即可将其纳入。"
    print(f"注意：{skipped} 个已固定的会话也符合这些筛选条件，但不会被{verb}"
          f"（固定表示保留）。{optin}")


def _cmd_prune_or_archive(db, args, action):
    prune = action == "prune"
    if prune and getattr(args, "never_active", False):
        return _prune_never_active_keyed(db, args)
    from hermes_cli.session_filters import build_prune_filters, describe_filters, format_epoch
    # Bare `prune` keeps the historical "older than 90 days" default. ANY filter — including --source —
    # suppresses the implicit cutoff (`prune --source cron` matches ALL cron sessions); the preview +
    # confirmation below is the safety net.
    if prune and not _any_filter_args(args):
        args.older_than = "90"
    try:
        filters = build_prune_filters(args)
    except ValueError as e:
        print(f"错误：{e}")
        return 1
    if not prune and not any(v for k, v in filters.items() if k != "older_than_days"):
        print("拒绝归档所有已结束的会话：请至少传入一个"
              "筛选条件（例如 --newer-than 5h、--source cli、--title codex）。")
        return

    # Prune skips archived rows unless --include-archived; archive only targets not-yet-archived rows.
    filters["archived"] = None if prune and getattr(args, "include_archived", False) else False
    filters["include_pinned"] = getattr(args, "include_pinned", False)
    # Archive flips a compression lineage as a unit, matched through its tip (an old ancestor alone
    # never qualifies); the preview must show the same rows the archive will touch.
    filters["lineage_tips_only"] = not prune
    if not filters["include_pinned"]:
        _note_pinned_skipped(db, filters, action)
    candidates = db.list_prune_candidates(**filters)
    # Archive expands each matched tip to its compression lineage, so a direct-open count would
    # misdescribe its effect.
    skipped_open = db.count_open_prune_matches(**filters) if prune else 0
    if skipped_open:
        print(f"注意：{skipped_open} 个未结束的会话也符合这些筛选条件，但会被跳过，"
              "因为 prune 只删除已结束的会话。可用 `hermes sessions delete <id>` "
              "显式删除其中一个。")
    if not candidates:
        print(f"没有匹配的会话（{describe_filters(filters)}）。")
        return
    # Candidates are oldest-activity-first; show the span so a long-lived but recently used
    # conversation cannot look old merely by creation date.
    _span = (
        f"最早活动 {format_epoch(candidates[0].get('last_active'))}，"
        f"最近活动 {format_epoch(candidates[-1].get('last_active'))}"
    )
    if args.dry_run or not args.yes:
        shown = candidates if args.dry_run else candidates[:15]
        print(f"{len(candidates)} 个会话匹配（{describe_filters(filters)}；{_span}）：")
        for s in shown:
            model = (s.get("model") or "-").split("/")[-1][:24]
            print(f"  {s['id']}  {format_epoch(s.get('last_active')):<17} {s['source']:<10} {model:<24} "
                  f"{s['message_count']:>4} 条消息  {(s.get('title') or '')[:36]}")
        if len(candidates) > len(shown):
            print_truncated(len(candidates) - len(shown))
        if args.dry_run:
            print(f"试运行 — 未{'删除' if prune else '归档'}任何内容。")
            return
    verb = "删除" if prune else "归档"
    if not args.yes and not _confirm_prompt(f"{verb}这 {len(candidates)} 个会话（{_span}）？[y/N] "):
        print("已取消。")
        return
    if prune:
        print(f"已清理 {db.prune_sessions(sessions_dir=_sessions_dir(), **filters)} 个会话。")
    else:
        print(f"已归档 {db.archive_sessions(**filters)} 个会话。它们会从列表中隐藏，"
              "但完全可恢复（未删除任何内容）。")


# -- titles / pins -----------------------------------------------------------

def _cmd_rename(db, args):
    resolved_session_id = db.resolve_session_id(args.session_id)
    if not resolved_session_id:
        return _not_found(args.session_id)
    title = " ".join(args.title)
    # Empty titles render as "—" and newlines corrupt the `list` table; length is validated in set_session_title.
    if not title.strip():
        print("错误：标题不能为空或仅含空白字符。")
        return 1
    if "\n" in title or "\r" in title:
        print("错误：标题不能包含换行符。")
        return 1
    try:
        if not db.set_session_title(resolved_session_id, title):
            return _not_found(args.session_id)
    except ValueError as e:
        print(f"错误：{e}")
        return 1
    print(f"会话 '{resolved_session_id}' 已重命名为：{title}")


def _cmd_pin(db, args, pinning):
    """Durable "keep" flag (exempt from sessions.auto_archive, always listed); every surface shares the store."""
    failures = 0
    # Pinned sessions are exempt from the sessions.auto_archive stale sweep and always surface in listings;
    # until now only the Desktop sidebar could write the flag. Inspired by Perplexity Computer's
    # conversational session management (pin/archive from any surface): pin state is operational
    # infrastructure, so every surface — GUI, TUI, CLI, scripts — needs read/write access to the same store.
    # See #52955.
    for raw_id in args.session_ids:
        resolved = db.resolve_session_id(raw_id)
        if resolved and db.set_session_pinned(resolved, pinning):
            title = db.get_session_title(resolved)
            print(f"已{'固定' if pinning else '取消固定'}会话 '{resolved}'。{f'  （{title}）' if title else ''}")
        else:
            failures += _not_found(raw_id)
    return 1 if failures else None


def _cmd_pinned(db, args):
    # limit=1 keeps the recency page minimal; include_pinned back-fills ALL pinned rows the page missed.
    rows = db.list_sessions_rich(limit=1, include_pinned=True, exclude_sources=_default_exclude(args))
    pinned_rows = [s for s in rows if s.get("pinned")]
    if getattr(args, "json", False):
        keys = ("title", "source", "last_active", "message_count")
        print(json.dumps([{"id": s["id"], **{k: s.get(k) for k in keys}} for s in pinned_rows], indent=2))
        return
    if not pinned_rows:
        print("No pinned sessions. Pin one with: hermes sessions pin <session_id>")
        return
    print(f"{'Title':<32} {'Last Active':<13} {'Src':<9} {'ID'}\n" + "─" * 100)
    for s in pinned_rows:
        title = (s.get("title") or s.get("preview", "") or "—")[:30]
        print(f"{title:<32} {_relative_time(s.get('last_active'), session_id=s['id']):<13} {(s.get('source') or '-'):<9} {s['id']}")


def _cmd_retitle_skills(db, args):
    from agent.skill_commands import describe_skill_invocation
    from agent.title_generator import generate_title
    limit = max(1, int(getattr(args, "limit", 200) or 200))
    apply_changes = bool(getattr(args, "apply", False))
    candidates = db.list_skill_scaffolded_sessions(limit=limit)
    if not candidates:
        print("没有通过 /skill 调用命名的会话。")
        return
    mode = "" if apply_changes else "（试运行 — 传入 --apply 才会写入）"
    print(f"{len(candidates)} 个会话通过 /skill 打开{mode}：")
    changed = 0
    for row in candidates:
        session_id = row["id"]
        typed = describe_skill_invocation(row["content"]) or ""
        new_title = generate_title(typed)
        if not new_title or new_title == row["title"]:
            continue
        if not new_title[0].isalnum():
            # Non-title: an auxiliary model occasionally answers the prompt instead of titling it
            # ('$ df -h /'). This is a REPAIR — never replace a serviceable title with that.
            print(f"  {session_id}\n    保留 {row['title']!r} — 得到 {new_title!r}")
            continue
        print(f"  {session_id}\n    {row['title']!r}\n    → {new_title!r}")
        changed += 1
        if not apply_changes:
            continue
        try:
            db.set_session_title(session_id, new_title)
        except ValueError:  # unique-title collision: dedupe like the live auto-titler (base #2, #3, ...)
            deduped = db.get_next_title_in_lineage(new_title)
            try:
                db.set_session_title(session_id, deduped)
                print(f"    （已重命名为 {deduped!r} — 标题已被占用）")
            except ValueError as e:
                print(f"    已跳过：{e}")
                changed -= 1
    if not changed:
        print("  所有标题都已反映用户的请求。")
    elif apply_changes:
        print(f"✓ 已重新命名 {changed} 个会话。")


def _cmd_browse(db, args):
    limit = getattr(args, "limit", 500) or 500
    sessions = db.list_sessions_rich(
        source=getattr(args, "source", None), exclude_sources=_default_exclude(args), limit=limit
    )
    if not sessions:
        db.close()
        print("未找到任何会话。")
        return
    try:  # keep the DB open: the picker uses it for status tags and 'd' delete
        selected_id = _session_browse_picker(sessions, session_db=db)
    finally:
        db.close()
    if not selected_id:
        print("已取消。")
        return
    print(f"正在恢复会话：{selected_id}")
    from hermes_cli.relaunch import relaunch
    relaunch(["--resume", selected_id])  # won't return after execvp


# -- storage maintenance -----------------------------------------------------

def _print_size_change(db, before_mb, prefix=""):
    """Report before/after size, preferring SQLite's page accounting over stat(): in WAL mode a VACUUM's
    rewrite sits in the -wal file until a checkpoint (refused while a live gateway holds a read-mark),
    so the main file lags and stat() can even go negative."""
    logical_after = db.logical_size_bytes()
    after_mb = logical_after / (1024 * 1024) if logical_after is not None else _size_mb(db.db_path)
    delta = _size_delta_label(before_mb - after_mb)
    print(f"{prefix}数据库大小：{before_mb:.1f} MB -> {after_mb:.1f} MB（{delta}）")


def _cmd_optimize(db, args):
    before_mb = _size_mb(db.db_path)
    print("正在优化会话存储（FTS 合并 + VACUUM）……")
    try:
        n = db.vacuum()  # merges FTS5 segments then VACUUMs; returns indexes merged
    except Exception as e:
        print(f"错误：优化失败：{e}")
        return
    print(f"已优化 {n} 个 FTS 索引。")
    _print_size_change(db, before_mb)


def _cmd_clean_markers(db, args):
    print(f"{'试运行 — 正在扫描' if args.dry_run else '正在扫描'}过期的工具调用标记行（#78148）……")
    report = db.purge_stale_tool_call_markers(dry_run=args.dry_run, backup=not args.no_backup)
    if report["rows_affected"] == 0:
        print("✓ 未发现受影响的行 — 无需清理。")
    elif args.dry_run:
        print(f"将清除 {report['rows_affected']} 行：id {report['row_ids']}")
    else:
        if report["backup_path"]:
            print(f"  备份：{report['backup_path']}")
        print(f"✓ 已清除 {report['rows_affected']} 行。")


def _cmd_optimize_storage(db, args):
    db_path = db.db_path
    if not db.fts_optimize_available():
        print("搜索索引已是紧凑布局 — 无需操作。")
        return
    before_bytes = os.path.getsize(db_path) if db_path.exists() else 0
    before_mb = before_bytes / (1024 * 1024)
    # Disk preflight: the new index is built before the old is torn down, and VACUUM needs a full
    # second copy — require headroom ≈ current file size.
    do_vacuum = not getattr(args, "no_vacuum", False)
    try:
        free_bytes = shutil.disk_usage(db_path.parent).free
    except Exception:
        free_bytes = None
    need_bytes = before_bytes if do_vacuum else int(before_bytes * 0.3)
    print(f"搜索索引优化：{db_path}\n  当前数据库大小：{before_mb:.1f} MB")
    if free_bytes is not None:
        print(f"  可用磁盘：{free_bytes / (1024*1024):.0f} MB "
              f"（完成约需 {need_bytes / (1024*1024):.0f} MB{'（含 VACUUM）' if do_vacuum else ''}）")
        if free_bytes < need_bytes:
            print("\n⚠ 可用磁盘空间不足，无法安全完成。请释放空间，或使用 --no-vacuum "
                  "（会重建索引，但要等到之后再执行 VACUUM 才会回收空间）。")
            return
    if before_mb > 500:
        print("  在较大的数据库上可能需要一段时间。它在前台运行，下方会显示进度；"
              "可以安全地按 Ctrl-C 后重跑（会断点续传）。")
    if not getattr(args, "yes", False):
        try:
            resp = input("继续？[y/N] ").strip().lower()
        except EOFError:
            resp = ""
        if resp not in ("y", "yes"):
            print("已取消。")
            return
    _last = {"phase": None}
    labels = {"teardown": "正在回收旧索引", "vacuum": "正在压缩数据库（VACUUM）", "done": "完成"}

    def _progress(info):
        phase = info.get("phase")
        if phase == "backfill":
            print(f"\r  正在重建索引：{info.get('percent', 0):3d}% "
                  f"({info.get('indexed',0):,}/{info.get('total',0):,})", end="", flush=True)
        elif phase != _last["phase"]:
            print(f"\n  {labels.get(phase, phase)}…", flush=True)
        _last["phase"] = phase
    print("正在优化搜索索引存储……")
    try:
        result = db.optimize_fts_storage(progress_cb=_progress, vacuum=do_vacuum)
    except Exception as e:
        print(f"\n错误：优化失败：{e}\n没有数据丢失。重新运行即可继续。")
        return
    if not result.get("ok"):
        print(f"\n无法优化：{result.get('reason', '未知')}")
        return
    print("\n✓ 搜索索引已优化。")
    _print_size_change(db, before_mb, prefix="  ")
    if result.get("vacuumed") is False:
        print("  （VACUUM 被跳过或失败 — 之后可运行 `hermes sessions optimize`  回收释放的空间。）")


def _cmd_repair_routing(db, args):
    records = db.find_orphaned_gateway_sessions(max_gap_s=getattr(args, "max_gap_seconds", None))
    adoptable = [r for r in records if r["adoptable"]]
    for record in records:
        print(f"{record['orphan_id']}  （{record['source']}，{record['message_count']} 条消息）")
        if record["adoptable"]:
            print(f"  → 归入 {record['session_key']}（来自 {record['donor_id']}，"
                  f"依据：{record['evidence']}）")
        else:
            print(f"  ✗ 无法修复 — {record['reason']}")
    if not records:
        print("✓ 没有网关会话缺失其路由标识。")
        return
    if not adoptable:
        print(f"\n发现 {len(records)} 个孤儿会话，均无法明确修复。无需操作。")
        return
    if not getattr(args, "apply", False):
        print(f"\n{len(records)} 个孤儿会话中有 {len(adoptable)} 个可修复。"
              "重新运行并加上 --apply 以执行修复。")
        return
    print("\n应用前请先停止网关 — 正在运行的网关仍在内存中持有旧的路由映射。")
    if not _confirm_prompt(f"归入 {len(adoptable)} 个孤儿会话？[y/N] "):
        print("已中止 — 未做任何更改。")
        return
    repaired = 0
    for record in adoptable:
        if db.adopt_orphaned_gateway_session(record["orphan_id"], record["donor_id"]):
            repaired += 1
            print(f"✓ {record['orphan_id']} 现在归属于 {record['session_key']}")
        else:
            print(f"✗ {record['orphan_id']} 未被归入（该行自报告以来已发生变化）")
    print(f"\n已修复 {len(adoptable)} 个会话中的 {repaired} 个。")


def _cmd_stats(db, args):
    print(f"会话总数：{db.session_count()}\n消息总数：{db.message_count()}")
    for src in ("cli", "telegram", "discord", "whatsapp", "slack"):
        if (c := db.session_count(source=src)) > 0:
            print(f"  {src}：{c} 个会话")
    if db.db_path.exists():
        print(f"数据库大小：{_size_mb(db.db_path):.1f} MB")


# -- dispatch -----------------------------------------------------------------

def _cmd_repair_profiles(args):
    from hermes_cli.sessions_cmd_repair_profiles import cmd_repair_profiles
    return cmd_repair_profiles(args)


def _cmd_set_journal_mode(args):
    from hermes_cli.sessions_cmd_journal_mode import cmd_set_journal_mode
    return cmd_set_journal_mode(args)


_PRE_DB_HANDLERS = {
    "repair": _cmd_repair, "recover": _cmd_recover, "import": _cmd_import,
    "repair-profiles": _cmd_repair_profiles,  # opens every profile's store itself
    "set-journal-mode": _cmd_set_journal_mode,  # offline: must not open the store it converts
}
_OBSERVATIONAL_DB_ACTIONS = frozenset({"list", "stats", "pinned"})
_DB_HANDLERS = {
    "list": _cmd_list, "export": _cmd_export, "delete": _cmd_delete, "rename": _cmd_rename, "pinned": _cmd_pinned,
    "prune": partial(_cmd_prune_or_archive, action="prune"), "pin": partial(_cmd_pin, pinning=True),
    "archive": partial(_cmd_prune_or_archive, action="archive"), "unpin": partial(_cmd_pin, pinning=False),
    "retitle-skills": _cmd_retitle_skills, "browse": _cmd_browse, "optimize": _cmd_optimize,
    "clean-markers": _cmd_clean_markers, "optimize-storage": _cmd_optimize_storage,
    "repair-routing": _cmd_repair_routing, "stats": _cmd_stats,
}


def _print_empty_store(action: str, args) -> None:
    """A profile that never created state.db: report empty instead of opening a writer that creates it."""
    if action == "stats":
        print("Total sessions: 0\nTotal messages: 0")
    elif action == "pinned":
        print("[]" if getattr(args, "json", False) else "No pinned sessions. Pin one with: hermes sessions pin <session_id>")
    else:
        print("No sessions found.")


# VACUUM, the FTS-layout rebuild and bulk deletes rewrite the store; underneath a live gateway/Desktop/cron
# writer that is the second-writer class behind the retired-WAL refusal (#110054). `--force` is the override.
_HELD_STORE_ACTIONS = frozenset({"optimize", "optimize-storage", "prune"})


def cmd_sessions(args, sessions_parser=None):
    action = args.sessions_action
    pre = _PRE_DB_HANDLERS.get(action)
    if pre is not None:
        return pre(args)
    observational = action in _OBSERVATIONAL_DB_ACTIONS
    from hermes_state import SessionDB, _default_db_path
    try:
        db = SessionDB(read_only=observational)
    except Exception as e:
        # mode=ro cannot create the store; a reader on a fresh profile reports empty rather than failing.
        if observational and not _default_db_path().exists():
            return _print_empty_store(action, args)
        print("Could not open your session history database. "
              "Run: hermes sessions repair to fix it (a backup is made first).")
        print(f"Details: {e}")
        return 1
    try:
        handler = _DB_HANDLERS.get(action)
        if handler is None:
            sessions_parser.print_help()
            return
        if action in _HELD_STORE_ACTIONS and not getattr(args, "dry_run", False) and not getattr(args, "force", False):
            from hermes_state_holders import held_store_refusal
            # Same resolver the SessionDB above opened, so the scan never depends on the db object.
            refusal = held_store_refusal(_default_db_path(), command=action)
            if refusal:
                print(refusal)
                return 1
        try:
            return handler(db, args)
        except sqlite3.OperationalError as e:
            from hermes_state_repair import _schema_not_built

            if not observational or not _schema_not_built(e):
                raise
            # A read-only opener skips schema migration, so a store from an older release can lack a column.
            print(f"Error: session database needs migration — run any writing hermes command first ({e})")
            return 1
    finally:
        db.close()
