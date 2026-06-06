# Requirements — Polymarket Credentials & Environment

## What You Need to Prepare

Before any live trading (Phase 4+), you need these items. Read-only market data (Phase 1) requires none of them.

### 1. Polymarket Account / Wallet

| Item | Required For | How to Get |
|------|-------------|-----------|
| MetaMask wallet (or any EOA) on Polygon | All phases | Install MetaMask, create wallet, add Polygon network (chain ID 137) |
| Polygon MATIC balance (small) | Live trading only | Buy MATIC on exchange, send to wallet (~$2-5 worth for gas) |
| USDC on Polygon | Live trading only | Buy USDC, bridge to Polygon, deposit to Polymarket |
| pUSD balance (on Polymarket) | Live trading only | Deposit USDC into Polymarket → converts to pUSD internally |

### 2. CLOB API Credentials

Polymarket uses two separate credential systems. Do not confuse them.

| Type | Format | Purpose | Where to Get |
|------|--------|---------|-------------|
| **CLOB API Key** | Hex string (0x...) | Authenticated CLOB queries, order placement | Polymarket website (Settings → API) OR derived via `create_or_derive_api_creds()` |
| **CLOB API Secret** | Hex string | Signature generation for API requests | Same source as above |
| **CLOB API Passphrase** | String | Additional auth factor | Same source as above |
| **Relayer API Key** | UUID (e.g. 019d...) | Gasless relayer transactions | Polymarket website (Settings → API) |

**Important**: The Relayer API Key is NOT a CLOB credential. You need the 3-field set (Key, Secret, Passphrase) for CLOB trading.

#### Credential Generation Methods

**A. From Polymarket Website (Recommended)**
1. Go to [polymarket.com](https://polymarket.com)
2. Connect MetaMask and log in
3. Click profile icon → Settings → API
4. Look for "API Credentials" section
5. Copy the API Key, Secret, and Passphrase

**B. Local Derivation (Bypasses Cloudflare)**
If the API is blocked by Cloudflare, derive credentials locally:

```python
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds

client = ClobClient(
    host='https://clob.polymarket.com',
    key='0xYourPrivateKey',
    chain_id=137,
    signature_type=1,  # EOA — required for Polygon mainnet
)
creds = client.create_or_derive_api_creds()
print(f"API_KEY={creds.api_key}")
print(f"SECRET={creds.api_secret}")
print(f"PASSPHRASE={creds.api_passphrase}")
```

### 3. Funder / Proxy Address

Not required for read-only or paper trading. For live trading:

- Your main wallet address is sufficient for CLOB V2 trading
- Polymarket may use a proxy contract for settlement — this is handled by the CLOB SDK automatically
- No separate funder address needed for basic trading

### 4. Private Key / Signing

| Item | Required | Security Notes |
|------|----------|---------------|
| Wallet private key | Live trading only | NEVER log to stdout/files. NEVER commit. Store in .env only. |
| CLOB credentials | Authenticated CLOB calls | Second layer of auth. Also never commit. |
| EIP-712 signature capability | Order signing | Handled by py-clob-client SDK internally |

### 5. Target Market Information

For read-only queries, you only need the **market slug**.

| Field | Where to Find | Example |
|-------|--------------|---------|
| `market_slug` | Polymarket URL: `polymarket.com/event/<slug>` | `will-donald-trump-win-the-2028-presidential-election` |
| `condition_id` | Gamma API `GET /markets?slug=<slug>` → `conditionId` | `0xabc123...` |
| `clob_token_ids` | Gamma API → `clobTokenIds` (parsed JSON array) | `["12345", "67890"]` (Yes=0, No=1) |
| `token_id` (Yes) | First element of `clobTokenIds` | `12345` |
| `token_id` (No) | Second element of `clobTokenIds` | `67890` |

### 6. RPC Endpoint

| Network | RPC URL | Required For |
|---------|---------|-------------|
| Polygon Mainnet | `https://polygon-rpc.com` | Live trading (gas estimation, settlement) |
| Polygon (alternative) | Any public or private Polygon RPC node | Same — use fallback if rate limited |

**Not required** for read-only market data or paper trading.

### 7-10. Risk Limits

See `RISK_RULES.md` for full details. Quick reference:

| Rule | Default Value | Config Key |
|------|--------------|-----------|
| Max order size | $5 | `MAX_ORDER_SIZE` |
| Max daily loss | $10 | `MAX_DAILY_LOSS` |
| Max daily trades | 20 | `MAX_DAILY_TRADES` |
| Allowed markets | Empty (must be populated) | `ALLOWED_MARKETS` |

## Environment Setup

### .env Template

Create a `.env` file at the project root. **Never commit this file.**

```env
# === Wallet ===
POLY_RPC_URL=https://polygon-rpc.com
POLY_PK=0xYourPrivateKey
POLY_ADDRESS=0xYourWalletAddress
POLY_CHAIN_ID=137

# === CLOB API Credentials ===
POLYMARKET_API_KEY=
POLYMARKET_SECRET=
POLYMARKET_PASSPHRASE=

# === Risk Limits ===
MAX_ORDER_SIZE=5
MAX_DAILY_LOSS=10
MAX_DAILY_TRADES=20
ALLOWED_MARKETS=

# === Mode Flags ===
DRY_RUN=true
LIVE_TRADING=false
```

### .gitignore

Ensure `.env` and credential files are never committed:

```
# Secrets
.env
.env.*

# Logs
logs/
*.log

# Data
data/*.jsonl
data/*.json

# Python
__pycache__/
*.pyc
.venv/
venv/

# IDE
.vscode/
.idea/
```

### Verification Script

Before using CLOB credentials, verify they work:

```python
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType
import os

client = ClobClient(
    host='https://clob.polymarket.com',
    key=os.getenv('POLY_PK'),
    chain_id=137,
    signature_type=1,
)

creds = ApiCreds(
    api_key=os.getenv('POLYMARKET_API_KEY'),
    api_secret=os.getenv('POLYMARKET_SECRET'),
    api_passphrase=os.getenv('POLYMARKET_PASSPHRASE'),
)
client.set_api_creds(creds)

params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
bal = client.get_balance_allowance(params)
print(f"pUSD Balance: {int(bal['balance']) / 1_000_000:.2f}")
```

## Python Dependencies

```
py-clob-client>=0.30.0,<1.0.0
python-dotenv>=1.0.0
requests>=2.31.0
pandas>=2.0.0
python-dateutil>=2.8.0
```

Note: `py-clob-client>=1.0.1` does not exist on PyPI. Use `>=0.30.0` (tested with 0.34.6).
