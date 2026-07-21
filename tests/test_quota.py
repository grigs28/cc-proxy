"""配额查询解析器测试"""
import pytest

from cc_proxy.quota import parse_kimi, parse_minimax, parse_zhipu


class TestParseKimi:
    def test_真实响应_字符串数字(self):
        # 2026-07-21 实测 api.kimi.com/coding/v1/usages 响应结构
        data = {
            "usage": {"limit": "100", "used": "76", "remaining": "24",
                      "resetTime": "2026-07-24T00:53:20.746445Z"},
            "limits": [{
                "window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                "detail": {"limit": "100", "used": "47", "remaining": "53",
                           "resetTime": "2026-07-21T11:53:20.746445Z"},
            }],
        }
        tiers = parse_kimi(data)
        assert len(tiers) == 2
        assert tiers[0].name == "5小时窗"
        assert tiers[0].utilization == 47.0
        assert tiers[1].name == "周限额"
        assert tiers[1].utilization == 76.0

    def test_正常解析(self):
        data = {
            "limits": [{"detail": {"limit": 100, "remaining": 40, "resetTime": "2026-07-20T16:00:00Z"}}],
            "usage": {"limit": 1000, "remaining": 250, "resetTime": "2026-07-27T00:00:00Z"},
        }
        tiers = parse_kimi(data)
        assert len(tiers) == 2
        assert tiers[0].name == "限时窗"
        assert tiers[0].utilization == 60.0
        assert tiers[1].name == "周限额"
        assert tiers[1].utilization == 75.0

    def test_空数据(self):
        assert parse_kimi({}) == []
        assert parse_kimi({"limits": None, "usage": None}) == []

    def test_limit为0不除零(self):
        tiers = parse_kimi({"limits": [{"detail": {"limit": 0, "remaining": 0}}]})
        assert tiers[0].utilization == 0.0


class TestParseZhipu:
    def test_正常解析(self):
        data = {
            "data": {
                "limits": [
                    {"type": "TOKENS_LIMIT", "percentage": 45.5, "nextResetTime": 1789999999, "unit": 3, "number": 100},
                ],
                "level": "pro",
            }
        }
        tiers = parse_zhipu(data)
        assert len(tiers) == 1
        assert tiers[0].name == "Token 限额"
        assert tiers[0].utilization == 45.5
        assert "pro" in tiers[0].detail

    def test_未知类型保留原名(self):
        data = {"data": {"limits": [{"type": "RPM_LIMIT", "percentage": 10}]}}
        assert parse_zhipu(data)[0].name == "RPM_LIMIT"

    def test_空数据(self):
        assert parse_zhipu({}) == []
        assert parse_zhipu({"data": {}}) == []


class TestParseMinimax:
    def test_正常解析(self):
        data = {
            "model_remains": [{
                "model_name": "MiniMax-M2-general",
                "current_interval_remaining_percent": 30,
                "end_time": "2026-07-20 16:00:00",
                "current_weekly_remaining_percent": 80,
                "weekly_end_time": "2026-07-27 00:00:00",
            }]
        }
        tiers = parse_minimax(data)
        assert len(tiers) == 2
        assert tiers[0].name == "5小时窗"
        assert tiers[0].utilization == 70.0
        assert tiers[1].name == "周限额"
        assert tiers[1].utilization == 20.0

    def test_过滤非general模型(self):
        data = {"model_remains": [
            {"model_name": "other-model", "current_interval_remaining_percent": 50},
            {"model_name": "general", "current_interval_remaining_percent": 50},
        ]}
        tiers = parse_minimax(data)
        assert len(tiers) == 1

    def test_剩余百分比超界截断(self):
        data = {"model_remains": [{"model_name": "general", "current_interval_remaining_percent": 150}]}
        assert parse_minimax(data)[0].utilization == 0.0

    def test_空数据(self):
        assert parse_minimax({}) == []
        assert parse_minimax({"model_remains": []}) == []
