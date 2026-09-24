"""送件與讀進度的共用邏輯。avif.py 與 web/server.py 都走這支，行為不分岔。

Dict 的 key 配置刻意做到「每個 key 只有一個 writer」，完全避開
read-modify-write 競態：

    jobs:index              本機（送件時 append）
    job:{id}:submit         本機（送件時寫一次，含 driver 的 call id）
    job:{id}                雲端 driver（狀態機）
    job:{id}:batch:{i}      該批自己的容器（進度）
    job:{id}:cancelled      本機（取消時間戳）
"""

import time
import uuid

import modal

import convert

APP = "avif-convert"
DICT = "avif-jobs"
VOLUME = "avif"

DEFAULTS = dict(convert.ENCODE_DEFAULTS,
                cpu=4,              # 每個 worker 容器的核心數，容器內每核跑一張
                batch=40,           # 每個容器最多吃幾張。speed 1 的 12MP 照片每張約
                                    # 1–2 核分鐘，40 張 ÷ 4 核 ≈ 一個容器 15 分鐘：
                                    # 冷啟動攤得掉，被搶佔也只賠一小批
                max_containers=20,  # 同時最多幾個容器，免得一大批圖一口氣開上百個
                tag="",
                overwrite=False)

TERMINAL = ("done", "error", "cancelled")


def get_dict():
    return modal.Dict.from_name(DICT, create_if_missing=True)


def get_volume():
    return modal.Volume.from_name(VOLUME, create_if_missing=True)


def check_name(name: str) -> str:
    """Volume 上 in/ 底下的資料夾名。可以有子目錄，但不能往上跳。"""
    name = (name or "").replace("\\", "/").strip("/")
    if not name or any(part in ("", ".", "..") for part in name.split("/")):
        raise ValueError("資料夾名稱不合法：{!r}".format(name))
    return name


def build(name: str, **over) -> dict:
    """把使用者給的參數補上預設值，並算出輸出資料夾名。"""
    p = dict(DEFAULTS)
    p.update({k: v for k, v in over.items() if v is not None and k in DEFAULTS})
    p.update(convert.clean_params(p))

    p["cpu"] = int(p["cpu"])
    p["mem"] = p["cpu"] * 2048      # 大圖的 pixel cache 加上 aom 本身，每張抓 2 GiB
    p["batch"] = max(1, int(p["batch"]))
    p["max_containers"] = int(p["max_containers"] or 0)
    p["overwrite"] = bool(p["overwrite"])
    p["tag"] = str(p["tag"] or "").strip()
    if "/" in p["tag"] or "\\" in p["tag"]:
        raise ValueError("tag 不能有斜線")

    # tag 讓同一批圖可以用不同參數各轉一份而不互相覆蓋
    name = check_name(name)
    p["out_name"] = "{}_{}".format(name, p["tag"]) if p["tag"] else name
    return p


def submit(name: str, p: dict) -> dict:
    """spawn driver 並登記到 Dict。送出後本機不需要保持連線。"""
    d = get_dict()
    job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]

    call = modal.Function.from_name(APP, "run_job").spawn(job_id, name, p)

    d.put("job:{}:submit".format(job_id), {
        "job_id": job_id,
        "name": name,
        "params": p,
        "out_name": p["out_name"],
        "created": time.time(),
        "driver_call_id": call.object_id,
    })
    # job:{id} 不在這裡寫 "queued"：它的唯一 writer 是 driver。spawn 之後才寫的話，
    # driver 若一秒內就出錯（例如參數與既有輸出不符），它寫的 error 會被這裡晚到的
    # queued 蓋掉，job 永遠卡在 queued。沒有這個 key 時 read_job 本來就當成 queued。

    index = d.get("jobs:index") or []
    index.append(job_id)
    d.put("jobs:index", index[-200:])

    return {"job_id": job_id, "call_id": call.object_id, "out_name": p["out_name"]}


def driver_alive(call_id: str):
    """driver 是否還活著。

    不用 heartbeat —— driver 阻塞在 .map() 時沒辦法更新任何東西。
    FunctionCall 的狀態才是權威來源。True=執行中、False=已結束。
    """
    try:
        modal.FunctionCall.from_id(call_id).get(timeout=0)
        return False
    except TimeoutError:
        return True
    except Exception:
        return False


def snapshot(d) -> dict:
    """一次把整個 Dict 讀下來。單次 get 約 0.47s，整個 Dict 一次抓完約 2s，
    批數一多逐 key 讀就輪詢不動了。"""
    return {k: v for k, v in d.items()}


def read_job(job_id: str, snap: dict) -> dict:
    """把一個 job 的所有 key 併成一份。"""
    sub = snap.get("job:{}:submit".format(job_id)) or {}
    meta = snap.get("job:{}".format(job_id)) or {}

    state = meta.get("state", "queued")
    cancelled = snap.get("job:{}:cancelled".format(job_id))
    # 取消後 driver 在被終止前可能還會收到 .map() 的取消例外，把「整批失敗」寫成
    # done。所以 done 如果是取消之後才寫的，一樣算取消。
    if cancelled and (state not in TERMINAL or meta.get("updated", 0) >= cancelled):
        state = "cancelled"

    n = meta.get("total_batches") or 0
    batches = [snap.get("job:{}:batch:{}".format(job_id, i)) or {"idx": i, "state": "waiting"}
               for i in range(n)]

    counts = {k: meta[k] if state == "done" and k in meta
              else sum(b.get(k, 0) for b in batches)
              for k in ("converted", "skipped", "failed", "bytes_in", "bytes_out")}
    total = meta.get("total_files") or 0
    processed = counts["converted"] + counts["skipped"] + counts["failed"]

    failures = meta.get("failures") or []
    if state == "cancelled":
        # 取消造成的「整批失敗」不是這些檔案本身有問題，不列進失敗清單
        failures = [f for f in failures if not f["error"].startswith("整批失敗")]

    return dict(counts,
                job_id=job_id,
                name=sub.get("name", "?"),
                out_name=sub.get("out_name", ""),
                params=sub.get("params", {}),
                created=sub.get("created", 0),
                call_id=sub.get("driver_call_id", ""),
                state=state,
                total_files=total,
                processed=processed,
                pct=100.0 if state == "done" else round(processed / total * 100, 1) if total else 0.0,
                total_batches=n,
                batches=batches,
                failures=failures,
                animated=meta.get("animated") or [],
                usage=meta.get("usage") or {},
                error=meta.get("error", ""),
                updated=meta.get("updated", 0))


def cancel(d, job_id: str) -> None:
    sub = d.get("job:{}:submit".format(job_id)) or {}
    call_id = sub.get("driver_call_id")
    if call_id:
        try:
            modal.FunctionCall.from_id(call_id).cancel(terminate_containers=True)
        except Exception:
            pass
    d.put("job:{}:cancelled".format(job_id), time.time())


def forget(d, job_id: str) -> None:
    """把一個 job 的所有 key 從 Dict 移除（不動 Volume 上的檔案）。"""
    meta = d.get("job:{}".format(job_id)) or {}
    keys = ["job:{}:batch:{}".format(job_id, i) for i in range(meta.get("total_batches") or 0)]
    keys += [k.format(job_id) for k in ("job:{}", "job:{}:submit", "job:{}:cancelled")]
    for k in keys:
        try:
            d.pop(k)
        except KeyError:
            pass
    d.put("jobs:index", [j for j in (d.get("jobs:index") or []) if j != job_id])
