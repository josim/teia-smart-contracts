"""Teia FA2.1 token contract (permanent, multi-edition).

The permanent, "dumb-but-self-protecting" token half of the Teia minting system.
It holds ALL token state — ledger, operators, metadata, on-chain data and
royalties — so that those travel permanently with the token across upgrades of
the minting logic. It is a self-contained TZIP-12 (FA2) multi-edition contract
written in modern SmartPy (v0.17+); it does NOT use
`smartpy.templates.fa2_lib`.

This is the companion of `teia_minter_v2.py`. The architecture mirrors HEN: a
permanent FA2 token whose `administrator` is a separate, upgradeable minter
contract. Minting is `administrator`-only here; the open/permissionless mint with
the fee and the multisig governance live in the minter. Upgrading the minting
logic is done by deploying a new minter and transferring this contract's
administrator to it — the token address, all NFTs, balances and history stay put.

Design notes:

  * `mint` is `administrator`-only and enforces the token invariants itself
    (editions > 0, royalty shares valid) so the permanent contract stays
    authoritative even under a buggy or replaced minter.
  * `paused` is token-level: it halts `transfer` and `mint`. Governance flips it
    through the minter (the administrator).
  * Royalty `shares` map: recipient address -> share of the sale price in per
    mille (100 = 10%). Recipients may be artists or an ArtistsCollaboration
    contract. Total capped at 25% (250 per mille), at most 10 recipients. Payout
    happens in the marketplace contract; here royalties are only stored/exposed.

Standard: https://gitlab.com/tezos/tzip/-/blob/master/proposals/tzip-12/tzip-12.md
"""

import smartpy as sp


