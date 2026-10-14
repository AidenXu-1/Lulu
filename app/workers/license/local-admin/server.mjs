import crypto from "node:crypto";
import { execFile, spawn } from "node:child_process";
import { constants as fsConstants } from "node:fs";
import { chmod, lstat, mkdir, open, readFile, rename, rm, stat, writeFile } from "node:fs/promises";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { promisify } from "node:util";

import { renderAdminPage } from "./page.mjs";
import { DEFAULT_TARGET_DIR, ensureProjectLedgerDirectory } from "./ledger_migration.mjs";

const execFileAsync = promisify(execFile);
export const HOST = "127.0.0.1";
export const DEFAULT_PORTS = Array.from({ length: 20 }, (_, index) => 47821 + index);
export const DEFAULT_LEDGER_DIR = DEFAULT_TARGET_DIR;
export const DEFAULT_RUNTIME_FILE = path.join(os.homedir(), "Library", "Caches", "LuluLicenseAdmin", "runtime.json");
const SESSION_COOKIE = "lulu_local_admin_session";
const LICENSE_PATTERN = /^LULU-[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}$/;
const LICENSE_ID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const ALLOWED_STATUS = new Set(["active", "suspended", "revoked", "expired"]);
const ADMIN_TOKEN_SERVICES = ["lulu-production-admin-token", "lulu-license-production-admin-token", "lulu-license-admin-token-production", "lulu-cloudflare-admin-token"];
export const PRODUCTION_ADMIN_ROOT = "https://lulu-license-api.xuzhaoji2286.workers.dev/admin";
const SAFE_FIELDS = ["license_id", "status", "max_devices", "expires_at", "recipient_name", "contact", "batch", "remark", "created_at", "updated_at", "active_device_count"];
export const LEDGER_FILE_FLAGS = fsConstants.O_WRONLY | fsConstants.O_CREAT | fsConstants.O_EXCL | (fsConstants.O_NOFOLLOW || 0);

const NO_STORE = {
  "cache-control": "no-store, no-cache, max-age=0, must-revalidate",
  pragma: "no-cache",
  expires: "0",
  "x-content-type-options": "nosniff",
  "referrer-policy": "no-referrer",
  "x-frame-options": "DENY",
};

export class LocalAdminError extends Error {
  constructor(code, message, httpStatus = 500) { super(message); this.code = code; this.httpStatus = httpStatus; }
}

async function uniqueKeychainValue(services, runner = execFileAsync) {
  const values = new Set();
  for (const service of services) {
    try {
      const { stdout } = await runner("security", ["find-generic-password", "-s", service, "-w"], { encoding: "utf8", maxBuffer: 64 * 1024 });
      const value = String(stdout || "").trim();
      if (value) values.add(value);
    } catch { /* Try the next configured service name. */ }
  }
  if (values.size > 1) throw new LocalAdminError("keychain_conflict", "检测到多份不一致的 production 管理凭证，已停止连接。", 503);
  return values.values().next().value || "";
}

function normalizeAdminRoot(raw) {
  let url;
  try { url = new URL(String(raw || "").trim()); } catch { throw new LocalAdminError("bad_endpoint", "App 内 production 管理地址格式不正确。", 503); }
  const marker = url.href.toLowerCase();
  if (url.protocol !== "https:" || url.username || url.password || ["localhost", "127.0.0.1", "::1"].includes(url.hostname) || /(staging|mock|example|placeholder)/.test(marker)) {
    throw new LocalAdminError("bad_endpoint", "App 内 production 管理地址未通过安全检查。", 503);
  }
  url.search = ""; url.hash = "";
  const currentPath = url.pathname.replace(/\/+$/, "");
  if (!currentPath) url.pathname = "/admin";
  else if (!currentPath.endsWith("/admin")) throw new LocalAdminError("bad_endpoint_path", "App 内 production 管理地址路径不正确。", 503);
  else url.pathname = currentPath;
  return url.toString().replace(/\/+$/, "");
}

export async function loadProductionCredentials({ runner = execFileAsync } = {}) {
  const token = await uniqueKeychainValue(ADMIN_TOKEN_SERVICES, runner);
  if (!token) throw new LocalAdminError("keychain_missing", "缺少 production 管理 Keychain 配置，请先完成安全注入。", 503);
  if (token.length < 16) throw new LocalAdminError("bad_token", "production 管理凭证未通过安全检查。", 503);
  return { adminRoot: normalizeAdminRoot(PRODUCTION_ADMIN_ROOT), adminToken: token };
}

