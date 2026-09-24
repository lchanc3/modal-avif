r"""本機控制台。只綁 127.0.0.1。

啟動： start-web.bat      或   .venv\Scripts\python web\server.py

網頁只是遙控器：送件之後工作在雲端的 driver 手上跑，
關掉瀏覽器、關掉這個 server、關機都不影響。
"""

import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import modal          # noqa: E402
import convert        # noqa: E402
import jobspec        # noqa: E402
import transfer       # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT_DIR = os.path.join(ROOT, "out")
PORT = int(os.environ.get("AVIF_PORT", "8766"))     # 8765 留給 modal-av1，兩個可以同時開

ALLOWED_ORIGINS = {"http://127.0.0.1:{}".format(PORT), "http://localhost:{}".format(PORT)}

app = FastAPI(title="modal-avif")

_vol = None
_dict = None


def vol():
    global _vol
    if _vol is None:
        _vol = jobspec.get_volume()
    return _vol


def jd():
    global _dict
    if _dict is None:
        _dict = jobspec.get_dict()
    return _dict


# --------------------------------------------------------------------------
# 只綁 127.0.0.1 擋不住惡意網站對 localhost 發請求，所以改狀態的請求檢查 Origin。
# 沒有 Origin 的（curl 之類的非瀏覽器用戶端）放行。
# --------------------------------------------------------------------------

@app.middleware("http")
async def guard_origin(request: Request, call_next):
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and origin not in ALLOWED_ORIGINS:
            return JSONResponse({"detail": "跨來源請求已擋下"}, status_code=403)
    return await call_next(request)


@app.get("/")
def index():
    return FileResponse(os.path.join(HERE, "index.html"))


@app.get("/api/defaults")
def defaults():
    """表單的預設值從 jobspec 來，網頁不自己寫一份。"""
    return {"defaults": jobspec.DEFAULTS, "exts": sorted(convert.EXTS)}


def _name(raw) -> str:
    try:
        return jobspec.check_name(str(raw or ""))
    except ValueError as e:
        raise HTTPException(400, str(e))


# --------------------------------------------------------------------------
# Volume：以資料夾為單位
# --------------------------------------------------------------------------

def _folders(root: str, only_avif: bool) -> list:
    """把 root 底下的檔案依第一層資料夾分組。"""
    groups = {}
    for e in transfer.listdir(vol(), root):
        rel = e.path[len(root) + 1:]
        if "/" not in rel or (only_avif and not rel.endswith(".avif")):
            continue
        g = groups.setdefault(rel.split("/", 1)[0], {"files": 0, "bytes": 0, "mtime": 0})
        g["files"] += 1
        g["bytes"] += e.size or 0
        g["mtime"] = max(g["mtime"], e.mtime or 0)
    return [dict(v, name=k, path="{}/{}".format(root, k)) for k, v in sorted(groups.items())]


_files_cache = {"at": 0.0, "data": None}
FILES_TTL = 15     # VolumeListFiles 有速率限制，多開分頁不該讓呼叫次數翻倍


@app.get("/api/files")
def files():
    now = time.time()
    if _files_cache["data"] and now - _files_cache["at"] < FILES_TTL:
        return _files_cache["data"]
    try:
        data = {"in": _folders("in", False), "out": _folders("out", True)}
    except Exception as e:
        # 被限流或連線出錯時給上一次的結果並標記。不要回空清單 ——
        # 那看起來會像 Volume 被清空了。
        print("列檔失敗：{!r}".format(e), flush=True)
        if _files_cache["data"]:
            return dict(_files_cache["data"], stale=True)
        raise HTTPException(502, "讀取 Volume 失敗：{}".format(e))
    for f in data["out"]:
        f["local"] = os.path.isdir(os.path.join(OUT_DIR, *f["name"].split("/")))
    _files_cache.update(at=now, data=data)
    return data


def _invalidate_files():
    _files_cache["at"] = 0.0