@sp.module
def teia_fa2_module():
    """Teia FA2.1 token contract (permanent, multi-edition).

    Error codes:

    FA2 (TZIP-12 standard):
    - FA2_TOKEN_UNDEFINED: Token id is not defined
    - FA2_NOT_OPERATOR: Caller is neither owner nor an approved operator
    - FA2_INSUFFICIENT_BALANCE: Sender does not hold enough editions
    - FA2_NOT_OWNER: Caller is not the owner managing their operators
    - FA2_NOT_ADMIN: Caller is not the contract administrator

    Mint / token rules:
    - CONTRACT_PAUSED: Contract is globally paused
    - MINT_ZERO_EDITIONS: editions must be greater than zero
    - MINT_ZERO_SHARE: a royalty share must be strictly positive
    - MINT_TOO_MANY_RECIPIENTS: more than 10 royalty recipients
    - MINT_INVALID_ROYALTIES: total royalty shares exceed the 25% cap

    Administration:
    - TEZ_TRANSFER: Unexpected tez transfer
    - FA2_NO_NEW_ADMIN: No proposed administrator to accept
    - FA2_NOT_PROPOSED_ADMIN: Caller is not the proposed administrator
    """

    # ==========================================================================
    # FA2 Types (TZIP-12)
    # ==========================================================================

    ledger_key_type: type = sp.pair[sp.address, sp.nat]

    operator_key_type: type = sp.record(
        owner=sp.address,
        operator=sp.address,
        token_id=sp.nat,
    ).layout(("owner", ("operator", "token_id")))

    transfer_tx_type: type = sp.record(
        to_=sp.address,
        token_id=sp.nat,
        amount=sp.nat,
    ).layout(("to_", ("token_id", "amount")))

    transfer_batch_type: type = sp.record(
        from_=sp.address,
        txs=sp.list[transfer_tx_type],
    ).layout(("from_", "txs"))

    balance_of_request_type: type = sp.record(
        owner=sp.address,
        token_id=sp.nat,
    ).layout(("owner", "token_id"))

    balance_of_response_type: type = sp.record(
        request=balance_of_request_type,
        balance=sp.nat,
    ).layout(("request", "balance"))

    balance_of_params_type: type = sp.record(
        requests=sp.list[balance_of_request_type],
        callback=sp.contract[sp.list[balance_of_response_type]],
    ).layout(("requests", "callback"))

    token_info_type: type = sp.map[sp.string, sp.bytes]

    token_metadata_value_type: type = sp.record(
        token_id=sp.nat,
        token_info=token_info_type,
    ).layout(("token_id", "token_info"))

    update_operators_param_type: type = sp.list[
        sp.variant(
            add_operator=operator_key_type,
            remove_operator=operator_key_type,
        )
    ]

    burn_tx_type: type = sp.record(
        from_=sp.address,
        token_id=sp.nat,
        amount=sp.nat,
    ).layout(("from_", ("token_id", "amount")))

    # Royalty recipients -> share of the sale price, in per mille (100 = 10%).
    royalty_shares_type: type = sp.map[sp.address, sp.nat]

    # Mint parameters. `to_` receives the editions; `metadata` is the FA2 token
    # metadata (usually "" -> IPFS URI); `data` is arbitrary on-chain data
    # (international codes, etc.); `shares` are the royalty recipients.
    mint_params_type: type = sp.record(
        to_=sp.address,
        editions=sp.nat,
        metadata=token_info_type,
        data=token_info_type,
        shares=royalty_shares_type,
    ).layout(("to_", ("editions", ("metadata", ("data", "shares")))))

    metadata_type: type = sp.big_map[sp.string, sp.bytes]

    metadata_entry_type: type = sp.record(
        key=sp.string,
        value=sp.bytes,
    ).layout(("key", "value"))

    # ==========================================================================
    # Contract
    # ==========================================================================

    class TeiaFA2(sp.Contract):
        """Permanent multi-edition FA2.1 token; minting is administrator-only."""

        def __init__(self, administrator, metadata):
            self.data.administrator = sp.cast(administrator, sp.address)
            self.data.proposed_administrator = sp.cast(None, sp.option[sp.address])
            self.data.paused = False
            self.data.metadata = sp.cast(metadata, metadata_type)
            self.data.ledger = sp.cast(
                sp.big_map(), sp.big_map[ledger_key_type, sp.nat]
            )
            self.data.operators = sp.cast(
                sp.big_map(), sp.big_map[operator_key_type, sp.unit]
            )
            self.data.token_metadata = sp.cast(
                sp.big_map(), sp.big_map[sp.nat, token_metadata_value_type]
            )
            self.data.token_data = sp.cast(
                sp.big_map(), sp.big_map[sp.nat, token_info_type]
            )
            self.data.token_royalties = sp.cast(
                sp.big_map(), sp.big_map[sp.nat, royalty_shares_type]
            )
            self.data.supply = sp.cast(sp.big_map(), sp.big_map[sp.nat, sp.nat])
            self.data.next_token_id = sp.nat(0)

        # ======================================================================
        # Private Helpers
        # ======================================================================

        @sp.private(with_storage="read-only")
        def _check_not_paused(self):
            assert not self.data.paused, "CONTRACT_PAUSED"

        @sp.private(with_storage="read-only")
        def _check_is_admin(self):
            assert sp.sender == self.data.administrator, "FA2_NOT_ADMIN"

        @sp.private(with_storage="read-only")
        def _check_no_tez_transfer(self):
            assert sp.amount == sp.tez(0), "TEZ_TRANSFER"

        @sp.private(with_storage="read-only")
        def _check_token_defined(self, token_id):
            assert self.data.token_metadata.contains(token_id), "FA2_TOKEN_UNDEFINED"

        # ======================================================================
        # FA2 Standard Entrypoints
        # ======================================================================

        @sp.entrypoint
        def transfer(self, batch):
            """Standard FA2 batched transfer."""
            sp.cast(batch, sp.list[transfer_batch_type])
            self._check_no_tez_transfer()
            self._check_not_paused()
            for transfer in batch:
                for tx in transfer.txs:
                    self._check_token_defined(tx.token_id)
                    assert transfer.from_ == sp.sender or self.data.operators.contains(
                        sp.record(
                            owner=transfer.from_,
                            operator=sp.sender,
                            token_id=tx.token_id,
                        )
                    ), "FA2_NOT_OPERATOR"
                    if tx.amount > 0:
                        from_key = (transfer.from_, tx.token_id)
                        to_key = (tx.to_, tx.token_id)
                        self.data.ledger[from_key] = sp.as_nat(
                            self.data.ledger.get(from_key, default=0) - tx.amount,
                            error="FA2_INSUFFICIENT_BALANCE",
                        )
                        self.data.ledger[to_key] = (
                            self.data.ledger.get(to_key, default=0) + tx.amount
                        )

        @sp.entrypoint
        def update_operators(self, actions):
            """Add or remove operators. Only a token owner may manage their own
            operators."""
            sp.cast(actions, update_operators_param_type)
            self._check_no_tez_transfer()
            for action in actions:
                match action:
                    case add_operator(operator):
                        assert operator.owner == sp.sender, "FA2_NOT_OWNER"
                        self.data.operators[operator] = ()
                    case remove_operator(operator):
                        assert operator.owner == sp.sender, "FA2_NOT_OWNER"
                        if self.data.operators.contains(operator):
                            del self.data.operators[operator]

        @sp.entrypoint
        def balance_of(self, params):
            """Standard FA2 callback-based balance query."""
            sp.cast(params, balance_of_params_type)
            self._check_no_tez_transfer()
            balances = []
            for req in params.requests:
                self._check_token_defined(req.token_id)
                balances.push(
                    sp.record(
                        request=req,
                        balance=self.data.ledger.get(
                            (req.owner, req.token_id), default=0
                        ),
                    )
                )
            sp.transfer(reversed(balances), sp.mutez(0), params.callback)

        @sp.entrypoint
        def burn(self, batch):
            """Burn editions owned by `from_` (owner or operator only)."""
            sp.cast(batch, sp.list[burn_tx_type])
            self._check_no_tez_transfer()
            for tx in batch:
                self._check_token_defined(tx.token_id)
                assert tx.from_ == sp.sender or self.data.operators.contains(
                    sp.record(
                        owner=tx.from_,
                        operator=sp.sender,
                        token_id=tx.token_id,
                    )
                ), "FA2_NOT_OPERATOR"
                if tx.amount > 0:
                    from_key = (tx.from_, tx.token_id)
                    self.data.ledger[from_key] = sp.as_nat(
                        self.data.ledger.get(from_key, default=0) - tx.amount,
                        error="FA2_INSUFFICIENT_BALANCE",
                    )
                    self.data.supply[tx.token_id] = sp.as_nat(
                        self.data.supply.get(tx.token_id, default=0) - tx.amount,
                        error="FA2_INSUFFICIENT_BALANCE",
                    )
                    sp.emit(
                        sp.record(
                            token_id=tx.token_id,
                            owner=tx.from_,
                            amount=tx.amount,
                            burned_by=sp.sender,
                        ),
                        tag="token_burned",
                    )

        # ======================================================================
        # Mint (administrator only)
        # ======================================================================

        @sp.entrypoint
        def mint(self, params):
            """Mint a new token with `editions` copies. Administrator only.

            Called by the minter contract. Token invariants (editions, royalty
            shares) are enforced here so the permanent contract stays
            authoritative regardless of the minter in place. The original minter
            (the external account that initiated the call) is recorded in the
            emitted event via `sp.source`.
            """
            sp.cast(params, mint_params_type)
            self._check_no_tez_transfer()
            self._check_is_admin()
            self._check_not_paused()
            assert params.editions > 0, "MINT_ZERO_EDITIONS"

            # Validate royalty shares: each strictly positive, bounded count,
            # and a total capped at 25% (250 per mille).
            total_shares = sp.nat(0)
            recipient_count = sp.nat(0)
            for share in params.shares.values():
                assert share > 0, "MINT_ZERO_SHARE"
                total_shares += share
                recipient_count += 1
            assert recipient_count <= 10, "MINT_TOO_MANY_RECIPIENTS"
            assert total_shares <= 250, "MINT_INVALID_ROYALTIES"

            token_id = self.data.next_token_id
            self.data.token_metadata[token_id] = sp.record(
                token_id=token_id, token_info=params.metadata
            )
            self.data.token_data[token_id] = params.data
            self.data.token_royalties[token_id] = params.shares
            self.data.ledger[(params.to_, token_id)] = params.editions
            self.data.supply[token_id] = params.editions
            self.data.next_token_id += 1

            sp.emit(
                sp.record(
                    token_id=token_id,
                    owner=params.to_,
                    minter=sp.source,
                    shares=params.shares,
                    total_royalties=total_shares,
                    editions=params.editions,
                ),
                tag="token_minted",
            )

        # ======================================================================
        # Administration (administrator only)
        # ======================================================================

        @sp.entrypoint
        def set_pause(self, pause):
            """Pause or unpause the contract (halts transfer and mint)."""
            sp.cast(pause, sp.bool)
            self._check_no_tez_transfer()
            self._check_is_admin()
            self.data.paused = pause

        @sp.entrypoint
        def set_metadata(self, entry):
            """Update a contract (TZIP-16) metadata entry."""
            sp.cast(entry, metadata_entry_type)
            self._check_no_tez_transfer()
            self._check_is_admin()
            self.data.metadata[entry.key] = entry.value

        @sp.entrypoint
        def transfer_administrator(self, proposed_administrator):
            """Propose a new administrator (e.g. a new minter contract)."""
            sp.cast(proposed_administrator, sp.address)
            self._check_no_tez_transfer()
            self._check_is_admin()
            self.data.proposed_administrator = sp.Some(proposed_administrator)

        @sp.entrypoint
        def accept_administrator(self):
            """The proposed administrator accepts the administration."""
            assert (
                self.data.proposed_administrator.is_some()
            ), "FA2_NO_NEW_ADMIN"
            assert (
                sp.sender == self.data.proposed_administrator.unwrap_some()
            ), "FA2_NOT_PROPOSED_ADMIN"
            self._check_no_tez_transfer()
            self.data.administrator = sp.sender
            self.data.proposed_administrator = None

        # ======================================================================
        # FA2.1 On-chain Views
        # ======================================================================

        @sp.onchain_view()
        def get_balance(self, params):
            """Balance of `owner` for `token_id`."""
            sp.cast(params, balance_of_request_type)
            assert self.data.token_metadata.contains(
                params.token_id
            ), "FA2_TOKEN_UNDEFINED"
            return self.data.ledger.get((params.owner, params.token_id), default=0)

        @sp.onchain_view()
        def get_balance_of(self, requests):
            """On-chain equivalent of the `balance_of` entrypoint."""
            sp.cast(requests, sp.list[balance_of_request_type])
            balances = []
            for req in requests:
                assert self.data.token_metadata.contains(
                    req.token_id
                ), "FA2_TOKEN_UNDEFINED"
                balances.push(
                    sp.record(
                        request=req,
                        balance=self.data.ledger.get(
                            (req.owner, req.token_id), default=0
                        ),
                    )
                )
            return reversed(balances)

        @sp.onchain_view()
        def total_supply(self, token_id):
            """Total supply (number of live editions) of `token_id`."""
            sp.cast(token_id, sp.nat)
            assert self.data.token_metadata.contains(token_id), "FA2_TOKEN_UNDEFINED"
            return self.data.supply.get(token_id, default=0)

        @sp.onchain_view()
        def all_tokens(self):
            """All token ids ever minted."""
            return range(0, self.data.next_token_id)

        @sp.onchain_view()
        def count_tokens(self):
            """Number of token ids ever minted (== next token id)."""
            return self.data.next_token_id

        @sp.onchain_view()
        def token_exists(self, token_id):
            """Whether `token_id` is currently defined."""
            sp.cast(token_id, sp.nat)
            return self.data.token_metadata.contains(token_id)

        @sp.onchain_view()
        def is_operator(self, params):
            """Whether `operator` may transfer `(owner, token_id)`."""
            sp.cast(params, operator_key_type)
            return self.data.operators.contains(params)

        @sp.onchain_view()
        def get_token_metadata(self, token_id):
            """Token-metadata record for `token_id`."""
            sp.cast(token_id, sp.nat)
            assert self.data.token_metadata.contains(token_id), "FA2_TOKEN_UNDEFINED"
            return self.data.token_metadata[token_id]

        @sp.onchain_view()
        def get_token_data(self, token_id):
            """Arbitrary on-chain data map for `token_id` (intl. codes, etc.)."""
            sp.cast(token_id, sp.nat)
            assert self.data.token_data.contains(token_id), "FA2_TOKEN_UNDEFINED"
            return self.data.token_data[token_id]

        @sp.onchain_view()
        def get_token_royalties(self, token_id):
            """Royalty shares map (recipient -> per mille) for `token_id`."""
            sp.cast(token_id, sp.nat)
            assert self.data.token_royalties.contains(
                token_id
            ), "FA2_TOKEN_UNDEFINED"
            return self.data.token_royalties[token_id]

        @sp.onchain_view()
        def is_paused(self):
            """Whether the contract is currently paused."""
            return self.data.paused

        @sp.onchain_view()
        def get_administrator(self):
            """Current administrator address (the minter contract)."""
            return self.data.administrator

        # --- Off-chain view (read by indexers from the contract metadata) ---

        @sp.offchain_view
        def get_balance_offchain(self, params):
            sp.cast(params, balance_of_request_type)
            assert self.data.token_metadata.contains(
                params.token_id
            ), "FA2_TOKEN_UNDEFINED"
            return self.data.ledger.get((params.owner, params.token_id), default=0)


