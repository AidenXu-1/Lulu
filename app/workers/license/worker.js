// Lulu license activation Worker.
// Source artifact only: configure D1 bindings and secrets outside the repo.
// Never paste real admin tokens, code hash peppers, lease peppers, plaintext
// license codes, rate-limit peppers, Cloudflare IDs, or account secrets into
// this file.

const JSON_HEADERS = {
  "content-type": "application/json; charset=utf-8",
  "cache-control": "no-store",
};
const ADMIN_NO_STORE_HEADERS = {
  "cache-control": "no-store, no-cache, max-age=0, must-revalidate",
  pragma: "no-cache",
  expires: "0",
};
const ENTITLEMENTS = ["lulu.full_access"];
const DEFAULT_MAX_DEVICES = 2;
const VERIFY_HOURS = 24;
const GRACE_DAYS = 7;
const FAILURE_WINDOW_SECONDS = 15 * 60;
const COOLDOWN_SECONDS = 30 * 60;
const MAX_FAILURES = 6;
const LIST_PAGE_SIZE = 50;
const MIN_LICENSE_CODE_LENGTH = 8;
const PURGE_PREVIEW_TTL_SECONDS = 5 * 60;
const PURGE_PREVIEW_PURPOSE = "license-purge-preview-v1";
const ALLOWED_LICENSE_STATUS = new Set(["active", "revoked", "expired", "suspended"]);
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function json(status, body, extraHeaders = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...JSON_HEADERS, ...extraHeaders },
  });
}

function html(status, body, extraHeaders = {}) {
  return new Response(body, {
    status,
    headers: { "content-type": "text/html; charset=utf-8", ...extraHeaders },
  });
}

function adminJson(status, body, extraHeaders = {}) {
  return json(status, body, { ...ADMIN_NO_STORE_HEADERS, ...extraHeaders });
}

function adminHtml(status, body, extraHeaders = {}) {
  return html(status, body, { ...ADMIN_NO_STORE_HEADERS, ...extraHeaders });
}

function adminNotFound() {
  return adminJson(404, { status: "not_found", message: "not found" });
}

function publicMessage(status) {
  return {
    invalid: "授权码无效,请检查后重试。",
    expired: "授权已过期,请联系授权方续期。",
    revoked: "授权已停用,请联系授权方。",
    suspended: "授权已暂时停用,请联系授权方。",
    rate_limited: "尝试次数过多,请稍后再试。",
    lease_invalid: "授权凭证已失效,请重新激活或联系授权方。",
    idempotency_conflict: "本次停用请求与原设备不一致,请重新确认本机授权状态。",
    server_error: "授权服务暂时不可用,请稍后重试。",
  }[status] || "授权不可用,请联系授权方。";
}

function deviceLimitMessage(activeDeviceCount, maxDevices) {
  return `当前授权已使用 ${activeDeviceCount}/${maxDevices} 台设备。请先在仍可使用的旧设备停用本机后重试；若旧设备已无法使用，请联系授权方重置设备。`;
}

function nowIso() {
  return new Date().toISOString();
}

function plusHours(hours) {
  const d = new Date();
  d.setUTCHours(d.getUTCHours() + hours);
  return d.toISOString();
}

function plusDays(days) {
  const d = new Date();
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString();
}

function isPast(value) {
  return Boolean(value && new Date(value) < new Date());
}

function sanitizeText(value, maxLength = 200) {
  return String(value || "").replace(/[\r\n\t]+/g, " ").slice(0, maxLength);
}

function normalizeLedgerField(value, maxLength = 80) {
  return sanitizeText(value, maxLength).replace(/[|:]/g, " ").trim();
}

function buildLedgerNote(fields) {
  return [
    ["给谁", normalizeLedgerField(fields.recipient_name, 80)],
    ["联系方式", normalizeLedgerField(fields.contact, 100)],
    ["批次", normalizeLedgerField(fields.batch, 80)],
    ["备注", normalizeLedgerField(fields.remark, 160)],
  ].map(([key, value]) => `${key}:${value}`).join(" | ");
}

function parseLedgerNote(note) {
  const result = { recipient_name: "", contact: "", batch: "", remark: "" };
  const text = String(note || "");
  const map = {
    "给谁": "recipient_name",
    "联系方式": "contact",
    "批次": "batch",
    "备注": "remark",
  };
  for (const part of text.split("|")) {
    const [rawKey, ...rest] = part.split(":");
    const key = String(rawKey || "").trim();
    const value = rest.join(":").trim();
    if (map[key]) result[map[key]] = value;
  }
  if (!result.remark && text && !text.includes("给谁:")) result.remark = text;
  return result;
}

function normalizeNullableIso(value) {
  if (value === undefined) return undefined;
  if (value === null || value === "") return null;
  const text = String(value).trim();
  if (!text) return null;
  const parsed = new Date(text);
  if (Number.isNaN(parsed.getTime())) return undefined;
  return parsed.toISOString();
}

function parsePositiveInteger(value) {
  if (typeof value !== "number" && typeof value !== "string") return null;
  if (typeof value === "string" && !/^[1-9][0-9]*$/.test(value.trim())) return null;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed >= 1 ? parsed : null;
}

function assertEnv(env) {
  if (!env.DB || !env.CODE_HASH_PEPPER || !env.LEASE_TOKEN_PEPPER || !env.RATE_LIMIT_PEPPER) {
    throw new Error("license worker is not configured");
  }
}

async function sha256Hex(input, pepper) {
  const bytes = new TextEncoder().encode(`${pepper}:${input}`);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function base64UrlEncode(bytes) {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

function base64UrlDecode(value) {
  if (!/^[A-Za-z0-9_-]+$/.test(value)) throw new Error("invalid base64url");
  const padded = value.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - value.length % 4) % 4);
  const binary = atob(padded);
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

async function purgeHmacKey(env, usages) {
  return crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(`lulu:${PURGE_PREVIEW_PURPOSE}:${env.CODE_HASH_PEPPER}`),
    { name: "HMAC", hash: "SHA-256" },
    false,
    usages
  );
}

async function signPurgePreview(payload, env) {
  const encodedPayload = base64UrlEncode(new TextEncoder().encode(JSON.stringify(payload)));
  const key = await purgeHmacKey(env, ["sign"]);
  const signature = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(encodedPayload));
  return `${encodedPayload}.${base64UrlEncode(new Uint8Array(signature))}`;
}

async function verifyPurgePreview(token, env) {
  const tokenText = String(token || "");
  const [encodedPayload, encodedSignature, ...extra] = tokenText.split(".");
  if (!encodedPayload || !encodedSignature || extra.length || tokenText.length > 2048) return null;
  try {
    const key = await purgeHmacKey(env, ["verify"]);
    const valid = await crypto.subtle.verify(
      "HMAC",
      key,
      base64UrlDecode(encodedSignature),
      new TextEncoder().encode(encodedPayload)
    );
    if (!valid) return null;
    const payload = JSON.parse(new TextDecoder().decode(base64UrlDecode(encodedPayload)));
    const nowSeconds = Math.floor(Date.now() / 1000);
    if (
      !payload ||
      payload.purpose !== PURGE_PREVIEW_PURPOSE ||
      !UUID_PATTERN.test(String(payload.license_id || "")) ||
      !ALLOWED_LICENSE_STATUS.has(payload.status) ||
      payload.status === "active" ||
      !Number.isSafeInteger(payload.issued_at) ||
      !Number.isSafeInteger(payload.expires_at) ||
      payload.issued_at > nowSeconds + 60 ||
      payload.expires_at < nowSeconds ||
      payload.expires_at - payload.issued_at !== PURGE_PREVIEW_TTL_SECONDS
    ) return null;
    const counts = payload.counts;
    for (const field of ["devices", "leases", "deactivation_operations"]) {
      if (!Number.isSafeInteger(counts?.[field]) || counts[field] < 0) return null;
    }
    if (typeof payload.deactivation_operations_table !== "boolean") return null;
    return payload;
  } catch {
    return null;
  }
}

async function codeHash(licenseCode, env) {
  return sha256Hex(String(licenseCode || "").trim(), env.CODE_HASH_PEPPER);
}

async function leaseHash(leaseToken, env) {
  return sha256Hex(String(leaseToken || "").trim(), env.LEASE_TOKEN_PEPPER);
}

async function deactivationRequestHash(requestId, env) {
  return sha256Hex(`deactivate-request:${String(requestId || "").trim()}`, env.RATE_LIMIT_PEPPER);
}

async function deactivationFingerprint(fields, env) {
  return sha256Hex(
    JSON.stringify([
      "deactivate-fingerprint",
      fields.licenseId,
      fields.deviceId,
      fields.deviceHash,
      fields.tokenHash,
    ]),
    env.RATE_LIMIT_PEPPER
  );
}

