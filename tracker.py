"""USDT/USDC wallet tracker for Ethereum (ERC20), BSC (BEP20) and TRON (TRC20).

Managed via a Telegram bot (single admin). All config lives in .env.
Scam filtering: unknown token contracts, zero-value transfers, dust below
MIN_AMOUNT, and address-poisoning lookalikes of known counterparties.
"""
import asyncio
import html
import logging
import os
import re
import sqlite3
import time
from decimal import Decimal
from pathlib import Path

import httpx

BASE = Path(__file__).resolve().parent


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


load_env(BASE / ".env")

TG_TOKEN = os.environ["TG_BOT_TOKEN"]
TG_ADMIN = int(os.environ["TG_ADMIN_ID"])
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "30"))
MIN_AMOUNT = Decimal(os.getenv("MIN_AMOUNT", "1"))
TRONGRID = os.getenv("TRONGRID_URL", "https://api.trongrid.io").rstrip("/")
TRONGRID_KEY = os.getenv("TRONGRID_API_KEY", "")
DB_PATH = BASE / os.getenv("DB_PATH", "tracker.db")
# Etherscan-compatible tokentx history APIs. BSC needs a paid Etherscan V2 key (ETHERSCAN_API_KEY).
ETH_HISTORY_API = os.getenv("ETH_HISTORY_API",
                            "https://api.routescan.io/v2/network/mainnet/evm/1/etherscan/api")
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY", "")

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# Whitelisted (genuine) token contracts. Anything else is ignored as a potential fake.
EVM_CHAINS = {
    "eth": {
        "name": "Ethereum (ERC20)",
        "rpc": os.getenv("ETH_RPC", "https://ethereum-rpc.publicnode.com").split(","),
        "confirmations": 3,
        "chunk": 50,
        "explorer": "https://etherscan.io/tx/",
        "tokens": {
            "0xdac17f958d2ee523a2206206994597c13d831ec7": ("USDT", 6),
            "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": ("USDC", 6),
        },
    },
    "bsc": {
        "name": "BSC (BEP20)",
        "rpc": os.getenv("BSC_RPC", "https://bsc-rpc.publicnode.com").split(","),
        "confirmations": 5,
        "chunk": 50,
        "explorer": "https://bscscan.com/tx/",
        "tokens": {
            "0x55d398326f99059ff775485246999027b3197955": ("USDT", 18),
            "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d": ("USDC", 18),
        },
    },
}
TRON_TOKENS = {
    "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t": ("USDT", 6),
    "TEkxiTehnzSmSe2XqrBj4w32RtfS3Q5Ldw8": ("USDC", 6),
}
TRON_EXPLORER = "https://tronscan.org/#/transaction/"

EVM_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
TRON_RE = re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$")

log = logging.getLogger("tracker")


