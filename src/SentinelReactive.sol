// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @notice Minimal interface for the Reactive (Omni) system contract on Lasna.
///         System contract address: 0x8888888888888888888888888888888888888888.
interface ISystemContractOmni {
    struct CallbackConfiguration_V_1_0 {
        uint256 chainId;
        address recipient;
        uint64 gasLimit;
        bytes payload;
    }

    function subscribe(
        uint256 chain_id,
        address _contract,
        uint256 topic_0,
        uint256 topic_1,
        uint256 topic_2,
        uint256 topic_3
    ) external;

    function requestCallbackV_1_0(CallbackConfiguration_V_1_0 memory config) external;
}

/// @notice Log record delivered by the system contract to reactive contracts.
interface IReactiveOmni {
    struct LogRecord {
        uint256 chain_id;
        address _contract;
        uint256 topic_0;
        uint256 topic_1;
        uint256 topic_2;
        uint256 topic_3;
        bytes data;
        uint256 block_number;
        uint256 op_code;
        uint256 block_hash;
        uint256 tx_hash;
        uint256 log_index;
    }

    function react(LogRecord calldata log) external;
}

/// @title SentinelReactive — reactive contract deployed on Lasna (Omni pattern).
/// @notice Subscribes in the constructor to Uniswap V3 Swap events from a real
///         pool on Base Sepolia. Every swap is forwarded via callback to the
///         AIOracle contract on Base Sepolia, where the off-chain model service
///         picks it up, scores it, and posts a signed decision.
contract SentinelReactive is IReactiveOmni {
    ISystemContractOmni internal constant SYSTEM =
        ISystemContractOmni(0x8888888888888888888888888888888888888888);

    uint256 internal constant REACTIVE_IGNORE =
        0xa65f96fc951c35ead38878e0f0b7a3c744a6f5ccc1476b313353ce31712313ad;

    /// @notice topic0 of UniswapV3Pool.Swap(address,address,int256,int256,uint160,uint128,int24)
    uint256 internal constant SWAP_TOPIC0 =
        0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67;

    uint64 private constant CALLBACK_GAS_LIMIT = 1000000;

    uint256 public immutable destinationChainId;
    address public immutable oracle;

    uint256 public observations;
    int24 public lastTick;
    uint256 public lastOriginTxHash;

    /// @param originChainId  Chain the watched pool lives on (Base Sepolia = 84532).
    /// @param pool           The Uniswap V3 pool contract to watch.
    /// @param _destinationChainId  Chain the AIOracle lives on (Base Sepolia = 84532).
    /// @param _oracle        AIOracle contract address on the destination chain.
    constructor(
        uint256 originChainId,
        address pool,
        uint256 _destinationChainId,
        address _oracle
    ) payable {
        destinationChainId = _destinationChainId;
        oracle = _oracle;
        SYSTEM.subscribe(
            originChainId,
            pool,
            SWAP_TOPIC0,
            REACTIVE_IGNORE,
            REACTIVE_IGNORE,
            REACTIVE_IGNORE
        );
    }

    modifier onlySystem() {
        require(msg.sender == address(SYSTEM), "onlySystem");
        _;
    }

    receive() external payable {}

    function react(LogRecord calldata log) external override onlySystem {
        // Uniswap V3 Swap data: (int256 amount0, int256 amount1,
        // uint160 sqrtPriceX96, uint128 liquidity, int24 tick).
        (int256 amount0, int256 amount1, uint160 sqrtPriceX96, , int24 tick) =
            abi.decode(log.data, (int256, int256, uint160, uint128, int24));
        // topic_1 is the indexed swap sender, right-aligned in 256 bits.
        address sender = address(uint160(log.topic_1));

        observations++;
        lastTick = tick;
        lastOriginTxHash = log.tx_hash;

        // The system contract replaces the first 160 bits of the payload with
        // this reactive contract's address, so the first argument must be an
        // address placeholder (address(0)); the oracle sees our address there
        // and checks it against its allowlist.
        bytes memory payload = abi.encodeWithSignature(
            "recordObservation(address,address,address,int256,int256,uint160,int24,uint256)",
            address(0),
            log._contract,
            sender,
            amount0,
            amount1,
            sqrtPriceX96,
            tick,
            log.tx_hash
        );
        SYSTEM.requestCallbackV_1_0(
            ISystemContractOmni.CallbackConfiguration_V_1_0({
                chainId: destinationChainId,
                recipient: oracle,
                gasLimit: CALLBACK_GAS_LIMIT,
                payload: payload
            })
        );
    }
}
