import json

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

ASK_PROMPT = """당신은 회의 기록을 바탕으로 질문에 답하는 비서입니다.

## 규칙
- 아래 제공된 회의 기록에 근거해서만 답한다
- 기록에 없는 내용은 "회의 기록에 없는 내용입니다"라고 답한다
- 여러 회의에서 결정이 바뀐 경우, 가장 최근 회의의 결정을 기준으로 답하되 변경 이력을 덧붙인다
- 답은 간결하게, 근거가 된 회의 번호를 함께 표시한다"""


class MeetingRequest(BaseModel):
    meeting_text: str


class AskRequest(BaseModel):
    question: str


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


@app.post("/api/ask")
def ask(req: AskRequest):
    if not meetings:
        return {"answer": "저장된 회의가 없습니다. 먼저 회의록을 생성해 주세요.", "meeting_count": 0}

    # 기억 = 저장해둔 회의록(JSON)을 프롬프트에 다시 넣어 보내는 것
    context = "\n\n".join(
        f"[회의 {i + 1}]\n{json.dumps(m['minutes'], ensure_ascii=False, indent=2)}"
        for i, m in enumerate(meetings)
    )
    response = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=1024,
        system=ASK_PROMPT,
        messages=[
            {
                "role": "user",
                "content": f"지금까지의 회의 기록:\n\n{context}\n\n질문: {req.question}",
            }
        ],
    )
    answer = next(block.text for block in response.content if block.type == "text")
    return {"answer": answer, "meeting_count": len(meetings)}


app.mount("/", StaticFiles(directory="static", html=True), name="static")
