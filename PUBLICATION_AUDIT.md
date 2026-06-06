# Publication audit

Prepared for a public GitHub upload from the local private working tree.

## Safe-public strategy

Do not publish the existing local git history. The original repository has tracked runtime artifacts, including a large SQLite database. Publishing the original history, even through a temporary private repo, can leak data later when the repo is made public.

Use this sanitized copy as a fresh repository with new history.

## Excluded from this public copy

- `.env` and all `.env.*` files except `.env.example`
- `data/` runtime contents, including local SQLite databases and Dune CSV exports
- `logs/` runtime contents
- Python bytecode/cache directories
- root historical Polymarket CSV exports
- local document exports such as `.docx`
- one-off live order scripts with hardcoded funder/order/market IDs:
  - `place_trade.py`
  - `scripts/live_test.py`
  - `scripts/live_test_v2.py`

## Sanitization changes applied

- Replaced personal absolute paths with relative paths.
- Changed `src/realtime_trader.py` to read the proxy wallet from `POLY_PROXY_ADDRESS` instead of a hardcoded address.
- Added a stronger `.gitignore` for secrets, runtime data, local DBs, logs, CSV exports, and caches.
- Added `.env.example` with blank credential fields.
- Updated `README.md` to state that this is a sanitized public architecture/code snapshot and that no license has been selected yet.

## Remaining publication notes

- Public wallet addresses may appear in historical data if future CSV/database exports are added. Keep those exports out of git unless explicitly anonymized.
- The repository currently has no license. A public GitHub repository without a license is visible but not open-source licensed for reuse.
- Some one-off research scripts may require local data files that are intentionally excluded from this public copy.
