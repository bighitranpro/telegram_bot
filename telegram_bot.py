#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Telegram bot hỗ trợ theo dõi UID và quản lý Fanpage Facebook."""

import asyncio
import copy
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import aiohttp
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    JobQueue,
    MessageHandler,
    filters,
)

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
FACEBOOK_ACCESS_TOKEN = os.getenv("FACEBOOK_ACCESS_TOKEN", "").strip()
FACEBOOK_GRAPH_VERSION = os.getenv("FACEBOOK_GRAPH_VERSION", "v18.0").strip()
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "180"))
PAGE_MONITOR_INTERVAL_SECONDS = int(os.getenv("PAGE_MONITOR_INTERVAL_SECONDS", "180"))
STATE_FILE = Path(os.getenv("BOT_STATE_FILE", "bot_state.json"))

if not TELEGRAM_BOT_TOKEN:
    raise SystemExit("⚠️ Thiếu TELEGRAM_BOT_TOKEN trong môi trường.")

GRAPH_API_ROOT = f"https://graph.facebook.com/{FACEBOOK_GRAPH_VERSION}".rstrip("/")

STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

state_lock = asyncio.Lock()
state: Dict[str, Any] = {"users": {}}


def _load_state() -> None:
    global state
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if "users" not in state:
                state = {"users": {}}
        except Exception:
            state = {"users": {}}
    else:
        state = {"users": {}}


def _write_state_locked() -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def ensure_user(chat_id: int) -> Dict[str, Any]:
    users = state.setdefault("users", {})
    user = users.setdefault(
        str(chat_id),
        {
            "token": "",
            "uids": {},
            "pages": {},
        },
    )
    user.setdefault("token", "")
    user.setdefault("uids", {})
    user.setdefault("pages", {})
    return user


async def snapshot_users() -> Dict[str, Any]:
    async with state_lock:
        return copy.deepcopy(state.get("users", {}))



