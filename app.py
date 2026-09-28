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

signals = json.loads((BASE / "data" / "signals.json").read_text(encoding="utf-8"))["items"]
by_id = {s["id"]: s for s in signals}


def run_all_if_needed():
    """세션 시작 시 15건을 전부 한 번 흘려보낸다(멈출 건 멈추고, 자동인 건 끝까지 실행)."""
    if st.session_state.get("_ran"):
        return
    for sig in signals:
        snap = graph.get_snapshot(sig["id"])
        if not snap.values:  # 이 thread_id로 아직 한 번도 안 돌렸으면
            graph.start_signal(sig)
    st.session_state["_ran"] = True


run_all_if_needed()

tab_pending, tab_history = st.tabs(["승인 대기", "처리 이력"])

with tab_pending:
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
