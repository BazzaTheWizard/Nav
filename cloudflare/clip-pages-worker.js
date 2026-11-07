// Nav Studios clip pages: https://clips.navstudiolab.com/<page>
//
// One page per pitch: the creator's clips in vertical players, a download button for
// each, and a reply button. Files live in an R2 bucket, bound to this Worker as CLIPS.
// The pipeline (core/clip_pages.py) uploads them; nothing here needs editing per pitch.
//
// Setup: claude_review/round16/README.md. To change the page, edit below and paste the
// whole file into Cloudflare again (Workers & Pages -> nav-clips -> Edit code -> Deploy).

const SITE = "https://navstudiolab.com";
const CONTACT_EMAIL = "barrynavan@gmail.com";
const SENDER = "Barry Navan";
const AGENCY = "Nav Studios";

const PAGE = /^[a-z0-9-]{6,80}$/;              // page names the pipeline makes
const FILE = /^(clip|poster)_(0[1-9])\.(mp4|jpg)$/;
const EVENTS = new Set(["open", "play", "half", "end", "dl"]);

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const parts = url.pathname.split("/").filter(Boolean);
    if (url.pathname === "/e" && request.method === "POST") return recordEvent(request, env);
    if (request.method !== "GET" && request.method !== "HEAD") return new Response("Method not allowed", { status: 405 });
    if (parts.length === 0) return Response.redirect(SITE, 302);
    if (parts.length === 1 && PAGE.test(parts[0])) return page(parts[0], url, env);
    if (parts.length === 2 && PAGE.test(parts[0]) && FILE.test(parts[1])) return file(parts[0], parts[1], request, url, env);
    return notFound();
  },
};

// ---------- Video and poster files (with seeking: HTTP range requests) ----------
async function file(slug, name, request, url, env) {
  const object = await env.CLIPS.get(`${slug}/${name}`, { range: request.headers, onlyIf: request.headers });
  if (object === null) return notFound();
  const headers = new Headers();
  object.writeHttpMetadata(headers);
  headers.set("etag", object.httpEtag);
  headers.set("accept-ranges", "bytes");
  headers.set("cache-control", "no-cache"); // a trimmed clip replaces the file; always check the ETag
  headers.set("x-robots-tag", "noindex");
  if (url.searchParams.has("dl") && name.endsWith(".mp4")) {
    headers.set("content-disposition", `attachment; filename="${downloadName(slug, name)}"`);
  }
  if (!("body" in object) || object.body === undefined) {
    return new Response(null, { status: request.headers.has("if-none-match") ? 304 : 412, headers });
  }
  const size = object.size;
  let start = 0;
  let end = size - 1;
  if (request.headers.has("range") && object.range) {
    const range = object.range;
    if (range.suffix !== undefined) {
      start = Math.max(0, size - range.suffix);
    } else {
      start = range.offset ?? 0;
      if (range.length !== undefined) end = Math.min(size - 1, start + range.length - 1);
    }
    headers.set("content-range", `bytes ${start}-${end}/${size}`);
    headers.set("content-length", String(end - start + 1));
    return new Response(request.method === "HEAD" ? null : object.body, { status: 206, headers });
  }
  headers.set("content-length", String(size));
  return new Response(request.method === "HEAD" ? null : object.body, { status: 200, headers });
}

function downloadName(slug, name) {
  const n = Number(name.slice(5, 7));
  const base = slug.replace(/-[a-z0-9]{6}$/, "") || "clip";
  return `${base}-short-${n}.mp4`;
}

// ---------- Plays, for the app's Sent tab ----------
async function recordEvent(request, env) {
  const done = new Response(null, { status: 204 });
  let data;
  try {
    data = JSON.parse((await request.text()).slice(0, 400));
  } catch {
    return done;
  }
  const slug = String(data?.s ?? "");
  const type = String(data?.t ?? "");
  const clip = Number(data?.c ?? 0);
  if (!PAGE.test(slug) || !EVENTS.has(type) || !Number.isInteger(clip) || clip < 0 || clip > 9) return done;
  if ((await env.CLIPS.head(`${slug}/page.json`)) === null) return done;
  const key = `_events/${slug}/${Date.now()}_${type}_${clip}_${crypto.randomUUID().slice(0, 8)}`;
  await env.CLIPS.put(key, "", {
    customMetadata: {
      ua: (request.headers.get("user-agent") || "").slice(0, 200),
      country: String(request.cf?.country || ""),
    },
  });
  return done;
}

