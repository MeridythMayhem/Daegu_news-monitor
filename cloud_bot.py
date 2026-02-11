import requests
from bs4 import BeautifulSoup
import time
import json
import os
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold
from difflib import SequenceMatcher # [추가] 텍스트 유사도 비교 도구

# =========================================================
# [1] 환경변수 및 설정
# =========================================================
NAVER_CLIENT_ID = os.environ.get("NAVER_ID")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_SECRET")
GOOGLE_API_KEY = os.environ.get("GOOGLE_KEY")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_URL")

KEYWORDS = ["대구", "경북", "경상북도", "국세청"]
DB_FILE = "processed_links.txt"

# =========================================================
# [2] AI 모델 연결
# =========================================================
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

# =========================================================
# [3] 유틸리티 (파일 저장, 유사도 검사 등)
# =========================================================
def load_processed_links():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f:
            return set(f.read().splitlines())
    return set()

def save_processed_link(link):
    with open(DB_FILE, "a") as f:
        f.write(link + "\n")

# [신규 기능] 두 문장의 유사도를 0~1 사이 숫자로 반환 (1에 가까울수록 같음)
def get_similarity(a, b):
    return SequenceMatcher(None, a, b).ratio()

# [디스코드] 즉시 알림
def send_alert_discord(title, summary, reason, link, category):
    try:
        data = {
            "username": "리스크 감시 봇",
            "embeds": [{
                "title": f"🚨 [{category}] 긴급 이슈 감지",
                "description": f"**{title}**",
                "color": 0xFF0000, 
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

# [디스코드] 1시간 정기 보고 (내용 중복 카운트 추가)
def send_hourly_report(logs, duplicate_link_count, duplicate_content_count):
    total_scanned = len(logs)
    risk_alerts = [l for l in logs if l['status'] == 'ALERT']
    risk_count = len(risk_alerts)
    
    # 보고서 멘트 조합
    msg_parts = []
    if duplicate_link_count > 0:
        msg_parts.append(f"• 이미 본 링크 제외: **{duplicate_link_count}**건")
    if duplicate_content_count > 0:
        msg_parts.append(f"• 내용 중복(도배) 제외: **{duplicate_content_count}**건")
    
    exclusion_msg = "\n".join(msg_parts)
    if exclusion_msg: exclusion_msg = "\n\n(참고)\n" + exclusion_msg

    # 1. 이슈 없음
    if risk_count == 0:
        title = "🟢 정기 보고 (특이사항 없음)"
        if total_scanned == 0:
            description = f"지난 1시간 동안 새로운 기사가 없습니다.{exclusion_msg}"
        else:
            description = f"새로운 기사 **{total_scanned}**건을 확인했으나 위험 요소는 없습니다.{exclusion_msg}\n\n**[주요 기사]**\n"
            for log in logs[:5]:
                description += f"• {log['title'][:40]}\n"
        color = 0x2ecc71

    # 2. 이슈 있음
    else:
        title = f"🚨 정기 보고 (리스크 {risk_count}건 감지)"
        description = f"총 **{total_scanned}**건 중 **{risk_count}**건의 중요 이슈를 처리했습니다.{exclusion_msg}\n\n**[감지된 이슈]**\n"
        for log in risk_alerts:
            description += f"🔥 {log['title']}\n"
        color = 0xe74c3c

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

def is_recent_news(pubDate_str):
    try:
        news_date = parsedate_to_datetime(pubDate_str)
        now = datetime.now(news_date.tzinfo)
        return diff <= timedelta(minutes=70)
    except:
        return False

# =========================================================
# [4] 분석 로직 (필터 확장 및 유사도 적용)
# =========================================================

# [업그레이드] 키워드 대폭 추가 (구멍 메우기)
def is_suspicious_title(title):
    risk_keywords = [
        # 사고/재난
        "화재", "폭발", "붕괴", "사망", "숨진", "변사", "추락", "산재", "중대재해", "응급", "대피", "고립", "침수",
        # 범죄/수사
        "구속", "체포", "입건", "송치", "압수수색", "비리", "횡령", "배임", "뇌물", "도박", "마약", "성범죄", "폭행", "살인",
        # 경제/기업 위기
        "부도", "파산", "해고", "폐업", "법정관리", "워크아웃", "임금체불", "세무조사", "탈세", "추징",
        # 사법/행정
        "검찰", "경찰", "수사", "법원", "징역", "선고", "재판", "기소", "징계", "감사", "적발", "의혹", "논란", "위기"
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
    if not is_suspicious_title(title): return None 

    prompt = f"""
    기사 제목: {title}
    기사 본문: {content[:800]}

    [분석 목표]
    대구·경북 지역의 '기업 사건사고', '경·검찰 인사/수사', '국세청 이슈'를 분류하라.

    [판단 기준: is_risk = true]
    1. 지역: 대구, 경북 관련 (국세청 키워드는 지역 무관하게 체크 가능하면 체크)
    2. 주제: 부정적 이슈 전반 (사고, 범죄, 수사, 재판, 경제위기, 비리 등)

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
    execution_logs = []  
    
    # 카운팅 변수들
    duplicate_link_count = 0    # 아예 똑같은 링크 (완전 중복)
    duplicate_content_count = 0 # 링크는 다른데 내용은 같은 기사 (도배 방지)
    
    # 이번 실행 주기 동안 발견된 '위험 기사 제목'들을 기억하는 리스트
    recent_risk_titles = []

    if not model:
        print("🛑 모델 에러")
        return

    for keyword in KEYWORDS:
        articles = search_naver_news(keyword)
        
        for art in articles:
            title = art['title'].replace('<b>','').replace('</b>','').replace('&quot;','"')
            link = art['link']
            
            # 1. 완전 중복(링크) 체크
            if link in processed_links:
                duplicate_link_count += 1
                continue

            # 2. 날짜/도메인 체크
            try:
                news_date = parsedate_to_datetime(art['pubDate'])
                if (datetime.now(news_date.tzinfo) - news_date) > timedelta(minutes=70): continue
            except: continue
            if "news.naver.com" not in link: continue 

            print(f"🔍 확인 중: {title}")
            
            log_entry = {
                "title": title,
                "status": "PASS",
                "category": "일반",
                "reason": "특이사항 없음"
            }

            content = scrape_article(link)
            
            if content:
                result = analyze_with_ai(title, content)
                
                if result:
                    if result.get('is_risk'):
                        # 🚨 여기서 [도배 방지] 로직 작동!
                        # 방금 발견한 위험 기사들과 제목이 60% 이상 비슷하면 알림 스킵
                        is_duplicate_content = False
                        for past_title in recent_risk_titles:
                            if get_similarity(title, past_title) > 0.6: # 60% 유사도
                                is_duplicate_content = True
                                break
                        
                        if is_duplicate_content:
                            print(f"🔇 [중복 이슈] 알림 생략: {title}")
                            log_entry['status'] = "DUPLICATE_RISK" # 로그에는 남기되 알림은 안 보냄
                            duplicate_content_count += 1
                        else:
                            # 진짜 새로운 위험 기사
                            log_entry['status'] = "ALERT"
                            log_entry['category'] = result.get('category')
                            log_entry['reason'] = result.get('reason')
                            recent_risk_titles.append(title) # 기억해둠
                            
                            print(f"🚨 이슈 발견: {title}")
                            send_alert_discord(title, "주요 이슈 감지", result['reason'], link, result['category'])
                            time.sleep(3)
                    else:
                        log_entry['reason'] = "안전함"
                
                execution_logs.append(log_entry)
                save_processed_link(link) # 처리는 했으니 저장
            
            time.sleep(1)

    # 보고서 전송
    send_hourly_report(execution_logs, duplicate_link_count, duplicate_content_count)
    print("✅ 실행 완료")

if __name__ == "__main__":
    main()
