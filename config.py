"""Cấu hình dùng chung cho bot và script login."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
PROFILE_DIR = BASE_DIR / "browser-profile"
TARGET_FILE = BASE_DIR / "target.json"
DIAGNOSE_DIR = BASE_DIR / "diagnose"

load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


@dataclass
class Settings:
    discord_token: str = ""
    guild_ids: list[int] = field(default_factory=list)
    allowed_user_ids: set[int] = field(default_factory=set)
    ephemeral_reply: bool = True
    message_prefix: str = "[Discord - {user}] {message}"
    headless: bool = True
    send_timeout: int = 120
    cooldown_seconds: int = 10
    relay_channel_ids: list[int] = field(default_factory=list)
    relay_poll_seconds: int = 10
    relay_trigger: str = "discord-"

    @classmethod
    def load(cls) -> "Settings":
        raw_ids = os.getenv("ALLOWED_USER_IDS", "")
        allowed = {
            int(part.strip())
            for part in raw_ids.split(",")
            if part.strip().isdigit()
        }

        # Cho phép nhiều server, ngăn cách bằng dấu phẩy: bot có thể được mời vào
        # nhiều server và lệnh guild-scoped CHỈ hiện ở server được sync.
        guild_ids = [
            int(part.strip())
            for part in os.getenv("DISCORD_GUILD_ID", "").split(",")
            if part.strip().isdigit()
        ]

        return cls(
            discord_token=os.getenv("DISCORD_TOKEN", "").strip(),
            guild_ids=guild_ids,
            allowed_user_ids=allowed,
            ephemeral_reply=_bool("EPHEMERAL_REPLY", True),
            message_prefix=os.getenv(
                "MESSAGE_PREFIX", "[Discord - {user}] {message}"
            ),
            headless=_bool("TEAMS_HEADLESS", True),
            send_timeout=_int("TEAMS_SEND_TIMEOUT", 120),
            cooldown_seconds=_int("COOLDOWN_SECONDS", 10),
            relay_channel_ids=[
                int(part.strip())
                for part in os.getenv("DISCORD_RELAY_CHANNEL_ID", "").split(",")
                if part.strip().isdigit()
            ],
            relay_poll_seconds=max(5, _int("RELAY_POLL_SECONDS", 10)),
            # Không đặt trong .env -> mặc định "discord-". Đặt nhưng để RỖNG ->
            # chuyển mọi tin. Mặc định có trigger vì nhóm đông người mà chuyển
            # hết thì Discord ngập tin.
            relay_trigger=os.getenv("RELAY_TRIGGER_PREFIX", "discord-").strip(),
        )

    def echo_marker(self) -> str:
        """Phần chữ cố định đứng đầu MESSAGE_PREFIX, vd "[Discord - ".

        Dùng để nhận ra tin nào do chính bot đẩy từ Discord sang, khỏi chuyển
        ngược lại Discord thành vòng lặp.
        """
        index = self.message_prefix.find("{")
        return self.message_prefix[:index] if index > 0 else ""

    def match_relay_trigger(self, text: str) -> str | None:
        """Nội dung cần chuyển sang Discord, hoặc None nếu tin này bỏ qua.

        Trigger rỗng -> chuyển mọi tin, giữ nguyên nội dung.
        Có trigger   -> chỉ tin bắt đầu bằng trigger, và CẮT trigger đi:
            "discord-cho tôi 1 ly trà sữa"  ->  "cho tôi 1 ly trà sữa"
        """
        if not self.relay_trigger:
            return text or None

        if not text.lower().startswith(self.relay_trigger.lower()):
            return None

        body = text[len(self.relay_trigger):].strip()
        return body or None

    def relay_body(self, text: str) -> str | None:
        """Nội dung cuối cùng đẩy sang Discord, hoặc None nếu bỏ qua tin này.

        Hai lớp lọc, theo đúng thứ tự:
          1. Bỏ tin do chính bot đẩy từ Discord sang ("[Discord - ...") — chống
             vòng lặp Discord -> Teams -> Discord.
          2. Chỉ giữ tin có trigger, và cắt trigger đi.

        Hai lớp này không đụng nhau: echo bắt đầu bằng "[", còn trigger phải nằm
        ở đầu tin, nên "[Discord - Phú Đinh] abc" không thể khớp "discord-".
        """
        text = (text or "").strip()
        if not text:
            return None

        marker = self.echo_marker()
        if marker and text.startswith(marker):
            return None

        return self.match_relay_trigger(text)

    def is_allowed(self, user_id: int) -> bool:
        # Không cấu hình allowlist => mở cho tất cả (đã cảnh báo trong .env.example).
        return not self.allowed_user_ids or user_id in self.allowed_user_ids


def load_target() -> dict | None:
    """Group chat Teams đã chọn ở bước login. None nếu chưa chạy login.py.

    Neo vào thread_id (19:...@thread.skype) chứ không phải URL: Teams v2 giữ
    nguyên địa chỉ teams.live.com/v2/ khi chuyển chat nên URL vô dụng.

    File đời cũ chỉ có "url" cũng bị coi là chưa cấu hình -> buộc chạy lại
    login.py, thay vì im lặng gửi vào chat nào đang mở.
    """
    if not TARGET_FILE.exists():
        return None
    try:
        data = json.loads(TARGET_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if data.get("thread_id") else None


def save_target(thread_id: str, label: str) -> None:
    TARGET_FILE.write_text(
        json.dumps(
            {"thread_id": thread_id, "label": label}, ensure_ascii=False, indent=2
        ),
        encoding="utf-8",
    )
