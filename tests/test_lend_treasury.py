"""
Offline tests for lend_treasury.py: fake Jupiter / Kamino / RPC responses, real transaction signing.
Run: python tests/test_lend_treasury.py   (no network, no wallet, no money needed)
"""
import base64
import json

from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.signature import Signature
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import lend_treasury as lt  # noqa: E402

KP = Keypair()
JL_MINT = "9BEcn9aPEmhSPbPQeFGjidRiEKki46fVQDyPpSQXPA2D"
lt.time.sleep = lambda x: None


def unsigned_tx():
    ix = transfer(TransferParams(from_pubkey=KP.pubkey(), to_pubkey=Keypair().pubkey(), lamports=1))
    msg = MessageV0.try_compile(KP.pubkey(), [ix], [], Hash.default())
    return base64.b64encode(bytes(VersionedTransaction.populate(msg, [Signature.default()]))).decode()


class Resp:
    def __init__(self, code, data):
        self.status_code, self._d, self.text = code, data, json.dumps(data)

    def json(self):
        return self._d

    def raise_for_status(self):
        if self.status_code >= 400:
            raise lt.requests.HTTPError(self.status_code)


class FakeHTTP:
    def __init__(self):
        self.usdc, self.jup_shares, self.k_shares = 1000.0, 0, 0.0
        self.calls, self.sent = [], []
        self.kamino_down = False
        self.ata_exists = False
        self.created_ata = 0

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append((url, json, headers))
        if url == "RPC":
            m, p = json["method"], json["params"]
            if m == "getTokenAccountsByOwner":
                return Resp(200, {"result": {"value": [{"account": {"data": {"parsed": {"info": {"tokenAmount": {"uiAmount": self.usdc}}}}}}]}})
            if m == "getLatestBlockhash":
                return Resp(200, {"result": {"value": {"blockhash": str(Hash.new_unique())}}})
            if m == "getAccountInfo":
                if p[0] == JL_MINT:
                    return Resp(200, {"result": {"value": {"owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "data": ["", "base64"]}}})
                return Resp(200, {"result": {"value": {"owner": "x"} if self.ata_exists else None}})
            if m == "getBalance":
                return Resp(200, {"result": {"value": 50_000_000}})
            if m == "sendTransaction":
                tx = VersionedTransaction.from_bytes(base64.b64decode(p[0]))
                assert tx.signatures[0] != Signature.default() and tx.verify_with_results() == [True], "not signed by wallet"
                self.sent.append(tx)
                if str(tx.message.account_keys[-1]) == str(lt.ATA_PROGRAM) or lt.ATA_PROGRAM in tx.message.account_keys:
                    self.ata_exists = True
                    self.created_ata += 1
                    return Resp(200, {"result": f"ata{len(self.sent)}"})
                assert tx.message.recent_blockhash != Hash.default(), "blockhash must be replaced"
                self._apply()
                return Resp(200, {"result": f"sig{len(self.sent)}"})
            if m == "getSignatureStatuses":
                return Resp(200, {"result": {"value": [{"confirmationStatus": "confirmed", "err": None}]}})
        if "kamino" in url and self.kamino_down:
            return Resp(500, {"message": "down"})
        self.pending = (url, json)
        return Resp(200, {"transaction": unsigned_tx()})

    def _apply(self):                                             # simulate the effect of the last API tx
        url, body = self.pending
        if url.endswith("/earn/deposit"):
            amt = int(body["amount"]) / 1e6
            self.usdc -= amt
            self.jup_shares += int(body["amount"])
        elif url.endswith("/earn/redeem"):
            assert body["shares"] == str(self.jup_shares)
            self.usdc += self.jup_shares / 1e6 * 1.001
            self.jup_shares = 0
        elif url.endswith("/kvault/deposit"):
            self.usdc -= float(body["amount"])
            self.k_shares += float(body["amount"])
        elif url.endswith("/kvault/withdraw"):
            assert body["amount"] == str(self.k_shares), (body["amount"], self.k_shares)
            self.usdc += self.k_shares * 1.002
            self.k_shares = 0

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers))
        if url.endswith("/earn/positions"):           # like the real API: position exists even with 0 shares
            return Resp(200, [{"token": {"address": JL_MINT, "assetAddress": lt.USDC_MINT,
                                         "asset": {"address": lt.USDC_MINT}},
                               "shares": str(self.jup_shares), "underlyingAssets": str(self.jup_shares),
                               "underlyingBalance": str(int(self.usdc * 1e6))}])
        if "/kvaults/users/" in url:
            return Resp(200, {"totalShares": str(self.k_shares), "stakedShares": "0", "unstakedShares": str(self.k_shares)})
        raise AssertionError(url)


def make(http, vault="VAULT1"):
    return lt.Treasury(KP, "RPC", "KEY", vault, http=http)


def test_park_unpark():
    h = FakeHTTP()
    t = make(h)
    done = t.park()
    assert done == {"jup": 450.0, "kamino": 450.0}, done                  # 10 % buffer, 50/50 split
    assert abs(h.usdc - 100) < 1e-6 and h.jup_shares == 450_000_000 and h.k_shares == 450
    jup_calls = [c for c in h.calls if "jup.ag" in c[0]]
    assert all(c[2].get("x-api-key") == "KEY" for c in jup_calls)
    bal = t.unpark()
    assert abs(bal - (100 + 450 * 1.001 + 450 * 1.002)) < 1e-6 and h.jup_shares == 0 and h.k_shares == 0, bal
    assert len(h.sent) == 5 and h.created_ata == 1, (len(h.sent), h.created_ata)
    t.park()
    assert h.created_ata == 1, "token account must only be created once"
    print("OK  park: creates the jlUSDC token account once, 10 % buffer, 50/50 split, fresh blockhash; unpark: both")


def test_one_protocol_down():
    h = FakeHTTP()
    t = make(h)
    t.park()
    h.kamino_down = True
    bal = t.unpark()                                                       # Jupiter must still come back
    assert h.jup_shares == 0 and h.k_shares == 450 and bal > 500, (bal, h.k_shares)
    print("OK  Kamino down: Jupiter still withdrawn, error logged, bot can buy with what is there")


def test_small_and_no_kamino():
    h = FakeHTTP()
    h.usdc = 8
    assert make(h).park() == {}
    h2 = FakeHTTP()
    t = make(h2, vault="")
    assert t.park() == {"jup": 900.0}
    assert t.unpark() > 900
    print("OK  below minimum: nothing; no Kamino vault set: everything to Jupiter")


def test_fixed_buffer():
    h = FakeHTTP()
    t = lt.Treasury(KP, "RPC", "KEY", "VAULT1", http=h, buffer_usdc=20)
    assert t.park() == {"jup": 490.0, "kamino": 490.0}, "keeps exactly 20 USDC"
    assert abs(h.usdc - 20) < 1e-6
    print("OK  fixed buffer: keeps exactly 20 USDC, rest 50/50")


if __name__ == "__main__":
    for fn in [test_park_unpark, test_one_protocol_down, test_small_and_no_kamino, test_fixed_buffer]:
        fn()
    print("all tests passed")