async function clientBucket(env, request, route, secondary = "") {
  const pepper = String(env.RATE_LIMIT_PEPPER || "").trim();
  if (!pepper) {
    throw new Error("license worker is not configured");
  }
  const cfIp = String(request.headers.get("cf-connecting-ip") || "").trim();
  const clientKey = cfIp || "missing-cf-connecting-ip";
  const secondaryHash = secondary ? await sha256Hex(secondary, pepper) : "";
  const bucketHash = await sha256Hex(`${route}:${clientKey}:${secondaryHash}`, pepper);
  return `${route}:sha256:${bucketHash.slice(0, 48)}`;
}

async function readRateLimit(env, bucket) {
  const row = await env.DB.prepare(
    "SELECT attempts, reset_at, cooldown_until FROM rate_limits WHERE bucket = ?"
  ).bind(bucket).first();
  if (!row) return { allowed: true, retryAfter: null };
  if (row.cooldown_until && new Date(row.cooldown_until) > new Date()) {
    const retryAfter = Math.max(1, Math.ceil((new Date(row.cooldown_until) - new Date()) / 1000));
    return { allowed: false, retryAfter };
  }
  return { allowed: true, retryAfter: null };
}

async function recordFailure(env, bucket) {
  const row = await env.DB.prepare(
    "SELECT attempts, reset_at FROM rate_limits WHERE bucket = ?"
  ).bind(bucket).first();
  const resetAt = row?.reset_at && new Date(row.reset_at) > new Date()
    ? row.reset_at
    : plusSeconds(FAILURE_WINDOW_SECONDS);
  const attempts = row?.reset_at && new Date(row.reset_at) > new Date() ? Number(row.attempts || 0) + 1 : 1;
  const cooldownUntil = attempts >= MAX_FAILURES ? plusSeconds(COOLDOWN_SECONDS) : null;
  await env.DB.prepare(
    "INSERT INTO rate_limits (bucket, attempts, reset_at, cooldown_until, updated_at) VALUES (?, ?, ?, ?, ?) " +
      "ON CONFLICT(bucket) DO UPDATE SET attempts = excluded.attempts, reset_at = excluded.reset_at, cooldown_until = excluded.cooldown_until, updated_at = excluded.updated_at"
  ).bind(bucket, attempts, resetAt, cooldownUntil, nowIso()).run();
  return cooldownUntil;
}

async function clearFailure(env, bucket) {
  await env.DB.prepare("DELETE FROM rate_limits WHERE bucket = ?").bind(bucket).run();
}

function plusSeconds(seconds) {
  const d = new Date();
  d.setUTCSeconds(d.getUTCSeconds() + seconds);
  return d.toISOString();
}

function auditStatement(env, eventType, fields = {}) {
  return env.DB.prepare(
    "INSERT INTO audit_events (id, license_id, device_id, event_type, reason, app_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
  ).bind(
    crypto.randomUUID(),
    fields.licenseId || null,
    fields.deviceId || null,
    eventType,
    sanitizeText(fields.reason || ""),
    sanitizeText(fields.appVersion || "", 60),
    nowIso()
  );
}

async function audit(env, eventType, fields = {}) {
  await auditStatement(env, eventType, fields).run();
}

async function readDeviceSummary(env, licenseId) {
  const row = await env.DB.prepare(
    "SELECT l.max_devices, " +
      "COALESCE(SUM(CASE WHEN d.status = 'active' THEN 1 ELSE 0 END), 0) AS active_device_count " +
      "FROM licenses l LEFT JOIN devices d ON d.license_id = l.id " +
      "WHERE l.id = ? GROUP BY l.id, l.max_devices"
  ).bind(licenseId).first();
  if (!row) return null;
  const maxDevices = parsePositiveInteger(row.max_devices);
  const activeDeviceCount = Number(row.active_device_count);
  if (
    maxDevices === null ||
    !Number.isSafeInteger(activeDeviceCount) ||
    activeDeviceCount < 0
  ) {
    return { valid: false, activeDeviceCount: null, maxDevices: null };
  }
  return { valid: true, activeDeviceCount, maxDevices };
}

async function licenseConfigurationError(env, licenseId, deviceId = null) {
  await audit(env, "license_config_error", {
    licenseId,
    deviceId,
    reason: "invalid_max_devices",
  });
  return json(500, { status: "server_error", message: publicMessage("server_error") });
}

async function deviceLimitDenied(env, bucket, licenseId, appVersion, summary) {
  const cooldownUntil = await recordFailure(env, bucket);
  await audit(env, "deny", {
    licenseId,
    appVersion,
    reason: "device_limit",
  });
  const retryAfter = cooldownUntil ? COOLDOWN_SECONDS : null;
  const headers = retryAfter ? { "retry-after": String(retryAfter) } : {};
  return json(403, {
    status: "device_limit",
    active_device_count: summary.activeDeviceCount,
    max_devices: summary.maxDevices,
    message: deviceLimitMessage(summary.activeDeviceCount, summary.maxDevices),
    ...(retryAfter ? { retry_after: retryAfter } : {}),
  }, headers);
}

function entitlementsFor(license) {
  if (!license?.entitlements_json) return ENTITLEMENTS;
  try {
    const parsed = JSON.parse(license.entitlements_json);
    return Array.isArray(parsed) ? parsed.map(String) : ENTITLEMENTS;
  } catch {
    return ENTITLEMENTS;
  }
}

async function issueLease(env, license, device) {
  const token = crypto.randomUUID();
  const tokenHash = await leaseHash(token, env);
  const expiresAt = plusDays(GRACE_DAYS);
  await env.DB.prepare(
    "INSERT INTO lease_tokens (id, license_id, device_id, token_hash, status, issued_at, expires_at, last_seen_at) VALUES (?, ?, ?, ?, 'active', ?, ?, ?)"
  ).bind(crypto.randomUUID(), license.id, device.id, tokenHash, nowIso(), expiresAt, nowIso()).run();
  return { token, expiresAt };
}

async function revokeActiveLeases(env, licenseId, deviceId, reason, options = {}) {
  const statement = env.DB.prepare(
    "UPDATE lease_tokens SET status = 'revoked', revoked_at = ?, revoke_reason = ? " +
      "WHERE license_id = ? AND device_id = ? AND status = 'active'"
  ).bind(
    options.timestamp || nowIso(),
    sanitizeText(reason, 80),
    licenseId,
    deviceId
  );
  if (options.deferred === true) return statement;
  await statement.run();
}

async function successPayload(env, license, device, message = "授权已激活。") {
  const summary = await readDeviceSummary(env, license.id);
  if (!summary?.valid) {
    return licenseConfigurationError(env, license.id, device.id);
  }
  const lease = await issueLease(env, license, device);
  return json(200, {
    status: "ok",
    identity_id: license.owner_identity_id || device.identity_id || "anon",
    identity_kind: "anonymous",
    license_id: license.id,
    device_id: device.id,
    entitlements: entitlementsFor(license),
    lease_token: lease.token,
    server_time: nowIso(),
    next_check_at: plusHours(VERIFY_HOURS),
    grace_until: lease.expiresAt,
    active_device_count: summary.activeDeviceCount,
    max_devices: summary.maxDevices,
    message,
  });
}

