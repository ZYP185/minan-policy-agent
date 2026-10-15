"""Agent 主循环：流式对话 + 工具调用 + 多轮状态。"""
from __future__ import annotations

import json
import re
import threading
from typing import Iterator

import config
from core.llm import LLM, LLMError, text_of, tool_calls_of
from core.retriever import Retriever
from core.store import PolicyStore
from core import tools as T

# ================================================================ 提示词
RESIDENT_PROMPT = """你是「民安」，一名社区民生政策服务智能体，服务对象是普通居民——尤其是老年人、低收入家庭、残疾人、外来务工者等对政策不熟悉的群体。

# 你的工作方式（这决定了你和普通问答机器人的区别）
你不是「你问我答」的搜索引擎，而是主动服务的政策顾问。正确流程：

0. **一听地区就先查覆盖**：居民只要提到自己所在的城市/区（「我是广州的」
   「我们这边」），**立刻调用 check_coverage**。它告诉你库里有没有该地区的地方政策。
   如果 covered=false，你就绝不能拿别处的标准作答——这是最危险的失败模式，
   居民照着办会白跑一趟。如实说"没有收录你所在地区的政策"，
   并建议拨打 12345 或咨询当地居委会。
   居民没说地区时，先问一句「您是在哪个城市/区？」——各城市标准不同。
1. **先检索，再开口**：居民提问后立刻用 search_policy 检索（把口语换成政策书面语关键词）。
   看清库里到底有哪些政策之后，你才知道哪些信息是真正必要的。
2. **边给结论边问，不要先审问再回答**（最重要的一条）：
   - 从居民话里识别到的任何个人信息，立即用 update_profile 记录。
   - **信息不齐也可以、而且应该调用 assess_eligibility**——它本来就支持信息不全，
     会逐项返回「符合 / 可能符合 / 需补充信息 / 不符合」。不要等"问齐了"再体检，
     那会让居民干等好几轮。
   - 然后**先给一个带条件的结论，再用一句话问清最关键的那个未知项**。例如：
     「您家老人每月能领 180 元，前提是有本市户籍——老人是本市户籍吗？」
   - **绝不允许自己读几条条款就下"能领/不能领"的结论**。判定必须来自
     assess_eligibility 的返回值。检索到的原文只用来解释「为什么」和「怎么办」。
   - 追问绝不超过一轮。问过一轮还在问第二轮，就是失职。
   - 如果居民所在城市不明，不要默认某个城市——要么问一句，要么把你的结论
     明确限定成"如果老人在 XX 市，可以领……"。
3. **判定交给规则引擎**：符合不符合必须来自 assess_eligibility 的结果，不要自己凭常识判断。
4. **引用前必读原文**：任何向居民陈述的政策内容（金额、条件、材料、流程），必须来自 read_clause 读到的原文。search_policy 返回的只是预览片段。
5. **给可执行的指引**：明确告诉居民「去哪办、带什么、多久办完」。居民说「我带了身份证和户口本」时，用 check_materials 核对还缺什么。
6. **主动一点**：体检出符合的政策后，可以问居民「要我把申请表填好吗」，然后用 generate_form 生成草稿。

# 铁律（违反即为严重错误）
- **绝不编造**。不得凭常识补充政策内容。search_policy 检索不到时，必须明确告诉居民「我没有找到相关政策，建议拨打 12345 咨询」，而不是编一个听起来合理的答案。宁可说不知道。
- **金额、日期、材料清单、办理时限必须逐字来自原文**。不得四舍五入、不得换算、不得推测、不得用你记忆里的政策替代检索结果。
- **已废止政策不得作为依据**。检索结果中 status 为「已废止」的，绝不能告诉居民去申请。
- **区分事实与建议**。政策原文是事实；你关于「可以先去办A再办B」的判断要标明是建议。
- **不做法律意见**。涉及争议、诉讼、历史遗留问题，引导居民咨询专业人士或 12345。

# 语言风格（你的读者是一位可能 70 岁的普通居民）
- **第一句话就给答案**。他问「我妈能领什么钱」，第一句就得是
  「您母亲每月能领 180 元」——而不是「根据某某办法的规定」。
  先给结论，再解释为什么，最后说怎么做。
  如果还有条件没确认，就给**带条件的结论**并把那个条件问出来
  （「能领 180 元，前提是有本市户籍——老人是本市户籍吗？」），
  而不是把答案扣着不给。但要注意：这个结论必须来自 assess_eligibility，
  不能自己读完条款就下判断。
- **说人话**。政策写「处于法定劳动年龄内、有劳动能力且处于无业状态的人员」，
  你要说「您如果没工作、又有劳动能力」。
- **一次最多讲 3 件事**。信息给多了记不住。次要内容一句话带过，或先不提，等居民问。
- **长句拆短句**。一句话超过 40 个字就拆开。
- 金额说「每月 180 元」，时限说「20 个工作日」，不要含糊。
- 不用「根据我的知识」「作为一个AI」这类话。
- **全程只说中文**。任何过渡性的话都必须是中文，绝对不要出现英文句子，
  哪怕是你自己"想一下"的话也不行——居民会原样看到它。

# 输出格式
- **调用工具前不要写任何说明文字**。不要写「我这就去查」「让我核对一下」这类预告，
  更不要写英文的思考。界面上会**自动显示**你的每一步动作（居民能看到
  「检索政策原文」「政策资格体检」这样的步骤提示），不需要你用文字预告。
  工具调完之后，直接给结论就行。
- **主文里绝对不要出现【依据：…】这类条款引用**。居民不关心条款号，
  每段后面挂一条引用会把阅读节奏打碎，让他看不下去。
  溯源由界面右侧的「引用来源」面板自动承载——你只要正常用 read_clause 读原文，
  系统会自动把出处送过去，你不需要在正文里标。
- **不要用「### 政策名称」这种小标题去堆结构**。用自然的段落 + 短列表。
  政策名可以在句子里直接说出来，例如「这项叫老年综合津贴」。
- 讲一项政策，讲清三件事就够：**能领多少** → **要做什么** → **去哪问**。
  办理材料、时限这些，等居民问或者确实关键时再说。
- 强调用**加粗**，但一段里别超过两处。
- 结尾给一句能照着做的动作，例如
  「您去居委会问一句『我家老人 82 岁，津贴在名单里吗』就行」。
- 不要输出 JSON，不要暴露工具名和内部字段名（别写 read_clause、search_policy 这类词）。
"""

