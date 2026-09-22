"""Chinese display text for the dangerous-command approval card.

DISPLAY-ONLY module. Every key here is an approval / persistence identifier:

* ``PATTERN_KEY_ZH`` keys are Hermes ``DANGEROUS_PATTERNS`` descriptions, which double as
  the ``pattern_key`` persisted into the session/allowslist store. Translating them in place
  would break stored allowlist entries and historical matching, so they are only mapped to
  Chinese at render time.
* ``TIRITH_TITLE_ZH`` keys are rule titles emitted by the external ``tirith`` Rust binary
  (no locale support, 237 rules). Only the danger-relevant categories are enumerated;
  ``tirith`` finding *descriptions* stay in the scanner's original wording.

Unmapped text falls through unchanged — the fallback is a plain pass-through, never an error,
so a new upstream rule can never make the approval card fail to render.
"""

from __future__ import annotations

from typing import Iterable

# ── severity labels: "CRITICAL" -> "严重" ────────────────────────────────────────────────
SEVERITY_ZH = {
    "CRITICAL": "严重",
    "HIGH": "高",
    "MEDIUM": "中",
    "LOW": "低",
    "WARN": "警告",
    "WARNING": "警告",
    "INFO": "提示",
}

# ── Hermes dangerous-pattern descriptions (approval keys — display aliases only) ─────────
PATTERN_KEY_ZH = {
    "delete in root path": "在根路径删除",
    "recursive delete": "递归删除",
    "recursive delete (long flag)": "递归删除（长选项）",
    "recursive delete (flags after operands)": "递归删除（选项在操作数之后）",
    "Windows cmd destructive delete": "Windows cmd 破坏性删除",
    "Windows PowerShell destructive delete": "Windows PowerShell 破坏性删除",
    "PowerShell encoded command execution": "PowerShell 编码命令执行",
    "PowerShell destructive delete (Remove-Item)": "PowerShell 破坏性删除（Remove-Item）",
    "Windows destructive delete (recursive/quiet switch)": "Windows 破坏性删除（递归/静默开关）",
    "pipe remote content to PowerShell (iwr | iex)": "把远程内容管道给 PowerShell（iwr | iex）",
    "execute remote content via Invoke-Expression": "通过 Invoke-Expression 执行远程内容",
    "force kill processes (taskkill /F)": "强制结束进程（taskkill /F）",
    "force kill processes (Stop-Process -Force)": "强制结束进程（Stop-Process -Force）",
    "format filesystem (Format-Volume)": "格式化文件系统（Format-Volume）",
    "wipe disk (Clear-Disk)": "擦除磁盘（Clear-Disk）",
    "disk partitioning (diskpart)": "磁盘分区（diskpart）",
    "format drive (format.com)": "格式化驱动器（format.com）",
    "wipe free space (cipher /w)": "擦除空闲空间（cipher /w）",
    "grant Everyone access (icacls)": "授予 Everyone 访问权（icacls）",
    "reset ACLs recursively (icacls /reset)": "递归重置 ACL（icacls /reset）",
    "delete volume shadow copies (vssadmin)": "删除卷影副本（vssadmin）",
    "delete backups (wbadmin)": "删除备份（wbadmin）",
    "modify boot configuration (bcdedit /set)": "修改启动配置（bcdedit /set）",
    "registry delete (reg delete)": "删除注册表项（reg delete）",
    "registry value delete (Remove-ItemProperty -Force)": "删除注册表值（Remove-ItemProperty -Force）",
    "force stop service (Stop-Service -Force)": "强制停止服务（Stop-Service -Force）",
    "stop/delete service (sc)": "停止/删除服务（sc）",
    "access to SSH keys (Windows path)": "访问 SSH 密钥（Windows 路径）",
    "access to Hermes secrets (Windows path)": "访问 Hermes 密钥（Windows 路径）",
    "world/other-writable permissions": "设置其他人可写权限",
    "recursive world/other-writable (long flag)": "递归设置其他人可写（长选项）",
    "recursive chown to root": "递归 chown 到 root",
    "recursive chown to root (long flag)": "递归 chown 到 root（长选项）",
    "format filesystem": "格式化文件系统",
    "disk copy": "磁盘拷贝",
    "write to block device": "写入块设备",
    "SQL DROP": "SQL DROP 语句",
    "SQL DELETE without WHERE": "无 WHERE 的 SQL DELETE",
    "SQL TRUNCATE": "SQL TRUNCATE 语句",
    "overwrite system config": "覆盖系统配置",
    "stop/restart system service": "停止/重启系统服务",
    "kill all processes": "结束所有进程",
    "force kill processes": "强制结束进程",
    "force kill processes (killall -KILL)": "强制结束进程（killall -KILL）",
    "force kill processes (killall -s KILL)": "强制结束进程（killall -s KILL）",
    "kill processes by regex (killall -r)": "按正则结束进程（killall -r）",
    "fork bomb": "fork 炸弹",
    "pipe remote content to shell": "把远程内容管道给 shell",
    "execute remote script via process substitution": "通过进程替换执行远程脚本",
    "execute remote content via command substitution": "通过命令替换执行远程内容",
    "cloud metadata endpoint access (instance credentials)": "访问云元数据端点（实例凭据）",
    "pipe decoded content to shell (possible command obfuscation)": "把解码内容管道给 shell（疑似命令混淆）",
    "pipe xxd-decoded content to shell (possible command obfuscation)": "把 xxd 解码内容管道给 shell（疑似命令混淆）",
    "pipe tr-transformed output to shell (possible command obfuscation)": "把 tr 转换后的输出管道给 shell（疑似命令混淆）",
    "pipe openssl-decoded content to shell (possible command obfuscation)": "把 openssl 解码内容管道给 shell（疑似命令混淆）",
    "overwrite system file via tee": "通过 tee 覆盖系统文件",
    "overwrite system file via redirection": "通过重定向覆盖系统文件",
    "overwrite project env/config via tee": "通过 tee 覆盖项目 env/config",
    "overwrite project env/config via redirection": "通过重定向覆盖项目 env/config",
    "xargs with rm": "xargs 调用 rm",
    "find -exec/-execdir rm": "find -exec/-execdir 调用 rm",
    "find dynamic shell word may expand to destructive flag": "find 的动态 shell 词可能展开为破坏性选项",
    "find -delete": "find -delete 递归删除",
    "dynamic shell word may expand to arbitrary program execution flag": "动态 shell 词可能展开为任意程序执行选项",
    "stop/restart hermes gateway (kills running agents)": "停止/重启 Hermes 网关（会终止正在运行的代理）",
    "hermes update (restarts gateway, kills running agents)": "hermes update（重启网关，终止正在运行的代理）",
    "docker with remote daemon redirect (-H/--host)": "docker 远程守护进程重定向（-H/--host）",
    "docker with daemon redirect (--context: alternate daemon)": "docker 守护进程重定向（--context：切换到其他守护进程）",
    "docker context use (switches default daemon for future commands)": "docker context use（切换后续命令的默认守护进程）",
    "podman with remote daemon redirect (--url/--connection/--identity)": "podman 远程守护进程重定向（--url/--connection/--identity）",
    "podman remote mode (-r/--remote: remote daemon)": "podman 远程模式（-r/--remote：远程守护进程）",
    "docker/podman daemon redirect via environment (DOCKER_HOST/CONTAINER_HOST)": "通过环境变量重定向 docker/podman 守护进程（DOCKER_HOST/CONTAINER_HOST）",
    "docker compose restart/stop/kill/down (container lifecycle)": "docker compose restart/stop/kill/down（容器生命周期）",
    "docker restart/stop/kill (container lifecycle)": "docker restart/stop/kill（容器生命周期）",
    "start gateway outside systemd (use 'systemctl --user restart hermes-gateway')": "在 systemd 之外启动网关（应使用 'systemctl --user restart hermes-gateway'）",
    "kill hermes/gateway process (self-termination)": "结束 hermes/网关进程（自我终止）",
    "kill process via pgrep/pidof expansion (self-termination)": "通过 pgrep/pidof 展开结束进程（自我终止）",
    "kill process via backtick pgrep/pidof expansion (self-termination)": "通过反引号 pgrep/pidof 展开结束进程（自我终止）",
    "stop/restart hermes launchd service (kills running agents)": "停止/重启 hermes launchd 服务（会终止正在运行的代理）",
    "copy/move file into system config path": "复制/移动文件到系统配置路径",
    "overwrite project env/config file": "覆盖项目 env/config 文件",
    "copy/move file into sensitive credential/SSH/shell-rc path": "复制/移动文件到敏感凭据/SSH/shell-rc 路径",
    "in-place edit of sensitive credential/SSH/shell-rc path": "就地编辑敏感凭据/SSH/shell-rc 路径",
    "in-place edit of sensitive credential/SSH/shell-rc path (long flag)": "就地编辑敏感凭据/SSH/shell-rc 路径（长选项）",
    "in-place edit of sensitive credential/SSH/shell-rc path (perl/ruby)": "就地编辑敏感凭据/SSH/shell-rc 路径（perl/ruby）",
    "in-place edit of system config": "就地编辑系统配置",
    "in-place edit of system config (long flag)": "就地编辑系统配置（长选项）",
    "in-place edit of Hermes config/env": "就地编辑 Hermes 配置/env",
    "in-place edit of Hermes config/env (long flag)": "就地编辑 Hermes 配置/env（长选项）",
    "in-place edit of Hermes config/env (perl/ruby)": "就地编辑 Hermes 配置/env（perl/ruby）",
    "shell execution via heredoc": "通过 heredoc 执行 shell",
    "git reset --hard (destroys uncommitted changes)": "git reset --hard（丢弃未提交的改动）",
    "git force push (rewrites remote history)": "git 强制推送（重写远端历史）",
    "git force push short flag (rewrites remote history)": "git 强制推送短选项（重写远端历史）",
    "git clean with force (deletes untracked files)": "git clean 强制模式（删除未跟踪文件）",
    "git branch force delete": "git 分支强制删除",
    "git branch force delete (long flags)": "git 分支强制删除（长选项）",
    "git branch force delete (long flags, force-first)": "git 分支强制删除（长选项，force 在前）",
    "chmod +x followed by immediate execution": "chmod +x 后立即执行",
    "sudo with privilege flag (stdin/askpass/shell/list)": "带提权选项的 sudo（stdin/askpass/shell/list）",
    "sudo with combined-flag privilege escalation": "合并选项的 sudo 提权",
    "package manager uninstall": "包管理器卸载",
}

