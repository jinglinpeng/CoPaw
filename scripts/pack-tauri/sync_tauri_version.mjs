import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(scriptDir, "../..");
const versionFile = path.join(repoRoot, "src/qwenpaw/__version__.py");
const packageFile = path.join(repoRoot, "console/package.json");
const tauriConfigFile = path.join(
  repoRoot,
  "console/src-tauri/tauri.conf.json",
);

function readPythonVersion() {
  const text = fs.readFileSync(versionFile, "utf8");
  const match = text.match(/__version__\s*=\s*"([^"]+)"/);
  if (!match) {
    throw new Error(`Could not read __version__ from ${versionFile}`);
  }
  return match[1];
}

function toSemver(version) {
  const match = version.match(
    /^(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?(?:\.post(\d+))?(?:\.dev(\d+))?$/,
  );
  if (!match) {
    throw new Error(`Unsupported Python version for Tauri: ${version}`);
  }

  const [, major, minor, patch, prerelease, prereleaseNumber, post, dev] =
    match;
  const prereleaseMap = { a: "alpha", b: "beta", rc: "rc" };
  const labels = [];
  if (prerelease)
    labels.push(`${prereleaseMap[prerelease]}.${prereleaseNumber}`);
  if (post) labels.push(`post.${post}`);
  if (dev) labels.push(`dev.${dev}`);

  return `${major}.${minor}.${patch}${
    labels.length ? `-${labels.join(".")}` : ""
  }`;
}

function updateJson(file, update) {
  const data = JSON.parse(fs.readFileSync(file, "utf8"));
  update(data);
  fs.writeFileSync(file, `${JSON.stringify(data, null, 2)}\n`);
}

function updateTauriConfigVersion() {
  const text = fs.readFileSync(tauriConfigFile, "utf8");
  const updated = text.replace(
    /("version"\s*:\s*)"[^"]+"/,
    '$1"../package.json"',
  );
  if (updated === text && !text.includes('"version": "../package.json"')) {
    throw new Error(`Could not update version in ${tauriConfigFile}`);
  }
  fs.writeFileSync(tauriConfigFile, updated);
}

const semver = toSemver(readPythonVersion());

updateJson(packageFile, (data) => {
  data.version = semver;
});

updateTauriConfigVersion();

console.log(`Synced Tauri version to ${semver}`);
