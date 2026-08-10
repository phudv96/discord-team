"""Discord bot: /team <message> -> gửi tin nhắn sang group chat Microsoft Teams.

    python bot.py

Yêu cầu: đã chạy `python login.py` trước để có session Teams + group chat đích.
"""

from __future__ import annotations

import asyncio
import logging
import sys

import discord
from discord import app_commands
from discord.ext import tasks

from config import Settings, load_target
from teams_client import (
    TeamsClient,
    TeamsError,
    TeamsLoginRequired,
    TeamsNotConfigured,
    TeamsProfileLocked,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bot")

# Tên cố định để nhận lại đúng webhook đã tạo (thay vì tạo mới mỗi lần).
WEBHOOK_NAME = "discord-teams relay"


class RelayWebhookError(Exception):
    """Không tạo/dùng được webhook để đăng tin trông như tin nhắn thật."""


class TeamsBridge(discord.Client):
    def __init__(self, settings: Settings):
        # Chỉ intent mặc định (không có cái nào privileged, khỏi bật gì trong
        # Developer Portal). Cần GUILDS để đọc được tên server/kênh cho MESSAGE_PREFIX.
        super().__init__(intents=discord.Intents.default())
        self.settings = settings
        self.tree = app_commands.CommandTree(self)
        self.teams: TeamsClient | None = None
        self._start_error: str | None = None
        self._start_lock = asyncio.Lock()
        self._relay_channels: list[discord.abc.Messageable] = []
        self._webhooks: dict[int, discord.Webhook] = {}

    async def setup_hook(self) -> None:
        # Khởi động Teams sớm để lần /team đầu tiên không phải chờ cold-start.
        # Thất bại ở đây KHÔNG được làm chết bot: cứ online rồi báo lỗi rõ ràng
        # khi user gọi lệnh, hơn là chết im lặng lúc boot.
        try:
            await self.ensure_teams()
        except Exception as exc:  # noqa: BLE001
            log.error("Chưa khởi động được Teams: %s", exc)
            log.error("Bot vẫn online; /team sẽ báo lỗi này cho tới khi khắc phục.")

        if self.settings.guild_ids:
            for guild_id in self.settings.guild_ids:
                guild = discord.Object(id=guild_id)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                log.info("Đã sync %d command vào guild %s", len(synced), guild_id)

        else:
            synced = await self.tree.sync()
            log.info(
                "Đã sync %d command global — hợp lệ cho cả User Install. "
                "Discord có thể mất vài phút tới ~1h mới hiện lệnh.",
                len(synced),
            )

    async def ensure_teams(self) -> TeamsClient:
        """Khởi động Playwright nếu chưa có. Lỗi ở đây không làm chết bot —
        bot vẫn online và báo lỗi rõ ràng khi user gọi /team."""
        async with self._start_lock:
            if self.teams is not None:
                return self.teams

            target = load_target()
            if target is None:
                self._start_error = (
                    "Chưa chọn group chat Teams. Chạy `python login.py` trên máy host bot."
                )
                raise TeamsNotConfigured(self._start_error)

            client = TeamsClient(
                headless=self.settings.headless,
                target_thread_id=target["thread_id"],
                target_label=target.get("label", ""),
            )
            try:
                await client.start()
            except TeamsProfileLocked as exc:
                self._start_error = str(exc)
                raise
            except Exception as exc:  # noqa: BLE001 - báo lại cho user qua Discord
                self._start_error = f"Không khởi động được trình duyệt: {exc}"
                raise TeamsError(self._start_error) from exc

            self.teams = client
            self._start_error = None
            log.info("Teams sẵn sàng — group chat: %s", target["label"])
            return client

    async def on_ready(self) -> None:
        log.info("Đăng nhập Discord với tên %s (id=%s)", self.user, self.user.id)

        await self.start_relay()

        # Cache guild chỉ đầy sau khi kết nối gateway, nên phần đối chiếu này
        # không đặt được trong setup_hook.
        if not self.settings.guild_ids:
            return

        for guild in self.guilds:
            if guild.id not in self.settings.guild_ids:
                log.warning(
                    "Bot đang ở '%s' (%s) nhưng server này KHÔNG có trong "
                    "DISCORD_GUILD_ID -> /team sẽ KHÔNG hiện ở đó. "
                    "Thêm id này vào .env (ngăn cách bằng dấu phẩy) rồi khởi động lại.",
                    guild.name,
                    guild.id,
                )

        known = {g.id for g in self.guilds}
        for guild_id in self.settings.guild_ids:
            if guild_id not in known:
                log.warning(
                    "DISCORD_GUILD_ID có %s nhưng bot KHÔNG ở trong server đó.",
                    guild_id,
                )

    # ------------------------------------------------ Teams -> Discord

    async def start_relay(self) -> None:
        """Bật vòng lặp chuyển tin Teams về Discord (nếu đã cấu hình kênh)."""
        if not self.settings.relay_channel_ids or self.relay_loop.is_running():
            return

        channels = []
        for channel_id in self.settings.relay_channel_ids:
            channel = self.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await self.fetch_channel(channel_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                    # Nhầm lẫn hay gặp nhất: dán ID SERVER thay vì ID KÊNH.
                    hint = ""
                    if channel_id in self.settings.guild_ids or self.get_guild(channel_id):
                        hint = " — đây là ID SERVER, không phải ID kênh."
                    log.error(
                        "Không dùng được kênh relay %s (%s)%s",
                        channel_id,
                        type(exc).__name__,
                        hint,
                    )
                    continue

            if not isinstance(channel, discord.abc.Messageable):
                log.error("Kênh relay %s không gửi tin được (loại: %s)",
                          channel_id, type(channel).__name__)
                continue
            channels.append(channel)

        if not channels:
            log.error(
                "Không có kênh relay hợp lệ nào -> chiều Teams -> Discord vẫn TẮT. "
                "Lấy ID đúng bằng cách chuột phải vào KÊNH -> Copy Channel ID."
            )
            return

        self._relay_channels = channels
        self.relay_loop.change_interval(seconds=self.settings.relay_poll_seconds)
        self.relay_loop.start()
        log.info(
            "Relay Teams -> Discord: bật, đẩy vào %s mỗi %ds",
            ", ".join(f"#{getattr(c, 'name', c.id)}" for c in channels),
            self.settings.relay_poll_seconds,
        )
        if self.settings.relay_trigger:
            log.info(
                "Chỉ chuyển tin Teams bắt đầu bằng %r (trigger sẽ bị cắt bỏ).",
                self.settings.relay_trigger,
            )
        else:
            log.warning(
                "RELAY_TRIGGER_PREFIX rỗng -> chuyển MỌI tin nhắn trong group "
                "Teams sang Discord. Nhóm đông người sẽ rất ồn."
            )

    @tasks.loop(seconds=10)
    async def relay_loop(self) -> None:
        if self.teams is None or not self._relay_channels:
            return

        try:
            messages = await self.teams.fetch_new_messages()
        except TeamsError as exc:
            log.debug("Relay bỏ qua vòng này: %s", exc)
            return

        for msg in messages:
            # Lọc echo + trigger (xem Settings.relay_body).
            body = self.settings.relay_body(msg.get("text") or "")
            if body is None:
                continue

            author = (msg.get("author") or "?").strip()
            if len(body) > 1800:
                body = body[:1797] + "..."
            payload = f"**{author}** _(Teams)_\n{body}"

            for channel in self._relay_channels:
                try:
                    await channel.send(
                        payload,
                        # Tin từ Teams là nội dung ngoài tầm kiểm soát -> chặn mọi
                        # @everyone/@here/@role để không bị ping cả server.
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException as exc:
                    log.warning(
                        "Không gửi được tin relay vào #%s: %s",
                        getattr(channel, "name", channel.id),
                        exc,
                    )

    @relay_loop.before_loop
    async def _before_relay(self) -> None:
        await self.wait_until_ready()

    # ------------------------------------------------ /team_discord (webhook)

    async def get_relay_webhook(
        self, channel: discord.abc.GuildChannel
    ) -> discord.Webhook:
        """Webhook để đăng tin trông như tin nhắn thật (tên + avatar tuỳ chỉnh
        theo từng lần gửi). Cache theo channel id, tạo mới nếu chưa có.

        Chỉ hoạt động trong kênh của server (guild), KHÔNG hoạt động ở DM —
        Discord không cho webhook trong DM.
        """
        cached = self._webhooks.get(channel.id)
        if cached is not None:
            return cached

        try:
            existing = await channel.webhooks()
        except discord.Forbidden as exc:
            raise RelayWebhookError(
                "Bot thiếu quyền **Manage Webhooks** trong kênh này."
            ) from exc
        except discord.HTTPException as exc:
            raise RelayWebhookError(f"Không đọc được danh sách webhook: {exc}") from exc

        hook = next((w for w in existing if w.name == WEBHOOK_NAME), None)
        if hook is None:
            try:
                hook = await channel.create_webhook(
                    name=WEBHOOK_NAME,
                    reason="discord-teams: đăng bản /team_discord trông như tin nhắn thật",
                )
            except discord.Forbidden as exc:
                raise RelayWebhookError(
                    "Bot thiếu quyền **Manage Webhooks** trong kênh này."
                ) from exc
            except discord.HTTPException as exc:
                raise RelayWebhookError(f"Không tạo được webhook: {exc}") from exc

        self._webhooks[channel.id] = hook
        return hook

    async def post_as_user(
        self, channel: discord.abc.Messageable, user: discord.abc.User, content: str
    ) -> None:
        """Đăng `content` vào `channel`, hiện tên + avatar của `user` — không
        nhãn bot, không dòng "X used /command" cho người khác thấy.

        DM/group DM không hỗ trợ webhook -> raise RelayWebhookError rõ ràng,
        để nơi gọi báo cho người dùng đổi qua /team.
        """
        target_channel = channel
        thread_param = None
        if isinstance(channel, discord.Thread):
            if channel.parent is None:
                raise RelayWebhookError("Không xác định được kênh cha của thread này.")
            target_channel = channel.parent
            thread_param = channel

        if not isinstance(target_channel, discord.abc.GuildChannel):
            raise RelayWebhookError(
                "Không dùng được ở DM/group DM — Discord không hỗ trợ webhook ở đó. "
                "Đổi qua `/team`."
            )

        webhook = await self.get_relay_webhook(target_channel)

        # KHÔNG lặp tên trong content — username của webhook đã hiện tên rồi.
        body = content if len(content) <= 2000 else content[:1997] + "..."

        kwargs = {}
        if thread_param is not None:
            kwargs["thread"] = thread_param

        try:
            await webhook.send(
                body,
                username=user.display_name,
                avatar_url=user.display_avatar.url,
                # Giống /team: cho phép ping @user (như tin nhắn thật), chặn
                # @everyone/@here/@role để không lách quyền qua bot.
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, roles=False, users=True
                ),
                **kwargs,
            )
        except discord.HTTPException as exc:
            raise RelayWebhookError(f"Discord từ chối đăng bản này: {exc}") from exc

    async def close(self) -> None:
        if self.relay_loop.is_running():
            self.relay_loop.cancel()
        if self.teams is not None:
            await self.teams.close()
        await super().close()


def register_commands(client: TeamsBridge, settings: Settings) -> None:
    def maybe_cooldown(func):
        """Giới hạn tần suất theo từng user. COOLDOWN_SECONDS=0 để tắt.

        Quan trọng khi mở cho cả server: chỉ có MỘT tab Chromium, nên vài người
        spam cùng lúc sẽ xếp hàng và làm nghẽn hàng đợi của mọi người.
        """
        if settings.cooldown_seconds <= 0:
            return func
        return app_commands.checks.cooldown(
            1, float(settings.cooldown_seconds), key=lambda i: i.user.id
        )(func)

    @client.tree.error
    async def on_command_error(
        interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        # Cooldown bị chặn TRƯỚC khi callback chạy, nên try/except trong callback
        # không bắt được -> phải xử lý ở tầng tree.
        if isinstance(error, app_commands.CommandOnCooldown):
            msg = f"⏳ Chờ {error.retry_after:.0f}s nữa rồi gửi tiếp nhé."
        else:
            log.exception("Lỗi app command", exc_info=error)
            msg = f"❌ Lỗi: `{type(error).__name__}: {error}`"

        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    @client.tree.command(
        name="team",
        description="Gửi tin nhắn sang group chat Microsoft Teams",
    )
    @app_commands.describe(message="Nội dung tin nhắn gửi sang Teams")
    # User Install: cài app vào TÀI KHOẢN bạn, không cần quyền admin của server.
    # Nhờ vậy /team dùng được ở mọi server bạn có mặt, kể cả trong DM.
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @maybe_cooldown
    async def team(interaction: discord.Interaction, message: str) -> None:
        if not settings.is_allowed(interaction.user.id):
            await interaction.response.send_message(
                "Bạn không có quyền dùng lệnh này.", ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=settings.ephemeral_reply)

        target = load_target()
        label = target["label"] if target else "?"

        payload = settings.message_prefix.format(
            user=interaction.user.display_name,
            message=message,
            channel=getattr(interaction.channel, "name", "DM"),
            guild=interaction.guild.name if interaction.guild else "DM",
        )

        try:
            teams = await client.ensure_teams()
            await asyncio.wait_for(teams.send(payload), timeout=settings.send_timeout)
        except asyncio.TimeoutError:
            await interaction.followup.send(
                f"⏱ Quá {settings.send_timeout}s chưa gửi xong — Teams web có thể đang treo. "
                "Kiểm tra lại trên Teams xem tin đã tới chưa.",
                ephemeral=settings.ephemeral_reply,
            )
        except TeamsLoginRequired as exc:
            await interaction.followup.send(
                f"🔑 {exc}", ephemeral=settings.ephemeral_reply
            )
        except TeamsError as exc:
            await interaction.followup.send(
                f"❌ {exc}", ephemeral=settings.ephemeral_reply
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Gửi tin thất bại")
            await interaction.followup.send(
                f"❌ Lỗi ngoài dự kiến: `{type(exc).__name__}: {exc}`",
                ephemeral=settings.ephemeral_reply,
            )
        else:
            preview = message if len(message) <= 120 else message[:117] + "..."
            await interaction.followup.send(
                f"✅ Đã gửi tới Teams — **{label}**\n> {preview}",
                ephemeral=settings.ephemeral_reply,
            )

    @client.tree.command(
        name="team_discord",
        description="Như /team, nhưng giữ lại 1 bản trông như tin nhắn thật trong kênh này",
    )
    @app_commands.describe(message="Nội dung tin nhắn gửi sang Teams")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @maybe_cooldown
    async def team_discord(interaction: discord.Interaction, message: str) -> None:
        if not settings.is_allowed(interaction.user.id):
            await interaction.response.send_message(
                "Bạn không có quyền dùng lệnh này.", ephemeral=True
            )
            return

        # Luôn ẩn: bản "thật" hiện ra là qua webhook ở bước sau, xác nhận này
        # chỉ để người gõ lệnh biết kết quả, không cần ai khác thấy.
        await interaction.response.defer(thinking=True, ephemeral=True)

        payload = settings.message_prefix.format(
            user=interaction.user.display_name,
            message=message,
            channel=getattr(interaction.channel, "name", "DM"),
            guild=interaction.guild.name if interaction.guild else "DM",
        )

        try:
            teams = await client.ensure_teams()
            await asyncio.wait_for(teams.send(payload), timeout=settings.send_timeout)
        except asyncio.TimeoutError:
            await interaction.followup.send(
                f"⏱ Quá {settings.send_timeout}s chưa gửi xong — Teams web có thể đang treo. "
                "Kiểm tra lại trên Teams xem tin đã tới chưa.",
                ephemeral=True,
            )
            return
        except TeamsError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("Gửi tin thất bại")
            await interaction.followup.send(
                f"❌ Lỗi ngoài dự kiến: `{type(exc).__name__}: {exc}`", ephemeral=True
            )
            return

        try:
            await client.post_as_user(interaction.channel, interaction.user, message)
        except RelayWebhookError as exc:
            await interaction.followup.send(
                f"✅ Đã gửi tới Teams, nhưng KHÔNG giữ được bản trong Discord: {exc}",
                ephemeral=True,
            )
            return

        await interaction.followup.send("✅ Đã gửi.", ephemeral=True)

    @client.tree.command(
        name="team_status",
        description="Kiểm tra session Teams còn sống không (không gửi tin nào)",
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def team_status(interaction: discord.Interaction) -> None:
        if not settings.is_allowed(interaction.user.id):
            await interaction.response.send_message(
                "Bạn không có quyền dùng lệnh này.", ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=True)

        target = load_target()
        if target is None:
            await interaction.followup.send(
                "⚠️ Chưa chọn group chat. Chạy `python login.py` trên máy host bot.",
                ephemeral=True,
            )
            return

        try:
            teams = await client.ensure_teams()
            ok = await asyncio.wait_for(teams.check_login(), timeout=settings.send_timeout)
        except (TeamsError, asyncio.TimeoutError) as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return

        if ok:
            await interaction.followup.send(
                f"✅ Session Teams OK — sẵn sàng gửi vào **{target['label']}**",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "🔑 Session Teams đã hết hạn. Dừng bot và chạy lại `python login.py`.",
                ephemeral=True,
            )


def main() -> int:
    settings = Settings.load()

    if not settings.discord_token:
        print("LỖI: chưa đặt DISCORD_TOKEN. Copy .env.example thành .env rồi điền token.")
        return 1

    if load_target() is None:
        print("LỖI: chưa chọn group chat Teams. Chạy trước: python login.py")
        return 1

    if settings.allowed_user_ids:
        log.info(
            "Giới hạn %d user được dùng /team.", len(settings.allowed_user_ids)
        )
    else:
        log.info(
            "ALLOWED_USER_IDS trống — MỌI người trong server đều gọi được /team "
            "(cooldown %ds/user). Tin nhắn tới Teams mang danh nghĩa tài khoản của bạn.",
            settings.cooldown_seconds,
        )

    client = TeamsBridge(settings)
    register_commands(client, settings)

    try:
        client.run(settings.discord_token, log_handler=None)
    except discord.LoginFailure:
        print("LỖI: DISCORD_TOKEN không hợp lệ.")
        return 1
    except discord.Forbidden as exc:
        # 50001 lúc sync = bot được mời vào server mà thiếu scope
        # applications.commands (scope này KHÔNG nằm trong scope 'bot').
        app_id = client.application_id or "<CLIENT_ID>"
        print(
            f"\nLỖI: Discord từ chối đăng ký slash command ({exc.text}).\n\n"
            "Thường là do bot được mời vào server mà THIẾU scope "
            "'applications.commands'.\n"
            "Mời lại bot bằng link này (không cần kick bot ra trước):\n\n"
            f"  https://discord.com/oauth2/authorize"
            f"?client_id={app_id}&scope=bot+applications.commands&permissions=536870912\n\n"
            "  (quyền 536870912 = Manage Webhooks, cần cho /team_discord — "
            "bỏ qua nếu chỉ dùng /team)\n\n"
            f"Nếu vẫn lỗi: kiểm tra DISCORD_GUILD_ID (đang là {settings.guild_ids}) "
            "có đúng server vừa mời không.\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
