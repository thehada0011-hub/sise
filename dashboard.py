"""수집한 경락가격을 보여주는 로컬 대시보드.

  py dashboard.py            # http://127.0.0.1:8517 에서 열림
  py dashboard.py --lan      # 같은 와이파이의 휴대폰에서도 접속 가능

외부 패키지 없이 파이썬 기본 기능만 사용한다.
가격 데이터(prices.db)는 읽기만 하고, 관심 품목 목록(watchlist.json)만 저장한다.
"""

from __future__ import annotations

import argparse
import json
import socket
import sqlite3
import webbrowser
from datetime import date
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "prices.db"
WEB_DIR = BASE_DIR / "web"
WATCH_PATH = BASE_DIR / "data" / "watchlist.json"

# 가중평균가: 총금액 / (총금액/평균가 = 거래 건수) 로 시장·등급이 섞여도 거래량 비중대로 평균낸다
WEIGHTED_AVG = "SUM(totprc) / NULLIF(SUM(totprc / avgprc), 0)"
# 최저가가 0 이하로 찍힌 행은 실제 낙찰가가 아니다
LOW = "MIN(CASE WHEN lwprc > 0 THEN lwprc END)"
UNIT = "COALESCE(printf('%g', unit_qty), '') || COALESCE(unit_nm, '')"
# 일요일·연휴에 몇 건만 정산되는 날은 비교 기준에서 뺀다 (평일은 하루 수만 건)
TRADING_DAY_ROWS = 1000
TRADING_DAYS = f"SELECT trd_date FROM collect_log WHERE rows >= {TRADING_DAY_ROWS}"
# 부모님 화면에서 상세히 보여줄 지역: 도매시장 코드 앞 두 자리가 지역 (24 = 광주: 광주각화·광주서부)
LOCAL_REGION = {"code": "24", "name": "광주"}


def db() -> sqlite3.Connection:
    # 읽기 전용으로 열어 수집기와 동시에 돌아도 데이터를 건드리지 않는다
    conn = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def rows(sql: str, params: tuple = ()) -> list[dict]:
    with db() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def market_filter(q: dict) -> tuple[str, list]:
    # 금액·평균가가 0 이하인 행은 정정·취소분이라 가격 계산에서 뺀다
    where, params = ["totprc > 0", "avgprc > 0"], []
    if q.get("market"):
        where.append("whsl_mrkt_cd = ?")
        params.append(q["market"])
    if q.get("category"):
        where.append("gds_lclsf_nm = ?")
        params.append(q["category"])
    if q.get("item_name"):
        where.append("gds_mclsf_nm = ?")
        params.append(q["item_name"])
    if q.get("region"):
        where.append("whsl_mrkt_cd LIKE ? || '%'")
        params.append(q["region"])
    # 공판장(농협·원협 등)은 법인명 끝에 '(공)'이 붙어 온다
    if q.get("corp_type") == "gong":
        where.append("corp_nm LIKE '%(공)%'")
    elif q.get("corp_type") == "corp":
        where.append("corp_nm NOT LIKE '%(공)%'")
    return "".join(f" AND {w}" for w in where), params


def api_meta(q: dict) -> dict:
    return {
        "dates": [r["d"] for r in rows(
            f"SELECT trd_date AS d FROM ({TRADING_DAYS}) ORDER BY d DESC LIMIT 400")],
        "markets": rows(
            "SELECT DISTINCT whsl_mrkt_cd AS code, whsl_mrkt_nm AS name FROM trades "
            "WHERE whsl_mrkt_cd IS NOT NULL ORDER BY name"),
        "categories": [r["c"] for r in rows(
            "SELECT DISTINCT gds_lclsf_nm AS c FROM trades "
            "WHERE gds_lclsf_nm IS NOT NULL ORDER BY c")],
        "log": rows("SELECT * FROM collect_log ORDER BY trd_date DESC LIMIT 1"),
    }


