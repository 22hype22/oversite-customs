"""Order execution.

Two executors with the same interface:

``PaperExecutor``
    Fills against the real quote, then applies a haircut and charges the same
    modeled gas a live trade would pay. Paper results that flatter reality are
    worse than no results, so the haircut only ever moves against you.

``LiveSolanaExecutor``
    Requests a swap transaction from the aggregator for the exact quote the
    strategy evaluated, signs it locally, sends it, and waits for confirmation.
    ``solders`` and ``base58`` are imported lazily so the Discord bot can run
    without them installed.

Neither executor decides *whether* to trade — the risk manager already did.
"""

import asyncio
import base64
import time
from dataclasses import dataclass, field

import httpx

from .costs import CostModel


@dataclass
class FillResult:
    ok: bool
    status: str = "filled"        # filled | failed | rejected
    in_amount: float = 0.0
    out_amount: float = 0.0
    fee_usd: float = 0.0
    tx_sig: str = ""
    error: str = ""
    meta: dict = field(default_factory=dict)


class ExecutionError(RuntimeError):
    pass


class PaperExecutor:
    mode = "paper"

    def __init__(self, cfg, market):
        self.cfg = cfg
        self.market = market
        self.costs = CostModel(cfg)

    async def execute(self, signal, sol_usd) -> FillResult:
        q = signal.quote
        in_tok, out_tok = signal.in_token, signal.out_token
        in_amount = in_tok.from_atoms(q.in_atoms)
        gross_out = out_tok.from_atoms(q.out_atoms)
        haircut = max(0.0, self.cfg.paper_fill_haircut_bps) / 10_000
        out_amount = gross_out * (1 - haircut)
        fee_usd = self.costs.gas_usd(sol_usd, legs=1)
        return FillResult(
            ok=True, status="filled", in_amount=in_amount, out_amount=out_amount,
            fee_usd=fee_usd, tx_sig="", meta={"haircut_bps": self.cfg.paper_fill_haircut_bps,
                                              "quoted_out": gross_out},
        )


