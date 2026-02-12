import requests
from bs4 import BeautifulSoup
import time
import json
import os
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
import google.generativeai as genai
from difflib import SequenceMatcher

# =========================================================
# [1] 환경변수 및 설정
# =========================================================
NAVER_CLIENT_ID = os.environ.get("NAVER_ID")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_SECRET")
GOOGLE_API_KEY = os.environ.get("GOOGLE_KEY")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_URL")

# 키워드 설정
KEYWORDS = ["대구", "경북", "경상북도", "국세청", "경찰청", "검찰청"]

# =========================================================
# [2] AI 모델 연결
# =========================================================
def get_available_model():
    if not GOOGLE_API_KEY: return None
    genai.configure(api_key=GOOGLE_API_KEY)
    try:
        return genai.GenerativeModel('gemini-1.5-flash')
    except:
        return genai.GenerativeModel('gemini-pro')

model = get_available_model()

# =========================================================
# [3] 유틸리티 및 점수 계산 로직
# =========================================================
def get_similarity(a, b):
    return SequenceMatcher(None, a, b).ratio()

# [파이썬 강제 필터] 사용자 지정 절대 기준 (이 조건 맞으면 무조건 100점)
def check_critical_patterns(title):
    title = title.replace(" ", "") # 띄어쓰기 무시
    
    # 1. 대구/경북 + 재난/사고
    if any(loc in title for loc in ["대구", "경북", "구미", "포항"]) and \
       any(disaster in title for disaster in ["화재", "불", "폭발", "사망", "숨져", "붕괴", "산불"]):
        return 100, "지역 내 재난/사고 발생 (강제필터)"

    # 2. 국세청/세무서 + 강력 이슈
    if any(agency in title for agency in ["국세청", "세무서", "국세공무원"]) and \
       any(issue in title for issue in ["자살", "압수수색", "구속", "횡령", "비리", "체포", "사망"]):
        return 100, "국세청 핵심 리스크 (강제필터)"

    # 3. 경찰/검찰 + 인사 (단순 수사 기사가 아님)
    if any(agency in title for agency in ["경찰", "검찰", "지검", "지청"]) and \
       any(insa in title for insa in ["인사", "전보", "발령", "승진", "프로필", "내정"]):
        return 100, "경검 인사 주요뉴스 (강제필터)"
        
    return 0, ""

# [파이썬 기본 점수] AI 미사용 시 순위 산정용
def calculate_basic_score(title):
    score = 0
    if any(k in title for k in ["대구", "경북", "국세청", "경찰", "검찰"]): score += 10
    if any(k in title for k in ["사망", "구속", "횡령", "화재", "인사"]): score += 20
    return score

# [디스코드] 즉시 알림 전송
def send_alert_discord(title, summary, reason, link, category, score):
    color = 0xFF0000 if score >= 80 else 0xFFA500
    try:
        data = {
            "username": "리스크 감시 봇",
            "embeds": [{
                "title": f"🚨 [심각도: {score}점] {category}",
                "description": f"**{title}**",
                "color": color, 
                "fields": [
                    {"name": "💡 감지 사유", "value": reason, "inline": False},
                    {"name": "🔗 바로가기", "value": f"[기사 원문]({link})", "inline": True}
                ],
                "footer": {"text": "Critical News Alert"}
            }]
        }
        requests.post(DISCORD_WEBHOOK_URL, json=data)
    except: pass

# [디스코드] 정기 보고 전송
def send_hourly_report(logs):
    # 점수 높은 순 정렬
    sorted_logs = sorted(logs, key=lambda x: x.get('score', 0), reverse=True)
    high_risks = [l for l in sorted_logs if l.get('score', 0) >= 80 and l['status'] == 'ALERT']
    
    # 80점 이상인 '새로운' 리스크가 있을 때만 빨간 보고서
    if high_risks:
        title = f"🚨 정기 보고 (주요 뉴스 {len(high_risks)}건)"
        description = "설정하신 **절대 기준(화재, 자살, 인사 등)**에 부합하는 기사가 있습니다.\n\n"
        for log in high_risks:
            description += f"🔥 **[{log['score']}점]** {log['title']}\n└ {log['reason']}\n"
        color = 0xe74c3c
    # 없으면 초록 보고서 (Top 7)
    else:
        title = "🟢 정기 보고 (특이사항 없음)"
        top_7 = sorted_logs[:7]
        if not top_7: description = "새로운 뉴스가 없습니다."
        else:
            description = "주요 리스크는 없습니다. 현재 가장 관련성 높은 기사 7건입니다.\n\n"
            for i, log in enumerate(top_7, 1):
                short = log['title'][:35] + "..." if len(log['title']) > 35 else log['title']
                description += f"**{i}.** [{short}]({log['link']}) `Score: {log['score']}`\n"
        color = 0x2ecc71

    try:
        data = {
            "username": "뉴스 모니터링 요약",
            "embeds": [{
                "title": title, "description": description, "color": color,
                "footer": {"text": f"{datetime.now().strftime('%H:%M')} 기준"}
            }]
        }
        requests.post(DISCORD_WEBHOOK_URL, json=data)
    except: pass

