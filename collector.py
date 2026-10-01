"""전국 공영도매시장(공판장 포함) 정산 경락가격 수집기.

데이터 출처: 공공데이터포털 - 한국농수산식품유통공사_전국 공영도매시장 정산정보
  https://www.data.go.kr/data/15141809/openapi.do

사용법:
  py collector.py                 # 최근 3일치 수집(기본값, 늦게 올라온 정산분까지 다시 받음)
  py collector.py --days 7        # 최근 7일치
  py collector.py --date 2026-09-29
  py collector.py --from 2026-09-01 --to 2026-09-29   # 기간 백필
  py collector.py --refresh-markets                   # 도매시장 코드 목록 다시 찾기
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
EXCEL_DIR = BASE_DIR / "엑셀"
LOG_DIR = BASE_DIR / "logs"
DB_PATH = DATA_DIR / "prices.db"
MARKETS_PATH = DATA_DIR / "markets.json"
ENV_PATH = BASE_DIR / ".env"

SETTLEMENT_URL = "https://apis.data.go.kr/B552845/katSale/trades"
REALTIME_URL = "https://apis.data.go.kr/B552845/katRealTime2/trades2"

PAGE_SIZE = 1000
REQUEST_TIMEOUT = 30
RETRIES = 3

# API 응답 필드 -> 엑셀/대시보드에 쓰는 한글 이름 (응답 순서 그대로)
FIELDS = {
    "trd_clcln_ymd": "정산일자",
    "whsl_mrkt_cd": "도매시장코드",
    "whsl_mrkt_nm": "도매시장",
    "corp_cd": "법인코드",
    "corp_nm": "법인(공판장)",
    "gds_lclsf_cd": "대분류코드",
    "gds_lclsf_nm": "대분류",
    "gds_mclsf_cd": "품목코드",
    "gds_mclsf_nm": "품목",
    "gds_sclsf_cd": "품종코드",
    "gds_sclsf_nm": "품종",
    "trd_se": "매매구분",
    "unit_qty": "단위물량",
    "unit_cd": "단위코드",
    "unit_nm": "단위",
    "pkg_cd": "포장코드",
    "pkg_nm": "포장",
    "sz_cd": "크기코드",
    "sz_nm": "크기",
    "grd_cd": "등급코드",
    "grd_nm": "등급",
    "plor_cd": "산지코드",
    "plor_nm": "산지",
    "unit_tot_qty": "총물량",
    "totprc": "총금액",
    "lwprc": "최저가",
    "hgprc": "최고가",
    "avgprc": "평균가",
}
NUMERIC_FIELDS = {"unit_qty", "unit_tot_qty", "totprc", "lwprc", "hgprc", "avgprc"}

log = logging.getLogger("collector")


class ApiError(Exception):
    pass


# ---------------------------------------------------------------- 설정

def load_api_key() -> str:
    # GitHub Actions에서는 저장소 비밀 설정(Secrets)이 환경변수로 들어온다
    key = os.environ.get("API_KEY", "").strip()
    if not key and ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not key or key == "여기에_인증키_붙여넣기":
        sys.exit(
            "API 키가 없습니다. .env 파일의 API_KEY= 뒤에 공공데이터포털 인증키를 붙여넣으세요.\n"
            f"  파일 위치: {ENV_PATH}"
        )
    # 포털의 'Encoding' 키를 붙여넣어도 동작하도록 한 번 풀어준다 (이중 인코딩 방지)
    if "%" in key:
        key = urllib.parse.unquote(key)
    return key


def setup_logging() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    file_handler = logging.FileHandler(LOG_DIR / "collect.log", encoding="utf-8")
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    log.setLevel(logging.INFO)
    log.addHandler(file_handler)
    log.addHandler(console)


# ---------------------------------------------------------------- API 호출

def call_api(url: str, key: str, params: dict) -> dict:
    """한 페이지를 요청해 body(dict)를 돌려준다. 일시 오류는 재시도."""
    query = urllib.parse.urlencode({"serviceKey": key, "returnType": "json", **params})
    full_url = f"{url}?{query}"
    last_err: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            with urllib.request.urlopen(full_url, timeout=REQUEST_TIMEOUT) as resp:
                raw = resp.read().decode("utf-8")
            break
        except urllib.error.HTTPError as e:
            # 4xx(잘못된 요청·인증 오류)는 다시 보내도 같으므로 바로 알린다
            if e.code < 500:
                detail = e.read().decode("utf-8", "replace")[:300]
                raise ApiError(f"HTTP {e.code}: {detail}")
            last_err = e
            log.warning("요청 실패(%d/%d): %s", attempt, RETRIES, e)
            time.sleep(2 * attempt)
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            log.warning("요청 실패(%d/%d): %s", attempt, RETRIES, e)
            time.sleep(2 * attempt)
    else:
        raise ApiError(f"네트워크 오류: {last_err}")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # 인증키 오류 등은 XML로 떨어지는 경우가 있다
        raise ApiError(f"JSON이 아닌 응답: {raw[:300]}")

    response = data.get("response", data)
    header = response.get("header", {})
    code = str(header.get("resultCode", ""))
    if code not in ("00", "0", "000", "INFO-000", ""):
        raise ApiError(f"API 오류 {code}: {header.get('resultMsg')}")
    return response.get("body", {}) or {}


def body_items(body: dict) -> list[dict]:
    items = body.get("items") or {}
    if isinstance(items, list):
        return items
    item = items.get("item") if isinstance(items, dict) else None
    if item is None:
        return []
    return item if isinstance(item, list) else [item]


def fetch_all(url: str, key: str, cond: dict, selectable: str | None = None) -> list[dict]:
    """조건에 맞는 모든 페이지를 받아온다."""
    rows: list[dict] = []
    page = 1
    while True:
        params = {"pageNo": page, "numOfRows": PAGE_SIZE, **cond}
        if selectable:
            params["selectable"] = selectable
        body = call_api(url, key, params)
        items = body_items(body)
        rows.extend(items)
        total = int(body.get("totalCount") or 0)
        if not items or len(rows) >= total:
            return rows
        page += 1


# ---------------------------------------------------------------- 도매시장 목록

def load_markets() -> dict[str, str]:
    if MARKETS_PATH.exists():
        return json.loads(MARKETS_PATH.read_text(encoding="utf-8"))
    return {}


def save_markets(markets: dict[str, str]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    MARKETS_PATH.write_text(
        json.dumps(dict(sorted(markets.items())), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def discover_markets(key: str, trading_days: int = 2, max_back: int = 7) -> dict[str, str]:
    """실시간 경매 API(시장코드 선택값)에서 거래가 있었던 도매시장 코드를 모은다.

    하루 경매가 수만 건이라(한 번에 1,000건씩) 거래가 있던 최근 며칠만 훑는다.
    """
    found = load_markets()
    scanned = 0
    for offset in range(1, max_back + 1):
        if scanned >= trading_days:
            break
        d = (date.today() - timedelta(days=offset)).isoformat()
        try:
            rows = fetch_all(
                REALTIME_URL,
                key,
                {"cond[trd_clcln_ymd::EQ]": d, "cond[scsbd_dt::LIKE]": d},
                selectable="whsl_mrkt_cd,whsl_mrkt_nm",
            )
        except ApiError as e:
            log.warning("%s 시장 목록 조회 실패: %s", d, e)
            continue
        if rows:
            scanned += 1
        before = len(found)
        for r in rows:
            if r.get("whsl_mrkt_cd"):
                found[str(r["whsl_mrkt_cd"])] = r.get("whsl_mrkt_nm") or ""
        log.info("%s 기준 도매시장 %d곳 확인 (새로 %d곳)", d, len(found), len(found) - before)
    if found:
        save_markets(found)
    return found


# ---------------------------------------------------------------- 저장

def open_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cols = ", ".join(
        f"{f} {'REAL' if f in NUMERIC_FIELDS else 'TEXT'}" for f in FIELDS
    )
    conn.execute(f"CREATE TABLE IF NOT EXISTS trades ({cols})")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_date ON trades(trd_clcln_ymd, whsl_mrkt_cd)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_item ON trades(gds_mclsf_nm, gds_sclsf_nm)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS collect_log ("
        "trd_date TEXT PRIMARY KEY, rows INTEGER, markets INTEGER, collected_at TEXT)"
    )
    return conn


def to_number(v):
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def normalize(row: dict) -> tuple:
    return tuple(
        to_number(row.get(f)) if f in NUMERIC_FIELDS else (row.get(f) or None)
        for f in FIELDS
    )


def replace_rows(conn: sqlite3.Connection, trd_date: str, rows: list[dict], market: str | None) -> None:
    """같은 날짜(와 시장)의 기존 데이터를 지우고 새로 넣는다. 여러 번 돌려도 중복되지 않는다."""
    placeholders = ", ".join("?" for _ in FIELDS)
    with conn:
        if market:
            conn.execute(
                "DELETE FROM trades WHERE trd_clcln_ymd=? AND whsl_mrkt_cd=?", (trd_date, market)
            )
        else:
            conn.execute("DELETE FROM trades WHERE trd_clcln_ymd=?", (trd_date,))
        conn.executemany(
            f"INSERT INTO trades ({', '.join(FIELDS)}) VALUES ({placeholders})",
            [normalize(r) for r in rows],
        )


# ---------------------------------------------------------------- 수집

def collect_date(conn: sqlite3.Connection, key: str, trd_date: str, markets: dict[str, str]) -> int:
    """하루치 정산 데이터를 시장별로 받아 저장하고 저장한 행 수를 돌려준다."""
    total = 0
    ok_markets = 0
    for code, name in markets.items():
        try:
            rows = fetch_all(
                SETTLEMENT_URL,
                key,
                {"cond[trd_clcln_ymd::EQ]": trd_date, "cond[whsl_mrkt_cd::EQ]": code},
            )
        except ApiError as e:
            log.error("%s %s(%s) 실패: %s", trd_date, name, code, e)
            continue
        replace_rows(conn, trd_date, rows, market=code)
        ok_markets += 1
        total += len(rows)
        if rows:
            log.info("%s %s: %d건", trd_date, name or code, len(rows))
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO collect_log VALUES (?, ?, ?, ?)",
            (trd_date, total, ok_markets, datetime.now().isoformat(timespec="seconds")),
        )
    return total


# ---------------------------------------------------------------- 엑셀

def export_excel(conn: sqlite3.Connection, trd_date: str) -> Path | None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    rows = conn.execute(
        f"SELECT {', '.join(FIELDS)} FROM trades WHERE trd_clcln_ymd=? "
        "ORDER BY whsl_mrkt_nm, corp_nm, gds_lclsf_nm, gds_mclsf_nm, gds_sclsf_nm, grd_nm",
        (trd_date,),
    ).fetchall()
    if not rows:
        return None

    summary = conn.execute(
        """
        SELECT gds_lclsf_nm, gds_mclsf_nm, gds_sclsf_nm,
               COALESCE(printf('%g', unit_qty), '') || COALESCE(unit_nm, '') AS unit,
               COUNT(DISTINCT whsl_mrkt_cd), SUM(totprc),
               MIN(CASE WHEN lwprc > 0 THEN lwprc END), MAX(hgprc),
               SUM(totprc) / NULLIF(SUM(totprc / avgprc), 0)
        FROM trades
        -- 금액·평균가 0 이하는 정정·취소분이라 요약에서 뺀다 (전체 경락가 시트에는 그대로 둔다)
        WHERE trd_clcln_ymd=? AND totprc > 0 AND avgprc > 0
        GROUP BY 1, 2, 3, 4
        ORDER BY SUM(totprc) DESC
        """,
        (trd_date,),
    ).fetchall()

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="2F6B3A")
    wb = Workbook()

    ws = wb.active
    ws.title = "품목별 요약"
    ws.append(["대분류", "품목", "품종", "단위", "거래시장 수", "총거래금액(원)",
               "최저가(원)", "최고가(원)", "가중평균가(원)"])
    for r in summary:
        ws.append([r[0], r[1], r[2], r[3], r[4],
                   round(r[5] or 0), r[6], r[7], round(r[8]) if r[8] else None])

    detail = wb.create_sheet("전체 경락가")
    detail.append(list(FIELDS.values()))
    for r in rows:
        detail.append(list(r))

    for sheet in (ws, detail):
        for cell in sheet[1]:
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center")
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for i, col in enumerate(sheet.iter_cols(min_row=1, max_row=min(sheet.max_row, 200)), 1):
            width = max(len(str(c.value or "")) for c in col)
            sheet.column_dimensions[get_column_letter(i)].width = min(max(width * 1.6, 8), 30)
    for col in "FGHI":
        for cell in ws[col][1:]:
            cell.number_format = "#,##0"
    for cell_row in detail.iter_rows(min_row=2, min_col=24, max_col=28):
        for cell in cell_row:
            cell.number_format = "#,##0"

    EXCEL_DIR.mkdir(exist_ok=True)
    path = EXCEL_DIR / f"경락가_{trd_date}.xlsx"
    try:
        wb.save(path)
    except PermissionError:
        log.error("엑셀 파일이 열려 있어 저장하지 못했습니다: %s (파일을 닫고 다시 실행하세요)", path)
        return None
    return path


# ---------------------------------------------------------------- 실행

def date_range(args) -> list[str]:
    if args.date:
        return [args.date]
    if args.start:
        start = date.fromisoformat(args.start)
        end = date.fromisoformat(args.end) if args.end else date.today()
        return [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
    return [(date.today() - timedelta(days=i)).isoformat() for i in range(args.days - 1, -1, -1)]


def main() -> None:
    p = argparse.ArgumentParser(description="전국 공영도매시장 정산 경락가격 수집")
    p.add_argument("--date", help="하루만 수집 (YYYY-MM-DD)")
    p.add_argument("--from", dest="start", help="기간 시작일 (YYYY-MM-DD)")
    p.add_argument("--to", dest="end", help="기간 종료일 (YYYY-MM-DD, 기본: 오늘)")
    p.add_argument("--days", type=int, default=3, help="오늘 포함 최근 N일 (기본 3)")
    p.add_argument("--refresh-markets", action="store_true", help="도매시장 목록 다시 찾기")
    p.add_argument("--no-excel", action="store_true", help="엑셀 저장 생략")
    p.add_argument("--keep-days", type=int, help="이 일수보다 오래된 데이터 삭제 (클라우드 DB 크기 유지용)")
    args = p.parse_args()

    setup_logging()
    key = load_api_key()

    markets = load_markets()
    if args.refresh_markets or not markets:
        log.info("도매시장 목록을 찾는 중...")
        markets = discover_markets(key)
    if not markets:
        sys.exit("도매시장 목록을 찾지 못했습니다. logs/collect.log 를 확인하세요.")
    log.info("대상 도매시장 %d곳", len(markets))

    conn = open_db()
    for d in date_range(args):
        n = collect_date(conn, key, d, markets)
        log.info("== %s 저장 %d건", d, n)
        if n and not args.no_excel:
            path = export_excel(conn, d)
            if path:
                log.info("엑셀 저장: %s", path.relative_to(BASE_DIR))
    if args.keep_days:
        prune(conn, args.keep_days)
    conn.close()


def prune(conn: sqlite3.Connection, keep_days: int) -> None:
    cutoff = (date.today() - timedelta(days=keep_days)).isoformat()
    with conn:
        n = conn.execute("DELETE FROM trades WHERE trd_clcln_ymd < ?", (cutoff,)).rowcount
        conn.execute("DELETE FROM collect_log WHERE trd_date < ?", (cutoff,))
    if n:
        conn.execute("VACUUM")
        log.info("%s 이전 데이터 %d건 삭제", cutoff, n)


if __name__ == "__main__":
    main()
