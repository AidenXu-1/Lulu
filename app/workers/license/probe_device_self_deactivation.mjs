import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { DatabaseSync } from "node:sqlite";

import worker from "./worker.js";

const CODE_HASH_PEPPER = "probe-code-pepper";
const LEASE_TOKEN_PEPPER = "probe-lease-pepper";
const RATE_LIMIT_PEPPER = "probe-rate-pepper";
const ADMIN_TOKEN = "probe-admin-token";
const BASE_URL = "https://license-probe.invalid";

class FakeD1Statement {
  constructor(owner, sql, values = []) {
    this.owner = owner;
    this.sql = sql;
    this.values = values;
  }

  bind(...values) {
    return new FakeD1Statement(this.owner, this.sql, values);
  }

  async first() {
    await Promise.resolve();
    return this.owner.database.prepare(this.sql).get(...this.values) ?? null;
  }

  async run() {
    await Promise.resolve();
    return this.owner.database.prepare(this.sql).run(...this.values);
  }

  async all() {
    await Promise.resolve();
    return { results: this.owner.database.prepare(this.sql).all(...this.values) };
  }
}

class FakeD1 {
  constructor(database) {
    this.database = database;
    this.failBatchAt = null;
    this.throwAfterCommit = false;
    this.lastBatchLength = 0;
    this.batchQueue = Promise.resolve();
  }

  prepare(sql) {
    return new FakeD1Statement(this, sql);
  }

  async batch(statements) {
    const execute = async () => {
      this.lastBatchLength = statements.length;
      this.database.exec("BEGIN IMMEDIATE");
      try {
        for (let index = 0; index < statements.length; index += 1) {
          if (this.failBatchAt === index + 1) {
            throw new Error(`probe batch fault at step ${index + 1}`);
          }
          const statement = statements[index];
          const prepared = this.database.prepare(statement.sql);
          if (/^\s*(?:SELECT|WITH)\b/i.test(statement.sql)) prepared.get(...statement.values);
          else prepared.run(...statement.values);
        }
        this.database.exec("COMMIT");
      } catch (error) {
        if (this.database.isTransaction) this.database.exec("ROLLBACK");
        throw error;
      }
      if (this.throwAfterCommit) {
        this.throwAfterCommit = false;
        throw new Error("probe lost result after commit");
      }
      return statements.map(() => ({ success: true }));
    };
    const result = this.batchQueue.then(execute, execute);
    this.batchQueue = result.catch(() => {});
    return result;
  }
}

async function loadSql(name) {
  return readFile(new URL(name, import.meta.url), "utf8");
}

async function createFixture({ beforeMigration, includeSelfDeactivationMigration = true } = {}) {
  const database = new DatabaseSync(":memory:");
  database.exec("PRAGMA foreign_keys = ON");
  database.exec(await loadSql("./migrations/0001_initial.sql"));
  if (beforeMigration) await beforeMigration(database);
  if (includeSelfDeactivationMigration) {
    database.exec(await loadSql("./migrations/0002_device_self_deactivation.sql"));
  }
  const DB = new FakeD1(database);
  return {
    database,
    DB,
    env: {
      DB,
      CODE_HASH_PEPPER,
      LEASE_TOKEN_PEPPER,
      RATE_LIMIT_PEPPER,
      ADMIN_TOKEN,
    },
  };
}

async function sha256Hex(input, pepper) {
  const bytes = new TextEncoder().encode(`${pepper}:${input}`);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)]
    .map((value) => value.toString(16).padStart(2, "0"))
    .join("");
}

async function seedLicense(database, {
  id,
  code,
  maxDevices,
  status = "active",
  expiresAt = null,
}) {
  const hash = await sha256Hex(code, CODE_HASH_PEPPER);
  database.prepare(
    "INSERT INTO licenses (id, code_hash, status, max_devices, expires_at, owner_identity_id, entitlements_json, created_at, note) " +
      "VALUES (?, ?, ?, ?, ?, NULL, ?, ?, NULL)"
  ).run(
    id,
    hash,
    status,
    maxDevices,
    expiresAt,
    JSON.stringify(["lulu.full_access"]),
    new Date().toISOString()
  );
}

function request(path, body, {
  ip = "203.0.113.10",
  admin = false,
  method = "POST",
} = {}) {
  const headers = {
    "content-type": "application/json; charset=utf-8",
    "cf-connecting-ip": ip,
  };
  if (admin) {
    headers.origin = BASE_URL;
    headers["x-admin-token"] = ADMIN_TOKEN;
    headers["x-lulu-admin-csrf"] = "same-origin";
  }
  return new Request(`${BASE_URL}${path}`, {
    method,
    headers,
    body: method === "GET" || method === "HEAD" ? undefined : JSON.stringify(body),
  });
}

async function call(env, path, body, options) {
  const response = await worker.fetch(request(path, body, options), env);
  const payload = await response.json();
  assert.match(response.headers.get("cache-control") || "", /no-store/);
  return { response, payload };
}