// ---------- The page ----------
async function page(slug, url, env) {
  const object = await env.CLIPS.get(`${slug}/page.json`);
  if (object === null) return notFound();
  let info;
  try {
    info = await object.json();
  } catch {
    return notFound();
  }
  const clips = Array.isArray(info.clips) ? info.clips.slice(0, 9) : [];
  const channel = String(info.channel_title || "you");
  const video = String(info.video_title || "");
  const count = clips.length;
  const origin = url.origin;
  const mailto = `mailto:${CONTACT_EMAIL}?subject=${encodeURIComponent(`Shorts for ${channel}`)}`;
  const cards = clips.map((clip, i) => {
    const n = i + 1;
    const nn = String(n).padStart(2, "0");
    const seconds = Math.round(Number(clip.end) - Number(clip.start));
    return `
      <figure class="card">
        <div class="clip">
          <video src="/${slug}/clip_${nn}.mp4" poster="/${slug}/poster_${nn}.jpg" controls playsinline preload="metadata" aria-label="Short ${n}"></video>
        </div>
        <figcaption>
          <div><strong>Short ${n}</strong><span>${seconds} s · from ${clock(clip.start)}</span></div>
          <a class="btn btn-ghost btn-small" data-dl="${n}" href="/${slug}/clip_${nn}.mp4?dl=1" download="${downloadName(slug, `clip_${nn}.mp4`)}">Download</a>
        </figcaption>
      </figure>`;
  }).join("");
  const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${count} Shorts for ${esc(channel)} · ${AGENCY}</title>
<meta name="robots" content="noindex, nofollow">
<meta name="description" content="Vertical clips from ${esc(video)}, made by ${AGENCY}.">
<meta property="og:title" content="${count} Shorts for ${esc(channel)}">
<meta property="og:description" content="Vertical clips with captions, cut from ${esc(video)}. Made by ${AGENCY}.">
<meta property="og:image" content="${origin}/${slug}/poster_01.jpg">
<meta name="theme-color" content="#0B0C10">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Geist:wght@400..800&display=swap" rel="stylesheet">
<style>
  :root { --bg:#0B0C10; --panel:#0F1117; --line:#1F2430; --line-2:#2A3040; --text:#F2F3F5; --muted:#9097A3; --dim:#646B78; --accent:#FFE600;
          --sans:"Geist", ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text); font: 400 16px/1.6 var(--sans); -webkit-font-smoothing: antialiased; }
  a { color: inherit; }
  :focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; border-radius: 6px; }
  .wrap { max-width: 1080px; margin: 0 auto; padding: 0 24px; }
  .nav { border-bottom: 1px solid var(--line); }
  .nav .wrap { display: flex; align-items: center; justify-content: space-between; height: 64px; }
  .brand { display: flex; align-items: center; gap: 10px; text-decoration: none; font-weight: 650; letter-spacing: -0.02em; font-size: 17px; }
  .brand-mark { width: 13px; height: 22px; border: 2.5px solid var(--accent); border-radius: 4px; }
  .site { color: var(--muted); font-size: 14px; text-decoration: none; }
  .site:hover { color: var(--text); }
  header.intro { padding: 48px 0 32px; }
  .eyebrow { color: var(--accent); font-weight: 600; font-size: 14px; letter-spacing: 0.02em; margin: 0 0 12px; }
  h1 { font-size: clamp(32px, 5vw, 52px); font-weight: 700; letter-spacing: -0.04em; line-height: 1.02; margin: 0; text-wrap: balance; }
  .lead { color: var(--muted); font-size: 18px; max-width: 56ch; margin: 16px 0 0; }
  .lead q { color: var(--text); quotes: "“" "”"; }
  .clips { display: grid; grid-template-columns: repeat(${Math.max(1, Math.min(count, 3))}, minmax(0, 300px)); gap: 24px; }
  .card { margin: 0; }
  .clip { position: relative; aspect-ratio: 9 / 16; border-radius: 18px; overflow: hidden; background: #000; box-shadow: 0 0 0 1px var(--line-2); }
  .clip video { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; display: block; background: #000; }
  figcaption { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 12px; }
  figcaption strong { display: block; font-weight: 600; font-size: 15px; }
  figcaption span { color: var(--dim); font-size: 13px; }
  .btn { display: inline-flex; align-items: center; justify-content: center; font: 600 15px/1 var(--sans); text-decoration: none;
         padding: 14px 20px; border-radius: 999px; border: 1px solid transparent; white-space: nowrap; }
  .btn-primary { background: var(--accent); color: #0B0C10; }
  .btn-primary:hover { background: #FFF06B; }
  .btn-ghost { color: var(--text); border-color: var(--line-2); }
  .btn-ghost:hover { border-color: #4A5266; background: #141721; }
  .btn-small { padding: 9px 14px; font-size: 13px; }
  .swipe { display: none; color: var(--dim); font-size: 13px; margin: 0 0 12px; }
  .rights { color: var(--muted); font-size: 15px; margin: 28px 0 0; }
  .cta { margin: 56px 0 0; padding: 32px; background: var(--panel); border: 1px solid var(--line); border-radius: 20px;
         display: flex; align-items: center; justify-content: space-between; gap: 24px; flex-wrap: wrap; }
  .cta h2 { font-size: clamp(22px, 3vw, 28px); font-weight: 650; letter-spacing: -0.03em; line-height: 1.15; margin: 0; max-width: 24ch; }
  .cta p { color: var(--muted); margin: 8px 0 0; }
  .cta .actions { display: flex; gap: 12px; flex-wrap: wrap; }
  footer { color: var(--dim); font-size: 13px; padding: 40px 0 48px; }
  footer .wrap { padding-top: 0; padding-bottom: 0; }
  footer .credit { margin-top: 8px; font-size: 12px; }
  @media (max-width: 760px) {
    header.intro { padding: 32px 0 24px; }
    .swipe { display: block; }
    .clips { display: flex; overflow-x: auto; scroll-snap-type: x mandatory; gap: 14px; margin: 0 -24px; padding: 0 24px 8px; scrollbar-width: none; }
    .clips::-webkit-scrollbar { display: none; }
    .card { flex: 0 0 78vw; max-width: 320px; scroll-snap-align: center; }
    .cta { padding: 24px; }
  }
</style>
</head>
<body>
<nav class="nav"><div class="wrap">
  <a class="brand" href="${SITE}"><span class="brand-mark" aria-hidden="true"></span>${AGENCY}</a>
  <a class="site" href="${SITE}">${SITE.replace(/^https?:\/\//, "")}</a>
</div></nav>
<main class="wrap">
  <header class="intro">
    <p class="eyebrow">Made for ${esc(channel)}</p>
    <h1>Your ${count} Short${count === 1 ? "" : "s"}, ready to post</h1>
    <p class="lead">${video ? `Cut from your ${info.kind === "episode" ? "episode" : "video"} <q>${esc(video)}</q>. ` : ""}Vertical, with captions, no watermark.</p>
  </header>
  ${count > 1 ? `<p class="swipe">Swipe for all ${count} →</p>` : ""}
  <section class="clips" aria-label="Your Shorts">${cards}
  </section>
  <p class="rights">They're yours to post on Shorts, Reels or TikTok. No strings attached.</p>
  <section class="cta">
    <div>
      <h2>Want 3–5 of these for every new upload?</h2>
      <p>Just reply to my email, or write to me here.</p>
    </div>
    <div class="actions">
      <a class="btn btn-primary" href="${mailto}">Email ${SENDER.split(" ")[0]}</a>
      <a class="btn btn-ghost" href="${SITE}">How it works</a>
    </div>
  </section>
</main>
<footer><div class="wrap">${SENDER} · ${AGENCY} · <a href="${SITE}">${SITE.replace(/^https?:\/\//, "")}</a><div class="credit">Price charts: TradingView Lightweight Charts™, © TradingView, Inc. · <a href="https://www.tradingview.com/">tradingview.com</a></div></div></footer>
<script>
(() => {
  const page = ${JSON.stringify(slug)};
  const send = (t, c = 0) => { try { navigator.sendBeacon("/e", JSON.stringify({ s: page, t, c })); } catch (e) {} };
  send("open");
  const videos = [...document.querySelectorAll("video")];
  videos.forEach((v, i) => {
    let played = false, half = false;
    v.addEventListener("play", () => {
      videos.forEach((o) => { if (o !== v) o.pause(); });   // one at a time
      if (!played) { played = true; send("play", i + 1); }
    });
    v.addEventListener("timeupdate", () => {
      if (!half && v.duration && v.currentTime / v.duration >= 0.5) { half = true; send("half", i + 1); }
    });
    v.addEventListener("ended", () => send("end", i + 1));
  });
  document.querySelectorAll("[data-dl]").forEach((a) => a.addEventListener("click", () => send("dl", Number(a.dataset.dl))));
})();
</script>
</body>
</html>`;
  return new Response(html, {
    headers: {
      "content-type": "text/html; charset=utf-8",
      "cache-control": "no-cache",
      "x-robots-tag": "noindex, nofollow",
      "referrer-policy": "no-referrer",
    },
  });
}

function clock(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const h = Math.floor(total / 3600), m = Math.floor((total % 3600) / 60), s = total % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}

function esc(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function notFound() {
  return new Response(
    `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">
<title>Not found · ${AGENCY}</title>
<body style="margin:0;background:#0B0C10;color:#F2F3F5;font:16px/1.6 system-ui,sans-serif;display:grid;place-items:center;min-height:100vh;text-align:center;padding:24px">
<div><p style="font-size:22px;font-weight:650;margin:0 0 8px">This page isn't here any more.</p>
<p style="color:#9097A3;margin:0">Write to <a style="color:#FFE600" href="mailto:${CONTACT_EMAIL}">${CONTACT_EMAIL}</a> and I'll send the clips again.</p></div></body>`,
    { status: 404, headers: { "content-type": "text/html; charset=utf-8", "x-robots-tag": "noindex" } },
  );
}
