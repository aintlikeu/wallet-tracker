"""Offline test of the inline menu: fake Telegram records every screen."""
import asyncio
import os
import tempfile
from pathlib import Path

os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "ui.db")
import tracker as t  # noqa: E402
from decimal import Decimal  # noqa: E402


async def _fake_bal(client, chain, addr):
    return Decimal("0.0123"), [("USDT", Decimal("150.5"))]

t.fetch_balances = _fake_bal


async def _fake_hist(client, db, chain, addr):
    now = 1791100000
    return [dict(txid=f"0x{i:064x}", ts=now - i * 60, direction="in" if i % 2 else "out",
                 peer=f"0x{i + 7:040x}", symbol="USDT", amount=Decimal(10 + i)) for i in range(25)], "Routescan"

t.fetch_history = _fake_hist


class FakeTG:
    def __init__(self):
        self.log, self.n = [], 100

    async def call(self, method, **p):
        return True

    async def send(self, text, markup=None):
        self.n += 1
        self.log.append(("send", text, markup))
        return {"message_id": self.n}

    async def edit(self, msg_id, text, markup=None):
        self.log.append(("edit", text, markup))


def buttons(markup):
    return [b.get("callback_data") or b.get("url") for row in (markup or {}).get("inline_keyboard", []) for b in row]


async def main():
    db = t.DB(Path(os.environ["DB_PATH"]))
    tg = FakeTG()
    ui = t.UI(tg, db)
    cq = lambda d: {"id": "x", "data": d, "message": {"message_id": 1}}  # noqa: E731
    last = lambda: tg.log[-1]  # noqa: E731

    await ui.on_text("/start")
    assert "add" in buttons(last()[2]) and "list:0" in buttons(last()[2])
    # add EVM address via menu -> pick chain
    await ui.on_callback(cq("add"))
    await ui.on_text("0xAbCdEf0000000000000000000000000000001234 Биржа")
    assert "pick:both" in buttons(last()[2])
    await ui.on_callback(cq("pick:both"))
    assert "Ethereum" in last()[1] and "BSC" in last()[1]
    # add TRON address straight from text
    await ui.on_text("TNXoiAJ3dct8Fjg4M9fkLFh9S2v9TXc32G Холодный")
    assert "TRON" in last()[1]
    # invalid address keeps waiting
    await ui.on_callback(cq("add"))
    await ui.on_text("hello")
    assert "Не похоже" in last()[1] and ui.pending[0] == "add"
    await ui.on_callback(cq("main"))
    assert ui.pending is None
    # list, open, rename, delete
    await ui.on_callback(cq("list:0"))
    rows = [b for b in buttons(last()[2]) if b.startswith("addr:")]
    assert len(rows) == 3, rows
    await ui.on_callback(cq(rows[0]))
    rid = rows[0].split(":")[1]
    assert f"ren:{rid}" in buttons(last()[2])
    assert "150.5" in last()[1] and "Баланс" in last()[1], last()[1]
    await ui.on_callback(cq(f"ren:{rid}"))
    await ui.on_text("Новая метка")
    assert "Новая метка" in last()[1]
    await ui.on_callback(cq(f"del:{rid}"))
    assert f"delok:{rid}" in buttons(last()[2])
    await ui.on_callback(cq(f"delok:{rid}"))
    assert len(db.rows()) == 2
    # pagination
    for i in range(12):
        db.add("eth", f"0x{i:040x}", f"w{i}")
    await ui.on_callback(cq("list:0"))
    assert "list:1" in buttons(last()[2])
    await ui.on_callback(cq("list:1"))
    assert "list:0" in buttons(last()[2])
    await ui.on_callback(cq("info"))
    assert "Скрываются" in last()[1]

    # --- naming
    # address without name -> name step -> name applied
    evm = "0x00000000000000000000000000000000000beef1"
    await ui.on_callback(cq("add"))
    await ui.on_text(evm)
    await ui.on_callback(cq("pick:both"))
    assert "Как назвать" in last()[1] and ui.pending[0] == "name"
    await ui.on_text("Ledger основной")
    assert "Ledger основной" in last()[1]
    assert {r[3] for r in db.rows() if r[2] == evm} == {"Ledger основной"}
    # skip name
    await ui.on_text("TLa2f6VPqDgRE67v1736s7bJ8Ray5wYjU7")
    assert "skipname" in buttons(last()[2])
    await ui.on_callback(cq("skipname"))
    assert "Добавлен" in last()[1]
    # rename one row -> renamed in both chains
    rid = next(r[0] for r in db.rows() if r[2] == evm)
    await ui.on_callback(cq(f"ren:{rid}"))
    assert "Сейчас" in last()[1] and f"unname:{rid}" in buttons(last()[2])
    await ui.on_text("Холодный")
    assert {r[3] for r in db.rows() if r[2] == evm} == {"Холодный"}
    # remove name
    await ui.on_callback(cq(f"unname:{rid}"))
    assert {r[3] for r in db.rows() if r[2] == evm} == {""}
    assert "Дать название" in str(last()[2])
    # re-adding to another chain keeps existing name
    db.rename(rid, "Биржа2")
    await ui.on_text(evm)
    await ui.on_callback(cq("pick:eth"))
    assert "Биржа2" in last()[1] and ui.pending is None
    print("naming OK")
    # --- history
    await ui.on_callback(cq(f"addr:{rid}"))
    assert f"txs:{rid}:0" in buttons(last()[2])
    await ui.on_callback(cq(f"txs:{rid}:0"))
    assert "последние операции" in last()[1] and f"txs:{rid}:1" in buttons(last()[2]), last()[1]
    await ui.on_callback(cq(f"txs:{rid}:2"))
    assert f"txs:{rid}:1" in buttons(last()[2]) and f"txs:{rid}:3" not in buttons(last()[2])
    print("history OK")
    print("UI OK, screens rendered:", len(tg.log))
    print("--- main ---\n" + t.screen_main(db)[0])


asyncio.run(main())
