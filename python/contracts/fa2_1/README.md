# Teia Minter v2 — open-mint, multi-edition FA2.1 (two-contract system)

Teia's own minting contracts. Self-contained TZIP-12 (FA2) contracts written from
scratch in modern SmartPy (v0.17+). The system follows the FA2.1 conventions by
exposing the full token state through on-chain views.

The system is split into **two contracts** (mirroring HEN):

```
        users ──mint(fee)──▶  Teia Minter  ──mint()──▶  Teia FA2 token
                              (upgradeable,            (permanent address,
                               FA2 administrator)       holds all token state)
        Core Team multisig ──governance──▶ Minter ──admin calls──▶ FA2
```

| File | Contract | Role |
|------|----------|------|
| [`teia_fa2.py`](teia_fa2.py) | `TeiaFA2` | Permanent token — holds all token state |
| [`teia_minter_v2.py`](teia_minter_v2.py) | `TeiaMinter` | Upgradeable open-mint orchestrator / FA2 administrator |

---

## Design rules

1. **Open minting** — `mint` (on the minter) is permissionless. Anyone may mint a
   token of N editions by attaching exactly `mint_fee`. The fee is forwarded to
   `fee_recipient` (Teia treasury) and the mint is forwarded to the FA2, which
   assigns the token id, enforces the invariants (`editions > 0`, royalty shares
   valid, not paused) and emits the canonical `token_minted` event.
2. **Multi-edition / "unlimited"** — `editions` is a `nat` with no upper bound
   (only `editions > 0`), matching the HEN minter.
3. **On-chain royalties (collab-ready)** — each token stores a `shares` map of
   recipient address → share of the sale price in per mille. Total capped at 25%
   (250 per mille), at most 10 recipients. Royalties are only **exposed via
   views**
4. **Burn** — holders (or operators) may burn editions they own.
5. **Multisig governance** — delegated to the **Teia Core Team multisig** (queried
   via `is_user`, `get_minimum_votes`, `get_expiration_time`). Proposals are
   stored and voted on locally; only multisig users may submit / vote / execute.

---

## Contract 1 — `teia_fa2.py`

Holds **all token state**, and `mint` is **administrator-only** and enforces the
token invariants itself (defense-in-depth, the permanent contract stays
authoritative even under a buggy or replaced minter).

### Entrypoints

| Entrypoint               | Access            | Description                                          |
|--------------------------|-------------------|-----------------------------------------------------|
| `transfer`               | owner / operator  | Standard FA2 batched transfer (blocked when paused) |
| `update_operators`       | token owner       | Add / remove operators                              |
| `balance_of`             | anyone            | Standard FA2 callback-based balance query           |
| `burn`                   | owner / operator  | Burn owned editions                                 |
| `mint`                   | **administrator** | Mint N editions (called by the minter)              |
| `set_pause`              | administrator     | Pause / unpause (halts transfer **and** mint)       |
| `set_metadata`           | administrator     | Update a contract (TZIP-16) metadata entry          |
| `transfer_administrator` | administrator     | Propose a new administrator (e.g. a new minter)     |
| `accept_administrator`   | proposed admin    | Accept the administrator role                        |

All entrypoints reject attached tez (`TEZ_TRANSFER`), there is no withdraw, so
this prevents locked funds. The original minter (the account that initiated the
call) is recorded in the `token_minted` event via `sp.source`.

### Views

`get_balance`, `get_balance_of`, `total_supply`, `all_tokens`, `count_tokens`,
`token_exists`, `is_operator`, `get_token_metadata`, `get_token_data`,
`get_token_royalties`, `is_paused`, `get_administrator`, and the off-chain
`get_balance_offchain`.

### Storage

```
administrator          : address                          # the minter contract
proposed_administrator : option[address]
paused                 : bool
metadata               : big_map[string, bytes]           # TZIP-16
ledger                 : big_map[(address, nat), nat]      # (owner, token_id) -> balance
operators              : big_map[(owner, operator, token_id), unit]
token_metadata         : big_map[nat, {token_id, token_info}]
token_data             : big_map[nat, map[string, bytes]]  # arbitrary on-chain data
token_royalties        : big_map[nat, map[address, nat]]   # token_id -> shares
supply                 : big_map[nat, nat]                 # token_id -> live editions
next_token_id          : nat
```

---

## Contract 2 — `teia_minter_v2.py`

The FA2's `administrator`. Mint logic, fee and governance live here.

### Entrypoints

| Entrypoint                 | Access         | Description                                          |
|----------------------------|----------------|-----------------------------------------------------|
| `mint`                     | **anyone**     | Mint N editions; must attach exactly `mint_fee`      |
| `accept_fa2_administrator` | multisig user  | Make the minter the FA2 administrator (setup/upgrade)|
| `submit_proposal`          | multisig user  | Submit a governance proposal                        |
| `vote_proposal`            | multisig user  | Approve / reject a proposal                          |
| `execute_proposal`         | multisig user  | Execute once `positive_votes >= minimum_votes`      |

**Proposal actions** — minter-local: `set_mint_fee(mutez)`,
`set_fee_recipient(address)`, `update_multisig_address(address)`,
`update_metadata({key,value})`. Executed on the FA2 (the minter is its admin):
`set_fa2_pause(bool)`, `set_fa2_metadata({key,value})`,
`transfer_fa2_administrator(address)` ← **the upgrade path to a new minter**.

