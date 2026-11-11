// Nav Studios clip pages: https://clips.navstudiolab.com/<page>
//
// One page per pitch: the client's deliverables portal. Each clip in a vertical player with
// one-click downloads (video, captions file, cover image), and its two posts in drop-downs with
// Copy buttons: the LinkedIn post and the Shorts & Reels caption (Round 50); the offer and a
// "Request a call" email at the bottom. Files live in an R2 bucket, bound to this Worker as CLIPS.
// The pipeline (core/clip_pages.py) uploads them; nothing here needs editing per pitch.
//
// Setup: claude_review/round16/README.md. To change the page, edit below and paste the
// whole file into Cloudflare again (Workers & Pages -> nav-clips -> Edit code -> Deploy).

const SITE = "https://navstudiolab.com";
const CONTACT_EMAIL = "barrynavan@gmail.com";
const SENDER = "Barry Navan";
const AGENCY = "Nav Studios";
// Round 50: "Request a call" opens an email to you with a short request already written (no
// phone number on the page: you answer when you can and propose a time).
const CALL_SUBJECT = (channel) => `Call about weekly clips for ${channel}`;
const CALL_BODY = (channel) => `Hi ${SENDER.split(" ")[0]},\n\nWe looked through the clips for ${channel}. `
  + `Could we set up a quick 15-minute call this week to talk about doing this every week?\n\n`
  + `A few times that work for us:\n- \n- \n\nThanks,\n`;

// ---------- Round 45: the offer (https://clips.navstudiolab.com/offer) ----------
// Every number on the offer page and the clip pages' offer strip comes from here. The price,
// spots and trial must match core/outreach.py (FOUNDING_PRICE, FOUNDING_SPOTS, TRIAL): the
// emails quote them too. tests/test_round45.py checks they agree.
const OFFER = {
  price: "$500",                 // core/outreach.py FOUNDING_PRICE "$500/month"
  per: "month",
  spots: 3,                      // FOUNDING_SPOTS
  trial: "2-week trial",         // TRIAL "a 2-week trial"
  clipsPerEpisode: 3,
  episodesPerMonth: 4,           // so 12 clips a month
  turnaround: "within 24 hours", // after you share a recording
  // Round 54: the plans after the founding spots, and the guarantee (core/outreach.py
  // EXECUTIVE_PRICE, TEAM_PRICE, TEAM_SIZE, GUARANTEE; tests/test_round54.py checks they agree)
  executivePrice: "$990",        // EXECUTIVE_PRICE "$990/month"
  teamPrice: "$2,500",           // TEAM_PRICE "$2,500/month"
  teamSize: "2 to 4 executives", // TEAM_SIZE
  guarantee: "If your first 12 clips don't beat your last month of LinkedIn posts on impressions, your next month is free.",
};
const CLIPS_PER_MONTH = OFFER.clipsPerEpisode * OFFER.episodesPerMonth;
// Round 50: what every clip comes with, in one phrase (clip pages and the offer page say the same).
const PER_CLIP = "1 LinkedIn post and 1 Shorts &amp; Reels caption";
const CHEVRON = `<span class="chev" aria-hidden="true"><svg width="12" height="12" viewBox="0 0 12 12" fill="none">`
  + `<path d="M2.5 4.5 6 8l3.5-3.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg></span>`;

// Round 50: a clip's Shorts & Reels caption from page.json ({title, description, hashtags}), or null.
function socialOf(clip) {
  const s = clip && typeof clip.social === "object" ? clip.social : null;
  if (!s || !s.title || !s.description) return null;
  const tags = (Array.isArray(s.hashtags) ? s.hashtags : []).map((t) => String(t).replace(/[^A-Za-z0-9]/g, ""))
    .filter(Boolean).slice(0, 8).map((t) => `#${t}`).join(" ");
  return { title: String(s.title).slice(0, 100), description: String(s.description).slice(0, 600), tags };
}

