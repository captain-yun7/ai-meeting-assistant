import json
import os
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

app = FastAPI()
client = anthropic.Anthropic()

# v3: LLM API는 무상태(stateless) — "기억"은 우리가 저장했다가 매 요청에 실어 보내는 것.
# 서버 메모리에만 저장하므로 서버를 재시작하면 사라진다 (수업 포인트 → DB는 범위 밖)
meetings: list[dict] = []


# v4: 출력의 "모양"을 스키마로 강제 — 프롬프트는 내용을, 스키마는 형식을 책임진다.
# 요청 검증에 쓰던 pydantic을 LLM 출력 검증에 그대로 쓴다
class ActionItem(BaseModel):
    assignee: str  # 담당자가 없으면 "(미정)"
    task: str
    due: str | None  # 기한이 없으면 null


class MeetingMinutes(BaseModel):
    one_line_summary: str
    decisions: list[str]
    action_items: list[ActionItem]
    next_schedule: list[str]


# v2에서 배운 프롬프트 구조화 — v4부터 형식은 스키마가 맡고, 내용 규칙만 남았다
MINUTES_PROMPT = """당신은 회의록을 정리하는 전문 서기입니다.

## 규칙
- 회의 내용에 실제로 나온 것만 쓴다. 없는 내용을 지어내지 않는다
- 회의 중 번복된 결정은 최종 결정만 남기고, 번복 사실을 결정사항에 병기한다
- decisions에는 결정된 것만 넣는다. 논의만 되고 결정 안 된 것은 제외한다
- 담당자가 명시되지 않은 할 일은 assignee를 "(미정)"으로 한다
- 기한이 명시되지 않은 할 일은 due를 null로 한다"""

ASK_PROMPT = """당신은 회의 기록을 바탕으로 질문에 답하고 일을 처리하는 비서입니다.

## 정보의 원천 (이 두 가지에 근거해서만 답한다)
1. 아래 제공된 회의 기록
2. search_company_docs로 검색한 회사 규정 — 질문이 회사 규정·절차와 조금이라도 관련되면(방문, 경비, 근태, 보안 등) 허락을 구하지 말고 먼저 검색한다

두 원천 어디에도 없는 내용만 "기록에 없는 내용입니다"라고 답한다. 지어내지 않는다.

## 규칙
- 여러 회의에서 결정이 바뀐 경우, 가장 최근 회의의 결정을 기준으로 답하되 변경 이력을 덧붙인다
- 답은 간결하게, 근거를 함께 표시한다 (회의 번호, 규정 문서명)
- 사용자가 일정 등록을 요청하면 register_schedule 도구를 사용한다. 등록 후 무엇을 등록했는지 알려준다
- 사용자가 회의록 저장을 요청하면 저장 도구(Notion 도구가 연결되어 있으면 그것을, 아니면 save_minutes)를 사용하고, 어디에 저장했는지 알려준다
- 도구를 사용할 일은 말로 예고하거나 허락을 구하지 말고 같은 턴에서 바로 실행한다"""


# v5: Tool Calling — LLM이 처음으로 "행동"한다.
# 모델은 도구를 직접 실행하지 못한다. "이 도구를 이 입력으로 호출하고 싶다"는 의사만
# 반환하고, 실행은 언제나 우리 코드가 한다. (실제 서비스라면 여기가 구글 캘린더 API 자리)
calendar: list[dict] = []

TOOLS = [
    {
        "name": "register_schedule",
        "description": "팀 캘린더에 일정을 등록한다. 사용자가 회의·마감 등의 일정 등록을 요청할 때 사용한다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "일정 제목"},
                "date": {"type": "string", "description": "날짜 (회의록에 나온 표현 그대로. 예: 2026-08-04, 다음 주 월요일)"},
                "time": {"type": "string", "description": "시간 (예: 10:00). 언급이 없으면 생략"},
            },
            "required": ["title", "date"],
        },
    }
]


def register_schedule(title: str, date: str, time: str | None = None) -> str:
    calendar.append({"title": title, "date": date, "time": time})
    when = f"{date} {time}" if time else date
    return f"등록 완료: {title} ({when})"


# v6: 회의록 저장 도구.
# 지금은 로컬 파일 저장(폴백)이지만, 인터페이스는 "Notion 저장"과 동일하다.
# NOTION_MCP_TOKEN이 설정되면 아래 MCP 연결(ask 함수 참고)로 진짜 Notion에 저장된다.
SAVE_DIR = Path("saved-minutes")

