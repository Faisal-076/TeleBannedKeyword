"""Admin authorization commands: /authorize /revoke /authorized /checkuser /authlogs.

Root admin only.  Confirmation flows use server-side ``PendingAction``
records so callback data alone cannot mutate the authorization table.
"""

from __future__ import annotations

import logging
from html import escape as html_escape

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot.middleware import is_root_admin
from app.services import authorization as auth_svc

logger = logging.getLogger("app.bot.handlers.auth_commands")

router = Router(name="auth_commands")

_PAGE_SIZE = 20
_UNAUTHORIZED_RESPONSE = (
    "You are not authorized to use this bot. Please contact an administrator."
)


# --------------------------------------------------------------- FSM


class AuthFlow(StatesGroup):
    waiting_for_user_id = State()
    waiting_for_revoke_id = State()


def _admin_only(message: Message) -> bool:
    if not is_root_admin(message.from_user.id if message.from_user else None):
        return False
    return True


# --------------------------------------------------------------- /authorize


@router.message(Command("authorize"))
async def cmd_authorize(message: Message, state: FSMContext) -> None:
    if not _admin_only(message):
        await message.answer("Root admin only.")
        return
    await state.set_state(AuthFlow.waiting_for_user_id)
    await message.answer("Send the Telegram numeric user ID you want to authorize.")


@router.message(AuthFlow.waiting_for_user_id)
async def step_authorize_user_id(message: Message, state: FSMContext) -> None:
    if not _admin_only(message):
        await state.clear()
        await message.answer(_UNAUTHORIZED_RESPONSE)
        return

    raw = message.text.strip() if message.text else ""
    try:
        target_id = int(raw)
    except ValueError:
        await message.answer("Invalid user ID. Send a numeric Telegram user ID, or /cancel.")
        return

    if target_id <= 0:
        await message.answer("Invalid user ID. Must be a positive integer.")
        return

    actor_id = message.from_user.id
    if target_id == actor_id:
        await message.answer("You are already a root admin; you don't need to authorize yourself.")
        await state.clear()
        return

    # Prevent a non-root-admin from authorizing through this flow — but the
    # admin-only guard at the top catches that.  Also prevent root admins from
    # being added to the DB table (they are authoritative via ADMIN_USER_IDS).
    if is_root_admin(target_id):
        await message.answer(
            f"User {target_id} is already a root administrator (ADMIN_USER_IDS)."
        )
        await state.clear()
        return

    existing = await auth_svc.get_user_authorization(target_id)
    if existing and existing["status"] == "authorized":
        await message.answer(
            f"User {target_id} ({existing.get('username') or 'no username'}) "
            f"is already authorized."
        )
        await state.clear()
        return

    username = message.text.split(maxsplit=1)[0] if message.text else None
    if username and not username.lstrip("-").isdigit():
        username = None  # not a username, it's the numeric ID

    token = await auth_svc.create_pending_action(
        "authorize",
        actor_id=actor_id,
        target_user_id=target_id,
        target_username=None,
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="Authorize", callback_data=f"auth:confirm:{token}")
    kb.button(text="Cancel", callback_data=f"auth:cancel:{token}")
    kb.adjust(2)

    await state.clear()
    await message.answer(
        f"Authorize this user?\n\n"
        f"User ID: {html_escape(str(target_id))}\n"
        f"Status: currently unauthorized",
        reply_markup=kb.as_markup(),
    )


# --------------------------------------------------------------- /revoke


@router.message(Command("revoke"))
async def cmd_revoke(message: Message, state: FSMContext) -> None:
    if not _admin_only(message):
        await message.answer("Root admin only.")
        return
    await state.set_state(AuthFlow.waiting_for_revoke_id)
    await message.answer("Send the Telegram numeric user ID to revoke.")


@router.message(AuthFlow.waiting_for_revoke_id)
async def step_revoke_user_id(message: Message, state: FSMContext) -> None:
    if not _admin_only(message):
        await state.clear()
        await message.answer(_UNAUTHORIZED_RESPONSE)
        return

    raw = message.text.strip() if message.text else ""
    try:
        target_id = int(raw)
    except ValueError:
        await message.answer("Invalid user ID. Send a numeric Telegram user ID, or /cancel.")
        return

    if target_id <= 0:
        await message.answer("Invalid user ID.")
        return

    actor_id = message.from_user.id
    if target_id == actor_id:
        await message.answer("Cannot revoke yourself.")
        await state.clear()
        return

    # Root admins cannot be revoked through this flow.
    if is_root_admin(target_id):
        await message.answer("Cannot revoke a root administrator (ADMIN_USER_IDS).")
        await state.clear()
        return

    info = await auth_svc.get_user_authorization(target_id)
    if info is None or info["status"] != "authorized":
        await message.answer(
            f"User {target_id} is not currently authorized (or not in the table)."
        )
        await state.clear()
        return

    token = await auth_svc.create_pending_action(
        "revoke",
        actor_id=actor_id,
        target_user_id=target_id,
        target_username=info.get("username"),
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="Revoke", callback_data=f"auth:confirm:{token}")
    kb.button(text="Cancel", callback_data=f"auth:cancel:{token}")
    kb.adjust(2)

    await state.clear()
    await message.answer(
        f"Revoke authorization for this user?\n\n"
        f"User ID: {html_escape(str(target_id))}\n"
        f"Username: {info.get('username') or 'n/a'}",
        reply_markup=kb.as_markup(),
    )


# --------------------------------------------------------------- callback handler


