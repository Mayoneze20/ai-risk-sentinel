// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title AIOracle — deployed on Base Sepolia.
/// @notice On-chain meeting point of the Reactive loop and the AI model service.
///         1. The SentinelReactive RSC (on Lasna) reports every watched-pool swap
///            here via a Reactive callback (`recordObservation`).
///         2. The off-chain model service scores the swap window and posts a
///            decision (`submitDecision`) carrying an ECDSA signature that is
///            verified on-chain against the configured model signer — a decision
///            only exists if the model actually produced and signed it.
///         3. The GuardianVault consumes each decision exactly once
///            (`markExecuted`, vault-only).
contract AIOracle {
    enum Action {
        Hold,
        Pause,
        Exit
    }

    struct Observation {
        address pool;
        address sender;
        int256 amount0;
        int256 amount1;
        uint160 sqrtPriceX96;
        int24 tick;
        uint256 originTxHash;
        uint256 timestamp;
    }

    struct Decision {
        uint256 observationId;
        Action action;
        uint256 confidence; // 0..100
        bytes32 featuresHash;
        address model;
        uint256 timestamp;
        bool executed;
    }

    address public owner;
    /// @notice Address whose ECDSA signature authorizes decisions (model service key).
    address public modelSigner;
    /// @notice The only contract allowed to mark decisions executed.
    address public vault;
    /// @notice Reactive contracts allowed to report observations (by the address
    ///         the system contract stamps into the callback payload).
    mapping(address => bool) public allowedReactive;

    Observation[] public observations;
    Decision[] public decisions;

    event ObservationRecorded(
        uint256 indexed id,
        address indexed pool,
        address indexed sender,
        int256 amount0,
        int256 amount1,
        int24 tick,
        uint256 originTxHash
    );
    event DecisionSubmitted(
        uint256 indexed id,
        uint256 indexed observationId,
        Action action,
        uint256 confidence,
        bytes32 featuresHash,
        address model
    );
    event DecisionExecuted(uint256 indexed id);

    modifier onlyOwner() {
        require(msg.sender == owner, "onlyOwner");
        _;
    }

    constructor(address _modelSigner) {
        owner = msg.sender;
        modelSigner = _modelSigner;
    }

    // --- Reactive side -------------------------------------------------------

    /// @notice Called via Reactive callback. `reactive` is NOT supplied by the
    ///         caller: the Reactive system contract overwrites the first 160
    ///         bits of the callback payload with the reactive contract's
    ///         address, so this parameter authentically names the reporting RSC.
    function recordObservation(
        address reactive,
        address pool,
        address sender,
        int256 amount0,
        int256 amount1,
        uint160 sqrtPriceX96,
        int24 tick,
        uint256 originTxHash
    ) external {
        require(allowedReactive[reactive], "reactive not allowed");
        uint256 id = observations.length;
        observations.push(
            Observation({
                pool: pool,
                sender: sender,
                amount0: amount0,
                amount1: amount1,
                sqrtPriceX96: sqrtPriceX96,
                tick: tick,
                originTxHash: originTxHash,
                timestamp: block.timestamp
            })
        );
        emit ObservationRecorded(id, pool, sender, amount0, amount1, tick, originTxHash);
    }

    // --- Model service side ----------------------------------------------------

    /// @notice The digest the model service signs (personal_sign / EIP-191).
    function decisionDigest(
        uint256 observationId,
        Action action,
        uint256 confidence,
        bytes32 featuresHash
    ) public view returns (bytes32) {
        return keccak256(
            abi.encodePacked(
                "AI-RISK-SENTINEL-DECISION",
                address(this),
                observationId,
                uint8(action),
                confidence,
                featuresHash
            )
        );
    }

    function submitDecision(
        uint256 observationId,
        Action action,
        uint256 confidence,
        bytes32 featuresHash,
        bytes calldata signature
    ) external returns (uint256 id) {
        require(observationId < observations.length, "no such observation");
        require(confidence <= 100, "confidence > 100");
        bytes32 digest = decisionDigest(observationId, action, confidence, featuresHash);
        address signer = _recoverSigner(digest, signature);
        require(signer == modelSigner, "bad model signature");

        id = decisions.length;
        decisions.push(
            Decision({
                observationId: observationId,
                action: action,
                confidence: confidence,
                featuresHash: featuresHash,
                model: signer,
                timestamp: block.timestamp,
                executed: false
            })
        );
        emit DecisionSubmitted(id, observationId, action, confidence, featuresHash, signer);
    }

    // --- Vault side ------------------------------------------------------------

    function markExecuted(uint256 decisionId) external {
        require(msg.sender == vault, "only vault");
        require(decisionId < decisions.length, "no such decision");
        require(!decisions[decisionId].executed, "already executed");
        decisions[decisionId].executed = true;
        emit DecisionExecuted(decisionId);
    }

    function getDecision(uint256 decisionId) external view returns (Decision memory) {
        require(decisionId < decisions.length, "no such decision");
        return decisions[decisionId];
    }

    function observationCount() external view returns (uint256) {
        return observations.length;
    }

    function decisionCount() external view returns (uint256) {
        return decisions.length;
    }

    // --- Admin -------------------------------------------------------------------

    function setAllowedReactive(address reactive, bool allowed) external onlyOwner {
        allowedReactive[reactive] = allowed;
    }

    function setModelSigner(address _modelSigner) external onlyOwner {
        modelSigner = _modelSigner;
    }

    function setVault(address _vault) external onlyOwner {
        vault = _vault;
    }

    function transferOwnership(address newOwner) external onlyOwner {
        require(newOwner != address(0), "zero address");
        owner = newOwner;
    }

    // --- Internal ------------------------------------------------------------------

    function _recoverSigner(bytes32 digest, bytes calldata signature)
        internal
        pure
        returns (address)
    {
        require(signature.length == 65, "bad signature length");
        bytes32 r;
        bytes32 s;
        uint8 v;
        assembly {
            r := calldataload(signature.offset)
            s := calldataload(add(signature.offset, 32))
            v := byte(0, calldataload(add(signature.offset, 64)))
        }
        if (v < 27) v += 27;
        bytes32 ethDigest = keccak256(
            abi.encodePacked("\x19Ethereum Signed Message:\n32", digest)
        );
        return ecrecover(ethDigest, v, r, s);
    }
}
