"""Teia Minter v2 — open-mint orchestrator for the Teia FA2.1 token.

The upgradeable, multisig-governed minting half of the Teia minting system. It is
the `administrator` of the permanent `teia_fa2.py` token contract. The minting
logic, fee and governance live here; all token state lives in the FA2. Upgrading
the minting logic is done by deploying a new minter and transferring the FA2's
administrator to it — the token address and every NFT stay put.

Architecture (mirrors HEN):

    users ──mint(fee)──▶  Teia Minter  ──mint()──▶  Teia FA2 token
                          (this contract,           (permanent address,
                           FA2 administrator)         holds all token state)

Design rules:

  1. **Open minting** — `mint` is permissionless. Anyone may mint a token of N
     editions by attaching exactly `mint_fee`. The fee is forwarded to
     `fee_recipient` (the Teia treasury) and the mint is forwarded to the FA2,
     which assigns the token id, enforces the token invariants (editions > 0,
     royalty shares valid, not paused) and emits the canonical `token_minted`
     event. If the FA2 call reverts, the whole transaction — including the fee —
     reverts.

  2. **Multisig governance** — governance is delegated to an EXTERNAL multisig
     (the Teia Core Team multisig), queried through the views `is_user`,
     `get_minimum_votes` and `get_expiration_time`. Proposals are stored and
     voted on locally; only multisig users may submit / vote / execute.
     Governance controls the `mint_fee`, the `fee_recipient`, the multisig
     address, this contract's metadata, and — through its FA2 administrator
     rights — the FA2 `pause`, the FA2 contract metadata, and the FA2
     administrator handoff (the upgrade path to a new minter).

Standard: https://gitlab.com/tezos/tzip/-/blob/master/proposals/tzip-12/tzip-12.md
"""

import smartpy as sp

from teia_fa2 import (
    teia_fa2_module,
    _count_tokens,
    _get_balance,
    _total_supply,
    _get_token_royalties,
    _get_token_data,
    _is_paused,
    _get_administrator,
)


