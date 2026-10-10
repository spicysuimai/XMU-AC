# XMU Tronclass 全自动签到

厦门大学畅课（Tronclass）全自动签到脚本。支持数字签到和 GPS 签到（三角定位自动算坐标）。

基于 [ch-z-hc/XMU-AC](https://github.com/ch-z-hc/XMU-AC) 的分支，吸收上游修复之外，额外包含：

- **签到门控**：等班里已签人数超过全班 1/4 或超过 20 人才签（避免总是第一个签）；
  保底：拿不到名单 / 等待超 120s / 距结束不足 30s 直接签。
- **数字码双通道取码**（2026-10 服务端改动适配）：数字码优先从点名详情接口取；
  取不到时自动改走课表日期接口 `GET /api/timetable_rollcalls`（按点名时间的 UTC 日期查询）。
- **防环境代理劫持**：会话强制直连（`trust_env=False`），避免残留的失效系统代理
  把请求拦成 ProxyError（曾导致重启后连登录都失败）。
- **Cookie 载入修复**：忽略快照里的过期时间戳（服务端滑动续期，是否存在由服务端判定）。
- **控制面板** `control_panel.py`：查看状态 / 最近签到事件 / 启停签到进程。
- **看门狗** `xmu_watchdog.bat` + 计划任务：进程崩溃 15 秒自动拉起。

## 使用

```powershell
# 1. 安装依赖
pip install -r requirements.txt

# 2. 运行
python auto_checkin.py
```

首次运行会提示输入学号和密码，后续通过 Cookie 持久化自动登录。也可通过环境变量传入：

```powershell
$env:STUDENT_ID = "学号"
$env:PASSWORD = "密码"
python auto_checkin.py
```

### 离线测试

```powershell
python tests/test_gate.py   # 32 项门控/计数/状态判定用例，不联网
```

## 接口口径备忘（实测）

- 课程列表 `GET /api/course/{cid}/student/{sid}/rollcalls`：`status` 是**本人**状态
  （未签 = absent、签上 = on_call_fine）；`end_time` 恒为 None。
- 点名详情 `GET /api/rollcall/{id}/student_rollcalls`：顶层 `status` 是**点名本身**
  （in_progress / finished）；名单里 `rollcall_status == on_call_fine` 才计入已签人数。
  ⚠️ 2026-10-10 起该接口的 `number_code` 恒为 null（服务端不再下发）。
- **数字码**：`GET /api/timetable_rollcalls?course_ids={cid}&rollcall_date={YYYY-MM-DD}`，
  `rollcall_date` 取点名时间（`rollcall_time`）的 **UTC 日期**；返回记录内含
  `number_code`，点名结束后仍保留。参数须为单值/重复参数，数组形式返回空。
- QR 签到（`type=qr_rollcall`）无法自动化：token 为服务端密钥哈希，学生端不提供接口。

## 声明

本项目仅供技术交流与学习，请勿用于非法用途。
