"""Autonomy-loop gateway commands: /goal, /subgoal, /heartbeat, /loop, /refine, /review.
Bound onto ``GatewayRunner`` through ``GatewaySlashCommandsMixin``."""

from __future__ import annotations

import logging

from agent.i18n import t
from gateway.platforms.event import MessageEvent, MessageType

# Log-record parity with gateway/run.py and the origin module.
logger = logging.getLogger("gateway.run")


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}{'s' if n != 1 else ''}"


def _quiet_bool(fn) -> bool:
    try:
        return bool(fn())
    except Exception:
        return False


def _mgr_call(prefix: str, fn, *args, errors=(RuntimeError, ValueError)):
    """``(result, None)`` from ``fn(*args)``, or ``(None, "<prefix>: <exc>")`` on a manager error."""
    try:
        return fn(*args), None
    except errors as exc:
        return None, f"{prefix}: {exc}"


class GatewayGoalCommandsMixin:
    """Autonomy-loop gateway commands: /goal, /subgoal, /heartbeat, /loop, /refine, /review."""

    async def _handle_goal_command(self, event: MessageEvent) -> str:
        from hermes_cli.goal_command import dispatch_goal_command
        from hermes_cli.goals import last_user_message_from_db

        mgr, _session_entry = await self._get_goal_manager_for_event(event)
        if mgr is None:
            return t("gateway.goal.unavailable")

        def authorize_gate():
            if not self._resume_caller_is_admin(event.source):
                return ("⛔ /goal gate add 需要显式配置的 "
                        "网关管理员（私聊用 allow_admin_from，"
                        "群聊用 group_allow_admin_from）。")
            return None

        def dispatch():
            return dispatch_goal_command(
                mgr, event.get_command_args() or "", authorize_gate=authorize_gate,
                render=lambda key, default, **values: t(key, **values),
                last_user_message=last_user_message_from_db(getattr(mgr, "session_id", None)),
            )

        # Drafting resolves profile-scoped credentials. Keep ContextVars across the
        # executor hop; manager I/O must also stay off the messaging event loop.
        result = await self._run_in_executor_with_context(dispatch)
        if result.clear_pending:
            self._clear_goal_continuations(event, result.clear_pending)
        if result.prompt:
            self._enqueue_goal_turn(event, result.prompt, label="command enqueue", kickoff=result.kickoff)
        return result.output

    def _clear_goal_continuations(self, event: MessageEvent, verb: str) -> None:
        try:
            adapter, quick_key = self._adapter_and_key_for(event)
            if adapter and quick_key:
                self._clear_goal_pending_continuations(quick_key, adapter)
        except Exception as exc:
            logger.debug("goal %s: pending continuation cleanup failed: %s", verb, exc)

    def _enqueue_goal_turn(
        self, event: MessageEvent, text: str, *, label: str, kickoff: bool
    ) -> None:
        """Enqueue *text* as the next turn through the adapter FIFO (the post-turn judge's path).

        A kickoff keeps the triggering message id / channel prompt; a resume continuation carries
        none. Best-effort: failures only logged.
        """
        try:
            adapter, quick_key = self._adapter_and_key_for(event)
            if text and adapter and quick_key:
                turn = MessageEvent(
                    text=text,
                    message_type=MessageType.TEXT,
                    source=event.source,
                    message_id=event.message_id if kickoff else None,
                    channel_prompt=event.channel_prompt if kickoff else None,
                )
                self._enqueue_fifo(quick_key, turn, adapter)
        except Exception as exc:
            logger.debug("goal %s failed: %s", label, exc)

    async def _handle_heartbeat_command(self, event: MessageEvent) -> str:
        """Handle /heartbeat (mirror of the CLI handler): the session's one recurring re-entry
        prompt. The gateway-wide poller injects due heartbeats through the adapter FIFO as
        ordinary user turns, so alternation and caching hold."""
        from hermes_cli.heartbeat import parse_interval, format_interval, MIN_INTERVAL_SECONDS
        args = (event.get_command_args() or "").strip()
        lower = args.lower()
        mgr, _session_entry = await self._get_heartbeat_manager_for_event(event)
        if mgr is None:
            return "心跳不可用（无会话）。"
        quick_key = self._session_key_for_source(event.source) if event.source else None

        def _watch():
            if quick_key and event.source is not None:
                self._register_heartbeat_watch(quick_key, event.source, mgr.session_id)

        if not args or lower == "status":
            return mgr.status_line()
        if lower == "pause":
            state = mgr.pause()
            return f"⏸ 心跳已暂停：{state.prompt}" if state else "未设置心跳。"
        if lower == "resume":
            state = mgr.resume()
            if state is None:
                return "没有可恢复的心跳。"
            _watch()
            return f"▶ 心跳已恢复（每 {format_interval(state.interval_seconds)}）：{state.prompt}"
        if lower in {"clear", "stop", "off"}:
            had = mgr.clear()
            if quick_key:
                self._unregister_heartbeat_watch(quick_key)
            return "✓ 心跳已清除。" if had else "未设置心跳。"

        # Set: `/heartbeat every 10m <prompt>` (also accepts `10m <prompt>`).
        tokens = args.split(None, 2)
        interval, prompt = None, ""
        if tokens[0].lower() == "every" and len(tokens) >= 2:
            interval = parse_interval(f"every {tokens[1]}")
            prompt = tokens[2] if len(tokens) > 2 else ""
        else:
            interval = parse_interval(tokens[0])
            prompt = args[len(tokens[0]):].strip() if interval and interval > 0 else ""
        if interval is None:
            return (
                "用法：/heartbeat every <interval> <prompt>  （例如 /heartbeat every 10m Check CI）\n"
                "其他：/heartbeat status | pause | resume | clear"
            )
        if interval < 0:
            return f"间隔太短——最小为 {MIN_INTERVAL_SECONDS}s。"
        if not prompt.strip():
            return "用法：/heartbeat every <interval> <prompt>——必须提供提示词。"
        state, err = _mgr_call("无效的心跳", mgr.set, prompt, interval, errors=(ValueError,))
        if err:
            return err
        _watch()
        return (
            f"♥ 心跳已设置（每 {format_interval(state.interval_seconds)}）：{state.prompt}\n"
            "本会话空闲且间隔到时后，它将作为一次普通对话触发。仅在网关运行期间有效——"
            "如需持久化定时，请使用 `hermes cron`。"
        )

    def _idle_cached_agent_or_error(self, event: MessageEvent, verb: str):
        """``(session_key, cached_agent, None)`` for /refine and /review, or ``(_, _, error_text)``:
        both need a cached agent from a completed turn and refuse while a run is in flight."""
        quick_key = self._session_key_for_source(event.source) if event.source else None
        if not quick_key:
            return None, None, f"/{verb} 不可用（无会话）。"
        if quick_key in self._running_agents:
            return quick_key, None, f"代理正在运行 —— 请等本回合结束，然后执行 /{verb}。"
        agent = self._cached_agent_for(quick_key)
        if agent is None:
            return quick_key, None, f"暂无可 {verb} 的内容——请先发送一条消息。"
        return quick_key, agent, None

    async def _handle_refine_command(self, event: MessageEvent) -> str:
        """Handle /refine — run the memory/skill review fork on demand, in a daemon thread against a
        snapshot of the cached AIAgent's conversation (live session and prompt cache untouched)."""
        args = (event.get_command_args() or "").strip()
        _quick_key, agent, error = self._idle_cached_agent_or_error(event, "refine")
        if error:
            return error
        snapshot = list(getattr(agent, "_session_messages", None) or [])
        if not snapshot:
            return "暂无可精简的内容——对话为空。"
        try:
            agent._spawn_background_review(
                messages_snapshot=snapshot, review_memory=True,
                review_skills="skill_manage" in getattr(agent, "valid_tool_names", set()), focus=args or None,
            )
        except Exception as exc:
            return f"/refine 启动失败：{exc}"
        tail = f"（重点：{args}）" if args else ""
        return (
            f"⚗ 正在后台审查本次对话{tail}——"
            f"完成后会报告任何记忆/技能更新。"
        )

    async def _handle_review_command(self, event: MessageEvent) -> str:
        """Handle /review — spawn an independent reviewer subagent. The approval session-key
        contextvar is only bound during agent turns, so bind it explicitly here or the completion
        event carries no gateway route and never re-enters this chat."""
        args = (event.get_command_args() or "").strip()
        quick_key, agent, error = self._idle_cached_agent_or_error(event, "review")
        if error:
            return error
        snapshot = list(getattr(agent, "_session_messages", None) or [])
        from tools.approval_context import reset_current_session_key, set_current_session_key

        def _dispatch():
            token = set_current_session_key(quick_key)
            try:
                from agent.review_engine import start_review
                return start_review(agent, snapshot, args)
            finally:
                reset_current_session_key(token)

        try:
            # _run_in_executor_with_context, not a bare hop: the reviewer subagent is spawned from
            # the worker and inherits its context; a bare hop would run it under the launch home.
            result = await self._run_in_executor_with_context(_dispatch)
        except ValueError as exc:
            return str(exc)
        except Exception as exc:
            return f"/review 启动失败：{exc}"
        from agent.review_engine import format_dispatch_note
        return format_dispatch_note(result, args)

    async def _handle_subgoal_command(self, event: MessageEvent) -> str:
        """Handle /subgoal (mirror of the CLI handler): extra criteria appended to the active goal
        mid-loop. They modify state read at the next turn boundary, so this is safe while the
        agent is running."""
        args = (event.get_command_args() or "").strip()
        mgr, _session_entry = await self._get_goal_manager_for_event(event)
        if mgr is None:
            return t("gateway.goal.unavailable")
        if not mgr.has_goal():
            return "没有进行中的目标。请用 /goal <text> 设置一个。"
        if not args:
            return f"{mgr.status_line()}\n{mgr.render_subgoals()}"
        tokens = args.split(None, 1)
        verb = tokens[0].lower()
        rest = tokens[1].strip() if len(tokens) > 1 else ""
        if verb == "remove":
            if not rest:
                return "用法：/subgoal remove <n>"
            try:
                idx = int(rest.split()[0])
            except ValueError:
                return "/subgoal remove：<n> 必须是整数（从 1 开始）。"
            removed, err = _mgr_call(
                "/subgoal remove", mgr.remove_subgoal, idx, errors=(IndexError, RuntimeError)
            )
            return err or f"✓ 已移除子目标 {idx}：{removed}"
        if verb == "clear":
            prev, err = _mgr_call("/subgoal clear", mgr.clear_subgoals, errors=(RuntimeError,))
            if err:
                return err
            return f"✓ 已清除 {prev} 个子目标。" if prev else "没有可清除的子目标。"
        text, err = _mgr_call("/subgoal", mgr.add_subgoal, args)
        if err:
            return err
        idx = len(mgr.state.subgoals) if mgr.state else 0
        return f"✓ 已添加子目标 {idx}：{text}"

    async def _handle_loop_command(self, event: MessageEvent) -> str:
        """Handle /loop — recurring in-session wakeups, via ``dispatch_loop_command`` (CLI mirror)."""
        try:
            from hermes_cli.loops import LoopManager, dispatch_loop_command, goal_blocks_loop_tick
        except Exception as exc:
            logger.debug("loops module unavailable: %s", exc)
            return "循环不可用。"

        # Warm the SessionDB cache off-loop: a cold cache drops the first /loop write while the
        # reply claims the loop was set (same class as the /goal false-ack fix).
        await self._warm_goals_session_db("loop manager")
        try:
            session_entry = await self.async_session_store.get_or_create_session(event.source)
        except Exception:
            session_entry = None
        sid = getattr(session_entry, "session_id", None) or ""
        if not sid:
            return "循环不可用（无活动会话）。"
        mgr = LoopManager(session_id=sid)

        # New loops capture the event's routing so the idle loop-wakeup watcher can inject ticks
        # here after a restart; best-effort, empty fields dropped. ``profile`` pins the wakeup to this
        # session's own bot under multiplex (the watcher must never fire it through the default bot).
        route: dict = {}
        try:
            src = event.source
            if src is not None:
                platform = getattr(src, "platform", "")
                route = {"platform": platform.value if hasattr(platform, "value") else str(platform or "")}
                for key in ("chat_id", "chat_type", "thread_id", "user_id", "user_name", "profile"):
                    route[key] = str(getattr(src, key, "") or "")
                route = {k: v for k, v in route.items() if v}
        except Exception:
            route = {}
        result = dispatch_loop_command(mgr, (event.get_command_args() or "").strip(), route=route)
        output = result.get("output") or ""
        if result.get("created") and _quiet_bool(lambda: goal_blocks_loop_tick(mgr.session_id)):
            output += (
                "\n注意：本会话正由进行中的 /goal 驱动——循环"
                "唤醒将推迟到该目标完成、暂停或搁置之后。"
            )
        return output
