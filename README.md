# discord-teams

Discord bot: gõ `/team message:<nội dung>` → tin nhắn được gửi vào **group chat Microsoft Teams** của bạn.

Hỗ trợ **account Microsoft cá nhân (Teams Free)**.

---

## Vì sao phải dùng browser automation?

Đây là điều bạn nên biết trước khi dùng. Với account Teams cá nhân, **không có API nào gửi được tin nhắn**:

| Cách | Trạng thái |
|---|---|
| Microsoft Graph API (`ChatMessage.Send`) | ❌ Docs ghi rõ *"Delegated (personal Microsoft account): **Not supported**"* |
| Incoming Webhook (Office 365 Connector) | ❌ Đã khai tử — không tạo mới được từ 15/08/2024 |
| Power Automate Workflows (bản thay thế) | ❌ Cần license work/school, account cá nhân không có |
| **Điều khiển Teams web (Playwright)** | ✅ Cách duy nhất còn lại |

Nên tool này chạy một Chromium ẩn đã đăng nhập sẵn Teams, và "gõ" tin nhắn hộ bạn.

**Hệ quả cần chấp nhận:**
- Máy chạy bot phải luôn bật, và tốn ~300–500MB RAM cho Chromium.
- Tin nhắn hiện trên Teams **dưới danh nghĩa chính bạn**, không phải bot.
- Session Microsoft sẽ hết hạn sau vài tuần → phải chạy lại `login.py`. Dùng `/team_status` để kiểm tra.
- Nếu Microsoft đổi giao diện Teams web, selector có thể hỏng. Khi đó tool tự lưu ảnh + HTML vào `diagnose/` để sửa.
- Đây là automation trên chính tài khoản của bạn, nhưng vẫn nằm ngoài cách dùng Microsoft dự tính. Đừng dùng để spam.

---

## Cài đặt

```powershell
cd D:\Tool\discord-teams

py -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m playwright install chromium
```

### 1. Tạo Discord bot

1. Vào https://discord.com/developers/applications → **New Application**
2. Tab **Bot** → **Reset Token** → copy token

Rồi chọn cách cài theo **ai sẽ dùng `/team`**. Không cần bật Privileged Intent nào cả.

| Muốn gì | Chọn |
|---|---|
| Cả server đều gửi được | **Cách B — Guild Install** (cần quyền Manage Server) |
| Chỉ mình bạn gửi | **Cách A — User Install** (không cần quyền gì) |

#### Cách A — User Install (chỉ mình bạn dùng được)

Cài app vào **tài khoản bạn** thay vì vào server. `/team` chạy được ở mọi server
bạn có mặt và cả trong DM, **không cần quyền admin của server nào**. Nhưng người
khác sẽ không thấy và không gọi được lệnh.

1. Tab **Installation** → **Installation Contexts** → tick **User Install**
2. Mục **Install Link** → chọn *Discord Provided Link* → copy
3. Ở **Default Install Settings** → **User Install** → Scopes: `applications.commands`
4. Mở link → **Authorize**
5. Trong `.env`: **để trống** `DISCORD_GUILD_ID`

> Lệnh sync global nên Discord có thể mất vài phút tới ~1h mới hiện. Kiên nhẫn.

#### Cách B — Guild Install (cả server dùng được)

Cần quyền **Manage Server** ở server đó — dropdown "Add to Server" chỉ liệt kê
những server như vậy. Không có quyền thì phải **nhờ chủ server tự bấm link**;
Discord không cho người ngoài thêm bot vào server người khác.

1. Tab **OAuth2** → **OAuth2 URL Generator**
2. SCOPES: tick **cả** `bot` **và** `applications.commands`
   — thiếu cái thứ hai sẽ gặp lỗi `50001 Missing Access` lúc sync
3. Mở URL → chọn server → Authorize
4. Trong `.env`: điền `DISCORD_GUILD_ID` → lệnh xuất hiện tức thì

