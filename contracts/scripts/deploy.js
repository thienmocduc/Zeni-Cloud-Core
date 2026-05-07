// SPDX-License-Identifier: MIT
//
// Deploys ZeniAccessControl behind a UUPS proxy on the active network and
// wires up the initial admin + legal signer sets read from environment vars.
//
// Required env vars:
//   CHAIRMAN_ADDRESS         - chairman wallet (defaults to deployer)
//   INITIAL_ADMINS           - comma-separated list of admin addresses
//   INITIAL_LEGAL_SIGNERS    - comma-separated list of EXACTLY 5 legal signer addresses
//
// Usage:
//   npx hardhat run scripts/deploy.js --network polygonMumbai
//   npx hardhat run scripts/deploy.js --network polygon
//
const fs = require("fs");
const path = require("path");
const { ethers, upgrades, network } = require("hardhat");

function parseList(envVar) {
  if (!envVar) return [];
  return envVar
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

async function main() {
  const [deployer] = await ethers.getSigners();

  const chairman =
    (process.env.CHAIRMAN_ADDRESS && process.env.CHAIRMAN_ADDRESS.trim()) ||
    deployer.address;

  const admins = parseList(process.env.INITIAL_ADMINS);
  const legalSigners = parseList(process.env.INITIAL_LEGAL_SIGNERS);

  console.log("=========================================================");
  console.log("Deploying ZeniAccessControl");
  console.log("---------------------------------------------------------");
  console.log("Network        :", network.name);
  console.log("Deployer       :", deployer.address);
  console.log("Chairman       :", chairman);
  console.log("Admins         :", admins);
  console.log("Legal Signers  :", legalSigners);
  console.log("=========================================================");

  if (legalSigners.length > 0 && legalSigners.length !== 5) {
    throw new Error(
      `INITIAL_LEGAL_SIGNERS must contain exactly 5 addresses (got ${legalSigners.length}).`
    );
  }

  const Factory = await ethers.getContractFactory("ZeniAccessControl");
  const proxy = await upgrades.deployProxy(Factory, [chairman], {
    kind: "uups",
    initializer: "initialize",
  });
  await proxy.waitForDeployment();

  const proxyAddress = await proxy.getAddress();
  const implAddress = await upgrades.erc1967.getImplementationAddress(
    proxyAddress
  );
  const adminAddress = await upgrades.erc1967.getAdminAddress(proxyAddress);

  console.log("Proxy deployed at         :", proxyAddress);
  console.log("Implementation address    :", implAddress);
  console.log("ProxyAdmin (UUPS=zero)    :", adminAddress);

  const txHashes = [];

  // The deployer must be the chairman to call addAdmin / addLegalSigner.
  // If chairman != deployer, we skip wiring and instruct the operator.
  const wireFromDeployer =
    chairman.toLowerCase() === deployer.address.toLowerCase();

  if (wireFromDeployer) {
    for (const admin of admins) {
      const tx = await proxy.addAdmin(admin);
      const r = await tx.wait();
      console.log(`  addAdmin(${admin}) tx: ${r.hash}`);
      txHashes.push({ action: "addAdmin", target: admin, tx: r.hash });
    }
    for (const signer of legalSigners) {
      const tx = await proxy.addLegalSigner(signer);
      const r = await tx.wait();
      console.log(`  addLegalSigner(${signer}) tx: ${r.hash}`);
      txHashes.push({ action: "addLegalSigner", target: signer, tx: r.hash });
    }
  } else {
    console.log(
      "Deployer != chairman — skipping addAdmin/addLegalSigner. Run those from the chairman wallet."
    );
  }

  // Persist deployment metadata.
  const deploymentsDir = path.join(__dirname, "..", "deployments");
  if (!fs.existsSync(deploymentsDir)) {
    fs.mkdirSync(deploymentsDir, { recursive: true });
  }
  const outFile = path.join(deploymentsDir, `${network.name}.json`);
  const payload = {
    network: network.name,
    chainId: Number(
      (await ethers.provider.getNetwork()).chainId
    ),
    deployedAt: new Date().toISOString(),
    deployer: deployer.address,
    chairman,
    proxy: proxyAddress,
    implementation: implAddress,
    proxyAdmin: adminAddress,
    initialAdmins: admins,
    initialLegalSigners: legalSigners,
    txHashes,
  };
  fs.writeFileSync(outFile, JSON.stringify(payload, null, 2));
  console.log("\nDeployment record written to:", outFile);

  console.log("\nVerify with:");
  console.log(
    `  npx hardhat verify --network ${network.name} ${implAddress}`
  );
}

main().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
