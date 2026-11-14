"""连接器状态流转与代理准入的端到端核对脚本。

核对对象：CONN-0001、CONN-0003、PROX-0003。
覆盖：违规跳转被拒绝、正确路径可完成、并发只有一个成功、重复点击不产生
重复状态变化/重复解绑、三种拒绝依据、审计端点快照与认证参数脱敏、
离线不吞历史测试结果、列表/详情返回新值。

用法：先启动后端（uvicorn app.main:app --port 8000），再运行本脚本。
"""
from __future__ import annotations

import json
import threading
import urllib.request

BASE = "http://127.0.0.1:8000"
PASS = "\033[32m通过\033[0m"
FAIL = "\033[31m失败\033[0m"

failures: list[str] = []


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  [{PASS if cond else FAIL}] {name}{(' —— ' + extra) if extra else ''}")
    if not cond:
        failures.append(name)


def action(entry_id: int, act: str) -> dict:
    _, payload = call("POST", f"/api/connector/{entry_id}/actions", {"values": {"action": act}})
    return payload


def get(path: str) -> dict:
    _, payload = call("GET", path)
    return payload


print("== 1. CONN-0001 草稿：标记就绪属违规跳转，必须被拒绝且不保存 ==")
entry = get("/api/connector/1")
check("CONN-0001 初始为草稿", entry["status"] == "草稿", entry["status"])
result = action(1, "标记就绪")
check("草稿→标记就绪 被拒绝", result["ok"] is False, result["message"])
check("拒绝说明给出单向推进口径", "单向推进" in result["message"], result["message"])
entry = get("/api/connector/1")
check("拒绝后状态仍是草稿（未保存）", entry["status"] == "草稿", entry["status"])

print("== 2. CONN-0001 正确路径：草稿→连通测试→就绪→离线，PROX-0003 参与准入 ==")
result = action(1, "发起连通测试")
check("草稿→发起连通测试 通过", result["ok"] is True, result["message"])
check("状态进入连通测试中", result["entry"]["status"] == "连通测试中")
result = action(1, "标记就绪")
check("测试通过后标记就绪 成功", result["ok"] is True, result["message"])
check("返回消息含代理分配结果", "PROX-0003" in result["message"], result["message"])
proxy = get("/api/proxypool/3")
check("PROX-0003 已分配给 CONN-0001（两侧一致）", proxy.get("绑定连接器") == "CONN-0001",
      f"绑定连接器={proxy.get('绑定连接器')!r}")
result = action(1, "标记离线")
check("已就绪→标记离线 成功", result["ok"] is True, result["message"])
check("离线产生明确解绑结果", "解绑" in result["message"] and "PROX-0003" in result["message"],
      result["message"])
proxy = get("/api/proxypool/3")
check("PROX-0003 绑定已释放（分配释放一致）", proxy.get("绑定连接器") == "",
      f"绑定连接器={proxy.get('绑定连接器')!r}")
entry = get("/api/connector/1")
check("离线后历史测试结果仍保留（未吞掉）",
      any(h.get("动作") == "发起连通测试" and h.get("结果") == "通过" for h in entry["history"])
      and entry["最近测试结果"]["结论"] == "通过")

print("== 3. 终态与倒退防护：已离线不可再流转；已就绪不可退回测试中 ==")
result = action(1, "标记离线")
check("已离线重复标记离线 被拒绝（不产生重复解绑）", result["ok"] is False, result["message"])
result = action(1, "发起连通测试")
check("已离线发起测试 被拒绝", result["ok"] is False, result["message"])
result = action(3, "发起连通测试")
check("CONN-0003 已就绪→发起连通测试 被拒绝（状态不可倒退）", result["ok"] is False, result["message"])
entry = get("/api/connector/3")
check("CONN-0003 仍是已就绪（未保存倒退）", entry["status"] == "已就绪", entry["status"])
result = action(3, "标记就绪")
check("CONN-0003 已就绪→标记就绪 被拒绝（不在允许范围）", result["ok"] is False, result["message"])

print("== 4. CONN-0003 正确路径：已就绪→离线（历史遗留未绑定代理） ==")
result = action(3, "标记离线")
check("CONN-0003 标记离线 成功", result["ok"] is True, result["message"])
check("无绑定代理时解绑结果明确", "无需解绑" in result["message"], result["message"])

print("== 5. 三种拒绝依据：代理不可达 / 认证失败 / 端点超时 ==")
# 代理不可达：绑定草稿态代理 PROX-0001
_, created = call("POST", "/api/connector", {"values": {
    "连接器编号": "CONN-T001", "协议类型": "HTTPS",
    "目标端点": "https://t1.example.com/x", "认证参数": "token:ok",
    "超时阈值": "5", "绑定代理": "PROX-0001"}})
cid = created["entry"]["id"]
result = action(cid, "发起连通测试")
check("绑定非可用代理 → 代理不可达", result["ok"] is False and "代理不可达" in result["message"],
      result["message"])
check("拒绝后状态保持草稿", get(f"/api/connector/{cid}")["status"] == "草稿")
# 认证失败：CONN-0002 认证参数 token:expired（连通测试中允许重新测试）
result = action(2, "发起连通测试")
check("CONN-0002 重新测试 → 认证失败", result["ok"] is False and "认证失败" in result["message"],
      result["message"])
result = action(2, "标记就绪")
check("无有效通过结论 → 标记就绪被拒绝", result["ok"] is False and "准入标准" in result["message"],
      result["message"])
# 端点超时：构造探测耗时 > 超时阈值的端点（耗时 = 100 + sum(ord) % 1500，阈值 1s）
base_endpoint = "https://slow.example.com/"
pad = 0
while 100 + sum(ord(c) for c in base_endpoint + "s" * pad) % 1500 <= 1000:
    pad += 1
