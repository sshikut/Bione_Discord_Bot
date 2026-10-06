import os
import re
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
GITHUB_DEFAULT_BRANCH = os.getenv("GITHUB_DEFAULT_BRANCH", "main")

# 클라이언트 초기화
groq_client = Groq(api_key=GROQ_API_KEY)
intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)
app = FastAPI()

# --- GitHub REST API 헬퍼 함수 ---

def get_github_headers():
    headers = {
        "User-Agent": "Discord-RAG-Bot",
        "Accept": "application/vnd.github.v3+json"
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"
    return headers

def fetch_all_branches() -> list[dict]:
    """저장소의 모든 브랜치 목록 반환"""
    url = f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/branches"
    resp = requests.get(url, headers=get_github_headers())
    if resp.status_code == 200:
        return resp.json()
    return []

def detect_target_branch(query: str, available_branches: list[str]) -> str:
    """사용자 질문에서 브랜치명이 언급되었는지 감지 (없으면 GITHUB_DEFAULT_BRANCH)"""
    query_lower = query.lower()
    for b in available_branches:
        if b.lower() in query_lower:
            return b
    # 질문에 명시적 브랜치가 없고 Develop 브랜치가 존재하면 Develop을 우선 기본값으로 고려
    if "develop" in [b.lower() for b in available_branches] and "main" not in query_lower:
        for b in available_branches:
            if b.lower() == "develop":
                return b
    return GITHUB_DEFAULT_BRANCH

def fetch_repo_file_content(path: str, branch: str) -> str:
    """특정 브랜치의 파일 원본 코드 가져오기"""
    url = f"https://raw.githubusercontent.com/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/{branch}/{path}"
    headers = {}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"
    resp = requests.get(url, headers=headers)
    if resp.status_code == 200:
        return resp.text
    return ""

# 도메인 키워드 -> 스크립트 파일명 힌트 매핑 테이블 (확장)
DOMAIN_KEYWORD_MAP = {
    # 물류 / 스폰 / 박스 / 포장
    "Cargo": ["물류", "카고", "화물"],
    "Spawner": ["스폰", "소환", "생성기", "spawner"],
    "Pack": ["포장", "패키징", "상자포장", "박싱", "wrap"],
    # 손님 / AI
    "Customer": ["손님", "고객", "npc", "구매", "쇼핑", "바구니", "장바구니"],
    # 진열 / 가구
    "Display": ["진열", "진열대", "선반", "전시", "배치", "buildable"],
    # 아이템 / 신선도 / 보관
    "Item": ["아이템", "신선도", "냉장고", "유통기한", "보관", "부패", "음식"],
    # 결제 / 포스기
    "POS": ["포스", "계산", "결제", "정산", "매출", "돈", "가격", "체크아웃", "checkout"],
    # 플레이어
    "Player": ["플레이어", "조작", "이동", "상호작용", "속도"]
}

def search_relevant_script(query: str, target_branch: str) -> tuple[str, str, list[str]]:
    """
    질문과 브랜치 기반으로 최적의 C# 스크립트 1개를 탐색.
    반환값: (매칭된 파일경로, 파일내용, 전체C#파일목록)
    """
    tree_url = f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/git/trees/{target_branch}?recursive=1"
    resp = requests.get(tree_url, headers=get_github_headers())
    if resp.status_code != 200:
        print(f"[Fetch Error] Trees API 실패 ({resp.status_code}) on branch {target_branch}")
        return "", "", []

    all_cs_files = [item["path"] for item in resp.json().get("tree", []) if item["path"].endswith(".cs")]
    query_lower = query.lower()

    # 1순위: 영문 스크립트명 또는 클래스명 직접 언급 탐색 (예: NetCargoSpawner, NetCargoSpawner.cs)
    words = re.findall(r'[a-zA-Z0-9_]+', query)
    for path in all_cs_files:
        file_name = path.split("/")[-1] # NetCargoSpawner.cs
        clean_name = file_name.replace(".cs", "") # NetCargoSpawner
        
        # .cs 명시 또는 단어 단위 일치 검사
        if file_name.lower() in query_lower:
            content = fetch_repo_file_content(path, target_branch)
            return path, content, all_cs_files
        for w in words:
            if len(w) >= 4 and w.lower() == clean_name.lower():
                content = fetch_repo_file_content(path, target_branch)
                return path, content, all_cs_files

    # 2순위: 도메인 자연어 키워드 매핑 매칭
    matched_hints = []
    for class_hint, keywords in DOMAIN_KEYWORD_MAP.items():
        if any(k in query_lower for k in keywords):
            matched_hints.append(class_hint.lower())

    if matched_hints:
        for path in all_cs_files:
            file_name = path.split("/")[-1].lower()
            # 힌트 단어가 파일명에 포함되어 있는지 검사 (예: cargo, spawner)
            if any(hint in file_name for hint in matched_hints):
                content = fetch_repo_file_content(path, target_branch)
                return path, content, all_cs_files

    return "", "", all_cs_files

def fetch_recent_commits(branch_name: str, count: int = 5) -> str:
    url = f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/commits?sha={branch_name}&per_page={count}"
    resp = requests.get(url, headers=get_github_headers())
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

def fetch_branch_activity_summary(target_branch: str = None) -> str:
    branches = fetch_all_branches()
    if not branches:
        return "브랜치 목록을 가져올 수 없습니다."

    if target_branch:
        commits = fetch_recent_commits(target_branch, count=3)
        return f"### 📌 브랜치 [{target_branch}] 최근 작업 내역:\n{commits}"

    active_branches = [b["name"] for b in branches if b["name"] not in ["main", "master"]]
    if not active_branches:
        return f"현재 활성화된 작업 브랜치가 없습니다.\n\n### 📌 기본 브랜치 최근 작업:\n" + fetch_recent_commits(GITHUB_DEFAULT_BRANCH, 3)

    summary_lines = [f"현재 발견된 작업 브랜치: {', '.join([f'`{b}`' for b in active_branches])}\n"]
    for b_name in active_branches[:4]:
        commits = fetch_recent_commits(b_name, count=2)
        summary_lines.append(f"▶ **브랜치 `{b_name}`**:\n{commits}")

    return "\n\n".join(summary_lines)

# --- Groq LPU 추론 함수 ---
def query_groq(prompt: str, system_prompt: str) -> tuple[str, str, bool]:
    fallback_models = ["openai/gpt-oss-20b", "openai/gpt-oss-120b", "qwen/qwen3.8-27b"]
    last_error = ""
    for idx, model_name in enumerate(fallback_models):
        try:
            completion = groq_client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.2, # 환각 방지를 위해 0.2로 낮춤
                max_completion_tokens=1024,
                top_p=1,
                stream=False
            )
            is_fallback = (idx > 0)
            return completion.choices[0].message.content.strip(), model_name, is_fallback
        except Exception as e:
            last_error = str(e)
            if "429" in last_error or "rate_limit" in last_error.lower():
                print(f"[경고] {model_name} 한도 초과(429). 대체 모델로 전환합니다...")
                continue
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
            "• 스크립트 이름이나 도메인을 브랜치와 함께 명시하세요.\n"
            "• `예시: Develop 브랜치의 NetCargoSpawner.cs 분석해줘`\n"
            "• `예시: 손님의 물류 선택 기준이나 AI 이동 로직 알려줘`"
        ),
        inline=False
    )
    embed.add_field(
        name="2️⃣ 브랜치 & 최근 커밋 현황 질문",
        value=(
            "• '커밋', '최근', '브랜치' 키워드를 포함하세요.\n"
            "• `예시: 최근 커밋 내역 요약해줘`\n"
            "• `예시: feature/BuildableDisplay 브랜치 최근 커밋 요약해줘`"
        ),
        inline=False
    )
    embed.add_field(
        name="3️⃣ 일상 잡담 & 아이디어 브레인스토밍",
        value="• `예시: 오늘 개발 끝나고 먹을 야식 추천해줘`",
        inline=False
    )
    embed.set_footer(text="💡 구체적인 파일명이나 브랜치를 언급할수록 정확도가 대폭 향상됩니다.")
    await interaction.response.send_message(embed=embed)

