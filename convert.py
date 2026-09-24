"""Modal 端 AVIF 轉檔：列檔 → 分批 → 每個容器吃一整批。

部署： modal deploy convert.py
送件： python avif.py run <本機資料夾>   或分步 upload / submit / wait / pull

跟 modal-av1 同一套骨架：driver（0.25 核、nonpreemptible）列檔、分批，
.map() 給可搶佔的 worker 容器，進度寫進 Dict，每個 key 只有一個 writer。

不同的是工作粒度。影片一段要編好幾分鐘，圖片一張只要一兩秒 —— 冷啟動
比編碼本身還久，所以一個容器吃一整批，不是一張圖一個容器。也因為每張都短，
被搶佔的損失很小，worker 不需要 nonpreemptible。

續跑靠檔名：輸出已經存在就跳過，所以取消後重送、或某批被搶佔重跑，
都只會補做還沒完成的那幾張。每張先寫 .part 再 rename，半個檔不會被當成完成。
"""

from __future__ import annotations

import asyncio
import heapq
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import modal

app = modal.App("avif-convert")
vol = modal.Volume.from_name("avif", create_if_missing=True)
jobs = modal.Dict.from_name("avif-jobs", create_if_missing=True)

HERE = os.path.dirname(os.path.abspath(__file__))

# Modal 的 debian_slim 是 bookworm，只有 ImageMagick 6.9.11。trixie 才有 IM 7.1.1
# 和拆成獨立套件的 libheif 1.19 外掛（aomenc 編 AVIF、dav1d 讀 AVIF、libde265 讀
# iPhone 的 HEIC）。最後一步在建置時就確認 AVIF 是 rw，壞了 build 直接失敗，
# 不會等到轉檔才發現。
image = (modal.Image.from_registry("debian:trixie-slim", add_python="3.12")
    .apt_install("imagemagick", "libheif-plugin-aomenc", "libheif-plugin-dav1d",
                 "libheif-plugin-libde265", "icc-profiles-free")
    .pip_install("fastapi[standard]")
    .add_local_file(os.path.join(HERE, "policy.xml"), "/etc/ImageMagick-7/policy.xml", copy=True)
    .run_commands("magick -list format | grep -E '^ *AVIF +HEIC +rw'",
                  "test -f /usr/share/color/icc/sRGB.icc"))

driver_image = modal.Image.debian_slim()

with image.imports():
    from fastapi import HTTPException, Request, Response

HOUR = 3600
SRGB_ICC = "/usr/share/color/icc/sRGB.icc"
FILE_TIMEOUT = 15 * 60      # 單張上限；卡住的檔案記成失敗，不拖垮整批
HTTP_MAX = 64 * 2 ** 20     # HTTP 端點自己設的上限；Modal 本身的 body 上限要實測

# 批次模式收的副檔名。.avif 刻意不收：已經是 AVIF 的再壓一次只會掉畫質。
EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".tif", ".tiff", ".bmp", ".heic", ".heif"}

# 沿用 modal-av1 從帳單回歸出來的費率（見該 repo 的 RATE_CPU_S 說明）。
# 那是用十分鐘級的長容器校正的；這裡的容器短，開機成本佔比高，實際會偏高一些。
RATE_CPU_S = 0.0000262      # 美元 / 實體核心 / 秒
RATE_MEM_S = 0.00000276     # 美元 / GiB / 秒


def _usage(t0: float, cpu: float, mem_mib: int, nonpreemptible: bool = False) -> dict:
    el = time.time() - t0
    rate = cpu * RATE_CPU_S + (mem_mib / 1024.0) * RATE_MEM_S
    return {"elapsed": round(el, 1), "cpu": cpu, "mem": mem_mib,
            "cost": round(rate * el * (3 if nonpreemptible else 1), 5)}


# --------------------------------------------------------------------------
# 編碼參數：批次與 HTTP 兩條路的單一真相，jobspec.py 也從這裡拿
# --------------------------------------------------------------------------

ENCODE_DEFAULTS = {
    "quality": 90,      # IM 的 -quality，1–100
    "speed": 1,         # aom 的 speed，0 最慢最好、9 最快
    "depth": 8,         # 一定要明確指定：實測 16-bit PNG 不指定會被寫成 12-bit AVIF
    "chroma": "444",    # 動畫圖實測：比 420 大 16%，線條邊緣色差少一半以上
                        # （2026-09-25，5 張插畫 PNG，q90 speed 1）。照片用 420 就夠
    "strip": False,     # True：去掉 EXIF/XMP 等中繼資料，但保留 ICC
    "srgb": False,      # True：依內嵌 ICC 轉成 sRGB；False：原樣保留 ICC
}


