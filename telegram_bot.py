#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Telegram ↔ ChatGPT bot (Bi Ads)
- Hỗ trợ OpenAI (mặc định) hoặc OpenRouter.
- Ghi nhớ ngắn hạn theo từng chat (lịch sử 8 lượt).
- Lệnh: /start, /help, /model <tên_model>, /switch openai|openrouter
- Xử lý lỗi rõ ràng, thông báo về admin (tuỳ chọn).

ENV cần có:
- TELEGRAM_BOT_TOKEN=...
- PROVIDER=openai|openrouter (mặc định: openai)
- MODEL=gpt-4o-mini (đổi tuỳ thích)
- OPENAI_API_KEY=... (khi PROVIDER=openai)
- OPENROUTER_API_KEY=... (khi PROVIDER=openrouter)
- TELEGRAM_ADMIN_CHAT_ID=... (tuỳ chọn, để nhận lỗi)
- SYSTEM_PROMPT=... (tuỳ chọn)
"""

import asyncio
import os
from collections import defaultdict, deque
from typing import Deque, Dict, Tuple

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
if not TELEGRAM_BOT_TOKEN:
    raise SystemExit("⚠️ Thiếu TELEGRAM_BOT_TOKEN trong .env / Secrets")

PROVIDER = os.getenv("PROVIDER", "openai").strip().lower()
MODEL = os.getenv("MODEL", "gpt-4o-mini").strip()
SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT", "Bạn là trợ lý của Bi Ads, trả lời ngắn gọn, rõ ràng, tiếng Việt.").strip()
ADMIN_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "").strip() or None

# ====== OpenAI / OpenRouter clients ======
class LLM:
    def __init__(self):
        self.provider = PROVIDER
        if self.provider == "openrouter":
            self.key = os.getenv("OPENROUTER_API_KEY", "").strip()
            if not self.key:
                raise SystemExit("⚠️ Thiếu OPENROUTER_API_KEY")
        else:
            # openai
            import openai
            self.openai = openai
            self.key = os.getenv("OPENAI_API_KEY", "").strip()
            if not self.key:
                raise SystemExit("⚠️ Thiếu OPENAI_API_KEY")

    async def chat(self, messages):
        if self.provider == "openrouter":
            import aiohttp
            headers = {
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/bighitranpro/mochiappai",
                "X-Title": "BiAds Telegram Bot",
            }
            body = {"model": MODEL, "messages": messages, "stream": False}
            async with aiohttp.ClientSession() as s:
                async with s.post("https://openrouter.ai/api/v1/chat/completions", json=body, headers=headers, timeout=120) as r:
                    j = await r.json()
                    try:
                        return j["choices"][0]["message"]["content"]
                    except Exception:
                        raise RuntimeError(f"OpenRouter error: {j}")
        else:
            # OpenAI official client (responses API via Chat Completions-compatible)
            from openai import OpenAI
            client = OpenAI(api_key=self.key)
            # Map to chat.completions for wide model support
            resp = client.chat.completions.create(model=MODEL, messages=messages, temperature=0.4)
            return resp.choices[0].message.content

llm = LLM()

# ====== Memory per chat ======
history: Dict[int, Deque[Tuple[str, str]]] = defaultdict(lambda: deque(maxlen=8))

def build_messages(chat_id: int, user_text: str):
    msgs = [{"role":"system","content": SYSTEM_PROMPT}]
    for u, a in history[chat_id]:
        msgs.append({"role":"user","content": u})
        msgs.append({"role":"assistant","content": a})
    msgs.append({"role":"user","content": user_text})
    return msgs

async def notify_admin(ctx: ContextTypes.DEFAULT_TYPE, text: str):
    if ADMIN_ID:
        try:
            await ctx.bot.send_message(chat_id=int(ADMIN_ID), text=text[:4000])
        except Exception:
            pass

# ====== Handlers ======
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Xin chào, mình là bot ChatGPT của Bi Ads!\n"
        "Gõ câu hỏi bất kỳ để mình trả lời.\n"
        "Lệnh: /help, /model <tên_model>, /switch openai|openrouter"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hướng dẫn nhanh:\n"
        "- Nhắn tin bình thường để hỏi ChatGPT\n"
        "- /model <tên> để đổi model (vd: gpt-4o-mini)\n"
        "- /switch openai|openrouter để đổi provider\n"
        "- Bot nhớ ~8 lượt hội thoại gần nhất"
    )

async def model_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global MODEL
    if not context.args:
        await update.message.reply_text(f"Model hiện tại: {MODEL}")
        return
    MODEL = " ".join(context.args).strip()
    await update.message.reply_text(f"✅ Đã đổi model: `{MODEL}`", parse_mode=ParseMode.MARKDOWN)

async def switch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global PROVIDER, llm
    if not context.args:
        await update.message.reply_text(f"Provider hiện tại: {PROVIDER}")
        return
    prov = context.args[0].strip().lower()
    if prov not in ("openai","openrouter"):
        await update.message.reply_text("Chỉ hỗ trợ: openai | openrouter")
        return
    PROVIDER = prov
    llm.__init__()  # re-init
    await update.message.reply_text(f"✅ Đã chuyển provider: {PROVIDER}")

async def chat_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_text = (update.message.text or "").strip()
    if not user_text:
        return
    typing = context.bot.send_chat_action(chat_id=chat_id, action="typing")
    await typing
    try:
        msgs = build_messages(chat_id, user_text)
        reply = await llm.chat(msgs)
        history[chat_id].append((user_text, reply))
        await update.message.reply_text(reply[:4000], parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text("⚠️ Xin lỗi, hệ thống bận hoặc cấu hình thiếu. Liên hệ admin để kiểm tra.")
        await notify_admin(context, f"❌ Lỗi: {e}")

async def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("model", model_cmd))
    app.add_handler(CommandHandler("switch", switch_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat_handler))
    print("🚀 Telegram bot started.")
    await app.run_polling(close_loop=False)

if __name__ == "__main__":
    asyncio.run(main())
