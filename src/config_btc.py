"""BTC 5-minute collector configuration loader.

Loads settings from .env with sensible defaults.
"""

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    _HAS_DOTENV = True
except ImportError:
    _HAS_DOTENV = False


class ConfigBTC:
    """BTC collector configuration loaded from .env."""

    def __init__(self, env_file: str = ".env"):
        project_root = Path(__file__).resolve().parent.parent
        env_path = Path(env_file)
        if not env_path.is_absolute():
            env_path = project_root / env_path

        if _HAS_DOTENV:
            load_dotenv(dotenv_path=env_path, override=False)
        elif env_path.exists():
            self._load_env_manual(str(env_path))

    @staticmethod
    def _load_env_manual(path: str) -> None:
        """Manual .env parser when python-dotenv is not installed."""
        with open(path, encoding="utf-8") as f:
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

    @staticmethod
    def _env(key: str, default: str = "") -> str:
        return os.getenv(key, default)

    @staticmethod
    def _env_int(key: str, default: int) -> int:
        try:
            return int(os.getenv(key, "").strip())
        except (ValueError, TypeError):
            return default

    @property
    def DB_PATH(self) -> str:
        return self._env("DB_PATH", "data/btc5m.db")

    @property
    def BINANCE_SYMBOL(self) -> str:
        return self._env("BINANCE_SYMBOL", "BTCUSDT")

    @property
    def COLLECTOR_INTERVAL_SEC(self) -> int:
        return self._env_int("COLLECTOR_INTERVAL_SEC", 2)

    @property
    def MAX_MARKETS(self) -> int:
        return self._env_int("MAX_MARKETS", 10)

    @property
    def db_path(self) -> str:
        return self.DB_PATH

    @property
    def binance_symbol(self) -> str:
        return self.BINANCE_SYMBOL

    @property
    def collector_interval_sec(self) -> int:
        return self.COLLECTOR_INTERVAL_SEC

    @property
    def max_markets(self) -> int:
        return self.MAX_MARKETS

    # ── Risk limits (shared with Config) ──

    @property
    def max_order_size(self) -> float:
        return float(self._env("MAX_ORDER_SIZE", "5.0"))

    @property
    def max_daily_loss(self) -> float:
        return float(self._env("MAX_DAILY_LOSS", "10.0"))

    @property
    def max_daily_trades(self) -> int:
        return self._env_int("MAX_DAILY_TRADES", 20)

    @property
    def max_cost_per_market(self) -> float:
        return float(self._env("MAX_COST_PER_MARKET", "25.0"))

    @property
    def max_loss_per_market(self) -> float:
        return float(self._env("MAX_LOSS_PER_MARKET", "10.0"))

    def __repr__(self) -> str:
        return (
            "ConfigBTC("
            f"db_path={self.db_path}, "
            f"binance_symbol={self.binance_symbol}, "
            f"collector_interval_sec={self.collector_interval_sec}, "
            f"max_markets={self.max_markets}"
            ")"
        )
