
from typing import Any, Dict, Optional, Tuple
import time
import json
import requests
import datetime
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from dotenv import load_dotenv

THIS_FOLDER = Path(__file__).parent.absolute()
ACTIVITY_LOG_FILE = THIS_FOLDER / "activity.log"

BINANCE_API_BASE = "https://api.binance.com"
BINANCE_DATA_API_BASE = "https://data-api.binance.vision"


TTL_PRICE = 5           
TTL_24HR = 10           
TTL_EXCHANGE_INFO = 60 * 60  

MAX_RETRIES = 3
BACKOFF_BASE = 0.5 

FRIENDLY_NAME_MAP = {
    "bitcoin": "BTCUSDT",
    "btc": "BTCUSDT",
    "ethereum": "ETHUSDT",
    "eth": "ETHUSDT",
    "bnb": "BNBUSDT",
    "binancecoin": "BNBUSDT",
    "litecoin": "LTCUSDT",
    "ltc": "LTCUSDT",
    "dogecoin": "DOGEUSDT",
    "doge": "DOGEUSDT",
}


_cache: Dict[str, Tuple[float, Any]] = {}

def _cache_get(key: str, ttl: int) -> Optional[Any]:
    entry = _cache.get(key)
    if not entry:
        return None
    ts, val = entry
    if time.time() - ts > ttl:

        _cache.pop(key, None)
        return None
    return val

def _cache_set(key: str, val: Any) -> None:
    _cache[key] = (time.time(), val)


