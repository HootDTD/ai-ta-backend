import { readFileSync } from "node:fs";
import { resolve } from "node:path";

function usage() {
  console.error("Usage: node scripts/db/compare-schema-dump.mjs <left.sql> <right.sql>");
  process.exit(2);
}

function stripCommentsAndSplit(sql) {
  const statements = [];
  let current = "";
  let state = "plain";
  let dollarTag = "";

  for (let index = 0; index < sql.length; index += 1) {
    const char = sql[index];
    const next = sql[index + 1] ?? "";

    if (state === "line-comment") {
      if (char === "\n") {
        state = "plain";
        current += " ";
      }
      continue;
    }
    if (state === "block-comment") {
      if (char === "*" && next === "/") {
        state = "plain";
        current += " ";
        index += 1;
      }
      continue;
    }
    if (state === "single") {
      current += char;
      if (char === "'" && next === "'") {
        current += next;
        index += 1;
      } else if (char === "'") {
        state = "plain";
      }
      continue;
    }
    if (state === "double") {
      current += char;
      if (char === '"' && next === '"') {
        current += next;
        index += 1;
      } else if (char === '"') {
        state = "plain";
      }
      continue;
    }
    if (state === "dollar") {
      if (sql.startsWith(dollarTag, index)) {
        current += dollarTag;
        index += dollarTag.length - 1;
        state = "plain";
      } else {
        current += char;
      }
      continue;
    }

    if (char === "-" && next === "-") {
      state = "line-comment";
      index += 1;
    } else if (char === "/" && next === "*") {
      state = "block-comment";
      index += 1;
    } else if (char === "'") {
      state = "single";
      current += char;
    } else if (char === '"') {
      state = "double";
      current += char;
    } else if (char === "$") {
      const match = sql.slice(index).match(/^\$[A-Za-z_][A-Za-z0-9_]*\$|^\$\$/);
      if (match) {
        dollarTag = match[0];
        state = "dollar";
        current += dollarTag;
        index += dollarTag.length - 1;
      } else {
        current += char;
      }
    } else if (char === ";") {
      if (current.trim()) statements.push(current.trim());
      current = "";
    } else {
      current += char;
    }
  }

  if (state !== "plain" && state !== "line-comment") {
    throw new Error(`unterminated SQL ${state}`);
  }
  if (current.trim()) statements.push(current.trim());
  return statements;
}

function normalize(path) {
  const sql = readFileSync(path, "utf8").replace(/\r\n?/g, "\n");
  return stripCommentsAndSplit(sql)
    .map((statement) => statement.replace(/\s+/g, " ").trim())
    .filter(Boolean)
    .filter((statement) => !/^SET\s/i.test(statement))
    .filter((statement) => !/^SELECT\s+pg_catalog\.set_config\b/i.test(statement))
    .filter((statement) => !/^\\(?:restrict|unrestrict)\b/i.test(statement))
    .sort((left, right) => left.localeCompare(right));
}

function counts(values) {
  const result = new Map();
  for (const value of values) result.set(value, (result.get(value) ?? 0) + 1);
  return result;
}

if (process.argv.length !== 4) usage();
const leftPath = resolve(process.argv[2]);
const rightPath = resolve(process.argv[3]);
const left = counts(normalize(leftPath));
const right = counts(normalize(rightPath));
const onlyLeft = [];
const onlyRight = [];

for (const [statement, count] of left) {
  const difference = count - (right.get(statement) ?? 0);
  for (let index = 0; index < difference; index += 1) onlyLeft.push(statement);
}
for (const [statement, count] of right) {
  const difference = count - (left.get(statement) ?? 0);
  for (let index = 0; index < difference; index += 1) onlyRight.push(statement);
}

if (onlyLeft.length === 0 && onlyRight.length === 0) {
  console.log(`Schema dumps match after normalization (${[...left.values()].reduce((a, b) => a + b, 0)} objects).`);
  process.exit(0);
}

console.error(`Schema dumps differ: ${onlyLeft.length} only in ${leftPath}; ${onlyRight.length} only in ${rightPath}.`);
for (const statement of onlyLeft.slice(0, 100)) console.error(`- ${statement}`);
for (const statement of onlyRight.slice(0, 100)) console.error(`+ ${statement}`);
if (onlyLeft.length + onlyRight.length > 200) console.error("Diff truncated after 200 statements.");
process.exit(1);
