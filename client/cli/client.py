import os
import sys
import httpx
import asyncio
from typing import Optional, Dict, Any
from dotenv import load_dotenv
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.text import Text
from rich.live import Live
from rich.spinner import Spinner

# 설정 로드
load_dotenv()

# 환경 변수 체크 및 기본값 설정
SERVER_URL = os.getenv("ALPHRED_SERVER_URL", "http://localhost:8000")
ACCESS_TOKEN = os.getenv("ALPHRED_ACCESS_TOKEN")
CONSOLE = Console()

def clear_screen():
    """OS에 맞는 화면 지우기 명령을 실행합니다."""
    os.system('cls' if os.name == 'nt' else 'clear')

async def send_message(client: httpx.AsyncClient, message: str) -> Optional[Dict[str, Any]]:
    """서버에 비동기 요청을 보내고 응답을 받습니다."""
    url = f"{SERVER_URL.rstrip('/')}/chat"
    headers = {
        "Content-Type": "application/json",
        "x-alphred-token": ACCESS_TOKEN
    }
    payload = {"message": message}

    try:
        response = await client.post(url, json=payload, headers=headers, timeout=60.0)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as e:
        CONSOLE.print(f"[bold red]HTTP 오류:[/bold red] {e.response.status_code} - {e.response.text}")
    except httpx.RequestError as e:
        CONSOLE.print(f"[bold red]연결 오류:[/bold red] {str(e)}")
    except Exception as e:
        CONSOLE.print(f"[bold red]오류 발생:[/bold red] {str(e)}")
    return None

async def main():
    if not ACCESS_TOKEN:
        CONSOLE.print("[bold red]오류:[/bold red] .env 파일에서 ALPHRED_ACCESS_TOKEN을 찾을 수 없습니다.")
        return

    clear_screen()
    
    # 헤더 패널 (사용자 버전의 스타일 채용)
    CONSOLE.print(Panel.fit(
        "[bold cyan]Alphred 지능형 비서 시스템 v3.1[/bold cyan]\n"
        f"[white]연결된 서버: {SERVER_URL}[/white]\n\n"
        "[dim]종료하려면 'exit' 또는 'quit'을 입력하세요.[/dim]",
        title="🤖 Alphred CLI",
        border_style="bright_magenta"
    ))

    async with httpx.AsyncClient() as client:
        while True:
            try:
                # 사용자 입력 (사용자 버전의 '알파' 프롬프트 채용)
                user_input = Prompt.ask("\n[bold green]알파[/bold green]")
                
                if not user_input.strip():
                    continue
                
                if user_input.lower() in ["/exit", "/quit", "exit", "quit"]:
                    CONSOLE.print("[yellow]접속을 종료합니다. 좋은 하루 되세요![/yellow]")
                    break
                
                if user_input.lower() in ["/clear", "clear"]:
                    clear_screen()
                    continue

                # 대기 애니메이션 (사용자 버전의 Live Spinner + Async 조합)
                response = None
                with Live(Spinner("bouncingBar", text="[cyan]Alphred가 생각 중...[/cyan]"), transient=True):
                    response = await send_message(client, user_input)

                if response:
                    reply = response.get("reply", "")
                    was_long_term = response.get("long_term_searched", False)
                    mcp_used = response.get("mcp_used", [])

                    # UI 구성 (사용자 버전의 배지 스타일 채용)
                    title_text = Text("Alphred", style="bold blue")
                    badges = []
                    
                    if was_long_term:
                        badges.append("[기억 참조]")
                    
                    for mcp_name in mcp_used:
                        badges.append(f"[{mcp_name} 실행]")
                    
                    if badges:
                        title_text += Text(" " + " ".join(badges), style="italic magenta")

                    # 답변 출력 (Markdown 렌더링 추가)
                    # Panel 안에 Markdown 객체를 넣어서 코드 하이라이팅 지원
                    CONSOLE.print(Panel(
                        Markdown(reply),
                        title=title_text,
                        border_style="cyan",
                        padding=(1, 2)
                    ))

            except KeyboardInterrupt:
                CONSOLE.print("\n[yellow]시스템을 종료합니다.[/yellow]")
                break

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