# ── tirith rule titles (danger-relevant categories: COMMAND / EXEC / BLAST / PERSISTENCE) ─
TIRITH_RULE_ZH = {
    "pipe_to_interpreter": "管道给 shell 解释器",
    "curl_pipe_shell": "curl 管道给 shell",
    "wget_pipe_shell": "wget 管道给 shell",
    "httpie_pipe_shell": "HTTPie 管道给 shell",
    "xh_pipe_shell": "xh 管道给 shell",
    "dotfile_overwrite": "通过重定向覆盖 dotfile",
    "archive_extract": "解压到敏感路径",
    "proc_mem_access": "通过 /proc 访问进程内存",
    "docker_remote_priv_esc": "Docker 远程守护进程提权",
    "credential_file_sweep": "批量访问凭据文件",
    "base64_decode_execute": "Base64 解码并执行",
    "data_exfiltration": "通过上传外泄数据",
    "reverse_shell": "反弹/绑定 shell",
    "interpreter_suspicious_inline_exec": "内联解释器执行可疑载荷",
    "wrapper_chain_too_deep": "管道给过度混淆的包装链",
    "ps_set_execution_policy_bypass": "PowerShell 绕过执行策略",
    "ps_defender_exclusion": "添加 Windows Defender 排除项",
    "ps_inline_download_execute": "PowerShell 内联下载并执行",
    "sudo_shell_spawn": "sudo 启动交互式 root shell",
    "sudo_env_preserve_sensitive": "sudo -E 将敏感凭据传入特权进程",
    "sudo_tee_system_file": "sudo tee 把可控输入写入受保护的系统路径",
    "sudo_download_install": "sudo 直接把远程内容下载到特权系统路径",
    "sudo_recursive_perms_broad_path": "sudo chmod/chown -R 作用于大范围系统目录树",
    "persistence_shell_rc_modified": "shell rc / profile 文件被修改",
    "persistence_authorized_keys_new_entry": "向 ~/.ssh/authorized_keys 添加新条目",
    "persistence_crontab_modified": "用户 crontab 被修改",
    "persistence_launch_agent_added": "新增 launch agent / systemd 用户单元",
    "persistence_ssh_config_include": "向 ~/.ssh/config 添加 Include 指令",
    "persistence_direnv_new_envrc": "目录树中出现新的 .envrc（direnv）",
    "post_run_shell_rc_modified": "受监视的命令修改了 shell rc / profile 文件",
    "exec_in_tmp": "命令解析到 /tmp 下的可执行文件",
    "exec_recently_modified": "可执行文件在最近 5 分钟内被修改",
    "exec_world_writable": "可执行文件任何人可写",
    "exec_shadows_system_command": "命令解析到系统路径之外",
    "exec_unsigned": "可执行文件没有有效代码签名",
    "exec_in_repo_bin": "命令解析到仓库内的可执行文件",
    "path_writable_dir_before_system": "命令解析自排在系统路径之前的用户可写 PATH 目录",
    "path_duplicate_command_name": "命令名在多个 PATH 目录中都能解析",
    "path_dir_in_repo": "PATH 目录解析到仓库内",
    "path_dir_in_tmp": "PATH 目录位于 /tmp 下",
    "blast_deletes_outside_repo": "破坏性命令波及仓库之外",
    "blast_writes_system_path": "破坏性命令针对系统路径",
    "blast_symlink_traversal": "破坏性命令的目标目录树含符号链接",
    "blast_empty_var_glob": "破坏性命令针对空变量路径",
    "blast_find_delete": "find -delete 会递归删除文件",
    "blast_rsync_delete": "rsync --delete 会修剪目标目录",
    "blast_large_file_count": "破坏性命令影响大量文件",
    "secret_write_then_network": "写入含密文件后 30 秒内发生网络调用",
    "dependency_change_then_network": "修改依赖清单后 60 秒内发生网络调用",
    "delete_then_force_push": "删除文件后 60 秒内执行 git 强制推送",
    "mass_file_deletion": "短时间内大量文件删除",
    "config_injection": "AI 配置文件中存在提示注入",
    "config_suspicious_indicator": "配置文件中存在可疑指示",
    "config_malformed": "配置文件格式错误",
    "config_non_ascii": "ASCII 配置文件中含非 ASCII 字符",
    "config_invisible_unicode": "配置文件中含不可见 Unicode",
    "mcp_insecure_server": "MCP 服务器使用不安全传输",
    "mcp_untrusted_server": "MCP 服务器来自不可信来源",
    "mcp_duplicate_server_name": "MCP 服务器名称重复",
    "mcp_overly_permissive": "MCP 服务器权限过宽",
    "mcp_suspicious_args": "MCP 服务器参数中含 shell 元字符",
    "mcp_server_drift": "MCP 服务器清单与已提交的 lockfile 不一致",
    "agent_instruction_hidden": "AI 代理指令文件中存在隐藏指令",
    "dynamic_code_execution": "动态执行编码载荷的代码",
    "obfuscated_payload": "混淆的代码载荷",
    "suspicious_code_exfiltration": "代码文件中存在可疑网络请求",
    "proxy_env_set": "设置了代理环境变量",
    "sensitive_env_export": "敏感凭据被导出到环境变量",
    "code_injection_env": "通过环境变量注入代码",
    "interpreter_hijack_env": "劫持解释器模块路径",
    "shell_injection_env": "通过启动变量注入 shell",
    "env_sensitive_exposed_to_unknown_script": "敏感环境变量暴露给未知的下载脚本",
    "env_sensitive_persisted_in_shell_rc": "敏感环境变量被写入 shell rc/profile",
    "env_printenv_to_network_sink": "环境变量被发送到网络端点",
    "metadata_endpoint": "访问云元数据端点",
    "private_network_access": "访问私有/保留 IP 地址",
    "command_network_deny": "命中策略网络拒绝清单",
    "credential_in_text": "检测到已知凭据模式",
    "high_entropy_secret": "检测到高熵密钥",
    "private_key_exposed": "私钥暴露",
    "repo_hook_network_call": "仓库 hook 发起网络调用",
    "repo_hook_credential_read": "仓库 hook 读取凭据文件",
    "repo_hook_sudo": "仓库 hook 使用 sudo",
    "repo_hook_suspicious_shell_pattern": "仓库 hook 使用可疑 shell 模式",
    "repo_hook_external_fetch": "仓库 hook 拉取外部资源",
}

