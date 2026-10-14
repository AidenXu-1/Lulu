#!/usr/bin/env node
import http from "node:http";
import { constants as fsConstants } from "node:fs";
import { chmod, mkdir, mkdtemp, readFile, rm, stat, symlink } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import { findRunningInstance } from "./launch.mjs";
import { LEDGER_FILE_FLAGS, PRODUCTION_ADMIN_ROOT, loadProductionCredentials, neutralizeCsvFormula, safeFilename, startLocalAdmin, writeLedgerCsv } from "./server.mjs";

function assert(condition, message) { if (!condition) throw new Error(message); }
function writeJsonLine(stream, payload) {
  const written = stream.write(JSON.stringify(payload) + "\n");
  if (written) return Promise.resolve();
  return new Promise(resolve => stream.once("drain", resolve));
}
function assertNoStore(response, label) {
  assert((response.headers.get("cache-control") || "").includes("no-store"), `${label}: no-store missing`);
  assert((response.headers.get("pragma") || "").includes("no-cache"), `${label}: pragma missing`);
  assert(response.headers.get("expires") === "0", `${label}: expires missing`);
}

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
      continue;
    }
    if (char === '"') quoted = true;
    else if (char === ",") { row.push(field); field = ""; }
    else if (char === "\n") { row.push(field); rows.push(row); row = []; field = ""; }
    else if (char !== "\r") field += char;
  }
  if (field || row.length) { row.push(field); rows.push(row); }
  if (quoted) throw new Error("unterminated CSV quote");
  return rows;
}

