"""统计报表 API。"""
import os
from collections import Counter
from datetime import datetime, timedelta

from flask import Blueprint, request

from backend import config
from backend.api import ok, err
from backend.storage import read_json, list_files, list_dirs, io_stats
from backend.utils import parse_time, now_ts, prob_key, TIME_FORMAT
from backend.judge import engine

stats_bp = Blueprint("stats", __name__)

# 自定义时间范围的保护上限
MAX_RANGE_SECONDS = 366 * 5 * 24 * 3600     # 最长 5 年
MAX_TIMELINE_BUCKETS = 2000                 # 时间轴最多 2000 个点
_RANGE_FORMATS = (
    TIME_FORMAT,                # 2026-09-25T18:30:00
    "%Y-%m-%dT%H:%M",           # datetime-local（秒可选）
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
)


def _iter_submissions():
    """遍历所有提交分片，产出提交记录。"""
    for s in list(engine._recent):
        yield s


def _iter_problems():
    for pid in list_files(config.PROBLEMS_DIR):
        p = read_json(os.path.join(config.PROBLEMS_DIR, f"{pid}.json"))
        if p:
            yield p


def _parse_range_dt(raw, is_end=False):
    """解析前端传入的时间字符串，返回 (datetime, error)。

    仅传日期时，开始取当天 00:00:00、结束取当天 23:59:59。
    """
    raw = (raw or "").strip()
    if not raw:
        return None, None
    for fmt in _RANGE_FORMATS:
        try:
            dt = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        if fmt == "%Y-%m-%d" and is_end:
            dt = dt.replace(hour=23, minute=59, second=59)
        return dt, None
    return None, "时间格式不正确，请通过页面上的时间选择器选择时间"


def _granularity(span_seconds):
    """根据时间跨度选择时间轴粒度（各档桶数都控制在百级以内）。"""
    if span_seconds <= 2 * 86400:
        return "hour"
    if span_seconds <= 90 * 86400:
        return "day"
    if span_seconds <= 366 * 86400:
        return "week"
    return "month"


def _floor_dt(dt, gran):
    """把时间向下取整到粒度边界。"""
    if gran == "hour":
        return dt.replace(minute=0, second=0, microsecond=0)
    if gran == "day":
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if gran == "week":
        d = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        return d - timedelta(days=d.weekday())
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _add_period(dt, gran):
    """推进一个粒度周期。"""
    if gran == "hour":
        return dt + timedelta(hours=1)
    if gran == "day":
        return dt + timedelta(days=1)
    if gran == "week":
        return dt + timedelta(weeks=1)
    if dt.month == 12:
        return dt.replace(year=dt.year + 1, month=1)
    return dt.replace(month=dt.month + 1)


def _bucket_key(dt, gran):
    """时间轴分桶键（与 _build_buckets 的 time 字段一致）。"""
    return _floor_dt(dt, gran).strftime(
        "%Y-%m-%dT%H" if gran == "hour" else
        "%Y-%m-%d" if gran in ("day", "week") else "%Y-%m"
    )


def _bucket_label(dt, gran):
    if gran == "hour":
        return dt.strftime("%m-%d %H:00")
    if gran == "day":
        return dt.strftime("%m-%d")
    if gran == "week":
        return dt.strftime("%m-%d") + " 周"
    return dt.strftime("%Y-%m")


def _build_buckets(start_dt, end_dt, gran):
    """生成覆盖整个时间段的时间轴分桶（缺失补 0）。"""
    cursor = _floor_dt(start_dt, gran)
    buckets = []
    while cursor <= end_dt and len(buckets) <= MAX_TIMELINE_BUCKETS:
        buckets.append({
            "time": cursor.strftime(
                "%Y-%m-%dT%H" if gran == "hour" else
                "%Y-%m-%d" if gran in ("day", "week") else "%Y-%m"
            ),
            "label": _bucket_label(cursor, gran),
            "count": 0,
        })
        cursor = _add_period(cursor, gran)
    return buckets


