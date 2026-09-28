"""FABOT 매매 승인 데모 — 대기 중인 건을 확인하고 승인/수정/반려/재판정한다.
실행: streamlit run app.py
"""
import json
import os
from pathlib import Path

import streamlit as st

try:
    has_oauth_secret = "CLAUDE_CODE_OAUTH_TOKEN" in st.secrets
except Exception:
    has_oauth_secret = False
if has_oauth_secret:
    os.environ.setdefault("CLAUDE_CODE_OAUTH_TOKEN", st.secrets["CLAUDE_CODE_OAUTH_TOKEN"])

import graph

BASE = Path(__file__).parent

st.set_page_config(page_title="FABOT 매매 승인", page_icon="✅")
st.title("✅ FABOT 매매 승인 데모")
st.caption("Human-in-the-Loop 실습 프로젝트 — 되돌릴 수 없는 매매 결정 앞에서 멈추고, 사람이 승인/수정/반려/재판정한다")

with st.expander("이 데모에 대해", expanded=False):
    st.markdown(
        """
        - **FABOT의 최종 목표는 완전 자동화**입니다 — 이 승인 단계는 이번 과제(HITL) 전용으로,
          FABOT의 실제 운영 방향을 바꾸는 게 아닙니다.
        - **승인이 필요한 3가지 기준**(서로 다른 위험): 매수 비중 100%(금액 위험) ·
          매도 전량(비가역 위험) · 쿨다운 예외 해제 매수(로직 오류 위험)
        - 여기 안 걸리는 매수 25%/50%, 매도 50%, 평시 커버드콜 10% 정기매수는 이미 검증된
          반복 규칙이라 자동으로 통과합니다.
        - 신호 15건은 실제 F&G 지수 역사 데이터(`fabot-auto-trade/fg_index.csv`)에서 각
          구간별로 뽑은 진짜 날짜·점수입니다. 체결은 전부 모의(로컬 `data/executions.json`)이고,
          실제 FABOT 운영 계좌와는 연결돼 있지 않습니다.
        """
    )

SEED_SIGNALS = json.loads((BASE / "data" / "signals.json").read_text(encoding="utf-8"))["items"]
USER_SIGNALS_PATH = BASE / "data" / "user_signals.json"


def load_user_signals() -> list:
    if USER_SIGNALS_PATH.exists():
        return json.loads(USER_SIGNALS_PATH.read_text(encoding="utf-8"))["items"]
    return []


def save_user_signal(sig: dict):
    items = load_user_signals()
    items.append(sig)
    USER_SIGNALS_PATH.write_text(json.dumps({"items": items}, ensure_ascii=False, indent=2),
                                  encoding="utf-8")


signals = SEED_SIGNALS + load_user_signals()
by_id = {s["id"]: s for s in signals}


def run_all_if_needed():
    """세션 시작 시 시드 15건을 전부 한 번 흘려보낸다(멈출 건 멈추고, 자동인 건 끝까지 실행).
    직접 넣은 신호는 제출하는 순간 바로 흘려보내므로 여기서 다시 처리할 필요가 없다."""
    if st.session_state.get("_ran"):
        return
    for sig in SEED_SIGNALS:
        snap = graph.get_snapshot(sig["id"])
        if not snap.values:  # 이 thread_id로 아직 한 번도 안 돌렸으면
            graph.start_signal(sig)
    st.session_state["_ran"] = True


run_all_if_needed()

