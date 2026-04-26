// SPDX-License-Identifier: MIT
pragma solidity =0.8.28;

interface IERC20 {
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
    function transfer(address to, uint256 amount) external returns (bool);
    function balanceOf(address account) external view returns (uint256);
}

/// @title WayforthEscrow
/// @notice Non-custodial USDC payment rail. Pulls gross amount from payer,
///         forwards 98.5% to the service owner and 1.5% to the fee recipient
///         in a single atomic transaction. Contract never retains funds.
contract WayforthEscrow {
    /// @notice USDC token contract. Immutable so an admin key compromise cannot
    ///         swap it for a malicious token.
    address public immutable usdc;

    address public feeRecipient;
    address public admin;
    address public pendingAdmin;

    uint256 public constant FEE_BPS = 150;
    uint256 public constant BPS_DENOMINATOR = 10000;

    bool private _entered;

    event PaymentRouted(
        bytes32 indexed serviceId,
        address indexed payer,
        address indexed serviceOwner,
        uint256 grossAmount,
        uint256 feeAmount,
        uint256 netAmount
    );
    event FeeRecipientUpdated(address indexed oldRecipient, address indexed newRecipient);
    event AdminTransferStarted(address indexed currentAdmin, address indexed pendingAdmin);
    event AdminTransferred(address indexed oldAdmin, address indexed newAdmin);
    event TokenRescued(address indexed token, address indexed to, uint256 amount);

    modifier onlyAdmin() {
        require(msg.sender == admin, "Only admin");
        _;
    }

    modifier nonReentrant() {
        require(!_entered, "Reentrant call");
        _entered = true;
        _;
        _entered = false;
    }

    constructor(address _usdc, address _feeRecipient) {
        require(_usdc != address(0), "USDC zero");
        require(_feeRecipient != address(0), "Fee recipient zero");
        usdc = _usdc;
        feeRecipient = _feeRecipient;
        admin = msg.sender;
    }

    /// @notice Route a USDC payment: payer sends `amount`, `serviceOwner`
    ///         receives 98.5%, `feeRecipient` receives 1.5%.
    /// @dev    nonReentrant is defence-in-depth; Circle USDC on Base does not
    ///         trigger callbacks. Fee is floored by integer division, so we
    ///         require feeAmount > 0 to block dust-attack fee avoidance.
    function routePayment(
        bytes32 serviceId,
        address serviceOwner,
        uint256 amount
    ) external nonReentrant {
        require(amount > 0, "Amount must be positive");
        require(serviceOwner != address(0), "Invalid service owner");
        require(serviceOwner != address(this), "Self-payment");

        uint256 feeAmount = (amount * FEE_BPS) / BPS_DENOMINATOR;
        require(feeAmount > 0, "Amount too small for fee");
        uint256 netAmount = amount - feeAmount;

        require(
            IERC20(usdc).transferFrom(msg.sender, address(this), amount),
            "USDC transferFrom failed"
        );
        require(
            IERC20(usdc).transfer(serviceOwner, netAmount),
            "Net transfer failed"
        );
        require(
            IERC20(usdc).transfer(feeRecipient, feeAmount),
            "Fee transfer failed"
        );

        emit PaymentRouted(serviceId, msg.sender, serviceOwner, amount, feeAmount, netAmount);
    }

    /// @notice Update the fee recipient. Probes the new recipient with a zero-value
    ///         USDC transfer to ensure it can receive — guards against bricking
    ///         the payment rail by setting `feeRecipient` to a contract that
    ///         reverts on transfer (M-3 from Opus 4.7 audit).
    function updateFeeRecipient(address newRecipient) external onlyAdmin {
        require(newRecipient != address(0), "Zero address");
        require(newRecipient != address(this), "Self as recipient");
        require(IERC20(usdc).transfer(newRecipient, 0), "Recipient cannot receive");
        address old = feeRecipient;
        feeRecipient = newRecipient;
        emit FeeRecipientUpdated(old, newRecipient);
    }

    /// @notice Rescue tokens accidentally sent to this contract. Cannot rescue
    ///         USDC — that would break the non-custodial property of the
    ///         payment rail. For all other tokens, sweeps the full balance to
    ///         `to`. (M-1 from Opus 4.7 audit.)
    function rescueToken(address token, address to) external onlyAdmin {
        require(token != usdc, "Cannot rescue USDC");
        require(to != address(0), "Zero address");
        uint256 bal = IERC20(token).balanceOf(address(this));
        require(bal > 0, "Nothing to rescue");
        require(IERC20(token).transfer(to, bal), "Rescue transfer failed");
        emit TokenRescued(token, to, bal);
    }

    /// @notice Two-step admin rotation: current admin nominates, nominee accepts.
    function transferAdmin(address newAdmin) external onlyAdmin {
        require(newAdmin != address(0), "Zero address");
        pendingAdmin = newAdmin;
        emit AdminTransferStarted(admin, newAdmin);
    }

    function acceptAdmin() external {
        require(msg.sender == pendingAdmin, "Not pending admin");
        address oldAdmin = admin;
        admin = pendingAdmin;
        pendingAdmin = address(0);
        emit AdminTransferred(oldAdmin, admin);
    }
}
