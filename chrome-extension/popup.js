const listEl = document.getElementById("list");
const statusEl = document.getElementById("status");
const portInput = document.getElementById("portInput");
const connBadge = document.getElementById("connBadge");

let currentPort = 8765;

async function checkConnection(port) {
  connBadge.textContent = "Checking...";
  connBadge.className = "conn-err";
  try {
    const res = await fetch(`http://127.0.0.1:${port}/`, { signal: AbortSignal.timeout(1000) });
    const data = await res.json();
    if (data.app === "M3U8Downloader") {
      connBadge.textContent = `Connected (port ${port})`;
      connBadge.className = "conn-ok";
      return;
    }
    connBadge.textContent = `Port ${port} used by another app`;
  } catch {
    connBadge.textContent = `Not connected (check app/port)`;
  }
}

portInput.addEventListener("change", () => {
  const val = parseInt(portInput.value, 10);
  if (val >= 1024 && val <= 65535) {
    currentPort = val;
    chrome.storage.local.set({ port: val });
    checkConnection(val);
  }
});

function render() {
  chrome.storage.local.get({ captures: [], port: 8765 }, ({ captures, port }) => {
    currentPort = port;
    portInput.value = port;
    checkConnection(port);

    listEl.innerHTML = "";
    if (!captures.length) {
      listEl.innerHTML = '<div class="status">No .m3u8 streams captured yet. Play a video in Chrome first.</div>';
      return;
    }

    for (const item of captures) {
      const div = document.createElement("div");
      div.className = "item";

      const urlDiv = document.createElement("div");
      urlDiv.className = "url";
      urlDiv.textContent = item.url;

      const btn = document.createElement("button");
      btn.className = "send";
      btn.textContent = "Send to App";
      btn.onclick = async () => {
        statusEl.textContent = "Sending...";
        try {
          const res = await fetch(`http://127.0.0.1:${currentPort}/capture`, {
            method: "POST",
            headers: { "Content-Type": "text/plain" },
            body: JSON.stringify({ url: item.url, headers: item.headers }),
            signal: AbortSignal.timeout(3000),
          });
          statusEl.textContent = res.ok ? "Sent to M3U8 Downloader!" : `Error: HTTP ${res.status}`;
        } catch (err) {
          statusEl.textContent = `Failed: ${err.message}`;
        }
      };

      div.append(urlDiv, btn);
      listEl.appendChild(div);
    }
  });
}

document.getElementById("clearBtn").onclick = () => {
  chrome.storage.local.set({ captures: [] }, render);
};

render();
