"""FABOT 자동매매 승인 에이전트 — Human-in-the-Loop 실습 과제.

FABOT의 최종 목표는 사람 개입 없는 완전 자동화다(CLAUDE.md 확정 방향). 이 프로젝트는
그 목표와 별개로, 이번 과제가 요구하는 "되돌릴 수 없는 결정 앞에서 멈추고 사람에게
확인받는" 구조 자체를 만드는 게 목적이라 이번 한 번은 의도적으로 사람 승인 단계를
넣는다 — 실제 FABOT 운영 방향을 바꾸는 것이 아니다.

흐름: classify(위험 판정, 코드) -> ai_judge(LLM이 판정 근거 작성)
     -> [필요하면] review(interrupt로 멈춤, 사람 답변 반영) -> execute/reject
"""
import asyncio
import json
import operator
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Optional, TypedDict

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, CLINotFoundError, TextBlock, query
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

BASE = Path(__file__).parent
LLM_MODEL = "claude-haiku-4-5-20251001"
MAX_RETRY = 2

# ── 승인 기준 (계산 가능한 형태로, 서로 다른 위험 하나씩) ──────────────────────
# 1) 매수 비중 100%(F&G≤15)  — 실탄 전액을 한 번에 태우는 가장 큰 단일 결정(금액 위험)
# 2) 매도 전량(F&G≥80)       — 보유 물량을 통째로 정리하는 되돌리기 어려운 결정(비가역 위험)
# 3) 쿨다운 예외 해제 매수    — 평소와 다른 특수 규칙이 적용되는 경우(로직 오류 위험)
# 이 세 가지에 안 걸리는 매수 25%/50%, 매도 50%, 평시 커버드콜 10% 정기매수는 블록부트
# 스트랩 시뮬레이션으로 이미 검증된 반복 규칙이라(CLAUDE.md 2026-08-22 확정) 자동 통과한다.
def needs_approval(sig: dict) -> tuple[bool, str]:
    if sig["asset"] == "TQQQ" and sig["action"] == "buy" and sig["tier_pct"] >= 1.0:
        return True, "매수 비중 100% — 실탄 전액을 한 번에 투입하는 최대 단일 결정"
    if sig["asset"] == "TQQQ" and sig["action"] == "sell" and sig["tier_pct"] >= 1.0:
        return True, "매도 전량 — 보유 물량 전체를 정리하는 되돌리기 어려운 결정"
    if sig.get("cooldown_override"):
        return True, "쿨다운 예외 해제로 실행되는 매수 — 평소와 다른 특수 규칙 적용 케이스"
    return False, ""


def compute_amount(sig: dict) -> int:
    if sig["action"] == "buy":
        return round(sig["available_capital_krw"] * sig["tier_pct"])
    return round(sig["held_value_krw"] * sig["tier_pct"])


class State(TypedDict):
    signal: dict
    amount_krw: int
    needs_approval: bool
    stop_reason: str
    ai_reasoning: str
    human_decision: Optional[str]
    human_feedback: Optional[str]
    final_amount_krw: Optional[int]
    executed: bool
    rejected: bool
    retry_count: int
    log: Annotated[list, operator.add]


# ── claude_agent_sdk 호출 (deep-researcher-tax/graph.py와 동일 견고성 패턴) ──
async def _call_llm(prompt: str, system_prompt: str) -> str:
    options = ClaudeAgentOptions(
        model=LLM_MODEL, allowed_tools=[], max_turns=1,
        setting_sources=[], system_prompt=system_prompt,
    )
    last_error = None
    for attempt in range(3):
        try:
            messages = [msg async for msg in query(prompt=prompt, options=options)]
            parts = []
            for msg in messages:
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            parts.append(block.text)
            return "".join(parts).strip()
        except CLINotFoundError:
            import subprocess
            subprocess.run(["npm", "install", "-g", "@anthropic-ai/claude-code"],
                            check=True, capture_output=True, text=True, timeout=120)
        except Exception as e:
            last_error = e
            await asyncio.sleep(2.0 * (attempt + 1))
    return f"[LLM 호출 실패 — 재시도 후에도 실패: {last_error}]"


JUDGE_SYSTEM_PROMPT = (
    "당신은 FABOT 자동매매의 판정 보조입니다. 아래 신호 정보를 보고, 이 매매가 FABOT의 "
    "확정된 규칙(F&G 지수 기반 매수·매도 임계값)에 맞는 결정인지, 왜 타당한지를 한국어로 "
    "3~4문장으로 설명하세요. F&G 점수·금액·비중 숫자를 반드시 인용하세요. 담당자 피드백이 "
    "있으면 그걸 반영해서 판정을 다시 쓰세요. 주어진 정보만 쓰고 추측하지 마세요."
)


