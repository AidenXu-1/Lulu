#!/usr/bin/env node
import assert from "node:assert/strict";
import { chmod, lstat, mkdir, mkdtemp, readFile, rm, stat, symlink, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import { DEFAULT_FS_OPS, LEDGER_IGNORE_RULE, LedgerMigrationError, migrateLegacyLedger } from "./ledger_migration.mjs";

const DAILY_ENTRY = new URL("../../../../授权管理/Lulu授权码管理.command", import.meta.url);

async function layout(base, { ignored = true, target = false } = {}) {
  const root = path.join(base, "movable-project");
  const auth = path.join(root, "授权管理"), ledger = path.join(auth, "授权码台账");
  const sourceParent = path.join(base, "Documents"), source = path.join(sourceParent, "Lulu授权码台账");
  await mkdir(root, { recursive:true });
  await mkdir(auth, { mode:0o700 });
  await mkdir(source, { recursive:true, mode:0o700 });
  if (ignored) await writeFile(path.join(root, ".gitignore"), `${LEDGER_IGNORE_RULE}\n`, "utf8");
  if (target) await mkdir(ledger, { mode:0o700 });
  return { root, auth, ledger, sourceParent, source };
}

async function expectBlocked(promise, code) {
  await assert.rejects(promise, (error) => error instanceof LedgerMigrationError && error.code === code);
}

const temp = await mkdtemp(path.join(os.tmpdir(), "lulu-ledger-layout-"));
try {
  const success = await layout(path.join(temp, "success"));
  await writeFile(path.join(success.source, "one.csv"), "synthetic-one", { mode:0o644 });
  await writeFile(path.join(success.source, "two.csv"), "synthetic-two", { mode:0o600 });
  const moved = await migrateLegacyLedger({ projectRoot:success.root, sourceDir:success.source });
  assert.equal(moved.migrated_files, 2);
  assert.equal(moved.hashes_match && moved.sizes_match && moved.names_match && moved.permissions_match, true);
  assert.equal(moved.legacy_dir_removed, true);
  assert.equal((await stat(success.auth)).mode & 0o777, 0o700);
  assert.equal((await stat(success.ledger)).mode & 0o777, 0o700);
  assert.equal((await stat(path.join(success.ledger, "one.csv"))).mode & 0o777, 0o600);
  assert.equal((await stat(path.join(success.ledger, "two.csv"))).mode & 0o777, 0o600);

  const conflict = await layout(path.join(temp, "conflict"), { target:true });
  await writeFile(path.join(conflict.source, "same.csv"), "source", { mode:0o600 });
  await writeFile(path.join(conflict.ledger, "same.csv"), "target", { mode:0o600 });
  await expectBlocked(migrateLegacyLedger({ projectRoot:conflict.root, sourceDir:conflict.source }), "ledger_conflict");
  assert.equal(await readFile(path.join(conflict.source, "same.csv"), "utf8"), "source");
  assert.equal(await readFile(path.join(conflict.ledger, "same.csv"), "utf8"), "target");

  const sourceLink = await layout(path.join(temp, "source-link"));
  const realSource = path.join(temp, "source-link-real"); await mkdir(realSource); await rm(sourceLink.source, { recursive:true }); await symlink(realSource, sourceLink.source);
  await expectBlocked(migrateLegacyLedger({ projectRoot:sourceLink.root, sourceDir:sourceLink.source }), "ledger_source_symlink");

  const sourceParentLink = await layout(path.join(temp, "source-parent-link-base"));
  const linkedParent = path.join(temp, "source-parent-link"), realParent = path.join(temp, "source-parent-real");
  await mkdir(path.join(realParent, "Lulu授权码台账"), { recursive:true }); await symlink(realParent, linkedParent);
  await expectBlocked(migrateLegacyLedger({ projectRoot:sourceParentLink.root, sourceDir:path.join(linkedParent, "Lulu授权码台账") }), "ledger_source_parent_symlink");

  const targetLink = await layout(path.join(temp, "target-link"));
  const externalTarget = path.join(temp, "target-link-external"); await mkdir(externalTarget); await symlink(externalTarget, targetLink.ledger);
  await expectBlocked(migrateLegacyLedger({ projectRoot:targetLink.root, sourceDir:targetLink.source }), "ledger_target_symlink");

  const targetParentLinkBase = path.join(temp, "target-parent-link"), targetParentRoot = path.join(targetParentLinkBase, "movable-project");
  await mkdir(targetParentRoot, { recursive:true }); await writeFile(path.join(targetParentRoot, ".gitignore"), `${LEDGER_IGNORE_RULE}\n`, "utf8");
  const externalAuth = path.join(temp, "external-auth"); await mkdir(externalAuth); await symlink(externalAuth, path.join(targetParentRoot, "授权管理"));
  const targetParentSource = path.join(targetParentLinkBase, "Documents", "Lulu授权码台账"); await mkdir(targetParentSource, { recursive:true });
  await expectBlocked(migrateLegacyLedger({ projectRoot:targetParentRoot, sourceDir:targetParentSource }), "ledger_parent_symlink");

  const ignored = await layout(path.join(temp, "missing-ignore"), { ignored:false });
  await expectBlocked(migrateLegacyLedger({ projectRoot:ignored.root, sourceDir:ignored.source }), "ledger_gitignore_missing");
  await assert.rejects(lstat(ignored.ledger), (error) => error?.code === "ENOENT");

  const permission = await layout(path.join(temp, "permission"));
  await writeFile(path.join(permission.source, "safe.csv"), "safe", { mode:0o600 });
  const deniedOps = { ...DEFAULT_FS_OPS, chmod:async () => { const error = new Error("denied"); error.code = "EPERM"; throw error; } };
  await expectBlocked(migrateLegacyLedger({ projectRoot:permission.root, sourceDir:permission.source, ops:deniedOps }), "ledger_parent_permission_failed");
  assert.equal((await stat(path.join(permission.source, "safe.csv"))).isFile(), true);

  const filePermission = await layout(path.join(temp, "file-permission"));
  await writeFile(path.join(filePermission.source, "safe.csv"), "safe", { mode:0o644 });
  const deniedFileOps = {
    ...DEFAULT_FS_OPS,
    chmod:async (target, mode) => {
      if (String(target).endsWith(".csv")) { const error = new Error("denied"); error.code = "EPERM"; throw error; }
      return DEFAULT_FS_OPS.chmod(target, mode);
    },
  };
  await expectBlocked(migrateLegacyLedger({ projectRoot:filePermission.root, sourceDir:filePermission.source, ops:deniedFileOps }), "ledger_file_permission_failed");
  assert.equal((await stat(path.join(filePermission.source, "safe.csv"))).isFile(), true);

  const movedProject = await layout(path.join(temp, "nested", "after-project-move"));
  const checked = await migrateLegacyLedger({ projectRoot:movedProject.root, sourceDir:movedProject.source, checkOnly:true });
  assert.equal(checked.check_only, true);
  assert.equal(path.dirname(movedProject.ledger), path.join(movedProject.root, "授权管理"));
  const dailyEntry = await readFile(DAILY_ENTRY, "utf8");
  assert.match(dailyEntry, /SCRIPT_DIR=.*dirname/);
  assert.match(dailyEntry, /PROJECT_ROOT=.*SCRIPT_DIR\/\.\./);
  assert.doesNotMatch(dailyEntry, /Documents\/Lulu授权码台账/);

  process.stdout.write(`${JSON.stringify({
    status:"pass",
    production_requests:0,
    csv_body_reads_in_real_migration:0,
    success_files:2,
    conflict_no_overwrite:"ok",
    source_symlink:"rejected",
    source_parent_symlink:"rejected",
    target_symlink:"rejected",
    target_parent_symlink:"rejected",
    directory_permission_failure:"blocked_source_preserved",
    csv_permission_failure:"blocked_source_preserved",
    missing_gitignore:"blocked_before_ledger_create",
    moved_project_relative_entry:"ok",
  })}\n`);
} finally { await chmod(temp, 0o700).catch(() => {}); await rm(temp, { recursive:true, force:true }); }
