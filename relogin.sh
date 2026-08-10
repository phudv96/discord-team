#!/usr/bin/env bash
#
# Đăng nhập lại Teams trên server không có màn hình.
#
#     ./relogin.sh
#
# Script tự lo phần lằng nhằng: tắt bot, dựng màn hình ảo, bật VNC, chạy login.py,
# rồi dọn dẹp. Việc của bạn chỉ còn: nối VNC viewer vào và bấm đăng nhập.
#
# Chỉ dựng những thứ còn thiếu — Xvfb/x11vnc nào đang chạy sẵn thì dùng lại và
# KHÔNG tắt lúc kết thúc (tránh giết mất service của bạn).

set -euo pipefail
cd "$(dirname "$0")"

DISPLAY_NUM="${DISPLAY_NUM:-:99}"
VNC_PORT="${VNC_PORT:-5900}"
PYTHON="${PYTHON:-.venv/bin/python}"

if [ ! -x "$PYTHON" ]; then
    echo "LỖI: không thấy $PYTHON — đã tạo venv chưa?" >&2
    exit 1
fi

for cmd in Xvfb x11vnc; do
    if ! command -v "$cmd" >/dev/null; then
        echo "LỖI: thiếu $cmd. Cài bằng: sudo apt install -y xvfb x11vnc" >&2
        exit 1
    fi
done

# --- 1. Tắt bot: bot và login.py không dùng chung profile Chromium được -------
if pgrep -f "[p]ython bot.py" >/dev/null; then
    echo "==> Tắt bot đang chạy..."
    pkill -f "[p]ython bot.py" || true
    sleep 2
fi

started_xvfb=0
started_vnc=0

cleanup() {
    if [ "$started_vnc" = 1 ]; then
        pkill -x x11vnc >/dev/null 2>&1 || true
    fi
    if [ "$started_xvfb" = 1 ]; then
        pkill -f "Xvfb $DISPLAY_NUM" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

# --- 2. Màn hình ảo ----------------------------------------------------------
if pgrep -f "Xvfb $DISPLAY_NUM" >/dev/null; then
    echo "==> Xvfb $DISPLAY_NUM đã chạy sẵn, dùng lại."
else
    echo "==> Dựng màn hình ảo $DISPLAY_NUM..."
    Xvfb "$DISPLAY_NUM" -screen 0 1440x900x24 >/dev/null 2>&1 &
    started_xvfb=1
    sleep 1
fi

# --- 3. VNC (chỉ localhost — bắt buộc đi qua SSH tunnel) ---------------------
if pgrep -x x11vnc >/dev/null; then
    echo "==> x11vnc đã chạy sẵn, dùng lại."
else
    if [ -f "$HOME/.vnc/passwd" ]; then
        auth=(-rfbauth "$HOME/.vnc/passwd")
    else
        echo "==> Chưa có ~/.vnc/passwd -> bật chế độ không mật khẩu."
        echo "    (RealVNC Viewer sẽ từ chối. Đặt mật khẩu bằng: x11vnc -storepasswd)"
        auth=(-nopw)
    fi
    echo "==> Bật VNC trên cổng $VNC_PORT (chỉ localhost)..."
    x11vnc -display "$DISPLAY_NUM" -localhost -rfbport "$VNC_PORT" \
           -forever -shared "${auth[@]}" >/dev/null 2>&1 &
    started_vnc=1
    sleep 1
fi

# --- 4. Hướng dẫn ------------------------------------------------------------
cat <<INSTRUCTIONS

──────────────────────────────────────────────────────────────
 Trên MÁY CÁ NHÂN, mở tunnel ở một cửa sổ khác:

     ssh -L $VNC_PORT:localhost:$VNC_PORT $(whoami)@<ip-server>

 Rồi mở VNC viewer (TigerVNC / TightVNC) nối tới:

     localhost:$VNC_PORT

 KHÔNG dùng trình duyệt web để mở địa chỉ này — nó nói HTTP,
 còn VNC nói RFB, hai bên không hiểu nhau.
──────────────────────────────────────────────────────────────

INSTRUCTIONS

# --- 5. Đăng nhập ------------------------------------------------------------
# "|| status=$?" là bắt buộc: set -e sẽ thoát ngay khi login.py lỗi, khiến dòng
# gán status không bao giờ chạy tới.
status=0
DISPLAY="$DISPLAY_NUM" "$PYTHON" login.py || status=$?

if [ "$status" -eq 0 ]; then
    cat <<'DONE'

==> Xong. Bật lại bot:

    tmux new -s teams
    .venv/bin/python bot.py

    (Ctrl+B rồi D để thoát ra, bot vẫn chạy)
DONE
fi

exit "$status"
