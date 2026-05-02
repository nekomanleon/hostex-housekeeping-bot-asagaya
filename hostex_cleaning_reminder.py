from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import requests
from playwright.async_api import Locator, Page, TimeoutError as PlaywrightTimeoutError, async_playwright

CALENDAR_URL = "https://hostex.io/app/calendar"

# 本地测试用：先写死
HOSTEX_USERNAME = "leonzhang@lodgegeek.com"
HOSTEX_PASSWORD = "Bigman844721482"

TARGET_ROOM = "阿佐谷A"
TARGET_ROOMS_RAW = "阿佐谷A,阿佐谷B,阿佐谷C"
ROOM_LABEL = TARGET_ROOM
TIMEZONE_NAME = "Asia/Tokyo"
TODAY_OVERRIDE = os.getenv("HOSTEX_TODAY_OVERRIDE", "").strip()
HEADLESS = True
IGNORE_HTTPS_ERRORS = os.getenv("HOSTEX_IGNORE_HTTPS_ERRORS", "false").strip().lower() in {"1", "true", "yes", "on"}
DEBUG = True
REMINDER_DAY_DIFF_RAW = ""
REMINDER_DAY_DIFFS_RAW = "2,1,0"
LOGIN_TIMEOUT_MS = int(os.getenv("HOSTEX_LOGIN_TIMEOUT_MS", "90000"))
PAGE_TIMEOUT_MS = int(os.getenv("HOSTEX_PAGE_TIMEOUT_MS", "90000"))
ROOM_FILTER_WAIT_MS = int(os.getenv("HOSTEX_ROOM_FILTER_WAIT_MS", "2500"))
SNAPSHOT_ATTEMPTS = int(os.getenv("HOSTEX_SNAPSHOT_ATTEMPTS", "3"))
WECOM_SEND_ATTEMPTS = int(os.getenv("WECOM_SEND_ATTEMPTS", "3"))
LOG_DIR = Path(os.getenv("HOSTEX_CLEANING_LOG_DIR", "logs/hostex-housekeeping-bot"))
FULL_DATE_RE = re.compile(r"^(\d{1,2})-(\d{1,2})$")
DAY_ONLY_RE = re.compile(r"^(\d{1,2})$")
REMINDER_DAY_DIFF = int(REMINDER_DAY_DIFF_RAW) if REMINDER_DAY_DIFF_RAW else None
REMINDER_DAY_DIFFS = sorted({
    int(part.strip())
    for part in REMINDER_DAY_DIFFS_RAW.split(",")
    if part.strip()
}) if REMINDER_DAY_DIFFS_RAW else ([REMINDER_DAY_DIFF] if REMINDER_DAY_DIFF is not None else [])
TARGET_ROOMS = [
    part.strip()
    for part in re.split(r"[\r\n,]+", TARGET_ROOMS_RAW)
    if part.strip()
] if TARGET_ROOMS_RAW else ["阿佐谷A", "阿佐谷B", "阿佐谷C"]

VISIBLE_TEXT_NODES_SCRIPT = r'''
() => {
  const nodes = [];
  const normalize = (text) => (text || '').replace(/\u00a0/g, ' ').replace(/[ \t]+/g, ' ').replace(/\n{3,}/g, '\n\n').trim();
  const ok = (el) => {
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity || '1') !== 0 && rect.width >= 1 && rect.height >= 1;
  };
  for (const el of document.body.querySelectorAll('*')) {
    if (!ok(el)) continue;
    const text = normalize(el.innerText || '');
    if (!text) continue;
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    nodes.push({ text, x: rect.x, y: rect.y, width: rect.width, height: rect.height, backgroundColor: style.backgroundColor });
  }
  return nodes;
}
'''

ROW_BOOKING_NODES_SCRIPT = r'''
(args) => {
  const [xMin, yMin, yMax] = args;
  const nodes = [];
  const normalize = (text) => (text || '').replace(/\u00a0/g, ' ').replace(/[ \t]+/g, ' ').replace(/\n{3,}/g, '\n\n').trim();
  for (const el of document.body.querySelectorAll('*')) {
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity || '1') === 0) continue;
    if (rect.width < 1 || rect.height < 1 || rect.right <= xMin || rect.bottom < yMin || rect.top > yMax) continue;
    const text = normalize(el.innerText || '');
    if (!text) continue;
    nodes.push({ text, x: rect.x, y: rect.y, width: rect.width, height: rect.height, backgroundColor: style.backgroundColor });
  }
  return nodes;
}
'''

