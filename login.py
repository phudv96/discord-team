"""Chạy 1 lần: đăng nhập Teams thủ công và chọn group chat đích.

    python login.py

Script mở Chromium (profile riêng ở browser-profile/). Bạn đăng nhập bằng tay,
mở đúng group chat muốn bot gửi vào, rồi quay lại terminal bấm Enter.
Session và group chat được lưu lại để bot.py dùng sau này.

LƯU Ý: không chạy song song với bot.py — hai process không dùng chung được
một profile Chromium.
"""

from __future__ import annotations

import asyncio
import sys

from playwright.async_api import Error as PlaywrightError

from config import PROFILE_DIR, TARGET_FILE, load_target, save_target
from teams_client import COMPOSE_SELECTORS, TeamsClient, TeamsProfileLocked


async def main() -> int:
    existing = load_target()
    if existing:
        print(f"Group chat hiện tại: {existing['label']}")
        print(f"  thread_id: {existing['thread_id']}\n")

    client = TeamsClient(headless=False)

    print("Đang mở Chromium...")
    try:
        await client.start()
    except TeamsProfileLocked as exc:
        print(f"\nLỖI: {exc}")
        return 1

    print(
        "\n"
        "──────────────────────────────────────────────────────────────\n"
        " 1. Đăng nhập Microsoft trong cửa sổ vừa mở.\n"
        "    -> Nhớ TICK 'Stay signed in' để session sống lâu.\n"
        " 2. Mở đúng GROUP CHAT bạn muốn bot gửi tin vào.\n"
        " 3. Quay lại đây và bấm Enter.\n"
        "──────────────────────────────────────────────────────────────\n"
    )
    await asyncio.to_thread(input, "Xong thì bấm Enter... ")

    try:
        _ = client.page.url
    except PlaywrightError:
        print("\nLỖI: cửa sổ trình duyệt đã bị đóng. Chạy lại script.")
        await client.close()
        return 1

    compose = await client._first_visible(COMPOSE_SELECTORS, timeout=10.0)
    if compose is None:
        print(
            "\nLỖI: không thấy ô soạn tin ở trang hiện tại.\n"
            "Hãy chắc chắn bạn đang MỞ HẲN một group chat (không phải màn hình danh sách)."
        )
        await client.close()
        return 1

    # Teams v2 không đổi URL khi chuyển chat -> phải lấy thread id từ DOM.
    thread_id = await client._current_thread_id()
    label = await client._current_title()

    if not thread_id:
        await client.diagnose("login-no-thread-id")
        print(
            "\nLỖI: không đọc được thread id của chat đang mở.\n"
            "Thanh danh sách chat bên trái có thể đang bị thu gọn — mở rộng nó ra\n"
            "(hoặc phóng to cửa sổ) để chat đích hiện rõ, rồi chạy lại script.\n"
            "Đã lưu ảnh chẩn đoán trong diagnose/."
        )
        await client.close()
        return 1

    if not label:
        print("\nCẢNH BÁO: không đọc được tên chat, vẫn lưu theo thread id.")
        label = thread_id

    save_target(thread_id, label)

    print("\n✔ Đã lưu.")
    print(f"  Group chat : {label}")
    print(f"  thread_id  : {thread_id}")
    print(f"  Cấu hình   : {TARGET_FILE}")
    print(f"  Session    : {PROFILE_DIR}")
    print("\nGiờ đóng cửa sổ này và chạy: python bot.py")

    await client.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
