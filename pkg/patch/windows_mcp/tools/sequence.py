"""PATCH 0.5.17：一次调用跑一串动作，省的是模型推理而不是鼠标时间。

维护者 9-15：「对于后面代替人操作电脑应用速度太慢也不行，尤其是有真实业务的场景」。

先量清楚时间花在哪，再决定改什么。实测每个动作约 6-7 秒，构成是：

| 环节 | 耗时 |
|---|---|
| 按窗口截图 | 0.05 秒 |
| OCR / ReadColor | 0.05–0.8 秒 |
| 拖一张牌 | 0.85 秒（已可压到 0.21） |
| **模型看图 + 决策** | **4–5 秒** |

**工具本身只占一两秒，大头是模型每次往返的推理。** 所以把鼠标调快只能省 0.6 秒，
动不了根本；**减少往返次数才动得了**。
「点这里 → 打字 → 回车」三次调用就是三轮推理，合成一次就省两轮。

这也是 9-14 拆 Codex 时量到的那个数的由来：它的观察/动作比只有 1.45，
**不是每动一下就重看一遍**。

**关于「批量」最要紧的一条诚实**：这不是事务，没有回滚。
第 3 步失败时，第 1、2 步**已经发生了**。所以返回里必须逐步说清哪步做了、
哪步没做、界面停在什么状态——否则批量就是把一次看得见的失败，
变成一团看不见的烂摊子。宁可话说得啰嗦，也不能让调用方以为「失败了 = 什么都没发生」。

刻意的取舍：
- **一步失败就停**，绝不硬着头皮往下做。前面的步骤改变了界面，
  后面的坐标多半已经不作数了。
- **每步之间都认 Esc**，和单步工具一样——人要停的时候，批量不能变成一列刹不住的车。
- **支持 expect 检查点**，让一串动作能自己核实结果，而不是回去再问模型一轮。
"""

import time
from typing import Optional

from mcp.types import ToolAnnotations
from fastmcp import Context
from windows_mcp.infrastructure import with_analytics

MAX_STEPS = 20
MAX_WAIT = 20


