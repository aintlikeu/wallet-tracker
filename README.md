# wallet-tracker

**English** | [Русский](README.ru.md)

Telegram bot that notifies about USDT/USDC transfers on tracked addresses in Ethereum (ERC20), BSC (BEP20) and TRON (TRC20).

## Configuration
All settings live in `.env` (chmod 600): `TG_BOT_TOKEN`, `TG_ADMIN_ID`, `POLL_INTERVAL`, `MIN_AMOUNT`, `ETH_RPC`, `BSC_RPC`, `TRONGRID_URL`, `TRONGRID_API_KEY`, `DB_PATH`.

## Usage (TG_ADMIN_ID only)
Inline menu via `/start`: add an address (for 0x… choose the network: ETH / BSC / both), paginated list,
address card (label, deletion with confirmation, explorer link), settings and filters.
Shortcuts: `/add <address> [label]`, `/list`, or just send an address as a message.
Notifications come with "Transaction" and "My addresses" buttons.

Tests: `selftest.py` (live networks), `uitest.py` (menu, offline).

## Scam filter
- tokens not in the contract whitelist (fake USDT/USDC) are ignored;
- zero-value transfers;
- incoming transfers below `MIN_AMOUNT`;
- address poisoning: the counterparty matches the first and last 4 characters of an already known counterparty or of your own address.

## Running
```
uv venv .venv && uv pip install --python .venv/bin/python httpx
.venv/bin/python selftest.py      # checks against live networks, does not post to Telegram
systemctl --user enable --now wallet-tracker
journalctl --user -u wallet-tracker -f
```

## Data sources
Free public endpoints: publicnode (eth_getLogs, ~100 blocks lookback limit), TronGrid (~1 req/s without a key).
After downtime longer than ~20 min, part of the EVM history may be missed.
