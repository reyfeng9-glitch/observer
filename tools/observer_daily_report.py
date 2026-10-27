#!/usr/bin/env python3
"""Observer daily report to a Feishu group (runs on the production server, read-only).

Reads the production Langfuse "observer" project through the public API and posts one
text message to a Feishu custom-bot webhook (the bot's keyword is "Observer").
Only the Python standard library is used, so it runs inside the observer-auth image:

    docker run --rm --network langfuse-net --read-only \
        --env-file /data/observer-auth/observer.env \
        -v /data/observer-auth/report:/report:ro \
        -v /data/observer-auth/secrets/feishu-webhook.md:/run/feishu-webhook.md:ro \
        --entrypoint python3 observer-auth:<tag> /report/observer_daily_report.py \
        --webhook-file /run/feishu-webhook.md [--dry-run]

Environment: OBSERVER_LANGFUSE_BASE_URL, OBSERVER_LANGFUSE_PUBLIC_KEY, OBSERVER_LANGFUSE_SECRET_KEY
(the auth service's own settings). Nothing is written anywhere; --dry-run prints the message.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
RUNTIMES = {"codex": "Codex", "claude": "Claude", "dsh": "AI Hub", "workbuddy": "WorkBuddy", "workbuddy-ai": "WorkBuddy AI"}
INSTALLATION = re.compile(r"^observer-(codex|claude|dsh|workbuddy-desktop)-[0-9a-f]{24}$")
CHANNEL_URL = "https://raw.githubusercontent.com/LoctekIT-AI/observer/main/channels/release.json"
WINDOW_DAYS = 3          # "no calls for 3 days" and "offline" look back this far
OFFLINE_HOURS = 24       # no health record for this long = offline
PENDING_LIMIT = 100      # upload queue backlog worth reporting


# ---------------------------------------------------------------- data access
def _flatten(value: object, prefix: str = "") -> dict[str, object]:
    """Langfuse may return metadata nested or with dotted keys; read both the same way."""
    flat: dict[str, object] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(item, dict):
                flat.update(_flatten(item, name))
            else:
                flat[name] = item
    return flat


def fetch_observations(base: str, public: str, secret: str, start: datetime, end: datetime,
                       opener=urllib.request.urlopen) -> list[dict]:
    auth = base64.b64encode(f"{public}:{secret}".encode()).decode()
    rows: list[dict] = []
    cursor = None
    for _ in range(1000):  # hard stop: 1,000 pages of 1,000
        query = {"fields": "core,metadata", "limit": "1000",
                 "fromStartTime": start.isoformat().replace("+00:00", "Z"),
                 "toStartTime": end.isoformat().replace("+00:00", "Z")}
        if cursor:
            query["cursor"] = cursor
        request = urllib.request.Request(f"{base.rstrip('/')}/api/public/v2/observations?{urllib.parse.urlencode(query)}",
                                         headers={"Authorization": f"Basic {auth}"})
        with opener(request, timeout=60) as response:
            page = json.loads(response.read().decode("utf-8"))
        rows.extend(page.get("data") or [])
        cursor = (page.get("meta") or {}).get("cursor")
        if not cursor or not page.get("data"):
            return rows
    raise RuntimeError("too many pages")


def _get(meta: dict[str, object], name: str) -> object:
    for key in (f"attributes.{name}", f"resourceAttributes.{name}", name):
        if key in meta and meta[key] not in (None, ""):
            return meta[key]
    return None


def _time(row: dict) -> datetime | None:
    value = row.get("startTime") or row.get("start_time")
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def normalize(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split into (calls, health) records; drop hand-made smoke-test data."""
    calls, health = [], []
    for row in rows:
        meta = _flatten(row.get("metadata") or {})
        at = _time(row)
        installation = str(_get(meta, "observer.installation_id") or "")
        session = str(_get(meta, "langfuse.session.id") or "")
        if at is None or session.startswith("smoketest") or not INSTALLATION.match(installation):
            continue
        item = {
            "at": at,
            "id": str(_get(meta, "observer.employee_id") or "?"),
            "name": str(_get(meta, "observer.employee_name") or "?"),
            "runtime": str(_get(meta, "cowork.runtime") or "?"),
            "kind": str(_get(meta, "cowork.component_kind") or ""),
            "component": str(_get(meta, "cowork.component_id") or row.get("name") or "?"),
            "stage": str(_get(meta, "cowork.stage") or ""),
            "meta": meta,
        }
        (health if item["kind"] == "observer_health" else calls).append(item)
    return calls, health


# ---------------------------------------------------------------- report
def _version(value: object) -> tuple[int, ...]:
    parts = re.findall(r"\d+", str(value or ""))[:3]
    return tuple(int(part) for part in parts) if parts else ()


def _number(value: object) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _who(item: dict) -> str:
    return f"{item['name']}({item['id']})"


