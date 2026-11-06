"""
Minimal example: how a long/flat trading bot can use the Treasury around its own swaps.
The swap functions are placeholders - plug in your own (e.g. Jupiter swap API).
"""
import os

from solders.keypair import Keypair

from lend_treasury import Treasury

treasury = Treasury(
    Keypair.from_base58_string(os.environ["PRIVATE_KEY"]),
    os.environ.get("SOL_RPC_URL", "https://api.mainnet-beta.solana.com"),
    jup_api_key=os.environ.get("JUP_API_KEY", ""),
    kamino_vault=os.environ.get("KAMINO_VAULT", ""),
    buffer_usdc=20,                     # keep 20 USDC in the wallet, lend the rest (50/50)
)


def swap_usdc_to_token():               # placeholder for your buy
    ...


def swap_token_to_usdc():               # placeholder for your sell
    ...


def on_buy_signal():
    treasury.unpark()                   # 1. pull all USDC back from both protocols
    swap_usdc_to_token()                # 2. then buy


def on_sell_signal():
    swap_token_to_usdc()                # 1. sell into USDC
    treasury.park()                     # 2. lend the idle USDC until the next buy
