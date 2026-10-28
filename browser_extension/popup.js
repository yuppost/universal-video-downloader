const $ = (id) => document.getElementById(id);
let tabUrl = "";
chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
  tabUrl = (tabs[0] && tabs[0].url) || "";
  $("u").textContent = tabUrl || "No page";
  if (!/^https?:/.test(tabUrl)) $("go").disabled = true;
});
chrome.runtime.sendMessage({ type: "ping" }, (r) => {
  const ok = !chrome.runtime.lastError && r && r.ok;
  $("st").className = ok ? "ok" : "bad";
  $("st").textContent = ok ? "Connected to the app." : "The app is not connected. Install Universal Video Studio and start it once, then reload this page.";
});
$("go").onclick = () => {
  $("go").disabled = true;
  chrome.runtime.sendMessage({ type: "open", url: tabUrl }, (r) => {
    const ok = !chrome.runtime.lastError && r && r.ok;
    $("st").className = ok ? "ok" : "bad";
    $("st").textContent = ok ? (r.started ? "Opening the app..." : "Sent to the app.") : "Could not reach the app.";
    setTimeout(() => $("go").disabled = false, 1500);
  });
};
$("opt").onclick = (e) => { e.preventDefault(); chrome.runtime.openOptionsPage(); };
