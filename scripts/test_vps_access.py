#!/usr/bin/env python3
"""
Test VPS Access for Polymarket Bot.

Run this on your VPS to check if virtual trading will work.

Usage:
    python scripts/test_vps_access.py
    
    # With proxy:
    PROXY_ENABLED=true PROXY_URL=http://user:pass@proxy.com:port python scripts/test_vps_access.py
"""

import asyncio
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import aiohttp
import ssl
import certifi
import os


async def main():
    print("""
╔══════════════════════════════════════════════════════════════╗
║              VPS ACCESS TEST FOR POLYMARKET BOT              ║
╚══════════════════════════════════════════════════════════════╝
    """)
    
    # Check proxy settings
    proxy_enabled = os.getenv("PROXY_ENABLED", "false").lower() == "true"
    proxy_url = os.getenv("PROXY_URL") if proxy_enabled else None
    
    print(f"🔧 Proxy enabled: {proxy_enabled}")
    if proxy_url:
        print(f"🔧 Proxy URL: {proxy_url[:30]}...")
    print()
    
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    connector = aiohttp.TCPConnector(ssl=ssl_context)
    
    results = {}
    
    async with aiohttp.ClientSession(connector=connector) as session:
        # Test 1: Get our IP
        print("1️⃣ Checking your IP address...")
        try:
            async with session.get(
                "https://api.ipify.org?format=json",
                proxy=proxy_url,
                timeout=10,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    ip = data.get("ip")
                    results["ip"] = ip
                    print(f"   ✅ Your IP: {ip}")
                else:
                    print(f"   ❌ Failed: {resp.status}")
        except Exception as e:
            print(f"   ❌ Error: {e}")
        print()
        
        # Test 2: Polymarket Gamma API (market data - should work)
        print("2️⃣ Testing Polymarket Gamma API (market discovery)...")
        try:
            async with session.get(
                "https://gamma-api.polymarket.com/markets?active=true&limit=3",
                proxy=proxy_url,
                timeout=10,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    results["gamma_api"] = True
                    print(f"   ✅ OK - Found {len(data)} markets")
                else:
                    results["gamma_api"] = False
                    print(f"   ❌ Failed: {resp.status}")
        except Exception as e:
            results["gamma_api"] = False
            print(f"   ❌ Error: {e}")
        print()
        
        # Test 3: Polymarket CLOB time endpoint
        print("3️⃣ Testing Polymarket CLOB API (trading endpoint)...")
        try:
            async with session.get(
                "https://clob.polymarket.com/time",
                proxy=proxy_url,
                timeout=10,
            ) as resp:
                if resp.status == 200:
                    results["clob_api"] = True
                    print(f"   ✅ OK - CLOB accessible")
                else:
                    results["clob_api"] = False
                    print(f"   ⚠️ Status {resp.status} - May be rate limited")
        except Exception as e:
            results["clob_api"] = False
            print(f"   ⚠️ Error: {e}")
        print()
        
        # Test 4: Binance API (always works)
        print("4️⃣ Testing Binance API (exchange prices)...")
        try:
            async with session.get(
                "https://api.binance.com/api/v3/ticker/price?symbol=ETHUSDT",
                proxy=proxy_url,
                timeout=10,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    results["binance"] = True
                    print(f"   ✅ OK - ETH = ${float(data['price']):,.2f}")
                else:
                    results["binance"] = False
                    print(f"   ❌ Failed: {resp.status}")
        except Exception as e:
            results["binance"] = False
            print(f"   ❌ Error: {e}")
        print()
        
        # Test 5: Coinbase API
        print("5️⃣ Testing Coinbase API (exchange prices)...")
        try:
            async with session.get(
                "https://api.coinbase.com/v2/prices/ETH-USD/spot",
                proxy=proxy_url,
                timeout=10,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    results["coinbase"] = True
                    print(f"   ✅ OK - ETH = ${float(data['data']['amount']):,.2f}")
                else:
                    results["coinbase"] = False
                    print(f"   ❌ Failed: {resp.status}")
        except Exception as e:
            results["coinbase"] = False
            print(f"   ❌ Error: {e}")
        print()
        
        # Test 6: Kraken API
        print("6️⃣ Testing Kraken API (exchange prices)...")
        try:
            async with session.get(
                "https://api.kraken.com/0/public/Ticker?pair=ETHUSD",
                proxy=proxy_url,
                timeout=10,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    results["kraken"] = True
                    if data.get("result"):
                        price = list(data["result"].values())[0]["c"][0]
                        print(f"   ✅ OK - ETH = ${float(price):,.2f}")
                    else:
                        print(f"   ✅ OK")
                else:
                    results["kraken"] = False
                    print(f"   ❌ Failed: {resp.status}")
        except Exception as e:
            results["kraken"] = False
            print(f"   ❌ Error: {e}")
    
    # Summary
    print()
    print("=" * 60)
    print("📋 SUMMARY")
    print("=" * 60)
    print()
    
    gamma_ok = results.get("gamma_api", False)
    exchanges_ok = results.get("binance", False) or results.get("coinbase", False)
    clob_ok = results.get("clob_api", False)
    
    if gamma_ok and exchanges_ok:
        print("✅ VIRTUAL MODE WILL WORK")
        print("   - Market data accessible")
        print("   - Exchange prices accessible")
        print("   - You can run: python -m src.strategies.run_advanced")
        print()
        
        if not clob_ok:
            print("⚠️ REAL TRADING MAY NOT WORK")
            print("   - CLOB API blocked or rate-limited")
            print("   - For real trading, you'll need:")
            print("     a) Run from home, OR")
            print("     b) Use a residential proxy")
    else:
        print("❌ VIRTUAL MODE MAY NOT WORK")
        print("   - Some APIs are blocked")
        print("   - Try with a residential proxy:")
        print("     PROXY_ENABLED=true PROXY_URL=http://... python scripts/test_vps_access.py")
    
    print()
    
    # Return exit code based on virtual mode viability
    return 0 if (gamma_ok and exchanges_ok) else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)

