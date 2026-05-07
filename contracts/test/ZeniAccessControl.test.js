// SPDX-License-Identifier: MIT
const { expect } = require("chai");
const { ethers, upgrades } = require("hardhat");
const { time } = require("@nomicfoundation/hardhat-network-helpers");

describe("ZeniAccessControl", function () {
  // ----- Constants -----
  const SIX_HOURS = 6 * 60 * 60;
  const TWELVE_HOURS = 12 * 60 * 60;
  const TWENTY_FOUR_HOURS = 24 * 60 * 60;
  const FIVE_HOURS = 5 * 60 * 60;
  const TWENTY_FIVE_HOURS = 25 * 60 * 60;

  const Status = { Pending: 0, Approved: 1, Revoked: 2, Expired: 3 };
  const Reason = { CustomerSupport: 0, LegalAuthority: 1 };

  // ----- Fixtures -----
  async function deployFixture() {
    const [
      chairman,
      admin1,
      admin2,
      customer1,
      customer2,
      legal1,
      legal2,
      legal3,
      legal4,
      legal5,
      stranger,
    ] = await ethers.getSigners();

    const Factory = await ethers.getContractFactory("ZeniAccessControl");
    const proxy = await upgrades.deployProxy(Factory, [chairman.address], {
      kind: "uups",
      initializer: "initialize",
    });
    await proxy.waitForDeployment();

    // Wire admin + legal signers
    await proxy.connect(chairman).addAdmin(admin1.address);
    await proxy.connect(chairman).addAdmin(admin2.address);
    await proxy.connect(chairman).addLegalSigner(legal1.address);
    await proxy.connect(chairman).addLegalSigner(legal2.address);
    await proxy.connect(chairman).addLegalSigner(legal3.address);
    await proxy.connect(chairman).addLegalSigner(legal4.address);
    await proxy.connect(chairman).addLegalSigner(legal5.address);

    const scope = ethers.keccak256(ethers.toUtf8Bytes("workspace_42"));
    const courtOrderHash = ethers.keccak256(
      ethers.toUtf8Bytes("court_order_2026_001")
    );

    return {
      proxy,
      chairman,
      admin1,
      admin2,
      customer1,
      customer2,
      legal1,
      legal2,
      legal3,
      legal4,
      legal5,
      stranger,
      scope,
      courtOrderHash,
    };
  }

  // ============================================================
  // 1. Deployment & wiring
  // ============================================================
  describe("Deployment & wiring", function () {
    it("sets chairman, whitelists 2 admins and 5 legal signers", async function () {
      const f = await deployFixture();
      expect(await f.proxy.chairman()).to.equal(f.chairman.address);
      expect(await f.proxy.adminWhitelist(f.admin1.address)).to.equal(true);
      expect(await f.proxy.adminWhitelist(f.admin2.address)).to.equal(true);
      expect(await f.proxy.legalMultisig(f.legal1.address)).to.equal(true);
      expect(await f.proxy.legalMultisig(f.legal5.address)).to.equal(true);
      expect(await f.proxy.requestCounter()).to.equal(0n);
    });

    it("exposes correct constants", async function () {
      const f = await deployFixture();
      expect(await f.proxy.MIN_DURATION()).to.equal(SIX_HOURS);
      expect(await f.proxy.MAX_DURATION()).to.equal(TWENTY_FOUR_HOURS);
      expect(await f.proxy.REQUIRED_LEGAL_SIGS()).to.equal(3n);
    });

    it("rejects zero-address chairman in initializer", async function () {
      const Factory = await ethers.getContractFactory("ZeniAccessControl");
      await expect(
        upgrades.deployProxy(Factory, [ethers.ZeroAddress], {
          kind: "uups",
          initializer: "initialize",
        })
      ).to.be.revertedWithCustomError(Factory, "ZeroAddress");
    });
  });

  // ============================================================
  // 2. Customer support flow
  // ============================================================
  describe("Customer support flow", function () {
    it("admin requests, customer approves, isAccessActive true within window, false after expiry", async function () {
      const f = await deployFixture();

      const tx = await f.proxy
        .connect(f.admin1)
        .requestAccess(f.customer1.address, f.scope, "TICKET-001", TWELVE_HOURS);
      const receipt = await tx.wait();

      // requestId = 1
      expect(await f.proxy.requestCounter()).to.equal(1n);

      const reqBefore = await f.proxy.getRequest(1);
      expect(reqBefore.status).to.equal(Status.Pending);
      expect(reqBefore.admin).to.equal(f.admin1.address);
      expect(reqBefore.customer).to.equal(f.customer1.address);
      expect(reqBefore.reason).to.equal(Reason.CustomerSupport);
      expect(reqBefore.detail).to.equal("TICKET-001");
      expect(reqBefore.duration).to.equal(TWELVE_HOURS);

      expect(await f.proxy.isAccessActive(1)).to.equal(false);

      await expect(f.proxy.connect(f.customer1).approveByCustomer(1))
        .to.emit(f.proxy, "AccessApproved");

      expect(await f.proxy.isAccessActive(1)).to.equal(true);

      // Fast-forward past expiry
      await time.increase(TWELVE_HOURS + 1);
      expect(await f.proxy.isAccessActive(1)).to.equal(false);
    });

    it("emits AccessRequested event with correct fields", async function () {
      const f = await deployFixture();
      await expect(
        f.proxy
          .connect(f.admin1)
          .requestAccess(f.customer1.address, f.scope, "TICKET-X", SIX_HOURS)
      )
        .to.emit(f.proxy, "AccessRequested")
        .withArgs(
          1n,
          f.admin1.address,
          f.customer1.address,
          f.scope,
          Reason.CustomerSupport,
          SIX_HOURS,
          "TICKET-X"
        );
    });
  });

  // ============================================================
  // 3. Customer denies (revokes pending)
  // ============================================================
  describe("Customer denies pending request", function () {
    it("status becomes Revoked, never Approved, isAccessActive false", async function () {
      const f = await deployFixture();
      await f.proxy
        .connect(f.admin1)
        .requestAccess(f.customer1.address, f.scope, "TICKET-DENY", SIX_HOURS);

      await expect(f.proxy.connect(f.customer1).revokeAccess(1))
        .to.emit(f.proxy, "AccessRevoked");

      const r = await f.proxy.getRequest(1);
      expect(r.status).to.equal(Status.Revoked);
      expect(r.approvedAt).to.equal(0n);
      expect(await f.proxy.isAccessActive(1)).to.equal(false);
    });
  });

  // ============================================================
  // 4. Customer revokes after approval
  // ============================================================
  describe("Customer revokes after approval", function () {
    it("isAccessActive flips to false immediately", async function () {
      const f = await deployFixture();
      await f.proxy
        .connect(f.admin1)
        .requestAccess(f.customer1.address, f.scope, "TICKET-002", TWELVE_HOURS);
      await f.proxy.connect(f.customer1).approveByCustomer(1);
      expect(await f.proxy.isAccessActive(1)).to.equal(true);

      await f.proxy.connect(f.customer1).revokeAccess(1);
      const r = await f.proxy.getRequest(1);
      expect(r.status).to.equal(Status.Revoked);
      expect(await f.proxy.isAccessActive(1)).to.equal(false);
    });
  });

  // ============================================================
  // 5. Admin self-releases
  // ============================================================
  describe("Admin self-release", function () {
    it("admin who filed request can revoke it", async function () {
      const f = await deployFixture();
      await f.proxy
        .connect(f.admin1)
        .requestAccess(f.customer1.address, f.scope, "TICKET-003", TWELVE_HOURS);
      await f.proxy.connect(f.customer1).approveByCustomer(1);

      await expect(f.proxy.connect(f.admin1).revokeAccess(1))
        .to.emit(f.proxy, "AccessRevoked");
      expect(await f.proxy.isAccessActive(1)).to.equal(false);
    });
  });

  // ============================================================
  // 6. Chairman force-revoke
  // ============================================================
  describe("Chairman force-revoke", function () {
    it("chairman can revoke any request", async function () {
      const f = await deployFixture();
      await f.proxy
        .connect(f.admin1)
        .requestAccess(f.customer1.address, f.scope, "TICKET-004", SIX_HOURS);
      await f.proxy.connect(f.customer1).approveByCustomer(1);

      await f.proxy.connect(f.chairman).revokeAccess(1);
      const r = await f.proxy.getRequest(1);
      expect(r.status).to.equal(Status.Revoked);
    });

    it("non-customer/non-admin/non-chairman cannot revoke", async function () {
      const f = await deployFixture();
      await f.proxy
        .connect(f.admin1)
        .requestAccess(f.customer1.address, f.scope, "TICKET-005", SIX_HOURS);

      await expect(
        f.proxy.connect(f.stranger).revokeAccess(1)
      ).to.be.revertedWithCustomError(f.proxy, "NotCustomer");
    });
  });

  // ============================================================
  // 7. Legal authority flow (3-of-5)
  // ============================================================
  describe("Legal authority 3-of-5 flow", function () {
    it("collects 3 sigs, status flips to Approved, courtOrderHash recorded", async function () {
      const f = await deployFixture();
      // Admin opens a request first (Pending)
      await f.proxy
        .connect(f.admin1)
        .requestAccess(f.customer1.address, f.scope, "COURT-CASE-1", TWELVE_HOURS);

      // 1st signer
      await expect(
        f.proxy.connect(f.legal1).emergencyApprove(1, f.courtOrderHash)
      ).to.emit(f.proxy, "EmergencyTriggered");

      let r = await f.proxy.getRequest(1);
      expect(r.status).to.equal(Status.Pending);
      expect(await f.proxy.emergencySigCount(1)).to.equal(1n);

      // 2nd signer
      await f.proxy.connect(f.legal2).emergencyApprove(1, f.courtOrderHash);
      r = await f.proxy.getRequest(1);
      expect(r.status).to.equal(Status.Pending);
      expect(await f.proxy.emergencySigCount(1)).to.equal(2n);

      // 3rd signer flips to Approved
      await expect(
        f.proxy.connect(f.legal3).emergencyApprove(1, f.courtOrderHash)
      ).to.emit(f.proxy, "AccessApproved");

      r = await f.proxy.getRequest(1);
      expect(r.status).to.equal(Status.Approved);
      expect(r.reason).to.equal(Reason.LegalAuthority);
      expect(r.courtOrderHash).to.equal(f.courtOrderHash);
      expect(await f.proxy.isAccessActive(1)).to.equal(true);

      const sigs = await f.proxy.getEmergencySignatures(1);
      expect(sigs.length).to.equal(3);
      expect(sigs).to.include(f.legal1.address);
      expect(sigs).to.include(f.legal2.address);
      expect(sigs).to.include(f.legal3.address);
    });
  });

  // ============================================================
  // 8. Cannot legal-approve with same signer twice
  // ============================================================
  describe("Duplicate legal signature prevention", function () {
    it("reverts AlreadySigned on second sig from same signer", async function () {
      const f = await deployFixture();
      await f.proxy
        .connect(f.admin1)
        .requestAccess(f.customer1.address, f.scope, "COURT-CASE-2", TWELVE_HOURS);
      await f.proxy.connect(f.legal1).emergencyApprove(1, f.courtOrderHash);
      await expect(
        f.proxy.connect(f.legal1).emergencyApprove(1, f.courtOrderHash)
      ).to.be.revertedWithCustomError(f.proxy, "AlreadySigned");
    });

    it("reverts on courtOrderHash mismatch by later signer", async function () {
      const f = await deployFixture();
      await f.proxy
        .connect(f.admin1)
        .requestAccess(f.customer1.address, f.scope, "COURT-CASE-3", TWELVE_HOURS);
      await f.proxy.connect(f.legal1).emergencyApprove(1, f.courtOrderHash);
      const otherHash = ethers.keccak256(ethers.toUtf8Bytes("different_order"));
      await expect(
        f.proxy.connect(f.legal2).emergencyApprove(1, otherHash)
      ).to.be.revertedWithCustomError(f.proxy, "InvalidReason");
    });
  });

  // ============================================================
  // 9. Cannot legal-approve without 3 sigs
  // ============================================================
  describe("Insufficient legal signatures", function () {
    it("status remains Pending with only 2 sigs", async function () {
      const f = await deployFixture();
      await f.proxy
        .connect(f.admin1)
        .requestAccess(f.customer1.address, f.scope, "COURT-CASE-4", TWELVE_HOURS);
      await f.proxy.connect(f.legal1).emergencyApprove(1, f.courtOrderHash);
      await f.proxy.connect(f.legal2).emergencyApprove(1, f.courtOrderHash);

      const r = await f.proxy.getRequest(1);
      expect(r.status).to.equal(Status.Pending);
      expect(await f.proxy.isAccessActive(1)).to.equal(false);
    });
  });

  // ============================================================
  // 10. Duration bounds
  // ============================================================
  describe("Duration bounds", function () {
    it("reverts when duration < MIN_DURATION", async function () {
      const f = await deployFixture();
      await expect(
        f.proxy
          .connect(f.admin1)
          .requestAccess(f.customer1.address, f.scope, "TICKET-SHORT", FIVE_HOURS)
      ).to.be.revertedWithCustomError(f.proxy, "InvalidDuration");
    });

    it("reverts when duration > MAX_DURATION", async function () {
      const f = await deployFixture();
      await expect(
        f.proxy
          .connect(f.admin1)
          .requestAccess(f.customer1.address, f.scope, "TICKET-LONG", TWENTY_FIVE_HOURS)
      ).to.be.revertedWithCustomError(f.proxy, "InvalidDuration");
    });

    it("accepts MIN_DURATION exactly", async function () {
      const f = await deployFixture();
      await expect(
        f.proxy
          .connect(f.admin1)
          .requestAccess(f.customer1.address, f.scope, "TICKET-MIN", SIX_HOURS)
      ).to.not.be.reverted;
    });

    it("accepts MAX_DURATION exactly", async function () {
      const f = await deployFixture();
      await expect(
        f.proxy
          .connect(f.admin1)
          .requestAccess(f.customer1.address, f.scope, "TICKET-MAX", TWENTY_FOUR_HOURS)
      ).to.not.be.reverted;
    });
  });

  // ============================================================
  // 11. Non-customer tries to approve
  // ============================================================
  describe("Approval authorization", function () {
    it("reverts NotCustomer when stranger tries approveByCustomer", async function () {
      const f = await deployFixture();
      await f.proxy
        .connect(f.admin1)
        .requestAccess(f.customer1.address, f.scope, "TICKET-A", SIX_HOURS);
      await expect(
        f.proxy.connect(f.stranger).approveByCustomer(1)
      ).to.be.revertedWithCustomError(f.proxy, "NotCustomer");
    });

    it("reverts on double approval", async function () {
      const f = await deployFixture();
      await f.proxy
        .connect(f.admin1)
        .requestAccess(f.customer1.address, f.scope, "TICKET-B", SIX_HOURS);
      await f.proxy.connect(f.customer1).approveByCustomer(1);
      await expect(
        f.proxy.connect(f.customer1).approveByCustomer(1)
      ).to.be.revertedWithCustomError(f.proxy, "InvalidStatus");
    });
  });

  // ============================================================
  // 12. Non-admin tries requestAccess
  // ============================================================
  describe("Request authorization", function () {
    it("reverts NotAdmin when stranger calls requestAccess", async function () {
      const f = await deployFixture();
      await expect(
        f.proxy
          .connect(f.stranger)
          .requestAccess(f.customer1.address, f.scope, "TICKET-X", SIX_HOURS)
      ).to.be.revertedWithCustomError(f.proxy, "NotAdmin");
    });

    it("reverts after admin removed", async function () {
      const f = await deployFixture();
      await f.proxy.connect(f.chairman).removeAdmin(f.admin1.address);
      await expect(
        f.proxy
          .connect(f.admin1)
          .requestAccess(f.customer1.address, f.scope, "TICKET-Y", SIX_HOURS)
      ).to.be.revertedWithCustomError(f.proxy, "NotAdmin");
    });
  });

  // ============================================================
  // 13. Non-chairman tries chairman-only ops
  // ============================================================
  describe("Chairman-only authorization", function () {
    it("reverts NotChairman on addAdmin from stranger", async function () {
      const f = await deployFixture();
      await expect(
        f.proxy.connect(f.stranger).addAdmin(f.stranger.address)
      ).to.be.revertedWithCustomError(f.proxy, "NotChairman");
    });

    it("reverts NotChairman on addLegalSigner from admin", async function () {
      const f = await deployFixture();
      await expect(
        f.proxy.connect(f.admin1).addLegalSigner(f.stranger.address)
      ).to.be.revertedWithCustomError(f.proxy, "NotChairman");
    });

    it("transferChairman moves role and Ownable owner", async function () {
      const f = await deployFixture();
      await f.proxy.connect(f.chairman).transferChairman(f.admin1.address);
      expect(await f.proxy.chairman()).to.equal(f.admin1.address);
      // Old chairman can no longer act
      await expect(
        f.proxy.connect(f.chairman).addAdmin(f.stranger.address)
      ).to.be.revertedWithCustomError(f.proxy, "NotChairman");
    });
  });

  // ============================================================
  // 14. Multiple concurrent requests
  // ============================================================
  describe("Concurrent independent requests", function () {
    it("two requests for same customer evolve independently", async function () {
      const f = await deployFixture();
      const scopeA = ethers.keccak256(ethers.toUtf8Bytes("ws_A"));
      const scopeB = ethers.keccak256(ethers.toUtf8Bytes("ws_B"));

      await f.proxy
        .connect(f.admin1)
        .requestAccess(f.customer1.address, scopeA, "T-A", SIX_HOURS);
      await f.proxy
        .connect(f.admin2)
        .requestAccess(f.customer1.address, scopeB, "T-B", TWELVE_HOURS);

      expect(await f.proxy.requestCounter()).to.equal(2n);

      // Approve only request 1
      await f.proxy.connect(f.customer1).approveByCustomer(1);
      expect(await f.proxy.isAccessActive(1)).to.equal(true);
      expect(await f.proxy.isAccessActive(2)).to.equal(false);

      // Revoke request 2
      await f.proxy.connect(f.customer1).revokeAccess(2);
      const r2 = await f.proxy.getRequest(2);
      expect(r2.status).to.equal(Status.Revoked);

      // Request 1 unchanged
      const r1 = await f.proxy.getRequest(1);
      expect(r1.status).to.equal(Status.Approved);
    });
  });

  // ============================================================
  // 15. View functions
  // ============================================================
  describe("View functions", function () {
    it("getRequest reverts RequestNotFound for id 0 or unknown", async function () {
      const f = await deployFixture();
      await expect(f.proxy.getRequest(0)).to.be.revertedWithCustomError(
        f.proxy,
        "RequestNotFound"
      );
      await expect(f.proxy.getRequest(99)).to.be.revertedWithCustomError(
        f.proxy,
        "RequestNotFound"
      );
    });

    it("getEmergencySignatures returns empty array initially, grows with sigs", async function () {
      const f = await deployFixture();
      await f.proxy
        .connect(f.admin1)
        .requestAccess(f.customer1.address, f.scope, "T", SIX_HOURS);
      let sigs = await f.proxy.getEmergencySignatures(1);
      expect(sigs.length).to.equal(0);

      await f.proxy.connect(f.legal1).emergencyApprove(1, f.courtOrderHash);
      sigs = await f.proxy.getEmergencySignatures(1);
      expect(sigs.length).to.equal(1);
      expect(sigs[0]).to.equal(f.legal1.address);
    });

    it("isAccessActive returns false for unknown requestId without revert", async function () {
      const f = await deployFixture();
      expect(await f.proxy.isAccessActive(0)).to.equal(false);
      expect(await f.proxy.isAccessActive(999)).to.equal(false);
    });
  });

  // ============================================================
  // 16. Negative paths for emergencyApprove
  // ============================================================
  describe("emergencyApprove negative paths", function () {
    it("reverts NotLegal when caller is not in legalMultisig", async function () {
      const f = await deployFixture();
      await f.proxy
        .connect(f.admin1)
        .requestAccess(f.customer1.address, f.scope, "T", SIX_HOURS);
      await expect(
        f.proxy.connect(f.stranger).emergencyApprove(1, f.courtOrderHash)
      ).to.be.revertedWithCustomError(f.proxy, "NotLegal");
    });

    it("reverts InvalidStatus when request already Revoked", async function () {
      const f = await deployFixture();
      await f.proxy
        .connect(f.admin1)
        .requestAccess(f.customer1.address, f.scope, "T", SIX_HOURS);
      await f.proxy.connect(f.customer1).revokeAccess(1);
      await expect(
        f.proxy.connect(f.legal1).emergencyApprove(1, f.courtOrderHash)
      ).to.be.revertedWithCustomError(f.proxy, "InvalidStatus");
    });

    it("reverts on zero courtOrderHash", async function () {
      const f = await deployFixture();
      await f.proxy
        .connect(f.admin1)
        .requestAccess(f.customer1.address, f.scope, "T", SIX_HOURS);
      await expect(
        f.proxy.connect(f.legal1).emergencyApprove(1, ethers.ZeroHash)
      ).to.be.revertedWithCustomError(f.proxy, "InvalidReason");
    });
  });

  // ============================================================
  // 17. Cannot request with zero customer
  // ============================================================
  describe("Input validation", function () {
    it("reverts ZeroAddress on requestAccess with zero customer", async function () {
      const f = await deployFixture();
      await expect(
        f.proxy
          .connect(f.admin1)
          .requestAccess(ethers.ZeroAddress, f.scope, "T", SIX_HOURS)
      ).to.be.revertedWithCustomError(f.proxy, "ZeroAddress");
    });

    it("reverts ZeroAddress on addAdmin / addLegalSigner with zero", async function () {
      const f = await deployFixture();
      await expect(
        f.proxy.connect(f.chairman).addAdmin(ethers.ZeroAddress)
      ).to.be.revertedWithCustomError(f.proxy, "ZeroAddress");
      await expect(
        f.proxy.connect(f.chairman).addLegalSigner(ethers.ZeroAddress)
      ).to.be.revertedWithCustomError(f.proxy, "ZeroAddress");
    });
  });
});
