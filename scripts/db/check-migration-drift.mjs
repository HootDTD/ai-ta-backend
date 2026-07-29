import { createHash } from "node:crypto";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
const legacyDir = join(repoRoot, "database", "migrations");
const activeDir = join(repoRoot, "supabase", "migrations");
const manifestPath = join(legacyDir, "legacy-manifest.sha256");
const legacyPattern = /^\d{3}_[a-z0-9_]+\.(?:py|sql)$/;
const activePattern = /^\d{14}_[a-z][a-z0-9_]*\.sql$/;

function fail(message) {
  console.error(`migration drift check failed: ${message}`);
  process.exitCode = 1;
}

function normalizedSha256(path) {
  const normalized = readFileSync(path, "utf8").replace(/\r\n?/g, "\n");
  return createHash("sha256").update(normalized).digest("hex");
}

if (!existsSync(manifestPath)) {
  fail(`${relative(repoRoot, manifestPath)} is missing`);
} else if (!existsSync(activeDir)) {
  fail(`${relative(repoRoot, activeDir)} is missing`);
} else {
  const manifest = new Map();
  for (const [index, line] of readFileSync(manifestPath, "utf8")
    .split(/\r?\n/)
    .entries()) {
    if (!line || line.startsWith("#")) continue;
    const match = line.match(/^([0-9a-f]{64})  (\d{3}_[a-z0-9_]+\.(?:py|sql))$/);
    if (!match) {
      fail(`invalid manifest entry on line ${index + 1}`);
      continue;
    }
    if (manifest.has(match[2])) fail(`duplicate manifest entry: ${match[2]}`);
    manifest.set(match[2], match[1]);
  }

  const legacyFiles = readdirSync(legacyDir).filter((name) => legacyPattern.test(name)).sort();
  const expectedLegacy = [...manifest.keys()].sort();
  for (const name of legacyFiles.filter((name) => !manifest.has(name))) {
    fail(`legacy migration added after freeze: database/migrations/${name}`);
  }
  for (const name of expectedLegacy.filter((name) => !legacyFiles.includes(name))) {
    fail(`frozen legacy migration removed: database/migrations/${name}`);
  }
  for (const name of legacyFiles) {
    const actual = normalizedSha256(join(legacyDir, name));
    if (actual !== manifest.get(name)) {
      fail(`frozen legacy migration changed: database/migrations/${name}`);
    }
  }

  const activeEntries = readdirSync(activeDir, { withFileTypes: true });
  const activeFiles = activeEntries
    .filter((entry) => entry.isFile() && !entry.name.startsWith("."))
    .map((entry) => entry.name)
    .sort();
  for (const entry of activeEntries.filter((entry) => entry.isDirectory())) {
    fail(`active migration directory must be flat: supabase/migrations/${entry.name}`);
  }
  for (const name of activeFiles.filter((name) => !activePattern.test(name))) {
    fail(`active migration is not timestamped SQL: supabase/migrations/${name}`);
  }
  const versions = activeFiles.filter((name) => activePattern.test(name)).map((name) => name.slice(0, 14));
  if (new Set(versions).size !== versions.length) fail("active migration timestamps are not unique");

  if (!process.exitCode) {
    console.log(
      `Migration histories valid: ${legacyFiles.length} frozen legacy files; ` +
        `${activeFiles.length} timestamped active migrations.`,
    );
  }
}
