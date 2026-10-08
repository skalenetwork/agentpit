"""High-level on-chain operations executed with the admin/operator key."""

from eth_account.signers.local import LocalAccount
from web3 import Web3
from web3.contract.contract import ContractFunction
from web3.types import TxReceipt

from agentpit.onchain.chain_rpc import current_fee_params
from agentpit.onchain.contracts import Contracts
from agentpit.onchain.tx_sender import PendingTx
from agentpit.onchain.user_wallet import (
    fund_user_with_native,
    send_admin_tx,
    send_user_tx,
)
from agentpit.onchain.web3_client import Web3Client

# How many token ids go into one balanceOfBatch. Large enough that an ordinary
# account is a single call, small enough that a heavy one does not ask a public
# node for a reply it may cap.
_BALANCE_BATCH = 200

_MAX_UINT256 = 2**256 - 1

# parentCollectionId of a top-level position. agentpit never nests positions,
# so every split, merge and redeem names this one.
_ZERO_BYTES32 = b"\x00" * 32

# Static gas limits for the sync's batched sends, about twice the gas measured
# on SKALE (57,226 and 109,909). An estimate would cost a round trip per
# market (a batch is signed in one go, so it has none), and a registerToken
# sent behind its own prepareCondition must not depend on state that is not
# mined yet. Over-provisioning costs nothing: the unused gas is refunded.
PREPARE_CONDITION_GAS = 120_000
REGISTER_TOKEN_GAS = 220_000

# Three eth_calls per market in one JSON-RPC batch: 40 markets = 120 requests,
# under SKALE's cap of 128.
_STATE_BATCH = 40


