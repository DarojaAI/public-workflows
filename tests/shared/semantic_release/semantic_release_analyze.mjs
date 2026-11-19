// Runs the installed commit-analyzer and release-notes-generator the way
// semantic-release would, using the plugin config from a .releaserc.json.
//
// usage: node semantic_release_analyze.mjs <install-dir> <releaserc.json>
// stdin: JSON array of commit messages
// stdout: JSON array of {message, release, notesError, notes}
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import path from "node:path";

const [installDir, rcPath] = process.argv.slice(2);
const req = createRequire(path.join(path.resolve(installDir), "noop.js"));
const rc = JSON.parse(readFileSync(rcPath, "utf8"));

function pluginConfig(name) {
  for (const p of rc.plugins ?? []) {
    if (Array.isArray(p) && p[0] === name) return p[1] ?? {};
    if (p === name) return {};
  }
  throw new Error(`${name} not configured in ${rcPath}`);
}

const analyzer = await import(req.resolve("@semantic-release/commit-analyzer"));
const notes = await import(req.resolve("@semantic-release/release-notes-generator"));
const logger = { log() {}, error() {}, warn() {} };
const messages = JSON.parse(readFileSync(0, "utf8"));
const out = [];

for (const message of messages) {
  const commit = { hash: "a".repeat(40), message, committerDate: new Date().toISOString() };
  const base = { cwd: path.resolve(installDir), commits: [commit], logger };
  const row = { message, release: null, notesError: null, notes: "" };
  row.release = await analyzer.analyzeCommits(pluginConfig("@semantic-release/commit-analyzer"), base);
  try {
    row.notes = await notes.generateNotes(pluginConfig("@semantic-release/release-notes-generator"), {
      ...base,
      options: { repositoryUrl: "https://github.com/o/r" },
      lastRelease: { gitTag: "v1.0.0" },
      nextRelease: { gitTag: "v2.0.0", version: "2.0.0" },
    });
  } catch (e) {
    row.notesError = String(e && e.message ? e.message : e);
  }
  out.push(row);
}
console.log(JSON.stringify(out));
