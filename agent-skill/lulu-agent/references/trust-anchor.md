# Verify the installed product

1. Accept only `/Applications/Lulu.app` and its `/Applications/Lulu.app/Contents/MacOS/lulu-agent-cli`, using the current-user service selected by the verified installation.
2. Run `python3 -B scripts/verify_lulu.py` from this Skill directory, with no arguments. It checks generation 9, committed installation materials, the exact external manifest bytes, the full App tree, constrained relative links and native signatures for delivery v2. It does not import App code or read Keychain. Core independently verifies HMAC ownership before business.
3. Continue only with `lulu.install-verification/v2`, `verified=true`, candidate generation 9 and CLI protocol `1.0.0`. Query actual installed capabilities next. Unknown versions fail closed.
4. This public Skill is optional and is not part of Lulu's signed installation. It does not require `~/.local/bin/lulu` or `~/.agents/skills/lulu-agent`. The App's bundled Skill resources remain part of the verified App tree.
5. Never discover an executable via PATH, another App, a socket, old Core or caller-provided identity. Missing Team ID is normal for the currently supported ad-hoc delivery; it is not evidence of author identity.
6. On mismatch stop with zero business effect and follow the recovery cases below. Never edit receipts or manufacture a manifest. Existing legacy managed copies in `~/.agents/skills/lulu-agent` must not be overwritten or removed manually; they belong to that installation's receipts.

This checks damage and mixed product versions within a trusted macOS account. It does not defend against malicious software that already controls that account. Independent Skill removal leaves product files and service untouched.

## Standalone App installation

The signed `standalone-install.v1.json` file supports descriptor v1 and v2. V1 resolves `AgentEntry/app-generations/<digest>`; v2 accepts only `storage_scheme=local-integrity-v1` and resolves `AgentEntry/local-app-generations/<digest>`. Verify the same closed receipts, complete signed tree and native Core identity in either format. Unknown schemes fail closed; the public projection never reads system credentials.

When the signed `standalone-install.v1.json` descriptor is present, resolve only its exact path-bound directory in the descriptor-specific namespace above. Verify the committed action, owner projection, verified-App receipt, complete signed tree, Core signature and service registration together; never fall back to an older DMG receipt if that generation is missing or damaged. The native Core authenticates ownership HMAC before serving commands; this read-only Python check does not read Keychain or claim to authenticate HMAC.

The public Skill can be upgraded separately from Lulu and does not depend on the optional shared shortcut. The App-bundled managed Skill additionally checks its installed file tree and the exact launcher template for that installation format. Use Python `-B` so the check does not create cache files inside managed Skill directories.

## Recovery by installation format

- **Standalone App, first preparation or system authorization unfinished:** open `/Applications/Lulu.app` normally and let the user finish any macOS authorization. If preparation fails again, preserve the reason and diagnostics and stop; do not loop through reinstall attempts. A malformed or unreadable standalone descriptor must not fall back to legacy recovery.
- **Standalone App, damaged files or inconsistent existing materials:** stop and preserve diagnostics for the developer. Re-copying the same App may leave damaged external records unchanged; do not promise that it repairs them. If a replacement is needed, use an authorized product update or the standard DMG drag-to-Applications flow. This format contains `Lulu.app` and `Applications`, with no separate “安装 Lulu” program. Never delete installation records or user data to force preparation.
- **Legacy installation without a standalone descriptor:** use its matching complete official delivery and its included installer, only when that legacy package actually provides one and the repair is authorized. Do not use legacy records to repair a standalone generation.
- **Only the independent Skill is outdated:** update it through its own documented manager, then verify Lulu again. Updating this Skill does not update App components. Do not repeatedly reinstall Lulu or change its managed Skill to repair an independent copy.

## One active Skill per host

When the host discovers both an independent and a Lulu-managed `lulu-agent`, select by absolute `SKILL.md` path, not by name or list order. After verifying the independent copy, use the host's supported per-path enable/disable setting to leave that copy enabled and the managed copy disabled. Reload discovery and confirm exactly one enabled `lulu-agent`. Keep the managed directory, shortcut and receipts intact. If the host cannot select or disable by path, stop and explain the ambiguity rather than assume which copy will run.
