from decimal import Decimal

# All prices as strings to avoid float precision issues with USDC (6 decimal places)
X402_PRICES_USDC = {
    # Inference
    "groq":           "0.003",
    "together":       "0.004",
    # Translation
    "deepl":          "0.020",
    # Search
    "serper":         "0.003",
    "tavily":         "0.004",
    "brave":          "0.005",
    "perplexity":     "0.010",
    # Data
    "openweather":    "0.003",
    "newsapi":        "0.005",
    "alphavantage":   "0.004",
    "jina":           "0.004",
    # Audio/Voice
    "assemblyai":     "0.020",
    "elevenlabs":     "0.200",
    # Image
    "stability":      "0.100",
    "stability_core": "0.045",
    # Email
    "resend":         "0.003",
    # Discovery
    "search":         "0.001",
}


def to_micro_usdc(price_str: str) -> str:
    """Convert USDC price string to micro-units for 402 header.

    USDC has 6 decimal places. $0.003 USDC = 3000 micro-units.
    """
    price = Decimal(price_str)
    micro = price * Decimal("1000000")
    return str(int(micro))


# Cross-rail fee: ONLY on external x402 catalog services, never on our 16 managed services
CROSS_RAIL_FEE_RATE = Decimal("0.05")

# Routing fee: internal accounting only, NEVER shown to users
ROUTING_FEE_RATE = Decimal("0.015")

# Minimum charge per x402 call ($0.001 USDC covers gas on Base)
X402_MIN_USDC = Decimal("0.001")

# Payment verification timeout — after this, accept optimistically
X402_VERIFY_TIMEOUT_MS = 5000
