import os
import re
import shutil
import threading
import uuid

import yt_dlp
from flask import Flask, jsonify, request, send_file, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

app = Flask(__name__, static_folder=os.path.join(BASE_DIR, "static"))
jobs = {}


def safe_name(name):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip().strip(".")
    return name[:150]


def run_job(job_id, url, fmt):
    job = jobs[job_id]
    folder = os.path.join(DOWNLOAD_DIR, job_id)
    os.makedirs(folder, exist_ok=True)

    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                job["progress"] = round(d["downloaded_bytes"] / total * 90, 1)
        elif d["status"] == "finished":
            job["progress"] = 90
            job["status"] = "processing"

    opts = {
        "outtmpl": os.path.join(folder, "%(title)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [hook],
    }
    if fmt == "mp3":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    else:
        opts["format"] = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
        opts["merge_output_format"] = "mp4"

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        files = os.listdir(folder)
        if not files:
            raise RuntimeError("Файл не был создан")
        job["path"] = os.path.join(folder, files[0])
        job["filename"] = files[0]
        job["progress"] = 100
        job["status"] = "done"
    except Exception as e:
        job["status"] = "error"
        job["error"] = re.sub(r"\x1b\[[0-9;]*m", "", str(e))


def public(job_id):
    j = jobs[job_id]
    return {k: j.get(k) for k in ("status", "progress", "filename", "error")}


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/sw.js")
def service_worker():
    resp = send_from_directory(app.static_folder, "sw.js", mimetype="text/javascript")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/manifest.webmanifest")
def manifest():
    return send_from_directory(app.static_folder, "manifest.webmanifest", mimetype="application/manifest+json")


@app.post("/api/download")
def download():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    fmt = data.get("format", "mp3")
    if not re.match(r"^https?://", url):
        return jsonify(error="Некорректная ссылка"), 400
    if fmt not in ("mp3", "video"):
        return jsonify(error="Неверный формат"), 400
    job_id = uuid.uuid4().hex
    jobs[job_id] = {"status": "downloading", "progress": 0}
    threading.Thread(target=run_job, args=(job_id, url, fmt), daemon=True).start()
    return jsonify(id=job_id)


@app.get("/api/status/<job_id>")
def status(job_id):
    if job_id not in jobs:
        return jsonify(error="Не найдено"), 404
    return jsonify(public(job_id))


@app.post("/api/rename/<job_id>")
def rename(job_id):
    job = jobs.get(job_id)
    if not job or job.get("status") != "done":
        return jsonify(error="Файл не готов"), 400
    new = safe_name((request.get_json(silent=True) or {}).get("name", ""))
    if not new:
        return jsonify(error="Пустое имя"), 400
    ext = os.path.splitext(job["path"])[1]
    if new.lower().endswith(ext.lower()):
        new = new[: -len(ext)]
    new_path = os.path.join(os.path.dirname(job["path"]), new + ext)
    os.rename(job["path"], new_path)
    job["path"] = new_path
    job["filename"] = new + ext
    return jsonify(filename=job["filename"])


def is_active(job_id):
    return jobs.get(job_id, {}).get("status") in ("downloading", "processing")


def folder_file(job_id):
    """Готовый файл в папке загрузки или None."""
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        return None
    folder = os.path.join(DOWNLOAD_DIR, job_id)
    if not os.path.isdir(folder):
        return None
    for f in sorted(os.listdir(folder)):
        if not f.endswith((".part", ".ytdl")):
            return os.path.join(folder, f)
    return None


def drop(job_id):
    shutil.rmtree(os.path.join(DOWNLOAD_DIR, job_id), ignore_errors=True)
    jobs.pop(job_id, None)


@app.get("/api/history")
def history():
    items = []
    for job_id in os.listdir(DOWNLOAD_DIR):
        if is_active(job_id):
            continue
        path = folder_file(job_id)
        if not path:
            continue
        st = os.stat(path)
        items.append({"id": job_id, "filename": os.path.basename(path),
                      "size": st.st_size, "time": st.st_mtime})
    items.sort(key=lambda i: i["time"], reverse=True)
    return jsonify(items)


@app.delete("/api/history/<job_id>")
def delete_one(job_id):
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        return jsonify(error="Не найдено"), 404
    if is_active(job_id):
        return jsonify(error="Файл ещё скачивается"), 400
    drop(job_id)
    return jsonify(ok=True)


@app.delete("/api/history")
def delete_all():
    for job_id in os.listdir(DOWNLOAD_DIR):
        if re.fullmatch(r"[0-9a-f]{32}", job_id) and not is_active(job_id):
            drop(job_id)
    return jsonify(ok=True)


@app.get("/api/file/<job_id>")
def get_file(job_id):
    path = folder_file(job_id)
    if not path or is_active(job_id):
        return jsonify(error="Файл не готов"), 404
    return send_file(path, as_attachment=True, download_name=os.path.basename(path))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True)