function fakeProduction() {
  const items = new Map();
  const codes = new Set();
  const purged = new Set();
  const upstreamHeaders = [];
  const upstreamOptions = [];
  let sequence = 0;
  const fetchImpl = async (url, options = {}) => {
    const parsed = new URL(url);
    const headers = Object.fromEntries(new Headers(options.headers || {}).entries());
    upstreamHeaders.push(headers);
    upstreamOptions.push({ redirect:options.redirect || "follow", url:String(url) });
    if (parsed.searchParams.get("limit") === "1") {
      return new Promise((resolve, reject) => {
        const onAbort = () => { const error = new Error("aborted"); error.name = "AbortError"; reject(error); };
        options.signal?.addEventListener("abort", onAbort, { once:true });
      });
    }
    if (parsed.searchParams.get("limit") === "2") return new Response(JSON.stringify({ status:"forbidden" }), { status:403, headers:{ "content-type":"application/json" } });
    if (parsed.searchParams.get("limit") === "3") return new Response("", { status:302, headers:{ location:"https://evil.invalid/admin/licenses" } });
    const body = options.body ? JSON.parse(options.body) : {};
    if (options.method === "POST" && parsed.pathname.endsWith("/admin/licenses")) {
      if (codes.has(body.license_code)) return new Response(JSON.stringify({ status:"duplicate" }), { status:409, headers:{ "content-type":"application/json" } });
      codes.add(body.license_code); sequence += 1;
      const id = `00000000-0000-4000-8000-${String(sequence).padStart(12,"0")}`;
      items.set(id, { license_id:id, status:"active", max_devices:body.max_devices, expires_at:body.expires_at, recipient_name:body.recipient_name, contact:body.contact, batch:body.batch, remark:body.remark, created_at:"2026-07-10T11:00:00.000Z", updated_at:"2026-07-10T11:00:00.000Z", active_device_count:0, code_hash:"forbidden-hash", device_hash:"forbidden-device", lease_token:"forbidden-lease" });
      return new Response(JSON.stringify({ status:"ok", license_id:id, created_at:"2026-07-10T11:00:00.000Z" }), { status:201, headers:{ "content-type":"application/json" } });
    }
    if (options.method === "GET" && parsed.pathname.endsWith("/admin/licenses")) return new Response(JSON.stringify({ status:"ok", items:Array.from(items.values()) }), { status:200, headers:{ "content-type":"application/json" } });
    const preview = parsed.pathname.match(/\/admin\/licenses\/([^/]+)\/purge-preview$/);
    if (options.method === "POST" && preview) {
      const id = decodeURIComponent(preview[1]), item = items.get(id);
      if (!item) return new Response(JSON.stringify({ status:"not_found" }), { status:404 });
      if (item.status === "active") return new Response(JSON.stringify({ status:"purge_blocked" }), { status:409 });
      const suffix = id.replaceAll("-","").slice(-8).toUpperCase();
      return new Response(JSON.stringify({
        status:"ok", license_id:id, license_status:item.status, recipient_name:item.recipient_name, id_suffix:suffix,
        confirmation_phrase:`永久删除 ${suffix}`, counts:{ devices:2, leases:3, deactivation_operations:1 },
        preview_token:`preview-${id}`, expires_at:"2026-07-10T11:05:00.000Z", audit_retained:true, local_csv_deleted:false,
        code_hash:"forbidden-preview-hash", admin_token:"forbidden-preview-token",
      }), { status:200, headers:{ "content-type":"application/json" } });
    }
    const reset = parsed.pathname.match(/\/admin\/licenses\/([^/]+)\/devices\/reset$/);
    if (options.method === "POST" && reset) { const item = items.get(decodeURIComponent(reset[1])); if (!item) return new Response(JSON.stringify({ status:"not_found" }), { status:404 }); item.active_device_count = 0; return new Response(JSON.stringify({ status:"ok" }), { status:200 }); }
    const patch = parsed.pathname.match(/\/admin\/licenses\/([^/]+)$/);
    if (options.method === "PATCH" && patch) { const item = items.get(decodeURIComponent(patch[1])); if (!item) return new Response(JSON.stringify({ status:"not_found" }), { status:404 }); Object.assign(item, body, { updated_at:"2026-07-10T11:01:00.000Z" }); return new Response(JSON.stringify({ status:"ok" }), { status:200 }); }
    if (options.method === "DELETE" && patch) {
      const id = decodeURIComponent(patch[1]), item = items.get(id);
      if (!item && purged.has(id)) return new Response(JSON.stringify({ status:"ok", purged:true, replayed:true, audit_retained:true, local_csv_deleted:false }), { status:200 });
      if (!item) return new Response(JSON.stringify({ status:"not_found" }), { status:404 });
      const phrase = `永久删除 ${id.replaceAll("-","").slice(-8).toUpperCase()}`;
      if (body.confirmation_phrase !== phrase) return new Response(JSON.stringify({ status:"invalid_confirmation" }), { status:400 });
      if (body.preview_token !== `preview-${id}`) return new Response(JSON.stringify({ status:"purge_stale" }), { status:409 });
      items.delete(id); purged.add(id);
      return new Response(JSON.stringify({ status:"ok", purged:true, replayed:false, audit_retained:true, local_csv_deleted:false }), { status:200 });
    }
    return new Response(JSON.stringify({ status:"not_found" }), { status:404, headers:{ "content-type":"application/json" } });
  };
  return { fetchImpl, upstreamHeaders, upstreamOptions };
}

function requestWithHost(port, hostHeader) {
  return new Promise((resolve, reject) => {
    const req = http.request({ hostname:"127.0.0.1", port, path:"/__health", method:"GET", headers:{ host:hostHeader } }, res => { let text=""; res.setEncoding("utf8"); res.on("data", chunk => { text += chunk; }); res.on("end", () => resolve({ status:res.statusCode, text })); });
    req.on("error", reject); req.end();
  });
}

function listenServer(server, port = 0) {
  return new Promise((resolve, reject) => {
    const onError = error => { cleanup(); reject(error); };
    const onListening = () => { cleanup(); resolve(server.address().port); };
    const cleanup = () => { server.off("error", onError); server.off("listening", onListening); };
    server.once("error", onError); server.once("listening", onListening); server.listen(port, "127.0.0.1");
  });
}

async function freePort() {
  const server = http.createServer();
  const port = await listenServer(server);
  await new Promise(resolve => server.close(resolve));
  return port;
}

