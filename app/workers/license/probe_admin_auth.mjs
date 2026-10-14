#!/usr/bin/env node
import worker from "./worker.js";

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

async function main() {
  const env = {
    ADMIN_ALLOWED_EMAILS: "owner@example.com",
    ADMIN_TOKEN: "TEST_ADMIN_TOKEN_NEVER_REAL_123456",
    ADMIN_WEB_DEV_IDENTITY: "false",
    ADMIN_DEV_IDENTITY: "TEST_DEV_IDENTITY_NEVER_REAL",
  };
  const forgedHeaders = { "cf-access-authenticated-user-email": "owner@example.com" };

  const forgedPage = await worker.fetch(new Request("https://license.example/admin", {
    headers: forgedHeaders,
  }), env);
  assert(forgedPage.status === 403, "forged Access email reached admin page");

  const forgedMutation = await worker.fetch(new Request("https://license.example/admin/licenses", {
    method: "POST",
    headers: {
      ...forgedHeaders,
      origin: "https://license.example",
      "x-lulu-admin-csrf": "same-origin",
      "content-type": "application/json",
    },
    body: "{}",
  }), env);
  assert(forgedMutation.status === 403, "forged Access email reached admin mutation");

  const tokenMutation = await worker.fetch(new Request("https://license.example/admin/licenses", {
    method: "POST",
    headers: {
      "x-admin-token": env.ADMIN_TOKEN,
      origin: "https://license.example",
      "x-lulu-admin-csrf": "same-origin",
      "content-type": "application/json",
    },
    body: "{}",
  }), env);
  assert(tokenMutation.status === 400, "valid gateway token did not reach business validation");

  const devEnv = { ...env, ADMIN_WEB_DEV_IDENTITY: "true", LULU_LICENSE_ENV: "test" };
  const productionDevPage = await worker.fetch(new Request("https://license.example/admin", {
    headers: { "x-lulu-admin-dev-identity": env.ADMIN_DEV_IDENTITY },
  }), devEnv);
  assert(productionDevPage.status === 403, "development identity escaped onto a production-capable hostname");

  const devPage = await worker.fetch(new Request("https://license-probe.invalid/admin", {
    headers: { "x-lulu-admin-dev-identity": env.ADMIN_DEV_IDENTITY },
  }), devEnv);
  assert(devPage.status === 200, "reserved test-origin development identity stopped working");

  return {
    forged_access_email_page: "fail_closed",
    forged_access_email_mutation: "fail_closed",
    gateway_token: "preserved",
    production_dev_header: "fail_closed",
    reserved_test_dev_identity: "preserved",
  };
}

main().then(result => process.stdout.write(JSON.stringify(result) + "\n")).catch(error => {
  process.stderr.write(String(error?.stack || error) + "\n");
  process.exitCode = 1;
});