@sp.module
def teia_minter_module():
    """Teia Minter v2 — open-mint orchestrator for the Teia FA2.1 token.

    Error codes:

    Mint / fees:
    - INCORRECT_FEE: sp.amount does not match the required mint_fee
    - MINT_FA2_HANDLE_FAILED: could not resolve the FA2 mint entrypoint
    - ADMIN_FA2_HANDLE_FAILED: could not resolve an FA2 admin entrypoint

    Governance (multisig-gated):
    - TEZ_TRANSFER: Unexpected tez transfer on a governance call
    - NFT_VIEW_FAILED: is_user view on multisig failed
    - NFT_NOT_MULTISIG_USER: Caller is not a multisig user
    - NFT_GET_MIN_VOTES_FAILED: get_minimum_votes view failed
    - NFT_GET_EXPIRATION_FAILED: get_expiration_time view failed
    - NFT_PROPOSAL_EXPIRED: Proposal past expiration window
    - NFT_NO_PROPOSAL: Proposal ID does not exist
    - NFT_ALREADY_EXECUTED: Proposal already executed
    - NFT_NOT_ENOUGH_VOTES: Positive votes below minimum

    Token-level errors (FA2_*, MINT_*, CONTRACT_PAUSED) are raised by the FA2
    contract during the forwarded mint.
    """

    # ==========================================================================
    # Mint types (must match teia_fa2.mint_params_type exactly)
    # ==========================================================================

    token_info_type: type = sp.map[sp.string, sp.bytes]
    royalty_shares_type: type = sp.map[sp.address, sp.nat]

    mint_params_type: type = sp.record(
        to_=sp.address,
        editions=sp.nat,
        metadata=token_info_type,
        data=token_info_type,
        shares=royalty_shares_type,
    ).layout(("to_", ("editions", ("metadata", ("data", "shares")))))

    # ==========================================================================
    # Governance types
    # ==========================================================================

    metadata_type: type = sp.big_map[sp.string, sp.bytes]

    metadata_entry_type: type = sp.record(
        key=sp.string,
        value=sp.bytes,
    ).layout(("key", "value"))

    proposal_action_type: type = sp.variant(
        # Minter-local settings.
        set_mint_fee=sp.mutez,
        set_fee_recipient=sp.address,
        update_multisig_address=sp.address,
        update_metadata=metadata_entry_type,
        # Actions executed on the FA2 (the minter is its administrator).
        set_fa2_pause=sp.bool,
        set_fa2_metadata=metadata_entry_type,
        transfer_fa2_administrator=sp.address,
    )

    proposal_type: type = sp.record(
        action=proposal_action_type,
        executed=sp.bool,
        issuer=sp.address,
        timestamp=sp.timestamp,
        positive_votes=sp.nat,
        negative_votes=sp.nat,
    ).layout(
        (
            "action",
            (
                "executed",
                ("issuer", ("timestamp", ("positive_votes", "negative_votes"))),
            ),
        )
    )

    vote_key_type: type = sp.pair[sp.nat, sp.address]

    vote_params_type: type = sp.record(
        proposal_id=sp.nat,
        approval=sp.bool,
    ).layout(("proposal_id", "approval"))

    # ==========================================================================
    # Contract
    # ==========================================================================

    class TeiaMinter(sp.Contract):
        """Open-mint orchestrator; the administrator of the Teia FA2 token."""

        def __init__(
            self, fa2, multisig_address, fee_recipient, mint_fee, metadata, counter
        ):
            self.data.fa2 = sp.cast(fa2, sp.address)
            self.data.multisig_address = sp.cast(multisig_address, sp.address)
            self.data.fee_recipient = sp.cast(fee_recipient, sp.address)
            self.data.mint_fee = sp.cast(mint_fee, sp.mutez)
            self.data.metadata = sp.cast(metadata, metadata_type)
            self.data.proposals = sp.cast(
                sp.big_map(), sp.big_map[sp.nat, proposal_type]
            )
            self.data.votes = sp.cast(
                sp.big_map(), sp.big_map[vote_key_type, sp.bool]
            )
            self.data.counter = sp.cast(counter, sp.nat)

        # ======================================================================
        # Private Helpers
        # ======================================================================

        @sp.private(with_storage="read-only")
        def _check_no_tez_transfer(self):
            assert sp.amount == sp.tez(0), "TEZ_TRANSFER"

        @sp.private(with_storage="read-only")
        def _check_is_user(self):
            is_user = sp.view(
                "is_user", self.data.multisig_address, sp.sender, sp.bool
            ).unwrap_some(error="NFT_VIEW_FAILED")
            assert is_user, "NFT_NOT_MULTISIG_USER"

        @sp.private(with_storage="read-only")
        def _get_minimum_votes(self):
            return sp.view(
                "get_minimum_votes", self.data.multisig_address, (), sp.nat
            ).unwrap_some(error="NFT_GET_MIN_VOTES_FAILED")

        @sp.private(with_storage="read-only")
        def _check_not_expired(self, timestamp):
            expiration_days = sp.view(
                "get_expiration_time", self.data.multisig_address, (), sp.nat
            ).unwrap_some(error="NFT_GET_EXPIRATION_FAILED")
            expiration_seconds = sp.to_int(expiration_days * 86400)
            expiration_time = sp.add_seconds(timestamp, expiration_seconds)
            assert not (sp.now > expiration_time), "NFT_PROPOSAL_EXPIRED"

        # ======================================================================
        # Open Mint
        # ======================================================================

        @sp.entrypoint
        def mint(self, params):
            """Permissionless mint. Attach exactly `mint_fee`.

            The fee is forwarded to `fee_recipient`, then the mint is forwarded
            to the FA2 (which assigns the token id, enforces invariants and
            emits the event). The whole call is atomic.
            """
            sp.cast(params, mint_params_type)
            assert sp.amount == self.data.mint_fee, "INCORRECT_FEE"

            if sp.amount > sp.mutez(0):
                sp.send(self.data.fee_recipient, sp.amount)

            fa2_mint = sp.contract(
                mint_params_type, self.data.fa2, "mint"
            ).unwrap_some(error="MINT_FA2_HANDLE_FAILED")
            sp.transfer(params, sp.mutez(0), fa2_mint)

        @sp.entrypoint
        def accept_fa2_administrator(self):
            """Accept the FA2 administrator role (used once during setup).

            After the FA2 owner calls `transfer_administrator(this_minter)`, a
            multisig user calls this to make the minter the FA2 administrator.
            """
            self._check_no_tez_transfer()
            self._check_is_user()
            fa2_accept = sp.contract(
                sp.unit, self.data.fa2, "accept_administrator"
            ).unwrap_some(error="ADMIN_FA2_HANDLE_FAILED")
            sp.transfer((), sp.mutez(0), fa2_accept)

        # ======================================================================
        # Governance (external multisig)
        # ======================================================================

        @sp.entrypoint
        def submit_proposal(self, action):
            """Submit a new governance proposal (multisig users only)."""
            sp.cast(action, proposal_action_type)
            self._check_no_tez_transfer()
            self._check_is_user()

            self.data.proposals[self.data.counter] = sp.record(
                action=action,
                executed=False,
                issuer=sp.sender,
                timestamp=sp.now,
                positive_votes=sp.nat(0),
                negative_votes=sp.nat(0),
            )
            self.data.counter += 1

        @sp.entrypoint
        def vote_proposal(self, vote):
            """Vote on an existing proposal (multisig users only)."""
            sp.cast(vote, vote_params_type)
            self._check_no_tez_transfer()
            self._check_is_user()
            assert self.data.proposals.contains(vote.proposal_id), "NFT_NO_PROPOSAL"

            proposal = self.data.proposals[vote.proposal_id]
            assert not proposal.executed, "NFT_ALREADY_EXECUTED"
            self._check_not_expired(proposal.timestamp)

            vote_key = (vote.proposal_id, sp.sender)
            if self.data.votes.contains(vote_key):
                if self.data.votes[vote_key]:
                    proposal.positive_votes = sp.as_nat(proposal.positive_votes - 1)
                else:
                    proposal.negative_votes = sp.as_nat(proposal.negative_votes - 1)

            if vote.approval:
                proposal.positive_votes += 1
            else:
                proposal.negative_votes += 1

            self.data.votes[vote_key] = vote.approval
            self.data.proposals[vote.proposal_id] = proposal

        @sp.entrypoint
        def execute_proposal(self, proposal_id):
            """Execute an approved proposal (multisig users only)."""
            sp.cast(proposal_id, sp.nat)
            self._check_no_tez_transfer()
            self._check_is_user()
            assert self.data.proposals.contains(proposal_id), "NFT_NO_PROPOSAL"

            proposal = self.data.proposals[proposal_id]
            assert not proposal.executed, "NFT_ALREADY_EXECUTED"
            minimum_votes = self._get_minimum_votes()
            assert proposal.positive_votes >= minimum_votes, "NFT_NOT_ENOUGH_VOTES"
            self._check_not_expired(proposal.timestamp)

            proposal.executed = True
            self.data.proposals[proposal_id] = proposal

            # --- Minter-local actions ---
            if proposal.action.is_variant.set_mint_fee():
                self.data.mint_fee = proposal.action.unwrap.set_mint_fee()
                sp.emit(
                    sp.record(
                        proposal_id=proposal_id,
                        mint_fee=self.data.mint_fee,
                        updated_by=sp.sender,
                    ),
                    tag="mint_fee_set",
                )

            if proposal.action.is_variant.set_fee_recipient():
                self.data.fee_recipient = proposal.action.unwrap.set_fee_recipient()
                sp.emit(
                    sp.record(
                        proposal_id=proposal_id,
                        fee_recipient=self.data.fee_recipient,
                        updated_by=sp.sender,
                    ),
                    tag="fee_recipient_set",
                )

            if proposal.action.is_variant.update_multisig_address():
                self.data.multisig_address = (
                    proposal.action.unwrap.update_multisig_address()
                )
                sp.emit(
                    sp.record(
                        proposal_id=proposal_id,
                        multisig_address=self.data.multisig_address,
                        updated_by=sp.sender,
                    ),
                    tag="multisig_address_set",
                )

            if proposal.action.is_variant.update_metadata():
                entry = proposal.action.unwrap.update_metadata()
                self.data.metadata[entry.key] = entry.value
                sp.emit(
                    sp.record(
                        proposal_id=proposal_id,
                        key=entry.key,
                        updated_by=sp.sender,
                    ),
                    tag="metadata_updated",
                )

            # --- Actions executed on the FA2 ---
            if proposal.action.is_variant.set_fa2_pause():
                fa2_pause = sp.contract(
                    sp.bool, self.data.fa2, "set_pause"
                ).unwrap_some(error="ADMIN_FA2_HANDLE_FAILED")
                sp.transfer(
                    proposal.action.unwrap.set_fa2_pause(),
                    sp.mutez(0),
                    fa2_pause,
                )

            if proposal.action.is_variant.set_fa2_metadata():
                fa2_metadata = sp.contract(
                    metadata_entry_type, self.data.fa2, "set_metadata"
                ).unwrap_some(error="ADMIN_FA2_HANDLE_FAILED")
                sp.transfer(
                    proposal.action.unwrap.set_fa2_metadata(),
                    sp.mutez(0),
                    fa2_metadata,
                )

            if proposal.action.is_variant.transfer_fa2_administrator():
                fa2_admin = sp.contract(
                    sp.address, self.data.fa2, "transfer_administrator"
                ).unwrap_some(error="ADMIN_FA2_HANDLE_FAILED")
                sp.transfer(
                    proposal.action.unwrap.transfer_fa2_administrator(),
                    sp.mutez(0),
                    fa2_admin,
                )

        # ======================================================================
        # Views
        # ======================================================================

        @sp.onchain_view()
        def get_mint_fee(self):
            """Current mint fee."""
            return self.data.mint_fee

        @sp.onchain_view()
        def get_fee_recipient(self):
            """Current fee recipient (Teia treasury)."""
            return self.data.fee_recipient

        @sp.onchain_view()
        def get_fa2(self):
            """Address of the governed FA2 token contract."""
            return self.data.fa2

        @sp.onchain_view()
        def get_multisig_address(self):
            """Address of the governing multisig."""
            return self.data.multisig_address

        @sp.onchain_view()
        def get_proposal(self, proposal_id):
            """Governance proposal by id."""
            sp.cast(proposal_id, sp.nat)
            assert self.data.proposals.contains(proposal_id), "NFT_NO_PROPOSAL"
            return self.data.proposals[proposal_id]

        @sp.onchain_view()
        def get_vote(self, key):
            """Get a vote by key (proposal_id, voter)."""
            sp.cast(key, vote_key_type)
            return self.data.votes.get(key, default=False)


