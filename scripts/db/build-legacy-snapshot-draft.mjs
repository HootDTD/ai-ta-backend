import { readFileSync, readdirSync, writeFileSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
const legacyDir = join(repoRoot, "database", "migrations");
const activeDir = join(repoRoot, "supabase", "migrations");
const snapshotPattern = /^\d{14}_legacy_public_snapshot\.sql$/;

const snapshotFiles = readdirSync(activeDir).filter((name) => snapshotPattern.test(name));
if (snapshotFiles.length !== 1) {
  throw new Error(`expected exactly one draft snapshot; found ${snapshotFiles.length}`);
}

const source001 = readFileSync(join(legacyDir, "001_create_schema.py"), "utf8");
const ddlMatch = source001.match(/DDL = f"""([\s\S]*?)"""\s*\n\s*if ENABLE_HNSW:/);
if (!ddlMatch) throw new Error("could not extract DDL from 001_create_schema.py");

const baseDdl = ddlMatch[1]
  .replaceAll("{EMBEDDING_DIM}", "3072")
  .replaceAll("{{", "{")
  .replaceAll("}}", "}")
  .trim();
const legacySqlFiles = readdirSync(legacyDir)
  .filter((name) => /^\d{3}_[a-z0-9_]+\.sql$/.test(name))
  .sort();

const warning = `/*
 * DRAFT - DERIVED FROM THE FROZEN LEGACY MIGRATION CHAIN, NOT PRODUCTION.
 *
 * This file is a locally reconstructed fallback. The HUMAN-ONLY db-pull
 * snapshot in docs/_archive/handoffs/2026-07-16-db-history-repair-handoff.md
 * is authoritative and MUST replace this draft before any remote history
 * reconciliation. Known production drift includes the duplicate 023 pair and
 * untracked pre-043 history, so production may differ from this file.
 *
 * Reconstruction method: migration 001's public schema DDL at the production
 * embedding dimension (3072), followed by every frozen SQL migration 004..047
 * in lexicographic filename order (including both 023 files). Python migrations
 * 002 and 003 move data and are intentionally excluded from this schema draft.
 */

SET lock_timeout = '5s';
SET statement_timeout = '5min';
`;

const sections = [
  warning,
  `\n-- SOURCE: database/migrations/001_create_schema.py (DDL)\n${baseDdl}\n`,
  ...legacySqlFiles.map((name) => {
    const sql = readFileSync(join(legacyDir, name), "utf8").trim();
    return `\n-- SOURCE: database/migrations/${name}\n${sql}\n`;
  }),
];

const outputPath = join(activeDir, snapshotFiles[0]);
writeFileSync(outputPath, `${sections.join("\n").replace(/\r\n?/g, "\n").trim()}\n`, "utf8");
console.log(`Wrote ${relative(repoRoot, outputPath)} from ${legacySqlFiles.length + 1} schema sources.`);
