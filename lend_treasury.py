#!/usr/bin/env python3
"""
lend_treasury.py - park idle USDC of a Solana wallet in two lending protocols and pull it back on demand.

  Jupiter Lend Earn : REST https://api.jup.ag/lend/v1/earn/{deposit,redeem,positions}  (API key from portal.jup.ag)
  Kamino Earn vault : REST https://api.kamino.finance/ktx/kvault/{deposit,withdraw}, /kvaults/...  (no key)
Both return an unsigned base64 transaction -> the blockhash is refreshed from your own RPC -> signed with your
wallet -> sent to the Solana RPC -> confirmation is awaited.

Use from a trading bot:
    from lend_treasury import Treasury
    t = Treasury(keypair, rpc_url, jup_api_key, kamino_vault)
    t.park()      # wallet is idle (e.g. after a SELL): deposit USDC, keep a buffer, split between both protocols
    t.unpark()    # before a BUY: withdraw everything from both; returns the USDC now in the wallet

CLI:
    python lend_treasury.py status
    python lend_treasury.py kamino-vaults                 # list Kamino USDC vaults (no key needed)
    python lend_treasury.py deposit jup 10                # deposit 10 USDC into Jupiter Lend
    python lend_treasury.py deposit kamino 10
    python lend_treasury.py withdraw-all jup
    python lend_treasury.py withdraw-all kamino
    python lend_treasury.py park | unpark
Environment (a .env file in the current folder is read automatically):
    PRIVATE_KEY (base58) or SOL_KEYPAIR_FILE (json array), JUP_API_KEY, KAMINO_VAULT, SOL_RPC_URL,
    LEND_SPLIT_JUP (0.5), LEND_BUFFER_USDC (fixed USDC kept; overrides LEND_BUFFER_PCT, default 0.10), LEND_MIN_USDC (10)
"""
import base64
import json
import logging
import os
import sys
import time

import requests
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import Message, MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.system_program import ID as SYSTEM_PROGRAM
from solders.transaction import VersionedTransaction

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
JUP = "https://api.jup.ag/lend/v1/earn"
KAMINO = "https://api.kamino.finance"
ATA_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")


def with_blockhash(msg, blockhash):
    """same transaction, fresh blockhash from OUR rpc (the API's blockhash may be unknown to the public RPC)."""
    if isinstance(msg, MessageV0):
        return MessageV0(msg.header, msg.account_keys, blockhash, msg.instructions, msg.address_table_lookups)
    h = msg.header
    return Message.new_with_compiled_instructions(h.num_required_signatures, h.num_readonly_signed_accounts,
                                                  h.num_readonly_unsigned_accounts, msg.account_keys, blockhash,
                                                  msg.instructions)


def ata_address(owner: Pubkey, mint: Pubkey, token_program: Pubkey) -> Pubkey:
    return Pubkey.find_program_address([bytes(owner), bytes(token_program), bytes(mint)], ATA_PROGRAM)[0]


def create_ata_idempotent_ix(payer: Pubkey, owner: Pubkey, mint: Pubkey, token_program: Pubkey) -> Instruction:
    ata = ata_address(owner, mint, token_program)
    return Instruction(ATA_PROGRAM, bytes([1]), [                     # 1 = CreateIdempotent
        AccountMeta(payer, True, True), AccountMeta(ata, False, True), AccountMeta(owner, False, False),
        AccountMeta(mint, False, False), AccountMeta(SYSTEM_PROGRAM, False, False),
        AccountMeta(token_program, False, False)])


log = logging.getLogger("lend_treasury")


def _find(obj, keys):
    """first value of any of `keys` anywhere in a nested dict/list (API field names are not fully documented)."""
    if isinstance(obj, dict):
        for k in keys:
            if k in obj and obj[k] not in (None, ""):
                return obj[k]
        for v in obj.values():
            r = _find(v, keys)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find(v, keys)
            if r is not None:
                return r
    return None


def _mentions(obj, text):
    return text in json.dumps(obj)


