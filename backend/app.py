"""Backend converter Java <-> Bedrock. Memakai Chunker CLI sebagai engine konversi."""
import json, os, shutil, subprocess, threading, time, uuid, zipfile
from pathlib import Path
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

JAR = os.getenv("CHUNKER_JAR", "/app/chunker-cli.jar")
DATA = Path(os.getenv("DATA_DIR", "/tmp/j2b")); DATA.mkdir(parents=True, exist_ok=True)
MAX_MB = int(os.getenv("MAX_UPLOAD_MB", "500"))
TIMEOUT = int(os.getenv("JOB_TIMEOUT_SEC", "1800"))
XMX = os.getenv("JAVA_XMX", "2g")
# Nama format harus sesuai Chunker CLI kamu. Cek: java -jar chunker-cli.jar --help
FORMATS = json.loads(os.getenv("FORMATS_JSON", json.dumps({
    "bedrock": ["BEDROCK_1_21_0", "BEDROCK_1_20_80", "BEDROCK_1_20_0", "BEDROCK_1_19_80"],
    "java": ["JAVA_1_21", "JAVA_1_20_4", "JAVA_1_20", "JAVA_1_19_4"],
})))
JOBS, SEM = {}, threading.Semaphore(int(os.getenv("MAX_JOBS", "1")))

app = FastAPI(title="J2B Converter")
app.add_middleware(CORSMiddleware, allow_origins=os.getenv("ALLOWED_ORIGINS", "*").split(","),
                   allow_methods=["*"], allow_headers=["*"], expose_headers=["Content-Disposition"])

def safe_extract(z, dest):
    root = dest.resolve()
    for m in z.infolist():
        t = (dest / m.filename).resolve()
        if t != root and root not in t.parents:
            raise ValueError("ZIP tidak aman")
    z.extractall(dest)

def find_world(base):
    for p in [base, *base.rglob("*")]:
        if p.is_dir() and (p / "level.dat").exists() and ((p / "region").exists() or (p / "db").exists()):
            return p

def work(jid, target, version):
    j = JOBS[jid]; wd = DATA / jid
    with SEM:
        try:
            j["status"] = "running"
            ex = wd / "in"; ex.mkdir()
            with zipfile.ZipFile(wd / "in.zip") as z: safe_extract(z, ex)
            world = find_world(ex)
            if not world: raise ValueError("Dunia tidak ditemukan (butuh level.dat + folder region atau db)")
            src = "java" if (world / "region").exists() else "bedrock"
            if src == target: raise ValueError(f"Dunia sudah berformat {target}")
            j["log"].append(f"Terdeteksi dunia {src}, konversi ke {version}")
            out = wd / "out"
            p = subprocess.Popen(["java", f"-Xmx{XMX}", "-jar", JAR, "-i", str(world), "-f", version, "-o", str(out)],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            threading.Timer(TIMEOUT, p.kill).start()
            for line in p.stdout:
                j["log"] = (j["log"] + [line.rstrip()])[-200:]
            if p.wait() != 0: raise RuntimeError("Chunker gagal, lihat log")
            res = find_world(out) or out
            ext = "mcworld" if target == "bedrock" else "zip"
            zp = wd / f"result.{ext}"
            with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
                for f in res.rglob("*"):
                    if f.is_file(): z.write(f, f.relative_to(res))
            j.update(status="done", file=str(zp), name=f"converted-{target}.{ext}")
        except Exception as e:
            j.update(status="error", error=str(e))
        finally:
            shutil.rmtree(wd / "in", ignore_errors=True); shutil.rmtree(wd / "out", ignore_errors=True)
            (wd / "in.zip").unlink(missing_ok=True)

def janitor():
    while True:
        time.sleep(300)
        for jid, j in list(JOBS.items()):
            if time.time() - j["t"] > 3600 and j["status"] != "running":
                shutil.rmtree(DATA / jid, ignore_errors=True); JOBS.pop(jid, None)
threading.Thread(target=janitor, daemon=True).start()

@app.get("/formats")
def formats(): return FORMATS

@app.post("/convert")
async def convert(file: UploadFile = File(...), target: str = Form(...), version: str = Form(...)):
    if version not in FORMATS.get(target, []): raise HTTPException(400, "Target/versi tidak valid")
    jid = uuid.uuid4().hex; wd = DATA / jid; wd.mkdir(); size = 0
    with open(wd / "in.zip", "wb") as f:
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            if size > MAX_MB << 20:
                f.close(); shutil.rmtree(wd, ignore_errors=True)
                raise HTTPException(413, f"Maksimal {MAX_MB} MB")
            f.write(chunk)
    if not zipfile.is_zipfile(wd / "in.zip"):
        shutil.rmtree(wd, ignore_errors=True); raise HTTPException(400, "File harus .zip / .mcworld")
    JOBS[jid] = {"status": "queued", "log": [], "t": time.time()}
    threading.Thread(target=work, args=(jid, target, version), daemon=True).start()
    return {"id": jid}

@app.get("/status/{jid}")
def status(jid: str):
    j = JOBS.get(jid)
    if not j: raise HTTPException(404, "Job tidak ada / kedaluwarsa")
    return {"status": j["status"], "error": j.get("error"), "log": j["log"][-15:]}

@app.get("/download/{jid}")
def download(jid: str):
    j = JOBS.get(jid)
    if not j or j["status"] != "done": raise HTTPException(404, "Belum siap")
    return FileResponse(j["file"], filename=j["name"])
