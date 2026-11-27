"""PATCH 0.5.20：这只鼠标此刻归谁——另一个 agent，还是人。

两件事放一个模块，因为它们回答的是同一个问题，而且答案都只有一个「谁在用输入」：

1. **两个 agent 抢桌面**。9-15 本机实测撞了一整天：我在跑测试（要造遮挡、
   要把窗口提来提去），opencode 同时在打牌，互相把对方的前台抢走。
   表现是 test_type_here / test_app_window / test_overlay_live 这类碰实时桌面的
   suite 偶发失败、单独重跑就绿——**不是回归，是两个 agent 抢同一个桌面**。

2. **人来了要让路**。这条才是真正要紧的：以后 AI 替人操作电脑，
   人一动鼠标键盘，AI 就该停下来把桌面还给人，而不是跟人抢鼠标。
   维护者这台机器上开着微信、钉钉和真实业务数据，跟人抢鼠标是会出事的。

**怎么判断「人在动」**：只能靠低层钩子的 injected 位。
`GetLastInputInfo` 完全没用——它把我们自己合成的输入也算进去，
我们每点一下它就刷新，永远显示「人刚刚用过」。这跟 overlay.py 里
Esc 叫停只认非注入按键是同一个道理，也是同一个坑。

**为什么用命名互斥体而不是锁文件**：互斥体是内核对象，
**进程一死系统自动释放**。锁文件做不到——agent 被杀（我一天要重启
opencode 好几次）就会留下一把谁也拿不走的锁，然后所有人都卡住。
身份信息（谁拿着、在干嘛）另外写个小文件，那个丢了也不影响正确性，
只影响报错时说得清不清楚。
"""

import ctypes
import json
import os
import tempfile
import threading
import time
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
LLKHF_INJECTED = 0x10
LLKHF_LOWER_IL_INJECTED = 0x02
LLMHF_INJECTED = 0x01
LLMHF_LOWER_IL_INJECTED = 0x02
WM_MOUSEMOVE = 0x0200

WAIT_OBJECT_0 = 0x00000000
WAIT_ABANDONED = 0x00000080
WAIT_TIMEOUT = 0x00000102

# 人停手多久算「人不在用了」。默认 2 秒：再短会在人两次动作的间隙里插进去，
# 再长则人只要在旁边偶尔碰一下鼠标，AI 就彻底干不了活。
# 设成 0 关掉这条（比如无人值守的定时任务，本来就没人在跟前）。
def _yield_window():
    try:
        return max(0.0, float(os.environ.get("UIA_YIELD_TO_HUMAN", "2")))
    except Exception:
        return 2.0


def _lock_enabled():
    return os.environ.get("UIA_DESKTOP_LOCK", "1") not in ("0", "false", "no")


def _env_secs(name, default):
    try:
        return max(0.0, float(os.environ.get(name, default)))
    except Exception:
        return float(default)


# 等锁和等人，要各自封顶，**不能直接用调用方的整个预算**。
#
# 9-15 接 Codex 时发现的：它的客户端截止时间配到 60 秒，于是预算是 55 秒。
# 如果照预算等锁，另一个 agent 手上跑着长序列时，Codex 这边就会**干等 55 秒**。
# 那不叫等待，那叫卡死——而且它什么反馈都拿不到，只能盯着一个不返回的调用。
#
# 等锁 15 秒：足够骑过一次普通动作（约 1 秒）和一段九步序列（实测 5.4 秒）。
# 等人 8 秒：人真在用电脑的话，他可能还要用好几分钟，等下去没有意义，
# 不如快点回去告诉调用方"人在用"，让它自己决定是改期还是问一句。
LOCK_WAIT_CAP = _env_secs("UIA_LOCK_WAIT", 15)
HUMAN_WAIT_CAP = _env_secs("UIA_HUMAN_WAIT", 8)


_MUTEX_NAME = "Local\\uia-desktop-input-v1"
_INFO_PATH = os.path.join(tempfile.gettempdir(), "uia-desktop-input-owner.json")

# seen 统计的是**钩子回调被调用的总次数**（不管注入与否）。
# 它只有一个用途：让测试能区分"钩子装上了、看到了我们的合成输入、
# 并正确判定成非人类"和"钩子根本没装上，所以什么都没记"——
# 这两种情况下 last_human 都是 0，光看 last_human 的断言是白过的。
_state = {"last_human": 0.0, "last_pos": None, "started": False, "seen": 0}
_slock = threading.Lock()


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt_x", ctypes.c_long), ("pt_y", ctypes.c_long),
                ("mouseData", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_void_p)]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_void_p)]


def _note_human():
    with _slock:
        _state["last_human"] = time.time()