def api_summary(q: dict) -> list[dict]:
    """선택한 날짜의 품목·품종·단위별 요약 + 전 거래일 대비 변동."""
    extra, params = market_filter(q)
    date = q["date"]
    prev = rows(
        f"SELECT MAX(trd_date) AS d FROM ({TRADING_DAYS}) WHERE trd_date < ?", (date,))[0]["d"]
    sql = f"""
        WITH cur AS (
          SELECT gds_lclsf_nm AS category, gds_mclsf_nm AS item, gds_sclsf_nm AS variety,
                 {UNIT} AS unit, COUNT(DISTINCT whsl_mrkt_cd) AS markets,
                 SUM(totprc) AS amount, {LOW} AS low, MAX(hgprc) AS high,
                 {WEIGHTED_AVG} AS avg
          FROM trades WHERE trd_clcln_ymd = ? {extra}
          GROUP BY 1, 2, 3, 4
        ), prv AS (
          SELECT gds_mclsf_nm AS item, gds_sclsf_nm AS variety, {UNIT} AS unit,
                 {WEIGHTED_AVG} AS avg
          FROM trades WHERE trd_clcln_ymd = ? {extra}
          GROUP BY 1, 2, 3
        )
        SELECT cur.*, prv.avg AS prev_avg
        FROM cur LEFT JOIN prv
          ON prv.item IS cur.item AND prv.variety IS cur.variety AND prv.unit IS cur.unit
        ORDER BY cur.amount DESC
    """
    return rows(sql, (date, *params, prev or "", *params))


def api_trend(q: dict) -> list[dict]:
    """한 품목의 일별 가격. 기준일(to)부터 days일 전까지."""
    extra, params = market_filter(q)
    days = int(q.get("days", 30))
    to = q.get("to") or date.today().isoformat()
    sql = f"""
        SELECT trd_clcln_ymd AS date, {WEIGHTED_AVG} AS avg,
               {LOW} AS low, MAX(hgprc) AS high, SUM(totprc) AS amount
        FROM trades
        WHERE gds_mclsf_nm IS ? AND gds_sclsf_nm IS ? AND {UNIT} = ? {extra}
          AND trd_clcln_ymd BETWEEN date(?, ?) AND ?
          AND trd_clcln_ymd IN ({TRADING_DAYS})
        GROUP BY 1 ORDER BY 1
    """
    return rows(sql, (q.get("item"), q.get("variety"), q.get("unit", ""), *params,
                      to, f"-{days} day", to))


# ---------------------------------------------------------------- 관심 품목

WATCH_KEYS = ("item", "variety", "unit", "market", "corp_type")


def load_watch() -> list[dict]:
    if WATCH_PATH.exists():
        return json.loads(WATCH_PATH.read_text(encoding="utf-8"))
    return []


def clean_entry(raw: dict) -> dict:
    entry = {k: str(raw.get(k) or "") for k in WATCH_KEYS}
    if not entry["item"]:
        raise ValueError("품목이 비어 있습니다.")
    return entry


def market_name(code: str) -> str:
    if not code:
        return ""
    r = rows("SELECT whsl_mrkt_nm AS n FROM trades WHERE whsl_mrkt_cd = ? LIMIT 1", (code,))
    return r[0]["n"] if r else code


def api_watch(q: dict) -> list[dict]:
    """관심 품목마다 기준일까지 최근 30일 가격을 붙여 돌려준다."""
    out = []
    for entry in load_watch():
        cond = {**entry, "variety": entry["variety"] or None, "to": q.get("to"), "days": 30}
        out.append({**entry, "market_name": market_name(entry["market"]), "points": api_trend(cond)})
    return out


def update_watch(body: dict) -> list[dict]:
    """관심 품목 추가(action=add) / 삭제(action=remove)."""
    entry = clean_entry(body.get("entry") or {})
    entries = [e for e in load_watch() if clean_entry(e) != entry]
    if body.get("action") == "add":
        entries.append(entry)
    WATCH_PATH.parent.mkdir(exist_ok=True)
    WATCH_PATH.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    return entries


def api_markets(q: dict) -> list[dict]:
    """한 품목의 시장·법인별 가격 비교 (선택 날짜). 시장 선택은 무시하고 전국을 보여준다."""
    extra, params = market_filter({**q, "market": ""})
    sql = f"""
        SELECT whsl_mrkt_nm AS market, corp_nm AS corp, {WEIGHTED_AVG} AS avg,
               {LOW} AS low, MAX(hgprc) AS high, SUM(totprc) AS amount
        FROM trades
        WHERE trd_clcln_ymd = ? AND gds_mclsf_nm IS ? AND gds_sclsf_nm IS ? AND {UNIT} = ? {extra}
        GROUP BY 1, 2 ORDER BY avg DESC
    """
    return rows(sql, (q["date"], q.get("item"), q.get("variety"), q.get("unit", ""), *params))


