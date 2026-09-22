"""CLI subcommand: `hermes curator <subcommand>`."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _parse_ts(ts) -> Optional[datetime]:
    """ISO timestamp -> aware UTC datetime, or None when unparseable."""
    try:
        dt = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _fmt_ts(ts: Optional[str]) -> str:
    if not ts:
        return "从未"
    dt = _parse_ts(ts)
    if dt is None:
        return str(ts)
    secs = int((datetime.now(timezone.utc) - dt).total_seconds())
    for unit, div, limit in (("秒", 1, 60), ("分钟", 60, 3600), ("小时", 3600, 86400)):
        if secs < limit:
            return f"{secs // div}{unit}前"
    return f"{secs // 86400}天前"


def _confirm(prompt: str, cancel: str = "已取消", eof_prefix: str = "\n") -> bool:
    """Ask ``prompt``; print ``cancel`` (prefixed on EOF/Ctrl-C) and return False unless y/yes."""
    try:
        if input(prompt).strip().lower() in {"y", "yes"}:
            return True
    except (EOFError, KeyboardInterrupt):
        print(eof_prefix, end="")
    print(cancel)
    return False


def _print_skill_rows(title: str, rows: list) -> None:
    print(f"\n{title}：")
    for r in rows:
        print(
            f"  {r['name']:40s}  "
            f"活动={r.get('activity_count', 0):3d}  "
            f"使用={r.get('use_count', 0):3d}  "
            f"查看={r.get('view_count', 0):3d}  "
            f"补丁={r.get('patch_count', 0):3d}  "
            f"最近活动={_fmt_ts(r.get('last_activity_at'))}")


def _print_unmanaged_summary() -> None:
    """Report curation-eligible skills that carry no provenance marker: only background-review
    creations get ``created_by: agent``; older skills and every foreground ``skill_manage(create)``
    are eligible but unmanaged, so no automatic transition touches them."""
    from tools import skill_usage
    try:
        unmanaged = skill_usage.unmanaged_report()
    except Exception:
        return
    if not unmanaged:
        return
    legacy = sum(1 for r in unmanaged if not r.get("has_provenance_key"))
    foreground = len(unmanaged) - legacy
    print(f"\n未纳管（无来源标记）：共 {len(unmanaged)} 个")
    print(f"  早于标记机制        {legacy}")
    print(f"  前台创建            {foreground}")
    print("  永不自动标记为过期或归档——用 `hermes curator adopt <name>` 可交给策展器管理")


def _print_curator_config(curator) -> None:
    state = curator.load_state()
    status_line = (
        "已暂停" if state.get("paused", False)
        else "已启用" if curator.is_enabled() else "已禁用")
    print(f"策展器：{status_line}")
    print(f"  运行次数：      {state.get('run_count', 0)}")
    print(f"  上次运行：      {_fmt_ts(state.get('last_run_at'))}")
    # 策展器归档过技能时会变成多行（重命名映射以 `名称 → 归并到的技能` 的形式追加）；
    # 续行同样缩进，让整块读起来是一个字段。
    summary = state.get("last_run_summary") or "（无）"
    first, *rest = summary.splitlines() if "\n" in summary else [summary]
    print(f"  上次摘要：      {first}")
    for line in rest:
        print(f"                  {line}")
    report = state.get("last_report_path")
    if report:
        print(f"  上次报告：      {report}{'' if Path(report).exists() else '（缺失）'}")
    ih = curator.get_interval_hours()
    span = f"{ih // 24} 天" if ih % 24 == 0 and ih >= 24 else f"{ih} 小时"
    print(f"  运行间隔：      每 {span}")
    print(f"  过期阈值：      {curator.get_stale_after_days()} 天未使用")
    print(f"  归档阈值：      {curator.get_archive_after_days()} 天未使用")
    consolidate = curator.get_consolidate()
    print(
        f"  合并整理：      {'开' if consolidate else '关'}"
        f"{'' if consolidate else '（仅清理；LLM 合并需显式开启）'}")


def _cmd_status(args) -> int:
    from agent import curator
    from tools import skill_usage
    _print_curator_config(curator)
    rows = skill_usage.curated_report()
    if not rows:
        print("\n没有由策展器管理的技能")
        _print_unmanaged_summary()
        return 0
    by_state: dict = {}
    for r in rows:
        by_state.setdefault(r.get("state", "active"), []).append(r)
    pinned = [r["name"] for r in rows if r.get("pinned")]
    provenance = [r.get("provenance", "agent") for r in rows]
    print(f"\n策展器管理的技能：共 {len(rows)} 个  "
          f"（agent-created={provenance.count('agent')}  bundled={provenance.count('bundled')}）")
    for state_name, state_label in (("active", "活跃"), ("stale", "过期"), ("archived", "归档")):
        print(f"  {state_label:8s} {len(by_state.get(state_name, []))}")
    if pinned:
        print(f"\n已固定（{len(pinned)}）：{', '.join(pinned)}")
    _print_unmanaged_summary()  # 策展盲区在已纳管路径上同样值得提示
    # 查看与编辑同样算作活动：技能不能在 skill_view()/skill_manage() 刚碰过它之后
    # 就被读成「从未使用」。新近度（last_activity_at）与频次（activity_count）是
    # 两种不同的信号，因此两个榜单都给出前 5。
    active_all = by_state.get("active", [])
    if not active_all:
        return 0
    recency = sorted(
        active_all, key=lambda r: r.get("last_activity_at") or r.get("created_at") or "")
    _print_skill_rows("最久未活动（前 5）", recency[:5])

    def _freq(r):
        return (r.get("activity_count") or 0, r.get("last_activity_at") or "")

    most_active = sorted(active_all, key=_freq, reverse=True)[:5]
    if (most_active[0].get("activity_count") or 0) > 0:
        _print_skill_rows("最活跃（前 5）", most_active)
    _print_skill_rows("最不活跃（前 5）", sorted(active_all, key=_freq)[:5])
    return 0


def _cmd_run(args) -> int:
    from agent import curator
    if not curator.is_enabled():
        print("策展器：已通过配置禁用；用 `curator.enabled: true` 启用")
        return 1
    dry = bool(getattr(args, "dry_run", False))
    background = bool(getattr(args, "background", False))
    synchronous = bool(getattr(args, "synchronous", False)) or not background
    # --consolidate 强制开启 LLM 归并整理；未传则为 None，交给 run_curator_review
    # 从配置读取 curator.consolidate。
    consolidate = True if getattr(args, "consolidate", False) else None
    print(
        "策展器：正在试运行（仅生成报告，不做任何改动）……" if dry
        else "策展器：正在执行审查……")
    if consolidate is None and not curator.get_consolidate():
        print(
            "策展器：合并整理已关闭——仅执行清理 "
            "（确定性的过期/归档）。传入 --consolidate 或设置 "
            "`curator.consolidate: true` 以启用 LLM 合并。")
    result = curator.run_curator_review(
        on_summary=print, synchronous=synchronous, dry_run=dry, consolidate=consolidate)
    auto = result.get("auto_transitions", {})
    if auto and dry:
        print(
            f"自动转换（预览）：{auto.get('checked', 0)} 个候选技能 "
            "——试运行期间不应用任何转换")
    elif auto:
        print(
            f"自动转换：已检查={auto.get('checked', 0)} "
            f"过期={auto.get('marked_stale', 0)} "
            f"归档={auto.get('archived', 0)} "
            f"重新激活={auto.get('reactivated', 0)}")
    if not synchronous:
        print("LLM 审查在后台运行——稍后用 `hermes curator status` 查看")
    if dry:
        print(
            "试运行：未应用任何改动。用 "
            "`hermes curator status` 查看报告，再运行 `hermes curator run`（不带参数）实际应用。"
            if synchronous else
            "试运行：未应用任何改动。报告生成后，用 "
            "`hermes curator status` 查看，再运行 `hermes curator run`（不带参数）实际应用。")
    return 0


def _set_paused(paused: bool) -> int:
    from agent import curator
    curator.set_paused(paused)
    print("策展器：已暂停" if paused else "策展器：已恢复")
    return 0


def _cmd_pause(args) -> int: return _set_paused(True)
def _cmd_resume(args) -> int: return _set_paused(False)


_PIN_MESSAGES = {
    True: (
        "无法固定（只有代理创建的技能才参与策展）",
        "无法固定 '{skill}'——该技能不符合策展条件（受保护的内置技能或外部技能）。"
        "`hermes curator list-unmanaged` 可查看策展器跟踪的技能。",
        # 未纳管的技能永远不会被自动转换，因此固定会被记录，但只有在该技能被接管后才真正
        # 起保护作用——把这一点说清楚，并指向 `adopt`。
        "已固定 '{skill}'（已记录；该技能未纳管——自动转换从不考虑它。"
        "运行 `hermes curator adopt {skill}` 可将其纳入策展器管理）",
        "已固定 '{skill}'（将跳过自动转换）"),
    False: (
        "没有可取消固定的内容（策展器只跟踪代理创建的技能）",
        "无法取消固定 '{skill}'——该技能不符合策展条件（受保护的内置技能或外部技能）。",
        "已取消固定 '{skill}'（已记录；该技能未纳管——它从一开始就不在自动转换范围内）",
        "已取消固定 '{skill}'")}


def _set_pin(args, pinned: bool) -> int:
    from tools import skill_usage
    not_agent, not_eligible, unmanaged, done = _PIN_MESSAGES[pinned]
    skill = args.skill
    if not skill_usage.is_agent_created(skill):
        print(f"策展器：'{skill}' 属于内置技能或从技能中心安装——{not_agent}")
        return 1
    if not skill_usage.set_pinned(skill, pinned):
        print("策展器：" + not_eligible.replace("{skill}", skill))
        return 1
    if not skill_usage.is_curator_managed(skill):
        print("策展器：" + unmanaged.replace("{skill}", skill))
        return 0
    print("策展器：" + done.replace("{skill}", skill))
    return 0


def _cmd_pin(args) -> int: return _set_pin(args, True)
def _cmd_unpin(args) -> int: return _set_pin(args, False)


def _cmd_list_unmanaged(args) -> int:
    """Itemize the unmanaged population that `status` summarizes (input for `adopt`)."""
    from tools import skill_usage
    rows = skill_usage.unmanaged_report()
    if not rows:
        print("策展器：没有未纳管的技能— every eligible skill is managed")
        return 0
    print(f"unmanaged skills ({len(rows)}):")
    for r in sorted(rows, key=lambda x: x["name"]):
        why = f"created_by:{r.get('created_by') or 'null'}" if r.get("has_provenance_key") else "no marker"
        print(
            f"  {r['name']:44s} activity={r.get('activity_count', 0):4d}  "
            f"last_activity={_fmt_ts(r.get('last_activity_at')):14s}  ({why})")
    print("\nadopt one with `hermes curator adopt <name>`, "
          "or all with `hermes curator adopt --all-unmanaged`")
    return 0


def _cmd_adopt(args) -> int:
    """Hand unmanaged skills to the curator by explicit user declaration: provenance cannot be
    inferred from telemetry (a high patch count proves the agent MAINTAINS a skill, not that it
    AUTHORED it)."""
    from tools import skill_usage
    names = list(getattr(args, "skill", None) or [])
    adopt_all = bool(getattr(args, "all_unmanaged", False))
    if adopt_all:
        if names:
            print("策展器：请只传入技能名或 --all-unmanaged，二者不能同时传入")
            return 1
        names = skill_usage.list_unmanaged_skill_names()
        if not names:
            print("策展器：没有可接管的未纳管技能")
            return 0
    if not names:
        print("策展器：请指定要接管的技能名，或传入 --all-unmanaged")
        return 1
    if getattr(args, "dry_run", False):
        print(f"策展器：将接管 {len(names)} 个技能（试运行）：")
        for n in names:
            print(f"  + {n}")
        return 0
    # 批量接管属于生命周期变更（被接管的技能会变为可归档）：需要确认。
    if adopt_all and not getattr(args, "yes", False):
        print(f"策展器：将 {len(names)} 个未纳管技能纳入策展器管理？")
        print("  它们将可以被自动标记过期并归档")
        if not _confirm("  是否继续？[y/N] ", "策展器：已中止", eof_prefix=""):
            return 1
    failed = 0
    for n in names:
        ok, msg = skill_usage.adopt_skill(n)
        print(f"策展器：{msg}")
        failed += not ok
    if len(names) > 1:
        print(f"策展器：已接管 {len(names) - failed}/{len(names)}")
    return 1 if failed else 0


def _as_user(fn, skill: str) -> int:
    """Run a skill mutation with the ledger actor set to ``user``; print and map its result."""
    from tools import skill_ledger
    tok = skill_ledger.set_ledger_actor("user")
    try:
        ok, msg = fn(skill)
    finally:
        skill_ledger.reset_ledger_actor(tok)
    print(f"策展器：{msg}")
    return 0 if ok else 1


def _cmd_restore(args) -> int:
    from tools import skill_usage
    return _as_user(skill_usage.restore_skill, args.skill)


def _cmd_archive(args) -> int:
    """Manually archive an agent-created skill. Refuses if pinned."""
    from tools import skill_usage
    if skill_usage.get_record(args.skill).get("pinned"):
        print(
            f"策展器：'{args.skill}' 已固定——请先用 "
            f"`hermes curator unpin {args.skill}` 取消固定")
        return 1
    return _as_user(skill_usage.archive_skill, args.skill)


def _idle_days(record: dict) -> Optional[int]:
    """Days since last activity, falling back to ``created_at`` so never-used skills aren't
    immortal; None only when both fields are missing or unparseable."""
    ts = record.get("last_activity_at") or record.get("created_at")
    dt = _parse_ts(str(ts)) if ts else None
    return None if dt is None else max(0, (datetime.now(timezone.utc) - dt).days)


def _cmd_prune(args) -> int:
    """Bulk-archive curator-managed skills idle for >= N days (pinned exempt, archived skipped)."""
    from agent import curator
    from tools import skill_usage
    days = getattr(args, "days", None)
    if days is None:
        days = curator.get_archive_after_days()
    if days < 1:
        print(f"curator: --days must be >= 1 (got {days})", file=sys.stderr)
        return 2
    candidates = [
        (r["name"], idle) for r in skill_usage.curated_report()
        if not (r.get("pinned") or r.get("state") == skill_usage.STATE_ARCHIVED)
        and (idle := _idle_days(r)) is not None and idle >= days]
    if not candidates:
        print(f"策展器：无需清理（没有空闲 >= {days} 天的未固定技能）")
        return 0
    candidates.sort(key=lambda c: -c[1])
    print(f"策展器：{len(candidates)} 个技能空闲 >= {days} 天：")
    for name, idle in candidates:
        print(f"  {name:40s} 已空闲 {idle} 天")
    if getattr(args, "dry_run", False):
        print("\n（试运行——未做任何改动）")
        return 0
    if not getattr(args, "yes", False) and not _confirm(
        f"\n归档 {len(candidates)} 个技能？[y/N] ", "策展器：已中止"):
        return 1
    results = [(name, *skill_usage.archive_skill(name)) for name, _ in candidates]
    failures = [(name, msg) for name, ok, msg in results if not ok]
    print(f"\n策展器：已归档 {len(results) - len(failures)}/{len(candidates)}")
    if failures:
        print("失败项：")
        for name, msg in failures:
            print(f"  {name}: {msg}")
        return 1
    return 0


def _cmd_backup(args) -> int:
    """Manual skills-tree snapshot (same mechanism as the automatic pre-run snapshot)."""
    from agent import curator_backup
    if not curator_backup.is_enabled():
        print(
            "策展器：备份已通过配置禁用 "
            "（`curator.backup.enabled: false`）；重新启用后才能创建快照")
        return 1
    snap = curator_backup.snapshot_skills(reason=getattr(args, "reason", None) or "manual")
    if snap is None:
        print("策展器：快照创建失败——请查看日志（备份已禁用或发生 IO 错误）")
        return 1
    print(f"策展器：快照已创建于 ~/.hermes/skills/.curator_backups/{snap.name}")
    return 0


def _cmd_ledger(args) -> int:
    """List per-mutation audit ledger entries (newest first), or compact the file in place."""
    from tools import skill_ledger
    if getattr(args, "compact", False):
        entries, before, after = skill_ledger.compact_ledger()
        blobs, freed = skill_ledger.gc_blobs()
        print(f"curator: ledger compacted — {entries} entries, {before / 2**20:.1f} MB → {after / 2**20:.1f} MB; "
              f"{blobs} unreferenced blob(s) removed ({freed / 2**20:.1f} MB)")
        return 0
    rows = skill_ledger.list_entries(
        skill=getattr(args, "skill", None), limit=getattr(args, "limit", None) or 20)
    if not rows:
        print("策展器：审计账本为空（或 skills.ledger 已禁用）。")
        return 0
    print(f"{'id':<14} {'时间':<10} {'操作者':<5} {'动作':<10} 技能")
    for r in rows:
        evidence = r.get("evidence") or {}
        extra = ""
        if evidence.get("absorbed_into"):
            extra = f"  → 已合并到 '{evidence['absorbed_into']}'"
        elif evidence.get("rollback_target"):
            extra = f"  → 回滚自 {evidence['rollback_target']}"
        print(
            f"{r.get('id', '?'):<14} {_fmt_ts(r.get('ts')):<12} "
            f"{r.get('actor', '?'):<8} {r.get('action', '?'):<12} "
            f"{r.get('skill', '?')}{extra}")
    print(
        "\n用 `hermes curator rollback <id>` 回滚单次改动；"
        "整树快照仍可通过 `hermes curator rollback --list` 使用。")
    return 0


def _cmd_purge(args) -> int:
    """Delete archived skills older than curator.archive_ttl_days. Explicit only — never
    automatic; each purged skill is captured (before-blobs) and recorded as a 'purge' ledger
    entry, so even a purge is auditable and blob-recoverable."""
    import shutil
    import time
    from hermes_cli.config import cfg_get, load_config
    from tools import skill_ledger
    from tools.skill_usage import _archive_dir
    ttl_days = getattr(args, "days", None)
    if ttl_days is None:
        ttl_days = int(cfg_get(load_config(), "curator", "archive_ttl_days", default=0) or 0)
    if ttl_days <= 0:
        print(
            "策展器：清理已禁用（curator.archive_ttl_days 为 0）。请设置该"
            "配置项，或传入 --days N 以清理超过 N 天的归档。")
        return 1
    archive_root = _archive_dir()
    if not archive_root.exists():
        print("策展器：没有归档目录——无需清理。")
        return 0
    cutoff = time.time() - ttl_days * 86400
    candidates = sorted(
        p for p in archive_root.iterdir() if p.is_dir() and p.stat().st_mtime < cutoff)
    if not candidates:
        print(f"策展器：没有超过 {ttl_days} 天的归档技能。")
        return 0
    print(f"超过 {ttl_days} 天的归档技能：")
    for p in candidates:
        print(f"  {p.name}")
    if getattr(args, "dry_run", False):
        print("（试运行——未删除任何内容）")
        return 0
    if not getattr(args, "yes", False) and not _confirm(
        f"永久删除 {len(candidates)} 个归档技能？[y/N] "):
        return 1
    purged = 0
    for p in candidates:
        before = skill_ledger.capture_before(p, complete_package=True, skill=p.name)
        try:
            shutil.rmtree(p)
        except OSError as e:
            print(f"策展器：清理 {p.name} 失败：{e}")
            continue
        skill_ledger.append_entry(
            "purge", p.name, before=before or [], after=[], actor="user",
            evidence={"ttl_days": ttl_days})
        purged += 1
    print(f"策展器：已清理 {purged} 个归档技能。审计账本条目已记录。")
    return 0


def _report_rollback(ok: bool, msg: str) -> int:
    print(f"策展器：{msg}" if ok else f"策展器：回滚失败——{msg}")
    return 0 if ok else 1


def _rollback_ledger_entry(args, entry_id: str) -> int:
    """Restore exactly the files touched by one ledger entry (from content-addressed blobs); a
    pre-rollback safety ledger entry is taken first and it fails closed if that capture fails."""
    from tools import skill_ledger
    entry = skill_ledger.get_entry(entry_id)
    if entry is None:
        print(
            f"策展器：没有审计账本条目 '{entry_id}'。"
            "用 `hermes curator ledger` 查看条目 id，或用 "
            "`--id <snapshot>` 做整树快照回滚。")
        return 1
    print(f"回滚目标：审计账本条目 {entry_id}")
    print(f"  动作：  {entry.get('action', '?')}")
    print(f"  技能：  {entry.get('skill', '?')}")
    print(f"  操作者：{entry.get('actor', '?')}")
    print(f"  时间：  {entry.get('ts', '?')}")
    touched = {i.get("path") for i in (entry.get("before") or []) + (entry.get("after") or [])}
    print(f"  文件数：{len(touched)}")
    if not getattr(args, "yes", False) and not _confirm(
        "恢复该次改动之前的状态？[y/N] "):
        return 1
    return _report_rollback(*skill_ledger.rollback_entry(entry_id))


def _cmd_rollback(args) -> int:
    """Restore the skills tree from a snapshot, or a single mutation from the audit ledger."""
    from agent import curator_backup
    entry_id = getattr(args, "entry_id", None)
    if entry_id:
        return _rollback_ledger_entry(args, entry_id)
    if getattr(args, "list", False):
        print(curator_backup.summarize_backups())
        return 0
    backup_id = getattr(args, "backup_id", None)
    target_path = curator_backup._resolve_backup(backup_id)
    if target_path is None:
        if not curator_backup.list_backups():
            print(
                "策展器：尚不存在任何快照。可用 "
                "`hermes curator backup` 创建一个，或等待下次策展器运行。")
        else:
            print(
                f"策展器：没有匹配 "
                f"{'id ' + repr(backup_id) if backup_id else '你的查询'} 的快照。")
            print("可用快照：")
            print(curator_backup.summarize_backups())
        return 1
    manifest = curator_backup._read_manifest(target_path)
    print(f"回滚目标：{target_path.name}")
    if manifest:
        print(f"  原因：       {manifest.get('reason', '?')}")
        print(f"  创建时间：   {manifest.get('created_at', '?')}")
        print(f"  技能文件数： {manifest.get('skill_files', '?')}")
        cron = manifest.get("cron_jobs") or {}
        if isinstance(cron, dict):
            if cron.get("backed_up"):
                print(
                    f"  定时任务：   {cron.get('jobs_count', 0)} 个 "
                    f"（仅恢复技能引用相关字段）")
            else:
                print(f"  定时任务：   不在快照中（{cron.get('reason', '未捕获')}）")
    print(
        "\n这将替换当前的 ~/.hermes/skills/ 目录树（会先为当前状态创建一份安全"
        "快照，因此可以撤销）。仍然存在的定时任务，其 skills/skill 字段会"
        "从快照恢复；其他定时任务字段保持不变。")
    if not getattr(args, "yes", False) and not _confirm("是否继续？[y/N] "):
        return 1
    ok, msg, _ = curator_backup.rollback(backup_id=target_path.name)
    return _report_rollback(ok, msg)


def _cmd_list_archived(args) -> int:
    """List archived (recoverable) skills."""
    from tools import skill_usage
    names = skill_usage.list_archived_skill_names()
    print("\n".join(names) if names else "策展器：没有已归档的技能")
    return 0


_USAGE_SORTS = {
    "name": (lambda r: r["name"], False),
    "recent": (lambda r: r.get("last_activity_at") or "", True),
    "activity": (lambda r: r.get("activity_count", 0), True)}


def _cmd_usage(args) -> int:
    """Usage telemetry for ALL skills on disk (bundled + hub included), with provenance."""
    import json as _json
    from tools import skill_usage
    rows = skill_usage.usage_report()
    prov_filter = getattr(args, "provenance", None)
    if prov_filter:
        rows = [r for r in rows if r.get("provenance") == prov_filter]
    # name：按字母序；recent：最近活动的排在最前（从未活动的沉底）；activity
    # （默认）：使用最多的排在最前。
    key, reverse = _USAGE_SORTS.get(getattr(args, "sort", "activity"), _USAGE_SORTS["activity"])
    rows.sort(key=key, reverse=reverse)
    if getattr(args, "json", False):
        print(_json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    if not rows:
        print("策展器：未找到任何技能")
        return 0
    provenance = [r.get("provenance", "agent") for r in rows]
    counts = {k: provenance.count(k) for k in ("agent", "bundled", "hub")}
    print(
        f"技能：共 {len(rows)} 个  "
        f"（agent={counts['agent']}  bundled={counts['bundled']}  hub={counts['hub']}）\n")
    print(
        f"  {'技能':<38}  {'来源':<6}  "
        f"{'使用':>2}  {'查看':>2}  {'补丁':>3}  {'活动':>2}  最近活动")
    for r in rows:
        print(
            f"  {r['name'][:40]:40s}  "
            f"{r.get('provenance', 'agent'):8s}  "
            f"{r.get('use_count', 0):>4d}  "
            f"{r.get('view_count', 0):>4d}  "
            f"{r.get('patch_count', 0):>5d}  "
            f"{r.get('activity_count', 0):>4d}  "
            f"{_fmt_ts(r.get('last_activity_at'))}")
    return 0


def _arg(*flags, **kwargs):
    return flags, kwargs


_SKILL = _arg("skill", help="技能名")
_YES = _arg("-y", "--yes", action="store_true", help="Skip the confirmation prompt")
_STORE_TRUE = dict(action="store_true")

# argparse wiring: (name, help, handler, *(flags, add_argument kwargs))
_SUBCOMMANDS = (
    ("status", "显示策展器状态与技能统计", _cmd_status),
    (
        "usage", "显示所有技能（内置、技能中心、代理创建）的使用遥测及其来源",
        _cmd_usage,
        _arg("--sort", choices=("activity", "recent", "name"), default="activity",
             help="排序方式：activity（使用最多在前，默认）、recent"
                  "（最近活动在前）或 name（按字母序）"),
        _arg("--provenance", choices=("agent", "bundled", "hub"), default=None,
             help="仅显示该来源的技能"),
        _arg("--json", **_STORE_TRUE, help="以 JSON 而非表格输出完整报告")),
    (
        "run", "立即触发一次策展器审查", _cmd_run,
        _arg("--sync", "--synchronous", dest="synchronous", **_STORE_TRUE,
             help="等待 LLM 审查完成（手动运行时为默认）"),
        _arg("--background", dest="background", **_STORE_TRUE,
             help="在后台线程启动 LLM 审查并立即返回"),
        _arg("--dry-run", dest="dry_run", **_STORE_TRUE,
             help="仅生成报告——不改状态、不归档、不做合并整理"
                  "（用于预览策展器会做什么）"),
        _arg("--consolidate", dest="consolidate", **_STORE_TRUE,
             help="为本次运行强制开启 LLM 归并整理，覆盖配置默认值（关）。"
                  "不传该参数时，除非设置了 `curator.consolidate: true`，"
                  "本次运行仅执行清理。")),
    ("pause", "暂停策展器，直到恢复", _cmd_pause),
    ("resume", "恢复已暂停的策展器", _cmd_resume),
    ("pin", "固定技能，使策展器永不自动转换它", _cmd_pin, _SKILL),
    ("unpin", "取消固定技能", _cmd_unpin, _SKILL),
    ("list-unmanaged", "列出符合策展条件但没有来源标记的技能",
     _cmd_list_unmanaged),
    (
        "adopt", "把未纳管的技能交给策展器（来源由用户声明）",
        _cmd_adopt,
        _arg("skill", nargs="*", help="要接管的技能名。使用 --all-unmanaged 时省略。"),
        _arg("--all-unmanaged", **_STORE_TRUE,
             help="接管所有符合策展条件但没有来源标记的技能"),
        _arg("--dry-run", **_STORE_TRUE,
             help="只列出会接管哪些技能，不做任何写入"),
        _arg("--yes", **_STORE_TRUE, help="使用 --all-unmanaged 时跳过确认提示")),
    ("restore", "恢复已归档的技能", _cmd_restore, _SKILL),
    ("list-archived", "列出已归档的技能", _cmd_list_archived),
    ("archive", "手动归档技能（移动到 .archive/，从提示词中排除）", _cmd_archive,
     _SKILL),
    (
        "prune", "Bulk-archive curator-managed skills idle for >= N days (default: curator.archive_after_days)",
        _cmd_prune,
        _arg("--days", type=int, default=None,
             help="Archive skills idle for at least N days (default: curator.archive_after_days, 30)"),
        _YES,
        _arg("--dry-run", dest="dry_run", **_STORE_TRUE,
             help="只显示会归档哪些技能，不实际执行")),
    (
        "backup",
        "为 ~/.hermes/skills/ 手动创建 tar.gz 快照"
        "（策展器在每次真实运行前也会自动做这件事）",
        _cmd_backup,
        _arg("--reason", default=None,
             help="存入 manifest.json 的自由文本标签（默认：'manual'）")),
    (
        "rollback",
        "从策展器快照恢复 ~/.hermes/skills/，或按审计账本条目 id "
        "回滚单次改动（见 `hermes curator ledger`）",
        _cmd_rollback,
        _arg("entry_id", nargs="?", default=None,
             help="用于单次改动回滚的审计账本条目 id（来自 "
                  "`hermes curator ledger`）。整树快照回滚时省略。"),
        _arg("--list", **_STORE_TRUE, help="列出可用快照后退出，不执行恢复"),
        _arg("--id", dest="backup_id", default=None,
             help="要恢复的快照 id（见 `--list`）；默认：最新的"),
        _arg("-y", "--yes", **_STORE_TRUE, help="Skip confirmation prompt")),
    (
        "ledger", "列出逐次改动的技能审计账本（所有操作者：curator/agent/user）",
        _cmd_ledger,
        _arg("--skill", default=None, help="只显示该技能的条目"),
        _arg("--limit", type=int, default=20, help="最多显示的条目数（默认：20）"),
        _arg("--compact", **_STORE_TRUE,
             help="重写账本，从每个条目中删除未变化的路径（id 与回滚信息保留）")),
    (
        "purge",
        "删除超过 curator.archive_ttl_days 的归档技能"
        "（仅显式执行——从不自动；记录到审计账本）",
        _cmd_purge,
        _arg("--days", type=int, default=None,
             help="本次调用覆盖 curator.archive_ttl_days"),
        _arg("--dry-run", dest="dry_run", **_STORE_TRUE,
             help="只显示会清理哪些内容，不实际删除"),
        _YES))


def register_cli(parent: argparse.ArgumentParser) -> None:
    """Attach `curator` subcommands to *parent*."""
    parent.set_defaults(func=lambda a: (parent.print_help(), 0)[1])
    subs = parent.add_subparsers(dest="curator_command")
    for name, help_text, handler, *arguments in _SUBCOMMANDS:
        sub = subs.add_parser(name, help=help_text)
        for flags, arg_kwargs in arguments:
            sub.add_argument(*flags, **arg_kwargs)
        sub.set_defaults(func=handler)


def cli_main(argv=None) -> int:
    """Standalone entry (also usable by hermes_cli.main fallthrough)."""
    parser = argparse.ArgumentParser(prog="hermes curator")
    register_cli(parser)
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(cli_main())
