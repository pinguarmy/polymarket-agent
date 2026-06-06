"""Polymarket Agent — Configuration Loader.

Loads settings from .env with sensible defaults.
Never logs or exposes raw private keys.
"""

import os
import sys
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
    _HAS_DOTENV = True
except ImportError:
    _HAS_DOTENV = False


class Config:
    """Polymarket agent configuration loaded from .env.

    Usage:
        config = Config()                # loads .env from project root
        config = Config(".env.test")     # load a specific env file
        print(config.max_order_size)     # 5.0
        print(config.dry_run)            # True
        print(config.wallet_address)     # "" if not set
    """

    def __init__(self, env_file: str = ".env"):
        """Load configuration from an env file.

        Args:
            env_file: Path to .env file. Resolved relative to project root
                      (polymarket-agent/) or as absolute path.
        """
        # Resolve env_file relative to project root
        project_root = Path(__file__).resolve().parent.parent
        env_path = Path(env_file)
        if not env_path.is_absolute():
            env_path = project_root / env_path

        if _HAS_DOTENV:
            load_dotenv(dotenv_path=env_path, override=False)
        elif env_path.exists():
            # Fallback: manual .env parser
            self._load_env_manual(str(env_path))

    @staticmethod
    def _load_env_manual(path: str) -> None:
        """Manual .env parser when python-dotenv is not installed."""
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, _, val = line.partition("=")
                    key = key.strip()
                    val = val.strip().strip('"').strip("'")
                    if key not in os.environ:
                        os.environ[key] = val

    # ── helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _env(key: str, default: str = "") -> str:
        return os.getenv(key, default)

    @staticmethod
    def _env_bool(key: str, default: bool = False) -> bool:
        val = os.getenv(key, "").strip().lower()
        if val in ("true", "1", "yes", "on"):
            return True
        if val in ("false", "0", "no", "off"):
            return False
        return default

    @staticmethod
    def _env_float(key: str, default: float) -> float:
        try:
            return float(os.getenv(key, "").strip())
        except (ValueError, TypeError):
            return default

    @staticmethod
    def _env_int(key: str, default: int) -> int:
        try:
            return int(os.getenv(key, "").strip())
        except (ValueError, TypeError):
            return default

    @staticmethod
    def _env_list(key: str, default: Optional[list] = None) -> list:
        if default is None:
            default = []
        val = os.getenv(key, "").strip()
        if not val:
            return default
        return [item.strip() for item in val.split(",") if item.strip()]

    # ── wallet ───────────────────────────────────────────────────────

    @property
    def rpc_url(self) -> str:
        """Polygon RPC endpoint."""
        return self._env("POLY_RPC_URL", "https://polygon-rpc.com")

    @property
    def chain_id(self) -> int:
        """Chain ID. 137 = Polygon mainnet."""
        return self._env_int("POLY_CHAIN_ID", 137)

    @property
    def private_key(self) -> str:
        """Wallet private key (empty if not configured)."""
        return self._env("POLY_PK", "")

    @property
    def wallet_address(self) -> str:
        """Wallet address (empty if not configured)."""
        return self._env("POLY_ADDRESS", "")

    @property
    def proxy_address(self) -> str:
        """Polymarket proxy wallet address (API use only)."""
        return self._env("POLY_PROXY_ADDRESS", "")

    # ── CLOB credentials ────────────────────────────────────────────

    @property
    def api_key(self) -> str:
        return self._env("POLYMARKET_API_KEY", "")

    @property
    def api_secret(self) -> str:
        return self._env("POLYMARKET_SECRET", "")

    @property
    def api_passphrase(self) -> str:
        return self._env("POLYMARKET_PASSPHRASE", "")

    def has_credentials(self) -> bool:
        """Check if CLOB credentials are configured."""
        return bool(self.api_key and self.api_secret and self.api_passphrase)

    # ── risk limits ─────────────────────────────────────────────────

    @property
    def max_order_size(self) -> float:
        """Maximum order size in pUSD."""
        return self._env_float("MAX_ORDER_SIZE", 5.0)

    @property
    def max_daily_loss(self) -> float:
        """Maximum daily loss in pUSD."""
        return self._env_float("MAX_DAILY_LOSS", 10.0)

    @property
    def max_daily_trades(self) -> int:
        """Maximum number of trades per day."""
        return self._env_int("MAX_DAILY_TRADES", 20)

    @property
    def allowed_markets(self) -> list:
        """Whitelist of market slugs. Empty = block all live orders."""
        return self._env_list("ALLOWED_MARKETS")

    @property
    def max_buy_price_yes(self) -> float:
        """Never buy YES above this price."""
        return self._env_float("MAX_BUY_PRICE_YES", 0.95)

    @property
    def min_buy_price_no(self) -> float:
        """Never buy NO below this price."""
        return self._env_float("MIN_BUY_PRICE_NO", 0.05)

    @property
    def min_spread_pct(self) -> float:
        """Minimum acceptable spread as decimal (0.05 = 5%)."""
        return self._env_float("MIN_SPREAD_PCT", 0.05)

    @property
    def max_cost_per_market(self) -> float:
        """Maximum total cost per single market (prevents over-scaling)."""
        return self._env_float("MAX_COST_PER_MARKET", 25.0)

    @property
    def max_loss_per_market(self) -> float:
        """Maximum loss on a single market (prevents one bad call wiping balance)."""
        return self._env_float("MAX_LOSS_PER_MARKET", 10.0)

    @property
    def balance(self) -> float:
        """Actual wallet balance in pUSD (for risk engine checks)."""
        return self._env_float("BALANCE", 254.0)

    # ── mode flags ──────────────────────────────────────────────────

    @property
    def dry_run(self) -> bool:
        """Dry-run mode. Default True — no real orders."""
        return self._env_bool("DRY_RUN", True)

    @property
    def live_trading(self) -> bool:
        """Live trading mode. Must be explicitly set to true."""
        return self._env_bool("LIVE_TRADING", False)

    # ── project paths ───────────────────────────────────────────────

    @property
    def project_root(self) -> Path:
        return Path(__file__).resolve().parent.parent

    @property
    def logs_dir(self) -> Path:
        return self.project_root / "logs"

    @property
    def data_dir(self) -> Path:
        return self.project_root / "data"

    # ── check ───────────────────────────────────────────────────────

    def check_phase_1(self) -> bool:
        """Phase 1 (read-only) needs no config at all."""
        return True

    def check_phase_4(self) -> dict:
        """Check readiness for Phase 4 (confirmed orders). Returns dict
        with keys: ready (bool), missing (list of strings).
        """
        missing = []
        if not self.wallet_address:
            missing.append("POLY_ADDRESS")
        if not self.private_key:
            missing.append("POLY_PK")
        if not self.has_credentials():
            missing.append("POLYMARKET_API_KEY/SECRET/PASSPHRASE")
        if not self.allowed_markets:
            missing.append("ALLOWED_MARKETS (whitelist is empty)")
        return {"ready": len(missing) == 0, "missing": missing}

    def __repr__(self) -> str:
        safe = []
        safe.append(f"dry_run={self.dry_run}")
        safe.append(f"live_trading={self.live_trading}")
        safe.append(f"max_order_size={self.max_order_size}")
        safe.append(f"max_daily_loss={self.max_daily_loss}")
        safe.append(f"wallet_address={'configured' if self.wallet_address else 'not set'}")
        safe.append(f"api_key={'configured' if self.api_key else 'not set'}")
        safe.append(f"allowed_markets={len(self.allowed_markets)} markets")
        return f"Config({', '.join(safe)})"
