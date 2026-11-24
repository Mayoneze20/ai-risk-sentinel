// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface IERC20Minimal {
    function transfer(address to, uint256 amount) external returns (bool);
    function balanceOf(address account) external view returns (uint256);
}

interface IWETHMinimal is IERC20Minimal {
    function deposit() external payable;
    function withdraw(uint256 amount) external;
}

interface IUniswapV3PoolMinimal {
    function swap(
        address recipient,
        bool zeroForOne,
        int256 amountSpecified,
        uint160 sqrtPriceLimitX96,
        bytes calldata data
    ) external returns (int256 amount0, int256 amount1);
}

/// @title DemoTrader — Phase 2 demo driver helper (Base Sepolia testnet only).
/// @notice Holds WETH + USDC and swaps directly against the watched Uniswap V3
///         pool, so the pool's Swap event names THIS contract as `sender`
///         (topic_1). Deploying several instances gives the model service
///         distinct swap senders to reason about — the SwapRouter would make
///         every swap share one sender (the router) and destroy the
///         concentration / roundtrip features.
contract DemoTrader {
    address public owner;
    address public immutable pool;
    address public immutable token0; // USDC on the watched pool
    address public immutable token1; // WETH on the watched pool

    uint160 private constant MIN_SQRT_RATIO_PLUS_1 = 4295128740;
    uint160 private constant MAX_SQRT_RATIO_MINUS_1 =
        1461446703485210103287273052203988822378723970341;

    modifier onlyOwner() {
        require(msg.sender == owner, "onlyOwner");
        _;
    }

    constructor(address _pool, address _token0, address _token1) {
        owner = msg.sender;
        pool = _pool;
        token0 = _token0;
        token1 = _token1;
    }

    receive() external payable {}

    function wrap(uint256 amount) external onlyOwner {
        require(address(this).balance >= amount, "insufficient ETH");
        IWETHMinimal(token1).deposit{value: amount}();
    }

    function unwrap(uint256 amount) external onlyOwner {
        IWETHMinimal(token1).withdraw(amount);
    }

    /// @notice Exact-input swap against the pool. Output lands in this contract.
    function swap(bool zeroForOne, uint256 amountIn)
        external
        onlyOwner
        returns (int256 amount0, int256 amount1)
    {
        uint160 limit = zeroForOne
            ? MIN_SQRT_RATIO_PLUS_1
            : MAX_SQRT_RATIO_MINUS_1;
        (amount0, amount1) = IUniswapV3PoolMinimal(pool).swap(
            address(this),
            zeroForOne,
            int256(amountIn),
            limit,
            abi.encode(zeroForOne)
        );
    }

    /// @notice Uniswap V3 swap callback: pay whichever input delta is positive.
    function uniswapV3SwapCallback(
        int256 amount0Delta,
        int256 amount1Delta,
        bytes calldata /* data */
    ) external {
        require(msg.sender == pool, "only pool");
        if (amount0Delta > 0) {
            require(
                IERC20Minimal(token0).transfer(pool, uint256(amount0Delta)),
                "pay token0 failed"
            );
        }
        if (amount1Delta > 0) {
            require(
                IERC20Minimal(token1).transfer(pool, uint256(amount1Delta)),
                "pay token1 failed"
            );
        }
    }

    function transferToken(address token, address to, uint256 amount)
        external
        onlyOwner
    {
        require(IERC20Minimal(token).transfer(to, amount), "transfer failed");
    }

    function sweepETH(address to) external onlyOwner {
        (bool ok, ) = payable(to).call{value: address(this).balance}("");
        require(ok, "sweep failed");
    }
}
