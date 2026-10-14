# Fixed component check

Read before locating or invoking Lulu.

1. Accept only `/Applications/Lulu.app`, `~/.local/bin/lulu`, and the current-user service selected by the verified installation.
2. Run `python3 -B scripts/verify_lulu_generation9.py` with no arguments. For external delivery v2 it checks committed product materials, the exact external manifest bytes, the full signed App tree (including signature files and constrained relative links), both native signature checks, the installed Skill tree and fixed launcher. It does not import App code or read Keychain; Core independently verifies HMAC ownership before business.
3. Continue only when it returns `lulu.install-verification/v2` with `verified=true` and candidate generation 9.
4. Never discover Lulu through PATH, another App, an old Core, a socket, or caller-provided identity fields.
5. A missing certificate or Team ID is normal. An ad-hoc CDHash is diagnostic only and never author identity.
6. On mismatch, stop with zero business effect and follow the recovery cases below. Never create a separate access path, edit receipts, or manufacture a manifest.

This check prevents accidental damage and mixed versions in a trusted macOS account. It does not defend against malicious software that already controls the same account.

## Standalone App installation

The signed `standalone-install.v1.json` file supports descriptor v1 and v2. V1 resolves `AgentEntry/app-generations/<digest>`; v2 accepts only `storage_scheme=local-integrity-v1` and resolves `AgentEntry/local-app-generations/<digest>`. Verify the same closed receipts, complete signed tree and native Core identity in either format. Unknown schemes fail closed; the public projection never reads system credentials.

When the signed `standalone-install.v1.json` descriptor is present, resolve only its exact path-bound directory in the descriptor-specific namespace above. Verify the committed action, owner projection, verified-App receipt, complete signed tree, Core signature and service registration together; never fall back to an older DMG receipt if that generation is missing or damaged. The native Core authenticates ownership HMAC before serving commands; this read-only Python check does not read Keychain or claim to authenticate HMAC.

The public Skill can be upgraded separately from Lulu and does not depend on the optional shared shortcut. The App-bundled managed Skill additionally checks its installed file tree and the exact launcher template for that installation format. Use Python `-B` so the check does not create cache files inside managed Skill directories.

## Recovery by installation format

- **Standalone App, first preparation or system authorization unfinished:** open `/Applications/Lulu.app` normally and let the user finish any macOS authorization. If preparation fails again, preserve the reason and diagnostics and stop; do not loop through reinstall attempts. A malformed or unreadable standalone descriptor must not fall back to legacy recovery.
- **Standalone App, damaged files or inconsistent existing materials:** stop and preserve diagnostics for the developer. Re-copying the same App may leave damaged external records unchanged; do not promise that it repairs them. If a replacement is needed, use an authorized product update or the standard DMG drag-to-Applications flow. This format contains `Lulu.app` and `Applications`, with no separate “安装 Lulu” program. Never delete installation records or user data to force preparation.
- **Legacy installation without a standalone descriptor:** use its matching complete official delivery and its included installer, only when that legacy package actually provides one and the repair is authorized. Do not use legacy records to repair a standalone generation.
- **Only this managed Skill or shortcut is outdated:** Lulu Settings > CLI > 本机连接与命令参考 can install the resources bundled with that App. If those bundled resources still reject the current format, repeating this action cannot upgrade them. Use a verified current independent Skill, or wait for a product update that includes the fix; never manually overwrite the managed files.

## One active Skill per host

When the host discovers both an independent and a Lulu-managed `lulu-agent`, select by absolute `SKILL.md` path, not by name or list order. After verifying the independent copy, use the host's supported per-path enable/disable setting to leave that copy enabled and the managed copy disabled. Reload discovery and confirm exactly one enabled `lulu-agent`. Keep the managed directory, shortcut and receipts intact. If the host cannot select or disable by path, stop and explain the ambiguity rather than assume which copy will run.
