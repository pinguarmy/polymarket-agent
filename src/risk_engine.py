"""Polymarket Agent — Risk Engine.

Enforces all 15 risk rules before any order reaches the exchange.
Phase 4: validates order drafts and supports kill-switch.

Usage:
    from risk_engine import RiskEngine, kill_switch, reset_kill_switch
    engine = RiskEngine(config, logger)
    result = engine.validate_order(draft, open_orders, daily_trades, daily_loss, balance)
    if result["can_proceed"]:
        ...  # safe to submit
"""

from __future__ import annotations

from typing import Optional

# Module-level kill switch — toggled by emergency_kill_switch()
LIVE_TRADING = True


def emergency_kill_switch(logger=None) -> bool:
    """Engage emergency kill switch. Blocks all live orders.

    Args:
        logger: Optional TradeLogger for audit trail

    Returns:
        True (kill switch is now engaged)
    """
    global LIVE_TRADING
    LIVE_TRADING = False
    if logger is not None:
        logger.log_kill_event("manual_kill_switch_engaged")
    return True


def reset_kill_switch(logger=None) -> bool:
    """Reset kill switch. Restores live trading capability.

    Returns:
        True (kill switch is now disengaged)
    """
    global LIVE_TRADING
    LIVE_TRADING = True
    if logger is not None:
        logger.log_kill_event("kill_switch_reset")
    return True


def _money(v: float) -> float:
    return round(float(v), 2)


