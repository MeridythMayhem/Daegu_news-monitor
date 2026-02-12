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

# 검색 키워드
KEYWORDS = ["대구", "경북", "경상북도", "국세청"]

# =========================================================
# [2] AI 모델 연결
# =========================================================
def get_available_model():
    if not GOOGLE_API_KEY:
        return None
    genai.configure(api_key=GOOGLE_API_KEY)
    try:
        # 속도와 비용 효율을 위해 1.5-flash 우선 사용
        return genai.GenerativeModel('gemini-1.5-flash')
    except:
        return genai.GenerativeModel('gemini-pro')

model = get_available_model()

# =========================================================
# [3] 유틸리티 (점수 계산 및 유사도)
# =========================================================
def get_similarity(a, b):
    return SequenceMatcher(None, a, b).ratio()

# [NEW] 파이썬 자체 가중치 점수 계산 (AI 미사용 시 순위 산정용)
def calculate_basic_score(title):
    score = 0
    # 1. 지역/기관 점수
    if any(k in title for k in ["대구", "경북", "국세청", "경찰", "검찰"]):
        score += 10
    
    # 2. 부정 이슈 점수 (AI 필터 전 1차 점수)
    risk_words = ["사망", "구속", "횡령", "화재", "압수수색", "비리", "적발", "인사", "전보"]
    for word in risk_words:
        if word in title:
            score += 20
            
    # 3. 감점 요소 (홍보, 단순 행사)
    safe_words = ["개최", "모집", "행사", "축제", "기부", "협약", "MOU"]
    if any(word in title for word in safe_words):
        score -= 30
        
    return score

# [디스코드] 즉시 알림 (긴급 이슈 - 80점 이상일 때만)
def send_alert_discord(title, summary, reason, link, category, score):
    try:
        data = {
            "username": "리스크 감시 봇",
            "embeds": [{
                "title": f"🚨 [심각도: {score}점] {category} 긴급 이슈",
                "description": f"**{title}**",
                "color": 0xFF0000, 
                "fields": [
                    {"name": "📝 요약", "value": summary, "inline": False},
                    {"name": "💡 판단 근거", "value": reason, "inline": False},
                    {"name": "🔗 링크", "value": f"[기사 원문 보기]({link})", "inline": True}
                ],
                "footer": {"text": "Urgent Alert System"}
            }]
        }
        requests.post(DISCORD_WEBHOOK_URL, json=data)
    except:
        pass

# [디스코드] 정기 보고 (로직 변경: 주요 기사 유무에 따라 분기)
def send_hourly_report(logs):
    # 점수 높은 순으로 정렬
    sorted_logs = sorted(logs, key=lambda x: x.get('score', 0), reverse=True)
    
    # 80점 이상인 '진짜 위험' 기사 필터링
    high_risks = [l for l in sorted_logs if l.get('score', 0) >= 80 and l['status'] == 'ALERT']
    
    # [Case 1] 주요 기사가 있는 경우 (기존 방식 유지)
    if high_risks:
        title = f"🚨 정기 보고 (위험 {len(high_risks)}건 감지)"
        description = f"**심각도 80점 이상**의 주요 이슈가 감지되었습니다.\n\n"
        for log in high_risks:
            description += f"🔥 **[{log['score']}점]** {log['title']} ({log['reason']})\n"
        color = 0xe74c3c # 빨강

    # [Case 2] 주요 기사가 없는 경우 (요청하신 Top 7 요약 기능)
    else:
        title = "🟢 정기 보고 (주요 특이사항 없음)"
        # 상위 7개만 추출
        top_7 = sorted_logs[:7]
        
        if not top_7:
            description = "수집된 뉴스가 없습니다."
        else:
            description = "심각한 리스크는 발견되지 않았습니다.\n점수 기반 **상위 7개 일반 뉴스**를 보고합니다.\n\n"
            for i, log in enumerate(top_7, 1):
                # 제목 클릭 시 이동하도록 링크 적용
                short_title = log['title'][:35] + "..." if len(log['title']) > 35 else log['title']
                description += f"**{i}.** [{short_title}]({log['link']}) `Score: {log['score']}`\n"
        color = 0x2ecc71 # 초록

    try:
        data = {
            "username": "뉴스 모니터링 요약",
            "embeds": [{
                "title": title,
                "description": description,
                "color": color,
                "footer": {"text": f"Reported at {datetime.now().strftime('%H:%M')}"}
            }]
        }
        requests.post(DISCORD_WEBHOOK_URL, json=data)
    except:
        pass

# =========================================================
# [4] 분석 로직
# =========================================================

def is_suspicious_title(title):
    # 기존 키워드 필터 유지
    risk_keywords = [
        "화재", "폭발", "붕괴", "사망", "숨진", "변사", "추락", "산재", "중대재해", 
        "구속", "체포", "입건", "송치", "압수수색", "비리", "횡령", "배임", "뇌물", 
        "부도", "파산", "해고", "세무조사", "탈세", 
        "검찰", "경찰", "수사", "감사", "적발", "의혹",
        "인사", "전보", "발령", "승진", "청장", "서장"
    ]
    return any(keyword in title for keyword in risk_keywords)

