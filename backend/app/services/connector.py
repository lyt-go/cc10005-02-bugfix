"""连接器管理业务规则：状态流转、字段校验与筛选口径都收在这里。

接入准入判定口径（规则与阈值）
================================

状态机（只允许单向推进，「已离线」为终态）::

    草稿 → 连通测试中 → 已就绪 → 已离线

允许范围（动作 → 允许的前置状态）：

- 发起连通测试：仅「草稿」「连通测试中」。在「连通测试中」再次发起视为重新测试
  （重新测试只允许这两个状态）；「已就绪」「已离线」不允许退回测试中。
- 标记就绪：仅「连通测试中」。
- 标记离线：仅「连通测试中」「已就绪」；「草稿」未进入接入流程无需离线。

上限与触发条件：

- 超时阈值允许范围 1–60 秒，未配置或无法解析时按默认 5 秒计；
  登记时显式给值但越界或不是数字的，直接拒绝登记（未达标准不允许保存）。
- 标记就绪触发准入校验：最近一次连通测试结论必须为「通过」，
  且通过结论距标记动作不超过 600 秒（有效期上限），过期必须重新测试。
- 同一连接器连通测试次数上限 5 次（通过+拒绝合计），达到上限拒绝继续测试。
- 同一连接器同一动作 3 秒内重复提交视为重复点击，直接拒绝；
  配合状态机校验，保证两个操作入口同时提交同一连接器时只有一个成功。

未达标准时不允许保存：以上任一校验失败，连接器状态与代理绑定关系一律不写入，
只追加一条脱敏后的审计记录（校验后的状态说明 + 端点摘要，不含认证参数）。

一致性：连接器写入与代理分配/释放在同一把锁的临界区内完成，先校验后写入，
任何一步失败都不落地，不会留下半套接入准入结论。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from app.store import store

MODULE = "connector"
PROXY_MODULE = "proxypool"
AUDIT_MODULE = "audittrail"
REQUIRED_FIELDS = ["连接器编号", "协议类型", "目标端点"]

STATUS_ORDER = ["草稿", "连通测试中", "已就绪", "已离线"]
TERMINAL_STATUS = "已离线"

# 动作 → (允许的前置状态集合, 目标状态)：单向推进，不允许倒退
ACTION_RULES: dict[str, tuple[set[str], str]] = {
    "发起连通测试": ({"草稿", "连通测试中"}, "连通测试中"),
    "标记就绪": ({"连通测试中"}, "已就绪"),
    "标记离线": ({"连通测试中", "已就绪"}, "已离线"),
}

# ---- 判定口径：阈值与上限 ----
TIMEOUT_MIN_SECONDS = 1          # 超时阈值允许范围下限（秒）
TIMEOUT_MAX_SECONDS = 60         # 超时阈值允许范围上限（秒）
DEFAULT_TIMEOUT_SECONDS = 5      # 未配置超时阈值时的默认口径（秒）
READY_RESULT_TTL_SECONDS = 600   # 测试通过结论有效期上限（秒）
MAX_TEST_ATTEMPTS = 5            # 同一连接器连通测试次数上限
DUPLICATE_WINDOW_SECONDS = 3.0   # 同一动作重复提交的去重窗口（秒）

# 联调三种拒绝依据
REJECT_PROXY_UNREACHABLE = "代理不可达"
REJECT_AUTH_FAILED = "认证失败"
REJECT_ENDPOINT_TIMEOUT = "端点超时"

AUTH_FAIL_MARKERS = ("expired", "invalid", "失效", "作废")

# 连接器写入与代理分配/释放必须在同一临界区完成：一把锁保证一致性，
# 同时保证两个入口同时提交同一连接器时只有一个成功。
_STATE_LOCK = threading.RLock()
# 每个连接器最近一次成功动作（用于重复点击去重）：entry_id -> (action, monotonic_ts)
_LAST_ACTIONS: dict[int, tuple[str, float]] = {}


def _now() -> datetime:
    return datetime.now().astimezone()


def _now_text() -> str:
    return _now().isoformat(timespec="seconds")


def _endpoint_summary(endpoint: str) -> str:
    """端点摘要：只保留 scheme://host/path，去掉账号、查询串等可能带敏感信息的部分。"""
    text = str(endpoint or "").strip()
    if not text:
        return "（未配置端点）"
    try:
        parts = urlsplit(text if "://" in text else f"//{text}")
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        summary = urlunsplit((parts.scheme, host, parts.path.rstrip("/"), "", ""))
        return summary or text[:80]
    except ValueError:
        return text[:80]


