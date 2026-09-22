"""``hermes profile`` 命令——每个 action 一个处理函数，由 ``PROFILE_ACTIONS`` 分发。

对 ``hermes_cli.profiles`` 的导入保持惰性（放在各处理函数内部），以便测试 monkeypatch
该模块的属性。
"""

from __future__ import annotations

from pathlib import Path
import os
import sys
from typing import NoReturn, Optional
import unicodedata


def _display_width(text: str) -> int:
    """终端显示宽度：CJK/全角字符占 2 列，其余占 1 列。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    """按显示宽度把 *text* 左对齐补齐到 *width* 列（一个汉字算 2 列）。"""
    return text + " " * max(0, width - _display_width(text))


def _die(msg: str, code: int = 1, *, err: bool = False) -> NoReturn:
    print(msg, file=sys.stderr if err else sys.stdout)
    sys.exit(code)


def _confirm(prompt: str) -> bool:
    """y/N 提示；EOF / Ctrl-C 视为“否”。"""
    try:
        answer = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    return answer in {"y", "yes"}


def _is_active(p, active: str) -> bool:
    return p.name == active or (active == "default" and p.is_default)


def _env_file_has_key(env_path: Path, key: str) -> bool:
    """当 *key* is assigned in *env_path* (unreadable/mis-encoded file → False, never aborts)."""
    from agent.secret_scope import load_env_file

    return key in load_env_file(env_path)


def _render_distribution_plan(plan) -> None:
    """打印待安装发行版的可读摘要。"""
    from hermes_cli.profile_distribution import MANIFEST_FILENAME
    mf = plan.manifest
    print(f"\n发行版：{mf.name} v{mf.version}")
    if mf.description:
        print(f"  {mf.description}")
    if mf.author:
        print(f"  作者：    {mf.author}")
    if mf.hermes_requires:
        print(f"  依赖：    Hermes {mf.hermes_requires}")
    print(f"  来源：    {plan.provenance}")
    print(f"  目标目录：{plan.target_dir}")
    if plan.existing:
        # 更新已有发行版（覆盖发行版归属的文件、保留配置、不动用户数据）
        # data untouched) vs overwriting a hand-built plain profile (same mechanics, but the
        # user didn't sign up for it).
        if (plan.target_dir / MANIFEST_FILENAME).is_file():
            print("  （profile 已存在，只覆盖发行版归属的文件）")
        else:
            print(
                "  ⚠ Profile exists but is NOT a distribution.  Installing here will\n"
                "    overwrite its SOUL.md and mcp.json and replace any skill or cron job\n"
                "    of the same name the distribution ships.\n"
                "    Your memories, sessions, auth.json, and .env will be preserved,\n"
                "    but any hand-edits to distribution-owned files will be lost."
            )
    if mf.env_requires:
        print("\n  环境变量：")
        for er in mf.env_requires:
            tag = "required" if er.required else "optional"
            # Shell environment OR the target profile's .env — don't nag about set keys.
            already = os.environ.get(er.name) is not None or (
                plan.target_dir.is_dir() and _env_file_has_key(plan.target_dir / ".env", er.name)
            )
            status = "✓ set" if already else ("需要设置" if er.required else "—")
            line = f"    • {er.name} ({tag}, {status})"
            if er.description:
                line += f" — {er.description}"
            print(line)
    if plan.has_cron:
        print(
            "\n  ⚠ 该发行版附带 cron 任务，它们不会自动运行——请手动检查并启用。"
        )


def _profile_status(args):
    """裸 ``hermes profile``——显示当前 profile 状态。"""
    from hermes_constants import display_hermes_home
    from hermes_cli.profiles import format_profile_label, get_active_profile_name, list_profiles
    profile_name = get_active_profile_name()
    dhh = display_hermes_home()
    current = next((p for p in list_profiles() if _is_active(p, profile_name)), None)
    label = format_profile_label(profile_name, current.display_name if current else "")
    print(f"\n当前 profile：  {label}")
    print(f"路径：          {dhh}")
    if current is not None:
        p = current
        if p.model:
            print(f"模型：          {p.model}" + (f" ({p.provider})" if p.provider else ""))
        print(f"网关：          {'运行中' if p.gateway_running else '已停止'}")
        print(f"技能：          已安装 {p.skill_count} 个")
        if p.alias_path:
            print(f"别名：          {p.alias_name or p.name} → hermes -p {p.name}")
    print()


def _profile_list(args):
    from hermes_cli.profiles import format_profile_label, get_active_profile_name, list_profiles
    profiles = list_profiles()
    active = get_active_profile_name()
    if not profiles:
        print("未找到任何 profile。")
        return
    print(f"\n {'Profile':<16} {_pad('模型', 28)} {_pad('网关', 12)} {_pad('别名', 12)} 发行版")
    print(f" {'─' * 15}    {'─' * 27}    {'─' * 11}    {'─' * 11}    {'─' * 20}")
    for p in profiles:
        marker = " ◆" if _is_active(p, active) else "  "
        name = format_profile_label(p.name, p.display_name)
        model = (p.model or "—")[:26]
        gw = "running" if p.gateway_running else "已停止"
        alias = (p.alias_name or p.name) if p.alias_path and not p.is_default else "—"
        dist = f"{p.distribution_name}@{p.distribution_version or '?'}"[:30] if p.distribution_name else "—"
        print(f"{marker}{name:<15} {model:<28} {gw:<12} {alias:<12} {dist}")
    print()
    for line in _shared_credential_warnings(profiles):
        print(line)


def _shared_credential_warnings(profiles) -> list:
    """One warning per named profile whose bot credential is byte-identical to the default's
    (typically an old ``--clone`` that copied .env): the collision that parks a multiplexed
    adapter or makes two standalone gateways fight over one bot."""
    from hermes_cli.profile_channels import shared_channel_credentials, shared_credential_warning
    default = next((p for p in profiles if p.is_default), None)
    if default is None:
        return []
    lines = []
    for p in profiles:
        if p.is_default:
            continue
        try:
            shared = shared_channel_credentials(p.path, default.path)
        except Exception:
            continue
        if shared:
            lines.append(shared_credential_warning(p.name, shared))
    return lines + ([""] if lines else [])


def _profile_use(args):
    from hermes_cli.profiles import set_active_profile
    name = args.profile_name
    try:
        set_active_profile(name)
        print("已切换到：default（~/.hermes）" if name == "default" else f"已切换到：{name}")
    except (ValueError, FileNotFoundError) as e:
        _die(f"错误：{e}")


def _source_profile_dir(source_label: str) -> Path:
    from hermes_cli.profiles import get_profile_dir
    source_dir = get_profile_dir(source_label)
    if not source_dir.is_dir():
        raise FileNotFoundError(source_dir)
    return source_dir


def _print_channel_clone_notice(name: str, source_label: str, clone_channels: bool, clone_flag: str) -> None:
    from hermes_cli.profile_channels import (
        channel_platforms_configured, format_stripped_notice, shared_channel_credentials,
        shared_credential_warning,
    )
    from hermes_cli.profiles import get_profile_dir
    try:
        source_dir = _source_profile_dir(source_label)
    except FileNotFoundError:
        return
    if not clone_channels:
        for line in format_stripped_notice(name, channel_platforms_configured(source_dir), clone_flag):
            print(line)
        return
    shared = shared_channel_credentials(get_profile_dir(name), source_dir)
    if shared:
        print(shared_credential_warning(name, shared, source_label))


def _profile_create(args):
    from hermes_cli.profiles import (
        _get_wrapper_dir, _is_wrapper_dir_in_path, check_alias_collision, create_profile,
        create_wrapper_script, get_active_profile_name, seed_profile_skills,
    )
    name = args.profile_name
    clone = getattr(args, "clone", False)
    clone_all = getattr(args, "clone_all", False)
    no_alias = getattr(args, "no_alias", False)
    no_skills = getattr(args, "no_skills", False)
    clone_from = getattr(args, "clone_from", None)
    clone_channels = getattr(args, "clone_channels", False)
    sync_imports = getattr(args, "sync_imports", False)
    clone_config = clone or clone_from is not None
    cloned = clone_config or clone_all
    source_label = clone_from or get_active_profile_name()
    try:
        profile_dir = create_profile(
            name=name, clone_from=clone_from, clone_all=clone_all, clone_config=clone_config,
            no_alias=no_alias, no_skills=no_skills, description=getattr(args, "description", None),
            clone_channels=clone_channels, sync_imports=sync_imports,
        )
    except (ValueError, FileExistsError, FileNotFoundError) as e:
        _die(f"Error: {e}")
    print(f"\nProfile '{name}' created at {profile_dir}")
    if cloned:
        if clone_all:
            print(f"Full copy from {source_label} (excluding session history, cron jobs, backups, and snapshots).")
        else:
            print(f"Cloned config, .env, SOUL.md, and skills from {source_label}.")
        if sync_imports:
            print(f"Import sources carried over — `hermes -p {name} import-agent --sync` "
                  "keeps pulling the same Claude Code / Codex trees.")
        _print_channel_clone_notice(name, source_label, clone_channels, "--clone-all" if clone_all else "--clone")
        # Auto-clone Honcho config for the new profile (only with clone operations)
        try:
            from plugins.memory.honcho.cli import ConfigWriteRefused, clone_honcho_for_profile
        except Exception:
            clone_honcho_for_profile = None  # Honcho plugin not installed
        if clone_honcho_for_profile is not None:
            try:
                if clone_honcho_for_profile(name):
                    print(f"Honcho config cloned (peer: {name})")
            except ConfigWriteRefused as e:
                print(f"Honcho config not cloned: {e}")
            except Exception:
                pass  # Honcho not configured
    else:
        # Fresh profiles only: clones already carry the source's (user-curated) skills.
        result = seed_profile_skills(profile_dir)
        if result and result.get("skipped_opt_out"):
            print("No bundled skills seeded (--no-skills). Delete .no-bundled-skills in the profile to opt back in.")
        elif result:
            print(f"{len(result.get('copied', []))} bundled skills synced.")
        else:
            print(f"⚠ Skills could not be seeded. Run `{name} update` to retry.")
    if not no_alias:
        collision = check_alias_collision(name)
        if collision:
            print(f"\n⚠ Cannot create alias '{name}' — {collision}")
            print(f"  Choose a custom alias:  hermes profile alias {name} --name <custom>")
            print(f"  Or access via flag:     hermes -p {name} chat")
        else:
            wrapper_path = create_wrapper_script(name)
            if wrapper_path:
                print(f"Wrapper created: {wrapper_path}")
                if not _is_wrapper_dir_in_path():
                    print(f"\n⚠ {_get_wrapper_dir()} is not in your PATH.")
                    print("  Add to your shell config (~/.bashrc or ~/.zshrc):")
                    print('    export PATH="$HOME/.local/bin:$PATH"')
    try:
        profile_dir_display = "~/" + profile_dir.relative_to(Path.home()).as_posix()
    except ValueError:
        profile_dir_display = str(profile_dir)
    print("\nNext steps:")
    print(f"  {name} setup              Configure API keys and model")
    print(f"  {name} chat               Start chatting")
    from hermes_cli.gateway_multiplex_served import live_default_gateway_pid, recorded_served_profiles
    from hermes_cli.profiles import normalize_profile_name
    served = recorded_served_profiles() if live_default_gateway_pid() is not None else None
    if served is not None and normalize_profile_name(name) in {normalize_profile_name(p) for p in served}:
        print("  (served now by the running multiplexed gateway — add its bot token and it connects)")
    elif served is not None:
        # The multiplexer did not pick the profile up (older gateway or the signal failed): a restart serves it.
        print("  hermes gateway restart    Serve this profile from the running multiplexed gateway")
    else:
        print(f"  {name} gateway start      Start the messaging gateway")
    if clone or clone_all:
        print(f"\n  Edit {profile_dir_display}/.env for different API keys")
        print(f"  Edit {profile_dir_display}/SOUL.md for different personality")
    else:
        print(f"\n  ⚠ This profile has no API keys yet. Run '{name} setup' first,")
        print("    or it will inherit keys from your shell environment.")
        print(f"  Edit {profile_dir_display}/SOUL.md to customize personality")
    print()


def _profile_delete(args):
    from hermes_cli.profiles import delete_profile
    try:
        delete_profile(args.profile_name, yes=getattr(args, "yes", False))
    except (ValueError, FileNotFoundError, RuntimeError) as e:
        _die(f"错误：{e}")


def _describe_target_dir(name: str) -> Path:
    """``describe`` 用的 profile 目录：``default`` 映射到当前 home（get_hermes_home），
    其余映射到各自命名的目录。"""
    from hermes_cli import profiles as _profiles_mod
    if _profiles_mod.normalize_profile_name(name) == "default":
        from hermes_constants import get_hermes_home as _hh
        return Path(_hh())
    return _profiles_mod.get_profile_dir(name)


def _profile_describe(args):
    from hermes_cli import profiles as _profiles_mod
    all_flag = bool(getattr(args, "all_missing", False))
    auto_flag = bool(getattr(args, "auto", False))
    overwrite_flag = bool(getattr(args, "overwrite", False))
    text_value = getattr(args, "text", None)
    name = getattr(args, "profile_name", None)
    if all_flag and not auto_flag:
        _die("profile describe：--all 需要 --auto", 2, err=True)
    if all_flag and (text_value or name):
        _die("profile describe：--all 与 profile 名 / --text 互斥", 2, err=True)
    if not all_flag and not name:
        _die("profile describe：需要指定 profile 名（或使用 --all --auto）", 2, err=True)
    if text_value and auto_flag:
        _die("profile describe：--text 与 --auto 互斥", 2, err=True)

    # 未请求任何操作时，显示当前描述。
    if name and not text_value and not auto_flag:
        try:
            profile_dir = _describe_target_dir(name)
        except Exception as exc:
            _die(f"错误：{exc}", err=True)
        if not profile_dir.is_dir():
            _die(f"错误：找不到 profile '{name}'", err=True)
        meta = _profiles_mod.read_profile_meta(profile_dir)
        desc = meta.get("description") or ""
        if not desc:
            print(f"（'{name}' 尚未设置描述）")
        else:
            tag = "[自动] " if meta.get("description_auto") else ""
            print(f"{tag}{desc}")
        sys.exit(0)

    # --text 路径：直接写入用户手写的描述。
    if text_value:
        try:
            _profiles_mod.write_profile_meta(_describe_target_dir(name), description=text_value, description_auto=False)
            print(f"已更新 '{name}' 的描述。")
        except Exception as exc:
            _die(f"错误：{exc}", err=True)
        sys.exit(0)

    # --auto 路径：调用 LLM 生成描述。
    from hermes_cli import profile_describer as _pd
    if all_flag:
        targets = _pd.list_describable_profiles(missing_only=True)
        if not targets:
            _die("所有 profile 都已设置描述。", 0)
    else:
        targets = [name]
    ok_count = 0
    for tgt in targets:
        outcome = _pd.describe_profile(tgt, overwrite=overwrite_flag)
        if outcome.ok:
            ok_count += 1
            print(f"已为 '{outcome.profile_name}' 生成描述：{outcome.description}")
        else:
            print(f"profile describe {outcome.profile_name}：{outcome.reason}", file=sys.stderr)
    sys.exit(0 if (ok_count > 0 if all_flag else ok_count == 1) else 1)


def _profile_show(args):
    name = args.profile_name
    from hermes_cli.profiles import (
        get_profile_dir, profile_exists, _read_config_model, _check_gateway_running,
        _served_by_running_multiplexer, _count_skills, _read_distribution_meta, _wrapper_path,
        find_alias_for_profile, format_profile_label, read_profile_meta,
    )
    if not profile_exists(name):
        _die(f"错误：profile '{name}' 不存在。")
    profile_dir = get_profile_dir(name)
    model, provider = _read_config_model(profile_dir)
    gw = _check_gateway_running(profile_dir) or _served_by_running_multiplexer(name)
    dist_name, dist_version, dist_source = _read_distribution_meta(profile_dir)
    alias_name = find_alias_for_profile(name)
    display = read_profile_meta(profile_dir).get("display_name", "")
    print(f"\nprofile：{format_profile_label(name, display)}")
    print(f"路径：    {profile_dir}")
    if model:
        print(f"模型：    {model}" + (f" ({provider})" if provider else ""))
    print(f"网关：    {'运行中' if gw else '已停止'}")
    print(f"技能：    {_count_skills(profile_dir)}")
    print(f".env：    {'已存在' if (profile_dir / '.env').exists() else '未配置'}")
    print(f"SOUL.md：{'已存在' if (profile_dir / 'SOUL.md').exists() else '未配置'}")
    if dist_name:
        print(f"发行版：{dist_name}@{dist_version or '?'}")
        if dist_source:
            print(f"安装来源：{dist_source}")
        print(f"  （运行 `hermes profile info {name}` 查看完整清单）")
    if alias_name:
        print(f"别名：   {alias_name} → hermes -p {name}  ({_wrapper_path(alias_name)})")
    print()


def _profile_alias(args):
    from hermes_cli.profiles import (
        _get_wrapper_dir, _is_wrapper_dir_in_path, check_alias_collision, create_wrapper_script,
        profile_exists, remove_wrapper_script, validate_alias_name,
    )
    name = args.profile_name
    remove = getattr(args, "remove", False)
    custom_name = getattr(args, "alias_name", None)
    if not profile_exists(name):
        _die(f"错误：profile '{name}' 不存在。")
    alias_name = custom_name or name
    try:
        validate_alias_name(alias_name)
    except ValueError as exc:
        _die(f"错误：{exc}")
    if remove:
        if remove_wrapper_script(alias_name):
            print(f"✓ 已删除别名 '{alias_name}'")
        else:
            print(f"未找到可删除的别名 '{alias_name}'。")
        return
    collision = check_alias_collision(alias_name)
    if collision:
        _die(f"错误：{collision}")
    wrapper_path = create_wrapper_script(alias_name, target=name if custom_name else None)
    if wrapper_path:
        print(f"✓ 已创建别名：{wrapper_path}")
        if not _is_wrapper_dir_in_path():
            print(f"⚠ {_get_wrapper_dir()} 不在你的 PATH 中。")


def _profile_rename(args):
    from hermes_cli.profiles import normalize_profile_name, rename_profile
    try:
        new_dir = rename_profile(args.old_name, args.new_name)
        if normalize_profile_name(args.old_name) != "default":
            print(f"\n已重命名 profile：{args.old_name} → {args.new_name}")
            print(f"路径：{new_dir}\n")
    except (ValueError, FileExistsError, FileNotFoundError) as e:
        _die(f"错误：{e}")


def _profile_migrate_identity(args):
    """Retry the identity migration of a rename that already completed. Exits non-zero when a
    live gateway would not migrate (it still owns the routing index in memory), or when a
    database rejected the rewrite (collision, lock, partial failure)."""
    from hermes_cli.profile_identity import migrate_profile_identity
    try:
        migrated = migrate_profile_identity(args.old_name, args.new_name)
    except (ValueError, FileNotFoundError) as e:
        _die(f"Error: {e}")
    if not migrated:
        _die(f"Error: session identity was not migrated. Restart or stop the gateway, then run:\n"
             f"    hermes profile migrate-identity {args.old_name} {args.new_name}", err=True)
    print(f"✓ Session/routing identity migrated: {args.old_name} → {args.new_name}")


def _profile_purge_identity(args):
    """Retry the identity purge of a delete that already completed. Exits non-zero when a live
    gateway would not purge (it still owns the routing index in memory), or when a database rejected
    the delete (lock, partial failure)."""
    from hermes_cli.profile_identity import purge_profile_identity
    try:
        purged = purge_profile_identity(args.profile_name)
    except ValueError as e:
        _die(f"Error: {e}")
    if not purged:
        _die(f"Error: session identity was not purged. Restart or stop the gateway, then run:\n"
             f"    hermes profile purge-identity {args.profile_name}", err=True)
    print(f"✓ Session/routing identity purged: {args.profile_name}")


def _profile_export(args):
    from hermes_cli.profiles import export_profile, get_profile_export_path
    name = args.profile_name
    try:
        output = args.output or str(get_profile_export_path(name))
        result_path = export_profile(name, output)
        print(f"✓ 已导出 '{name}' 至 {result_path}")
    except (ValueError, FileNotFoundError, OSError) as e:
        _die(f"错误：{e}")


def _profile_import(args):
    from hermes_cli.profiles import check_alias_collision, create_wrapper_script, import_profile
    try:
        profile_dir = import_profile(args.archive, name=getattr(args, "import_name", None))
        name = profile_dir.name
        print(f"✓ 已导入 profile '{name}'：{profile_dir}")
        if not check_alias_collision(name):
            wrapper_path = create_wrapper_script(name)
            if wrapper_path:
                print(f"  已创建包装脚本：{wrapper_path}")
        print()
    except (ValueError, FileExistsError, FileNotFoundError) as e:
        _die(f"错误：{e}")


def _profile_install(args):
    import tempfile
    from hermes_cli.profile_distribution import DistributionError, install_distribution, plan_install
    try:
        # 预览：先暂存到临时目录、展示清单，再做真正的安装。
        # 两段式暂存可确保用户拒绝时不产生任何副作用。
        with tempfile.TemporaryDirectory(prefix="hermes_dist_preview_") as tmp:
            plan = plan_install(args.source, Path(tmp), override_name=getattr(args, "install_name", None))
            _render_distribution_plan(plan)
            if not getattr(args, "yes", False) and not _confirm("\n是否继续安装？[y/N] "):
                print("已取消安装。")
                return
        plan = install_distribution(
            args.source, name=getattr(args, "install_name", None), force=getattr(args, "force", False),
            create_alias=getattr(args, "alias", False),
        )
        print(f"\n✓ 已安装 '{plan.manifest.name}' v{plan.manifest.version}")
        print(f"  profile 路径：{plan.target_dir}")
        if plan.manifest.env_requires:
            print(
                f"  下一步：将 .env.EXAMPLE 复制为 .env 并填入必需的密钥：\n"
                f"    {plan.target_dir}/.env.EXAMPLE"
            )
        if plan.has_cron:
            print(
                "  包含的 cron 任务不会自动调度。\n"
                f"  查看方式：  hermes -p {plan.manifest.name} cron list"
            )
        print(f"\n  使用方式：    hermes -p {plan.manifest.name} chat")
    except (DistributionError, ValueError) as e:
        _die(f"错误：{e}")


def _profile_update(args):
    from hermes_cli.profile_distribution import DistributionError, read_manifest, update_distribution
    from hermes_cli.profiles import get_profile_dir, normalize_profile_name
    try:
        canon = normalize_profile_name(args.profile_name)
        current = read_manifest(get_profile_dir(canon))
        if current is None:
            _die(
                f"错误：profile '{canon}' 不是发行版（缺少 distribution.yaml）。"
                "只有通过 `hermes profile install` 安装的 profile 才能更新。"
            )
        force_config = getattr(args, "force_config", False)
        if not getattr(args, "yes", False):
            print(f"\n更新 '{canon}'，来源：{current.source or '（无来源）'}")
            print(f"  当前版本 {current.version}")
            if force_config:
                print("  已设置 --force-config：config.yaml 将被覆盖。")
            else:
                print("  config.yaml 会被保留（如需覆盖请传 --force-config）。")
            print("  用户数据（记忆、会话、auth、.env）不会被改动。")
            if not _confirm("\n是否继续？[y/N] "):
                print("已取消更新。")
                return
        plan = update_distribution(canon, force_config=force_config)
        print(f"\n✓ 已更新 '{plan.manifest.name}' → v{plan.manifest.version}")
        if plan.has_cron:
            print(f"  cron 文件已刷新。查看方式：  hermes -p {plan.manifest.name} cron list")
    except (DistributionError, ValueError) as e:
        _die(f"错误：{e}")


_INFO_FIELDS = (
    ("description", "描述：        "),
    ("author", "作者：        "),
    ("license", "许可证：      "),
    ("hermes_requires", "依赖：        Hermes "),
    ("source", "来源：        "),
    ("installed_at", "安装时间：    "),
)


def _profile_info(args):
    from hermes_cli.profile_distribution import describe_distribution, DistributionError
    try:
        data = describe_distribution(args.profile_name)
    except (DistributionError, ValueError) as e:
        _die(f"错误：{e}")
    if not data:
        print(f"profile '{args.profile_name}' 不是发行版（缺少 distribution.yaml）。")
        return
    print(f"\n发行版：{data.get('name')}")
    print(f"版本：        {data.get('version', '?')}")
    for key, label in _INFO_FIELDS:
        if data.get(key):
            print(f"{label}{data[key]}")
    env_reqs = data.get("env_requires") or []
    if env_reqs:
        print("\n环境变量：")
        for er in env_reqs:
            tag = "必需" if er.get("required", True) else "可选"
            line = f"  {er['name']} ({tag})"
            if er.get("description"):
                line += f" —— {er['description']}"
            print(line)
            if er.get("default") is not None:
                print(f"      默认值：{er['default']}")
    print()


# 顺序与原 if/elif 链一致；None = 裸 ``hermes profile``。
PROFILE_ACTIONS = {
    None: _profile_status,
    'list': _profile_list,
    'use': _profile_use,
    'create': _profile_create,
    'delete': _profile_delete,
    'describe': _profile_describe,
    'show': _profile_show,
    'alias': _profile_alias,
    'rename': _profile_rename,
    'purge-identity': _profile_purge_identity,
    'migrate-identity': _profile_migrate_identity,
    'export': _profile_export,
    'import': _profile_import,
    'install': _profile_install,
    'update': _profile_update,
    'info': _profile_info,
}


def cmd_profile(args):
    """profile 管理——创建、删除、列出、切换、别名。"""
    handler = PROFILE_ACTIONS.get(getattr(args, "profile_action", None))
    if handler is not None:
        return handler(args)
