-- Solvr (solvrbot.com) catalog entries — 6 endpoints across free, standard, and full tiers
INSERT INTO services (name, description, endpoint_url, category, coverage_tier, pricing_usdc, source, payment_protocol, x402_supported, metadata)
VALUES
  (
    'Solvr World News',
    'Real-time global news feed from Solvr. Free tier — no per-call cost. Returns latest world news headlines and summaries.',
    'https://api.solvrbot.com/api/v1/news',
    'data',
    1,
    0.0,
    'catalog',
    'wayforth',
    false,
    '{"auth": "bearer_eip191", "auth_note": "Bearer token via EIP-191 wallet signature", "provider": "Solvr", "provider_url": "https://solvrbot.com", "pricing_tier": "free"}'
  ),
  (
    'Solvr World Data',
    'Global economic and macroeconomic data from Solvr. Free tier — covers GDP, inflation, trade, and other country-level indicators.',
    'https://api.solvrbot.com/api/v1/worlddata',
    'data',
    1,
    0.0,
    'catalog',
    'wayforth',
    false,
    '{"auth": "bearer_eip191", "auth_note": "Bearer token via EIP-191 wallet signature", "provider": "Solvr", "provider_url": "https://solvrbot.com", "pricing_tier": "free"}'
  ),
  (
    'Solvr Token Intelligence',
    'On-chain token intelligence for a given contract address (CA). Returns holder analysis, liquidity, volume trends, and risk signals. Standard tier.',
    'https://api.solvrbot.com/api/v1/intel/{ca}',
    'analytics',
    1,
    0.001,
    'catalog',
    'wayforth',
    false,
    '{"auth": "bearer_eip191", "auth_note": "Bearer token via EIP-191 wallet signature", "provider": "Solvr", "provider_url": "https://solvrbot.com", "pricing_tier": "standard", "path_param": "ca"}'
  ),
  (
    'Solvr Token Security Scan',
    'Security scan for a token contract address. Detects honeypots, rug pull indicators, ownership renouncement, and contract vulnerabilities. Standard tier.',
    'https://api.solvrbot.com/api/v1/security/scan',
    'analytics',
    1,
    0.001,
    'catalog',
    'wayforth',
    false,
    '{"auth": "bearer_eip191", "auth_note": "Bearer token via EIP-191 wallet signature", "provider": "Solvr", "provider_url": "https://solvrbot.com", "pricing_tier": "standard", "method": "POST"}'
  ),
  (
    'Solvr Quick Technical Analysis',
    'Fast technical analysis snapshot for a token or asset. Returns key indicators (RSI, MACD, moving averages) in a single low-latency call. Standard tier.',
    'https://api.solvrbot.com/api/v1/ta/quick',
    'analytics',
    1,
    0.001,
    'catalog',
    'wayforth',
    false,
    '{"auth": "bearer_eip191", "auth_note": "Bearer token via EIP-191 wallet signature", "provider": "Solvr", "provider_url": "https://solvrbot.com", "pricing_tier": "standard", "method": "POST"}'
  ),
  (
    'Solvr Full TA Stack',
    'Comprehensive technical analysis suite from Solvr. Runs the full indicator stack — Bollinger Bands, Ichimoku, Fibonacci, volume profile, and pattern recognition. Full tier.',
    'https://api.solvrbot.com/api/v1/ta/analysis',
    'analytics',
    1,
    0.003,
    'catalog',
    'wayforth',
    false,
    '{"auth": "bearer_eip191", "auth_note": "Bearer token via EIP-191 wallet signature", "provider": "Solvr", "provider_url": "https://solvrbot.com", "pricing_tier": "full", "method": "POST"}'
  )
ON CONFLICT (endpoint_url) DO UPDATE SET
  name = EXCLUDED.name,
  description = EXCLUDED.description,
  category = EXCLUDED.category,
  coverage_tier = EXCLUDED.coverage_tier,
  pricing_usdc = EXCLUDED.pricing_usdc,
  metadata = EXCLUDED.metadata;
