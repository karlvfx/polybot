"""
USDC Rescue Script - Race the Sweeper Bot

This script attempts to rescue USDC from a compromised wallet by:
1. Pre-signing the USDC transfer
2. Sending POL for gas
3. Immediately broadcasting the USDC transfer

Run with: python rescue_usdc.py
"""

import asyncio
import sys
from web3 import Web3
from eth_account import Account

# Enable mnemonic features
Account.enable_unaudited_hdwallet_features()


def get_private_key(private_key: str, seed_phrase: str) -> str:
    """Get private key from either direct key or seed phrase."""
    if private_key and private_key.startswith("0x"):
        return private_key
    elif seed_phrase and len(seed_phrase.split()) >= 12:
        # Derive from seed phrase (default path for MetaMask/Phantom: m/44'/60'/0'/0/0)
        account = Account.from_mnemonic(seed_phrase)
        return account.key.hex()
    else:
        return ""

# ============================================================================
# CONFIGURATION - FILL THESE IN (use EITHER private key OR seed phrase)
# ============================================================================

# Option 1: Use private keys directly
COMPROMISED_PRIVATE_KEY = "0xf02e36f9b10b74e3ab2b6331fe9ec78f989ccdcc2ce92456c420755498bd26ca"
GAS_DONOR_PRIVATE_KEY = ""    # 0x... (leave empty if using seed phrase)

# Option 2: Use seed phrases (12 or 24 words)
COMPROMISED_SEED_PHRASE = "" 
GAS_DONOR_SEED_PHRASE = "good wagon theme toast artist side hunt jewel jacket vessel pass daring"

# Destination for rescued USDC
DESTINATION_ADDRESS = "0x76217C19E7427e2eE948C8cAE7DB6729bd5Cd35F"

# USDC contract on Polygon
USDC_CONTRACT = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"  # Native USDC on Polygon

# Amount to rescue (in USDC, 6 decimals)
USDC_AMOUNT = 112  # $112 USDC

# POL to send for gas - send extra because sweeper takes some!
POL_FOR_GAS = 0.5  # ~$0.75 worth - even if sweeper takes half, we have enough

# ============================================================================
# POLYGON RPC - Using multiple for reliability
# ============================================================================

RPC_URLS = [
    "https://polygon-rpc.com",
    "https://rpc-mainnet.matic.quiknode.pro",
    "https://polygon-mainnet.g.alchemy.com/v2/demo",
]

# USDC ABI (just transfer function)
USDC_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "_to", "type": "address"},
            {"name": "_value", "type": "uint256"}
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function"
    }
]


