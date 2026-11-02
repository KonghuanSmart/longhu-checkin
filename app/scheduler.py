"""定时调度：每天固定时间跑一轮签到。时间可在前端改，改完立即生效。"""

import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import db, runner

logger = logging.getLogger(__name__)

JOB_ID = "daily_checkin"
scheduler = BackgroundScheduler(timezone="Asia/Shanghai")


def _job():
    logger.info("定时任务开始执行")
    results = runner.run_all()
    runner.daily_summary(results)
    logger.info("定时任务完成，共 %d 个账号", len(results))


def _parse_time(text: str) -> tuple[int, int]:
    try:
        hour, minute = text.split(":")
        hour, minute = int(hour), int(minute)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    except Exception:
        pass
    return 8, 5


def reschedule():
    """按当前设置重建定时任务。"""
    text = db.get_settings().get("checkin_time", "08:05")
    hour, minute = _parse_time(text)
    scheduler.add_job(
        _job,
        CronTrigger(hour=hour, minute=minute),
        id=JOB_ID,
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
    )
    logger.info("签到时间已设为每天 %02d:%02d", hour, minute)


def start():
    if not scheduler.running:
        scheduler.start()
        reschedule()


def next_run_text() -> str:
    job = scheduler.get_job(JOB_ID)
    if job and job.next_run_time:
        return job.next_run_time.strftime("%Y-%m-%d %H:%M")
    return ""