def normalize_uid(raw: str) -> str:
    text = raw.strip()
    if not text:
        return text
    cleaned = re.sub(r"[^0-9a-zA-Z_-]", "", text)
    return cleaned or text


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_fb_time(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return None


async def send_reply(update: Update, text: str, *, parse_mode: Optional[str] = None) -> None:
    if update.message:
        await update.message.reply_text(text, parse_mode=parse_mode)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_reply(
        update,
        (
            "🤖 Xin chào!\n"
            "Bot hỗ trợ:\n"
            "1️⃣ Theo dõi trạng thái LIVE/DIE của UID Facebook.\n"
            "2️⃣ Lưu UID riêng cho từng người dùng.\n"
            "3️⃣ Gửi thông báo ngay khi UID đổi trạng thái.\n"
            "4️⃣ Quản lý fanpage: auto like/ẩn/xóa/chặn, gửi tin nhắn mẫu cho bình luận mới.\n"
            "5️⃣ Sử dụng trực tiếp trên Telegram – không cần cài đặt.\n\n"
            "Gõ /help để xem chi tiết lệnh."),
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_reply(
        update,
        (
            "📚 Danh sách lệnh:\n"
            "/adduid <uid> [ghi_chú] – Thêm UID để theo dõi.\n"
            "/removeuid <uid> – Xoá UID khỏi danh sách.\n"
            "/listuid – Liệt kê UID đã lưu.\n"
            "/checkuid [uid] – Kiểm tra trạng thái ngay lập tức.\n"
            "/settoken <token> – Ghi nhớ token Facebook riêng của bạn.\n"
            "/addpage <page_id> <page_token> – Lưu trang fanpage để quản lý.\n"
            "/removepage <page_id> – Gỡ trang khỏi danh sách.\n"
            "/listpages – Danh sách trang & cấu hình hiện tại.\n"
            "/watchpost <page_id> <post_id> – Theo dõi bình luận của bài viết.\n"
            "/unwatchpost <page_id> <post_id> – Dừng theo dõi bài viết.\n"
            "/setkeywords <page_id> <hide|delete|block> <từ,khoá> – Tự động ẩn/xóa/chặn khi gặp từ khóa.\n"
            "/autolike <page_id> <on|off> – Bật/tắt auto like bình luận.\n"
            "/settemplate <page_id> <nội dung> – Mẫu tin nhắn trả lời bình luận.\n"
            "/pagestatus <page_id> – Xem thiết lập auto hiện tại."),
    )


async def set_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await send_reply(update, "⚠️ Vui lòng nhập token. Ví dụ: /settoken EAAB...")
        return
    token = context.args[0].strip()
    chat_id = update.effective_chat.id
    async with state_lock:
        user = ensure_user(chat_id)
        user["token"] = token
        _write_state_locked()
    await send_reply(update, "✅ Đã lưu token Facebook riêng cho bạn.")


async def add_uid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await send_reply(update, "⚠️ Cú pháp: /adduid <uid> [ghi_chú]")
        return
    uid = normalize_uid(context.args[0])
    label = " ".join(context.args[1:]).strip() if len(context.args) > 1 else ""
    if not uid:
        await send_reply(update, "⚠️ UID không hợp lệ.")
        return
    chat_id = update.effective_chat.id
    async with state_lock:
        user = ensure_user(chat_id)
        user["uids"].setdefault(uid, {})
        user["uids"][uid].update(
            {
                "label": label,
                "status": user["uids"][uid].get("status", "unknown"),
                "summary": user["uids"][uid].get("summary", ""),
                "name": user["uids"][uid].get("name"),
                "last_checked": user["uids"][uid].get("last_checked"),
            }
        )
        _write_state_locked()
    await send_reply(update, f"✅ Đã lưu UID {uid}{f' ({label})' if label else ''}.")


async def remove_uid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await send_reply(update, "⚠️ Cú pháp: /removeuid <uid>")
        return
    uid = normalize_uid(context.args[0])
    chat_id = update.effective_chat.id
    async with state_lock:
        user = ensure_user(chat_id)
        if uid in user["uids"]:
            del user["uids"][uid]
            _write_state_locked()
            await send_reply(update, f"🗑️ Đã xoá UID {uid}.")
        else:
            await send_reply(update, "⚠️ UID chưa được lưu.")


async def list_uid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    users = await snapshot_users()
    user = users.get(str(chat_id))
    if not user or not user.get("uids"):
        await send_reply(update, "📭 Chưa có UID nào được lưu. Dùng /adduid để thêm.")
        return
    lines: List[str] = ["📌 Danh sách UID:"]
    for idx, (uid, meta) in enumerate(sorted(user["uids"].items()), start=1):
        label = f" – {meta.get('label')}" if meta.get("label") else ""
        status = meta.get("status", "unknown")
        summary = meta.get("summary") or ""
        last = meta.get("last_checked") or "chưa kiểm tra"
        lines.append(f"{idx}. {uid}{label}\n   Trạng thái: {status} – {summary}\n   Lần kiểm tra: {last}")
    await send_reply(update, "\n".join(lines))


async def manual_check_uids(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    users = await snapshot_users()
    user = users.get(str(chat_id))
    if not user or not user.get("uids"):
        await send_reply(update, "📭 Chưa có UID nào để kiểm tra.")
        return
    selected: Dict[str, Dict[str, Any]] = {}
    if context.args:
        for raw in context.args:
            uid = normalize_uid(raw)
            if uid in user["uids"]:
                selected[uid] = user["uids"][uid]
        if not selected:
            await send_reply(update, "⚠️ Không tìm thấy UID đã lưu với tham số bạn nhập.")
            return
    else:
        selected = user["uids"]
    token = user.get("token") or FACEBOOK_ACCESS_TOKEN
    if not token:
        await send_reply(update, "⚠️ Chưa có token Facebook. Dùng /settoken hoặc cấu hình FACEBOOK_ACCESS_TOKEN.")
        return
    await send_reply(update, "🔄 Đang kiểm tra...")
    results = await run_uid_checks(chat_id, selected.keys(), token)
    await apply_uid_results(chat_id, results)
    lines = ["✅ Kết quả kiểm tra UID:"]
    for uid, res in results.items():
        summary = res.get("summary") or ""
        status = res.get("status", "unknown")
        name = res.get("name")
        live_flag = "🔴 LIVE" if res.get("live_video") else ""
        lines.append(f"• {uid}{f' ({name})' if name else ''}: {status} {live_flag}\n  {summary}")
    await send_reply(update, "\n".join(lines))


async def add_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 2:
        await send_reply(update, "⚠️ Cú pháp: /addpage <page_id> <page_token>")
        return
    page_id = normalize_uid(context.args[0])
    page_token = context.args[1].strip()
    if not page_id or not page_token:
        await send_reply(update, "⚠️ Thiếu page_id hoặc token.")
        return
    await send_reply(update, "🔄 Đang xác minh trang...")
    try:
        page_info = await fetch_page_info(page_id, page_token)
    except Exception as exc:
        await send_reply(update, f"❌ Không thể truy cập trang: {exc}")
        return
    chat_id = update.effective_chat.id
    async with state_lock:
        user = ensure_user(chat_id)
        page = user["pages"].setdefault(
            page_id,
            {
                "token": page_token,
                "name": page_info.get("name") or page_id,
                "auto": {
                    "like": True,
                    "hide_keywords": [],
                    "delete_keywords": [],
                    "block_keywords": [],
                    "message_template": "",
                },
                "posts": {},
                "last_error": "",
            },
        )
        page["token"] = page_token
        page["name"] = page_info.get("name") or page_id
        page.setdefault("auto", {})
        page["auto"].setdefault("like", True)
        page["auto"].setdefault("hide_keywords", [])
        page["auto"].setdefault("delete_keywords", [])
        page["auto"].setdefault("block_keywords", [])
        page["auto"].setdefault("message_template", "")
        page.setdefault("posts", {})
        page.setdefault("last_error", "")
        _write_state_locked()
    await send_reply(update, f"✅ Đã lưu trang {page_info.get('name') or page_id} ({page_id}).")


async def remove_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await send_reply(update, "⚠️ Cú pháp: /removepage <page_id>")
        return
    page_id = normalize_uid(context.args[0])
    chat_id = update.effective_chat.id
    async with state_lock:
        user = ensure_user(chat_id)
        if page_id in user["pages"]:
            del user["pages"][page_id]
            _write_state_locked()
            await send_reply(update, f"🗑️ Đã xoá trang {page_id}.")
        else:
            await send_reply(update, "⚠️ Trang chưa được lưu.")


def format_keywords(keywords: Iterable[str]) -> str:
    values = [k for k in keywords if k]
    return ", ".join(values) if values else "(trống)"


async def list_pages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    users = await snapshot_users()
    user = users.get(str(chat_id))
    if not user or not user.get("pages"):
        await send_reply(update, "📭 Chưa lưu trang nào. Dùng /addpage để thêm.")
        return
    lines = ["📘 Trang đang quản lý:"]
    for idx, (page_id, page) in enumerate(sorted(user["pages"].items()), start=1):
        auto = page.get("auto", {})
        posts = page.get("posts", {})
        lines.append(
            (
                f"{idx}. {page.get('name', page_id)} ({page_id})\n"
                f"   Auto like: {'bật' if auto.get('like', True) else 'tắt'}\n"
                f"   Hide keywords: {format_keywords(auto.get('hide_keywords', []))}\n"
                f"   Delete keywords: {format_keywords(auto.get('delete_keywords', []))}\n"
                f"   Block keywords: {format_keywords(auto.get('block_keywords', []))}\n"
                f"   Template: {auto.get('message_template') or '(chưa thiết lập)'}\n"
                f"   Bài đang theo dõi ({len(posts)}): {', '.join(posts.keys()) or '(none)'}"
            )
        )
    await send_reply(update, "\n".join(lines))


async def page_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await send_reply(update, "⚠️ Cú pháp: /pagestatus <page_id>")
        return
    page_id = normalize_uid(context.args[0])
    users = await snapshot_users()
    chat_id = update.effective_chat.id
    user = users.get(str(chat_id))
    page = user and user.get("pages", {}).get(page_id)
    if not page:
        await send_reply(update, "⚠️ Trang chưa được lưu.")
        return
    auto = page.get("auto", {})
    posts = page.get("posts", {})
    lines = [
        f"📄 Trang {page.get('name', page_id)} ({page_id})",
        f"• Auto like: {'bật' if auto.get('like', True) else 'tắt'}",
        f"• Hide keywords: {format_keywords(auto.get('hide_keywords', []))}",
        f"• Delete keywords: {format_keywords(auto.get('delete_keywords', []))}",
        f"• Block keywords: {format_keywords(auto.get('block_keywords', []))}",
        f"• Template: {auto.get('message_template') or '(chưa thiết lập)'}",
    ]
    if posts:
        lines.append("• Bài theo dõi:")
        for post_id, post in posts.items():
            last = post.get("last_comment_time") or "chưa"
            lines.append(f"  - {post_id} (lần cuối: {last})")
    else:
        lines.append("• Chưa có bài viết nào được theo dõi.")
    await send_reply(update, "\n".join(lines))


async def watch_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 2:
        await send_reply(update, "⚠️ Cú pháp: /watchpost <page_id> <post_id>")
        return
    page_id = normalize_uid(context.args[0])
    post_id = normalize_uid(context.args[1])
    chat_id = update.effective_chat.id
    async with state_lock:
        user = ensure_user(chat_id)
        page = user["pages"].get(page_id)
        if not page:
            await send_reply(update, "⚠️ Trang chưa được lưu. Dùng /addpage trước.")
            return
        posts = page.setdefault("posts", {})
        posts.setdefault(post_id, {"last_comment_time": None, "last_comment_id": None})
        _write_state_locked()
    await send_reply(update, f"✅ Đã bật theo dõi bình luận cho bài {post_id}.")


async def unwatch_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 2:
        await send_reply(update, "⚠️ Cú pháp: /unwatchpost <page_id> <post_id>")
        return
    page_id = normalize_uid(context.args[0])
    post_id = normalize_uid(context.args[1])
    chat_id = update.effective_chat.id
    async with state_lock:
        user = ensure_user(chat_id)
        page = user["pages"].get(page_id)
        if not page or post_id not in page.get("posts", {}):
            await send_reply(update, "⚠️ Bài viết chưa được theo dõi.")
            return
        del page["posts"][post_id]
        _write_state_locked()
    await send_reply(update, f"🛑 Đã dừng theo dõi bài {post_id}.")


async def set_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 3:
        await send_reply(update, "⚠️ Cú pháp: /setkeywords <page_id> <hide|delete|block> <từ,khoá>")
        return
    page_id = normalize_uid(context.args[0])
    action = context.args[1].strip().lower()
    if action not in {"hide", "delete", "block"}:
        await send_reply(update, "⚠️ Hành động phải là hide, delete hoặc block.")
        return
    keyword_text = " ".join(context.args[2:])
    keywords = [k.strip().lower() for k in re.split(r"[,;]", keyword_text) if k.strip()]
    chat_id = update.effective_chat.id
    async with state_lock:
        user = ensure_user(chat_id)
        page = user["pages"].get(page_id)
        if not page:
            await send_reply(update, "⚠️ Trang chưa được lưu.")
            return
        page.setdefault("auto", {})
        key = f"{action}_keywords"
        page["auto"][key] = keywords
        _write_state_locked()
    await send_reply(update, f"✅ Đã cập nhật từ khoá {action}: {format_keywords(keywords)}")


async def auto_like(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 2:
        await send_reply(update, "⚠️ Cú pháp: /autolike <page_id> <on|off>")
        return
    page_id = normalize_uid(context.args[0])
    flag = context.args[1].strip().lower()
    if flag not in {"on", "off"}:
        await send_reply(update, "⚠️ Giá trị phải là on hoặc off.")
        return
    enabled = flag == "on"
    chat_id = update.effective_chat.id
    async with state_lock:
        user = ensure_user(chat_id)
        page = user["pages"].get(page_id)
        if not page:
            await send_reply(update, "⚠️ Trang chưa được lưu.")
            return
        page.setdefault("auto", {})
        page["auto"]["like"] = enabled
        _write_state_locked()
    await send_reply(update, f"✅ Auto like đã được {'bật' if enabled else 'tắt'} cho trang {page_id}.")


async def set_template(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 2:
        await send_reply(update, "⚠️ Cú pháp: /settemplate <page_id> <nội dung>")
        return
    page_id = normalize_uid(context.args[0])
    message = update.message.text.partition(" ")[2].partition(" ")[2].strip()
    if not message:
        await send_reply(update, "⚠️ Vui lòng nhập nội dung mẫu tin nhắn.")
        return
    chat_id = update.effective_chat.id
    async with state_lock:
        user = ensure_user(chat_id)
        page = user["pages"].get(page_id)
        if not page:
            await send_reply(update, "⚠️ Trang chưa được lưu.")
            return
        page.setdefault("auto", {})
        page["auto"]["message_template"] = message
        _write_state_locked()
    await send_reply(update, f"✅ Đã cập nhật mẫu tin nhắn cho trang {page_id}.")


async def fetch_page_info(page_id: str, token: str) -> Dict[str, Any]:
    async with aiohttp.ClientSession() as session:
        params = {"access_token": token, "fields": "id,name"}
        async with session.get(f"{GRAPH_API_ROOT}/{page_id}", params=params, timeout=30) as resp:
            data = await resp.json()
    if "error" in data:
        error = data["error"].get("message", "Không xác định")
        raise RuntimeError(error)
    return data


async def run_uid_checks(chat_id: int, uids: Iterable[str], token: str) -> Dict[str, Dict[str, Any]]:
    results: Dict[str, Dict[str, Any]] = {}
    async with aiohttp.ClientSession() as session:
        for uid in uids:
            result = await fetch_uid_status(session, uid, token)
            results[uid] = result
            if result.get("status") == "token_error":
                break
    return results


async def fetch_uid_status(session: aiohttp.ClientSession, uid: str, token: str) -> Dict[str, Any]:
    base_url = f"{GRAPH_API_ROOT}/{uid}"
    checked_at = now_iso()
    params = {"access_token": token, "fields": "id,name,link"}
    try:
        async with session.get(base_url, params=params, timeout=30) as resp:
            info = await resp.json()
    except asyncio.TimeoutError:
        return {"status": "error", "summary": "Timeout khi gọi Graph API", "checked_at": checked_at}
    except aiohttp.ClientError as exc:
        return {"status": "error", "summary": f"Lỗi kết nối: {exc}", "checked_at": checked_at}

    if "error" in info:
        error = info["error"]
        code = error.get("code")
        message = error.get("message", "Không xác định")
        if code == 190:
            return {
                "status": "token_error",
                "summary": f"Token không hợp lệ hoặc hết hạn: {message}",
                "checked_at": checked_at,
            }
        if code in {803, 200, 368}:
            status = "die"
        else:
            status = "error"
        return {
            "status": status,
            "summary": message,
            "checked_at": checked_at,
        }

    name = info.get("name")
    summary = "Tài khoản hoạt động"
    status = "live"
    live_video = False
    params_live = {"access_token": token, "fields": "status,live_views", "limit": 1}
    try:
        async with session.get(f"{base_url}/live_videos", params=params_live, timeout=30) as resp:
            live_info = await resp.json()
    except asyncio.TimeoutError:
        live_info = {"error": {"message": "Timeout khi kiểm tra live"}}
    except aiohttp.ClientError as exc:
        live_info = {"error": {"message": str(exc)}}

    if isinstance(live_info, dict) and live_info.get("data"):
        entry = live_info["data"][0]
        live_status = (entry.get("status") or "").lower()
        if live_status in {"live", "live_now", "live_streaming", "live_video"}:
            status = "live"
            live_video = True
            views = entry.get("live_views")
            if views is not None:
                summary = f"Đang phát live ({views} lượt xem)"
            else:
                summary = "Đang phát live"
    elif isinstance(live_info, dict) and live_info.get("error"):
        summary = f"Không lấy được trạng thái live: {live_info['error'].get('message', 'không rõ')}"

    return {
        "status": status,
        "summary": summary,
        "name": name,
        "checked_at": checked_at,
        "live_video": live_video,
    }


async def apply_uid_results(chat_id: int, results: Dict[str, Dict[str, Any]]) -> None:
    async with state_lock:
        user = ensure_user(chat_id)
        changed = False
        for uid, res in results.items():
            if uid not in user["uids"]:
                continue
            meta = user["uids"][uid]
            status_before = meta.get("status")
            summary_before = meta.get("summary")
            meta.update(
                {
                    "status": res.get("status", "unknown"),
                    "summary": res.get("summary"),
                    "name": res.get("name") or meta.get("name"),
                    "last_checked": res.get("checked_at") or now_iso(),
                    "live_video": res.get("live_video", False),
                }
            )
            if status_before != meta["status"] or summary_before != meta.get("summary"):
                changed = True
        if changed:
            _write_state_locked()


async def check_all_uids(context: ContextTypes.DEFAULT_TYPE) -> None:
    users = await snapshot_users()
    if not users:
        return
    notifications: List[Tuple[int, str]] = []
    updates: Dict[int, Dict[str, Dict[str, Any]]] = {}
    async with aiohttp.ClientSession() as session:
        for chat_id_str, user in users.items():
            chat_id = int(chat_id_str)
            token = user.get("token") or FACEBOOK_ACCESS_TOKEN
            if not token:
                continue
            uids = user.get("uids", {})
            if not uids:
                continue
            user_updates: Dict[str, Dict[str, Any]] = {}
            for uid, meta in uids.items():
                res = await fetch_uid_status(session, uid, token)
                user_updates[uid] = res
                prev_status = meta.get("status")
                if res.get("status") == "token_error":
                    notifications.append((chat_id, f"❌ Token Facebook lỗi: {res.get('summary')}. Dùng /settoken để cập nhật."))
                    break
                if prev_status != res.get("status") or meta.get("summary") != res.get("summary"):
                    name = res.get("name") or meta.get("name") or uid
                    summary = res.get("summary") or ""
                    live_flag = "🔴 LIVE" if res.get("live_video") else ""
                    notifications.append(
                        (
                            chat_id,
                            f"⚠️ UID {name} ({uid}) đổi trạng thái: {prev_status or 'unknown'} ➜ {res.get('status')} {live_flag}\n{summary}",
                        )
                    )
            if user_updates:
                updates[chat_id] = user_updates
    for chat_id, user_updates in updates.items():
        await apply_uid_results(chat_id, user_updates)
    for chat_id, text in notifications:
        try:
            await context.bot.send_message(chat_id=chat_id, text=text[:4000])
        except Exception:
            continue


def comment_matches(text: str, keywords: Iterable[str]) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(keyword in lowered for keyword in keywords if keyword)


async def fb_request(
    session: aiohttp.ClientSession,
    method: str,
    endpoint: str,
    token: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    data: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    params = dict(params or {})
    params["access_token"] = token
    url = f"{GRAPH_API_ROOT}/{endpoint}".rstrip("/")
    try:
        async with session.request(method.upper(), url, params=params, data=data, timeout=30) as resp:
            try:
                result = await resp.json()
            except aiohttp.ContentTypeError:
                text = await resp.text()
                return resp.status < 400, text
    except asyncio.TimeoutError:
        return False, "Timeout"
    except aiohttp.ClientError as exc:
        return False, str(exc)
    if isinstance(result, dict) and result.get("error"):
        return False, result["error"].get("message", "Không rõ")
    return True, "OK"


async def process_page_posts(
    session: aiohttp.ClientSession,
    chat_id: int,
    page_id: str,
    page: Dict[str, Any],
    token: str,
) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    if not token:
        return {}, []
    auto = page.get("auto", {})
    posts = page.get("posts", {})
    updates: Dict[str, Dict[str, Any]] = {}
    messages: List[str] = []
    for post_id, meta in posts.items():
        params = {
            "fields": "id,message,created_time,from{id,name}",
            "order": "reverse_chronological",
            "limit": 30,
            "filter": "stream",
            "access_token": token,
        }
        try:
            async with session.get(
                f"{GRAPH_API_ROOT}/{post_id}/comments", params=params, timeout=30
            ) as resp:
                data = await resp.json()
        except asyncio.TimeoutError:
            messages.append(f"⚠️ Trang {page.get('name', page_id)}: Timeout khi lấy bình luận bài {post_id}.")
            continue
        except aiohttp.ClientError as exc:
            messages.append(f"⚠️ Trang {page.get('name', page_id)}: Lỗi kết nối ({exc}).")
            continue
        if isinstance(data, dict) and data.get("error"):
            err = data["error"].get("message", "Không rõ")
            messages.append(f"❌ Trang {page.get('name', page_id)}: {err}")
            continue
        comments = data.get("data", [])
        if not comments:
            continue
        last_time_stored = meta.get("last_comment_time")
        last_id_stored = meta.get("last_comment_id")
        stored_ids = meta.get("last_comment_ids") or []
        last_ids: Set[str] = set()
        if isinstance(stored_ids, list):
            last_ids.update(filter(None, stored_ids))
        if last_id_stored:
            last_ids.add(last_id_stored)
        last_dt = datetime.fromisoformat(last_time_stored) if last_time_stored else None
        latest_dt = last_dt
        latest_id = last_id_stored
        latest_ids: Set[str] = set(last_ids)
        for comment in reversed(comments):
            created = parse_fb_time(comment.get("created_time", ""))
            if created is None:
                continue
            if last_dt and created < last_dt:
                continue
            comment_id = comment.get("id")
            if (
                last_dt
                and created == last_dt
                and comment_id
                and comment_id in last_ids
            ):
                continue
            text = comment.get("message") or ""
            actor = comment.get("from") or {}
            actor_name = actor.get("name", "Người dùng")
            actor_id = actor.get("id")
            actions_taken: List[str] = []
            deleted = False
            if comment_matches(text, auto.get("delete_keywords", [])):
                ok, msg = await fb_request(session, "DELETE", comment_id, token)
                actions_taken.append(f"Xoá bình luận: {'✅' if ok else '❌'} {msg}")
                deleted = ok
            if not deleted and comment_matches(text, auto.get("hide_keywords", [])):
                ok, msg = await fb_request(
                    session,
                    "POST",
                    comment_id,
                    token,
                    data={"is_hidden": "true"},
                )
                actions_taken.append(f"Ẩn bình luận: {'✅' if ok else '❌'} {msg}")
            if auto.get("like", True) and not deleted:
                ok, msg = await fb_request(session, "POST", f"{comment_id}/likes", token)
                actions_taken.append(f"Like bình luận: {'✅' if ok else '❌'} {msg}")
            if actor_id and comment_matches(text, auto.get("block_keywords", [])):
                ok, msg = await fb_request(
                    session,
                    "POST",
                    f"{page_id}/blocked",
                    token,
                    data={"user": actor_id},
                )
                actions_taken.append(f"Chặn người dùng {actor_id}: {'✅' if ok else '❌'} {msg}")
            template = auto.get("message_template")
            if template and actor_id:
                try:
                    formatted_message = template.format(name=actor_name)
                except (IndexError, KeyError, ValueError) as exc:
                    actions_taken.append(
                        f"Gửi tin nhắn: ❌ Lỗi định dạng mẫu ({exc})"
                    )
                else:
                    ok, msg = await fb_request(
                        session,
                        "POST",
                        f"{comment_id}/private_replies",
                        token,
                        data={"message": formatted_message},
                    )
                    actions_taken.append(f"Gửi tin nhắn: {'✅' if ok else '❌'} {msg}")
            summary_lines = [
                f"💬 Bình luận mới trên {page.get('name', page_id)}",
                f"• Bài: {post_id}",
                f"• Người dùng: {actor_name}{f' ({actor_id})' if actor_id else ''}",
                f"• Nội dung: {text[:300] or '(trống)'}",
            ]
            if actions_taken:
                summary_lines.append("• Hành động: \n  - " + "\n  - ".join(actions_taken))
            else:
                summary_lines.append("• Chưa có hành động tự động.")
            messages.append("\n".join(summary_lines))
            if not latest_dt or created > latest_dt:
                latest_dt = created
                latest_ids = set()
            if created == latest_dt and comment_id:
                latest_ids.add(comment_id)
                latest_id = comment_id
        if latest_dt:
            update_payload: Dict[str, Any] = {
                "last_comment_time": latest_dt.astimezone(timezone.utc).isoformat(),
                "last_comment_id": latest_id,
            }
            if latest_ids:
                update_payload["last_comment_ids"] = sorted(latest_ids)
            updates[post_id] = update_payload
    return updates, messages


async def monitor_pages(context: ContextTypes.DEFAULT_TYPE) -> None:
    users = await snapshot_users()
    if not users:
        return
    updates: Dict[Tuple[int, str, str], Dict[str, Any]] = {}
    notifications: List[Tuple[int, str]] = []
    async with aiohttp.ClientSession() as session:
        for chat_id_str, user in users.items():
            chat_id = int(chat_id_str)
            pages = user.get("pages", {})
            for page_id, page in pages.items():
                posts = page.get("posts", {})
                if not posts:
                    continue
                token = page.get("token") or user.get("token") or FACEBOOK_ACCESS_TOKEN
                post_updates, messages = await process_page_posts(session, chat_id, page_id, page, token)
                for post_id, meta in post_updates.items():
                    updates[(chat_id, page_id, post_id)] = meta
                for message in messages:
                    notifications.append((chat_id, message))
    if updates:
        async with state_lock:
            changed = False
            for (chat_id, page_id, post_id), meta in updates.items():
                user = ensure_user(chat_id)
                page = user["pages"].get(page_id)
                if not page:
                    continue
                post = page.setdefault("posts", {}).setdefault(post_id, {})
                post.update(meta)
                changed = True
            if changed:
                _write_state_locked()
    for chat_id, message in notifications:
        try:
            await context.bot.send_message(chat_id=chat_id, text=message[:4000])
        except Exception:
            continue


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_reply(update, "⚠️ Lệnh không hỗ trợ. Dùng /help để xem hướng dẫn.")


async def on_startup(_: Application) -> None:
    _load_state()


def register_handlers(app: Application, job_queue: JobQueue) -> None:
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("settoken", set_token))
    app.add_handler(CommandHandler("adduid", add_uid))
    app.add_handler(CommandHandler("removeuid", remove_uid))
    app.add_handler(CommandHandler("listuid", list_uid))
    app.add_handler(CommandHandler("checkuid", manual_check_uids))
    app.add_handler(CommandHandler("addpage", add_page))
    app.add_handler(CommandHandler("removepage", remove_page))
    app.add_handler(CommandHandler("listpages", list_pages))
    app.add_handler(CommandHandler("pagestatus", page_status))
    app.add_handler(CommandHandler("watchpost", watch_post))
    app.add_handler(CommandHandler("unwatchpost", unwatch_post))
    app.add_handler(CommandHandler("setkeywords", set_keywords))
    app.add_handler(CommandHandler("autolike", auto_like))
    app.add_handler(CommandHandler("settemplate", set_template))
    app.add_handler(MessageHandler(filters.COMMAND, unknown))

    job_queue.run_repeating(check_all_uids, interval=CHECK_INTERVAL_SECONDS, first=10)
    job_queue.run_repeating(monitor_pages, interval=PAGE_MONITOR_INTERVAL_SECONDS, first=15)


async def main() -> None:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(on_startup).build()
    register_handlers(app, app.job_queue)
    print("🚀 Telegram bot ready.")
    await app.run_polling(close_loop=False)


if __name__ == "__main__":
    asyncio.run(main())
