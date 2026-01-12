"""
Proxy support for VPS deployments.

Use residential proxies when running from datacenter IPs (Hetzner, etc.)
that may be blocked by Polymarket.

Configuration via .env:
    PROXY_URL=http://user:pass@proxy.example.com:port
    PROXY_ENABLED=true
"""

import os
from typing import Optional
import aiohttp


def get_proxy_url() -> Optional[str]:
    """Get proxy URL from environment if enabled."""
    if os.getenv("PROXY_ENABLED", "false").lower() != "true":
        return None
    return os.getenv("PROXY_URL")


def get_proxy_connector() -> Optional[aiohttp.TCPConnector]:
    """Get aiohttp connector with proxy support."""
    proxy_url = get_proxy_url()
    if not proxy_url:
        return None
    
    # For aiohttp, we don't use connector for proxy
    # Instead, pass proxy= to the request
    return None


def get_session_kwargs() -> dict:
    """Get kwargs for aiohttp.ClientSession with proxy support."""
    proxy_url = get_proxy_url()
    if proxy_url:
        return {"trust_env": True}  # Use env proxy settings
    return {}


async def test_proxy_connection() -> dict:
    """Test if proxy is working and returns a residential IP."""
    import ssl
    import certifi
    
    result = {
        "proxy_enabled": False,
        "proxy_url": None,
        "your_ip": None,
        "ip_type": None,
        "polymarket_accessible": False,
    }
    
    proxy_url = get_proxy_url()
    result["proxy_enabled"] = proxy_url is not None
    result["proxy_url"] = proxy_url[:30] + "..." if proxy_url else None
    
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    connector = aiohttp.TCPConnector(ssl=ssl_context)
    
    async with aiohttp.ClientSession(connector=connector) as session:
        # Check our IP
        try:
            async with session.get(
                "https://api.ipify.org?format=json",
                proxy=proxy_url,
                timeout=10,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    result["your_ip"] = data.get("ip")
        except Exception as e:
            result["your_ip"] = f"Error: {e}"
        
        # Check if Polymarket CLOB is accessible
        try:
            async with session.get(
                "https://clob.polymarket.com/time",
                proxy=proxy_url,
                timeout=10,
            ) as resp:
                result["polymarket_accessible"] = resp.status == 200
        except Exception:
            result["polymarket_accessible"] = False
    
    return result

