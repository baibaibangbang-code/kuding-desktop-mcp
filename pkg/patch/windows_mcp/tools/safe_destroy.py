"""PATCH 0.5.15：把上游三个「不可撤销」的默认行为改成可撤销。

审上游代码时挖出来的三处，都是同一个毛病：**破坏性操作的默认行为没有退路**。
一个 AI 工具拿着这些能力，路径判断错一次、或者被一段网页/文档里的指令带偏一次，
这台机器上的业务数据就没了——而这台机器上跑的是内部业务资料。

| 上游做法 | 后果 |
|---|---|
| `FileSystem(delete)` → `Path.unlink()` / `shutil.rmtree()` | 永久删除，不进回收站，没有撤销 |
| `FileSystem(write)` 覆盖已有文件 | 原内容直接没了，返回还写着 "Written to X" 像是新建 |
| `Registry(delete)` | 删键值没有退路，注册表删坏了系统起不来 |

**处理原则：不禁掉，改成可撤销。** 禁掉是砍能力（维护者 9-14 明确说过测试期别多搞规矩），
而且被禁的 agent 往往会绕路——用 PowerShell 去做同一件事，反而更没记录。
把删除改成进回收站、把覆盖改成先备份、把注册表删除改成先导出，
能力一点没少，**但每一步都留了回头路，而且返回里明确告诉调用方退路在哪**。
"""

import ctypes
import os
import shutil
import subprocess
import time
from ctypes import wintypes

# SHFileOperationW
FO_DELETE = 0x0003
FOF_ALLOWUNDO = 0x0040          # 这一位就是「进回收站」
FOF_NOCONFIRMATION = 0x0010     # 不弹系统确认框（我们是无人值守）
FOF_SILENT = 0x0004
FOF_NOERRORUI = 0x0400

BACKUP_MAX = 64 * 1024 * 1024   # 超过这个大小就不备份了，只警告——
                                # 免得备份本身把磁盘塞满，那是另一种事故


# SHFileOperation 的错误码对人是没有意义的。9-15 真撞上一次：
# 删剪映的测试素材时回了 32，光看数字完全不知道发生了什么——
# 其实是**剪映的草稿正开着那个文件**。
# 报一个数字等于没报；调用方要的是「我现在能做什么」。
_CODES = {
    32: "another program has that file open (close it and try again)",
    5: "access denied",
    2: "the file was not there by the time Windows looked",
    3: "the path was not found",
    26: "that drive has no Recycle Bin (network or removable drives often do not)",
    120: "this drive does not support moving to the Recycle Bin",
    1223: "the operation was cancelled",
    0x4C7: "the operation was cancelled",
}


def _why(rc):
    return _CODES.get(rc, "Windows refused the move to the Recycle Bin")


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def recycle(path):
    """删到回收站。成功返回 None，失败返回原因。

    `pFrom` 必须是**双 \\0 结尾**的字符串（它本来是给多文件列表用的），
    少一个 \\0 就会读到越界内存——这是 SHFileOperation 最经典的坑。
    """
    p = os.path.abspath(path)
    if not os.path.exists(p):
        return "path not found: %s" % p
    op = SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = FO_DELETE
    op.pFrom = p + "\0\0"
    op.pTo = None
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
    shell32 = ctypes.WinDLL("shell32")
    shell32.SHFileOperationW.argtypes = [ctypes.POINTER(SHFILEOPSTRUCTW)]
    shell32.SHFileOperationW.restype = ctypes.c_int
    rc = shell32.SHFileOperationW(ctypes.byref(op))
    if rc != 0:
        return "%s (SHFileOperation code %d)" % (_why(rc), rc)
    if op.fAnyOperationsAborted:
        return "the delete was aborted"
    return None


def backup_before_write(path):
    """覆盖前把原文件留一份。返回 (备份路径, 说明)，不需要备份时返回 (None, 说明)。"""
    p = os.path.abspath(path)
    if not os.path.exists(p) or not os.path.isfile(p):
        return None, None                      # 新建文件，没什么可留的
    try:
        size = os.path.getsize(p)
    except OSError:
        return None, None
    if size == 0:
        return None, None
    if size > BACKUP_MAX:
        return None, ("the existing file is %.1f MB, too big to back up automatically, so "
                      "its previous contents are GONE" % (size / 1048576.0))
    bak = "%s.bak-%s" % (p, time.strftime("%Y%m%d-%H%M%S"))
    try:
        shutil.copy2(p, bak)
        return bak, None
    except Exception as e:
        return None, "could not back it up (%s), so its previous contents are GONE" % e


def _reg_export_path(ps_path):
    """把 PowerShell 写法 `HKCU:\\Software\\X` 转成 reg.exe 认的 `HKCU\\Software\\X`。"""
    s = (ps_path or "").strip().strip('"')
    return s.replace(":\\", "\\", 1)


def export_registry(ps_path, out_dir=None):
    """删注册表之前先导出这一支。返回 (导出文件, 说明)。"""
    try:
        if out_dir is None:
            runtime_home = os.getenv("KUDING_DESKTOP_HOME", os.path.join(os.getenv("LOCALAPPDATA", os.path.expanduser("~")), "KudingDesktopMCP"))
            out_dir = os.path.join(runtime_home, "registry-backup")
        os.makedirs(out_dir, exist_ok=True)
        key = _reg_export_path(ps_path)
        safe = "".join(c if c.isalnum() else "_" for c in key)[:80]
        out = os.path.join(out_dir, "%s-%s.reg" % (safe, time.strftime("%Y%m%d-%H%M%S")))
        r = subprocess.run(["reg", "export", key, out, "/y"],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and os.path.exists(out):
            return out, None
        return None, (r.stderr or r.stdout or "reg export failed").strip()[:150]
    except Exception as e:
        return None, str(e)[:150]
