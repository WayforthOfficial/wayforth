// SPDX-License-Identifier: MIT
pragma solidity =0.8.28;

import {Script} from "forge-std/Script.sol";
import {WayforthRegistry} from "../src/Registry.sol";
import {WayforthEscrow} from "../src/Escrow.sol";

/// @notice Deploys Registry + Escrow and (optionally) hands admin to a timelock.
///
/// Required env:
///   USDC_ADDRESS         - USDC token address on the target chain
///   FEE_RECIPIENT        - Initial fee recipient (EOA or multisig)
///
/// Optional env (L-2 from Opus 4.7 audit):
///   TIMELOCK_ADDRESS     - If set, deployer initiates `transferAdmin` to this
///                          address on both contracts. The timelock must then
///                          call `acceptAdmin` from a queued operation. If
///                          unset, admin remains the deployer EOA.
///
/// Example (Base Sepolia):
///   forge script script/Deploy.s.sol \
///     --rpc-url $BASE_SEPOLIA_RPC \
///     --private-key $DEPLOYER_KEY \
///     --broadcast --verify
contract Deploy is Script {
    function run() external {
        address usdc = vm.envAddress("USDC_ADDRESS");
        address feeRecipient = vm.envAddress("FEE_RECIPIENT");
        address timelock = vm.envOr("TIMELOCK_ADDRESS", address(0));

        vm.startBroadcast();

        WayforthRegistry registry = new WayforthRegistry();
        WayforthEscrow escrow = new WayforthEscrow(usdc, feeRecipient);

        if (timelock != address(0)) {
            registry.transferAdmin(timelock);
            escrow.transferAdmin(timelock);
        }

        vm.stopBroadcast();

        // Log addresses so the deployment can be parsed from forge output.
        // The timelock (if supplied) must call `acceptAdmin()` on each contract
        // separately to complete the rotation.
        emit log_named_address("WayforthRegistry", address(registry));
        emit log_named_address("WayforthEscrow", address(escrow));
        if (timelock != address(0)) {
            emit log_named_address("PendingAdmin (timelock)", timelock);
        }
    }

    event log_named_address(string key, address val);
}
