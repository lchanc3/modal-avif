"""本機 CLI。工作在雲端的 driver 手上跑，送出之後關掉這支、關機都不影響。

  python avif.py run 照片資料夾             上傳 → 送件 → 等 → 下載，一次走完
  python avif.py upload 照片資料夾          只上傳到 Volume 的 in/<名稱>/
  python avif.py submit 名稱 [參數]          送件，不等
  python avif.py wait <job id>             看進度直到結束
  python avif.py pull <job id|輸出名稱>     下載 out/<輸出名稱>/
  python avif.py status [job id]           單一 job 的明細（預設最近一個）
  python avif.py jobs                      最近的 job
  python avif.py cancel <job id>
  python avif.py ls [路徑]  /  rm <路徑>     看 / 清 Volume
  python avif.py probe                     確認雲端 ImageMagick 能寫 AVIF

先 modal deploy convert.py 一次，之後改了 convert.py 才要重新 deploy。
"""

import argparse
import os
import sys
import time

import modal
from modal.exception import NotFoundError
from modal.volume import FileEntryType

import convert
import jobspec
import transfer

MiB = 2 ** 20


def _mib(n) -> str:
    return "{:.1f} MiB".format((n or 0) / MiB)


def _dur(s) -> str:
    s = int(s or 0)
    return "{}m{:02d}s".format(s // 60, s % 60) if s >= 60 else "{}s".format(s)


class _Printer(transfer.Progress):
    """在同一行更新進度，最多每 0.5 秒印一次。"""

    def __init__(self, verb: str, files: int, total: int):
        super().__init__(files, total)
        self.verb, self.last = verb, 0.0

    def changed(self):
        now = time.time()
        if now - self.last > 0.5 or self.done == self.files:
            self.last = now
            print("\r{}中 {}/{} 張　{} / {}".format(
                self.verb, self.done, self.files, _mib(self.sent), _mib(self.total)),
                end="", flush=True)


# --------------------------------------------------------------------------
# 上傳
# --------------------------------------------------------------------------

def cmd_upload(local: str, name: str = None) -> str:
    local = os.path.abspath(local)
    if not os.path.exists(local):
        sys.exit("找不到 {}".format(local))
    files = transfer.local_files(local)
    if not files:
        sys.exit("{} 底下沒有可轉的圖片（收 {}）".format(local, " ".join(sorted(convert.EXTS))))

    base = os.path.splitext(os.path.basename(local))[0] if os.path.isfile(local) \
        else os.path.basename(local.rstrip("\\/"))
    name = jobspec.check_name(name or base)

    vol = jobspec.get_volume()
    todo, skipped, total = transfer.plan_upload(vol, files, name)
    print("{} → in/{}　共 {} 張，要傳 {} 張（{}），已在雲端 {} 張".format(
        local, name, len(files), len(todo), _mib(total), skipped))
    if todo:
        transfer.upload(vol, todo, _Printer("上傳", len(todo), total))
        print()
    return name


# --------------------------------------------------------------------------
# 送件與進度
# --------------------------------------------------------------------------

def cmd_submit(name: str, args) -> dict:
    over = {k: getattr(args, k, None) for k in jobspec.DEFAULTS}
    try:
        p = jobspec.build(name, preset=args.preset, **over)
    except ValueError as e:
        sys.exit(str(e))
    r = jobspec.submit(jobspec.check_name(name), p)
    print("已送出：in/{} → out/{}".format(name, r["out_name"]))
    print("參數：  [{label}]　quality {quality}　speed {speed}　depth {depth}　chroma {chroma}"
          "{z}{s}{c}　每容器 {cpu} 核 / {gib:g} GiB / 最多 {batch} 張".format(
              label=jobspec.PRESETS[p["preset"]]["label"],
              z="　縮成 {}%".format(p["resize"]) if p["resize"] < 100 else "",
              s="　strip" if p["strip"] else "", c="　轉 sRGB" if p["srgb"] else "",
              gib=p["mem"] / 1024, **p))
    print("job id：{}".format(r["job_id"]))
    return r


def _line(j: dict) -> str:
    s = "{state:10s} {processed}/{total_files} 張 ({pct:.1f}%)　轉 {converted}　沿用 {skipped}".format(**j)
    if j["failed"]:
        s += "　失敗 {}".format(j["failed"])
    if j["total_batches"]:
        done = sum(1 for b in j["batches"] if b.get("state") == "done")
        retry = sum(1 for b in j["batches"] if (b.get("attempt") or 1) > 1)
        s += "　批 {}/{}".format(done, j["total_batches"])
        if retry:
            s += "（{} 批被搶佔重跑）".format(retry)
    return s


def _load(job_id: str) -> dict:
    d = jobspec.get_dict()
    snap = jobspec.snapshot(d)
    if job_id is None:
        index = snap.get("jobs:index") or []
        if not index:
            sys.exit("還沒有任何 job")
        job_id = index[-1]
    if "job:{}:submit".format(job_id) not in snap:
        sys.exit("找不到 job {}".format(job_id))
    return jobspec.read_job(job_id, snap)


def _stale(j: dict) -> bool:
    """driver 不在了但狀態沒收尾＝中途死掉。

    queued 也要查：driver 若在寫第一筆狀態前就死了，會一直停在 queued。
    還在排隊的 driver 查起來是「執行中」，不會誤判。
    """
    return (j["state"] not in jobspec.TERMINAL
            and j["call_id"] and jobspec.driver_alive(j["call_id"]) is False)


def summary(j: dict) -> None:
    print("job {}　in/{} → out/{}".format(j["job_id"], j["name"], j["out_name"]))
    print("狀態：{}".format(_line(j)))
    if j["error"]:
        print("錯誤：{}".format(j["error"]))
    if j["bytes_in"]:
        print("大小：{} → {}（{:.1f}%，只算這次轉的）".format(
            _mib(j["bytes_in"]), _mib(j["bytes_out"]), j["bytes_out"] / j["bytes_in"] * 100))
    u = j["usage"]
    if u:
        s = "花費：約 ${:.4f}　容器 {} 個　{:.2f} 核時　牆鐘 {}".format(
            u.get("cost", 0), u.get("containers", 0), u.get("core_seconds", 0) / 3600,
            _dur(u.get("wall")))
        if u.get("retries"):
            s += "　被搶佔重跑 {} 次".format(u["retries"])
        print(s)
    if j["animated"]:
        print("動態圖只轉了第一格（{} 張）：".format(len(j["animated"])))
        for a in j["animated"][:10]:
            print("  " + a)
    if j["failures"]:
        print("失敗（列出前 {} 張）：".format(min(20, len(j["failures"]))))
        for f in j["failures"][:20]:
            print("  {}\n    {}".format(f["path"], f["error"].replace("\n", " ")[:300]))


def cmd_wait(job_id: str) -> dict:
    last = ""
    while True:
        j = _load(job_id)
        line = _line(j)
        if line != last:
            print("\r" + line.ljust(len(last)), end="", flush=True)
            last = line
        if j["state"] in jobspec.TERMINAL:
            print()
            summary(j)
            return j
        if _stale(j):
            print("\ndriver 已經不在了，狀態卻沒收尾。用 cancel 清掉再重送，已轉好的會沿用。")
            return j
        time.sleep(5)


def cmd_jobs(n: int) -> None:
    d = jobspec.get_dict()
    snap = jobspec.snapshot(d)
    for jid in list(reversed(snap.get("jobs:index") or []))[:n]:
        j = jobspec.read_job(jid, snap)
        print("{}  {:24s} {}".format(jid, j["out_name"][:24], _line(j)))


# --------------------------------------------------------------------------
# 下載
# --------------------------------------------------------------------------

def cmd_pull(target: str, dest: str = None) -> None:
    """target 可以是 job id 或 out/ 底下的資料夾名。"""
    out_name = target
    if target[:8].isdigit() and target.count("-") == 2:
        out_name = _load(target)["out_name"]
    out_name = jobspec.check_name(out_name)
    dest = os.path.abspath(dest or os.path.join("out", *out_name.split("/")))

    vol = jobspec.get_volume()
    todo, n, total = transfer.plan_pull(vol, out_name, dest)
    if not n:
        sys.exit("out/{} 底下沒有 .avif".format(out_name))
    print("out/{} → {}　共 {} 張，要下載 {} 張（{}）".format(
        out_name, dest, n, len(todo), _mib(total)))
    if todo:
        transfer.pull(vol, todo, _Printer("下載", len(todo), total))
        print()
    print("完成：{}".format(dest))


# --------------------------------------------------------------------------
# 其他
# --------------------------------------------------------------------------

def cmd_ls(path: str) -> None:
    vol = jobspec.get_volume()
    path = (path or "").strip("/")
    try:
        entries = vol.listdir(path or "/")
    except (FileNotFoundError, NotFoundError):
        sys.exit("找不到 {}".format(path))
    for e in sorted(entries, key=lambda e: e.path):
        size = "<dir>" if e.type == FileEntryType.DIRECTORY else _mib(e.size)
        print("{:>12s}  {}".format(size, e.path))


def cmd_rm(path: str, yes: bool) -> None:
    path = jobspec.check_name(path)
    if not path.startswith(("in/", "out/")):
        sys.exit("只能刪 in/ 或 out/ 底下的東西")
    vol = jobspec.get_volume()
    files = transfer.listdir(vol, path)
    print("{}：{} 個檔，{}".format(path, len(files), _mib(sum(e.size for e in files))))
    if not yes and input("確定刪除？刪了無法復原 (y/N) ").strip().lower() != "y":
        print("已取消")
        return
    vol.remove_file(path, recursive=True)
    print("已刪除", path)


def cmd_probe() -> None:
    print(modal.Function.from_name(jobspec.APP, "probe").remote())


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser(description="在 Modal 上用 CPU 把圖片批次轉成 AVIF")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def enc_opts(sp):
        g = sp.add_argument_group("編碼參數（沒給就用 --preset 的值）")
        g.add_argument("--preset", choices=list(jobspec.PRESETS),
                       help="預設組合，預設 {}。".format(jobspec.DEFAULT_PRESET) + "　".join(
                           "{}：{}".format(k, v["label"]) for k, v in jobspec.PRESETS.items()))
        g.add_argument("-q", "--quality", type=int,
                       help="1–100，預設 {}".format(jobspec.DEFAULTS["quality"]))
        g.add_argument("--speed", type=int,
                       help="0–9，越小越慢越好，預設 {}".format(jobspec.DEFAULTS["speed"]))
        g.add_argument("--depth", type=int, choices=(8, 10, 12), help="位元深度，預設 8")
        g.add_argument("--chroma", choices=("420", "422", "444"), help="色度取樣，預設 {}".format(jobspec.DEFAULTS["chroma"]))
        g.add_argument("--resize", type=int, help="縮放百分比 1–100，例如 50 = 長寬各一半，預設 100")
        g.add_argument("--strip", action="store_true", default=None,
                       help="去掉 EXIF/XMP 等中繼資料（ICC 保留）")
        g.add_argument("--srgb", action="store_true", default=None,
                       help="依內嵌 ICC 轉成 sRGB，預設原樣保留 ICC")
        g.add_argument("--tag", help="輸出資料夾加後綴，同一批圖可以用不同參數各轉一份")
        g.add_argument("--overwrite", action="store_true", default=None,
                       help="整個輸出資料夾刪掉重轉")
        g = sp.add_argument_group("容器")
        g.add_argument("--cpu", type=int, help="每個容器的核心數，預設 {}".format(jobspec.DEFAULTS["cpu"]))
        g.add_argument("--batch", type=int, help="每個容器最多幾張，預設 {}".format(jobspec.DEFAULTS["batch"]))
        g.add_argument("--max-containers", type=int, dest="max_containers",
                       help="同時最多幾個容器，預設 {}".format(jobspec.DEFAULTS["max_containers"]))
        g.add_argument("--mem-per-image", type=int, dest="mem_per_image",
                       help="每張圖分到的記憶體（MiB），容器記憶體 = 核心數 × 這個值，預設 {}".format(
                           jobspec.DEFAULTS["mem_per_image"]))

    sp = sub.add_parser("run", help="上傳 → 送件 → 等 → 下載")
    sp.add_argument("local", help="本機資料夾（或單一檔案）")
    sp.add_argument("--name", help="Volume 上的名稱，預設用資料夾名")
    sp.add_argument("--dest", help="下載到哪裡，預設 ./out/<輸出名稱>")
    sp.add_argument("--no-pull", action="store_true", help="轉完不下載")
    enc_opts(sp)

    sp = sub.add_parser("upload", help="上傳到 Volume 的 in/<名稱>/")
    sp.add_argument("local")
    sp.add_argument("--name")

    sp = sub.add_parser("submit", help="送件，不等")
    sp.add_argument("name", help="in/ 底下的資料夾名")
    enc_opts(sp)

    for c, h in (("wait", "看進度直到結束"), ("cancel", "取消")):
        sub.add_parser(c, help=h).add_argument("job")
    sub.add_parser("status", help="單一 job 的明細").add_argument("job", nargs="?")
    sub.add_parser("jobs", help="最近的 job").add_argument("-n", type=int, default=10)

    sp = sub.add_parser("pull", help="下載 out/<輸出名稱>/")
    sp.add_argument("target", help="job id 或 out/ 底下的資料夾名")
    sp.add_argument("--dest")

    sub.add_parser("ls", help="看 Volume").add_argument("path", nargs="?", default="")
    sp = sub.add_parser("rm", help="刪 Volume 上 in/ 或 out/ 底下的東西")
    sp.add_argument("path")
    sp.add_argument("-y", "--yes", action="store_true")
    sub.add_parser("probe", help="確認雲端 ImageMagick 能寫 AVIF")

    a = ap.parse_args()

    if a.cmd == "run":
        name = cmd_upload(a.local, a.name)
        r = cmd_submit(name, a)
        j = cmd_wait(r["job_id"])
        if j["state"] == "done" and not a.no_pull:
            cmd_pull(j["out_name"], a.dest)
    elif a.cmd == "upload":
        cmd_upload(a.local, a.name)
    elif a.cmd == "submit":
        cmd_submit(a.name, a)
    elif a.cmd == "wait":
        cmd_wait(a.job)
    elif a.cmd == "status":
        j = _load(a.job)
        summary(j)
        if _stale(j):
            print("driver 已經不在了，狀態卻沒收尾。用 cancel 清掉再重送，已轉好的會沿用。")
    elif a.cmd == "jobs":
        cmd_jobs(a.n)
    elif a.cmd == "cancel":
        jobspec.cancel(jobspec.get_dict(), a.job)
        print("已取消", a.job)
    elif a.cmd == "pull":
        cmd_pull(a.target, a.dest)
    elif a.cmd == "ls":
        cmd_ls(a.path)
    elif a.cmd == "rm":
        cmd_rm(a.path, a.yes)
    elif a.cmd == "probe":
        cmd_probe()


if __name__ == "__main__":
    main()