WORKER_PROMPT = """你是「民安·社区工作端」，服务对象是社区网格员、居委会工作人员。

你的职责是帮基层工作者**从海量政策里找出辖区居民应享未享的政策**，而不是回答居民咨询。

工作方式：
- 用 scan_unapplied 按辖区和类别筛查政策覆盖情况。**先把辖区问清楚再扫描**——
  不限定地域的扫描会把多个省市的政策混在一起。
- 需要说明某项政策的具体条件时，用 search_policy 和 read_clause 取原文，逐字引用，不要概括。
  注意工具参数的区别：search_policy 返回 clause_id（形如 p1a2b3c4#3），
  传政策 id（形如 p1a2b3c4）给 read_clause 会返回该政策全部条款。
- 输出要方便比对台账：政策名 / 适用地区 / 适用对象 / 待遇标准 / 办理地点。
- 主动提示容易漏掉的政策（残疾人两项补贴、临时救助、高龄津贴这类需要主动申请的）。

# 铁律
- **绝不能把不同地区的标准混在一起**。同一种补贴在上海和在芜湖的标准完全不同。
  表格里必须有「适用地区」列；如果结果跨了多个地区，按地区分节呈现，
  并明确说明「以下标准仅适用于 XX 市」。
- 不得编造政策内容，所有事实必须来自工具返回的原文。
- **不要把内部工具名写进回复**（不要出现 get_process、scan_unapplied、
  search_policy 这类词）。要用"我帮您调取办理流程""我再检索一次"这样的说法。
- **全程只说中文**。调用工具前不要写预告文字（界面上会自动显示你在做什么），
  也不要出现任何英文句子。

输出用 Markdown 表格便于比对，依据用 `【依据：《政策名》第X条】` 标注。
"""


class _PreambleFilter:
    """拦掉模型在调用工具前用英文自言自语的前缀。

    模型有时会先吐一句 "I'll search for relevant policies for you." 再调工具，
    居民会原样看到这串英文。提示词能大幅减少这种现象，但管不住随机性，
    而这是使用者直接看得见的，所以必须在服务端兜一道。

    策略保守：只处理「开头的一段完整英文句子」，一旦见到中文就不再干预，
    避免误伤正文。宁可漏拦，不可误删。
    """
    _CJK = re.compile(r"[一-鿿]")
    # 完整的英文句子：以大写字母起头，以 . ! ? 收尾
    _EN_LEAD = re.compile(r"^[A-Za-z][A-Za-z0-9 ,:'\"()\-–—/]{12,}?[.!?]\s*")

    def __init__(self) -> None:
        self._buf = ""
        self._open = True          # 还在试探开头阶段

    def feed(self, chunk: str) -> str:
        if not self._open:
            return chunk
        self._buf += chunk
        if not self._CJK.search(self._buf):
            return ""                       # 还没见到中文，先扣着不发
        self._open = False
        out = self._EN_LEAD.sub("", self._buf, count=1)
        self._buf = ""
        return out

    def flush(self) -> str:
        """一段文本结束时调用。整段都是英文的一律丢弃（那是自言自语，不是回答）。"""
        if not self._open:
            return ""
        self._open = False
        held, self._buf = self._buf, ""
        if not held:
            return ""
        if self._CJK.search(held):
            return self._EN_LEAD.sub("", held, count=1)
        return ""                           # 纯英文 → 丢掉


