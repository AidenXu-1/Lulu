#!/usr/bin/env node
/*
 * Lulu license admin helper.
 *
 * Source artifact only. It never contains real endpoint, admin token, or
 * license code values. Provide them through environment variables or hidden
 * prompts when a separately approved staging/production operation is executed.
 */

import { readFile, stat } from "node:fs/promises";

const endpoint = (process.env.LULU_LICENSE_ADMIN_ENDPOINT || "").replace(/\/+$/, "");
const adminToken = process.env.LULU_LICENSE_ADMIN_TOKEN || "";

function usage() {
  console.error(`Usage:
  LULU_LICENSE_ADMIN_ENDPOINT=https://... \\
  LULU_LICENSE_ADMIN_TOKEN=... \\
  node tools/admin.mjs create [--code-stdin | --code-file /path/to/0600-code-file] [--max-devices 2] [--expires-at ISO] [--note text]

  node tools/admin.mjs list [--limit 50] [--offset 0]
  node tools/admin.mjs patch --license-id <id> [--status active|revoked|expired|suspended] [--expires-at ISO|null] [--max-devices n]
  node tools/admin.mjs reset-device --license-id <id> [--device-id <id>]

Rules:
  - Do not paste real tokens or real authorization codes into repo files.
  - Do not pass plaintext license codes as CLI arguments.
  - For manual issue-code creation, omit code flags and enter the code in the hidden prompt.
  - For automation, use --code-stdin from a secret manager or --code-file with owner-only permissions.
  - Do not print plaintext license codes into logs or reports.
  - Use staging endpoint/secrets before production.
`);
  process.exit(2);
}

function argsToObject(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i += 1) {
    const key = argv[i];
    if (!key.startsWith("--")) continue;
    const next = argv[i + 1];
    out[key.slice(2)] = next && !next.startsWith("--") ? next : true;
    if (next && !next.startsWith("--")) i += 1;
  }
  return out;
}

async function request(path, { method = "GET", body = null } = {}) {
  if (!endpoint || !adminToken) usage();
  const headers = {
    "content-type": "application/json; charset=utf-8",
    "x-admin-token": adminToken,
  };
  if (method !== "GET") {
    headers.origin = new URL(endpoint).origin;
    headers["x-lulu-admin-csrf"] = "same-origin";
  }
  const response = await fetch(`${endpoint}${path}`, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(`request failed: ${response.status} ${data.status || "unknown"}`);
  }
  return data;
}

async function readStdinText() {
  const chunks = [];
  for await (const chunk of process.stdin) {
    chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
  }
  return Buffer.concat(chunks).toString("utf8");
}

async function readCodeFile(filePath) {
  if (!filePath || filePath === true) {
    throw new Error("Missing --code-file path.");
  }
  const fileStat = await stat(filePath);
  if (!fileStat.isFile()) {
    throw new Error("License code file must be a regular file.");
  }
  if ((fileStat.mode & 0o077) !== 0) {
    throw new Error("License code file permissions are too broad; use owner-only permissions such as chmod 600.");
  }
  return readFile(filePath, "utf8");
}

function readHiddenLine(promptText) {
  if (!process.stdin.isTTY || !process.stdout.isTTY || typeof process.stdin.setRawMode !== "function") {
    throw new Error("Interactive hidden input requires a TTY. Use --code-stdin from a secret manager or --code-file with owner-only permissions.");
  }
  return new Promise((resolve, reject) => {
    let value = "";
    const onData = (chunk) => {
      const text = chunk.toString("utf8");
      for (const char of text) {
        if (char === "\u0003") {
          cleanup();
          reject(new Error("License code input cancelled."));
          return;
        }
        if (char === "\r" || char === "\n") {
          cleanup();
          process.stdout.write("\n");
          resolve(value);
          return;
        }
        if (char === "\u007f" || char === "\b") {
          value = value.slice(0, -1);
          continue;
        }
        value += char;
      }
    };
    const cleanup = () => {
      process.stdin.off("data", onData);
      process.stdin.setRawMode(false);
      process.stdin.pause();
    };
    process.stdout.write(promptText);
    process.stdin.setEncoding("utf8");
    process.stdin.setRawMode(true);
    process.stdin.resume();
    process.stdin.on("data", onData);
  });
}

async function main() {
  const [command, ...rest] = process.argv.slice(2);
  const opts = argsToObject(rest);
  if (!command) usage();
  if (command === "create") {
    if (opts.code) {
      throw new Error("Refusing --code because CLI arguments can leak plaintext license codes. Use the hidden prompt, --code-stdin from a secret manager, or --code-file with owner-only permissions.");
    }
    if (opts["code-stdin"] && opts["code-file"]) {
      throw new Error("Choose only one license code input method.");
    }
    let licenseCode = "";
    if (opts["code-stdin"]) {
      licenseCode = (await readStdinText()).trim();
    } else if (opts["code-file"]) {
      licenseCode = (await readCodeFile(opts["code-file"])).trim();
    } else {
      licenseCode = (await readHiddenLine("License code (hidden): ")).trim();
    }
    if (!licenseCode) {
      throw new Error("No license code received.");
    }
    const data = await request("/admin/licenses", {
      method: "POST",
      body: {
        license_code: licenseCode,
        max_devices: opts["max-devices"] ? Number(opts["max-devices"]) : 2,
        expires_at: opts["expires-at"] || null,
        note: opts.note || "",
      },
    });
    console.log(JSON.stringify({ ...data, plaintext_code_stored: false }, null, 2));
    return;
  }
  if (command === "list") {
    const limit = Number(opts.limit || 50);
    const offset = Number(opts.offset || 0);
    console.log(JSON.stringify(await request(`/admin/licenses?limit=${limit}&offset=${offset}`), null, 2));
    return;
  }
  if (command === "patch") {
    if (!opts["license-id"]) usage();
    const body = {};
    if (opts.status) body.status = opts.status;
    if (Object.prototype.hasOwnProperty.call(opts, "expires-at")) {
      body.expires_at = opts["expires-at"] === "null" ? null : opts["expires-at"];
    }
    if (opts["max-devices"]) body.max_devices = Number(opts["max-devices"]);
    console.log(JSON.stringify(await request(`/admin/licenses/${opts["license-id"]}`, { method: "PATCH", body }), null, 2));
    return;
  }
  if (command === "reset-device") {
    if (!opts["license-id"]) usage();
    const body = opts["device-id"] ? { device_id: opts["device-id"] } : {};
    console.log(JSON.stringify(await request(`/admin/licenses/${opts["license-id"]}/devices/reset`, { method: "POST", body }), null, 2));
    return;
  }
  usage();
}

main().catch((error) => {
  console.error(error.message);
  process.exit(1);
});