function generateGroup() {
  const alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
  return Array.from({ length: 4 }, () => alphabet[crypto.randomInt(alphabet.length)]).join("");
}

export function generateLicenseCode() { return `LULU-${generateGroup()}-${generateGroup()}-${generateGroup()}-${generateGroup()}`; }

function text(value, max) {
  const cleaned = String(value ?? "").trim();
  return cleaned.length <= max ? cleaned : cleaned.slice(0, max);
}

function normalizeMaxDevices(value) {
  const number = Number(value);
  if (!Number.isInteger(number) || number < 1 || number > 99) throw new LocalAdminError("invalid", "设备数必须是 1-99 的整数。", 400);
  return number;
}

function normalizeExpires(value) {
  if (value === null || value === undefined || value === "") return null;
  const date = new Date(String(value));
  if (Number.isNaN(date.getTime())) throw new LocalAdminError("invalid", "有效期格式不正确。", 400);
  return date.toISOString();
}

function validateCreate(body) {
  const licenseCode = text(body.license_code, 64).toUpperCase();
  if (!LICENSE_PATTERN.test(licenseCode)) throw new LocalAdminError("invalid", "授权码格式不正确。", 400);
  const recipientName = text(body.recipient_name, 80), contact = text(body.contact, 120), batch = text(body.batch, 80), remark = text(body.remark, 240);
  if (!recipientName || !contact || !batch || !remark) throw new LocalAdminError("invalid", "给谁、联系方式、批次和备注都必须填写。", 400);
  return { license_code: licenseCode, max_devices: normalizeMaxDevices(body.max_devices), expires_at: normalizeExpires(body.expires_at), recipient_name: recipientName, contact, batch, remark };
}

function validatePatch(body) {
  const out = {};
  if (Object.prototype.hasOwnProperty.call(body, "status")) { const status = text(body.status, 20); if (!ALLOWED_STATUS.has(status)) throw new LocalAdminError("invalid", "状态不正确。", 400); out.status = status; }
  if (Object.prototype.hasOwnProperty.call(body, "max_devices")) out.max_devices = normalizeMaxDevices(body.max_devices);
  if (Object.prototype.hasOwnProperty.call(body, "expires_at")) out.expires_at = normalizeExpires(body.expires_at);
  for (const [key, max] of [["recipient_name",80],["contact",120],["batch",80],["remark",240]]) {
    if (Object.prototype.hasOwnProperty.call(body, key)) { const value = text(body[key], max); if (!value) throw new LocalAdminError("invalid", "归属、联系方式、批次和备注不能为空。", 400); out[key] = value; }
  }
  if (!Object.keys(out).length) throw new LocalAdminError("invalid", "没有可保存的修改。", 400);
  return out;
}

function validateLicenseId(value) {
  const id = String(value || "").trim();
  if (!LICENSE_ID_PATTERN.test(id)) throw new LocalAdminError("invalid", "授权标识不正确。", 400);
  return id;
}

function validatePurge(body) {
  const confirmationPhrase = text(body?.confirmation_phrase, 80);
  const previewToken = text(body?.preview_token, 2048);
  if (!confirmationPhrase || !previewToken) {
    throw new LocalAdminError("invalid", "永久删除确认信息不完整。", 400);
  }
  return { confirmation_phrase:confirmationPhrase, preview_token:previewToken };
}

function publicLicense(item) {
  const safe = {};
  for (const field of SAFE_FIELDS) safe[field] = item?.[field] ?? null;
  safe.max_devices = Number(safe.max_devices || 1);
  safe.active_device_count = Number(safe.active_device_count || 0);
  return safe;
}

