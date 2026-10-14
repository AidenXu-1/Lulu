#!/usr/bin/env node
import { createHash } from "node:crypto";
import { constants as fsConstants } from "node:fs";
import { chmod, lstat, mkdir, open, readdir, readFile, rename, rmdir } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

export const LEDGER_IGNORE_RULE = "/授权管理/授权码台账/";
export const DEFAULT_PROJECT_ROOT = path.resolve(fileURLToPath(new URL("../../../../", import.meta.url)));
export const DEFAULT_TARGET_DIR = path.join(DEFAULT_PROJECT_ROOT, "授权管理", "授权码台账");
export const DEFAULT_LEGACY_DIR = path.join(os.homedir(), "Documents", "Lulu授权码台账");

export const DEFAULT_FS_OPS = Object.freeze({ chmod, lstat, mkdir, open, readdir, readFile, rename, rmdir });

export class LedgerMigrationError extends Error {
  constructor(code) { super(code); this.code = code; }
}

function fail(code) { throw new LedgerMigrationError(code); }

async function directoryState(directory, code, { missing = false, ops = DEFAULT_FS_OPS } = {}) {
  try {
    const state = await ops.lstat(directory);
    if (state.isSymbolicLink()) fail(`${code}_symlink`);
    if (!state.isDirectory()) fail(`${code}_not_directory`);
    return state;
  } catch (error) {
    if (error instanceof LedgerMigrationError) throw error;
    if (missing && error?.code === "ENOENT") return null;
    fail(`${code}_check_failed`);
  }
}

async function setPrivateMode(target, expected, code, ops = DEFAULT_FS_OPS) {
  try { await ops.chmod(target, expected); }
  catch { fail(`${code}_permission_failed`); }
  let state;
  try { state = await ops.lstat(target); }
  catch { fail(`${code}_permission_failed`); }
  if ((state.mode & 0o777) !== expected) fail(`${code}_permission_failed`);
}

function expectedTarget(projectRoot) {
  return path.join(path.resolve(projectRoot), "授权管理", "授权码台账");
}

async function assertIgnored(projectRoot, ops = DEFAULT_FS_OPS) {
  let value;
  try { value = await ops.readFile(path.join(projectRoot, ".gitignore"), "utf8"); }
  catch { fail("ledger_gitignore_missing"); }
  const rules = value.split(/\r?\n/).map((line) => line.trim());
  if (!rules.includes(LEDGER_IGNORE_RULE)) fail("ledger_gitignore_missing");
}

export async function ensureProjectLedgerDirectory({
  projectRoot = DEFAULT_PROJECT_ROOT,
  targetDir = expectedTarget(projectRoot),
  ops = DEFAULT_FS_OPS,
} = {}) {
  const root = path.resolve(projectRoot);
  const target = path.resolve(targetDir);
  if (target !== expectedTarget(root)) fail("ledger_target_outside_project");
  await directoryState(root, "project_root", { ops });
  await assertIgnored(root, ops);
  const parent = path.dirname(target);
  await directoryState(parent, "ledger_parent", { ops });
  await setPrivateMode(parent, 0o700, "ledger_parent", ops);
  let targetState = await directoryState(target, "ledger_target", { missing:true, ops });
  if (!targetState) {
    try { await ops.mkdir(target, { mode:0o700 }); }
    catch { fail("ledger_target_create_failed"); }
    targetState = await directoryState(target, "ledger_target", { ops });
  }
  await setPrivateMode(target, 0o700, "ledger_target", ops);
  return { projectRoot:root, targetDir:target, targetState };
}

async function fileMetadata(file, name, ops = DEFAULT_FS_OPS) {
  let state;
  try { state = await ops.lstat(file); }
  catch { fail("ledger_file_check_failed"); }
  if (state.isSymbolicLink()) fail("ledger_file_symlink");
  if (!state.isFile()) fail("ledger_file_not_regular");
  const flags = fsConstants.O_RDONLY | (fsConstants.O_NOFOLLOW || 0);
  let handle;
  try { handle = await ops.open(file, flags); }
  catch { fail("ledger_file_open_failed"); }
  const hash = createHash("sha256");
  try {
    for await (const chunk of handle.createReadStream({ autoClose:false })) hash.update(chunk);
  } catch { fail("ledger_file_hash_failed"); }
  finally { await handle.close().catch(() => {}); }
  return { name, size:state.size, mode:state.mode & 0o777, sha256:hash.digest("hex") };
}