@app.delete("/api/files")
def remove_folder(path: str):
    if not path.startswith(("in/", "out/")) or path.count("/") != 1:
        raise HTTPException(400, "只能刪 in/ 或 out/ 底下的整個資料夾")
    _name(path)
    try:
        vol().remove_file(path, recursive=True)
    except Exception as e:
        raise HTTPException(502, "刪除失敗：{}".format(e))
    _invalidate_files()
    return {"ok": True}


# --------------------------------------------------------------------------
# 傳輸：上傳與下載共用一張表，進度由 server 回報
# --------------------------------------------------------------------------

class Transfer(transfer.Progress):
    """一筆上傳或下載。server 端是唯一真相來源，重新整理頁面也看得到。"""

    def __init__(self, kind: str, name: str, files: int = 0, total: int = 0):
        super().__init__(files, total)
        self.kind, self.name = kind, name
        self.phase, self.error = "local" if kind == "upload" else "prepare", ""
        self.skipped, self.ended, self.dest = 0, 0.0, ""

    def fail(self, e):
        self.phase, self.error, self.ended = "error", str(e)[:500], time.time()

    def finish(self):
        self.phase, self.ended = "done", time.time()


TRANSFERS = {}        # uid -> Transfer
STAGING = {}          # uid -> 瀏覽器上傳的暫存資料夾


def _start(target, *args):
    threading.Thread(target=target, args=args, daemon=True).start()


def _push(uid: str, files: list, cleanup: str = None):
    """把本機檔案推到 in/<name>/。cleanup 是傳完要刪的暫存資料夾。"""
    t = TRANSFERS[uid]
    try:
        t.phase = "plan"
        todo, t.skipped, t.total = transfer.plan_upload(vol(), files, t.name)
        t.files, t.done, t.sent, t.phase = len(todo), 0, 0, "modal"
        if todo:
            transfer.upload(vol(), todo, t)
        t.finish()
        _invalidate_files()
    except Exception as e:
        t.fail(e)
    finally:
        if cleanup:
            shutil.rmtree(cleanup, ignore_errors=True)


@app.post("/api/upload/local")
async def upload_local(request: Request):
    """檔案已經在這台機器上：直接把原檔交給 Modal，不複製到暫存。

    只綁 127.0.0.1 且改狀態的請求會檢查 Origin，所以惡意網頁碰不到這個端點。
    """
    body = await request.json()
    src = os.path.abspath(str(body.get("path") or "").strip().strip('"'))
    if not os.path.exists(src):
        raise HTTPException(400, "找不到：{}".format(src))
    files = transfer.local_files(src)
    if not files:
        raise HTTPException(400, "底下沒有可轉的圖片（收 {}）".format(" ".join(sorted(convert.EXTS))))
    base = os.path.splitext(os.path.basename(src))[0] if os.path.isfile(src) \
        else os.path.basename(src.rstrip("\\/"))
    name = _name(body.get("name") or base)

    uid = uuid.uuid4().hex[:8]
    TRANSFERS[uid] = Transfer("upload", name, len(files))
    _start(_push, uid, files)
    return {"ok": True, "uid": uid, "name": name, "files": len(files)}


@app.post("/api/upload/start")
async def upload_start(request: Request):
    """瀏覽器上傳分三步：start → 每張 PUT → finish。

    需要先落地成暫存檔，是因為 Modal 的上傳器要求可 seek 的檔案物件，
    請求串流不行。檔案已經在這台機器上的話，用 /api/upload/local 可以省掉這次複製。
    """
    body = await request.json()
    name = _name(body.get("name"))
    uid = uuid.uuid4().hex[:8]
    STAGING[uid] = tempfile.mkdtemp(prefix="avifup_")
    TRANSFERS[uid] = Transfer("upload", name, int(body.get("files") or 0),
                              int(body.get("bytes") or 0))
    return {"ok": True, "uid": uid}


@app.put("/api/upload/{uid}")
async def upload_file(uid: str, request: Request, path: str):
    base, t = STAGING.get(uid), TRANSFERS.get(uid)
    if not base or not t or t.phase != "local":
        raise HTTPException(404, "這個上傳不存在或已經結束")
    rel = path.replace("\\", "/").strip("/")
    if not rel or any(p in ("", ".", "..") for p in rel.split("/")) \
            or os.path.splitext(rel)[1].lower() not in convert.EXTS:
        raise HTTPException(400, "路徑不合法：{}".format(path))

    dst = os.path.join(base, *rel.split("/"))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, "wb") as f:
        async for chunk in request.stream():
            f.write(chunk)
            t.sent += len(chunk)
    t.done += 1
    return {"ok": True}


