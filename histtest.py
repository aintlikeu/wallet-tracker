"""Live check of transaction history for each chain (no Telegram)."""
import asyncio, os, tempfile, time
from pathlib import Path
os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "h.db")
import httpx
import tracker as t

SAMPLES = {"eth": "0x7652929c6e3a3c7a5c5934e6ea2e4f4c4931276b",
           "tron": "TNXoiAJ3dct8Fjg4M9fkLFh9S2v9TXc32G",
           "bsc": "0x302642976506d247c7017b85ddc89e3956f96ba1"}


async def main():
    db = t.DB(Path(os.environ["DB_PATH"]))
    async with httpx.AsyncClient() as c:
        for chain, a in SAMPLES.items():
            db.add(chain, a, "")
            txs, src = await t.fetch_history(c, db, chain, a)
            txs.sort(key=lambda x: x["ts"], reverse=True)
            clean, hidden = t.filter_history(db, chain, a, txs)
            print(chain, src, "raw", len(txs), "clean", len(clean), "hidden", hidden)
            rid = db.rows()[-1][0] if False else [r[0] for r in db.rows() if r[1] == chain][0]
            text, _ = t.screen_txs(db, rid, clean[:t.HISTORY_LIMIT], hidden, src, 0)
            print("\n".join(text.splitlines()[:5]))
    # poisoning in history
    txs = [dict(txid="a", ts=2, direction="out", peer="0x1234aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa5678", symbol="USDT", amount=t.Decimal(100)),
           dict(txid="b", ts=1, direction="in", peer="0x1234bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb5678", symbol="USDT", amount=t.Decimal(0)),
           dict(txid="c", ts=3, direction="in", peer="0x1234cccccccccccccccccccccccccccccccc5678", symbol="USDT", amount=t.Decimal(5))]
    clean, hidden = t.filter_history(db, "eth", "0xdead", txs)
    assert [x["txid"] for x in clean] == ["a"] and hidden == 2, (clean, hidden)
    print("poison filter: clean", [x["txid"] for x in clean], "hidden", hidden)

asyncio.run(main())