def _judge_prompt(state: State) -> str:
    sig = state["signal"]
    p = (
        f"신호 날짜: {sig['date']}, F&G 점수: {sig['fg_score']}, 자산: {sig['asset']}, "
        f"동작: {sig['action']}, 비중: {sig['tier_pct']*100:.0f}%, 금액: {state['amount_krw']:,}원, "
        f"쿨다운 예외 해제 여부: {sig.get('cooldown_override', False)}"
    )
    if sig.get("note"):
        p += f"\n참고: {sig['note']}"
    if state.get("human_feedback"):
        p += f"\n\n담당자가 '다시 판정'을 요청하며 남긴 피드백: {state['human_feedback']}"
    return p


def classify(state: State) -> dict:
    sig = state["signal"]
    amount = compute_amount(sig)
    need, reason = needs_approval(sig)
    return {
        "amount_krw": amount,
        "needs_approval": need,
        "stop_reason": reason,
        "retry_count": 0,
        "executed": False,
        "rejected": False,
        "log": [f"분류: {'승인 필요' if need else '자동 통과'}" + (f" ({reason})" if reason else "")],
    }


def _rule_based_reasoning(state: State) -> str:
    """LLM 호출이 재시도 후에도 실패할 때의 대체 경로. 실측으로 겪은 문제: 특정 신호
    조합(예: 매수 100%/매도 전량 케이스)에서 claude_agent_sdk가 "Reached maximum
    number of turns"로 반복 실패했는데, 프롬프트 문구를 바꿔봐도 재현되어 일시적
    오류가 아니라고 판단했다. FABOT이 실제 시스템 알림에서 이미 쓰는 원칙
    ("정확해야 하는 수치는 LLM 없이 그대로 조립") 그대로, LLM이 끝내 실패하면
    실제 신호 값만으로 판정 문구를 조립해서 화면에 빈 자리가 남지 않게 한다."""
    sig = state["signal"]
    action_ko = "매수" if sig["action"] == "buy" else "매도"
    parts = [
        f"[LLM 판정 실패로 규칙 기반 문구로 대체됨] F&G {sig['fg_score']}점, "
        f"{sig['asset']} {action_ko} {sig['tier_pct']*100:.0f}%, 금액 {state['amount_krw']:,}원.",
        f"멈춘 이유: {state['stop_reason']}." if state["stop_reason"] else "",
    ]
    if state.get("human_feedback"):
        parts.append(f"담당자 피드백: {state['human_feedback']}")
    return " ".join(p for p in parts if p)


def ai_judge(state: State) -> dict:
    reasoning = asyncio.run(_call_llm(_judge_prompt(state), JUDGE_SYSTEM_PROMPT))
    if "[LLM 호출 실패" in reasoning:
        reasoning = _rule_based_reasoning(state)
        return {"ai_reasoning": reasoning, "log": ["AI 판정 실패 → 규칙 기반 문구로 대체"]}
    return {"ai_reasoning": reasoning, "log": ["AI 판정 완료"]}


def route_after_judge(state: State) -> str:
    return "review" if state["needs_approval"] else "execute"


def review(state: State) -> dict:
    """주의: interrupt() 이전에는 부작용(알림 발송 등)을 두지 않는다 — 재개 시 이 함수가
    처음부터 다시 실행되기 때문(섹션 2 강의 내용 그대로)."""
    sig = state["signal"]
    action_ko = "매수" if sig["action"] == "buy" else "매도"
    payload = {
        "요청": f"{sig['date']} F&G {sig['fg_score']}점 — {sig['asset']} {action_ko} {sig['tier_pct']*100:.0f}%",
        "금액": f"{state['amount_krw']:,}원",
        "AI 판정": state["ai_reasoning"],
        "멈춘 이유": state["stop_reason"],
        "통과시키면": f"{state['amount_krw']:,}원 {action_ko} 주문이 (모의)체결됩니다",
    }
    answer = interrupt(payload)
    decision = answer.get("action")
    update = {"human_decision": decision, "human_feedback": answer.get("feedback"),
              "log": [f"담당자 결정: {decision}"]}
    if decision == "approve":
        update["final_amount_krw"] = state["amount_krw"]
    elif decision == "modify":
        update["final_amount_krw"] = answer.get("amount", state["amount_krw"])
    elif decision == "retry":
        update["retry_count"] = state["retry_count"] + 1
    return update