def _log(level: str, message: str, extra: Optional[Dict[str, Any]] = None) -> None:
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    record = {"ts": now, "level": level, "message": message}
    if extra:
        record.update({"extra": extra})
    with open(ACTIVITY_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


if not ACTIVITY_LOG_FILE.exists():
    ACTIVITY_LOG_FILE.touch()


def _request_with_retries(method: str, url: str, params: Optional[Dict[str, Any]] = None, timeout: float = 5.0) -> requests.Response:
    """
    Perform HTTP request with retries, exponential backoff, and special handling for 429.
    Raises requests.HTTPError if final attempt fails.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            resp = requests.request(method, url, params=params, timeout=timeout)
    
            status = resp.status_code
            if status == 429:

                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else BACKOFF_BASE * (2 ** (attempt - 1))
                _log("WARN", f"429 rate limited on {url}. attempt={attempt}. waiting {wait}s")
                if attempt >= MAX_RETRIES:
                    resp.raise_for_status()
                time.sleep(wait)
                continue
            if 500 <= status < 600:

                if attempt >= MAX_RETRIES:
                    resp.raise_for_status()
                wait = BACKOFF_BASE * (2 ** (attempt - 1))
                _log("WARN", f"Server error {status} on {url}. attempt={attempt}. waiting {wait}s")
                time.sleep(wait)
                continue

            return resp
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt >= MAX_RETRIES:
                _log("ERROR", f"Request failed after {attempt} attempts for {url}.", {"error": str(exc)})
                raise
            wait = BACKOFF_BASE * (2 ** (attempt - 1))
            _log("WARN", f"Network error on {url}: {exc}. attempt={attempt}. waiting {wait}s")
            time.sleep(wait)
            continue


def resolve_symbol(name: str) -> str:
    """
    Resolve a friendly name or symbol to a validated Binance symbol (like BTCUSDT).
    Strategy:
    1. quick friendly map (FRIENDLY_NAME_MAP)
    2. try uppercase name as provided: e.g. "BTCUSDT", "BTC"
       - if no USDT suffix, try adding 'USDT'
    3. validate against Binance exchangeInfo (cached)
    Raises ValueError if symbol cannot be resolved.
    """
    if not name:
        raise ValueError("Empty symbol/name")

    name_l = name.lower().strip()

    if name_l in FRIENDLY_NAME_MAP:
        cand = FRIENDLY_NAME_MAP[name_l]
        if _validate_symbol_exists(cand):
            return cand

    cand_variants = []
    raw_upper = name.strip().upper()
    cand_variants.append(raw_upper)
    if not raw_upper.endswith("USDT"):
        cand_variants.append(raw_upper + "USDT")
    if raw_upper.endswith("USDT") and len(raw_upper) > 4:
        cand_variants.append(raw_upper[:-4])
    for cand in cand_variants:
        if _validate_symbol_exists(cand):
            if cand.endswith("USDT"):
                return cand
            if _validate_symbol_exists(cand + "USDT"):
                return cand + "USDT"
            return cand

    fallback = "".join(part[0].upper() + part[1:] for part in name_l.split())

    if _validate_symbol_exists(fallback + "USDT"):
        return fallback + "USDT"

    raise ValueError(f"Could not resolve symbol/name '{name}' to a valid Binance trading pair")


def _validate_symbol_exists(symbol: str) -> bool:
    """
    Validate if a symbol exists on Binance (uses cached exchangeInfo).
    """
    symbol = symbol.upper()
    cache_key = f"exchange_info_all"
    data = _cache_get(cache_key, TTL_EXCHANGE_INFO)
    if data is None:
        url = f"{BINANCE_API_BASE}/api/v3/exchangeInfo"
        resp = _request_with_retries("GET", url)

        if resp.status_code != 200:
            _log("ERROR", f"Failed to fetch exchangeInfo: {resp.status_code} {resp.text}")
            return False
        
        info = resp.json()
        symbols_list = info.get("symbols", [])
        valid = {s.get("symbol", "").upper() for s in symbols_list}
        _cache_set(cache_key, valid)
        data = valid
    else:
        valid = data
    return symbol in valid

mcp = FastMCP("Binance - MCP Server")


@mcp.tool()
def get_price(symbol: str) -> Any:
    """
    Returns a structured dict with current price for the validated symbol.
    Caches the response for TTL_PRICE seconds.
    """
    try:
        resolved = resolve_symbol(symbol)
    except ValueError as e:
        _log("ERROR", f"resolve_symbol failed: {e}", {"input": symbol})
        raise

    cache_key = f"price::{resolved}"
    cached = _cache_get(cache_key, TTL_PRICE)

    if cached is not None:
        _log("DEBUG", f"cache hit for price {resolved}")
        return {"symbol": resolved, "price": cached, "cached": True}

    url = f"{BINANCE_API_BASE}/api/v3/ticker/price"
    resp = _request_with_retries("GET", url, params={"symbol": resolved})

    if resp.status_code != 200:
        _log("ERROR", f"get_price failed for {resolved}", {"status": resp.status_code, "body": resp.text})
        resp.raise_for_status()

    data = resp.json()
    price = data.get("price")
    _cache_set(cache_key, price)
    _log("INFO", f"Fetched price for {resolved}", {"price": price})
    return {"symbol": resolved, "price": price, "cached": False}


@mcp.tool()
def get_price_price_change(symbol: str) -> Any:
    """
    Returns Binance 24hr ticker data (structured) for the validated symbol.
    Caches response for TTL_24HR seconds.
    """
    try:
        resolved = resolve_symbol(symbol)
    except ValueError as e:
        _log("ERROR", f"resolve_symbol failed: {e}", {"input": symbol})
        raise

    cache_key = f"24hr::{resolved}"
    cached = _cache_get(cache_key, TTL_24HR)

    if cached is not None:
        _log("DEBUG", f"cache hit for 24hr {resolved}")
        return {"symbol": resolved, "data": cached, "cached": True}
    
    url = f"{BINANCE_DATA_API_BASE}/api/v3/ticker/24hr"
    resp = _request_with_retries("GET", url, params={"symbol": resolved})

    if resp.status_code != 200:
        _log("ERROR", f"get_price_price_change failed for {resolved}", {"status": resp.status_code, "body": resp.text})
        resp.raise_for_status()

    data = resp.json()
    _cache_set(cache_key, data)
    _log("INFO", f"Fetched 24hr ticker for {resolved}")
    return {"symbol": resolved, "data": data, "cached": False}


@mcp.prompt()
def executive_summary() -> str:
    return """
Get prices for: btc, eth.

Provide an executive summary including:
- A 2-sentence summary for each crypto
- Current price
- 24-hour price change
- 24-hour percentage change

Use these tools:
- get_price(symbol)
- get_price_price_change(symbol)

Symbols:
- bitcoin/btc → BTCUSDT
- ethereum/eth → ETHUSDT
    """


@mcp.prompt()
def crypto_summary(crypto: str) -> str:
    return f"""
Get the current price of: {crypto}
Also provide 24-hour price change summary.

Use get_price(symbol) and get_price_price_change(symbol).
"""

@mcp.resource("file://activity.log")
def activity_log() -> str:
    with open(ACTIVITY_LOG_FILE, "r", encoding="utf-8") as f:
        return f.read()


@mcp.resource("resource://crypto_price/{symbol}")
def get_crypto_price(symbol: str) -> str:
    result = get_price(symbol)
    return json.dumps(result, ensure_ascii=False)


if __name__ == "__main__":
    print("Starting improved Binance MCP (with retries, caching, symbol resolution)")
    mcp.run(transport="stdio")


