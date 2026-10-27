"""Feishu daily report: built from Langfuse v2 observations, smoke-test data excluded."""
from __future__ import annotations

import importlib.util
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

spec = importlib.util.spec_from_file_location("report", Path(__file__).resolve().parents[1] / "tools" / "observer_daily_report.py")
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)

NOW = datetime(2026, 10, 23, 1, 0, tzinfo=timezone.utc)  # 09:00 Beijing
INSTALL = "observer-codex-82b75da4e24f96507c47d14c"


def _row(at, *, who=("L05721", "陈芝依"), runtime="codex", kind="skill", component="dev-expert", stage="completed",
         nested=False, install=INSTALL, session="s1", **health):
    attributes = {"observer.employee_id": who[0], "observer.employee_name": who[1], "observer.installation_id": install,
                  "cowork.runtime": runtime, "cowork.component_kind": kind, "cowork.component_id": component,
                  "cowork.stage": stage, "langfuse.session.id": session}
    attributes.update({f"observer.health.{key}": value for key, value in health.items()})
    if nested:
        metadata = {"attributes": attributes}
    else:
        metadata = {f"attributes.{key}": value for key, value in attributes.items()}
    return {"id": "x", "name": "", "startTime": at.isoformat().replace("+00:00", "Z"), "metadata": metadata}


def _health(at, runtime="codex", who=("L05721", "陈芝依"), **values):
    values.setdefault("status", "ok")
    values.setdefault("bundle_version", "0.3.2")
    return _row(at, who=who, runtime=runtime, kind="observer_health", component="observer.health", stage="", **values)


def test_report_counts_calls_and_lists_problems():
    rows = [
        _row(NOW - timedelta(hours=2)),
        _row(NOW - timedelta(hours=3), runtime="workbuddy-ai", component="skill-creator", nested=True),
        _row(NOW - timedelta(hours=4), kind="mcp_tool", component="mcp__x__y", stage="failed"),
        _row(NOW - timedelta(hours=5), kind="unknown", component="unknown"),
        _row(NOW - timedelta(hours=1), session="smoketest-1"),                       # excluded
        _row(NOW - timedelta(hours=1), install="observer-codex-smoketest"),          # excluded
        _health(NOW - timedelta(minutes=30), outbox_pending=250),
        _health(NOW - timedelta(minutes=30), runtime="workbuddy", bundle_version="0.3.1", update_status="gave_up"),
        _health(NOW - timedelta(hours=30), runtime="claude"),                        # offline
        _health(NOW - timedelta(hours=2), who=("L10998", "宋扬")),                    # online, never used
        _health(NOW - timedelta(hours=2), runtime="dsh", coverage_gaps="coverage_gap:skill"),
    ]
    calls, health = report.normalize(rows)
    text = report.build_report(calls, health, now=NOW, latest_release="0.3.2", auth_ok=True)
    assert text.startswith("Observer 日报 2026-10-23")
    assert "【调用】3 次（Skill 2 · MCP 1 · 失败 1），1 人" in text
    assert "WorkBuddy AI 1" in text
    assert "- 陈芝依(L05721)：Codex 2 · WorkBuddy AI 1" in text
    assert "上传积压 250 条" in text
    assert "陈芝依(L05721) WorkBuddy：版本 0.3.1，落后于 0.3.2（自动更新：gave_up）" in text
    assert "陈芝依(L05721) Claude：24 小时没有上线" in text
    assert "宋扬(L10998)：电脑在线，但 3 天没有任何 Skill / MCP 调用" in text
    assert "疑似漏采 coverage_gap:skill" in text
    assert "1 次读不懂的调用" in text
    assert "smoketest" not in text


def test_quiet_day_says_nothing_to_watch():
    calls, health = report.normalize([_row(NOW - timedelta(hours=1)), _health(NOW - timedelta(minutes=5))])
    text = report.build_report(calls, health, now=NOW, latest_release="0.3.2", auth_ok=True)
    assert "【需要关注】无" in text


def test_fetch_follows_cursor_and_send_checks_feishu_answer(tmp_path):
    pages = [{"data": [{"a": 1}], "meta": {"cursor": "c2"}}, {"data": [{"a": 2}], "meta": {}}]
    seen = []

    class Response(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def opener(request, timeout=0):
        seen.append(request.full_url)
        return Response(json.dumps(pages.pop(0)).encode())

    rows = report.fetch_observations("https://lf", "pk", "sk", NOW - timedelta(days=3), NOW, opener=opener)
    assert rows == [{"a": 1}, {"a": 2}] and "cursor=c2" in seen[1] and "fields=core%2Cmetadata" in seen[0]
    hook = tmp_path / "ob.md"
    hook.write_text("# <https://open.feishu.cn/open-apis/bot/v2/hook/abc-123>\n")
    assert report.webhook_url(str(hook)) == "https://open.feishu.cn/open-apis/bot/v2/hook/abc-123"
    sent = []
    def feishu(request, timeout=0):
        sent.append(json.loads(request.data))
        return Response(b'{"code":0,"msg":"success"}')
    report.send("https://open.feishu.cn/x", "Observer 日报", opener=feishu)
    assert sent == [{"msg_type": "text", "content": {"text": "Observer 日报"}}]


def test_send_retries_temporary_feishu_refusals_but_not_permanent_ones():
    import pytest

    class Response(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    answers = [b'{"code":19006,"msg":"internal error"}', b'{"code":11232,"msg":"frequency limited"}', b'{"code":0,"msg":"success"}']
    waits = []
    attempts = report.send_with_retry("https://open.feishu.cn/x", "Observer", sleep=waits.append,
                                      opener=lambda request, timeout=0: Response(answers.pop(0)))
    assert attempts == 3 and waits == [60, 120]
    with pytest.raises(RuntimeError, match="19024"):
        report.send_with_retry("https://open.feishu.cn/x", "Observer", sleep=waits.append,
                               opener=lambda request, timeout=0: Response(b'{"code":19024,"msg":"Key Words Not Found"}'))
    assert waits == [60, 120]  # no wait for a refusal that retrying cannot fix