function publicPurgePreview(data) {
  const counts = {};
  for (const field of ["devices", "leases", "deactivation_operations"]) {
    const value = Number(data?.counts?.[field]);
    if (!Number.isSafeInteger(value) || value < 0) throw new LocalAdminError("upstream_invalid", "production 删除预检格式异常。", 502);
    counts[field] = value;
  }
  const previewToken = String(data?.preview_token || "");
  const confirmationPhrase = String(data?.confirmation_phrase || "");
  if (!LICENSE_ID_PATTERN.test(String(data?.license_id || "")) || !previewToken || previewToken.length > 2048 || !confirmationPhrase || confirmationPhrase.length > 80) {
    throw new LocalAdminError("upstream_invalid", "production 删除预检格式异常。", 502);
  }
  return {
    status:"ok",
    license_id:String(data.license_id),
    license_status:String(data.license_status || ""),
    recipient_name:text(data.recipient_name, 80),
    id_suffix:text(data.id_suffix, 16),
    confirmation_phrase:confirmationPhrase,
    counts,
    preview_token:previewToken,
    expires_at:String(data.expires_at || ""),
    audit_retained:data.audit_retained === true,
    local_csv_deleted:data.local_csv_deleted === true,
  };
}

function safeUpstreamMessage(status) {
  return ({
    duplicate:"授权码已存在，请重新生成。",
    invalid:"输入不符合 production 规则。",
    invalid_confirmation:"确认短语不一致，永久删除未执行。",
    purge_blocked:"该授权当前不能永久删除，请先停用并结束相关设备操作。",
    purge_stale:"删除确认已过期或数据已变化，请重新预检。",
    not_found:"目标授权不存在。",
    forbidden:"production 管理凭证被拒绝。",
  })[status] || "production 管理请求失败，请稍后重试。";
}

function upstreamClient(credentials, fetchImpl, timeoutMs) {
  return async (apiPath, { method = "GET", body = null } = {}) => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const headers = { accept:"application/json", "content-type":"application/json; charset=utf-8", "x-admin-token":credentials.adminToken };
      if (method !== "GET") { headers.origin = new URL(credentials.adminRoot).origin; headers["x-lulu-admin-csrf"] = "same-origin"; }
      const response = await fetchImpl(credentials.adminRoot + apiPath, { method, headers, body:body ? JSON.stringify(body) : undefined, signal:controller.signal, redirect:"manual" });
      const raw = await response.text();
      if (raw.length > 1024 * 1024) throw new LocalAdminError("upstream_large", "production 返回内容异常。", 502);
      let data = {}; try { data = raw ? JSON.parse(raw) : {}; } catch { throw new LocalAdminError("upstream_invalid", "production 返回格式异常。", 502); }
      if (!response.ok) {
        const status = text(data?.status, 30) || "unknown";
        const httpStatus = ["duplicate", "purge_blocked", "purge_stale"].includes(status)
          ? 409
          : ["invalid", "invalid_confirmation"].includes(status)
            ? 400
            : status === "not_found"
              ? 404
              : 502;
        throw new LocalAdminError(status, safeUpstreamMessage(status), httpStatus);
      }
      return data;
    } catch (error) {
      if (error instanceof LocalAdminError) throw error;
      if (error?.name === "AbortError") throw new LocalAdminError("upstream_timeout", "production 请求超时，请稍后重试。", 504);
      throw new LocalAdminError("upstream_unavailable", "production 暂时不可达，请稍后重试。", 502);
    } finally { clearTimeout(timer); }
  };
}

async function parseBody(req) {
  let raw = ""; req.setEncoding("utf8");
  for await (const chunk of req) { raw += chunk; if (raw.length > 128 * 1024) throw new LocalAdminError("too_large", "请求内容过大。", 413); }
  if (!raw) return {};
  try { return JSON.parse(raw); } catch { throw new LocalAdminError("invalid_json", "请求格式不正确。", 400); }
}

function cookieValue(req, name) {
  const cookies = String(req.headers.cookie || "").split(";");
  for (const cookie of cookies) { const [key, ...rest] = cookie.trim().split("="); if (key === name) return rest.join("="); }
  return "";
}

function sendJson(res, status, body, extra = {}) { res.writeHead(status, { ...NO_STORE, "content-type":"application/json; charset=utf-8", ...extra }); res.end(JSON.stringify(body)); }
function sendHtml(res, status, body, csp, cookie = "") {
  const headers = { ...NO_STORE, "content-type":"text/html; charset=utf-8", "content-security-policy":csp };
  if (cookie) headers["set-cookie"] = cookie;
  res.writeHead(status, headers);
  res.end(body);
}
function sendEmpty(res, status, extra = {}) { res.writeHead(status, { ...NO_STORE, ...extra }); res.end(); }