class Treasury:
    def __init__(self, keypair: Keypair, rpc_url: str, jup_api_key: str = "", kamino_vault: str = "",
                 split_jup: float = 0.5, buffer_pct: float = 0.10, min_usdc: float = 10.0, http=None,
                 buffer_usdc: float = None):
        self.kp, self.rpc_url = keypair, rpc_url
        self.wallet = str(keypair.pubkey())
        self.jup_key, self.kvault = jup_api_key, kamino_vault
        self.split_jup, self.buffer_pct, self.min_usdc = split_jup, buffer_pct, min_usdc
        self.buffer_usdc = buffer_usdc            # fixed USDC kept in the wallet; overrides buffer_pct when set
        self.http = http or requests.Session()

    # ------------------------------------------------------------ RPC
    def rpc(self, method, params):
        r = self.http.post(self.rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=30)
        r.raise_for_status()
        j = r.json()
        if "error" in j:
            raise RuntimeError(f"RPC {method}: {j['error']}")
        return j["result"]

    def usdc_balance(self):
        res = self.rpc("getTokenAccountsByOwner", [self.wallet, {"mint": USDC_MINT}, {"encoding": "jsonParsed"}])
        return sum(float(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0) for a in res["value"])

    def sol_balance(self):
        return self.rpc("getBalance", [self.wallet])["value"] / 1e9

    def latest_blockhash(self):
        return Hash.from_string(self.rpc("getLatestBlockhash", [{"commitment": "confirmed"}])["value"]["blockhash"])

    def sign_and_send(self, tx_b64, label):
        tx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
        signed = VersionedTransaction(with_blockhash(tx.message, self.latest_blockhash()), [self.kp])
        sig = self.rpc("sendTransaction", [base64.b64encode(bytes(signed)).decode(),
                                           {"encoding": "base64", "preflightCommitment": "confirmed", "maxRetries": 3}])
        for _ in range(60):                                       # ~60 s
            st = self.rpc("getSignatureStatuses", [[sig], {"searchTransactionHistory": True}])["value"][0]
            if st and st.get("err"):
                raise RuntimeError(f"{label}: transaction failed on chain: {st['err']} (sig {sig})")
            if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
                log.info("%s confirmed: %s", label, sig)
                return sig
            time.sleep(1)
        raise RuntimeError(f"{label}: not confirmed after 60 s (sig {sig}) - check on solscan before retrying")

    def _api_tx(self, url, body, label, headers=None, tries=3):
        """get a fresh unsigned tx from the API and send it; a new tx is requested on each try (fresh blockhash)."""
        last = None
        for i in range(tries):
            try:
                r = self.http.post(url, json=body, headers={"Content-Type": "application/json", **(headers or {})}, timeout=30)
                if r.status_code != 200:
                    raise RuntimeError(f"{label}: HTTP {r.status_code} {r.text[:300]}")
                tx = r.json().get("transaction")
                if not tx:
                    raise RuntimeError(f"{label}: no 'transaction' in response: {r.text[:300]}")
                return self.sign_and_send(tx, label)
            except RuntimeError as e:
                last = e
                log.warning("%s try %d failed: %s", label, i + 1, e)
                if "not confirmed" in str(e):
                    break                                         # never resend blindly - it may still land
                time.sleep(3)
        raise last

    def ensure_token_account(self, mint_str, label):
        """create the wallet's token account for `mint` if it does not exist yet (one-time, ~0.002 SOL rent)."""
        mint = Pubkey.from_string(mint_str)
        info = self.rpc("getAccountInfo", [mint_str, {"encoding": "base64"}])["value"]
        if not info:
            raise RuntimeError(f"{label}: mint {mint_str} not found")
        token_program = Pubkey.from_string(info["owner"])
        ata = ata_address(self.kp.pubkey(), mint, token_program)
        if self.rpc("getAccountInfo", [str(ata), {"encoding": "base64"}])["value"]:
            return False
        ix = create_ata_idempotent_ix(self.kp.pubkey(), self.kp.pubkey(), mint, token_program)
        msg = MessageV0.try_compile(self.kp.pubkey(), [ix], [], self.latest_blockhash())
        unsigned = VersionedTransaction.populate(msg, [Signature.default()])
        self.sign_and_send(base64.b64encode(bytes(unsigned)).decode(), f"{label}: create token account")
        return True

    # ------------------------------------------------------------ Jupiter Lend
    def _jh(self):
        return {"x-api-key": self.jup_key} if self.jup_key else {}

    def jup_position(self):
        r = self.http.get(f"{JUP}/positions", params={"users": self.wallet}, headers=self._jh(), timeout=30)
        r.raise_for_status()
        data = r.json()
        items = data if isinstance(data, list) else data.get("positions", data.get("data", [data]))
        for it in items:
            if _mentions(it, USDC_MINT):
                return it
        return None

    def jup_deposit(self, usdc):
        pos = self.jup_position()
        jl_mint = (pos.get("token") or {}).get("address") if isinstance(pos, dict) else None   # jlUSDC receipt token
        if jl_mint and jl_mint != USDC_MINT:
            self.ensure_token_account(jl_mint, "Jupiter Lend")
        return self._api_tx(f"{JUP}/deposit", {"asset": USDC_MINT, "amount": str(int(round(usdc * 1e6))),
                                               "signer": self.wallet}, f"Jupiter Lend deposit {usdc:.2f} USDC", self._jh())

    def jup_shares(self):
        pos = self.jup_position()
        shares = _find(pos, ["shares", "lendingTokenShares", "jlTokenShares", "sharesBalance"]) if pos else None
        return float(shares) if shares else 0.0

    def parked(self):
        """True if anything is still deposited in either protocol."""
        return self.jup_shares() > 0 or self.kamino_shares() > 0

    def jup_withdraw_all(self):
        shares = self.jup_shares()
        if shares <= 0:
            log.info("Jupiter Lend: nothing to withdraw")
            return None
        return self._api_tx(f"{JUP}/redeem", {"asset": USDC_MINT, "shares": str(int(float(shares))), "signer": self.wallet},
                            "Jupiter Lend withdraw all", self._jh())

    # ------------------------------------------------------------ Kamino Earn vault
    def kamino_shares_raw(self):
        """exact share balance as the API's decimal string ('0' if none)."""
        if not self.kvault:
            return "0"
        r = self.http.get(f"{KAMINO}/kvaults/users/{self.wallet}/positions/{self.kvault}", timeout=30)
        if r.status_code == 404:
            return "0"
        r.raise_for_status()
        return str(r.json().get("totalShares") or "0")

    def kamino_shares(self):
        return float(self.kamino_shares_raw())

    def kamino_deposit(self, usdc):
        if not self.kvault:
            raise RuntimeError("KAMINO_VAULT is not set")
        return self._api_tx(f"{KAMINO}/ktx/kvault/deposit", {"wallet": self.wallet, "kvault": self.kvault,
                                                             "amount": f"{usdc:.6f}"}, f"Kamino deposit {usdc:.2f} USDC")

    def kamino_withdraw_all(self):
        shares = self.kamino_shares_raw()             # exact share balance (the documented u64-max "withdraw all" is rejected by the API)
        if float(shares) <= 0:
            log.info("Kamino: nothing to withdraw")
            return None
        return self._api_tx(f"{KAMINO}/ktx/kvault/withdraw", {"wallet": self.wallet, "kvault": self.kvault,
                                                              "amount": shares}, "Kamino withdraw all")

    def kamino_usdc_vaults(self):
        r = self.http.get(f"{KAMINO}/kvaults/vaults", timeout=60)
        r.raise_for_status()
        out = []
        for v in r.json():
            st = v.get("state", {})
            if st.get("tokenMint") == USDC_MINT:
                m = {}
                try:
                    mr = self.http.get(f"{KAMINO}/kvaults/vaults/{v['address']}/metrics", timeout=30)
                    m = mr.json() if mr.status_code == 200 else {}
                except requests.RequestException:
                    pass
                out.append({"address": v["address"], "name": st.get("name"), "metrics": m})
        return out

    # ------------------------------------------------------------ what the bot calls
    def park(self):
        """deposit idle USDC: keep a buffer in the wallet (fixed buffer_usdc, else buffer_pct), split the rest."""
        if self.sol_balance() < 0.01:
            log.warning("less than 0.01 SOL for fees - not parking")
            return {}
        usdc = self.usdc_balance()
        keep = self.buffer_usdc if self.buffer_usdc is not None else usdc * self.buffer_pct
        deploy = max(0.0, usdc - keep)
        if deploy < self.min_usdc:
            log.info("park: %.2f USDC idle, below minimum - nothing to do", usdc)
            return {}
        legs = {"jup": deploy * self.split_jup, "kamino": deploy * (1 - self.split_jup)} if self.kvault else {"jup": deploy}
        done = {}
        for name, amt in legs.items():
            if amt < self.min_usdc:
                continue
            try:
                (self.jup_deposit if name == "jup" else self.kamino_deposit)(amt)
                done[name] = round(amt, 2)
            except Exception as e:                                # noqa: BLE001 - one protocol failing must not stop the other
                log.error("park %s failed: %s", name, e)
        log.info("park: wallet had %.2f USDC, deposited %s", usdc, done)
        return done

    def unpark(self):
        """withdraw everything from both protocols; returns the USDC balance in the wallet afterwards."""
        errors = []
        for name, fn in [("jup", self.jup_withdraw_all), ("kamino", self.kamino_withdraw_all if self.kvault else None)]:
            if fn is None:
                continue
            try:
                fn()
            except Exception as e:                                # noqa: BLE001
                errors.append(f"{name}: {e}")
                log.error("unpark %s failed: %s", name, e)
        bal = self.usdc_balance()
        log.info("unpark: wallet now %.2f USDC%s", bal, f" (errors: {errors})" if errors else "")
        return bal

    def status(self):
        jp = None
        try:
            jp = self.jup_position()
        except Exception as e:                                    # noqa: BLE001
            jp = f"error: {e}"
        try:
            ks = self.kamino_shares()
        except Exception as e:                                    # noqa: BLE001
            ks = f"error: {e}"
        return {"wallet": self.wallet, "SOL": round(self.sol_balance(), 4), "USDC in wallet": round(self.usdc_balance(), 2),
                "Jupiter Lend position (raw)": jp, "Kamino vault": self.kvault or "(not set)", "Kamino shares": ks}


