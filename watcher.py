#!/usr/bin/env python3
"""Watch CGV Yongsan IMAX showtimes for The Odyssey and notify Discord."""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from curl_cffi import requests


KST = timezone(timedelta(hours=9))
WEEKDAYS_KO = ("월", "화", "수", "목", "금", "토", "일")
CGV_TIMETABLE_PROXY_URL = "https://mcp.aka.page/api/cgv/timetable"
CGV_BOOKING_URL = "https://cgv.co.kr/cnm/movieBook/movie?movNo={movie_no}&siteNo={site_no}"

SITE_NO = os.environ.get("CGV_SITE_NO", "0013")
SITE_NAME = os.environ.get("CGV_SITE_NAME", "CGV 용산아이파크몰")
MOVIE_NO = os.environ.get("CGV_MOV_NO", "30001323")
MOVIE_NAME = os.environ.get("CGV_MOV_NAME", "오디세이")
FORMAT_KEYWORD = os.environ.get("CGV_FORMAT_KEYWORD", "IMAX")
LOOKAHEAD_DAYS = int(os.environ.get("CGV_LOOKAHEAD_DAYS", "14"))
IMAX_MIN_SEATS = int(os.environ.get("CGV_IMAX_MIN_SEATS", "500"))
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
DISCORD_USER_ID = os.environ.get("DISCORD_USER_ID", "").strip()
STATE_FILE = Path(os.environ.get("CGV_STATE_FILE", Path(__file__).with_name("state.json")))

FAIL_ALERT_AFTER = 10
MAX_RETRY_WAIT_SECONDS = 120
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
    "Origin": "https://cgv.co.kr",
    "Referer": "https://cgv.co.kr/cnm/movieBook/movie",
}


def log(message: str) -> None:
    print(f"[{datetime.now(KST):%Y-%m-%d %H:%M:%S}] {message}", flush=True)


def decode_json_response(response: Any) -> dict[str, Any]:
    """CGV sometimes omits a charset, so decode its JSON bytes explicitly."""
    return json.loads(response.content.decode("utf-8-sig"))


def fetch_open_dates(_session: Any) -> list[str]:
    """Return the dates to inspect without making an extra CGV request."""
    today = datetime.now(KST).date()
    return [
        (today + timedelta(days=offset)).strftime("%Y%m%d")
        for offset in range(LOOKAHEAD_DAYS)
    ]


def fetch_showtimes(session: Any, ymd: str) -> list[dict[str, Any]]:
    """Fetch normalized CGV rows through a public cache/proxy.

    CGV blocks GitHub-hosted runner IPs with HTTP 403.  The public endpoint is
    rate-limited, so the workflow checks a 14-day window every 7.5 minutes.
    """
    response = session.get(
        CGV_TIMETABLE_PROXY_URL,
        params={
            "playDate": ymd,
            "theaterCode": SITE_NO,
            "movieCode": MOVIE_NO,
            "limit": "50",
        },
        headers={"Accept": "application/json", "User-Agent": HEADERS["User-Agent"]},
        timeout=30,
    )
    if response.status_code != 200:
        raise RuntimeError(f"CGV timetable proxy failed: HTTP {response.status_code}")

    body = decode_json_response(response)
    if body.get("success") is not True:
        raise RuntimeError(f"CGV timetable proxy error: {body.get('error') or body}")
    rows = ((body.get("data") or {}).get("timetable") or [])
    if not isinstance(rows, list):
        raise RuntimeError("CGV timetable proxy returned an invalid timetable")

    normalized: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("movieCode") or "") != MOVIE_NO:
            continue
        total = row.get("totalSeats")
        normalized.append(
            {
                "scnYmd": str(row.get("playDate") or ymd),
                "scnsrtTm": str(row.get("startTime") or "").replace(":", ""),
                "scnendTm": str(row.get("endTime") or "").replace(":", ""),
                "frSeatCnt": row.get("remainingSeats"),
                "cpSeatCnt": total,
                "movNo": row.get("movieCode"),
                "movNm": row.get("movieName"),
                "siteNo": row.get("theaterCode"),
                "siteNm": row.get("theaterName"),
                "_proxyImax": isinstance(total, (int, float)) and total >= IMAX_MIN_SEATS,
            }
        )
    return normalized


def is_target_format(row: dict[str, Any], keyword: str = FORMAT_KEYWORD) -> bool:
    format_fields = (
        "scnsNm",
        "expoScnsNm",
        "movkndDsplNm",
        "movkndDsplEnm",
        "prodNm",
        "expoProdNm",
        "engProdNm",
    )
    haystack = " ".join(str(row.get(field) or "") for field in format_fields)
    return bool(row.get("_proxyImax")) or keyword.casefold() in haystack.casefold()


