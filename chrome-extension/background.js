const WANTED_HEADERS = new Set(["referer", "user-agent", "cookie", "origin"]);

chrome.webRequest.onBeforeSendHeaders.addListener(
  (details) => {
    if (!details.url.toLowerCase().includes(".m3u8")) return;

    const headers = {};
    for (const h of details.requestHeaders || []) {
      if (WANTED_HEADERS.has(h.name.toLowerCase()) && h.value) {
        headers[h.name] = h.value;
      }
    }

    chrome.storage.local.get({ captures: [] }, ({ captures }) => {
      const filtered = captures.filter((c) => c.url !== details.url);
      filtered.unshift({ url: details.url, headers, tabId: details.tabId, time: Date.now() });
      chrome.storage.local.set({ captures: filtered.slice(0, 15) });
    });

    if (details.tabId >= 0) {
      chrome.action.setBadgeText({ text: "M3U8", tabId: details.tabId });
      chrome.action.setBadgeBackgroundColor({ color: "#0969da", tabId: details.tabId });
    }
  },
  { urls: ["<all_urls>"] },
  ["requestHeaders", "extraHeaders"]
);
