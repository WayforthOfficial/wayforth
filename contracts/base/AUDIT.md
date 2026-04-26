# WayforthEscrow + WayforthRegistry — Security Audit Log

## Automated Analysis

### Foundry Test Suite
- 54 tests passing (27 Escrow, 27 Registry)
- 256-run fuzz suite on payment routing (`testFuzz_routePayment_splitArithmetic`)
- Run: `forge test -vvv`

### Slither Static Analysis (April 2026) — v0.11.4

Run: `slither src/ --print human-summary`

**Summary after pragma pin to `=0.8.28` (src/ — 3 contracts, 205 SLOC):**
- High: 0
- Medium: 0
- Low: 7
- Informational: 0

All 7 Low findings are the same `timestamp` detector false positive: Slither flags
any address comparison on a struct slot whose initialization touched `block.timestamp`
(via `services[id].registeredAt`). These checks are owner/existence checks, not
timestamp-dependent comparisons. The `serviceCount` nonce already guarantees
serviceId uniqueness — the timestamp value is not a security boundary.

The previous Informational `solc-version` finding was resolved by pinning pragma
to `=0.8.28` (no known severe bugs in this version).

**Action items from Slither:**
- [x] Pin pragma to `=0.8.28` (resolved 2026-04-26)

### Aderyn Analysis (April 2026)

Not available in current environment (requires `cargo`). Run manually:
```bash
cargo install aderyn
cd contracts/base && aderyn .
```

---

## Opus 4.7 Follow-up Audit (2026-04-26)

After the initial Opus 4.7 review (9 issues, all resolved — see below), a deeper
audit identified 3 Medium and 5 Low operational-hardening issues. All Mediums and
the deployment-related Low were resolved:

| ID | Severity | Issue | Resolution |
|---|---|---|---|
| M-1 | Medium | Stuck USDC has no rescue path — non-USDC tokens accidentally sent to Escrow are permanently locked | **Fixed**: added `rescueToken(address, address)` (Escrow.sol). Refuses to rescue USDC to preserve non-custodial property. |
| M-2 | Medium | Service ownership cannot be transferred — losing a service-owner key permanently bricks the service | **Fixed**: added two-step `transferServiceOwnership` / `acceptServiceOwnership` (Registry.sol). |
| M-3 | Medium | Fee recipient DoS — admin could brick the rail by setting `feeRecipient` to a USDC-blacklisted or otherwise non-receiving address | **Fixed**: `updateFeeRecipient` now probes the recipient with `IERC20(usdc).transfer(newRecipient, 0)` before committing. |
| L-1 | Low | No admin renunciation path | Deferred — `transferAdmin(address(0))` reverts; can be addressed in a future contract revision if full decentralization is needed |
| L-2 | Low | No timelock on sensitive admin operations | **Fixed (deployment-only)**: added `script/Deploy.s.sol` that supports `TIMELOCK_ADDRESS` env var and initiates `transferAdmin` to a timelock at deploy time |
| L-3 | Low | `getOwnerServices` returns unbounded array | Deferred — UX risk only, not a security risk |
| L-4 | Low | Storage strings have no length cap in Registry | Deferred — gas griefing on Base is bounded by L1 cost |
| L-5 | Low | `block.timestamp` in serviceId derivation is redundant | Deferred — would require a migration; Slither false positives documented |

## Original Opus 4.7 Security Review (April 2026)

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
| WayforthRegistry | `0x55810EfB3444A693556C3f9910dbFbF2dDaC369C` |
| WayforthEscrow | `0xE6EDB0a93e0e0cB9F0402Bd49F2eD1Fffc448809` |
| Deployer | `0xAE99a420073780bCcd13E832222E0b07731da431` |

---

## Deployment History

### Base Sepolia — v2 (April 26, 2026)
Redeployed after Opus 4.7 security review. Changes: `rescueToken()`, two-step ownership transfer, `updateFeeRecipient` zero-value probe, pragma pinned to `=0.8.28`.
- Registry: `0x55810EfB3444A693556C3f9910dbFbF2dDaC369C`
- Escrow: `0xE6EDB0a93e0e0cB9F0402Bd49F2eD1Fffc448809`
- Registry deploy tx: `0x8ad0969ff8b458174e89ca1fd764b1d040a03e99d170551814539711500958cc`
- Escrow deploy tx: `0xfb99e89c969133e06f4a8ffeea409b11b0a3204bb0ba72eeddae2a819afdfa98`
- Both contracts verified on Basescan

### Base Sepolia — v1 (April 24, 2026) — DEPRECATED
- Registry: `0xE0596DbF37Fd9e3e5E39822602732CC0865E49C7`
- Escrow: `0xC9945621CfefD9a15972D3f3d33e2D6f0cc3E320`