FILTERED_ROW_DATA_SCRIPT = r'''
(roomNames) => {
  const normalize = (text) => (text || '').replace(/\u00a0/g, ' ').replace(/[ \t]+/g, ' ').replace(/\n{3,}/g, '\n\n').trim();
  const visible = (el) => {
    if (!el) return false;
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity || '1') !== 0 && rect.width >= 1 && rect.height >= 1;
  };

  const roomTitles = Array.from(document.querySelectorAll('.house-cell .name, [title]'))
    .filter(visible)
    .map((el) => ({
      title: normalize(el.getAttribute('title') || el.innerText || ''),
      text: normalize(el.innerText || ''),
    }))
    .filter((item) => item.title || item.text);

  const dateLabels = Array.from(document.querySelectorAll('host-calendar-date-cell .date'))
    .filter((el) => {
      if (!visible(el)) return false;
      const rect = el.getBoundingClientRect();
      return rect.right > 0 && rect.left < window.innerWidth;
    })
    .map((el) => normalize(el.innerText || ''))
    .filter(Boolean);

  const rows = Array.from(document.querySelectorAll('.calendar-cell-row-col .calendar-row')).filter(visible);
  const bars = [];
  if (rows.length === 1) {
    for (const bar of rows[0].querySelectorAll('host-reservation-bar[data-in]')) {
      const block = bar.querySelector('.host-reservation-bar');
      if (!block) continue;
      bars.push({
        dataIn: bar.getAttribute('data-in') || '',
        name: normalize(block.querySelector('.name')?.innerText || ''),
        platform: normalize(block.querySelector('.platform')?.innerText || ''),
        nights: normalize(block.querySelector('.nights')?.innerText || ''),
      });
    }
  }

  return {
    rowCount: rows.length,
    roomTitles,
    dateLabels,
    bars,
  };
}
'''


@dataclass(frozen=True)
class CalendarColumn:
    visible_date: date
    left: float
    right: float
    label: str


@dataclass(frozen=True)
class CleaningEvent:
    reservation: str
    check_in_date: date
    last_stay_date: date
    cleaning_date: date
    day_diff: int
    back_to_back: bool = False


@dataclass(frozen=True)
class CalendarSnapshot:
    room_name: str
    room_label: str
    today: date
    visible_start: date
    visible_end: date
    events: list[CleaningEvent]
    skipped_reservations: list[str]


def today_local() -> date:
    if TODAY_OVERRIDE:
        return date.fromisoformat(TODAY_OVERRIDE)
    return datetime.now(ZoneInfo(TIMEZONE_NAME)).date()


def repair_text(text: str) -> str:
    value = text or ""
    try:
        repaired = value.encode("gbk").decode("utf-8")
    except Exception:
        return value
    return repaired or value


def text_variants(text: str) -> list[str]:
    variants = [text.strip()]
    repaired = repair_text(text).strip()
    if repaired and repaired not in variants:
        variants.append(repaired)
    try:
        mojibake = text.encode("utf-8").decode("gbk").strip()
    except Exception:
        mojibake = ""
    if mojibake and mojibake not in variants:
        variants.append(mojibake)
    return variants


def room_name_matches(expected: str, candidate: str) -> bool:
    candidate_fixed = repair_text(candidate).strip()
    if not candidate_fixed:
        return False
    for variant in text_variants(expected):
        if not variant:
            continue
        if candidate_fixed == variant:
            return True
        if len(variant) >= 3 and variant in candidate_fixed:
            return True
        if len(candidate_fixed) >= 3 and candidate_fixed in variant:
            return True
    return False


def first_line(text: str) -> str:
    text = repair_text(text)
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line
    return text.strip()