async function activate(request, env) {
  assertEnv(env);
  const body = await request.json().catch(() => ({}));
  const licenseCode = String(body.license_code || "").trim();
  const deviceHash = String(body.device_hash || "").trim();
  const appVersion = sanitizeText(body.app_version || "", 60);
  const platform = sanitizeText(body.platform || "", 60);
  if (!licenseCode || !deviceHash) return deny(env, request, "activate", "invalid", 400, appVersion);

  const hash = await codeHash(licenseCode, env);
  const bucket = await clientBucket(env, request, "activate", hash);
  const limit = await readRateLimit(env, bucket);
  if (!limit.allowed) {
    return json(429, {
      status: "rate_limited",
      retry_after: limit.retryAfter,
      message: publicMessage("rate_limited"),
    }, { "retry-after": String(limit.retryAfter) });
  }

  const license = await env.DB.prepare("SELECT * FROM licenses WHERE code_hash = ?").bind(hash).first();
  if (!license) return deny(env, request, "activate", "invalid", 403, appVersion, bucket);
  const inactiveStatus = inactiveLicenseStatus(license);
  if (inactiveStatus) return deny(env, request, "activate", inactiveStatus, 403, appVersion, bucket, license.id);

  const existing = await env.DB.prepare(
    "SELECT * FROM devices WHERE license_id = ? AND device_hash = ?"
  ).bind(license.id, deviceHash).first();
  if (existing && existing.status === "active") {
    await clearFailure(env, bucket);
    await env.DB.prepare("UPDATE devices SET last_seen_at = ?, last_app_version = ? WHERE id = ?")
      .bind(nowIso(), appVersion, existing.id).run();
    await audit(env, "activate_idempotent", { licenseId: license.id, deviceId: existing.id, appVersion });
    return successPayload(env, license, existing, "授权已激活。");
  }

  const summary = await readDeviceSummary(env, license.id);
  if (!summary?.valid) return licenseConfigurationError(env, license.id, existing?.id || null);
  if (summary.activeDeviceCount >= summary.maxDevices) {
    return deviceLimitDenied(env, bucket, license.id, appVersion, summary);
  }

  if (existing && existing.status === "revoked") {
    try {
      await env.DB.prepare(
        "UPDATE devices SET status = 'active', platform = ?, last_seen_at = ?, last_app_version = ? WHERE id = ?"
      ).bind(platform, nowIso(), appVersion, existing.id).run();
    } catch (error) {
      const latest = await readDeviceSummary(env, license.id);
      if (latest?.valid && (
        String(error).includes("device_limit") ||
        latest.activeDeviceCount >= latest.maxDevices
      )) {
        return deviceLimitDenied(env, bucket, license.id, appVersion, latest);
      }
      throw error;
    }
    await clearFailure(env, bucket);
    await audit(env, "activate_reactivated", { licenseId: license.id, deviceId: existing.id, appVersion });
    return successPayload(env, license, { ...existing, status: "active", platform }, "授权已重新激活。");
  }

  const identityId = license.owner_identity_id || crypto.randomUUID();
  const deviceId = crypto.randomUUID();
  await env.DB.prepare(
    "INSERT OR IGNORE INTO identities (id, kind, created_at) VALUES (?, 'anonymous', ?)"
  ).bind(identityId, nowIso()).run();
  await env.DB.prepare(
    "UPDATE licenses SET owner_identity_id = COALESCE(owner_identity_id, ?) WHERE id = ?"
  ).bind(identityId, license.id).run();
  try {
    await env.DB.prepare(
      "INSERT INTO devices (id, identity_id, license_id, device_hash, device_label, platform, status, first_seen_at, last_seen_at, last_app_version) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)"
    ).bind(deviceId, identityId, license.id, deviceHash, "Lulu device", platform, nowIso(), nowIso(), appVersion).run();
  } catch (error) {
    const racedExisting = await env.DB.prepare(
      "SELECT * FROM devices WHERE license_id = ? AND device_hash = ?"
    ).bind(license.id, deviceHash).first();
    if (racedExisting?.status === "active") {
      await clearFailure(env, bucket);
      await audit(env, "activate_idempotent", {
        licenseId: license.id,
        deviceId: racedExisting.id,
        appVersion,
      });
      return successPayload(env, license, racedExisting, "授权已激活。");
    }
    const latest = await readDeviceSummary(env, license.id);
    if (latest?.valid && (
      String(error).includes("device_limit") ||
      latest.activeDeviceCount >= latest.maxDevices
    )) {
      return deviceLimitDenied(env, bucket, license.id, appVersion, latest);
    }
    throw error;
  }
  await clearFailure(env, bucket);
  await audit(env, "activate", { licenseId: license.id, deviceId, appVersion });
  return successPayload(env, { ...license, owner_identity_id: identityId }, { id: deviceId, identity_id: identityId }, "授权已激活。");
}

async function verify(request, env) {
  assertEnv(env);
  const body = await request.json().catch(() => ({}));
  const licenseId = String(body.license_id || "").trim();
  const deviceId = String(body.device_id || "").trim();
  const deviceHash = String(body.device_hash || "").trim();
  const leaseToken = String(body.lease_token || "").trim();
  const appVersion = sanitizeText(body.app_version || "", 60);
  const bucket = await clientBucket(env, request, "verify", licenseId);
  const limit = await readRateLimit(env, bucket);
  if (!limit.allowed) {
    return json(429, {
      status: "rate_limited",
      retry_after: limit.retryAfter,
      message: publicMessage("rate_limited"),
    }, { "retry-after": String(limit.retryAfter) });
  }
  if (!licenseId || !deviceId || !deviceHash || !leaseToken) return deny(env, request, "verify", "invalid", 400, appVersion, bucket);

  const license = await env.DB.prepare("SELECT * FROM licenses WHERE id = ?").bind(licenseId).first();
  const device = await env.DB.prepare(
    "SELECT * FROM devices WHERE id = ? AND license_id = ? AND device_hash = ?"
  ).bind(deviceId, licenseId, deviceHash).first();
  if (!license || !device || device.status !== "active") {
    return deny(env, request, "verify", "invalid", 403, appVersion, bucket, licenseId, deviceId);
  }
  const inactiveStatus = inactiveLicenseStatus(license);
  if (inactiveStatus) return deny(env, request, "verify", inactiveStatus, 403, appVersion, bucket, licenseId, deviceId);

  const tokenHash = await leaseHash(leaseToken, env);
  const lease = await env.DB.prepare(
    "SELECT * FROM lease_tokens WHERE license_id = ? AND device_id = ? AND token_hash = ? ORDER BY issued_at DESC LIMIT 1"
  ).bind(licenseId, deviceId, tokenHash).first();
  if (!lease || lease.status !== "active" || isPast(lease.expires_at)) {
    await recordFailure(env, bucket);
    await audit(env, "deny", { licenseId, deviceId, appVersion, reason: "lease_invalid" });
    return json(403, { status: "lease_invalid", message: publicMessage("lease_invalid") });
  }

  await clearFailure(env, bucket);
  await env.DB.prepare("UPDATE devices SET last_seen_at = ?, last_app_version = ? WHERE id = ?")
    .bind(nowIso(), appVersion, deviceId).run();
  await env.DB.prepare("UPDATE lease_tokens SET last_seen_at = ? WHERE id = ?").bind(nowIso(), lease.id).run();
  return successPayload(env, license, device, "授权复验成功。");
}

async function readDeactivationOperation(env, requestKeyHash) {
  return env.DB.prepare(
    "SELECT license_id, device_id, request_fingerprint, status " +
      "FROM device_deactivation_operations WHERE request_key_hash = ?"
  ).bind(requestKeyHash).first();
}

async function currentDeactivationCredentialIsValid(env, fields, timestamp) {
  const row = await env.DB.prepare(
    "SELECT l.id AS license_id, d.id AS device_id " +
      "FROM licenses l " +
      "JOIN devices d ON d.license_id = l.id " +
      "JOIN lease_tokens lt ON lt.license_id = l.id AND lt.device_id = d.id " +
      "WHERE l.id = ? AND l.status = 'active' AND (l.expires_at IS NULL OR l.expires_at > ?) " +
      "AND d.id = ? AND d.device_hash = ? AND d.status = 'active' " +
      "AND lt.token_hash = ? AND lt.status = 'active' AND lt.expires_at > ? " +
      "LIMIT 1"
  ).bind(
    fields.licenseId,
    timestamp,
    fields.deviceId,
    fields.deviceHash,
    fields.tokenHash,
    timestamp
  ).first();
  if (!row) return null;
  return {
    licenseId: String(row.license_id),
    deviceId: String(row.device_id),
  };
}

async function deactivationDenied(
  env,
  bucket,
  reason = "lease_invalid",
  statusCode = 403,
  authenticatedIdentity = {}
) {
  const cooldownUntil = await recordFailure(env, bucket);
  await audit(env, "deny", {
    licenseId: authenticatedIdentity.licenseId || null,
    deviceId: authenticatedIdentity.deviceId || null,
    reason,
  });
  const retryAfter = cooldownUntil ? COOLDOWN_SECONDS : null;
  const headers = retryAfter ? { "retry-after": String(retryAfter) } : {};
  return json(statusCode, {
    status: reason,
    message: publicMessage(reason),
    ...(retryAfter ? { retry_after: retryAfter } : {}),
  }, headers);
}

async function deactivationServerError(env, authenticatedIdentity = {}) {
  await audit(env, "deny", {
    licenseId: authenticatedIdentity.licenseId || null,
    deviceId: authenticatedIdentity.deviceId || null,
    reason: "server_error",
  });
  return json(500, { status: "server_error", message: publicMessage("server_error") });
}

async function deactivationResult(env, bucket, fields, replayed) {
  const summary = await readDeviceSummary(env, fields.licenseId);
  if (!summary?.valid) {
    return licenseConfigurationError(env, fields.licenseId, fields.deviceId);
  }
  await clearFailure(env, bucket);
  return json(200, {
    status: "ok",
    deactivation_status: "stopped",
    replayed,
    active_device_count: summary.activeDeviceCount,
    max_devices: summary.maxDevices,
    message: "本机已停用，设备名额已释放。",
  });
}

