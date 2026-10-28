const $ = (id) => document.getElementById(id);
const say = (t) => { $("msg").textContent = t; setTimeout(() => $("msg").textContent = "", 2500); };
chrome.storage.sync.get({ hoverOnly: true, allSites: false }, (s) => { $("hover").checked = s.hoverOnly; $("all").checked = s.allSites; });
$("hover").onchange = () => chrome.storage.sync.set({ hoverOnly: $("hover").checked }, () => say("Saved"));
$("all").onchange = async () => {
  if ($("all").checked) {
    const ok = await chrome.permissions.request({ origins: ["http://*/*", "https://*/*"] });
    if (!ok) { $("all").checked = false; return say("Permission was not given"); }
  } else {
    await chrome.permissions.remove({ origins: ["http://*/*", "https://*/*"] });
  }
  chrome.storage.sync.set({ allSites: $("all").checked }, () => say("Saved - reload open pages"));
};
