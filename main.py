import anthropic
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

app = FastAPI()
client = anthropic.Anthropic()

SYSTEM_PROMPT = "당신은 회의록을 요약하는 AI 비서입니다. 회의 내용을 받으면 핵심을 간결하게 요약합니다."


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
