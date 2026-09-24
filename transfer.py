"""Volume 的上傳與下載，avif.py 和 web/server.py 共用。

兩邊都是「先規劃、再傳」：規劃時各叫一次 listdir 比對大小，已經在另一端
而且大小一樣的就跳過，所以中斷後重跑只補剩下的。VolumeListFiles 有速率
限制，規劃階段只叫這一次，傳的過程中不再列檔。
"""

import os
import threading
from concurrent.futures import ThreadPoolExecutor

from modal.exception import NotFoundError
from modal.volume import FileEntryType

import convert


def listdir(vol, path: str) -> list:
    """遞迴列出檔案（不含資料夾）。資料夾不存在時回傳空的。

    Modal 對不存在的路徑丟的是 modal.exception.NotFoundError，不是 FileNotFoundError。
    """
    try:
        return [e for e in vol.listdir(path, recursive=True) if e.type == FileEntryType.FILE]
    except (FileNotFoundError, NotFoundError):
        return []


def local_files(local: str) -> list:
    """回傳 [(本機路徑, 相對路徑)]。只收 convert.EXTS 的副檔名。"""
    if os.path.isfile(local):
        return [(local, os.path.basename(local))]
    out = []
    for d, dirs, fs in os.walk(local):
        dirs.sort()
        for f in sorted(fs):
            if os.path.splitext(f)[1].lower() in convert.EXTS:
                p = os.path.join(d, f)
                out.append((p, os.path.relpath(p, local).replace(os.sep, "/")))
    return out


class Progress:
    """上傳 / 下載進度。CLI 覆寫 changed() 來印，網頁直接讀欄位。

    同時也是 Modal 上傳器的進度回呼。protocol：cb(name=, size=) 回傳 task_id；
    之後 cb(task_id, advance=n)；結束時 cb(task_id=, complete=True)。
    """

    def __init__(self, files: int = 0, total: int = 0):
        self.files, self.total = files, total
        self.done = self.sent = 0

    def changed(self):
        pass

    def __call__(self, task_id=None, **kw):
        if kw.get("advance"):
            self.sent = min(self.total, self.sent + kw["advance"])
        if kw.get("complete"):
            self.done += 1
        self.changed()
        return 0


# --------------------------------------------------------------------------
# 上傳
# --------------------------------------------------------------------------

def plan_upload(vol, files: list, name: str):
    """files 是 [(本機路徑, 相對路徑)]。回傳 (todo, 跳過張數, 要傳的位元組)。"""
    root = "in/" + name
    remote = {e.path: e.size for e in listdir(vol, root)}
    todo, skipped, total = [], 0, 0
    for lp, rel in files:
        rp = "{}/{}".format(root, rel)
        size = os.path.getsize(lp)
        if remote.get(rp) == size:
            skipped += 1
            continue
        todo.append((lp, rp))
        total += size
    return todo, skipped, total


def upload(vol, todo: list, prog: Progress) -> None:
    """用 Modal 自己的進度回呼上傳（跟 modal CLI 同一條路）。

    batch_upload() 沒有把 progress_cb 露出來，只能走內部 API；它一出錯就
    退回公開的 batch_upload，只是沒有位元組進度，上傳本身不會因此壞掉。
    """
    try:
        from modal.volume import AbstractVolumeUploadContextManager
        vol.hydrate()
        with AbstractVolumeUploadContextManager.resolve(
                vol._get_metadata().version, vol.object_id, vol.client,
                progress_cb=prog, force=True) as batch:
            for lp, rp in todo:
                batch.put_file(lp, rp)
    except Exception as e:
        print("進度版上傳失敗，改用一般上傳：{!r}".format(e), flush=True)
        with vol.batch_upload(force=True) as batch:
            for lp, rp in todo:
                batch.put_file(lp, rp)
    prog.done, prog.sent = len(todo), prog.total
    prog.changed()


# --------------------------------------------------------------------------
# 下載
# --------------------------------------------------------------------------

def plan_pull(vol, out_name: str, dest: str):
    """回傳 (todo, 雲端總張數, 要下載的位元組)。todo 是 [(FileEntry, 本機路徑)]。"""
    root = "out/" + out_name
    entries = [e for e in listdir(vol, root) if e.path.endswith(".avif")]
    todo = []
    for e in entries:
        local = os.path.join(dest, *e.path[len(root) + 1:].split("/"))
        if os.path.exists(local) and os.path.getsize(local) == e.size:
            continue
        todo.append((e, local))
    return todo, len(entries), sum(e.size for e, _ in todo)


def pull(vol, todo: list, prog: Progress, workers: int = 8) -> None:
    """平行下載。先寫 .part 再 rename，中斷不會留下被當成完成的半個檔。"""
    lock = threading.Lock()

    def get(item):
        e, local = item
        os.makedirs(os.path.dirname(local), exist_ok=True)
        part = local + ".part"
        with open(part, "wb") as f:
            vol.read_file_into_fileobj(e.path, f)
        os.replace(part, local)
        with lock:
            prog.done += 1
            prog.sent += e.size
            prog.changed()

    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(get, todo))