TOOLS.append(
    {
        "name": "save_minutes",
        "description": "가장 최근에 생성된 회의록을 문서 저장소에 저장한다. 사용자가 회의록 저장을 요청할 때 사용한다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "문서 제목 (예: 2026-07-20 주간 개발 회의)"},
            },
            "required": ["title"],
        },
    }
)


def save_minutes(title: str) -> str:
    if not meetings:
        return "오류: 저장할 회의록이 없습니다"
    latest = meetings[-1]["minutes"]
    SAVE_DIR.mkdir(exist_ok=True)
    safe_name = title.replace("/", "-")
    path = SAVE_DIR / f"{safe_name}.md"

    lines = [f"# {title}", "", f"> {latest['one_line_summary']}", "", "## 결정사항"]
    lines += [f"- {d}" for d in latest["decisions"]]
    lines += ["", "## Action Item", "| 담당자 | 할 일 | 기한 |", "|---|---|---|"]
    lines += [
        f"| {a['assignee']} | {a['task']} | {a['due'] or '-'} |"
        for a in latest["action_items"]
    ]
    lines += ["", "## 다음 일정"]
    lines += [f"- {s}" for s in latest["next_schedule"]] or ["- 없음"]
    path.write_text("\n".join(lines), encoding="utf-8")
    return f"저장 완료: {path}"


# v7: RAG — 회사 문서가 많아지면 전부 프롬프트에 넣을 수 없다(비용·컨텍스트 한계).
# 그래서 "질문과 의미가 가까운 조각만 골라서" 넣는다:
#   구축(서버 시작 시 1회): 문서 → 청킹 → 임베딩 → 저장
#   검색(질문마다):        질문 임베딩 → 유사도 비교 → top-k만 반환
from sentence_transformers import SentenceTransformer  # noqa: E402
import numpy as np  # noqa: E402

embedder = SentenceTransformer("intfloat/multilingual-e5-small")
doc_chunks: list[dict] = []  # 미니 벡터 DB: [{"source", "text", "vector"}]


def index_document(source: str, text: str) -> int:
    """문서 하나를 청킹→임베딩해서 인덱스에 추가한다. 추가된 청크 수를 반환."""
    added = 0
    # 청킹: "## " 소제목 단위로 자른다 — 문서 구조를 살리는 가장 단순한 전략
    for chunk in text.split("\n## "):
        chunk = chunk.strip()
        if not chunk:
            continue
        vector = embedder.encode(f"passage: {chunk}", normalize_embeddings=True)
        doc_chunks.append({"source": source, "text": chunk, "vector": vector})
        added += 1
    return added


# 인덱스는 빈 상태로 시작한다 — 문서는 화면에서 업로드하는 순간 검색 대상이 된다.
# (company-docs/는 업로드용 샘플 문서 모음)


def search_company_docs(query: str) -> str:
    if not doc_chunks:
        return "인덱스에 문서가 없습니다. 화면의 '회사 문서' 섹션에서 규정 문서를 먼저 업로드해야 검색할 수 있습니다."
    query_vector = embedder.encode(f"query: {query}", normalize_embeddings=True)
    # 코사인 유사도 = (정규화된 벡터끼리의) 내적 — "의미가 가까운 순" 정렬
    scored = sorted(
        doc_chunks,
        key=lambda c: float(np.dot(query_vector, c["vector"])),
        reverse=True,
    )
    top = scored[:3]
    return "\n\n---\n\n".join(f"[{c['source']}]\n{c['text']}" for c in top)


TOOLS.append(
    {
        "name": "search_company_docs",
        "description": "회사 규정·정책 문서(경비, 보안, 근태 등)에서 관련 내용을 검색한다. 회사 규정이나 절차에 대한 질문이 나오면 반드시 이 도구로 검색해서 근거를 확보한 뒤 답한다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "검색할 내용 (예: 야근 식대 지원)"},
            },
            "required": ["query"],
        },
    }
)

TOOL_FUNCTIONS = {
    "register_schedule": register_schedule,
    "save_minutes": save_minutes,
    "search_company_docs": search_company_docs,
}


def run_tool(name: str, tool_input: dict) -> tuple[str, bool]:
    """도구 실행 — 모델이 부르는 이름과 인자는 신뢰할 수 없는 입력이다(시스템 경계).
    실패해도 서버를 죽이지 않고 오류를 tool_result로 돌려주면 모델이 스스로 복구한다."""
    func = TOOL_FUNCTIONS.get(name)
    if func is None:
        return f"오류: '{name}'은(는) 존재하지 않는 도구입니다.", True
    try:
        return func(**tool_input), False
    except Exception as e:
        return f"오류: 도구 실행 실패 ({type(e).__name__}: {e})", True


class MeetingRequest(BaseModel):
    meeting_text: str