# 부모님용 쉬운 화면(simple.html) 데이터.
# 나중에 정적 파일(JSON)로 내보내 인터넷에 올릴 때도 이 구조를 그대로 쓴다.

def api_simple(q: dict) -> dict:
    """첫 화면용: 최근 경매일, 검색·음성용 품목 이름 목록, 처음 즐겨찾기로 쓸 관심 품목."""
    latest = rows(f"SELECT MAX(trd_date) AS d FROM ({TRADING_DAYS})")[0]["d"]
    if not latest:
        return {"date": None, "names": [], "watch": []}
    # 최근 30일 안에 거래된 품목·품종 이름 (거래가 많은 순)
    names = rows(
        f"""
        SELECT gds_mclsf_nm AS item, gds_sclsf_nm AS variety, SUM(totprc) AS amount
        FROM trades
        WHERE trd_clcln_ymd BETWEEN date(?, '-30 day') AND ? AND totprc > 0 AND avgprc > 0
          AND gds_mclsf_nm IS NOT NULL
        GROUP BY 1, 2 ORDER BY amount DESC
        """,
        (latest, latest),
    )
    watch = [{k: e[k] for k in ("item", "variety", "unit")} for e in load_watch()]
    return {"date": latest, "names": names, "watch": watch}


def api_item(q: dict) -> dict:
    """품목 화면용: 한 품목의 품종·단위별 최근 가격, 두 달 추이, 시장별 시세."""
    name = q.get("name") or ""
    latest = rows(
        f"""SELECT MAX(trd_clcln_ymd) AS d FROM trades
            WHERE gds_mclsf_nm = ? AND totprc > 0 AND avgprc > 0
              AND trd_clcln_ymd IN ({TRADING_DAYS})""",
        (name,),
    )[0]["d"]
    if not latest:
        return {"name": name, "date": None, "rows": []}
    out = []
    for r in api_summary({"date": latest, "item_name": name})[:15]:
        if not r["avg"]:
            continue
        key = {"item": name, "variety": r["variety"], "unit": r["unit"]}
        trend = api_trend({**key, "to": latest, "days": 60})
        markets = api_markets({**key, "date": latest})
        out.append({
            **{k: r[k] for k in ("variety", "unit", "avg", "prev_avg", "low", "high", "amount")},
            "trend": [{"date": p["date"], "avg": p["avg"]} for p in trend],
            "markets": [{k: m[k] for k in ("market", "corp", "avg", "low", "high")} for m in markets],
            "local": local_detail(key, latest),
        })
    return {"name": name, "date": latest, "rows": out, "region": LOCAL_REGION["name"]}