def _client(runtime: str) -> str:
    return RUNTIMES.get(runtime, runtime)


def _cst(at: datetime) -> str:
    return at.astimezone(CST).strftime("%m-%d %H:%M")


def build_report(calls: list[dict], health: list[dict], *, now: datetime, latest_release: str | None,
                 auth_ok: bool | None) -> str:
    day_start = now - timedelta(hours=24)
    window_start = now - timedelta(days=WINDOW_DAYS)
    today = [c for c in calls if c["at"] >= day_start]
    real = [c for c in today if c["kind"] in {"skill", "mcp_tool"}]
    unknown = [c for c in today if c["kind"] == "unknown"]
    failed = [c for c in real if c["stage"] in {"failed", "timeout", "cancelled"}]
    lines = [f"Observer 日报 {now.astimezone(CST):%Y-%m-%d}（{_cst(day_start)} ～ {_cst(now)}）", ""]

    people = {c["id"]: _who(c) for c in today}
    lines.append(f"【调用】{len(real)} 次（Skill {sum(c['kind'] == 'skill' for c in real)} · MCP {sum(c['kind'] == 'mcp_tool' for c in real)}"
                 f" · 失败 {len(failed)}），{len({c['id'] for c in real})} 人")
    turns = [c for c in today if c["kind"] == "turn"]
    if turns:
        lines.append(f"【对话】{len(turns)} 轮用到了 Skill / MCP（已记录输入和最终回复）")
    by_client = Counter(c["runtime"] for c in real)
    if by_client:
        lines.append("按客户端：" + " · ".join(f"{_client(r)} {n}" for r, n in by_client.most_common()))
    per_person: dict[str, Counter] = defaultdict(Counter)
    for c in real:
        per_person[c["id"]][c["runtime"]] += 1
    if per_person:
        lines.append("")
        lines.append("【每人】")
        for person, counter in sorted(per_person.items(), key=lambda kv: -sum(kv[1].values())):
            lines.append(f"- {people[person]}：" + " · ".join(f"{_client(r)} {n}" for r, n in counter.most_common()))
    top = Counter((c["kind"], c["component"]) for c in real).most_common(10)
    if top:
        lines.append("")
        lines.append("【常用 Skill / MCP】")
        for (kind, component), count in top:
            lines.append(f"- {component}（{'Skill' if kind == 'skill' else 'MCP'}）{count}")

    # ---- things that need attention
    issues: list[str] = []
    if auth_ok is False:
        issues.append("认证服务 healthz 不正常（同事无法登录 / 上传）")
    latest: dict[tuple[str, str], dict] = {}
    for h in health:
        key = (h["id"], h["runtime"])
        if key not in latest or h["at"] > latest[key]["at"]:
            latest[key] = h
    for (person, runtime), h in sorted(latest.items()):
        meta, who, client = h["meta"], _who(h), _client(runtime)
        if h["at"] < now - timedelta(hours=OFFLINE_HOURS):
            issues.append(f"{who} {client}：{OFFLINE_HOURS} 小时没有上线（最后 {_cst(h['at'])}，可能关机或收集器停了）")
            continue
        status = _get(meta, "observer.health.status")
        codes = _get(meta, "observer.health.error_codes")
        if status not in (None, "ok") or codes:
            issues.append(f"{who} {client}：状态 {status}" + (f"（{codes}）" if codes else ""))
        pending = _number(_get(meta, "observer.health.outbox_pending"))
        failed_rows = _number(_get(meta, "observer.health.outbox_failed"))
        if pending is not None and pending > PENDING_LIMIT:
            issues.append(f"{who} {client}：上传积压 {int(pending)} 条")
        if failed_rows is not None and failed_rows > 0:
            issues.append(f"{who} {client}：{int(failed_rows)} 条上传失败")
        version = _get(meta, "observer.health.bundle_version")
        if latest_release and _version(version) and _version(version) < _version(latest_release):
            update = _get(meta, "observer.health.update_status")
            issues.append(f"{who} {client}：版本 {version}，落后于 {latest_release}（自动更新：{update or '未知'}）")
        update_error = _get(meta, "observer.health.update_error")
        if update_error:
            issues.append(f"{who} {client}：自动更新出错 {update_error}")
    gaps = sorted({(h["id"], h["runtime"], str(_get(h["meta"], "observer.health.coverage_gaps")))
                   for h in health if h["at"] >= day_start and _get(h["meta"], "observer.health.coverage_gaps")})
    people.update({h["id"]: _who(h) for h in health})
    for person, runtime, gap in gaps:
        issues.append(f"{people[person]} {_client(runtime)}：疑似漏采 {gap}")
    for (person, runtime), count in Counter((c["id"], c["runtime"]) for c in unknown).most_common():
        issues.append(f"{people[person]} {_client(runtime)}：{count} 次读不懂的调用（客户端可能升级了格式）")
    online = {h["id"] for h in health if h["at"] >= window_start}
    used = {c["id"] for c in calls if c["at"] >= window_start and c["kind"] in {"skill", "mcp_tool"}}
    for person in sorted(online - used):
        issues.append(f"{people[person]}：电脑在线，但 {WINDOW_DAYS} 天没有任何 Skill / MCP 调用")

    lines.append("")
    lines.append("【需要关注】" if issues else "【需要关注】无")
    lines.extend(f"- {issue}" for issue in issues)
    machines = len({key for key, h in latest.items() if h["at"] >= now - timedelta(hours=OFFLINE_HOURS)})
    lines.append("")
    lines.append(f"在线收集器 {machines} 个（{len({p for p, _ in latest})} 人）" + (f"；正式版最新 {latest_release}" if latest_release else ""))
    return "\n".join(lines)