# ==============================================================================
# Mock multisig (for tests only)
# ==============================================================================


@sp.module
def mock_multisig_module():
    class MockMultisig(sp.Contract):
        def __init__(self, users, minimum_votes, expiration_time):
            self.data.users = sp.cast(users, sp.set[sp.address])
            self.data.minimum_votes = sp.cast(minimum_votes, sp.nat)
            self.data.expiration_time = sp.cast(expiration_time, sp.nat)

        @sp.onchain_view()
        def is_user(self, address):
            sp.cast(address, sp.address)
            return self.data.users.contains(address)

        @sp.onchain_view()
        def get_minimum_votes(self):
            return self.data.minimum_votes

        @sp.onchain_view()
        def get_expiration_time(self):
            return self.data.expiration_time


# ==============================================================================
# View helpers for the minter (FA2 helpers are imported from teia_fa2)
# ==============================================================================


def _get_mint_fee(c):
    return sp.View(c, "get_mint_fee")()


def _get_fa2(c):
    return sp.View(c, "get_fa2")()


def _get_fee_recipient(c):
    return sp.View(c, "get_fee_recipient")()


def _get_multisig_address(c):
    return sp.View(c, "get_multisig_address")()


# ==============================================================================
# Tests
# ==============================================================================


@sp.add_test()
def test():
    scenario = sp.test_scenario(
        "TeiaMinter_v2",
        [teia_fa2_module, teia_minter_module, mock_multisig_module],
    )
    scenario.h1("Teia Minter v2 — orchestrator over the Teia FA2 token")

    deployer = sp.test_account("Deployer")
    alice = sp.test_account("Alice")
    bob = sp.test_account("Bob")
    carol = sp.test_account("Carol")
    collab = sp.test_account("Collab")
    treasury = sp.test_account("Treasury")
    voter1 = sp.test_account("Voter1")
    voter2 = sp.test_account("Voter2")

    scenario.h2("Origination and wiring")
    multisig = mock_multisig_module.MockMultisig(
        users=sp.set([voter1.address, voter2.address]),
        minimum_votes=sp.nat(2),
        expiration_time=sp.nat(7),
    )
    scenario += multisig

    fa2 = teia_fa2_module.TeiaFA2(
        administrator=deployer.address,
        metadata=sp.big_map(),
    )
    scenario += fa2

    FEE = sp.mutez(100000)  # 0.1 tez
    minter = teia_minter_module.TeiaMinter(
        fa2=fa2.address,
        multisig_address=multisig.address,
        fee_recipient=treasury.address,
        mint_fee=FEE,
        metadata=sp.big_map(),
        counter=sp.nat(0),
    )
    scenario += minter

    # Two-step admin handoff: deployer hands the FA2 to the minter.
    fa2.transfer_administrator(minter.address, _sender=deployer)
    # A multisig user triggers the minter to accept.
    minter.accept_fa2_administrator(
        _sender=alice, _valid=False, _exception="NFT_NOT_MULTISIG_USER"
    )
    minter.accept_fa2_administrator(_sender=voter1)
    scenario.verify(_get_administrator(fa2) == minter.address)

    md = sp.map({"": sp.bytes("0x697066733a2f2f6d657461")})  # ipfs://meta
    codes = sp.map(
        {
            "isrc": sp.scenario_utils.bytes_of_string("US-S1Z-99-00001"),
            "iscc": sp.scenario_utils.bytes_of_string("ISCC:KACT4EBWK27737D2"),
        }
    )

    scenario.h2("Open minting through the minter (anyone can mint)")
    minter.mint(
        sp.record(
            to_=alice.address,
            editions=10,
            metadata=md,
            data=codes,
            shares={alice.address: 50, bob.address: 50},
        ),
        _sender=alice,
        _amount=FEE,
    )
    minter.mint(
        sp.record(
            to_=bob.address,
            editions=1,
            metadata=md,
            data={},
            shares={collab.address: 250},
        ),
        _sender=bob,
        _amount=FEE,
    )
    # Tokens landed in the FA2 with the right state.
    scenario.verify(_count_tokens(fa2) == 2)
    scenario.verify(_total_supply(fa2, 0) == 10)
    scenario.verify(
        _get_balance(fa2, sp.record(owner=alice.address, token_id=0)) == 10
    )
    scenario.verify(_get_token_royalties(fa2, 0)[bob.address] == 50)
    scenario.verify(_get_token_royalties(fa2, 1)[collab.address] == 250)
    scenario.verify(
        _get_token_data(fa2, 0)["isrc"]
        == sp.scenario_utils.bytes_of_string("US-S1Z-99-00001")
    )

    scenario.h2("Fee enforcement")
    # Wrong fee rejected (by the minter).
    minter.mint(
        sp.record(
            to_=carol.address, editions=1, metadata=md, data={},
            shares={carol.address: 100},
        ),
        _sender=carol, _amount=sp.tez(2),
        _valid=False, _exception="INCORRECT_FEE",
    )
    # Token invariant rejected (by the FA2, surfaced through the minter).
    minter.mint(
        sp.record(
            to_=carol.address, editions=0, metadata=md, data={},
            shares={carol.address: 100},
        ),
        _sender=carol, _amount=FEE,
        _valid=False, _exception="MINT_ZERO_EDITIONS",
    )
    minter.mint(
        sp.record(
            to_=carol.address, editions=1, metadata=md, data={},
            shares={carol.address: 200, alice.address: 51},
        ),
        _sender=carol, _amount=FEE,
        _valid=False, _exception="MINT_INVALID_ROYALTIES",
    )

    scenario.h2("Governance: change the mint fee")
    minter.submit_proposal(
        sp.variant.set_mint_fee(sp.tez(0)),
        _sender=alice, _valid=False, _exception="NFT_NOT_MULTISIG_USER",
    )
    minter.submit_proposal(sp.variant.set_mint_fee(sp.tez(0)), _sender=voter1)  # id 0
    minter.vote_proposal(sp.record(proposal_id=0, approval=True), _sender=voter1)
    minter.execute_proposal(
        0, _sender=voter1, _valid=False, _exception="NFT_NOT_ENOUGH_VOTES"
    )
    minter.vote_proposal(sp.record(proposal_id=0, approval=True), _sender=voter2)
    minter.execute_proposal(0, _sender=voter2)
    scenario.verify(_get_mint_fee(minter) == sp.tez(0))

    scenario.h2("Free minting after the fee is zeroed")
    minter.mint(
        sp.record(
            to_=carol.address, editions=3, metadata=md, data={},
            shares={carol.address: 100},
        ),
        _sender=carol,
    )
    scenario.verify(_count_tokens(fa2) == 3)
    scenario.verify(_total_supply(fa2, 2) == 3)

    scenario.h2("Governance: pause the FA2 through the minter")
    minter.submit_proposal(sp.variant.set_fa2_pause(True), _sender=voter1)  # id 1
    minter.vote_proposal(sp.record(proposal_id=1, approval=True), _sender=voter1)
    minter.vote_proposal(sp.record(proposal_id=1, approval=True), _sender=voter2)
    minter.execute_proposal(1, _sender=voter1)
    scenario.verify(_is_paused(fa2))
    # Minting now blocked at the FA2.
    minter.mint(
        sp.record(
            to_=carol.address, editions=1, metadata=md, data={},
            shares={carol.address: 100},
        ),
        _sender=carol,
        _valid=False, _exception="CONTRACT_PAUSED",
    )
    # Unpause.
    minter.submit_proposal(sp.variant.set_fa2_pause(False), _sender=voter1)  # id 2
    minter.vote_proposal(sp.record(proposal_id=2, approval=True), _sender=voter1)
    minter.vote_proposal(sp.record(proposal_id=2, approval=True), _sender=voter2)
    minter.execute_proposal(2, _sender=voter2)
    scenario.verify(~_is_paused(fa2))

    scenario.h2("Governance: set_fee_recipient")
    new_treasury = sp.test_account("NewTreasury")
    minter.submit_proposal(
        sp.variant.set_fee_recipient(new_treasury.address), _sender=voter1
    )  # id 3
    minter.vote_proposal(sp.record(proposal_id=3, approval=True), _sender=voter1)
    minter.vote_proposal(sp.record(proposal_id=3, approval=True), _sender=voter2)
    minter.execute_proposal(3, _sender=voter1)
    scenario.verify(_get_fee_recipient(minter) == new_treasury.address)

    scenario.h2("Governance: update_metadata (minter's own TZIP-16 metadata)")
    minter_md_value = sp.scenario_utils.bytes_of_string("ipfs://minter-meta")
    minter.submit_proposal(
        sp.variant.update_metadata(sp.record(key="", value=minter_md_value)),
        _sender=voter1,
    )  # id 4
    minter.vote_proposal(sp.record(proposal_id=4, approval=True), _sender=voter1)
    minter.vote_proposal(sp.record(proposal_id=4, approval=True), _sender=voter2)
    minter.execute_proposal(4, _sender=voter2)
    scenario.verify(minter.data.metadata[""] == minter_md_value)

    scenario.h2("Governance: set_fa2_metadata (inter-contract call to the FA2)")
    fa2_md_value = sp.scenario_utils.bytes_of_string("ipfs://fa2-meta-v2")
    minter.submit_proposal(
        sp.variant.set_fa2_metadata(sp.record(key="", value=fa2_md_value)),
        _sender=voter1,
    )  # id 5
    minter.vote_proposal(sp.record(proposal_id=5, approval=True), _sender=voter1)
    minter.vote_proposal(sp.record(proposal_id=5, approval=True), _sender=voter2)
    minter.execute_proposal(5, _sender=voter1)
    scenario.verify(fa2.data.metadata[""] == fa2_md_value)

    scenario.h2("Governance: update_multisig_address")
    multisig2 = mock_multisig_module.MockMultisig(
        users=sp.set([voter1.address, voter2.address]),
        minimum_votes=sp.nat(2),
        expiration_time=sp.nat(7),
    )
    scenario += multisig2
    minter.submit_proposal(
        sp.variant.update_multisig_address(multisig2.address), _sender=voter1
    )  # id 6
    minter.vote_proposal(sp.record(proposal_id=6, approval=True), _sender=voter1)
    minter.vote_proposal(sp.record(proposal_id=6, approval=True), _sender=voter2)
    minter.execute_proposal(6, _sender=voter2)
    scenario.verify(_get_multisig_address(minter) == multisig2.address)
    # Governance now runs through multisig2 (same voters).

    scenario.h2("Governance: upgrade path — hand the FA2 to a new minter")
    new_minter = teia_minter_module.TeiaMinter(
        fa2=fa2.address,
        multisig_address=multisig2.address,
        fee_recipient=treasury.address,
        mint_fee=FEE,
        metadata=sp.big_map(),
        counter=sp.nat(0),
    )
    scenario += new_minter
    minter.submit_proposal(
        sp.variant.transfer_fa2_administrator(new_minter.address), _sender=voter1
    )  # id 7 (gated by multisig2)
    minter.vote_proposal(sp.record(proposal_id=7, approval=True), _sender=voter1)
    minter.vote_proposal(sp.record(proposal_id=7, approval=True), _sender=voter2)
    minter.execute_proposal(7, _sender=voter1)
    # The new minter accepts and becomes the FA2 administrator.
    new_minter.accept_fa2_administrator(_sender=voter1)
    scenario.verify(_get_administrator(fa2) == new_minter.address)
    # The old minter can no longer mint into the FA2.
    minter.mint(
        sp.record(
            to_=carol.address, editions=1, metadata=md, data={},
            shares={carol.address: 100},
        ),
        _sender=carol,
        _valid=False, _exception="FA2_NOT_ADMIN",
    )
    # The new minter can (it carries its own 0.1 XTZ fee).
    new_minter.mint(
        sp.record(
            to_=carol.address, editions=1, metadata=md, data={},
            shares={carol.address: 100},
        ),
        _sender=carol,
        _amount=FEE,
    )
    scenario.verify(_count_tokens(fa2) == 4)