def _watch():
    """装两个低层钩子，只记「**不是我们合成的**那些输入」发生在什么时候。

    钩子只看不吞：一律 CallNextHookEx，人的操作该怎样还怎样。
    """
    HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, ctypes.c_int,
                                  ctypes.c_ulonglong, ctypes.c_longlong)
    user32.SetWindowsHookExW.restype = wintypes.HANDLE
    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC,
                                         wintypes.HINSTANCE, wintypes.DWORD]
    user32.CallNextHookEx.restype = ctypes.c_longlong
    user32.CallNextHookEx.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                      ctypes.c_ulonglong, ctypes.c_longlong]

    def on_mouse(code, wparam, lparam):
        try:
            if code >= 0:
                _state["seen"] += 1
                ms = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                if not (ms.flags & (LLMHF_INJECTED | LLMHF_LOWER_IL_INJECTED)):
                    if wparam == WM_MOUSEMOVE:
                        # 纯移动要过一个门槛。鼠标垫上的一点抖动、
                        # 桌子被碰一下都会产生 1-2 像素的移动事件，
                        # 那不是「人来了」，只是桌子在震。
                        p = (ms.pt_x, ms.pt_y)
                        q = _state.get("last_pos")
                        _state["last_pos"] = p
                        if q is not None and abs(p[0] - q[0]) + abs(p[1] - q[1]) < 8:
                            return user32.CallNextHookEx(None, code, wparam, lparam)
                    _note_human()
        except Exception:
            pass
        return user32.CallNextHookEx(None, code, wparam, lparam)

    def on_key(code, wparam, lparam):
        try:
            if code >= 0:
                _state["seen"] += 1
                kb = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                if not (kb.flags & (LLKHF_INJECTED | LLKHF_LOWER_IL_INJECTED)):
                    _note_human()
        except Exception:
            pass
        return user32.CallNextHookEx(None, code, wparam, lparam)

    pm = HOOKPROC(on_mouse)      # 必须留引用，否则回调变野指针
    pk = HOOKPROC(on_key)
    _state["_procs"] = (pm, pk)
    try:
        # hMod 传 NULL：回调是 ctypes 生成的 thunk，不属于任何模块，
        # 传当前 exe 的句柄会得到 NULL + 错误码 126 ERROR_MOD_NOT_FOUND。
        # overlay.py 的 Esc 钩子就是这么静默失效了整整一版。
        _state["hook_mouse"] = user32.SetWindowsHookExW(WH_MOUSE_LL, pm, None, 0)
        _state["hook_key"] = user32.SetWindowsHookExW(WH_KEYBOARD_LL, pk, None, 0)
    except Exception:
        _state["hook_mouse"] = _state["hook_key"] = None
    if not _state.get("hook_mouse") or not _state.get("hook_key"):
        # 装不上就别假装能检测到人。让路这条要么真的在守，要么就明说守不住。
        return
    # 低层钩子要求装钩子的线程有消息循环，否则回调永远不会被调用
    msg = ctypes.create_string_buffer(64)
    while True:
        user32.PeekMessageW(msg, None, 0, 0, 1)
        time.sleep(0.02)


def start():
    """起监视线程。多次调用只起一次。"""
    with _slock:
        if _state["started"]:
            return
        _state["started"] = True
    t = threading.Thread(target=_watch, daemon=True, name="uia-human-watch")
    t.start()
    # 钩子装上之前的那一小段时间是瞎的，等它一下
    time.sleep(0.05)


def watching():
    """两个钩子都装上了吗。装不上就说明「人来了让路」这条根本没在守。"""
    start()
    return bool(_state.get("hook_mouse")) and bool(_state.get("hook_key"))


def human_idle():
    """人停手多久了（秒）。从没见过人动就返回一个很大的数。"""
    start()
    with _slock:
        last = _state["last_human"]
    if not last:
        return 1e9
    return time.time() - last


def human_active():
    """人此刻是不是在用这台电脑。"""
    w = _yield_window()
    return w > 0 and human_idle() < w


# ---- 跨进程互斥 -------------------------------------------------------

_tls = threading.local()


def _open_mutex():
    h = getattr(_tls, "mutex", None)
    if h:
        return h
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    h = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    _tls.mutex = h
    return h


def _write_owner(tool):
    try:
        with open(_INFO_PATH, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "tool": tool, "since": time.time(),
                       "exe": os.path.basename(os.sys.executable)}, f)
    except Exception:
        pass


def _clear_owner():
    try:
        os.remove(_INFO_PATH)
    except Exception:
        pass


def owner():
    """现在谁拿着桌面。拿不到身份信息就返回 None——
    身份文件只影响报错说得清不清楚，不影响互斥本身的正确性。"""
    try:
        with open(_INFO_PATH, encoding="utf-8") as f:
            d = json.load(f)
        if int(d.get("pid", 0)) == os.getpid():
            return None
        return d
    except Exception:
        return None