# ---------------------------------------------------------------- side calls
def latest_release(opener=urllib.request.urlopen) -> str | None:
    try:
        with opener(CHANNEL_URL, timeout=20) as response:
            return str(json.loads(response.read().decode("utf-8")).get("version") or "") or None
    except (OSError, ValueError, urllib.error.URLError):
        return None


def auth_healthy(url: str, opener=urllib.request.urlopen) -> bool | None:
    try:
        with opener(url, timeout=10) as response:
            return json.loads(response.read().decode("utf-8")).get("status") == "ok"
    except (OSError, ValueError, urllib.error.URLError):
        return False


def webhook_url(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        match = re.search(r"https://open\.(?:feishu\.cn|larksuite\.com)/open-apis/bot/v2/hook/[A-Za-z0-9-]+", handle.read())
    if not match:
        raise ValueError("no Feishu bot webhook URL in the webhook file")
    return match.group(0)


def send(url: str, text: str, opener=urllib.request.urlopen) -> dict:
    body = json.dumps({"msg_type": "text", "content": {"text": text}}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json; charset=utf-8"})
    with opener(request, timeout=30) as response:
        result = json.loads(response.read().decode("utf-8"))
    if result.get("code", result.get("StatusCode", 0)) != 0:
        raise RuntimeError(f"Feishu rejected the message: code={result.get('code')} msg={result.get('msg')}")
    return result


# Feishu sometimes refuses at 09:00 sharp, when every scheduled bot posts at once
# (19006 internal error, 11232 frequency limited): retry for about a quarter of an hour.
RETRY_DELAYS = (60, 120, 180, 240, 300)
_PERMANENT = ("19024", "19001", "19002", "19007", "19021", "19022")  # keyword, bad URL / signature: retrying cannot help


def send_with_retry(url: str, text: str, *, sleep=None, opener=urllib.request.urlopen) -> int:
    """Send; on a temporary refusal wait and try again. Returns the number of attempts."""
    import time
    sleep = sleep or time.sleep
    for attempt, delay in enumerate((*RETRY_DELAYS, None), start=1):
        try:
            send(url, text, opener=opener)
            return attempt
        except (RuntimeError, OSError, ValueError) as error:
            permanent = isinstance(error, RuntimeError) and any(f"code={code} " in str(error) for code in _PERMANENT)
            if delay is None or permanent:
                raise
            print(f"attempt {attempt} failed ({error}); retrying in {delay} s", file=sys.stderr, flush=True)
            sleep(delay)
    raise AssertionError("unreachable")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--webhook-file", help="file containing the Feishu bot webhook URL")
    parser.add_argument("--dry-run", action="store_true", help="print the message instead of sending it")
    parser.add_argument("--auth-healthz", default="http://observer-auth:8090/observer/healthz")
    args = parser.parse_args(argv)
    base = os.environ.get("OBSERVER_LANGFUSE_BASE_URL", "").strip()
    public = os.environ.get("OBSERVER_LANGFUSE_PUBLIC_KEY", "").strip()
    secret = os.environ.get("OBSERVER_LANGFUSE_SECRET_KEY", "").strip()
    if not (base and public and secret):
        print("missing OBSERVER_LANGFUSE_BASE_URL / _PUBLIC_KEY / _SECRET_KEY", file=sys.stderr)
        return 2
    now = datetime.now(timezone.utc)
    rows = fetch_observations(base, public, secret, now - timedelta(days=WINDOW_DAYS), now)
    calls, health = normalize(rows)
    text = build_report(calls, health, now=now, latest_release=latest_release(), auth_ok=auth_healthy(args.auth_healthz))
    if args.dry_run:
        print(text)
        print(f"\n(dry run: {len(rows)} records read, nothing sent)")
        return 0
    if not args.webhook_file:
        print("--webhook-file is required unless --dry-run", file=sys.stderr)
        return 2
    attempts = send_with_retry(webhook_url(args.webhook_file), text)
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z sent ({len(rows)} records read, attempt {attempts})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