def _timeout_seconds(entry: dict[str, Any]) -> float:
    raw = str(entry.get("超时阈值") or "").strip()
    if not raw:
        return float(DEFAULT_TIMEOUT_SECONDS)
    try:
        return float(raw)
    except ValueError:
        return float(DEFAULT_TIMEOUT_SECONDS)


def _simulate_latency_ms(endpoint: str) -> int:
    """演示环境不做真实网络探测：用端点字符串派生 100–1599ms 的确定性耗时。"""
    return 100 + sum(ord(ch) for ch in str(endpoint)) % 1500


def _find_proxy(instance_no: str) -> dict[str, Any] | None:
    for row in store.rows(PROXY_MODULE):
        if str(row.get("实例编号", "")) == instance_no:
            return row
    return None


def _snapshot_text(snapshot: dict[str, Any]) -> str:
    parts = [
        f"端点快照：{snapshot.get('端点摘要')}",
        f"协议：{snapshot.get('协议类型')}",
        f"超时阈值：{snapshot.get('超时阈值秒')}s",
        f"绑定代理：{snapshot.get('绑定代理')}",
    ]
    if snapshot.get("探测耗时毫秒") is not None:
        parts.append(f"探测耗时：{snapshot['探测耗时毫秒']}ms")
    return "；".join(parts)


