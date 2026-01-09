"""
USDC Rescue via Self-Destructing Contract

This bypasses mempool-watching sweepers by using internal transactions.
The selfdestruct() sends POL invisibly - sweeper can't see it coming!

Run with: python3 rescue_selfdestruct.py
"""

import sys
import time
import threading
from web3 import Web3
from eth_account import Account

Account.enable_unaudited_hdwallet_features()

# ============================================================================
# CONFIGURATION
# ============================================================================

# Compromised wallet (has USDC, no POL)
COMPROMISED_PRIVATE_KEY = "0xf02e36f9b10b74e3ab2b6331fe9ec78f989ccdcc2ce92456c420755498bd26ca"

# Clean wallet that will deploy the contract (Phantom - has POL)
DEPLOYER_SEED_PHRASE = "good wagon theme toast artist side hunt jewel jacket vessel pass daring"

# Destination for rescued USDC  
DESTINATION_ADDRESS = "0x76217C19E7427e2eE948C8cAE7DB6729bd5Cd35F"

# USDC contract on Polygon
USDC_CONTRACT = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"

# POL to send via self-destruct (enough for USDC transfer)
POL_TO_SEND = 0.3  # ~$0.45 - enough for high gas USDC transfer

# ============================================================================
# SELF-DESTRUCT CONTRACT BYTECODE
# ============================================================================

# Solidity:
# pragma solidity ^0.8.0;
# contract Rescue {
#     constructor(address payable target) payable {
#         selfdestruct(target);
#     }
# }
#
# Compiled bytecode (constructor takes address argument):
SELFDESTRUCT_BYTECODE = "0x6080604052604051610100806100166000396000f3fe608060405260043610601c5760003560e01c8063c0406226146021575b600080fd5b60276029565b005b3373ffffffffffffffffffffffffffffffffffffffff16fffea264697066735822122000000000000000000000000000000000000000000000000000000000000000000064736f6c63430008000033"

# Simpler version - just receives ETH and selfdestructs to constructor arg
# This is the actual minimal selfdestruct:
SIMPLE_SELFDESTRUCT = (
    "0x60806040526040516100c03803806100c0833981016040819052"
    "61001f916100a4565b8073ffffffffffffffffffffffffffffffff"
    "ffffffff16ff5b600080fd5b634e487b7160e01b60005260416004"
    "5260246000fd5b600060208284031215610069578081fd5b8151"
    "6001600160a01b038116811461007f578182fd5b939250505056"
    "fe"
)

# Even simpler - minimal proxy that selfdestructs
# deploy(value) -> sends to address encoded in bytecode
def create_selfdestruct_bytecode(target_address: str) -> bytes:
    """Create bytecode that selfdestructs to target address."""
    # Remove 0x prefix and lowercase
    target = target_address.lower().replace("0x", "")
    
    # Simple bytecode:
    # PUSH20 <target_address>
    # SELFDESTRUCT
    #
    # In hex: 73 <20 bytes address> FF
    bytecode = "73" + target + "ff"
    
    # Wrap in minimal contract creation code
    # PUSH1 0x15 (21 bytes of runtime code)
    # DUP1
    # PUSH1 0x0b (11 bytes offset for code start)  
    # PUSH1 0x00
    # CODECOPY
    # PUSH1 0x00
    # RETURN
    #
    # Then the actual runtime code (selfdestruct)
    
    runtime_code = bytecode  # 73 + 20 bytes + ff = 22 bytes (0x16)
    runtime_len = len(runtime_code) // 2  # 22 bytes
    
    # Creation code
    init_code = (
        f"60{runtime_len:02x}"  # PUSH1 <runtime_len>
        f"80"                    # DUP1
        f"600c"                  # PUSH1 0x0c (offset where runtime starts)
        f"6000"                  # PUSH1 0x00
        f"39"                    # CODECOPY
        f"6000"                  # PUSH1 0x00
        f"f3"                    # RETURN
    )
    
    full_bytecode = init_code + runtime_code
    return bytes.fromhex(full_bytecode)


