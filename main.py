import anthropic
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

app = FastAPI()
client = anthropic.Anthropic()

# v2: 프롬프트를 역할/규칙/출력형식으로 구조화 — 코드는 그대로, 프롬프트만 바꿔 품질을 올린다
SYSTEM_PROMPT = """당신은 회의록을 정리하는 전문 서기입니다.

## 규칙
- 회의 내용에 실제로 나온 것만 쓴다. 없는 내용을 지어내지 않는다
- 회의 중 번복된 결정은 최종 결정만 남기고, 번복 사실을 결정사항에 병기한다
- 담당자가 명시되지 않은 할 일은 담당자를 "(미정)"으로 표시한다
- 잡담·인사말은 제외한다

## 출력 형식 (순서와 제목을 그대로 지킬 것)
### 한 줄 요약
(이 회의를 한 문장으로)

### 결정사항
- (결정된 것만. 논의만 되고 결정 안 된 것은 제외)

### Action Item
| 담당자 | 할 일 | 기한 |
|---|---|---|

### 다음 일정
- (다음 회의·마감 등. 없으면 "없음")"""


# v3: LLM API는 무상태(stateless) — "기억"은 우리가 저장했다가 매 요청에 실어 보내는 것.
# 서버 메모리에만 저장하므로 서버를 재시작하면 사라진다 (수업 포인트 → DB는 범위 밖)
meetings: list[dict] = []

ASK_PROMPT = """당신은 회의 기록을 바탕으로 질문에 답하는 비서입니다.

## 규칙
- 아래 제공된 회의 기록에 근거해서만 답한다
- 기록에 없는 내용은 "회의 기록에 없는 내용입니다"라고 답한다
- 여러 회의에서 결정이 바뀐 경우, 가장 최근 회의의 결정을 기준으로 답하되 변경 이력을 덧붙인다
- 답은 간결하게, 근거가 된 회의 번호를 함께 표시한다"""


class SummarizeRequest(BaseModel):
    meeting_text: str


class AskRequest(BaseModel):
    question: str


@app.get("/api/meetings")
def meeting_status():
    return {"meeting_count": len(meetings)}


@app.post("/api/summarize")
def summarize(req: SummarizeRequest):
    response = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": req.meeting_text}],
    )
    summary = next(block.text for block in response.content if block.type == "text")
    meetings.append({"meeting_text": req.meeting_text, "summary": summary})
    return {"summary": summary, "meeting_count": len(meetings)}


@app.post("/api/ask")
def ask(req: AskRequest):
    if not meetings:
        return {"answer": "저장된 회의가 없습니다. 먼저 회의를 요약해 주세요.", "meeting_count": 0}

    # 기억 = 저장해둔 요약을 프롬프트에 다시 넣어 보내는 것
    context = "\n\n".join(
        f"[회의 {i + 1}]\n{m['summary']}" for i, m in enumerate(meetings)
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