def route_after_review(state: State) -> str:
    d = state["human_decision"]
    if d in ("approve", "modify"):
        return "execute"
    if d == "retry" and state["retry_count"] <= MAX_RETRY:
        return "ai_judge"
    return "reject"  # reject 결정 또는 재판정 한도 초과


def _append_ledger(record: dict):
    path = BASE / "data" / "executions.json"
    items = []
    if path.exists():
        items = json.loads(path.read_text(encoding="utf-8")).get("items", [])
    items.append(record)
    path.write_text(json.dumps({"items": items}, ensure_ascii=False, indent=2), encoding="utf-8")


def execute(state: State) -> dict:
    sig = state["signal"]
    amount = state.get("final_amount_krw") or state["amount_krw"]
    _append_ledger({
        "signal_id": sig["id"], "date": sig["date"], "fg_score": sig["fg_score"],
        "asset": sig["asset"], "action": sig["action"], "tier_pct": sig["tier_pct"],
        "cooldown_override": sig.get("cooldown_override", False),
        "amount_krw": amount, "auto": not state["needs_approval"],
        "human_decision": state.get("human_decision"),
        "executed_at": datetime.now(timezone.utc).isoformat(),
    })
    return {"executed": True, "final_amount_krw": amount,
            "log": [f"체결(모의): {amount:,}원"]}


def reject(state: State) -> dict:
    sig = state["signal"]
    reason = state.get("human_feedback") or "재판정 한도 초과"
    _append_ledger({
        "signal_id": sig["id"], "date": sig["date"], "fg_score": sig["fg_score"],
        "asset": sig["asset"], "action": sig["action"], "tier_pct": sig["tier_pct"],
        "cooldown_override": sig.get("cooldown_override", False),
        "amount_krw": 0, "auto": False, "human_decision": state.get("human_decision"),
        "reject_reason": reason, "executed_at": datetime.now(timezone.utc).isoformat(),
    })
    return {"rejected": True, "log": [f"반려: {reason}"]}


def build():
    g = StateGraph(State)
    g.add_node("classify", classify)
    g.add_node("ai_judge", ai_judge)
    g.add_node("review", review)
    g.add_node("execute", execute)
    g.add_node("reject", reject)

    g.add_edge(START, "classify")
    g.add_edge("classify", "ai_judge")
    g.add_conditional_edges("ai_judge", route_after_judge, {"review": "review", "execute": "execute"})
    g.add_conditional_edges("review", route_after_review,
                             {"execute": "execute", "reject": "reject", "ai_judge": "ai_judge"})
    g.add_edge("execute", END)
    g.add_edge("reject", END)

    conn = sqlite3.connect(str(BASE / "data" / "checkpoints.sqlite"), check_same_thread=False)
    checkpointer = SqliteSaver(conn)
    return g.compile(checkpointer=checkpointer)


APP = build()


def _config(signal_id: str) -> dict:
    return {"configurable": {"thread_id": signal_id}}


def start_signal(sig: dict) -> dict:
    init: State = {
        "signal": sig, "amount_krw": 0, "needs_approval": False, "stop_reason": "",
        "ai_reasoning": "", "human_decision": None, "human_feedback": None,
        "final_amount_krw": None, "executed": False, "rejected": False,
        "retry_count": 0, "log": [],
    }
    return APP.invoke(init, config=_config(sig["id"]))


def resume_signal(signal_id: str, answer: dict) -> dict:
    return APP.invoke(Command(resume=answer), config=_config(signal_id))


def get_snapshot(signal_id: str):
    return APP.get_state(_config(signal_id))


def is_pending(signal_id: str) -> bool:
    return bool(get_snapshot(signal_id).next)


def pending_payload(signal_id: str) -> Optional[dict]:
    snap = get_snapshot(signal_id)
    for task in snap.tasks:
        if task.interrupts:
            return task.interrupts[0].value
    return None


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(errors="replace")  # Windows 콘솔 cp949 대비(실측으로 겪음)
    import time
    signals = json.loads((BASE / "data" / "signals.json").read_text(encoding="utf-8"))["items"]
    for sig in signals:
        result = start_signal(sig)
        time.sleep(2)  # claude_agent_sdk 연속 호출 시 일시적 오류 빈도를 줄이기 위한 간격
        if "__interrupt__" in result:
            print(f"{sig['id']}: 승인 대기 — {result['__interrupt__'][0].value['멈춘 이유']}")
        else:
            print(f"{sig['id']}: 자동 통과 — {result['final_amount_krw']:,}원 체결")