async function activate(env, code, deviceHash, ip = "203.0.113.10") {
  return call(env, "/v1/activate", {
    license_code: code,
    device_hash: deviceHash,
    platform: "probe",
    app_version: "probe",
  }, { ip });
}

async function verify(env, activation, deviceHash, ip = "203.0.113.10") {
  return call(env, "/v1/verify", {
    license_id: activation.license_id,
    device_id: activation.device_id,
    device_hash: deviceHash,
    lease_token: activation.lease_token,
    app_version: "probe",
  }, { ip });
}

async function deactivate(env, activation, deviceHash, requestId, options = {}) {
  return call(env, "/v1/deactivate", {
    license_id: activation.license_id,
    device_id: activation.device_id,
    device_hash: deviceHash,
    lease_token: activation.lease_token,
    request_id: requestId,
    client_time: "1999-01-01T00:00:00.000Z",
  }, options);
}

function scalar(database, sql, ...values) {
  const row = database.prepare(sql).get(...values);
  return row ? Object.values(row)[0] : null;
}

async function testDynamicLimitsAndVerify() {
  for (const maxDevices of [1, 2, 5, 17]) {
    const fixture = await createFixture();
    const code = `DYNAMIC-${maxDevices}-CODE`;
    const id = `license-dynamic-${maxDevices}`;
    await seedLicense(fixture.database, { id, code, maxDevices });
    for (let index = 0; index < maxDevices; index += 1) {
      const result = await activate(
        fixture.env,
        code,
        `device-dynamic-${maxDevices}-${index}`,
        `203.0.113.${20 + index}`
      );
      assert.equal(result.response.status, 200);
      assert.equal(result.payload.active_device_count, index + 1);
      assert.equal(result.payload.max_devices, maxDevices);
    }
    const idempotent = await activate(
      fixture.env,
      code,
      `device-dynamic-${maxDevices}-0`,
      "203.0.113.239"
    );
    assert.equal(idempotent.response.status, 200);
    assert.equal(idempotent.payload.active_device_count, maxDevices);
    assert.equal(idempotent.payload.max_devices, maxDevices);
    const denied = await activate(
      fixture.env,
      code,
      `device-dynamic-${maxDevices}-overflow`,
      "203.0.113.240"
    );
    assert.equal(denied.response.status, 403);
    assert.equal(denied.payload.status, "device_limit");
    assert.equal(denied.payload.active_device_count, maxDevices);
    assert.equal(denied.payload.max_devices, maxDevices);
    assert.match(denied.payload.message, new RegExp(`${maxDevices}/${maxDevices}`));

    const firstDevice = fixture.database.prepare(
      "SELECT id, device_hash FROM devices WHERE license_id = ? ORDER BY first_seen_at LIMIT 1"
    ).get(id);
    const firstLease = fixture.database.prepare(
      "SELECT token_hash FROM lease_tokens WHERE license_id = ? AND device_id = ? AND status = 'active' ORDER BY issued_at LIMIT 1"
    ).get(id, firstDevice.id);
    assert.ok(firstLease);
  }

  const fixture = await createFixture();
  await seedLicense(fixture.database, {
    id: "license-verify-dynamic",
    code: "VERIFY-DYNAMIC-CODE",
    maxDevices: 5,
  });
  const activated = await activate(fixture.env, "VERIFY-DYNAMIC-CODE", "verify-device");
  const verified = await verify(fixture.env, activated.payload, "verify-device");
  assert.equal(verified.response.status, 200);
  assert.equal(verified.payload.active_device_count, 1);
  assert.equal(verified.payload.max_devices, 5);
}

async function testConcurrentActivationAndOverLimitHistory() {
  const fixture = await createFixture();
  await seedLicense(fixture.database, {
    id: "license-concurrent",
    code: "CONCURRENT-CODE",
    maxDevices: 1,
  });
  const results = await Promise.all([
    activate(fixture.env, "CONCURRENT-CODE", "concurrent-a", "203.0.113.41"),
    activate(fixture.env, "CONCURRENT-CODE", "concurrent-b", "203.0.113.42"),
  ]);
  assert.deepEqual(
    results.map((result) => result.payload.status).sort(),
    ["device_limit", "ok"]
  );
  assert.equal(
    scalar(
      fixture.database,
      "SELECT COUNT(*) FROM devices WHERE license_id = ? AND status = 'active'",
      "license-concurrent"
    ),
    1
  );

  await seedLicense(fixture.database, {
    id: "license-over-limit",
    code: "OVER-LIMIT-CODE",
    maxDevices: 3,
  });
  for (let index = 0; index < 3; index += 1) {
    const result = await activate(
      fixture.env,
      "OVER-LIMIT-CODE",
      `over-limit-${index}`,
      `203.0.113.${50 + index}`
    );
    assert.equal(result.payload.status, "ok");
  }
  fixture.database.prepare("UPDATE licenses SET max_devices = 2 WHERE id = ?")
    .run("license-over-limit");
  const denied = await activate(
    fixture.env,
    "OVER-LIMIT-CODE",
    "over-limit-new",
    "203.0.113.60"
  );
  assert.equal(denied.payload.status, "device_limit");
  assert.equal(denied.payload.active_device_count, 3);
  assert.equal(denied.payload.max_devices, 2);
  assert.equal(
    scalar(
      fixture.database,
      "SELECT COUNT(*) FROM devices WHERE license_id = ? AND status = 'active'",
      "license-over-limit"
    ),
    3
  );
}