class OnchainAdmin:
    def __init__(self, client: Web3Client, contracts: Contracts):
        self._client = client
        self._contracts = contracts

    # --- onboarding -------------------------------------------------

    def faucet_drip(self, recipient: str, *, timeout: int = 30) -> TxReceipt:
        fn = self._contracts.faucet.functions.drip(Web3.to_checksum_address(recipient))
        return send_admin_tx(self._client, fn, timeout=timeout)

    def mint_to(
        self, recipient: str, amount_raw: int, *, timeout: int = 30
    ) -> TxReceipt:
        """Mint an arbitrary amount of apUSD — house funding and top-ups.

        `faucet_drip` mints the fixed signup grant; this is the same faucet's
        unrestricted entry point, and like drip it is operator-only on chain.
        """
        fn = self._contracts.faucet.functions.mintTo(
            Web3.to_checksum_address(recipient), amount_raw
        )
        return send_admin_tx(self._client, fn, timeout=timeout)

    def fund_gas(
        self, user_address: str, value_wei: int, *, timeout: int = 30
    ) -> TxReceipt:
        return fund_user_with_native(
            self._client, user_address, value_wei, timeout=timeout
        )

    def grant_user_approvals(
        self, user_account: LocalAccount, *, timeout: int = 30
    ) -> tuple[TxReceipt, TxReceipt, TxReceipt]:
        """Send `approval_calls` from `user_account`, which pays its own gas.

        House accounts only: the house signs its own approvals from the
        balance its floor/target loop keeps funded. A user or agent wallet
        holds no gas of its own, so it onboards through `UserGasSponsor`,
        which tops it up to exactly what `approval_calls` need and sends them
        with `send_as_user`.
        """
        rcpt_a, rcpt_b, rcpt_c = [
            send_user_tx(self._client, user_account, fn, timeout=timeout)
            for fn in self.approval_calls()
        ]
        return rcpt_a, rcpt_b, rcpt_c

    # --- user-signed calls ------------------------------------------
    #
    # Built here, sized, funded and sent by `UserGasSponsor`: it estimates
    # each call with `estimate_user_gas`, prices it at `gas_price`, tops the
    # wallet up to exactly that, then sends with `send_as_user`. A builder
    # only builds; nothing reaches the chain until the sponsor sends.

    def approval_calls(self) -> list[ContractFunction]:
        """The three one-time approvals every trading account needs, in order.

        1. usd.approve(exchange, max)         — collateral movement during fills
        2. usd.approve(ctf, max)              — splitPosition during MINT matches
        3. ctf.setApprovalForAll(exchange,1)  — outcome-token movement
        """
        usd = self._contracts.usd
        ctf = self._contracts.ctf
        exch = self._contracts.exchange.address
        return [
            usd.functions.approve(exch, _MAX_UINT256),
            usd.functions.approve(ctf.address, _MAX_UINT256),
            ctf.functions.setApprovalForAll(exch, True),
        ]

    def split_call(
        self, condition_id: bytes, partition: list[int], amount: int
    ) -> ContractFunction:
        """splitPosition: lock `amount` apUSD, receive `amount` of every
        outcome in `partition`. Needs `usd.approve(ctf)` from onboarding."""
        return self._contracts.ctf.functions.splitPosition(
            self._contracts.usd.address, _ZERO_BYTES32, condition_id, partition, amount
        )

    def merge_call(
        self, condition_id: bytes, partition: list[int], amount: int
    ) -> ContractFunction:
        """mergePositions: burn `amount` of every outcome in `partition`,
        receive `amount` apUSD back."""
        return self._contracts.ctf.functions.mergePositions(
            self._contracts.usd.address, _ZERO_BYTES32, condition_id, partition, amount
        )

    def redeem_call(
        self, condition_id: bytes, partition: list[int]
    ) -> ContractFunction:
        """redeemPositions over every index set in `partition`.

        Pass the whole partition, losers included, so losing tokens burn in
        the same transaction instead of lingering as worthless balances. The
        CTF pays `msg.sender` only, and it succeeds with zero holdings: the
        chain never refuses a worthless claim, so a sponsored one is gated on
        `payout_vector` and `ctf_balances` before it is sent.
        """
        return self._contracts.ctf.functions.redeemPositions(
            self._contracts.usd.address, _ZERO_BYTES32, condition_id, partition
        )

    def gas_price(self) -> int:
        """The node's current `eth_gasPrice`: the `maxFeePerGas` every user
        transaction carries (`current_fee_params`), so the price a top-up is
        sized at."""
        return current_fee_params(self._client.web3)[0]

    def estimate_user_gas(self, fn: ContractFunction, address: str) -> int:
        """Gas `fn` needs when `address` sends it, on committed state.

        No fee fields, on purpose. A sponsored wallet is usually empty when
        it is sized, and anvil answers "gas required exceeds allowance: 0" to
        an estimate that carries a `gasPrice` or `maxFeePerGas` from a
        zero-balance sender. Without them, anvil and skaled both estimate an
        empty wallet's call.
        """
        return fn.estimate_gas({"from": Web3.to_checksum_address(address)})

    def transaction_count(self, address: str) -> int:
        """How many transactions `address` has had mined: its "latest" nonce.

        Incoming transfers do not count, so a wallet that has only ever been
        topped up reads 0. That separates "this chain never saw the account's
        approvals" (a wiped anvil) from "the account spent its gas down to
        near zero", which with exact top-ups is every healthy wallet.
        """
        return self._client.web3.eth.get_transaction_count(
            Web3.to_checksum_address(address), "latest"
        )

    def send_as_user(
        self,
        user_account: LocalAccount,
        fn: ContractFunction,
        *,
        gas: int,
        max_fee: int,
        timeout: int = 30,
    ) -> TxReceipt:
        """Send `fn` from `user_account` with exactly the gas limit and price
        its top-up paid for; wait for the receipt.

        Neither is worked out again: another estimate is one more ~0.5 s
        round trip on SKALE, and a higher limit or price than the top-up
        covered is refused at import, because skaled checks
        `gasLimit × maxFeePerGas` against the balance. Returns the receipt
        whatever its status: with the limit given nothing is estimated, so a
        call that reverts is mined and paid for instead of refused up front.
        """
        return send_user_tx(
            self._client, user_account, fn, timeout=timeout, gas=gas, max_fee=max_fee
        )

    # --- markets ----------------------------------------------------

    def prepare_condition_call(
        self, oracle: str, question_id: bytes, outcome_slot_count: int
    ) -> tuple[ContractFunction, int]:
        """`prepareCondition` with its static gas limit, for `submit_many`."""
        fn = self._contracts.ctf.functions.prepareCondition(
            Web3.to_checksum_address(oracle), question_id, outcome_slot_count
        )
        return fn, PREPARE_CONDITION_GAS

    def register_token_call(
        self, token_a: int, token_b: int, condition_id: bytes
    ) -> tuple[ContractFunction, int]:
        """`registerToken` with its static gas limit, for `submit_many`."""
        fn = self._contracts.exchange.functions.registerToken(
            token_a, token_b, condition_id
        )
        return fn, REGISTER_TOKEN_GAS

    def submit_many(
        self, calls: list[tuple[ContractFunction, int]]
    ) -> list[PendingTx | Exception]:
        """Broadcast every call in JSON-RPC batches without waiting; one
        result per call, in order (`AdminTxSender.submit_many`).

        Essential: its only callers are the catalogue/pin sync and operator
        market creation (`prepare_markets_on_chain`), which keep running while
        the gas breaker is paused -- a market that cannot be prepared is one
        nobody can trade.
        """
        return self._client.admin_sender.submit_many(calls, essential=True)

    def wait_all(
        self, pendings: list[PendingTx], *, timeout: float
    ) -> list[TxReceipt | Exception]:
        return self._client.admin_sender.wait_all(pendings, timeout=timeout)

    def read_market_states(
        self, markets: list[tuple[bytes, list[int]]]
    ) -> list[tuple[int, int, int]]:
        """(outcome slot count, registry complement of token 0, of token 1)
        for each (condition id, [token 0, token 1]), in JSON-RPC batches."""
        out: list[tuple[int, int, int]] = []
        ctf = self._contracts.ctf.functions
        registry = self._contracts.exchange.functions.registry
        for start in range(0, len(markets), _STATE_BATCH):
            chunk = markets[start : start + _STATE_BATCH]
            with self._client.web3.batch_requests() as batch:
                for condition_id, tokens in chunk:
                    batch.add(ctf.getOutcomeSlotCount(condition_id))
                    batch.add(registry(tokens[0]))
                    batch.add(registry(tokens[1]))
                results = batch.execute()
            for i in range(len(chunk)):
                slots, comp_a, comp_b = results[3 * i : 3 * i + 3]
                out.append((int(slots), int(comp_a[0]), int(comp_b[0])))
        return out

    def prepare_condition(
        self,
        oracle: str,
        question_id: bytes,
        outcome_slot_count: int,
        *,
        timeout: int = 30,
    ) -> TxReceipt:
        fn = self._contracts.ctf.functions.prepareCondition(
            Web3.to_checksum_address(oracle), question_id, outcome_slot_count
        )
        return send_admin_tx(self._client, fn, timeout=timeout, essential=True)

    def register_token(
        self, token_a: int, token_b: int, condition_id: bytes, *, timeout: int = 30
    ) -> TxReceipt:
        fn = self._contracts.exchange.functions.registerToken(
            token_a, token_b, condition_id
        )
        return send_admin_tx(self._client, fn, timeout=timeout, essential=True)

    def report_payouts(
        self, question_id: bytes, payouts: list[int], *, timeout: int = 30
    ) -> TxReceipt:
        """Admin (= local oracle) reports the resolved payout vector.

        For binary YES/NO markets `payouts` is `[1, 0]` if YES won, `[0, 1]`
        if NO won. The CTF rejects re-reporting (custom error) — callers
        wanting idempotency should pre-check `payoutDenominator` or catch.
        """
        fn = self._contracts.ctf.functions.reportPayouts(question_id, payouts)
        return send_admin_tx(self._client, fn, timeout=timeout, essential=True)

    def user_split_position(
        self,
        user_account: LocalAccount,
        condition_id: bytes,
        amount: int,
        *,
        timeout: int = 30,
    ) -> TxReceipt:
        """Self-funded splitPosition: lock `amount` apUSD, get equal YES+NO tokens.

        House accounts only in production (the mirror reconciler's inventory
        splits): the house pays its own gas from the balance its floor/target
        loop keeps funded. A user's split goes through `UserGasSponsor` with
        `split_call`. Tests use it on accounts they fund themselves.
        """
        fn = self.split_call(condition_id, [1, 2], amount)
        return send_user_tx(self._client, user_account, fn, timeout=timeout)

    # --- read-only --------------------------------------------------

    def check_sponsored(self) -> None:
        """Raise `AdminGasPausedError` while the admin wallet is below its stop level."""
        self._client.admin_sender.check_sponsored()

    def refresh_admin_gas(self) -> tuple[int, str]:
        """Re-read the admin balance; (balance in wei, breaker state)."""
        sender = self._client.admin_sender
        balance = sender.refresh_gas_balance()
        return balance, sender.gas_state()

    @property
    def oracle_address(self) -> str:
        """The admin, which is the oracle of every locally prepared condition."""
        return self._client.admin.address

    @property
    def collateral_address(self) -> str:
        return self._contracts.usd.address

    @property
    def sync_chunk_size(self) -> int:
        """Markets per batched sync step: two transactions each, so a chunk uses
        at most half the in-flight capacity and user trades keep the other half
        during a sync burst. With the default 128: 32 markets, up to 64
        transactions, one JSON-RPC batch."""
        return max(1, self._client.admin_sender.max_in_flight // 4)

    @property
    def deployment_id(self) -> str:
        """Identity of the chain deployment these contracts belong to.

        The CTF address: a redeploy always produces a new one, because the
        contracts themselves are new. Callers compare it against what they
        recorded to tell "the chain was replaced" from "nothing happened",
        without an RPC round-trip.
        """
        return self._client.deployment.ctf

    @property
    def chain_id(self) -> int:
        """Id of the chain this deployment lives on, from the deployment file:
        no RPC round-trip, like `deployment_id`."""
        return self._client.deployment.chain_id

    def usd_balance(self, address: str) -> int:
        return self._contracts.usd.functions.balanceOf(
            Web3.to_checksum_address(address)
        ).call()

    def native_balance(self, address: str) -> int:
        return self._client.web3.eth.get_balance(Web3.to_checksum_address(address))

    def ctf_balance(self, address: str, token_id: int) -> int:
        return self._contracts.ctf.functions.balanceOf(
            Web3.to_checksum_address(address), token_id
        ).call()

    def ctf_balances(self, address: str, token_ids: list[int]) -> list[int]:
        """Every token's balance for one holder, in as few calls as possible.

        `ctf_balance` per token is what made the profile page slow: a chain
        read costs ~0.5s on a remote node, and a position scan asks for two
        tokens per market the account has ever touched, so a 500-trade
        account paid over twenty seconds of round trips for a page of
        fourteen rows. ERC-1155 answers the whole list in one call.

        Chunked because the list grows with the account: an eth_call
        returning a thousand words is a different proposition from one
        returning a few dozen, and the node is entitled to refuse it.
        """
        if not token_ids:
            return []
        owner = Web3.to_checksum_address(address)
        out: list[int] = []
        for start in range(0, len(token_ids), _BALANCE_BATCH):
            chunk = token_ids[start : start + _BALANCE_BATCH]
            out.extend(
                self._contracts.ctf.functions.balanceOfBatch(
                    [owner] * len(chunk), chunk
                ).call()
            )
        return out

    def payout_vector(
        self, condition_id: bytes, outcome_count: int = 2
    ) -> tuple[int, list[int]]:
        """(payoutDenominator, [payoutNumerators(cid, i) for every outcome]).

        A claim pays `Σ balance_i × num_i // den`, and that sum is the gate in
        front of every sponsored claim. It is read from the CTF, not taken from
        the database's RESOLVED: a market can be RESOLVED in the database
        before (or without) its `reportPayouts` being mined, and
        redeemPositions reverts until it is ("result for condition not
        received yet").

        `den == 0` means not reported: every numerator is zero, so none is
        read. Otherwise the numerators go in one JSON-RPC batch, one round
        trip however many outcomes, as in `read_market_states`.
        """
        ctf = self._contracts.ctf.functions
        den = int(ctf.payoutDenominator(condition_id).call())
        if den == 0:
            return 0, [0] * outcome_count
        with self._client.web3.batch_requests() as batch:
            for i in range(outcome_count):
                batch.add(ctf.payoutNumerators(condition_id, i))
            nums = batch.execute()
        return den, [int(n) for n in nums]
