import os
import shutil
import subprocess
import sys
import zipfile

os.chdir(os.path.dirname(os.path.abspath(__file__)))

print("1/3 Installing dependencies...")
subprocess.check_call([sys.executable, "-m", "pip", "install", "requests", "cryptography", "pyinstaller"])

print("2/3 Cleaning old build & compiling app.py into M3U8Downloader.exe...")
shutil.rmtree("build", ignore_errors=True)
shutil.rmtree("dist", ignore_errors=True)
subprocess.check_call([
    sys.executable, "-m", "PyInstaller",
    "--clean", "--noconfirm", "--onefile", "--windowed",
    "--name", "M3U8Downloader", "app.py",
])

print("3/3 Packing release ZIP...")
zip_name = "M3U8Downloader-Release.zip"
with zipfile.ZipFile(zip_name, "w", zipfile.ZIP_DEFLATED) as zf:
    exe_path = os.path.join("dist", "M3U8Downloader.exe")
    if os.path.exists(exe_path):
        zf.write(exe_path, "M3U8Downloader.exe")
    if os.path.isdir("chrome-extension"):
        for root, _, files in os.walk("chrome-extension"):
            for file in files:
                full_path = os.path.join(root, file)
                zf.write(full_path, os.path.relpath(full_path, "."))

print(f"\nDone! Created {zip_name}")
input("Press Enter to exit...")
