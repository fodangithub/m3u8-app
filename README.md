# M3U8 Downloader (Windows + Chrome Extension)

## Folder Structure

- app.py — Main Windows desktop app (proxy, multi-video queue, quality selector, AES-128 decryption, 2x auto-retry, visual piece grid, local Chrome receiver)
- requirements.txt — Python dependencies (requests, cryptography, pyinstaller)
- chrome-extension/ — Unpacked Chrome extension to capture .m3u8 URLs and HTTP headers (manifest.json, background.js, popup.html, popup.js)

## Quick Windows Setup

1. Install Python (check Add python.exe to PATH) and restart your terminal.
2. Install FFmpeg: winget install Gyan.FFmpeg
3. Install Python libraries inside the m3u8-app folder: pip install -r requirements.txt
4. Run the app: python app.py (Optional: build a standalone .exe with pyinstaller --onefile --windowed --name M3U8Downloader app.py)
5. Load the Chrome extension:
	- Open chrome://extensions/
	- Enable Developer mode
	- Click Load unpacked and select the chrome-extension folder