def _evaluate_connectivity(entry: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    """连通测试口径：依次校验代理可达、认证有效、端点不超时。

    返回 (是否通过, 拒绝依据, 端点快照)。快照只含脱敏后的端点摘要，不含认证参数。
    """
    endpoint = str(entry.get("目标端点") or "").strip()
    snapshot: dict[str, Any] = {
        "端点摘要": _endpoint_summary(endpoint),
        "协议类型": str(entry.get("协议类型") or "未配置"),
        "超时阈值秒": _timeout_seconds(entry),
        "绑定代理": str(entry.get("绑定代理") or "").strip() or "未绑定",
    }
    # 1) 代理可达性：必须绑定代理实例，且实例当前状态为「可用」
    proxy = _find_proxy(snapshot["绑定代理"]) if snapshot["绑定代理"] != "未绑定" else None
    if proxy is None or proxy.get("status") != "可用":
        return False, REJECT_PROXY_UNREACHABLE, snapshot
    # 2) 认证校验：认证参数为空或带失效标记 → 认证失败（参数本身不进快照/审计）
    auth = str(entry.get("认证参数") or "").strip()
    if not auth or any(marker in auth.lower() for marker in AUTH_FAIL_MARKERS):
        return False, REJECT_AUTH_FAILED, snapshot
    # 3) 端点超时：模拟探测耗时超过超时阈值 → 端点超时
    latency_ms = _simulate_latency_ms(endpoint)
    snapshot["探测耗时毫秒"] = latency_ms
    if latency_ms > float(snapshot["超时阈值秒"]) * 1000:
        return False, REJECT_ENDPOINT_TIMEOUT, snapshot
    return True, "", snapshot


def _append_history(
    entry: dict[str, Any],
    *,
    action: str,
    result: str,
    reason: str = "",
    detail: str = "",
) -> None:
    """历史事件只追加不删除：离线事件也不得吞掉已完成的测试结果。"""
    history = entry.setdefault("history", [])
    history.append({
        "时间": _now_text(),
        "动作": action,
        "结果": result,
        "依据": reason,
        "说明": detail,
        "状态": entry.get("status"),
    })


def _append_audit(*, action_type: str, target: str, result: str, detail: str) -> None:
    """审计记录：只保存校验后的状态说明和端点摘要，绝不写入认证参数。"""
    rows = store.rows(AUDIT_MODULE)
    next_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
    rows.append({
        "id": next_id,
        "status": "已记录",
        "pending": False,
        "abnormal": result != "通过",
        "记录编号": f"AUDI-{next_id:04d}",
        "操作类型": action_type,
        "操作对象": target,
        "操作人": "系统",
        "操作时间": _now_text(),
        "操作IP": "127.0.0.1",
        "变更详情": detail,
        "操作结果": result,
    })


class ConnectorService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("连接器编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, [f"缺少必填字段：{'、'.join(missing)}"]
        timeout_raw = str(values.get("超时阈值") or "").strip()
        if timeout_raw:
            try:
                timeout_value = float(timeout_raw)
            except ValueError:
                return None, [f"超时阈值必须是数字（允许范围 {TIMEOUT_MIN_SECONDS}–{TIMEOUT_MAX_SECONDS} 秒），未达标准不允许保存"]
            if not TIMEOUT_MIN_SECONDS <= timeout_value <= TIMEOUT_MAX_SECONDS:
                return None, [f"超时阈值超出允许范围（{TIMEOUT_MIN_SECONDS}–{TIMEOUT_MAX_SECONDS} 秒），未达标准不允许保存"]
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for optional in ("认证参数", "重连次数", "心跳间隔", "数据编码", "超时阈值"):
            if values.get(optional) is not None:
                entry[optional] = values.get(optional)
        entry["绑定代理"] = str(values.get("绑定代理") or "").strip()
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["最近测试结果"] = None
        entry["history"] = []
        _append_history(entry, action="登记", result="通过", detail="连接器登记，初始状态「草稿」")
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        with _STATE_LOCK:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"数据连接器 {entry_id} 不存在或已归档"
            if action not in ACTION_RULES:
                return None, f"动作「{action}」不属于连接器管理可执行范围"
            allowed_sources, _target = ACTION_RULES[action]
            current = str(entry.get("status") or "")
            if current not in allowed_sources:
                return None, (
                    f"当前状态「{current}」不允许执行「{action}」：接入流程只能按 "
                    f"草稿→连通测试中→已就绪→已离线 单向推进，已离线为终态；未保存状态变更"
                )
            now = time.monotonic()
            last = _LAST_ACTIONS.get(entry_id)
            if last and last[0] == action and now - last[1] < DUPLICATE_WINDOW_SECONDS:
                return None, (
                    f"同一连接器 {int(DUPLICATE_WINDOW_SECONDS)} 秒内重复提交「{action}」，"
                    "已按重复点击忽略，避免重复状态变化或重复解绑"
                )
            handler = {
                "发起连通测试": self._handle_test,
                "标记就绪": self._handle_ready,
                "标记离线": self._handle_offline,
            }[action]
            result, message = handler(entry)
            if result is not None:
                _LAST_ACTIONS[entry_id] = (action, now)
            return result, message

    # ---- 三个动作的处理函数：都在 _STATE_LOCK 临界区内先校验后写入 ----

    def _handle_test(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        code = str(entry.get("连接器编号") or entry.get("id"))
        history = entry.setdefault("history", [])
        attempts = sum(1 for item in history if item.get("动作") == "发起连通测试")
        if attempts >= MAX_TEST_ATTEMPTS:
            message = f"连通测试次数已达上限（{MAX_TEST_ATTEMPTS} 次），请核对配置后重新登记；未保存状态变更"
            _append_history(entry, action="发起连通测试", result="拒绝", reason="超过次数上限", detail=message)
            _append_audit(action_type="连接器连通测试", target=code, result="拒绝：超过次数上限",
                          detail=f"状态说明：当前状态「{entry.get('status')}」；{message}")
            return None, message
        passed, reason, snapshot = _evaluate_connectivity(entry)
        snapshot_text = _snapshot_text(snapshot)
        if not passed:
            entry["abnormal"] = True
            entry["最近测试结果"] = {"结论": "拒绝", "依据": reason, "时间": _now_text(),
                                    "时间戳": time.time(), "端点快照": snapshot}
            message = f"连通测试未通过：{reason}；状态保持「{entry.get('status')}」，未保存状态变更"
            _append_history(entry, action="发起连通测试", result="拒绝", reason=reason, detail=snapshot_text)
            _append_audit(action_type="连接器连通测试", target=code, result=f"拒绝：{reason}",
                          detail=f"状态说明：当前状态「{entry.get('status')}」；{snapshot_text}")
            return None, message
        previous = str(entry.get("status"))
        entry["status"] = "连通测试中"
        entry["pending"] = True
        entry["abnormal"] = False
        entry["最近测试结果"] = {"结论": "通过", "依据": "", "时间": _now_text(),
                                "时间戳": time.time(), "端点快照": snapshot}
        _append_history(entry, action="发起连通测试", result="通过",
                        detail=f"状态迁移：{previous}→连通测试中；{snapshot_text}")
        _append_audit(action_type="连接器连通测试", target=code, result="通过",
                      detail=f"状态迁移：{previous}→连通测试中；{snapshot_text}")
        return entry, f"连通测试通过，连接器已进入「连通测试中」（{snapshot_text}）"

    def _handle_ready(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        code = str(entry.get("连接器编号") or entry.get("id"))
        last = entry.get("最近测试结果") or {}
        if last.get("结论") != "通过":
            detail_reason = last.get("依据") or "尚无连通测试结论"
            message = f"标记就绪未达准入标准：最近一次连通测试结论不是「通过」（{detail_reason}）；未保存状态变更"
            _append_history(entry, action="标记就绪", result="拒绝", reason="无有效通过结论", detail=message)
            _append_audit(action_type="连接器准入校验", target=code, result="拒绝：无有效通过结论",
                          detail=f"状态说明：当前状态「{entry.get('status')}」；{message}")
            return None, message
        age = time.time() - float(last.get("时间戳") or 0)
        if age > READY_RESULT_TTL_SECONDS:
            message = (f"标记就绪未达准入标准：连通测试通过结论已过期"
                       f"（有效期上限 {READY_RESULT_TTL_SECONDS // 60} 分钟），请重新发起连通测试；未保存状态变更")
            _append_history(entry, action="标记就绪", result="拒绝", reason="通过结论已过期", detail=message)
            _append_audit(action_type="连接器准入校验", target=code, result="拒绝：通过结论已过期",
                          detail=f"状态说明：当前状态「{entry.get('status')}」；{message}")
            return None, message
        proxy_no = str(entry.get("绑定代理") or "").strip()
        proxy = _find_proxy(proxy_no) if proxy_no else None
        if proxy is None or proxy.get("status") != "可用":
            message = (f"标记就绪未达准入标准：{REJECT_PROXY_UNREACHABLE}"
                       f"（绑定代理 {proxy_no or '未绑定'} 不是「可用」状态）；未保存状态变更")
            _append_history(entry, action="标记就绪", result="拒绝", reason=REJECT_PROXY_UNREACHABLE, detail=message)
            _append_audit(action_type="连接器准入校验", target=code, result=f"拒绝：{REJECT_PROXY_UNREACHABLE}",
                          detail=f"状态说明：当前状态「{entry.get('status')}」；{message}")
            return None, message
        holder = str(proxy.get("绑定连接器") or "").strip()
        if holder and holder != code:
            message = f"标记就绪未达准入标准：绑定代理 {proxy_no} 已被 {holder} 占用；未保存状态变更"
            _append_history(entry, action="标记就绪", result="拒绝", reason="代理被占用", detail=message)
            _append_audit(action_type="连接器准入校验", target=code, result="拒绝：代理被占用",
                          detail=f"状态说明：当前状态「{entry.get('status')}」；{message}")
            return None, message
        # 全部校验通过：同一临界区内同时写连接器与代理，失败即整体不落地
        previous = str(entry.get("status"))
        entry["status"] = "已就绪"
        entry["pending"] = False
        entry["abnormal"] = False
        proxy["绑定连接器"] = code
        detail = f"状态迁移：{previous}→已就绪；代理实例 {proxy_no} 已分配给 {code}"
        _append_history(entry, action="标记就绪", result="通过", detail=detail)
        _append_audit(action_type="连接器准入校验", target=code, result="通过", detail=detail)
        return entry, f"数据连接器已标记就绪，代理实例 {proxy_no} 已分配给 {code}"

    def _handle_offline(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        code = str(entry.get("连接器编号") or entry.get("id"))
        proxy_no = str(entry.get("绑定代理") or "").strip()
        proxy = _find_proxy(proxy_no) if proxy_no else None
        # 离线前若仍绑定可用代理实例，必须给出明确的解绑结果；同一临界区内释放
        if proxy is not None and proxy.get("status") == "可用" and str(proxy.get("绑定连接器") or "").strip() == code:
            proxy["绑定连接器"] = ""
            unbind_text = f"已解绑代理实例 {proxy_no}（绑定关系已释放）"
        elif proxy_no:
            unbind_text = f"绑定代理 {proxy_no} 当前无有效占用，无需解绑"
        else:
            unbind_text = "未绑定代理实例，无需解绑"
        previous = str(entry.get("status"))
        entry["status"] = TERMINAL_STATUS
        entry["pending"] = False
        entry["abnormal"] = False
        detail = f"状态迁移：{previous}→已离线；{unbind_text}；历史测试结果保留可追溯"
        _append_history(entry, action="标记离线", result="通过", detail=detail)
        _append_audit(action_type="连接器离线", target=code, result="通过", detail=detail)
        return entry, f"数据连接器已标记离线；{unbind_text}；历史测试结果保留可追溯"
