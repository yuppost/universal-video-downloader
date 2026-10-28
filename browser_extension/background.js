// Universal Video Studio - service worker. Sends links to the desktop app through the native messaging host.
const HOST = "com.universalvideostudio.host";

function sendToApp(url) {
  return new Promise((resolve) => {
    try {
      chrome.runtime.sendNativeMessage(HOST, { type: "open", url }, (reply) => {
        if (chrome.runtime.lastError) {
          resolve({ ok: false, error: "not-connected", detail: chrome.runtime.lastError.message });
        } else {
          resolve(reply || { ok: false, error: "no-reply" });
        }
      });
    } catch (e) {
      resolve({ ok: false, error: "not-connected", detail: String(e) });
    }
  });
}

function ping() {
  return new Promise((resolve) => {
    chrome.runtime.sendNativeMessage(HOST, { type: "ping" }, (reply) => {
      resolve(!chrome.runtime.lastError && !!reply && reply.ok === true);
    });
  });
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg && msg.type === "open" && typeof msg.url === "string") {
    sendToApp(msg.url).then(sendResponse);
    return true; // async answer
  }
  if (msg && msg.type === "ping") {
    ping().then((ok) => sendResponse({ ok }));
    return true;
  }
  return false;
});

// Right-click menu: works on every site, for the page or for a link.
chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({ id: "uvs-page", title: "Download this page with Universal Video Studio", contexts: ["page", "video"] });
  chrome.contextMenus.create({ id: "uvs-link", title: "Download this link with Universal Video Studio", contexts: ["link"] });
  restoreAllSites();
});
chrome.runtime.onStartup.addListener(restoreAllSites);

chrome.contextMenus.onClicked.addListener((info, tab) => {
  const url = info.menuItemId === "uvs-link" ? info.linkUrl : (info.srcUrl && /^https?:/.test(info.srcUrl) ? info.srcUrl : (info.pageUrl || (tab && tab.url)));
  if (url) sendToApp(url).then((r) => badge(r.ok, tab && tab.id));
});

function badge(ok, tabId) {
  if (tabId === undefined) return;
  chrome.action.setBadgeBackgroundColor({ color: ok ? "#1f9d55" : "#c0392b", tabId });
  chrome.action.setBadgeText({ text: ok ? "OK" : "!", tabId });
  setTimeout(() => chrome.action.setBadgeText({ text: "", tabId }), 2500);
}

// "Show the button on every website" (optional permission, set on the options page).
async function restoreAllSites() {
  const { allSites } = await chrome.storage.sync.get({ allSites: false });
  try {
    const existing = await chrome.scripting.getRegisteredContentScripts({ ids: ["uvs-all"] });
    if (!allSites) {
      if (existing.length) await chrome.scripting.unregisterContentScripts({ ids: ["uvs-all"] });
      return;
    }
    const granted = await chrome.permissions.contains({ origins: ["http://*/*", "https://*/*"] });
    if (granted && !existing.length) {
      await chrome.scripting.registerContentScripts([{
        id: "uvs-all", matches: ["http://*/*", "https://*/*"], js: ["content.js"], allFrames: true, runAt: "document_idle", persistAcrossSessions: true,
      }]);
    }
  } catch (e) { /* not fatal */ }
}
chrome.storage.onChanged.addListener((ch) => { if (ch.allSites) restoreAllSites(); });
