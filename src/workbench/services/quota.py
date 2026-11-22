"""额度：Codex（本机 app-server 实时读取，失败时回退到会话日志快照）与远程服务器上的 Claude（经 SSH）。"""
from __future__ import annotations

import asyncio
import json
import shlex
import time
from datetime import datetime
from typing import Optional

from workbench.db.repositories import SshRepo
from workbench.engines.codex import codex_home, codex_rpc
from workbench.ssh import commands as cmds
from workbench.ssh import transport

CACHE_TTL = 120

# ---------------------------------------------------------------- Codex

_codex_cache: dict = {"at": 0.0, "data": None}
_codex_lock: Optional[asyncio.Lock] = None


def _rate_limits_from_logs() -> Optional[dict]:
    """兜底：从最近的 codex 会话日志里取最后一次 rate_limits 快照（字段为 snake_case）。"""
    files = sorted((codex_home() / "sessions").glob("*/*/*/rollout-*.jsonl"),
                   key=lambda f: f.stat().st_mtime, reverse=True)[:5]
    for f in files:
        snap = None
        with open(f, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"rate_limits"' not in line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rl = (ev.get("payload") or {}).get("rate_limits")
                if rl:
                    snap = (rl, ev.get("timestamp"))
        if snap:
            rl, ts = snap

            def win(w):
                return w and {"usedPercent": w.get("used_percent"), "windowDurationMins": w.get("window_minutes"),
                              "resetsAt": w.get("resets_at")}
            cr = rl.get("credits") or {}
            return {"rateLimits": {"limitId": rl.get("limit_id"), "limitName": rl.get("limit_name"),
                                   "primary": win(rl.get("primary")), "secondary": win(rl.get("secondary")),
                                   "credits": cr and {"hasCredits": cr.get("has_credits"), "unlimited": cr.get("unlimited"),
                                                      "balance": cr.get("balance")},
                                   "planType": rl.get("plan_type"), "rateLimitReachedType": rl.get("rate_limit_reached_type")},
                    "_snapshot_at": ts}
    return None


def window_label(mins: Optional[int]) -> str:
    if not mins:
        return "额度"
    if mins == 10080:
        return "每周"
    if mins % 1440 == 0:
        return f"{mins // 1440} 天"
    if mins % 60 == 0:
        return f"{mins // 60} 小时"
    return f"{mins} 分钟"


def normalize_codex(raw: dict, source: str) -> dict:
    limits = raw.get("rateLimitsByLimitId") or {"codex": raw.get("rateLimits") or {}}
    groups = []
    for lid, rl in limits.items():
        if not rl:
            continue
        windows = [{"label": window_label(w.get("windowDurationMins")), "used_percent": w.get("usedPercent"),
                    "window_mins": w.get("windowDurationMins"), "resets_at": w.get("resetsAt")}
                   for w in (rl.get("primary"), rl.get("secondary")) if w]
        groups.append({"id": lid, "name": rl.get("limitName") or ("Codex" if lid == "codex" else lid),
                       "windows": windows, "reached": rl.get("rateLimitReachedType")})
    main = raw.get("rateLimits") or next(iter(limits.values()), {}) or {}
    credits = main.get("credits") or {}
    resets = [c for c in (raw.get("rateLimitResetCredits") or {}).get("credits", []) if c.get("status") == "available"]
    return {
        "available": True, "source": source, "snapshot_at": raw.get("_snapshot_at"),
        "plan": main.get("planType"), "usage_allowed": raw.get("ordinaryUsageAllowed", True),
        "spend_control_reached": bool(main.get("spendControlReached")),
        "groups": groups,
        "credits": {"balance": credits.get("balance"), "unlimited": bool(credits.get("unlimited")),
                    "has": bool(credits.get("hasCredits"))} if credits else None,
        "reset_credits": {"count": len(resets), "expires_at": min((c.get("expiresAt") or 0 for c in resets), default=None)},
    }


async def overview(refresh: bool = False) -> dict:
    global _codex_lock
    _codex_lock = _codex_lock or asyncio.Lock()
    async with _codex_lock:
        if not refresh and _codex_cache["data"] and time.time() - _codex_cache["at"] < CACHE_TTL:
            return _codex_cache["data"]
        try:
            codex = normalize_codex((await codex_rpc([("account/rateLimits/read", None)]))[0], "live")
        except Exception as e:  # noqa: BLE001
            snap = _rate_limits_from_logs()
            codex = normalize_codex(snap, "log") if snap else {"available": False, "error": str(e) or type(e).__name__}
        data = {"fetched_at": time.time(), "codex": codex, "claude": {"available": False, "reason": "暂未接入"}}
        _codex_cache.update(at=time.time(), data=data)
        return data


# ---------------------------------------------------------------- 远程 Claude

# 在服务器上执行：从 ~/.claude/.credentials.json 取 OAuth 令牌，调用 Claude Code /usage 用的同一个接口。
# 令牌经 stdin 交给 curl（不出现在进程参数里），也不会传回本机；只回传额度 JSON。
CLAUDE_USAGE_SCRIPT = r'''
f="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/.credentials.json"
if [ ! -r "$f" ]; then echo "WB_ERR:nocred"; exit 0; fi
one() { tr -d '\n' < "$f" | sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\{0,1\}\([^\",}]*\).*/\1/p" | head -n 1; }
t=$(one accessToken)
echo "WB_SUB:$(one subscriptionType)"
echo "WB_TIER:$(one rateLimitTier)"
echo "WB_EXP:$(one expiresAt)"
if [ -z "$t" ]; then echo "WB_ERR:nocred"; exit 0; fi
if ! command -v curl >/dev/null 2>&1; then echo "WB_ERR:nocurl"; exit 0; fi
printf 'Authorization: Bearer %s\n' "$t" | curl -sS -m 15 -H @- -H 'anthropic-beta: oauth-2025-04-20' \
  -H 'Content-Type: application/json' -w '\nWB_HTTP:%{http_code}\n' https://api.anthropic.com/api/oauth/usage
'''
CLAUDE_WINDOW_LABELS = {"five_hour": "5 小时", "seven_day": "每周", "seven_day_opus": "每周 · Opus",
                        "seven_day_sonnet": "每周 · Sonnet"}
_claude_cache: dict[tuple, dict] = {}   # (user, host, port) -> {"at", "data"}


def iso_ts(v) -> Optional[float]:
    if isinstance(v, (int, float)):
        return float(v) / (1000 if v > 1e12 else 1)
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse_claude_usage(out: str) -> dict:
    kv, body = {}, []
    for line in out.splitlines():
        if line.startswith("WB_") and ":" in line:
            k, v = line.split(":", 1)
            kv[k] = v.strip()
        else:
            body.append(line)
    res = {"plan": kv.get("WB_SUB") or "", "tier": kv.get("WB_TIER") or "", "windows": []}
    err = kv.get("WB_ERR")
    if err == "nocred":
        return {**res, "error": "服务器上没有 Claude 登录凭据（~/.claude/.credentials.json）。在服务器上运行 claude 登录一次；用 API Key 计费的不显示额度。"}
    if err == "nocurl":
        return {**res, "error": "服务器上没有 curl，无法查询额度"}
    code = kv.get("WB_HTTP", "")
    exp = iso_ts(float(kv["WB_EXP"])) if kv.get("WB_EXP", "").isdigit() else None
    if code == "401":
        stale = exp and exp < time.time()
        return {**res, "error": "登录令牌已过期，在服务器上执行一次 Claude 任务后会自动刷新" if stale
                else "服务器上的 Claude 登录已失效，请在服务器上重新运行 claude 登录"}
    if code != "200":
        return {**res, "error": f"查询失败（HTTP {code or '无响应'}）：{' '.join(body).strip()[:160]}"}
    try:
        data = json.loads("\n".join(body))
    except json.JSONDecodeError:
        return {**res, "error": "额度接口返回了无法解析的内容"}
    for key, w in data.items():
        if not isinstance(w, dict) or w.get("utilization") is None:
            continue
        res["windows"].append({"key": key, "label": CLAUDE_WINDOW_LABELS.get(key, key),
                               "used_percent": float(w["utilization"]), "resets_at": iso_ts(w.get("resets_at"))})
    order = list(CLAUDE_WINDOW_LABELS)
    res["windows"].sort(key=lambda w: order.index(w["key"]) if w["key"] in order else 99)
    if isinstance(data.get("extra_usage"), dict) and data["extra_usage"].get("is_enabled"):
        x = data["extra_usage"]
        res["extra"] = {"used": x.get("used_credits"), "limit": x.get("monthly_limit"), "used_percent": x.get("utilization")}
    if not res["windows"]:
        res["error"] = "额度接口没有返回用量数据"
    return res


async def _claude_remote_one(c: dict, refresh: bool) -> dict:
    key = (c["user"], c["host"], c["port"])
    hit = _claude_cache.get(key)
    if hit and not refresh and time.time() - hit["at"] < CACHE_TTL:
        return hit["data"]
    base = {"id": c["id"], "name": c["name"], "project": c["project"],
            "target": cmds.target(c, with_port=False), "fetched_at": time.time()}
    try:
        code, out, err = await transport.run(c, "sh -c " + shlex.quote(CLAUDE_USAGE_SCRIPT), timeout=30)
        if code != 0 and "WB_" not in out:
            msg = [m for m in err.strip().splitlines() if not m.startswith("Warning: Permanently added")]
            data = {**base, "error": "SSH 连接失败：" + (msg[-1] if msg else f"退出码 {code}")}
        else:
            data = {**base, **parse_claude_usage(out)}
    except RuntimeError as e:
        data = {**base, "error": str(e)}
    _claude_cache[key] = {"at": time.time(), "data": data}
    return data


async def claude_remote(refresh: bool = False) -> list[dict]:
    """用过远程 Claude 任务的 SSH 连接各自的额度（同一 user@host:port 只查一次）。"""
    uniq: dict[tuple, dict] = {}
    for c in SshRepo.used_by_remote_claude():
        uniq.setdefault((c["user"], c["host"], c["port"]), c)
    return list(await asyncio.gather(*(_claude_remote_one(c, refresh) for c in uniq.values())))