def format_date(ymd: str) -> str:
    parsed = date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8]))
    return f"{parsed:%Y-%m-%d} ({WEEKDAYS_KO[parsed.weekday()]})"


def format_time(hhmm: str | None) -> str:
    value = str(hhmm or "")
    return f"{value[:2]}:{value[2:]}" if len(value) == 4 else value


def format_showtimes(rows: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for row in sorted(rows, key=lambda item: str(item.get("scnsrtTm") or "")):
        start = format_time(row.get("scnsrtTm"))
        free = row.get("frSeatCnt")
        total = row.get("cpSeatCnt") or row.get("stcnt")
        seats = f" · 잔여 {free}/{total}석" if free is not None and total else ""
        lines.append(f"• **{start}**{seats}")
    return "\n".join(lines) or "CGV 예매 페이지에서 회차를 확인해 주세요."


def mention_content() -> str:
    return f"<@{DISCORD_USER_ID}>" if DISCORD_USER_ID.isdigit() else ""


def discord_payload_for_open_date(ymd: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    booking_url = CGV_BOOKING_URL.format(movie_no=MOVIE_NO, site_no=SITE_NO)
    payload: dict[str, Any] = {
        "username": "CGV IMAX 알리미",
        "content": mention_content(),
        "allowed_mentions": {
            "parse": [],
            "users": [DISCORD_USER_ID] if DISCORD_USER_ID.isdigit() else [],
        },
        "embeds": [
            {
                "title": "🎟️ 용산 IMAX 예매가 열렸습니다!",
                "description": f"**{MOVIE_NAME}** · {format_date(ymd)}",
                "url": booking_url,
                "color": 0xE21A2C,
                "fields": [
                    {"name": "극장", "value": SITE_NAME, "inline": True},
                    {"name": "상영 포맷", "value": FORMAT_KEYWORD, "inline": True},
                    {"name": "회차 / 잔여 좌석", "value": format_showtimes(rows)},
                ],
                "footer": {"text": "좌석 수는 알림 시점 기준입니다. 제목을 눌러 예매하세요."},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ],
    }
    return payload


def simple_discord_payload(title: str, description: str) -> dict[str, Any]:
    return {
        "username": "CGV IMAX 알리미",
        "content": mention_content(),
        "allowed_mentions": {
            "parse": [],
            "users": [DISCORD_USER_ID] if DISCORD_USER_ID.isdigit() else [],
        },
        "embeds": [
            {
                "title": title,
                "description": description,
                "color": 0x5865F2,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ],
    }


def send_discord(payload: dict[str, Any]) -> bool:
    if not DISCORD_WEBHOOK_URL:
        log("DISCORD_WEBHOOK_URL is missing")
        return False

    for attempt in range(3):
        try:
            response = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=20)
            if response.status_code in (200, 204):
                return True

            if response.status_code == 429:
                try:
                    retry_after = float(decode_json_response(response).get("retry_after") or 0)
                except Exception:
                    retry_after = 0
                if retry_after > MAX_RETRY_WAIT_SECONDS:
                    log(f"Discord rate limited for {retry_after:.1f}s; retry next cycle")
                    return False
                time.sleep(max(1.0, retry_after + 0.5))
                continue

            log(f"Discord send failed: HTTP {response.status_code} {response.text[:200]}")
        except Exception as exc:
            log(f"Discord send error: {type(exc).__name__}: {exc}")

        if attempt < 2:
            time.sleep(2 * (attempt + 1))
    return False


def load_state() -> dict[str, Any]:
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        log(f"State file is invalid ({exc}); starting with a fresh state")
        return {}


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(STATE_FILE)


def target_rows_for_date(session: Any, ymd: str) -> list[dict[str, Any]]:
    return [row for row in fetch_showtimes(session, ymd) if is_target_format(row)]


def initialize_baseline(session: Any, state: dict[str, Any], open_dates: list[str]) -> None:
    imax_dates: list[str] = []
    for ymd in open_dates:
        if target_rows_for_date(session, ymd):
            imax_dates.append(ymd)

    state["alerted_dates"] = imax_dates
    state["initialized"] = True
    description = (
        f"**{MOVIE_NAME}** · **{SITE_NAME} {FORMAT_KEYWORD}**\n"
        f"현재 열린 IMAX 예매일 {len(imax_dates)}일을 기준선으로 저장했습니다.\n"
        "이후 새 날짜에 IMAX 회차가 생기면 바로 알려드릴게요."
    )
    if not send_discord(simple_discord_payload("👀 감시를 시작했습니다", description)):
        raise RuntimeError("Discord start notification failed")
    log(f"Baseline saved: {len(imax_dates)} IMAX date(s)")


def check_once(session: Any) -> int:
    state = load_state()
    try:
        open_dates = fetch_open_dates(session)

        if not state.get("initialized"):
            initialize_baseline(session, state, open_dates)
        else:
            alerted = {str(value) for value in state.get("alerted_dates", [])}
            today = datetime.now(KST).strftime("%Y%m%d")
            alerted = {ymd for ymd in alerted if ymd >= today}
            candidates = [ymd for ymd in open_dates if ymd not in alerted]

            for ymd in candidates:
                rows = target_rows_for_date(session, ymd)
                if not rows:
                    continue
                if not send_discord(discord_payload_for_open_date(ymd, rows)):
                    raise RuntimeError(f"Discord notification failed for {ymd}")
                alerted.add(ymd)
                log(f"New {FORMAT_KEYWORD} booking date alerted: {ymd}")

            state["alerted_dates"] = sorted(alerted)
            log(
                f"Checked {len(open_dates)} open date(s); "
                f"{len(candidates)} unalerted date(s) inspected"
            )

        previous_failures = int(state.get("consecutive_failures", 0))
        if previous_failures >= FAIL_ALERT_AFTER and state.get("failure_alerted"):
            send_discord(
                simple_discord_payload(
                    "✅ CGV 감시가 정상 복구됐습니다",
                    f"{MOVIE_NAME} · {SITE_NAME} {FORMAT_KEYWORD}",
                )
            )
        state["consecutive_failures"] = 0
        state["failure_alerted"] = False
    except Exception as exc:
        failures = int(state.get("consecutive_failures", 0)) + 1
        state["consecutive_failures"] = failures
        log(f"Check failed ({failures} consecutive): {type(exc).__name__}: {exc}")
        if failures >= FAIL_ALERT_AFTER and not state.get("failure_alerted"):
            sent = send_discord(
                simple_discord_payload(
                    "⚠️ CGV 감시 오류",
                    f"{FAIL_ALERT_AFTER}회 연속 조회에 실패했습니다.\n"
                    "GitHub Actions 실행 기록을 확인해 주세요.",
                )
            )
            if sent:
                state["failure_alerted"] = True

    state["last_checked"] = datetime.now(KST).isoformat(timespec="seconds")
    save_state(state)
    return 0


def send_test_alert(session: Any) -> int:
    open_dates = fetch_open_dates(session)
    for ymd in reversed(open_dates):
        rows = target_rows_for_date(session, ymd)
        if rows:
            payload = discord_payload_for_open_date(ymd, rows)
            payload["embeds"][0]["title"] = "🧪 테스트: Discord 알림 연결 성공"
            return 0 if send_discord(payload) else 1

    payload = simple_discord_payload(
        "🧪 테스트: Discord 알림 연결 성공",
        f"현재 조회 가능한 {MOVIE_NAME} {FORMAT_KEYWORD} 회차가 없어 샘플만 보냅니다.",
    )
    return 0 if send_discord(payload) else 1


def main() -> int:
    if not DISCORD_WEBHOOK_URL:
        log("Set the DISCORD_WEBHOOK_URL secret before running the watcher")
        return 2

    session = requests.Session(impersonate="chrome")
    if os.environ.get("CGV_TEST_ALERT") == "1":
        return send_test_alert(session)

    interval = int(os.environ.get("CGV_LOOP_INTERVAL", "0"))
    duration_minutes = float(os.environ.get("CGV_LOOP_DURATION_MIN", "0"))
    if interval <= 0 or duration_minutes <= 0:
        return check_once(session)

    deadline = time.monotonic() + duration_minutes * 60
    iteration = 0
    log(f"Watcher started: every {interval}s for about {duration_minutes:g} minutes")
    while True:
        started = time.monotonic()
        iteration += 1
        if iteration % 60 == 0:
            session = requests.Session(impersonate="chrome")
            log("HTTP session refreshed")
        check_once(session)
        if time.monotonic() + interval > deadline:
            log(f"Watcher finished after {iteration} check(s)")
            return 0
        time.sleep(max(0, interval - (time.monotonic() - started)))


if __name__ == "__main__":
    sys.exit(main())
