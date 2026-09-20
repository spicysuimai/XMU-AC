"""
XMU Tronclass 全自动签到脚本
- xmulogin 纯 API CAS 登录（无浏览器，稳定）
- Cookie 持久化（重启免登录）
- 数字签到：API 直接抓码 → 提交
- GPS 签到：两条线三角定位自动算坐标 → 提交
- 内置看门狗：崩溃自动重启

用法:
  $env:STUDENT_ID="你的学号"
  $env:PASSWORD="你的密码"
  python auto_checkin.py
"""
import getpass
import json
import math
import os
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests
from xmulogin import xmulogin

# ============================================================
# 配置 —— 优先环境变量，否则交互输入
# ============================================================
STUDENT_ID = os.environ.get("STUDENT_ID")
PASSWORD = os.environ.get("PASSWORD")
INTERVAL = 15           # 轮询间隔秒数
# GPS 坐标通过三角定位自动获取，无需手动填写
# ============================================================

if not STUDENT_ID:
    STUDENT_ID = input("学号: ").strip()
if not PASSWORD:
    PASSWORD = getpass.getpass("密码: ")

if not STUDENT_ID or not PASSWORD:
    print("错误: 学号和密码不能为空")
    exit(1)

BASE_URL = "https://lnt.xmu.edu.cn"
SESSION_FILE = Path(__file__).resolve().parent / "auto_checkin_session.json"
RECORD_FILE = Path(__file__).resolve().parent / "checkin_record.json"

HEADERS_BASE = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "zh-CN,zh;q=0.9",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/147.0.0.0 Safari/537.36"
    ),
}

# GPS 三角定位误差容忍（米）：两圆交点距离在此范围内视为相交
GPS_DISTANCE_TOLERANCE = 5.0
# 未知状态最大重试次数（超出后视为终端，不再重试）
MAX_UNKNOWN_RETRIES = 10


# ============================================================
# 登录模块：xmulogin + cookie 持久化
# ============================================================