async function resolveDeactivationOperation(env, bucket, fields, operation, replayed) {
  if (
    operation.license_id !== fields.licenseId ||
    operation.device_id !== fields.deviceId ||
    operation.request_fingerprint !== fields.requestFingerprint
  ) {
    return deactivationDenied(env, bucket, "idempotency_conflict", 409, {
      licenseId: operation.license_id,
      deviceId: operation.device_id,
    });
  }
  if (operation.status !== "committed") {
    return deactivationServerError(env, {
      licenseId: operation.license_id,
      deviceId: operation.device_id,
    });
  }
  return deactivationResult(env, bucket, {
    ...fields,
    licenseId: operation.license_id,
    deviceId: operation.device_id,
  }, replayed);
}

async function deactivate(request, env) {
  assertEnv(env);
  const body = await request.json().catch(() => ({}));
  const licenseId = String(body.license_id || "").trim();
  const deviceId = String(body.device_id || "").trim();
  const deviceHash = String(body.device_hash || "").trim();
  const leaseToken = String(body.lease_token || "").trim();
  const requestId = String(body.request_id || "").trim();
  const bucket = await clientBucket(env, request, "deactivate", licenseId);
  const limit = await readRateLimit(env, bucket);
  if (!limit.allowed) {
    return json(429, {
      status: "rate_limited",
      retry_after: limit.retryAfter,
      message: publicMessage("rate_limited"),
    }, { "retry-after": String(limit.retryAfter) });
  }
  if (!licenseId || !deviceId || !deviceHash || !leaseToken || !UUID_PATTERN.test(requestId)) {
    return deactivationDenied(env, bucket, "invalid", 400);
  }

  const tokenHash = await leaseHash(leaseToken, env);
  const requestKeyHash = await deactivationRequestHash(requestId, env);
  const requestFingerprint = await deactivationFingerprint({
    licenseId,
    deviceId,
    deviceHash,
    tokenHash,
  }, env);
  const fields = {
    licenseId,
    deviceId,
    deviceHash,
    tokenHash,
    requestKeyHash,
    requestFingerprint,
  };

  const existingOperation = await readDeactivationOperation(env, requestKeyHash);
  if (existingOperation) {
    return resolveDeactivationOperation(env, bucket, fields, existingOperation, true);
  }

  const timestamp = nowIso();
  const authenticatedIdentity = await currentDeactivationCredentialIsValid(env, fields, timestamp);
  if (!authenticatedIdentity) {
    return deactivationDenied(env, bucket, "lease_invalid", 403);
  }

  const operationId = crypto.randomUUID();
  const auditEventId = crypto.randomUUID();
  const revokeLeasesStatement = await revokeActiveLeases(
    env,
    authenticatedIdentity.licenseId,
    authenticatedIdentity.deviceId,
    "user_self_deactivate",
    { deferred: true, timestamp }
  );
  const statements = [
    env.DB.prepare(
      "INSERT INTO device_deactivation_operations " +
        "(id, license_id, device_id, request_key_hash, request_fingerprint, status, audit_event_id, created_at, committed_at) " +
        "VALUES (?, ?, ?, ?, ?, CASE WHEN EXISTS (" +
          "SELECT 1 FROM licenses l " +
          "JOIN devices d ON d.license_id = l.id " +
          "JOIN lease_tokens lt ON lt.license_id = l.id AND lt.device_id = d.id " +
          "WHERE l.id = ? AND l.status = 'active' AND (l.expires_at IS NULL OR l.expires_at > ?) " +
          "AND d.id = ? AND d.device_hash = ? AND d.status = 'active' " +
          "AND lt.token_hash = ? AND lt.status = 'active' AND lt.expires_at > ?" +
        ") THEN 'pending' ELSE 'invalid' END, ?, ?, NULL)"
    ).bind(
      operationId,
      authenticatedIdentity.licenseId,
      authenticatedIdentity.deviceId,
      requestKeyHash,
      requestFingerprint,
      licenseId,
      timestamp,
      deviceId,
      deviceHash,
      tokenHash,
      timestamp,
      auditEventId,
      timestamp
    ),
    revokeLeasesStatement,
    env.DB.prepare(
      "UPDATE devices SET status = 'revoked', last_seen_at = ? " +
        "WHERE license_id = ? AND id = ? AND device_hash = ? AND status = 'active'"
    ).bind(
      timestamp,
      authenticatedIdentity.licenseId,
      authenticatedIdentity.deviceId,
      deviceHash
    ),
    env.DB.prepare(
      "INSERT INTO audit_events " +
        "(id, license_id, device_id, event_type, reason, app_version, created_at) " +
        "VALUES (?, ?, ?, 'self_deactivate_committed', ?, '', ?)"
    ).bind(
      auditEventId,
      authenticatedIdentity.licenseId,
      authenticatedIdentity.deviceId,
      `operation:${operationId}`,
      timestamp
    ),
    env.DB.prepare(
      "UPDATE device_deactivation_operations SET status = 'committed', committed_at = ? " +
        "WHERE id = ? AND status = 'pending'"
    ).bind(timestamp, operationId),
  ];

  try {
    await env.DB.batch(statements);
    return deactivationResult(env, bucket, fields, false);
  } catch {
    const recoveredOperation = await readDeactivationOperation(env, requestKeyHash);
    if (recoveredOperation) {
      return resolveDeactivationOperation(env, bucket, fields, recoveredOperation, true);
    }
    if (!(await currentDeactivationCredentialIsValid(env, fields, nowIso()))) {
      return deactivationDenied(env, bucket, "lease_invalid", 403);
    }
    return deactivationServerError(env, authenticatedIdentity);
  }
}

function inactiveLicenseStatus(license) {
  if (license.status && license.status !== "active") return license.status;
  if (isPast(license.expires_at)) return "expired";
  return null;
}

async function deny(env, request, route, reason, statusCode, appVersion, bucket = null, licenseId = null, deviceId = null) {
  const selectedBucket = bucket || await clientBucket(env, request, route);
  const cooldownUntil = await recordFailure(env, selectedBucket);
  await audit(env, "deny", { licenseId, deviceId, appVersion, reason });
  const headers = cooldownUntil ? { "retry-after": String(COOLDOWN_SECONDS) } : {};
  return json(statusCode, { status: reason, message: publicMessage(reason) }, headers);
}

function splitCsv(value) {
  return String(value || "")
    .split(",")
    .map((item) => item.trim().toLowerCase())
    .filter(Boolean);
}

async function requireAdmin(request, env, options = {}) {
  const requestHost = new URL(request.url).hostname.toLowerCase();
  const explicitTestIdentity = (
    env.ADMIN_WEB_DEV_IDENTITY === "true" &&
    env.LULU_LICENSE_ENV === "test" &&
    (requestHost === "invalid" || requestHost.endsWith(".invalid"))
  );
  if (explicitTestIdentity) {
    const devIdentity = (request.headers.get("x-lulu-admin-dev-identity") || "").trim();
    if (devIdentity && devIdentity === env.ADMIN_DEV_IDENTITY) return true;
  }

  if (options.allowToken !== false) {
    const expected = env.ADMIN_TOKEN;
    const provided = request.headers.get("x-admin-token") || "";
    if (expected && provided === expected) return true;
  }
  return false;
}

function parseOrigin(value) {
  try {
    return new URL(value).origin.toLowerCase();
  } catch {
    return "";
  }
}

function allowedAdminOrigin(request, env) {
  const requestOrigin = new URL(request.url).origin.toLowerCase();
  const origin = parseOrigin(request.headers.get("origin") || "");
  if (!origin) return false;
  if (origin === requestOrigin) return true;
  return splitCsv(env.ADMIN_ALLOWED_ORIGINS).includes(origin);
}

async function requireAdminMutation(request, env) {
  if (!(await requireAdmin(request, env))) {
    return { ok: false, response: adminJson(403, { status: "forbidden", message: "forbidden" }) };
  }
  const csrfProof = (request.headers.get("x-lulu-admin-csrf") || "").trim();
  if (!allowedAdminOrigin(request, env) || csrfProof !== "same-origin") {
    return { ok: false, response: adminJson(403, { status: "forbidden", message: "forbidden" }) };
  }
  return { ok: true, response: null };
}

