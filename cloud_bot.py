import requests
from bs4 import BeautifulSoup
import time
import json
import os
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold

# ---------------------------------------------------------
# 1. 환경변수 로드
# ---------------------------------------------------------
NAVER_CLIENT_ID = os.environ.get("NAVER_ID")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_SECRET")
GOOGLE_API_KEY = os.environ.get("GOOGLE_KEY")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_URL")

# [변경] 국세청 키워드 추가
KEYWORDS = ["대구", "경북", "경상북도", "국세청"]
DB_FILE = "processed_links.txt"

# ---------------------------------------------------------
# 2. 모델 설정 함수
# ---------------------------------------------------------
def get_available_model():
    if not GOOGLE_API_KEY:
        print("❌ API 키가 없습니다.")
        return None
    
    genai.configure(api_key=GOOGLE_API_KEY)
    try:
        return genai.GenerativeModel('gemini-1.5-flash')
    except:
        return genai.GenerativeModel('gemini-pro')

model = get_available_model()

# ---------------------------------------------------------
# 3. 유틸리티 및 디스코드 알림 함수
# ---------------------------------------------------------
def load_processed_links():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f:
            return set(f.read().splitlines())
    return set()

def save_processed_link(link):
    with open(DB_FILE, "a") as f:
        f.write(link + "\n")

# [즉시 알림] 위험한 기사가 발견되면 바로 전송
def send_alert_discord(title, summary, reason, link, category):
    try:
        data = {
            "username": "리스크 감시 봇",
            "embeds": [{
                "title": f"🚨 [{category}] 긴급 이슈 감지",
                "description": f"**{title}**",
                "color": 0xFF0000, # 빨간색
                "fields": [
                    {"name": "📝 요약", "value": summary, "inline": False},
                    {"name": "💡 판단 근거", "value": reason, "inline": False},
                    {"name": "🔗 링크", "value": f"[기사 원문 보기]({link})", "inline": False}
                ],
                "footer": {"text": "Urgent Alert System"}
            }]
        }
        requests.post(DISCORD_WEBHOOK_URL, json=data)
    except:
        pass

# [정기 보고] 1시간 동안 수집된 뉴스 요약 전송 (수정됨)
def send_hourly_report(logs):
    # logs 리스트에는 이번 실행에서 처리한 모든 기사(위험+일반)가 들어있음
    total_count = len(logs)
    risk_count = sum(1 for log in logs if log['status'] == 'ALERT')
    safe_count = total_count - risk_count
    
    # 1. 기사가 아예 없을 때
    if total_count == 0:
        title = "💤 뉴스 없음"
        description = "지난 1시간 동안 '대구/경북/국세청' 관련 새로운 기사가 없습니다."
        color = 0x95a5a6 # 회색
    
    # 2. 기사는 있는데 리스크는 없을 때 (생존 신고)
    elif risk_count == 0:
        title = f"🟢 정기 보고 (특이사항 없음)"
        description = f"총 **{safe_count}**건의 관련 뉴스가 감지되었으나, 설정된 리스크는 발견되지 않았습니다.\n\n**[주요 일반 뉴스 헤드라인]**\n"
        
        # 기사 제목 최대 10개까지만 리스트업
        for i, log in enumerate(logs[:10]):
            safe_title = log['title'][:40] + ".." if len(log['title']) > 40 else log['title']
            description += f"• {safe_title}\n"
            
        if len(logs) > 10:
            description += f"\n외 {len(logs)-10}건..."
            
        color = 0x2ecc71 # 초록색

    # 3. 리스크가 있었을 때 (이미 알림은 갔지만 요약 보고)
    else:
        title = f"🚨 정기 보고 (리스크 {risk_count}건 감지)"
        description = f"총 **{total_count}**건 중 **{risk_count}**건의 중요 이슈가 처리되었습니다.\n(상세 내용은 이전 알림을 확인하세요.)\n\n**[일반 뉴스 요약]**\n"
        
        safe_logs = [l for l in logs if l['status'] == 'PASS']
        for log in safe_logs[:5]:
            description += f"• {log['title']}\n"
            
        color = 0xe74c3c # 빨간색 섞인 주황

    try:
        data = {
            "username": "뉴스 모니터링 요약",
            "embeds": [{
                "title": title,
                "description": description,
                "color": color,
                "footer": {"text": f"Reported at {datetime.now().strftime('%H:%M')} • Hourly Check"}
            }]
        }
        requests.post(DISCORD_WEBHOOK_URL, json=data)
    except:
        pass