async function csvNames(directory, ops = DEFAULT_FS_OPS) {
  let entries;
  try { entries = await ops.readdir(directory, { withFileTypes:true }); }
  catch { fail("ledger_source_read_failed"); }
  const csv = [];
  for (const entry of entries) {
    if (!entry.name.toLowerCase().endsWith(".csv")) continue;
    if (entry.isSymbolicLink()) fail("ledger_file_symlink");
    if (!entry.isFile()) fail("ledger_file_not_regular");
    csv.push(entry.name);
  }
  return csv.sort((a, b) => a.localeCompare(b, "zh-CN"));
}

async function pathExists(target, ops = DEFAULT_FS_OPS) {
  try { await ops.lstat(target); return true; }
  catch (error) { if (error?.code === "ENOENT") return false; fail("ledger_target_check_failed"); }
}

function sameMetadata(before, after) {
  return before.name === after.name
    && before.size === after.size
    && before.mode === 0o600
    && after.mode === 0o600
    && before.sha256 === after.sha256;
}

export async function migrateLegacyLedger({
  projectRoot = DEFAULT_PROJECT_ROOT,
  sourceDir = DEFAULT_LEGACY_DIR,
  targetDir = expectedTarget(projectRoot),
  checkOnly = false,
  ops = DEFAULT_FS_OPS,
} = {}) {
  const layout = await ensureProjectLedgerDirectory({ projectRoot, targetDir, ops });
  const source = path.resolve(sourceDir);
  if (source === layout.targetDir) fail("ledger_source_equals_target");
  const sourceParent = path.dirname(source);
  await directoryState(sourceParent, "ledger_source_parent", { ops });
  const sourceState = await directoryState(source, "ledger_source", { missing:true, ops });
  if (!sourceState) return { status:"ok", check_only:checkOnly, migrated_files:0, migrated_bytes:0, names_match:true, sizes_match:true, hashes_match:true, permissions_match:true, legacy_dir_removed:true };
  await setPrivateMode(source, 0o700, "ledger_source", ops);
  const names = await csvNames(source, ops);
  const before = [];
  for (const name of names) {
    const from = path.join(source, name);
    const to = path.join(layout.targetDir, name);
    if (await pathExists(to, ops)) fail("ledger_conflict");
    await setPrivateMode(from, 0o600, "ledger_file", ops);
    before.push(await fileMetadata(from, name, ops));
  }
  const bytes = before.reduce((sum, item) => sum + item.size, 0);
  if (checkOnly) return { status:"ok", check_only:true, migrated_files:0, pending_files:names.length, migrated_bytes:0, pending_bytes:bytes, names_match:true, sizes_match:true, hashes_match:true, permissions_match:true, legacy_dir_removed:false };

  const moved = [];
  try {
    for (const item of before) {
      const from = path.join(source, item.name), to = path.join(layout.targetDir, item.name);
      await ops.rename(from, to);
      moved.push(item.name);
    }
    const after = [];
    for (const item of before) after.push(await fileMetadata(path.join(layout.targetDir, item.name), item.name, ops));
    if (after.length !== before.length || before.some((item, index) => !sameMetadata(item, after[index]))) fail("ledger_postcheck_failed");
  } catch (error) {
    for (const name of moved.reverse()) {
      await ops.rename(path.join(layout.targetDir, name), path.join(source, name)).catch(() => {});
    }
    if (error instanceof LedgerMigrationError) throw error;
    fail(error?.code === "EXDEV" ? "ledger_cross_device_blocked" : "ledger_move_failed");
  }

  let legacyDirRemoved = false;
  const remaining = await ops.readdir(source);
  if (remaining.length === 0) {
    try { await ops.rmdir(source); legacyDirRemoved = true; }
    catch { fail("ledger_legacy_remove_failed"); }
  }
  return { status:"ok", check_only:false, migrated_files:before.length, migrated_bytes:bytes, names_match:true, sizes_match:true, hashes_match:true, permissions_match:true, legacy_dir_removed:legacyDirRemoved };
}

function parseArgs(argv) {
  const options = {};
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === "--check-only") options.checkOnly = true;
    else if (["--project-root", "--source", "--target"].includes(arg)) {
      const value = argv[index + 1];
      if (!value) fail("invalid_arguments");
      index += 1;
      if (arg === "--project-root") options.projectRoot = value;
      if (arg === "--source") options.sourceDir = value;
      if (arg === "--target") options.targetDir = value;
    } else fail("invalid_arguments");
  }
  return options;
}

async function main() {
  try {
    const result = await migrateLegacyLedger(parseArgs(process.argv.slice(2)));
    process.stdout.write(`${JSON.stringify(result)}\n`);
  } catch (error) {
    const code = error instanceof LedgerMigrationError ? error.code : "unexpected_failure";
    process.stderr.write(`${JSON.stringify({ status:"blocked", reason:code })}\n`);
    process.exitCode = 1;
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) await main();
