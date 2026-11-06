"""分时调度：每个账号的「小程序」和「APP」两个入口各排各的时间，互不相连。

改动原因（2026-11-06）：原先两个入口在同一轮里连着打，日志上相隔 1 秒。
两个活动、同一设备、同一时间连续命中的特征太明显，容易被判成机器行为。
现在拆成两个独立的排期，各自有窗口、各自重试。

时间取值优先级：账号自定义 > 全局设置 > 内置默认。
窗口 = [起点, 起点 + 随机浮动分钟)，每天在这个区间里随机取一时刻，
避免固定时刻被风控识别；浮动填 0 就退化成固定时间。

实现上不用 cron（cron 只支持固定值），而是每个（账号 × 入口）排一个一次性
任务，再由每 5 分钟的巡场任务补齐。每天挑中的时刻写进 slots.plan_at，
这样巡场反复跑也不会把当天的时间重掷一遍。
"""

import logging
import random
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from . import db, runner

logger = logging.getLogger(__name__)

JOB_PREFIX = "slot:"
PLANNER_ID = "slot_planner"
DEFAULT_TIME = "08:00"
DEFAULT_JITTER = 60

# 失败重试：入口被上游临时拒绝（如「网络故障，请稍后再试」）时当天再补几次。
# 退避 10/20/30/40 分钟。每个入口独立计数，互不影响。
MAX_RETRY = 4
RETRY_MINUTES = 10

# 不需要重试的状态：已签、已签过、指纹冲突、风控、待人工、已关闭
NO_RETRY_STATUS = {"already", "conflict", "risk", "todo", "skipped"}

ENTRY_LABEL = {"mini": "小程序", "app": "APP"}

scheduler = BackgroundScheduler(timezone="Asia/Shanghai")


def _now() -> datetime:
    return datetime.now(scheduler.timezone)


def _today_text() -> str:
    return _now().strftime("%Y-%m-%d")


def _parse_hhmm(text: str | None, fallback: tuple[int, int] = (8, 0)) -> tuple[int, int]:
    try:
        hour, minute = str(text).split(":")
        hour, minute = int(hour), int(minute)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    except Exception:
        pass
    return fallback


def _parse_iso(text: str | None) -> datetime | None:
    try:
        dt = datetime.fromisoformat(text) if text else None
    except (TypeError, ValueError):
        return None
    if dt is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=scheduler.timezone)
    return dt


def _jitter(settings: dict, entry: str) -> int:
    key = "checkin_jitter" if entry == "mini" else "app_checkin_jitter"
    try:
        value = int(float(settings.get(key) or 0))
    except (TypeError, ValueError):
        value = DEFAULT_JITTER
    return max(0, min(value, 1439))


def _start(settings: dict, account: dict | None, entry: str) -> tuple[int, int]:
    """入口的窗口起点。账号自定义优先，其次全局。"""
    if account:
        custom = (account.get("mini_time") if entry == "mini" else account.get("app_time")) or ""
        if custom.strip():
            return _parse_hhmm(custom.strip())
    key = "checkin_time" if entry == "mini" else "app_checkin_time"
    return _parse_hhmm(settings.get(key) or DEFAULT_TIME)


def _config(settings: dict, account: dict | None, entry: str) -> tuple[int, int, int]:
    hour, minute = _start(settings, account, entry)
    return hour, minute, _jitter(settings, entry)


def _window_text(hour: int, minute: int, jitter: int) -> str:
    start = f"{hour:02d}:{minute:02d}"
    if jitter <= 0:
        return start
    total = hour * 60 + minute + jitter
    return f"{start}–{(total // 60) % 24:02d}:{total % 60:02d}"


def window_text(entry: str = "mini") -> str:
    """全局窗口描述，给前端和启动日志用（不含账号自定义）。"""
    settings = db.get_settings()
    hour, minute, jitter = _config(settings, None, entry)
    return _window_text(hour, minute, jitter)


def account_window(account: dict, entry: str, settings: dict | None = None) -> str:
    """某个账号在某入口的实际窗口（考虑了它的自定义时间）。"""
    settings = settings if settings is not None else db.get_settings()
    hour, minute, jitter = _config(settings, account, entry)
    return _window_text(hour, minute, jitter)


def effective_start(account: dict | None, entry: str, settings: dict | None = None) -> str:
    """某个账号在某入口的实际起点（HH:MM），用于校验两个入口是否撞车。"""
    settings = settings if settings is not None else db.get_settings()
    hour, minute, _ = _config(settings, account, entry)
    return f"{hour:02d}:{minute:02d}"


def _job_id(account_id: int, entry: str) -> str:
    return f"{JOB_PREFIX}{account_id}:{entry}"


