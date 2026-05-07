// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Initializable} from "@openzeppelin/contracts-upgradeable/proxy/utils/Initializable.sol";
import {UUPSUpgradeable} from "@openzeppelin/contracts-upgradeable/proxy/utils/UUPSUpgradeable.sol";
import {OwnableUpgradeable} from "@openzeppelin/contracts-upgradeable/access/OwnableUpgradeable.sol";
import {ReentrancyGuardUpgradeable} from "@openzeppelin/contracts-upgradeable/utils/ReentrancyGuardUpgradeable.sol";

/**
 * @title ZeniAccessControl
 * @author Zeni Cloud Core Team
 * @notice On-chain governance for Zeni Cloud admin access to customer data.
 * @dev Enforces two access pathways:
 *      1) Customer Support — explicit customer wallet approval, time-boxed (6h..24h).
 *      2) Legal Authority — 3-of-5 multi-signature legal team + court order hash.
 *
 *      All actions emit events publicly auditable on Polygon (polygonscan).
 *      Upgradeable via UUPS proxy. Chairman role manages admin and legal signer sets.
 */
contract ZeniAccessControl is
    Initializable,
    UUPSUpgradeable,
    OwnableUpgradeable,
    ReentrancyGuardUpgradeable
{
    // ============================================================
    //                      Constants
    // ============================================================

    /// @notice Minimum approval window (6 hours).
    uint256 public constant MIN_DURATION = 6 hours;

    /// @notice Maximum approval window (24 hours).
    uint256 public constant MAX_DURATION = 24 hours;

    /// @notice Number of legal signatures required for emergency access.
    uint256 public constant REQUIRED_LEGAL_SIGS = 3;

    // ============================================================
    //                       Enums
    // ============================================================

    /// @notice Reason an admin requests access to customer data.
    enum Reason {
        CustomerSupport, // 0
        LegalAuthority   // 1
    }

    /// @notice Lifecycle state of an access request.
    enum Status {
        Pending,  // 0
        Approved, // 1
        Revoked,  // 2
        Expired   // 3
    }

    // ============================================================
    //                       Structs
    // ============================================================

    /**
     * @notice Access request record stored on-chain.
     * @param admin           Admin wallet that filed the request.
     * @param customer        Customer wallet that owns the data.
     * @param scope           keccak256 hash of the workspace_id / data scope.
     * @param reason          CustomerSupport (0) or LegalAuthority (1).
     * @param detail          Free-form ticket reference or court order reference.
     * @param duration        Requested duration in seconds (6h..24h).
     * @param requestedAt     Timestamp request was filed.
     * @param approvedAt      Timestamp of approval (0 if not approved).
     * @param expiresAt       Timestamp at which approval auto-expires.
     * @param courtOrderHash  Hash of court order document (LegalAuthority only).
     * @param status          Current status (Pending / Approved / Revoked / Expired).
     */
    struct Request {
        address admin;
        address customer;
        bytes32 scope;
        Reason reason;
        string detail;
        uint256 duration;
        uint256 requestedAt;
        uint256 approvedAt;
        uint256 expiresAt;
        bytes32 courtOrderHash;
        Status status;
    }

    // ============================================================
    //                       Storage
    // ============================================================

    /// @notice Master role; can manage admin + legal signer sets, force-revoke any request.
    address public chairman;

    /// @notice Whitelisted Zeni admin wallets allowed to file access requests.
    mapping(address => bool) public adminWhitelist;

    /// @notice Legal authority wallets (5 total expected) authorized to sign emergencies.
    mapping(address => bool) public legalMultisig;

    /// @notice Monotonically increasing counter for request IDs (1-indexed).
    uint256 public requestCounter;

    /// @notice Lookup table: requestId => Request.
    mapping(uint256 => Request) public requests;

    /// @dev Tracks legal signers per request to prevent double-signing.
    mapping(uint256 => mapping(address => bool)) private _hasSigned;

    /// @dev Ordered list of signers per request for getEmergencySignatures() view.
    mapping(uint256 => address[]) private _emergencySigners;

    /// @dev Reserved storage gap for upgradeability (50 slots).
    uint256[50] private __gap;

    // ============================================================
    //                       Events
    // ============================================================

    event AccessRequested(
        uint256 indexed requestId,
        address indexed admin,
        address indexed customer,
        bytes32 scope,
        Reason reason,
        uint256 duration,
        string detail
    );

    event AccessApproved(
        uint256 indexed requestId,
        address indexed approver,
        uint256 approvedAt,
        uint256 expiresAt,
        Reason reason
    );

    event AccessRevoked(
        uint256 indexed requestId,
        address indexed revoker,
        uint256 revokedAt,
        string note
    );

    event EmergencyTriggered(
        uint256 indexed requestId,
        address indexed signer,
        uint256 sigCount,
        bytes32 courtOrderHash
    );

    event AdminAdded(address indexed admin, address indexed by);
    event AdminRemoved(address indexed admin, address indexed by);
    event LegalSignerAdded(address indexed signer, address indexed by);
    event LegalSignerRemoved(address indexed signer, address indexed by);
    event ChairmanTransferred(address indexed previousChairman, address indexed newChairman);

    // ============================================================
    //                    Custom Errors
    // ============================================================

    error NotAdmin();
    error NotLegal();
    error NotCustomer();
    error NotChairman();
    error InvalidStatus();
    error InvalidDuration();
    error AlreadySigned();
    error RequestNotFound();
    error ZeroAddress();
    error InvalidReason();

    // ============================================================
    //                    Modifiers
    // ============================================================

    modifier onlyAdmin() {
        if (!adminWhitelist[msg.sender]) revert NotAdmin();
        _;
    }

    modifier onlyLegal() {
        if (!legalMultisig[msg.sender]) revert NotLegal();
        _;
    }

    modifier onlyChairman() {
        if (msg.sender != chairman) revert NotChairman();
        _;
    }

    modifier requestExists(uint256 requestId) {
        if (requestId == 0 || requestId > requestCounter) revert RequestNotFound();
        _;
    }

    // ============================================================
    //                    Initializer
    // ============================================================

    /// @custom:oz-upgrades-unsafe-allow constructor
    constructor() {
        _disableInitializers();
    }

    /**
     * @notice Initializes the proxy with an initial chairman.
     * @dev Replaces constructor for UUPS pattern. Can only be called once.
     * @param _chairman Address that will hold chairman privileges.
     */
    function initialize(address _chairman) external initializer {
        if (_chairman == address(0)) revert ZeroAddress();

        __Ownable_init(_chairman);
        __UUPSUpgradeable_init();
        __ReentrancyGuard_init();

        chairman = _chairman;
        emit ChairmanTransferred(address(0), _chairman);
    }

    // ============================================================
    //                  Access Request Flow
    // ============================================================

    /**
     * @notice Admin files a new access request against a customer.
     * @dev Emits {AccessRequested}. Status begins as Pending.
     * @param customer  Wallet of the customer whose data is being accessed.
     * @param scope     keccak256 hash of workspace_id / scope identifier.
     * @param ticketRef Free-form ticket reference for off-chain lookup.
     * @param duration  Requested approval duration in seconds (6h..24h).
     * @return requestId Newly minted request identifier.
     */
    function requestAccess(
        address customer,
        bytes32 scope,
        string calldata ticketRef,
        uint256 duration
    ) external onlyAdmin nonReentrant returns (uint256 requestId) {
        if (customer == address(0)) revert ZeroAddress();
        if (duration < MIN_DURATION || duration > MAX_DURATION) revert InvalidDuration();

        unchecked {
            requestId = ++requestCounter;
        }

        requests[requestId] = Request({
            admin: msg.sender,
            customer: customer,
            scope: scope,
            reason: Reason.CustomerSupport,
            detail: ticketRef,
            duration: duration,
            requestedAt: block.timestamp,
            approvedAt: 0,
            expiresAt: 0,
            courtOrderHash: bytes32(0),
            status: Status.Pending
        });

        emit AccessRequested(
            requestId,
            msg.sender,
            customer,
            scope,
            Reason.CustomerSupport,
            duration,
            ticketRef
        );
    }

    /**
     * @notice Customer approves a pending request, starting the access window.
     * @dev Only callable by the customer named in the request. Sets approvedAt + expiresAt.
     * @param requestId ID of the request to approve.
     */
    function approveByCustomer(
        uint256 requestId
    ) external requestExists(requestId) nonReentrant {
        Request storage r = requests[requestId];

        if (msg.sender != r.customer) revert NotCustomer();
        if (r.status != Status.Pending) revert InvalidStatus();
        if (r.reason != Reason.CustomerSupport) revert InvalidReason();

        r.status = Status.Approved;
        r.approvedAt = block.timestamp;
        r.expiresAt = block.timestamp + r.duration;

        emit AccessApproved(
            requestId,
            msg.sender,
            r.approvedAt,
            r.expiresAt,
            r.reason
        );
    }

    /**
     * @notice Legal signer adds their signature toward emergency access.
     * @dev When the count reaches REQUIRED_LEGAL_SIGS (3), status flips to Approved
     *      and the courtOrderHash provided by the FIRST signer is locked in.
     *      Subsequent signers must provide the same courtOrderHash.
     * @param requestId       ID of the request to emergency-approve.
     * @param courtOrderHash  Hash of the court order authorizing access.
     */
    function emergencyApprove(
        uint256 requestId,
        bytes32 courtOrderHash
    ) external onlyLegal requestExists(requestId) nonReentrant {
        Request storage r = requests[requestId];

        if (r.status != Status.Pending) revert InvalidStatus();
        if (_hasSigned[requestId][msg.sender]) revert AlreadySigned();
        if (courtOrderHash == bytes32(0)) revert InvalidReason();

        // Lock court order hash on first signature; require match thereafter.
        if (_emergencySigners[requestId].length == 0) {
            r.courtOrderHash = courtOrderHash;
            r.reason = Reason.LegalAuthority;
        } else if (r.courtOrderHash != courtOrderHash) {
            revert InvalidReason();
        }

        _hasSigned[requestId][msg.sender] = true;
        _emergencySigners[requestId].push(msg.sender);

        uint256 sigCount = _emergencySigners[requestId].length;

        emit EmergencyTriggered(requestId, msg.sender, sigCount, courtOrderHash);

        if (sigCount >= REQUIRED_LEGAL_SIGS) {
            // If duration was never set (request was opened by admin under CustomerSupport
            // and now upgraded to LegalAuthority via emergency), default to MAX_DURATION.
            uint256 dur = r.duration;
            if (dur < MIN_DURATION || dur > MAX_DURATION) {
                dur = MAX_DURATION;
                r.duration = dur;
            }

            r.status = Status.Approved;
            r.approvedAt = block.timestamp;
            r.expiresAt = block.timestamp + dur;

            emit AccessApproved(
                requestId,
                msg.sender,
                r.approvedAt,
                r.expiresAt,
                Reason.LegalAuthority
            );
        }
    }

    /**
     * @notice Revoke an active or pending request.
     * @dev Allowed callers: customer (data owner), admin who filed it, or chairman.
     *      Idempotent guard: cannot revoke an already-Revoked or Expired request.
     * @param requestId ID of the request to revoke.
     */
    function revokeAccess(
        uint256 requestId
    ) external requestExists(requestId) nonReentrant {
        Request storage r = requests[requestId];

        if (r.status == Status.Revoked || r.status == Status.Expired) {
            revert InvalidStatus();
        }

        bool authorized = (msg.sender == r.customer) ||
            (msg.sender == r.admin) ||
            (msg.sender == chairman);
        if (!authorized) revert NotCustomer();

        r.status = Status.Revoked;
        r.expiresAt = block.timestamp;

        emit AccessRevoked(requestId, msg.sender, block.timestamp, "");
    }

    // ============================================================
    //                  Chairman Administration
    // ============================================================

    /**
     * @notice Add an admin wallet to the whitelist.
     * @param admin Address to grant admin role.
     */
    function addAdmin(address admin) external onlyChairman {
        if (admin == address(0)) revert ZeroAddress();
        adminWhitelist[admin] = true;
        emit AdminAdded(admin, msg.sender);
    }

    /**
     * @notice Remove an admin wallet from the whitelist.
     * @param admin Address to revoke admin role.
     */
    function removeAdmin(address admin) external onlyChairman {
        adminWhitelist[admin] = false;
        emit AdminRemoved(admin, msg.sender);
    }

    /**
     * @notice Add a legal authority signer.
     * @param signer Address to grant legal multisig role.
     */
    function addLegalSigner(address signer) external onlyChairman {
        if (signer == address(0)) revert ZeroAddress();
        legalMultisig[signer] = true;
        emit LegalSignerAdded(signer, msg.sender);
    }

    /**
     * @notice Remove a legal authority signer.
     * @param signer Address to revoke legal multisig role.
     */
    function removeLegalSigner(address signer) external onlyChairman {
        legalMultisig[signer] = false;
        emit LegalSignerRemoved(signer, msg.sender);
    }

    /**
     * @notice Transfer the chairman role to a new wallet.
     * @dev Also transfers OZ Ownable owner so future upgrades require new chairman.
     * @param newChairman Address to receive the chairman role.
     */
    function transferChairman(address newChairman) external onlyChairman {
        if (newChairman == address(0)) revert ZeroAddress();
        address previous = chairman;
        chairman = newChairman;
        _transferOwnership(newChairman);
        emit ChairmanTransferred(previous, newChairman);
    }

    // ============================================================
    //                       Views
    // ============================================================

    /**
     * @notice Returns true if a request is currently granting active access.
     * @param requestId ID of the request to check.
     */
    function isAccessActive(
        uint256 requestId
    ) external view returns (bool) {
        if (requestId == 0 || requestId > requestCounter) return false;
        Request storage r = requests[requestId];
        return r.status == Status.Approved && block.timestamp < r.expiresAt;
    }

    /**
     * @notice Read the full request record by ID.
     * @param requestId ID of the request.
     * @return The Request struct.
     */
    function getRequest(
        uint256 requestId
    ) external view requestExists(requestId) returns (Request memory) {
        return requests[requestId];
    }

    /**
     * @notice Returns all legal signers that have signed on a given request.
     * @param requestId ID of the request.
     */
    function getEmergencySignatures(
        uint256 requestId
    ) external view requestExists(requestId) returns (address[] memory) {
        return _emergencySigners[requestId];
    }

    /**
     * @notice Returns the current count of accumulated emergency signatures.
     * @param requestId ID of the request.
     */
    function emergencySigCount(
        uint256 requestId
    ) external view requestExists(requestId) returns (uint256) {
        return _emergencySigners[requestId].length;
    }

    // ============================================================
    //                  UUPS Upgrade Authorization
    // ============================================================

    /**
     * @dev Restricts upgrades to the chairman role.
     */
    function _authorizeUpgrade(address) internal view override onlyChairman {}
}
