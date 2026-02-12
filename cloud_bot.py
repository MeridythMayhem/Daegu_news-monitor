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

# [수정됨] 절대적 기준 로직: "기업" 관련성이 확인되어야만 100점 부여
def check_critical_patterns(title):
    title = title.replace(" ", "") # 띄어쓰기 무시
    
    # [0] 방어 로직 (Safe Guard)
    safe_guard_keywords = [
        "예방", "방지", "점검", "훈련", "모의", "감지", "대책", 
        "설명회", "참관", "캠페인", "전통시장", "활력", "MOU", "협약",
        "임시주택", "요금", "폭탄", "논란", "지원", "성금", "기탁", 
        "복구", "위로", "격려", "봉사", "전달", "나눔"
    ]
    if any(safe in title for safe in safe_guard_keywords):
        return 0, ""

    # [1] 대구/경북 기업 재난 (3단 조건: 지역 + 재난 + 기업)
    # 수정: 단순히 화재가 났다고 잡는 게 아니라, '공장/기업/산단' 등 기업 키워드가 있어야 함
    loc_condition = any(loc in title for loc in ["대구", "경북", "구미", "포항"])
    
    disaster_condition = any(d in title for d in ["화재", "폭발", "사망", "숨져", "숨진", "붕괴", "산불", "중상", "중대재해"])
    
    # [NEW] 기업 특정 키워드 (이게 없으면 일반 재난으로 취급하여 0점)
    target_condition = any(t in title for t in ["공장", "기업", "업체", "산단", "공단", "사업장", "노동자", "근로자", "법인", "대표", "사옥", "본사"])

    if loc_condition and disaster_condition and target_condition:
        return 100, "지역 기업 내 재난/사고 발생 (강제필터)"

    # [2] 국세청/세무서 + 강력 이슈 (여기는 기관 자체가 특정이므로 유지)
    if any(agency in title for agency in ["국세청", "세무서", "국세공무원"]) and \
       any(issue in title for issue in ["자살", "압수수색", "구속", "횡령", "비리", "체포", "사망"]):
        return 100, "국세청 핵심 리스크 (강제필터)"

    # [3] 경찰/검찰 + 인사
    if any(agency in title for agency in ["경찰", "검찰", "지검", "지청"]) and \
       any(insa in title for insa in ["인사", "전보", "발령", "승진", "프로필", "내정"]):
        return 100, "경검 인사 주요뉴스 (강제필터)"
        
    return 0, ""

# [파이썬 기본 점수]
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
    sorted_logs = sorted(logs, key=lambda x: x.get('score', 0), reverse=True)
    high_risks = [l for l in sorted_logs if l.get('score', 0) >= 80 and l['status'] == 'ALERT']
    
    if high_risks:
        title = f"🚨 정기 보고 (주요 뉴스 {len(high_risks)}건)"
        description = "설정하신 **절대 기준(기업 재난, 자살, 인사)**에 부합하는 기사가 있습니다.\n\n"
        for log in high_risks:
            description += f"🔥 **[{log['score']}점]** {log['title']}\n└ {log['reason']}\n"
        color = 0xe74c3c
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
# [4] 분석 로직
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
        response = requests.get(url, headers=headers, timeout=5)
        soup = BeautifulSoup(response.text, 'html.parser')
        content = soup.select_one('#dic_area') or soup.select_one('#articeBody') or soup.select_one('.go_trans._article_content')
        return content.get_text(strip=True)[:1000] if content else None
    except: return None

def analyze_with_ai(title, content, forced_score):
    if not model: return None
    
    context_hint = ""
    if forced_score == 100:
        context_hint = "※ 중요: 이 기사는 '기업 재난/공무원 비리/인사' 관련 핵심 키워드가 있어 무조건 중요 기사임."

    prompt = f"""
    [분석 요청]
    기사: {title}
    본문: {content[:600]}
    {context_hint}

    다음 기준에 따라 점수(0~100)를 엄격하게 매기시오.

    [🚨 100점 기준 (반드시 기업/기관 관련)]
    1. 대구/경북 지역의 '공장, 기업, 산단, 업체'에서 발생한 화재, 폭발, 사망사고
    2. 국세청/세무서 내부 비리, 자살, 압수수색, 구속
    3. 경찰/검찰 조직의 '인사', '전보', '승진' 명단

    [⚠️ 0점 처리 기준]
    1. 기업과 무관한 일반 가정집 화재, 단순 산불, 교통사고
    2. 단순 사건 수사 진행 (송치, 불송치, 제동, 구형, 선고)
    3. 정책 홍보, 지원금, 캠페인, 행사

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

    if not model: 
        print("API 키 오류")
        return

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

            # [1] 파이썬 강제 필터
            forced_score, forced_reason = check_critical_patterns(title)
            
            log_entry = {
                "title": title, "link": link, "status": "PASS",
                "score": forced_score, 
                "category": "일반", "reason": forced_reason
            }

            # [2] AI 정밀 분석 조건
            suspicious_keywords = ["부도", "해고", "재판", "선고", "의혹", "논란", "위기", "제동", "송치"]
            needs_ai_check = forced_score == 100 or any(k in title for k in suspicious_keywords)

            if needs_ai_check:
                print(f"🔍 AI 분석 진행: {title}")
                content = scrape_article(link)
                if content:
                    result = analyze_with_ai(title, content, forced_score)
                    if result:
                        ai_score = result.get('score', 0)
                        
                        final_score = max(forced_score, ai_score)
                        
                        log_entry['score'] = final_score
                        log_entry['category'] = result.get('category', '미분류')
                        log_entry['reason'] = result.get('reason', '')
                        
                        if forced_score == 100:
                            log_entry['reason'] = f"[자동탐지] {forced_reason} / " + log_entry['reason']

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
                log_entry['score'] = calculate_basic_score(title)

            execution_logs.append(log_entry)
            time.sleep(1)

    send_hourly_report(execution_logs)
    print("✅ 완료")

if __name__ == "__main__":
    main()
