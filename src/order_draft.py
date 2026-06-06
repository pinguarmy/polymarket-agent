"""Polymarket Agent — Order Draft & Human Confirmation.

Phase 4: bridges paper trading signals to structured order drafts.
ALL orders require human CONFIRM before any live submission.

Usage:
    from order_draft import create_order_draft, generate_confirmation_prompt
    draft = create_order_draft("test-market", "0xabc", "YES", "tok1", "BUY", 0.65, 5)
    prompt = generate_confirmation_prompt(draft, validation_result)
    print(prompt)
"""

from __future__ import annotations

from datetime import datetime, timezone

# ── helpers ──────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Task 4.1: Order Draft Generator ──────────────────────────────────


def create_order_draft(
    market_slug: str,
    condition_id: str,
    side: str,
    token_id: str,
    action: str,
    limit_price: float,
    size: float,
    max_cost: float | None = None,
    max_loss: float | None = None,
    reason: str = "",
    risk_notes: str = "",
    best_bid: float | None = None,
    best_ask: float | None = None,
    spread_pct: float | None = None,
    expected_edge: float | None = None,
    confidence: float = 0.5,
    signal_type: str = "",
) -> dict:
    """Create a structured order draft for human review.

    This does NOT submit an order — it only creates the draft for validation
    and confirmation.

    Args:
        market_slug: Polymarket event slug
        condition_id: Market condition ID (0x...)
        side: "YES" or "NO"
        token_id: CLOB token ID for this side
        action: "BUY" or "SELL"
        limit_price: Limit price per share (0.00-1.00)
        size: Number of shares
        max_cost: Maximum cost = limit_price * size (auto-computed if None)
        max_loss: Maximum loss = max_cost (auto-computed if None, assumes full loss)
        reason: Human-readable reason for the trade
        risk_notes: Any identified risks
        best_bid: Current best bid from order book
        best_ask: Current best ask from order book
        spread_pct: Current spread as percentage
        expected_edge: Expected positive return
        confidence: Signal confidence (0.0-1.0)
        signal_type: Which signal triggered this trade

    Returns:
        Order draft dict ready for validation
    """
    side = side.upper()
    action = action.upper()

    if side not in ("YES", "NO"):
        raise ValueError("side must be 'YES' or 'NO'")
    if action not in ("BUY", "SELL"):
        raise ValueError("action must be 'BUY' or 'SELL'")

    cost = round(limit_price * size, 2)
    if max_cost is None:
        max_cost = cost
    if max_loss is None:
        max_loss = cost  # Assume full loss in worst case

    return {
        "market_slug": market_slug,
        "condition_id": condition_id,
        "side": side,
        "token_id": token_id,
        "action": action,
        "limit_price": float(limit_price),
        "size": float(size),
        "max_cost": float(max_cost),
        "max_loss": float(max_loss),
        "reason": reason,
        "risk_notes": risk_notes,
        "best_bid": float(best_bid) if best_bid is not None else None,
        "best_ask": float(best_ask) if best_ask is not None else None,
        "spread_pct": float(spread_pct) if spread_pct is not None else None,
        "expected_edge": float(expected_edge) if expected_edge is not None else None,
        "confidence": float(confidence),
        "signal_type": signal_type,
        "order_type": "LIMIT",
        "created_at": _now_iso(),
        "human_confirmed": False,
    }


# ── Task 4.3: Human Confirmation Prompt ──────────────────────────────


def generate_confirmation_prompt(
    draft: dict,
    risk_validation: dict | None = None,
) -> str:
    """Generate the human-readable confirmation prompt (Chinese).

    Args:
        draft: Order draft from create_order_draft()
        risk_validation: Result from RiskEngine.validate_order() or None

    Returns:
        String prompt to display to the user
    """
    market = draft.get("market_slug", "?")
    side = draft.get("side", "?")
    action = draft.get("action", "?")
    token_id = draft.get("token_id", "?")
    price = draft.get("limit_price", 0)
    size = draft.get("size", 0)
    max_cost = draft.get("max_cost", 0)
    max_loss = draft.get("max_loss", 0)
    reason = draft.get("reason", "N/A")
    risks = draft.get("risk_notes", "N/A")
    best_bid = draft.get("best_bid", "N/A")
    best_ask = draft.get("best_ask", "N/A")

    lines = []

    # Risk warning header
    if risk_validation and not risk_validation.get("valid", True):
        violations = risk_validation.get("violations", [])
        lines.append(
            f"\u26a0\ufe0f  RISK WARNING — {len(violations)} violation(s) detected:"
        )
        for v in violations:
            lines.append(f"   \u2022 {v}")
        lines.append("")

    lines.append(
        "I am prepared to submit the following order draft. "
        "It will NOT be submitted unless you explicitly reply CONFIRM:"
    )
    lines.append("")
    lines.append(f"Market:      {market}")
    lines.append(f"Side:        {side}")
    lines.append(f"Action:      {action}")
    lines.append(f"Token ID:    {token_id}")
    lines.append(f"Limit price: ${price:.4f}")
    lines.append(f"Size:        {size:.0f} shares")
    lines.append(f"Max cost:    ${max_cost:.2f}")
    lines.append(f"Max loss:    ${max_loss:.2f}")
    lines.append(f"Best bid:    ${best_bid:.4f}" if isinstance(best_bid, (int, float)) else f"Best bid:    {best_bid}")
    lines.append(f"Best ask:    ${best_ask:.4f}" if isinstance(best_ask, (int, float)) else f"Best ask:    {best_ask}")
    lines.append("")
    lines.append(f"Reason:      {reason}")
    lines.append(f"Risks:       {risks}")
    lines.append("")
    lines.append("Please reply CONFIRM to proceed.")

    return "\n".join(lines)


