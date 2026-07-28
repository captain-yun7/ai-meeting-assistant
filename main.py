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
#   구축(문서 업로드 시): 문서 → 청킹 → 임베딩 → 벡터 DB에 저장
#   검색(질문마다):      질문 임베딩 → 유사도 검색 → top-k만 반환
import uuid  # noqa: E402

import chromadb  # noqa: E402
from sentence_transformers import SentenceTransformer  # noqa: E402

embedder = SentenceTransformer("intfloat/multilingual-e5-small")

# 벡터 DB(Chroma). 인메모리 모드 — 서버 재시작 시 초기화 (영속화하려면 PersistentClient 한 줄)
chroma = chromadb.Client()
docs_collection = chroma.create_collection(
    "company-docs", metadata={"hnsw:space": "cosine"}  # 유사도 기준: 코사인
)


def index_document(source: str, text: str) -> int:
    """문서 하나를 청킹→임베딩해서 벡터 DB에 추가한다. 추가된 청크 수를 반환."""
    # 청킹: "## " 소제목 단위로 자른다 — 문서 구조를 살리는 가장 단순한 전략
    chunks = [c.strip() for c in text.split("\n## ") if c.strip()]
    if not chunks:
        return 0
    vectors = [
        embedder.encode(f"passage: {c}", normalize_embeddings=True).tolist()
        for c in chunks
    ]
    docs_collection.add(
        ids=[uuid.uuid4().hex for _ in chunks],
        embeddings=vectors,
        documents=chunks,
        metadatas=[{"source": source} for _ in chunks],
    )
    return len(chunks)


def search_company_docs(query: str) -> str:
    if docs_collection.count() == 0:
        return "인덱스에 문서가 없습니다. 화면의 '회사 문서' 섹션에서 규정 문서를 먼저 업로드해야 검색할 수 있습니다."
    query_vector = embedder.encode(f"query: {query}", normalize_embeddings=True).tolist()
    # 벡터 DB가 유사도 검색(top-k)을 대신해준다
    result = docs_collection.query(
        query_embeddings=[query_vector],
        n_results=min(3, docs_collection.count()),
        include=["documents", "metadatas"],
    )
    return "\n\n---\n\n".join(
        f"[{meta['source']}]\n{doc}"
        for doc, meta in zip(result["documents"][0], result["metadatas"][0])
    )


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
    for meta in docs_collection.get(include=["metadatas"])["metadatas"]:
        counts[meta["source"]] = counts.get(meta["source"], 0) + 1
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


@app.get("/api/calendar")
def calendar_list():
    return {"calendar": calendar}