async function testAdminMaxValidationAndLegacyInvalidMax() {
  const fixture = await createFixture();
  for (const maxDevices of [0, -1, 1.5, true]) {
    const result = await call(fixture.env, "/admin/licenses", {
      license_code: `ADMIN-INVALID-${maxDevices}-CODE`,
      max_devices: maxDevices,
      recipient_name: "probe",
      contact: "probe",
      batch: "probe",
      remark: "probe",
      expires_at: null,
    }, { admin: true, ip: "203.0.113.70" });
    assert.equal(result.response.status, 400);
  }
  const higher = await call(fixture.env, "/admin/licenses", {
    license_code: "ADMIN-HIGHER-CODE",
    max_devices: 250,
    recipient_name: "probe",
    contact: "probe",
    batch: "probe",
    remark: "probe",
    expires_at: null,
  }, { admin: true, ip: "203.0.113.71" });
  assert.equal(higher.response.status, 201, JSON.stringify(higher.payload));
  assert.equal(
    scalar(fixture.database, "SELECT max_devices FROM licenses WHERE id = ?", higher.payload.license_id),
    250
  );
  for (const maxDevices of [0, -1, 1.5, true]) {
    const patched = await call(
      fixture.env,
      `/admin/licenses/${encodeURIComponent(higher.payload.license_id)}`,
      { max_devices: maxDevices },
      { admin: true, method: "PATCH", ip: "203.0.113.73" }
    );
    assert.equal(patched.response.status, 400);
  }
  const validPatch = await call(
    fixture.env,
    `/admin/licenses/${encodeURIComponent(higher.payload.license_id)}`,
    { max_devices: 5 },
    { admin: true, method: "PATCH", ip: "203.0.113.73" }
  );
  assert.equal(validPatch.response.status, 200);
  const activated = await activate(
    fixture.env,
    "ADMIN-HIGHER-CODE",
    "admin-regression-device",
    "203.0.113.75"
  );
  assert.equal(activated.payload.status, "ok");
  assert.equal(activated.payload.max_devices, 5);
  const listed = await call(
    fixture.env,
    "/admin/licenses?limit=50&offset=0",
    null,
    { admin: true, method: "GET", ip: "203.0.113.73" }
  );
  const listedHigher = listed.payload.items.find(
    (item) => item.license_id === higher.payload.license_id
  );
  assert.equal(listed.response.status, 200);
  assert.equal(listedHigher.max_devices, 5);
  assert.equal(listedHigher.active_device_count, 1);
  assert.equal(JSON.stringify(listed.payload).includes("ADMIN-HIGHER-CODE"), false);
  const reset = await call(
    fixture.env,
    `/admin/licenses/${encodeURIComponent(higher.payload.license_id)}/devices/reset`,
    {},
    { admin: true, ip: "203.0.113.73" }
  );
  assert.equal(reset.response.status, 200);
  assert.equal(
    scalar(fixture.database, "SELECT status FROM devices WHERE id = ?", activated.payload.device_id),
    "revoked"
  );
  const defaulted = await call(fixture.env, "/admin/licenses", {
    license_code: "ADMIN-DEFAULT-CODE",
    recipient_name: "probe",
    contact: "probe",
    batch: "probe",
    remark: "probe",
    expires_at: null,
  }, { admin: true, ip: "203.0.113.74" });
  assert.equal(defaulted.response.status, 201);
  assert.equal(
    scalar(fixture.database, "SELECT max_devices FROM licenses WHERE id = ?", defaulted.payload.license_id),
    2
  );

  const invalid = await createFixture({
    beforeMigration: async (database) => {
      await seedLicense(database, {
        id: "license-invalid-max",
        code: "INVALID-MAX-CODE",
        maxDevices: 0,
      });
    },
  });
  const response = await activate(
    invalid.env,
    "INVALID-MAX-CODE",
    "invalid-max-device",
    "203.0.113.72"
  );
  assert.equal(response.response.status, 500);
  assert.equal(response.payload.status, "server_error");
  assert.equal(
    scalar(
      invalid.database,
      "SELECT COUNT(*) FROM audit_events WHERE license_id = ? AND event_type = 'license_config_error'",
      "license-invalid-max"
    ),
    1
  );
}

