"""/check workflow (FSM) + result-message buttons.

The draft is analysis-only — it is never forwarded to any target chat.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.bot.formatters import format_chats, format_evidence
from app.services.analysis_service import AnalysisService

logger = logging.getLogger("app.bot.handlers.check")

router = Router(name="check")


class CheckStates(StatesGroup):
    waiting_text = State()
    waiting_edit = State()


@router.message(Command("check"))
async def cmd_check(message: Message, state: FSMContext) -> None:
    await state.set_state(CheckStates.waiting_text)
    await state.update_data(original_id=message.message_id)
    await message.answer(
        "Send the message you want me to analyze.\n\n"
        "It will only be analyzed locally — it will never be forwarded to any "
        "Telegram chat."
    )


@router.message(CheckStates.waiting_text)
async def on_draft_submitted(message: Message, state: FSMContext, analysis: AnalysisService) -> None:
    await state.clear()
    text = message.text or message.caption or ""
    if not text.strip():
        await message.answer("Please send a text message.")
        return
    result = await analysis.submit(text, message.from_user.id)
    if result.request_id is None:
        await message.answer(f"❌ {result.error}")
        return
    if result.degraded:
        await message.answer(
            f"🧠 Request stored (Redis unavailable — the worker is offline). "
            f"Request `{result.request_id}`.\n"
            "It will be analyzed automatically once the worker is back."
        )
    else:
        await message.answer(
            f"🧠 Analysis queued. Request `{result.request_id}`.\n"
            "The result will appear here shortly."
        )


@router.message(CheckStates.waiting_edit)
async def on_edited_draft(message: Message, state: FSMContext, analysis: AnalysisService) -> None:
    data = await state.get_data()
    await state.clear()
    request_id = data.get("request_id")
    if not request_id:
        await message.answer("No pending edit. Start with /check.")
        return
    text = message.text or ""
    if not text.strip():
        await message.answer("Please send a text message.")
        return
    result = await analysis.recheck(request_id, text)
    if result.request_id is None:
        await message.answer(f"❌ {result.error}")
        return
    await message.answer(
        f"🔄 Re-analyzing edited message. Request `{request_id}`.\n"
        "The result will appear here shortly."
    )


@router.callback_query(F.data.startswith("edit:"))
async def cb_edit(callback: CallbackQuery, state: FSMContext) -> None:
    request_id = callback.data.removeprefix("edit:")
    await state.set_state(CheckStates.waiting_edit)
    await state.update_data(request_id=request_id)
    await callback.answer("Editing…")
    await callback.message.answer(
        "Send the revised message. It will be re-analyzed against the same chats."
    )


@router.callback_query(F.data.startswith("recheck:"))
async def cb_recheck(callback: CallbackQuery, analysis: AnalysisService) -> None:
    request_id = callback.data.removeprefix("recheck:")
    outcome = await analysis.get_outcome(request_id)
    if outcome is None:
        await callback.answer("Request not found.")
        return
    result = await analysis.recheck(request_id, outcome.original_text)
    await callback.answer("Recheck queued.")
    if result.request_id is not None:
        await callback.message.answer(
            f"🔄 Rechecking. Request `{request_id}`.\nThe result will appear here shortly."
        )
    else:
        await callback.message.answer(f"❌ {result.error}")


@router.callback_query(F.data.startswith("evidence:"))
async def cb_evidence(callback: CallbackQuery, analysis: AnalysisService) -> None:
    request_id = callback.data.removeprefix("evidence:")
    outcome = await analysis.get_outcome(request_id)
    if outcome is None:
        await callback.answer("Result not ready or not found.")
        return
    await callback.answer()
    for chunk in format_evidence(outcome):
        await callback.message.answer(chunk)


@router.callback_query(F.data.startswith("chats:"))
async def cb_chats(callback: CallbackQuery, analysis: AnalysisService) -> None:
    request_id = callback.data.removeprefix("chats:")
    outcome = await analysis.get_outcome(request_id)
    if outcome is None:
        await callback.answer("Result not ready or not found.")
        return
    await callback.answer()
    await callback.message.answer(format_chats(outcome))


@router.callback_query(F.data.startswith("cancel:"))
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    request_id = callback.data.removeprefix("cancel:")
    await state.clear()
    await callback.answer("Cancelled.")
    await callback.message.answer(f"Analysis `{request_id}` discarded (results still in "
                                  "history until retention cleanup).")