def register(mcp, *, get_desktop, get_analytics):
    @mcp.tool(
        name="Sequence",
        description=(
            "Runs several desktop actions in ONE call. Each separate tool call costs the model a "
            "whole reasoning turn - measured on this machine, roughly 4-5 seconds of thinking "
            "against 1-2 seconds of actual mouse and screen work - so collapsing 'click, type, "
            "press enter' from three calls into one saves two turns. "
            "steps is a list; each step is {\"do\": ...} plus its arguments: "
            "{\"do\":\"click\",\"loc\":[x,y]} (add \"button\":\"right\", \"clicks\":2, or "
            "\"expected_process\":\"Weixin\" to refuse if that pixel belongs to something else); "
            "{\"do\":\"click_text\",\"text\":\"发表\",\"hwnd\":N} locates by OCR at that moment; "
            "{\"do\":\"type\",\"text\":\"...\"} types into whatever has focus, no click first; "
            "{\"do\":\"key\",\"keys\":\"ctrl+a\"}; {\"do\":\"wait\",\"seconds\":1.5}; "
            "{\"do\":\"drag\",\"from\":[x,y],\"to\":[x,y]}; "
            "{\"do\":\"front\",\"hwnd\":N} brings that window to the front INSIDE the "
            "sequence - put it first when later steps use expected_process, so the raise and the "
            "clicks happen in one call with no gap for another window to take the foreground "
            "between them (note it does steal focus from whoever is typing, like any raise); "
            "{\"do\":\"expect_text\",\"text\":\"已发送\",\"hwnd\":N,\"timeout\":5} which fails "
            "the sequence if that text does not appear - use it to make a run check itself "
            "instead of asking you to look. "
            "IT IS NOT A TRANSACTION AND THERE IS NO ROLLBACK: if step 3 fails, steps 1 and 2 "
            "have already happened. It stops at the first failure and tells you exactly which "
            "steps ran, which did not, and what the screen was left like - read that before "
            "retrying, because the earlier steps changed the screen and your later coordinates "
            "are probably stale. Esc from the human stops it between steps like any other tool. "
            "Use it for steps you are already confident about; when you need to look before "
            "deciding the next move, use the single tools."
        ),
        annotations=ToolAnnotations(
            title="Sequence",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    @with_analytics(get_analytics(), "Sequence-Tool")
    def sequence_tool(
        steps: list,
        hwnd: Optional[int] = None,
        ctx: Context = None,
    ) -> str:
        from windows_mcp.tools.overlay import activity, take_cancel
        from windows_mcp.tools import type_here as th

        desktop = get_desktop()
        if not isinstance(steps, list) or not steps:
            return "Sequence: steps must be a non-empty list of {\"do\": ...} objects."
        if len(steps) > MAX_STEPS:
            return ("Sequence: %d steps is too many (limit %d). A long blind run is how a small "
                    "mistake turns into a big one - split it and look in between."
                    % (len(steps), MAX_STEPS))

        log = []
        t0 = time.time()

        def done(i, ok, why=""):
            """把「做到哪一步、后面还剩什么」说清楚。批量最容易骗人的就是这一点。"""
            ran = "\n".join("  %d. %s" % (n + 1, m) for n, m in enumerate(log))
            left = len(steps) - (i + 1)
            # 「全部完成」是句会骗人的话。
            #
            # Codex 9-15 在剪映上撞到：返回 "Sequence finished all 3 steps ... dragged
            # (gesture sent; the app may have refused the drop)"，而字幕**根本没被拖动**。
            # 它的原话：「属于动作发送成功而目标未达成，工具虽有警告，
            # 标题式的 finished 容易误读。」它排 P0 的建议就是把
            # **「动作发出」和「效果达成」拆成两件事说**。
            #
            # 我们能验的只有 expect_text 那种步（它真去看了屏幕）；
            # click/drag/type 只能证明事件发出去了。所以标题里如实分两类报，
            # 而不是用一个 finished 把两者混成一句。
            verified = sum(1 for m in log if "VERIFIED" in m)
            sent_only = len(log) - verified
            # 说"验过几步"仍然把因果说大了。
            #
            # opencode 9-15 第七轮的 P3：`expect_text("分数")` 只能证明那段文字出现了，
            # **不代表它前面那张牌落对了**。它的原话：
            # 「『验过几步』最好继续避免暗示因果关系……写成『1 个显式断言通过』更精确。」
            # 它说得对：我们能证明的只有"这个断言成立"，
            # 而"这个断言成立"和"前面那些动作达成了目的"之间没有必然联系。
            if verified and sent_only:
                headline = ("Sequence sent all %d steps in %.1fs. %d explicit check(s) passed; "
                            "the other %d step(s) were only sent. A check proves its own "
                            "condition held - it does not prove the steps before it achieved "
                            "what you wanted."
                            % (len(steps), time.time() - t0, verified, sent_only))
            elif verified:
                headline = ("Sequence sent all %d steps in %.1fs, and %d explicit check(s) "
                            "passed. A check proves its own condition held - it does not prove "
                            "the steps before it achieved what you wanted."
                            % (len(steps), time.time() - t0, verified))
            else:
                headline = ("Sequence SENT all %d steps in %.1fs. None of them was verified - "
                            "these tools can prove the input was delivered, not that the app did "
                            "anything with it. Add an expect_text step, or look afterwards, when "
                            "it matters whether it actually landed."
                            % (len(steps), time.time() - t0))
            head = (headline
                    if ok else
                    "Sequence STOPPED at step %d of %d after %.1fs: %s"
                    % (i + 1, len(steps), time.time() - t0, why))
            tail = ""
            if not ok:
                tail = ("\nSteps 1-%d ALREADY RAN and their effects are still on screen - this is "
                        "not a transaction and nothing was undone. %d step(s) never ran. Look at "
                        "the screen before retrying; coordinates from before this call are "
                        "probably stale now." % (i, left) if i > 0 else
                        "\nNothing ran before this, so the screen is as you left it. "
                        "%d step(s) never ran." % left)
            return "%s\n%s%s" % (head, ran, tail)

        for i, st in enumerate(steps):
            if not isinstance(st, dict):
                return done(i, False, "step is not an object: %r" % (st,))
            kind = str(st.get("do") or "").strip().lower()

            ago = take_cancel()
            if ago is not None:
                log.append("(not run - somebody pressed Esc)")
                return done(i, False,
                            "somebody pressed Esc %.0f seconds ago while this machine was being "
                            "operated, so the rest of the sequence was abandoned" % ago)

            try:
                if kind == "wait":
                    # 千问办公第一次调用就吃了校验错：独立的 Wait 工具参数叫
                    # duration，而这里的等待步骤叫 seconds。同一件事两个名字，
                    # 是纯粹的自找麻烦——两个都认，谁也不用背。
                    secs = max(0.0, min(float(st.get("seconds")
                                              or st.get("duration") or 1), MAX_WAIT))
                    time.sleep(secs)
                    log.append("waited %.1fs (a wait proves nothing about the steps around it)"
                               % secs)
                    continue

                if kind == "expect_text":
                    from windows_mcp.tools.ocr_find import resolve_text
                    want = str(st.get("text") or "")
                    budget = max(0.5, min(float(st.get("timeout") or 5), MAX_WAIT))
                    tgt = st.get("hwnd") or hwnd
                    deadline = time.time() + budget
                    seen = None
                    while time.time() < deadline:
                        xy, why, _t = resolve_text(want, hwnd=tgt)
                        if xy:
                            seen = xy
                            break
                        time.sleep(0.4)
                    if not seen:
                        log.append("checked for %r - NOT THERE" % want)
                        return done(i, False,
                                    "the checkpoint failed: %r never appeared within %.0fs. The "
                                    "steps before it did run, so whatever they did, it did not "
                                    "produce what you expected." % (want, budget))
                    # 只有这一步是真去看了屏幕的，所以只有它配叫 VERIFIED。
                    # 标题里按这个词统计"验过几步、几步只是发出去了"。
                    log.append("VERIFIED on screen: %r found at %s" % (want, seen))
                    continue

                # 以下都是真动手的步骤：亮提示层，让人看得见
                activity(int(st.get("hwnd") or hwnd or 0),
                         "uia-desktop 正在操控这台电脑 · Sequence %d/%d" % (i + 1, len(steps)))

                if kind in ("click", "click_text"):
                    button = str(st.get("button") or "left")
                    clicks = int(st.get("clicks") or 1)
                    if kind == "click_text":
                        from windows_mcp.tools.ocr_find import resolve_text
                        want = str(st.get("text") or "")
                        xy, why, _t = resolve_text(
                            want, hwnd=st.get("hwnd") or hwnd,
                            process=st.get("process"), region=st.get("region"))
                        if not xy:
                            log.append("could not find %r" % want)
                            return done(i, False, "could not locate %r: %s" % (want, why))
                        loc = list(xy)
                    else:
                        loc = list(st.get("loc") or [])
                        if len(loc) != 2:
                            return done(i, False, "click needs loc=[x, y]")
                    want_p = st.get("expected_process")
                    if want_p:
                        from windows_mcp.tools.frame_wrap import _check_owner
                        _check_owner({"loc": loc, "expected_process": want_p}, "Sequence")
                    desktop.click(loc=loc, button=button, clicks=clicks)
                    log.append("%s %s at %s" % (button, "click" if clicks == 1 else
                                                "x%d click" % clicks, tuple(loc)))
                    continue

                if kind in ("type", "typehere"):
                    text = str(st.get("text") or "")
                    mode = th._type_no_click(text)
                    log.append("typed %d chars into the focused control via %s"
                               % (len(text), mode))
                    continue

                if kind == "front":
                    # opencode 9-15 报的本轮最大摩擦，原话：
                    # 「现在必须先单独调用 Window(front)，但下一次调用或 Sequence
                    #   等待期间仍可能被其他窗口抢占。expected_process 能防误点，
                    #   却无法让流程继续。」
                    # 它说得对：拒绝是对的，但只拒绝不给出路，等于把问题原样退回去。
                    # 把"提到前台"做成序列里的一步，抢占和补救就在同一次调用里，
                    # 中间没有留给别人插队的往返。
                    from windows_mcp.tools.window_ctl import _force_front, _describe
                    tgt = int(st.get("hwnd") or hwnd or 0)
                    if not tgt:
                        return done(i, False,
                                    "step 'front' needs a window: pass hwnd on the step or on "
                                    "the whole Sequence")
                    okf = _force_front(tgt)
                    time.sleep(float(st.get("settle") or 0.25))
                    d = _describe(tgt)
                    log.append("brought hwnd=%d '%s' to the front%s"
                               % (tgt, d["title"][:30],
                                  "" if okf else " (SetForegroundWindow refused; it was raised "
                                                 "but may not have focus)"))
                    continue

                if kind == "key":
                    keys = str(st.get("keys") or st.get("shortcut") or "")
                    desktop.shortcut(keys)
                    log.append("pressed %s" % keys)
                    continue

                if kind == "drag":
                    a = list(st.get("from") or [])
                    b = list(st.get("to") or [])
                    if len(a) != 2 or len(b) != 2:
                        return done(i, False, "drag needs from=[x,y] and to=[x,y]")
                    desktop.drag(b, from_loc=a, duration=st.get("duration"))
                    log.append("dragged %s -> %s (gesture sent; the app may have refused the drop)"
                               % (tuple(a), tuple(b)))
                    continue

                return done(i, False, "unknown step %r" % kind)

            except Exception as e:
                log.append("FAILED: %s" % str(e)[:150])
                return done(i, False, "%s: %s" % (type(e).__name__, str(e)[:200]))

        return done(len(steps) - 1, True)