const PAGE = /^[a-z0-9-]{6,80}$/;              // page names the pipeline makes
const FILE = /^(clip|poster|captions)_(0[1-9])\.(mp4|jpg|srt)$/;  // Round 46: captions_NN.srt
const COVER = /^cover_(0[1-9])\.jpg$/;                              // Round 49: the full-size cover image
const EVENTS = new Set(["open", "play", "half", "end", "dl", "copy", "offer", "srt", "cover", "social", "call"]);  // Round 45:
// copied a post, opened the offer; Round 49: took the captions file or the cover image. Round 56: "social" (copied
// the Shorts & Reels caption) and "call" (clicked Request a call) were sent by the page and read by the app
// (core/clip_pages.py EVENT_TYPES) since Round 50, but dropped here: the Sent tab never saw them.
// Round 56: every HTML page: no framing by other sites (clickjacking), no MIME sniffing.
const SECURITY_HEADERS = {
  "x-content-type-options": "nosniff",
  "x-frame-options": "DENY",
  "content-security-policy": "frame-ancestors 'none'; base-uri 'none'; form-action 'none'; object-src 'none'",
};
// Round 49: link scanners and previewers that run scripts don't count as a view.
const NOT_A_PERSON = /bot|crawl|spider|slurp|headless|preview|scanner|monitor|lighthouse|phantom|python|curl|wget/i;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const parts = url.pathname.split("/").filter(Boolean);
    if (url.pathname === "/e" && request.method === "POST") return recordEvent(request, env);
    if (request.method !== "GET" && request.method !== "HEAD") return new Response("Method not allowed", { status: 405 });
    if (parts.length === 0) return Response.redirect(SITE, 302);
    if (parts.length === 1 && parts[0] === "offer") return offerPage(url);
    if (parts.length === 1 && parts[0] === "_nav_owner") return ownerPage(request, env, url);
    if (parts.length === 1 && PAGE.test(parts[0])) return page(parts[0], url, env);
    if (parts.length === 2 && PAGE.test(parts[0]) && (FILE.test(parts[1]) || COVER.test(parts[1]))) {
      return file(parts[0], parts[1], request, url, env);
    }
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
  headers.set("x-content-type-options", "nosniff");
  if (url.searchParams.has("dl") && (name.endsWith(".mp4") || name.endsWith(".srt") || COVER.test(name))) {
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
  const n = Number(name.match(/_(\d\d)\./)[1]);
  const base = slug.replace(/-[a-z0-9]{6}$/, "") || "clip";
  if (name.endsWith(".srt")) return `${base}-clip-${n}-captions.srt`;
  if (name.startsWith("cover_")) return `${base}-clip-${n}-cover.jpg`;
  return `${base}-clip-${n}.mp4`;
}

// ---------- Plays, for the app's Sent tab ----------
async function recordEvent(request, env) {
  const done = new Response(null, { status: 204 });
  // Round 56: the page's own beacons come from this origin; another site posting here would only
  // be faking a prospect's activity (which drives the app's follow-up plan).
  const origin = request.headers.get("origin");
  if (origin && origin !== new URL(request.url).origin) return done;
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
  if (NOT_A_PERSON.test(request.headers.get("user-agent") || "")) return done;
  if ((await env.CLIPS.head(`${slug}/page.json`)) === null) return done;
  // Round 51: your own network never counts. The event's id starts with a short hash of the
  // visitor's network (never the address itself); networks you marked as yours (/_nav_owner,
  // which the app calls by itself) are skipped here, and the app drops their older events too.
  const net = await networkHash(request);
  if (net && (await env.CLIPS.head(`_me/${net}`)) !== null) return done;
  const key = `_events/${slug}/${Date.now()}_${type}_${clip}_${net || "00000000"}${crypto.randomUUID().slice(0, 4)}`;
  await env.CLIPS.put(key, "", {
    customMetadata: {
      ua: (request.headers.get("user-agent") || "").slice(0, 200),
      country: String(request.cf?.country || ""),
    },
  });
  return done;
}

// ---------- Round 51: your own devices and networks ----------
// A short one-way hash of the visitor's network (IPv4 address, or an IPv6 address's /64: the
// same home or office network), salted. Stored only as a prefix of event ids and _me/ keys.
async function networkHash(request) {
  let ip = String(request.headers.get("cf-connecting-ip") || "");
  if (!ip) return "";
  if (ip.includes(":")) ip = ip.split(":").slice(0, 4).join(":");
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(`nav-owner-v1|${ip}`));
  return [...new Uint8Array(digest)].slice(0, 4).map((b) => b.toString(16).padStart(2, "0")).join("");
}

// GET /_nav_owner: marks this network as yours (your views, downloads and copies are never
// counted from it) and, in a browser, this browser too. The app calls it by itself on start;
// open it once on your phone so it's marked on mobile data as well.
async function ownerPage(request, env, url) {
  const net = await networkHash(request);
  if (net) {
    await env.CLIPS.put(`_me/${net}`, "", { customMetadata: { at: new Date().toISOString(),
      ua: (request.headers.get("user-agent") || "").slice(0, 200) } });
  }
  if (url.searchParams.has("app") || url.searchParams.has("ping")) {
    return new Response(JSON.stringify({ ok: Boolean(net), network: net }), {
      headers: { "content-type": "application/json", "cache-control": "no-store", "x-robots-tag": "noindex",
                 "x-content-type-options": "nosniff" },
    });
  }
  return new Response(`<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>Marked as yours · ${AGENCY}</title>
<body style="margin:0;background:#0B0C10;color:#F2F3F5;font:16px/1.6 system-ui,sans-serif;display:grid;place-items:center;min-height:100vh;text-align:center;padding:24px">
<div><p style="font-size:22px;font-weight:650;margin:0 0 8px">This device and network are marked as yours.</p>
<p style="color:#9097A3;margin:0">Your visits, plays, downloads and copies on clip pages won't count as a prospect's.</p></div>
<script>try { localStorage.setItem("nav_me", "1"); } catch (e) {}</script></body>`, {
    headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store", "x-robots-tag": "noindex",
               ...SECURITY_HEADERS },
  });
}