# ==============================================================================
# Balance receiver (for testing balance_of)
# ==============================================================================


@sp.module
def receiver_module():
    response_type: type = sp.record(
        request=sp.record(owner=sp.address, token_id=sp.nat).layout(
            ("owner", "token_id")
        ),
        balance=sp.nat,
    ).layout(("request", "balance"))

    class BalanceReceiver(sp.Contract):
        def __init__(self):
            self.data.last = sp.cast(None, sp.option[sp.list[response_type]])

        @sp.entrypoint
        def receive(self, responses):
            sp.cast(responses, sp.list[response_type])
            self.data.last = sp.Some(responses)


# ==============================================================================
# View helpers (pure Python, outside @sp.module)
# ==============================================================================


def _count_tokens(c):
    return sp.View(c, "count_tokens")()


def _token_exists(c, token_id):
    return sp.View(c, "token_exists")(token_id)


def _get_balance(c, args):
    return sp.View(c, "get_balance")(args)


def _total_supply(c, token_id):
    return sp.View(c, "total_supply")(token_id)


def _is_operator(c, args):
    return sp.View(c, "is_operator")(args)


def _get_token_data(c, token_id):
    return sp.View(c, "get_token_data")(token_id)


def _get_token_royalties(c, token_id):
    return sp.View(c, "get_token_royalties")(token_id)