@stats_bp.get("/stats/overview")
def overview():
    range_mode = (request.args.get("range") or "").strip()
    start_raw = (request.args.get("start") or "").strip()
    end_raw = (request.args.get("end") or "").strip()

    if range_mode not in ("", "24h", "custom"):
        return err("不支持的范围类型", 400, 400)

    # 指定时间范围（近 24 小时 / 自定义起止）时走区间统计
    if range_mode == "24h" or range_mode == "custom" or start_raw or end_raw:
        return _scoped_overview(range_mode, start_raw, end_raw)

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


def _scoped_overview(range_mode, start_raw, end_raw):
    """按指定时间段汇总：趋势、判定分布、语言、题目通过、活跃用户全部限定在区间内。"""
    if range_mode == "24h":
        end_dt = datetime.utcnow().replace(microsecond=0)
        start_dt = end_dt - timedelta(hours=24)
        gran = "hour"
    else:
        start_dt, msg = _parse_range_dt(start_raw, False)
        if msg:
            return err(msg)
        end_dt, msg = _parse_range_dt(end_raw, True)
        if msg:
            return err(msg)
        if start_dt is None or end_dt is None:
            return err("请同时选择开始时间和结束时间")
        if start_dt > end_dt:
            return err("开始时间不能晚于结束时间")
        span = (end_dt - start_dt).total_seconds()
        if span > MAX_RANGE_SECONDS:
            return err("时间范围过大，请将跨度控制在 5 年以内")
        gran = _granularity(span)

    start_ts = start_dt.timestamp()
    end_ts = end_dt.timestamp()

    verdict_counter = Counter()
    lang_counter = Counter()
    problem_counter = Counter()
    problem_ac = Counter()
    contest_counter = Counter()
    user_counter = Counter()
    total = 0
    ac = 0

    buckets = _build_buckets(start_dt, end_dt, gran)
    if len(buckets) > MAX_TIMELINE_BUCKETS:
        return err("时间范围内数据点过多，请缩短时间范围后重试")
    bucket_index = {b["time"]: b for b in buckets}

    for s in _iter_submissions():
        t = parse_time(s.get("created_at"))
        if t is None or t < start_ts or t > end_ts:
            continue
        total += 1
        st = s.get("status", "PENDING")
        pid = s.get("problem_id")
        verdict_counter[st] += 1
        lang_counter[s.get("language", "?")] += 1
        problem_counter[pid] += 1
        user_counter[s.get("username", s.get("user_id"))] += 1
        if s.get("contest_id"):
            contest_counter[s.get("contest_id")] += 1
        if st == "AC":
            ac += 1
            problem_ac[pid] += 1
        bkey = _bucket_key(datetime.utcfromtimestamp(t), gran)
        bucket = bucket_index.get(bkey)
        if bucket is not None:
            bucket["count"] += 1

    # 仅保留该时间段内有提交的题目
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
    problem_stats.sort(key=lambda x: (-x["submissions"], x["id"] or ""))

    top_users = [
        {"username": u, "submissions": c}
        for u, c in user_counter.most_common(9)
    ]

    return ok({
        "scoped": True,
        "range": {
            "mode": "24h" if range_mode == "24h" else "custom",
            "start": start_dt.strftime(TIME_FORMAT),
            "end": end_dt.strftime(TIME_FORMAT),
            "granularity": gran,
            "submissions": total,
        },
        "counts": {
            "users": len(user_counter),
            "problems": len(problem_counter),
            "contests": len(contest_counter),
            "submissions": total, "accepted": ac,
            "ac_rate": round(ac / total, 4) if total else 0,
        },
        "verdicts": dict(verdict_counter),
        "languages": dict(lang_counter),
        "timeline": buckets,
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


@stats_bp.get("/stats/cheat-report")
def cheat_report():
    from backend.api import get_current_user, err
    user = get_current_user()
    if not user or user.get("role") != "admin":
        return err("需要管理员权限", 403, 403)
    from backend.judge import cheat
    return ok(cheat.get_report())
