import smartpy as sp


@sp.module
def channels_module():
    """Teia Channels Contract.

    Three-tier access (creator > admin > merkle user) with optional allowlist gating.
    Channel-level governance via the Teia multisig contract.

    Error codes:

    Channel / message:
    - CONTRACT_PAUSED: Contract is globally paused
    - TEZ_TRANSFER: Unexpected tez transfer
    - INCORRECT_FEE: sp.amount does not match the required fee
    - CHANNEL_NOT_FOUND: Channel ID does not exist
    - CHANNEL_HIDDEN: Channel has been hidden
    - EMPTY_METADATA: metadata_uri is empty
    - EMPTY_CONTENT: Message content is empty
    - CONTENT_TOO_LARGE: Message content exceeds 32 KiB
    - PARENT_NOT_FOUND: Reply target message does not exist
    - PARENT_WRONG_CHANNEL: Reply target is in a different channel
    - MESSAGE_NOT_FOUND: Message ID does not exist
    - UNDERFLOW: message_count would go negative

    Authorization:
    - NOT_AUTHORIZED: Caller cannot perform this action
    - NOT_CHANNEL_CREATOR: Caller is not the channel creator
    - NOT_CHANNEL_ADMIN: Caller is not creator or admin
    - TOO_MANY_ADMINS: Channel admin count would exceed 15

    Access config / Merkle:
    - ALLOWLIST_NEEDS_ROOT: allowlist mode requires merkle_root
    - ROOT_NOT_ALLOWED: non-allowlist modes must not set merkle_root
    - NO_MERKLE_ROOT: Channel has no merkle_root configured
    - PROOF_REQUIRED: Caller must supply a merkle proof
    - INVALID_MERKLE_PROOF: Provided proof does not match merkle_root

    Governance (multisig-gated):
    - CHANNELS_VIEW_FAILED: is_user view on multisig failed
    - CHANNELS_NOT_MULTISIG_USER: Caller is not a multisig user
    - CHANNELS_GET_MIN_VOTES_FAILED: get_minimum_votes view failed
    - CHANNELS_GET_EXPIRATION_FAILED: get_expiration_time view failed
    - CHANNELS_PROPOSAL_EXPIRED: Proposal past expiration window
    - CHANNELS_NO_PROPOSAL: Proposal ID does not exist
    - CHANNELS_ALREADY_EXECUTED: Proposal already executed
    - CHANNELS_NOT_ENOUGH_VOTES: Positive votes below minimum
    """
    
    # ===========================================================================
    # Types
    # ===========================================================================

    # --- Access Control ---

    access_mode_type: type = sp.variant(
        unrestricted=sp.unit,
        allowlist=sp.unit,
        closed=sp.unit,
    )

    merkle_step_type: type = sp.record(
        hash=sp.bytes,
        direction=sp.nat,  # 0 = sibling is left, 1 = sibling is right
    )

    # --- Channel ---

    channel_type: type = sp.record(
        creator=sp.address,
        metadata_uri=sp.bytes,
        access_mode=access_mode_type,
        merkle_root=sp.option[sp.bytes],
        merkle_uri=sp.option[sp.bytes],
        message_count=sp.nat,
        hidden=sp.bool,
        timestamp=sp.timestamp,
    )

    # --- Message ---

    message_type: type = sp.record(
        channel_id=sp.nat,
        sender=sp.address,
        content=sp.bytes,
        parent_id=sp.option[sp.nat],
        timestamp=sp.timestamp,
    )

    # --- Entrypoint Params ---

    post_message_params_type: type = sp.record(
        channel_id=sp.nat,
        content=sp.bytes,
        proof=sp.option[sp.list[merkle_step_type]],
        parent_id=sp.option[sp.nat],
    )

    create_channel_params_type: type = sp.record(
        metadata_uri=sp.bytes,
        access_mode=access_mode_type,
        merkle_root=sp.option[sp.bytes],
        merkle_uri=sp.option[sp.bytes],
        admins=sp.list[sp.address],
    )

    configure_channel_params_type: type = sp.record(
        channel_id=sp.nat,
        access_mode=access_mode_type,
        merkle_root=sp.option[sp.bytes],
        merkle_uri=sp.option[sp.bytes],
    )

    write_message_params_type: type = sp.record(
        channel_id=sp.nat,
        content=sp.bytes,
        parent_id=sp.option[sp.nat],
    )

    update_channel_admins_params_type: type = sp.record(
        channel_id=sp.nat,
        to_add=sp.list[sp.address],
        to_remove=sp.list[sp.address],
    )

    # --- Storage Types ---

    channels_map_type: type = sp.big_map[sp.nat, channel_type]
    messages_map_type: type = sp.big_map[sp.nat, message_type]
    # Used as the input record for the is_channel_admin view.
    channel_admin_key_type: type = sp.record(channel_id=sp.nat, address=sp.address)
    # One set of admin addresses per channel. sp.len() of the set enforces the
    # 15-admin cap directly. Always initialised (possibly empty) on create_channel.
    channel_admins_map_type: type = sp.big_map[sp.nat, sp.set[sp.address]]
    metadata_type: type = sp.big_map[sp.string, sp.bytes]

    # --- Governance Types ---

    metadata_entry_type: type = sp.record(
        key=sp.string,
        value=sp.bytes,
    ).layout(("key", "value"))

    proposal_action_type: type = sp.variant(
        set_pause=sp.bool,
        set_message_fee=sp.mutez,
        set_channel_fee=sp.mutez,
        set_fee_recipient=sp.address,
        update_multisig_address=sp.address,
        update_metadata=metadata_entry_type,
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
                (
                    "issuer",
                    ("timestamp", ("positive_votes", "negative_votes")),
                ),
            ),
        )
    )

    vote_key_type: type = sp.pair[sp.nat, sp.address]

    vote_params_type: type = sp.record(
        proposal_id=sp.nat,
        approval=sp.bool,
    ).layout(("proposal_id", "approval"))

    # ===========================================================================
    # Contract
    # ===========================================================================

    class Channels(sp.Contract):
        """Teia Channels — three-tier access (creator/admin/merkle user) with optional allowlist."""

        def __init__(self, multisig_address, fee_recipient, message_fee, channel_fee, metadata, counter):
            self.data.multisig_address = sp.cast(multisig_address, sp.address)
            self.data.metadata = sp.cast(metadata, metadata_type)
            self.data.paused = False
            self.data.channels = sp.cast(sp.big_map(), channels_map_type)
            self.data.messages = sp.cast(sp.big_map(), messages_map_type)
            self.data.channel_admins = sp.cast(sp.big_map(), channel_admins_map_type)
            self.data.channel_id_counter = sp.nat(1)
            self.data.message_id_counter = sp.nat(1)
            self.data.message_fee = sp.cast(message_fee, sp.mutez)
            self.data.channel_fee = sp.cast(channel_fee, sp.mutez)
            self.data.fee_recipient = sp.cast(fee_recipient, sp.address)
            self.data.proposals = sp.cast(sp.big_map(), sp.big_map[sp.nat, proposal_type])
            self.data.votes = sp.cast(sp.big_map(), sp.big_map[vote_key_type, sp.bool])
            self.data.counter = sp.cast(counter, sp.nat)

        # =======================================================================
        # Private Helpers
        # =======================================================================

        @sp.private(with_storage="read-only")
        def _check_not_paused(self):
            assert not self.data.paused, "CONTRACT_PAUSED"

        @sp.private(with_storage="read-only")
        def _validate_access_config(self, params):
            """Assert that access_mode and merkle_root are consistent."""
            sp.cast(params, sp.record(access_mode=access_mode_type, merkle_root=sp.option[sp.bytes]))
            if params.access_mode.is_variant.allowlist():
                assert params.merkle_root.is_some(), "ALLOWLIST_NEEDS_ROOT"
            else:
                assert not params.merkle_root.is_some(), "ROOT_NOT_ALLOWED"

        @sp.private(with_storage="read-only")
        def _check_no_tez_transfer(self):
            assert sp.amount == sp.tez(0), "TEZ_TRANSFER"

        @sp.private(with_storage="read-only")
        def _check_is_user(self):
            is_user = sp.view(
                "is_user",
                self.data.multisig_address,
                sp.sender,
                sp.bool,
            ).unwrap_some(error="CHANNELS_VIEW_FAILED")
            assert is_user, "CHANNELS_NOT_MULTISIG_USER"

        @sp.private(with_storage="read-only")
        def _get_minimum_votes(self):
            return sp.view(
                "get_minimum_votes",
                self.data.multisig_address,
                (),
                sp.nat,
            ).unwrap_some(error="CHANNELS_GET_MIN_VOTES_FAILED")

        @sp.private(with_storage="read-only")
        def _check_not_expired(self, timestamp):
            expiration_days = sp.view(
                "get_expiration_time",
                self.data.multisig_address,
                (),
                sp.nat,
            ).unwrap_some(error="CHANNELS_GET_EXPIRATION_FAILED")

            expiration_seconds = sp.to_int(expiration_days * 86400)
            expiration_time = sp.add_seconds(timestamp, expiration_seconds)
            assert not (sp.now > expiration_time), "CHANNELS_PROPOSAL_EXPIRED"

        @sp.private(with_storage="read-write", with_operations=True)
        def _send_fee(self, fee):
            """Sends fee to fee_recipient if > 0."""
            if fee > sp.mutez(0):
                sp.send(self.data.fee_recipient, fee)

        @sp.private(with_storage="read-only")
        def _check_channel_admin(self, channel_id):
            """Assert sender is channel creator or channel admin."""
            sp.cast(channel_id, sp.nat)
            channel = self.data.channels[channel_id]
            assert channel.creator == sp.sender or sp.sender in self.data.channel_admins[channel_id], "NOT_CHANNEL_ADMIN"

        @sp.private(with_storage="read-write")
        def _write_message(self, params):
            """Writes a message and increments counters."""
            sp.cast(params, write_message_params_type)
            message_id = self.data.message_id_counter
            self.data.messages[message_id] = sp.record(
                channel_id=params.channel_id,
                sender=sp.sender,
                content=params.content,
                parent_id=params.parent_id,
                timestamp=sp.now,
            )

            channel = self.data.channels[params.channel_id]
            self.data.channels[params.channel_id] = sp.record(
                creator=channel.creator,
                metadata_uri=channel.metadata_uri,
                access_mode=channel.access_mode,
                merkle_root=channel.merkle_root,
                merkle_uri=channel.merkle_uri,
                message_count=channel.message_count + 1,
                hidden=channel.hidden,
                timestamp=channel.timestamp,
            )

            self.data.message_id_counter += 1
            return message_id

        @sp.private(with_storage="read-only")
        def _check_can_post(self, params):
            """Assert sender can post: creator, admin, or valid merkle proof in allowlist mode.
            Closed channels reject all non-privileged posters."""
            sp.cast(params, sp.record(channel_id=sp.nat, proof=sp.option[sp.list[merkle_step_type]]))
            channel = self.data.channels[params.channel_id]
            is_privileged = channel.creator == sp.sender or sp.sender in self.data.channel_admins[params.channel_id]
            if not is_privileged:
                if channel.access_mode.is_variant.closed():
                    assert False, "NOT_AUTHORIZED"
                if channel.access_mode.is_variant.allowlist():
                    root = channel.merkle_root.unwrap_some(error="NO_MERKLE_ROOT")
                    proof = params.proof.unwrap_some(error="PROOF_REQUIRED")
                    computed = sp.blake2b(sp.pack(sp.sender))
                    for step in proof:
                        if step.direction == 0:
                            computed = sp.blake2b(step.hash + computed)
                        else:
                            computed = sp.blake2b(computed + step.hash)
                    assert computed == root, "INVALID_MERKLE_PROOF"

        # =======================================================================
        # Channel Entrypoints
        # =======================================================================

        @sp.entrypoint
        def create_channel(self, params):
            """Create a new channel with initial configuration. Anyone can create."""
            sp.cast(params, create_channel_params_type)
            self._check_not_paused()
            assert sp.len(params.metadata_uri) > 0, "EMPTY_METADATA"
            assert sp.amount == self.data.channel_fee, "INCORRECT_FEE"

            self._validate_access_config(sp.record(access_mode=params.access_mode, merkle_root=params.merkle_root))
            self._send_fee(self.data.channel_fee)

            channel_id = self.data.channel_id_counter
            self.data.channels[channel_id] = sp.record(
                creator=sp.sender,
                metadata_uri=params.metadata_uri,
                access_mode=params.access_mode,
                merkle_root=params.merkle_root,
                merkle_uri=params.merkle_uri,
                message_count=sp.nat(0),
                hidden=False,
                timestamp=sp.now,
            )

            # Build the admin set, deduping any repeats in the input list, then
            # enforce the 15-admin cap on the resulting set size.
            admins_set = sp.cast(set(), sp.set[sp.address])
            for addr in params.admins:
                admins_set.add(addr)
            assert sp.len(admins_set) <= sp.nat(15), "TOO_MANY_ADMINS"
            self.data.channel_admins[channel_id] = admins_set

            self.data.channel_id_counter += 1

            sp.emit(
                sp.record(
                    channel_id=channel_id,
                    creator=sp.sender,
                    metadata_uri=params.metadata_uri,
                    access_mode=params.access_mode,
                    merkle_root=params.merkle_root,
                    merkle_uri=params.merkle_uri,
                    admins=params.admins,
                    timestamp=sp.now,
                ),
                tag="channel_created",
            )

        @sp.entrypoint
        def configure_channel(self, params):
            """Set access mode and Merkle root. Creator or admin."""
            sp.cast(params, configure_channel_params_type)
            self._check_not_paused()
            self._check_no_tez_transfer()

            assert params.channel_id in self.data.channels, "CHANNEL_NOT_FOUND"
            channel = self.data.channels[params.channel_id]
            self._check_channel_admin(params.channel_id)
            assert not channel.hidden, "CHANNEL_HIDDEN"

            self._validate_access_config(sp.record(access_mode=params.access_mode, merkle_root=params.merkle_root))

            self.data.channels[params.channel_id] = sp.record(
                creator=channel.creator,
                metadata_uri=channel.metadata_uri,
                access_mode=params.access_mode,
                merkle_root=params.merkle_root,
                merkle_uri=params.merkle_uri,
                message_count=channel.message_count,
                hidden=channel.hidden,
                timestamp=channel.timestamp,
            )

            sp.emit(
                sp.record(
                    channel_id=params.channel_id,
                    access_mode=params.access_mode,
                    merkle_root=params.merkle_root,
                    merkle_uri=params.merkle_uri,
                    configured_by=sp.sender,
                ),
                tag="channel_configured",
            )

        @sp.entrypoint
        def update_channel_admins(self, params):
            """Add or remove channel admins. Creator only."""
            sp.cast(params, update_channel_admins_params_type)
            self._check_not_paused()
            self._check_no_tez_transfer()

            assert params.channel_id in self.data.channels, "CHANNEL_NOT_FOUND"
            channel = self.data.channels[params.channel_id]
            assert channel.creator == sp.sender, "NOT_CHANNEL_CREATOR"

            # Apply removals first, then additions, so a creator can swap
            # admins (remove one, add another) without temporarily exceeding 15.
            for addr in params.to_remove:
                if addr in self.data.channel_admins[params.channel_id]:
                    self.data.channel_admins[params.channel_id].remove(addr)

            for addr in params.to_add:
                self.data.channel_admins[params.channel_id].add(addr)

            assert sp.len(self.data.channel_admins[params.channel_id]) <= sp.nat(15), "TOO_MANY_ADMINS"

            sp.emit(
                sp.record(
                    channel_id=params.channel_id,
                    to_add=params.to_add,
                    to_remove=params.to_remove,
                ),
                tag="channel_admins_updated",
            )

        @sp.entrypoint
        def post_message(self, params):
            """Post a message to a channel."""
            sp.cast(params, post_message_params_type)
            self._check_not_paused()

            assert sp.len(params.content) > 0, "EMPTY_CONTENT"
            assert sp.len(params.content) <= 32768, "CONTENT_TOO_LARGE"
            assert params.channel_id in self.data.channels, "CHANNEL_NOT_FOUND"

            channel = self.data.channels[params.channel_id]
            assert not channel.hidden, "CHANNEL_HIDDEN"

            # Validate parent message if replying
            if params.parent_id.is_some():
                parent_msg_id = params.parent_id.unwrap_some()
                assert parent_msg_id in self.data.messages, "PARENT_NOT_FOUND"
                parent_msg = self.data.messages[parent_msg_id]
                assert parent_msg.channel_id == params.channel_id, "PARENT_WRONG_CHANNEL"

            self._check_can_post(sp.record(channel_id=params.channel_id, proof=params.proof))

            # Fee
            assert sp.amount == self.data.message_fee, "INCORRECT_FEE"
            self._send_fee(self.data.message_fee)

            message_id = self._write_message(sp.record(channel_id=params.channel_id, content=params.content, parent_id=params.parent_id))

            # Note: `content` is intentionally NOT emitted in the event so that
            # `delete_message` actually deletes — events are part of permanent block
            # history. Indexers should read content from the `messages` big_map.
            sp.emit(
                sp.record(
                    channel_id=params.channel_id,
                    message_id=message_id,
                    sender=sp.sender,
                    parent_id=params.parent_id,
                    timestamp=sp.now,
                ),
                tag="message_posted",
            )

        @sp.entrypoint
        def delete_message(self, message_id):
            """Delete a message. In closed channels: sender only.
            In unrestricted/allowlist channels: sender or channel creator."""
            sp.cast(message_id, sp.nat)
            self._check_not_paused()
            self._check_no_tez_transfer()

            assert message_id in self.data.messages, "MESSAGE_NOT_FOUND"
            message = self.data.messages[message_id]

            assert message.channel_id in self.data.channels, "CHANNEL_NOT_FOUND"
            channel = self.data.channels[message.channel_id]

            if channel.access_mode.is_variant.closed():
                assert message.sender == sp.sender, "NOT_AUTHORIZED"
            else:
                is_sender = message.sender == sp.sender
                is_creator = channel.creator == sp.sender
                assert is_sender or is_creator, "NOT_AUTHORIZED"

            self.data.channels[message.channel_id] = sp.record(
                creator=channel.creator,
                metadata_uri=channel.metadata_uri,
                access_mode=channel.access_mode,
                merkle_root=channel.merkle_root,
                merkle_uri=channel.merkle_uri,
                message_count=sp.as_nat(channel.message_count - 1, error="UNDERFLOW"),
                hidden=channel.hidden,
                timestamp=channel.timestamp,
            )

            del self.data.messages[message_id]

            sp.emit(
                sp.record(
                    channel_id=message.channel_id,
                    message_id=message_id,
                    deleted_by=sp.sender,
                ),
                tag="message_deleted",
            )

        @sp.entrypoint
        def update_channel(self, params):
            """Update channel metadata. Creator or admin."""
            sp.cast(params, sp.record(channel_id=sp.nat, metadata_uri=sp.bytes))
            self._check_not_paused()
            self._check_no_tez_transfer()

            assert params.channel_id in self.data.channels, "CHANNEL_NOT_FOUND"
            channel = self.data.channels[params.channel_id]
            self._check_channel_admin(params.channel_id)
            assert sp.len(params.metadata_uri) > 0, "EMPTY_METADATA"

            self.data.channels[params.channel_id] = sp.record(
                creator=channel.creator,
                metadata_uri=params.metadata_uri,
                access_mode=channel.access_mode,
                merkle_root=channel.merkle_root,
                merkle_uri=channel.merkle_uri,
                message_count=channel.message_count,
                hidden=channel.hidden,
                timestamp=channel.timestamp,
            )

            sp.emit(
                sp.record(
                    channel_id=params.channel_id,
                    metadata_uri=params.metadata_uri,
                    updated_by=sp.sender,
                ),
                tag="channel_updated",
            )

        @sp.entrypoint
        def hide_channel(self, channel_id):
            """Hide a channel (soft delete). Creator only."""
            sp.cast(channel_id, sp.nat)
            self._check_not_paused()
            self._check_no_tez_transfer()

            assert channel_id in self.data.channels, "CHANNEL_NOT_FOUND"
            channel = self.data.channels[channel_id]
            assert channel.creator == sp.sender, "NOT_CHANNEL_CREATOR"

            self.data.channels[channel_id] = sp.record(
                creator=channel.creator,
                metadata_uri=channel.metadata_uri,
                access_mode=channel.access_mode,
                merkle_root=channel.merkle_root,
                merkle_uri=channel.merkle_uri,
                message_count=channel.message_count,
                hidden=True,
                timestamp=channel.timestamp,
            )

            sp.emit(
                sp.record(
                    channel_id=channel_id,
                    hidden_by=sp.sender,
                ),
                tag="channel_hidden",
            )

        # =======================================================================
        # Governance Entrypoints
        # =======================================================================

        @sp.entrypoint
        def submit_proposal(self, action):
            """Submit a new proposal."""
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
            """Vote on an existing proposal."""
            sp.cast(vote, vote_params_type)
            self._check_no_tez_transfer()
            self._check_is_user()
            assert vote.proposal_id in self.data.proposals, "CHANNELS_NO_PROPOSAL"

            proposal = self.data.proposals[vote.proposal_id]
            assert not proposal.executed, "CHANNELS_ALREADY_EXECUTED"
            self._check_not_expired(proposal.timestamp)

            vote_key = (vote.proposal_id, sp.sender)

            if vote_key in self.data.votes:
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
            """Execute an approved proposal."""
            sp.cast(proposal_id, sp.nat)
            self._check_no_tez_transfer()
            self._check_is_user()
            assert proposal_id in self.data.proposals, "CHANNELS_NO_PROPOSAL"

            proposal = self.data.proposals[proposal_id]
            assert not proposal.executed, "CHANNELS_ALREADY_EXECUTED"
            minimum_votes = self._get_minimum_votes()
            assert proposal.positive_votes >= minimum_votes, "CHANNELS_NOT_ENOUGH_VOTES"
            self._check_not_expired(proposal.timestamp)

            proposal.executed = True
            self.data.proposals[proposal_id] = proposal

            if proposal.action.is_variant.set_pause():
                self.data.paused = proposal.action.unwrap.set_pause()

            if proposal.action.is_variant.set_message_fee():
                self.data.message_fee = proposal.action.unwrap.set_message_fee()

            if proposal.action.is_variant.set_channel_fee():
                self.data.channel_fee = proposal.action.unwrap.set_channel_fee()

            if proposal.action.is_variant.set_fee_recipient():
                self.data.fee_recipient = proposal.action.unwrap.set_fee_recipient()

            if proposal.action.is_variant.update_multisig_address():
                self.data.multisig_address = proposal.action.unwrap.update_multisig_address()

            if proposal.action.is_variant.update_metadata():
                entry = proposal.action.unwrap.update_metadata()
                self.data.metadata[entry.key] = entry.value

        # =======================================================================
        # Views
        # =======================================================================

        @sp.onchain_view()
        def get_channel(self, channel_id):
            """Get channel by ID."""
            sp.cast(channel_id, sp.nat)
            assert channel_id in self.data.channels, "CHANNEL_NOT_FOUND"
            return self.data.channels[channel_id]

        @sp.onchain_view()
        def get_message(self, message_id):
            """Get message by ID."""
            sp.cast(message_id, sp.nat)
            assert message_id in self.data.messages, "MESSAGE_NOT_FOUND"
            return self.data.messages[message_id]

        @sp.onchain_view()
        def get_message_fee(self):
            """Get the current message fee."""
            return self.data.message_fee

        @sp.onchain_view()
        def get_channel_fee(self):
            """Get the current channel creation fee."""
            return self.data.channel_fee

        @sp.onchain_view()
        def is_channel_admin(self, params):
            """Check if an address is a channel admin (or creator)."""
            sp.cast(params, channel_admin_key_type)
            assert params.channel_id in self.data.channels, "CHANNEL_NOT_FOUND"
            channel = self.data.channels[params.channel_id]
            return channel.creator == params.address or params.address in self.data.channel_admins[params.channel_id]

        @sp.onchain_view()
        def get_proposal(self, proposal_id):
            """Get proposal by ID."""
            sp.cast(proposal_id, sp.nat)
            assert proposal_id in self.data.proposals, "CHANNELS_NO_PROPOSAL"
            return self.data.proposals[proposal_id]

        @sp.onchain_view()
        def get_vote(self, key):
            """Get vote by key (proposal_id, voter)."""
            sp.cast(key, vote_key_type)
            return self.data.votes.get(key, default=False)



# ==============================================================================
# Deployment Scenario
# ==============================================================================



@sp.add_test()
def channel_deploy_shadownet():
    """Deployment scenario."""
    scenario = sp.test_scenario("channel_deploy_shadownet", channels_module)
    scenario.h1("Channel Merkle - Deployment Shadownet")

    MULTISIG_ADDRESS = sp.address("KT1KeGd4YtjcKqgyiXUPJQkm2iYA3fQwLGQP")
    FEE_RECIPIENT_ADDRESS = sp.address("KT1KeGd4YtjcKqgyiXUPJQkm2iYA3fQwLGQP")
    MESSAGE_FEE = sp.mutez(100000)
    CHANNEL_FEE = sp.mutez(100000)

    contract_metadata = sp.big_map(
        {
            "": sp.scenario_utils.bytes_of_string("ipfs://aaa"),
        }
    )

    contract = channels_module.Channels(
        multisig_address=MULTISIG_ADDRESS,
        fee_recipient=FEE_RECIPIENT_ADDRESS,
        message_fee=MESSAGE_FEE,
        channel_fee=CHANNEL_FEE,
        metadata=contract_metadata,
        counter=sp.nat(0),
    )
    scenario += contract
