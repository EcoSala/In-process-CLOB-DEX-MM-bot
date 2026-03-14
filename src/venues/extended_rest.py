"""
Extended Exchange REST API client for market discovery.
"""
import logging
import aiohttp
from typing import Optional

log = logging.getLogger("mm")


class ExtendedRESTClient:
    """
    REST client for Extended Exchange public API.
    Used for dynamic market discovery.
    """
    
    def __init__(self, base_url: str = "https://api.starknet.extended.exchange"):
        self.base_url = base_url.rstrip("/")
        self.session: Optional[aiohttp.ClientSession] = None
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            )
        return self.session
    
    async def close(self):
        """Close the HTTP session."""
        if self.session and not self.session.closed:
            await self.session.close()
    
    async def get_markets(self) -> list[dict]:
        """
        Fetch all available markets from Extended API.
        
        GET /api/v1/info/markets
        
        Returns list of market objects with:
        - name: market symbol (e.g. "BTC-USD")
        - active: boolean
        - status: "ACTIVE" | "REDUCE_ONLY" | "DELISTED" | "PRELISTED" | "DISABLED"
        - tradingConfig: min sizes, max leverage, etc.
        - assetName: base asset name
        
        Rate limit: 1,000 requests/minute (shared across all REST endpoints)
        """
        session = await self._get_session()
        url = f"{self.base_url}/api/v1/info/markets"
        
        try:
            async with session.get(url) as resp:
                if resp.status == 429:
                    log.warning("Extended API rate limit hit when fetching markets")
                    return []
                
                if resp.status != 200:
                    log.error(f"Extended API returned status {resp.status} when fetching markets")
                    return []
                
                data = await resp.json()
                
                if data.get("status") != "OK":
                    log.error(f"Extended API error: {data.get('error', 'unknown')}")
                    return []
                
                markets = data.get("data", [])
                log.info(f"Fetched {len(markets)} markets from Extended API")
                return markets
        
        except aiohttp.ClientError as e:
            log.error(f"HTTP error fetching markets from Extended: {e}")
            return []
        except Exception as e:
            log.error(f"Unexpected error fetching markets: {e}")
            return []
    
    async def get_active_spot_markets(self) -> list[str]:
        """
        Get list of active spot market symbols suitable for trading.
        
        Filters:
        - status == "ACTIVE" (not REDUCE_ONLY, DELISTED, etc.)
        - active == True
        
        Returns list of market symbols like ["BTC-USD", "ETH-USD", ...]
        """
        markets = await self.get_markets()
        
        active_markets = []
        for m in markets:
            name = m.get("name")
            status = m.get("status")
            active = m.get("active")
            
            # Filter: only ACTIVE markets that are currently tradeable
            if status == "ACTIVE" and active:
                active_markets.append(name)
        
        log.info(
            f"Discovered {len(active_markets)} active markets: "
            f"{', '.join(active_markets[:10])}"
            f"{' ...' if len(active_markets) > 10 else ''}"
        )
        
        return sorted(active_markets)
