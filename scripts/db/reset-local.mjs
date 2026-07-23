import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const cliVersion = "2.109.0";
const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
const localCli = join(
  repoRoot,
  "node_modules",
  "supabase",
  "bin",
  process.platform === "win32" ? "supabase.exe" : "supabase",
);
const cli = process.env.SUPABASE_BIN || (existsSync(localCli) ? localCli : "supabase");
const env = { ...process.env, SUPABASE_TELEMETRY_DISABLED: "1" };

function run(command, args, options = {}) {
  const result = spawnSync(command, args, {
    cwd: repoRoot,
    env,
    encoding: "utf8",
    stdio: options.capture ? "pipe" : "inherit",
    shell: false,
  });
  if (result.error) throw result.error;
  if (!options.allowFailure && result.status !== 0) process.exit(result.status ?? 1);
  return result;
}

run(process.execPath, [join(repoRoot, "scripts", "db", "check-migration-drift.mjs")]);

const versionResult = run(cli, ["--version"], { capture: true });
const actualVersion = versionResult.stdout.trim().replace(/^v/, "");
if (actualVersion !== cliVersion) {
  console.error(`Supabase CLI ${cliVersion} is required; found ${actualVersion || "unknown"}.`);
  console.error("Run npm install, or point SUPABASE_BIN at the pinned CLI binary.");
  process.exit(1);
}

const status = run(cli, ["status"], { allowFailure: true, capture: true });
if (status.status !== 0) run(cli, ["start"]);
run(cli, ["db", "reset", "--local", "--yes"]);