def normalize_summary(text: str) -> str:
    text = repair_text(text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return " | ".join(lines[:2]) if lines else text.strip()


def parse_visible_date_labels(labels: list[str], today: date) -> list[date]:
    ordered = [first_line(label) for label in labels if FULL_DATE_RE.match(first_line(label)) or DAY_ONLY_RE.match(first_line(label))]
    if not ordered:
        raise RuntimeError("未识别到日历顶部日期列。")

    anchor_index = next((i for i, label in enumerate(ordered) if FULL_DATE_RE.match(label)), None)
    if anchor_index is None:
        raise RuntimeError("未识别到带月份的日期列，例如 04-15。")

    match = FULL_DATE_RE.match(ordered[anchor_index])
    assert match is not None
    anchor_date = infer_anchor_date(int(match.group(1)), int(match.group(2)), today)
    return [anchor_date + timedelta(days=i - anchor_index) for i in range(len(ordered))]


def load_wecom_webhook() -> str:
    return "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=c0792157-f79f-4ec3-9151-662c804619e1"


def is_transparent(color: str) -> bool:
    value = (color or "").replace(" ", "").lower()
    return value in {"transparent", "rgba(0,0,0,0)", "rgba(255,255,255,0)"}


def cluster(items: list[dict[str, Any]], key: str, tolerance: float) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    for item in sorted(items, key=lambda row: row[key]):
        if not groups or abs(item[key] - groups[-1][-1][key]) > tolerance:
            groups.append([item])
        else:
            groups[-1].append(item)
    return groups


def dedupe_by_x(items: list[dict[str, Any]], tolerance: float = 14.0) -> list[dict[str, Any]]:
    rows = cluster(items, "x", tolerance)
    return [max(row, key=lambda item: item["width"] * item["height"]) for row in rows]


def infer_anchor_date(month: int, day: int, today: date) -> date:
    guessed = date(today.year, month, day)
    if guessed < today - timedelta(days=200):
        return date(today.year + 1, month, day)
    if guessed > today + timedelta(days=200):
        return date(today.year - 1, month, day)
    return guessed


def describe_day_diff(day_diff: int) -> str:
    if day_diff == 0:
        return "今天需要清洁"
    if day_diff == 1:
        return "明天需要清洁"
    if day_diff > 1:
        return f"还有 {day_diff} 天需要清洁"
    if day_diff == -1:
        return "已过 1 天"
    return f"已过 {abs(day_diff)} 天"


def describe_reminder_day_diffs(day_diffs: list[int]) -> str:
    if not day_diffs:
        return "当前设定"

    parts: list[str] = []
    for day_diff in sorted(set(day_diffs), reverse=True):
        if day_diff == 0:
            parts.append("清扫当天")
        else:
            parts.append(f"提前 {day_diff} 天")
    return "、".join(parts)


def format_cn_date(value: date) -> str:
    return f"{value.month}月{value.day}号"


def resolve_room_label(room_name: str) -> str:
    if len(TARGET_ROOMS) == 1:
        return ROOM_LABEL
    return room_name


def primary_event(snapshot: CalendarSnapshot, *, future_only: bool = False) -> Optional[CleaningEvent]:
    future = [event for event in snapshot.events if event.cleaning_date >= snapshot.today]
    if future:
        return future[0]
    if future_only or not snapshot.events:
        return None
    return snapshot.events[-1]


def snapshot_with_events(snapshot: CalendarSnapshot, events: list[CleaningEvent]) -> CalendarSnapshot:
    return CalendarSnapshot(
        snapshot.room_name,
        snapshot.room_label,
        snapshot.today,
        snapshot.visible_start,
        snapshot.visible_end,
        events,
        snapshot.skipped_reservations,
    )


def build_wecom_room_line(snapshot: CalendarSnapshot) -> list[str]:
    if not snapshot.events:
        return []

    primary = snapshot.events[0]
    lines = [
        f"{snapshot.room_label}将在{format_cn_date(primary.cleaning_date)}上午11am退房"
    ]

    if primary.back_to_back:
        lines.append("注意这次客人背靠背，退房时间11AM，下一波客人入住时间16PM。")

    return lines


def build_wecom_combined_message(snapshots: list[CalendarSnapshot]) -> str:
    room_lines: list[str] = []
    back_to_back_lines: list[str] = []

    for snapshot in snapshots:
        if not snapshot.events:
            continue

        primary = snapshot.events[0]
        room_lines.append(
            f"{snapshot.room_label}将在{format_cn_date(primary.cleaning_date)}上午11点退房"
        )

        if primary.back_to_back:
            back_to_back_lines.append(
                f"{snapshot.room_label}注意这次客人背靠背，退房时间11AM，下一波客人入住时间16PM。"
            )

    if not room_lines:
        return ""

    message_lines = [
        "各位打扰了，" + "，".join(room_lines) + "，麻烦各位安排一下打扫，多谢。"
    ]

    if back_to_back_lines:
        message_lines.extend(back_to_back_lines)

    return "\n".join(message_lines)


def build_message(snapshot: CalendarSnapshot) -> str:
    lines = [
        f"Hostex 清洁提醒 | {snapshot.room_label}",
        f"当前日期: {snapshot.today.isoformat()}",
        f"可见日历区间: {snapshot.visible_start.isoformat()} -> {snapshot.visible_end.isoformat()}",
    ]
    if snapshot.events:
        primary = primary_event(snapshot)
        assert primary is not None
        lines.extend([
            f"下一次清洁日期: {primary.cleaning_date.isoformat()} ({describe_day_diff(primary.day_diff)})",
            f"入住最后一天: {primary.last_stay_date.isoformat()}",
            f"预订信息: {primary.reservation}",
        ])
        if primary.back_to_back:
            lines.append("背靠背: 是，退房时间 11AM，下一波客人入住时间 16PM")
        if len(snapshot.events) > 1:
            lines.append("当前可见日历中的清洁节点:")
            for event in snapshot.events:
                extra = " | 背靠背" if event.back_to_back else ""
                lines.append(f"- {event.cleaning_date.isoformat()} | {describe_day_diff(event.day_diff)} | 最后入住日 {event.last_stay_date.isoformat()} | {event.reservation}{extra}")
    else:
        lines.append("当前可见日历中没有识别到明确的退房清洁日期。")
    if snapshot.skipped_reservations:
        lines.append("以下预订延伸到当前可见日历右边界，暂不计算退房日:")
        for item in snapshot.skipped_reservations:
            lines.append(f"- {item}")
    return "\n".join(lines)


def build_skip_message(snapshot: CalendarSnapshot, reminder_day_diffs: list[int]) -> str:
    lines = [
        f"Hostex 清洁提醒 | {snapshot.room_label}",
        f"当前日期: {snapshot.today.isoformat()}",
        f"可见日历区间: {snapshot.visible_start.isoformat()} -> {snapshot.visible_end.isoformat()}",
        f"当前没有“{describe_reminder_day_diffs(reminder_day_diffs)}”的清洁提醒，本次跳过企业微信发送。",
    ]
    if snapshot.events:
        primary = primary_event(snapshot)
        assert primary is not None
        lines.extend([
            f"最近一次清洁日期: {primary.cleaning_date.isoformat()} ({describe_day_diff(primary.day_diff)})",
            f"入住最后一天: {primary.last_stay_date.isoformat()}",
            f"预订信息: {primary.reservation}",
        ])
    else:
        lines.append("当前可见日历中没有识别到明确的退房清洁日期。")
    return "\n".join(lines)


def annotate_back_to_back(events: list[CleaningEvent]) -> list[CleaningEvent]:
    check_in_dates = {event.check_in_date for event in events}
    annotated = [
        CleaningEvent(
            reservation=event.reservation,
            check_in_date=event.check_in_date,
            last_stay_date=event.last_stay_date,
            cleaning_date=event.cleaning_date,
            day_diff=event.day_diff,
            back_to_back=event.cleaning_date in check_in_dates,
        )
        for event in events
    ]
    annotated.sort(key=lambda event: (event.cleaning_date, event.reservation))
    return annotated


def select_snapshots_for_reminder(snapshots: list[CalendarSnapshot]) -> tuple[list[CalendarSnapshot], list[CalendarSnapshot]]:
    """
    返回: (需要发送到企业微信的房间快照, 本次跳过的房间快照)

    旧逻辑只会从多个房间里选最近的 1 个发送。
    新逻辑会让阿佐谷A / 阿佐谷B / 阿佐谷C 分别判断、分别发送；
    如果同一天三间都符合条件，就会三条同时推送到企业微信机器人。
    """
    if not snapshots:
        raise RuntimeError("未抓取到任何房间的日历数据。")

    send_snapshots: list[CalendarSnapshot] = []
    skip_snapshots: list[CalendarSnapshot] = []

    for snapshot in snapshots:
        event = primary_event(snapshot, future_only=True)
        selected = snapshot_with_events(snapshot, [event] if event is not None else [])

        if event is None:
            skip_snapshots.append(selected)
            continue

        # 未配置 HOSTEX_REMINDER_DAY_DIFF / HOSTEX_REMINDER_DAY_DIFFS 时，
        # 只要识别到未来清扫节点就发送。
        should_send = True if not REMINDER_DAY_DIFFS else event.day_diff in REMINDER_DAY_DIFFS
        if should_send:
            send_snapshots.append(selected)
        else:
            skip_snapshots.append(selected)

    return send_snapshots, skip_snapshots


def pick_room_box(nodes: list[dict[str, Any]], room_name: str) -> Optional[dict[str, float]]:
    exact = [node for node in nodes if room_name_matches(room_name, str(node.get("text", "")))]
    if exact:
        best = max(
            exact,
            key=lambda node: (
                1 if float(node.get("height", 0)) >= 20 else 0,
                1 if not is_transparent(str(node.get("backgroundColor", ""))) else 0,
                float(node.get("width", 0)) * float(node.get("height", 0)),
            ),
        )
        return {
            "x": float(best["x"]),
            "y": float(best["y"]),
            "width": float(best["width"]),
            "height": float(best["height"]),
        }
    return None


async def first_visible(locator: Locator) -> Optional[Locator]:
    try:
        count = await locator.count()
    except Exception:
        return None
    for i in range(count):
        item = locator.nth(i)
        try:
            if await item.is_visible():
                return item
        except Exception:
            continue
    return None


async def save_debug(page: Page, name: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        await page.screenshot(path=str(LOG_DIR / f"{stamp}_{name}.png"), full_page=True)
    except Exception:
        pass
    try:
        (LOG_DIR / f"{stamp}_{name}.html").write_text(await page.content(), encoding="utf-8")
    except Exception:
        pass

async def maybe_close_modal(page: Page) -> None:
    candidates = [
        page.get_by_role("button", name=re.compile(r"关闭|Close", re.IGNORECASE)),
        page.locator("button").filter(has_text=re.compile(r"关闭|Close", re.IGNORECASE)),
    ]
    for candidate in candidates:
        button = await first_visible(candidate)
        if button is None:
            continue
        try:
            await button.click(timeout=1500)
            await page.wait_for_timeout(300)
            return
        except Exception:
            continue


async def wait_for_room_box(page: Page, room_name: str, timeout_ms: int = 20000) -> dict[str, float]:
    room_labels = text_variants(room_name)
    deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
    last_nodes: list[dict[str, Any]] = []
    while asyncio.get_running_loop().time() < deadline:
        nodes = await visible_nodes(page)
        last_nodes = nodes
        room_box = pick_room_box(nodes, room_name)
        if room_box is not None:
            return room_box

        for label in room_labels:
            title_locator = page.locator(f'[title="{label}"]')
            title_node = await first_visible(title_locator)
            if title_node is None:
                continue
            title_box = await title_node.bounding_box()
            if title_box is None:
                continue
            nearby = [
                node
                for node in nodes
                if room_name_matches(room_name, str(node.get("text", "")))
                and abs(float(node.get("y", 0)) - title_box["y"]) <= 24
            ]
            matched = pick_room_box(nearby, room_name)
            if matched is not None:
                return matched
            return title_box

        await maybe_close_modal(page)
        await page.wait_for_timeout(1000)

    if DEBUG and last_nodes:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        (LOG_DIR / f"{stamp}_visible_nodes_preview.json").write_text(
            str(last_nodes[:120]),
            encoding="utf-8",
        )
    await save_debug(page, "room_not_visible")
    raise RuntimeError(f"未在 Hostex 日历中定位到房间: {room_name}")


async def ensure_password_login_mode(page: Page) -> None:
    toggle = await first_visible(
        page.get_by_role("button", name=re.compile(r"使用密码登录", re.IGNORECASE))
    ) or await first_visible(
        page.locator("button").filter(has_text=re.compile(r"使用密码登录", re.IGNORECASE))
    )
    if toggle is None:
        return

    await toggle.click()
    await page.wait_for_timeout(1000)
    try:
        await page.wait_for_selector("input[type='password']", timeout=5000)
    except Exception:
        pass


async def goto_with_retry(page: Page, url: str, *, attempts: int = 3) -> None:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            return
        except Exception as exc:
            last_error = exc
            if attempt >= attempts or not any(
                token in str(exc) for token in ("ERR_CONNECTION_RESET", "ERR_ABORTED", "ERR_TIMED_OUT", "Timeout")
            ):
                raise
            await page.wait_for_timeout(1500 * attempt)
    if last_error is not None:
        raise last_error


async def login_if_needed(page: Page) -> None:
    await goto_with_retry(page, CALENDAR_URL)
    await page.wait_for_timeout(2500)
    await maybe_close_modal(page)
    await ensure_password_login_mode(page)

    username_input = await first_visible(
        page.locator("input[placeholder*='手机号'], input[placeholder*='邮箱'], input[placeholder*='email'], input[type='text'], input[type='email']")
    )
    password_input = await first_visible(page.locator("input[type='password']"))
    on_login_page = "/app/login/" in page.url or (username_input is not None and password_input is not None)
    if not on_login_page:
        return

    if not HOSTEX_USERNAME or not HOSTEX_PASSWORD:
        raise RuntimeError("Hostex 需要登录，但未配置 HOSTEX_USERNAME/HOSTEX_PASSWORD。")
    if password_input is None:
        await save_debug(page, "password_input_not_found")
        raise RuntimeError("已进入 Hostex 登录页，但未找到密码输入框。")

    await username_input.fill(HOSTEX_USERNAME)
    await password_input.fill(HOSTEX_PASSWORD)
    submit_name = re.compile(r"^\s*(登录|Log\s*in|Sign\s*in)\s*$", re.IGNORECASE)
    login_button = await first_visible(
        page.get_by_role("button", name=submit_name)
    ) or await first_visible(
        page.locator("button").filter(has_text=submit_name)
    )
    if login_button is None:
        raise RuntimeError("未找到 Hostex 登录按钮。")

    await login_button.click()
    try:
        await page.wait_for_url(re.compile(r"https://hostex\.io/app/calendar.*"), timeout=LOGIN_TIMEOUT_MS)
    except PlaywrightTimeoutError as exc:
        await save_debug(page, "hostex_login_failed")
        raise RuntimeError("Hostex 登录后未能进入日历页面。") from exc
    await page.wait_for_timeout(3000)


async def filter_room(page: Page, room_name: str) -> dict[str, float]:
    try:
        return await wait_for_room_box(page, room_name, timeout_ms=8000)
    except RuntimeError:
        pass

    search_input = await first_visible(
        page.locator(
            "input[placeholder*='房间'], input[placeholder*='Room'], input[placeholder*='名称'], "
            "input.ant-input:not([readonly]):not([disabled]), input[type='text']:not([readonly]):not([disabled])"
        )
    )
    if search_input is not None:
        try:
            await search_input.click()
            await search_input.press("Control+A")
            await search_input.press("Backspace")
        except Exception:
            pass
        await search_input.fill(room_name)
        try:
            await search_input.press("Enter")
        except Exception:
            pass
        await page.wait_for_timeout(ROOM_FILTER_WAIT_MS)

    return await wait_for_room_box(page, room_name)


async def apply_room_filter(page: Page, room_name: str) -> None:
    search_input = await first_visible(
        page.locator(
            "input[placeholder*='房间'], input[placeholder*='Room'], input[placeholder*='名称'], "
            "input.ant-input:not([readonly]):not([disabled]), input[type='text']:not([readonly]):not([disabled])"
        )
    )
    if search_input is None:
        return
    try:
        await search_input.click()
        await search_input.press("Control+A")
        await search_input.press("Backspace")
    except Exception:
        pass
    await search_input.fill(room_name)
    try:
        await search_input.press("Enter")
    except Exception:
        pass
    await page.wait_for_timeout(ROOM_FILTER_WAIT_MS)


async def visible_nodes(page: Page) -> list[dict[str, Any]]:
    nodes = await page.evaluate(VISIBLE_TEXT_NODES_SCRIPT)
    for node in nodes:
        node["text"] = repair_text(str(node.get("text", "")))
    return nodes


async def row_nodes(page: Page, room_box: dict[str, float]) -> list[dict[str, Any]]:
    x_min = room_box["x"] + room_box["width"] + 8
    row_center = room_box["y"] + room_box["height"] / 2
    band_half = max(room_box["height"] / 2 + 6, 24)
    band_half = min(band_half, 32)
    nodes = await page.evaluate(ROW_BOOKING_NODES_SCRIPT, [x_min, row_center - band_half, row_center + band_half])
    for node in nodes:
        node["text"] = repair_text(str(node.get("text", "")))
    return nodes


async def build_snapshot_from_filtered_dom(page: Page, room_name: str, room_label: str) -> Optional[CalendarSnapshot]:
    data = await page.evaluate(FILTERED_ROW_DATA_SCRIPT, text_variants(room_name))
    room_titles = [repair_text(str(item.get("title", ""))).strip() for item in data.get("roomTitles", [])]
    if data.get("rowCount") != 1:
        return None
    matched_titles = [title for title in room_titles if room_name_matches(room_name, title)]
    if room_titles and not matched_titles:
        return None

    today = today_local()
    visible_dates = parse_visible_date_labels(list(data.get("dateLabels", [])), today)
    bars = []
    for item in data.get("bars", []):
        data_in = str(item.get("dataIn", "")).strip()
        if not data_in:
            continue
        try:
            check_in = date.fromisoformat(data_in)
        except ValueError:
            continue
        nights_text = repair_text(str(item.get("nights", ""))).strip()
        nights_match = re.search(r"(\d+)", nights_text)
        if nights_match is None:
            continue
        nights = int(nights_match.group(1))
        name = repair_text(str(item.get("name", ""))).strip()
        platform = repair_text(str(item.get("platform", ""))).strip()
        summary = " | ".join(part for part in [name, " ".join(part for part in [platform, nights_text] if part).strip()] if part)
        last_stay_date = check_in + timedelta(days=nights - 1)
        cleaning_date = check_in + timedelta(days=nights)
        bars.append(CleaningEvent(summary, check_in, last_stay_date, cleaning_date, (cleaning_date - today).days))

    bars = annotate_back_to_back(bars)
    resolved_room = matched_titles[0] if matched_titles else (room_titles[0] if room_titles else room_name)
    return CalendarSnapshot(resolved_room, room_label, today, visible_dates[0], visible_dates[-1], bars, [])


async def wait_for_filtered_dom_snapshot(page: Page, room_name: str, room_label: str, attempts: int = 6) -> Optional[CalendarSnapshot]:
    for attempt in range(attempts):
        snapshot = await build_snapshot_from_filtered_dom(page, room_name, room_label)
        if snapshot is not None:
            return snapshot
        if attempt < attempts - 1:
            await apply_room_filter(page, room_name)
            await page.wait_for_timeout(1000)
    return None


def parse_columns(nodes: list[dict[str, Any]], room_box: dict[str, float], today: date) -> list[CalendarColumn]:
    room_top = room_box["y"]
    room_right = room_box["x"] + room_box["width"]
    candidates: list[dict[str, Any]] = []
    for node in nodes:
        if node["x"] <= room_right - 10:
            continue
        if node["y"] + node["height"] / 2 >= room_top - 4:
            continue
        if node["width"] < 35 or node["height"] < 24:
            continue
        label = first_line(node["text"])
        if FULL_DATE_RE.match(label) or DAY_ONLY_RE.match(label):
            item = dict(node)
            item["label"] = label
            candidates.append(item)
    if not candidates:
        raise RuntimeError("未识别到日历顶部日期列。")

    best_row: list[dict[str, Any]] = []
    best_score: tuple[int, int] | None = None
    for row in cluster(candidates, "y", 26.0):
        deduped = dedupe_by_x(row)
        score = (1 if any(FULL_DATE_RE.match(item["label"]) for item in deduped) else 0, len(deduped))
        if best_score is None or score > best_score:
            best_score = score
            best_row = deduped

    ordered = sorted(best_row, key=lambda item: item["x"])
    anchor_index = next((i for i, item in enumerate(ordered) if FULL_DATE_RE.match(item["label"])), None)
    if anchor_index is None:
        raise RuntimeError("未识别到带月份的日期列，例如 04-15。")
    match = FULL_DATE_RE.match(ordered[anchor_index]["label"])
    assert match is not None
    anchor_date = infer_anchor_date(int(match.group(1)), int(match.group(2)), today)

    columns: list[CalendarColumn] = []
    for i, item in enumerate(ordered):
        columns.append(
            CalendarColumn(
                visible_date=anchor_date + timedelta(days=i - anchor_index),
                left=float(item["x"]),
                right=float(item["x"] + item["width"]),
                label=item["label"],
            )
        )
    return columns


def extract_bars(nodes: list[dict[str, Any]], columns: list[CalendarColumn]) -> tuple[list[dict[str, Any]], list[str]]:
    first_left = columns[0].left
    last_right = columns[-1].right
    visible_span = last_right - first_left
    candidates: list[dict[str, Any]] = []
    for node in nodes:
        if node["width"] < 60 or node["height"] < 20:
            continue
        if node["width"] > visible_span * 1.35:
            continue
        if is_transparent(str(node.get("backgroundColor", ""))):
            continue
        if node["x"] + node["width"] <= first_left + 4:
            continue
        summary = normalize_summary(str(node["text"]))
        if not summary:
            continue
        candidates.append({
            "summary": summary,
            "left": float(node["x"]),
            "right": float(node["x"] + node["width"]),
            "clipped_right": node["x"] + node["width"] >= last_right - 8,
        })

    deduped = dedupe_by_x([{**item, "x": item["left"], "width": item["right"] - item["left"], "height": 24} for item in candidates], 12.0)
    bars: list[dict[str, Any]] = []
    skipped: list[str] = []
    for item in deduped:
        summary = item["summary"]
        clipped_right = bool(item["clipped_right"])
        bars.append({"summary": summary, "left": item["left"], "right": item["right"], "clipped_right": clipped_right})
        if clipped_right:
            skipped.append(summary)
    return bars, skipped


def compute_events(bars: list[dict[str, Any]], columns: list[CalendarColumn], today: date) -> list[CleaningEvent]:
    events: list[CleaningEvent] = []
    for bar in bars:
        if bar["clipped_right"]:
            continue
        overlapped = [col for col in columns if bar["left"] < col.right - 4 and bar["right"] > col.left + 4]
        if not overlapped:
            continue
        check_in_date = overlapped[0].visible_date
        last_stay_date = overlapped[-1].visible_date
        cleaning_date = last_stay_date + timedelta(days=1)
        events.append(CleaningEvent(bar["summary"], check_in_date, last_stay_date, cleaning_date, (cleaning_date - today).days))
    return annotate_back_to_back(events)

async def build_snapshot(page: Page, room_name: str, room_label: str) -> CalendarSnapshot:
    await apply_room_filter(page, room_name)
    dom_snapshot = await wait_for_filtered_dom_snapshot(page, room_name, room_label)
    if dom_snapshot is not None:
        return dom_snapshot

    await login_if_needed(page)
    room_box = await filter_room(page, room_name)

    today = today_local()
    columns = parse_columns(await visible_nodes(page), room_box, today)
    bars, skipped = extract_bars(await row_nodes(page, room_box), columns)
    events = compute_events(bars, columns, today)
    return CalendarSnapshot(room_name, room_label, today, columns[0].visible_date, columns[-1].visible_date, events, skipped)


async def collect_snapshot_with_retry(page: Page, room_name: str, room_label: str) -> CalendarSnapshot:
    last_error: Exception | None = None
    for attempt in range(1, SNAPSHOT_ATTEMPTS + 1):
        try:
            await login_if_needed(page)
            return await build_snapshot(page, room_name, room_label)
        except Exception as exc:
            last_error = exc
            await save_debug(page, f"{room_label}_snapshot_attempt_{attempt}_failed")
            if attempt >= SNAPSHOT_ATTEMPTS:
                break
            await page.wait_for_timeout(1500 * attempt)
    if last_error is not None:
        raise last_error
    raise RuntimeError("未能从 Hostex 日历抓取到房态数据。")


def send_wecom_text(message: str) -> bool:
    webhook = load_wecom_webhook()
    if not webhook:
        print("\n===== WECOM_WEBHOOK 未配置，跳过企业微信发送 =====")
        return False
    if not webhook.lower().startswith("http"):
        print("\n===== WECOM_WEBHOOK 配置无效，跳过企业微信发送 =====")
        return False

    payload = {"msgtype": "text", "text": {"content": message}}
    last_error: Exception | None = None
    for attempt in range(1, WECOM_SEND_ATTEMPTS + 1):
        try:
            response = requests.post(
                webhook,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json; charset=utf-8"},
                timeout=20,
            )
            response.raise_for_status()
            try:
                data = response.json()
            except ValueError:
                data = {"raw": response.text}
            if isinstance(data, dict) and data.get("errcode", 0) not in (0, "0", None):
                raise RuntimeError(f"企业微信 webhook 返回错误: {data}")
            print("\n===== 企业微信发送成功 =====")
            print(json.dumps(data, ensure_ascii=False))
            return True
        except requests.RequestException as exc:
            last_error = exc
            if attempt >= WECOM_SEND_ATTEMPTS:
                break
            print(f"\n===== 企业微信发送失败，准备重试 ({attempt}/{WECOM_SEND_ATTEMPTS}) =====")
            print(str(exc))
            time.sleep(attempt)

    if last_error is not None:
        raise RuntimeError("企业微信发送失败，已达到最大重试次数。") from last_error
    raise RuntimeError("企业微信发送失败。")


async def run() -> tuple[str, list[str]]:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        context = await browser.new_context(ignore_https_errors=IGNORE_HTTPS_ERRORS)
        page = await context.new_page()
        page.set_default_timeout(PAGE_TIMEOUT_MS)
        page.set_default_navigation_timeout(PAGE_TIMEOUT_MS)
        try:
            snapshots = [
                await collect_snapshot_with_retry(page, room_name, resolve_room_label(room_name))
                for room_name in TARGET_ROOMS
            ]
            send_snapshots, skip_snapshots = select_snapshots_for_reminder(snapshots)
            if DEBUG:
                await save_debug(page, "calendar_snapshot")

            log_blocks: list[str] = []
            wecom_messages: list[str] = []

            for snapshot in send_snapshots:
                log_blocks.append(build_message(snapshot))

            combined_wecom_message = build_wecom_combined_message(send_snapshots)
            if combined_wecom_message:
                wecom_messages.append(combined_wecom_message)

            for snapshot in skip_snapshots:
                log_blocks.append(build_skip_message(snapshot, REMINDER_DAY_DIFFS))

            return "\n\n".join(log_blocks), wecom_messages
        except Exception:
            await save_debug(page, "run_failed")
            raise
        finally:
            await context.close()
            await browser.close()


async def main() -> None:
    message, wecom_messages = await run()
    if wecom_messages:
        # 同时向企业微信机器人发送多条消息；阿佐谷A/B/C 同时符合条件时会并行推送。
        results = await asyncio.gather(
            *(asyncio.to_thread(send_wecom_text, item) for item in wecom_messages),
            return_exceptions=True,
        )
        errors = [result for result in results if isinstance(result, Exception)]
        if errors:
            raise RuntimeError(f"企业微信部分消息发送失败: {errors}")
    else:
        print("\n===== 当前没有符合发送条件的提醒，已跳过企业微信发送 =====")
    print(message)


if __name__ == "__main__":
    asyncio.run(main())