class Session:
    """一次会话：消息历史 + 工具上下文。"""

    def __init__(self, store: PolicyStore, retriever: Retriever, mode: str = "resident"):
        self.llm = LLM()
        self.store = store
        self.retriever = retriever
        self.mode = mode
        self.ctx = T.ToolContext(store, retriever, llm=self.llm)
        self.messages: list[dict] = []
        self._cited_sent = 0      # 已推送给前端的引用条数
        # 同一会话同时只能跑一轮。放在 Session 上而不是调用方，
        # 是为了让"不并发"成为 run() 的固有性质，任何调用方都绕不过去。
        self._busy = threading.Lock()

    @property
    def system(self) -> str:
        return WORKER_PROMPT if self.mode == "worker" else RESIDENT_PROMPT

    def reset(self) -> None:
        self.messages.clear()
        self.ctx = T.ToolContext(self.store, self.retriever, llm=self.llm)
        self._cited_sent = 0


def run(session: Session, user_text: str) -> Iterator[dict]:
    """跑一轮对话，产出 SSE 事件。

    入口先抢会话锁，拿不到就直说上一条还在处理。**不能阻塞等待**：
    单次 LLM 超时 180 秒、最多 12 步，最坏一轮能跑几十分钟，
    用户连点两下发送就会一直卡在「处理中」。

    finally 里的收尾修复同样重要——客户端中断时生成器被 close，
    这里必须把残缺的 tool_use 补齐，否则该会话永久损坏（见 _repair_tail）。
    """
    if not session._busy.acquire(blocking=False):
        yield {"type": "error", "message": "上一条消息还在处理中，请稍候再发。"}
        return
    try:
        yield from _run_inner(session, user_text)
    finally:
        try:
            _repair_tail(session)
        finally:
            session._busy.release()


def _run_inner(session: Session, user_text: str) -> Iterator[dict]:
    """一轮对话的实际流程。调用方负责持锁与收尾修复。"""
    session.messages.append({"role": "user", "content": user_text})

    for step in range(config.MAX_AGENT_STEPS):
        # 每步只允许 worker 模式用 scan_unapplied，居民端不给这个工具
        schemas = [
            s for s in T.SCHEMAS
            if session.mode == "worker" or s["name"] != "scan_unapplied"
        ]

        assistant_msg = None
        pending_tools: list[dict] = []
        pf = _PreambleFilter()
        try:
            for ev in session.llm.stream(
                messages=session.messages,
                system=session.system,
                tools=schemas,
            ):
                if ev["type"] == "text":
                    clean = pf.feed(ev["text"])
                    if clean:
                        yield {"type": "text", "text": clean}
                elif ev["type"] == "tool_start":
                    # 工具调用开始 ⇒ 前面那段文本已经结束，把扣着的放出来
                    rest = pf.flush()
                    if rest:
                        yield {"type": "text", "text": rest}
                    yield {"type": "tool_start", "name": ev["name"]}
                elif ev["type"] == "_final":
                    rest = pf.flush()
                    if rest:
                        yield {"type": "text", "text": rest}
                    assistant_msg = ev["message"]
        except LLMError as e:
            yield {"type": "error", "message": str(e)}
            return

        if not assistant_msg:
            yield {"type": "error", "message": "模型未返回有效响应"}
            return

        calls = tool_calls_of(assistant_msg)
        pending_tools = calls

        # 必须把 assistant 消息原样放回历史，工具调用链才连得上
        session.messages.append({
            "role": "assistant",
            "content": assistant_msg.get("content", []),
        })

        if not calls:
            yield {"type": "done", "steps": step + 1,
                   "usage": assistant_msg.get("usage", {})}
            return

        results = []
        for call in calls:
            name = call.get("name", "")
            args = call.get("input") or {}
            yield {"type": "tool_call", "name": name, "args": _safe(args)}

            try:
                out = T.call(name, session.ctx, args)
                ok = True
            except T.ToolError as e:
                out = {"error": str(e)}
                ok = False
            except Exception as e:  # 工具内部 bug 不应该打断整轮对话
                out = {"error": f"工具执行异常：{type(e).__name__}: {e}"}
                ok = False

            yield {"type": "tool_result", "name": name, "ok": ok, "summary": _summarize(name, out)}

            # 工具产生的可视化卡片
            while session.ctx.artifacts:
                yield {"type": "artifact", "artifact": session.ctx.artifacts.pop(0)}

            if name == "update_profile" and ok:
                yield {"type": "profile", "profile": session.ctx.profile}

            # 引用过的政策条款 → 前端右侧的「引用来源」面板。
            # 只推新增的，避免每轮重复传全量。
            if len(session.ctx.cited) > session._cited_sent:
                fresh = session.ctx.cited[session._cited_sent:]
                session._cited_sent = len(session.ctx.cited)
                yield {"type": "citations", "citations": fresh}

            results.append({
                "type": "tool_result",
                "tool_use_id": call.get("id", ""),
                "content": json.dumps(out, ensure_ascii=False, default=str)[:14000],
            })

        session.messages.append({"role": "user", "content": results})

    yield {"type": "error", "message": f"已达最大工具调用轮数（{config.MAX_AGENT_STEPS}），已中止。"}


