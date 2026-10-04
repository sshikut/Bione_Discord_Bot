import os
import asyncio
import requests
import uvicorn
from fastapi import FastAPI, Request
from dotenv import load_dotenv
import discord
from discord import app_commands
from groq import Groq

load_dotenv()

# 환경 변수 로드
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
PORT = int(os.getenv("PORT", "8000"))

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO_OWNER = os.getenv("GITHUB_REPO_OWNER")
GITHUB_REPO_NAME = os.getenv("GITHUB_REPO_NAME")
GITHUB_BRANCH = os.getenv("GITHUB_DEFAULT_BRANCH", "main")

# 클라이언트 초기화
groq_client = Groq(api_key=GROQ_API_KEY)
intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)
app = FastAPI()

# --- GitHub REST API 헬퍼 함수 ---
def fetch_repo_file_content(path: str) -> str:
    """GitHub API를 통해 특정 경로의 최신 파일 원문을 가져옵니다."""
    url = f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/contents/{path}?ref={GITHUB_BRANCH}"
    headers = {
        "Accept": "application/vnd.github.v3.raw",
        "User-Agent": "Discord-RAG-Bot"
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"

    resp = requests.get(url, headers=headers)
    if resp.status_code == 200:
        return resp.text
    return ""

# 도메인 키워드 -> 연관 스크립트 매핑 테이블 (토큰 절약 & 정확도 핵심)
DOMAIN_KEYWORD_MAP = {
    # 손님 / 물류 / 구매 / AI 관련 질문
    "Customer": ["손님", "고객", "npc", "구매", "물류", "선택", "쇼핑", "바구니", "장바구니"],
    # 아이템 / 신선도 / 보관 관련 질문
    "ItemData": ["아이템", "신선도", "냉장고", "유통기한", "보관", "부패", "음식"],
    # 결제 / 포스기 / 매출 관련 질문
    "POS": ["포스", "계산", "결제", "정산", "매출", "돈", "가격"],
    # 플레이어 / 조작 관련 질문
    "Player": ["플레이어", "조작", "이동", "상호작용", "속도"]
}

def search_relevant_script(keyword: str) -> tuple[str, str]:
    """저장소 내 C# 스크립트 중 질문 의도와 가장 밀접한 단 1개 파일만 핀포인트 로드 (토큰 최적화)"""
    tree_url = f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/git/trees/{GITHUB_BRANCH}?recursive=1"
    headers = {"User-Agent": "Discord-RAG-Bot"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"

    resp = requests.get(tree_url, headers=headers)
    if resp.status_code != 200:
        return "", ""

    files = [item["path"] for item in resp.json().get("tree", []) if item["path"].endswith(".cs")]
    query_lower = keyword.lower()

    # 1단계: 도메인 자연어 키워드 매핑 매칭 (예: '손님', '물류' -> Customer 관련 스크립트 우선 탐색)
    target_class_hint = None
    for class_hint, keywords in DOMAIN_KEYWORD_MAP.items():
        if any(k in query_lower for k in keywords):
            target_class_hint = class_hint.lower()
            break

    if target_class_hint:
        for path in files:
            file_name = path.split("/")[-1].replace(".cs", "").lower()
            if target_class_hint in file_name:
                content = fetch_repo_file_content(path)
                return path, content

    # 2단계: 스크립트 파일명 직접 언급 매칭 (예: "ItemData에서~")
    for path in files:
        file_name = path.split("/")[-1].replace(".cs", "").lower()
        if file_name in query_lower:
            content = fetch_repo_file_content(path)
            return path, content

    # 3단계: 매칭되는 도메인이 없으면 불필요한 토큰 낭비를 막기 위해 코드를 태우지 않음 (일반 대화/모름 처리)
    return "", ""

def fetch_recent_commits(branch_name: str = None, count: int = 5) -> str:
    """특정 브랜치의 최근 커밋 N개를 조회하여 텍스트로 반환"""
    target = branch_name or GITHUB_BRANCH
    url = f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/commits?sha={target}&per_page={count}"
    headers = {"User-Agent": "Discord-RAG-Bot"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"

    resp = requests.get(url, headers=headers)
    if resp.status_code != 200:
        return "커밋 내역을 불러오지 못했습니다."

    commits = resp.json()
    result = []
    for c in commits:
        sha = c.get("sha", "")[:7]
        author = c.get("commit", {}).get("author", {}).get("name", "Unknown")
        date = c.get("commit", {}).get("author", {}).get("date", "")[:10]
        message = c.get("commit", {}).get("message", "").strip().split("\n")[0]
        result.append(f"- [{sha}] {message} (작업자: {author}, 일자: {date})")

    return "\n".join(result)

def fetch_all_branches() -> list[dict]:
    """저장소의 모든 브랜치 목록과 최신 커밋 SHA를 반환"""
    url = f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/branches"
    headers = {"User-Agent": "Discord-RAG-Bot"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"

    resp = requests.get(url, headers=headers)
    if resp.status_code == 200:
        return resp.json()
    return []

def fetch_branch_activity_summary(target_branch: str = None) -> str:
    """
    특정 브랜치 또는 활성 feature 브랜치들의 최근 작업 내역을 동적으로 수집
    """
    branches = fetch_all_branches()
    if not branches:
        return "브랜치 목록을 가져올 수 없습니다."

    summary_lines = []

    # 1. 사용자가 특정 브랜치를 지정해서 질문한 경우 (예: feature/xxx)
    if target_branch:
        commits = fetch_recent_commits(target_branch, count=3)
        return f"### 📌 브랜치 [{target_branch}] 최근 작업 내역:\n{commits}"

    # 2. 전체적인 기능 개발 브랜치 동적 파악
    # main, master를 제외한 feature, feat, dev 브랜치들을 우선 필터링
    active_branches = [
        b["name"] for b in branches 
        if b["name"] not in ["main", "master"]
    ]

    if not active_branches:
        # feature 브랜치가 없으면 기본 브랜치 커밋 반환
        return f"현재 활성화된 작업(feature) 브랜치가 없습니다.\n\n### 📌 기본 브랜치 ({GITHUB_BRANCH}) 최근 작업:\n" + fetch_recent_commits(GITHUB_BRANCH, 3)

    summary_lines.append(f"현재 발견된 작업 브랜치: {', '.join([f'`{b}`' for b in active_branches])}\n")

    # 각 브랜치별 최근 커밋 2~3개씩 요약 수집 (최대 4개 브랜치)
    for b_name in active_branches[:4]:
        commits = fetch_recent_commits(b_name, count=2)
        summary_lines.append(f"▶ **브랜치 `{b_name}`**:\n{commits}")

    return "\n\n".join(summary_lines)

# --- Groq LPU 추론 함수 ---
def query_groq(prompt: str, system_prompt: str) -> tuple[str, str, bool]:
    """
    Groq LPU API를 호출하며, 429 한도 초과 시 대체 모델로 자동 우회(Fallback)합니다.
    반환값: (답변 내용, 사용된 모델명, Fallback 발생 여부)
    """
    fallback_models = ["openai/gpt-oss-20b", "openai/gpt-oss-120b", "qwen/qwen3.8-27b"]
    default_model = fallback_models[0]
    
    last_error = ""
    for idx, model_name in enumerate(fallback_models):
        try:
            completion = groq_client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3,
                max_completion_tokens=1024,
                top_p=1,
                stream=False
            )
            # 첫 번째 모델이 아니면 Fallback이 발생한 것(True)
            is_fallback = (idx > 0)
            return completion.choices[0].message.content.strip(), model_name, is_fallback
        except Exception as e:
            last_error = str(e)
            # 429 한도 초과(Rate Limit) 에러인 경우에만 다음 대체 모델로 루프 진행
            if "429" in last_error or "rate_limit" in last_error.lower():
                print(f"[경고] {model_name} 한도 초과(429). 대체 모델로 전환합니다...")
                continue
            # 인증 실패 등 다른 에러는 즉시 반환
            return f"Groq 추론 에러: {last_error}", model_name, False

    return f"모든 대체 모델의 한도가 초과되었습니다: {last_error}", "None", True

# --- 디스코드 슬래시 커맨드 (/질문) ---

@tree.command(name="질문가이드", description="AI 어시스턴트에게 효과적으로 질문하는 방법과 예시를 안내합니다.")
async def question_guide(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📖 게임 개발 AI 어시스턴트 질문 가이드",
        description="봇은 GitHub 저장소의 최신 C# 코드와 실시간 커밋 내역을 분석하여 답변합니다.\n원하는 정보에 맞게 아래 키워드를 포함해 질문해 보세요!",
        color=discord.Color.green()
    )

    embed.add_field(
        name="1️⃣ C# 시스템 스펙 & 데이터 질문",
        value=(
            "• 스크립트 이름이나 도메인(아이템, 손님, 포스기 등)을 포함하세요.\n"
            "• `예시: ItemData에서 냉장 보관 아이템 신선도 로직 어떻게 돼?`\n"
            "• `예시: 손님이 물건 고르는 기준이나 AI 이동 로직 알려줘`"
        ),
        inline=False
    )

    embed.add_field(
        name="2️⃣ 브랜치 & 최근 커밋 현황 질문",
        value=(
            "• '커밋', '최근', '브랜치' 키워드를 포함하세요.\n"
            "• `예시: 최근 커밋 내역 요약해줘`\n"
            "• `예시: feature/itemDataCreator 브랜치에서 뭐 작업했어?`\n"
            "• `예시: 지금 브랜치별 작업 현황 알려줘`"
        ),
        inline=False
    )

    embed.add_field(
        name="3️⃣ 일상 잡담 & 아이디어 브레인스토밍",
        value=(
            "• 개발 질문이 아니어도 편하게 물어보세요.\n"
            "• `예시: 오늘 개발 끝나고 먹을 저녁 메뉴 추천해줘`\n"
            "• `예시: 편의점 진열대 관련 재밌는 기획 아이디어 있어?`"
        ),
        inline=False
    )

    embed.set_footer(text="💡 구체적인 시스템 명칭이나 파일명을 언급할수록 답변 정확도가 올라갑니다!")
    await interaction.response.send_message(embed=embed)

@tree.command(name="질문", description="GitHub 최신 코드, 브랜치별 작업 내역 기반으로 AI에게 질문합니다.")
@app_commands.describe(query="궁금한 시스템 스펙, 특정 브랜치 작업 내역, 또는 일상 질문을 입력하세요")
async def ask_rag(interaction: discord.Interaction, query: str):
    # 1. 질문이 너무 짧거나 모호한 경우 가이드 안내 (불필요한 API 토큰 낭비 방지)
    clean_query = query.strip()
    if len(clean_query) < 4:
        guide_embed = discord.Embed(
            title="❓ 질문이 너무 짧아요!",
            description="더 정확한 답변을 위해 조금 더 구체적으로 질문해 주세요.\n`/질문가이드` 명령어를 입력하시면 자세한 예시를 확인하실 수 있습니다.",
            color=discord.Color.orange()
        )
        guide_embed.add_field(
            name="💡 추천 질문 예시",
            value=(
                "• `ItemData 스크립트 역할 분석해줘`\n"
                "• `손님의 물류 선택 기준이 뭐야?`\n"
                "• `최근 커밋 변경점 3줄 요약해줘`"
            ),
            inline=False
        )
        await interaction.response.send_message(embed=guide_embed, ephemeral=True) # ephemeral=True: 본인에게만 보임
        return

    await interaction.response.defer()

    # 브랜치 관련 키워드 감지
    branch_keywords = ["브랜치", "branch", "feature", "feat", "작업 현황"]
    is_branch_query = any(k in query.lower() for k in branch_keywords)
    commit_keywords = ["커밋", "최근", "변경점", "업데이트"]
    is_commit_query = any(k in query for k in commit_keywords)

    matched_path = ""
    
    if is_branch_query:
        # 혹시 질문 안에 특정 브랜치명이 직접 적혀있는지 체크
        all_branches = [b["name"] for b in fetch_all_branches()]
        target_b = next((b for b in all_branches if b.lower() in query.lower()), None)
        
        branch_context = fetch_branch_activity_summary(target_branch=target_b)
        context_text = f"[GitHub 활성 브랜치 작업 현황]:\n{branch_context}"
        matched_path = f"동적 브랜치 스캔 ({target_b if target_b else '전체 작업 브랜치'})"

    elif is_commit_query:
        recent_commits = fetch_recent_commits(GITHUB_BRANCH, 5)
        context_text = f"[최근 저장소 커밋 내역 ({GITHUB_BRANCH})]:\n{recent_commits}"
        matched_path = f"최근 커밋 히스토리 ({GITHUB_BRANCH})"

    else:
        # 기존 C# 코드 검색
        matched_path, script_code = search_relevant_script(query)
        context_text = f"참조 파일 경로: {matched_path}\n\n{script_code}" if script_code else "참조 가능한 C# 코드를 찾지 못함 (일반 대화 모드로 전환)"

    # Groq 추론 실행 (시스템 프롬프트는 기존과 동일하게 context_text 주입)
    system_instruction = f"""
    당신은 편의점 게임 개발팀의 전문 테크니컬 리드 AI 어시스턴트입니다.

    [답변 모드 가이드]:
    1. 최근 커밋 또는 개발/코드 질문:
       - 아래 제공된 [GitHub 컨텍스트]를 최우선 팩트로 삼아 알기 쉽게 답변하세요.
       - 커밋 내역 질문인 경우 변경된 핵심 작업 흐름을 요약해 주세요.
    2. 일반 잡담 등 일상 질문:
       - 게임 개발팀 동료처럼 친절하고 유쾌하게 답변하세요.

    [디스코드 가독성 출력 규칙]:
    - 마크다운 표(|---|---|)는 절대 사용하지 마세요.
    - 불릿 포인트(`•`), 볼드체(`**`), 인라인 코드 블록(`` ` ``)을 사용하세요.
    - 핵심 요약은 인용 블록(`> `)을 활용하세요.

    [GitHub 컨텍스트]:
    {context_text}
    """

    # 추론 실행 (답변, 실제 쓰인 모델, 폴백 여부 수신)
    answer, used_model, is_fallback = query_groq(query, system_instruction)

    # 기본 모델이 막혀서 우회된 경우 경고 문구 추가
    fallback_notice = f"⚠️ [안내] 기본 모델 한도 초과로 대체 모델(`{used_model}`)이 사용되었습니다.\n\n" if is_fallback else ""

    # 디스코드 임베드 카드 생성
    embed = discord.Embed(
        title="💬 AI 채선우의 답변",
        description=fallback_notice + answer,
        color=discord.Color.gold() if is_fallback else discord.Color.blue()  # 우회 시 주황/골드색으로 강조
    )
    embed.add_field(name="질문", value=f"`{query}`", inline=False)
    
    footer_text = f"엔진: {used_model}"
    if matched_path:
        footer_text += f" | {matched_path}"
    embed.set_footer(text=footer_text)

    await interaction.followup.send(embed=embed)

@bot.event
async def on_ready():
    await tree.sync()
    print(f"디스코드 봇 로그인 완료: {bot.user.name}")
    print(f"GitHub 동기화 리포지토리: {GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME} ({GITHUB_BRANCH})")

# --- FastAPI Webhook 엔드포인트 ---
@app.get("/")
def health_check():
    return {"status": "ok", "service": "GitHub Live Fetch & Webhook Discord Bot"}

@app.post("/webhook/github")
async def github_webhook(request: Request):
    event_type = request.headers.get("X-GitHub-Event", "ping")
    
    if event_type == "ping":
        print("GitHub Webhook Ping 수신 완료!")
        return {"msg": "pong"}

    payload = await request.json()
    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    if not channel:
        print(f"디스코드 채널(ID: {DISCORD_CHANNEL_ID})을 찾을 수 없습니다.")
        return {"status": "error", "message": "Channel not found"}

    # ---------------------------------------------------------
    # 1. PUSH 이벤트 (새로운 커밋이 올라왔을 때)
    # ---------------------------------------------------------
    if event_type == "push":
        commits = payload.get("commits", [])
        if not commits:
            return {"status": "ignored", "reason": "No commits"}

        ref = payload.get("ref", "")
        branch = ref.replace("refs/heads/", "")
        pusher = payload.get("pusher", {}).get("name", "Unknown")
        repo_name = payload.get("repository", {}).get("name", "Repo")
        compare_url = payload.get("compare", "")

        # 커밋 메시지 및 변경 파일 수집
        commit_lines = []
        for c in commits[:5]:
            msg = c.get("message", "").strip().split("\n")[0]
            cid = c.get("id", "")[:7]
            commit_lines.append(f"• [`{cid}`] {msg}")
        commits_text = "\n".join(commit_lines)

        # Groq 요약 요청
        prompt = f"""
        [저장소]: {repo_name}
        [브랜치]: {branch}
        [작업자]: {pusher}
        [커밋 목록]:
        {commits_text}

        위 커밋 내역의 변경점을 분석하여 기획자 및 아트 팀원이 알기 쉽게 3줄 이내의 핵심 요약으로 작성해줘.
        """
        system_instruction = """
        당신은 편의점 게임 개발팀의 친절한 테크니컬 릴리즈 브리퍼입니다.
        커밋 메시지를 바탕으로 게임 플레이, 데이터, 리소스 등에 미치는 핵심 영향도를 1, 2, 3 번호 매김 형식으로 3줄 요약만 작성하세요.
        마크다운 표는 절대 쓰지 말고, 간결하고 명확한 구어체 한국어로 작성하세요.
        """
        ai_summary, used_model, _ = query_groq(prompt, system_instruction)

        # 디스코드 임베드 생성
        embed = discord.Embed(
            title=f"📦 [{repo_name}] 새로운 커밋 푸시 알림",
            url=compare_url,
            color=discord.Color.green()
        )
        embed.add_field(name="🌿 브랜치 / 작업자", value=f"`{branch}` 브랜치 | `{pusher}`", inline=False)
        embed.add_field(name="📝 커밋 내역", value=commits_text[:1000], inline=False)
        embed.add_field(name="💡 AI 핵심 요약 (3줄)", value=ai_summary, inline=False)
        embed.set_footer(text=f"GitHub Webhook Auto-Briefing | Engine: {used_model}")

        await channel.send(embed=embed)
        return {"status": "ok", "event": "push"}

    # ---------------------------------------------------------
    # 2. PULL REQUEST 이벤트 (PR 생성, 머지 등)
    # ---------------------------------------------------------
    elif event_type == "pull_request":
        action = payload.get("action")
        pr = payload.get("pull_request", {})
        title = pr.get("title", "")
        body = pr.get("body", "") or "설명 없음"
        user = pr.get("user", {}).get("login", "Unknown")
        pr_url = pr.get("html_url", "")
        head_branch = pr.get("head", {}).get("ref", "")
        base_branch = pr.get("base", {}).get("ref", "")
        merged = pr.get("merged", False)

        # 관심 있는 액션: opened(새 PR 생성) 또는 closed이면서 merged(머지 완료)
        if action == "opened":
            status_title = "🔀 새로운 Pull Request 요청"
            card_color = discord.Color.blue()
            status_desc = f"`{head_branch}` ➔ `{base_branch}` 병합 요청이 등록되었습니다."
        elif action == "closed" and merged:
            status_title = "🎉 Pull Request 머지 완료 (Release)"
            card_color = discord.Color.purple()
            status_desc = f"`{head_branch}` 브랜치가 `{base_branch}`에 성공적으로 병합되었습니다."
        else:
            return {"status": "ignored", "action": action}

        prompt = f"""
        [PR 제목]: {title}
        [PR 내용]: {body}
        [작업 브랜치]: {head_branch} -> {base_branch}
        [작업자]: {user}

        위 PR 내용을 분석하여 기획/아트 팀원이 바로 이해할 수 있도록 어떤 기능이 변경되었는지 3줄 이내로 핵심 요약해줘.
        """
        system_instruction = """
        당신은 편의점 게임 개발팀의 테크니컬 리드입니다.
        PR의 개발 의도와 변경 사항을 비개발 직군도 한눈에 알기 쉽게 1, 2, 3 번호 매김 형식으로 핵심 요약 3줄만 작성하세요.
        마크다운 표는 쓰지 마세요.
        """
        ai_summary, used_model, _ = query_groq(prompt, system_instruction)

        embed = discord.Embed(
            title=f"{status_title}: #{pr.get('number')} {title}",
            url=pr_url,
            color=card_color
        )
        embed.add_field(name="상태", value=status_desc, inline=False)
        embed.add_field(name="작업자", value=f"`{user}`", inline=True)
        embed.add_field(name="브랜치", value=f"`{head_branch}` ➔ `{base_branch}`", inline=True)
        embed.add_field(name="💡 AI 핵심 요약 (3줄)", value=ai_summary, inline=False)
        embed.set_footer(text=f"GitHub Webhook Auto-Briefing | Engine: {used_model}")

        await channel.send(embed=embed)
        return {"status": "ok", "event": "pull_request", "action": action}

    return {"status": "ignored", "event": event_type}

async def main():
    config = uvicorn.Config(app=app, host="0.0.0.0", port=PORT, log_level="info")
    server = uvicorn.Server(config)
    await asyncio.gather(
        server.serve(),
        bot.start(DISCORD_BOT_TOKEN)
    )

if __name__ == "__main__":
    asyncio.run(main())