Khi mở cho cả nhóm, xem lại 3 thiết lập trong `.env`:

| Biến | Ý nghĩa |
|---|---|
| `ALLOWED_USER_IDS=` | Để trống = mọi người dùng được |
| `COOLDOWN_SECONDS=10` | Chặn 1 người spam làm nghẽn hàng đợi chung |
| `EPHEMERAL_REPLY=0` | Để cả nhóm thấy tin đã gửi, tránh gửi trùng |

> Tin nhắn tới Teams luôn hiện **dưới danh nghĩa tài khoản Teams của bạn**, bất kể
> ai gõ lệnh. `MESSAGE_PREFIX` mặc định có ghi kèm tên người gửi bên Discord —
> đừng bỏ `{user}` ra khỏi template, nếu không sẽ không truy được ai gửi gì.

### 2. Cấu hình

```powershell
Copy-Item .env.example .env
notepad .env
```

Điền `DISCORD_TOKEN`. Nên điền luôn:
- `DISCORD_GUILD_ID` — để slash command xuất hiện ngay (bỏ trống thì Discord cache tới ~1 tiếng).
- `ALLOWED_USER_IDS` — **quan trọng**: nếu bỏ trống thì bất kỳ ai trong server cũng gửi được tin nhắn vào Teams của bạn.

> Lấy ID: bật Discord → Settings → Advanced → Developer Mode, rồi chuột phải vào server/user → Copy ID.

### 3. Đăng nhập Teams (chạy 1 lần)

```powershell
.venv\Scripts\python.exe login.py
```

Một cửa sổ Chromium mở ra:
1. Đăng nhập Microsoft — **nhớ tick "Stay signed in"**.
2. Mở đúng group chat bạn muốn bot gửi vào.
3. Quay lại terminal, bấm Enter.

Session lưu ở `browser-profile/`, group chat lưu ở `target.json`.

### 4. Chạy bot

```powershell
.venv\Scripts\python.exe bot.py
```

---

## Dùng

```
/team message: Deploy xong rồi nhé mọi người
```

Teams nhận được:

```
[Discord - Puddy] Deploy xong rồi nhé mọi người
```

Đổi định dạng bằng `MESSAGE_PREFIX` trong `.env` — biến dùng được: `{user}`, `{message}`, `{channel}`, `{guild}`.

### Chiều ngược: Teams → Discord

Điền ID kênh Discord vào `.env` để bật:

```
DISCORD_RELAY_CHANNEL_ID=<id kênh>
RELAY_POLL_SECONDS=10
```

`DISCORD_RELAY_CHANNEL_ID` là **ID kênh**, không phải ID server — chuột phải vào
tên kênh (có dấu `#`) → *Copy Channel ID*. Nhiều kênh thì ngăn cách bằng dấu phẩy.
Bỏ trống = tắt hẳn chiều này.

Mặc định chỉ những tin có **trigger** mới được chuyển, và trigger bị cắt bỏ:

```
Teams:    Thiện Nguyễn: discord-cho tôi 1 ly trà sữa
                        └───────┘
                        bị cắt đi

Discord:  **Thiện Nguyễn** (Teams)
          cho tôi 1 ly trà sữa
```

Tin không có trigger (`nay ai uống gì ko`, `ok`, ...) bị bỏ qua hoàn toàn — nhóm
đông người vẫn chat bình thường mà Discord không bị ngập.

Đổi trigger bằng `RELAY_TRIGGER_PREFIX` (không phân biệt hoa thường).
Đặt thành rỗng = chuyển mọi tin.

Ba điểm đã xử lý sẵn:

- **Không lặp vô hạn.** Tin do bot đẩy từ Discord sang (bắt đầu bằng `[Discord - `)
  bị bỏ qua, không chuyển ngược lại.
