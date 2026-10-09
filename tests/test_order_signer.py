from dataclasses import asdict

from eth_account import Account
from eth_account.messages import encode_typed_data

from agentpit.onchain.deployment import Deployment
from agentpit.onchain.order_signer import _TYPES, OrderData, _domain, sign_order


def test_a_signed_order_recovers_to_its_signer():
    account = Account.create()
    deployment = Deployment.model_construct(chain_id=31337, exchange=Account.create().address)
    order = OrderData(
        salt=1,
        maker=account.address,
        signer=account.address,
        taker="0x" + "00" * 20,
        tokenId=7,
        makerAmount=500_000,
        takerAmount=1_000_000,
        expiration=0,
        nonce=0,
        feeRateBps=0,
        side=0,
        signatureType=0,
    )
    typed = {"types": _TYPES, "primaryType": "Order", "domain": _domain(deployment), "message": asdict(order)}
    signature = sign_order(account, deployment, order)
    assert Account.recover_message(encode_typed_data(full_message=typed), signature=signature) == account.address