async function main() {
  if (process.env.LULU_LOCAL_ADMIN_PROBE_FORCE_FAILURE === "1") throw new Error("forced probe failure");
  const temp = await mkdtemp(path.join(os.tmpdir(), "lulu-local-admin-probe-"));
  const ledgerDir = path.join(temp, "ledger"), runtimeFile = path.join(temp, "runtime", "state.json");
  const dummy = http.createServer((req, res) => { res.writeHead(200); res.end("unrelated"); });
  const occupiedPort = await listenServer(dummy);
  const fallbackPort = await freePort();
  const fake = fakeProduction();
  let handle;
  let code = "";
  try {
    const queriedServices = [];
    const credentials = await loadProductionCredentials({ runner:async (_command, args) => {
      const service = args[2];
      queriedServices.push(service);
      if (service === "lulu-production-admin-token") return { stdout:"TEST_ADMIN_TOKEN_NEVER_REAL_123456\n" };
      throw new Error("missing fixture service");
    } });
    assert(credentials.adminRoot === PRODUCTION_ADMIN_ROOT, "production origin was not code-bound");
    assert(!queriedServices.some(service => service.includes("endpoint")), "gateway still selected endpoint from Keychain");

    let conflictClosed = false;
    try {
      await loadProductionCredentials({ runner:async (_command, args) => {
        const service = args[2];
        if (service === "lulu-production-admin-token") return { stdout:"TEST_ADMIN_TOKEN_NEVER_REAL_123456\n" };
        if (service === "lulu-license-production-admin-token") return { stdout:"DIFFERENT_TEST_TOKEN_NEVER_REAL_987654\n" };
        throw new Error("missing fixture service");
      } });
    } catch (error) { conflictClosed = error?.code === "keychain_conflict"; }
    assert(conflictClosed, "conflicting Keychain tokens did not fail closed");

    for (const value of ["=1+1", "+SUM(1,1)", "-2+3", "@cmd", "  =hidden", "\u00a0=hidden", "\ttext", "\rtext", "\ntext", " \ttext"]) {
      assert(neutralizeCsvFormula(value).startsWith("'"), "dangerous CSV prefix was not neutralized");
    }
    for (const value of ["普通中文", "person@example.com", "13800138000", "LULU-ABCD-2345-EFGH-6789", "  普通文本"]) {
      assert(neutralizeCsvFormula(value) === value, "safe CSV text was changed");
    }
    const structuredDir = path.join(temp, "structured-ledger");
    const structuredPath = await writeLedgerCsv({
      ledgerDir:structuredDir,
      created:{ license_id:"safe-id", created_at:"2026-07-10T12:00:00.000Z" },
      form:{ license_code:"LULU-ABCD-2345-EFGH-6789", recipient_name:"=WEBSERVICE(\"x\")", contact:"  +SUM(1,1)", batch:"../;$(unsafe) @batch", max_devices:2, expires_at:null, remark:"\t@formula,\n\"quoted\"\r\n第二行" },
    });
    const structuredCsv = await readFile(structuredPath, "utf8");
    const parsedCsv = parseCsv(structuredCsv);
    assert(parsedCsv.length === 2 && parsedCsv.every(row => row.length === 10), "structured CSV row/column shape changed");
    assert(parsedCsv[1][0] === "LULU-ABCD-2345-EFGH-6789", "valid license code was changed");
    assert(parsedCsv[1][2].startsWith("'="), "recipient formula prefix not neutralized");
    assert(parsedCsv[1][3].startsWith("'  +"), "leading-space formula prefix not neutralized");
    assert(parsedCsv[1][9].startsWith("'\t") && parsedCsv[1][9].includes(',\n"quoted"\r\n'), "multiline/quote CSV structure changed");
    const hardenedName = path.parse(structuredPath).name;
    assert(!/[.;$()@/\\]/.test(hardenedName) && !hardenedName.includes(".."), "batch filename was not hardened");
    assert(safeFilename("../;$(unsafe) @batch").length <= 40, "safe filename length invalid");
    if (fsConstants.O_NOFOLLOW) assert((LEDGER_FILE_FLAGS & fsConstants.O_NOFOLLOW) !== 0, "O_NOFOLLOW missing from ledger flags");

    const symlinkTarget = path.join(temp, "symlink-target");
    const symlinkLedger = path.join(temp, "symlink-ledger");
    await mkdir(symlinkTarget, { mode:0o700 });
    await symlink(symlinkTarget, symlinkLedger);
    let symlinkRejected = false;
    try {
      await writeLedgerCsv({ ledgerDir:symlinkLedger, created:{ license_id:"safe-id", created_at:"2026-07-10T12:00:00.000Z" }, form:{ license_code:"LULU-ABCD-2345-EFGH-6789", recipient_name:"safe", contact:"safe", batch:"safe", max_devices:2, expires_at:null, remark:"safe" } });
    } catch (error) { symlinkRejected = error?.code === "ledger_symlink"; }
    assert(symlinkRejected, "symlink ledger directory was not rejected");

    handle = await startLocalAdmin({ credentials:{ adminRoot:"https://production-admin.invalid/admin", adminToken:"TEST_ADMIN_TOKEN_NEVER_REAL_123456" }, fetchImpl:fake.fetchImpl, ledgerDir, runtimeFile, ports:[occupiedPort,fallbackPort], upstreamTimeoutMs:120 });
    assert(handle.port === fallbackPort, "unrelated port conflict did not self-heal");

    const launchUrl = new URL(handle.url);
    const bootstrap = new URLSearchParams(launchUrl.hash.slice(1)).get("bootstrap") || "";
    assert(bootstrap.length >= 32, "launch URL missing high-entropy bootstrap capability");
    assert(!String(handle.url).includes("TEST_ADMIN_TOKEN_NEVER_REAL_123456"), "launch URL leaked upstream token");

    const unauthenticatedPage = await fetch(`http://127.0.0.1:${handle.port}/admin`);
    assert(unauthenticatedPage.status === 200, "bootstrap page unavailable"); assertNoStore(unauthenticatedPage, "bootstrap page");
    assert(!(unauthenticatedPage.headers.get("set-cookie") || ""), "bare admin GET minted a session cookie");
    const bootstrapPage = await unauthenticatedPage.text();
    assert(!bootstrapPage.includes('"csrfToken"'), "bare admin GET leaked a CSRF token");
    const bootstrapScript = bootstrapPage.match(/<script nonce="[^"]+">([\s\S]*)<\/script>/)?.[1] || "";
    new Function(bootstrapScript);

    const base = `http://127.0.0.1:${handle.port}`;
    const exchanged = await fetch(base + "/admin/bootstrap", {
      method:"POST",
      headers:{ origin:base, "content-type":"application/json", accept:"application/json" },
      body:JSON.stringify({ bootstrap }),
    });
    assert(exchanged.status === 204, "launch capability did not establish a browser session");
    const cookie = (exchanged.headers.get("set-cookie") || "").split(";")[0];
    assert(cookie, "bootstrap exchange did not return a session cookie");

    const replayedBootstrap = await fetch(base + "/admin/bootstrap", {
      method:"POST",
      headers:{ origin:base, "content-type":"application/json", accept:"application/json" },
      body:JSON.stringify({ bootstrap }),
    });
    assert(replayedBootstrap.status === 403, "bootstrap capability was replayable");

    const unauthorizedReopen = await fetch(base + "/__reopen", { method:"POST", headers:{ accept:"application/json" } });
    assert(unauthorizedReopen.status === 403, "bare localhost client minted a new bootstrap");
    const running = await findRunningInstance(runtimeFile);
    assert(running?.port === fallbackPort, "existing instance was not detected");
    const reopenedUrl = new URL(running.url);
    const reopenedBootstrap = new URLSearchParams(reopenedUrl.hash.slice(1)).get("bootstrap") || "";
    assert(reopenedBootstrap.length >= 32 && reopenedBootstrap !== bootstrap, "existing instance did not rotate bootstrap capability");
    const reopenedExchange = await fetch(base + "/admin/bootstrap", {
      method:"POST",
      headers:{ origin:base, "content-type":"application/json", accept:"application/json" },
      body:JSON.stringify({ bootstrap:reopenedBootstrap }),
    });
    assert(reopenedExchange.status === 204, "fresh launcher could not restore a lost browser session");
    const reopenedCookie = (reopenedExchange.headers.get("set-cookie") || "").split(";")[0];
    const reopenedPage = await fetch(base + "/admin", { headers:{ cookie:reopenedCookie } });
    assert(reopenedCookie && reopenedPage.status === 200, "reopened browser session is unusable");

    const pageResponse = await fetch(base + "/admin", { headers:{ cookie } });
    assert(pageResponse.status === 200, "admin page unavailable after bootstrap"); assertNoStore(pageResponse, "page");
    assert((pageResponse.headers.get("content-security-policy") || "").includes("frame-ancestors 'none'"), "CSP missing");
    const page = await pageResponse.text();
    const csrf = page.match(/"csrfToken":"([^"]+)"/)?.[1] || "";
    assert(cookie && csrf, "session/csrf missing");
    assert(!page.includes("TEST_ADMIN_TOKEN_NEVER_REAL_123456"), "page leaked upstream token");
    assert(!page.includes("production-admin.invalid"), "page leaked upstream endpoint");
    assert(!/localStorage|sessionStorage|x-admin-token|Admin Token/i.test(page), "page contains forbidden browser secret pattern");
    const inlineScript = page.match(/<script nonce="[^"]+">([\s\S]*)<\/script>/)?.[1] || "";
    new Function(inlineScript);

    async function local(pathname, { method="GET", body=null, origin=base, csrfValue=csrf, sessionCookie=cookie } = {}) {
      const headers = { accept:"application/json", cookie:sessionCookie };
      if (method !== "GET") { headers.origin = origin; headers["x-lulu-local-csrf"] = csrfValue; headers["content-type"] = "application/json"; }
      const response = await fetch(base + pathname, { method, headers, body:body === null ? undefined : JSON.stringify(body) });
      const data = await response.json().catch(() => ({}));
      return { response, data };
    }

    const badHost = await requestWithHost(handle.port, "evil.example"); assert(badHost.status === 403, "non-loopback Host did not fail closed");
    const noSession = await local("/admin/licenses", { sessionCookie:"" }); assert(noSession.response.status === 403, "missing session did not fail closed"); assertNoStore(noSession.response, "missing session");
    const noOrigin = await local("/admin/licenses", { method:"POST", origin:"", body:{} }); assert(noOrigin.response.status === 403, "missing origin did not fail closed");
    const badOrigin = await local("/admin/licenses", { method:"POST", origin:"https://evil.invalid", body:{} }); assert(badOrigin.response.status === 403, "bad origin did not fail closed");
    const badCsrf = await local("/admin/licenses", { method:"POST", csrfValue:"wrong", body:{} }); assert(badCsrf.response.status === 403, "bad csrf did not fail closed");

    const generated = await local("/admin/code"); code = generated.data.license_code; assert(/^LULU-[A-Z2-9]{4}(?:-[A-Z2-9]{4}){3}$/.test(code), "random code format invalid");
    const createBody = { license_code:code, max_devices:2, expires_at:"2027-07-10T00:00:00.000Z", recipient_name:"本地探针", contact:"redacted-contact", batch:"本机工具探针", remark:"fake upstream only" };
    const created = await local("/admin/licenses", { method:"POST", body:createBody }); assert(created.response.status === 201, "create failed"); assertNoStore(created.response, "create");
    assert(!JSON.stringify(created.data).includes(code), "create response echoed plaintext code");
    const ledgerPath = created.data.ledger_path; assert(ledgerPath && ledgerPath.startsWith(ledgerDir), "ledger path invalid");
    assert(((await stat(ledgerDir)).mode & 0o777) === 0o700, "ledger directory is not 0700");
    assert(((await stat(ledgerPath)).mode & 0o777) === 0o600, "ledger file is not 0600");
    const csv = await readFile(ledgerPath, "utf8"); assert(csv.includes("明文授权码") && csv.includes("license_id") && csv.includes(code), "ledger fields missing");
    const duplicate = await local("/admin/licenses", { method:"POST", body:createBody }); assert(duplicate.response.status === 409 && duplicate.data.status === "duplicate", "duplicate did not fail closed");

    const listed = await local("/admin/licenses"); assert(listed.data.items.length === 1, "list missing created item");
    const serializedList = JSON.stringify(listed.data); assert(!serializedList.includes(code), "list leaked plaintext code"); assert(!/forbidden-hash|forbidden-device|forbidden-lease/.test(serializedList), "list leaked upstream sensitive fields");
    const id = created.data.license_id;
    const suspended = await local(`/admin/licenses/${encodeURIComponent(id)}`, { method:"PATCH", body:{ ...createBody, license_code:undefined, status:"suspended", max_devices:3 } }); assert(suspended.response.status === 200, "suspend failed");
    const restored = await local(`/admin/licenses/${encodeURIComponent(id)}`, { method:"PATCH", body:{ status:"active", recipient_name:"本地探针", contact:"redacted-contact", batch:"本机工具探针", remark:"restored", max_devices:3, expires_at:null } }); assert(restored.response.status === 200, "restore failed");
    const reset = await local(`/admin/licenses/${encodeURIComponent(id)}/devices/reset`, { method:"POST", body:{} }); assert(reset.response.status === 200, "reset failed");
    const blockedPurge = await local(`/admin/licenses/${encodeURIComponent(id)}/purge-preview`, { method:"POST", body:{} }); assert(blockedPurge.response.status === 409 && blockedPurge.data.status === "purge_blocked", "active purge preview did not fail closed");
    const stoppedForPurge = await local(`/admin/licenses/${encodeURIComponent(id)}`, { method:"PATCH", body:{ status:"suspended" } }); assert(stoppedForPurge.response.status === 200, "pre-purge suspend failed");
    const purgePreview = await local(`/admin/licenses/${encodeURIComponent(id)}/purge-preview`, { method:"POST", body:{} }); assert(purgePreview.response.status === 200, "purge preview failed");
    assert(purgePreview.data.counts.devices === 2 && purgePreview.data.counts.leases === 3 && purgePreview.data.counts.deactivation_operations === 1, "purge preview counts changed");
    assert(!/forbidden-preview-hash|forbidden-preview-token/.test(JSON.stringify(purgePreview.data)), "purge preview leaked unknown upstream fields");
    const badPurge = await local(`/admin/licenses/${encodeURIComponent(id)}`, { method:"DELETE", body:{ confirmation_phrase:"永久删除 错误目标", preview_token:purgePreview.data.preview_token } }); assert(badPurge.response.status === 400 && badPurge.data.status === "invalid_confirmation", "bad purge phrase did not fail closed");
    const purged = await local(`/admin/licenses/${encodeURIComponent(id)}`, { method:"DELETE", body:{ confirmation_phrase:purgePreview.data.confirmation_phrase, preview_token:purgePreview.data.preview_token } }); assert(purged.response.status === 200 && purged.data.purged === true && purged.data.replayed === false, "purge failed");
    assert(await readFile(ledgerPath, "utf8"), "purge removed local CSV");
    const afterPurgeList = await local("/admin/licenses"); assert(afterPurgeList.data.items.length === 0, "purged item stayed in list");
    const replayedPurge = await local(`/admin/licenses/${encodeURIComponent(id)}`, { method:"DELETE", body:{ confirmation_phrase:purgePreview.data.confirmation_phrase, preview_token:purgePreview.data.preview_token } }); assert(replayedPurge.response.status === 200 && replayedPurge.data.replayed === true, "purge replay was not idempotent");

    const timeout = await local("/admin/licenses?limit=1&offset=0"); assert(timeout.response.status === 504 && timeout.data.status === "upstream_timeout", "upstream timeout not redacted");
    const refused = await local("/admin/licenses?limit=2&offset=0"); assert(refused.response.status === 502 && refused.data.status === "forbidden", "upstream refusal not redacted");
    const redirected = await local("/admin/licenses?limit=3&offset=0"); assert(redirected.response.status === 502, "upstream redirect was accepted");
    assert(fake.upstreamHeaders.some(headers => headers["x-admin-token"] === "TEST_ADMIN_TOKEN_NEVER_REAL_123456"), "backend did not inject admin token");
    assert(fake.upstreamHeaders.filter(headers => headers["x-lulu-admin-csrf"]).every(headers => headers.origin === "https://production-admin.invalid" && headers["x-lulu-admin-csrf"] === "same-origin"), "upstream mutation headers invalid");
    assert(fake.upstreamOptions.every(options => options.redirect === "manual"), "upstream redirect mode was not fail-closed");

    let missingKeychainClosed = false;
    try { await startLocalAdmin({ loadCredentials:async () => { throw new Error("missing"); }, ledgerDir:path.join(temp,"unused"), runtimeFile:path.join(temp,"unused.json"), ports:[await freePort()] }); } catch { missingKeychainClosed = true; }
    assert(missingKeychainClosed, "missing Keychain path did not fail closed");

    const shutdown = await local("/admin/shutdown", { method:"POST", body:{} }); assert(shutdown.response.status === 200, "shutdown failed"); await handle.closed;
    const afterClose = await fetch(base + "/__health").then(() => false).catch(() => true); assert(afterClose, "closed session still reachable");

    return {
      syntax_and_page_script:"ok",
      default_port_conflict_self_heal:"ok",
      existing_instance_detection:"ok",
      loopback_host:"fail_closed",
      bare_admin_get:"no_session_or_csrf",
      bootstrap_capability:"single_use",
      session_origin_csrf:"fail_closed",
      no_store_and_csp:"ok",
      browser_zero_upstream_secret:"ok",
      random_code_format:"ok",
      create_and_refresh_no_plaintext:"ok",
      duplicate_constraint:"ok",
      ledger_directory_mode:"0700",
      ledger_file_mode:"0600",
      csv_formula_prefix_neutralization:"ok",
      csv_structured_parse_rows_columns:"ok",
      csv_commas_quotes_multiline:"ok",
      csv_safe_text_unchanged:"ok",
      ledger_directory_symlink:"rejected",
      ledger_file_no_follow:fsConstants.O_NOFOLLOW ? "ok" : "platform_unavailable",
      batch_filename_hardening:"ok",
      ledger_no_overwrite_shape:"unique_timestamp_batch_random",
      list_sensitive_field_allowlist:"ok",
      patch_suspend_restore_reset:"ok",
      strict_single_purge_preview_delete_replay:"ok",
      purge_keeps_local_csv:"ok",
      keychain_missing:"fail_closed",
      production_origin:"code_bound",
      keychain_token_conflict:"fail_closed",
      upstream_timeout_refusal:"redacted_fail_closed",
      upstream_redirect:"manual_fail_closed",
      close_invalidates_session:"ok",
      raw_code_printed:false,
      raw_endpoint_printed:false,
      raw_token_printed:false,
    };
  } finally {
    if (handle) await handle.stop().catch(() => {});
    await new Promise(resolve => dummy.close(resolve));
    code = "";
    await chmod(temp, 0o700).catch(() => {});
    await rm(temp, { recursive:true, force:true });
  }
}

try {
  await writeJsonLine(process.stdout, await main());
} catch (error) {
  await writeJsonLine(process.stderr, {
    status:"failed",
    probe:"local_admin",
    reason:"assertion_or_runtime_failure",
    detail:String(error?.message || "unknown").slice(0, 160),
    sensitive_values_printed:false,
  });
  process.exitCode = 1;
}