with st.expander("➕ 새 신호 직접 넣기", expanded=False):
    with st.form("new_signal_form", clear_on_submit=True):
        c1, c2 = st.columns(2)
        date = c1.date_input("날짜").isoformat()
        fg_score = c2.number_input("F&G 점수", min_value=0.0, max_value=100.0, value=50.0, step=1.0)
        asset = c1.selectbox("자산", ["TQQQ", "커버드콜"])
        action = c2.selectbox("동작", ["buy", "sell"])
        tier_pct = c1.selectbox("비중", [1.0, 0.5, 0.25, 0.1],
                                 format_func=lambda x: f"{x*100:.0f}%")
        cooldown_override = c2.checkbox("쿨다운 예외 해제로 실행되는 매수")
        if action == "buy":
            amount_base = st.number_input("가용 실탄(원)", min_value=0, value=30000000, step=1000000)
        else:
            amount_base = st.number_input("보유 평가금액(원)", min_value=0, value=50000000, step=1000000)
        submitted = st.form_submit_button("신호 넣기", type="primary")
        if submitted:
            new_id = f"U{len(load_user_signals()) + 1:03d}"
            sig = {
                "id": new_id, "date": date, "fg_score": fg_score, "asset": asset,
                "action": action, "tier_pct": tier_pct, "cooldown_override": cooldown_override,
                "available_capital_krw": amount_base if action == "buy" else None,
                "held_value_krw": amount_base if action == "sell" else None,
            }
            save_user_signal(sig)
            graph.start_signal(sig)
            st.success(f"{new_id} 신호를 넣었습니다.")
            st.rerun()

tab_pending, tab_history = st.tabs(["승인 대기", "처리 이력"])

with tab_pending:
    # by_id에 방금 넣은 신호도 반영되도록 매번 새로 불러온다(폼 제출 직후 rerun에서도 최신 상태 유지).
    signals = SEED_SIGNALS + load_user_signals()
    by_id = {s["id"]: s for s in signals}
    pending_ids = [s["id"] for s in signals if graph.is_pending(s["id"])]
    st.write(f"**대기 중 {len(pending_ids)}건** / 전체 {len(signals)}건")

    if not pending_ids:
        st.info("대기 중인 건이 없습니다.")

    for sid in pending_ids:
        sig = by_id[sid]
        payload = graph.pending_payload(sid)
        with st.container(border=True):
            st.subheader(f"{sid} — {payload['요청']}")
            st.markdown(f"**금액**: {payload['금액']}")
            st.markdown(f"**AI 판정**: {payload['AI 판정']}")
            st.warning(f"**멈춘 이유**: {payload['멈춘 이유']}")
            st.markdown(f"**통과시키면**: {payload['통과시키면']}")

            col1, col2, col3, col4 = st.columns(4)
            if col1.button("승인", key=f"approve-{sid}", type="primary", use_container_width=True):
                graph.resume_signal(sid, {"action": "approve"})
                st.rerun()
            with col2.popover("수정 후 승인", use_container_width=True):
                new_amt = st.number_input("수정 금액(원)", min_value=0,
                                           value=int(graph.get_snapshot(sid).values["amount_krw"]),
                                           step=100000, key=f"amt-{sid}")
                if st.button("이 금액으로 승인", key=f"modify-go-{sid}"):
                    graph.resume_signal(sid, {"action": "modify", "amount": new_amt})
                    st.rerun()
            with col3.popover("반려", use_container_width=True):
                reason = st.text_area("반려 사유", key=f"reason-{sid}")
                if st.button("반려 확정", key=f"reject-go-{sid}"):
                    graph.resume_signal(sid, {"action": "reject", "feedback": reason or "사유 미기재"})
                    st.rerun()
            with col4.popover("다시 판정", use_container_width=True):
                fb = st.text_area("AI에게 줄 피드백", key=f"fb-{sid}")
                if st.button("재판정 요청", key=f"retry-go-{sid}"):
                    graph.resume_signal(sid, {"action": "retry", "feedback": fb or "더 구체적으로 판단해줘"})
                    st.rerun()

with tab_history:
    ledger_path = BASE / "data" / "executions.json"
    items = json.loads(ledger_path.read_text(encoding="utf-8"))["items"] if ledger_path.exists() else []
    st.write(f"**처리 완료 {len(items)}건**")
    for it in reversed(items):
        icon = "🤖" if it["auto"] else ("✅" if it["amount_krw"] > 0 else "❌")
        label = f"{icon} {it['signal_id']} — {it['date']} {it['asset']} {it['action']}"
        with st.expander(label):
            st.json(it)

st.divider()
st.caption("신호 데이터: `data/signals.json` (실제 F&G 역사 기반) · 체결 기록: `data/executions.json` · "
           "대기 상태 저장: `data/checkpoints.sqlite`")