# ---------------------------------------------------------------- storage
class DB:
    def __init__(self, path: Path):
        self.c = sqlite3.connect(path)
        self.c.executescript(
            """
            CREATE TABLE IF NOT EXISTS addresses(chain TEXT, address TEXT, label TEXT,
                added INTEGER, PRIMARY KEY(chain, address));
            CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS seen(id TEXT PRIMARY KEY, ts INTEGER);
            CREATE TABLE IF NOT EXISTS counterparties(chain TEXT, tracked TEXT, peer TEXT,
                PRIMARY KEY(chain, tracked, peer));
            CREATE TABLE IF NOT EXISTS txs(uid TEXT PRIMARY KEY, chain TEXT, address TEXT, txid TEXT,
                ts INTEGER, direction TEXT, peer TEXT, symbol TEXT, amount TEXT, scam TEXT);
            CREATE INDEX IF NOT EXISTS txs_addr ON txs(chain, address, ts);
            """
        )
        self.c.commit()

    def add(self, chain, addr, label):
        old = self.c.execute("SELECT added FROM addresses WHERE chain=? AND address=?", (chain, addr)).fetchone()
        self.c.execute("INSERT OR REPLACE INTO addresses VALUES(?,?,?,?)",
                       (chain, addr, label, old[0] if old else int(time.time())))
        if label:  # keep one name per address across chains
            self.c.execute("UPDATE addresses SET label=? WHERE address=?", (label, addr))
        self.c.commit()

    def label_of(self, addr):
        r = self.c.execute("SELECT label FROM addresses WHERE address=? AND label!='' LIMIT 1", (addr,)).fetchone()
        return r[0] if r else ""

    def remove(self, addr) -> int:
        n = self.c.execute("DELETE FROM addresses WHERE address=?", (addr,)).rowcount
        self.c.commit()
        return n

    def addresses(self, chain=None):
        q = "SELECT chain, address, label, added FROM addresses"
        rows = self.c.execute(q + (" WHERE chain=?" if chain else "") + " ORDER BY chain, added",
                              (chain,) if chain else ()).fetchall()
        return rows

    def rows(self):
        return self.c.execute("SELECT rowid, chain, address, label, added FROM addresses "
                              "ORDER BY chain, added").fetchall()

    def row(self, rid):
        return self.c.execute("SELECT rowid, chain, address, label, added FROM addresses WHERE rowid=?",
                              (rid,)).fetchone()

    def rename(self, rid, label):
        """Renames the address in every chain it is tracked in."""
        r = self.row(rid)
        if r:
            self.c.execute("UPDATE addresses SET label=? WHERE address=?", (label, r[2]))
            self.c.commit()

    def delete_row(self, rid) -> int:
        n = self.c.execute("DELETE FROM addresses WHERE rowid=?", (rid,)).rowcount
        self.c.commit()
        return n

    def get(self, key, default=None):
        r = self.c.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    def set(self, key, value):
        self.c.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (key, str(value)))
        self.c.commit()

    def mark_seen(self, uid) -> bool:
        """Returns True if uid is new."""
        try:
            self.c.execute("INSERT INTO seen VALUES(?,?)", (uid, int(time.time())))
            self.c.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def add_peer(self, chain, tracked, peer):
        self.c.execute("INSERT OR IGNORE INTO counterparties VALUES(?,?,?)", (chain, tracked, peer))
        self.c.commit()

    def peers(self, chain, tracked):
        return [r[0] for r in self.c.execute(
            "SELECT peer FROM counterparties WHERE chain=? AND tracked=?", (chain, tracked))]

    def save_tx(self, uid, chain, address, txid, ts, direction, peer, symbol, amount, scam):
        self.c.execute("INSERT OR IGNORE INTO txs VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (uid, chain, address, txid, ts, direction, peer, symbol, str(amount), scam or ""))
        self.c.commit()

    def local_txs(self, chain, address, limit=50):
        return [dict(txid=r[0], ts=r[1], direction=r[2], peer=r[3], symbol=r[4], amount=Decimal(r[5]))
                for r in self.c.execute("SELECT txid, ts, direction, peer, symbol, amount FROM txs "
                                        "WHERE chain=? AND address=? ORDER BY ts DESC LIMIT ?",
                                        (chain, address, limit))]

    def prune_seen(self, days=30):
        self.c.execute("DELETE FROM seen WHERE ts < ?", (int(time.time()) - days * 86400,))
        self.c.commit()


# ---------------------------------------------------------------- scam filter
def norm(a: str) -> str:
    return a.lower() if a.startswith("0x") else a


def looks_like(a: str, b: str) -> bool:
    """Address-poisoning heuristic: same first 4 and last 4 chars, different address."""
    a, b = norm(a), norm(b)
    if a == b:
        return False
    pa = a[2:] if a.startswith("0x") else a[1:]
    pb = b[2:] if b.startswith("0x") else b[1:]
    return pa[:4] == pb[:4] and pa[-4:] == pb[-4:]


def scam_reason(db: DB, chain, tracked, direction, peer, amount: Decimal):
    if amount == 0:
        return "zero-value"
    if direction == "in" and amount < MIN_AMOUNT:
        return "dust"
    # address poisoning: incoming from a lookalike of a known counterparty or own address.
    # Outgoing transfers are never hidden (a real send to a poisoned address must be visible).
    if direction == "in":
        if any(looks_like(peer, p) for p in db.peers(chain, tracked)):
            return "poisoning"
        if any(looks_like(peer, a) for _, a, _, _ in db.addresses(chain)):
            return "poisoning"
    return None


# ---------------------------------------------------------------- telegram
class TG:
    def __init__(self, client: httpx.AsyncClient):
        self.c = client
        self.url = f"https://api.telegram.org/bot{TG_TOKEN}/"

    async def call(self, method, **params):
        r = await self.c.post(self.url + method, json=params, timeout=60)
        d = r.json()
        if not d.get("ok"):
            raise RuntimeError(f"telegram {method}: {d.get('description')}")
        return d["result"]

    async def send(self, text, markup=None):
        params = dict(chat_id=TG_ADMIN, text=text, parse_mode="HTML", disable_web_page_preview=True)
        if markup:
            params["reply_markup"] = markup
        for i in range(3):
            try:
                return await self.call("sendMessage", **params)
            except Exception as e:
                log.warning("send failed: %s", e)
                await asyncio.sleep(2 ** i)

    async def edit(self, msg_id, text, markup=None):
        try:
            await self.call("editMessageText", chat_id=TG_ADMIN, message_id=msg_id, text=text, parse_mode="HTML",
                            disable_web_page_preview=True, reply_markup=markup or {"inline_keyboard": []})
        except RuntimeError as e:
            if "not modified" in str(e):
                return
            log.info("edit failed (%s), sending new message", e)
            await self.send(text, markup)


def fmt_amount(a: Decimal) -> str:
    s = f"{a:,.6f}".rstrip("0").rstrip(".")
    return s.replace(",", " ")


def short(a: str) -> str:
    return f"{a[:6]}…{a[-4:]}"


async def notify(tg: TG, db: DB, chain_name, explorer, tracked, label, direction, peer, symbol, amount, txid):
    arrow = "📥 Входящий" if direction == "in" else "📤 Исходящий"
    who = f"{html.escape(label)} " if label else ""
    text = (
        f"{arrow} · <b>{fmt_amount(amount)} {symbol}</b>\n"
        f"Сеть: {chain_name}\n"
        f"Кошелёк: {who}<code>{tracked}</code>\n"
        f"{'От' if direction == 'in' else 'Кому'}: <code>{peer}</code>"
    )
    await tg.send(text, kb([[("🔗 Транзакция", explorer + txid), ("📋 Мои адреса", "list:0")]]))


async def handle_transfer(tg, db, chain, chain_name, explorer, uid, tracked, label, direction, peer, symbol,
                          amount, txid, ts=None):
    if not db.mark_seen(uid):
        return
    reason = scam_reason(db, chain, tracked, direction, peer, amount)
    db.save_tx(uid, chain, tracked, txid, ts or int(time.time()), direction, peer, symbol, amount, reason)
    if reason:
        log.info("skip %s %s %s %s %s: %s", chain, tracked, direction, amount, symbol, reason)
        return
    db.add_peer(chain, tracked, norm(peer))
    await notify(tg, db, chain_name, explorer, tracked, label, direction, peer, symbol, amount, txid)


# ---------------------------------------------------------------- EVM
class RPC:
    def __init__(self, client, urls):
        self.c, self.urls = client, [u.strip() for u in urls if u.strip()]

    async def call(self, method, params):
        last = None
        for u in self.urls:
            try:
                r = await self.c.post(u, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                                      timeout=25)
                d = r.json()
                if "error" in d:
                    raise RuntimeError(d["error"])
                return d["result"]
            except Exception as e:
                last = e
                log.debug("rpc %s %s failed: %s", u, method, e)
        raise RuntimeError(f"all RPCs failed for {method}: {last}")


def topic(addr):
    return "0x" + "0" * 24 + addr[2:].lower()


async def poll_evm(tg: TG, db: DB, client, chain):
    cfg = EVM_CHAINS[chain]
    rows = db.addresses(chain)
    rpc = RPC(client, cfg["rpc"])
    head = int(await rpc.call("eth_blockNumber", []), 16) - cfg["confirmations"]
    key = f"{chain}_last_block"
    last = int(db.get(key, head))
    if not rows:
        db.set(key, head)
        return
    labels = {norm(a): l for _, a, l, _ in rows}
    topics = [topic(a) for _, a, _, _ in rows]
    tokens = list(cfg["tokens"])
    start = last + 1
    # do not fall too far behind (e.g. after long downtime): cap backlog to 2000 blocks
    start = max(start, head - 2000)
    while start <= head:
        end = min(start + cfg["chunk"] - 1, head)
        logs = []
        for tq in ([TRANSFER_TOPIC, topics], [TRANSFER_TOPIC, None, topics]):
            logs += await rpc.call("eth_getLogs", [{"fromBlock": hex(start), "toBlock": hex(end),
                                                    "address": tokens, "topics": tq}])
        logs.sort(key=lambda l: (int(l["blockNumber"], 16), int(l["logIndex"], 16)))
        for lg in logs:
            if len(lg["topics"]) != 3:
                continue
            sym, dec = cfg["tokens"][lg["address"].lower()]
            frm = "0x" + lg["topics"][1][-40:]
            to = "0x" + lg["topics"][2][-40:]
            amount = Decimal(int(lg["data"], 16)) / Decimal(10 ** dec)
            uid = f"{chain}:{lg['transactionHash']}:{int(lg['logIndex'], 16)}"
            for direction, tracked, peer in (("out", frm, to), ("in", to, frm)):
                if tracked in labels:
                    await handle_transfer(tg, db, chain, cfg["name"], cfg["explorer"], uid + direction,
                                          tracked, labels[tracked], direction, peer, sym, amount,
                                          lg["transactionHash"])
        db.set(key, end)
        start = end + 1


# ---------------------------------------------------------------- TRON
async def poll_tron(tg: TG, db: DB, client):
    headers = {"TRON-PRO-API-KEY": TRONGRID_KEY} if TRONGRID_KEY else {}
    for _, addr, label, added in db.addresses("tron"):
        key = f"tron_ts_{addr}"
        since = int(db.get(key, added * 1000))
        r = await client.get(f"{TRONGRID}/v1/accounts/{addr}/transactions/trc20",
                             params={"only_confirmed": "true", "min_timestamp": since + 1, "limit": 200,
                                     "order_by": "block_timestamp,asc"},
                             headers=headers, timeout=25)
        d = r.json()
        if not d.get("success"):
            raise RuntimeError(f"trongrid: {d}")
        newest = since
        for tx in d.get("data", []):
            newest = max(newest, tx["block_timestamp"])
            contract = tx["token_info"]["address"]
            if contract not in TRON_TOKENS or tx.get("type") != "Transfer":
                continue
            sym, dec = TRON_TOKENS[contract]
            amount = Decimal(int(tx["value"])) / Decimal(10 ** dec)
            direction = "out" if tx["from"] == addr else "in"
            peer = tx["to"] if direction == "out" else tx["from"]
            uid = f"tron:{tx['transaction_id']}:{direction}:{tx['from']}:{tx['to']}:{tx['value']}"
            await handle_transfer(tg, db, "tron", "TRON (TRC20)", TRON_EXPLORER, uid, addr, label, direction,
                                  peer, sym, amount, tx["transaction_id"], tx["block_timestamp"] // 1000)
        db.set(key, newest)
        await asyncio.sleep(0.5 if TRONGRID_KEY else 1.2)  # free tier rate limit



# ---------------------------------------------------------------- balances
# Verified token contracts shown on the address card. Anything not listed is hidden (fakes/airdrop scam).
BALANCE_TOKENS = {
    "eth": {
        "0xdac17f958d2ee523a2206206994597c13d831ec7": ("USDT", 6),
        "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": ("USDC", 6),
        "0x6b175474e89094c44da98b954eedeac495271d0f": ("DAI", 18),
        "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": ("WETH", 18),
        "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599": ("WBTC", 8),
    },
    "bsc": {
        "0x55d398326f99059ff775485246999027b3197955": ("USDT", 18),
        "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d": ("USDC", 18),
        "0x1af3f329e8be154074d8769d1ffa4ee058b1dbc3": ("DAI", 18),
        "0xe9e7cea3dedca5984780bafc599bd69add087d56": ("BUSD", 18),
        "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c": ("WBNB", 18),
        "0x2170ed0880ac9a755fd29b2688956bd959f933f8": ("ETH", 18),
        "0x7130d2a12b9bcbfae4f2634d864a1ee1ce3ead9c": ("BTCB", 18),
    },
    "tron": {
        "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t": ("USDT", 6),
        "TEkxiTehnzSmSe2XqrBj4w32RtfS3Q5Ldw8": ("USDC", 6),
        "TPYmHEhy5n8TCEfYGqW2rPxsghSfzghPDn": ("USDD", 18),
        "TNUC9Qb1rRpS5CbWLmNMxXBjyFoydXjWFR": ("WTRX", 6),
    },
}
NATIVE = {"eth": ("ETH", 18), "bsc": ("BNB", 18), "tron": ("TRX", 6)}


async def fetch_balances(client, chain, addr):
    """Returns (native_amount, [(symbol, amount), ...]) — only verified tokens, non-zero."""
    toks = BALANCE_TOKENS[chain]
    nsym, ndec = NATIVE[chain]
    out = []
    if chain == "tron":
        headers = {"TRON-PRO-API-KEY": TRONGRID_KEY} if TRONGRID_KEY else {}
        r = await client.get(f"{TRONGRID}/v1/accounts/{addr}", headers=headers, timeout=25)
        d = r.json()
        if not d.get("success"):
            raise RuntimeError(f"trongrid: {d.get('error', d)}")
        acc = (d.get("data") or [{}])[0]
        native = Decimal(acc.get("balance", 0)) / Decimal(10 ** ndec)
        held = {}
        for item in acc.get("trc20", []):
            held.update(item)
        for contract, (sym, dec) in toks.items():
            v = int(held.get(contract, 0) or 0)
            if v:
                out.append((sym, Decimal(v) / Decimal(10 ** dec)))
        return native, out
    rpc = RPC(client, EVM_CHAINS[chain]["rpc"])
    data = "0x70a08231" + "0" * 24 + addr[2:].lower()

    async def bal(contract):
        return contract, int(await rpc.call("eth_call", [{"to": contract, "data": data}, "latest"]) or "0x0", 16)

    native_raw, *tok = await asyncio.gather(rpc.call("eth_getBalance", [addr, "latest"]),
                                            *(bal(c) for c in toks))
    native = Decimal(int(native_raw, 16)) / Decimal(10 ** ndec)
    for contract, v in tok:
        if v:
            sym, dec = toks[contract]
            out.append((sym, Decimal(v) / Decimal(10 ** dec)))
    return native, out


def fmt_balances(chain, bal):
    if isinstance(bal, Exception):
        return f"\n\n⚠️ Баланс не получен: {html.escape(str(bal)[:150])}"
    native, toks = bal
    lines = [f"\n\n<b>💰 Баланс</b>", f"{NATIVE[chain][0]}: <b>{fmt_amount(native.quantize(Decimal('0.000001')))}</b>"]
    lines += [f"{sym}: <b>{fmt_amount(a.quantize(Decimal('0.000001')))}</b>" for sym, a in toks]
    if not toks:
        lines.append("<i>Проверенных токенов нет</i>")
    lines.append("<i>Показаны только проверенные контракты, скам-токены скрыты</i>")
    return "\n".join(lines)



# ---------------------------------------------------------------- history
HISTORY_LIMIT = 50
TX_PAGE = 10


def filter_history(db: DB, chain, addr, txs):
    """Drops scam-like transfers. Returns (clean, hidden_count). txs sorted newest first."""
    # trusted = addresses you actually sent money to + counterparties seen live + own addresses
    trusted = {norm(t["peer"]) for t in txs if t["direction"] == "out" and t["amount"] > 0}
    trusted |= set(db.peers(chain, addr)) | {norm(a) for _, a, _, _ in db.addresses()}
    clean, hidden = [], 0
    for t in txs:
        peer = norm(t["peer"])
        bad = (t["amount"] == 0
               or (t["direction"] == "in" and t["amount"] < MIN_AMOUNT)
               or (t["direction"] == "in" and peer not in trusted
                   and any(looks_like(peer, p) for p in trusted)))
        if bad:
            hidden += 1
        else:
            clean.append(t)
    return clean, hidden


async def _etherscan_tokentx(client, url, params, chain, addr):
    toks = BALANCE_TOKENS[chain]
    r = await client.get(url, params={"module": "account", "action": "tokentx", "address": addr, "page": 1,
                                      "offset": 200, "sort": "desc", **params}, timeout=30)
    d = r.json()
    if d.get("status") != "1":
        if "No transactions" in str(d.get("message")):
            return []
        raise RuntimeError(f"history api: {d.get('result') or d.get('message')}")
    out = []
    for x in d["result"]:
        c = x["contractAddress"].lower()
        if c not in toks:  # fake / unknown tokens
            continue
        sym, dec = toks[c]
        out_dir = x["from"].lower() == addr
        out.append(dict(txid=x["hash"], ts=int(x["timeStamp"]), direction="out" if out_dir else "in",
                        peer=x["to"] if out_dir else x["from"], symbol=sym,
                        amount=Decimal(int(x["value"])) / Decimal(10 ** dec)))
    return out


async def fetch_history(client, db: DB, chain, addr):
    """Returns (txs newest first, source description)."""
    if chain == "tron":
        headers = {"TRON-PRO-API-KEY": TRONGRID_KEY} if TRONGRID_KEY else {}
        r = await client.get(f"{TRONGRID}/v1/accounts/{addr}/transactions/trc20",
                             params={"only_confirmed": "true", "limit": 200, "order_by": "block_timestamp,desc"},
                             headers=headers, timeout=30)
        d = r.json()
        if not d.get("success"):
            raise RuntimeError(f"trongrid: {d.get('error', d)}")
        out = []
        for x in d.get("data", []):
            c = x["token_info"]["address"]
            if c not in BALANCE_TOKENS["tron"] or x.get("type") != "Transfer":
                continue
            sym, dec = BALANCE_TOKENS["tron"][c]
            o = x["from"] == addr
            out.append(dict(txid=x["transaction_id"], ts=x["block_timestamp"] // 1000,
                            direction="out" if o else "in", peer=x["to"] if o else x["from"], symbol=sym,
                            amount=Decimal(int(x["value"])) / Decimal(10 ** dec)))
        return out, "TronGrid"
    if chain == "eth":
        return await _etherscan_tokentx(client, ETH_HISTORY_API, {}, chain, addr), "Routescan"
    if ETHERSCAN_API_KEY:
        return (await _etherscan_tokentx(client, "https://api.etherscan.io/v2/api",
                                         {"chainid": 56, "apikey": ETHERSCAN_API_KEY}, chain, addr), "Etherscan")
    return db.local_txs(chain, addr, HISTORY_LIMIT), "local"


def screen_txs(db: DB, rid, txs, hidden, source, page):
    r = db.row(rid)
    _, chain, addr, label, _ = r
    explorer = EVM_CHAINS[chain]["explorer"] if chain in EVM_CHAINS else TRON_EXPLORER
    title = html.escape(label) if label else short(addr)
    head = f"<b>📜 {CHAIN_ICON[chain]} {title} — последние операции</b>\n"
    pages = max(1, (len(txs) + TX_PAGE - 1) // TX_PAGE)
    page = max(0, min(page, pages - 1))
    lines = []
    for t in txs[page * TX_PAGE:(page + 1) * TX_PAGE]:
        sign, arrow = ("+", "📥") if t["direction"] == "in" else ("−", "📤")
        when = time.strftime("%d.%m %H:%M", time.localtime(t["ts"]))
        lines.append(f"{arrow} <b>{sign}{fmt_amount(t['amount'])} {t['symbol']}</b> · {when}\n"
                     f"    {'от' if t['direction'] == 'in' else 'кому'} <code>{short(t['peer'])}</code> · "
                     f"<a href=\"{explorer}{t['txid']}\">tx</a>")
    body = "\n".join(lines) if lines else "<i>Операций не найдено</i>"
    foot = []
    if hidden:
        foot.append(f"🚫 Скрыто скам-операций: {hidden}")
    if source == "local":
        foot.append("<i>BSC: только операции, замеченные трекером с момента добавления. "
                    "Полная история — с ETHERSCAN_API_KEY в .env.</i>")
    text = head + "\n" + body + ("\n\n" + "\n".join(foot) if foot else "")
    nav = []
    if page > 0:
        nav.append(("◀️ Новее", f"txs:{rid}:{page - 1}"))
    if page < pages - 1:
        nav.append(("Старее ▶️", f"txs:{rid}:{page + 1}"))
    rows = ([nav] if nav else []) + [[("🔄 Обновить", f"txs:{rid}:0"), ("⬅️ К адресу", f"addr:{rid}")]]
    return text, kb(rows)


# ---------------------------------------------------------------- bot UI (inline menu)
CHAIN_TITLE = {"eth": "Ethereum", "bsc": "BSC", "tron": "TRON"}
CHAIN_ICON = {"eth": "🔷", "bsc": "🟡", "tron": "🔴"}
PAGE = 8


def kb(rows):
    """rows: [[(text, callback_data_or_url), ...], ...]"""
    out = []
    for r in rows:
        out.append([{"text": t, "url": d} if d.startswith("http") else {"text": t, "callback_data": d}
                    for t, d in r])
    return {"inline_keyboard": out}


def explorer_addr(chain, addr):
    return {"eth": "https://etherscan.io/address/", "bsc": "https://bscscan.com/address/",
            "tron": "https://tronscan.org/#/address/"}[chain] + addr


def screen_main(db: DB):
    rows = db.rows()
    by = {c: sum(1 for r in rows if r[1] == c) for c in CHAIN_TITLE}
    text = ("<b>💼 Трекер USDT / USDC</b>\n\n"
            + "\n".join(f"{CHAIN_ICON[c]} {CHAIN_TITLE[c]}: {by[c]}" for c in CHAIN_TITLE)
            + f"\n\nОпрос каждые {POLL_INTERVAL} с · порог входящих {MIN_AMOUNT}")
    return text, kb([[("➕ Добавить адрес", "add")],
                     [("📋 Мои адреса", "list:0")],
                     [("⚙️ Настройки и фильтры", "info"), ("🔄", "main")]])


def screen_list(db: DB, page: int):
    rows = db.rows()
    if not rows:
        return "Список пуст.", kb([[("➕ Добавить адрес", "add")], [("⬅️ Меню", "main")]])
    pages = (len(rows) + PAGE - 1) // PAGE
    page = max(0, min(page, pages - 1))
    btns = []
    for rid, chain, addr, label, _ in rows[page * PAGE:(page + 1) * PAGE]:
        name = label or short(addr)
        btns.append([(f"{CHAIN_ICON[chain]} {name}" + (f" · {short(addr)}" if label else ""), f"addr:{rid}")])
    nav = []
    if page > 0:
        nav.append(("◀️", f"list:{page - 1}"))
    if pages > 1:
        nav.append((f"{page + 1}/{pages}", f"list:{page}"))
    if page < pages - 1:
        nav.append(("▶️", f"list:{page + 1}"))
    if nav:
        btns.append(nav)
    btns.append([("➕ Добавить", "add"), ("⬅️ Меню", "main")])
    return f"<b>📋 Отслеживаемые адреса</b> ({len(rows)})", kb(btns)


def screen_addr(db: DB, rid: int, bal=None):
    r = db.row(rid)
    if not r:
        return "Адрес не найден.", kb([[("⬅️ К списку", "list:0")]])
    _, chain, addr, label, added = r
    text = (f"{CHAIN_ICON[chain]} <b>{html.escape(label) if label else 'Без названия'}</b>\n"
            f"Сеть: {CHAIN_TITLE[chain]}\n<code>{addr}</code>\n"
            f"Добавлен: {time.strftime('%d.%m.%Y %H:%M', time.localtime(added))}")
    if bal is not None:
        text += fmt_balances(chain, bal)
    return text, kb([[("✏️ " + ("Переименовать" if label else "Дать название"), f"ren:{rid}"),
                      ("🗑 Удалить", f"del:{rid}")],
                     [("📜 Транзакции", f"txs:{rid}:0")],
                     [("🔄 Обновить баланс", f"addr:{rid}"), ("🔗 Обозреватель", explorer_addr(chain, addr))],
                     [("⬅️ К списку", "list:0")]])


def screen_info():
    text = ("<b>⚙️ Настройки</b> (меняются в .env)\n\n"
            f"Интервал опроса: {POLL_INTERVAL} с\n"
            f"Порог входящих: {MIN_AMOUNT}\n"
            "Токены: USDT, USDC (только оригинальные контракты)\n\n"
            "<b>Скрываются:</b>\n"
            "• поддельные токены с похожим названием\n"
            "• переводы на 0\n"
            f"• входящие меньше {MIN_AMOUNT}\n"
            "• адреса-двойники (совпадают первые и последние 4 символа с вашими контрагентами)")
    return text, kb([[("⬅️ Меню", "main")]])


ADD_PROMPT = ("<b>➕ Новый адрес</b>\n\nОтправьте адрес сообщением. Название можно указать сразу:\n"
              "<code>0x… Биржа</code> или <code>T… Холодный</code>\n"
              "или ввести на следующем шаге.\n\n"
              "0x… — Ethereum и/или BSC, T… — TRON.")
NAME_RULES = "до 40 символов"


class UI:
    """Holds pending dialog state (awaiting address / label input)."""

    def __init__(self, tg: TG, db: DB, client=None):
        self.tg, self.db, self.client = tg, db, client
        self.tx_cache = {}  # rid -> (fetched_at, txs, hidden, source)
        self.pending = None  # ("add", msg_id) | ("ren", rid, msg_id) | ("pick", addr, label, msg_id)

    async def card(self, msg_id, rid):
        """Address card with live balance (shows a loading state first)."""
        r = self.db.row(rid)
        if not r:
            return await self.show(msg_id, screen_list(self.db, 0))
        if msg_id:
            await self.tg.edit(msg_id, screen_addr(self.db, rid)[0] + "\n\n⏳ Загружаю баланс…",
                               screen_addr(self.db, rid)[1])
        try:
            bal = await fetch_balances(self.client, r[1], r[2])
        except Exception as e:
            log.warning("balance %s %s: %s", r[1], r[2], e)
            bal = e
        if msg_id:
            await self.tg.edit(msg_id, *screen_addr(self.db, rid, bal))
        else:
            await self.tg.send(*screen_addr(self.db, rid, bal))

    async def history(self, msg_id, rid, page):
        r = self.db.row(rid)
        if not r:
            return await self.show(msg_id, screen_list(self.db, 0))
        cached = self.tx_cache.get(rid)
        if page == 0 or not cached or time.time() - cached[0] > 300:
            await self.tg.edit(msg_id, "⏳ Загружаю операции…", kb([[("⬅️ К адресу", f"addr:{rid}")]]))
            try:
                txs, source = await fetch_history(self.client, self.db, r[1], r[2])
            except Exception as e:
                log.warning("history %s %s: %s", r[1], r[2], e)
                return await self.tg.edit(msg_id, f"⚠️ История не получена: {html.escape(str(e)[:200])}",
                                          kb([[("🔄 Повторить", f"txs:{rid}:0"), ("⬅️ К адресу", f"addr:{rid}")]]))
            txs.sort(key=lambda t: t["ts"], reverse=True)
            clean, hidden = filter_history(self.db, r[1], r[2], txs)
            cached = (time.time(), clean[:HISTORY_LIMIT], hidden, source)
            self.tx_cache[rid] = cached
        _, txs, hidden, source = cached
        await self.tg.edit(msg_id, *screen_txs(self.db, rid, txs, hidden, source, page))

    async def show(self, msg_id, screen):
        text, markup = screen
        if msg_id:
            await self.tg.edit(msg_id, text, markup)
        else:
            await self.tg.send(text, markup)

    async def on_callback(self, cq):
        data = cq.get("data", "")
        msg_id = cq["message"]["message_id"]
        await self.tg.call("answerCallbackQuery", callback_query_id=cq["id"])
        cmd, _, arg = data.partition(":")
        if cmd not in ("pick", "skipname"):
            self.pending = None
        if cmd == "main":
            return await self.show(msg_id, screen_main(self.db))
        if cmd == "list":
            return await self.show(msg_id, screen_list(self.db, int(arg or 0)))
        if cmd == "addr":
            return await self.card(msg_id, int(arg))
        if cmd == "info":
            return await self.show(msg_id, screen_info())
        if cmd == "txs":
            rid, page = (int(x) for x in arg.split(":"))
            return await self.history(msg_id, rid, page)
        if cmd == "add":
            self.pending = ("add", msg_id)
            return await self.show(msg_id, (ADD_PROMPT, kb([[("✖️ Отмена", "main")]])))
        if cmd == "pick" and self.pending and self.pending[0] == "pick":
            _, addr, label, _ = self.pending
            self.pending = None
            chains = {"eth": ["eth"], "bsc": ["bsc"], "both": ["eth", "bsc"]}[arg]
            return await self.do_add(msg_id, addr, label, chains)
        if cmd == "ren":
            r = self.db.row(int(arg))
            if not r:
                return await self.show(msg_id, screen_list(self.db, 0))
            self.pending = ("ren", int(arg), msg_id)
            cur = f"Сейчас: <b>{html.escape(r[3])}</b>\n" if r[3] else ""
            btns = [[("🧹 Убрать название", f"unname:{arg}")]] if r[3] else []
            return await self.show(msg_id, (f"✏️ Название для <code>{r[2]}</code>\n{cur}"
                                            f"Отправьте новое название ({NAME_RULES}).\n"
                                            "Применится во всех сетях этого адреса.",
                                            kb(btns + [[("✖️ Отмена", f"addr:{arg}")]])))
        if cmd == "unname":
            self.db.rename(int(arg), "")
            return await self.card(msg_id, int(arg))
        if cmd == "skipname" and self.pending and self.pending[0] == "name":
            _, addr, chains, _ = self.pending
            self.pending = None
            return await self.finish_add(msg_id, addr, "", chains)
        if cmd == "del":
            r = self.db.row(int(arg))
            if not r:
                return await self.show(msg_id, screen_list(self.db, 0))
            return await self.show(msg_id, (f"Удалить {CHAIN_ICON[r[1]]} <code>{r[2]}</code> "
                                            f"из {CHAIN_TITLE[r[1]]}?",
                                            kb([[("🗑 Да, удалить", f"delok:{arg}"), ("✖️ Нет", f"addr:{arg}")]])))
        if cmd == "delok":
            self.db.delete_row(int(arg))
            return await self.show(msg_id, screen_list(self.db, 0))
        await self.show(msg_id, screen_main(self.db))

    async def do_add(self, msg_id, addr, label, chains):
        label = label or self.db.label_of(addr)
        if not label:
            text = (f"<code>{addr}</code>\nКак назвать этот адрес? Отправьте название ({NAME_RULES}).\n"
                    "Оно будет в списке и в уведомлениях.")
            markup = kb([[("⏭ Без названия", "skipname")], [("✖️ Отмена", "main")]])
            if msg_id:
                await self.tg.edit(msg_id, text, markup)
            else:
                m = await self.tg.send(text, markup)
                msg_id = m["message_id"] if m else None
            self.pending = ("name", addr, chains, msg_id)
            return
        await self.finish_add(msg_id, addr, label, chains)

    async def finish_add(self, msg_id, addr, label, chains):
        for c in chains:
            self.db.add(c, addr, label)
        text = (f"✅ Добавлен: {', '.join(CHAIN_ICON[c] + ' ' + CHAIN_TITLE[c] for c in chains)}\n"
                + (f"<b>{html.escape(label)}</b>\n" if label else "")
                + f"<code>{addr}</code>\n\nУведомления — о новых операциях с этого момента.")
        await self.show(msg_id, (text, kb([[("➕ Ещё адрес", "add"), ("📋 Мои адреса", "list:0")],
                                           [("⬅️ Меню", "main")]])))

    async def on_text(self, text):
        text = text.strip()
        if text.startswith("/"):
            self.pending = None
            cmd = text.split()[0].split("@")[0].lower()
            if cmd == "/list":
                return await self.show(None, screen_list(self.db, 0))
            if cmd == "/add":
                rest = text.split(maxsplit=1)[1] if " " in text else ""
                if rest:
                    return await self.parse_address(None, rest)
                msg = await self.tg.send(ADD_PROMPT, kb([[("✖️ Отмена", "main")]]))
                self.pending = ("add", msg["message_id"] if msg else None)
                return
            return await self.show(None, screen_main(self.db))
        if not self.pending:
            # bare address without pressing "Add" — handle it anyway
            if EVM_RE.match(text.split()[0]) or TRON_RE.match(text.split()[0]):
                return await self.parse_address(None, text)
            return await self.show(None, screen_main(self.db))
        kind = self.pending[0]
        if kind == "add":
            msg_id = self.pending[1]
            self.pending = None
            return await self.parse_address(None, text, retry_on_fail=True, old_msg=msg_id)
        if kind == "ren":
            _, rid, msg_id = self.pending
            self.pending = None
            self.db.rename(rid, "" if text == "-" else text[:40])
            return await self.card(None, rid)
        if kind == "name":
            _, addr, chains, msg_id = self.pending
            self.pending = None
            return await self.finish_add(None, addr, text[:40], chains)

    async def parse_address(self, msg_id, text, retry_on_fail=False, old_msg=None):
        parts = text.split(maxsplit=1)
        addr, label = parts[0], (parts[1][:40] if len(parts) > 1 else "")
        if EVM_RE.match(addr):
            addr = addr.lower()
            msg = await self.tg.send(f"<code>{addr}</code>\nВ какой сети отслеживать?",
                                     kb([[("🔷 Ethereum", "pick:eth"), ("🟡 BSC", "pick:bsc")],
                                         [("🔷🟡 Обе сети", "pick:both")], [("✖️ Отмена", "main")]]))
            self.pending = ("pick", addr, label, msg["message_id"] if msg else None)
            return
        if TRON_RE.match(addr):
            return await self.do_add(None, addr, label, ["tron"])
        if retry_on_fail:
            msg = await self.tg.send("❌ Не похоже на адрес. Отправьте 0x… или T… ещё раз.",
                                     kb([[("✖️ Отмена", "main")]]))
            self.pending = ("add", msg["message_id"] if msg else None)
            return
        await self.tg.send("❌ Неверный адрес.", kb([[("⬅️ Меню", "main")]]))


async def bot_loop(tg: TG, db: DB):
    await tg.call("setMyCommands", commands=[
        {"command": "start", "description": "Меню"},
        {"command": "add", "description": "Добавить адрес"},
        {"command": "list", "description": "Мои адреса"},
    ])
    ui = UI(tg, db, tg.c)
    offset = int(db.get("tg_offset", 0))
    while True:
        try:
            updates = await tg.call("getUpdates", offset=offset, timeout=50,
                                    allowed_updates=["message", "callback_query"])
            for u in updates:
                offset = u["update_id"] + 1
                db.set("tg_offset", offset)
                ev = u.get("callback_query") or u.get("message") or {}
                if ev.get("from", {}).get("id") != TG_ADMIN:
                    continue  # single-admin bot: ignore everyone else silently
                try:
                    if "callback_query" in u:
                        await ui.on_callback(ev)
                    elif ev.get("text"):
                        await ui.on_text(ev["text"])
                except Exception as e:
                    log.exception("ui failed")
                    await tg.send(f"Ошибка: {html.escape(str(e))}")
        except Exception as e:
            log.warning("getUpdates: %s", e)
            await asyncio.sleep(5)


async def watch_loop(tg: TG, db: DB, client):
    errors = {}
    tick = 0
    while True:
        for name, coro in (("eth", lambda: poll_evm(tg, db, client, "eth")),
                           ("bsc", lambda: poll_evm(tg, db, client, "bsc")),
                           ("tron", lambda: poll_tron(tg, db, client))):
            try:
                await coro()
                if errors.get(name, 0) >= 10:
                    await tg.send(f"✅ {name.upper()}: опрос восстановлен")
                errors[name] = 0
            except Exception as e:
                errors[name] = errors.get(name, 0) + 1
                log.warning("%s poll error (%d): %s", name, errors[name], e)
                if errors[name] == 10:
                    await tg.send(f"⚠️ {name.upper()}: 10 ошибок опроса подряд: {html.escape(str(e)[:200])}")
        tick += 1
        if tick % 1000 == 0:
            db.prune_seen()
        await asyncio.sleep(POLL_INTERVAL)


async def main():
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    db = DB(DB_PATH)
    async with httpx.AsyncClient() as client:
        tg = TG(client)
        log.info("started, %d addresses", len(db.addresses()))
        await asyncio.gather(bot_loop(tg, db), watch_loop(tg, db, client))


if __name__ == "__main__":
    asyncio.run(main())