def _plan_slot(account: dict, entry: str, slot: dict, settings: dict) -> datetime:
    """算出这个（账号 × 入口）下一次该跑的时刻，并落库。

    优先级：当天待执行的重试 > 当天已定的计划 > 今天窗口内随机 > 明天窗口内随机。
    """
    now = _now()
    today = _today_text()

    retry_at = _parse_iso(slot.get("retry_at"))
    if retry_at and retry_at.date() == now.date() and retry_at > now:
        db.save_slot(account["id"], entry, plan_date=today, plan_at=retry_at.isoformat())
        return retry_at

    planned = _parse_iso(slot.get("plan_at"))
    if planned and slot.get("plan_date") == today and planned.date() == now.date():
        return planned

    hour, minute, jitter = _config(settings, account, entry)
    start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    end = start + timedelta(minutes=jitter)

    done_today = (slot.get("last_date") == today
                  or (account.get("last_run_at") or "")[:10] == today)

    if now >= end:
        if done_today:
            start += timedelta(days=1)
            end += timedelta(days=1)
            base = start
        else:
            # 窗口已经过了但今天还没跑（比如刚部署、或机器睡醒了）：
            # 一分钟后就补一次，别把今天整天丢掉
            when = now + timedelta(seconds=60)
            db.save_slot(account["id"], entry, plan_date=today, plan_at=when.isoformat())
            return when
    elif now <= start:
        base = start
    else:
        base = now

    span = int((end - base).total_seconds())
    when = base + timedelta(seconds=random.randint(0, span - 1)) if span > 0 else base
    db.save_slot(account["id"], entry, plan_date=when.strftime("%Y-%m-%d"),
                 plan_at=when.isoformat())
    return when


def _slot_job(account_id: int, entry: str):
    """跑一个账号的一个入口。"""
    account = db.get_account(account_id)
    if not account or not account.get("enabled"):
        return
    slot = db.get_slot(account_id, entry)
    today = _today_text()
    if slot.get("last_date") == today:
        return

    label = ENTRY_LABEL.get(entry, entry)
    try:
        logger.info("[%s] %s入口开始执行", account["name"], label)
        result = runner.run_account(
            account,
            do_checkin=True,
            entries=(entry,),
            with_profile=(entry == "mini"),
        )
        status = result.get("checkin_status") or "fail"
    except Exception:
        logger.exception("[%s] %s入口执行失败", account["name"], label)
        status = "fail"

    retries = int(slot.get("retry_count") or 0)
    if status == "ok" or status in NO_RETRY_STATUS:
        db.save_slot(account_id, entry, last_date=today, plan_date="", plan_at="",
                     retry_at="", retry_count=0)
        return

    if retries < MAX_RETRY:
        delay = RETRY_MINUTES * (retries + 1)
        when = _now() + timedelta(minutes=delay)
        db.save_slot(account_id, entry, retry_at=when.isoformat(), retry_count=retries + 1,
                     plan_date="", plan_at="")
        logger.warning("[%s] %s入口失败，%d 分钟后重试（第 %d/%d 次）",
                       account["name"], label, delay, retries + 1, MAX_RETRY)
    else:
        db.save_slot(account_id, entry, last_date=today, retry_at="", retry_count=0,
                     plan_date="", plan_at="")
        logger.error("[%s] %s入口已重试 %d 次仍失败，今天不再重试",
                     account["name"], label, MAX_RETRY)


def _planner():
    """巡场：按当前账号与设置，为每个（账号 × 入口）补齐下一次排期。"""
    try:
        settings = db.get_settings()
        app_mode = (settings.get("app_checkin_mode") or "auto").strip().lower()
        accounts = db.list_accounts(only_enabled=True)

        wanted: dict[str, tuple[dict, str]] = {}
        for account in accounts:
            entries = ("mini",) if app_mode == "off" else db.SLOT_ENTRIES
            for entry in entries:
                wanted[_job_id(account["id"], entry)] = (account, entry)

        for job in list(scheduler.get_jobs()):
            if job.id.startswith(JOB_PREFIX) and job.id not in wanted:
                job.remove()

        for job_id, (account, entry) in wanted.items():
            when = _plan_slot(account, entry, db.get_slot(account["id"], entry), settings)
            job = scheduler.get_job(job_id)
            if job and job.next_run_time and abs((job.next_run_time - when).total_seconds()) < 1:
                continue
            scheduler.add_job(
                _slot_job,
                DateTrigger(run_date=when),
                args=[account["id"], entry],
                id=job_id,
                replace_existing=True,
                misfire_grace_time=1800,
                coalesce=True,
            )
            logger.info("排期 %s %s入口 → %s",
                        account["name"], ENTRY_LABEL.get(entry, entry),
                        when.strftime("%Y-%m-%d %H:%M:%S"))
    except Exception:
        logger.exception("重排签到任务失败")


def reschedule():
    """按当前设置重建排程，改完立即生效。"""
    scheduler.add_job(
        _planner,
        CronTrigger(minute="*/5"),
        id=PLANNER_ID,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    _planner()
    logger.info("签到窗口：小程序 %s，APP %s", window_text("mini"), window_text("app"))


def start():
    if not scheduler.running:
        scheduler.start()
        reschedule()


def next_run_text() -> str:
    """最近的一次执行时刻（两个入口混在一起看）。"""
    times = [j.next_run_time for j in scheduler.get_jobs()
             if j.id.startswith(JOB_PREFIX) and j.next_run_time]
    if not times:
        return ""
    return min(times).strftime("%Y-%m-%d %H:%M")


def next_run_map() -> dict[str, str]:
    """每个（账号 × 入口）的下次执行时刻，键形如 '1:mini'。"""
    out: dict[str, str] = {}
    for job in scheduler.get_jobs():
        if job.id.startswith(JOB_PREFIX) and job.next_run_time:
            out[job.id[len(JOB_PREFIX):]] = job.next_run_time.strftime("%m-%d %H:%M")
    return out
