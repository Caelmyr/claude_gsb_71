"""统计报表 API。"""
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from flask import Blueprint, request

from backend import config
from backend.api import ok, err
from backend.storage import read_json, list_files, list_dirs, io_stats
from backend.utils import parse_time, now_ts, prob_key
from backend.judge import engine

stats_bp = Blueprint("stats", __name__)

# 时间轴桶数量上限，防止跨度过大导致响应膨胀
_MAX_BUCKETS = 500


def _iter_submissions():
    """遍历所有提交分片，产出提交记录。"""
    for s in list(engine._recent):
        yield s


def _iter_problems():
    for pid in list_files(config.PROBLEMS_DIR):
        p = read_json(os.path.join(config.PROBLEMS_DIR, f"{pid}.json"))
        if p:
            yield p


@stats_bp.get("/stats/overview")
def overview():
    users = len(list_files(config.USERS_DIR))
    problems = len(list_files(config.PROBLEMS_DIR))
    contests = len(list_files(config.CONTESTS_DIR))

    verdict_counter = Counter()
    lang_counter = Counter()
    problem_counter = Counter()
    problem_ac = Counter()
    hour_counter = Counter()
    user_counter = Counter()
    total = 0
    ac = 0

    now = now_ts()
    cutoff = now - 24 * 3600

    for s in _iter_submissions():
        total += 1
        st = s.get("status", "PENDING")
        verdict_counter[st] += 1
        lang_counter[s.get("language", "?")] += 1
        problem_counter[s.get("problem_id")] += 1
        user_counter[s.get("username", s.get("user_id"))] += 1
        if st == "AC":
            ac += 1
            problem_ac[prob_key(s)] += 1
        t = parse_time(s.get("created_at"))
        if t is not None and t >= cutoff:
            hour_counter[datetime.utcfromtimestamp(t).strftime("%Y-%m-%dT%H")] += 1

    # 填充 24 小时时间轴（缺失小时补 0）
    hours = []
    base = datetime.now().replace(minute=0, second=0, microsecond=0)
    for i in range(23, -1, -1):
        key = (base - timedelta(hours=i)).strftime("%Y-%m-%dT%H")
        hours.append({"hour": key, "count": hour_counter.get(key, 0)})

    problem_stats = []
    for p in _iter_problems():
        pid = p.get("id")
        total_p = problem_counter.get(pid, 0)
        ac_p = problem_ac.get(pid, 0)
        problem_stats.append({
            "id": pid, "title": p.get("title"),
            "submissions": total_p, "accepted": ac_p,
            "pass_rate": round(ac_p / total_p, 4) if total_p else 0,
        })
    problem_stats.sort(key=lambda x: -x["submissions"])

    top_users = [
        {"username": u, "submissions": c}
        for u, c in user_counter.most_common(9)
    ]

    return ok({
        "counts": {"users": users, "problems": problems, "contests": contests,
                   "submissions": total, "accepted": ac,
                   "ac_rate": round(ac / total, 4) if total else 0},
        "verdicts": dict(verdict_counter),
        "languages": dict(lang_counter),
        "timeline": hours,
        "problem_stats": problem_stats,
        "top_users": top_users,
        "engine": engine.stats(),
        "io": io_stats(),
    })


@stats_bp.get("/stats/verdicts")
def verdicts():
    c = Counter()
    for s in _iter_submissions():
        c[s.get("status", "PENDING")] += 1
    return ok(dict(c))


# ---- 自定义时间段统计 ----

def _parse_range_param(s):
    """解析起止时间参数，支持 YYYY-MM-DD[THH:MM[:SS]]，失败返回 None。"""
    if not s:
        return None
    s = str(s).strip().replace(" ", "T")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    return None


def _timeline_granularity(span):
    """按时间跨度选择时间轴粒度：≤48 小时按小时，≤62 天按天，否则按月。"""
    if span <= 48 * 3600:
        return "hour"
    if span <= 62 * 86400:
        return "day"
    return "month"


_BUCKET_FMT = {"hour": "%Y-%m-%dT%H", "day": "%Y-%m-%d", "month": "%Y-%m"}


