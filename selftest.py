"""Self-test against live chains with a temp DB and a fake Telegram sender.

Picks currently active USDT addresses, rewinds the cursor a few minutes and
checks that transfers are found, parsed and filtered. Sends nothing to Telegram.
"""
import asyncio
import os
import tempfile
import time
from decimal import Decimal
from pathlib import Path

os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "t.db")
import httpx  # noqa: E402

import tracker as t  # noqa: E402


class FakeTG:
    def __init__(self):
        self.sent = []

    sent_full = []

    async def send(self, text, markup=None):
        self.sent.append(text)
        self.sent_full.append((text, markup))


async def main():
    db = t.DB(Path(os.environ["DB_PATH"]))
    tg = FakeTG()
    async with httpx.AsyncClient() as c:
        # --- EVM: pick a recent USDT recipient on each chain
        for chain in ("eth", "bsc"):
            cfg = t.EVM_CHAINS[chain]
            rpc = t.RPC(c, cfg["rpc"])
            head = int(await rpc.call("eth_blockNumber", []), 16)
            usdt = next(a for a, (s, _) in cfg["tokens"].items() if s == "USDT")
            logs = await rpc.call("eth_getLogs", [{"fromBlock": hex(head - 25), "toBlock": hex(head - 10),
                                                   "address": usdt, "topics": [t.TRANSFER_TOPIC]}])
            addr = "0x" + logs[-1]["topics"][2][-40:]
            db.add(chain, addr, "test")
            db.set(f"{chain}_last_block", head - 40)
            await t.poll_evm(tg, db, c, chain)
            print(chain, addr, "notifications so far:", len(tg.sent))
        # --- TRON
        addr = "TNXoiAJ3dct8Fjg4M9fkLFh9S2v9TXc32G"
        db.add("tron", addr, "test")
        db.set(f"tron_ts_{addr}", int(time.time() * 1000) - 15 * 60 * 1000)
        await t.poll_tron(tg, db, c)
        print("tron notifications total:", len(tg.sent))
        # second run must not duplicate
        n = len(tg.sent)
        db.set(f"tron_ts_{addr}", int(time.time() * 1000) - 15 * 60 * 1000)
        await t.poll_tron(tg, db, c)
        import re as _re
        ids = [_re.search(r"transaction/([0-9a-f]+)", str(x)) for x in tg.sent_full]
        ids = [m.group(1) for m in ids if m]
        print("re-poll: new messages", len(tg.sent) - n, "| duplicate txids:", len(ids) - len(set(ids)))
    # --- scam filter unit checks
    db.add_peer("eth", "0xaaa", "0x1234567890abcdef1234567890abcdef12345678")
    assert t.scam_reason(db, "eth", "0xaaa", "in", "0x1234000000000000000000000000000000005678", Decimal(5)) == "poisoning"
    assert t.scam_reason(db, "eth", "0xaaa", "in", "0x9999000000000000000000000000000000005678", Decimal(5)) is None
    assert t.scam_reason(db, "eth", "0xaaa", "in", "0x9999000000000000000000000000000000005678", Decimal("0.5")) == "dust"
    assert t.scam_reason(db, "eth", "0xaaa", "out", "0x9999000000000000000000000000000000005678", Decimal(0)) == "zero-value"
    # outgoing to a lookalike is NOT hidden
    assert t.scam_reason(db, "eth", "0xaaa", "out", "0x1234000000000000000000000000000000005678", Decimal(5)) is None
    print("filter checks OK")
    print("\n--- sample notification ---\n" + (tg.sent[0] if tg.sent else "none"))


asyncio.run(main())