# ==============================================================================
# Deployment Scenarios
# ==============================================================================


@sp.add_test()
def teia_minter_v2_deploy_shadownet():
    """Deployment scenario for Tezos Shadownet (FA2 + minter + handoff)."""
    scenario = sp.test_scenario(
        "teia_minter_v2_deploy_shadownet", [teia_fa2_module, teia_minter_module]
    )
    scenario.h1("Teia Minter v2 — Deployment Shadownet")

    # The deployer originates the FA2, then hands it to the minter.
    deployer = sp.test_account("Deployer")
    MULTISIG_ADDRESS = sp.address("KT1KeGd4YtjcKqgyiXUPJQkm2iYA3fQwLGQP")
    # TODO: confirm the Teia treasury address that should receive mint fees.
    FEE_RECIPIENT_ADDRESS = sp.address("KT1KeGd4YtjcKqgyiXUPJQkm2iYA3fQwLGQP")
    MINT_FEE = sp.mutez(100000)  # 0.1 tez; multisig can change it later.

    contract_metadata = sp.big_map(
        {
            # TODO: replace with the real contract-metadata IPFS hash.
            "": sp.scenario_utils.bytes_of_string(
                "ipfs://QmcgWNSbBDqe2Q1Nmpg6crsAV7QDjXAQpAxQuLcWW2ffFC"
            ),
        }
    )

    fa2 = teia_fa2_module.TeiaFA2(
        administrator=deployer.address,
        metadata=contract_metadata,
    )
    scenario += fa2

    minter = teia_minter_module.TeiaMinter(
        fa2=fa2.address,
        multisig_address=MULTISIG_ADDRESS,
        fee_recipient=FEE_RECIPIENT_ADDRESS,
        mint_fee=MINT_FEE,
        metadata=contract_metadata,
        counter=sp.nat(0),
    )
    scenario += minter

    fa2.transfer_administrator(minter.address, _sender=deployer)
    # On-chain, a multisig user then calls minter.accept_fa2_administrator().