endpoint = base_endpoint + "s" * pad
latency = 100 + sum(ord(c) for c in endpoint) % 1500
_, created = call("POST", "/api/connector", {"values": {
    "连接器编号": "CONN-T002", "协议类型": "HTTPS",
    "目标端点": endpoint, "认证参数": "token:ok",
    "超时阈值": "1", "绑定代理": "PROX-0003"}})
cid = created["entry"]["id"]
result = action(cid, "发起连通测试")
check(f"探测耗时 {latency}ms > 阈值 1000ms → 端点超时",
      result["ok"] is False and "端点超时" in result["message"], result["message"])

print("== 6. 登记口径：超时阈值越界不允许保存 ==")
_, created = call("POST", "/api/connector", {"values": {
    "连接器编号": "CONN-T003", "协议类型": "HTTPS",
    "目标端点": "https://t3.example.com/x", "超时阈值": "99"}})
check("超时阈值 99s 超出 1–60 上限 → 登记被拒绝", created["ok"] is False, created["message"])
_, created = call("POST", "/api/connector", {"values": {
    "连接器编号": "CONN-T004", "协议类型": "HTTPS",
    "目标端点": "https://t4.example.com/x", "超时阈值": "abc"}})
check("超时阈值非数字 → 登记被拒绝", created["ok"] is False, created["message"])

print("== 7. 并发与重复点击：两个入口同时提交只有一个成功 ==")
_, created = call("POST", "/api/connector", {"values": {
    "连接器编号": "CONN-T005", "协议类型": "HTTPS",
    "目标端点": "https://t5.example.com/x", "认证参数": "token:ok",
    "超时阈值": "5", "绑定代理": "PROX-0003"}})
cid = created["entry"]["id"]
results: list[dict] = []
t1 = threading.Thread(target=lambda: results.append(action(cid, "发起连通测试")))
t2 = threading.Thread(target=lambda: results.append(action(cid, "发起连通测试")))
t1.start(); t2.start(); t1.join(); t2.join()
ok_count = sum(1 for r in results if r["ok"])
check("并发发起连通测试只有一个成功", ok_count == 1, f"成功数={ok_count}")
check("另一个被明确拒绝", any("重复" in r["message"] or "单向" in r["message"] for r in results if not r["ok"]))
entry = get(f"/api/connector/{cid}")
test_events = [h for h in entry["history"] if h.get("动作") == "发起连通测试" and h.get("结果") == "通过"]
check("只产生一次状态变化（测试通过事件仅一条）", len(test_events) == 1, f"通过事件={len(test_events)}")
results = []
t1 = threading.Thread(target=lambda: results.append(action(cid, "标记就绪")))
t2 = threading.Thread(target=lambda: results.append(action(cid, "标记就绪")))
t1.start(); t2.start(); t1.join(); t2.join()
ok_count = sum(1 for r in results if r["ok"])
check("并发标记就绪只有一个成功", ok_count == 1, f"成功数={ok_count}")
proxy = get("/api/proxypool/3")
check("PROX-0003 只被分配一次", proxy.get("绑定连接器") == "CONN-T005",
      f"绑定连接器={proxy.get('绑定连接器')!r}")
results = []
t1 = threading.Thread(target=lambda: results.append(action(cid, "标记离线")))
t2 = threading.Thread(target=lambda: results.append(action(cid, "标记离线")))
t1.start(); t2.start(); t1.join(); t2.join()
ok_count = sum(1 for r in results if r["ok"])
check("并发标记离线只有一个成功（不重复解绑）", ok_count == 1, f"成功数={ok_count}")
entry = get(f"/api/connector/{cid}")
unbind_events = [h for h in entry["history"] if h.get("动作") == "标记离线" and h.get("结果") == "通过"]
check("解绑只发生一次", len(unbind_events) == 1, f"离线事件={len(unbind_events)}")

print("== 8. 审计：每次测试留端点快照，拒绝记录不含认证参数 ==")
audit = get("/api/audittrail?size=200")
records = [r for r in audit["items"] if str(r.get("操作对象", "")).startswith("CONN-")]
check("存在连接器连通测试审计", any(r.get("操作类型") == "连接器连通测试" for r in records))
check("存在连接器准入校验审计", any(r.get("操作类型") == "连接器准入校验" for r in records))
check("存在连接器离线审计", any(r.get("操作类型") == "连接器离线" for r in records))
test_records = [r for r in records if r.get("操作类型") == "连接器连通测试"]
check("每次连通测试审计都带端点快照", all("端点快照" in str(r.get("变更详情", "")) for r in test_records))
leak = [r for r in records if any(s in json.dumps(r, ensure_ascii=False)
                                  for s in ("demo-0001", "demo-0003", "expired", "token:ok"))]
check("审计与拒绝记录不泄露认证参数", not leak, f"泄露记录数={len(leak)}")
rejects = [r for r in records if str(r.get("操作结果", "")).startswith("拒绝")]
check("拒绝记录含校验后状态说明", all("状态说明" in str(r.get("变更详情", "")) for r in rejects),
      f"拒绝记录数={len(rejects)}")

print("== 9. 列表与详情返回新值（不读旧缓存） ==")
listing = get("/api/connector?keyword=CONN-0001")
check("列表中 CONN-0001 为已离线（与详情一致）",
      listing["items"][0]["status"] == "已离线" and get("/api/connector/1")["status"] == "已离线")

print()
if failures:
    print(f"共 {len(failures)} 项未通过：")
    for name in failures:
        print(f"  - {name}")
    raise SystemExit(1)
print("全部核对项通过。")
