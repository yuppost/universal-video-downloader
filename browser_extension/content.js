// Universal Video Studio - shows a small "Download" pill on the video under the mouse.
(() => {
  if (window.__uvsLoaded) return;
  window.__uvsLoaded = true;

  const MIN_W = 320, MIN_H = 180;
  const HIDE_AFTER_MS = 2600;
  let settings = { hoverOnly: true };
  try { chrome.storage.sync.get({ hoverOnly: true }, (s) => { settings = s || settings; }); } catch (e) {}

  const host = location.hostname.replace(/^www\./, "");
  const isYouTube = /(^|\.)youtube\.com$/.test(host) || host === "youtu.be";

  // ---- the button (Shadow DOM: the page's CSS cannot break it) -----------------------------------
  const root = document.createElement("div");
  root.style.cssText = "all:initial;position:fixed;z-index:2147483647;left:0;top:0;pointer-events:none;";
  const shadow = root.attachShadow({ mode: "closed" });
  shadow.innerHTML = `
    <style>
      .pill{position:fixed;pointer-events:auto;display:none;align-items:center;gap:6px;height:28px;padding:0 12px 0 9px;
        border-radius:14px;background:linear-gradient(135deg,#2dd4ff,#4f7cff);color:#fff;font:600 12px/1 system-ui,Segoe UI,Arial,sans-serif;
        cursor:pointer;user-select:none;box-shadow:0 2px 10px rgba(0,0,0,.45);opacity:0;transition:opacity .18s;white-space:nowrap}
      .pill.show{display:flex;opacity:.96}
      .pill:hover{filter:brightness(1.08)}
      .pill.ok{background:#1f9d55}.pill.err{background:#c0392b}
      svg{width:14px;height:14px;fill:none;stroke:#fff;stroke-width:2.4;stroke-linecap:round;stroke-linejoin:round}
    </style>
    <div class="pill" id="b"><svg viewBox="0 0 24 24"><path d="M12 4v11m0 0l-4.5-4.5M12 15l4.5-4.5M5 19h14"/></svg><span id="t">Download</span></div>`;
  const pill = shadow.getElementById("b");
  const label = shadow.getElementById("t");
  (document.documentElement || document.body).appendChild(root);

  let current = null, hideTimer = 0, busy = false;

  function bigVideos() {
    return Array.from(document.getElementsByTagName("video")).filter((v) => {
      const r = v.getBoundingClientRect();
      return r.width >= MIN_W && r.height >= MIN_H && r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth
        && getComputedStyle(v).visibility !== "hidden";
    });
  }

  function onWatchPage() {
    if (!isYouTube) return true;
    return /^\/(watch|shorts|live|embed)\b/.test(location.pathname);
  }

  function place(video) {
    const r = video.getBoundingClientRect();
    pill.style.left = Math.max(4, Math.min(innerWidth - 120, r.right - 124)) + "px";
    pill.style.top = Math.max(4, r.top + 10) + "px";
  }

  function show(video) {
    if (!onWatchPage()) return hide();
    current = video;
    place(video);
    pill.classList.add("show");
    clearTimeout(hideTimer);
    if (settings.hoverOnly) hideTimer = setTimeout(hide, HIDE_AFTER_MS);
  }
  function hide() { pill.classList.remove("show"); }

  document.addEventListener("mousemove", (e) => {
    if (busy) return;
    const path = e.composedPath ? e.composedPath() : [];
    if (path.includes(root)) { clearTimeout(hideTimer); return; }
    const hit = bigVideos().find((v) => { const r = v.getBoundingClientRect(); return e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom; });
    if (hit) show(hit); else if (settings.hoverOnly) hide();
  }, { passive: true, capture: true });

  addEventListener("scroll", () => { if (current && pill.classList.contains("show")) place(current); }, { passive: true, capture: true });
  addEventListener("resize", () => { if (current) place(current); });

  // ---- which URL to send -------------------------------------------------------------------------
  // Feeds (Facebook, Instagram, X, TikTok, Reddit) show many videos on ONE page, so the address bar is not the video.
  // For these sites: if the address already points at one post/video use it, else find the permalink of the post
  // that contains the clicked video.
  const SITES = [
    { host: /(^|\.)(facebook\.com|fb\.watch)$/,
      page: /^\/(watch\/?|reel\/|[^/]+\/videos\/|videos\/|share\/(v|r|p)\/|[^/]+\/posts\/|groups\/[^/]+\/(posts|permalink)\/|permalink\.php|story\.php|photo)/,
      pageQuery: /(^|[?&])v=\d+/,
      container: ['[role="article"]', '[data-pagelet^="FeedUnit"]', '[data-pagelet*="Video"]'],
      links: ['a[href*="/reel/"]', 'a[href*="/videos/"]', 'a[href*="/watch/?v="]', 'a[href*="/watch?v="]', 'a[href*="/share/v/"]',
              'a[href*="/share/r/"]', 'a[href*="/posts/"]', 'a[href*="permalink.php"]', 'a[href*="story_fbid"]', 'a[href*="/permalink/"]'] },
    { host: /(^|\.)instagram\.com$/, page: /^\/(p|reel|reels|tv)\/[\w-]+/,
      container: ['article', 'main'], links: ['a[href*="/reel/"]', 'a[href*="/p/"]', 'a[href*="/tv/"]'] },
    { host: /(^|\.)(x|twitter)\.com$/, page: /^\/[^/]+\/status\/\d+/,
      container: ['article'], links: ['a[href*="/status/"]:has(time)', 'a[href*="/status/"]'] },
    { host: /(^|\.)tiktok\.com$/, page: /^\/@[^/]*\/video\/\d+|^\/(embed|player)\/(v\d\/)?\d+/, tiktok: true,
      container: ['[data-e2e="recommend-list-item-container"]', '[data-e2e="user-post-item"]', '[data-e2e="search_top-item"]',
                  '[data-e2e="browse-video"]', 'article', 'div[class*="DivItemContainer"]', 'div[class*="DivVideoCard"]'],
      links: ['a[href*="/video/"]'] },
    { host: /(^|\.)reddit\.com$/, page: /^\/r\/[^/]+\/comments\//,
      container: ['shreddit-post', 'article', '[data-testid="post-container"]'], links: ['a[href*="/comments/"]'] },
    { host: /(^|\.)twitch\.tv$/, page: /^\/(videos\/\d+|[^/]+\/(clip|video)\/[\w-]+|clip\/[\w-]+)/,
      container: ['article', '[data-a-target="clips-card"]', 'main'], links: ['a[href*="/clip/"]', 'a[href*="/videos/"]'] },
    { host: /(^|\.)dailymotion\.com$/, page: /^\/(video|embed\/video)\/[\w]+/,
      container: ['article', '[class*="VideoCard"]'], links: ['a[href*="/video/"]'] },
    { host: /(^|\.)vimeo\.com$/, page: /^\/(\d+|[^/]+\/[^/]+\/\d+|channels\/[^/]+\/\d+|groups\/[^/]+\/videos\/\d+)/,
      container: ['article', 'li'], links: ['a[href*="vimeo.com/"][href*="/"]'] },
    { host: /(^|\.)bilibili\.com$/, page: /^\/(video|bangumi\/play)\//, container: ['.bili-video-card', 'article'], links: ['a[href*="/video/"]'] },
    { host: /(^|\.)(rumble\.com|streamable\.com|ok\.ru|vk\.com)$/, page: /^\/(v[\w-]+|[\w]{4,8}$|video[\w-]*|clip\/)/i,
      container: ['article', 'li'], links: ['a[href*="/video"]', 'a[href*="/v"]'] },
  ];

  // TikTok's feed page keeps the video id in the player's element id ("xgwrapper-<n>-<id>") and its data attributes.
  function tiktokUrl(video, box) {
    let id = "", el = video;
    for (let i = 0; i < 12 && el && !id; i++, el = el.parentElement) {
      const hay = [el.id || "", ...Array.from(el.attributes || []).filter((a) => a.name.startsWith("data-")).map((a) => a.value)].join(" ");
      const m = hay.match(/(?:^|[^\d])(\d{17,20})(?!\d)/);
      if (m) id = m[1];
    }
    if (!id) return "";
    let user = "";
    for (const scope of [box, video.closest("article"), video.parentElement && video.parentElement.parentElement].filter(Boolean)) {
      const a = scope.querySelector('a[href^="/@"], a[href*="tiktok.com/@"]');
      if (a) { const m = (a.getAttribute("href") || "").match(/\/@([\w.\-]+)/); if (m) { user = m[1]; break; } }
      const t = scope.querySelector('[data-e2e="video-author-uniqueid"],[data-e2e="browse-username"]');
      if (t && t.textContent.trim()) { user = t.textContent.trim().replace(/^@/, ""); break; }
    }
    return `https://www.tiktok.com/@${user}/video/${id}`;
  }

  function abs(href) { try { return new URL(href, location.href).href; } catch (e) { return ""; } }
  function ok(u) { return /^https?:\/\//.test(u); }

  function siteUrl(video) {
    const site = SITES.find((s) => s.host.test(host));
    if (!site) return "";
    if (site.page.test(location.pathname) || (site.pageQuery && site.pageQuery.test(location.search))) return location.href;
    // the post that contains this video
    const boxes = [];
    for (const sel of site.container) { const c = video.closest(sel); if (c) boxes.push(c); }
    let el = video.parentElement;
    for (let i = 0; i < 14 && el; i++, el = el.parentElement) boxes.push(el);   // fallback: walk up
    const permalink = video.closest("shreddit-post");
    if (permalink && permalink.getAttribute("permalink")) return abs(permalink.getAttribute("permalink"));
    if (site.tiktok) {
      const t = tiktokUrl(video, boxes[0]);
      if (t) return t;
    }
    for (const box of boxes) {
      for (const sel of site.links) {
        let a = null;
        try { a = box.querySelector(sel); } catch (e) { /* :has unsupported */ }
        if (a && ok(abs(a.getAttribute("href")))) return abs(a.getAttribute("href"));
      }
    }
    return "";
  }

  // Any other website: the address if it already names one video, else a permalink next to the player, else the file.
  const SPECIFIC_ADDR = /(\/(watch|video|videos|v|clip|clips|reel|reels|shorts|embed|status|p|tv|view|play|movie|episode)\b|[?&](v|video|vid|id)=|\/\d{6,}|\/[\w-]{8,}$)/i;
  const PERMALINKS = ['a[href*="/watch"]', 'a[href*="/video/"]', 'a[href*="/videos/"]', 'a[href*="/v/"]', 'a[href*="/clip"]', 'a[href*="/reel"]',
                      'a[href*="/shorts/"]', 'a[href*="/status/"]', 'a[href*="/embed/"]', 'a[href*="?v="]', 'a[href*="/post/"]', 'a[href*="/posts/"]'];
  function genericUrl(video) {
    if (location.pathname.length > 1 && SPECIFIC_ADDR.test(location.pathname + location.search)) return location.href;
    const own = video.closest('article, [role="article"], li, [class*="card" i], [class*="item" i]');
    const boxes = own ? [own] : [];
    let el = video.parentElement;
    for (let i = 0; i < 8 && el; i++, el = el.parentElement) boxes.push(el);
    for (const box of boxes) {
      for (const sel of PERMALINKS) {
        const a = box.querySelector(sel);
        if (a && ok(abs(a.getAttribute("href"))) && abs(a.getAttribute("href")) !== location.href) return abs(a.getAttribute("href"));
      }
    }
    return "";
  }

  function bestUrl(video) {
    const fromSite = siteUrl(video);
    if (fromSite) return fromSite;
    const known = SITES.some((s) => s.host.test(host));
    const src = video.currentSrc || video.src || "";
    const direct = /^https?:/.test(src) && /\.(mp4|webm|m3u8|mpd|mov|mkv)(\?|$)/i.test(src);
    if (!isYouTube) {
      const g = genericUrl(video);
      if (g) return g;
      if (direct && !known) return src;             // a plain file on an ordinary website: the file itself downloads best
    }
    return location.href;
  }

  function flash(cls, text) {
    pill.classList.remove("ok", "err");
    if (cls) pill.classList.add(cls);
    label.textContent = text;
    setTimeout(() => { pill.classList.remove("ok", "err"); label.textContent = "Download"; busy = false; if (settings.hoverOnly) hideTimer = setTimeout(hide, 1200); }, 2200);
  }

  pill.addEventListener("click", (e) => {
    e.preventDefault(); e.stopPropagation();
    if (busy || !current) return;
    busy = true; clearTimeout(hideTimer);
    label.textContent = "Sending...";
    let done = false;
    const fail = () => { if (!done) { done = true; flash("err", "App not connected"); } };
    const timer = setTimeout(fail, 8000);
    try {
      chrome.runtime.sendMessage({ type: "open", url: bestUrl(current) }, (reply) => {
        clearTimeout(timer);
        if (done) return;
        done = true;
        if (chrome.runtime.lastError || !reply || !reply.ok) flash("err", "App not connected");
        else flash("ok", reply.started ? "Opening app..." : "Sent ✓");
      });
    } catch (err) { clearTimeout(timer); fail(); }
  }, true);
})();
