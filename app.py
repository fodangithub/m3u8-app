from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from urllib.parse import urljoin
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7
import requests

CELL_SIZE = 11
GAP = 3
COLS = 34
MAX_WORKERS_PER_JOB = 4
MAX_CONCURRENT_JOBS = 3
MAX_RETRIES = 2
DEFAULT_PORT = 18765
COLORS = {"pending": "#d0d7de", "active": "#0969da", "done": "#2da44e", "fail": "#cf222e"}

APP_DIR = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")
PIECES_DIR = os.path.join(APP_DIR, "pieces")


class ExclusiveThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False


def decrypt_aes128(data, key, iv):
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(data) + decryptor.finalize()
    unpadder = PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def format_size(num_bytes):
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024 or unit == "GB":
            return f"{num_bytes:.1f} {unit}" if unit != "B" else f"{num_bytes} B"
        num_bytes /= 1024


class M3U8DownloaderApp:
    def __init__(self, root):
        self.root = root
        self.root.title("M3U8 Downloader")
        self.root.geometry("580x775")
        self.root.resizable(False, False)
        self.quality_map = {}
        self.jobs = {}
        self.job_counter = 0
        self.selected_job_id = None
        self.job_semaphore = threading.Semaphore(MAX_CONCURRENT_JOBS)
        self.httpd = None

        settings = self.load_settings()
        self.listen_port = int(settings.get("port", DEFAULT_PORT))

        # URL input + Fetch Qualities
        tk.Label(root, text=".m3u8 URL:").pack(anchor="w", padx=16, pady=(10, 2))
        url_frame = tk.Frame(root)
        url_frame.pack(fill="x", padx=16)
        self.url_entry = tk.Entry(url_frame)
        self.url_entry.pack(side="left", fill="x", expand=True)
        tk.Button(url_frame, text="Fetch Qualities", command=self.fetch_qualities).pack(side="right", padx=(6, 0))

        # Quality selector
        tk.Label(root, text="Quality (for master playlists):").pack(anchor="w", padx=16, pady=(6, 2))
        self.quality_combo = ttk.Combobox(root, state="readonly")
        self.quality_combo.set("Auto (Best Quality)")
        self.quality_combo.pack(fill="x", padx=16)

        # Custom HTTP Headers
        tk.Label(root, text="HTTP Headers (optional, one per line e.g. Referer: https://...):").pack(anchor="w", padx=16, pady=(6, 2))
        self.headers_text = tk.Text(root, height=3)
        self.headers_text.pack(fill="x", padx=16)

        # Proxy input + Extension Port
        row_frame = tk.Frame(root)
        row_frame.pack(fill="x", padx=16, pady=(6, 2))
        left_col = tk.Frame(row_frame)
        left_col.pack(side="left", fill="x", expand=True)
        tk.Label(left_col, text="Proxy (optional, e.g. http://127.0.0.1:7890):").pack(anchor="w")
        self.proxy_entry = tk.Entry(left_col)
        self.proxy_entry.insert(0, settings.get("proxy", ""))
        self.proxy_entry.pack(fill="x")

        right_col = tk.Frame(row_frame)
        right_col.pack(side="right", padx=(10, 0))
        tk.Label(right_col, text="Chrome Port:").pack(anchor="w")
        port_box = tk.Frame(right_col)
        port_box.pack()
        self.port_entry = tk.Entry(port_box, width=7)
        self.port_entry.insert(0, str(self.listen_port))
        self.port_entry.pack(side="left")
        tk.Button(port_box, text="Apply", command=self.apply_port).pack(side="left", padx=(4, 0))

        # Save As input + Browse
        tk.Label(root, text="Save as:").pack(anchor="w", padx=16, pady=(6, 2))
        save_frame = tk.Frame(root)
        save_frame.pack(fill="x", padx=16)
        self.save_entry = tk.Entry(save_frame)
        default_dir = settings.get("save_dir", APP_DIR)
        self.save_entry.insert(0, os.path.join(default_dir, "output.mp4"))
        self.save_entry.pack(side="left", fill="x", expand=True)
        tk.Button(save_frame, text="Browse...", command=self.choose_save_path).pack(side="right", padx=(6, 0))

        # Queue action buttons
        btn_frame = tk.Frame(root)
        btn_frame.pack(fill="x", padx=16, pady=10)
        tk.Button(btn_frame, text="Add & Start Download", command=self.add_job, bg="#0969da", fg="white", pady=5).pack(side="left", fill="x", expand=True)
        tk.Button(btn_frame, text="Retry Selected", command=self.retry_selected, pady=5, width=13).pack(side="left", padx=(6, 0))
        tk.Button(btn_frame, text="Cancel Selected", command=self.cancel_selected, pady=5, width=14).pack(side="right", padx=(6, 0))

        # Queue header + Clear Dangling Cache button
        q_header = tk.Frame(root)
        q_header.pack(fill="x", padx=16, pady=(2, 2))
        tk.Label(q_header, text="Download Queue (click a row to view its piece grid):").pack(side="left")
        self.clean_btn = tk.Button(q_header, text="Clear Dangling Cache (0 B)", command=self.clear_dangling_cache, font=("TkDefaultFont", 8))
        self.clean_btn.pack(side="right")

        # Queue table
        self.tree = ttk.Treeview(root, columns=("file", "status", "progress"), show="headings", height=5)
        self.tree.heading("file", text="File")
        self.tree.heading("status", text="Status")
        self.tree.heading("progress", text="Progress")
        self.tree.column("file", width=230)
        self.tree.column("status", width=160)
        self.tree.column("progress", width=130)
        self.tree.pack(fill="x", padx=16)
        self.tree.bind("<<TreeviewSelect>>", self.on_select_job)

        # Wrapped status line
        self.status_label = tk.Label(root, text="Starting Chrome extension listener...", wraplength=540, justify="left", anchor="w")
        self.status_label.pack(fill="x", padx=16, pady=(6, 4))

        # Scrollable piece-status grid for selected job
        frame = tk.Frame(root)
        frame.pack(fill="both", expand=True, padx=16, pady=(0, 12))
        self.canvas = tk.Canvas(frame, bg="white", highlightthickness=1, highlightbackground="#d0d7de")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.squares = []
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.start_local_server(self.listen_port, auto_fallback=True)
        self.refresh_cache_size()

    def get_dangling_items(self):
        if not os.path.isdir(PIECES_DIR):
            return [], 0
        active_ids = {hashlib.md5(j["url"].encode()).hexdigest()[:10] for j in self.jobs.values()}
        dangling = []
        total_bytes = 0
        for entry in os.scandir(PIECES_DIR):
            if entry.name in active_ids:
                continue
            dangling.append(entry.path)
            if entry.is_file():
                total_bytes += entry.stat().st_size
            elif entry.is_dir():
                for root_dir, _, files in os.walk(entry.path):
                    for f in files:
                        try:
                            total_bytes += os.path.getsize(os.path.join(root_dir, f))
                        except OSError:
                            pass
        return dangling, total_bytes

    def refresh_cache_size(self):
        def worker():
            _, total_bytes = self.get_dangling_items()
            self.root.after(0, lambda b=total_bytes: self.clean_btn.config(text=f"Clear Dangling Cache ({format_size(b)})"))

        threading.Thread(target=worker, daemon=True).start()

    def clear_dangling_cache(self):
        dangling, total_bytes = self.get_dangling_items()
        if not dangling:
            self.status_label.config(text="No dangling files in pieces/ to clear.", fg="#24292f")
            self.refresh_cache_size()
            return
        for path in dangling:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                try:
                    os.remove(path)
                except OSError:
                    pass
        self.status_label.config(text=f"Cleared {format_size(total_bytes)} of dangling cache from pieces/.", fg="#1a7f37")
        self.refresh_cache_size()

    def load_settings(self):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def save_settings(self):
        save_path = self.save_entry.get().strip()
        save_dir = os.path.dirname(save_path) if save_path else APP_DIR
        data = {
            "proxy": self.proxy_entry.get().strip(),
            "save_dir": save_dir or APP_DIR,
            "port": self.listen_port,
        }
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    def on_close(self):
        self.save_settings()
        if self.httpd:
            try:
                self.httpd.shutdown()
            except Exception:
                pass
        self.root.destroy()

    def apply_port(self):
        try:
            new_port = int(self.port_entry.get().strip())
            if not (1024 <= new_port <= 65535):
                raise ValueError
        except ValueError:
            messagebox.showwarning("Invalid Port", "Enter a port number between 1024 and 65535.")
            return

        if self.httpd:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                pass
            self.httpd = None
        self.start_local_server(new_port, auto_fallback=False)

    def start_local_server(self, preferred_port, auto_fallback=True):
        app = self

        class CaptureHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"app": "M3U8Downloader", "port": app.listen_port}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Private-Network", "true")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                if self.path == "/capture":
                    try:
                        length = int(self.headers.get("Content-Length", 0))
                        data = json.loads(self.rfile.read(length).decode("utf-8"))
                        app.root.after(0, app.apply_capture, data)
                        self.send_response(200)
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.send_header("Access-Control-Allow-Private-Network", "true")
                        self.send_header("Content-Length", "2")
                        self.send_header("Connection", "close")
                        self.end_headers()
                        self.wfile.write(b"OK")
                    except Exception as e:
                        msg = str(e).encode("utf-8")
                        self.send_response(500)
                        self.send_header("Content-Length", str(len(msg)))
                        self.send_header("Connection", "close")
                        self.end_headers()
                        self.wfile.write(msg)

            def do_OPTIONS(self):
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Access-Control-Allow-Private-Network", "true")
                self.send_header("Connection", "close")
                self.end_headers()

            def log_message(self, *args):
                pass

        candidates = (
            [preferred_port, *range(preferred_port + 1, min(preferred_port + 10, 65536)), 18765, 28765, 38765, 0]
            if auto_fallback
            else [preferred_port]
        )

        bound_server = None
        bound_port = preferred_port
        last_err = None

        for port in candidates:
            try:
                bound_server = ExclusiveThreadingHTTPServer(("127.0.0.1", port), CaptureHandler)
                bound_port = bound_server.server_address[1]
                break
            except OSError as e:
                last_err = e

        if bound_server is None:
            self.status_label.config(text=f"Port {preferred_port} unavailable ({last_err}). Try another Chrome Port.", fg="#cf222e")
            return

        self.httpd = bound_server
        self.listen_port = bound_port
        self.port_entry.delete(0, tk.END)
        self.port_entry.insert(0, str(bound_port))
        self.save_settings()

        if bound_port != preferred_port:
            self.status_label.config(
                text=f"Port {preferred_port} was unavailable — listening on {bound_port} instead. Set Chrome extension port to {bound_port}.",
                fg="#9a6700",
            )
        else:
            self.status_label.config(text=f"Ready (Chrome extension listening on port {bound_port})", fg="#24292f")

        threading.Thread(target=bound_server.serve_forever, daemon=True).start()

    def apply_capture(self, data):
        self.url_entry.delete(0, tk.END)
        self.url_entry.insert(0, data.get("url", ""))
        self.headers_text.delete("1.0", tk.END)
        for k, v in data.get("headers", {}).items():
            self.headers_text.insert(tk.END, f"{k}: {v}\n")
        self.quality_map.clear()
        self.quality_combo.configure(values=[])
        self.quality_combo.set("Auto (Best Quality)")
        self.status_label.config(text="Captured URL & headers from Chrome! Set 'Save as' and click Add & Start.", fg="#1a7f37")
        self.root.lift()

    def get_headers(self):
        headers = {}
        for line in self.headers_text.get("1.0", tk.END).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip()] = v.strip()
        return headers

    def get_proxies(self):
        p = self.proxy_entry.get().strip()
        return {"http": p, "https": p} if p else None

    def choose_save_path(self):
        current = self.save_entry.get().strip()
        init_dir = os.path.dirname(current) if current else APP_DIR
        path = filedialog.asksaveasfilename(initialdir=init_dir, defaultextension=".mp4", filetypes=[("MP4 video", "*.mp4")])
        if path:
            self.save_entry.delete(0, tk.END)
            self.save_entry.insert(0, path)
            self.save_settings()

    def fetch_qualities(self):
        url = self.url_entry.get().strip()
        if not url:
            messagebox.showwarning("Missing URL", "Paste an .m3u8 URL first.")
            return
        self.status_label.config(text="Checking available qualities...", fg="#24292f")
        threading.Thread(
            target=self._fetch_qualities_worker,
            args=(url, self.get_proxies(), self.get_headers()),
            daemon=True,
        ).start()

    def _fetch_qualities_worker(self, url, proxies, headers):
        try:
            res = requests.get(url, proxies=proxies, headers=headers, timeout=15)
            res.raise_for_status()
            variants = self.parse_variants(url, res.text)
            if not variants:
                self.root.after(0, lambda: (
                    self.quality_map.clear(),
                    self.quality_combo.configure(values=["Single Stream"]),
                    self.quality_combo.set("Single Stream"),
                    self.status_label.config(text="Single quality stream detected."),
                ))
            else:
                qmap = {label: sub_url for _, label, sub_url in variants}
                labels = list(qmap.keys())
                self.root.after(0, lambda: (
                    self.quality_map.clear(),
                    self.quality_map.update(qmap),
                    self.quality_combo.configure(values=labels),
                    self.quality_combo.set(labels[0]),
                    self.status_label.config(text=f"Found {len(labels)} qualities."),
                ))
        except Exception as e:
            self.root.after(0, messagebox.showerror, "Error", str(e))

    def parse_variants(self, base_url, text):
        variants = []
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        for idx, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF:") and idx + 1 < len(lines):
                bw = int(m.group(1)) if (m := re.search(r"BANDWIDTH=(\d+)", line)) else 0
                res = m.group(1) if (m := re.search(r"RESOLUTION=(\d+x\d+)", line)) else "Unknown res"
                label = f"{res} ({bw / 1_000_000:.1f} Mbps)" if bw else res
                variants.append((bw, label, urljoin(base_url, lines[idx + 1])))
        variants.sort(key=lambda x: x[0], reverse=True)
        return variants

    def on_select_job(self, event=None):
        sel = self.tree.selection()
        if not sel:
            return
        self.selected_job_id = sel[0]
        self.render_job_grid(self.selected_job_id)

    def render_job_grid(self, job_id):
        job = self.jobs.get(job_id)
        if not job:
            return
        self.canvas.delete("all")
        self.squares = []
        for i, state in enumerate(job["states"]):
            row, col = divmod(i, COLS)
            x1 = col * (CELL_SIZE + GAP) + 8
            y1 = row * (CELL_SIZE + GAP) + 8
            sq = self.canvas.create_rectangle(x1, y1, x1 + CELL_SIZE, y1 + CELL_SIZE, fill=COLORS[state], outline="")
            self.squares.append(sq)
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def update_job_square(self, job_id, index, state):
        self.jobs[job_id]["states"][index] = state
        if self.selected_job_id == job_id and index < len(self.squares):
            self.canvas.itemconfig(self.squares[index], fill=COLORS[state])

    def update_job_row(self, job_id, status, progress_text):
        if self.tree.exists(job_id):
            fname = os.path.basename(self.jobs[job_id]["save_path"])
            self.tree.item(job_id, values=(fname, status, progress_text))

    def add_job(self):
        url = self.url_entry.get().strip()
        save_path = self.save_entry.get().strip()
        if not url or not save_path:
            messagebox.showwarning("Missing Info", "Please enter both an .m3u8 URL and a save path.")
            return

        self.save_settings()
        selected = self.quality_combo.get()
        target_url = self.quality_map.get(selected, url)
        self.job_counter += 1
        job_id = f"job_{self.job_counter}"
        self.jobs[job_id] = {
            "url": target_url,
            "proxies": self.get_proxies(),
            "headers": self.get_headers(),
            "save_path": save_path,
            "states": [],
            "cancel_event": threading.Event(),
        }
        self.tree.insert("", "end", iid=job_id, values=(os.path.basename(save_path), "Queued", "0%"))
        self.tree.selection_set(job_id)
        self.selected_job_id = job_id
        self.refresh_cache_size()
        threading.Thread(target=self.run_job, args=(job_id,), daemon=True).start()

    def retry_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        job_id = sel[0]
        job = self.jobs[job_id]
        job["cancel_event"].clear()
        self.update_job_row(job_id, "Queued", "Retrying...")
        threading.Thread(target=self.run_job, args=(job_id,), daemon=True).start()

    def cancel_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        job_id = sel[0]
        self.jobs[job_id]["cancel_event"].set()
        self.update_job_row(job_id, "Cancelling...", "")

    def parse_playlist(self, url, proxies, headers):
        res = requests.get(url, proxies=proxies, headers=headers, timeout=15)
        res.raise_for_status()
        text = res.text

        if "#EXT-X-STREAM-INF" in text:
            variants = self.parse_variants(url, text)
            url = variants[0][2]
            res = requests.get(url, proxies=proxies, headers=headers, timeout=15)
            res.raise_for_status()
            text = res.text

        pieces = []
        key_cache = {}
        current_key, current_iv, media_seq = None, None, 0

        for line in (l.strip() for l in text.splitlines() if l.strip()):
            if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                media_seq = int(line.split(":")[1])
            elif line.startswith("#EXT-X-KEY:"):
                method = re.search(r"METHOD=([^,]+)", line)
                if method and method.group(1) == "NONE":
                    current_key, current_iv = None, None
                elif method and method.group(1) == "AES-128":
                    uri = re.search(r'URI="([^"]+)"', line).group(1)
                    key_url = urljoin(url, uri)
                    if key_url not in key_cache:
                        key_cache[key_url] = requests.get(key_url, proxies=proxies, headers=headers, timeout=15).content
                    current_key = key_cache[key_url]
                    iv_match = re.search(r"IV=0x([0-9a-fA-F]+)", line)
                    current_iv = bytes.fromhex(iv_match.group(1).zfill(32)) if iv_match else None
            elif not line.startswith("#"):
                iv = current_iv if current_iv is not None else (media_seq + len(pieces)).to_bytes(16, "big")
                pieces.append((urljoin(url, line), current_key, iv))

        return pieces

    def run_job(self, job_id):
        job = self.jobs[job_id]
        cancel_event = job["cancel_event"]

        with self.job_semaphore:
            if cancel_event.is_set():
                self.root.after(0, self.update_job_row, job_id, "Cancelled", "-")
                return

            url = job["url"]
            proxies = job["proxies"]
            headers = job["headers"]
            save_path = job["save_path"]
            url_id = hashlib.md5(url.encode()).hexdigest()[:10]
            piece_dir = os.path.join(PIECES_DIR, url_id)
            os.makedirs(piece_dir, exist_ok=True)

            def download_one(i, piece_url, key, iv):
                if cancel_event.is_set():
                    return "cancelled"

                piece_path = os.path.join(piece_dir, f"{i:05d}.ts")
                if os.path.exists(piece_path) and os.path.getsize(piece_path) > 0:
                    self.root.after(0, self.update_job_square, job_id, i, "done")
                    return "done"

                self.root.after(0, self.update_job_square, job_id, i, "active")
                tmp_path = piece_path + ".tmp"

                for attempt in range(MAX_RETRIES + 1):
                    if cancel_event.is_set():
                        return "cancelled"
                    try:
                        r = requests.get(piece_url, proxies=proxies, headers=headers, timeout=15)
                        r.raise_for_status()
                        content = decrypt_aes128(r.content, key, iv) if key else r.content
                        with open(tmp_path, "wb") as f:
                            f.write(content)
                        os.replace(tmp_path, piece_path)
                        self.root.after(0, self.update_job_square, job_id, i, "done")
                        return "done"
                    except Exception:
                        if os.path.exists(tmp_path):
                            os.remove(tmp_path)
                        if attempt < MAX_RETRIES:
                            time.sleep(attempt + 1)

                self.root.after(0, self.update_job_square, job_id, i, "fail")
                return "fail"

            try:
                self.root.after(0, self.update_job_row, job_id, "Fetching playlist...", "0%")
                pieces = self.parse_playlist(url, proxies, headers)
                total = len(pieces)
                job["states"] = ["pending"] * total
                self.root.after(0, lambda j=job_id: self.selected_job_id == j and self.render_job_grid(j))

                done_count = 0
                failed_count = 0

                with ThreadPoolExecutor(max_workers=MAX_WORKERS_PER_JOB) as pool:
                    futures = [pool.submit(download_one, i, u, k, iv) for i, (u, k, iv) in enumerate(pieces)]
                    for future in as_completed(futures):
                        if cancel_event.is_set():
                            pool.shutdown(wait=False, cancel_futures=True)
                            break
                        result = future.result()
                        if result == "done":
                            done_count += 1
                        elif result == "fail":
                            failed_count += 1
                        pct = (done_count / total) * 100
                        self.root.after(0, self.update_job_row, job_id, "Downloading", f"{done_count}/{total} ({pct:.0f}%)")

                if cancel_event.is_set():
                    self.root.after(0, self.update_job_row, job_id, "Cancelled", f"{done_count}/{total}")
                elif failed_count:
                    self.root.after(0, self.update_job_row, job_id, f"Failed ({failed_count})", f"{done_count}/{total}")
                else:
                    self.root.after(0, self.update_job_row, job_id, "Stitching...", "100%")
                    list_path = os.path.join(piece_dir, "list.txt")
                    with open(list_path, "w") as f:
                        for i in range(total):
                            f.write(f"file '{i:05d}.ts'\n")
                    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                    subprocess.run(
                        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", save_path],
                        check=True,
                        creationflags=flags,
                    )
                    shutil.rmtree(piece_dir, ignore_errors=True)
                    self.root.after(0, self.update_job_row, job_id, "Done", "100%")
                    self.refresh_cache_size()
            except Exception as e:
                self.root.after(0, self.update_job_row, job_id, "Error", str(e)[:24])


root = tk.Tk()
M3U8DownloaderApp(root)
root.mainloop()
