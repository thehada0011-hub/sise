"""부모님용 쉬운 화면(web/simple.html)을 인터넷에 올릴 정적 사이트로 내보낸다.

  py export_static.py --out site

GitHub Actions가 수집 직후 실행해 GitHub Pages에 올린다. 서버 없이 동작하도록
dashboard.py의 /api/simple, /api/item 응답을 JSON 파일로 미리 만들어 둔다.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import struct
import zlib
from pathlib import Path

import dashboard

BASE_DIR = Path(__file__).resolve().parent
FAVORITES_PATH = BASE_DIR / "favorites.json"  # 부모님 휴대폰에 처음 들어갈 즐겨찾기
APP_NAME = "농산물 시세"
GREEN = (0x1F, 0x5E, 0x2C)


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


# ---------------------------------------------------------------- 아이콘 (외부 패키지 없이 PNG 생성)

def icon_png(size: int) -> bytes:
    """초록 바탕에 흰 꺾은선(시세 그래프) 아이콘. 마스크(원형 잘림)에도 안전하게 가운데 70%에 그린다."""
    pts = [(7, 22), (13, 15), (18, 19), (25, 10)]  # 32칸 기준 좌표 (사이트 파비콘과 같은 모양)
    scale = size / 32 * 0.7
    off = size * 0.15
    pts = [(off + x * scale, off + y * scale) for x, y in pts]
    half = size * 0.045  # 선 두께의 절반
    rows = []
    for py in range(size):
        row = bytearray([0])  # PNG 필터 바이트
        for px in range(size):
            cx, cy = px + 0.5, py + 0.5
            dist = min(_seg_dist(cx, cy, *a, *b) for a, b in zip(pts, pts[1:]))
            t = max(0.0, min(1.0, half + 0.5 - dist))  # 가장자리 부드럽게
            row += bytes(round(g + (255 - g) * t) for g in GREEN)
        rows.append(bytes(row))
    raw = zlib.compress(b"".join(rows), 9)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8비트 RGB
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")


def _seg_dist(px, py, ax, ay, bx, by) -> float:
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


# ---------------------------------------------------------------- 사이트

MANIFEST = {
    "name": APP_NAME,
    "short_name": APP_NAME,
    "lang": "ko",
    "start_url": "./",
    "scope": "./",
    "display": "standalone",
    "background_color": "#f4f1ea",
    "theme_color": "#1f5e2c",
    "icons": [
        {"src": "icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
        {"src": "icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
    ],
}

# 화면은 캐시해 두고, 시세(JSON)는 항상 새로 받되 인터넷이 끊기면 마지막 시세를 보여준다
SERVICE_WORKER = """
const CACHE = "sise-v1";
self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(["./", "manifest.webmanifest", "icons/icon-192.png"])));
  self.skipWaiting();
});
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (e) => {
  if (e.request.method !== "GET") return;
  e.respondWith(
    fetch(e.request)
      .then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy));
        return res;
      })
      .catch(() => caches.match(e.request, { ignoreSearch: true }))
  );
});
""".lstrip()

HEAD = """<link rel="manifest" href="manifest.webmanifest">
<link rel="apple-touch-icon" href="icons/icon-192.png">"""


def build(out: Path) -> None:
    if out.exists():
        shutil.rmtree(out)
    (out / "data" / "items").mkdir(parents=True)
    (out / "icons").mkdir()

    simple = dashboard.api_simple({})
    if not simple["date"]:
        raise SystemExit("수집된 데이터가 없어 내보낼 수 없습니다.")
    if FAVORITES_PATH.exists():
        simple["watch"] = json.loads(FAVORITES_PATH.read_text(encoding="utf-8"))

    # 품목 이름에는 괄호·공백이 섞여 있어 파일 이름은 번호로 쓰고 이름→파일 표를 함께 내보낸다
    files = {}
    for i, name in enumerate(dict.fromkeys(n["item"] for n in simple["names"])):
        item = dashboard.api_item({"name": name})
        if item["rows"]:
            files[name] = f"i{i}.json"
            write_json(out / "data" / "items" / files[name], item)
    simple["files"] = files
    write_json(out / "data" / "simple.json", simple)

    html = (BASE_DIR / "web" / "simple.html").read_text(encoding="utf-8")
    for marker, replacement in [
        ("const STATIC_DATA = false;", "const STATIC_DATA = true;"),
        ("<!-- export:head -->", HEAD),
    ]:
        if marker not in html:
            raise SystemExit(f"simple.html에서 '{marker}'를 찾지 못했습니다.")
        html = html.replace(marker, replacement)
    (out / "index.html").write_text(html, encoding="utf-8")
    write_json(out / "manifest.webmanifest", MANIFEST)
    (out / "sw.js").write_text(SERVICE_WORKER, encoding="utf-8")
    for size in (192, 512):
        (out / "icons" / f"icon-{size}.png").write_bytes(icon_png(size))
    (out / ".nojekyll").write_text("", encoding="utf-8")  # GitHub Pages가 파일을 그대로 올리게 한다

    print(f"{simple['date']} 기준, 품목 {len(files)}개 → {out}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="site")
    args = p.parse_args()
    build(BASE_DIR / args.out)


if __name__ == "__main__":
    main()
