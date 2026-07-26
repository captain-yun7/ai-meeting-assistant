# AI Meeting Assistant

회의 내용을 입력하면 AI가 처리해주는 회의 비서. 강의(LLM 서비스 개발 기초)에서 버전을 올려가며 확장한다.

## 버전 로드맵

| 버전 | 기능 | 배우는 것 |
|---|---|---|
| v1 | 회의 내용 → 요약 | LLM API 연결, 요청/응답 |
| v2 | 요약 품질 개선 | Prompt Engineering |
| v3 | 이전 회의 기억 | Memory / Context |
| v4 | 회의록 JSON 생성 | Structured Output |
| v5 | 일정 등록 등 외부 기능 | Tool Calling |
| v6 | Notion 저장 | MCP |
| v7 | 회사 문서 참고 | RAG |
| v8 | 회의 비서 완성 | AI Agent |

## 실행 방법

```bash
# 1. 가상환경 생성 및 의존성 설치 (최초 1회)
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. API 키 설정 (최초 1회)
cp .env.example .env   # .env를 열어 발급받은 키 입력

# 3. 서버 실행
uvicorn main:app --reload
```

브라우저에서 http://localhost:8000 접속 → `sample-data/meeting-01.txt` 내용을 붙여넣고 요약.

## 구조

```
main.py              # FastAPI 백엔드 — LLM 호출은 전부 여기서
static/index.html    # 프론트 (건드릴 일 거의 없음)
sample-data/         # 실습용 회의록
```
