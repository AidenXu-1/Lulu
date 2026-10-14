#!/usr/bin/env node
import { constants as fsConstants } from "node:fs";
import { mkdir, mkdtemp, readFile, rm, stat, symlink } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import { LEDGER_FILE_FLAGS, neutralizeCsvFormula, safeFilename, writeLedgerCsv } from "./server.mjs";

function assert(condition, message) { if (!condition) throw new Error(message); }

function parseCsv(input) {
  const rows = [];
  let row = [], field = "", quoted = false;
  const text = input.replace(/^\ufeff/, "");
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    if (quoted) {
      if (char === '"' && text[index + 1] === '"') { field += '"'; index += 1; }
      else if (char === '"') quoted = false;
      else field += char;
    } else if (char === '"') quoted = true;
    else if (char === ",") { row.push(field); field = ""; }
    else if (char === "\n") { row.push(field); rows.push(row); row = []; field = ""; }
    else if (char !== "\r") field += char;
  }
  if (field || row.length) { row.push(field); rows.push(row); }
  assert(!quoted, "unterminated CSV quote");
  return rows;
}

async function main() {
  const temp = await mkdtemp(path.join(os.tmpdir(), "lulu-csv-hardening-"));
  try {
    const dangerous = ["=1+1", "+SUM(1,1)", "-2+3", "@cmd", "  =hidden", "\u00a0=hidden", "\ttext", "\rtext", "\ntext", " \ttext"];
    const safe = ["普通中文", "person@example.com", "13800138000", "LULU-ABCD-2345-EFGH-6789", "  普通文本"];
    assert(dangerous.every(value => neutralizeCsvFormula(value).startsWith("'")), "formula prefix matrix failed");
    assert(safe.every(value => neutralizeCsvFormula(value) === value), "safe text matrix failed");

    const ledgerDir = path.join(temp, "ledger");
    const ledger = await writeLedgerCsv({
      ledgerDir,
      created:{ license_id:"safe-id", created_at:"2026-07-10T12:00:00.000Z" },
      form:{ license_code:"LULU-ABCD-2345-EFGH-6789", recipient_name:"=WEBSERVICE(\"x\")", contact:"  +SUM(1,1)", batch:"../;$(unsafe) @batch", max_devices:2, expires_at:null, remark:"\t@formula,\n\"quoted\"\r\n第二行" },
    });
    const parsed = parseCsv(await readFile(ledger, "utf8"));
    assert(parsed.length === 2 && parsed.every(row => row.length === 10), "CSV row/column shape failed");
    assert(parsed[1][0] === "LULU-ABCD-2345-EFGH-6789", "valid license code changed");
    assert(parsed[1][2].startsWith("'=") && parsed[1][3].startsWith("'  +") && parsed[1][9].startsWith("'\t"), "CSV text fields not neutralized");
    assert(parsed[1][9].includes(',\n"quoted"\r\n'), "CSV comma/quote/multiline structure failed");
    assert(((await stat(ledgerDir)).mode & 0o777) === 0o700, "ledger directory mode failed");
    assert(((await stat(ledger)).mode & 0o777) === 0o600, "ledger file mode failed");

    const name = path.parse(ledger).name;
    assert(!/[.;$()@/\\]/.test(name) && !name.includes("..") && safeFilename("../;$(unsafe) @batch").length <= 40, "batch filename hardening failed");
    if (fsConstants.O_NOFOLLOW) assert((LEDGER_FILE_FLAGS & fsConstants.O_NOFOLLOW) !== 0, "O_NOFOLLOW flag missing");

    const target = path.join(temp, "target"), link = path.join(temp, "linked-ledger");
    await mkdir(target, { mode:0o700 }); await symlink(target, link);
    let rejected = false;
    try { await writeLedgerCsv({ ledgerDir:link, created:{ license_id:"safe-id", created_at:"2026-07-10T12:00:00.000Z" }, form:{ license_code:"LULU-ABCD-2345-EFGH-6789", recipient_name:"safe", contact:"safe", batch:"safe", max_devices:2, expires_at:null, remark:"safe" } }); }
    catch (error) { rejected = error?.code === "ledger_symlink"; }
    assert(rejected, "symlink ledger directory not rejected");

    process.stdout.write(JSON.stringify({ formula_prefix_matrix:"ok", leading_whitespace_controls:"ok", structured_csv_rows_columns:"ok", commas_quotes_multiline:"ok", safe_text_and_license_code_unchanged:"ok", ledger_directory_symlink:"rejected", ledger_file_no_follow:fsConstants.O_NOFOLLOW ? "ok" : "platform_unavailable", batch_filename_hardening:"ok", directory_mode:"0700", file_mode:"0600", stdout_sensitive_values:false }) + "\n");
  } finally { await rm(temp, { recursive:true, force:true }); }
}

main().catch(error => { process.stderr.write(`csv hardening probe failed: ${error.message}\n`); process.exit(1); });
