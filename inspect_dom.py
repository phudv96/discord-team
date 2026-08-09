"""Chạy 1 lần để đọc cấu trúc DOM thật của Teams web (KHÔNG gửi tin nào).

    python inspect_dom.py

Dùng khi cần sửa selector: Teams v2 không đổi URL khi chuyển chat, nên phải
định danh group chat bằng thứ khác (tên chat / thread id trong DOM). Script này
dump những thông tin đó ra diagnose/dom-report.txt để đối chiếu.

Script chỉ ĐỌC. Không nhập, không gửi, không bấm gì.
"""

from __future__ import annotations

import asyncio
import json
import sys

from playwright.async_api import Error as PlaywrightError

from config import DIAGNOSE_DIR
from teams_client import COMPOSE_SELECTORS, TeamsClient, TeamsProfileLocked

# Chạy trong trang: gom mọi manh mối để định danh chat đang mở.
PROBE_JS = """
() => {
  const out = {};
  out.url = location.href;
  out.documentTitle = document.title;

  // 1. Tất cả giá trị data-tid đang có (Teams dùng attribute này làm "selector chính thức")
  const tids = new Set();
  document.querySelectorAll('[data-tid]').forEach(e => tids.add(e.getAttribute('data-tid')));
  out.allDataTids = [...tids].sort();

  // 2. Ứng viên cho tiêu đề chat đang mở
  const titleSels = [
    '[data-tid="chat-header-title"]', '[data-tid="threadHeaderTitle"]',
    '[data-tid*="header" i][data-tid*="title" i]', '[data-tid*="chat-header" i]',
    'main h1', 'h1', 'h2', '[role="heading"]',
  ];
  out.titleCandidates = [];
  for (const sel of titleSels) {
    document.querySelectorAll(sel).forEach(e => {
      const t = (e.innerText || '').trim().split('\\n')[0];
      if (t) out.titleCandidates.push({
        sel, text: t.slice(0, 100),
        dataTid: e.getAttribute('data-tid'),
        ariaLabel: (e.getAttribute('aria-label') || '').slice(0, 100),
      });
    });
  }

  // 3. Ô soạn tin: selector nào khớp thật
  out.composeMatches = [];
  for (const sel of %COMPOSE%) {
    const n = document.querySelectorAll(sel).length;
    if (n) out.composeMatches.push({ sel, count: n });
  }

  // 4. Thread id (19:...@thread.v2) nằm ở attribute nào
  const idHits = [];
  document.querySelectorAll('*').forEach(e => {
    for (const a of e.attributes) {
      const v = a.value || '';
      if (v.includes('@thread') || v.includes('19:')) {
        idHits.push({ tag: e.tagName.toLowerCase(), attr: a.name, value: v.slice(0, 140) });
      }
    }
  });
  out.threadIdHits = idHits.slice(0, 40);
  out.threadIdHitTotal = idHits.length;

  // 5. Danh sách chat bên trái: item trông như thế nào
  const listSels = [
    '[data-tid="chat-list-item"]', '[data-tid*="chat-list" i]',
    '[role="listitem"]', '[data-tid*="list-item" i]',
  ];
  out.chatListItems = [];
  for (const sel of listSels) {
    const els = document.querySelectorAll(sel);
    if (!els.length) continue;
    out.chatListItems.push({
      sel, count: els.length,
      samples: [...els].slice(0, 5).map(e => ({
        text: (e.innerText || '').trim().split('\\n').slice(0, 2).join(' / ').slice(0, 90),
        dataTid: e.getAttribute('data-tid'),
        id: e.id || null,
        ariaLabel: (e.getAttribute('aria-label') || '').slice(0, 90),
      })),
    });
  }
  return out;
}
"""


def _summarise(data: dict) -> str:
    lines = []
    add = lines.append
    add("=" * 70)
    add("BAO CAO DOM — Microsoft Teams web")
    add("=" * 70)
    add(f"url            : {data.get('url')}")
    add(f"document.title : {data.get('documentTitle')}")

    add("\n--- TIEU DE CHAT DANG MO (title candidates) ---")
    for c in data.get("titleCandidates", [])[:25]:
        add(f"  {c['sel']:45} data-tid={c.get('dataTid')!r}")
        add(f"      text={c['text']!r}")
        if c.get("ariaLabel"):
            add(f"      aria-label={c['ariaLabel']!r}")

    add("\n--- O SOAN TIN (selector nao khop) ---")
    for c in data.get("composeMatches", []):
        add(f"  count={c['count']:<3} {c['sel']}")
    if not data.get("composeMatches"):
        add("  (KHONG selector nao khop — day la van de)")

    add(f"\n--- THREAD ID (tong {data.get('threadIdHitTotal', 0)} hit) ---")
    for h in data.get("threadIdHits", []):
        add(f"  <{h['tag']}> {h['attr']} = {h['value']}")
    if not data.get("threadIdHits"):
        add("  (khong tim thay 19:...@thread trong attribute nao)")

    add("\n--- DANH SACH CHAT ---")
    for g in data.get("chatListItems", []):
        add(f"  {g['sel']}  (count={g['count']})")
        for s in g["samples"]:
            add(f"      text={s['text']!r} data-tid={s.get('dataTid')!r} id={s.get('id')!r}")

    tids = data.get("allDataTids", [])
    add(f"\n--- TAT CA data-tid ({len(tids)}) ---")
    for i in range(0, len(tids), 3):
        add("  " + "  ".join(f"{t:35}" for t in tids[i:i + 3]))
    return "\n".join(lines)


async def main() -> int:
    client = TeamsClient(headless=False)
    print("Dang mo Chromium (profile da dang nhap)...")
    try:
        await client.start()
    except TeamsProfileLocked as exc:
        print(f"\nLOI: {exc}")
        return 1

    print(
        "\n"
        "──────────────────────────────────────────────────────────────\n"
        " Mo dung GROUP CHAT ban muon bot gui vao, roi bam Enter.\n"
        " (Script chi DOC DOM, khong gui gi ca.)\n"
        "──────────────────────────────────────────────────────────────\n"
    )
    await asyncio.to_thread(input, "Xong thi bam Enter... ")

    js = PROBE_JS.replace("%COMPOSE%", json.dumps(COMPOSE_SELECTORS))
    try:
        data = await client.page.evaluate(js)
    except PlaywrightError as exc:
        print(f"LOI khi doc DOM: {exc}")
        await client.close()
        return 1

    DIAGNOSE_DIR.mkdir(parents=True, exist_ok=True)
    report = DIAGNOSE_DIR / "dom-report.txt"
    report.write_text(_summarise(data), encoding="utf-8")
    (DIAGNOSE_DIR / "dom-report.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    await client.diagnose("dom-inspect")

    print(f"\n✔ Da luu bao cao:\n  {report}")
    print(f"  {DIAGNOSE_DIR / 'dom-report.json'}")
    await client.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
