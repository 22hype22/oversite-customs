"""Token registry and pair parsing.

Mints are baked in for convenience only. They are treated as *unverified* until
``verify_mints`` checks them against Jupiter's token API at startup — a wrong
mint is the single easiest way to send funds somewhere you did not intend, so
live mode refuses to run on an unverified registry.

Override or extend the registry without touching code:

    CRYPTO_TOKENS='{"WIF": {"mint": "...", "decimals": 6, "cex": "WIFUSDT"}}'
"""

import json
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Token:
    symbol: str
    mint: str
    decimals: int
    cex_symbol: str = ""      # Binance symbol, e.g. SOLUSDT
    coinbase_id: str = ""     # Coinbase product id, e.g. SOL-USD

    def to_atoms(self, amount: float) -> int:
        return int(round(amount * (10 ** self.decimals)))

    def from_atoms(self, atoms: int) -> float:
        return int(atoms) / (10 ** self.decimals)


# Well-known Solana mints. Verified at runtime via verify_mints().
DEFAULT_TOKENS = {
    "SOL":  Token("SOL",  "So11111111111111111111111111111111111111112",  9, "SOLUSDT",  "SOL-USD"),
    "USDC": Token("USDC", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", 6, "",         "USDC-USD"),
    "USDT": Token("USDT", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB", 6, "",         "USDT-USD"),
    "JUP":  Token("JUP",  "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",  6, "JUPUSDT",  "JUP-USD"),
    "BONK": Token("BONK", "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", 5, "BONKUSDT", "BONK-USD"),
    "JTO":  Token("JTO",  "jtojtomepa8beP8AuQc6eXt5FriJwfFMwQx2v2f9mCL",  9, "JTOUSDT",  "JTO-USD"),
    "RAY":  Token("RAY",  "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R", 6, "RAYUSDT",  "RAY-USD"),
    "PYTH": Token("PYTH", "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3", 6, "PYTHUSDT", "PYTH-USD"),
    "WIF":  Token("WIF",  "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm", 6, "WIFUSDT",  "WIF-USD"),
    "MSOL": Token("MSOL", "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",  9, "",         "MSOL-USD"),
}

STABLES = {"USDC", "USDT"}


class UnknownToken(KeyError):
    pass


class Registry:
    def __init__(self, tokens=None):
        self._tokens = dict(tokens or DEFAULT_TOKENS)
        self.verified = False

    @classmethod
    def from_env(cls, raw=None):
        reg = cls()
        raw = raw if raw is not None else os.getenv("CRYPTO_TOKENS", "")
        if raw.strip():
            try:
                extra = json.loads(raw)
            except Exception as e:
                raise ValueError(f"CRYPTO_TOKENS is not valid JSON: {e}") from e
            for sym, spec in (extra or {}).items():
                sym = sym.upper()
                reg._tokens[sym] = Token(
                    sym,
                    str(spec["mint"]),
                    int(spec["decimals"]),
                    str(spec.get("cex", "") or ""),
                    str(spec.get("coinbase", "") or ""),
                )
        return reg

    def get(self, symbol: str) -> Token:
        try:
            return self._tokens[symbol.upper()]
        except KeyError:
            raise UnknownToken(
                f"{symbol!r} is not in the token registry. Add it with CRYPTO_TOKENS, e.g. "
                f'CRYPTO_TOKENS=\'{{"{symbol.upper()}": {{"mint": "<mint>", "decimals": 6}}}}\''
            ) from None

    def symbols(self):
        return sorted(self._tokens)

    def all(self):
        return dict(self._tokens)

    async def verify_mints(self, http, symbols=None, base_url="https://lite-api.jup.ag/tokens/v2"):
        """Confirm every mint we might trade is a real, known SPL mint.

        Returns (ok, problems). Never raises on network failure — the caller
        decides whether an unverified registry is fatal (it is, for live mode).
        """
        problems = []
        want = [self.get(s) for s in (symbols or self.symbols())]
        for tok in want:
            try:
                r = await http.get(f"{base_url}/search", params={"query": tok.mint}, timeout=15)
                if r.status_code != 200:
                    problems.append(f"{tok.symbol}: token API HTTP {r.status_code}")
                    continue
                body = r.json()
                rows = body if isinstance(body, list) else (body.get("tokens") or body.get("data") or [])
                hit = next((x for x in rows if str(x.get("id") or x.get("address") or "") == tok.mint), None)
                if not hit:
                    problems.append(f"{tok.symbol}: mint {tok.mint} not found upstream")
                    continue
                dec = hit.get("decimals")
                if dec is not None and int(dec) != tok.decimals:
                    problems.append(f"{tok.symbol}: decimals {tok.decimals} != upstream {dec}")
            except Exception as e:
                problems.append(f"{tok.symbol}: verify failed ({e!r})")
        self.verified = not problems
        return self.verified, problems


@dataclass(frozen=True)
class Pair:
    base: Token
    quote: Token

    @property
    def name(self) -> str:
        return f"{self.base.symbol}/{self.quote.symbol}"

    def __str__(self) -> str:
        return self.name


def parse_pairs(spec: str, registry: Registry):
    """'SOL/USDC, JUP/USDC' -> [Pair, Pair]."""
    out = []
    for chunk in (spec or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "/" not in chunk:
            raise ValueError(f"Bad pair {chunk!r} — use BASE/QUOTE, e.g. SOL/USDC")
        b, q = chunk.split("/", 1)
        pair = Pair(registry.get(b), registry.get(q))
        if pair.base.symbol == pair.quote.symbol:
            raise ValueError(f"Bad pair {chunk!r} — base and quote are the same token")
        out.append(pair)
    if not out:
        raise ValueError("No trading pairs configured (set CRYPTO_PAIRS)")
    return out