async function deactivationFixture() {
  const fixture = await createFixture();
  await seedLicense(fixture.database, {
    id: "license-deactivate",
    code: "DEACTIVATE-CODE",
    maxDevices: 5,
  });
  const first = await activate(
    fixture.env,
    "DEACTIVATE-CODE",
    "device-current-hash",
    "203.0.113.80"
  );
  assert.equal(first.payload.status, "ok");
  const secondLease = await verify(
    fixture.env,
    first.payload,
    "device-current-hash",
    "203.0.113.80"
  );
  assert.equal(secondLease.payload.status, "ok");
  const other = await activate(
    fixture.env,
    "DEACTIVATE-CODE",
    "device-other-hash",
    "203.0.113.81"
  );
  assert.equal(other.payload.status, "ok");
  return { ...fixture, first: first.payload, secondLease: secondLease.payload, other: other.payload };
}

async function testDeactivationCommitReplayAndIsolation() {
  const fixture = await deactivationFixture();
  const requestId = "11111111-1111-4111-8111-111111111111";
  const before = Date.now();
  const stopped = await deactivate(
    fixture.env,
    fixture.secondLease,
    "device-current-hash",
    requestId,
    { ip: "203.0.113.80" }
  );
  const after = Date.now();
  assert.equal(stopped.response.status, 200);
  assert.equal(stopped.payload.status, "ok");
  assert.equal(stopped.payload.deactivation_status, "stopped");
  assert.equal(stopped.payload.replayed, false);
  assert.equal(stopped.payload.active_device_count, 1);
  assert.equal(stopped.payload.max_devices, 5);
  assert.equal(fixture.DB.lastBatchLength, 5);
  assert.equal(
    scalar(fixture.database, "SELECT status FROM devices WHERE id = ?", fixture.first.device_id),
    "revoked"
  );
  assert.equal(
    scalar(
      fixture.database,
      "SELECT COUNT(*) FROM lease_tokens WHERE device_id = ? AND status = 'active'",
      fixture.first.device_id
    ),
    0
  );
  assert.equal(
    scalar(fixture.database, "SELECT status FROM devices WHERE id = ?", fixture.other.device_id),
    "active"
  );
  assert.equal(
    scalar(
      fixture.database,
      "SELECT COUNT(*) FROM audit_events WHERE device_id = ? AND event_type = 'self_deactivate_committed'",
      fixture.first.device_id
    ),
    1
  );
  const operation = fixture.database.prepare(
    "SELECT * FROM device_deactivation_operations WHERE license_id = ? AND device_id = ?"
  ).get(fixture.first.license_id, fixture.first.device_id);
  assert.equal(operation.status, "committed");
  assert.ok(Date.parse(operation.created_at) >= before);
  assert.ok(Date.parse(operation.created_at) <= after);
  const persisted = JSON.stringify({
    operation: [...fixture.database.prepare("SELECT * FROM device_deactivation_operations").all()],
    audits: [...fixture.database.prepare("SELECT * FROM audit_events").all()],
  });
  assert.equal(persisted.includes(requestId), false);
  assert.equal(persisted.includes("DEACTIVATE-CODE"), false);
  assert.equal(persisted.includes(fixture.secondLease.lease_token), false);
  assert.equal(persisted.includes("device-current-hash"), false);

  const replay = await deactivate(
    fixture.env,
    fixture.secondLease,
    "device-current-hash",
    requestId,
    { ip: "203.0.113.80" }
  );
  assert.equal(replay.response.status, 200);
  assert.equal(replay.payload.replayed, true);
  assert.equal(
    scalar(
      fixture.database,
      "SELECT COUNT(*) FROM audit_events WHERE device_id = ? AND event_type = 'self_deactivate_committed'",
      fixture.first.device_id
    ),
    1
  );

  const conflict = await deactivate(
    fixture.env,
    fixture.other,
    "device-other-hash",
    requestId,
    { ip: "203.0.113.81" }
  );
  assert.equal(conflict.response.status, 409);
  assert.equal(conflict.payload.status, "idempotency_conflict");
  assert.equal(
    scalar(fixture.database, "SELECT status FROM devices WHERE id = ?", fixture.other.device_id),
    "active"
  );
}

async function testDeactivationRejectsForeignExpiredAndInvalidRequests() {
  const fixture = await deactivationFixture();
  const foreign = await call(fixture.env, "/v1/deactivate", {
    license_id: fixture.other.license_id,
    device_id: fixture.other.device_id,
    device_hash: "device-other-hash",
    lease_token: fixture.secondLease.lease_token,
    request_id: "22222222-2222-4222-8222-222222222222",
  }, { ip: "203.0.113.90" });
  assert.equal(foreign.response.status, 403);
  assert.equal(foreign.payload.status, "lease_invalid");
  assert.equal(
    scalar(fixture.database, "SELECT status FROM devices WHERE id = ?", fixture.other.device_id),
    "active"
  );

  fixture.database.prepare(
    "UPDATE lease_tokens SET expires_at = ? WHERE token_hash = ?"
  ).run(
    "2000-01-01T00:00:00.000Z",
    await sha256Hex(fixture.secondLease.lease_token, LEASE_TOKEN_PEPPER)
  );
  const expired = await deactivate(
    fixture.env,
    fixture.secondLease,
    "device-current-hash",
    "33333333-3333-4333-8333-333333333333",
    { ip: "203.0.113.91" }
  );
  assert.equal(expired.response.status, 403);
  assert.equal(expired.payload.status, "lease_invalid");

  const invalidRequest = await call(fixture.env, "/v1/deactivate", {
    license_id: fixture.other.license_id,
    device_id: fixture.other.device_id,
    device_hash: "device-other-hash",
    lease_token: fixture.other.lease_token,
    request_id: "not-a-uuid",
  }, { ip: "203.0.113.92" });
  assert.equal(invalidRequest.response.status, 400);
  assert.equal(invalidRequest.payload.status, "invalid");
}