class LiveSolanaExecutor:
    """Signs and sends a real swap. Requires solders + base58."""

    mode = "live"

    def __init__(self, cfg, market, http):
        self.cfg = cfg
        self.market = market
        self.http = http
        self.costs = CostModel(cfg)
        self._keypair = None
        self._pubkey = ""

    # ------------------------------------------------------------- wallet
    def _load_keypair(self):
        if self._keypair is not None:
            return self._keypair
        try:
            import base58  # noqa: F401
            from solders.keypair import Keypair
        except ImportError as e:
            raise ExecutionError(
                "live mode needs the Solana libraries: pip install -r requirements-crypto.txt"
            ) from e
        secret = (self.cfg.private_key or "").strip()
        if not secret:
            raise ExecutionError("SOLANA_PRIVATE_KEY is not set")
        try:
            if secret.startswith("["):
                import json
                kp = Keypair.from_bytes(bytes(json.loads(secret)))
            else:
                kp = Keypair.from_base58_string(secret)
        except Exception as e:
            raise ExecutionError(f"could not parse SOLANA_PRIVATE_KEY: {e}") from e
        self._keypair = kp
        self._pubkey = str(kp.pubkey())
        return kp

    @property
    def pubkey(self):
        if not self._pubkey:
            self._load_keypair()
        return self._pubkey

    # ---------------------------------------------------------------- rpc
    async def _rpc(self, method, params, timeout=25):
        r = await self.http.post(
            self.cfg.solana_rpc_url,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            timeout=timeout,
        )
        if r.status_code != 200:
            raise ExecutionError(f"RPC {method} HTTP {r.status_code}: {r.text[:200]}")
        body = r.json()
        if "error" in body:
            raise ExecutionError(f"RPC {method} error: {str(body['error'])[:300]}")
        return body.get("result")

    async def balance_sol(self):
        res = await self._rpc("getBalance", [self.pubkey])
        val = (res or {}).get("value", 0)
        return int(val) / 1_000_000_000

    async def preflight_checks(self, sol_usd):
        """Cheap pre-trade sanity: can we actually pay for a transaction?"""
        problems = []
        try:
            bal = await self.balance_sol()
        except Exception as e:
            return [f"cannot read wallet balance: {e}"]
        need_lamports = (self.cfg.base_fee_lamports + self.cfg.priority_fee_lamports) * 4
        need_sol = need_lamports / 1_000_000_000
        if bal < need_sol:
            problems.append(f"wallet holds {bal:.6f} SOL, needs at least {need_sol:.6f} SOL for fees")
        return problems

    # ------------------------------------------------------------- execute
    async def execute(self, signal, sol_usd) -> FillResult:
        kp = self._load_keypair()
        from solders.transaction import VersionedTransaction

        quote_response = getattr(signal.quote, "raw", None)
        if not quote_response:
            return FillResult(False, "rejected", error="quote has no raw aggregator response to swap against")

        body = {
            "quoteResponse": quote_response,
            "userPublicKey": self.pubkey,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": int(self.cfg.priority_fee_lamports),
        }
        try:
            r = await self.http.post(f"{self.cfg.jupiter_quote_url}/swap", json=body, timeout=30)
            if r.status_code != 200:
                return FillResult(False, "rejected", error=f"swap build HTTP {r.status_code}: {r.text[:200]}")
            swap = r.json()
        except (httpx.TransportError, httpx.TimeoutException) as e:
            return FillResult(False, "rejected", error=f"swap build transport error: {e!r}")

        encoded = swap.get("swapTransaction")
        if not encoded:
            return FillResult(False, "rejected", error=f"swap response missing transaction: {str(swap)[:200]}")

        try:
            unsigned = VersionedTransaction.from_bytes(base64.b64decode(encoded))
            signed = VersionedTransaction(unsigned.message, [kp])
            wire = base64.b64encode(bytes(signed)).decode()
        except Exception as e:
            return FillResult(False, "rejected", error=f"signing failed: {e!r}")

        fee_usd = self.costs.gas_usd(sol_usd, legs=1)
        try:
            sig = await self._rpc("sendTransaction", [wire, {
                "encoding": "base64",
                "skipPreflight": False,
                "maxRetries": 3,
                "preflightCommitment": "confirmed",
            }])
        except ExecutionError as e:
            # Never landed — only the simulated cost, no base fee burned.
            return FillResult(False, "rejected", fee_usd=0.0, error=str(e))

        confirmed, detail = await self._confirm(sig)
        if not confirmed:
            # It hit the chain and failed, or we lost track of it. Gas is gone.
            return FillResult(False, "failed", fee_usd=self.costs.failed_tx_usd(sol_usd),
                              tx_sig=str(sig), error=detail)

        in_tok, out_tok = signal.in_token, signal.out_token
        in_amount = in_tok.from_atoms(signal.quote.in_atoms)
        # Trust the guaranteed floor rather than the optimistic quote: the
        # aggregator only promises otherAmountThreshold.
        out_amount = out_tok.from_atoms(signal.quote.min_out_atoms or signal.quote.out_atoms)
        return FillResult(True, "filled", in_amount=in_amount, out_amount=out_amount,
                          fee_usd=fee_usd, tx_sig=str(sig),
                          meta={"quoted_out": out_tok.from_atoms(signal.quote.out_atoms)})

    async def _confirm(self, sig, timeout=75.0, interval=2.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                res = await self._rpc("getSignatureStatuses", [[str(sig)], {"searchTransactionHistory": True}])
            except ExecutionError as e:
                await asyncio.sleep(interval)
                _ = e
                continue
            value = ((res or {}).get("value") or [None])[0]
            if value:
                if value.get("err"):
                    return False, f"transaction reverted on-chain: {str(value['err'])[:200]}"
                status = value.get("confirmationStatus")
                if status in ("confirmed", "finalized"):
                    return True, status
            await asyncio.sleep(interval)
        return False, f"not confirmed within {timeout:.0f}s (signature {sig})"


def build_executor(cfg, market, http):
    if cfg.is_live:
        return LiveSolanaExecutor(cfg, market, http)
    return PaperExecutor(cfg, market)