def anthropic_api_config():
    """v6: NOTION_MCP_TOKEN이 있으면 Notion의 MCP 서버(남이 만들어둔 도구 세트)를 연결.
    MCP 도구는 API가 서버 쪽에서 대신 실행해준다 — 우리 루프는 우리 도구만 실행하면 된다."""
    notion_token = os.getenv("NOTION_MCP_TOKEN")
    if notion_token:
        return client.beta.messages, {
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
    return client.messages, {"tools": TOOLS}


def run_tool_loop(system: str, user_content: str) -> str:
    """tool use 루프 — v8까지의 심장. v9부터 질문(/api/ask)은 이 순정 루프를,
    자동 처리(/api/process)는 LangGraph 그래프를 쓴다 — 같은 일, 두 구현의 대비."""
    history = [{"role": "user", "content": user_content}]
    api, extra = anthropic_api_config()

    # 모델이 도구를 그만 찾을 때까지 [호출 의사 → 실행 → 결과 반환] 반복.
    # v8 안전장치: 자율 루프에는 반드시 상한을 둔다 (무한 루프·폭주 비용 방지)
    for _ in range(15):
        response = api.create(
            model="claude-opus-4-8",
            max_tokens=4096,
            system=system,
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
    # 상한(15회)까지 도구만 부르다 끝나면 텍스트가 하나도 없을 수 있다
    texts = [block.text for block in response.content if block.type == "text"]
    if not texts:
        return "답을 정리하지 못했습니다 (도구 호출 상한에 도달). 질문을 좁혀서 다시 시도해 주세요."
    return texts[-1]


def meetings_context() -> str:
    return "\n\n".join(
        f"[회의 {i + 1}]\n{json.dumps(m['minutes'], ensure_ascii=False, indent=2)}"
        for i, m in enumerate(meetings)
    )


# v8: Agent — 새 기술이 아니다. LLM + Prompt + Memory(v3) + Tool(v5·v6) + RAG(v7)가
# 이미 다 모여 있고, 바뀌는 것은 하나: 사람이 한 건씩 시키던 것을 모델이 스스로 계획해서
# 연쇄 실행한다. "무엇을 할지"의 주도권이 프롬프트의 지시에서 모델의 판단으로 넘어간다.
AGENT_PROMPT = """당신은 회의가 끝나면 후속 처리를 스스로 수행하는 AI 회의 비서입니다.

방금 끝난 회의의 회의록(JSON)이 주어집니다. 아래 임무를 스스로 판단해서 필요한 것을 모두 수행하세요.

## 임무
1. next_schedule의 일정들을 캘린더에 등록한다 (register_schedule)
2. 회의 내용 중 회사 규정 확인이 필요한 사안(외부인 방문, 경비, 야근, 보안 등)이 있으면
   search_company_docs로 규정을 검색해서 지켜야 할 것을 확인한다
3. 회의록을 문서 저장소에 저장한다 (Notion 도구가 연결되어 있으면 그것을, 아니면 save_minutes)

## 최종 보고 형식
모든 처리가 끝나면 아래 형식으로 보고한다:
- **처리한 일**: 등록한 일정, 저장 위치 (링크 포함)
- **규정 확인 결과**: 관련 규정과 지켜야 할 것 (규정 문서가 없거나 관련 규정이 없으면 그렇게 표기)
- **사람이 챙겨야 할 일**: 비서가 대신 못 하는 것 (승인, 예약 등)

## 규칙
- 허락을 구하거나 예고만 하지 말고 같은 턴에서 바로 실행한다
- 회의록과 검색 결과에 없는 내용은 지어내지 않는다"""


# ── v9: LangGraph — 순정 루프를 그래프로 ──────────────────────────────────
# LangGraph는 오케스트레이션(누가 언제 실행되는지)만 담당하고, 모델 호출은
# 기존 Anthropic SDK 그대로다(Notion MCP 포함). 전환의 실익은 interrupt:
# "위험한 도구(캘린더 등록) 실행 전 사람 승인"을 그래프 멈춤/재개로 얻는다.
import uuid as _uuid  # noqa: E402
from typing import TypedDict  # noqa: E402

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402
from langgraph.types import Command, interrupt  # noqa: E402


class AgentState(TypedDict):
    history: list  # v3부터 쓰던 그 대화 이력 — 그래프의 "상태"가 됐을 뿐
    stop_reason: str
    report: str


def agent_node(state: AgentState) -> dict:
    """순정 루프의 'api.create 호출' 부분이 노드 하나가 된 것."""
    api, extra = anthropic_api_config()
    response = api.create(
        model="claude-opus-4-8",
        max_tokens=4096,
        system=AGENT_PROMPT,
        messages=state["history"],
        **extra,
    )
    texts = [b.text for b in response.content if b.type == "text"]
    return {
        "history": state["history"] + [{"role": "assistant", "content": response.content}],
        "stop_reason": response.stop_reason,
        "report": texts[-1] if texts else "",
    }


# 승인이 필요한 도구 = "쓰기" 도구. 되돌리기 어려운 행동만 사람에게 묻는다.
# 읽기(search_company_docs)까지 물으면 승인 피로만 쌓이고 실제로 안전해지지는 않는다.
#
# 주의: Notion MCP 도구는 이 집합에 넣어도 소용없다. MCP 도구는 API 서버가 대신
# 실행하므로 우리 루프에 실행 대상으로 도착하지 않는다 — 클라이언트에서 막으려면
# MCP 대신 우리 커스텀 도구로 감싸야 한다. (편리함과 통제력의 맞교환)
APPROVAL_REQUIRED = {"register_schedule", "save_minutes"}


def approval_label(name: str, tool_input: dict) -> str:
    """승인 카드에 보여줄 한 줄 요약 — 사용자는 무엇을 승인하는지 알아야 한다."""
    if name == "register_schedule":
        when = " ".join(filter(None, [tool_input.get("date"), tool_input.get("time")]))
        return f"일정 등록: {when} — {tool_input.get('title', '(제목 없음)')}"
    if name == "save_minutes":
        return f"회의록 저장: {tool_input.get('title', '(제목 없음)')}"
    return f"{name}: {tool_input}"


def tools_node(state: AgentState) -> dict:
    """순정 루프의 '도구 실행' 부분. 단, 쓰기 도구는 실행 전에 사람 승인을 받는다."""
    blocks = [b for b in state["history"][-1]["content"] if b.type == "tool_use"]
    write_calls = [b for b in blocks if b.name in APPROVAL_REQUIRED]

    decision = {"approved": True}
    if write_calls:
        # interrupt: 그래프가 여기서 멈추고 상태가 저장된다.
        # 사용자가 /api/approve로 답하면 이 지점부터 재개되어 답이 반환값이 된다
        decision = interrupt(
            {
                "action": "write_tools",
                "message": "다음 작업을 실행하려 합니다. 승인하시겠습니까?",
                "items": [
                    {"tool": b.name, "label": approval_label(b.name, b.input)}
                    for b in write_calls
                ],
            }
        )

    approved = decision.get("approved", False)
    tool_results = []
    for b in blocks:
        if b.name in APPROVAL_REQUIRED and not approved:
            result, is_error = (
                f"사용자가 '{b.name}' 실행을 거부했습니다. 실행하지 않았습니다. 보고에 반영하세요.",
                False,
            )
        else:
            result, is_error = run_tool(b.name, b.input)
        tool_results.append(
            {
                "type": "tool_result",
                "tool_use_id": b.id,
                "content": result,
                "is_error": is_error,
            }
        )
    return {"history": state["history"] + [{"role": "user", "content": tool_results}]}


def route(state: AgentState) -> str:
    """순정 루프의 if/break가 조건 엣지가 된 것."""
    if state["stop_reason"] == "tool_use":
        return "tools"
    if state["stop_reason"] == "pause_turn":  # 서버측(MCP) 작업 재개
        return "agent"
    return END


_builder = StateGraph(AgentState)
_builder.add_node("agent", agent_node)
_builder.add_node("tools", tools_node)
_builder.add_edge(START, "agent")
_builder.add_conditional_edges("agent", route, {"tools": "tools", "agent": "agent", END: END})
_builder.add_edge("tools", "agent")
# checkpointer: 매 단계의 상태 저장 — interrupt에서 멈췄다 재개하는 능력의 원천
agent_graph = _builder.compile(checkpointer=MemorySaver())


def graph_result(state: dict, config: dict, minutes_payload: dict | None = None) -> dict:
    """그래프 실행 결과를 API 응답으로 변환 — 승인 대기 중인지 완료인지 구분."""
    snapshot = agent_graph.get_state(config)
    if snapshot.next:  # 그래프가 중간(interrupt)에 멈춰 있음
        for task in snapshot.tasks:
            if task.interrupts:
                return {
                    "status": "pending_approval",
                    "thread_id": config["configurable"]["thread_id"],
                    "approval": task.interrupts[0].value,
                    "minutes": minutes_payload,
                    "meeting_count": len(meetings),
                    "calendar": calendar,
                }
    return {
        "status": "done",
        "report": state.get("report", ""),
        "minutes": minutes_payload,
        "meeting_count": len(meetings),
        "calendar": calendar,
    }


class ApproveRequest(BaseModel):
    thread_id: str
    approved: bool


@app.post("/api/process")
def process_meeting(req: MeetingRequest):
    # 1단계(고정): 회의록 생성 — v4의 Structured Output. 항상 실행되므로 그래프 밖의 일반 코드
    response = client.messages.parse(
        model="claude-opus-4-8",
        max_tokens=2048,
        system=MINUTES_PROMPT,
        messages=[{"role": "user", "content": req.meeting_text}],
        output_format=MeetingMinutes,
    )
    result = response.parsed_output.model_dump()
    meetings.append({"meeting_text": req.meeting_text, "minutes": result})

    # 2단계(자율): LangGraph 그래프 실행. thread_id = 이 처리 건의 식별자 (승인 재개에 사용)
    config = {"configurable": {"thread_id": _uuid.uuid4().hex}}
    state = agent_graph.invoke(
        {
            "history": [
                {
                    "role": "user",
                    "content": f"방금 끝난 회의의 회의록:\n\n{json.dumps(result, ensure_ascii=False, indent=2)}\n\n후속 처리를 진행해 주세요.",
                }
            ],
            "stop_reason": "",
            "report": "",
        },
        config,
    )
    return graph_result(state, config, minutes_payload=result)


@app.post("/api/approve")
def approve(req: ApproveRequest):
    config = {"configurable": {"thread_id": req.thread_id}}

    # 승인 대기 상태는 MemorySaver(인메모리)에만 있다 — 서버가 재시작되면(--reload 포함)
    # 사라진다. 재개할 지점이 없으면 조용히 실패하지 말고 만료를 알린다
    if not agent_graph.get_state(config).next:
        return {
            "status": "expired",
            "report": "승인 대기 정보가 만료되었습니다 (서버 재시작 등). 회의를 다시 처리해 주세요.",
            "minutes": None,
            "meeting_count": len(meetings),
            "calendar": calendar,
        }

    # Command(resume=...): interrupt에서 멈춘 그래프를 사용자의 결정과 함께 재개
    state = agent_graph.invoke(Command(resume={"approved": req.approved}), config)
    return graph_result(state, config)


@app.post("/api/ask")
def ask(req: AskRequest):
    if not meetings:
        return {"answer": "저장된 회의가 없습니다. 먼저 회의를 처리해 주세요.", "meeting_count": 0}

    answer = run_tool_loop(
        ASK_PROMPT,
        f"지금까지의 회의 기록:\n\n{meetings_context()}\n\n질문: {req.question}",
    )
    return {"answer": answer, "meeting_count": len(meetings), "calendar": calendar}


app.mount("/", StaticFiles(directory="static", html=True), name="static")
