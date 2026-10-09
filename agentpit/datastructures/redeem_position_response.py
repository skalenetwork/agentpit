from pydantic import BaseModel


class RedeemPositionResponse(BaseModel):
    """`collateral_amount` is what the claim paid: the `payout` of the CTF's
    `PayoutRedemption` in its receipt, whatever else moved the wallet's apUSD
    meanwhile. `new_usdc_balance` is the post-redeem on-chain apUSD balance."""

    market_id: int
    collateral_amount: int = 0
    new_usdc_balance: int