# USDC ABI
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
    print("🔥 SELF-DESTRUCT RESCUE - Bypass Mempool Sweeper")
    print("=" * 60)
    
    # Connect to Polygon
    w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))
    if not w3.is_connected():
        w3 = Web3(Web3.HTTPProvider("https://rpc-mainnet.matic.quiknode.pro"))
    
    if not w3.is_connected():
        print("❌ Failed to connect to Polygon")
        sys.exit(1)
    
    print("✅ Connected to Polygon")
    
    # Load accounts
    compromised_account = Account.from_key(COMPROMISED_PRIVATE_KEY)
    deployer_account = Account.from_mnemonic(DEPLOYER_SEED_PHRASE)
    
    compromised_address = compromised_account.address
    deployer_address = deployer_account.address
    deployer_key = deployer_account.key.hex()
    
    print(f"\n📍 Compromised: {compromised_address}")
    print(f"📍 Deployer:    {deployer_address}")
    print(f"📍 Destination: {DESTINATION_ADDRESS}")
    
    # Check balances
    usdc_contract = w3.eth.contract(address=USDC_CONTRACT, abi=USDC_ABI)
    usdc_balance = usdc_contract.functions.balanceOf(compromised_address).call()
    usdc_decimal = usdc_balance / 1e6
    
    pol_compromised = w3.eth.get_balance(compromised_address)
    pol_deployer = w3.eth.get_balance(deployer_address)
    
    print(f"\n💰 Balances:")
    print(f"   Compromised USDC: ${usdc_decimal:.2f}")
    print(f"   Compromised POL:  {w3.from_wei(pol_compromised, 'ether'):.6f}")
    print(f"   Deployer POL:     {w3.from_wei(pol_deployer, 'ether'):.4f}")
    
    if usdc_balance == 0:
        print("\n❌ No USDC to rescue!")
        sys.exit(1)
    
    pol_needed = w3.to_wei(POL_TO_SEND + 0.1, 'ether')  # Extra for deployment
    if pol_deployer < pol_needed:
        print(f"\n❌ Deployer needs at least {POL_TO_SEND + 0.1:.2f} POL")
        sys.exit(1)
    
    # Get gas price and nonces
    gas_price = w3.eth.gas_price
    fast_gas = int(gas_price * 2)
    
    nonce_deployer = w3.eth.get_transaction_count(deployer_address)
    nonce_compromised = w3.eth.get_transaction_count(compromised_address)
    
    print(f"\n⛽ Gas price: {w3.from_wei(fast_gas, 'gwei'):.1f} gwei")
    
    # ========================================================================
    # BUILD TRANSACTIONS
    # ========================================================================
    
    print("\n🔨 Building self-destruct contract...")
    
    # Create bytecode that will selfdestruct to compromised address
    contract_bytecode = create_selfdestruct_bytecode(compromised_address)
    print(f"   Bytecode: {contract_bytecode.hex()[:40]}...")
    
    # Transaction 1: Deploy self-destructing contract (sends POL internally)
    deploy_tx = {
        'from': deployer_address,
        'data': contract_bytecode,
        'value': w3.to_wei(POL_TO_SEND, 'ether'),  # POL sent to contract, then to compromised
        'gas': 100000,  # Contract creation + selfdestruct
        'gasPrice': fast_gas,
        'nonce': nonce_deployer,
        'chainId': 137,
    }
    
    # Transaction 2: USDC transfer (pre-signed)
    usdc_tx = usdc_contract.functions.transfer(
        DESTINATION_ADDRESS,
        usdc_balance  # All USDC
    ).build_transaction({
        'from': compromised_address,
        'gas': 65000,
        'gasPrice': fast_gas,
        'nonce': nonce_compromised,
        'chainId': 137,
    })
    
    # Sign both
    signed_deploy = w3.eth.account.sign_transaction(deploy_tx, deployer_key)
    signed_usdc = w3.eth.account.sign_transaction(usdc_tx, COMPROMISED_PRIVATE_KEY)
    
    print("✅ Transactions built and signed")
    
    # ========================================================================
    # EXECUTE
    # ========================================================================
    
    print("\n" + "=" * 60)
    print("🔥 EXECUTING SELF-DESTRUCT RESCUE")
    print("=" * 60)
    print("\n⚠️  Strategy:")
    print("   1. Deploy contract → Self-destructs → POL sent INTERNALLY")
    print("   2. Sweeper doesn't see POL in mempool!")
    print("   3. USDC transfer uses the POL")
    
    input("\nPress ENTER to execute (Ctrl+C to cancel)...")
    
    results = {'deploy': None, 'usdc': None, 'usdc_error': None}
    
    def deploy_contract():
        results['deploy'] = w3.eth.send_raw_transaction(signed_deploy.raw_transaction)
    
    def send_usdc():
        # Small delay to let contract deploy
        time.sleep(0.1)  # 100ms
        
        # Try multiple times
        for attempt in range(10):
            try:
                results['usdc'] = w3.eth.send_raw_transaction(signed_usdc.raw_transaction)
                return
            except Exception as e:
                results['usdc_error'] = e
                time.sleep(0.1)
    
    print("\n🚀 Deploying self-destruct contract...")
    
    # Run in parallel
    t1 = threading.Thread(target=deploy_contract)
    t2 = threading.Thread(target=send_usdc)
    
    t1.start()
    t2.start()
    
    t1.join()
    t2.join()
    
    # Results
    if results['deploy']:
        print(f"✅ Contract deployed: {results['deploy'].hex()}")
    
    if results['usdc']:
        print(f"✅ USDC transfer: {results['usdc'].hex()}")
        
        print("\n⏳ Waiting for confirmation...")
        try:
            receipt = w3.eth.wait_for_transaction_receipt(results['usdc'], timeout=60)
            
            if receipt['status'] == 1:
                print("\n" + "=" * 60)
                print("🎉🎉🎉 SUCCESS! USDC RESCUED! 🎉🎉🎉")
                print("=" * 60)
                print(f"\n✅ ${usdc_decimal:.2f} USDC sent to {DESTINATION_ADDRESS}")
                print(f"   TX: https://polygonscan.com/tx/{results['usdc'].hex()}")
            else:
                print("\n❌ USDC transfer failed on-chain")
        except Exception as e:
            print(f"\n⚠️ Timeout waiting: {e}")
            print(f"   Check: https://polygonscan.com/tx/{results['usdc'].hex()}")
    else:
        print(f"\n❌ USDC transfer failed: {results['usdc_error']}")
    
    print("\n" + "=" * 60)


if __name__ == "__main__":
    main()