function renderBootstrapPage(nonce) {
  return `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Lulu 授权管理</title><style nonce="${nonce}">
:root{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;color:#3f3832;background:#f6f0e8}body{min-height:100vh;margin:0;display:grid;place-items:center}.card{width:min(420px,calc(100vw - 48px));padding:28px;border:1px solid #e5d8ca;border-radius:18px;background:#fffaf3;box-shadow:0 18px 48px rgba(76,57,39,.12)}h1{font-size:22px;margin:0 0 12px}p{margin:0;color:#75695f;line-height:1.7}
</style></head><body><main class="card"><h1>正在建立安全会话</h1><p id="status">请稍候…</p></main>
<script nonce="${nonce}">
(()=>{const status=document.getElementById("status");const token=new URLSearchParams(location.hash.slice(1)).get("bootstrap")||"";history.replaceState(null,"","/admin");if(!token){status.textContent="请从 Lulu 授权管理启动入口重新打开。";return;}fetch("/admin/bootstrap",{method:"POST",headers:{"content-type":"application/json","accept":"application/json"},body:JSON.stringify({bootstrap:token}),credentials:"same-origin"}).then(response=>{if(response.status!==204)throw new Error("denied");location.replace("/admin");}).catch(()=>{status.textContent="安全会话已失效，请关闭此页后从启动入口重新打开。";});})();
</script></body></html>`;
}

function sameSecret(left, right) {
  const a = Buffer.from(String(left || ""));
  const b = Buffer.from(String(right || ""));
  return a.length > 0 && a.length === b.length && crypto.timingSafeEqual(a, b);
}

export function neutralizeCsvFormula(value) {
  const raw = String(value ?? "");
  const leading = raw.match(/^\s*/u)?.[0] || "";
  const visible = raw.slice(leading.length);
  const dangerousControlPrefix = /[\t\r\n]/.test(leading);
  const dangerousFormulaPrefix = /^[=+\-@]/.test(visible);
  return dangerousControlPrefix || dangerousFormulaPrefix ? `'${raw}` : raw;
}

export function csvCell(value) { return `"${neutralizeCsvFormula(value).replaceAll('"','""')}"`; }

export function safeFilename(value) {
  const forbidden = new Set(Array.from("./\\:;|&$><`\"'!?*()[]{}%@"));
  const normalized = Array.from(String(value ?? "").normalize("NFKC"))
    .map(char => char.charCodeAt(0) < 32 || forbidden.has(char) ? "_" : char)
    .join("")
    .replace(/\s+/g, "-")
    .replace(/_+/g, "_")
    .replace(/-+/g, "-")
    .replace(/^[-_]+|[-_]+$/g, "")
    .slice(0, 40);
  return normalized || "未命名批次";
}

