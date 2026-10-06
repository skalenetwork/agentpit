from functools import lru_cache

import requests
from eth_account import Account
from eth_account.signers.local import LocalAccount
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from web3.providers.rpc.utils import (
    REQUEST_RETRY_ALLOWLIST,
    ExceptionRetryConfiguration,
)
from web3.types import RPCEndpoint

from agentpit.config import Settings
from agentpit.onchain.deployment import Deployment
from agentpit.onchain.chain_rpc import Web3ChainRpc
from agentpit.onchain.tx_sender import AdminTxSender


def build_http_provider(rpc_url: str) -> Web3.HTTPProvider:
    """web3's HTTP provider with two changes that matter against SKALE.

    `eth_chainId` is cached: web3's validation middleware asks for it twice
    around every eth_call and eth_estimateGas, which tripled the cost of each
    chain read (one SKALE round trip is ~0.2 s).

    `eth_sendRawTransaction` is never retried. After a timeout web3 would send
    the same transaction again, and the node's answer to that second copy
    ("already exists", "invalid nonce") would be read as a refusal of the
    first. `AdminTxSender` resolves a lost answer itself: it resends the
    identical bytes once and, if the node says the nonce is already used, looks
    for its own receipt. Reads keep their retries. User-key sends
    (`send_user_tx`) lose the automatic resend too, deliberately: a resend
    after a lost answer is how one action runs twice.

    An explicit request_cache_validation_threshold avoids web3's unlocked
    first-use probe that can switch the cache off permanently under concurrent
    first calls.
    """
    return Web3.HTTPProvider(
        rpc_url,
        cache_allowed_requests=True,
        cacheable_requests={RPCEndpoint("eth_chainId")},
        request_cache_validation_threshold=3600,
        exception_retry_configuration=ExceptionRetryConfiguration(
            errors=(ConnectionError, requests.HTTPError, requests.Timeout),
            retries=5,
            backoff_factor=0.125,
            method_allowlist=[
                m for m in REQUEST_RETRY_ALLOWLIST if m != "eth_sendRawTransaction"
            ],
        ),
    )


class Web3Client:
    """Holds the web3 client, the admin/operator account and its sender.

    Every admin-key transaction goes through `admin_sender`, which counts the
    nonce locally and keeps many transactions in flight, so concurrent admin
    sends (sync, settlement, faucet, gas grants) share blocks. One instance
    per process: two senders for one key would race on the nonce.
    """

    def __init__(self, settings: Settings, deployment: Deployment):
        rpc_url = settings.rpc_url_override or deployment.rpc_url
        self.web3 = Web3(build_http_provider(rpc_url))
        # Polygon (the forked chain) is Proof-of-Authority: its block headers
        # carry >32-byte extraData. Without this middleware web3 raises
        # ExtraDataLengthError on any real Polygon block — e.g. the anvil
        # fork-base block, which is `latest` right after a fork/restart until a
        # local tx mines an anvil block. That breaks polymarket_sync and every
        # on-chain op until the chain happens to advance.
        self.web3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        self.deployment = deployment

        if not settings.operator_private_key:
            raise RuntimeError(
                "PK env var is unset — agentpit requires an admin/operator key"
            )
        self.admin: LocalAccount = Account.from_key(settings.operator_private_key)

        if self.admin.address.lower() != deployment.admin.lower():
            raise RuntimeError(
                f"PK address {self.admin.address} does not match deployment.admin "
                f"{deployment.admin} from {settings.deployment_path}"
            )
        self.admin_sender = AdminTxSender(
            Web3ChainRpc(self.web3),
            self.admin,
            deployment.chain_id,
            max_in_flight=settings.admin_tx_max_in_flight,
        )

    def verify_chain(self) -> None:
        chain_id = self.web3.eth.chain_id
        if chain_id != self.deployment.chain_id:
            raise RuntimeError(
                f"Connected chain_id {chain_id} != deployment chain_id "
                f"{self.deployment.chain_id}"
            )


@lru_cache(maxsize=1)
def _shared_client(settings: Settings, deployment: Deployment) -> Web3Client:
    return Web3Client(settings, deployment)


def get_web3_client(settings: Settings, deployment: Deployment) -> Web3Client:
    return _shared_client(settings, deployment)