def main():
    print("=" * 60)
    print("🚨 USDC RESCUE SCRIPT - Race the Sweeper")
    print("=" * 60)
    
    # Get private keys (from direct key or seed phrase)
    compromised_key = get_private_key(COMPROMISED_PRIVATE_KEY, COMPROMISED_SEED_PHRASE)
    gas_donor_key = get_private_key(GAS_DONOR_PRIVATE_KEY, GAS_DONOR_SEED_PHRASE)
    
    # Validate configuration
    if not compromised_key:
        print("\n❌ ERROR: Please provide compromised wallet credentials!")
        print("   Either set COMPROMISED_PRIVATE_KEY or COMPROMISED_SEED_PHRASE")
        sys.exit(1)
    
    if not gas_donor_key:
        print("\n❌ ERROR: Please provide gas donor wallet credentials!")
        print("   Either set GAS_DONOR_PRIVATE_KEY or GAS_DONOR_SEED_PHRASE")
        sys.exit(1)
    
    # Connect to Polygon
    w3 = None
    for rpc in RPC_URLS:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc))
            if w3.is_connected():
                print(f"✅ Connected to Polygon via {rpc[:40]}...")
                break
        except:
            continue
    
    if not w3 or not w3.is_connected():
        print("❌ Failed to connect to Polygon RPC")
        sys.exit(1)
    
    # Load accounts
    compromised_account = Account.from_key(compromised_key)
    gas_donor_account = Account.from_key(gas_donor_key)
    
    compromised_address = compromised_account.address
    gas_donor_address = gas_donor_account.address
    
    print(f"\n📍 Compromised wallet: {compromised_address}")
    print(f"📍 Gas donor wallet:   {gas_donor_address}")
    print(f"📍 Destination:        {DESTINATION_ADDRESS}")
    
    # Check balances
    usdc_contract = w3.eth.contract(address=USDC_CONTRACT, abi=USDC_ABI)
    
    usdc_balance = usdc_contract.functions.balanceOf(compromised_address).call()
    usdc_balance_decimal = usdc_balance / 1e6
    
    pol_balance_compromised = w3.eth.get_balance(compromised_address)
    pol_balance_donor = w3.eth.get_balance(gas_donor_address)
    
    print(f"\n💰 Balances:")
    print(f"   Compromised USDC: ${usdc_balance_decimal:.2f}")
    print(f"   Compromised POL:  {w3.from_wei(pol_balance_compromised, 'ether'):.6f}")
    print(f"   Gas donor POL:    {w3.from_wei(pol_balance_donor, 'ether'):.6f}")
    
    if usdc_balance == 0:
        print("\n❌ No USDC to rescue!")
        sys.exit(1)
    
    pol_needed = w3.to_wei(POL_FOR_GAS, 'ether')
    if pol_balance_donor < pol_needed:
        print(f"\n❌ Gas donor needs at least {POL_FOR_GAS} POL!")
        sys.exit(1)
    
    # Get current gas price and nonces - USE MAXIMUM SPEED
    gas_price = w3.eth.gas_price
    fast_gas_price = int(gas_price * 2)  # 2X gas price - fast but affordable
    
    nonce_donor = w3.eth.get_transaction_count(gas_donor_address)
    nonce_compromised = w3.eth.get_transaction_count(compromised_address)
    
    print(f"\n⛽ Gas price: {w3.from_wei(fast_gas_price, 'gwei'):.1f} gwei (2X BOOST)")
    
    # ========================================================================
    # BUILD TRANSACTIONS
    # ========================================================================
    
    print("\n🔨 Building transactions...")
    
    # Transaction 1: Send POL for gas
    tx_send_pol = {
        'from': gas_donor_address,
        'to': compromised_address,
        'value': pol_needed,
        'gas': 21000,
        'gasPrice': fast_gas_price,
        'nonce': nonce_donor,
        'chainId': 137,  # Polygon
    }
    
    # Transaction 2: Transfer USDC (pre-signed)
    usdc_amount_raw = int(usdc_balance)  # Transfer ALL USDC
    
    tx_transfer_usdc = usdc_contract.functions.transfer(
        DESTINATION_ADDRESS,
        usdc_amount_raw
    ).build_transaction({
        'from': compromised_address,
        'gas': 65000,  # USDC transfer typically needs ~50k
        'gasPrice': fast_gas_price,
        'nonce': nonce_compromised,
        'chainId': 137,
    })
    
    # Sign both transactions
    signed_pol_tx = w3.eth.account.sign_transaction(tx_send_pol, gas_donor_key)
    signed_usdc_tx = w3.eth.account.sign_transaction(tx_transfer_usdc, compromised_key)
    
    print("✅ Transactions built and signed")
    
    # ========================================================================
    # EXECUTE THE RESCUE
    # ========================================================================
    
    print("\n" + "=" * 60)
    print("🚀 EXECUTING RESCUE - THIS IS IT!")
    print("=" * 60)
    
    input("\nPress ENTER to execute (or Ctrl+C to cancel)...")
    
    try:
        import threading
        import time as time_module
        import concurrent.futures
        
        NUM_SPAM_TXS = 15  # Number of POL spam transactions
        POL_PER_TX = 0.08  # POL per spam tx
        
        print(f"\n💣 BUILDING SPAM ATTACK: {NUM_SPAM_TXS} POL transactions + USDC")
        print(f"   Total POL needed: ~{NUM_SPAM_TXS * POL_PER_TX + 0.1:.2f} POL")
        
        # Build all spam POL transactions
        signed_spam_txs = []
        for i in range(NUM_SPAM_TXS):
            spam_tx = {
                'from': gas_donor_address,
                'to': compromised_address,
                'value': w3.to_wei(POL_PER_TX, 'ether'),
                'gas': 21000,
                'gasPrice': fast_gas_price,
                'nonce': nonce_donor + i,
                'chainId': 137,
            }
            signed = w3.eth.account.sign_transaction(spam_tx, gas_donor_key)
            signed_spam_txs.append(signed.raw_transaction)
        
        usdc_raw = signed_usdc_tx.raw_transaction
        
        results = {'spam': [], 'usdc': None, 'usdc_error': None}
        usdc_sent = threading.Event()
        
        def send_spam_tx(idx, raw_tx):
            try:
                tx_hash = w3.eth.send_raw_transaction(raw_tx)
                results['spam'].append((idx, tx_hash.hex()))
            except Exception as e:
                results['spam'].append((idx, f"ERR: {e}"))
        
        def send_usdc_repeatedly():
            # Try sending USDC multiple times
            for attempt in range(20):
                if usdc_sent.is_set():
                    break
                try:
                    results['usdc'] = w3.eth.send_raw_transaction(usdc_raw)
                    usdc_sent.set()
                    return
                except Exception as e:
                    results['usdc_error'] = e
                    time_module.sleep(0.05)  # 50ms between attempts
        
        print(f"\n💣💣💣 LAUNCHING SPAM ATTACK 💣💣💣")
        print(f"   Sending {NUM_SPAM_TXS} POL txs + USDC retries...")
        
        # Use thread pool for maximum speed
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
            # Submit all spam transactions
            spam_futures = []
            for i, raw_tx in enumerate(signed_spam_txs):
                f = executor.submit(send_spam_tx, i, raw_tx)
                spam_futures.append(f)
            
            # Also try USDC repeatedly in parallel
            usdc_future = executor.submit(send_usdc_repeatedly)
            
            # Wait for all
            concurrent.futures.wait(spam_futures)
            usdc_future.result()
        
        # Print results
        print(f"\n📊 SPAM RESULTS:")
        successful_spam = [r for r in results['spam'] if not r[1].startswith('ERR')]
        print(f"   POL txs sent: {len(successful_spam)}/{NUM_SPAM_TXS}")
        
        if results['usdc'] and not results['usdc_error']:
            usdc_tx_hash = results['usdc']
            print(f"✅ USDC TX: {usdc_tx_hash.hex()}")
        else:
            raise results['usdc_error'] if results['usdc_error'] else Exception("USDC never sent")
        
        pol_tx_hash = successful_spam[0][1] if successful_spam else "none"
        
        print("\n⏳ Waiting for confirmations...")
        
        # Wait for USDC transfer result
        try:
            receipt = w3.eth.wait_for_transaction_receipt(usdc_tx_hash, timeout=60)
            
            if receipt['status'] == 1:
                print("\n" + "=" * 60)
                print("🎉 SUCCESS! USDC RESCUED!")
                print("=" * 60)
                print(f"\n✅ ${usdc_balance_decimal:.2f} USDC sent to {DESTINATION_ADDRESS}")
                print(f"   View: https://polygonscan.com/tx/{usdc_tx_hash.hex()}")
            else:
                print("\n❌ USDC transfer failed!")
                print("   The sweeper might have been faster 😔")
                print(f"   Check: https://polygonscan.com/tx/{usdc_tx_hash.hex()}")
        except Exception as e:
            print(f"\n⚠️ Waiting timed out: {e}")
            print("   Check Polygonscan manually:")
            print(f"   https://polygonscan.com/tx/{usdc_tx_hash.hex()}")
            
    except Exception as e:
        print(f"\n❌ Error during rescue: {e}")
        print("   The sweeper might have intercepted.")
    
    print("\n" + "=" * 60)


if __name__ == "__main__":
    main()