def _repair_tail(session: Session) -> None:
    """补齐被中断的那一轮，让消息历史重新合法。

    历史是「先把带 tool_use 的 assistant 消息写进去，跑完工具再补
    tool_result」。中间这扇窗里客户端一断（刷新、切网、代理掐断），
    就永远停在「有 tool_use、没有 tool_result」——而 Anthropic 协议
    要求两者配对，于是该会话之后每次请求都判 400，且**无法自愈**。

    正常收尾（正常回答完 / 步数耗尽 / LLM 报错）时末条不带 tool_use，
    这里就是空操作，所以无条件调用是安全的。
    """
    msgs = session.messages
    if not msgs:
        return
    last = msgs[-1]
    if last.get("role") != "assistant":
        return

    ids = [
        b.get("id")
        for b in (last.get("content") or [])
        if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id")
    ]
    if not ids:
        return

    msgs.append({
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": i,
                "content": "（本轮被中断，工具未执行）",
                "is_error": True,
            }
            for i in ids
        ],
    })


# ---------------------------------------------------------------- 辅助
def _safe(args: dict) -> dict:
    """工具参数可能很长，截断后发给前端展示。

    嵌套的 dict/list 要序列化成 JSON 而不是 Python repr——
    否则前端会看到 {'age': 82} 这种带单引号的展示。
    """
    out = {}
    for k, v in (args or {}).items():
        if v is None or isinstance(v, (int, float, bool)):
            out[k] = v
            continue
        s = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
        out[k] = s if len(s) <= 120 else s[:120] + "…"
    return out


def _summarize(name: str, out: dict) -> str:
    if "error" in out:
        return "错误：" + str(out["error"])[:120]
    if name == "check_coverage":
        if out.get("covered"):
            return (f"{out.get('region')}：本地政策 {out.get('local_policy_count', 0)} 份，"
                    f"全国性 {out.get('national_policy_count', 0)} 份")
        return f"⚠ 库里没有 {out.get('region')} 的地方政策"
    if name == "search_policy":
        return f"命中 {out.get('found', 0)} 条政策条款"
    if name == "read_clause":
        return f"读取《{out.get('policy', {}).get('title', '')}》{out.get('clause_no', '')}"
    if name == "assess_eligibility":
        s = out.get("summary", {})
        return (f"体检 {len(out.get('results', []))} 项政策："
                f"符合 {s.get('符合', 0)}，可能符合 {s.get('可能符合', 0)}，"
                f"需补充 {s.get('需补充信息', 0)}，不符合 {s.get('不符合', 0)}")
    if name == "check_materials":
        return (f"已备 {len(out.get('already_have', []))} 项，"
                f"缺 {len(out.get('missing', []))} 项")
    if name == "generate_form":
        return f"生成申请表，已预填 {out.get('filled_count', 0)}/{out.get('total_count', 0)} 项"
    if name == "get_process":
        return f"办理流程 {len(out.get('process', []))} 步" + (
            f"，时限 {out.get('duration')}" if out.get("duration") else ""
        )
    if name == "scan_unapplied":
        return f"扫描到 {out.get('found', 0)} 项政策"
    if name == "update_profile":
        return f"已记录画像，当前 {len(out.get('profile', {}))} 项，缺 {len(out.get('missing_fields', []))} 项"
    return "完成"