async function testUnauthenticatedAuditSmugglingIsRedacted() {
  const fixture = await createFixture();
  const smuggled = [
    "SMUGGLED_RAW_DEVICE_HASH",
    "SMUGGLED_RAW_LEASE_TOKEN",
    "SMUGGLED_RAW_LICENSE_CODE",
    "SMUGGLED_ARBITRARY_SECRET",
  ];
  const denied = await call(fixture.env, "/v1/deactivate", {
    license_id: smuggled[0],
    device_id: smuggled[1],
    device_hash: smuggled[2],
    lease_token: smuggled[3],
    request_id: "99999999-9999-4999-8999-999999999991",
  }, { ip: "203.0.113.96" });
  assert.equal(denied.response.status, 403);
  assert.equal(denied.payload.status, "lease_invalid");

  const malformed = await call(fixture.env, "/v1/deactivate", {
    license_id: smuggled[3],
    device_id: smuggled[2],
    device_hash: smuggled[1],
    lease_token: smuggled[0],
    request_id: "malformed-request-id",
  }, { ip: "203.0.113.97" });
  assert.equal(malformed.response.status, 400);
  assert.equal(malformed.payload.status, "invalid");

  const audits = fixture.database.prepare(
    "SELECT * FROM audit_events ORDER BY created_at"
  ).all();
  assert.equal(audits.length, 2);
  for (const row of audits) {
    assert.equal(row.license_id, null);
    assert.equal(row.device_id, null);
    assert.equal(row.event_type, "deny");
    assert.ok(["invalid", "lease_invalid"].includes(row.reason));
  }
  const persisted = JSON.stringify(audits);
  for (const marker of smuggled) {
    assert.equal(persisted.includes(marker), false);
  }
}

async function testConcurrentDeactivationRequests() {
  const sameRequest = await deactivationFixture();
  const requestId = "77777777-7777-4777-8777-777777777777";
  const sameResults = await Promise.all([
    deactivate(
      sameRequest.env,
      sameRequest.secondLease,
      "device-current-hash",
      requestId,
      { ip: "203.0.113.93" }
    ),
    deactivate(
      sameRequest.env,
      sameRequest.secondLease,
      "device-current-hash",
      requestId,
      { ip: "203.0.113.93" }
    ),
  ]);
  assert.deepEqual(
    sameResults.map((result) => result.payload.status),
    ["ok", "ok"]
  );
  assert.deepEqual(
    sameResults.map((result) => result.payload.replayed).sort(),
    [false, true]
  );
  assert.equal(
    scalar(
      sameRequest.database,
      "SELECT COUNT(*) FROM audit_events WHERE device_id = ? AND event_type = 'self_deactivate_committed'",
      sameRequest.first.device_id
    ),
    1
  );

  const differentRequests = await deactivationFixture();
  const competing = await Promise.all([
    deactivate(
      differentRequests.env,
      differentRequests.secondLease,
      "device-current-hash",
      "88888888-8888-4888-8888-888888888881",
      { ip: "203.0.113.94" }
    ),
    deactivate(
      differentRequests.env,
      differentRequests.secondLease,
      "device-current-hash",
      "88888888-8888-4888-8888-888888888882",
      { ip: "203.0.113.95" }
    ),
  ]);
  assert.deepEqual(
    competing.map((result) => result.payload.status).sort(),
    ["lease_invalid", "ok"]
  );
  assert.equal(
    scalar(
      differentRequests.database,
      "SELECT COUNT(*) FROM device_deactivation_operations WHERE status = 'committed'"
    ),
    1
  );
}

async function testFiveStepRollbackAndRetry() {
  for (let step = 1; step <= 5; step += 1) {
    const fixture = await deactivationFixture();
    const requestId = `44444444-4444-4444-8444-44444444444${step}`;
    fixture.DB.failBatchAt = step;
    const failed = await deactivate(
      fixture.env,
      fixture.secondLease,
      "device-current-hash",
      requestId,
      { ip: `203.0.113.${100 + step}` }
    );
    assert.equal(failed.response.status, 500);
    assert.equal(failed.payload.status, "server_error");
    assert.equal(JSON.stringify(failed.payload).includes("probe batch fault"), false);
    assert.equal(
      scalar(fixture.database, "SELECT status FROM devices WHERE id = ?", fixture.first.device_id),
      "active"
    );
    assert.equal(
      scalar(
        fixture.database,
        "SELECT COUNT(*) FROM lease_tokens WHERE device_id = ? AND status = 'active'",
        fixture.first.device_id
      ),
      2
    );
    assert.equal(
      scalar(
        fixture.database,
        "SELECT COUNT(*) FROM device_deactivation_operations WHERE request_key_hash IS NOT NULL"
      ),
      0
    );
    assert.equal(
      scalar(
        fixture.database,
        "SELECT COUNT(*) FROM audit_events WHERE device_id = ? AND event_type = 'self_deactivate_committed'",
        fixture.first.device_id
      ),
      0
    );

    fixture.DB.failBatchAt = null;
    const retried = await deactivate(
      fixture.env,
      fixture.secondLease,
      "device-current-hash",
      requestId,
      { ip: `203.0.113.${100 + step}` }
    );
    assert.equal(retried.response.status, 200);
    assert.equal(retried.payload.deactivation_status, "stopped");
  }
}