function adminPage() {
  return adminHtml(200, `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Lulu 授权码管理后台</title>
  <style>
    :root {
      color: #332d28;
      background: #f8f4ee;
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", "Microsoft YaHei", sans-serif;
    }
    body { margin: 0; padding: 28px; }
    main { max-width: 1120px; margin: 0 auto; }
    h1 { margin: 0 0 8px; font-size: 28px; }
    p { color: #877b6f; line-height: 1.7; }
    .grid { display: grid; grid-template-columns: minmax(300px, 380px) 1fr; gap: 18px; align-items: start; }
    .card { background: #fffdf9; border: 1px solid #eadfd3; border-radius: 14px; padding: 18px; box-shadow: 0 12px 30px rgba(86, 62, 39, 0.08); }
    label { display: block; margin: 12px 0 6px; font-weight: 700; color: #5b5048; }
    input, select, textarea { width: 100%; box-sizing: border-box; border: 1px solid #e2d7ca; border-radius: 10px; padding: 10px 12px; background: #fffaf4; color: #332d28; font: inherit; }
    textarea { min-height: 74px; resize: vertical; }
    button { border: 0; border-radius: 10px; padding: 10px 14px; background: #d97043; color: #fff; font-weight: 800; cursor: pointer; }
    button.secondary { background: #eee8df; color: #574d45; }
    button.warning { background: #a94f3b; }
    button:disabled { opacity: .55; cursor: not-allowed; }
    .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
    .status { margin: 14px 0; padding: 10px 12px; border-radius: 10px; background: #f6eadf; color: #7a432b; min-height: 20px; }
    table { width: 100%; border-collapse: collapse; font-size: 14px; }
    th, td { text-align: left; border-bottom: 1px solid #eee5dc; padding: 10px 8px; vertical-align: top; }
    th { color: #8a7b6c; font-size: 12px; letter-spacing: .02em; }
    code { background: #f7ece2; color: #c15f3c; padding: 2px 5px; border-radius: 6px; }
    .muted { color: #9b8f83; font-size: 13px; }
    .actions { min-width: 230px; }
    .actions .row { gap: 6px; }
    .inline-input { width: 96px; padding: 7px 8px; }
    .nowrap { white-space: nowrap; }
    .ledger-output { display: none; margin-top: 16px; padding-top: 14px; border-top: 1px solid #eee5dc; }
    .ledger-output textarea { min-height: 150px; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 12px; }
  </style>
</head>
<body>
<main>
  <h1>Lulu 授权码管理后台</h1>
  <p>极简源码态后台。页面只依赖 Worker 侧受保护身份门禁,不会要求浏览器保存或发送长期管理密钥。新增授权码后只显示 license_id,后台只保存哈希;明文授权码只在创建成功当次用于本地 CSV 台账,刷新后不可恢复。</p>
  <div class="status" id="notice">已进入受保护管理页,可刷新列表。</div>
  <section class="grid">
    <div class="card">
      <h2>新增授权码</h2>
      <label for="licenseCode">明文授权码</label>
      <input id="licenseCode" type="password" autocomplete="new-password" placeholder="创建后不会保存或再次展示">
      <label for="maxDevices">设备数</label>
      <input id="maxDevices" type="number" min="1" step="1" value="2">
      <label for="expiresAt">过期时间</label>
      <input id="expiresAt" type="datetime-local">
      <label for="recipientName">给谁</label>
      <input id="recipientName" type="text" autocomplete="off" placeholder="例如: 张三">
      <label for="contact">联系方式/微信名</label>
      <input id="contact" type="text" autocomplete="off" placeholder="例如: 微信 zhangsan">
      <label for="batch">批次</label>
      <input id="batch" type="text" autocomplete="off" placeholder="例如: 第1批朋友内测">
      <label for="remark">备注</label>
      <textarea id="remark" placeholder="例如: 7月内测 / 月卡 / 需回访"></textarea>
      <button id="createLicense" style="margin-top: 14px;">创建授权码</button>
      <p class="muted">创建结果只返回 license_id;明文授权码不会出现在列表、日志或历史记录里。</p>
      <div class="ledger-output" id="ledgerOutput">
        <h3>一次性 CSV 台账</h3>
        <p class="muted">只在本次创建后生成。请立即下载或复制;刷新页面后无法恢复明文授权码。</p>
        <textarea id="ledgerCsv" readonly></textarea>
        <div class="row" style="margin-top: 10px;">
          <button id="downloadCsv" class="secondary" type="button">下载 CSV</button>
          <button id="copyCsv" class="secondary" type="button">复制 CSV</button>
          <button id="clearCsv" class="secondary" type="button">清空本地台账</button>
        </div>
      </div>
    </div>
    <div class="card">
      <div class="row" style="justify-content: space-between;">
        <h2>授权码列表</h2>
        <button id="refresh" class="secondary">刷新列表</button>
      </div>
      <table>
        <thead>
          <tr>
            <th>license_id</th>
            <th>归属</th>
            <th>批次</th>
            <th>状态</th>
            <th>设备数</th>
            <th>已激活</th>
            <th>过期时间</th>
            <th>备注</th>
            <th>创建时间</th>
            <th class="actions">操作</th>
          </tr>
        </thead>
        <tbody id="licenseRows"></tbody>
      </table>
    </div>
  </section>
</main>
<script>
const notice = document.getElementById("notice");
const rows = document.getElementById("licenseRows");

function show(message) { notice.textContent = message; }
function safeError(data, fallback) { return data && data.message ? data.message : fallback; }
function isoFromLocal(value) { return value ? new Date(value).toISOString() : null; }
function display(value) { return value || "未设置"; }
function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[char]);
}
function csvCell(value) {
  return '"' + String(value ?? "").replace(/"/g, '""') + '"';
}
function buildLedgerCsv(row) {
  const headers = ["明文授权码","license_id","给谁","联系方式","批次","设备数","创建时间","状态","备注"];
  const values = [
    row.license_code,
    row.license_id,
    row.recipient_name,
    row.contact,
    row.batch,
    row.max_devices,
    row.created_at,
    row.status,
    row.remark,
  ];
  return headers.map(csvCell).join(",") + "\\n" + values.map(csvCell).join(",") + "\\n";
}
function showLedgerCsv(csv) {
  document.getElementById("ledgerCsv").value = csv;
  document.getElementById("ledgerOutput").style.display = "block";
}
function clearLedgerCsv() {
  document.getElementById("ledgerCsv").value = "";
  document.getElementById("ledgerOutput").style.display = "none";
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "x-lulu-admin-csrf": "same-origin",
      ...(options.headers || {}),
    },
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(safeError(data, "请求失败,请检查输入或稍后重试。"));
  return data;
}

function render(items) {
  rows.innerHTML = "";
  for (const item of items) {
    const tr = document.createElement("tr");
    tr.innerHTML = \`
      <td><code>\${escapeHtml(item.license_id)}</code></td>
      <td>
        <input type="text" data-role="recipient" data-id="\${escapeHtml(item.license_id)}" value="\${escapeHtml(item.recipient_name)}" placeholder="给谁">
        <div class="muted">\${escapeHtml(display(item.contact))}</div>
        <input type="text" data-role="contact" data-id="\${escapeHtml(item.license_id)}" value="\${escapeHtml(item.contact)}" placeholder="联系方式">
      </td>
      <td><input type="text" data-role="batch" data-id="\${escapeHtml(item.license_id)}" value="\${escapeHtml(item.batch)}" placeholder="批次"></td>
      <td>
        <select data-role="status" data-id="\${escapeHtml(item.license_id)}">
          \${["active","revoked","suspended","expired"].map(s => \`<option value="\${s}" \${item.status === s ? "selected" : ""}>\${s}</option>\`).join("")}
        </select>
      </td>
      <td><input class="inline-input" type="number" min="1" step="1" data-role="max" data-id="\${escapeHtml(item.license_id)}" value="\${Number(item.max_devices)}"></td>
      <td class="nowrap">\${Number(item.active_device_count || 0)}</td>
      <td><input type="text" data-role="expires" data-id="\${escapeHtml(item.license_id)}" value="\${escapeHtml(item.expires_at)}" placeholder="ISO 或空"></td>
      <td><input type="text" data-role="remark" data-id="\${escapeHtml(item.license_id)}" value="\${escapeHtml(item.remark)}" placeholder="备注"></td>
      <td class="nowrap">\${escapeHtml(display(item.created_at))}</td>
      <td class="actions">
        <div class="row">
          <button data-action="save" data-id="\${escapeHtml(item.license_id)}" class="secondary">保存修改</button>
          <button data-action="reset" data-id="\${escapeHtml(item.license_id)}" class="warning">重置设备</button>
        </div>
      </td>\`;
    rows.appendChild(tr);
  }
}

async function refresh() {
  show("正在读取列表...");
  const data = await api("/admin/licenses?limit=50&offset=0");
  render(data.items || []);
  show(\`已读取 \${(data.items || []).length} 条授权记录。列表不会显示历史明文授权码。\`);
}

async function createLicense() {
  const code = document.getElementById("licenseCode").value.trim();
  const max = Number(document.getElementById("maxDevices").value || 2);
  const expires = isoFromLocal(document.getElementById("expiresAt").value);
  const recipient = document.getElementById("recipientName").value.trim();
  const contact = document.getElementById("contact").value.trim();
  const batch = document.getElementById("batch").value.trim();
  const remark = document.getElementById("remark").value.trim();
  show("正在创建授权码...");
  const data = await api("/admin/licenses", {
    method: "POST",
    body: JSON.stringify({
      license_code: code,
      max_devices: max,
      expires_at: expires,
      recipient_name: recipient,
      contact,
      batch,
      remark,
    }),
  });
  showLedgerCsv(buildLedgerCsv({
    license_code: code,
    license_id: data.license_id,
    recipient_name: recipient,
    contact,
    batch,
    max_devices: max,
    created_at: data.created_at,
    status: "active",
    remark,
  }));
  document.getElementById("licenseCode").value = "";
  document.getElementById("recipientName").value = "";
  document.getElementById("contact").value = "";
  document.getElementById("batch").value = "";
  document.getElementById("remark").value = "";
  show(\`创建成功: license_id=\${data.license_id}; 明文授权码未保存。\`);
  await refresh();
}

async function saveLicense(id) {
  const status = document.querySelector(\`[data-role="status"][data-id="\${id}"]\`).value;
  const max = Number(document.querySelector(\`[data-role="max"][data-id="\${id}"]\`).value || 1);
  const expiresRaw = document.querySelector(\`[data-role="expires"][data-id="\${id}"]\`).value.trim();
  const recipient = document.querySelector(\`[data-role="recipient"][data-id="\${id}"]\`).value.trim();
  const contact = document.querySelector(\`[data-role="contact"][data-id="\${id}"]\`).value.trim();
  const batch = document.querySelector(\`[data-role="batch"][data-id="\${id}"]\`).value.trim();
  const remark = document.querySelector(\`[data-role="remark"][data-id="\${id}"]\`).value.trim();
  await api(\`/admin/licenses/\${encodeURIComponent(id)}\`, {
    method: "PATCH",
    body: JSON.stringify({ status, max_devices: max, expires_at: expiresRaw || null, recipient_name: recipient, contact, batch, remark }),
  });
  show(\`已保存 \${id}。\`);
  await refresh();
}

async function resetDevices(id) {
  if (!confirm("确认重置该授权码的全部设备? 旧设备需要重新激活。")) return;
  await api(\`/admin/licenses/\${encodeURIComponent(id)}/devices/reset\`, {
    method: "POST",
    body: JSON.stringify({}),
  });
  show(\`已重置 \${id} 的全部设备。\`);
  await refresh();
}

document.getElementById("refresh").onclick = () => refresh().catch(e => show(e.message));
document.getElementById("createLicense").onclick = () => createLicense().catch(e => show(e.message));
document.getElementById("downloadCsv").onclick = () => {
  const csv = document.getElementById("ledgerCsv").value;
  if (!csv) return;
  const blob = new Blob(["\\ufeff" + csv], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "lulu-license-ledger-" + new Date().toISOString().slice(0, 10) + ".csv";
  a.click();
  URL.revokeObjectURL(a.href);
};
document.getElementById("copyCsv").onclick = () => navigator.clipboard.writeText(document.getElementById("ledgerCsv").value).then(() => show("已复制本次 CSV 台账。")).catch(() => show("复制失败,请手动选中复制。"));
document.getElementById("clearCsv").onclick = clearLedgerCsv;
rows.onclick = (event) => {
  const target = event.target;
  if (!(target instanceof HTMLElement)) return;
  const id = target.dataset.id;
  if (!id) return;
  if (target.dataset.action === "save") saveLicense(id).catch(e => show(e.message));
  if (target.dataset.action === "reset") resetDevices(id).catch(e => show(e.message));
};
</script>
</body>
</html>`);
}

