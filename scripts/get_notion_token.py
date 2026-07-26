"""Notion MCP OAuth 토큰 발급 스크립트.

사용법: python scripts/get_notion_token.py
브라우저가 열리면 Notion 로그인 후 워크스페이스 접근을 허용하면 끝.
발급된 토큰은 .env의 NOTION_MCP_TOKEN에 자동 기록된다.
(주의: mcp.notion.com은 OAuth 토큰만 받는다 — Notion REST API의 ntn_ 키가 아님)
"""

import base64
import hashlib
import http.server
import json
import secrets
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

MCP_URL = "https://mcp.notion.com/mcp"
CALLBACK_PORT = 8901
REDIRECT_URI = f"http://localhost:{CALLBACK_PORT}/callback"
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def fetch_json(url: str, data: bytes | None = None, headers: dict | None = None) -> dict:
    # 기본 python-urllib User-Agent는 Cloudflare 등에서 차단될 수 있다
    merged = {"User-Agent": "ai-meeting-assistant-oauth/1.0", "Accept": "application/json"}
    merged.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=merged)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def discover_auth_server() -> dict:
    origin = "https://mcp.notion.com"
    for prm_url in (
        f"{origin}/.well-known/oauth-protected-resource/mcp",
        f"{origin}/.well-known/oauth-protected-resource",
    ):
        try:
            prm = fetch_json(prm_url)
            auth_server = prm["authorization_servers"][0].rstrip("/")
            break
        except Exception:
            auth_server = origin

    parsed = urllib.parse.urlparse(auth_server)
    candidates = [
        f"{parsed.scheme}://{parsed.netloc}/.well-known/oauth-authorization-server{parsed.path}",
        f"{auth_server}/.well-known/oauth-authorization-server",
    ]
    for url in candidates:
        try:
            return fetch_json(url)
        except Exception:
            continue
    sys.exit(f"인증 서버 메타데이터를 찾지 못했습니다: {candidates}")


def main() -> None:
    meta = discover_auth_server()

    # 동적 클라이언트 등록 (RFC 7591)
    reg = fetch_json(
        meta["registration_endpoint"],
        data=json.dumps(
            {
                "client_name": "ai-meeting-assistant (local)",
                "redirect_uris": [REDIRECT_URI],
                "grant_types": ["authorization_code"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            }
        ).encode(),
        headers={"Content-Type": "application/json"},
    )
    client_id = reg["client_id"]

    # PKCE
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    state = secrets.token_urlsafe(16)
    auth_url = meta["authorization_endpoint"] + "?" + urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "resource": MCP_URL,
        }
    )

    print(f"\n브라우저에서 Notion 인증을 완료하세요:\n{auth_url}\n")
    subprocess.run(["open", auth_url], check=False)

    # 콜백 대기
    code_holder: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" in q and q.get("state", [""])[0] == state:
                code_holder["code"] = q["code"][0]
                body = "<h2>인증 완료 — 터미널로 돌아가세요. 이 창은 닫아도 됩니다.</h2>"
            else:
                body = f"<h2>인증 실패</h2><pre>{self.path}</pre>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *args):
            pass

    with http.server.HTTPServer(("localhost", CALLBACK_PORT), Handler) as server:
        while "code" not in code_holder:
            server.handle_request()

    # 토큰 교환
    token = fetch_json(
        meta["token_endpoint"],
        data=urllib.parse.urlencode(
            {
                "grant_type": "authorization_code",
                "code": code_holder["code"],
                "redirect_uri": REDIRECT_URI,
                "client_id": client_id,
                "code_verifier": verifier,
                "resource": MCP_URL,
            }
        ).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    access_token = token["access_token"]

    # .env 기록 (기존 줄 교체 또는 추가)
    lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    lines = [l for l in lines if not l.startswith("NOTION_MCP_TOKEN=")]
    lines.append(f"NOTION_MCP_TOKEN={access_token}")
    ENV_PATH.write_text("\n".join(lines) + "\n")

    expires = token.get("expires_in")
    print("✅ 토큰 발급 완료 — .env의 NOTION_MCP_TOKEN에 저장했습니다.")
    if expires:
        print(f"   (만료: 약 {int(expires) // 3600}시간 후 — 수업 당일 재발급 권장)")
    print("   서버를 재시작하면 Notion 저장이 활성화됩니다.")


if __name__ == "__main__":
    main()
