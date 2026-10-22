import importlib.util as iu
import os
import shutil
import socket
import subprocess
import sys
import time

os.chdir(os.path.dirname(os.path.abspath(__file__)))
PORT = 8000

with socket.socket() as s:
    if s.connect_ex(("127.0.0.1", PORT)) == 0:
        print("ZapStream backend is already running on port", PORT)
        sys.exit(0)

need = {"fastapi": "fastapi", "uvicorn": "uvicorn", "yt_dlp": "yt-dlp"}
pkgs = [p for m, p in need.items() if iu.find_spec(m) is None]
if not shutil.which("ffmpeg") and iu.find_spec("imageio_ffmpeg") is None:
    pkgs.append("imageio-ffmpeg")

# yt-dlp breaks often as sites change, so refresh it once a week
stamp = ".ytdlp_stamp"
if not pkgs and (not os.path.exists(stamp) or time.time() - os.path.getmtime(stamp) > 7 * 86400):
    pkgs.append("yt-dlp")

if pkgs:
    print("Installing/updating:", ", ".join(pkgs))
    subprocess.call([sys.executable, "-m", "pip", "install", "-U", *pkgs])
    open(stamp, "w").write("ok")

import uvicorn
uvicorn.run("backend:app", host="127.0.0.1", port=PORT)