async function adminCreateLicense(request, env) {
  const gate = await requireAdminMutation(request, env);
  if (!gate.ok) return gate.response;
  const body = await request.json().catch(() => ({}));
  const licenseCode = String(body.license_code || "").trim();
  if (!licenseCode) return adminJson(400, { status: "invalid", message: "请输入授权码。" });
  if (licenseCode.length < MIN_LICENSE_CODE_LENGTH) return adminJson(400, { status: "invalid", message: "授权码太短,请检查后重试。" });
  const id = String(body.license_id || crypto.randomUUID());
  const hash = await codeHash(licenseCode, env);
  const duplicate = await env.DB.prepare("SELECT id FROM licenses WHERE code_hash = ?").bind(hash).first();
  if (duplicate) return adminJson(409, { status: "duplicate", message: "该授权码已存在,请勿重复创建。" });
  const requestedMaxDevices = Object.prototype.hasOwnProperty.call(body, "max_devices")
    ? body.max_devices
    : DEFAULT_MAX_DEVICES;
  const maxDevices = parsePositiveInteger(requestedMaxDevices);
  if (maxDevices === null) {
    return adminJson(400, { status: "invalid", message: "设备数必须是正整数。" });
  }
  const expiresAt = normalizeNullableIso(body.expires_at);
  if (expiresAt === undefined) return adminJson(400, { status: "invalid", message: "过期时间格式不正确。" });
  const recipientName = normalizeLedgerField(body.recipient_name, 80);
  const contact = normalizeLedgerField(body.contact, 100);
  const batch = normalizeLedgerField(body.batch, 80);
  const remark = normalizeLedgerField(body.remark ?? body.note, 160);
  if (!recipientName) return adminJson(400, { status: "invalid", message: "请填写给谁。" });
  if (!contact) return adminJson(400, { status: "invalid", message: "请填写联系方式/微信名。" });
  if (!batch) return adminJson(400, { status: "invalid", message: "请填写批次。" });
  if (!remark) return adminJson(400, { status: "invalid", message: "请填写备注。" });
  const ledgerNote = buildLedgerNote({ recipient_name: recipientName, contact, batch, remark });
  const createdAt = nowIso();
  await env.DB.prepare(
    "INSERT INTO licenses (id, code_hash, status, max_devices, expires_at, owner_identity_id, entitlements_json, created_at, note) VALUES (?, ?, 'active', ?, ?, NULL, ?, ?, ?)"
  ).bind(
    id,
    hash,
    maxDevices,
    expiresAt,
    JSON.stringify(Array.isArray(body.entitlements) ? body.entitlements : ENTITLEMENTS),
    createdAt,
    ledgerNote
  ).run();
  await audit(env, "admin_create_license", { licenseId: id, reason: "admin" });
  return adminJson(201, { status: "ok", license_id: id, created_at: createdAt, message: "license created; plaintext code was not stored" });
}

async function adminListLicenses(request, env) {
  if (!(await requireAdmin(request, env))) return adminJson(403, { status: "forbidden", message: "forbidden" });
  const url = new URL(request.url);
  const limit = Math.min(Number(url.searchParams.get("limit") || LIST_PAGE_SIZE), LIST_PAGE_SIZE);
  const offset = Math.max(Number(url.searchParams.get("offset") || 0), 0);
  const rows = await env.DB.prepare(
    "SELECT l.id, l.status, l.max_devices, l.expires_at, l.owner_identity_id, l.created_at, l.note, " +
      "COALESCE(SUM(CASE WHEN d.status = 'active' THEN 1 ELSE 0 END), 0) AS active_device_count " +
      "FROM licenses l LEFT JOIN devices d ON d.license_id = l.id " +
      "GROUP BY l.id, l.status, l.max_devices, l.expires_at, l.owner_identity_id, l.created_at, l.note " +
      "ORDER BY l.created_at DESC LIMIT ? OFFSET ?"
  ).bind(limit, offset).all();
  const items = (rows.results || []).map((row) => {
    const ledger = parseLedgerNote(row.note || "");
    return {
      license_id: row.id,
      status: row.status,
      max_devices: row.max_devices,
      active_device_count: Number(row.active_device_count || 0),
      expires_at: row.expires_at,
      note: row.note || "",
      recipient_name: ledger.recipient_name,
      contact: ledger.contact,
      batch: ledger.batch,
      remark: ledger.remark,
      created_at: row.created_at,
    };
  });
  return adminJson(200, { status: "ok", items, limit, offset });
}

function purgeIdSuffix(licenseId) {
  return String(licenseId || "").replace(/-/g, "").slice(-8).toUpperCase();
}

