"""Live check: token contracts are what we think they are + balances fetch works."""
import asyncio
import httpx
import tracker as t

SAMPLES = {"eth": "0xf977814e90da44bfa03b6295a0616a897441acec",
           "bsc": "0xf977814e90da44bfa03b6295a0616a897441acec",
           "tron": "TNXoiAJ3dct8Fjg4M9fkLFh9S2v9TXc32G"}


async def main():
    async with httpx.AsyncClient() as c:
        for chain in ("eth", "bsc"):
            rpc = t.RPC(c, t.EVM_CHAINS[chain]["rpc"])
            for addr, (sym, dec) in t.BALANCE_TOKENS[chain].items():
                raw_s = await rpc.call("eth_call", [{"to": addr, "data": "0x95d89b41"}, "latest"])
                raw_d = await rpc.call("eth_call", [{"to": addr, "data": "0x313ce567"}, "latest"])
                b = bytes.fromhex(raw_s[2:])
                onchain = b[64:64 + int.from_bytes(b[32:64], "big")].decode() if len(b) > 64 else b.rstrip(b"\0").decode()
                ok = onchain.upper().replace("-", "") .startswith(sym[:3]) and int(raw_d, 16) == dec
                print(chain, sym, "onchain:", onchain, int(raw_d, 16), "OK" if ok else "MISMATCH")
        for chain, a in SAMPLES.items():
            n, toks = await t.fetch_balances(c, chain, a)
            print(chain, a[:10], t.NATIVE[chain][0], n, toks)

asyncio.run(main())
