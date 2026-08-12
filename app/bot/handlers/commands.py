"""General bot commands: /start /help /status /rules /allow /block /settings
/authstatus /logout /admin /history /sync."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from app.bot.formatters import build_confirm_keyboard
from app.config import Settings
from app.database.models import RuleKind, RuleScope
from app.rules import repository as rules_repo
from app.services.queue import enqueue
from app.services.session_state import mark_session_revoked, session_revoked
from app.services.status_service import collect_status, get_mtproto_state

logger = logging.getLogger("app.bot.handlers.commands")

router = Router(name="commands")

HELP_TEXT = (
    "Telegram Message Analyzer\n\n"
    "Commands:\n"
    "/check — analyze a draft message (never sent anywhere)\n"
    "/addchat &lt;@username|t.me link|invite link|chat id&gt; — add target chat\n"
    "/removechat &lt;ref&gt; — remove target chat\n"
    "/listchats — list configured chats\n"
    "/chatinfo &lt;ref&gt; — verify access to a chat\n"
    "/enablechat &lt;ref&gt; / /disablechat &lt;ref&gt;\n"
    "/sync [ref|all] [initial|incremental|resync] — history sync\n"
    "/history — sync status of all chats\n"
    "/status — system status\n"
    "/rules [@chat] — list moderation rules\n"
    "/allow &lt;word&gt; — allowlist term\n"
    "/block &lt;word|regex:pattern&gt; — block term or regex\n"
    "/settings — analysis configuration\n"
    "/authstatus — MTProto connection status\n"
    "/logout — revoke the scanner session (with confirmation)\n"
    "/admin — admin panel summary"
)


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer("Welcome to the Telegram Message Analyzer.\n\n" + HELP_TEXT)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    status = await collect_status(None)
    if status["worker_heartbeat_age"] is not None:
        worker_line = f"Worker heartbeat age: {status['worker_heartbeat_age']:.0f}s"
    else:
        worker_line = "Worker heartbeat age: n/a (worker not running)"
    mtproto = status["mtproto"]
    configured_raw = mtproto["configured"]
    configured_label = "unknown" if configured_raw is None else str(configured_raw)
    session_present = mtproto["session_present"]
    stale = mtproto.get("stale", False)
    if stale:
        session_label = "unknown (stale)"
    elif session_present is None:
        session_label = "unknown"
    elif session_present:
        session_label = "present"
    else:
        session_label = "absent"
    text = (
        f"🖥 System status\n"
        f"Environment: {status['service']}\n"
        f"Database: {status['database']}\n"
        f"Redis: {status['redis']}\n"
        f"MTProto: {'CONNECTED' if mtproto['connected'] else 'DISCONNECTED'}\n"
        f"  configured={configured_label} "
        f"session={session_label}\n"
        f"Bot API: {'configured' if status['bot_api']['configured'] else 'not configured'}\n"
        f"{worker_line}\n"
        f"MTProto state is reported by the worker process."
    )
    await message.answer(text)


@router.message(Command("history"))
async def cmd_history(message: Message, chats) -> None:
    user_id = message.from_user.id if message.from_user else 0
    chats_list = await chats.list_chats(user_id)
    if not chats_list:
        await message.answer("No chats configured.")
        return
    lines = ["🗂 History sync state"]
    for chat in chats_list:
        lines.append(
            f"• {chat.title or chat.telegram_chat_id}: {chat.sync_state} "
            f"(indexed={chat.sync_indexed_count}, cursor={chat.sync_cursor or 0}"
            + (f", topic={chat.topic_id}" if chat.topic_id else "")
            + ")"
        )
        if chat.sync_error:
            lines.append(f"  error: {chat.sync_error}")
    await message.answer("\n".join(lines))


@router.message(Command("sync"))
async def cmd_sync(message: Message, command: CommandObject, chats) -> None:
    user_id = message.from_user.id if message.from_user else 0
    args = (command.args or "").split()
    target = args[0] if args else "all"
    mode = args[1] if len(args) > 1 else "incremental"
    if mode not in ("initial", "incremental", "resync"):
        await message.answer("Mode must be one of: initial | incremental | resync")
        return
    if target == "all":
        chats_list = await chats.list_chats(user_id)
        enabled = [c for c in chats_list if c.enabled]
        for chat in enabled:
            await enqueue("sync_chat", chat.telegram_chat_id, mode)
        await message.answer(f"Queued {len(enabled)} chat syncs ({mode}).")
        return
    resolved = await _resolve_reference(target, chats, user_id)
    if resolved is None:
        await message.answer("Chat not found. Check /listchats.")
        return
    await enqueue("sync_chat", resolved, mode)
    await message.answer(f"Queued sync ({mode}) for chat {resolved}.")


@router.message(Command("rules"))
async def cmd_rules(message: Message, command: CommandObject) -> None:
    arg = (command.args or "").strip()
    rules = await rules_repo.list_rules()
    if arg:
        ref = arg.lstrip("@")
        rules = [
            r
            for r in rules
            if r.scope == RuleScope.CHAT.value
            and ref.isdigit()
            and r.chat_id == int(ref)
        ]
    if not rules:
        await message.answer("No rules configured.")
        return
    lines = ["📜 Rules"]
    for rule in rules[:30]:
        flag = "ALLOW" if rule.is_allowlist else "BLOCK"
        scope = f"chat:{rule.chat_id}" if rule.chat_id else "global"
        lines.append(
            f"• [{flag}] {rule.kind} {rule.pattern!r} ({rule.category}) scope={scope}"
            + (" [disabled]" if not rule.enabled else "")
        )
    if len(rules) > 30:
        lines.append(f"...and {len(rules) - 30} more.")
    await message.answer("\n".join(lines))


@router.message(Command("allow"))
async def cmd_allow(message: Message, command: CommandObject) -> None:
    pattern = (command.args or "").strip()
    if not pattern:
        await message.answer("Usage: /allow <term>")
        return
    await rules_repo.create_rule(
        scope=RuleScope.GLOBAL.value,
        kind=RuleKind.EXACT.value,
        pattern=pattern,
        is_allowlist=True,
        created_by=str(message.from_user.id),
    )
    await message.answer(f"Allowlisted: {pattern!r}")


@router.message(Command("block"))
async def cmd_block(message: Message, command: CommandObject) -> None:
    arg = (command.args or "").strip()
    if not arg:
        await message.answer("Usage: /block <term>  or  /block regex:<pattern>")
        return
    kind = RuleKind.REGEX.value if arg.startswith("regex:") else RuleKind.EXACT.value
    pattern = arg[6:].strip() if kind == RuleKind.REGEX.value else arg
    if kind == RuleKind.REGEX.value:
        import regex

        try:
            regex.compile(pattern)
        except Exception as exc:  # noqa: BLE001
            await message.answer(f"Invalid regex: {exc}")
            return
    await rules_repo.create_rule(
        scope=RuleScope.GLOBAL.value,
        kind=kind,
        pattern=pattern,
        created_by=str(message.from_user.id),
    )
    await message.answer(f"Blocked ({kind}): {pattern!r}")


@router.message(Command("settings"))
async def cmd_settings(message: Message, config: Settings) -> None:
    w = config.risk_weights
    text = (
        f"⚙️ Analysis settings\n"
        f"Fuzzy threshold: {config.fuzzy_threshold}\n"
        f"History search limit: {config.history_search_limit}\n"
        f"Max message chars: {config.max_message_chars}\n"
        f"Retention (days): {config.data_retention_days}\n"
        f"LLM provider: {config.llm_provider}\n"
        f"Weights: explicit={w.explicit_rule} regex={w.regex} fuzzy={w.fuzzy} "
        f"ai={w.ai} unseen={w.unseen} rare={w.rare} frequent_use={w.frequent_use} "
        f"explicit_floor={w.explicit_floor}"
    )
    await message.answer(text)


@router.message(Command("authstatus"))
async def cmd_authstatus(message: Message) -> None:
    # Session state is worker-reported: connection state + session presence
    # come from Redis (the worker publishes them), the revocation flag from
    # Postgres. The bot has no session material of its own.
    state = await get_mtproto_state()
    revoked = await session_revoked()
    connected = bool(state.get("connected"))
    last_connected = state.get("last_connected") or "never (no worker report yet)"
    stale = state.get("stale", False)

    if revoked:
        session_label = "REVOKED"
    elif not state.get("available") or state.get("session_present") is None:
        session_label = "UNKNOWN"
    elif stale:
        session_label = "UNKNOWN (stale)"
    elif state.get("session_present"):
        session_label = "PRESENT"
    else:
        session_label = "ABSENT"

    text = (
        "🔐 MTProto status\n"
        f"Connected (worker): {'CONNECTED' if connected else 'DISCONNECTED'}\n"
        f"Account: {state.get('username') or 'None'}\n"
        f"Session: {session_label}\n"
        f"Last successful connection: {last_connected}"
    )
    await message.answer(text)


@router.message(Command("logout"))
async def cmd_logout(message: Message) -> None:
    await message.answer(
        "⚠️ This revokes the Telegram scanner session. The bot will lose "
        "MTProto access until a new session is provisioned. Continue?",
        reply_markup=build_confirm_keyboard(),
    )


@router.callback_query(F.data == "confirm_logout")
async def cb_confirm_logout(callback: CallbackQuery) -> None:
    await callback.answer("Revoking…")
    queued = await enqueue("revoke_session", job_id="revoke-session")
    if queued:
        await callback.message.answer(
            "Session revocation queued. The worker will disconnect and "
            "wipe the session file."
        )
    else:
        # Redis is down: mark revoked in the database now (Postgres only —
        # the bot has no session file). The worker refuses to reconnect
        # once it sees the flag.
        try:
            await mark_session_revoked()
            await callback.message.answer(
                "Session marked REVOKED (Redis unavailable — worker could "
                "not be notified; it will refuse to reconnect)."
            )
        except Exception as exc:  # noqa: BLE001
            await callback.message.answer(f"Revocation failed: {type(exc).__name__}")
    await callback.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data == "cancel_logout")
async def cb_cancel_logout(callback: CallbackQuery) -> None:
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Cancelled.")


@router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    status = await collect_status(None)
    stats = status["analysis"]
    if status["worker_heartbeat_age"] is not None:
        worker_line = f"Worker heartbeat: {status['worker_heartbeat_age']:.0f}s ago"
    else:
        worker_line = "Worker heartbeat: n/a"
    text = (
        "🛠 Admin panel\n"
        f"Database: {status['database']} | Redis: {status['redis']}\n"
        f"MTProto: {'CONNECTED' if status['mtproto']['connected'] else 'DOWN'}\n"
        f"{worker_line}\n"
        f"Analysis jobs: {stats['total']} total "
        f"(queued={stats['queued']}, running={stats['running']}, failed={stats['failed']})\n"
        "Full status: GET /health on the API service."
    )
    await message.answer(text)


async def _resolve_reference(reference: str, chats, user_id: int) -> int | None:
    ref = reference.strip().lstrip("@")
    chats_list = await chats.list_chats(user_id)
    if ref.isdigit():
        chat_id = int(ref)
        if any(c.telegram_chat_id == chat_id or c.id == chat_id for c in chats_list):
            return chat_id
    for chat in chats_list:
        if chat.username and chat.username.lower() == ref.lower():
            return chat.telegram_chat_id
        if str(chat.telegram_chat_id) == ref:
            return chat.telegram_chat_id
    return None