@app.post("/api/upload/{uid}/finish")
def upload_finish(uid: str):
    base = STAGING.pop(uid, None)
    if not base:
        raise HTTPException(404, "這個上傳不存在或已經結束")
    _start(_push, uid, transfer.local_files(base), base)
    return {"ok": True}


def _pull(uid: str):
    t = TRANSFERS[uid]
    try:
        todo, n, t.total = transfer.plan_pull(vol(), t.name, t.dest)
        if not n:
            raise RuntimeError("out/{} 底下沒有 .avif".format(t.name))
        t.files, t.skipped, t.phase = len(todo), n - len(todo), "modal"
        if todo:
            transfer.pull(vol(), todo, t)
        t.finish()
        _invalidate_files()
    except Exception as e:
        t.fail(e)


@app.post("/api/pull")
async def pull(request: Request):
    """下載到這個 repo 的 out/<名稱>/。已經在本機而且大小一樣的會跳過。"""
    name = _name((await request.json()).get("name"))
    for t in TRANSFERS.values():
        if t.kind == "pull" and t.name == name and t.phase not in ("done", "error"):
            raise HTTPException(409, "這個資料夾正在下載")
    uid = uuid.uuid4().hex[:8]
    t = TRANSFERS[uid] = Transfer("pull", name)
    t.dest = os.path.join(OUT_DIR, *name.split("/"))
    _start(_pull, uid)
    return {"ok": True, "uid": uid, "dest": t.dest}


@app.post("/api/open")
async def open_folder(request: Request):
    """用檔案總管開下載好的資料夾。只開得了 out/ 底下的，路徑不由請求決定。"""
    name = _name((await request.json()).get("name"))
    dest = os.path.join(OUT_DIR, *name.split("/"))
    if not os.path.isdir(dest):
        raise HTTPException(404, "還沒下載到本機")
    os.startfile(dest)
    return {"ok": True}


@app.get("/api/transfers")
def transfers():
    now = time.time()
    for uid in [u for u, t in TRANSFERS.items() if t.ended and now - t.ended > 120]:
        TRANSFERS.pop(uid, None)
    return [{"uid": u, "kind": t.kind, "name": t.name, "phase": t.phase, "files": t.files,
             "done": t.done, "total": t.total, "sent": t.sent, "skipped": t.skipped,
             "error": t.error, "dest": t.dest} for u, t in TRANSFERS.items()]


# --------------------------------------------------------------------------
# 工作
# --------------------------------------------------------------------------