// ---------- The page: the client's deliverables portal ----------
// Round 49: everything a team needs to publish each clip, one click each: the video (.mp4), the
// captions file (.srt, for LinkedIn and Meta's native captions), the cover image (.jpg) and the
// ready-to-post LinkedIn copy with a Copy button. Opening it with ?me=1 (the app's buttons do)
// marks this browser as yours: your own visits are never counted as theirs.
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
  const kind = info.kind === "episode" ? "episode" : "video";
  const delivered = info.created_at ? new Date(info.created_at) : null;
  const deliveredText = delivered && !isNaN(delivered)
    ? delivered.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" }) : "";
  const cards = clips.map((clip, i) => {
    const n = i + 1;
    const nn = String(n).padStart(2, "0");
    const seconds = Math.round(Number(clip.end) - Number(clip.start));
    const social = socialOf(clip);
    const assets = [
      clip.srt ? `<a class="asset" data-asset="srt" data-n="${n}" href="/${slug}/captions_${nn}.srt?dl=1">Captions <span>.srt</span></a>` : "",
      clip.cover ? `<a class="asset" data-asset="cover" data-n="${n}" href="/${slug}/cover_${nn}.jpg?dl=1" download="${downloadName(slug, `cover_${nn}.jpg`)}">Cover image <span>.jpg</span></a>` : "",
    ].filter(Boolean).join("");
    return `
      <article class="card" aria-label="Clip ${n}">
        <div class="clip">
          <video src="/${slug}/clip_${nn}.mp4" poster="/${slug}/poster_${nn}.jpg" controls playsinline preload="metadata" aria-label="Clip ${n}"></video>
        </div>
        <div class="meta"><strong>Clip ${n}</strong><span>${seconds} s · from ${clock(clip.start)}</span></div>
        <a class="btn btn-light btn-block" data-dl="${n}" href="/${slug}/clip_${nn}.mp4?dl=1" download="${downloadName(slug, `clip_${nn}.mp4`)}">Download video <span class="ext">.mp4</span></a>
        ${assets ? `<div class="assets">${assets}</div>` : ""}${clip.post ? `
        <details class="drop" data-drop="linkedin">
          <summary><span class="drop-label">LinkedIn post<small>Tap to view and copy</small></span>${CHEVRON}</summary>
          <div class="drop-body">
            <pre id="post${n}">${esc(String(clip.post).slice(0, 3000).trim())}</pre>
            <button type="button" class="copy" data-copy="${n}">Copy post</button>
          </div>
        </details>` : ""}${social ? `
        <details class="drop" data-drop="social">
          <summary><span class="drop-label">Shorts &amp; Reels caption<small>YouTube · Instagram · TikTok</small></span>${CHEVRON}</summary>
          <div class="drop-body">
            <div class="field"><span>Title</span><button type="button" class="copy copy-small" data-copy-id="rtitle${n}" data-event="social" data-n="${n}">Copy title</button></div>
            <p class="reel-title" id="rtitle${n}">${esc(social.title)}</p>
            <div class="field"><span>Description &amp; hashtags</span><button type="button" class="copy copy-small" data-copy-id="rcap${n}" data-event="social" data-n="${n}">Copy caption</button></div>
            <pre id="rcap${n}">${esc(social.description)}${social.tags ? `\n\n<span class="tags">${esc(social.tags)}</span>` : ""}</pre>
          </div>
        </details>` : ""}
      </article>`;
  }).join("");
  const anyPost = clips.some((c) => c.post), anySrt = clips.some((c) => c.srt), anyCover = clips.some((c) => c.cover);
  const anySocial = clips.some((c) => socialOf(c));
  const callHref = `mailto:${CONTACT_EMAIL}?subject=${encodeURIComponent(CALL_SUBJECT(channel))}&body=${encodeURIComponent(CALL_BODY(channel))}`;
  const steps = [
    "Download the video",
    anyPost || anySocial ? "Copy the post for each platform" : "",
    anySrt ? "Add the captions file" : "",
    anyCover ? "Set the cover image" : "",
  ].filter(Boolean).map((t) => `<li>${t}</li>`).join("");
  const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${count} clip${count === 1 ? "" : "s"} for ${esc(channel)} · ${AGENCY}</title>