export async function ensureLedgerDirectory(ledgerDir) {
  if (path.resolve(ledgerDir) === path.resolve(DEFAULT_LEDGER_DIR)) {
    await ensureProjectLedgerDirectory({ targetDir:ledgerDir });
    return;
  }
  const parent = path.dirname(path.resolve(ledgerDir));
  try {
    const parentState = await lstat(parent);
    if (parentState.isSymbolicLink()) throw new LocalAdminError("ledger_parent_symlink", "台账父目录不能是符号链接。", 500);
    if (!parentState.isDirectory()) throw new LocalAdminError("ledger_parent_not_directory", "台账父路径不是目录。", 500);
  } catch (error) {
    if (error instanceof LocalAdminError) throw error;
    throw new LocalAdminError("ledger_parent_check_failed", "无法检查台账父目录。", 500);
  }
  try {
    const existing = await lstat(ledgerDir);
    if (existing.isSymbolicLink()) throw new LocalAdminError("ledger_symlink", "台账目录不能是符号链接。", 500);
    if (!existing.isDirectory()) throw new LocalAdminError("ledger_not_directory", "台账路径不是目录。", 500);
  } catch (error) {
    if (error instanceof LocalAdminError) throw error;
    if (error?.code !== "ENOENT") throw new LocalAdminError("ledger_check_failed", "无法检查台账目录。", 500);
    await mkdir(ledgerDir, { mode:0o700 });
    const created = await lstat(ledgerDir);
    if (created.isSymbolicLink() || !created.isDirectory()) throw new LocalAdminError("ledger_symlink", "台账目录安全检查失败。", 500);
  }
  await chmod(ledgerDir, 0o700);
}
function localTimestamp(date = new Date()) {
  const parts = new Intl.DateTimeFormat("sv-SE", { timeZone:"Asia/Shanghai", year:"numeric", month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit", second:"2-digit", hour12:false }).format(date);
  return parts.replaceAll("-", "").replace(" ", "-").replaceAll(":", "");
}

export async function writeLedgerCsv({ ledgerDir, created, form }) {
  await ensureLedgerDirectory(ledgerDir);
  const headers = ["明文授权码","license_id","给谁","联系方式","批次","设备数","有效期","创建时间","状态","备注"];
  const values = [form.license_code, created.license_id, form.recipient_name, form.contact, form.batch, form.max_devices, form.expires_at, created.created_at, "active", form.remark];
  const content = "\ufeff" + headers.map(csvCell).join(",") + "\n" + values.map(csvCell).join(",") + "\n";
  for (let attempt = 0; attempt < 5; attempt += 1) {
    const filename = `Lulu授权码台账-${localTimestamp()}-${safeFilename(form.batch)}-${crypto.randomBytes(3).toString("hex")}.csv`;
    const target = path.join(ledgerDir, filename);
    try { const handle = await open(target, LEDGER_FILE_FLAGS, 0o600); try { await handle.writeFile(content, "utf8"); } finally { await handle.close(); } await chmod(target, 0o600); return target; }
    catch (error) { if (error?.code !== "EEXIST") throw new LocalAdminError("ledger_failed", "CSV 台账保存失败，请检查本机目录权限。", 500); }
  }
  throw new LocalAdminError("ledger_collision", "CSV 台账文件名冲突，请重试。", 500);
}

async function writeRuntimeState(runtimeFile, state) {
  await mkdir(path.dirname(runtimeFile), { recursive:true, mode:0o700 }); await chmod(path.dirname(runtimeFile), 0o700);
  const temp = runtimeFile + `.tmp-${process.pid}-${crypto.randomBytes(3).toString("hex")}`;
  await writeFile(temp, JSON.stringify(state, null, 2) + "\n", { encoding:"utf8", mode:0o600, flag:"wx" }); await chmod(temp, 0o600); await rename(temp, runtimeFile); await chmod(runtimeFile, 0o600);
}

function listen(server, port) {
  return new Promise((resolve, reject) => {
    const onError = error => { cleanup(); reject(error); }; const onListening = () => { cleanup(); resolve(); };
    const cleanup = () => { server.off("error", onError); server.off("listening", onListening); };
    server.once("error", onError); server.once("listening", onListening); server.listen(port, HOST);
  });
}

export async function startLocalAdmin({
  credentials = null,
  loadCredentials = loadProductionCredentials,
  fetchImpl = globalThis.fetch,
  ledgerDir = DEFAULT_LEDGER_DIR,
  runtimeFile = DEFAULT_RUNTIME_FILE,
  ports = DEFAULT_PORTS,
  upstreamTimeoutMs = 10000,
  openBrowser = false,
} = {}) {
  const production = credentials || await loadCredentials();
  if (!production?.adminRoot || !production?.adminToken) throw new LocalAdminError("keychain_missing", "缺少 production 管理 Keychain 配置。", 503);
  await ensureLedgerDirectory(ledgerDir);
  const requestUpstream = upstreamClient(production, fetchImpl, upstreamTimeoutMs);
  const sessionToken = crypto.randomBytes(32).toString("base64url"), csrfToken = crypto.randomBytes(32).toString("base64url"), instanceId = crypto.randomUUID();
  let bootstrapToken = crypto.randomBytes(32).toString("base64url");
  let reopenToken = crypto.randomBytes(32).toString("base64url");
  let currentPort = null, stopping = false;
  let resolveClosed; const closed = new Promise(resolve => { resolveClosed = resolve; });

  const server = http.createServer(async (req, res) => {
    try {
      const expectedHost = `${HOST}:${currentPort}`;
      if (req.headers.host !== expectedHost) return sendJson(res, 403, { status:"forbidden", message:"仅允许本机 loopback Host。" });
      const url = new URL(req.url || "/", `http://${expectedHost}`);
      if (url.pathname === "/__health" && req.method === "GET") return sendJson(res, 200, { status:"ok", app:"lulu-license-local-admin", instance_id:instanceId, port:currentPort });
      if (url.pathname === "/__reopen" && req.method === "POST") {
        if (!sameSecret(req.headers["x-lulu-reopen-token"], reopenToken)) {
          return sendJson(res, 403, { status:"forbidden", message:"仅正式本机启动器可重新打开会话。" });
        }
        const consumed = reopenToken;
        reopenToken = "";
        const nextReopenToken = crypto.randomBytes(32).toString("base64url");
        const nextBootstrapToken = crypto.randomBytes(32).toString("base64url");
        try {
          await writeRuntimeState(runtimeFile, {
            app:"lulu-license-local-admin", instance_id:instanceId, pid:process.pid,
            port:currentPort, started_at:new Date().toISOString(), reopen_token:nextReopenToken,
          });
        } catch (error) {
          reopenToken = consumed;
          throw error;
        }
        reopenToken = nextReopenToken;
        bootstrapToken = nextBootstrapToken;
        return sendJson(res, 200, { status:"ok", bootstrap:nextBootstrapToken });
      }
      if ((url.pathname === "/" || url.pathname === "/admin") && req.method === "GET") {
        const nonce = crypto.randomBytes(18).toString("base64url");
        const csp = `default-src 'none'; script-src 'nonce-${nonce}'; style-src 'nonce-${nonce}'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`;
        if (cookieValue(req, SESSION_COOKIE) === sessionToken) {
          return sendHtml(res, 200, renderAdminPage({ csrfToken, cspNonce:nonce, port:currentPort, ledgerDir }), csp);
        }
        return sendHtml(res, 200, renderBootstrapPage(nonce), csp);
      }
      if (url.pathname === "/admin/bootstrap" && req.method === "POST") {
        const origin = `http://${expectedHost}`;
        const body = await parseBody(req);
        if (req.headers.origin !== origin || !sameSecret(body.bootstrap, bootstrapToken)) {
          return sendJson(res, 403, { status:"forbidden", message:"安全会话入口无效，请重新打开工具。" });
        }
        bootstrapToken = "";
        const cookie = `${SESSION_COOKIE}=${sessionToken}; HttpOnly; SameSite=Strict; Path=/admin`;
        return sendEmpty(res, 204, { "set-cookie":cookie });
      }
      if (!url.pathname.startsWith("/admin/")) return sendJson(res, 404, { status:"not_found", message:"not found" });
      if (cookieValue(req, SESSION_COOKIE) !== sessionToken) return sendJson(res, 403, { status:"forbidden", message:"本机会话无效，请重新打开工具。" });
      if (req.method !== "GET") {
        const origin = `http://${expectedHost}`;
        if (req.headers.origin !== origin || req.headers["x-lulu-local-csrf"] !== csrfToken) return sendJson(res, 403, { status:"forbidden", message:"Origin 或 CSRF 校验失败。" });
      }
      if (url.pathname === "/admin/code" && req.method === "GET") return sendJson(res, 200, { status:"ok", license_code:generateLicenseCode() });
      if (url.pathname === "/admin/licenses" && req.method === "GET") {
        const limit = Math.min(100, Math.max(1, Number(url.searchParams.get("limit") || 50))), offset = Math.max(0, Number(url.searchParams.get("offset") || 0));
        const data = await requestUpstream(`/licenses?limit=${limit}&offset=${offset}`);
        const items = Array.isArray(data.items) ? data.items.map(publicLicense) : [];
        return sendJson(res, 200, { status:"ok", items, limit, offset });
      }
      if (url.pathname === "/admin/licenses" && req.method === "POST") {
        const form = validateCreate(await parseBody(req));
        const created = await requestUpstream("/licenses", { method:"POST", body:form });
        if (!created?.license_id) throw new LocalAdminError("upstream_invalid", "production 创建结果缺少标识。", 502);
        const ledgerPath = await writeLedgerCsv({ ledgerDir, created, form });
        return sendJson(res, 201, { status:"ok", license_id:String(created.license_id), created_at:String(created.created_at || new Date().toISOString()), ledger_path:ledgerPath, plaintext_code_stored_by_service:false });
      }
      const purgePreviewMatch = url.pathname.match(/^\/admin\/licenses\/([^/]+)\/purge-preview$/);
      if (purgePreviewMatch && req.method === "POST") {
        const id = validateLicenseId(decodeURIComponent(purgePreviewMatch[1]));
        const data = await requestUpstream(`/licenses/${encodeURIComponent(id)}/purge-preview`, { method:"POST", body:{} });
        return sendJson(res, 200, publicPurgePreview(data));
      }
      const patchMatch = url.pathname.match(/^\/admin\/licenses\/([^/]+)$/);
      if (patchMatch && req.method === "PATCH") {
        const id = validateLicenseId(decodeURIComponent(patchMatch[1]));
        const data = await requestUpstream(`/licenses/${encodeURIComponent(id)}`, { method:"PATCH", body:validatePatch(await parseBody(req)) });
        return sendJson(res, 200, { status:String(data.status || "ok"), license_id:id });
      }
      if (patchMatch && req.method === "DELETE") {
        const id = validateLicenseId(decodeURIComponent(patchMatch[1]));
        const data = await requestUpstream(`/licenses/${encodeURIComponent(id)}`, { method:"DELETE", body:validatePurge(await parseBody(req)) });
        return sendJson(res, 200, {
          status:String(data.status || "ok"),
          license_id:id,
          purged:data.purged === true,
          replayed:data.replayed === true,
          audit_retained:data.audit_retained === true,
          local_csv_deleted:data.local_csv_deleted === true,
        });
      }
      const resetMatch = url.pathname.match(/^\/admin\/licenses\/([^/]+)\/devices\/reset$/);
      if (resetMatch && req.method === "POST") {
        const id = validateLicenseId(decodeURIComponent(resetMatch[1]));
        const data = await requestUpstream(`/licenses/${encodeURIComponent(id)}/devices/reset`, { method:"POST", body:{} });
        return sendJson(res, 200, { status:String(data.status || "ok"), license_id:id });
      }
      if (url.pathname === "/admin/open-ledger" && req.method === "POST") { await ensureLedgerDirectory(ledgerDir); spawn("open", [ledgerDir], { detached:true, stdio:"ignore" }).unref(); return sendJson(res, 200, { status:"ok" }); }
      if (url.pathname === "/admin/shutdown" && req.method === "POST") { sendJson(res, 200, { status:"ok" }); setTimeout(() => stop(), 25); return; }
      return sendJson(res, 404, { status:"not_found", message:"not found" });
    } catch (error) {
      const safe = error instanceof LocalAdminError ? error : new LocalAdminError("server_error", "本机管理工具异常，请关闭后重试。", 500);
      return sendJson(res, safe.httpStatus, { status:safe.code, message:safe.message });
    }
  });
  const connections = new Set();
  server.on("connection", socket => {
    connections.add(socket);
    socket.once("close", () => connections.delete(socket));
  });

  for (const port of ports) {
    try { await listen(server, port); currentPort = port; break; }
    catch (error) { if (error?.code !== "EADDRINUSE") throw error; }
  }
  if (!currentPort) throw new LocalAdminError("no_port", "本机管理端口均被占用，请关闭冲突程序后重试。", 503);
  await writeRuntimeState(runtimeFile, {
    app:"lulu-license-local-admin", instance_id:instanceId, pid:process.pid,
    port:currentPort, started_at:new Date().toISOString(), reopen_token:reopenToken,
  });
  const localUrl = `http://${HOST}:${currentPort}/admin#bootstrap=${encodeURIComponent(bootstrapToken)}`;
  if (openBrowser) spawn("open", [localUrl], { detached:true, stdio:"ignore" }).unref();

  async function stop() {
    if (stopping) return closed; stopping = true;
    const forceCloseTimer = setTimeout(() => {
      for (const socket of connections) socket.destroy();
    }, 750);
    forceCloseTimer.unref?.();
    const serverClosed = new Promise(resolve => server.close(() => resolve()));
    server.closeIdleConnections?.();
    await serverClosed;
    clearTimeout(forceCloseTimer);
    try { const state = JSON.parse(await readFile(runtimeFile, "utf8")); if (state.instance_id === instanceId) await rm(runtimeFile, { force:true }); } catch { /* Stale/missing runtime state is harmless. */ }
    resolveClosed(); return closed;
  }
  return { server, port:currentPort, url:localUrl, instanceId, runtimeFile, ledgerDir, stop, closed };
}

export async function runtimeFileMode(runtimeFile = DEFAULT_RUNTIME_FILE) { try { return (await stat(runtimeFile)).mode & 0o777; } catch { return null; } }
