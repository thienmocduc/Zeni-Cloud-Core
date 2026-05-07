# ZeniAccessControl

On-chain governance contract that gates Zeni Cloud admin access to customer data.
Deployed behind a UUPS proxy on Polygon. All access events are public and auditable on polygonscan.

## Why this exists

Zeni Cloud admins must NOT be able to read a customer's workspace data unless one of the following
is true on-chain:

1. **Customer Support** — the customer signed an approval transaction with their own wallet,
   granting a time-boxed access window between **6 and 24 hours**.
2. **Legal Authority** — at least **3 of 5** legal-team wallets co-signed an emergency request
   that includes a court order hash committed on-chain.

Customers, the originating admin, or the chairman can revoke any active or pending request at any
time. Approvals automatically expire after the requested duration.

## Architecture

```
                          +-------------------+
                          |   Chairman wallet |
                          +---------+---------+
                                    |
                       addAdmin / addLegalSigner / transferChairman
                                    |
+---------------------+   request   v   approveByCustomer    +-------------------+
|  Zeni Admin wallet  | ----------> ZeniAccessControl <----- |  Customer wallet  |
+---------------------+             (UUPS proxy)             +-------------------+
                                    ^   ^
                       3-of-5 sigs  |   |  revokeAccess
                                    |   |
                          +---------+---+----+
                          |  Legal Multisig  |
                          |  (5 signers)     |
                          +------------------+
```

Workflow:

* **Customer support flow**

  1. Admin calls `requestAccess(customer, scope, ticketRef, duration)` → status `Pending`.
  2. Customer calls `approveByCustomer(requestId)` → status `Approved`, expires at `now + duration`.
  3. Anyone (customer / admin / chairman) calls `revokeAccess(requestId)` to end early.

* **Legal authority flow (emergency)**

  1. Admin calls `requestAccess(...)` to open a `Pending` request (or it can already exist).
  2. Three different legal signers call `emergencyApprove(requestId, courtOrderHash)`.
     The first signer locks `courtOrderHash` on-chain; later signers must match.
  3. On the third signature, status flips to `Approved`, `reason = LegalAuthority`,
     `courtOrderHash` is permanent, and the access window starts.

## Files

| Path | Purpose |
|------|---------|
| `ZeniAccessControl.sol` | Main contract (UUPS upgradeable). |
| `test/ZeniAccessControl.test.js` | Hardhat / Mocha / Chai test suite (>95% line coverage target). |
| `hardhat.config.js` | Solidity 0.8.20, optimizer 200 runs, hardhat / mumbai / polygon networks. |
| `scripts/deploy.js` | Deploys via UUPS proxy + wires admins and legal signers. |
| `package.json` | NPM scripts: compile, test, coverage, deploy, verify. |
| `.env.example` | Template for required env vars. |
| `.gitignore` | Excludes secrets and build artifacts. |

## Quickstart

```bash
cd C:\Users\Admin\Documents\Zeni-Cloud-Core\contracts
npm install
cp .env.example .env       # fill in DEPLOYER_PRIVATE_KEY etc.

# compile
npm run compile

# run tests (no chain dependencies)
npm test

# coverage report
npm run coverage

# deploy to mumbai testnet
npm run deploy:mumbai

# deploy to polygon mainnet
npm run deploy:polygon
```

## Interacting with the contract

All write functions revert with custom errors for cheap gas; below are typical interactions
expressed in ethers v6:

```js
// Admin requests access to workspace_id "ws_42"
const scope = ethers.keccak256(ethers.toUtf8Bytes("ws_42"));
await zac.connect(admin).requestAccess(
  customer.address,
  scope,
  "TICKET-2026-001",
  12 * 60 * 60                     // 12 hours
);

// Customer approves
await zac.connect(customer).approveByCustomer(1);

// Frontend / backend gate
const ok = await zac.isAccessActive(1);    // true while window open

// Anyone authorized can revoke
await zac.connect(customer).revokeAccess(1);
```

Emergency (legal) flow:

```js
const courtHash = ethers.keccak256(
  ethers.toUtf8Bytes("court-order-2026-001-pdf-sha256")
);
await zac.connect(legal1).emergencyApprove(reqId, courtHash);
await zac.connect(legal2).emergencyApprove(reqId, courtHash);
await zac.connect(legal3).emergencyApprove(reqId, courtHash); // flips Approved
```

## Roles

| Role | Granted via | Power |
|------|-------------|-------|
| Chairman | `initialize()` then `transferChairman()` | Add/remove admins, add/remove legal signers, force-revoke any request, authorize UUPS upgrades. |
| Admin | `addAdmin()` (chairman only) | File access requests, revoke own requests. |
| Legal signer | `addLegalSigner()` (chairman only) | Sign emergency approvals (3-of-5 threshold). |
| Customer | implicit via wallet ownership | Approve or revoke any request that names them. |

## Security & audit

The contract is intentionally small (~400 LOC) and avoids:

* External calls to other contracts (no reentrancy attack surface beyond the OZ guard).
* Token transfers (no value leaves the contract).
* Loops over unbounded sets.

Recommended pre-mainnet steps:

* **Static analysis**: `slither .`
* **Unit + invariant tests**: included; aim for `>=95%` line coverage.
* **Formal review**: Trail of Bits or OpenZeppelin Defender Audit.
* **Multisig the chairman**: deploy the chairman role behind a Gnosis Safe (e.g. 2-of-3).
* **Timelock upgrades**: optionally wrap `_authorizeUpgrade` with an OZ TimelockController.

## Already-deployed Zeni contracts (Polygon mainnet)

These are the related Zeni Polygon contracts the Zeni Cloud product already ships against:

* `$ZENI Token` — `0x2d0Ec889F3889F0a364b82039db9F8Bef78f5EC1`
* `AffiliateCommission` — `0x1d5963FcCfC548275293e51f0F6C7aC482E0b714`
* `ZeniBadge (SBT)` — `0xB157c83beEeA7c7ebDB2CEa305135e3deCAeD79D`
* Deployer — `0x76ABe9d6252e1e151c039F66de19DEa5d8E7CE91`

`ZeniAccessControl` is independent of those; it gates **off-chain** data, not token logic.