class AskRequest(BaseModel):
    question: str


class DocUploadRequest(BaseModel):
    filename: str
    content: str


def doc_sources() -> list[dict]:
    counts: dict[str, int] = {}
    for c in doc_chunks:
        counts[c["source"]] = counts.get(c["source"], 0) + 1
    return [{"source": s, "chunks": n} for s, n in counts.items()]


@app.get("/api/docs")
def docs_list():
    return {"docs": doc_sources()}


@app.post("/api/docs")
def docs_upload(req: DocUploadRequest):
    # 업로드 = RAG의 "구축 단계"가 실시간으로 일어나는 것: 청킹 → 임베딩 → 인덱스 추가
    source = req.filename.rsplit(".", 1)[0]
    added = index_document(source, req.content)
    return {"added_chunks": added, "docs": doc_sources()}


@app.get("/api/meetings")
def meeting_status():
    return {"meeting_count": len(meetings)}


@app.post("/api/minutes")
def minutes(req: MeetingRequest):
    response = client.messages.parse(
        model="claude-opus-4-8",
        max_tokens=2048,
        system=MINUTES_PROMPT,
        messages=[{"role": "user", "content": req.meeting_text}],
        output_format=MeetingMinutes,  # 응답이 이 스키마를 따르도록 강제 + 자동 검증
    )
    result = response.parsed_output.model_dump()
    meetings.append({"meeting_text": req.meeting_text, "minutes": result})
    return {"minutes": result, "meeting_count": len(meetings)}


@app.get("/api/calendar")
def calendar_list():
    return {"calendar": calendar}


@app.post("/api/ask")
def ask(req: AskRequest):
    if not meetings:
        return {"answer": "저장된 회의가 없습니다. 먼저 회의록을 생성해 주세요.", "meeting_count": 0}

    # 기억 = 저장해둔 회의록(JSON)을 프롬프트에 다시 넣어 보내는 것
    context = "\n\n".join(
        f"[회의 {i + 1}]\n{json.dumps(m['minutes'], ensure_ascii=False, indent=2)}"
        for i, m in enumerate(meetings)
    )
    history = [
        {
            "role": "user",
            "content": f"지금까지의 회의 기록:\n\n{context}\n\n질문: {req.question}",
        }
    ]

    # v6: NOTION_MCP_TOKEN이 있으면 Notion의 MCP 서버(남이 만들어둔 도구 세트)를 연결.
    # MCP 도구는 API가 서버 쪽에서 대신 실행해준다 — 우리 루프는 우리 도구만 실행하면 된다
    notion_token = os.getenv("NOTION_MCP_TOKEN")
    if notion_token:
        api = client.beta.messages
        extra = {
            "betas": ["mcp-client-2025-11-20"],
            "mcp_servers": [
                {
                    "type": "url",
                    "url": "https://mcp.notion.com/mcp",
                    "name": "notion",
                    "authorization_token": notion_token,
                }
            ],
            "tools": TOOLS + [{"type": "mcp_toolset", "mcp_server_name": "notion"}],
        }
    else:
        api = client.messages
        extra = {"tools": TOOLS}

    # tool use 루프: 모델이 도구를 그만 찾을 때까지 [호출 의사 → 실행 → 결과 반환] 반복
    while True:
        response = api.create(
            model="claude-opus-4-8",
            max_tokens=4096,
            system=ASK_PROMPT,
            messages=history,
            **extra,
        )
        if response.stop_reason == "pause_turn":  # 서버측(MCP) 도구 작업이 길어지면 이어서 재개
            history.append({"role": "assistant", "content": response.content})
            continue
        if response.stop_reason != "tool_use":
            break

        history.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                # 실행은 우리 코드가 한다 — 모델은 이름과 입력만 정했을 뿐
                result, is_error = run_tool(block.name, block.input)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                        "is_error": is_error,
                    }
                )
        history.append({"role": "user", "content": tool_results})

    # 응답에는 텍스트 블록이 여러 개일 수 있다 (도구 사용 전 예고 + 사용 후 결과 보고).
    # 사용자에게 보여줄 답은 마지막 텍스트 블록 — 도구 결과까지 반영된 최종 발화다.
    # 도구만 부르다 끝나면 텍스트가 하나도 없을 수 있다
    texts = [block.text for block in response.content if block.type == "text"]
    answer = texts[-1] if texts else "답을 정리하지 못했습니다. 질문을 좁혀서 다시 시도해 주세요."
    return {"answer": answer, "meeting_count": len(meetings), "calendar": calendar}


app.mount("/", StaticFiles(directory="static", html=True), name="static")
