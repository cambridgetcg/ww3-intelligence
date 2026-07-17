#!/usr/bin/env node
/** Bounded local bridge to an exact RhetorLint checkout. No npm/network path. */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const MAX_PASSAGES = 8;
const MAX_OUTPUT_BYTES = 8 * 1024 * 1024;

async function main() {
  const root = process.argv[2];
  if (!root) throw new Error("missing root");
  const payload = JSON.parse(readFileSync(0, "utf8"));
  if (!payload || !Array.isArray(payload.passages) ||
      payload.passages.length > MAX_PASSAGES) {
    throw new Error("invalid passages");
  }

  const corePath = join(root, "packages", "core", "index.mjs");
  const rulesPath = join(root, "packages", "rules-en", "rules.json");
  const { analyze } = await import(pathToFileURL(corePath).href);
  const rules = JSON.parse(readFileSync(rulesPath, "utf8"));
  const results = payload.passages.map((passage) => ({
    field: passage.field,
    result: analyze(passage.text, { rules }),
  }));
  const encoded = JSON.stringify({ results });
  if (Buffer.byteLength(encoded, "utf8") > MAX_OUTPUT_BYTES) {
    throw new Error("output limit");
  }
  process.stdout.write(encoded);
}

main().catch(() => {
  process.stderr.write("rhetorlint bridge failed\n");
  process.exitCode = 2;
});