# ── Task 4.4: Confirmed Order Flow ───────────────────────────────────


def confirmed_order_flow(
    draft: dict,
    config,
    logger=None,
) -> dict:
    """Run the full confirmed order flow.

    1. Validate against risk engine
    2. Generate and print confirmation prompt
    3. Route based on mode: dry_run vs live_trading

    Args:
        draft: Order draft from create_order_draft()
        config: Config instance
        logger: Optional TradeLogger

    Returns:
        {"status": str, "message": str, "draft": dict}
    """
    from risk_engine import RiskEngine

    engine = RiskEngine(config, logger)

    # Validate
    validation = engine.validate_order(
        draft=draft,
        open_orders=[],
        daily_trades=0,
        daily_loss=0.0,
        balance=0.0,  # Dry-run skips balance check
    )

    # Generate prompt
    prompt = generate_confirmation_prompt(draft, validation)
    print(prompt)
    print()

    # Route by mode
    if config.dry_run:
        if logger:
            logger.log_paper_trade({
                "timestamp": _now_iso(),
                "session_id": "phase4",
                "market_slug": draft["market_slug"],
                "condition_id": draft["condition_id"],
                "token_id": draft["token_id"],
                "side": draft["side"],
                "action": draft["action"],
                "simulated_price": draft["limit_price"],
                "simulated_size": draft["size"],
                "simulated_cost": draft["max_cost"],
                "simulated_fill_type": "dry_run_draft",
                "reason": draft["reason"],
                "signal_type": draft["signal_type"],
                "confidence": draft["confidence"],
                "expected_edge": draft["expected_edge"],
                "risk_notes": draft["risk_notes"],
                "human_confirmed": draft["human_confirmed"],
                "entry_price": None,
                "exit_price": None,
                "theoretical_pnl": None,
                "theoretical_pnl_pct": None,
                "status": "draft",
            })
        return {
            "status": "dry_run",
            "message": "DRY RUN — order not submitted to exchange",
            "validation": validation,
            "draft": draft,
        }

    if config.live_trading:
        return {
            "status": "pending_confirmation",
            "message": "Awaiting human CONFIRM response",
            "validation": validation,
            "draft": draft,
        }

    return {
        "status": "blocked",
        "message": "Neither DRY_RUN nor LIVE_TRADING is enabled",
        "draft": draft,
    }


def submit_if_confirmed(
    user_input: str,
    draft: dict,
    config,
    risk_engine=None,
) -> dict:
    """Handle user confirmation response.

    Args:
        user_input: Raw user input string
        draft: The order draft
        config: Config instance
        risk_engine: Optional RiskEngine for re-validation

    Returns:
        {"status": str, "message": str, "draft": dict}
    """
    user_input = user_input.strip().upper()

    if user_input != "CONFIRM":
        return {
            "status": "cancelled",
            "message": "Order cancelled — user did not confirm",
            "draft": draft,
        }

    # Re-validate with fresh state before submission
    if risk_engine is not None:
        recheck = risk_engine.validate_order(
            draft=draft,
            open_orders=[],
            daily_trades=0,
            daily_loss=0.0,
            balance=0.0,
        )
        if not recheck["can_proceed"]:
            return {
                "status": "blocked",
                "message": f"Re-validation failed: {recheck['violations']}",
                "draft": draft,
            }

    draft["human_confirmed"] = True
    draft["confirmed_at"] = _now_iso()

    if config.dry_run:
        return {
            "status": "dry_run_confirmed",
            "message": "DRY RUN CONFIRMED — would have submitted to exchange",
            "draft": draft,
        }

    if config.live_trading:
        raise NotImplementedError(
            "Live trading not implemented — see Phase 5 in CODING_TASKS.md"
        )

    return {
        "status": "blocked",
        "message": "Live trading is not enabled",
        "draft": draft,
    }