def is_recent_news(pubDate_str):
    try:
        news_date = parsedate_to_datetime(pubDate_str)
        now = datetime.now(news_date.tzinfo)
        # 1시간 10분 전 기사까지 여유있게 허용 (누락 방지)
        diff = now - news_date
        return diff <= timedelta(minutes=70)
    except:
        return False

# ---------------------------------------------------------
# 4. 분석 로직
# ---------------------------------------------------------
def is_suspicious_title(title):
    risk_keywords = [
        "화재", "폭발", "붕괴", "사망", "숨진", "변사", "추락", 
        "구속", "체포", "입건", "송치", "압수수색", "비리", "횡령", 
        "부도", "파산", "해고", "검찰", "경찰", "수사", "법원", "징역",
        "산재", "중대재해", "폭로", "의혹", "논란", "위기", "세무조사", "탈세"
    ]
    return any(keyword in title for keyword in risk_keywords)

def search_naver_news(keyword):
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {"X-Naver-Client-Id": NAVER_CLIENT_ID, "X-Naver-Client-Secret": NAVER_CLIENT_SECRET}
    params = {"query": keyword, "display": 20, "sort": "date"} # display 20으로 증가
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
    
    if not is_suspicious_title(title):
        return None # 안전한 기사로 분류 (None 리턴)

    prompt = f"""
    기사 제목: {title}
    기사 본문: {content[:800]}

    [분석 목표]
    대구·경북 지역의 '기업 사건사고', '경·검찰 인사', '국세청 이슈'를 분류하라.

    [판단 기준: is_risk = true]
    1. 지역: 대구, 경북 관련 (국세청 키워드는 지역 무관하게 체크 가능하면 체크)
    2. 주제: 화재, 사망, 구속, 비리, 세무조사, 횡령, 부도, 경찰/검찰 인사 등 부정적 이슈.

    JSON 포맷 응답:
    {{ "is_risk": true/false, "category": "", "reason": "" }}
    """
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = model.generate_content(prompt, generation_config={"response_mime_type": "application/json"})
            return json.loads(response.text)
        except Exception as e:
            if "429" in str(e):
                time.sleep(60)
                continue
            return None
    return None

def main():
    print("☁️ 감시 봇 작동 시작...")
    processed_links = load_processed_links()
    execution_logs = [] # 이번 실행에서 발견된 모든 뉴스 기록용
    
    if not model:
        print("🛑 모델 에러")
        return

    for keyword in KEYWORDS:
        articles = search_naver_news(keyword)
        
        for art in articles:
            title = art['title'].replace('<b>','').replace('</b>','').replace('&quot;','"')
            link = art['link']
            
            if link in processed_links or not is_recent_news(art['pubDate']) or "news.naver.com" not in link:
                continue 

            print(f"🔍 확인 중: {title}")
            
            # 1. 일단 로그에 기록할 기본 정보 생성
            log_entry = {
                "title": title,
                "status": "PASS", # 기본값은 안전(PASS)
                "category": "일반",
                "reason": "특이사항 없음"
            }

            content = scrape_article(link)
            
            if content:
                # 2. AI 분석 시도 (제목이 위험해 보일 때만)
                result = analyze_with_ai(title, content)
                
                if result:
                    # AI가 분석한 결과가 있으면 업데이트
                    if result.get('is_risk'):
                        log_entry['status'] = "ALERT"
                        log_entry['category'] = result.get('category')
                        log_entry['reason'] = result.get('reason')
                        
                        # 위험하면 즉시 디스코드 전송
                        print(f"🚨 이슈 발견: {title}")
                        send_alert_discord(title, "주요 이슈 감지", result['reason'], link, result['category'])
                        time.sleep(3) # 전송 후 대기
                    else:
                        # AI가 분석했는데 안전하다고 한 경우
                        log_entry['reason'] = "AI 정밀 분석 결과 안전함"

                # 3. 로그 리스트에 추가 (위험하든 안전하든 모두 기록)
                execution_logs.append(log_entry)
                save_processed_link(link)
            
            time.sleep(1)

    # 모든 검색이 끝나면 마지막에 한 번 정기 보고 발송
    send_hourly_report(execution_logs)
    print("✅ 실행 완료 및 보고 전송됨")

if __name__ == "__main__":
    main()