def clean_params(p: dict) -> dict:
    """只留下影響輸出的欄位並檢查範圍。回傳值也是輸出資料夾 manifest 的內容。"""
    q = {k: v if p.get(k) is None else p[k] for k, v in ENCODE_DEFAULTS.items()}
    q["quality"] = int(q["quality"])
    q["speed"] = int(q["speed"])
    q["depth"] = int(q["depth"])
    q["chroma"] = str(q["chroma"])
    q["strip"] = bool(q["strip"])
    q["srgb"] = bool(q["srgb"])
    if not 1 <= q["quality"] <= 100:
        raise ValueError("quality 要在 1–100")
    if not 0 <= q["speed"] <= 9:
        raise ValueError("speed 要在 0–9")
    if q["depth"] not in (8, 10, 12):
        raise ValueError("depth 只能是 8、10、12")
    if q["chroma"] not in ("420", "422", "444"):
        raise ValueError("chroma 只能是 420、422、444")
    return q


# --------------------------------------------------------------------------
# 單張轉檔
# --------------------------------------------------------------------------

HEIF_BRANDS = (b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1")


def sniff(head: bytes):
    """看檔頭決定 coder，不看副檔名。

    明確寫成 JPEG:xxx 之後 IM 就不會再依內容猜格式；配合 policy 白名單，
    改了副檔名的 MVG/SVG 之類的東西進不來。副檔名寫錯的正常圖片也照樣能轉。
    """
    if head[:3] == b"\xff\xd8\xff":
        return "JPEG"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "PNG"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "GIF"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "WEBP"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "TIFF"
    if head[:2] == b"BM":
        return "BMP"
    if head[4:8] == b"ftyp":
        brands = head[8:min(len(head), int.from_bytes(head[:4], "big"))]
        if b"avif" in brands or b"avis" in brands:
            return "AVIF"
        if any(b in brands for b in HEIF_BRANDS):
            return "HEIC"
    return None


def _run(cmd: list, timeout: float = FILE_TIMEOUT) -> str:
    """跑 magick，失敗時把 stderr 的尾巴帶進例外訊息。"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError("超過 {} 秒沒轉完".format(int(timeout)))
    if r.returncode != 0:
        raise RuntimeError("{} 失敗（{}）：{}".format(cmd[0], r.returncode, r.stderr.strip()[-1000:]))
    return r.stdout


def magick_cmd(coder: str, src: str, dst: str, q: dict, threads: int, mem_mib: int) -> list:
    # -limit 要放在輸入前面才管得到讀檔；-auto-orient 要放在輸入後面，
    # 放前面會變成「no images found for operation」。
    # [0]：只取第一格。實測動態 GIF 丟給 AVIF writer 會被寫成好幾張靜態圖的
    # 集合（不是動畫），瀏覽器只顯示第一張，還白白多存了其他格。
    cmd = ["magick",
           "-limit", "thread", str(threads),
           "-limit", "memory", "{}MiB".format(max(64, mem_mib // 2)),
           "-limit", "map", "{}MiB".format(max(128, mem_mib)),
           "{}:{}[0]".format(coder, src),
           "-auto-orient"]
    if q["srgb"]:
        cmd += ["-profile", SRGB_ICC]
    if q["strip"]:
        cmd += ["+profile", "!icc,*"]
    cmd += ["-depth", str(q["depth"]),
            "-quality", str(q["quality"]),
            "-define", "heic:speed={}".format(q["speed"]),
            "-define", "heic:chroma={}".format(q["chroma"]),
            "AVIF:" + dst]
    return cmd


def convert_file(src: str, workdir: str, q: dict, mem_mib: int, threads: int = 1) -> dict:
    """把 src 轉成 workdir/out.avif。

    src 先 symlink 成 workdir/src 再交給 magick：IM 會解讀檔名裡的 [ ] % 等字元，
    使用者的檔名什麼都有，不能直接放進命令列。
    """
    with open(src, "rb") as f:
        head = f.read(64)
    coder = sniff(head)
    if coder is None:
        raise ValueError("不支援的格式（檔頭 {}）".format(head[:12].hex()))

    link = os.path.join(workdir, "src")
    out = os.path.join(workdir, "out.avif")
    os.symlink(os.path.abspath(src), link)

    frames = 1
    if coder in ("GIF", "WEBP"):
        n = _run(["magick", "identify", "-ping", "-format", "%n\n", "{}:{}".format(coder, link)],
                 timeout=60).split()
        frames = int(n[0]) if n else 1

    _run(magick_cmd(coder, link, out, q, threads, mem_mib))
    return {"out": out, "coder": coder, "frames": frames,
            "size_in": os.path.getsize(src), "size_out": os.path.getsize(out)}


def _publish(src_tmp: str, dst: str) -> None:
    """先寫 .part 再 rename：容器中途死掉不會在 dst 留下半個檔，
    否則下一輪會把半成品當成「已完成」跳過。"""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    part = dst + ".part"
    shutil.copyfile(src_tmp, part)
    os.replace(part, dst)


# --------------------------------------------------------------------------
# 批次 worker
# --------------------------------------------------------------------------

def _put_batch(job_id: str, idx: int, **kw) -> None:
    """每批只有自己會寫這個 key，不會有 read-modify-write 競態。"""
    jobs.put("job:{}:batch:{}".format(job_id, idx), dict(idx=idx, updated=time.time(), **kw))


@app.function(image=image, volumes={"/data": vol}, cpu=4, memory=8192,
              timeout=4 * HOUR, retries=2)
def convert_batch(arg: dict) -> dict:
    """轉一批。被搶佔重跑時，已經寫出去的會被跳過，只補剩下的。

    容器內以「檔案」為單位平行，每張只給 1 條 OpenMP 執行緒。實測 aom 不管
    -limit thread 設多少都會照主機核心數開約 20 條執行緒，單張吃不滿 4 核；
    4 張平行 0.70 秒、依序 2.01 秒。
    """
    t0 = time.time()
    job_id, idx, files, q = arg["job_id"], arg["idx"], arg["files"], arg["params"]
    cpu, mem = arg["cpu"], arg["mem"]
    workers = max(1, int(cpu))
    vol.reload()      # 暖容器可能是上一批留下來的，要看到最新上傳的檔案

    key = "job:{}:batch:{}".format(job_id, idx)
    prev = jobs.get(key) or {}        # 同一批不會同時有兩個容器，讀了再寫是安全的
    attempt = (prev.get("attempt") or 0) + 1
    if attempt > 1:
        print("第 {} 批第 {} 次嘗試（前一次被中斷）".format(idx, attempt))

    st = {"total": len(files), "converted": 0, "skipped": 0, "failed": 0,
          "bytes_in": 0, "bytes_out": 0}
    failures, animated = [], []
    lock = threading.Lock()           # 寫 Volume 與 commit 互斥

    def one(pair):
        src, dst = "/data/" + pair[0], "/data/" + pair[1]
        if os.path.exists(dst) and os.path.getsize(dst) > 0:
            return None
        work = tempfile.mkdtemp()
        try:
            info = convert_file(src, work, q, mem // workers)
            with lock:
                _publish(info["out"], dst)
            return info
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def report(state):
        _put_batch(job_id, idx, state=state, attempt=attempt, **st)

    report("converting")
    last_put = last_commit = time.time()
    with ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(one, pair): pair for pair in files}
        for fut in as_completed(futs):
            src = futs[fut][0]
            try:
                info = fut.result()
                if info is None:
                    st["skipped"] += 1
                else:
                    st["converted"] += 1
                    st["bytes_in"] += info["size_in"]
                    st["bytes_out"] += info["size_out"]
                    if info["frames"] > 1:
                        animated.append(src)
            except Exception as e:
                st["failed"] += 1
                failures.append({"path": src, "error": str(e)[:600]})
                print("失敗：{} {}".format(src, e))

            now = time.time()
            # 定期 commit：被搶佔時已經轉好的才留得住，重跑時才跳得過
            if now - last_commit > 30:
                with lock:
                    vol.commit()
                last_commit = now
            if now - last_put > 3:
                report("converting")
                last_put = now

    vol.commit()
    u = _usage(t0, cpu, mem)
    _put_batch(job_id, idx, state="done", attempt=attempt, **st, **u)
    print("第 {} 批：轉 {}、跳過 {}、失敗 {}，{:.1f}s".format(
        idx, st["converted"], st["skipped"], st["failed"], u["elapsed"]))
    return dict(st, failures=failures, animated=animated, usage=u, attempt=attempt)


# --------------------------------------------------------------------------
# 協調者
# --------------------------------------------------------------------------

def plan(in_root: str, out_root: str) -> list:
    """列出 in_root 底下要轉的圖，配好輸出路徑。回傳 [(src, dst, size)]，路徑相對 /data。

    同一個資料夾裡 a.jpg 和 a.png 會撞成同一個 a.avif，撞到的改用 a.jpg.avif。
    比對時不分大小寫：下載到 Windows 時 A.avif 和 a.avif 是同一個檔。
    """
    base = "/data/" + in_root
    rels = []
    for d, dirs, fs in os.walk(base):
        dirs.sort()
        for f in sorted(fs):
            if os.path.splitext(f)[1].lower() in EXTS:
                rels.append(os.path.relpath(os.path.join(d, f), base).replace(os.sep, "/"))

    count = {}
    for r in rels:
        k = os.path.splitext(r)[0].lower()
        count[k] = count.get(k, 0) + 1

    out = []
    for r in rels:
        stem = os.path.splitext(r)[0]
        target = (r if count[stem.lower()] > 1 else stem) + ".avif"
        out.append(("{}/{}".format(in_root, r), "{}/{}".format(out_root, target),
                    os.path.getsize("{}/{}".format(base, r))))
    return out


def make_batches(items: list, n: int) -> list:
    """大檔先排，每張丟給目前最輕的那批，讓每個容器的工作量差不多。"""
    heap = [(0, i) for i in range(n)]
    batches = [[] for _ in range(n)]
    for src, dst, size in sorted(items, key=lambda x: -x[2]):
        load, i = heapq.heappop(heap)
        batches[i].append((src, dst))
        heapq.heappush(heap, (load + size, i))
    return [sorted(b) for b in batches if b]


def check_manifest(out_root: str, q: dict, overwrite: bool) -> None:
    """輸出資料夾記下當初的參數。

    跳過已存在的輸出只在參數相同時才正確 —— 改了 quality 卻沿用舊檔，
    結果會是新舊參數混在一起而且看不出來。所以參數不同就拒絕，要使用者
    換 tag 或明講 overwrite。
    """
    out_abs = "/data/" + out_root
    man = out_abs + "/.avif-params.json"
    if overwrite and os.path.exists(out_abs):
        shutil.rmtree(out_abs)
    elif os.path.exists(man):
        with open(man, encoding="utf-8") as f:
            old = json.load(f)
        if old != q:
            diff = "、".join("{} {}→{}".format(k, old.get(k), q[k]) for k in q if old.get(k) != q[k])
            raise ValueError("{} 已經用別的參數轉過（{}）。換個 tag，或加 overwrite 整個重轉".format(
                out_root, diff))
    os.makedirs(out_abs, exist_ok=True)
    with open(man, "w", encoding="utf-8") as f:
        json.dump(q, f)
    vol.commit()


@app.function(image=driver_image, volumes={"/data": vol}, cpu=0.25, memory=512,
              timeout=24 * HOUR, nonpreemptible=True)
def run_job(job_id: str, name: str, p: dict) -> dict:
    """整趟的協調者。

    刻意設 nonpreemptible：它只要 0.25 核，但一旦它被搶佔，整個 job 的進度
    就斷了。真正花錢的轉檔留在便宜的可搶佔池裡。
    """
    key = "job:{}".format(job_id)
    state = {}
    t_job = time.time()
    usages = []

    def meta(**kw):
        """driver 是這個 key 的唯一 writer，狀態留在本地、每次整份寫出去，不回頭讀。"""
        state.update(kw)
        state["updated"] = time.time()
        jobs.put(key, dict(state))

    def totals():
        drv = _usage(t_job, 0.25, 512, nonpreemptible=True)
        all_u = usages + [drv]
        return {"cost": round(sum(u["cost"] for u in all_u), 4),
                "core_seconds": round(sum(u["cpu"] * u["elapsed"] for u in all_u)),
                "wall": round(time.time() - t_job),
                "containers": len(usages)}

    try:
        vol.reload()
        q = clean_params(p)
        in_root, out_root = "in/" + name, "out/" + p["out_name"]
        if not os.path.isdir("/data/" + in_root):
            raise ValueError("Volume 上沒有 {} 這個資料夾".format(in_root))

        meta(state="listing")
        items = plan(in_root, out_root)
        if not items:
            raise ValueError("{} 底下沒有可轉的圖片（收 {}）".format(
                in_root, " ".join(sorted(EXTS))))
        check_manifest(out_root, q, bool(p.get("overwrite")))

        n = max(1, -(-len(items) // int(p["batch"])))
        batches = make_batches(items, n)
        cpu, mem = int(p["cpu"]), int(p["mem"])
        meta(state="converting", total_batches=len(batches), total_files=len(items),
             out=out_root)

        args = [{"job_id": job_id, "idx": i, "files": b, "params": q, "cpu": cpu, "mem": mem}
                for i, b in enumerate(batches)]
        opts = {"cpu": cpu, "memory": mem}
        if p.get("max_containers"):
            opts["max_containers"] = int(p["max_containers"])
        results = list(convert_batch.with_options(**opts).map(args, return_exceptions=True))

        st = {"converted": 0, "skipped": 0, "failed": 0, "bytes_in": 0, "bytes_out": 0}
        failures, animated, retries = [], [], 0
        for b, r in zip(batches, results):
            if isinstance(r, Exception):
                # 整批在重試用完後仍然失敗：這批裡的檔案全記成失敗
                st["failed"] += len(b)
                failures += [{"path": s, "error": "整批失敗：{!r}".format(r)[:600]} for s, _ in b]
                continue
            for k in st:
                st[k] += r[k]
            failures += r["failures"]
            animated += r["animated"]
            retries += r["attempt"] - 1
            usages.append(r["usage"])

        res = dict(st, total_files=len(items), out=out_root,
                   failures=failures[:200], animated=animated[:200])
        meta(state="done", usage=dict(totals(), retries=retries), **res)
        return res

    except Exception as e:
        meta(state="error", error=str(e)[:2000] if isinstance(e, ValueError) else repr(e)[:2000])
        raise


# --------------------------------------------------------------------------
# 單張：HTTP 端點與自我檢查
# --------------------------------------------------------------------------

@app.function(image=image, cpu=2, memory=4096, timeout=10 * 60)
@modal.fastapi_endpoint(method="POST", requires_proxy_auth=True)
async def http(request: Request, quality: int | None = None, speed: int | None = None,
               depth: int | None = None, chroma: str | None = None,
               strip: bool | None = None, srgb: bool | None = None):
    """POST 原始圖片 bytes，回傳 AVIF。給本機以外的地方用。

    需要 proxy auth：請求要帶 Modal-Key / Modal-Secret 標頭（在 Modal 的
    workspace 設定建 proxy auth token）。參數走 query string，例如 ?quality=50，
    沒給的由 clean_params 補 ENCODE_DEFAULTS。
    """
    try:
        q = clean_params({"quality": quality, "speed": speed, "depth": depth,
                          "chroma": chroma, "strip": strip, "srgb": srgb})
    except ValueError as e:
        raise HTTPException(400, str(e))
    if int(request.headers.get("content-length") or 0) > HTTP_MAX:
        raise HTTPException(413, "檔案超過 {} MiB".format(HTTP_MAX // 2 ** 20))
    body = await request.body()
    if not body or len(body) > HTTP_MAX:
        raise HTTPException(413 if body else 400, "檔案是空的或太大")

    def work():
        tmp = tempfile.mkdtemp()
        try:
            src = os.path.join(tmp, "in")
            with open(src, "wb") as f:
                f.write(body)
            info = convert_file(src, tmp, q, 3072, threads=2)
            with open(info["out"], "rb") as f:
                return f.read(), info
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    try:
        data, info = await asyncio.to_thread(work)
    except ValueError as e:
        raise HTTPException(415, str(e))
    except RuntimeError as e:
        raise HTTPException(422, str(e))
    return Response(content=data, media_type="image/avif",
                    headers={"X-Input-Format": info["coder"], "X-Frames": str(info["frames"])})


@app.function(image=image, cpu=1, memory=1024)
def probe() -> str:
    """確認容器裡的 ImageMagick 真的能寫 AVIF，以及套用的是我們的 policy。"""
    out = []
    for cmd in (["magick", "-version"],
                ["bash", "-c", "magick -list format | grep -E '^ *(AVIF|HEIC|HEIF|WEBP|JPEG|PNG|GIF|TIFF|BMP)\\*? '"],
                ["bash", "-c", "ls /usr/lib/x86_64-linux-gnu/libheif/plugins/"],
                ["magick", "-list", "policy"]):
        r = subprocess.run(cmd, capture_output=True, text=True)
        out.append("$ {}\n{}{}".format(" ".join(cmd), r.stdout, r.stderr))
    return "\n".join(out)