def _is_paused(c):
    return sp.View(c, "is_paused")()


def _get_administrator(c):
    return sp.View(c, "get_administrator")()


# ==============================================================================
# Tests
# ==============================================================================


@sp.add_test()
def test():
    scenario = sp.test_scenario("TeiaFA2", [teia_fa2_module, receiver_module])
    scenario.h1("Teia FA2.1 token contract")

    admin = sp.test_account("Admin")
    new_admin = sp.test_account("NewAdmin")
    alice = sp.test_account("Alice")
    bob = sp.test_account("Bob")
    carol = sp.test_account("Carol")
    collab = sp.test_account("Collab")

    scenario.h2("Origination")
    contract = teia_fa2_module.TeiaFA2(
        administrator=admin.address,
        metadata=sp.big_map(),
    )
    scenario += contract

    md = sp.map({"": sp.bytes("0x697066733a2f2f6d657461")})  # ipfs://meta
    codes = sp.map(
        {
            "isrc": sp.scenario_utils.bytes_of_string("US-S1Z-99-00001"),
            "iscc": sp.scenario_utils.bytes_of_string("ISCC:KACT4EBWK27737D2"),
        }
    )

    scenario.h2("Mint is administrator-only")
    # A non-admin cannot mint.
    contract.mint(
        sp.record(
            to_=alice.address,
            editions=10,
            metadata=md,
            data=codes,
            shares={alice.address: 100},
        ),
        _sender=alice,
        _valid=False,
        _exception="FA2_NOT_ADMIN",
    )
    # Admin mints 10 editions to alice, royalties split alice/bob.
    contract.mint(
        sp.record(
            to_=alice.address,
            editions=10,
            metadata=md,
            data=codes,
            shares={alice.address: 50, bob.address: 50},
        ),
        _sender=admin,
    )
    # Admin mints 1 edition to bob, royalties to a collab contract.
    contract.mint(
        sp.record(
            to_=bob.address,
            editions=1,
            metadata=md,
            data={},
            shares={collab.address: 250},
        ),
        _sender=admin,
    )
    scenario.verify(_count_tokens(contract) == 2)
    scenario.verify(_total_supply(contract, 0) == 10)
    scenario.verify(
        _get_balance(contract, sp.record(owner=alice.address, token_id=0)) == 10
    )
    scenario.verify(_get_token_royalties(contract, 0)[alice.address] == 50)
    scenario.verify(_get_token_royalties(contract, 1)[collab.address] == 250)
    scenario.verify(
        _get_token_data(contract, 0)["isrc"]
        == sp.scenario_utils.bytes_of_string("US-S1Z-99-00001")
    )

    scenario.h2("Mint invariants")
    contract.mint(
        sp.record(
            to_=alice.address, editions=0, metadata=md, data={},
            shares={alice.address: 100},
        ),
        _sender=admin, _valid=False, _exception="MINT_ZERO_EDITIONS",
    )
    contract.mint(
        sp.record(
            to_=alice.address, editions=1, metadata=md, data={},
            shares={alice.address: 200, bob.address: 51},
        ),
        _sender=admin, _valid=False, _exception="MINT_INVALID_ROYALTIES",
    )
    contract.mint(
        sp.record(
            to_=alice.address, editions=1, metadata=md, data={},
            shares={alice.address: 0},
        ),
        _sender=admin, _valid=False, _exception="MINT_ZERO_SHARE",
    )
    many = {sp.test_account("R%d" % i).address: 1 for i in range(11)}
    contract.mint(
        sp.record(to_=alice.address, editions=1, metadata=md, data={}, shares=many),
        _sender=admin, _valid=False, _exception="MINT_TOO_MANY_RECIPIENTS",
    )

    scenario.h2("Operators and partial-edition transfer")
    contract.update_operators(
        [
            sp.variant.add_operator(
                sp.record(owner=alice.address, operator=bob.address, token_id=0)
            )
        ],
        _sender=alice,
    )
    scenario.verify(
        _is_operator(
            contract,
            sp.record(owner=alice.address, operator=bob.address, token_id=0),
        )
    )
    contract.transfer(
        [
            sp.record(
                from_=alice.address,
                txs=[sp.record(to_=carol.address, amount=3, token_id=0)],
            )
        ],
        _sender=bob,
    )
    scenario.verify(
        _get_balance(contract, sp.record(owner=alice.address, token_id=0)) == 7
    )
    scenario.verify(
        _get_balance(contract, sp.record(owner=carol.address, token_id=0)) == 3
    )
    # A stranger cannot move tokens.
    contract.transfer(
        [
            sp.record(
                from_=bob.address,
                txs=[sp.record(to_=alice.address, amount=1, token_id=1)],
            )
        ],
        _sender=alice,
        _valid=False,
        _exception="FA2_NOT_OPERATOR",
    )

    scenario.h2("Entrypoints reject attached tez (no locked funds)")
    contract.transfer(
        [
            sp.record(
                from_=alice.address,
                txs=[sp.record(to_=bob.address, amount=1, token_id=0)],
            )
        ],
        _sender=alice,
        _amount=sp.tez(1),
        _valid=False,
        _exception="TEZ_TRANSFER",
    )
    contract.burn(
        [sp.record(from_=alice.address, token_id=0, amount=1)],
        _sender=alice,
        _amount=sp.tez(1),
        _valid=False,
        _exception="TEZ_TRANSFER",
    )

    scenario.h2("balance_of via callback")
    receiver = receiver_module.BalanceReceiver()
    scenario += receiver
    cb = sp.contract(
        sp.list[receiver_module.response_type], receiver.address, "receive"
    ).unwrap_some()
    contract.balance_of(
        sp.record(
            requests=[sp.record(owner=carol.address, token_id=0)], callback=cb
        ),
        _sender=carol,
    )

    scenario.h2("Burn")
    contract.burn(
        [sp.record(from_=carol.address, token_id=0, amount=1)], _sender=carol
    )
    scenario.verify(_total_supply(contract, 0) == 9)
    contract.burn(
        [sp.record(from_=carol.address, token_id=0, amount=1)],
        _sender=alice,
        _valid=False,
        _exception="FA2_NOT_OPERATOR",
    )

    scenario.h2("Pause (admin only) blocks transfer")
    contract.set_pause(True, _sender=bob, _valid=False, _exception="FA2_NOT_ADMIN")
    contract.set_pause(True, _sender=admin)
    scenario.verify(_is_paused(contract))
    contract.transfer(
        [
            sp.record(
                from_=alice.address,
                txs=[sp.record(to_=bob.address, amount=1, token_id=0)],
            )
        ],
        _sender=alice,
        _valid=False,
        _exception="CONTRACT_PAUSED",
    )
    # Mint is also blocked while paused.
    contract.mint(
        sp.record(
            to_=alice.address, editions=1, metadata=md, data={},
            shares={alice.address: 100},
        ),
        _sender=admin, _valid=False, _exception="CONTRACT_PAUSED",
    )
    contract.set_pause(False, _sender=admin)

    scenario.h2("Administrator handoff (propose / accept)")
    # Only the admin can propose.
    contract.transfer_administrator(
        new_admin.address, _sender=bob, _valid=False, _exception="FA2_NOT_ADMIN"
    )
    contract.transfer_administrator(new_admin.address, _sender=admin)
    # Only the proposed admin can accept.
    contract.accept_administrator(
        _sender=bob, _valid=False, _exception="FA2_NOT_PROPOSED_ADMIN"
    )
    contract.accept_administrator(_sender=new_admin)
    scenario.verify(_get_administrator(contract) == new_admin.address)
    # The old admin can no longer mint.
    contract.mint(
        sp.record(
            to_=alice.address, editions=1, metadata=md, data={},
            shares={alice.address: 100},
        ),
        _sender=admin, _valid=False, _exception="FA2_NOT_ADMIN",
    )
    # The new admin can.
    contract.mint(
        sp.record(
            to_=alice.address, editions=2, metadata=md, data={},
            shares={alice.address: 100},
        ),
        _sender=new_admin,
    )
    scenario.verify(_count_tokens(contract) == 3)


@sp.add_test()
def teia_fa2_deploy():
    """Origination-only scenario (token; administrator set to the minter)."""
    scenario = sp.test_scenario("teia_fa2_deploy", teia_fa2_module)
    scenario.h1("Teia FA2.1 token - Deployment")

    # NOTE: the administrator is set during the two-step wiring documented in
    # teia_minter_v2.py. Here it is originated with a placeholder admin.
    ADMIN = sp.address("KT1J9FYz29RBQi1oGLw8uXyACrzXzV1dHuvb")

    contract_metadata = sp.big_map(
        {
            # TODO: replace with the real contract-metadata IPFS hash.
            "": sp.scenario_utils.bytes_of_string(
                "ipfs://QmcgWNSbBDqe2Q1Nmpg6crsAV7QDjXAQpAxQuLcWW2ffFC"
            ),
        }
    )

    contract = teia_fa2_module.TeiaFA2(
        administrator=ADMIN,
        metadata=contract_metadata,
    )
    scenario += contract
