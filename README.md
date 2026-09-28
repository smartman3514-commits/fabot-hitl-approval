# FABOT 매매 승인 에이전트 (Human-in-the-Loop 실습)

FABOT 자동매매 신호를 실제로 실행하기 전에, 되돌리기 어려운 결정만 사람에게 확인받고
나머지는 자동으로 처리하는 LangGraph 기반 HITL 파이프라인. 설계 배경과 결과는
[REPORT.md](REPORT.md) 참고.

## 실행 방법

```bash
pip install langgraph langgraph-checkpoint-sqlite claude-agent-sdk streamlit
```

1. 네이티브 Claude CLI 설치 확인:
   ```powershell
   irm https://claude.ai/install.ps1 | iex
   ```
2. 15건의 시드 신호를 한 번 흘려보내 대기 건을 만든다(이미 해봤다면 생략 가능):
   ```bash
   python graph.py
   ```
3. 승인 화면 실행:
   ```bash
   streamlit run app.py
   ```
4. 브라우저에서 `http://localhost:8503` 접속 — "승인 대기" 탭에서 멈춘 건을 확인하고
   승인/수정 후 승인/반려/다시 판정 중 하나를 고른다. "처리 이력" 탭에서 전체 결과를 본다.

## 구조

- `graph.py` — LangGraph 파이프라인(`classify` → `ai_judge` → [필요하면] `review`(interrupt) →
  `execute`/`reject`), `SqliteSaver`로 대기 상태를 `data/checkpoints.sqlite`에 저장.
- `app.py` — Streamlit 승인 화면.
- `data/signals.json` — 실제 F&G 지수 역사 데이터(`fabot-auto-trade/fg_index.csv`)에서 뽑은
  15건의 신호(매수/매도, 다양한 F&G 구간, 쿨다운 예외 케이스 1건 포함).
- `data/executions.json` — (모의) 체결·반려 기록. 실제 FABOT 운영 계좌와는 무관.
- `data/checkpoints.sqlite` — 대기/완료 상태 저장(파일 기반이라 재시작해도 안 사라짐).

## 중요: 이 프로젝트의 위치

FABOT의 최종 목표는 **사람 개입 없는 완전 자동화**다. 이 승인 단계는 이번 HITL 실습
과제 때문에 의도적으로 넣은 것이지, FABOT의 실제 운영 방향을 바꾸는 게 아니다
(REPORT.md 1절 참고).
