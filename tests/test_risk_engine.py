from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import risk_engine
from risk_engine import RiskEngine


@pytest.fixture(autouse=True)
def reset_global_kill_switch():
    risk_engine.reset_kill_switch()
    yield
    risk_engine.reset_kill_switch()


@pytest.fixture
def config():
    return SimpleNamespace(
        max_order_size=10.0,
        max_daily_loss=25.0,
        max_daily_trades=3,
        allowed_markets={"btc-updown-5m-123"},
        live_trading=True,
        max_buy_price_yes=0.80,
        min_buy_price_no=0.20,
        min_spread_pct=0.05,
        dry_run=False,
        max_cost_per_market=20.0,
        max_loss_per_market=20.0,
    )


def draft(**overrides):
    order = {
        "market_slug": "btc-updown-5m-123",
        "side": "YES",
        "action": "BUY",
        "order_type": "LIMIT",
        "limit_price": 0.50,
        "size": 4.0,
        "max_cost": 2.0,
        "max_loss": 2.0,
        "spread_pct": 1.0,
    }
    order.update(overrides)
    return order


def test_valid_order_passes_and_logs(config):
    logger = Mock()
    result = RiskEngine(config, logger).validate_order(
        draft(), open_orders=[], daily_trades=0, daily_loss=0.0, balance=100.0
    )

    assert result == {"valid": True, "violations": [], "can_proceed": True}
    logger.log_signal.assert_called_once()
    assert logger.log_signal.call_args.args[0]["signal_type"] == "risk_validation"


def test_rejects_size_daily_loss_and_trade_limit(config):
    result = RiskEngine(config).validate_order(
        draft(size=12.0, max_loss=10.0),
        open_orders=[],
        daily_trades=3,
        daily_loss=20.0,
        balance=100.0,
    )

    assert result["can_proceed"] is False
    assert any(v.startswith("R1:") for v in result["violations"])
    assert any(v.startswith("R2:") for v in result["violations"])
    assert any(v.startswith("R3:") for v in result["violations"])


def test_rejects_market_and_price_rule_violations(config):
    yes_result = RiskEngine(config).validate_order(
        draft(order_type="MARKET", limit_price=0.90),
        open_orders=[],
        daily_trades=0,
        daily_loss=0.0,
        balance=100.0,
    )
    no_result = RiskEngine(config).validate_order(
        draft(side="NO", limit_price=0.10),
        open_orders=[],
        daily_trades=0,
        daily_loss=0.0,
        balance=100.0,
    )

    assert any(v.startswith("R4:") for v in yes_result["violations"])
    assert any(v.startswith("R8: YES") for v in yes_result["violations"])
    assert any(v.startswith("R8: NO") for v in no_result["violations"])


def test_kill_switch_blocks_orders_and_audits(config):
    logger = Mock()
    assert risk_engine.emergency_kill_switch(logger) is True

    result = RiskEngine(config, logger).validate_order(
        draft(), open_orders=[], daily_trades=0, daily_loss=0.0, balance=100.0
    )

    logger.log_kill_event.assert_called_once_with("manual_kill_switch_engaged")
    assert result["can_proceed"] is False
    assert any(v.startswith("R7:") for v in result["violations"])


def test_duplicate_and_market_exposure_rules(config):
    open_orders = [
        {
            "market_slug": "btc-updown-5m-123",
            "side": "YES",
            "action": "BUY",
            "limit_price": 0.50,
            "size": 30.0,
            "cost": 15.0,
            "max_loss": 15.0,
        }
    ]

    result = RiskEngine(config).validate_order(
        draft(size=12.0, limit_price=0.50),
        open_orders=open_orders,
        daily_trades=0,
        daily_loss=0.0,
        balance=100.0,
    )

    assert any(v.startswith("R11:") for v in result["violations"])
    assert any(v.startswith("R16:") for v in result["violations"])
    assert any(v.startswith("R17:") for v in result["violations"])