@router.callback_query(F.data.startswith("auth:"))
async def handle_auth_callback(callback: CallbackQuery) -> None:
    """Process [Authorize] / [Revoke] / [Cancel] inline buttons."""
    parts = callback.data.split(":")
    if len(parts) < 3:
        await callback.answer("Invalid action.", show_alert=True)
        return

    verb = parts[1]       # "confirm" | "cancel"
    token = parts[2]      # pending-action token

    if verb == "cancel":
        await auth_svc.delete_pending_action(token)
        await _edit_or_drop(callback, "\n\nCancelled.")
        await callback.answer("Cancelled.")
        return

    # --- confirm ---
    actor_id = callback.from_user.id if callback.from_user else None
    if actor_id is None:
        await callback.answer("Cannot identify you.", show_alert=True)
        return

    if not is_root_admin(actor_id):
        await callback.answer("Root admin only.", show_alert=True)
        return

    pending = await auth_svc.resolve_pending_action(token)
    if pending is None:
        await callback.answer("Request expired or invalid. Start again.", show_alert=True)
        await _edit_or_drop(callback, "\n\nRequest expired. Start again.")
        return

    if pending.actor_id != actor_id:
        await callback.answer("This confirmation belongs to another admin.", show_alert=True)
        return

    if pending.action == "authorize":
        await auth_svc.authorize_user(
            pending.target_user_id,
            actor_id,
            username=pending.target_username,
        )
        await _edit_or_drop(
            callback,
            f"\n\n✅ User authorized.\n"
            f"ID: {html_escape(str(pending.target_user_id))}\n"
            f"Authorized by: {html_escape(str(actor_id))}",
        )
        await _notify_user(pending.target_user_id,
                           "You have been authorized to use TeleBannedKeyword.")

    elif pending.action == "revoke":
        row = await auth_svc.revoke_user(pending.target_user_id, actor_id)
        if row is None:
            await _edit_or_drop(
                callback, "\n\nUser was not authorized (already revoked or not in table)."
            )
        else:
            await _edit_or_drop(callback, "\n\n✅ Authorization revoked.")
        await _notify_user(pending.target_user_id,
                           "You are no longer authorized to use TeleBannedKeyword.")

    await auth_svc.delete_pending_action(token)
    await callback.answer()


# --------------------------------------------------------------- /authorized


@router.message(Command("authorized"))
async def cmd_authorized(message: Message) -> None:
    if not _admin_only(message):
        await message.answer("Root admin only.")
        return

    users = await auth_svc.list_authorized_users(limit=_PAGE_SIZE)
    if not users:
        await message.answer("No authorized users in the database.")
        return

    lines = ["Authorized users:\n"]
    for i, u in enumerate(users, start=1):
        username = f"@{u['username']}" if u.get("username") else str(u["telegram_user_id"])
        status = "REVOKED" if u["status"] == "revoked" else "AUTHORIZED"
        lines.append(f"{i}. {username} — {u['telegram_user_id']} — {status}")
    await message.answer("\n".join(lines))


# --------------------------------------------------------------- /checkuser


@router.message(Command("checkuser"))
async def cmd_checkuser(message: Message, command: CommandObject) -> None:
    if not _admin_only(message):
        await message.answer("Root admin only.")
        return

    raw = command.args.strip() if command.args else ""
    if not raw:
        await message.answer("Usage: /checkuser <telegram_user_id>")
        return

    try:
        target_id = int(raw.split()[0])
    except ValueError:
        await message.answer("Invalid numeric user ID.")
        return

    info = await auth_svc.get_user_authorization(target_id)
    if info is None:
        await message.answer(
            f"User {html_escape(str(target_id))} not found in the authorization table.\n"
            f"(Root admins in ADMIN_USER_IDS are always authorized.)"
        )
        return

    await message.answer(
        f"User ID: {html_escape(str(info['telegram_user_id']))}\n"
        f"Username: {info.get('username') or 'n/a'}\n"
        f"Status: {info.get('status', 'n/a')}\n"
        f"Role: {info.get('role', 'n/a')}\n"
        f"Authorized at: {info.get('authorized_at') or 'n/a'}\n"
        f"Authorized by: {info.get('authorized_by') or 'n/a'}",
    )


# --------------------------------------------------------------- /authlogs


@router.message(Command("authlogs"))
async def cmd_authlogs(message: Message) -> None:
    if not _admin_only(message):
        await message.answer("Root admin only.")
        return

    entries = await auth_svc.get_auth_audit_log(limit=_PAGE_SIZE)
    if not entries:
        await message.answer("No authorization audit events yet.")
        return

    lines = ["Recent authorization events:\n"]
    for e in entries:
        details = e.get("details", {})
        lines.append(
            f"• {e['operation']} — "
            f"target={details.get('target_user_id', '?')} "
            f"by={details.get('actor_user_id', '?')} "
            f"at {e['created_at'][:19]}"
        )
    await message.answer("\n".join(lines))


# --------------------------------------------------------------- /cancel (FSM escape)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    if current is not None:
        await state.clear()
        await message.answer("Cancelled.")
    else:
        await message.answer("Nothing to cancel.")


# --------------------------------------------------------------- helpers


async def _edit_or_drop(callback: CallbackQuery, suffix: str) -> None:
    """Safely append text to the confirmation message."""
    if callback.message is None or not hasattr(callback.message, "text"):
        return
    base = callback.message.text or ""
    await callback.message.edit_text(base + suffix)


async def _notify_user(user_id: int, text: str) -> None:
    try:
        from aiogram import Bot

        from app.config import get_settings

        settings = get_settings()
        if not settings.bot_configured:
            return
        bot = Bot(token=settings.bot_token.get_secret_value())
        try:
            await bot.send_message(user_id, text)
        finally:
            await bot.session.close()
    except Exception:  # noqa: BLE001
        logger.debug("auth: could not notify user %d", user_id)