function purgeConfirmationPhrase(licenseId) {
  return `永久删除 ${purgeIdSuffix(licenseId)}`;
}

async function readPurgeState(env, licenseId) {
  const row = await env.DB.prepare(
    "SELECT l.id, l.status, l.note, " +
      "(SELECT COUNT(*) FROM devices d WHERE d.license_id = l.id) AS device_count, " +
      "(SELECT COUNT(*) FROM lease_tokens t WHERE t.license_id = l.id) AS lease_count, " +
      "(SELECT COUNT(*) FROM lease_tokens t WHERE t.license_id = l.id AND t.status = 'active') AS active_lease_count " +
      "FROM licenses l WHERE l.id = ?"
  ).bind(licenseId).first();
  if (!row) return null;
  let hasDeactivationOperationsTable = true;
  let deactivationOperationCount = 0;
  let pendingDeactivationCount = 0;
  try {
    const operationRow = await env.DB.prepare(
      "SELECT COUNT(*) AS operation_count, " +
        "COALESCE(SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END), 0) AS pending_count " +
        "FROM device_deactivation_operations WHERE license_id = ?"
    ).bind(licenseId).first();
    deactivationOperationCount = Number(operationRow?.operation_count || 0);
    pendingDeactivationCount = Number(operationRow?.pending_count || 0);
  } catch (error) {
    const message = String(error?.message || error || "").toLowerCase();
    if (!message.includes("no such table") || !message.includes("device_deactivation_operations")) {
      throw error;
    }
    hasDeactivationOperationsTable = false;
  }
  return {
    licenseId: row.id,
    status: row.status,
    note: row.note || "",
    counts: {
      devices: Number(row.device_count || 0),
      leases: Number(row.lease_count || 0),
      deactivation_operations: deactivationOperationCount,
    },
    activeLeaseCount: Number(row.active_lease_count || 0),
    pendingDeactivationCount,
    hasDeactivationOperationsTable,
  };
}

function samePurgeState(state, payload) {
  return Boolean(
    state &&
    state.licenseId === payload.license_id &&
    state.status === payload.status &&
    state.counts.devices === payload.counts.devices &&
    state.counts.leases === payload.counts.leases &&
    state.counts.deactivation_operations === payload.counts.deactivation_operations &&
    state.hasDeactivationOperationsTable === payload.deactivation_operations_table &&
    state.activeLeaseCount === 0 &&
    state.pendingDeactivationCount === 0
  );
}

async function adminPurgePreview(request, env, licenseId) {
  const gate = await requireAdminMutation(request, env);
  if (!gate.ok) return gate.response;
  if (!UUID_PATTERN.test(licenseId)) {
    return adminJson(400, { status: "invalid", message: "授权 ID 格式不正确。" });
  }
  const state = await readPurgeState(env, licenseId);
  if (!state) return adminNotFound();
  if (state.status === "active") {
    return adminJson(409, {
      status: "purge_blocked",
      message: "正在使用的授权不能永久删除。请先停用，再核对影响。",
    });
  }
  if (state.activeLeaseCount > 0 || state.pendingDeactivationCount > 0) {
    return adminJson(409, {
      status: "purge_blocked",
      message: "该授权仍有未结束的凭证或设备操作，暂时不能删除。请稍后重试。",
    });
  }
  const issuedAt = Math.floor(Date.now() / 1000);
  const payload = {
    purpose: PURGE_PREVIEW_PURPOSE,
    license_id: state.licenseId,
    status: state.status,
    counts: state.counts,
    deactivation_operations_table: state.hasDeactivationOperationsTable,
    issued_at: issuedAt,
    expires_at: issuedAt + PURGE_PREVIEW_TTL_SECONDS,
    nonce: crypto.randomUUID(),
  };
  const ledger = parseLedgerNote(state.note);
  return adminJson(200, {
    status: "ok",
    license_id: state.licenseId,
    license_status: state.status,
    recipient_name: ledger.recipient_name,
    id_suffix: purgeIdSuffix(state.licenseId),
    confirmation_phrase: purgeConfirmationPhrase(state.licenseId),
    counts: state.counts,
    preview_token: await signPurgePreview(payload, env),
    expires_at: new Date(payload.expires_at * 1000).toISOString(),
    audit_retained: true,
    local_csv_deleted: false,
  });
}

async function adminPurgeLicense(request, env, licenseId) {
  const gate = await requireAdminMutation(request, env);
  if (!gate.ok) return gate.response;
  if (!UUID_PATTERN.test(licenseId)) {
    return adminJson(400, { status: "invalid", message: "授权 ID 格式不正确。" });
  }
  const state = await readPurgeState(env, licenseId);
  if (!state) {
    const prior = await env.DB.prepare(
      "SELECT id FROM audit_events WHERE license_id = ? AND event_type = 'admin_purge_license' ORDER BY created_at DESC LIMIT 1"
    ).bind(licenseId).first();
    if (prior) {
      return adminJson(200, {
        status: "ok",
        license_id: licenseId,
        purged: true,
        replayed: true,
        audit_retained: true,
        local_csv_deleted: false,
      });
    }
    return adminNotFound();
  }
  const body = await request.json().catch(() => null);
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    return adminJson(400, { status: "invalid", message: "请求内容格式不正确。" });
  }
  if (String(body.confirmation_phrase || "") !== purgeConfirmationPhrase(licenseId)) {
    return adminJson(400, { status: "invalid_confirmation", message: "确认短语不一致，永久删除未执行。" });
  }
  const preview = await verifyPurgePreview(body.preview_token, env);
  if (!preview || preview.license_id !== licenseId || !samePurgeState(state, preview)) {
    return adminJson(409, {
      status: "purge_stale",
      message: "删除确认已过期或关联数据发生变化，请重新预检。",
    });
  }
  const counts = preview.counts;
  const guardFailure = "json_extract('invalid', '$')";
  const operationCountGuard = state.hasDeactivationOperationsTable
    ? "AND (SELECT COUNT(*) FROM device_deactivation_operations WHERE license_id = ?) = ? "
    : "";
  const pendingOperationGuard = state.hasDeactivationOperationsTable
    ? "AND (SELECT COUNT(*) FROM device_deactivation_operations WHERE license_id = ? AND status = 'pending') = 0 "
    : "";
  const guardValues = [
    licenseId,
    preview.status,
    licenseId,
    counts.devices,
    licenseId,
    counts.leases,
  ];
  if (state.hasDeactivationOperationsTable) {
    guardValues.push(licenseId, counts.deactivation_operations);
  }
  guardValues.push(licenseId);
  if (state.hasDeactivationOperationsTable) guardValues.push(licenseId);

  const statements = [
    env.DB.prepare(
      "SELECT CASE WHEN " +
        "EXISTS (SELECT 1 FROM licenses WHERE id = ? AND status = ? AND status <> 'active') " +
        "AND (SELECT COUNT(*) FROM devices WHERE license_id = ?) = ? " +
        "AND (SELECT COUNT(*) FROM lease_tokens WHERE license_id = ?) = ? " +
        operationCountGuard +
        "AND (SELECT COUNT(*) FROM lease_tokens WHERE license_id = ? AND status = 'active') = 0 " +
        pendingOperationGuard +
        `THEN 1 ELSE ${guardFailure} END AS purge_guard`
    ).bind(...guardValues),
  ];
  if (state.hasDeactivationOperationsTable) {
    statements.push(
      env.DB.prepare("DELETE FROM device_deactivation_operations WHERE license_id = ?").bind(licenseId),
      env.DB.prepare(`SELECT CASE WHEN changes() = ? THEN 1 ELSE ${guardFailure} END AS purge_operations_guard`)
        .bind(counts.deactivation_operations)
    );
  }
  statements.push(
    env.DB.prepare("DELETE FROM lease_tokens WHERE license_id = ?").bind(licenseId),
    env.DB.prepare(`SELECT CASE WHEN changes() = ? THEN 1 ELSE ${guardFailure} END AS purge_leases_guard`)
      .bind(counts.leases),
    env.DB.prepare("DELETE FROM devices WHERE license_id = ?").bind(licenseId),
    env.DB.prepare(`SELECT CASE WHEN changes() = ? THEN 1 ELSE ${guardFailure} END AS purge_devices_guard`)
      .bind(counts.devices),
    env.DB.prepare("DELETE FROM licenses WHERE id = ? AND status = ? AND status <> 'active'")
      .bind(licenseId, preview.status),
    env.DB.prepare(`SELECT CASE WHEN changes() = 1 THEN 1 ELSE ${guardFailure} END AS purge_license_guard`),
    auditStatement(env, "admin_purge_license", {
      licenseId,
      reason: `permanent_delete;devices=${counts.devices};leases=${counts.leases};operations=${counts.deactivation_operations}`,
    })
  );
  try {
    await env.DB.batch(statements);
  } catch {
    return adminJson(409, {
      status: "purge_stale",
      message: "永久删除未完成，数据已保持原状。请重新预检后再试。",
    });
  }
  return adminJson(200, {
    status: "ok",
    license_id: licenseId,
    purged: true,
    replayed: false,
    counts,
    audit_retained: true,
    local_csv_deleted: false,
  });
}

