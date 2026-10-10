# -*- coding: utf-8 -*-
"""
XMU-AC 签到控制台
- 查看进程状态 / 最近签到事件
- 启动 / 停止签到进程
用法: python control_panel.py   (双击桌面快捷方式即可)
"""
import json
import os
import re
import signal
import subprocess
import sys
import time

if sys.platform == "win32":
    import msvcrt
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
LOG_FILE = BASE / "checkin.log"
PID_FILE = BASE / "auto_checkin.pid"
RECORD_FILE = BASE / "checkin_record.json"
PYTHON = BASE / ".venv" / "Scripts" / "python.exe"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# Windows 控制台切到 UTF-8，保证中文不乱码
if sys.platform == "win32":
    try:
        os.system("chcp 65001 >nul")
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def now():
    return datetime.now().strftime("%H:%M:%S")


def find_running():
    """返回正在运行的 auto_checkin python PID，找不到返回 None"""
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            if _pid_alive(pid):
                return pid
        except Exception:
            pass
    # 扫描系统进程兜底
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' "
             "-and $_.CommandLine -like '*auto_checkin*' } | Select-Object -ExpandProperty ProcessId"],
            capture_output=True, text=True, timeout=20,
        ).stdout
        for line in out.splitlines():
            line = line.strip()
            if line.isdigit():
                return int(line)
    except Exception:
        pass
    return None


def _pid_alive(pid):
    out = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}"],
        capture_output=True, text=True, timeout=15,
    ).stdout
    return str(pid) in out


def _load_creds():
    """读取 creds.env 里的 STUDENT_ID / PASSWORD，不存在返回 {}"""
    cfg = {}
    f = BASE / "creds.env"
    if f.exists():
        for ln in f.read_text(encoding="utf-8", errors="replace").splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                cfg[k.strip()] = v.strip()
    return cfg


def start():
    """后台启动签到进程（注入 creds.env 凭据），输出写入 checkin.log"""
    if find_running():
        return "已经在运行，无需重复启动"
    env = os.environ.copy()
    env.update(_load_creds())
    if not env.get("STUDENT_ID") or not env.get("PASSWORD"):
        return "缺少账号: 请编辑同目录 creds.env 填入 STUDENT_ID / PASSWORD"
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    logf = open(LOG_FILE, "a", encoding="utf-8")
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    p = subprocess.Popen(
        [str(PYTHON), "-u", "auto_checkin.py"],
        cwd=str(BASE), stdin=subprocess.DEVNULL,
        stdout=logf, stderr=subprocess.STDOUT,
        env=env, creationflags=flags,
    )
    PID_FILE.write_text(str(p.pid))
    return f"已启动 (PID {p.pid})"


def stop():
    pid = find_running()
    if not pid:
        return "进程未在运行"
    subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, text=True)
    if PID_FILE.exists():
        PID_FILE.unlink()
    return f"已停止 (PID {pid})"


def tail_events(n=12):
    """从日志里挑出签到事件行（形如 [OK] xxx），取最近 n 条"""
    if not LOG_FILE.exists():
        return ["(尚无日志，启动后才会产生)"]
    try:
        raw = LOG_FILE.read_bytes()
        try:
            lines = raw.decode("utf-8").splitlines()
        except UnicodeDecodeError:
            lines = raw.decode("gbk", errors="replace").splitlines()
    except Exception:
        return ["(日志读取失败)"]
    events = []
    for raw in lines:
        line = ANSI_RE.sub("", raw).strip()
        # 事件行以 [ 开头；dashboard 整屏刷屏的行不匹配
        if line.startswith("[") and "监控中" not in line:
            events.append(line)
    if not events:
        return ["(运行中，暂无签到事件)"]
    return events[-n:]


def today_done():
    """今日成功签到，按课程汇总。返回 {课程: {n, types:{数字:次数}, last:'HH:MM:SS'}}"""
    if not RECORD_FILE.exists():
        return {}
    try:
        recs = json.loads(RECORD_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    today = datetime.now().strftime("%Y-%m-%d")
    by = {}
    for r in recs:
        if not str(r.get("time", "")).startswith(today):
            continue
        c = r.get("course") or "?"
        e = by.setdefault(c, {"types": {}, "n": 0, "last": ""})
        t = r.get("type") or "?"
        e["types"][t] = e["types"].get(t, 0) + 1
        e["n"] += 1
        e["last"] = str(r.get("time", ""))[11:]
    return by


def status_line():
    pid = find_running()
    if pid:
        pid_file_mark = "（本面板启动）" if PID_FILE.exists() else ""
        return f"[运行中] PID {pid} {pid_file_mark}"
    return "[未运行]"


def show():
    os.system("cls")
    W = 60
    print("=" * W)
    print("  XMU 畅课全自动签到 - 控制台".center(W - 4))
    print("=" * W)
    print(f"  状态: {status_line()}   时间: {now()}")
    print("-" * W)
    today = today_done()
    if today:
        n = sum(e["n"] for e in today.values())
        print(f"  今日已签到 {n} 次:")
        for c in sorted(today):
            e = today[c]
            types = "  ".join(f"{k}×{v}" for k, v in e["types"].items())
            print(f"    ✓ {c}  ({types})  最近 {e['last']}")
    else:
        print("  今日已签到: (暂无，监控中...)")
    print("-" * W)
    for e in tail_events(6):
        print("  · " + e)
    print("-" * W)
    print("  [1] 启动/重启    [2] 停止    [0] 退出   (5秒自动刷新)")
    print("=" * W)


def wait_key(timeout=5.0):
    """等一个按键；超时返回 None（用于自动刷新）。"""
    end = time.time() + timeout
    while time.time() < end:
        if msvcrt.kbhit():
            return msvcrt.getwch()
        time.sleep(0.2)
    return None


def main():
    while True:
        show()
        c = wait_key()
        if c is None:
            continue            # 超时无按键 → 刷新
        if c == "1":
            print(">>", start())
            time.sleep(1.2)
        elif c == "2":
            print(">>", stop())
            time.sleep(1.2)
        elif c == "0":
            break


if __name__ == "__main__":
    main()
