from web3 import Web3

BASE_SEPOLIA_RPC = "https://sepolia.base.org"
REGISTRY_ADDRESS = "0xE0596DbF37Fd9e3e5E39822602732CC0865E49C7"
ESCROW_ADDRESS = "0xC9945621CfefD9a15972D3f3d33e2D6f0cc3E320"
USDC_ADDRESS = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"

REGISTRY_ABI = [
    {
        "name": "getService",
        "type": "function",
        "inputs": [{"name": "serviceId", "type": "bytes32"}],
        "outputs": [
            {
                "name": "",
                "type": "tuple",
                "components": [
                    {"name": "name", "type": "string"},
                    {"name": "endpointUrl", "type": "string"},
                    {"name": "category", "type": "string"},
                    {"name": "coverageTier", "type": "uint8"},
                    {"name": "owner", "type": "address"},
                    {"name": "active", "type": "bool"},
                    {"name": "registeredAt", "type": "uint256"},
                ],
            }
        ],
        "stateMutability": "view",
    },
    {
        "name": "serviceCount",
        "type": "function",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
]

USDC_ABI = [
    {
        "name": "approve",
        "type": "function",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
    }
]

ESCROW_ABI = [
    {
        "name": "FEE_BPS",
        "type": "function",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "name": "routePayment",
        "type": "function",
        "inputs": [
            {"name": "serviceId", "type": "bytes32"},
            {"name": "serviceOwner", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [],
        "stateMutability": "nonpayable",
    },
]

# fee_bps is immutable in the contract — no RPC needed per search request
PAYMENT_INFO = {
    "escrow": ESCROW_ADDRESS,
    "usdc": USDC_ADDRESS,
    "network": "base-sepolia",
    "fee_bps": 150,
    "instructions": "Approve USDC to escrow address, then call routePayment(serviceId, serviceOwner, amount)",
}


def get_web3():
    return Web3(Web3.HTTPProvider(BASE_SEPOLIA_RPC))


def get_registry():
    w3 = get_web3()
    return w3.eth.contract(address=REGISTRY_ADDRESS, abi=REGISTRY_ABI)


def get_escrow():
    w3 = get_web3()
    return w3.eth.contract(address=ESCROW_ADDRESS, abi=ESCROW_ABI)


def build_payment_calldata(service_id: str, service_owner: str, amount_usdc: float) -> dict:
    w3 = get_web3()
    usdc = w3.eth.contract(address=USDC_ADDRESS, abi=USDC_ABI)
    escrow = w3.eth.contract(address=ESCROW_ADDRESS, abi=ESCROW_ABI)

    amount_units = int(amount_usdc * 10**6)
    fee_units = (amount_units * 150) // 10000
    net_units = amount_units - fee_units

    service_id_bytes = bytes.fromhex(service_id.removeprefix("0x"))

    approve_calldata = usdc.encode_abi("approve", args=[ESCROW_ADDRESS, amount_units])
    route_calldata = escrow.encode_abi(
        "routePayment", args=[service_id_bytes, service_owner, amount_units]
    )

    return {
        "steps": [
            {
                "step": 1,
                "action": "approve",
                "description": "Approve USDC spend to Wayforth Escrow",
                "to": USDC_ADDRESS,
                "calldata": approve_calldata,
                "value": "0",
            },
            {
                "step": 2,
                "action": "routePayment",
                "description": "Route payment through Wayforth Escrow (1.5% fee)",
                "to": ESCROW_ADDRESS,
                "calldata": route_calldata,
                "value": "0",
            },
        ],
        "summary": {
            "gross_usdc": amount_usdc,
            "fee_usdc": round(fee_units / 10**6, 6),
            "net_usdc": round(net_units / 10**6, 6),
            "fee_pct": 1.5,
            "network": "base-sepolia",
            "usdc_decimals": 6,
        },
    }


def get_chain_stats() -> dict:
    try:
        count = get_registry().functions.serviceCount().call()
        fee_bps = get_escrow().functions.FEE_BPS().call()
        return {
            "onchain_service_count": count,
            "fee_bps": fee_bps,
            "fee_pct": fee_bps / 100,
            "registry": REGISTRY_ADDRESS,
            "escrow": ESCROW_ADDRESS,
            "network": "base-sepolia",
            "usdc": USDC_ADDRESS,
        }
    except Exception as e:
        return {"error": str(e)}