def from_env():
    try:                                            # optional: read settings from a .env file
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    key = os.environ.get("SOL_PRIVATE_KEY") or os.environ.get("PRIVATE_KEY")
    if key:
        kp = Keypair.from_base58_string(key.strip())
    elif os.environ.get("SOL_KEYPAIR_FILE"):
        kp = Keypair.from_bytes(bytes(json.load(open(os.environ["SOL_KEYPAIR_FILE"]))))
    else:
        raise SystemExit("no wallet key: set PRIVATE_KEY (base58) or SOL_KEYPAIR_FILE, e.g. in a .env file")
    return Treasury(kp, os.environ.get("SOL_RPC_URL", "https://api.mainnet-beta.solana.com"),
                    os.environ.get("JUP_API_KEY", ""), os.environ.get("KAMINO_VAULT", ""),
                    float(os.environ.get("LEND_SPLIT_JUP", "0.5")), float(os.environ.get("LEND_BUFFER_PCT", "0.10")),
                    float(os.environ.get("LEND_MIN_USDC", "10")),
                    buffer_usdc=float(os.environ["LEND_BUFFER_USDC"]) if os.environ.get("LEND_BUFFER_USDC") else None)


def main(argv):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not argv:
        print(__doc__)
        return
    cmd = argv[0]
    if cmd == "kamino-vaults":                                     # read-only, no wallet key needed
        t = Treasury(Keypair(), os.environ.get("SOL_RPC_URL", "https://api.mainnet-beta.solana.com"))
        for v in t.kamino_usdc_vaults():
            print(json.dumps(v, default=str)[:600])
        return
    t = from_env()
    if cmd == "status":
        print(json.dumps(t.status(), indent=1, default=str))
    elif cmd == "deposit":
        proto, amt = argv[1], float(argv[2])
        (t.jup_deposit if proto == "jup" else t.kamino_deposit)(amt)
        print(json.dumps(t.status(), indent=1, default=str))
    elif cmd == "withdraw-all":
        (t.jup_withdraw_all if argv[1] == "jup" else t.kamino_withdraw_all)()
        print(json.dumps(t.status(), indent=1, default=str))
    elif cmd == "park":
        print(t.park())
    elif cmd == "unpark":
        print("USDC in wallet:", t.unpark())
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