def record_ok(rtype, course, detail):
    """追加一条成功签到记录（供控制台实时展示今日已签到课程）"""
    try:
        recs = []
        if RECORD_FILE.exists():
            recs = json.loads(RECORD_FILE.read_text(encoding="utf-8"))
        recs.append({
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "type": rtype, "course": course, "detail": detail,
        })
        RECORD_FILE.write_text(json.dumps(recs[-200:], ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _save_cookies(session):
    """保存完整 cookie jar 到文件（含 domain/path/expires/secure 等属性）"""
    try:
        cookies = []
        for cookie in session.cookies:
            cookies.append({
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path,
                "expires": cookie.expires,
                "secure": cookie.secure,
                "rest": cookie._rest,
            })
        with open(SESSION_FILE, "w") as f:
            json.dump(cookies, f)
        print(f"[登录] Cookie 已保存到 {SESSION_FILE}")
    except Exception as e:
        print(f"[登录] 保存 Cookie 失败: {e}")


def _load_cookies(session):
    """从文件加载完整 cookie jar"""
    if not SESSION_FILE.exists():
        return False
    try:
        with open(SESSION_FILE, "r") as f:
            cookies = json.load(f)
        for c in cookies:
            session.cookies.set(
                name=c["name"],
                value=c["value"],
                domain=c.get("domain", ""),
                path=c.get("path", "/"),
                expires=c.get("expires"),
                secure=c.get("secure", False),
                rest=c.get("rest", {}),
            )
        return True
    except Exception as e:
        print(f"[登录] 加载 Cookie 失败: {e}")
        return False


def do_login():
    """xmulogin 纯 API CAS 登录，返回 (requests.Session, student_id)"""
    session = requests.Session()
    session.headers.update(HEADERS_BASE)

    # 尝试加载已保存的 session
    if _load_cookies(session):
        try:
            r = session.get(f"{BASE_URL}/api/profile", timeout=10)
            if r.status_code == 200 and r.json().get("id"):
                sid = r.json()["id"]
                print(f"[登录] Cookie 有效, student_id={sid}")
                return session, sid
            print("[登录] Cookie 已过期，重新登录...")
        except Exception as e:
            print(f"[登录] Cookie 校验请求失败: {e}，重新登录...")

    # xmulogin 登录（type=3 为 Tronclass）
    print("[登录] xmulogin CAS 登录中...")
    session = xmulogin(type=3, username=STUDENT_ID, password=PASSWORD)
    if not session:
        print("[登录] 登录失败")
        return None, None

    # 获取 student_id
    r = session.get(f"{BASE_URL}/api/profile", timeout=10)
    if r.status_code != 200:
        print("[登录] 获取用户信息失败")
        return None, None
    student_id = r.json()["id"]
    print(f"[登录] 登录成功, student_id={student_id}")

    _save_cookies(session)
    return session, student_id


# ============================================================
# API 工具函数（全部接收 requests.Session）
# ============================================================

def _get_semester(session):
    try:
        r = session.get(f"{BASE_URL}/api/current-semester-info", timeout=5)
        if r.status_code == 200:
            data = r.json()
            return str(data["semester"]["id"]), str(data["academic_year"]["id"])
    except Exception as e:
        print(f"[!] 获取学期信息失败: {e}，使用默认值")
    return "29", "12"


def get_courses(session):
    s_id, y_id = _get_semester(session)
    payload = {
        "conditions": {
            "semester_id": [s_id], "academic_year_id": [y_id],
            "keyword": "", "classify_type": "recently_started",
            "display_studio_list": False,
        },
        "fields": "id,name,display_name",
        "page": 1, "page_size": 30,
        "showScorePassedStatus": False,
    }
    r = session.post(f"{BASE_URL}/api/my-courses", json=payload, timeout=10)
    try:
        data = r.json()
    except Exception:
        return []
    if isinstance(data, list):
        courses = data
    elif "courses" in data:
        courses = data["courses"]
    elif "data" in data:
        courses = data["data"]
    else:
        return []
    seen_cids, unique = set(), []
    for c in courses:
        cid = c.get("id")
        if cid not in seen_cids:
            seen_cids.add(cid)
            unique.append(c)
    return unique


def get_rollcalls(course_id, session, student_id):
    url = f"{BASE_URL}/api/course/{course_id}/student/{student_id}/rollcalls?page=1&page_size=20"
    r = session.get(url, timeout=10)
    data = r.json()
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("rollcalls", data.get("data", []))
    return []


def get_number_code(rollcall_id, session):
    """返回 (number_code, status, end_time) 或 (None, None, None)"""
    url = f"{BASE_URL}/api/rollcall/{rollcall_id}/student_rollcalls"
    try:
        r = session.get(url, timeout=10)
        data = r.json()
        return data.get("number_code"), data.get("status"), data.get("end_time")
    except Exception as e:
        print(f"  [X] 抓取签到码失败: {e}")
        return None, None, None


def answer_number(rollcall_id, code, session):
    url = f"{BASE_URL}/api/rollcall/{rollcall_id}/answer_number_rollcall"
    payload = {"deviceId": str(uuid.uuid4()), "numberCode": str(code)}
    try:
        r = session.put(url, json=payload, timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"  [X] 提交签到失败: {e}")
        return False


def check_rollcall_status(rollcall_id, session):
    """回查签到状态，返回 status 字符串或 None"""
    try:
        r = session.get(
            f"{BASE_URL}/api/rollcall/{rollcall_id}/student_rollcalls",
            timeout=10,
        )
        if r.status_code == 200:
            return r.json().get("status")
    except Exception as e:
        print(f"  [X] 回查状态失败: {e}")
    return None


# ============================================================
# GPS 三角定位（两条线交点反推目标坐标）
# ============================================================

PROBE_LAT1, PROBE_LON1 = 24.3, 118.0
PROBE_LAT2, PROBE_LON2 = 24.6, 118.2


def _latlon_to_xy(lat, lon, lat0, lon0):
    R = 6371000.0
    x = math.radians(lon - lon0) * R * math.cos(math.radians(lat0))
    y = math.radians(lat - lat0) * R
    return x, y


def _xy_to_latlon(x, y, lat0, lon0):
    R = 6371000.0
    lat = lat0 + math.degrees(y / R)
    lon = lon0 + math.degrees(x / (R * math.cos(math.radians(lat0))))
    return lat, lon


def _circle_intersections(x1, y1, d1, x2, y2, d2):
    """两圆交点。返回 ((x1,y1), (x2,y2)) 或 None。

    容忍 GPS 距离舍入误差：两圆若在 tolerance 米内不相交，
    尝试微调 d1/d2 使其相切。
    """
    x1, y1, d1 = float(x1), float(y1), float(d1)
    x2, y2, d2 = float(x2), float(y2), float(d2)
    D = math.hypot(x2 - x1, y2 - y1)

    if D < 1e-9:
        # 同心圆：无法确定方向
        return None

    if D > d1 + d2:
        # 两圆分离，尝试 GPS 误差容忍
        gap = D - (d1 + d2)
        if gap <= GPS_DISTANCE_TOLERANCE:
            # 将两圆半径各扩大 gap/2，使其相切
            adjust = gap / 2.0 + 0.1
            d1 += adjust
            d2 += adjust
        else:
            return None

    if D < abs(d1 - d2):
        # 一圆包含另一圆，尝试容忍
        gap = abs(d1 - d2) - D
        if gap <= GPS_DISTANCE_TOLERANCE:
            # 缩小大圆或扩大小圆使其相切
            if d1 > d2:
                d1 = D + d2 - 0.1
            else:
                d2 = D + d1 - 0.1
        else:
            return None

    a = (d1 ** 2 - d2 ** 2 + D ** 2) / (2 * D)
    h_sq = max(0, d1 ** 2 - a ** 2)
    h = math.sqrt(h_sq)
    xm = x1 + a * (x2 - x1) / D
    ym = y1 + a * (y2 - y1) / D
    rx = -(y2 - y1) * (h / D)
    ry = (x2 - x1) * (h / D)
    return (xm + rx, ym + ry), (xm - rx, ym - ry)


def _solve_target(lat1, lon1, lat2, lon2, d1, d2):
    lat0 = (lat1 + lat2) / 2
    lon0 = (lon1 + lon2) / 2
    x1, y1 = _latlon_to_xy(lat1, lon1, lat0, lon0)
    x2, y2 = _latlon_to_xy(lat2, lon2, lat0, lon0)
    sols = _circle_intersections(x1, y1, d1, x2, y2, d2)
    if sols is None:
        return None
    p1 = _xy_to_latlon(sols[0][0], sols[0][1], lat0, lon0)
    p2 = _xy_to_latlon(sols[1][0], sols[1][1], lat0, lon0)
    return p1, p2


def answer_radar_auto(rollcall_id, session):
    """GPS 签到：两条线三角定位，自动算目标坐标。

    返回 (success, lat, lon)。提交后回查状态确认 on_call_fine。
    """
    url = f"{BASE_URL}/api/rollcall/{rollcall_id}/answer"

    def _payload(lat, lon):
        return {
            "accuracy": 35, "altitude": 0, "altitudeAccuracy": None,
            "deviceId": str(uuid.uuid4()), "heading": None,
            "latitude": str(lat), "longitude": str(lon), "speed": None,
        }

    try:
        r1 = session.put(url, json=_payload(PROBE_LAT1, PROBE_LON1), timeout=10)
        r2 = session.put(url, json=_payload(PROBE_LAT2, PROBE_LON2), timeout=10)
    except Exception as e:
        print(f"  探针请求失败: {e}")
        return False, None, None

    # 探针直接命中（已在范围内）
    for r, lat, lon in [(r1, PROBE_LAT1, PROBE_LON1), (r2, PROBE_LAT2, PROBE_LON2)]:
        if r.status_code == 200:
            status = check_rollcall_status(rollcall_id, session)
            if status == "on_call_fine":
                print(f"  探针命中，状态确认: {status}")
                return True, lat, lon
            print(f"  探针命中但状态为 {status}，尝试精确坐标...")

    try:
        d1 = r1.json().get("distance")
        d2 = r2.json().get("distance")
    except Exception:
        print("  服务器未返回距离信息")
        return False, None, None

    if d1 is None or d2 is None:
        print(f"  未获取到距离: d1={d1}, d2={d2}")
        return False, None, None

    print(f"  探针距离: d1={d1}m, d2={d2}m")

    sols = _solve_target(PROBE_LAT1, PROBE_LON1, PROBE_LAT2, PROBE_LON2, d1, d2)
    if sols is None:
        print("  两圆无交点（已尝试误差容忍）")
        return False, None, None

    (lat_a, lon_a), (lat_b, lon_b) = sols

    for lat, lon in [(lat_a, lon_a), (lat_b, lon_b)]:
        try:
            r = session.put(url, json=_payload(lat, lon), timeout=10)
            if r.status_code == 200:
                status = check_rollcall_status(rollcall_id, session)
                if status == "on_call_fine":
                    print(f"  精确坐标命中，状态确认: {status}")
                    return True, lat, lon
                print(f"  坐标提交 200 但状态为 {status}，尝试下一个...")
        except Exception as e:
            print(f"  候选坐标提交异常: {e}")

    print("  两个候选坐标均未确认成功")
    return False, None, None


# ============================================================
# 辅助
# ============================================================

# 终端状态：签到成功、已结束、已过期、已关闭、已请假
TERMINAL_STATUSES = {"finished", "expired", "closed", "on_call_fine", "on_personal_leave"}

# 明确活跃的状态（可尝试签到）
ACTIVE_STATUSES = {
    "active", "on_going", "on_coming", "started",
    "waiting", "pending", "not_started", "in_progress",
}


def _end_time_passed(end_time):
    """检查 end_time 是否已过"""
    if not end_time:
        return False
    try:
        dt = datetime.fromisoformat(str(end_time).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt < datetime.now(timezone.utc)
    except Exception:
        return False


def _is_terminal(status, end_time):
    """判断签到是否已终止（成功或过期），不应再重试。"""
    if status in TERMINAL_STATUSES:
        return True
    if status in ACTIVE_STATUSES:
        return _end_time_passed(end_time)
    # 未知 status + end_time 已过 → 视为终端
    if end_time and _end_time_passed(end_time):
        return True
    return False


def rollcall_label(rollcall):
    if rollcall.get("is_radar"):
        return "GPS"
    if rollcall.get("is_number"):
        return "数字"
    return "其他"


def clear():
    os.system("cls" if os.name == "nt" else "clear")


def _term_width():
    try:
        return os.get_terminal_size().columns
    except Exception:
        return 80


def _display_width(text):
    """计算字符串在终端中的显示宽度（CJK 字符占 2 列）"""
    w = 0
    for ch in text:
        ea = unicodedata.east_asian_width(ch)
        w += 2 if ea in ("W", "F") else 1
    return w


def _cline(text):
    """居中打印（按显示宽度）"""
    w = _term_width()
    dw = _display_width(text)
    print(" " * max(0, (w - dw) // 2) + text)


def dashboard(courses, seen_count, query_count, start_time, events):
    clear()
    elapsed = int(time.time() - start_time)
    m, s = divmod(elapsed, 60)
    h, m = divmod(m, 60)
    t = time.strftime("%H:%M:%S")
    rt = f"{h}h{m}m{s}s" if h else f"{m}m{s}s"

    sep = "=" * 46

    for line in [
        "",
        "XMU Tronclass 全自动签到",
        sep,
        f"时间: {t}    运行: {rt}",
        f"课程: {len(courses)} 门    查询: {query_count} 次",
        "GPS : 自动三角定位",
        sep,
    ]:
        _cline(line)

    if events:
        for evt in events[-8:]:
            _cline(evt)
    else:
        _cline("(监控中...)")

    _cline(sep)
    _cline("Ctrl+C 退出")


# ============================================================
# 主循环
# ============================================================

def main_loop():
    # 1. 登录
    session, student_id = do_login()
    if not session or not student_id:
        print("[错误] 登录失败")
        return False

    # 2. 获取课程
    courses = get_courses(session)
    if not courses:
        print("[错误] 获取课程列表失败")
        return False
    course_names = {
        c["id"]: c.get("display_name") or c.get("name") or "?"
        for c in courses
    }
    print(f"[课程] 共 {len(courses)} 门")

    # 3. 监控循环
    seen = set()                # 已处理终端 / 已成功签到的 rid
    unknown_retries = {}        # rid → 未知状态重试次数
    events = []
    query_count = 0
    start_time = time.time()
    last_poll = 0

    dashboard(courses, len(seen), query_count, start_time, events)

    while True:
        now = time.time()

        # 每隔 INTERVAL 秒轮询一次课程
        if now - last_poll >= INTERVAL:
            for c in courses:
                cid = c["id"]
                name = course_names.get(cid, "?")
                try:
                    rollcalls = get_rollcalls(cid, session, student_id)
                except Exception as e:
                    print(f"[!] 获取 {name} 签到列表失败: {e}")
                    continue

                query_count += 1

                for rc in rollcalls:
                    rid = rc.get("id") or rc.get("rollcall_id")
                    if not rid or rid in seen:
                        continue

                    # 获取详情
                    if rc.get("is_number") and not rc.get("is_radar"):
                        code, status, end_time = get_number_code(rid, session)
                    elif rc.get("is_radar"):
                        code, status, end_time = None, rc.get("status"), rc.get("end_time")
                    else:
                        code, status, end_time = None, None, None

                    # 终端状态 → 标记已处理，不再重试
                    if _is_terminal(status, end_time):
                        seen.add(rid)
                        continue

                    # API 返回 status=None（详情接口失败）→ 限次重试
                    if status is None:
                        retries = unknown_retries.get(rid, 0)
                        if retries >= MAX_UNKNOWN_RETRIES:
                            seen.add(rid)
                            events.append(
                                f"[GIVEUP] {name} 状态获取连续失败 {retries} 次，放弃"
                            )
                            unknown_retries.pop(rid, None)
                        else:
                            unknown_retries[rid] = retries + 1
                        continue

                    # 非标准 status（不在已知终端/活跃列表）→ 打印并继续尝试签到
                    if status not in TERMINAL_STATUSES and status not in ACTIVE_STATUSES:
                        events.append(
                            f"[?] {name} 未知状态值: '{status}'，仍尝试签到"
                        )

                    # 可以尝试签到
                    rtype = rollcall_label(rc)
                    events.append(f"[NEW] {name} ({rtype})")

                    if rc.get("is_number") and not rc.get("is_radar"):
                        if code:
                            ok = answer_number(rid, code, session)
                            if ok:
                                seen.add(rid)
                                record_ok("数字", name, f"码={code}")
                                events.append(f"[OK] 数字 {name} 码={code}")
                            else:
                                events.append(f"[FAIL] 数字 {name} 提交失败，将重试")
                        else:
                            events.append(f"[!] {name} 签到码暂未获取，将重试")

                    elif rc.get("is_radar"):
                        ok, lat, lon = answer_radar_auto(rid, session)
                        if ok:
                            seen.add(rid)
                            record_ok("GPS", name, f"({lat:.4f}, {lon:.4f})")
                            events.append(f"[OK] GPS {name} ({lat:.4f}, {lon:.4f})")
                        else:
                            events.append(f"[FAIL] GPS {name} 将重试")

                    else:
                        events.append(f"[!] {name} 不支持的签到类型")
                        seen.add(rid)  # 不支持的类型，无法处理

            last_poll = now

        # 限制 events 长度，防止内存泄漏
        if len(events) > 200:
            events = events[-100:]

        dashboard(courses, len(seen), query_count, start_time, events)
        time.sleep(1)


# ============================================================
# 看门狗入口
# ============================================================

def main():
    print()
    print(f"  XMU Tronclass 全自动签到 v1.0")
    print(f"  {'=' * 46}")
    print(f"  学号: {STUDENT_ID}")
    print(f"  登录: xmulogin 纯 API (无浏览器)")
    print(f"  {'=' * 46}")

    restart_count = 0
    restart_times = []

    while True:
        restart_count += 1
        try:
            ok = main_loop()
            if ok is False:
                restart_times.append(time.time())
                restart_times = [t for t in restart_times if time.time() - t < 600]
                if len(restart_times) > 10:
                    print("[看门狗] 10 分钟内失败超过 10 次，放弃。")
                    break
                print(f"[看门狗] 60s 后重试 (第 {restart_count} 次)...")
                time.sleep(60)
                # 仅登录失败时清除 session（网络故障不删）
                if SESSION_FILE.exists():
                    SESSION_FILE.unlink()
                    print("[看门狗] 已清除过期 Session")
            else:
                # main_loop 正常永不返回；到这里说明异常退出
                break
        except KeyboardInterrupt:
            print("\n[看门狗] 用户中断，退出。")
            break
        except Exception as e:
            print(f"[看门狗] 崩溃: {e}")
            restart_times.append(time.time())
            restart_times = [t for t in restart_times if time.time() - t < 600]
            if len(restart_times) > 10:
                print("[看门狗] 10 分钟内崩溃超过 10 次，放弃。")
                break
            print(f"[看门狗] 5s 后重启 (第 {restart_count} 次)...")
            time.sleep(5)


if __name__ == "__main__":
    main()