async function testUnknownResultRecoveryAndRateLimit() {
  const fixture = await deactivationFixture();
  fixture.DB.throwAfterCommit = true;
  const requestId = "55555555-5555-4555-8555-555555555555";
  const recovered = await deactivate(
    fixture.env,
    fixture.secondLease,
    "device-current-hash",
    requestId,
    { ip: "203.0.113.120" }
  );
  assert.equal(recovered.response.status, 200);
  assert.equal(recovered.payload.replayed, true);
  assert.equal(
    scalar(
      fixture.database,
      "SELECT COUNT(*) FROM audit_events WHERE device_id = ? AND event_type = 'self_deactivate_committed'",
      fixture.first.device_id
    ),
    1
  );

  const limited = await deactivationFixture();
  let latest;
  for (let index = 0; index < 7; index += 1) {
    latest = await call(limited.env, "/v1/deactivate", {
      license_id: limited.first.license_id,
      device_id: limited.first.device_id,
      device_hash: "device-current-hash",
      lease_token: "wrong-token",
      request_id: `66666666-6666-4666-8666-66666666666${index}`,
    }, { ip: "203.0.113.121" });
  }
  assert.equal(latest.response.status, 429);
  assert.equal(latest.payload.status, "rate_limited");
  assert.ok(Number.isInteger(latest.payload.retry_after));
  assert.ok(latest.payload.retry_after > 0);
  assert.ok(Number(latest.response.headers.get("retry-after")) > 0);
}

