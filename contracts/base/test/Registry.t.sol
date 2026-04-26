// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {WayforthRegistry} from "../src/Registry.sol";

contract RegistryTest is Test {
    WayforthRegistry internal registry;

    address internal admin = address(0xA11CE);
    address internal alice = address(0xBEEF);
    address internal bob = address(0xCAFE);

    event ServiceRegistered(bytes32 indexed serviceId, string name, address indexed owner);
    event ServiceUpdated(bytes32 indexed serviceId, uint8 newTier);
    event ServiceDeactivated(bytes32 indexed serviceId);
    event AdminTransferStarted(address indexed currentAdmin, address indexed pendingAdmin);
    event AdminTransferred(address indexed oldAdmin, address indexed newAdmin);
    event ServiceOwnershipTransferStarted(
        bytes32 indexed serviceId, address indexed currentOwner, address indexed pendingOwner
    );
    event ServiceOwnershipTransferred(
        bytes32 indexed serviceId, address indexed oldOwner, address indexed newOwner
    );

    function setUp() public {
        vm.prank(admin);
        registry = new WayforthRegistry();
    }

    function _register(address caller, string memory url) internal returns (bytes32 id) {
        vm.prank(caller);
        id = registry.registerService("Name", url, "inference");
    }

    // --- registerService ---

    function test_registerService_setsAllFields() public {
        vm.prank(alice);
        bytes32 id = registry.registerService("GPT-X", "https://gpt-x.example/v1", "inference");

        WayforthRegistry.Service memory s = registry.getService(id);
        assertEq(s.name, "GPT-X");
        assertEq(s.endpointUrl, "https://gpt-x.example/v1");
        assertEq(s.category, "inference");
        assertEq(s.coverageTier, 0);
        assertEq(s.owner, alice);
        assertTrue(s.active);
        assertEq(s.registeredAt, block.timestamp);
    }

    function test_registerService_emitsEvent() public {
        vm.expectEmit(false, true, false, true);
        emit ServiceRegistered(bytes32(0), "GPT-X", alice);
        vm.prank(alice);
        registry.registerService("GPT-X", "https://gpt-x.example/v1", "inference");
    }

    function test_registerService_incrementsCount() public {
        assertEq(registry.serviceCount(), 0);
        _register(alice, "https://a.example");
        assertEq(registry.serviceCount(), 1);
        _register(bob, "https://b.example");
        assertEq(registry.serviceCount(), 2);
    }

    function test_registerService_appendsToOwnerServices() public {
        bytes32 id1 = _register(alice, "https://a.example");
        bytes32 id2 = _register(alice, "https://b.example");
        bytes32[] memory owned = registry.getOwnerServices(alice);
        assertEq(owned.length, 2);
        assertEq(owned[0], id1);
        assertEq(owned[1], id2);
    }

    function test_registerService_revertsOnEmptyName() public {
        vm.prank(alice);
        vm.expectRevert("Empty name");
        registry.registerService("", "https://a.example", "inference");
    }

    function test_registerService_revertsOnEmptyUrl() public {
        vm.prank(alice);
        vm.expectRevert("Empty endpoint URL");
        registry.registerService("Name", "", "inference");
    }

    function test_registerService_sameBlockSameUrlDifferentIds() public {
        bytes32 id1 = _register(alice, "https://a.example");
        bytes32 id2 = _register(alice, "https://a.example");
        assertTrue(id1 != id2, "serviceCount must disambiguate same-block registrations");
    }

    // --- updateTier ---

    function test_updateTier_adminSucceeds() public {
        bytes32 id = _register(alice, "https://a.example");
        vm.prank(admin);
        vm.expectEmit(true, false, false, true);
        emit ServiceUpdated(id, 2);
        registry.updateTier(id, 2);
        assertEq(registry.getService(id).coverageTier, 2);
    }

    function test_updateTier_nonAdminReverts() public {
        bytes32 id = _register(alice, "https://a.example");
        vm.prank(alice);
        vm.expectRevert("Only admin");
        registry.updateTier(id, 2);
    }

    function test_updateTier_tierAboveThreeReverts() public {
        bytes32 id = _register(alice, "https://a.example");
        vm.prank(admin);
        vm.expectRevert("Invalid tier");
        registry.updateTier(id, 4);
    }

    function test_updateTier_nonexistentServiceReverts() public {
        vm.prank(admin);
        vm.expectRevert("Service does not exist");
        registry.updateTier(bytes32(uint256(0xdead)), 2);
    }

    // --- deactivateService ---

    function test_deactivateService_ownerSucceeds() public {
        bytes32 id = _register(alice, "https://a.example");
        vm.prank(alice);
        vm.expectEmit(true, false, false, false);
        emit ServiceDeactivated(id);
        registry.deactivateService(id);
        assertFalse(registry.getService(id).active);
    }

    function test_deactivateService_adminSucceeds() public {
        bytes32 id = _register(alice, "https://a.example");
        vm.prank(admin);
        registry.deactivateService(id);
        assertFalse(registry.getService(id).active);
    }

    function test_deactivateService_thirdPartyReverts() public {
        bytes32 id = _register(alice, "https://a.example");
        vm.prank(bob);
        vm.expectRevert("Not authorized");
        registry.deactivateService(id);
    }

    function test_deactivateService_nonexistentReverts() public {
        vm.prank(admin);
        vm.expectRevert("Service does not exist");
        registry.deactivateService(bytes32(uint256(0xdead)));
    }

    // --- getService ---

    function test_getService_returnsStruct() public {
        bytes32 id = _register(alice, "https://a.example");
        WayforthRegistry.Service memory s = registry.getService(id);
        assertEq(s.owner, alice);
        assertEq(s.endpointUrl, "https://a.example");
    }

    // --- admin rotation ---

    function test_transferAdmin_twoStepRotation() public {
        vm.prank(admin);
        vm.expectEmit(true, true, false, false);
        emit AdminTransferStarted(admin, bob);
        registry.transferAdmin(bob);
        assertEq(registry.pendingAdmin(), bob);
        assertEq(registry.admin(), admin, "admin not rotated before accept");

        vm.prank(bob);
        vm.expectEmit(true, true, false, false);
        emit AdminTransferred(admin, bob);
        registry.acceptAdmin();
        assertEq(registry.admin(), bob);
        assertEq(registry.pendingAdmin(), address(0));
    }

    function test_transferAdmin_nonAdminReverts() public {
        vm.prank(alice);
        vm.expectRevert("Only admin");
        registry.transferAdmin(bob);
    }

    function test_acceptAdmin_onlyPending() public {
        vm.prank(admin);
        registry.transferAdmin(bob);
        vm.prank(alice);
        vm.expectRevert("Not pending admin");
        registry.acceptAdmin();
    }

    // --- service ownership transfer: M-2 ---

    function test_transferServiceOwnership_twoStepRotation() public {
        bytes32 id = _register(alice, "https://a.example");

        vm.prank(alice);
        vm.expectEmit(true, true, true, false);
        emit ServiceOwnershipTransferStarted(id, alice, bob);
        registry.transferServiceOwnership(id, bob);

        assertEq(registry.pendingServiceOwner(id), bob);
        assertEq(registry.getService(id).owner, alice, "owner not rotated before accept");

        vm.prank(bob);
        vm.expectEmit(true, true, true, false);
        emit ServiceOwnershipTransferred(id, alice, bob);
        registry.acceptServiceOwnership(id);

        assertEq(registry.getService(id).owner, bob);
        assertEq(registry.pendingServiceOwner(id), address(0));
    }

    function test_transferServiceOwnership_appendsToNewOwner() public {
        bytes32 id = _register(alice, "https://a.example");
        vm.prank(alice);
        registry.transferServiceOwnership(id, bob);
        vm.prank(bob);
        registry.acceptServiceOwnership(id);

        bytes32[] memory bobsServices = registry.getOwnerServices(bob);
        assertEq(bobsServices.length, 1);
        assertEq(bobsServices[0], id);

        // Old owner's array still references id by design — indexers read
        // current ownership from `services[id].owner`, not `ownerServices[old]`.
        bytes32[] memory alicesServices = registry.getOwnerServices(alice);
        assertEq(alicesServices.length, 1);
        assertEq(alicesServices[0], id);
    }

    function test_transferServiceOwnership_nonOwnerReverts() public {
        bytes32 id = _register(alice, "https://a.example");
        vm.prank(bob);
        vm.expectRevert("Not service owner");
        registry.transferServiceOwnership(id, bob);
    }

    function test_transferServiceOwnership_adminCannot() public {
        // Admin can update tiers and deactivate, but cannot reassign ownership.
        bytes32 id = _register(alice, "https://a.example");
        vm.prank(admin);
        vm.expectRevert("Not service owner");
        registry.transferServiceOwnership(id, bob);
    }

    function test_transferServiceOwnership_zeroAddressReverts() public {
        bytes32 id = _register(alice, "https://a.example");
        vm.prank(alice);
        vm.expectRevert("Zero address");
        registry.transferServiceOwnership(id, address(0));
    }

    function test_transferServiceOwnership_nonexistentReverts() public {
        vm.prank(alice);
        vm.expectRevert("Service does not exist");
        registry.transferServiceOwnership(bytes32(uint256(0xdead)), bob);
    }

    function test_acceptServiceOwnership_wrongCallerReverts() public {
        bytes32 id = _register(alice, "https://a.example");
        vm.prank(alice);
        registry.transferServiceOwnership(id, bob);

        vm.prank(address(0xF00D));
        vm.expectRevert("Not pending owner");
        registry.acceptServiceOwnership(id);
    }

    function test_transferServiceOwnership_overwritePending() public {
        bytes32 id = _register(alice, "https://a.example");

        vm.prank(alice);
        registry.transferServiceOwnership(id, bob);
        assertEq(registry.pendingServiceOwner(id), bob);

        // Owner can re-target the pending transfer before acceptance.
        address otherCandidate = address(0xF00D);
        vm.prank(alice);
        registry.transferServiceOwnership(id, otherCandidate);
        assertEq(registry.pendingServiceOwner(id), otherCandidate);

        // Original nominee can no longer accept.
        vm.prank(bob);
        vm.expectRevert("Not pending owner");
        registry.acceptServiceOwnership(id);
    }
}