@sp.add_test()
def teia_minter_v2_deploy_mainnet():
    """Deployment scenario for Tezos mainnet (FA2 + minter + handoff)."""
    scenario = sp.test_scenario(
        "teia_minter_v2_deploy_mainnet", [teia_fa2_module, teia_minter_module]
    )
    scenario.h1("Teia Minter v2 — Deployment Mainnet")

    deployer = sp.test_account("Deployer")
    # Teia Core Team multisig / mini-DAO.
    MULTISIG_ADDRESS = sp.address("KT1J9FYz29RBQi1oGLw8uXyACrzXzV1dHuvb")
    # TODO: confirm the Teia treasury address that should receive mint fees.
    FEE_RECIPIENT_ADDRESS = sp.address("KT1J9FYz29RBQi1oGLw8uXyACrzXzV1dHuvb")
    MINT_FEE = sp.mutez(100000)  # 0.1 tez; multisig can change it later.

    contract_metadata = sp.big_map(
        {
            # TODO: replace with the real contract-metadata IPFS hash.
            "": sp.scenario_utils.bytes_of_string(
                "ipfs://QmcgWNSbBDqe2Q1Nmpg6crsAV7QDjXAQpAxQuLcWW2ffFC"
            ),
        }
    )

    fa2 = teia_fa2_module.TeiaFA2(
        administrator=deployer.address,
        metadata=contract_metadata,
    )
    scenario += fa2

    minter = teia_minter_module.TeiaMinter(
        fa2=fa2.address,
        multisig_address=MULTISIG_ADDRESS,
        fee_recipient=FEE_RECIPIENT_ADDRESS,
        mint_fee=MINT_FEE,
        metadata=contract_metadata,
        counter=sp.nat(0),
    )
    scenario += minter

    fa2.transfer_administrator(minter.address, _sender=deployer)
    # On-chain, a multisig user then calls minter.accept_fa2_administrator().