async function testStrictPermanentPurge() {
  const fixture = await createFixture();
  const targetId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
  const otherId = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
  await seedLicense(fixture.database, {
    id: targetId,
    code: "PURGE-TARGET-CODE",
    maxDevices: 2,
  });
  fixture.database.prepare("UPDATE licenses SET note = ? WHERE id = ?").run(
    "给谁:敏感对象 | 联系方式:敏感联系方式 | 批次:删除探针 | 备注:只用于内存测试",
    targetId
  );
  await seedLicense(fixture.database, {
    id: otherId,
    code: "PURGE-OTHER-CODE",
    maxDevices: 2,
  });
  const targetA = await activate(fixture.env, "PURGE-TARGET-CODE", "purge-device-a", "203.0.113.140");
  const targetB = await activate(fixture.env, "PURGE-TARGET-CODE", "purge-device-b", "203.0.113.141");
  const other = await activate(fixture.env, "PURGE-OTHER-CODE", "purge-other-device", "203.0.113.142");
  assert.equal(targetA.response.status, 200);
  assert.equal(targetB.response.status, 200);
  assert.equal(other.response.status, 200);
  const identitiesBeforePurge = scalar(fixture.database, "SELECT COUNT(*) FROM identities");

  const blockedActive = await call(
    fixture.env,
    `/admin/licenses/${targetId}/purge-preview`,
    {},
    { admin: true }
  );
  assert.equal(blockedActive.response.status, 409);
  assert.equal(blockedActive.payload.status, "purge_blocked");

  const suspended = await call(
    fixture.env,
    `/admin/licenses/${targetId}`,
    { status: "suspended" },
    { admin: true, method: "PATCH" }
  );
  assert.equal(suspended.response.status, 200);
  assert.equal(
    scalar(fixture.database, "SELECT COUNT(*) FROM lease_tokens WHERE license_id = ? AND status = 'active'", targetId),
    0
  );
  fixture.database.prepare(
    "INSERT INTO device_deactivation_operations " +
      "(id, license_id, device_id, request_key_hash, request_fingerprint, status, audit_event_id, created_at, committed_at) " +
      "VALUES (?, ?, ?, ?, ?, 'committed', ?, ?, ?)"
  ).run(
    "purge-operation-committed",
    targetId,
    targetA.payload.device_id,
    "purge-operation-key-committed",
    "purge-operation-fingerprint-committed",
    "purge-operation-audit-committed",
    new Date().toISOString(),
    new Date().toISOString()
  );

  const preview = await call(
    fixture.env,
    `/admin/licenses/${targetId}/purge-preview`,
    {},
    { admin: true }
  );
  assert.equal(preview.response.status, 200);
  assert.deepEqual(preview.payload.counts, {
    devices: 2,
    leases: 2,
    deactivation_operations: 1,
  });
  assert.equal(preview.payload.confirmation_phrase, "永久删除 AAAAAAAA");
  assert.equal(JSON.stringify(preview.payload).includes("敏感联系方式"), false);

  const invalidConfirmation = await call(
    fixture.env,
    `/admin/licenses/${targetId}`,
    {
      confirmation_phrase: "永久删除 BBBBBBBB",
      preview_token: preview.payload.preview_token,
    },
    { admin: true, method: "DELETE" }
  );
  assert.equal(invalidConfirmation.response.status, 400);
  assert.equal(invalidConfirmation.payload.status, "invalid_confirmation");
  assert.equal(scalar(fixture.database, "SELECT COUNT(*) FROM licenses WHERE id = ?", targetId), 1);

  const tampered = await call(
    fixture.env,
    `/admin/licenses/${targetId}`,
    {
      confirmation_phrase: preview.payload.confirmation_phrase,
      preview_token: `${preview.payload.preview_token.slice(0, -1)}x`,
    },
    { admin: true, method: "DELETE" }
  );
  assert.equal(tampered.response.status, 409);
  assert.equal(tampered.payload.status, "purge_stale");

  fixture.database.prepare(
    "INSERT INTO device_deactivation_operations " +
      "(id, license_id, device_id, request_key_hash, request_fingerprint, status, audit_event_id, created_at, committed_at) " +
      "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, NULL)"
  ).run(
    "purge-operation-pending",
    targetId,
    targetB.payload.device_id,
    "purge-operation-key-pending",
    "purge-operation-fingerprint-pending",
    "purge-operation-audit-pending",
    new Date().toISOString()
  );
  const blockedPending = await call(
    fixture.env,
    `/admin/licenses/${targetId}/purge-preview`,
    {},
    { admin: true }
  );
  assert.equal(blockedPending.response.status, 409);
  assert.equal(blockedPending.payload.status, "purge_blocked");
  const staleAfterPending = await call(
    fixture.env,
    `/admin/licenses/${targetId}`,
    {
      confirmation_phrase: preview.payload.confirmation_phrase,
      preview_token: preview.payload.preview_token,
    },
    { admin: true, method: "DELETE" }
  );
  assert.equal(staleAfterPending.response.status, 409);
  assert.equal(staleAfterPending.payload.status, "purge_stale");
  fixture.database.prepare("DELETE FROM device_deactivation_operations WHERE id = ?")
    .run("purge-operation-pending");

  const freshPreview = await call(
    fixture.env,
    `/admin/licenses/${targetId}/purge-preview`,
    {},
    { admin: true }
  );
  const realDateNow = Date.now;
  Date.now = () => realDateNow() + 6 * 60 * 1000;
  try {
    const expiredPreview = await call(
      fixture.env,
      `/admin/licenses/${targetId}`,
      {
        confirmation_phrase: freshPreview.payload.confirmation_phrase,
        preview_token: freshPreview.payload.preview_token,
      },
      { admin: true, method: "DELETE" }
    );
    assert.equal(expiredPreview.response.status, 409);
    assert.equal(expiredPreview.payload.status, "purge_stale");
  } finally {
    Date.now = realDateNow;
  }
  const retryPreview = await call(
    fixture.env,
    `/admin/licenses/${targetId}/purge-preview`,
    {},
    { admin: true }
  );
  const purgeBody = {
    confirmation_phrase: retryPreview.payload.confirmation_phrase,
    preview_token: retryPreview.payload.preview_token,
  };
  fixture.DB.failBatchAt = 4;
  const rolledBack = await call(
    fixture.env,
    `/admin/licenses/${targetId}`,
    purgeBody,
    { admin: true, method: "DELETE" }
  );
  assert.equal(rolledBack.response.status, 409);
  assert.equal(rolledBack.payload.status, "purge_stale");
  assert.equal(scalar(fixture.database, "SELECT COUNT(*) FROM licenses WHERE id = ?", targetId), 1);
  assert.equal(scalar(fixture.database, "SELECT COUNT(*) FROM devices WHERE license_id = ?", targetId), 2);
  assert.equal(scalar(fixture.database, "SELECT COUNT(*) FROM lease_tokens WHERE license_id = ?", targetId), 2);
  assert.equal(scalar(fixture.database, "SELECT COUNT(*) FROM device_deactivation_operations WHERE license_id = ?", targetId), 1);
  assert.equal(scalar(fixture.database, "SELECT COUNT(*) FROM audit_events WHERE license_id = ? AND event_type = 'admin_purge_license'", targetId), 0);

  fixture.DB.failBatchAt = null;
  const purged = await call(
    fixture.env,
    `/admin/licenses/${targetId}`,
    purgeBody,
    { admin: true, method: "DELETE" }
  );
  assert.equal(purged.response.status, 200);
  assert.equal(purged.payload.purged, true);
  assert.equal(purged.payload.replayed, false);
  for (const table of ["licenses", "devices", "lease_tokens", "device_deactivation_operations"]) {
    assert.equal(scalar(fixture.database, `SELECT COUNT(*) FROM ${table} WHERE ${table === "licenses" ? "id" : "license_id"} = ?`, targetId), 0);
  }
  assert.equal(scalar(fixture.database, "SELECT COUNT(*) FROM identities"), identitiesBeforePurge);
  const purgeAudit = fixture.database.prepare(
    "SELECT event_type, reason FROM audit_events WHERE license_id = ? AND event_type = 'admin_purge_license'"
  ).get(targetId);
  assert.equal(purgeAudit.event_type, "admin_purge_license");
  assert.equal(/敏感对象|敏感联系方式/.test(purgeAudit.reason), false);
  assert.equal(scalar(fixture.database, "SELECT COUNT(*) FROM licenses WHERE id = ?", otherId), 1);
  assert.equal(scalar(fixture.database, "SELECT COUNT(*) FROM devices WHERE license_id = ?", otherId), 1);
  const otherVerified = await verify(fixture.env, other.payload, "purge-other-device", "203.0.113.143");
  assert.equal(otherVerified.response.status, 200);

  const replayed = await call(
    fixture.env,
    `/admin/licenses/${targetId}`,
    purgeBody,
    { admin: true, method: "DELETE" }
  );
  assert.equal(replayed.response.status, 200);
  assert.equal(replayed.payload.purged, true);
  assert.equal(replayed.payload.replayed, true);
  assert.equal(
    scalar(fixture.database, "SELECT COUNT(*) FROM audit_events WHERE license_id = ? AND event_type = 'admin_purge_license'", targetId),
    1
  );
}

