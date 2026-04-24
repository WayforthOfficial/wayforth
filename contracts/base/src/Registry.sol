// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title WayforthRegistry
/// @notice On-chain mirror of the off-chain service catalog. Stores service
///         metadata and ownership so agents can verify a service's owner
///         before routing payment to it.
contract WayforthRegistry {
    struct Service {
        string name;
        string endpointUrl;
        string category;
        uint8 coverageTier;
        address owner;
        bool active;
        uint256 registeredAt;
    }

    mapping(bytes32 => Service) public services;
    mapping(address => bytes32[]) public ownerServices;

    address public admin;
    address public pendingAdmin;
    uint256 public serviceCount;

    event ServiceRegistered(bytes32 indexed serviceId, string name, address indexed owner);
    event ServiceUpdated(bytes32 indexed serviceId, uint8 newTier);
    event ServiceDeactivated(bytes32 indexed serviceId);
    event AdminTransferStarted(address indexed currentAdmin, address indexed pendingAdmin);
    event AdminTransferred(address indexed oldAdmin, address indexed newAdmin);

    modifier onlyAdmin() {
        require(msg.sender == admin, "Only admin");
        _;
    }

    constructor() {
        admin = msg.sender;
    }

    /// @notice Register a new service. The caller becomes the service owner.
    /// @dev serviceId derived from (endpointUrl, caller, block.timestamp, serviceCount)
    ///      using abi.encode (not encodePacked) to eliminate string-concat ambiguity.
    ///      The serviceCount term guarantees uniqueness even on same-block re-registrations.
    function registerService(
        string calldata name,
        string calldata endpointUrl,
        string calldata category
    ) external returns (bytes32 serviceId) {
        require(bytes(name).length > 0, "Empty name");
        require(bytes(endpointUrl).length > 0, "Empty endpoint URL");

        serviceId = keccak256(
            abi.encode(endpointUrl, msg.sender, block.timestamp, serviceCount)
        );
        require(services[serviceId].owner == address(0), "Service ID collision");

        services[serviceId] = Service({
            name: name,
            endpointUrl: endpointUrl,
            category: category,
            coverageTier: 0,
            owner: msg.sender,
            active: true,
            registeredAt: block.timestamp
        });
        ownerServices[msg.sender].push(serviceId);
        serviceCount++;
        emit ServiceRegistered(serviceId, name, msg.sender);
    }

    /// @notice Admin-only: update a service's coverage tier (0..3).
    function updateTier(bytes32 serviceId, uint8 newTier) external onlyAdmin {
        require(services[serviceId].owner != address(0), "Service does not exist");
        require(newTier <= 3, "Invalid tier");
        services[serviceId].coverageTier = newTier;
        emit ServiceUpdated(serviceId, newTier);
    }

    /// @notice Service owner or admin: mark a service as inactive.
    function deactivateService(bytes32 serviceId) external {
        Service storage s = services[serviceId];
        require(s.owner != address(0), "Service does not exist");
        require(msg.sender == s.owner || msg.sender == admin, "Not authorized");
        s.active = false;
        emit ServiceDeactivated(serviceId);
    }

    function getService(bytes32 serviceId) external view returns (Service memory) {
        return services[serviceId];
    }

    function getOwnerServices(address owner) external view returns (bytes32[] memory) {
        return ownerServices[owner];
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
