#!/usr/bin/env node
import { spawn } from "node:child_process";
import { lstat, readFile, rm } from "node:fs/promises";
import { pathToFileURL } from "node:url";

import { DEFAULT_RUNTIME_FILE, startLocalAdmin } from "./server.mjs";

export async function findRunningInstance(runtimeFile = DEFAULT_RUNTIME_FILE, fetchImpl = globalThis.fetch) {
  let state;
  try {
    const info = await lstat(runtimeFile);
    const uidOk = typeof process.getuid !== "function" || info.uid === process.getuid();
    if (info.isSymbolicLink() || !info.isFile() || !uidOk || (info.mode & 0o777) !== 0o600 || info.size > 16 * 1024) return null;
    state = JSON.parse(await readFile(runtimeFile, "utf8"));
  } catch { return null; }
  const port = Number(state.port);
  if (!Number.isInteger(port) || port < 1 || state.app !== "lulu-license-local-admin") return null;
  try {
    const response = await fetchImpl(`http://127.0.0.1:${port}/__health`, { headers:{ accept:"application/json" }, signal:AbortSignal.timeout(1200) });
    const data = await response.json();
    if (!response.ok || data.app !== "lulu-license-local-admin" || data.instance_id !== state.instance_id) return null;
    const reopenToken = String(state.reopen_token || "");
    if (!/^[A-Za-z0-9_-]{43}$/.test(reopenToken)) return { url:`http://127.0.0.1:${port}/admin`, port };
    const reopened = await fetchImpl(`http://127.0.0.1:${port}/__reopen`, {
      method:"POST",
      headers:{ accept:"application/json", "x-lulu-reopen-token":reopenToken },
      signal:AbortSignal.timeout(1200),
    });
    const reopenedData = await reopened.json();
    const bootstrap = String(reopenedData.bootstrap || "");
    if (!reopened.ok || reopenedData.status !== "ok" || !/^[A-Za-z0-9_-]{43}$/.test(bootstrap)) return null;
    return { url:`http://127.0.0.1:${port}/admin#bootstrap=${encodeURIComponent(bootstrap)}`, port };
  } catch { /* Stale state. */ }
  return null;
}

function openBrowser(url) { spawn("open", [url], { detached:true, stdio:"ignore" }).unref(); }

export async function main({ runtimeFile = DEFAULT_RUNTIME_FILE, startOptions = { openBrowser:true } } = {}) {
  const running = await findRunningInstance(runtimeFile);
  if (running) { openBrowser(running.url); process.stdout.write("Lulu 授权本机管理工具已在运行，已打开现有页面。\n"); return; }
  await rm(runtimeFile, { force:true }).catch(() => {});
  let handle;
  try { handle = await startLocalAdmin({ ...startOptions, runtimeFile }); }
  catch (error) { process.stderr.write(`启动失败：${error?.message || "请检查本机 Keychain 配置。"}\n`); process.exitCode = 1; return; }
  process.stdout.write(`Lulu 授权本机管理工具已启动，仅监听 127.0.0.1:${handle.port}。\n请在网页中点击“关闭管理工具”结束会话。\n`);
  let stopPromise = null;
  const stop = () => {
    if (!stopPromise) {
      stopPromise = handle.stop().then(
        () => { process.exitCode = 0; },
        () => { process.stderr.write("关闭失败：本机服务未能正常结束。\n"); process.exitCode = 1; },
      );
    }
    return stopPromise;
  };
  const signalHandlers = new Map();
  for (const signal of ["SIGINT", "SIGTERM", "SIGHUP"]) {
    const handler = () => { void stop(); };
    signalHandlers.set(signal, handler);
    process.once(signal, handler);
  }
  try { await handle.closed; }
  finally {
    for (const [signal, handler] of signalHandlers) process.off(signal, handler);
  }
  if (stopPromise) await stopPromise;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) main();
