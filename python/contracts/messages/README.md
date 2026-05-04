# Teia Messaging Network Token

Two smart contracts for on-chain messaging on Tezos. The Channels contract has its full reference inline below; Token Gated Chat has a brief summary (spec to follow).

## Contracts at a glance

### 1. Channels (`channels.py`)

Public or private community channels with three-tier access control.

- Anyone can create a channel
- Three access modes:
  - **Unrestricted**, anyone can post
  - **Allowlist**, only addresses with a valid Merkle proof against `merkle_root` (scales to thousands)
  - **Closed**, only creator and admins; intended for DMs and small private rooms
- Channel creator can add admins; admins can configure access, update metadata, and post freely
- Message deletion: in `closed` channels, sender only; in `unrestricted`/`allowlist` channels, sender or creator
- Only the creator can add/remove admins or hide the channel (soft delete)
- Message replies via `parent_id`

Full reference: see [Channels v1, full reference](#channels-v1-full-reference) below.

### 2. Token Gated Chat (`token_gate.py`)

Chat rooms for FA2 token holders.

- Every token gets a chat room automatically (no need to create one)
- Room key = FA2 contract address + token ID
- To post, the contract checks if you hold the token via FA2 `balance_of` callback
- Rooms are created on first message
- Only the message sender can delete their own messages (moderation via multisig, maybe every multisig wallet can hide/delete messages and ban wallets)
- Message replies via `parent_id`

## Shared features

Both contracts share:

- **Message fees**, configurable fee per message
- **Multisig governance**, pause contract, change fees, update metadata
- **Events**, all actions emit events with full data for frontend indexing
- **Reply support**, messages can reference a parent message

## Build & test

Depends on your setup, more info soon.


```bash
source smartpy-env/bin/activate
python python/contracts/messages/channels.py
python python/contracts/messages/token_gate.py
```

Compiled artifacts (Michelson + initial storage) land in the corresponding deploy folders next to the repo root.

---

# Channels

On-chain community channels with three-tier access control and optional Merkle-tree allowlists. Implemented in SmartPy at [`channels.py`](./channels.py).

## Access model

Three roles, per channel:

| Role | Set by | What they can do |
|---|---|---|
| **Creator** | whoever calls `create_channel` (immutable) | everything below + `update_channel_admins`, `hide_channel` |
| **Admin** | creator, via `update_channel_admins` | `configure_channel`, `update_channel`, `delete_message`, post freely |
| **User** (Merkle member) | proof against `merkle_root` set by creator/admin | post in allowlist channels |

Three access modes:

- `unrestricted`, anyone may post
- `allowlist`, only creator, admins, or addresses with a valid Merkle proof against `channel.merkle_root`
- `closed`, only creator and admins; no Merkle path. Intended for DMs and small private peer rooms

`hide_channel` the channel record stays but `post_message` and `configure_channel` reject it.

### Closed mode and DMs

`closed` direct messages: a 2-person DM is created with `access_mode=closed`, no `merkle_root`, and the peer added as the single admin. Two semantics distinguish it from the others:

- **Posting** is restricted to the privileged set (creator + admins). Non-privileged callers get `NOT_AUTHORIZED` with no Merkle path to override it.
- **Deletion** is sender-only: the creator cannot delete an admin/peer's message in a closed channel. In `unrestricted` and `allowlist` channels, the creator retains delete power over any message (sender-or-creator rule).

Switching modes is allowed in both directions. Switching out of `closed` (e.g., to `allowlist`) re-grants the creator delete power over any prior peer message, by design, but worth communicating to participants.

## Storage

```
multisig_address     : address                                # governance contract
metadata             : big_map[string, bytes]                 # TZIP-16
paused               : bool                                   # global pause flag (governance-set)
channels             : big_map[nat, channel]                  # channel records
messages             : big_map[nat, message]                  # flat message store
channel_admins       : big_map[nat, set[address]]             # per-channel admin set (≤ 15 admins per channel)
channel_id_counter   : nat                                    # next channel_id (starts at 1)
message_id_counter   : nat                                    # next message_id (starts at 1)
message_fee          : mutez                                  # required tez on post_message
channel_fee          : mutez                                  # required tez on create_channel
fee_recipient        : address
proposals            : big_map[nat, proposal]                 # governance proposals
votes                : big_map[(nat, address), bool]          # governance votes
counter              : nat                                    # proposal counter
```

### `channel` record

```
creator        : address
metadata_uri   : bytes                  # e.g. IPFS
access_mode    : variant(unrestricted | allowlist | closed)
merkle_root    : option[bytes]          # required iff access_mode = allowlist
merkle_uri     : option[bytes]          # off-chain pointer to the full address list
message_count  : nat                    # incremented on post, decremented on delete
hidden         : bool
timestamp      : timestamp              # creation time
```

### `message` record

```
channel_id  : nat
sender      : address
content     : bytes                     # ≤ 32768 bytes (32 KiB)
parent_id   : option[nat]               # for threaded replies
timestamp   : timestamp
```

## Entrypoints

### Channel ops

#### `create_channel(metadata_uri, access_mode, merkle_root, merkle_uri, admins)`

| | |
|---|---|
| Caller | anyone |
| Tez | exactly `channel_fee` (or 0 if fee is 0) |
| Behavior | spawns a new channel with sender as creator, full config applied at creation, optional initial admin set written into `channel_admins` |
| Validation | `metadata_uri` non-empty; `access_mode = allowlist ⇔ merkle_root.is_some()` |
| Notes | `admins` is `list[address]`; pass `[]` for none |
| Event | `channel_created { channel_id, creator, metadata_uri, access_mode, merkle_root, merkle_uri, admins, timestamp }` |

#### `configure_channel(channel_id, access_mode, merkle_root, merkle_uri)`

| | |
|---|---|
| Caller | creator or admin |
| Tez | 0 |
| Behavior | updates the access mode, Merkle root and Merkle URI |
| Restrictions | rejects if channel is hidden |
| Event | `channel_configured { channel_id, access_mode, merkle_root, merkle_uri, configured_by }` |

#### `update_channel(channel_id, metadata_uri)`

| | |
|---|---|
| Caller | creator or admin |
| Tez | 0 |
| Behavior | replaces the channel `metadata_uri` |
| Validation | `metadata_uri` non-empty |
| Event | `channel_updated { channel_id, metadata_uri, updated_by }` |

#### `update_channel_admins(channel_id, to_add, to_remove)`

| | |
|---|---|
| Caller | creator only |
| Tez | 0 |
| Behavior | adds/removes addresses from the per-channel admin set |
| Event | `channel_admins_updated { channel_id, to_add, to_remove }` |

#### `hide_channel(channel_id)`

| | |
|---|---|
| Caller | creator only |
| Tez | 0 |
| Behavior | sets `hidden = true`; the channel becomes read-only |
| Event | `channel_hidden { channel_id, hidden_by }` |

### Message ops

#### `post_message(channel_id, content, proof, parent_id)`

| | |
|---|---|
| Caller | creator, admin, or any address (subject to `access_mode`) |
| Tez | exactly `message_fee` |
| Behavior | appends a message; if `parent_id` is set it must point to a message in the same channel |
| Access | unrestricted: anyone. allowlist: creator/admin bypass; everyone else must supply a valid `proof` against `channel.merkle_root`. closed: creator/admin only, all others rejected with `NOT_AUTHORIZED` |
| Validation | `1 ≤ len(content) ≤ 32768`; channel exists and is not hidden |
| Event | `message_posted { channel_id, message_id, sender, parent_id, timestamp }` (content is intentionally not emitted, read it from the `messages` big_map so `delete_message` actually deletes) |

#### `delete_message(message_id)`

| | |
|---|---|
| Caller | depends on the channel's `access_mode`: closed → message sender only; unrestricted/allowlist → message sender or channel creator |
| Tez | 0 |
| Behavior | deletes the message and decrements `channel.message_count` |
| Event | `message_deleted { channel_id, message_id, deleted_by }` |

### Governance (multisig users only)

The contract delegates user-management and quorum to the multisig at `multisig_address` via three views: `is_user`, `get_minimum_votes`, `get_expiration_time`.

#### `submit_proposal(action)`

`action` is a variant:

| Tag | Payload | Effect on execute |
|---|---|---|
| `set_pause` | `bool` | sets global `paused` |
| `set_message_fee` | `mutez` | updates `message_fee` |
| `set_channel_fee` | `mutez` | updates `channel_fee` |
| `set_fee_recipient` | `address` | updates `fee_recipient` |
| `update_multisig_address` | `address` | swaps the multisig governance contract |
| `update_metadata` | `{key: string, value: bytes}` | writes to TZIP-16 metadata big_map |

#### `vote_proposal(proposal_id, approval)`

Multisig users vote yes/no. Re-voting overwrites the previous vote (the prior tally is decremented before applying the new one).

#### `execute_proposal(proposal_id)`

Any multisig user. Requires `positive_votes ≥ get_minimum_votes()` from the multisig and the proposal not to be expired.

## Views (on-chain)

| View | Returns |
|---|---|
| `get_channel(channel_id: nat)` | `channel` record |
| `get_message(message_id: nat)` | `message` record |
| `get_message_fee()` | `mutez` |
| `get_channel_fee()` | `mutez` |
| `is_channel_admin({channel_id, address})` | `bool` (true for creator or admin) |
| `get_proposal(proposal_id: nat)` | `proposal` record |
| `get_vote((proposal_id, voter))` | `bool` (default false) |

## Building a Merkle proof (allowlist channels)

The on-chain leaf format is **`blake2b(pack(address))`**, keyed only on the address (no per-channel salt, no namespace).

Off-chain steps:

1. Collect the list of allowed addresses, deduplicate, and sort (any deterministic order works as long as the tree is rebuilt the same way every time).
2. Compute each leaf as `blake2b(pack(address))`. SmartPy's `pack` for an address produces the standard Michelson `0x05...` encoding, most Tezos client libraries expose this as `packAddress` or similar.
3. Build a binary Merkle tree using `blake2b(left || right)` for internal nodes. Pair from the bottom up; if a level has an odd count, duplicate the last node (or use whichever convention you prefer, just be consistent).
4. The **root** is the last remaining hash; store it on-chain via `merkle_root`. Publish the address list at `merkle_uri` (typically IPFS).
5. To prove inclusion of an address, walk from its leaf to the root and emit one `merkle_step` per level:
   - `direction = 0` if the **sibling** is on the **left** (so on-chain we compute `blake2b(sibling || computed)`)
   - `direction = 1` if the **sibling** is on the **right** (`blake2b(computed || sibling)`)
6. Submit `proof = [merkle_step, ...]` along with `post_message`.

Smallest case: a single-address tree has `merkle_root = blake2b(pack(address))` and the proof is `Some([])`, empty list.


## Error codes

See the module docstring at the top of [`channels.py`](./channels.py) for the full list, grouped by area:

- channel/message ops (`CONTRACT_PAUSED`, `INCORRECT_FEE`, `CHANNEL_NOT_FOUND`, `CHANNEL_HIDDEN`, `EMPTY_CONTENT`, `CONTENT_TOO_LARGE`, `PARENT_NOT_FOUND`, `PARENT_WRONG_CHANNEL`, `MESSAGE_NOT_FOUND`, `UNDERFLOW`, `EMPTY_METADATA`, `TEZ_TRANSFER`)
- authorization (`NOT_AUTHORIZED`, `NOT_CHANNEL_CREATOR`, `NOT_CHANNEL_ADMIN`)
- access config (`ALLOWLIST_NEEDS_ROOT`, `ROOT_NOT_ALLOWED`, `NO_MERKLE_ROOT`, `PROOF_REQUIRED`, `INVALID_MERKLE_PROOF`)
- governance (`CHANNELS_VIEW_FAILED`, `CHANNELS_NOT_MULTISIG_USER`, `CHANNELS_GET_MIN_VOTES_FAILED`, `CHANNELS_GET_EXPIRATION_FAILED`, `CHANNELS_PROPOSAL_EXPIRED`, `CHANNELS_NO_PROPOSAL`, `CHANNELS_ALREADY_EXECUTED`, `CHANNELS_NOT_ENOUGH_VOTES`)

## Indexer notes

Every state mutation emits a typed event, so events can be decoded directly from contract type info. The full set of event tags:

`channel_created`, `channel_configured`, `channel_updated`, `channel_admins_updated`, `channel_hidden`, `message_posted`, `message_deleted`.

`update_channel_admins` and `delete_message` are the only mutations that don't include the resulting state, for the admin set you must replay all `channel_admins_updated` events; for a deleted message, `messages[id]` will be absent in storage.
