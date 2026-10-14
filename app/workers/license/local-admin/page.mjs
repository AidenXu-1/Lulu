import { readFileSync } from "node:fs";

let LOGO_PNG_BASE64 = "";
try { LOGO_PNG_BASE64 = readFileSync(new URL("./logo-64.png", import.meta.url)).toString("base64"); }
catch { /* Logo failure must not block authorization management. */ }

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

export function renderAdminPage({ csrfToken, cspNonce, port, ledgerDir }) {
  const config = JSON.stringify({ csrfToken, logoPngBase64:LOGO_PNG_BASE64 }).replaceAll("<", "\\u003c");
  return `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Lulu 授权 · 本机管理</title>
  <style nonce="${cspNonce}">
    :root {
      color-scheme:light; --paper:#fffefa; --white:#fff; --ink:#171717; --muted:#706d66;
      --line:#deddd7; --line-soft:#efeee9; --yellow:#ffc51b; --yellow-soft:#fff7d8;
      --green:#138357; --green-soft:#eaf8f1; --blue:#2f6fd0; --blue-soft:#edf4ff;
      --red:#c3493e; --red-soft:#fff0ed; --amber:#99610b; --amber-soft:#fff4df;
      font:14px/1.48 -apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang SC","Hiragino Sans GB",sans-serif;
    }
    * { box-sizing:border-box; }
    html { background:var(--paper); }
    body { margin:0; min-width:320px; background:var(--paper); color:var(--ink); }
    button,input,textarea,select { font:inherit; }
    button { cursor:pointer; }
    button:disabled { cursor:not-allowed; opacity:.52; }
    :focus-visible { outline:3px solid rgba(47,111,208,.42); outline-offset:2px; }
    .page { min-height:100vh; overflow-x:hidden; }
    .brand-band { border-bottom:1px solid var(--line); background:var(--white); }
    .brand-band::before { content:""; display:block; height:6px; background:var(--yellow); }
    .brand-inner { max-width:1376px; min-height:118px; margin:auto; padding:18px 32px; display:flex; align-items:center; justify-content:space-between; gap:24px; }
    .brand-lockup { display:flex; align-items:center; gap:16px; min-width:0; }
    .brand-logo { width:72px; height:72px; flex:0 0 auto; border-radius:16px; background:var(--yellow-soft); box-shadow:0 8px 20px rgba(203,145,0,.18); image-rendering:auto; }
    .brand-copy { min-width:0; }
    .eyebrow,.section-kicker { margin:0 0 3px; color:#8b6900; font-size:11px; font-weight:850; letter-spacing:.04em; }
    h1 { margin:0; font-size:30px; line-height:1.05; }
    .brand-copy p { margin:7px 0 0; color:var(--muted); font-size:13px; }
    .top-status { display:flex; align-items:center; justify-content:flex-end; gap:10px; flex-wrap:wrap; }
    .status-item { display:inline-flex; align-items:center; gap:7px; color:#4f4d48; font-size:12px; font-weight:760; white-space:nowrap; }
    .dot { width:8px; height:8px; border-radius:50%; background:var(--green); box-shadow:0 0 0 4px var(--green-soft); }
    .dot.blue { background:var(--blue); box-shadow:0 0 0 4px var(--blue-soft); }
    .top-divider { width:1px; height:20px; background:var(--line); }
    .icon-btn { width:38px; height:38px; padding:0; border:1px solid var(--line); border-radius:50%; background:var(--white); color:var(--ink); display:inline-grid; place-items:center; font-size:18px; font-weight:850; }
    .icon-btn.danger:hover { border-color:#e1aaa4; background:var(--red-soft); color:var(--red); }
    .summary-band { background:var(--ink); color:var(--white); }
    .summary-inner { max-width:1376px; min-height:82px; margin:auto; padding:15px 32px; display:grid; grid-template-columns:minmax(250px,1fr) repeat(4,minmax(100px,auto)); align-items:center; gap:26px; }
    .summary-lead strong { display:block; font-size:16px; }
    .summary-lead span { display:block; margin-top:4px; color:#aaa9a5; font-size:11px; }
    .metric { padding-left:20px; border-left:1px solid #454545; }
    .metric b { display:block; font-size:24px; line-height:1; }
    .metric span { display:block; margin-top:5px; color:#aaa9a5; font-size:11px; }
    .metric .green { color:#56d39e; } .metric .yellow { color:#ffd861; }
    .notice-wrap { max-width:1376px; margin:0 auto; padding:12px 32px 0; }
    .notice { min-height:36px; padding:8px 12px; border-left:3px solid var(--green); background:var(--green-soft); color:#245f46; font-size:12px; font-weight:720; }
    .notice[data-kind="loading"] { border-left-color:var(--amber); background:var(--amber-soft); color:#79500e; }
    .notice[data-kind="error"] { border-left-color:var(--red); background:var(--red-soft); color:#9d382f; }
    .workspace { max-width:1376px; margin:auto; padding:24px 32px 38px; display:grid; grid-template-columns:420px minmax(0,1fr); align-items:start; }
    .create-pane { padding-right:30px; }
    .directory-pane { min-width:0; padding-left:34px; border-left:1px solid var(--line); }
    .section-title-row { display:flex; align-items:flex-start; justify-content:space-between; gap:16px; }
    h2 { margin:0; font-size:25px; line-height:1.15; }
    .section-intro { margin:7px 0 18px; color:var(--muted); font-size:12px; }
    .one-time { display:none; margin:0 0 20px; border:1px solid #e7bd34; background:var(--yellow-soft); box-shadow:0 14px 36px rgba(35,30,18,.08); }
    .success-head { display:flex; align-items:center; justify-content:space-between; gap:12px; padding:13px 15px 10px; }
    .once { color:#875f00; font-size:10px; font-weight:850; }
    .plain-code { display:block; margin:0 15px; padding:13px 14px; background:var(--ink); color:var(--white); font:760 13px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace; overflow-wrap:anywhere; }
    .success-note { margin:9px 15px 0; color:#705b20; font-size:10px; }
    .success-path { margin:6px 15px 0; color:#705b20; font-size:10px; overflow-wrap:anywhere; }
    .success-actions { display:grid; grid-template-columns:1fr 1fr auto; gap:7px; padding:12px 15px 15px; }
    .field-group { padding:17px 0 0; margin-top:16px; border-top:1px solid var(--line-soft); }
    .group-name { display:block; margin-bottom:10px; color:#4e4b45; font-size:10px; font-weight:850; }
    label { display:block; margin:11px 0 5px; color:#4f4c47; font-size:11px; font-weight:780; }
    input,textarea,select { width:100%; min-width:0; min-height:42px; padding:9px 11px; border:1px solid #cbc9c2; border-radius:4px; background:var(--white); color:var(--ink); }
    textarea { min-height:66px; resize:vertical; }
    .input-action { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:8px; }
    .two { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
    .helper { margin:6px 0 0; color:#85817a; font-size:10px; }
    .btn { min-height:40px; padding:9px 14px; border:1px solid transparent; border-radius:4px; font-weight:820; white-space:nowrap; }
    .btn-primary { background:var(--yellow); border-color:var(--yellow); color:#17130a; }
    .btn-dark { background:var(--ink); border-color:var(--ink); color:var(--white); }
    .btn-line { background:var(--white); border-color:#c9c7c0; color:var(--ink); }
    .btn-quiet { background:transparent; border-color:transparent; color:#69655e; }
    .btn-danger { background:var(--red-soft); border-color:#edc0ba; color:#a83329; }
    .create-submit { width:100%; min-height:48px; margin-top:18px; font-size:14px; }
    .directory-meta { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
    .sync-pill { display:inline-flex; align-items:center; gap:6px; min-height:25px; padding:4px 9px; border-radius:999px; background:var(--green-soft); color:var(--green); font-size:10px; font-weight:820; }
    .sync-pill::before { content:""; width:6px; height:6px; border-radius:50%; background:currentColor; }
    .directory-tools { display:grid; grid-template-columns:minmax(180px,1fr) 126px auto; gap:8px; margin:17px 0 12px; }
    .record-list { border-top:1px solid var(--ink); }
    .record { border-bottom:1px solid var(--line); }
    .record-summary { display:grid; grid-template-columns:48px minmax(150px,1.25fr) minmax(98px,.7fr) minmax(125px,.75fr) 38px; gap:14px; align-items:center; padding:17px 4px; }
    .avatar { width:42px; height:42px; border-radius:50%; display:grid; place-items:center; background:var(--yellow); color:#1c1608; font-size:14px; font-weight:880; }
    .person { min-width:0; }
    .person strong { display:block; font-size:14px; }
    .contact { margin-top:3px; color:var(--muted); font-size:10px; overflow-wrap:anywhere; }
    .license-id { margin-top:3px; color:#9a978f; font:9px/1.35 ui-monospace,SFMono-Regular,Menlo,monospace; overflow-wrap:anywhere; }
    .record-label { display:block; margin-bottom:4px; color:#96928a; font-size:9px; font-weight:780; }
    .record-value { font-size:11px; font-weight:760; }
    .state-pill { display:inline-flex; align-items:center; gap:6px; min-height:24px; padding:3px 8px; border-radius:999px; background:var(--green-soft); color:var(--green); font-size:10px; font-weight:820; }
    .state-pill::before { content:""; width:6px; height:6px; border-radius:50%; background:currentColor; }
    .state-pill.suspended,.state-pill.revoked { background:#f0efec; color:#69665f; }
    .state-pill.expired { background:var(--amber-soft); color:var(--amber); }
    .toggle { width:34px; height:34px; padding:0; border:0; border-radius:50%; background:transparent; color:#5e5a53; font-size:19px; }
    .expanded { margin:0 0 14px; padding:17px 18px; border-left:4px solid var(--yellow); background:#faf9f5; }
    .edit-grid { display:grid; grid-template-columns:1fr 1fr 90px 1fr; gap:9px; }
    .edit-grid label { margin-top:0; }
    .wide { grid-column:span 2; }
    .edit-actions { display:flex; align-items:center; justify-content:space-between; gap:10px; margin-top:14px; flex-wrap:wrap; }
    .edit-actions .right { display:flex; gap:8px; flex-wrap:wrap; }
    .empty { padding:32px 18px; text-align:center; color:var(--muted); border-bottom:1px solid var(--line); }
    .loading-row { height:84px; position:relative; overflow:hidden; border-bottom:1px solid var(--line); }
    .loading-row::after { content:""; position:absolute; top:37px; left:0; width:42%; height:8px; background:var(--yellow); animation:pulse 1s ease-in-out infinite alternate; }
    @keyframes pulse { to { opacity:.35; transform:translateX(110%); } }
    .directory-footer { display:flex; justify-content:space-between; gap:16px; align-items:flex-start; margin-top:16px; padding-top:13px; border-top:1px solid var(--line-soft); color:var(--muted); font-size:10px; }
    .directory-footer span { overflow-wrap:anywhere; }
    @media(max-width:1024px) {
      .brand-inner { align-items:flex-start; }
      .summary-inner { grid-template-columns:1fr repeat(4,minmax(80px,auto)); gap:15px; }
      .workspace { grid-template-columns:360px minmax(0,1fr); padding-inline:22px; }
      .notice-wrap { padding-inline:22px; }
      .create-pane { padding-right:22px; } .directory-pane { padding-left:24px; }
      .record-summary { grid-template-columns:42px minmax(120px,1fr) 96px 38px; }
      .record-summary > :nth-child(4) { display:none; }
    }
    @media(max-width:680px) {
      .brand-band::before { height:5px; }
      .brand-inner { min-height:0; padding:14px 14px 13px; display:block; }
      .brand-logo { width:58px; height:58px; border-radius:13px; }
      .brand-lockup { gap:11px; }
      h1 { font-size:23px; }
      .brand-copy p { margin-top:4px; font-size:10px; }
      .top-status { margin-top:13px; justify-content:flex-start; gap:9px; }
      .top-status .status-item:nth-child(3) { width:calc(100% - 96px); }
      .top-divider { display:none; }
      .summary-inner { min-height:0; padding:14px; grid-template-columns:1fr 1fr; gap:13px 10px; }
      .summary-lead { grid-column:1/-1; }
      .metric { padding-left:10px; }
      .metric b { font-size:21px; }
      .notice-wrap { padding:10px 14px 0; }
      .workspace { display:block; padding:21px 14px 28px; }
      .create-pane { padding:0 0 27px; }
      .directory-pane { padding:26px 0 0; border-left:0; border-top:1px solid var(--line); }
      h2 { font-size:22px; }
      .success-actions { grid-template-columns:1fr 1fr; }
      .success-actions .btn-quiet { grid-column:1/-1; }
      .two { grid-template-columns:1fr; gap:0; }
      .directory-tools { grid-template-columns:minmax(0,1fr) 106px auto; }
      .directory-tools .btn { padding-inline:10px; }
      .record-summary { grid-template-columns:42px minmax(0,1fr) 34px; gap:9px; padding:15px 0; }
      .record-summary > :nth-child(3),.record-summary > :nth-child(4) { display:none; }
      .expanded { padding:14px 12px; }
      .edit-grid { grid-template-columns:1fr 1fr; }
      .wide { grid-column:span 2; }
      .edit-actions { align-items:flex-start; }
      .edit-actions .right { width:100%; }
      .edit-actions .right .btn { flex:1; padding-inline:7px; }
      .directory-footer { display:block; }
    }
    /* v3: directory-first consumer workbench. Backend and security contracts stay unchanged. */
    body { background:#f5f5f3; }
    .page { min-height:100vh; background:
      radial-gradient(circle at 88% 0,rgba(255,197,27,.11),transparent 24rem),
      #f5f5f3; }
    .brand-band { position:relative; z-index:20; background:rgba(255,255,255,.94); border-bottom:1px solid #e6e5e1; backdrop-filter:blur(18px); }
    .brand-band::before { height:4px; }
    .brand-inner { max-width:1280px; min-height:76px; padding:13px 24px; }
    .brand-lockup { gap:12px; }
    .brand-logo { width:48px; height:48px; border-radius:14px; box-shadow:0 7px 18px rgba(141,95,0,.14); }
    .brand-copy .eyebrow { margin-bottom:1px; font-size:10px; }
    .brand-copy h1 { font-size:20px; letter-spacing:-.02em; }
    .brand-copy p { margin-top:3px; font-size:12px; }
    .top-status { gap:8px; }
    .status-item { min-height:30px; padding:5px 10px; border:1px solid #e3e2dd; border-radius:999px; background:#fafaf8; color:#55524c; font-size:12px; font-weight:720; }
    .status-item.safe { border-color:#cfe9db; background:var(--green-soft); color:#237052; }
    .status-item.production { border-color:#ead7a2; background:var(--yellow-soft); color:#805e08; }
    .top-divider { display:none; }
    .icon-btn { width:40px; height:40px; border-radius:12px; font-size:17px; transition:background .18s ease,border-color .18s ease,transform .18s ease; }
    .icon-btn:hover { border-color:#bbb8af; background:#f7f6f2; }
    .icon-btn:active { transform:scale(.96); }
    .notice-wrap { max-width:1280px; padding:12px 24px 0; }
    .notice { min-height:42px; display:flex; align-items:center; gap:9px; padding:9px 13px; border:1px solid #d3e8dc; border-left-width:4px; border-radius:11px; font-size:13px; font-weight:650; }
    .notice::before { content:"✓"; display:grid; width:20px; height:20px; flex:0 0 auto; place-items:center; border-radius:50%; background:rgba(19,131,87,.11); font-size:11px; }
    .notice[data-kind="loading"] { border-color:#efd39e; }
    .notice[data-kind="loading"]::before { content:"↻"; background:rgba(153,97,11,.1); }
    .notice[data-kind="error"] { border-color:#edc3bd; }
    .notice[data-kind="error"]::before { content:"!"; background:rgba(195,73,62,.1); }
    .workspace { max-width:1280px; display:block; padding:24px 24px 44px; }
    .workspace-head { display:flex; align-items:flex-end; justify-content:space-between; gap:24px; margin-bottom:18px; }
    .workspace-head .section-kicker { margin-bottom:6px; }
    .workspace-head h2 { font-size:30px; letter-spacing:-.035em; }
    .workspace-head .section-intro { max-width:620px; margin:7px 0 0; font-size:13px; }
    .new-license-btn { min-height:46px; padding-inline:19px; border-radius:12px; box-shadow:0 8px 20px rgba(179,127,0,.18); }
    .summary-band { max-width:1280px; margin:0 auto 18px; padding:0 24px; background:transparent; color:var(--ink); }
    .summary-inner { min-height:0; padding:0; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; }
    .summary-lead { display:none; }
    .metric { min-height:92px; padding:17px 18px; border:1px solid #e1e0db; border-radius:14px; background:rgba(255,255,255,.88); box-shadow:0 7px 20px rgba(28,25,19,.035); }
    .metric b { font-size:27px; letter-spacing:-.035em; }
    .metric span { margin-top:8px; color:#77736c; font-size:12px; font-weight:680; }
    .metric .green { color:var(--green); }
    .metric .yellow { color:#9a6c00; }
    .directory-pane { min-width:0; padding:0; overflow:hidden; border:1px solid #deddd8; border-radius:16px; background:rgba(255,255,255,.94); box-shadow:0 18px 46px rgba(27,24,18,.065); }
    .directory-header { display:flex; align-items:flex-start; justify-content:space-between; gap:18px; padding:20px 22px 16px; border-bottom:1px solid var(--line-soft); }
    .directory-header h2 { font-size:21px; letter-spacing:-.02em; }
    .directory-header .section-intro { margin:5px 0 0; font-size:12px; }
    .directory-meta { justify-content:flex-end; }
    .sync-pill { min-height:28px; padding:5px 10px; font-size:11px; }
    .directory-tools { grid-template-columns:minmax(260px,1fr) 150px auto; gap:10px; margin:0; padding:14px 22px; border-bottom:1px solid var(--line-soft); background:#fbfbf9; }
    input,textarea,select { min-height:44px; padding:10px 12px; border-color:#ceccc5; border-radius:10px; transition:border-color .16s ease,box-shadow .16s ease,background .16s ease; }
    input:hover,textarea:hover,select:hover { border-color:#aaa79f; }
    input:focus,textarea:focus,select:focus { border-color:#b27f08; box-shadow:0 0 0 3px rgba(255,197,27,.2); }
    textarea { min-height:88px; }
    label { margin:13px 0 6px; font-size:12px; font-weight:760; }
    .record-list { border-top:0; }
    .record-list-head { display:grid; grid-template-columns:minmax(210px,1.45fr) minmax(170px,1.25fr) 116px 112px 122px 88px; gap:16px; padding:10px 22px; border-bottom:1px solid var(--line-soft); color:#8a867e; font-size:11px; font-weight:750; }
    .record { border-bottom:1px solid var(--line-soft); background:#fff; transition:background .15s ease; }
    .record:last-child { border-bottom:0; }
    .record:hover { background:#fdfcf8; }
    .record-summary { grid-template-columns:minmax(210px,1.45fr) minmax(170px,1.25fr) 116px 112px 122px 88px; gap:16px; min-height:76px; padding:13px 22px; }
    .identity-cell { display:flex; align-items:center; gap:11px; min-width:0; }
    .avatar { width:40px; height:40px; flex:0 0 auto; border-radius:12px; font-size:14px; }
    .person strong { overflow:hidden; font-size:14px; text-overflow:ellipsis; white-space:nowrap; }
    .license-id { margin-top:3px; font-size:10px; }
    .contact { margin-top:0; color:#4f4c46; font-size:12px; line-height:1.35; }
    .batch { margin-top:4px; overflow:hidden; color:#8a867e; font-size:11px; text-overflow:ellipsis; white-space:nowrap; }
    .record-label { display:none; }
    .record-value { font-size:12px; font-weight:720; }
    .state-pill { min-height:28px; padding:4px 9px; font-size:11px; }
    .manage-btn { min-height:36px; padding:7px 12px; border:1px solid #d1cfc8; border-radius:9px; background:#fff; color:#37342f; font-size:12px; font-weight:760; }
    .manage-btn:hover { border-color:#b38b27; background:var(--yellow-soft); }
    .directory-footer { margin:0; padding:13px 22px; border-top:1px solid var(--line-soft); background:#fbfbf9; font-size:11px; }
    .empty { padding:58px 22px; border-bottom:0; font-size:13px; }
    .loading-row { height:76px; margin:0 22px; }
    .loading-row::after { top:34px; height:7px; border-radius:999px; }
    .page-overlay { position:fixed; inset:0; z-index:39; border:0; background:rgba(20,19,17,.34); opacity:0; pointer-events:none; backdrop-filter:blur(2px); transition:opacity .2s ease; }
    .page-overlay.visible { opacity:1; pointer-events:auto; }
    body.panel-open { overflow:hidden; }
    .create-pane { position:fixed; top:0; right:0; z-index:40; width:min(540px,94vw); height:100vh; padding:0 24px 32px; overflow:auto; border-left:1px solid #dedbd2; background:#fffefa; box-shadow:-24px 0 70px rgba(25,22,17,.16); transform:translateX(104%); transition:transform .24s cubic-bezier(.22,.8,.24,1); }
    .create-pane.is-open { transform:translateX(0); }
    .panel-head { position:sticky; top:0; z-index:2; display:flex; align-items:flex-start; justify-content:space-between; gap:18px; margin:0 -24px 8px; padding:20px 24px 16px; border-bottom:1px solid var(--line-soft); background:rgba(255,254,250,.96); backdrop-filter:blur(14px); }
    .panel-head h2 { font-size:22px; }
    .panel-head .section-intro { margin:5px 0 0; font-size:12px; }
    .panel-close { flex:0 0 auto; }
    .one-time { border-radius:14px; overflow:hidden; }
    .success-head { padding:15px 16px 11px; }
    .once { min-height:25px; display:inline-flex; align-items:center; padding:4px 8px; border:1px solid #e7ca70; border-radius:999px; font-size:11px; }
    .plain-code { margin:0 16px; border-radius:9px; font-size:14px; }
    .success-note,.success-path { margin-inline:16px; font-size:11px; }
    .success-actions { padding:13px 16px 16px; }
    .field-group { padding-top:18px; margin-top:18px; }
    .group-name { font-size:11px; }
    .btn { min-height:42px; border-radius:10px; }
    .btn-primary { box-shadow:0 6px 16px rgba(179,127,0,.13); }
    .create-submit { min-height:48px; border-radius:12px; }
    .helper { font-size:11px; line-height:1.5; }
    .expanded { position:fixed; top:0; right:0; z-index:40; width:min(560px,94vw); height:100vh; margin:0; padding:0 24px 32px; overflow:auto; border:0; border-left:1px solid #dedbd2; background:#fffefa; box-shadow:-24px 0 70px rgba(25,22,17,.16); }
    .drawer-summary { display:grid; grid-template-columns:1fr 1fr; gap:10px; margin:16px 0 18px; }
    .drawer-stat { padding:12px 13px; border:1px solid var(--line-soft); border-radius:11px; background:#faf9f5; }
    .drawer-stat span { display:block; color:#88847d; font-size:11px; }
    .drawer-stat strong { display:block; margin-top:4px; font-size:13px; }
    .edit-grid { grid-template-columns:1fr 1fr; gap:2px 12px; }
    .edit-grid .wide { grid-column:1/-1; }
    .danger-zone { margin-top:22px; padding:16px; border:1px solid #edc0ba; border-radius:13px; background:var(--red-soft); }
    .danger-zone strong { display:block; color:#8f2f27; font-size:13px; }
    .danger-zone p { margin:6px 0 12px; color:#855650; font-size:11px; line-height:1.55; }
    .edit-actions { position:sticky; bottom:-32px; margin:22px -24px -32px; padding:14px 24px 18px; border-top:1px solid var(--line-soft); background:rgba(255,254,250,.96); backdrop-filter:blur(14px); }
    .edit-actions .right { gap:8px; }
    .confirm-layer { position:fixed; inset:0; z-index:80; display:none; place-items:center; padding:18px; background:rgba(20,19,17,.44); backdrop-filter:blur(3px); }
    .confirm-layer.is-open { display:grid; }
    .confirm-card { width:min(430px,100%); padding:22px; border:1px solid #dedbd2; border-radius:16px; background:#fffefa; box-shadow:0 28px 80px rgba(20,18,14,.25); }
    .confirm-icon { display:grid; width:40px; height:40px; margin-bottom:14px; place-items:center; border-radius:12px; background:var(--amber-soft); color:var(--amber); font-size:19px; font-weight:850; }
    .confirm-card.danger .confirm-icon { background:var(--red-soft); color:var(--red); }
    .confirm-card h2 { font-size:20px; }
    .confirm-card p { margin:8px 0 20px; color:var(--muted); font-size:13px; line-height:1.6; }
    .confirm-actions { display:flex; justify-content:flex-end; gap:9px; }
    .purge-layer { position:fixed; inset:0; z-index:90; display:none; place-items:center; padding:18px; background:rgba(20,19,17,.56); backdrop-filter:blur(4px); }
    .purge-layer.is-open { display:grid; }
    .purge-card { width:min(520px,100%); max-height:calc(100vh - 36px); padding:22px; overflow:auto; border:1px solid #dfaea8; border-radius:17px; background:#fffefa; box-shadow:0 30px 90px rgba(20,18,14,.3); }
    .purge-card h2 { margin-top:0; font-size:22px; }
    .purge-lead { margin:7px 0 15px; color:#6e625d; font-size:13px; line-height:1.6; }
    .purge-target { padding:13px 14px; border:1px solid #e8ddd7; border-radius:11px; background:#faf8f5; }
    .purge-target strong { display:block; font-size:14px; }
    .purge-target code { display:block; margin-top:4px; color:#6f6a63; font-size:11px; overflow-wrap:anywhere; }
    .purge-counts { display:grid; grid-template-columns:repeat(3,1fr); gap:8px; margin:12px 0; }
    .purge-count { padding:11px; border:1px solid var(--line-soft); border-radius:10px; text-align:center; }
    .purge-count b { display:block; font-size:20px; }
    .purge-count span { display:block; margin-top:3px; color:var(--muted); font-size:10px; }
    .purge-boundary { margin:12px 0; padding:11px 12px; border-left:3px solid var(--red); background:var(--red-soft); color:#824840; font-size:11px; line-height:1.55; }
    .purge-phrase { display:block; margin:6px 0; padding:9px 11px; border-radius:8px; background:#232220; color:#fff; font:760 12px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace; }
    .purge-actions { display:flex; justify-content:flex-end; gap:9px; margin-top:18px; }
    @media(max-width:900px) {
      .brand-inner { align-items:center; }
      .top-status .status-item:nth-child(2),.top-status .status-item:nth-child(3) { display:none; }
      .record-list-head,.record-summary { grid-template-columns:minmax(190px,1.35fr) minmax(150px,1fr) 105px 100px 84px; }
      .record-list-head > :nth-child(5),.record-summary > :nth-child(5) { display:none; }
    }
    @media(max-width:680px) {
      .brand-inner { display:flex; align-items:center; padding:11px 14px; }
      .brand-logo { width:44px; height:44px; }
      .brand-copy .eyebrow,.brand-copy p { display:none; }
      .brand-copy h1 { font-size:18px; }
      .top-status { margin:0 0 0 auto; }
      .top-status .status-item { display:none; }
      .icon-btn { width:38px; height:38px; border-radius:11px; }
      .notice-wrap { padding:10px 14px 0; }
      .notice { font-size:12px; }
      .workspace { padding:19px 14px 30px; }
      .workspace-head { align-items:flex-start; margin-bottom:14px; }
      .workspace-head h2 { font-size:25px; }
      .workspace-head .section-intro { display:none; }
      .new-license-btn { min-height:42px; padding-inline:14px; }
      .summary-band { margin-bottom:14px; padding:0 14px; }
      .summary-inner { grid-template-columns:1fr 1fr; gap:9px; }
      .metric { min-height:82px; padding:14px; }
      .metric b { font-size:24px; }
      .directory-header { padding:17px 16px 13px; }
      .directory-header .section-intro { display:none; }
      .directory-tools { grid-template-columns:1fr 110px auto; padding:12px 14px; gap:7px; }
      .record-list-head { display:none; }
      .record { margin:12px; overflow:hidden; border:1px solid #e2e0da; border-radius:13px; }
      .record:hover { background:#fff; }
      .record-summary { display:grid; grid-template-columns:1fr 1fr auto; gap:13px 10px; min-height:0; padding:15px; }
      .identity-cell { grid-column:1/-1; }
      .record-summary > :nth-child(2) { grid-column:1/-1; padding-bottom:11px; border-bottom:1px solid var(--line-soft); }
      .record-summary > :nth-child(3),.record-summary > :nth-child(4),.record-summary > :nth-child(5) { display:block; }
      .record-summary > :nth-child(3)::before { content:"状态"; }
      .record-summary > :nth-child(4)::before { content:"设备"; }
      .record-summary > :nth-child(5)::before { content:"到期"; }
      .record-summary > :nth-child(3)::before,.record-summary > :nth-child(4)::before,.record-summary > :nth-child(5)::before { display:block; margin-bottom:5px; color:#8a867e; font-size:10px; font-weight:700; }
      .record-summary > :nth-child(6) { grid-column:1/-1; }
      .manage-btn { width:100%; min-height:40px; }
      .directory-footer { display:block; padding:12px 15px; }
      .directory-footer span:first-child { display:none; }
      .directory-footer::before { content:"台账仅保存在本机 owner-only 目录"; }
      .create-pane,.expanded { width:100vw; max-width:100vw; padding-inline:18px; border-left:0; }
      .panel-head { margin-inline:-18px; padding-inline:18px; }
      .success-path span { display:-webkit-box; overflow:hidden; -webkit-box-orient:vertical; -webkit-line-clamp:2; }
      .success-actions { grid-template-columns:1fr 1fr; }
      .success-actions .btn-quiet { grid-column:1/-1; }
      .two,.edit-grid { grid-template-columns:1fr; }
      .wide,.edit-grid .wide { grid-column:1; }
      .edit-actions { margin-inline:-18px; padding-inline:18px; }
      .edit-actions,.edit-actions .right { width:auto; }
      .edit-actions .right { display:grid; grid-template-columns:1fr 1fr; width:100%; }
      .edit-actions > .btn { width:100%; }
      .confirm-actions { display:grid; grid-template-columns:1fr 1fr; }
      .purge-counts { grid-template-columns:1fr; }
      .purge-actions { display:grid; grid-template-columns:1fr 1fr; }
    }
    @media(prefers-reduced-motion:reduce) {
      *,*::before,*::after { scroll-behavior:auto!important; transition-duration:.01ms!important; animation-duration:.01ms!important; animation-iteration-count:1!important; }
    }
  </style>
</head>
<body>
<main class="page">
  <header class="brand-band">
    <div class="brand-inner">
      <div class="brand-lockup">
        <canvas id="brandLogo" class="brand-logo" width="64" height="64" role="img" aria-label="Lulu Logo"></canvas>
        <div class="brand-copy"><p class="eyebrow">LULU LOCAL CONTROL</p><h1>Lulu 授权管理</h1><p>仅在这台 Mac 上运行的 production 授权工作台</p></div>
      </div>
      <div class="top-status" aria-label="本机服务状态">
        <span class="status-item safe"><span class="dot"></span>本机已连接</span>
        <span class="status-item"><span class="dot blue"></span>Keychain 后端持有</span>
        <span class="status-item production">production 管理</span>
        <button id="refresh" class="icon-btn" type="button" aria-label="刷新授权目录" title="刷新授权目录">↻</button>
        <button id="shutdown" class="icon-btn danger" type="button" aria-label="关闭管理工具" title="关闭管理工具">×</button>
      </div>
    </div>
  </header>
  <div class="notice-wrap"><div class="notice" id="notice" role="status" aria-live="polite">本机管理工具已就绪。浏览器不会接收 production 后端凭证。</div></div>
  <section class="workspace">
    <div class="workspace-head">
      <div><p class="section-kicker">本机授权中心</p><h2>授权工作台</h2><p class="section-intro">先看清谁在使用，再处理授权。所有敏感凭证只由本机后端持有。</p></div>
      <button id="openCreate" class="btn btn-primary new-license-btn" type="button">＋ 新建授权</button>
    </div>
    <section class="summary-band" aria-label="当前授权摘要">
      <div class="summary-inner">
        <div class="summary-lead"><strong>授权目录摘要</strong><span>只由当前已加载目录在浏览器内计算。</span></div>
        <div class="metric"><b id="metricTotal">—</b><span>全部授权</span></div>
        <div class="metric"><b id="metricActive" class="green">—</b><span>正在使用</span></div>
        <div class="metric"><b id="metricInactive">—</b><span>停用 / 到期</span></div>
        <div class="metric"><b id="metricDevices" class="yellow">—</b><span>设备占用</span></div>
      </div>
    </section>
    <section class="directory-pane" aria-labelledby="directoryTitle">
      <div class="directory-header">
        <div><p class="section-kicker"><span id="directoryCount">当前 — 份</span></p><h2 id="directoryTitle">授权目录</h2><p class="section-intro">搜索、筛选并管理已发出的授权；列表不会返回历史明文码。</p></div>
        <div class="directory-meta"><span id="syncState" class="sync-pill">目录已同步</span></div>
      </div>
      <div class="directory-tools">
        <input id="searchInput" type="search" autocomplete="off" aria-label="按授权对象、联系方式、批次、备注或 ID 搜索" placeholder="搜索授权对象、联系方式、批次、备注或 ID">
        <select id="statusFilter" aria-label="按状态筛选"><option value="all">全部状态</option><option value="active">正在使用</option><option value="suspended">已停用</option><option value="revoked">已撤销</option><option value="expired">已到期</option></select>
        <button id="refreshDirectory" class="icon-btn" type="button" aria-label="刷新授权目录" title="刷新授权目录">↻</button>
      </div>
      <div class="record-list-head" aria-hidden="true"><span>授权对象</span><span>联系与批次</span><span>状态</span><span>设备</span><span>到期</span><span>操作</span></div>
      <div id="licenseRows" class="record-list" aria-live="polite"></div>
      <div class="directory-footer"><span>台账目录：${escapeHtml(ledgerDir)}</span><span>只监听 127.0.0.1:${port}</span></div>
    </section>
    <button id="pageOverlay" class="page-overlay" type="button" aria-label="关闭当前面板"></button>
    <section id="createPane" class="create-pane" aria-labelledby="createTitle" aria-hidden="true">
      <div class="panel-head">
        <div><p class="section-kicker">发出新授权</p><h2 id="createTitle">新建授权</h2><p class="section-intro">填写归属和使用范围，创建后先交付一次性明文。</p></div>
        <button id="closeCreate" class="icon-btn panel-close" type="button" aria-label="关闭新建授权" title="关闭">×</button>
      </div>
      <div class="one-time" id="oneTime">
        <div class="success-head"><strong>授权创建成功</strong><span class="once">仅这一次显示</span></div>
        <code class="plain-code" id="createdCode"></code>
        <p class="success-note">关闭或刷新后不再显示。请先交付明文或保留本机 CSV。</p>
        <p class="success-path">CSV 已安全保存：<span id="ledgerPath"></span></p>
        <div class="success-actions"><button id="copyCreated" class="btn btn-dark" type="button">复制明文</button><button id="openLedger" class="btn btn-line" type="button">打开台账文件夹</button><button id="clearCreated" class="btn btn-quiet" type="button">清除</button></div>
      </div>
      <div class="field-group"><span class="group-name">01 · 授权对象</span>
        <label for="recipientName">授权对象</label><input id="recipientName" type="text" autocomplete="off" placeholder="姓名或团队名">
        <label for="contact">联系方式</label><input id="contact" type="text" autocomplete="off" placeholder="微信名、邮箱或其他联系方式">
      </div>
      <div class="field-group"><span class="group-name">02 · 授权范围</span>
        <label for="licenseCode">授权码</label><div class="input-action"><input id="licenseCode" type="text" autocomplete="off" spellcheck="false" placeholder="随机生成，也可手动填写"><button id="generateCode" class="btn btn-line" type="button">换一组</button></div>
        <p class="helper">默认随机生成，也可手动填写已有规则允许的授权码。</p>
        <div class="two"><div><label for="maxDevices">可用设备</label><input id="maxDevices" type="number" min="1" max="99" step="1" value="2"></div><div><label for="expiresAt">到期时间</label><input id="expiresAt" type="datetime-local"></div></div>
      </div>
      <div class="field-group"><span class="group-name">03 · 留下记录</span>
        <div class="two"><div><label for="batch">批次</label><input id="batch" type="text" autocomplete="off" placeholder="例如：朋友内测 07"></div><div><label for="remark">备注</label><textarea id="remark" placeholder="用途与交付说明"></textarea></div></div>
      </div>
      <button id="createLicense" class="btn btn-primary create-submit" type="button">创建授权并保存 CSV</button>
      <p class="helper">CSV 仅保存到本机 owner-only 台账；刷新后列表不会返回历史明文码。</p>
    </section>
  </section>
</main>
<div id="confirmLayer" class="confirm-layer" aria-hidden="true">
  <section id="confirmCard" class="confirm-card" role="alertdialog" aria-modal="true" aria-labelledby="confirmTitle" aria-describedby="confirmMessage">
    <div class="confirm-icon" aria-hidden="true">!</div>
    <h2 id="confirmTitle">请确认操作</h2>
    <p id="confirmMessage"></p>
    <div class="confirm-actions"><button id="confirmCancel" class="btn btn-line" type="button">取消</button><button id="confirmAccept" class="btn btn-dark" type="button">确认</button></div>
  </section>
</div>
<div id="purgeLayer" class="purge-layer" aria-hidden="true">
  <section class="purge-card" role="alertdialog" aria-modal="true" aria-labelledby="purgeTitle" aria-describedby="purgeLead">
    <p class="section-kicker">严格永久删除</p>
    <h2 id="purgeTitle">最后核对这份授权</h2>
    <p id="purgeLead" class="purge-lead">删除后无法在工作台恢复。服务器已经重新清点目标及全部关联记录。</p>
    <div class="purge-target"><strong id="purgeRecipient"></strong><code id="purgeLicenseId"></code></div>
    <div class="purge-counts" aria-label="将被删除的关联记录">
      <div class="purge-count"><b id="purgeDeviceCount">0</b><span>设备记录</span></div>
      <div class="purge-count"><b id="purgeLeaseCount">0</b><span>凭证记录</span></div>
      <div class="purge-count"><b id="purgeOperationCount">0</b><span>设备操作记录</span></div>
    </div>
    <div class="purge-boundary">服务器将保留去敏审计；本机 CSV 台账不会自动删除。离线目标设备最迟可能在 7 天宽限期后失效，其他授权不受影响。</div>
    <label for="purgeInput">请输入下面的完整短语</label>
    <code id="purgePhrase" class="purge-phrase"></code>
    <input id="purgeInput" type="text" autocomplete="off" spellcheck="false" aria-describedby="purgePhrase">
    <div class="purge-actions"><button id="purgeCancel" class="btn btn-line" type="button">取消</button><button id="purgeAccept" class="btn btn-danger" type="button" disabled>永久删除</button></div>
  </section>
</div>
<script nonce="${cspNonce}">
const CONFIG = ${config};
const notice = document.getElementById("notice");
const rows = document.getElementById("licenseRows");
const state = { createdCode:"", items:[], expandedId:"", createOpen:false, query:"", status:"all", loading:false, confirmResolve:null, purge:null };
const STATUS_LABELS = { active:"使用中", suspended:"已停用", revoked:"已撤销", expired:"已到期" };
function show(message, kind) { notice.textContent=message; notice.dataset.kind=kind || "ok"; }
function esc(value) { return String(value ?? "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/\"/g,"&quot;").replace(/'/g,"&#39;"); }
function display(value) { return value || "未设置"; }
function localIso(value) { return value ? new Date(value).toISOString() : null; }
function statusLabel(value) { return STATUS_LABELS[value] || value || "未知"; }
function dateLabel(value) { return value ? String(value).slice(0,10) : "长期有效"; }
function avatar(value) { const text=String(value || "L").trim(); return esc(Array.from(text)[0] || "L"); }
function shortId(value) { const text=String(value || ""); return text.length>16 ? text.slice(0,9)+"…"+text.slice(-5) : text; }
function syncPanels() {
  const createPane=document.getElementById("createPane");
  const open=state.createOpen || Boolean(state.expandedId);
  createPane.classList.toggle("is-open",state.createOpen);
  createPane.setAttribute("aria-hidden",String(!state.createOpen));
  document.getElementById("pageOverlay").classList.toggle("visible",open);
  document.body.classList.toggle("panel-open",open || Boolean(state.confirmResolve) || Boolean(state.purge));
}
function openCreate() {
  state.expandedId=""; state.createOpen=true; render();
  setTimeout(()=>document.getElementById("recipientName").focus(),40);
}
function closePanels() { state.createOpen=false; state.expandedId=""; render(); }
function askConfirm({ title, message, confirmText, danger=false }) {
  if (state.confirmResolve) return Promise.resolve(false);
  const layer=document.getElementById("confirmLayer"), card=document.getElementById("confirmCard"), accept=document.getElementById("confirmAccept");
  document.getElementById("confirmTitle").textContent=title;
  document.getElementById("confirmMessage").textContent=message;
  accept.textContent=confirmText;
  accept.className="btn "+(danger?"btn-danger":"btn-dark");
  card.classList.toggle("danger",danger);
  layer.classList.add("is-open"); layer.setAttribute("aria-hidden","false");
  document.body.classList.add("panel-open");
  return new Promise(resolve=>{state.confirmResolve=resolve;setTimeout(()=>accept.focus(),30);});
}
function settleConfirm(value) {
  const resolve=state.confirmResolve; if (!resolve) return;
  state.confirmResolve=null;
  const layer=document.getElementById("confirmLayer"); layer.classList.remove("is-open"); layer.setAttribute("aria-hidden","true");
  syncPanels(); resolve(value);
}
function closePurge() {
  state.purge=null;
  const layer=document.getElementById("purgeLayer");
  layer.classList.remove("is-open"); layer.setAttribute("aria-hidden","true");
  document.getElementById("purgeInput").value="";
  document.getElementById("purgeAccept").disabled=true;
  syncPanels();
}
function showPurge(preview) {
  state.purge=preview;
  document.getElementById("purgeRecipient").textContent=preview.recipient_name || "未设置授权对象";
  document.getElementById("purgeLicenseId").textContent=preview.license_id;
  document.getElementById("purgeDeviceCount").textContent=String(preview.counts.devices);
  document.getElementById("purgeLeaseCount").textContent=String(preview.counts.leases);
  document.getElementById("purgeOperationCount").textContent=String(preview.counts.deactivation_operations);
  document.getElementById("purgePhrase").textContent=preview.confirmation_phrase;
  document.getElementById("purgeInput").value="";
  document.getElementById("purgeAccept").disabled=true;
  const layer=document.getElementById("purgeLayer");
  layer.classList.add("is-open"); layer.setAttribute("aria-hidden","false");
  syncPanels();
  setTimeout(()=>document.getElementById("purgeInput").focus(),30);
}
async function withBusy(button, label, task) {
  if (!button || button.disabled) return;
  const original=button.textContent; button.disabled=true; button.textContent=label;
  try { return await task(); }
  finally { button.disabled=false; button.textContent=original; }
}
async function drawLogo() {
  if (!CONFIG.logoPngBase64 || typeof createImageBitmap !== "function") return;
  try {
    const raw=atob(CONFIG.logoPngBase64), bytes=new Uint8Array(raw.length);
    for (let index=0; index<raw.length; index+=1) bytes[index]=raw.charCodeAt(index);
    const bitmap=await createImageBitmap(new Blob([bytes],{type:"image/png"}));
    const canvas=document.getElementById("brandLogo"), context=canvas.getContext("2d");
    context.clearRect(0,0,canvas.width,canvas.height); context.drawImage(bitmap,0,0,canvas.width,canvas.height); bitmap.close();
  } catch { /* Visual fallback only; authorization controls stay usable. */ }
}
async function api(path, options={}) {
  const method=options.method || "GET";
  const response=await fetch(path,{...options,headers:{"content-type":"application/json; charset=utf-8",...(method==="GET"?{}:{"x-lulu-local-csrf":CONFIG.csrfToken}),...(options.headers||{})}});
  const data=await response.json().catch(()=>({}));
  if (!response.ok) { const error=new Error(data.message || "请求失败，请稍后重试。"); error.status=data.status || "error"; throw error; }
  return data;
}
function updateMetrics(items) {
  const active=items.filter(item=>item.status==="active").length;
  const used=items.reduce((sum,item)=>sum+Number(item.active_device_count||0),0);
  const capacity=items.reduce((sum,item)=>sum+Number(item.max_devices||1),0);
  document.getElementById("metricTotal").textContent=String(items.length);
  document.getElementById("metricActive").textContent=String(active);
  document.getElementById("metricInactive").textContent=String(items.length-active);
  document.getElementById("metricDevices").textContent=used+" / "+capacity;
}
function filteredItems() {
  const query=state.query.toLocaleLowerCase("zh-CN");
  return state.items.filter(item=>{
    const statusOk=state.status==="all" || item.status===state.status;
    const haystack=[item.recipient_name,item.contact,item.batch,item.remark,item.license_id].join(" ").toLocaleLowerCase("zh-CN");
    return statusOk && (!query || haystack.includes(query));
  });
}
function render() {
  rows.replaceChildren();
  if (state.loading) {
    document.getElementById("directoryCount").textContent="正在读取目录";
    rows.innerHTML='<div class="loading-row" role="status" aria-label="正在读取授权目录"></div>';
    syncPanels(); return;
  }
  const items=filteredItems();
  if (state.expandedId && !items.some(item=>item.license_id===state.expandedId)) state.expandedId="";
  document.getElementById("directoryCount").textContent=items.length===state.items.length ? "共 "+state.items.length+" 份" : "共 "+state.items.length+" 份 · 显示 "+items.length+" 份";
  if (!items.length) {
    rows.innerHTML='<div class="empty">'+(state.items.length ? '没有符合当前搜索或筛选的授权。调整条件后再试。' : '还没有授权，点击“新建授权”发出第一份。')+'</div>';
    syncPanels(); return;
  }
  for (const item of items) {
    const record=document.createElement("article"); record.className="record";
    const id=esc(item.license_id), expanded=state.expandedId===item.license_id;
    const options=["active","suspended","revoked","expired"].map(function(value){return '<option value="'+value+'" '+(item.status===value?'selected':'')+'>'+statusLabel(value)+'</option>';}).join("");
    record.innerHTML='<div class="record-summary"><div class="identity-cell"><div class="avatar">'+avatar(item.recipient_name)+'</div><div class="person"><strong>'+esc(display(item.recipient_name))+'</strong><div class="license-id" title="'+id+'">'+esc(shortId(item.license_id))+'</div></div></div><div><div class="contact">'+esc(display(item.contact))+'</div><div class="batch">'+esc(display(item.batch))+'</div></div><div><span class="record-label">状态</span><span class="state-pill '+esc(item.status)+'">'+esc(statusLabel(item.status))+'</span></div><div><span class="record-label">设备</span><span class="record-value">'+Number(item.active_device_count||0)+' / '+Number(item.max_devices||1)+'</span></div><div><span class="record-label">到期</span><span class="record-value">'+esc(dateLabel(item.expires_at))+'</span></div><button class="manage-btn" data-action="toggle" data-id="'+id+'" type="button" aria-expanded="'+String(expanded)+'" aria-label="管理 '+esc(display(item.recipient_name))+' 的授权">管理</button></div>'+
      (expanded?'<section class="expanded" role="dialog" aria-modal="true" aria-labelledby="recordPanelTitle"><div class="panel-head"><div><p class="section-kicker">授权详情</p><h2 id="recordPanelTitle">'+esc(display(item.recipient_name))+'</h2><p class="section-intro">'+esc(statusLabel(item.status))+' · '+esc(shortId(item.license_id))+'</p></div><button class="icon-btn panel-close" data-action="toggle" data-id="'+id+'" type="button" aria-label="关闭授权详情" title="关闭">×</button></div><div class="drawer-summary"><div class="drawer-stat"><span>设备占用</span><strong>'+Number(item.active_device_count||0)+' / '+Number(item.max_devices||1)+'</strong></div><div class="drawer-stat"><span>到期时间</span><strong>'+esc(dateLabel(item.expires_at))+'</strong></div></div><div class="edit-grid"><div><label>授权对象</label><input data-role="recipient" data-id="'+id+'" value="'+esc(item.recipient_name)+'"></div><div><label>联系方式</label><input data-role="contact" data-id="'+id+'" value="'+esc(item.contact)+'"></div><div><label>设备数</label><input data-role="max" data-id="'+id+'" type="number" min="1" max="99" value="'+Number(item.max_devices||1)+'"></div><div><label>状态</label><select data-role="status" data-id="'+id+'">'+options+'</select></div><div><label>批次</label><input data-role="batch" data-id="'+id+'" value="'+esc(item.batch)+'"></div><div><label>到期时间</label><input data-role="expires" data-id="'+id+'" value="'+esc(item.expires_at)+'" placeholder="ISO 时间或留空"></div><div class="wide"><label>备注</label><textarea data-role="remark" data-id="'+id+'">'+esc(item.remark)+'</textarea></div></div><div class="danger-zone"><strong>永久删除</strong><p>只用于已经逐人核对、今后绝不再使用的授权。服务器关联记录将清除，去敏审计与本机 CSV 保留。</p>'+(item.status==="active"?'<button class="btn btn-danger" type="button" disabled>请先停用授权</button>':'<button class="btn btn-danger" data-action="purge" data-id="'+id+'" type="button">核对并永久删除</button>')+'</div><div class="edit-actions"><button class="btn btn-primary" data-action="save" data-id="'+id+'" type="button">保存更改</button><div class="right"><button class="btn btn-line" data-action="status" data-id="'+id+'" data-next-status="'+(item.status==="active"?'suspended':'active')+'" type="button">'+(item.status==="active"?'停用授权':'恢复授权')+'</button><button class="btn btn-danger" data-action="reset" data-id="'+id+'" type="button">重置设备</button></div></div></section>':'');
    rows.appendChild(record);
  }
  syncPanels();
}
async function generateCode(silent=false) {
  const data=await api("/admin/code"); document.getElementById("licenseCode").value=data.license_code;
  if (!silent) show("已生成一组新的随机授权码。");
}
async function refresh() {
  state.loading=true; document.getElementById("syncState").textContent="正在同步"; render(); show("正在读取授权目录…","loading");
  try {
    const PAGE=50, MAX_PAGES=100;
    const all=[], seen=new Set();
    for (let page=0; page<MAX_PAGES; page++) {
      const data=await api("/admin/licenses?limit="+PAGE+"&offset="+(page*PAGE));
      const items=Array.isArray(data.items)?data.items:[];
      for (const item of items) {
        const id=String(item?.license_id||"");
        if (id && seen.has(id)) continue;
        if (id) seen.add(id);
        all.push(item);
      }
      if (items.length<PAGE) break;
    }
    state.items=all;
    if (state.expandedId && !state.items.some(item=>item.license_id===state.expandedId)) state.expandedId="";
    updateMetrics(state.items); document.getElementById("syncState").textContent="目录已同步"; show("目录已同步；列表不包含历史明文码或敏感哈希。");
  } catch (error) {
    document.getElementById("syncState").textContent="同步失败"; throw error;
  } finally { state.loading=false; render(); }
}
async function createLicense(button) {
  return withBusy(button,"正在创建…",async ()=>{
    const code=document.getElementById("licenseCode").value.trim();
    const body={license_code:code,max_devices:Number(document.getElementById("maxDevices").value),expires_at:localIso(document.getElementById("expiresAt").value),recipient_name:document.getElementById("recipientName").value.trim(),contact:document.getElementById("contact").value.trim(),batch:document.getElementById("batch").value.trim(),remark:document.getElementById("remark").value.trim()};
    show("正在创建授权并写入本机安全台账…","loading");
    const data=await api("/admin/licenses",{method:"POST",body:JSON.stringify(body)});
    state.createdCode=code; document.getElementById("createdCode").textContent=code; document.getElementById("ledgerPath").textContent=data.ledger_path; document.getElementById("oneTime").style.display="block";
    for (const id of ["recipientName","contact","batch","remark"]) document.getElementById(id).value="";
    await generateCode(true); await refresh(); show("授权创建成功；请先复制本次明文或确认本机 CSV 已保存。");
    document.getElementById("copyCreated").focus();
  });
}
function field(id,role) { return document.querySelector('[data-role="'+role+'"][data-id="'+CSS.escape(id)+'"]'); }
async function saveLicense(id,forcedStatus,button) {
  return withBusy(button,forcedStatus?"正在处理…":"正在保存…",async ()=>{
    const body={status:forcedStatus||field(id,"status").value,max_devices:Number(field(id,"max").value),expires_at:field(id,"expires").value.trim()||null,recipient_name:field(id,"recipient").value.trim(),contact:field(id,"contact").value.trim(),batch:field(id,"batch").value.trim(),remark:field(id,"remark").value.trim()};
    show("正在保存授权变更…","loading"); await api("/admin/licenses/"+encodeURIComponent(id),{method:"PATCH",body:JSON.stringify(body)}); await refresh(); show(forcedStatus==="suspended"?"授权已停用。":forcedStatus==="active"?"授权已恢复。":"修改已保存。");
  });
}
async function changeStatus(id,nextStatus,button) {
  const suspend=nextStatus==="suspended";
  const accepted=await askConfirm({title:suspend?"停用这份授权？":"恢复这份授权？",message:suspend?"停用后，授权对象将无法继续使用 Lulu；记录与设备信息仍会保留。":"恢复后，授权对象可以重新使用 Lulu；设备数量限制保持不变。",confirmText:suspend?"确认停用":"确认恢复",danger:suspend});
  if (!accepted) return; await saveLicense(id,nextStatus,button);
}
async function resetDevices(id,button) {
  const accepted=await askConfirm({title:"重置全部设备？",message:"当前已激活设备会全部失效。授权对象需要在设备上重新激活，授权码本身不会被删除。",confirmText:"确认重置设备",danger:true});
  if (!accepted) return;
  await withBusy(button,"正在重置…",async ()=>{show("正在重置设备…","loading");await api("/admin/licenses/"+encodeURIComponent(id)+"/devices/reset",{method:"POST",body:"{}"});await refresh();show("设备已重置，旧设备需要重新激活。");});
}
async function openPurge(id,button) {
  await withBusy(button,"正在清点…",async ()=>{
    show("正在从服务器重新清点这份授权的关联记录…","loading");
    const preview=await api("/admin/licenses/"+encodeURIComponent(id)+"/purge-preview",{method:"POST",body:"{}"});
    showPurge(preview);
    show("删除预检已完成。请逐项核对后输入确认短语。","loading");
  });
}
async function purgeLicense(button) {
  if (!state.purge) return;
  const preview=state.purge;
  const phrase=document.getElementById("purgeInput").value;
  if (phrase!==preview.confirmation_phrase) return;
  await withBusy(button,"正在永久删除…",async ()=>{
    show("正在执行原子永久删除，请勿关闭页面…","loading");
    try {
      await api("/admin/licenses/"+encodeURIComponent(preview.license_id),{method:"DELETE",body:JSON.stringify({confirmation_phrase:phrase,preview_token:preview.preview_token})});
    } catch (error) {
      if (error?.status==="purge_stale") closePurge();
      throw error;
    }
    closePurge(); state.expandedId=""; await refresh();
    show("该授权及关联服务器记录已永久删除；去敏审计与本机 CSV 已保留。");
  });
}
function clearCreated() { state.createdCode=""; document.getElementById("createdCode").textContent=""; document.getElementById("ledgerPath").textContent=""; document.getElementById("oneTime").style.display="none"; show("页面明文已清除，请以本机 CSV 为准。"); }
function failed(error) { show(error && error.message ? error.message : "操作失败，请检查后重试。","error"); }
document.getElementById("openCreate").onclick=openCreate;
document.getElementById("closeCreate").onclick=closePanels;
document.getElementById("pageOverlay").onclick=closePanels;
document.getElementById("generateCode").onclick=event=>withBusy(event.currentTarget,"生成中…",()=>generateCode()).catch(failed);
document.getElementById("createLicense").onclick=event=>createLicense(event.currentTarget).catch(failed);
document.getElementById("refresh").onclick=()=>refresh().catch(failed);
document.getElementById("refreshDirectory").onclick=()=>refresh().catch(failed);
document.getElementById("searchInput").oninput=event=>{state.query=event.target.value.trim();render();};
document.getElementById("statusFilter").onchange=event=>{state.status=event.target.value;render();};
document.getElementById("copyCreated").onclick=()=>state.createdCode&&navigator.clipboard.writeText(state.createdCode).then(()=>show("本次授权码已复制。")).catch(()=>show("复制失败，请手动复制。","error"));
document.getElementById("clearCreated").onclick=clearCreated;
document.getElementById("openLedger").onclick=()=>api("/admin/open-ledger",{method:"POST",body:"{}"}).then(()=>show("已打开本机台账文件夹。")).catch(failed);
document.getElementById("confirmCancel").onclick=()=>settleConfirm(false);
document.getElementById("confirmAccept").onclick=()=>settleConfirm(true);
document.getElementById("purgeCancel").onclick=closePurge;
document.getElementById("purgeInput").oninput=event=>{document.getElementById("purgeAccept").disabled=!state.purge||event.target.value!==state.purge.confirmation_phrase;};
document.getElementById("purgeAccept").onclick=event=>purgeLicense(event.currentTarget).catch(failed);
document.getElementById("shutdown").onclick=async ()=>{
  const accepted=await askConfirm({title:"关闭管理工具？",message:"关闭后，本次 localhost 会话立即失效。下次管理授权时需要重新从项目入口打开。",confirmText:"确认关闭"});
  if (!accepted) return;
  api("/admin/shutdown",{method:"POST",body:"{}"}).then(()=>{clearCreated();show("管理工具已关闭，可以关闭本页。");document.querySelectorAll("button,input,select,textarea").forEach(element=>element.disabled=true);}).catch(failed);
};
rows.onclick=event=>{
  const target=event.target.closest("button[data-action][data-id]"); if (!target) return;
  const id=target.dataset.id;
  if (target.dataset.action==="toggle") { state.createOpen=false; state.expandedId=state.expandedId===id?"":id; render(); if(state.expandedId)setTimeout(()=>document.querySelector(".expanded .panel-close")?.focus(),30); }
  if (target.dataset.action==="save") saveLicense(id,null,target).catch(failed);
  if (target.dataset.action==="status") changeStatus(id,target.dataset.nextStatus,target).catch(failed);
  if (target.dataset.action==="reset") resetDevices(id,target).catch(failed);
  if (target.dataset.action==="purge") openPurge(id,target).catch(failed);
};
document.addEventListener("keydown",event=>{
  if (event.key==="Escape") { if(state.purge)closePurge();else if(state.confirmResolve)settleConfirm(false);else if(state.createOpen||state.expandedId)closePanels(); }
  if (event.key==="/" && !state.createOpen && !state.expandedId && !["INPUT","TEXTAREA","SELECT"].includes(document.activeElement?.tagName)) { event.preventDefault(); document.getElementById("searchInput").focus(); }
});
drawLogo(); generateCode(true).then(refresh).catch(failed);
</script>
</body>
</html>`;
}
