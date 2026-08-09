"""Driver Playwright để gửi tin nhắn vào Microsoft Teams (account cá nhân / Teams Free).

Vì sao phải dùng browser automation thay vì API:
  - Microsoft Graph (`ChatMessage.Send`, `Chat.ReadWrite`) ghi rõ
    "Delegated (personal Microsoft account): Not supported" => account cá nhân
    không gửi được chat qua Graph.
  - Office 365 Incoming Webhook đã bị khai tử; bản thay thế (Power Automate
    Workflows) yêu cầu license work/school.

Cách định danh group chat:
  Teams v2 KHÔNG đổi URL khi chuyển chat — địa chỉ luôn là teams.live.com/v2/.
  Nên không thể dùng URL làm mốc. Thay vào đó ta neo vào thread id
  (19:...@thread.skype) đọc trực tiếp từ DOM, và XÁC MINH trước mỗi lần gửi.
  Không xác minh được thì từ chối gửi, thà báo lỗi còn hơn gửi nhầm cuộc trò chuyện.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path

from playwright.async_api import (
    BrowserContext,
    Error as PlaywrightError,
    Locator,
    Page,
    Playwright,
    async_playwright,
)

from config import DIAGNOSE_DIR, PROFILE_DIR

log = logging.getLogger("teams")

TEAMS_HOME = "https://teams.live.com/v2/"

# Teams web cold-start (redirect /v2 -> /gather -> app, rồi render bằng JS) có thể
# mất khá lâu. Lần gửi đầu tiên sau khi bot khởi động sẽ chạm ngưỡng này; các lần
# sau chat đã mở sẵn nên gần như tức thì.
APP_READY_TIMEOUT = 60.0

# Các selector dưới đây lấy từ DOM thật (xem inspect_dom.py). Xếp từ cụ thể nhất
# tới chung nhất để còn sống sót khi Teams đổi giao diện.
COMPOSE_SELECTORS = [
    '[data-tid="ckeditor"] div[contenteditable="true"]',
    'div[role="textbox"][contenteditable="true"]',
    'div[contenteditable="true"][aria-label*="message" i]',
    'div[contenteditable="true"]',
]

SEND_BUTTON_SELECTORS = [
    '[data-tid="sendMessageCommands-send"]',
    'button[aria-label*="Send" i]',
    'button[title*="Send" i]',
]

CHAT_TITLE_SELECTORS = [
    '[data-tid="chat-title"]',
    '[data-tid="chat-title-name-group-chat"]',
]

# Cách đáng tin nhất để biết chat NÀO đang mở: avatar trên header chat, vì src của
# nó là .../api/groups/v1/threads/<thread_id>/profilepicturev2.
HEADER_AVATAR_SELECTORS = [
    '[data-tid="chat-title-avatar"] img',
    '[data-tid="entity-header"] img[src*="/threads/"]',
]

# Dự phòng: khi chạy headful, Teams gắn "LeftRailSelectedItem" vào data-tabster
# của item đang chọn. Headless KHÔNG có marker này (đã kiểm chứng) nên chỉ dùng
# làm lớp thứ hai, không được dựa vào nó.
SELECTED_CHAT_SELECTOR = '[data-tabster*="LeftRailSelectedItem"]'

THREAD_ID_RE = re.compile(r"19:[A-Za-z0-9_\-]+@thread\.[A-Za-z0-9]+")
THREAD_ID_IN_URL_RE = re.compile(r"/threads/(19:[A-Za-z0-9_\-]+@thread\.[A-Za-z0-9]+)")

# Đọc tin nhắn để chuyển ngược về Discord.
# Cấu trúc thật (xem inspect_dom.py / probe):
#   <div data-tid="chat-pane-message" data-mid="1786185314262" class="...ChatMessage__body...">
#   tên tác giả nằm ở  #author-<mid>   (có cho CẢ tin của mình lẫn người khác)
#   nội dung nằm ở     #content-<mid>
# data-mid là số tăng dần nên dùng làm mốc "đã đọc tới đâu" được.
# innerText chèn MỘT DÒNG TRỐNG giữa các khối block. Teams bọc mỗi dòng của tin
# nhắn trong một <p> riêng, nên tin 3 dòng bên Teams sang Discord bị giãn thành
# 3 dòng cách nhau bằng dòng trống. Gộp mọi chuỗi xuống dòng liên tiếp về 1.
BLANK_LINES_RE = re.compile(r"\n[ \t ]*\n+")


def normalise_message_text(text: str) -> str:
    if not text:
        return ""
    return BLANK_LINES_RE.sub("\n", text.replace("\r\n", "\n")).strip()


READ_MESSAGES_JS = """
(sinceMid) => {
  const out = [];
  document.querySelectorAll('[data-tid="chat-pane-message"]').forEach(el => {
    const mid = el.getAttribute('data-mid');
    if (!mid) return;
    const num = Number(mid);
    if (!Number.isFinite(num) || num <= sinceMid) return;
    const author = document.getElementById('author-' + mid);
    const content = document.getElementById('content-' + mid);
    out.push({
      mid: num,
      isMine: (el.className || '').includes('ChatMyMessage'),
      author: author ? author.innerText.trim() : '',
      text: content ? content.innerText.trim() : '',
    });
  });
  out.sort((a, b) => a.mid - b.mid);
  return out;
}
"""

# Xuất hiện = session hết hạn, cần login lại.
# Lưu ý: trang đăng nhập MSA đời mới render THẲNG tại teams.live.com/v2/ (không
# redirect sang login.live.com) và dùng #usernameEntry, nên chỉ dựa vào URL là trượt.
LOGIN_SELECTORS = [
    "#usernameEntry",           # MSA sign-in mới (Fluent)
    "#passwordEntry",
    'input[name="loginfmt"]',   # MSA/AAD đời cũ
    'input[name="passwd"]',
    "#i0116",
    "#i0118",
    'input[type="email"][autocomplete*="username"]',
]

# Marker nằm trong DOM nhưng ẩn (hidden input) -> phải kiểm tra bằng count(),
# không dùng được is_visible().
LOGIN_HIDDEN_MARKERS = [
    'input[name="PPFT"]',       # token chỉ có trên trang login Microsoft Account
]

LOGIN_URL_MARKERS = (
    "login.microsoftonline.com",
    "login.live.com",
    "login.microsoft.com",
    "account.microsoft.com",
)


class TeamsError(RuntimeError):
    """Lỗi chung của tầng Teams."""


class TeamsLoginRequired(TeamsError):
    """Session Microsoft hết hạn -> cần chạy lại login.py."""


class TeamsNotConfigured(TeamsError):
    """Chưa chọn group chat đích (chưa chạy login.py)."""


class TeamsProfileLocked(TeamsError):
    """Profile Chromium đang bị process khác giữ (thường là login.py còn mở)."""


class TeamsWrongChat(TeamsError):
    """Không mở/không xác minh được đúng group chat -> TỪ CHỐI gửi."""


class TeamsSendError(TeamsError):
    """Gửi thất bại vì không tìm thấy ô soạn tin / không xác nhận được đã gửi."""


class TeamsClient:
    """Giữ 1 browser context sống lâu, tái dùng cho mọi lần gửi.

    Playwright không thread-safe theo kiểu song song trên cùng 1 page, nên mọi
    thao tác gửi đều đi qua `self._lock` để tuần tự hoá.
    """

    def __init__(
        self,
        headless: bool = True,
        target_thread_id: str | None = None,
        target_label: str = "",
    ):
        self.headless = headless
        self.target_thread_id = target_thread_id
        self.target_label = target_label
        self._pw: Playwright | None = None
        self._ctx: BrowserContext | None = None
        self._page: Page | None = None
        self._lock = asyncio.Lock()
        # Mốc "đã đọc tới đâu" cho luồng Teams -> Discord.
        self._last_mid = 0
        self._relay_primed = False

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self._ctx is not None:
            return

        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()

        try:
            self._ctx = await self._pw.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=self.headless,
                viewport={"width": 1440, "height": 900},
                locale="en-US",
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
            )
        except PlaywrightError as exc:
            await self._shutdown_pw()
            if "ProcessSingleton" in str(exc) or "already in use" in str(exc).lower():
                raise TeamsProfileLocked(
                    "Profile Chromium đang được process khác dùng. "
                    "Đóng cửa sổ login.py (hoặc bot đang chạy) rồi thử lại."
                ) from exc
            raise

        self._ctx.set_default_timeout(20_000)
        self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()

        await self._goto(TEAMS_HOME)

    async def close(self) -> None:
        if self._ctx is not None:
            try:
                await self._ctx.close()
            except PlaywrightError:
                pass
            self._ctx = None
            self._page = None
        await self._shutdown_pw()

    async def _shutdown_pw(self) -> None:
        if self._pw is not None:
            try:
                await self._pw.stop()
            except (PlaywrightError, RuntimeError):
                pass
            self._pw = None

    # ------------------------------------------------------------------ helpers

    @property
    def page(self) -> Page:
        if self._page is None:
            raise TeamsError("TeamsClient chưa start().")
        return self._page

    async def _goto(self, url: str) -> None:
        # Teams web tải rất chậm; "load" hay timeout dù app đã dùng được,
        # nên chỉ chờ tới domcontentloaded rồi để các bước sau tự chờ selector.
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        except PlaywrightError as exc:
            log.warning("goto(%s) không hoàn tất: %s", url, exc)

    def _frames(self):
        """Teams đôi khi bọc app trong iframe -> phải quét cả main frame lẫn con."""
        page = self.page
        try:
            return list(page.frames) or [page.main_frame]
        except PlaywrightError:
            return [page.main_frame]

    async def _first_visible(
        self, selectors: list[str], timeout: float = 20.0
    ) -> Locator | None:
        """Trả về locator đầu tiên nhìn thấy được, quét qua mọi frame."""
        deadline = time.monotonic() + timeout
        while True:
            for frame in self._frames():
                for sel in selectors:
                    try:
                        loc = frame.locator(sel).first
                        if await loc.is_visible(timeout=200):
                            return loc
                    except PlaywrightError:
                        continue
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.3)

    async def _exists(self, selectors: list[str]) -> bool:
        """Có mặt trong DOM (kể cả đang ẩn), quét mọi frame."""
        for frame in self._frames():
            for sel in selectors:
                try:
                    if await frame.locator(sel).count() > 0:
                        return True
                except PlaywrightError:
                    continue
        return False

    async def _is_login_page(self) -> bool:
        """Kiểm tra tức thời (không chờ). Việc chờ do _wait_for_state lo.

        CHỈ tin vào form đăng nhập hiển thị thật. Không dùng URL ở đây: kể cả khi
        đã đăng nhập, Teams vẫn ghé qua login.live.com để SSO ngầm rồi bật lại —
        bắt theo URL sẽ báo nhầm "hết session" ngay giữa lúc đang đăng nhập được.
        """
        return await self._first_visible(LOGIN_SELECTORS, timeout=0.1) is not None

    async def _on_login_host(self) -> bool:
        """Tín hiệu yếu: chỉ dùng để giải thích khi đã hết thời gian chờ."""
        try:
            url = self.page.url or ""
        except PlaywrightError:
            return False
        if any(marker in url for marker in LOGIN_URL_MARKERS):
            return True
        return await self._exists(LOGIN_HIDDEN_MARKERS)

    async def _wait_for_state(self, timeout: float) -> tuple[str, Locator | None]:
        """Chờ tới khi xác định được trạng thái: 'ready' | 'login' | 'unknown'.

        Teams web redirect qua nhiều chặng (/v2 -> /gather -> login.live.com) và
        render bằng JS, mất ~6-30s. Nếu hỏi quá sớm thì chưa có gì trên DOM cả và
        ta sẽ kết luận sai, nên phải chờ CẢ HAI khả năng cùng lúc thay vì check
        tuần tự với timeout ngắn.
        """
        deadline = time.monotonic() + timeout
        while True:
            compose = await self._first_visible(COMPOSE_SELECTORS, timeout=0.1)
            if compose is not None:
                return "ready", compose
            if await self._is_login_page():
                return "login", None
            if time.monotonic() >= deadline:
                # Hết giờ mà vẫn mắc kẹt ở host đăng nhập -> nhiều khả năng session
                # hỏng chứ không phải Teams đổi giao diện. Báo cho đúng nguyên nhân.
                if await self._on_login_host():
                    return "login", None
                return "unknown", None
            await asyncio.sleep(0.5)

    # ------------------------------------------------- nhận diện chat đang mở

    async def _attr_of(self, selector: str, attr: str) -> str | None:
        for frame in self._frames():
            try:
                loc = frame.locator(selector).first
                if await loc.count() == 0:
                    continue
                value = await loc.get_attribute(attr, timeout=2_000)
            except PlaywrightError:
                continue
            if value:
                return value
        return None

    async def _current_thread_id(self) -> str | None:
        """Thread id của chat đang mở.

        Nguồn chính là src của avatar trên header chat:
            .../api/groups/v1/threads/19:xxxx@thread.skype/profilepicturev2
        Chạy được cả headless. (data-tabster/LeftRailSelectedItem chỉ có khi
        headful nên chỉ dùng làm dự phòng.)
        """
        for selector in HEADER_AVATAR_SELECTORS:
            src = await self._attr_of(selector, "src")
            if src:
                match = THREAD_ID_IN_URL_RE.search(src)
                if match:
                    return match.group(1)

        tabster = await self._attr_of(SELECTED_CHAT_SELECTOR, "data-tabster")
        if tabster:
            match = THREAD_ID_RE.search(tabster)
            if match:
                return match.group(0)
        return None

    async def _current_title(self) -> str:
        loc = await self._first_visible(CHAT_TITLE_SELECTORS, timeout=2.0)
        if loc is None:
            return ""
        try:
            return (await loc.inner_text()).strip()
        except PlaywrightError:
            return ""

    async def _on_target_chat(self) -> bool:
        """Có đang ở đúng group chat không.

        Ưu tiên thread id (định danh thật). Nếu không đọc được id — chẳng hạn
        thanh chat bên trái bị thu gọn — mới lùi về so tên chat.
        """
        current_id = await self._current_thread_id()
        if current_id and self.target_thread_id:
            return current_id == self.target_thread_id
        if self.target_label:
            return (await self._current_title()) == self.target_label
        return False

    async def _switch_to_target(self) -> bool:
        """Bấm vào group chat đích trong danh sách bên trái, rồi xác minh lại."""
        if not self.target_thread_id:
            return False

        # id có chứa ':' và '@' -> dùng attribute selector để khỏi phải escape CSS.
        selector = f'[id="title-chat-list-item_{self.target_thread_id}"]'
        item = await self._first_visible([selector], timeout=8.0)
        if item is None:
            return False

        try:
            await item.click(timeout=10_000)
        except PlaywrightError as exc:
            log.warning("Không bấm được vào chat đích: %s", exc)
            return False

        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            if await self._on_target_chat():
                return True
            await asyncio.sleep(0.5)
        return False

    # ------------------------------------------------------------------ public API

    async def check_login(self) -> bool:
        """True nếu session còn sống và mở được đúng chat đích."""
        async with self._lock:
            try:
                await self._open_chat()
                return True
            except TeamsError:
                return False

    async def _open_chat(self) -> Locator:
        """Đảm bảo đang ở ĐÚNG group chat đích, trả về locator ô soạn tin."""
        if not self.target_thread_id and not self.target_label:
            raise TeamsNotConfigured(
                "Chưa chọn group chat Teams. Chạy: python login.py"
            )

        try:
            if "teams.live.com" not in (self.page.url or ""):
                await self._goto(TEAMS_HOME)
        except PlaywrightError:
            await self._goto(TEAMS_HOME)

        state, compose = await self._wait_for_state(APP_READY_TIMEOUT)

        if state == "login":
            raise TeamsLoginRequired(
                "Session Microsoft đã hết hạn. Dừng bot và chạy lại: python login.py"
            )
        if state != "ready":
            await self.diagnose("no-compose-box")
            raise TeamsSendError(
                "Không tìm thấy ô soạn tin trong Teams (chờ "
                f"{APP_READY_TIMEOUT:.0f}s). Có thể mạng chậm, hoặc Teams web đã đổi "
                "giao diện — xem ảnh/HTML trong thư mục diagnose/ để cập nhật selector."
            )

        # Không bao giờ gửi mà chưa xác minh đang ở đúng chat.
        if await self._on_target_chat():
            return compose

        log.info("Không ở đúng chat đích, đang chuyển sang %r...", self.target_label)
        if await self._switch_to_target():
            # Ô soạn tin thuộc chat cũ đã bị render lại -> lấy locator mới.
            compose = await self._first_visible(COMPOSE_SELECTORS, timeout=20.0)
            if compose is not None:
                return compose

        await self.diagnose("wrong-chat")
        current = await self._current_title() or "(không đọc được)"
        raise TeamsWrongChat(
            f"Không mở được group chat đích ({self.target_label!r}). "
            f"Teams đang mở: {current!r}. Đã HUỶ gửi để tránh gửi nhầm chỗ. "
            "Chạy lại `python login.py` nếu bạn đã đổi/rời group chat đó."
        )

    async def send(self, text: str) -> None:
        """Gửi `text` vào group chat đích. Raise TeamsError nếu thất bại."""
        if not text.strip():
            raise TeamsSendError("Nội dung tin nhắn rỗng.")

        async with self._lock:
            compose = await self._open_chat()

            try:
                await compose.click(timeout=10_000)
            except PlaywrightError:
                await compose.focus()

            keyboard = self.page.keyboard

            # insert_text thay vì gõ từng phím: nhanh hơn và không làm hỏng
            # dấu tiếng Việt (mô phỏng phím dễ bị CKEditor/IME xử lý sai).
            lines = text.split("\n")
            for index, line in enumerate(lines):
                if index:
                    await keyboard.press("Shift+Enter")
                if line:
                    await keyboard.insert_text(line)

            await asyncio.sleep(0.3)

            send_button = await self._first_visible(SEND_BUTTON_SELECTORS, timeout=2.0)
            if send_button is not None:
                try:
                    await send_button.click(timeout=5_000)
                except PlaywrightError:
                    await keyboard.press("Enter")
            else:
                await keyboard.press("Enter")

            if not await self._wait_compose_cleared(compose, text):
                await self.diagnose("send-not-confirmed")
                raise TeamsSendError(
                    "Đã nhập nội dung nhưng ô soạn tin không được xoá — "
                    "không chắc tin đã gửi. Kiểm tra Teams và thư mục diagnose/."
                )

    async def fetch_new_messages(self) -> list[dict]:
        """Tin nhắn Teams mới kể từ lần gọi trước (luồng Teams -> Discord).

        Lần gọi đầu chỉ đặt mốc và trả về rỗng, để khỏi dội toàn bộ lịch sử chat
        sang Discord lúc bot vừa khởi động.

        Không tự điều hướng: nếu chưa ở đúng chat thì bỏ qua vòng này, để việc
        chuyển chat cho luồng gửi lo — tránh hai luồng giành nhau cái tab.
        """
        async with self._lock:
            if not await self._on_target_chat():
                return []

            try:
                rows = await self.page.evaluate(READ_MESSAGES_JS, self._last_mid)
            except PlaywrightError as exc:
                log.debug("Không đọc được tin nhắn: %s", exc)
                return []

            if not rows:
                return []

            for row in rows:
                row["text"] = normalise_message_text(row.get("text", ""))

            self._last_mid = max(row["mid"] for row in rows)

            if not self._relay_primed:
                # Danh sách tin nhắn render CHẬM hơn ô soạn tin, nên vòng đầu tiên
                # có thể đọc được 0 tin dù chat đã mở. Chỉ chốt mốc khi thật sự
                # thấy tin, nếu không sẽ coi cả lịch sử cũ là "tin mới".
                self._relay_primed = True
                log.info("Đã chốt mốc đọc tin Teams tại mid=%s", self._last_mid)
                return []

            return rows

    async def _wait_compose_cleared(
        self, compose: Locator, sent_text: str, timeout: float = 15.0
    ) -> bool:
        """Teams xoá ô soạn tin sau khi gửi thành công -> dùng làm tín hiệu xác nhận.

        Không chỉ kiểm tra rỗng: ô soạn tin có thể hiện lại placeholder ("Type a
        message") sau khi gửi, khi đó inner_text vẫn khác rỗng. Nên tín hiệu chắc
        chắn hơn là: nội dung vừa nhập KHÔNG còn nằm trong ô nữa.
        """
        probe = sent_text.strip()[:40]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                content = (await compose.inner_text()).strip()
                if not content:
                    return True
                if probe and probe not in content:
                    return True
            except PlaywrightError:
                # Ô soạn tin bị render lại sau khi gửi -> coi như đã gửi.
                return True
            await asyncio.sleep(0.4)
        return False

    async def diagnose(self, tag: str) -> Path | None:
        """Chụp màn hình + dump HTML để sửa selector khi Teams đổi giao diện."""
        try:
            DIAGNOSE_DIR.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            shot = DIAGNOSE_DIR / f"{stamp}-{tag}.png"
            await self.page.screenshot(path=str(shot), full_page=False)
            (DIAGNOSE_DIR / f"{stamp}-{tag}.html").write_text(
                await self.page.content(), encoding="utf-8"
            )
            log.warning("Đã lưu ảnh chẩn đoán: %s", shot)
            return shot
        except (PlaywrightError, OSError) as exc:
            log.warning("Không chụp được ảnh chẩn đoán: %s", exc)
            return None