# =========================================================
# [4] 분석 로직 (핵심 수정됨)
# =========================================================
def search_naver_news(keyword):
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {"X-Naver-Client-Id": NAVER_CLIENT_ID, "X-Naver-Client-Secret": NAVER_CLIENT_SECRET}
    params = {"query": keyword, "display": 30, "sort": "date"}
    try:
        return requests.get(url, headers=headers, params=params).json().get('items', [])
    except: return []

def scrape_article(url):
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=3)
        soup = BeautifulSoup(response.text, 'html.parser')
        content = soup.select_one('#dic_area') or soup.select_one('#articeBody')
        return content.get_text(strip=True)[:1000] if content else None
    except: return None

def analyze_with_ai(title, content, forced_score):
    if not model: return None
    
    # 강제 점수가 있으면 AI에게 힌트 제공
    context_hint = ""
    if forced_score == 100:
        context_hint = "※ 중요: 이 기사는 화재/자살/인사 등 핵심 키워드가 있어 무조건 중요 기사임."

    # [수정] 프롬프트에 '감점 기준(Negative Prompt)'을 명확히 추가
    prompt = f"""
    [분석 요청]
    기사: {title}
    본문: {content[:600]}
    {context_hint}

    다음 기준에 따라 점수(0~100)를 매우 엄격하게 매기시오.

    [🚨 100점 기준 (긴급/중요)]
    1. 대구/경북 지역 공장 화재, 폭발, 사망사고, 붕괴
    2. 국세청/세무서 내부 비리, 자살, 압수수색, 구속
    3. 경찰/검찰 조직의 '인사', '전보', '승진', '발령' 명단 (사람 이름 포함)

    [⚠️ 30점 이하 기준 (일반 뉴스 - 절대 고득점 금지)]
    1. 단순 사건 수사 뉴스: 송치, 불송치, 구형, 선고, 제동, 수사 착수, 고발 등
    2. 검찰/경찰이 주어가 되더라도 '인사'가 아닌 '수사' 내용은 점수를 낮게 줄 것.
    3. 단순 정책 홍보, 캠페인, MOU, 행사 개최

    JSON 포맷 응답: {{ "score": 점수, "category": "카테고리", "reason": "이유 한 줄 요약" }}
    """
    try:
        res = model.generate_content(prompt, generation_config={"response_mime_type": "application/json"})
        return json.loads(res.text)
    except: return None

def main():
    print("☁️ 봇 작동 시작...")
    execution_logs = []  
    processed_urls = set()
    recent_risk_titles = [] 
    time_threshold = datetime.now() - timedelta(minutes=70)

    if not model: return

    for keyword in KEYWORDS:
        articles = search_naver_news(keyword)
        for art in articles:
            title = art['title'].replace('<b>','').replace('</b>','').replace('&quot;','"')
            link = art['link']
            
            if link in processed_urls: continue
            processed_urls.add(link)
            
            try:
                if parsedate_to_datetime(art['pubDate']).replace(tzinfo=None) < time_threshold: continue
            except: continue

            # [1] 파이썬 강제 필터 (여기서 100점 받으면 AI가 깎아도 최종 100점 유지됨)
            forced_score, forced_reason = check_critical_patterns(title)
            
            log_entry = {
                "title": title, "link": link, "status": "PASS",
                "score": forced_score, 
                "category": "일반", "reason": forced_reason
            }

            # [2] AI 정밀 분석 조건
            # 강제 점수가 100점이거나, 다른 의심 키워드("부도", "논란" 등)가 있을 때만 AI 호출
            suspicious_keywords = ["부도", "해고", "재판", "선고", "의혹", "논란", "위기", "제동", "송치"]
            needs_ai_check = forced_score == 100 or any(k in title for k in suspicious_keywords)

            if needs_ai_check:
                print(f"🔍 AI 분석 진행: {title}")
                content = scrape_article(link)
                if content:
                    result = analyze_with_ai(title, content, forced_score)
                    if result:
                        ai_score = result.get('score', 0)
                        
                        # [핵심] 파이썬 점수 vs AI 점수 중 높은 것 채택
                        final_score = max(forced_score, ai_score)
                        
                        log_entry['score'] = final_score
                        log_entry['category'] = result.get('category', '미분류')
                        log_entry['reason'] = result.get('reason', '')
                        
                        if forced_score == 100:
                            log_entry['reason'] = f"[자동탐지] {forced_reason} / " + log_entry['reason']

                        # 80점 이상일 때 처리
                        if final_score >= 80:
                            is_dup = False
                            for past in recent_risk_titles:
                                if get_similarity(title, past) > 0.6: is_dup = True
                            
                            if not is_dup:
                                log_entry['status'] = "ALERT"
                                recent_risk_titles.append(title)
                                print(f"🚨 중요 기사 감지: {title}")
                                send_alert_discord(title, "주요 뉴스", log_entry['reason'], link, log_entry['category'], final_score)
                            else:
                                log_entry['status'] = "DUPLICATE"
            else:
                # AI 안 거치는 기사도 기본 점수 부여
                log_entry['score'] = calculate_basic_score(title)

            execution_logs.append(log_entry)
            time.sleep(1)

    send_hourly_report(execution_logs)
    print("✅ 완료")

if __name__ == "__main__":
    main()
