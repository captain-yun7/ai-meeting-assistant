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


class SummarizeRequest(BaseModel):
    meeting_text: str


@app.post("/api/summarize")
def summarize(req: SummarizeRequest):
    response = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": req.meeting_text}],
    )
    summary = next(block.text for block in response.content if block.type == "text")
    return {"summary": summary}

app.mount("/", StaticFiles(directory="static", html=True), name="static")