class RiskEngine:
    """Validate order drafts against all risk rules.

    Constructor takes a Config and optional TradeLogger.
    """

    def __init__(self, config, logger=None):
        """Initialize with config and optional logger.

        Args:
            config: Config instance (from src.config)
            logger: Optional TradeLogger for audit logging
        """
        self.config = config
        self.logger = logger

    def validate_order(
        self,
        draft: dict,
        open_orders: list,
        daily_trades: int,
        daily_loss: float,
        balance: float,
    ) -> dict:
        """Validate an order draft against all risk rules.

        Args:
            draft: Order draft dict from create_order_draft()
            open_orders: List of currently open order dicts (for duplicate check)
            daily_trades: Number of trades already executed today
            daily_loss: Realized loss so far today (positive = loss)
            balance: Available balance in pUSD (0 if dry_run)

        Returns:
            {"valid": bool, "violations": list[str], "can_proceed": bool}
        """
        try:
            violations = self._check_all_rules(
                draft, open_orders, daily_trades, daily_loss, balance
            )
        except Exception as e:
            # R14: graceful failure
            msg = f"R14: internal validation error: {e}"
            if self.logger:
                self.logger.log_kill_event(msg)
            return {"valid": False, "violations": [msg], "can_proceed": False}

        valid = len(violations) == 0

        # R13: log validation result
        if self.logger:
            self.logger.log_signal({
                "signal_type": "risk_validation",
                "market_slug": draft.get("market_slug", ""),
                "valid": valid,
                "violations": violations,
                "draft_price": draft.get("limit_price"),
                "draft_size": draft.get("size"),
            })

        return {"valid": valid, "violations": violations, "can_proceed": valid}

    def _check_all_rules(
        self,
        draft: dict,
        open_orders: list,
        daily_trades: int,
        daily_loss: float,
        balance: float,
    ) -> list[str]:
        """Run all 15 risk rule checks. Returns list of violation strings."""
        v: list[str] = []

        slug = draft.get("market_slug", "")
        size = float(draft.get("size", 0))
        limit_price = float(draft.get("limit_price", 0))
        max_cost = float(draft.get("max_cost", 0))
        max_loss = float(draft.get("max_loss", 0))
        side = draft.get("side", "")
        action = draft.get("action", "")
        spread_pct = draft.get("spread_pct")

        # R1: max order size
        if size > self.config.max_order_size:
            v.append(
                f"R1: order size {size:.2f} exceeds maximum "
                f"{self.config.max_order_size:.2f}"
            )

        # R2: max daily loss
        projected_loss = daily_loss + max_loss
        if projected_loss > self.config.max_daily_loss:
            v.append(
                f"R2: projected daily loss {projected_loss:.2f} exceeds "
                f"maximum {self.config.max_daily_loss:.2f}"
            )

        # R3: max daily trades
        if daily_trades >= self.config.max_daily_trades:
            v.append(
                f"R3: daily trade limit of "
                f"{self.config.max_daily_trades} reached"
            )

        # R4: limit orders only
        if not limit_price or limit_price <= 0:
            v.append("R4: limit price must be set (no market orders allowed)")
        order_type = draft.get("order_type", "LIMIT")
        if order_type.upper() == "MARKET":
            v.append("R4: market orders are prohibited — limit orders only")

        # R5: whitelist only
        allowed = self.config.allowed_markets
        if self.config.live_trading:
            if not allowed:
                v.append("R5: whitelist is empty — all live orders blocked")
            elif slug and slug not in allowed:
                v.append(f"R5: market '{slug}' is not in the allowed whitelist")

        # R6: no chase — documented, enforced by caller
        # (caller should not re-submit the same draft)

        # R7: kill switch — blocks ALL orders regardless of mode
        if not LIVE_TRADING:
            v.append("R7: kill switch engaged — all orders blocked")

        # R8: price limits
        if side == "YES" and limit_price > self.config.max_buy_price_yes:
            v.append(
                f"R8: YES price {limit_price:.4f} exceeds maximum "
                f"{self.config.max_buy_price_yes}"
            )
        if side == "NO" and limit_price < self.config.min_buy_price_no:
            v.append(
                f"R8: NO price {limit_price:.4f} below minimum "
                f"{self.config.min_buy_price_no}"
            )

        # R9: minimum spread check
        if spread_pct is not None and spread_pct > 0:
            threshold_pct = self.config.min_spread_pct * 100
            if spread_pct > threshold_pct:
                v.append(
                    f"R9: spread {spread_pct:.1f}% exceeds maximum "
                    f"{threshold_pct:.1f}%"
                )

        # R10: balance check
        if not self.config.dry_run:
            if balance <= 0:
                v.append(f"R10: insufficient balance (${balance:.2f})")
            elif max_cost > balance * 0.5:
                v.append(
                    f"R10: max cost ${max_cost:.2f} exceeds 50% of "
                    f"balance ${balance:.2f}"
                )
        else:
            # R12: dry_run — note that balance check is skipped
            pass  # Not a violation, just noted

        # R11: duplicate order check (skip for intentional scaling-in)
        if not draft.get("is_scaling") and open_orders:
            for oo in open_orders:
                if (
                    oo.get("market_slug") == slug
                    and oo.get("side") == side
                    and oo.get("action") == action
                    and float(oo.get("limit_price", -1)) == limit_price
                ):
                    v.append(f"R11: duplicate order detected for '{slug}'")
                    break

        # R16: max cumulative cost per market (prevents over-scaling)
        cumulative_cost = sum(
            float(o.get("cost", 0) or float(o.get("limit_price", 0)) * float(o.get("size", 0)))
            for o in open_orders
            if o.get("market_slug") == slug
        )
        total_cost = cumulative_cost + (size * limit_price)
        if total_cost > self.config.max_cost_per_market:
            v.append(
                f"R16: total cost ${total_cost:.2f} for market '{slug}' exceeds "
                f"max ${self.config.max_cost_per_market:.2f} per market"
            )

        # R17: max loss per market (hard stop on a single bad market)
        market_loss = sum(
            abs(float(o.get("max_loss", 0) or float(o.get("cost", 0))))
            for o in open_orders
            if o.get("market_slug") == slug
            and o.get("side") == side
            and o.get("action", "").upper() == "BUY"
        )
        projected_loss = market_loss + (size * limit_price)
        if projected_loss > self.config.max_loss_per_market:
            v.append(
                f"R17: projected loss ${projected_loss:.2f} for market '{slug}' exceeds "
                f"max ${self.config.max_loss_per_market:.2f} per market"
            )

        return v

    # R15: prohibited behaviors — static helpers

    @staticmethod
    def check_no_self_trade(orders: list[dict]) -> bool:
        """Check if any trades would match against each other (self-trading).

        Args:
            orders: list of order dicts with market_slug, side, action

        Returns:
            True if NO self-trading detected (safe), False if self-trade found
        """
        buys = set()
        sells = set()
        for o in orders:
            key = (o.get("market_slug", ""), o.get("side", ""))
            action = o.get("action", "").upper()
            if action == "BUY":
                buys.add(key)
            elif action == "SELL":
                sells.add(key)
        overlap = buys & sells
        return len(overlap) == 0

    @staticmethod
    def check_no_wash_trading(recent_trades: list[dict], new_draft: dict) -> bool:
        """Check if new order would constitute wash trading.

        Wash trading: buying and selling the same asset without economic purpose.
        Heuristic: if user has an opposite-side trade on same market within
        1 hour, flag as potential wash trade.

        Args:
            recent_trades: list of recent trade dicts
            new_draft: the proposed new order

        Returns:
            True if safe (no wash detected), False if wash detected
        """
        import datetime
        slug = new_draft.get("market_slug", "")
        side = new_draft.get("side", "")
        action = new_draft.get("action", "")

        cutoff = datetime.datetime.now(datetime.timezone.utc).timestamp() - 3600
        for t in recent_trades:
            ts = t.get("timestamp", "")
            if isinstance(ts, str):
                try:
                    ts = datetime.datetime.fromisoformat(
                        ts.replace("Z", "+00:00")
                    ).timestamp()
                except (ValueError, TypeError):
                    continue
            if ts < cutoff:
                continue
            if (
                t.get("market_slug") == slug
                and t.get("side") == side
                and t.get("action") != action
            ):
                return False  # wash trade detected
        return True