async function adminPatchLicense(request, env, licenseId) {
  const gate = await requireAdminMutation(request, env);
  if (!gate.ok) return gate.response;
  const existingLicense = await env.DB.prepare("SELECT id, note FROM licenses WHERE id = ?").bind(licenseId).first();
  if (!existingLicense) return adminNotFound();
  const body = await request.json().catch(() => null);
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    return adminJson(400, { status: "invalid", message: "请求内容格式不正确。" });
  }
  const mutableFields = ["status", "expires_at", "max_devices", "recipient_name", "contact", "batch", "remark"];
  if (!mutableFields.some((field) => Object.prototype.hasOwnProperty.call(body, field))) {
    return adminJson(400, { status: "invalid", message: "没有可保存的修改。" });
  }
  const hasStatus = Object.prototype.hasOwnProperty.call(body, "status");
  const status = hasStatus ? sanitizeText(body.status, 20).trim() : null;
  if (hasStatus && !ALLOWED_LICENSE_STATUS.has(status)) return adminJson(400, { status: "invalid", message: "状态不正确。" });
  const expiresAt = Object.prototype.hasOwnProperty.call(body, "expires_at") ? normalizeNullableIso(body.expires_at) : undefined;
  if (expiresAt === undefined && Object.prototype.hasOwnProperty.call(body, "expires_at")) return adminJson(400, { status: "invalid", message: "过期时间格式不正确。" });
  const hasMaxDevices = Object.prototype.hasOwnProperty.call(body, "max_devices");
  const maxDevices = hasMaxDevices ? parsePositiveInteger(body.max_devices) : undefined;
  if (hasMaxDevices && maxDevices === null) {
    return adminJson(400, { status: "invalid", message: "设备数必须是正整数。" });
  }
  const hasLedgerPatch = ["recipient_name", "contact", "batch", "remark"].some((field) => Object.prototype.hasOwnProperty.call(body, field));
  let note;
  if (hasLedgerPatch) {
    const current = parseLedgerNote(existingLicense.note || "");
    const recipientName = normalizeLedgerField(Object.prototype.hasOwnProperty.call(body, "recipient_name") ? body.recipient_name : current.recipient_name, 80);
    const contact = normalizeLedgerField(Object.prototype.hasOwnProperty.call(body, "contact") ? body.contact : current.contact, 100);
    const batch = normalizeLedgerField(Object.prototype.hasOwnProperty.call(body, "batch") ? body.batch : current.batch, 80);
    const remark = normalizeLedgerField(Object.prototype.hasOwnProperty.call(body, "remark") ? body.remark : current.remark, 160);
    if (!recipientName) return adminJson(400, { status: "invalid", message: "请填写给谁。" });
    if (!contact) return adminJson(400, { status: "invalid", message: "请填写联系方式/微信名。" });
    if (!batch) return adminJson(400, { status: "invalid", message: "请填写批次。" });
    if (!remark) return adminJson(400, { status: "invalid", message: "请填写备注。" });
    note = buildLedgerNote({ recipient_name: recipientName, contact, batch, remark });
  }
  const statements = [];
  if (status) statements.push(env.DB.prepare("UPDATE licenses SET status = ? WHERE id = ?").bind(status, licenseId));
  if (expiresAt !== undefined) statements.push(env.DB.prepare("UPDATE licenses SET expires_at = ? WHERE id = ?").bind(expiresAt, licenseId));
  if (maxDevices !== undefined) statements.push(env.DB.prepare("UPDATE licenses SET max_devices = ? WHERE id = ?").bind(maxDevices, licenseId));
  if (note !== undefined) statements.push(env.DB.prepare("UPDATE licenses SET note = ? WHERE id = ?").bind(note, licenseId));
  if (status && status !== "active") {
    statements.push(env.DB.prepare("UPDATE lease_tokens SET status = 'revoked', revoked_at = ?, revoke_reason = ? WHERE license_id = ? AND status = 'active'")
      .bind(nowIso(), `admin_${status}`, licenseId));
  }
  statements.push(auditStatement(env, "admin_patch_license", { licenseId, reason: status || "update" }));
  await env.DB.batch(statements);
  return adminJson(200, { status: "ok", license_id: licenseId });
}

async function adminResetDevice(request, env, licenseId) {
  const gate = await requireAdminMutation(request, env);
  if (!gate.ok) return gate.response;
  const existingLicense = await env.DB.prepare("SELECT id FROM licenses WHERE id = ?").bind(licenseId).first();
  if (!existingLicense) return adminNotFound();
  const body = await request.json().catch(() => null);
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    return adminJson(400, { status: "invalid", message: "请求内容格式不正确。" });
  }
  const deviceId = String(body.device_id || "").trim();
  if (deviceId) {
    const existingDevice = await env.DB.prepare("SELECT id FROM devices WHERE license_id = ? AND id = ?").bind(licenseId, deviceId).first();
    if (!existingDevice) return adminNotFound();
  }
  const statements = [];
  if (deviceId) {
    statements.push(env.DB.prepare(
      "UPDATE lease_tokens SET status = 'revoked', revoked_at = ?, revoke_reason = ? WHERE license_id = ? AND device_id = ? AND status = 'active'"
    ).bind(nowIso(), "admin_reset_device", licenseId, deviceId));
    statements.push(env.DB.prepare("UPDATE devices SET status = 'revoked', last_seen_at = ? WHERE license_id = ? AND id = ?")
      .bind(nowIso(), licenseId, deviceId));
  } else {
    statements.push(env.DB.prepare("UPDATE lease_tokens SET status = 'revoked', revoked_at = ?, revoke_reason = ? WHERE license_id = ? AND status = 'active'")
      .bind(nowIso(), "admin_reset_all_devices", licenseId));
    statements.push(env.DB.prepare("UPDATE devices SET status = 'revoked', last_seen_at = ? WHERE license_id = ?")
      .bind(nowIso(), licenseId));
  }
  statements.push(auditStatement(env, "admin_reset_device", {
    licenseId,
    deviceId: deviceId || null,
    reason: deviceId ? "single" : "all",
  }));
  await env.DB.batch(statements);
  return adminJson(200, { status: "ok", license_id: licenseId, device_id: deviceId || null });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    try {
      if (request.method === "POST" && url.pathname === "/v1/activate") return await activate(request, env);
      if (request.method === "POST" && url.pathname === "/v1/verify") return await verify(request, env);
      if (request.method === "POST" && url.pathname === "/v1/deactivate") return await deactivate(request, env);
      if (request.method === "GET" && url.pathname === "/admin") {
        if (!(await requireAdmin(request, env, { allowToken: false }))) return adminHtml(403, "<!doctype html><meta charset=\"utf-8\"><title>forbidden</title><h1>forbidden</h1>");
        return adminPage();
      }
      if (request.method === "POST" && url.pathname === "/admin/licenses") return await adminCreateLicense(request, env);
      if (request.method === "GET" && url.pathname === "/admin/licenses") return await adminListLicenses(request, env);
      const purgePreviewMatch = url.pathname.match(/^\/admin\/licenses\/([^/]+)\/purge-preview$/);
      if (request.method === "POST" && purgePreviewMatch) return await adminPurgePreview(request, env, purgePreviewMatch[1]);
      const patchMatch = url.pathname.match(/^\/admin\/licenses\/([^/]+)$/);
      if (request.method === "PATCH" && patchMatch) return await adminPatchLicense(request, env, patchMatch[1]);
      if (request.method === "DELETE" && patchMatch) return await adminPurgeLicense(request, env, patchMatch[1]);
      const resetMatch = url.pathname.match(/^\/admin\/licenses\/([^/]+)\/devices\/reset$/);
      if (request.method === "POST" && resetMatch) return await adminResetDevice(request, env, resetMatch[1]);
      return url.pathname.startsWith("/admin") ? adminNotFound() : json(404, { status: "not_found", message: "not found" });
    } catch {
      if (url.pathname.startsWith("/admin")) {
        return adminJson(500, { status: "server_error", message: "授权服务暂时不可用,请稍后重试。" });
      }
      return json(500, { status: "server_error", message: "授权服务暂时不可用,请稍后重试。" });
    }
  },
};