- **Không dội lịch sử.** Vòng đầu chỉ chốt mốc, không đẩy tin cũ sang Discord.
- **Không ping cả server.** Nội dung từ Teams nằm ngoài tầm kiểm soát nên mọi
  `@everyone`/`@here`/`@role` đều bị vô hiệu hoá.

> Bot phải có quyền **Send Messages** trong kênh relay. Chiều `/team` thì không
> cần, vì trả lời interaction đi đường khác.

```
/team_status
```

Kiểm tra session Teams còn sống không, **không gửi tin nào**. Dùng cái này thay vì gửi tin thử.

---

## Xử lý sự cố

| Triệu chứng | Cách xử lý |
|---|---|
| `🔑 Session Microsoft đã hết hạn` | Dừng bot (Ctrl+C) → chạy `login.py` → chạy lại `bot.py` |
| `Profile Chromium đang được process khác dùng` | `login.py` và `bot.py` không chạy chung profile được. Đóng cái kia đi. |
| `Không tìm thấy ô soạn tin` | Chạy `python inspect_dom.py` rồi đối chiếu `diagnose/dom-report.txt` để cập nhật `COMPOSE_SELECTORS` trong [teams_client.py](teams_client.py). Muốn xem trực tiếp thì đặt `TEAMS_HEADLESS=0`. |
| `Không mở được group chat đích` | Tool đã **huỷ gửi** để khỏi gửi nhầm chỗ. Thường do bạn đã rời/đổi group chat → chạy lại `login.py`. |
| `Chưa chọn group chat Teams` sau khi nâng cấp | `target.json` đời cũ lưu URL (vô dụng vì Teams không đổi URL). Chạy lại `login.py` để lưu theo thread id. |
| `403 Forbidden (50001): Missing Access` lúc sync | Bot được mời thiếu scope `applications.commands`. Mời lại theo Cách B, hoặc chuyển hẳn sang Cách A. Bot sẽ in sẵn link mời đúng khi gặp lỗi này. |
| Slash command không hiện | User Install: chờ vài phút tới ~1h (sync global). Guild Install: điền `DISCORD_GUILD_ID` rồi restart bot. |
| Không chọn được server khi mời bot | Dropdown chỉ hiện server bạn có quyền Manage Server. Dùng Cách A (User Install) để khỏi cần quyền đó. |
| Lần `/team` đầu chậm ~30–60s | Bình thường — Teams web cold-start. Các lần sau gần như tức thì. |

---

## Cấu trúc

| File | Vai trò |
|---|---|
| [bot.py](bot.py) | Discord bot, đăng ký `/team` và `/team_status` |
| [teams_client.py](teams_client.py) | Driver Playwright — mở chat, nhập nội dung, xác nhận đã gửi |
| [login.py](login.py) | Chạy 1 lần: đăng nhập thủ công + chọn group chat |
| [inspect_dom.py](inspect_dom.py) | Chỉ đọc DOM Teams, dump ra `diagnose/` — dùng khi cần sửa selector |
| [config.py](config.py) | Đọc `.env` và `target.json` |

### Group chat được định danh thế nào

Teams v2 **không đổi URL khi chuyển chat** — địa chỉ luôn là `teams.live.com/v2/`.
Nên tool neo vào **thread id** (`19:...@thread.skype`), đọc từ `src` của avatar
trên header chat.

Trước **mỗi** lần gửi, tool đối chiếu thread id đang mở với cái đã lưu:

- Khớp → gửi.
- Lệch → tự bấm vào đúng chat trong danh sách bên trái, rồi kiểm tra lại.
- Vẫn không được → **huỷ gửi** và báo lỗi.

Thà báo đỏ còn hơn gửi nhầm cuộc trò chuyện. Nếu không đọc được thread id, tool
lùi về so tên chat chứ không bao giờ gửi mù.

Ba thứ **không bao giờ được commit** (đã có trong `.gitignore`): `.env`, `browser-profile/` (chứa cookie Microsoft), `target.json`.