### Views

`get_mint_fee`, `get_fee_recipient`, `get_fa2`, `get_multisig_address`,
`get_proposal`, `get_vote`. (Token/pause views live on the FA2.)

### Storage

```
fa2              : address                          # the governed FA2 token
multisig_address : address
fee_recipient    : address                          # Teia treasury
mint_fee         : mutez
proposals        : big_map[nat, proposal]
votes            : big_map[(nat, address), bool]
counter          : nat                              # next proposal id
metadata         : big_map[string, bytes]           # TZIP-16
```

---

## `mint` parameters (minter → FA2)

```python
sp.record(
    to_      = address,                  # recipient of the editions
    editions = nat,                      # > 0
    metadata = map[string, bytes],       # FA2 token metadata (usually "" -> IPFS)
    data     = map[string, bytes],       # arbitrary on-chain data (intl. codes…)
    shares   = map[address, nat],        # royalty recipient -> per mille of sale
)
```

### Royalty `shares`

Map of recipient address → share of the sale price, in per mille (100 = 10%).
- single artist — `{tz_artist: 100}` (10%)
- native split — `{tz_a: 50, tz_b: 50}` (10% total)
- legacy collab — `{KT_collab: 100}` (10% to an `ArtistsCollaboration` contract)
- no royalties — `{}` (allowed)

Validation (enforced by the FA2): every share `> 0`, at most 10 recipients, and
`sum(shares) <= 250` (25% cap).

### On-chain data slots — international IDs & codes

The `data` map is arbitrary on-chain data under free-form keys, stored in the
FA2's `token_data` and queryable via its `get_token_data` view:

| Key    | Code |
|--------|------|
| `isrc` | International Standard Recording Code (audio) |
| `iswc` | International Standard Musical Work Code |
| `iscc` | International Standard Content Code |
| `isbn` | International Standard Book Number |
| `isan` | International Standard Audiovisual Number |
| `ean`  | European Article Number |

Keys are free-form (no fixed schema), so new code types never require a contract
upgrade. Values are raw `bytes` (UTF-8 strings). `data` may be empty (`{}`).

> ⚠️ On-chain data is **public and immutable** — never store personal/sensitive
> data (bank details, etc.) here. `data` is currently set once at mint; an
> updatable variant can be added if codes need to be assigned after minting.

---

## Fees

`mint` requires `sp.amount == mint_fee` and forwards it to `fee_recipient` (only
when non-zero), so no tez accumulates. The starting fee is **0.1 XTZ**
(`sp.mutez(100000)`); the multisig can change it (incl. to 0) by proposal.

---

## Deployment

Both `teia_minter_v2_deploy_shadownet` / `_deploy_mainnet` scenarios originate
**both** contracts and start the admin handoff:

| Scenario | Multisig | Mint fee | Fee recipient |
|----------|----------|----------|---------------|
| shadownet | `KT1KeGd4YtjcKqgyiXUPJQkm2iYA3fQwLGQP` | 0.1 XTZ | placeholder (TODO) |
| mainnet   | `KT1J9FYz29RBQi1oGLw8uXyACrzXzV1dHuvb` (Core Team multisig) | 0.1 XTZ | placeholder (TODO) |

**Origination sequence (resolves the admin chicken-and-egg):**
1. Originate **`TeiaFA2`** with `administrator = deployer`.
2. Originate **`TeiaMinter`** with `fa2 = FA2.address`, multisig, `mint_fee`,
   `fee_recipient`.
3. `FA2.transfer_administrator(minter)` — called by the deployer.
4. `minter.accept_fa2_administrator()` — called by a multisig user. The minter is
   now the sole FA2 administrator; the deployer is powerless.

> **Before mainnet:** (1) upload contract metadata to IPFS and replace the
> placeholder hash; (2) set `FEE_RECIPIENT_ADDRESS` to the confirmed **Teia
> treasury** address; (3) confirm the starting `MINT_FEE` (0.1 XTZ default).

A live Shadownet test deployment (with addresses and on-chain verification) is
recorded in [`SHADOWNET_DEPLOYMENT.md`](SHADOWNET_DEPLOYMENT.md).

---

## Build & test

```bash
python3 -m venv smartpy-env
source smartpy-env/bin/activate
pip install smartpy-tezos

python3 python/contracts/fa2_1/teia_fa2.py        # token contract
python3 python/contracts/fa2_1/teia_minter_v2.py  # minter (imports teia_fa2)
```

Exit code `0` means compilation and all tests passed. The `.contains()`
deprecation warnings come from SmartPy internals and are harmless. The minter
test originates the real FA2 + a mock multisig, performs the admin handoff, then
covers open minting with fee forwarding, fee/invariant rejections, the full
proposal → vote → execute governance flow (every proposal action), and the
**upgrade path** (hand the FA2 to a new minter and confirm the old one can no
longer mint).

---

## Security

A pre-audit security review is documented in
[`SECURITY_REVIEW.md`](SECURITY_REVIEW.md) (locked-tez guard fixed; full
inter-contract test coverage; governance footguns documented).