@tree.command(name="질문", description="GitHub 최신 코드, 브랜치별 작업 내역 기반으로 AI에게 질문합니다.")
@app_commands.describe(query="궁금한 시스템 스펙, 특정 브랜치 작업 내역, 또는 일상 질문을 입력하세요")
async def ask_rag(interaction: discord.Interaction, query: str):
    clean_query = query.strip()
    if len(clean_query) < 4:
        guide_embed = discord.Embed(
            title="❓ 질문이 너무 짧아요!",
            description="더 정확한 답변을 위해 조금 더 구체적으로 질문해 주세요.\n`/질문가이드` 명령어를 통해 예시를 확인하실 수 있습니다.",
            color=discord.Color.orange()
        )
        await interaction.response.send_message(embed=guide_embed, ephemeral=True)
        return

    await interaction.response.defer()

    # 1. 활성 브랜치 목록 동적 파악 및 타깃 브랜치 결정
    all_branch_dicts = fetch_all_branches()
    branch_names = [b["name"] for b in all_branch_dicts] if all_branch_dicts else [GITHUB_DEFAULT_BRANCH]
    target_branch = detect_target_branch(query, branch_names)

    # 2. 질문 의도 판별
    branch_keywords = ["브랜치", "branch", "작업 현황"]
    is_branch_query = any(k in query.lower() for k in branch_keywords)
    commit_keywords = ["커밋", "최근", "변경점", "업데이트"]
    is_commit_query = any(k in query for k in commit_keywords)
    
    # 코드/스크립트/구현 질문 여부 판별
    code_keywords = ["스크립트", ".cs", "코드", "구현", "어디", "함수", "로직", "스펙", "어떻게"]
    is_code_inquiry = any(k in query.lower() for k in code_keywords) or any(k in query for k in ["포장", "물류", "스폰", "손님", "진열", "계산"])

    matched_path = ""
    context_text = ""

    # 분기 A: 브랜치 작업 요약 요청
    if is_branch_query and not is_code_inquiry:
        target_b = next((b for b in branch_names if b.lower() in query.lower()), None)
        branch_context = fetch_branch_activity_summary(target_branch=target_b)
        context_text = f"[GitHub 활성 브랜치 작업 현황]:\n{branch_context}"
        matched_path = f"동적 브랜치 스캔 ({target_b if target_b else '전체 작업 브랜치'})"

    # 분기 B: 단순 최근 커밋 내역 요청
    elif is_commit_query and not is_code_inquiry:
        recent_commits = fetch_recent_commits(target_branch, 5)
        context_text = f"[최근 저장소 커밋 내역 ({target_branch})]:\n{recent_commits}"
        matched_path = f"최근 커밋 히스토리 ({target_branch})"

    # 분기 C: C# 코드 및 시스템 스펙 질문
    else:
        matched_path, script_code, all_files = search_relevant_script(query, target_branch)
        
        # [할루시네이션 원천 차단 가드레일]
        if not script_code:
            # 코드나 구현 위치를 묻는 질문인데 리포지토리에 실제 파일이 없는 경우 Groq를 부르지 않고 정중히 거절
            if is_code_inquiry:
                embed = discord.Embed(
                    title="🔍 관련된 스크립트를 찾지 못했습니다",
                    description=(
                        f"현재 `{target_branch}` 브랜치 리포지토리에서 질문과 일치하는 C# 스크립트를 발견하지 못했습니다.\n\n"
                        f"• **확인된 브랜치:** `{target_branch}`\n"
                        f"• 정확한 파일명(예: `NetCargoSpawner.cs`)을 포함하시거나, 올바른 작업 브랜치를 지정해 주세요!"
                    ),
                    color=discord.Color.orange()
                )
                embed.set_footer(text=f"GitHub RAG Guardrail | {target_branch} 브랜치 전수 검사 완료")
                await interaction.followup.send(embed=embed)
                return
            else:
                # 코드 질문이 아닌 일반 대화 질문인 경우
                context_text = "일반 질문 모드 (코드 참조 없음)"
        else:
            context_text = f"[대상 브랜치]: {target_branch}\n[참조 파일 경로]: {matched_path}\n\n[C# 소스 코드]:\n{script_code[:4500]}"

    # Groq 추론 프롬프트 구성
    system_instruction = f"""
    당신은 편의점 게임 개발팀의 전문 테크니컬 리드 AI 어시스턴트 채선우입니다.

    [답변 기본 원칙]:
    1. 비개발 직군(기획/아트)이 한눈에 파악할 수 있도록 핵심만 간결하게 설명하세요.
    2. 마크다운 표(|---|)는 디스코드에서 깨지므로 **절대 사용하지 마세요.**
    3. 코드를 분석할 때는 모든 변수나 함수를 나열하지 말고, 반드시 아래 3개 항목 템플릿에 맞춰 각각 2~3줄 이내로 완결성 있게 작성하세요.
    4. 문장이 중간에 잘리지 않도록 1024 토큰 분량 내에서 반드시 마지막 문장까지 온전히 마무리하세요.

    [C# 코드 분석 답변 템플릿]:
    • **핵심 역할**: (이 스크립트가 인게임에서 수행하는 목적 1~2줄 요약)
    • **주요 로직 및 동기화**: (호스트/서버 동작, 스폰 주기, 핵심 함수 동작 흐름을 2~3줄 요약)
    • **기획/아트 체크포인트**: (인스펙터 연결 데이터, SO 참조, 수치 수정 시 주의사항)

    [GitHub 컨텍스트]:
    {context_text}
    """

    answer, used_model, is_fallback = query_groq(query, system_instruction)
    fallback_notice = f"⚠️ [안내] 기본 모델 한도 초과로 대체 모델(`{used_model}`)이 사용되었습니다.\n\n" if is_fallback else ""

    # 디스코드 임베드 카드 생성
    embed = discord.Embed(
        title="💬 AI 채선우의 답변",
        description=fallback_notice + answer,
        color=discord.Color.gold() if is_fallback else discord.Color.blue()
    )
    embed.add_field(name="질문", value=f"`{query}`", inline=False)
    
    # 🌿 브랜치 및 참조 파일 명시 필드 추가
    if matched_path:
        embed.add_field(
            name="🌿 참조 브랜치 및 소스",
            value=f"`{target_branch}` 브랜치 | `{matched_path}`",
            inline=False
        )

    # 푸터 구성: 모델명 | 브랜치 정보
    footer_text = f"엔진: {used_model} | 브랜치: {target_branch}"
    embed.set_footer(text=footer_text)

    await interaction.followup.send(embed=embed)