def _iter_bucket_starts(start, end, granularity):
    """按粒度产出时间轴桶起点（本地时间）。"""
    cur = datetime.fromtimestamp(start)
    if granularity == "hour":
        cur = cur.replace(minute=0, second=0, microsecond=0)
    elif granularity == "day":
        cur = cur.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        cur = cur.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end_dt = datetime.fromtimestamp(end)
    while cur <= end_dt:
        yield cur
        if granularity == "hour":
            cur += timedelta(hours=1)
        elif granularity == "day":
            cur += timedelta(days=1)
        else:
            year, month = (cur.year, cur.month + 1) if cur.month < 12 else (cur.year + 1, 1)
            cur = cur.replace(year=year, month=month)


def _build_timeline(timestamps, start, end, granularity):
    """填充完整时间轴（缺失桶补 0）；桶数量超上限时返回 None。"""
    fmt = _BUCKET_FMT[granularity]
    counter = Counter()
    for t in timestamps:
        counter[datetime.fromtimestamp(t).strftime(fmt)] += 1
    points = []
    for bucket_start in _iter_bucket_starts(start, end, granularity):
        if len(points) >= _MAX_BUCKETS:
            return None
        key = bucket_start.strftime(fmt)
        points.append({"bucket": key, "count": counter.get(key, 0)})
    return points


@stats_bp.get("/stats/range")
def range_overview():
    """自定义时间段统计：提交趋势、判定结果分布与各题通过情况。"""
    start_raw = (request.args.get("start") or "").strip()
    end_raw = (request.args.get("end") or "").strip()
    if not start_raw or not end_raw:
        return err("请选择开始时间和结束时间", 400, 400)
    start = _parse_range_param(start_raw)
    end = _parse_range_param(end_raw)
    if start is None or end is None:
        return err("时间格式不正确，应为 YYYY-MM-DDTHH:MM", 400, 400)
    # 结束时间仅指定日期时，按当天末尾处理
    if len(end_raw) <= 10:
        end += 24 * 3600 - 1
    if start > end:
        return err("开始时间不能晚于结束时间", 400, 400)

    verdict_counter = Counter()
    lang_counter = Counter()
    problem_counter = Counter()
    problem_ac = Counter()
    user_counter = Counter()
    timestamps = []
    total = 0
    ac = 0

    for s in _iter_submissions():
        t = parse_time(s.get("created_at"))
        if t is None or t < start or t > end:
            continue
        total += 1
        st = s.get("status", "PENDING")
        verdict_counter[st] += 1
        lang_counter[s.get("language", "?")] += 1
        problem_counter[s.get("problem_id")] += 1
        user_counter[s.get("username", s.get("user_id"))] += 1
        if st == "AC":
            ac += 1
            problem_ac[s.get("problem_id")] += 1
        timestamps.append(t)

    granularity = _timeline_granularity(end - start)
    timeline = _build_timeline(timestamps, start, end, granularity)
    if timeline is None:
        return err("时间跨度过大，请缩小起止范围", 400, 400)

    # 仅统计时段内有提交的题目
    problem_stats = []
    for p in _iter_problems():
        pid = p.get("id")
        total_p = problem_counter.get(pid, 0)
        if not total_p:
            continue
        ac_p = problem_ac.get(pid, 0)
        problem_stats.append({
            "id": pid, "title": p.get("title"),
            "submissions": total_p, "accepted": ac_p,
            "pass_rate": round(ac_p / total_p, 4) if total_p else 0,
        })
    problem_stats.sort(key=lambda x: -x["submissions"])

    top_users = [
        {"username": u, "submissions": c}
        for u, c in user_counter.most_common(9)
    ]

    return ok({
        "range": {"start": start_raw, "end": end_raw, "granularity": granularity},
        "counts": {"submissions": total, "accepted": ac,
                   "ac_rate": round(ac / total, 4) if total else 0,
                   "users": len(user_counter)},
        "verdicts": dict(verdict_counter),
        "languages": dict(lang_counter),
        "timeline": timeline,
        "problem_stats": problem_stats,
        "top_users": top_users,
    })


@stats_bp.get("/stats/cheat-report")
def cheat_report():
    from backend.api import get_current_user, err
    user = get_current_user()
    if not user or user.get("role") != "admin":
        return err("需要管理员权限", 403, 403)
    from backend.judge import cheat
    return ok(cheat.get_report())