def _held_for():
    d = owner()
    if not d:
        return None
    try:
        return time.time() - float(d.get("since") or 0)
    except Exception:
        return None


def _describe_owner():
    d = owner()
    if not d:
        return "another uia-desktop process"
    held = time.time() - float(d.get("since") or 0)
    return "pid=%s (%s, running %s, holding it for %.0fs)" % (
        d.get("pid"), d.get("exe") or "?", d.get("tool") or "?", held)


def _what_to_do(held):
    """告诉调用方接下来该干嘛——**看对方已经占了多久，别给一句放之四海的废话**。

    9-15 压测时逮到的：我原来固定结尾写"Try again - a normal action finishes in
    about a second"，而同一句话的前半段刚刚告诉对方"这个人已经占了 85 秒"。
    自相矛盾的建议比没有建议更坏：调用方照着"一秒就好"去重试，
    只会连着撞上好几次，每次白等 15 秒、白烧一轮推理。
    """
    if held is None:
        return ("Try again in a moment - most actions hold the desktop for about a second.")
    if held < 10:
        return ("Try again - it has only been holding for %.0fs, so it is probably a normal "
                "action that is about to finish." % held)
    return ("It has already been holding for %.0fs, so this is NOT a quick action - retrying "
            "immediately will just burn another %.0fs wait. Either come back later, or do "
            "something that does not need the mouse in the meantime: reading tools "
            "(Screenshot, ReadText, FindText, Window) are never blocked by this."
            % (held, LOCK_WAIT_CAP))


class Busy(Exception):
    """桌面此刻被别人占着——人，或者另一个 agent。"""


def acquire(tool, budget):
    """拿到「可以动这只鼠标」的资格，拿不到就抛 Busy。

    两道门，顺序不能反：**先等人，再跟别的 agent 排队**。
    人优先——人在动的时候，两个 agent 谁也不该动。
    """
    budget = max(0.0, float(budget))
    human_deadline = time.time() + min(budget, HUMAN_WAIT_CAP)
    lock_deadline = time.time() + min(budget, LOCK_WAIT_CAP)

    if _yield_window() > 0:
        waited = 0.0
        while human_active():
            if time.time() >= human_deadline:
                raise Busy(
                    "DEFERRED - somebody is using this computer right now (real mouse or "
                    "keyboard input %.1fs ago, and it was not ours). Nothing was done. "
                    "uia-desktop gets out of the way instead of fighting a person for the "
                    "mouse: a click landing where the human just moved the pointer is how "
                    "real damage happens. Waited %.1fs. Try again in a moment, or ask the "
                    "person whether they want you to go ahead."
                    % (human_idle(), waited))
            time.sleep(0.15)
            waited += 0.15

    if not _lock_enabled():
        return False

    h = _open_mutex()
    if not h:
        return False
    ms = max(0, int((lock_deadline - time.time()) * 1000))
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    held_for = _held_for()
    r = kernel32.WaitForSingleObject(h, ms)
    if r in (WAIT_OBJECT_0, WAIT_ABANDONED):
        # WAIT_ABANDONED = 上一个持有者进程死了，系统把锁给了我们。
        # 这正是用内核互斥体而不是锁文件的理由：agent 被杀不会留下死锁。
        _write_owner(tool)
        return True
    raise Busy(
        "DEFERRED - another agent is driving this desktop right now: %s. Nothing was done. "
        "Two agents sharing one mouse will overwrite each other's clicks and steal each "
        "other's foreground, so this call waited %.1fs and then gave up rather than "
        "interleaving. %s"
        % (_describe_owner(), min(budget, LOCK_WAIT_CAP), _what_to_do(held_for)))


def release(got):
    if not got:
        return
    _clear_owner()
    h = getattr(_tls, "mutex", None)
    if h:
        try:
            kernel32.ReleaseMutex(h)
        except Exception:
            pass


class guard(object):
    """with 包起来：拿不到就抛 Busy，拿到了保证释放。

    Windows 的互斥体对**同一个线程**是可重入的（拿几次就得放几次），
    所以 Sequence 这种"整段持有、里面还会调别的动作"的用法是安全的。
    """

    def __init__(self, tool, budget=None):
        self.tool = tool
        if budget is None:
            try:
                from windows_mcp.tools.frame_wrap import SAFE_BUDGET
                budget = SAFE_BUDGET
            except Exception:
                budget = 10.0
        self.budget = budget
        self.got = False

    def __enter__(self):
        self.got = acquire(self.tool, self.budget)
        return self

    def __exit__(self, *a):
        release(self.got)
        return False