def search_naver_news(keyword):
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {"X-Naver-Client-Id": NAVER_CLIENT_ID, "X-Naver-Client-Secret": NAVER_CLIENT_SECRET}
    params = {"query": keyword, "display": 30, "sort": "date"}
    try:
        return requests.get(url, headers=headers, params=params).json().get('items', [])
    except:
        return []

def scrape_article(url):
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=5)
        soup = BeautifulSoup(response.text, 'html.parser')
        content = soup.select_one('#dic_area') or soup.select_one('#articeBody') or soup.select_one('.go_trans._article_content')
        return content.get_text(strip=True) if content else None
    except:
        return None

def analyze_with_ai(title, content):
    if not model: return None
    
    # [Prompt 수정] 점수(score) 필드 추가 및 채점 기준 제시
    prompt = f"""
    기사 제목: {title}
    기사 본문(일부): {content[:800]}

    [분석 목표]
    이 기사가 다음 3가지 중 하나에 해당하는지 분석하고, '심각도 점수(0~100)'를 매기시오.
    1. 대구·경북 '기업 사건사고/경제범죄'
    2. 국세청/세무서 '부정 이슈(비리, 감사, 압수수색)'
    3. 경찰/검찰 '인사/승진/전보'

    [점수 기준]
    - 80~100점: 사망, 구속, 횡령, 압수수색, 실제 인사 발령 등 확실한 주요 이슈.
    - 40~79점: 단순 의혹, 점검, 예방 활동, 루머, 예정 사항.
    - 0~39점: 관련 없음, 홍보성 기사, 단순 동정.

    JSON 포맷 응답:
    {{ 
        "is_risk": true/false, 
        "score": 0~100 (int),
        "category": "기업재난 / 국세청 / 경검인사", 
        "reason": "이유 한 줄 요약" 
    }}
    """
    
    try:
        response = model.generate_content(prompt, generation_config={"response_mime_type": "application/json"})
        return json.loads(response.text)
    except:
        return None

def main():
    print("☁️ 감시 봇 작동 시작...")
    execution_logs = []  
    processed_urls = set()
    recent_risk_titles = [] # 중복 방지용

    # 깃허브 액션용 시간 필터 (최근 70분)
    time_threshold = datetime.now() - timedelta(minutes=70)

    if not model:
        print("🛑 모델 에러: API 키 확인 필요")
        return

    for keyword in KEYWORDS:
        articles = search_naver_news(keyword)
        
        for art in articles:
            title = art['title'].replace('<b>','').replace('</b>','').replace('&quot;','"')
            link = art['link']
            
            if link in processed_urls: continue
            processed_urls.add(link)

            try:
                pub_date = parsedate_to_datetime(art['pubDate']).replace(tzinfo=None)
                if pub_date < time_threshold: continue
            except: continue

            # 기본 로그 엔트리 생성
            log_entry = {
                "title": title,
                "link": link,
                "status": "PASS",
                "score": 0, # 초기 점수
                "reason": "일반 기사"
            }

            # [분기점] 의심스러운 제목인가?
            if is_suspicious_title(title):
                print(f"🔍 AI 정밀 분석 중: {title}")
                content = scrape_article(link)
                
                if content:
                    result = analyze_with_ai(title, content)
                    
                    if result:
                        ai_score = result.get('score', 0)
                        log_entry['score'] = ai_score
                        log_entry['category'] = result.get('category', '미분류')
                        log_entry['reason'] = result.get('reason', '')
                        
                        # AI가 리스크라고 판단하고, 점수가 80점 이상일 때만 'ALERT'
                        if result.get('is_risk') and ai_score >= 80:
                            # 중복 체크
                            is_dup = False
                            for past in recent_risk_titles:
                                if get_similarity(title, past) > 0.6: is_dup = True
                            
                            if not is_dup:
                                log_entry['status'] = "ALERT"
                                recent_risk_titles.append(title)
                                print(f"🚨 긴급 이슈(점수 {ai_score}): {title}")
                                send_alert_discord(title, "긴급 이슈", log_entry['reason'], link, log_entry['category'], ai_score)
                            else:
                                log_entry['status'] = "DUPLICATE"
                        else:
                            # 리스크는 맞는데 점수가 낮거나(단순 의혹 등), AI가 아니라고 한 경우
                            log_entry['reason'] = f"(AI 점수 {ai_score}) {log_entry['reason']}"
            else:
                # [NEW] 의심스럽지 않은 기사도 파이썬으로 '기본 점수' 매김 (Top 7 산정용)
                log_entry['score'] = calculate_basic_score(title)

            execution_logs.append(log_entry)
            time.sleep(1)

    # [디스코드] 최종 정기 보고 (조건부 전송)
    send_hourly_report(execution_logs)
    print("✅ 실행 완료")

if __name__ == "__main__":
    main()
