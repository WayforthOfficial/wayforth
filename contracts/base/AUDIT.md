# WayforthEscrow + WayforthRegistry — Security Audit Log

## Automated Analysis

### Foundry Test Suite
- 39 tests passing (20 Escrow, 19 Registry)
- 256-run fuzz suite on payment routing (`testFuzz_routePayment_splitArithmetic`)
- Run: `forge test -vvv`

### Slither Static Analysis (April 2026) — v0.11.4

Run: `slither src/ --print human-summary`

**Summary (src/ — 3 contracts, 170 SLOC):**
- High: 0
- Medium: 0
- Low: 3
- Informational: 1

**Escrow.sol — 1 finding:**

| Severity | Detector | Finding | Status |
|---|---|---|---|
| Informational | `solc-version` | `^0.8.20` pragma includes compiler versions with known bugs (VerbatimInvalidDeduplication, FullInlinerNonExpressionSplitArgumentEvaluationOrder, MissingSideEffectsOnSelectorAccess) | Accepted — none of these bugs affect our contracts; will pin to `=0.8.28` before mainnet |

**Registry.sol — 4 findings:**

| Severity | Detector | Finding | Status |
|---|---|---|---|
| Low | `timestamp` | `block.timestamp` used in `registerService` serviceId derivation — Slither flags comparisons that use the storage slot written with this value | False positive — timestamp is a uniqueness nonce, not a security boundary; collision check (`owner == address(0)`) is sound |
| Low | `timestamp` | `updateTier` existence check uses slot derived from timestamp | False positive — same as above |
| Low | `timestamp` | `deactivateService` existence + auth checks use slot derived from timestamp | False positive — same as above |
| Informational | `solc-version` | `^0.8.20` pragma (same as Escrow) | Accepted — same note |

**Action items from Slither:**
- [ ] Pin pragma to `=0.8.28` in both contracts before mainnet deployment

### Aderyn Analysis (April 2026)

Not available in current environment (requires `cargo`). Run manually:
```bash
cargo install aderyn
cd contracts/base && aderyn .
```

---

## Opus 4.7 Security Review (April 2026)

9 issues identified and resolved prior to testnet deployment:

| Severity | Issue | Resolution |
|---|---|---|
| Critical | Reentrancy vulnerability in `routePayment` — external ERC-20 calls without guard | Fixed: custom `nonReentrant` modifier (Escrow.sol:44–49). Defense-in-depth; Circle USDC on Base has no callbacks. |
| Critical | Mutable USDC address — admin key compromise could swap to malicious token | Fixed: `address public immutable usdc` (Escrow.sol:16). Cannot be changed post-deploy. |
| High | Single-step admin transfer — losing admin key permanently locks admin functions | Fixed: two-step `transferAdmin` / `acceptAdmin` pattern in both contracts. |
| High | Dust-attack fee avoidance — integer division floors fee to 0 for tiny amounts | Fixed: `require(feeAmount > 0, "Amount too small for fee")` (Escrow.sol:74). |
| High | `abi.encodePacked` hash collision — string concat ambiguity allows same hash for different inputs | Fixed: `abi.encode(endpointUrl, msg.sender, block.timestamp, serviceCount)` (Registry.sol:53–55). |
| Medium | Missing zero-address check on `serviceOwner` param | Fixed: `require(serviceOwner != address(0), "Invalid service owner")` (Escrow.sol:70). |
| Medium | Self-payment — contract routing payment to itself retains funds | Fixed: `require(serviceOwner != address(this), "Self-payment")` (Escrow.sol:71). |
| Medium | Missing zero-address check in `updateFeeRecipient` | Fixed: `require(newRecipient != address(0), "Zero address")` (Escrow.sol:94). |
| Medium | Self-as-feeRecipient — could silently misconfigure fee routing | Fixed: `require(newRecipient != address(this), "Self as recipient")` (Escrow.sol:95). |

**Known limitations (design decisions, not vulnerabilities):**
- No uniqueness enforcement on `endpointUrl` in Registry — duplicate endpoint detection is off-chain by design
- No service ownership transfer — intentional v1 scope constraint
- No reactivation path for `deactivateService` — one-way deactivation by design

---

## Paid Audit Plan

- **Target:** Before Base mainnet deployment
- **Options:** Spearbit, Trail of Bits, Cyfrin, Code4rena contest ($5–50K range)
- **Timing:** After seed round close
- **Required for:** Mainnet deployment, institutional adoption

---

## Contract Addresses (Base Sepolia Testnet)

| Contract | Address |
|---|---|
| WayforthRegistry | `0xE0596DbF37Fd9e3e5E39822602732CC0865E49C7` |
| WayforthEscrow | `0xC9945621CfefD9a15972D3f3d33e2D6f0cc3E320` |
| Deployer | `0xAE99a420073780bCcd13E832222E0b07731da431` |