def local_detail(key: dict, latest: str) -> dict | None:
    """한 품목(품종·단위)의 지역 상세: 지역 평균, 두 달 추이, 법인·공판장별 / 등급별 가격.

    그날 지역에 거래가 없으면 최근 30일 안의 마지막 지역 경매일 기준으로 보여준다.
    """
    extra, params = market_filter({"region": LOCAL_REGION["code"]})
    cond = f"gds_mclsf_nm IS ? AND gds_sclsf_nm IS ? AND {UNIT} = ? {extra}"
    base = (key["item"], key["variety"], key["unit"], *params)
    days = rows(
        f"""SELECT DISTINCT trd_clcln_ymd AS d FROM trades
            WHERE {cond} AND trd_clcln_ymd BETWEEN date(?, '-30 day') AND ?
              AND trd_clcln_ymd IN ({TRADING_DAYS})
            ORDER BY d DESC LIMIT 2""",
        (*base, latest, latest),
    )
    if not days:
        return None
    day = days[0]["d"]
    boxes = "SUM(totprc / avgprc)"  # 거래 상자(단위) 수 = 총금액 / 평균가
    summary_sql = f"""SELECT {WEIGHTED_AVG} AS avg, {LOW} AS low, MAX(hgprc) AS high, {boxes} AS boxes
                      FROM trades WHERE trd_clcln_ymd = ? AND {cond}"""
    total = rows(summary_sql, (day, *base))[0]
    prev = rows(summary_sql, (days[1]["d"], *base))[0]["avg"] if len(days) > 1 else None
    corps = rows(
        f"""SELECT whsl_mrkt_nm AS market, corp_nm AS corp, {WEIGHTED_AVG} AS avg,
                   {LOW} AS low, MAX(hgprc) AS high, {boxes} AS boxes
            FROM trades WHERE trd_clcln_ymd = ? AND {cond}
            GROUP BY 1, 2 ORDER BY avg DESC""",
        (day, *base),
    )
    grades = rows(
        f"""SELECT whsl_mrkt_nm AS market, corp_nm AS corp, COALESCE(grd_nm, '-') AS grade,
                   {WEIGHTED_AVG} AS avg, {LOW} AS low, MAX(hgprc) AS high, {boxes} AS boxes
            FROM trades WHERE trd_clcln_ymd = ? AND {cond}
            GROUP BY 1, 2, 3 ORDER BY avg DESC""",
        (day, *base),
    )
    for c in corps:
        c["grades"] = [{k: g[k] for k in ("grade", "avg", "low", "high", "boxes")}
                       for g in grades if g["market"] == c["market"] and g["corp"] == c["corp"]]
    trend = api_trend({**key, "region": LOCAL_REGION["code"], "to": latest, "days": 60})
    return {
        "region": LOCAL_REGION["name"], "date": day, "prev_avg": prev, **total,
        "trend": [{"date": p["date"], "avg": p["avg"]} for p in trend],
        "corps": corps,
    }


ROUTES = {
    "/api/meta": api_meta,
    "/api/summary": api_summary,
    "/api/trend": api_trend,
    "/api/markets": api_markets,
    "/api/watch": api_watch,
    "/api/simple": api_simple,
    "/api/item": api_item,
}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(WEB_DIR), **kw)

    def do_GET(self):
        url = urlparse(self.path)
        route = ROUTES.get(url.path)
        if not route:
            return super().do_GET()
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if not DB_PATH.exists():
                raise FileNotFoundError("아직 수집된 데이터가 없습니다. 먼저 수집을 실행하세요.")
            status, payload = 200, route(q)
        except Exception as e:  # 화면에 원인을 그대로 보여준다
            status, payload = 500, {"error": str(e)}
        self.send_json(status, payload)

    def do_POST(self):
        # 쓰기는 관심 품목 저장 하나뿐이다
        if urlparse(self.path).path != "/api/watch":
            return self.send_json(404, {"error": "없는 주소입니다."})
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 10_000)
            status, payload = 200, update_watch(json.loads(self.rfile.read(length) or b"{}"))
        except Exception as e:
            status, payload = 400, {"error": str(e)}
        self.send_json(status, payload)

    def send_json(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class Server(ThreadingHTTPServer):
    # Windows에서는 재사용 옵션이 켜져 있으면 같은 포트에 서버가 중복으로 떠서
    # 예전 서버가 요청을 가로챈다. 꺼 두면 두 번째 실행은 바로 실패한다.
    allow_reuse_address = False


def lan_ip() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8517)
    p.add_argument("--lan", action="store_true", help="같은 와이파이 기기에서도 접속 허용")
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args()

    host = "0.0.0.0" if args.lan else "127.0.0.1"
    # localhost는 IPv6(::1)로 풀려 같은 포트를 쓰는 다른 프로그램에 붙을 수 있어 IPv4 주소로 연다
    local = f"http://127.0.0.1:{args.port}"
    try:
        server = Server((host, args.port), Handler)
    except OSError:
        print(f"이미 대시보드가 켜져 있습니다: {local}")
        if not args.no_browser:
            webbrowser.open(local)
        return
    print(f"대시보드: {local}")
    if args.lan:
        print(f"휴대폰(같은 와이파이): http://{lan_ip()}:{args.port}")
    print("끄려면 이 창을 닫거나 Ctrl+C")
    if not args.no_browser:
        webbrowser.open(local)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