@app.post("/api/jobs")
async def create_job(request: Request):
    body = await request.json()
    name = _name(body.pop("name", None))
    try:
        params = jobspec.build(name, **body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return await run_in_threadpool(jobspec.submit, name, params)


@app.get("/api/jobs")
def list_jobs():
    snap = jobspec.snapshot(jd())          # 一次 round-trip，而不是每個 job 好幾次
    out = []
    for jid in reversed(snap.get("jobs:index") or []):
        j = jobspec.read_job(jid, snap)
        if j["state"] in jobspec.TERMINAL:
            j.pop("batches", None)          # 已結束的不需要每批明細
        else:
            # driver 不在了但狀態沒收尾＝中途死掉。只查執行中的，通常 0~1 個
            j["stale"] = bool(j["call_id"]) and jobspec.driver_alive(j["call_id"]) is False
        out.append(j)
    return out


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    jobspec.cancel(jd(), job_id)
    return {"ok": True}


@app.delete("/api/jobs/{job_id}")
def forget_job(job_id: str):
    jobspec.forget(jd(), job_id)
    return {"ok": True}


@app.post("/api/stop-app")
def stop_app():
    """逃生門：直接停掉整個 App，確保沒有任何容器在燒錢。之後要重新 deploy。"""
    exe = os.path.join(ROOT, ".venv", "Scripts", "modal.exe")
    if not os.path.exists(exe):
        exe = "modal"
    r = subprocess.run([exe, "app", "stop", jobspec.APP], capture_output=True, text=True)
    return {"ok": r.returncode == 0, "out": (r.stdout + r.stderr)[-2000:]}


# --------------------------------------------------------------------------
# 預算（跟 modal-av1 同一套：數字來自 Modal 的 billing API，額度存本機）
# --------------------------------------------------------------------------

BUDGET_FILE = os.path.join(ROOT, "budget.json")
BILLING_CACHE_FILE = os.path.join(ROOT, ".billing-cache.json")
BILLING_TTL = 600     # billing report 有速率限制，不能頻繁打


def _load_billing_cache() -> dict:
    """快取寫到磁碟：重啟 server 不該又去打一次。"""
    try:
        with open(BILLING_CACHE_FILE, encoding="utf-8") as f:
            c = json.load(f)
        return {"at": float(c["at"]), "data": c["data"]}
    except Exception:
        return {"at": 0.0, "data": None}


_billing_cache = _load_billing_cache()


def _allowance() -> float:
    """每月額度。Modal 的 API 只回報用掉多少，沒說上限，所以額度存在本機。"""
    try:
        with open(BUDGET_FILE, encoding="utf-8") as f:
            return float(json.load(f)["allowance"])
    except Exception:
        return 30.0


def _with_allowance(data: dict) -> dict:
    out = dict(data)
    out["allowance"] = _allowance()
    out["remaining"] = out["allowance"] - out["metered"]
    return out


def _fetch_billing() -> dict:
    w = modal.Workspace.from_context()
    s = w.billing.summary()

    # 報表只回傳完整的區間：用日解析度時今天整天都會被排除，而花費多半就在今天。
    # 近 7 天改用小時解析度（API 的上限），只剩當前這一小時不完整。
    now = datetime.datetime.now(datetime.timezone.utc)
    cut = max(s.start, (now - datetime.timedelta(days=6)).replace(
        minute=0, second=0, microsecond=0))
    rows = []
    if cut > s.start:
        rows += w.billing.report(start=s.start, end=cut, resolution="d")
    rows += w.billing.report(start=cut, resolution="h")

    daily, by_app = {}, {}
    for r in rows:
        day = r["interval_start"].astimezone().date().isoformat()   # 使用者想的是本地的今天
        cost = float(r["cost"])
        daily[day] = daily.get(day, 0.0) + cost
        by_app[r["description"]] = by_app.get(r["description"], 0.0) + cost

    return {
        "cycle_start": s.start.date().isoformat(),
        "cycle_end": s.end.date().isoformat(),
        "metered": float(s.metered_cost),
        "daily": [{"date": k, "cost": v} for k, v in sorted(daily.items())],
        "by_app": sorted(({"name": k, "cost": v} for k, v in by_app.items()),
                         key=lambda x: -x["cost"])[:6],
        "fetched_at": time.time(),
        "stale": False,
    }


@app.get("/api/billing")
def billing(refresh: bool = False):
    now = time.time()
    cached = _billing_cache["data"]
    if cached and not refresh and now - _billing_cache["at"] < BILLING_TTL:
        return _with_allowance(cached)
    try:
        data = _fetch_billing()
        _billing_cache.update(at=now, data=data)
        try:
            with open(BILLING_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump({"at": now, "data": data}, f)
        except OSError:
            pass
        return _with_allowance(data)
    except Exception as e:
        if cached:
            return dict(_with_allowance(cached), stale=True, error=repr(e)[:200])
        raise HTTPException(502, "讀取帳務失敗：{}".format(e))


@app.post("/api/billing/allowance")
async def set_allowance(request: Request):
    v = float((await request.json()).get("allowance", 30))
    if not (0 < v <= 100000):
        raise HTTPException(400, "額度不合理")
    with open(BUDGET_FILE, "w", encoding="utf-8") as f:
        json.dump({"allowance": v}, f)
    return {"ok": True, "allowance": v}


if __name__ == "__main__":
    import uvicorn
    print("開啟 http://127.0.0.1:{}".format(PORT))
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
