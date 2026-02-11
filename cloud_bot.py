import requests
from bs4 import BeautifulSoup
import time
import json
import os
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold

# =========================================================
# [1] 환경변수 및 설정
# =========================================================
NAVER_CLIENT_ID = os.environ.get("NAVER_ID")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_SECRET")
GOOGLE_API_KEY = os.environ.get("GOOGLE_KEY")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_URL")

# 감시할 키워드 목록
KEYWORDS = ["대구", "경북", "경상북도", "국세청"]
# 이미 처리한 기사 링크를 저장할 파일명
DB_FILE = "processed_links.txt"

# =========================================================
# [2] AI 모델 연결 (무료/유료 자동 선택)
# =========================================================
def get_available_model():
    if not GOOGLE_API_KEY:
        print("❌ API 키가 없습니다.")
        return None
    
    genai.configure(api_key=GOOGLE_API_KEY)
    try:
        # 1.5-flash 모델이 가장 빠르고 무료 할당량이 넉넉함
        return genai.GenerativeModel('gemini-1.5-flash')
    except:
        # 실패 시 구형 pro 모델 시도
        return genai.GenerativeModel('gemini-pro')

model = get_available_model()

# =========================================================
# [3] 파일 저장 및 디스코드 알림 함수
# =========================================================
def load_processed_links():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f:
            return set(f.read().splitlines())
    return set()

def save_processed_link(link):
    with open(DB_FILE, "a") as f:
        f.write(link + "\n")

# [즉시 알림] 위험한 기사 발견 시 바로 발송
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

# [정기 보고] 프로그램 종료 전 요약본 발송
def send_hourly_report(logs, duplicate_count):
    total_new_count = len(logs)
    risk_count = sum(1 for log in logs if log['status'] == 'ALERT')
    safe_count = total_new_count - risk_count
    
    # 중복 안내 문구 (중복이 있을 때만 표시)
    dup_msg = f"\n(※ 이전에 처리된 중복 기사 **{duplicate_count}**건은 자동 제외됨)" if duplicate_count > 0 else ""

    # 케이스 1: 새로운 기사가 아예 없을 때
    if total_new_count == 0:
        title = "💤 새로운 뉴스 없음"
        description = f"지난 1시간 동안 새로운 기사가 없습니다.{dup_msg}"
        color = 0x95a5a6 # 회색
    
    # 케이스 2: 새로운 기사는 있는데 위험한 건 없을 때
    elif risk_count == 0:
        title = f"🟢 정기 보고 (특이사항 없음)"
        description = f"새로운 뉴스 **{safe_count}**건이 감지되었으나, 리스크는 없습니다.{dup_msg}\n\n**[주요 뉴스 헤드라인]**\n"
        
        # 안전한 기사 제목 최대 10개 나열
        for i, log in enumerate(logs[:10]):
            safe_title = log['title'][:40] + ".." if len(log['title']) > 40 else log['title']
            description += f"• {safe_title}\n"
            
        if len(logs) > 10:
            description += f"\n외 {len(logs)-10}건..."
            
        color = 0x2ecc71 # 초록색

    # 케이스 3: 위험한 기사가 있었을 때
    else:
        title = f"🚨 정기 보고 (리스크 {risk_count}건 감지)"
        description = f"새로운 뉴스 **{total_new_count}**건 중 **{risk_count}**건의 이슈가 처리되었습니다.{dup_msg}\n\n**[일반 뉴스 요약]**\n"
        
        safe_logs = [l for l in logs if l['status'] == 'PASS']
        for log in safe_logs[:5]:
            description += f"• {log['title']}\n"
            
        color = 0xe74c3c # 붉은색

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
        # 1시간 10분 전 기사까지 허용 (검색 누락 방지용 여유분)
        diff = now - news_date
        return diff <= timedelta(minutes=70)
    except:
        return False

# =========================================================
# [4] 뉴스 수집 및 AI 분석 로직
# =========================================================

# [중요] API 절약을 위한 1차 필터링 (제목에 위험 단어가 없으면 AI 사용 안 함)
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
    params = {"query": keyword, "display": 20, "sort": "date"}
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
    
    # 1. 제목 필터링: 위험 단어 없으면 AI 분석 생략 (비용 절약)
    if not is_suspicious_title(title):
        return None 

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
    
    # 2. 에러 발생 시 재시도 (429 Quota Exceeded 방지)
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = model.generate_content(prompt, generation_config={"response_mime_type": "application/json"})
            return json.loads(response.text)
        except Exception as e:
            if "429" in str(e):
                time.sleep(60) # 1분 대기 후 재시도
                continue
            return None
    return None

def main():
    print("☁️ 감시 봇 작동 시작...")
    processed_links = load_processed_links()
    execution_logs = []  # 이번 실행에서 처리한 뉴스 목록
    duplicate_count = 0  # 중복 기사 카운트 변수
    
    if not model:
        print("🛑 모델 에러: 프로그램을 종료합니다.")
        return

    for keyword in KEYWORDS:
        articles = search_naver_news(keyword)
        
        for art in articles:
            title = art['title'].replace('<b>','').replace('</b>','').replace('&quot;','"')
            link = art['link']
            
            # [중복 체크] 이미 본 뉴스면 숫자만 세고 넘어감
            if link in processed_links:
                duplicate_count += 1
                continue

            # [날짜/도메인 체크] 너무 옛날 기사거나 네이버 뉴스 아니면 패스
            if not is_recent_news(art['pubDate']) or "news.naver.com" not in link:
                continue 

            print(f"🔍 확인 중: {title}")
            
            # 로그 기록용 기본 데이터
            log_entry = {
                "title": title,
                "status": "PASS",
                "category": "일반",
                "reason": "특이사항 없음"
            }

            content = scrape_article(link)
            
            if content:
                # AI 분석 수행 (내부에서 제목 필터링 거침)
                result = analyze_with_ai(title, content)
                
                if result:
                    # AI가 분석 결과(위험/안전)를 내놓았을 때
                    if result.get('is_risk'):
                        log_entry['status'] = "ALERT"
                        log_entry['category'] = result.get('category')
                        log_entry['reason'] = result.get('reason')
                        
                        print(f"🚨 이슈 발견: {title}")
                        send_alert_discord(title, "주요 이슈 감지", result['reason'], link, result['category'])
                        time.sleep(3) # 전송 후 잠시 대기
                    else:
                        log_entry['reason'] = "AI 정밀 분석 결과 안전함"
                
                # 결과 저장 (위험하든 안전하든 기록)
                execution_logs.append(log_entry)
                save_processed_link(link)
            
            time.sleep(1) # 크롤링 매너 (너무 빠르게 요청하지 않음)

    # 모든 처리가 끝나면 요약 보고서 전송
    send_hourly_report(execution_logs, duplicate_count)
    print(f"✅ 실행 완료 (신규: {len(execution_logs)}건, 중복제외: {duplicate_count}건)")

if __name__ == "__main__":
    main()