async function testStrictPermanentPurgeWithoutOptionalDeactivationTable() {
  const fixture = await createFixture({ includeSelfDeactivationMigration: false });
  const missingId = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
  const targetId = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
  const otherId = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee";

  const missing = await call(
    fixture.env,
    `/admin/licenses/${missingId}/purge-preview`,
    {},
    { admin: true }
  );
  assert.equal(missing.response.status, 404);
  assert.equal(missing.payload.status, "not_found");

  await seedLicense(fixture.database, {
    id: targetId,
    code: "PURGE-LEGACY-TARGET",
    maxDevices: 2,
    status: "suspended",
  });
  await seedLicense(fixture.database, {
    id: otherId,
    code: "PURGE-LEGACY-OTHER",
    maxDevices: 2,
  });

  const preview = await call(
    fixture.env,
    `/admin/licenses/${targetId}/purge-preview`,
    {},
    { admin: true }
  );
  assert.equal(preview.response.status, 200);
  assert.deepEqual(preview.payload.counts, {
    devices: 0,
    leases: 0,
    deactivation_operations: 0,
  });

  const purged = await call(
    fixture.env,
    `/admin/licenses/${targetId}`,
    {
      confirmation_phrase: preview.payload.confirmation_phrase,
      preview_token: preview.payload.preview_token,
    },
    { admin: true, method: "DELETE" }
  );
  assert.equal(purged.response.status, 200);
  assert.equal(purged.payload.purged, true);
  assert.equal(fixture.DB.lastBatchLength, 8);
  assert.equal(scalar(fixture.database, "SELECT COUNT(*) FROM licenses WHERE id = ?", targetId), 0);
  assert.equal(scalar(fixture.database, "SELECT COUNT(*) FROM licenses WHERE id = ?", otherId), 1);
  assert.equal(
    scalar(fixture.database, "SELECT COUNT(*) FROM audit_events WHERE license_id = ? AND event_type = 'admin_purge_license'", targetId),
    1
  );
}

const tests = [
  ["dynamic max=1/2/5/higher and verify X/N", testDynamicLimitsAndVerify],
  ["concurrent activation and X>N history", testConcurrentActivationAndOverLimitHistory],
  ["admin max validation and legacy invalid max", testAdminMaxValidationAndLegacyInvalidMax],
  ["deactivation commit, replay, and current-device isolation", testDeactivationCommitReplayAndIsolation],
  ["foreign, expired, and malformed deactivation rejection", testDeactivationRejectsForeignExpiredAndInvalidRequests],
  ["unauthenticated audit field smuggling is redacted", testUnauthenticatedAuditSmugglingIsRedacted],
  ["concurrent same and competing deactivation requests", testConcurrentDeactivationRequests],
  ["five-step transaction rollback and same-request retry", testFiveStepRollbackAndRetry],
  ["unknown-result recovery and rate limiting", testUnknownResultRecoveryAndRateLimit],
  ["strict permanent purge isolation, rollback, and replay", testStrictPermanentPurge],
  ["strict permanent purge supports legacy schema without optional deactivation table", testStrictPermanentPurgeWithoutOptionalDeactivationTable],
];

let passed = 0;
for (const [name, test] of tests) {
  try {
    await test();
    passed += 1;
    console.log(`PASS ${name}`);
  } catch (error) {
    console.error(`FAIL ${name}`);
    throw error;
  }
}

console.log(`DEVICE_SELF_DEACTIVATION_PROBE_OK tests=${passed}`);