# Longest first so `recursive delete (long flag)` wins over `recursive delete`.
_REPLACEMENTS: list[tuple[str, str]] = sorted(
    [(en, zh) for en, zh in PATTERN_KEY_ZH.items()],
    key=lambda kv: -len(kv[0]),
)

_PREFIXES: list[tuple[str, str]] = [
    ("Security scan — ", "安全扫描 — "),
    ("Security scan: ", "安全扫描："),
    ("security issue detected", "检测到安全问题"),
]


def _iter_replacements() -> Iterable[tuple[str, str]]:
    return _REPLACEMENTS


def localize_pattern_key(key: str) -> str:
    """Chinese display alias for an approval key (display only — never persisted).

    Named keys come from ``PATTERN_KEY_ZH``; ``tirith:<rule_id>`` entries go through the rule
    map. Unknown keys are returned unchanged so the card still shows the raw identifier rather
    than a blank. Callers MUST keep persisting the original key.
    """
    if not key:
        return key
    if key.startswith("tirith:"):
        rule = key.split(":", 1)[1]
        return f"tirith:{TIRITH_RULE_ZH.get(rule, rule)}"
    return PATTERN_KEY_ZH.get(key, key)


def tirith_rule_name(rule_id: str, fallback_title: str = "") -> str:
    """Chinese name for a tirith finding, keyed by rule_id.

    The scanner emits its own (sometimes parameterised) English ``title`` at runtime, so the
    rule id is the only stable handle. Returns "" when the rule is unmapped, letting the
    caller keep the scanner's own wording.
    """
    return TIRITH_RULE_ZH.get(rule_id or "", "")


def localize_approval_text(text: str) -> str:
    """Map known English approval wording to Chinese for display only.

    Keys (``pattern_key``) are never modified here — callers must keep using the original
    ``pattern_key`` for persistence, allowlist lookups and hook payloads. Unknown text is
    returned unchanged so an upstream wording change degrades to English, never to an error.
    """
    if not text:
        return text
    for en, zh in _PREFIXES:
        if en in text:
            text = text.replace(en, zh)
    for en, zh in _REPLACEMENTS:
        if en in text:
            text = text.replace(en, zh)
    for en, zh in SEVERITY_ZH.items():
        marker = f"[{en}]"
        if marker in text:
            text = text.replace(marker, f"[{zh}]")
    return text