@bot.event
async def on_ready():
    await tree.sync()
    print(f"디스코드 봇 로그인 완료: {bot.user.name}")
    print(f"GitHub 동기화 리포지토리: {GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME} (기본: {GITHUB_DEFAULT_BRANCH})")

# --- FastAPI Webhook 엔드포인트 ---
@app.get("/")
def health_check():
    return {"status": "ok", "service": "GitHub Live Fetch & Webhook Discord Bot"}

@app.post("/webhook/github")
async def github_webhook(request: Request):
    event_type = request.headers.get("X-GitHub-Event", "ping")
    if event_type == "ping":
        return {"msg": "pong"}

    payload = await request.json()
    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    if not channel:
        return {"status": "error", "message": "Channel not found"}

    if event_type == "push":
        commits = payload.get("commits", [])
        if not commits:
            return {"status": "ignored", "reason": "No commits"}

        ref = payload.get("ref", "")
        branch = ref.replace("refs/heads/", "")
        pusher = payload.get("pusher", {}).get("name", "Unknown")
        repo_name = payload.get("repository", {}).get("name", "Repo")
        compare_url = payload.get("compare", "")

        commit_lines = [f"• [`{c.get('id', '')[:7]}`] {c.get('message', '').strip().split(chr(10))[0]}" for c in commits[:5]]
        commits_text = "\n".join(commit_lines)

        prompt = f"""
        [저장소]: {repo_name} | [브랜치]: {branch} | [작업자]: {pusher}
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
        [PR 제목]: {title} | [작업 브랜치]: {head_branch} -> {base_branch} | [작업자]: {user}
        [내용]: {body}
        위 PR 내용을 분석하여 기획/아트 팀원이 바로 이해할 수 있도록 어떤 기능이 변경되었는지 3줄 이내로 핵심 요약해줘.
        """
        system_instruction = "PR의 개발 의도와 변경 사항을 비개발 직군도 한눈에 알기 쉽게 1, 2, 3 번호 매김 형식으로 핵심 요약 3줄만 작성하세요. 마크다운 표는 쓰지 마세요."
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