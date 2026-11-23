"""消息语义演示的验收：单播轮询、通道广播、请求体落库幂等、无订阅者快速失败。"""
from __future__ import annotations

import json

from examples.messaging import demo as M


def test_unicast_is_round_robin_and_no_task_runs_twice():
    out = M.scenario_unicast(4)
    assert out["states"] == ["succeeded"] * 4
    assert out["per_agent"] == {"ocr-1": 2, "ocr-2": 2}   # 两个实例都被用上，各 2 个
    assert out["distinct_docs"] == 4                       # 没有任务被执行两次


def test_channel_merges_two_producers_and_fans_out_to_subscribers():
    out = M.scenario_broadcast()
    assert out["state"] == "succeeded"
    assert out["channel"] == ["铜矿停产", "库存连降三周", "美元走强"]   # append 合并
    assert out["sub_a"] == out["sub_b"] == out["channel"]              # 两个订阅者各得全量


def test_request_body_is_persisted_and_idempotent():
    out = M.scenario_durable()
    assert out["row"]["idempotency_key"] == "order:42"
    assert json.loads(out["row"]["params"])["doc"] == "订单#42"        # 请求体真在库里
    assert out["row"]["capability"] == "ocr" and out["row"]["action"] == "run"
    assert out["same_id"] is True                                     # 同键 → 同一条任务
    assert out["calls"] == 1                                          # handler 只跑一次
    assert out["final_state"] == "succeeded"


def test_no_subscriber_fails_fast_instead_of_queueing():
    out = M.scenario_no_subscriber()
    assert out["state"] == "failed"
    assert out["error"]["code"] == "no_agent"