<meta name="robots" content="noindex, nofollow">
<meta name="description" content="Vertical clips from ${esc(video)}, made by ${AGENCY}.">
<meta property="og:title" content="${count} clip${count === 1 ? "" : "s"} for ${esc(channel)}">
<meta property="og:description" content="Vertical clips with captions, LinkedIn posts and Shorts &amp; Reels captions, cut from ${esc(video)}. Made by ${AGENCY}.">
<meta property="og:image" content="${origin}/${slug}/poster_01.jpg">
<meta name="theme-color" content="#0B0C10">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Geist:wght@400..800&display=swap" rel="stylesheet">
<style>
  :root { --bg:#0B0C10; --panel:#0F1117; --panel-2:#12151D; --line:#1D2230; --line-2:#2A3040; --text:#F2F3F5; --muted:#9097A3;
          --dim:#646B78; --accent:#FFE600; --sans:"Geist", ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }
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
  header.intro { padding: 48px 0 28px; }
  .eyebrow { color: var(--muted); font-weight: 550; font-size: 13px; letter-spacing: 0.08em; text-transform: uppercase; margin: 0 0 14px; }
  h1 { font-size: clamp(30px, 4.6vw, 48px); font-weight: 680; letter-spacing: -0.04em; line-height: 1.04; margin: 0; text-wrap: balance; }
  .lead { color: var(--muted); font-size: 17px; max-width: 60ch; margin: 14px 0 0; }
  .lead q { color: var(--text); quotes: "“" "”"; }
  .facts { display: flex; flex-wrap: wrap; gap: 8px 18px; margin: 18px 0 0; padding: 0; list-style: none; color: var(--dim); font-size: 13px; }
  .facts li { display: inline-flex; align-items: center; gap: 7px; }
  .facts li::before { content: ""; width: 5px; height: 5px; border-radius: 50%; background: var(--line-2); }
  .theme { display: inline-block; margin: 16px 0 0; padding: 6px 12px; border: 1px solid var(--line-2); border-radius: 999px;
           color: var(--text); font-size: 14px; }
  .theme b { color: var(--accent); font-weight: 600; }
  .howto { display: flex; flex-wrap: wrap; gap: 8px; list-style: none; margin: 0 0 28px; padding: 0; counter-reset: s; }
  .howto li { counter-increment: s; display: flex; align-items: center; gap: 8px; color: var(--muted); font-size: 13px;
              border: 1px solid var(--line); border-radius: 999px; padding: 6px 12px 6px 6px; background: var(--panel); }
  .howto li::before { content: counter(s); display: inline-grid; place-items: center; width: 20px; height: 20px; border-radius: 50%;
                      background: var(--line-2); color: var(--text); font-size: 11px; font-weight: 650; }
  .clips { display: grid; grid-template-columns: repeat(${Math.max(1, Math.min(count, 3))}, minmax(0, 320px)); gap: 28px; align-items: start; }
  .card { margin: 0; display: flex; flex-direction: column; gap: 12px; }
  .clip { position: relative; aspect-ratio: 9 / 16; border-radius: 16px; overflow: hidden; background: #000; box-shadow: 0 0 0 1px var(--line-2); }
  .clip video { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; display: block; background: #000; }
  .meta { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin-top: 2px; }
  .meta strong { font-weight: 600; font-size: 15px; }
  .meta span { color: var(--dim); font-size: 13px; font-variant-numeric: tabular-nums; }
  .btn { display: inline-flex; align-items: center; justify-content: center; gap: 6px; font: 600 15px/1 var(--sans); text-decoration: none;
         padding: 14px 20px; border-radius: 999px; border: 1px solid transparent; white-space: nowrap; cursor: pointer; }
  .btn-primary { background: var(--accent); color: #0B0C10; }
  .btn-primary:hover { background: #FFF06B; }
  .btn-light { background: var(--text); color: #0B0C10; }
  .btn-light:hover { background: #FFFFFF; }
  .btn-ghost { color: var(--text); border-color: var(--line-2); background: transparent; }
  .btn-ghost:hover { border-color: #4A5266; background: #141721; }
  .btn-block { width: 100%; padding: 13px 16px; font-size: 14px; }
  .ext { font-weight: 500; opacity: 0.6; }
  .assets { display: grid; grid-template-columns: repeat(auto-fit, minmax(0, 1fr)); gap: 8px; }
  .asset { display: flex; align-items: center; justify-content: center; gap: 4px; text-decoration: none; font-size: 13px; font-weight: 550;
           padding: 10px 8px; border-radius: 12px; border: 1px solid var(--line-2); color: var(--text); white-space: nowrap; }
  .asset span { color: var(--dim); font-weight: 500; }
  .asset:hover { border-color: #4A5266; background: #141721; }
  .drop { border: 1px solid var(--line); border-radius: 14px; background: var(--panel); overflow: hidden; }
  .drop + .drop { margin-top: -4px; }
  .drop summary { list-style: none; cursor: pointer; display: flex; align-items: center; justify-content: space-between; gap: 10px;
                  padding: 11px 12px 11px 14px; user-select: none; }
  .drop summary::-webkit-details-marker { display: none; }
  .drop summary:hover { background: var(--panel-2); }
  .drop-label { display: flex; flex-direction: column; gap: 1px; font-weight: 600; font-size: 14px; }
  .drop-label small { color: var(--dim); font-weight: 450; font-size: 12px; }
  .chev { flex: 0 0 auto; width: 28px; height: 28px; display: grid; place-items: center; border-radius: 50%;
          border: 1px solid var(--line-2); color: var(--text); transition: transform .2s ease, background .2s ease; }
  .drop[open] .chev { transform: rotate(180deg); background: var(--line); }
  .drop-body { border-top: 1px solid var(--line); padding: 12px 14px 14px; display: flex; flex-direction: column; gap: 10px; }
  .drop pre { margin: 0; white-space: pre-wrap; font: 400 14px/1.55 var(--sans); color: var(--text); max-height: 280px; overflow: auto; }
  .drop .tags { color: #8FB4FF; }
  .field { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
  .field span { color: var(--muted); font-size: 12px; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; }
  .reel-title { margin: -4px 0 4px; font-weight: 600; font-size: 15px; line-height: 1.4; }
  .copy { align-self: flex-start; font: 600 13px/1 var(--sans); color: #0B0C10; background: var(--text); border: 0; border-radius: 999px;
          padding: 9px 16px; cursor: pointer; }
  .copy:hover { background: #FFFFFF; }
  .copy.done { background: var(--accent); }
  .copy-small { padding: 7px 12px; font-size: 12px; }
  .swipe { display: none; color: var(--dim); font-size: 13px; margin: 0 0 12px; }
  .rights { color: var(--muted); font-size: 14px; margin: 32px 0 0; }
  .cta { margin: 56px 0 0; padding: 32px; background: var(--panel); border: 1px solid var(--line); border-radius: 20px;
         display: flex; align-items: center; justify-content: space-between; gap: 24px; flex-wrap: wrap; }
  .cta h2 { font-size: clamp(22px, 3vw, 28px); font-weight: 650; letter-spacing: -0.03em; line-height: 1.15; margin: 0; max-width: 24ch; }
  .cta p { color: var(--muted); margin: 8px 0 0; font-size: 15px; }
  .cta .actions { display: flex; gap: 12px; flex-wrap: wrap; }
  footer { color: var(--dim); font-size: 13px; padding: 40px 0 48px; }
  footer .credit { margin-top: 8px; font-size: 12px; }
  @media (max-width: 760px) {
    header.intro { padding: 32px 0 22px; }
    .swipe { display: block; }
    .howto { flex-wrap: nowrap; overflow-x: auto; scrollbar-width: none; margin: 0 -24px 22px; padding: 0 24px; }
    .howto::-webkit-scrollbar { display: none; }
    .howto li { flex: 0 0 auto; }
    .clips { display: flex; overflow-x: auto; scroll-snap-type: x mandatory; gap: 16px; margin: 0 -24px; padding: 0 24px 8px; scrollbar-width: none; }
    .clips::-webkit-scrollbar { display: none; }
    .card { flex: 0 0 80vw; max-width: 330px; scroll-snap-align: center; }
    .cta { padding: 24px; }
    .cta .actions, .cta .btn { width: 100%; }
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
    <p class="eyebrow">Delivery for ${esc(channel)}</p>
    <h1>Your ${count} clip${count === 1 ? "" : "s"}, ready to post</h1>
    <p class="lead">${video ? `Cut from your ${kind} <q>${esc(video)}</q>. ` : ""}Vertical 9:16, captioned, no watermark.</p>
    <ul class="facts"><li>${count} clip${count === 1 ? "" : "s"}</li>${anyPost ? "<li>LinkedIn posts</li>" : ""}${anySocial ? "<li>Shorts &amp; Reels captions</li>" : ""}${anySrt ? "<li>Captions files</li>" : ""}${anyCover ? "<li>Cover images</li>" : ""}${deliveredText ? `<li>Delivered ${deliveredText}</li>` : ""}</ul>
    ${info.theme ? `<p class="theme"><b>Fits your theme</b> · ${esc(String(info.theme).slice(0, 80))}</p>` : ""}
  </header>
  <ol class="howto" aria-label="How to post each clip">${steps}</ol>
  ${count > 1 ? `<p class="swipe">Swipe for all ${count} →</p>` : ""}
  <section class="clips" aria-label="Your clips">${cards}
  </section>
  <p class="rights">Yours to post on LinkedIn, Shorts and Reels. No watermark, no strings attached.</p>
  <section class="cta">
    <div>
      <h2>Want this every week on your content calendar?</h2>
      <p>${CLIPS_PER_MONTH} clips a month, each with ${PER_CLIP}, delivered ${OFFER.turnaround}. Founding price ${OFFER.price}/${OFFER.per}, ${OFFER.spots} spots. Beat your usual posts or your next month is free.</p>
    </div>
    <div class="actions">
      <a class="btn btn-primary" data-offer href="/offer?from=${slug}">Claim your founding spot</a>
      <a class="btn btn-ghost" data-call href="${esc(callHref)}">Request a call</a>
    </div>
  </section>
</main>
<footer><div class="wrap">${SENDER} · ${AGENCY} · <a href="${SITE}">${SITE.replace(/^https?:\/\//, "")}</a> · <a href="mailto:${CONTACT_EMAIL}">${CONTACT_EMAIL}</a><div class="credit">Price charts: TradingView Lightweight Charts™, © TradingView, Inc. · <a href="https://www.tradingview.com/">tradingview.com</a></div></div></footer>
<script>
(() => {
  const page = ${JSON.stringify(slug)};
  let me = false;   // your own visits (opened from the app with ?me=1) are never counted
  try {
    if (new URLSearchParams(location.search).has("me")) localStorage.setItem("nav_me", "1");
    me = localStorage.getItem("nav_me") === "1";
  } catch (e) {}
  if (me) document.querySelectorAll("[data-offer]").forEach((a) => { a.href += "&me=1"; });
  if (me) { try { fetch("/_nav_owner?ping=1", { cache: "no-store" }); } catch (e) {} }  // keeps this network marked as yours
  const send = (t, c = 0) => { if (me) return; try { navigator.sendBeacon("/e", JSON.stringify({ s: page, t, c })); } catch (e) {} };
  // A view is a person looking: the page visible for a moment (not a link scanner's prefetch).
  let opened = false;
  const open = () => { if (!opened && document.visibilityState === "visible") { opened = true; setTimeout(() => send("open"), 800); } };
  open();
  document.addEventListener("visibilitychange", open);
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
  document.querySelectorAll("[data-asset]").forEach((a) => a.addEventListener("click", () => send(a.dataset.asset, Number(a.dataset.n))));
  document.querySelectorAll("[data-call]").forEach((a) => a.addEventListener("click", () => send("call")));
  const copyText = async (b, box, label, event, n) => {
    try { await navigator.clipboard.writeText(box.textContent); b.textContent = "Copied"; b.classList.add("done"); } catch (e) {
      const r = document.createRange(); r.selectNodeContents(box);
      const sel = getSelection(); sel.removeAllRanges(); sel.addRange(r); b.textContent = "Selected: press Copy";
    }
    send(event, n);
    setTimeout(() => { b.textContent = label; b.classList.remove("done"); }, 2200);
  };
  document.querySelectorAll("[data-copy-id]").forEach((b) => {
    const label = b.textContent;
    b.addEventListener("click", () => copyText(b, document.getElementById(b.dataset.copyId), label, b.dataset.event, Number(b.dataset.n)));
  });
  document.querySelectorAll("[data-copy]").forEach((b) => b.addEventListener("click", () =>
    copyText(b, document.getElementById("post" + b.dataset.copy), "Copy post", "copy", Number(b.dataset.copy))));
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
      ...SECURITY_HEADERS,
    },
  });
}

// ---------- Round 45: the offer page (/offer) ----------
function offerPage(url) {
  const from = String(url.searchParams.get("from") || "");
  const callHref = `mailto:${CONTACT_EMAIL}?subject=${encodeURIComponent("A call about LinkedIn clips")}`
    + `&body=${encodeURIComponent(CALL_BODY("our team").replace("We looked through the clips for our team. ", "We saw your offer. "))}`;
  const mailto = `mailto:${CONTACT_EMAIL}?subject=${encodeURIComponent(`LinkedIn clips: the ${OFFER.trial}`)}`
    + `&body=${encodeURIComponent("Hi Barry,\n\nWe'd like to try the LinkedIn clips.\n\nOur show / channel: \nWho speaks on it (names and titles): \nThis month's themes: \n")}`;
  const includes = [
    ["Executive captions", "Calm two-line captions in sentence case, made for the feed's sound-off viewers. No bouncing, all-caps creator styling."],
    ["Brand-safe by default", "The speaker stays full-screen. No stock footage, scraped images, third-party logos or meme graphics."],
    ["An opening that holds attention", "Each clip opens on its strongest line, then plays the moment in full, with smooth, subtle camera movement."],
    ["Name and title, only when it's right", "A clean lower-third ID that appears only when we've confirmed who is on screen."],
    ["A LinkedIn post for every clip", "A hook, three takeaways in the speaker's own words and a closing question that invites comments. Nothing invented."],
    ["A Shorts & Reels caption for every clip", "A title, a description of the episode and the moment, and a handful of fitting hashtags, ready for YouTube Shorts, Instagram Reels and TikTok."],
    ["Ready to publish anywhere", "Vertical 9:16 for LinkedIn, YouTube Shorts, Instagram Reels and TikTok, with a captions file (.srt) and a cover image for every clip."],
  ].map(([h, p]) => `<div class="item"><h3>${h}</h3><p>${p}</p></div>`).join("");
  const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LinkedIn short-form for B2B teams · ${AGENCY}</title>
<meta name="description" content="Turnkey LinkedIn and multi-platform short-form video, aligned to your monthly content calendar. ${CLIPS_PER_MONTH} clips a month, each with a LinkedIn post and a Shorts &amp; Reels caption.">
<meta property="og:title" content="Turnkey LinkedIn short-form video · ${AGENCY}">
<meta property="og:description" content="Clips from your podcast, webinars and executive talks, captioned for sound-off and aligned to your content calendar.">
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
  .eyebrow { color: var(--accent); font-weight: 600; font-size: 14px; letter-spacing: 0.02em; margin: 0 0 14px; }
  .hero { padding: 72px 0 40px; }
  h1 { font-size: clamp(34px, 5.4vw, 60px); font-weight: 700; letter-spacing: -0.045em; line-height: 1.02; margin: 0; max-width: 18ch; text-wrap: balance; }
  .lead { color: var(--muted); font-size: 19px; max-width: 58ch; margin: 20px 0 0; }
  .actions { display: flex; gap: 12px; flex-wrap: wrap; margin-top: 28px; }
  .btn { display: inline-flex; align-items: center; justify-content: center; font: 600 15px/1 var(--sans); text-decoration: none;
         padding: 14px 20px; border-radius: 999px; border: 1px solid transparent; white-space: nowrap; }
  .btn-primary { background: var(--accent); color: #0B0C10; }
  .btn-primary:hover { background: #FFF06B; }
  .btn-ghost { color: var(--text); border-color: var(--line-2); }
  .btn-ghost:hover { border-color: #4A5266; background: #141721; }
  .facts { display: grid; grid-template-columns: repeat(4, 1fr); border: 1px solid var(--line); border-radius: 20px; background: var(--panel); margin: 24px 0 0; }
  .fact { padding: 22px 24px; border-left: 1px solid var(--line); }
  .fact:first-child { border-left: 0; }
  .fact b { display: block; font-size: 30px; font-weight: 700; letter-spacing: -0.04em; line-height: 1.1; font-variant-numeric: tabular-nums; }
  .fact span { color: var(--muted); font-size: 14px; }
  section { padding: 64px 0 0; }
  h2 { font-size: clamp(24px, 3.2vw, 34px); font-weight: 680; letter-spacing: -0.035em; line-height: 1.12; margin: 0 0 8px; max-width: 26ch; }
  .sub { color: var(--muted); margin: 0 0 28px; max-width: 60ch; }
  .grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; }
  .item { border: 1px solid var(--line); border-radius: 18px; padding: 22px; background: var(--panel); }
  .item h3 { margin: 0 0 6px; font-size: 17px; font-weight: 640; letter-spacing: -0.02em; }
  .item p { margin: 0; color: var(--muted); font-size: 15px; }
  .steps { list-style: none; padding: 0; margin: 0; display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; counter-reset: s; }
  .steps li { counter-increment: s; border-top: 2px solid var(--line-2); padding: 18px 4px 0; color: var(--muted); }
  .steps li::before { content: counter(s, decimal-leading-zero); display: block; color: var(--accent); font-weight: 650; font-size: 14px; margin-bottom: 8px; }
  .steps strong { color: var(--text); display: block; font-weight: 620; margin-bottom: 4px; }
  .price { display: grid; grid-template-columns: 1.1fr 1fr; gap: 24px; border: 1px solid var(--line-2); border-radius: 24px; padding: 32px; background: var(--panel); }
  .amount { font-size: clamp(44px, 6vw, 64px); font-weight: 720; letter-spacing: -0.05em; line-height: 1; font-variant-numeric: tabular-nums; }
  .amount small { font-size: 18px; color: var(--muted); font-weight: 500; letter-spacing: 0; }
  .tag { display: inline-block; font-size: 13px; font-weight: 600; color: #0B0C10; background: var(--accent); border-radius: 999px; padding: 4px 10px; margin-bottom: 14px; }
  .price ul { margin: 0; padding-left: 18px; color: var(--muted); }
  .plans { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; }
  .plan { border: 1px solid var(--line); border-radius: 22px; padding: 26px; background: var(--panel); display: flex; flex-direction: column; }
  .plan-featured { border-color: var(--accent); }
  .plan h3 { margin: 0 0 10px; font-size: 18px; font-weight: 650; letter-spacing: -0.02em; }
  .plan .amount { font-size: clamp(38px, 4.6vw, 52px); }
  .plan p { color: var(--muted); font-size: 15px; margin: 14px 0 0; flex: 1; }
  .plan .tag { align-self: flex-start; }
  .guarantee { margin-top: 16px; border: 1px dashed var(--line-2); border-radius: 16px; padding: 16px 20px; color: var(--muted); }
  .guarantee b { color: var(--text); }
  .price li { margin: 6px 0; }
  .price li b { color: var(--text); font-weight: 600; }
  .faq { display: grid; grid-template-columns: repeat(3, 1fr); gap: 24px; }
  .faq h3 { margin: 0 0 6px; font-size: 16px; font-weight: 620; }
  .faq p { margin: 0; color: var(--muted); font-size: 15px; }
  footer { color: var(--dim); font-size: 13px; padding: 64px 0 48px; }
  @media (max-width: 860px) {
    .hero { padding: 44px 0 24px; }
    .facts { grid-template-columns: repeat(2, 1fr); }
    .fact:nth-child(3) { border-left: 0; }
    .fact:nth-child(n+3) { border-top: 1px solid var(--line); }
    .grid, .steps, .faq { grid-template-columns: 1fr; }
    .price { grid-template-columns: 1fr; padding: 24px; }
    .plans { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>
<nav class="nav"><div class="wrap">
  <a class="brand" href="${SITE}"><span class="brand-mark" aria-hidden="true"></span>${AGENCY}</a>
  <a class="site" href="${SITE}">${SITE.replace(/^https?:\/\//, "")}</a>
</div></nav>
<main class="wrap">
  <header class="hero">
    <p class="eyebrow">For B2B companies, executive podcasts and business brands</p>
    <h1>Turnkey LinkedIn &amp; multi-platform short-form video, aligned to your monthly content calendar.</h1>
    <p class="lead">Send us your podcast, webinar or executive talk. You get vertical clips cut from it, captioned for sound-off viewing and written up as ready-to-post LinkedIn copy, on your themes and in your brand.</p>
    <div class="actions">
      <a class="btn btn-primary" href="${mailto}">Start with a ${OFFER.trial}</a>
      <a class="btn btn-ghost" href="${esc(callHref)}">Request a call</a>
    </div>
    <div class="facts" role="list">
      <div class="fact" role="listitem"><b>${CLIPS_PER_MONTH}</b><span>clips a month: ${OFFER.clipsPerEpisode} from each of up to ${OFFER.episodesPerMonth} recordings</span></div>
      <div class="fact" role="listitem"><b>2</b><span>posts written for every clip: ${PER_CLIP}</span></div>
      <div class="fact" role="listitem"><b>24 h</b><span>delivery ${OFFER.turnaround} of your recording</span></div>
      <div class="fact" role="listitem"><b>${OFFER.price}</b><span>a ${OFFER.per}, month to month</span></div>
    </div>
  </header>

  <section id="included">
    <h2>What every clip includes</h2>
    <p class="sub">Made for a company's LinkedIn feed: professional, accurate and consistent with your brand.</p>
    <div class="grid">${includes}</div>
  </section>

  <section>
    <h2>Aligned to your content calendar</h2>
    <p class="sub">Tell us the month's themes: a product launch, a hiring push, a quarter's results. We pick the moments that fit each theme and deliver them grouped by week, so your team posts on plan instead of hunting for clips.</p>
    <ol class="steps">
      <li><strong>Share the month's themes</strong>And your brand basics: accent colour, speaker names and titles.</li>
      <li><strong>Send each new recording</strong>A file or a link. Podcasts, webinars, panels and talks, with slides or without.</li>
      <li><strong>Post on schedule</strong>Clips, posts and captions arrive ${OFFER.turnaround}, grouped by theme. Ask for any change and we re-cut it.</li>
    </ol>
  </section>

  <section>
    <h2>Accurate enough for serious subjects</h2>
    <p class="sub">Built for finance, legal, venture and B2B conversations. Every clip runs as long as the point takes, 35 seconds to 3 minutes: it opens where the idea starts and ends when the speaker has landed it, never mid-thought. Figures and names appear exactly as they were said, and nothing is added that the speaker didn't say.</p>
  </section>

  <section id="pricing">
    <h2>Plans</h2>
    <p class="sub">Month to month, no contract. Every plan: ${CLIPS_PER_MONTH} vertical clips a month per executive (${OFFER.clipsPerEpisode} from each of up to ${OFFER.episodesPerMonth} recordings), ${PER_CLIP} for every clip, executive captions, a captions file and a cover image, delivered ${OFFER.turnaround}.</p>
    <div class="plans">
      <div class="plan plan-featured">
        <span class="tag">Founding clients: ${OFFER.spots} spots</span>
        <h3>Founding</h3>
        <div class="amount">${OFFER.price}<small> / ${OFFER.per}</small></div>
        <p>One executive. The full service at the founding price, locked in while you stay.</p>
        <div class="actions"><a class="btn btn-primary" href="${mailto}">Start the ${OFFER.trial}</a></div>
      </div>
      <div class="plan">
        <h3>Executive</h3>
        <div class="amount">${OFFER.executivePrice}<small> / ${OFFER.per}</small></div>
        <p>One executive: your founder, CEO or the voice of your show. Grouped by your monthly themes.</p>
        <div class="actions"><a class="btn btn-ghost" href="${mailto}">Start the ${OFFER.trial}</a></div>
      </div>
      <div class="plan">
        <h3>Team</h3>
        <div class="amount">${OFFER.teamPrice}<small> / ${OFFER.per}</small></div>
        <p>${OFFER.teamSize}, ${CLIPS_PER_MONTH} clips each, on one shared content calendar.</p>
        <div class="actions"><a class="btn btn-ghost" href="${esc(callHref)}">Request a call</a></div>
      </div>
    </div>
    <div class="guarantee"><b>Our guarantee.</b> ${OFFER.guarantee}</div>
  </section>

  <section>
    <div class="faq">
      <div><h3>Who owns the clips?</h3><p>You do. No watermark, and they're yours to post anywhere.</p></div>
      <div><h3>Do we need to change how we record?</h3><p>No. We work from the footage you already make, camera shots and slides alike.</p></div>
      <div><h3>What if a clip isn't right?</h3><p>Tell us what to change and we re-cut it.</p></div>
      <div><h3>What's the guarantee?</h3><p>${OFFER.guarantee} Compare them with your own LinkedIn analytics.</p></div>
      <div><h3>Do you work with agencies?</h3><p>Yes. We cut clips white-label for podcast, content and LinkedIn agencies, under your brand. <a href="mailto:${CONTACT_EMAIL}?subject=${encodeURIComponent("White-label clips for our clients")}">Email us</a>.</p></div>
      <div><h3>Can we start small?</h3><p>Yes: start with a ${OFFER.trial}, month to month after that.</p></div>
    </div>
  </section>
</main>
<footer><div class="wrap">${SENDER} · ${AGENCY} · <a href="mailto:${CONTACT_EMAIL}">${CONTACT_EMAIL}</a></div></footer>
${PAGE.test(from) ? `<script>try { if (!url_me() && localStorage.getItem("nav_me") !== "1") navigator.sendBeacon("/e", JSON.stringify({ s: ${JSON.stringify(from)}, t: "offer", c: 0 })); } catch (e) {} function url_me() { return new URLSearchParams(location.search).has("me"); }</script>` : ""}
</body>
</html>`;
  return new Response(html, {
    headers: { "content-type": "text/html; charset=utf-8", "cache-control": "public, max-age=300", "referrer-policy": "no-referrer",
               ...SECURITY_HEADERS },
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
    { status: 404, headers: { "content-type": "text/html; charset=utf-8", "x-robots-tag": "noindex", ...SECURITY_HEADERS } },
  );
}
