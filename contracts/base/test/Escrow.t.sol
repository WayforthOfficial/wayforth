// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {WayforthEscrow} from "../src/Escrow.sol";

/// @dev Minimal ERC20 used to simulate USDC under Foundry. Six decimals to match
///      Circle USDC on Base.
contract MockUSDC {
    string public constant name = "USD Coin";
    string public constant symbol = "USDC";
    uint8 public constant decimals = 6;

    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);

    function mint(address to, uint256 amount) external {
        balanceOf[to] += amount;
        emit Transfer(address(0), to, amount);
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        emit Approval(msg.sender, spender, amount);
        return true;
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        require(balanceOf[msg.sender] >= amount, "balance");
        balanceOf[msg.sender] -= amount;
        balanceOf[to] += amount;
        emit Transfer(msg.sender, to, amount);
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        uint256 a = allowance[from][msg.sender];
        require(a >= amount, "allowance");
        require(balanceOf[from] >= amount, "balance");
        if (a != type(uint256).max) allowance[from][msg.sender] = a - amount;
        balanceOf[from] -= amount;
        balanceOf[to] += amount;
        emit Transfer(from, to, amount);
        return true;
    }
}

contract EscrowTest is Test {
    WayforthEscrow internal escrow;
    MockUSDC internal usdc;

    address internal admin = address(0xA11CE);
    address internal feeRecipient = address(0xFEE);
    address internal serviceOwner = address(0x5E);
    address internal payer = address(0xBEEF);
    address internal outsider = address(0xC0DE);

    bytes32 internal constant SERVICE_ID = keccak256("service-1");

    // Base Sepolia USDC: 0x036CbD53842c5426634e7929541eC2318f3dCF7e
    // Kept here for reference — tests use MockUSDC so runs are hermetic.
    address internal constant BASE_SEPOLIA_USDC = 0x036CbD53842c5426634e7929541eC2318f3dCF7e;

    event PaymentRouted(
        bytes32 indexed serviceId,
        address indexed payer,
        address indexed serviceOwner,
        uint256 grossAmount,
        uint256 feeAmount,
        uint256 netAmount
    );
    event FeeRecipientUpdated(address indexed oldRecipient, address indexed newRecipient);

    function setUp() public {
        usdc = new MockUSDC();
        vm.prank(admin);
        escrow = new WayforthEscrow(address(usdc), feeRecipient);
    }

    function _fund(address to, uint256 amount) internal {
        usdc.mint(to, amount);
    }

    // --- constructor ---

    function test_constructor_zeroUsdcReverts() public {
        vm.expectRevert("USDC zero");
        new WayforthEscrow(address(0), feeRecipient);
    }

    function test_constructor_zeroFeeRecipientReverts() public {
        vm.expectRevert("Fee recipient zero");
        new WayforthEscrow(address(usdc), address(0));
    }

    function test_constructor_setsState() public view {
        assertEq(escrow.usdc(), address(usdc));
        assertEq(escrow.feeRecipient(), feeRecipient);
        assertEq(escrow.admin(), admin);
        assertEq(escrow.FEE_BPS(), 150);
        assertEq(escrow.BPS_DENOMINATOR(), 10000);
    }

    // --- routePayment: split correctness ---

    function test_routePayment_splitsCorrectly() public {
        uint256 amount = 1_000_000; // 1 USDC @ 6 decimals
        _fund(payer, amount);

        vm.prank(payer);
        usdc.approve(address(escrow), amount);

        vm.prank(payer);
        vm.expectEmit(true, true, true, true);
        emit PaymentRouted(SERVICE_ID, payer, serviceOwner, amount, 15_000, 985_000);
        escrow.routePayment(SERVICE_ID, serviceOwner, amount);

        assertEq(usdc.balanceOf(payer), 0);
        assertEq(usdc.balanceOf(serviceOwner), 985_000, "98.5% to service owner");
        assertEq(usdc.balanceOf(feeRecipient), 15_000, "1.5% to fee recipient");
        assertEq(usdc.balanceOf(address(escrow)), 0, "escrow holds no funds");
    }

    function test_routePayment_noDustIntegerSplit() public {
        // Fuzz a couple of odd amounts to verify fee + net == gross exactly.
        uint256[4] memory amounts = [uint256(67), 12345, 999_999, 1_234_567];
        for (uint256 i = 0; i < amounts.length; i++) {
            uint256 amount = amounts[i];
            address p = address(uint160(0x1000 + i));
            _fund(p, amount);
            vm.prank(p);
            usdc.approve(address(escrow), amount);
            vm.prank(p);
            escrow.routePayment(SERVICE_ID, serviceOwner, amount);
        }
        // No dust anywhere — every unit accounted for.
        uint256 totalGross = 67 + 12345 + 999_999 + 1_234_567;
        assertEq(
            usdc.balanceOf(serviceOwner) + usdc.balanceOf(feeRecipient),
            totalGross
        );
        assertEq(usdc.balanceOf(address(escrow)), 0);
    }

    function test_routePayment_feePlusNetEqualsGross() public {
        uint256 amount = 987_654_321;
        _fund(payer, amount);
        vm.prank(payer);
        usdc.approve(address(escrow), amount);
        vm.prank(payer);
        escrow.routePayment(SERVICE_ID, serviceOwner, amount);
        assertEq(
            usdc.balanceOf(serviceOwner) + usdc.balanceOf(feeRecipient),
            amount
        );
    }

    // --- routePayment: reverts ---

    function test_routePayment_zeroAmountReverts() public {
        vm.prank(payer);
        vm.expectRevert("Amount must be positive");
        escrow.routePayment(SERVICE_ID, serviceOwner, 0);
    }

    function test_routePayment_zeroServiceOwnerReverts() public {
        vm.prank(payer);
        vm.expectRevert("Invalid service owner");
        escrow.routePayment(SERVICE_ID, address(0), 1_000_000);
    }

    function test_routePayment_selfAsServiceOwnerReverts() public {
        vm.prank(payer);
        vm.expectRevert("Self-payment");
        escrow.routePayment(SERVICE_ID, address(escrow), 1_000_000);
    }

    function test_routePayment_dustAmountReverts() public {
        // amount * 150 / 10000 rounds to 0 for any amount <= 66.
        _fund(payer, 100);
        vm.prank(payer);
        usdc.approve(address(escrow), 100);

        vm.prank(payer);
        vm.expectRevert("Amount too small for fee");
        escrow.routePayment(SERVICE_ID, serviceOwner, 66);
    }

    function test_routePayment_minimumAmountSucceeds() public {
        // 67 is the smallest amount with non-zero fee (67 * 150 / 10000 = 1).
        _fund(payer, 67);
        vm.prank(payer);
        usdc.approve(address(escrow), 67);
        vm.prank(payer);
        escrow.routePayment(SERVICE_ID, serviceOwner, 67);
        assertEq(usdc.balanceOf(feeRecipient), 1);
        assertEq(usdc.balanceOf(serviceOwner), 66);
    }

    function test_routePayment_noApprovalReverts() public {
        _fund(payer, 1_000_000);
        vm.prank(payer);
        vm.expectRevert("allowance");
        escrow.routePayment(SERVICE_ID, serviceOwner, 1_000_000);
    }

    // --- updateFeeRecipient ---

    function test_updateFeeRecipient_adminSucceeds() public {
        address newRecipient = address(0xF001);
        vm.prank(admin);
        vm.expectEmit(true, true, false, false);
        emit FeeRecipientUpdated(feeRecipient, newRecipient);
        escrow.updateFeeRecipient(newRecipient);
        assertEq(escrow.feeRecipient(), newRecipient);
    }

    function test_updateFeeRecipient_nonAdminReverts() public {
        vm.prank(outsider);
        vm.expectRevert("Only admin");
        escrow.updateFeeRecipient(address(0xF001));
    }

    function test_updateFeeRecipient_zeroReverts() public {
        vm.prank(admin);
        vm.expectRevert("Zero address");
        escrow.updateFeeRecipient(address(0));
    }

    function test_updateFeeRecipient_selfReverts() public {
        vm.prank(admin);
        vm.expectRevert("Self as recipient");
        escrow.updateFeeRecipient(address(escrow));
    }

    // --- admin rotation ---

    function test_transferAdmin_twoStep() public {
        vm.prank(admin);
        escrow.transferAdmin(outsider);
        assertEq(escrow.pendingAdmin(), outsider);
        assertEq(escrow.admin(), admin);

        vm.prank(outsider);
        escrow.acceptAdmin();
        assertEq(escrow.admin(), outsider);
        assertEq(escrow.pendingAdmin(), address(0));
    }

    function test_transferAdmin_nonAdminReverts() public {
        vm.prank(outsider);
        vm.expectRevert("Only admin");
        escrow.transferAdmin(outsider);
    }

    function test_acceptAdmin_wrongCallerReverts() public {
        vm.prank(admin);
        escrow.transferAdmin(outsider);
        vm.prank(payer);
        vm.expectRevert("Not pending admin");
        escrow.acceptAdmin();
    }

    // --- fuzz: split arithmetic holds for any valid amount ---

    function testFuzz_routePayment_splitArithmetic(uint256 amount) public {
        amount = bound(amount, 67, type(uint128).max);
        _fund(payer, amount);
        vm.prank(payer);
        usdc.approve(address(escrow), amount);
        vm.prank(payer);
        escrow.routePayment(SERVICE_ID, serviceOwner, amount);

        uint256 expectedFee = (amount * 150) / 10000;
        uint256 expectedNet = amount - expectedFee;
        assertEq(usdc.balanceOf(feeRecipient), expectedFee);
        assertEq(usdc.balanceOf(serviceOwner), expectedNet);
        assertEq(usdc.balanceOf(address(escrow)), 0);
    }
}
