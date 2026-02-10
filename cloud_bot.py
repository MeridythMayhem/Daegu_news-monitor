import requests
from bs4 import BeautifulSoup
import time
import json
import os
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold

# 환경변수 로드
NAVER_CLIENT_ID = os.environ.get("NAVER_ID")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_SECRET")
GOOGLE_API_KEY = os.environ.get("GOOGLE_KEY")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_URL")

# [변경 1] 감시 키워드 확장 (대구 + 경북)
KEYWORDS = ["대구", "경북", "경상북도"]

# [추가] 중복 알림 방지를 위한 파일 경로
DB_FILE = "processed_links.txt"

# Gemini 설정
if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)
    model = genai.GenerativeModel('gemini-1.5-flash')

# 파일에서 이미 보낸 기사 링크 불러오기
def load_processed_links():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f:
            return set(f.read().splitlines())
    return set()

# 보낸 기사 링크 저장하기
def save_processed_link(link):
    with open(DB_FILE, "a") as f:
        f.write(link + "\n")

# 1. 이슈 발생 시 보내는 '긴급 알림' (빨간색)
def send_alert_discord(title, summary, reason, link, category):
    try:
        color = 0xFF0000 # 빨간색
        data = {
            "username": "대구·경북 리스크 감시 봇", # 이름 변경
            "embeds": [{
                "title": f"🚨 [{category}] 주요 소식 감지",
                "description": f"**{title}**",
                "color": color,
                "fields": [
                    {"name": "📝 요약", "value": summary, "inline": False},
                    {"name": "💡 판단 근거", "value": reason, "inline": False},
                    {"name": "🔗 링크", "value": f"[기사 원문 보기]({link})", "inline": False}
                ],
                "footer": {"text": "DG Risk Monitor • Urgent Alert"}
            }]
        }
        requests.post(DISCORD_WEBHOOK_URL, json=data)
    except:
        pass

# 2. 활동 보고서 (선택 사항 - 필요 없으면 주석 처리 가능)
def send_status_report(logs):
    if not logs: return
    
    alert_count = sum(1 for log in logs if log['status'] == 'ALERT')
    # 알림이 없으면 보고서를 보내지 않거나, 필요시 주석 해제
    if alert_count == 0: return 

    description = f"🔍 총 **{len(logs)}**건 검토 완료 (🚨이슈: {alert_count}건)\n\n"
    for log in logs[:10]:
        if log['status'] == 'ALERT':
            description += f"🚨 **{log['title'][:20]}..** ({log['category']})\n"
    
    try:
        data = {
            "username": "감시 봇 보고",
            "embeds": [{
                "title": "📋 활동 요약",
                "description": description,
                "color": 0x3498db,
                "footer": {"text": f"Time: {datetime.now().strftime('%H:%M')}"}
            }]
        }
        requests.post(DISCORD_WEBHOOK_URL, json=data)
    except:
        pass

# 시간 체크 (최근 60분 - 범위를 조금 늘림)
def is_recent_news(pubDate_str):
    try:
        news_date = parsedate_to_datetime(pubDate_str)
        now = datetime.now(news_date.tzinfo)
        diff = now - news_date
        return diff <= timedelta(minutes=60) # 30분 주기 실행이므로 여유있게 60분
    except:
        return False

def search_naver_news(keyword):
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {"X-Naver-Client-Id": NAVER_CLIENT_ID, "X-Naver-Client-Secret": NAVER_CLIENT_SECRET}
    params = {"query": keyword, "display": 15, "sort": "date"} # 검색량 약간 증가
    try:
        return requests.get(url, headers=headers, params=params).json().get('items', [])
    except:
        return []

def scrape_article(url):
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=5)
        soup = BeautifulSoup(response.text, 'html.parser')
        content = soup.select_one('#dic_area')
        return content.get_text(strip=True) if content else None
    except:
        return None

# [변경 2] AI 분석 로직 (프롬프트 대폭 수정)
def analyze_with_ai(title, content):
    if not GOOGLE_API_KEY: return None
    
    prompt = f"""
    기사 제목: {title}
    기사 본문: {content[:800]}

    [분석 목표]
    대구·경북 지역의 '기업 사건사고'와 '경·검찰 인사' 소식을 분류하라.

    [판단 기준: is_risk = true 조건]
    1. 필수 지역 조건: 내용이 '대구' 또는 '경북(경상북도)' 관련일 것.
    2. 타겟 주제 (A 또는 B 중 하나):
       A. 기업 및 재난 리스크:
          - 기업/공장 화재, 폭발, 붕괴
          - 공장 작업자 사망, 산재 사고
          - 기업 관련 범죄: 횡령, 배임, 부도, 구속, 비리, 징계, 압수수색, 세무조사
       B. 수사기관 인사 (예외적 허용):
          - **경찰** 또는 **검찰** 관련 인사 소식 (지방청장, 서장, 부장검사 등 승진/전보/발령)
          - *주의: 시청, 구청, 일반 기업의 인사는 false 처리*

    [제외 조건: is_risk = false]
    - 단순 날씨, 축제, 행사, 홍보, 맛집 소개.
    - 정치인의 단순 선거 유세나 동정.
    - 경찰/검찰이 아닌 일반 공무원 인사.

    JSON 포맷 응답:
    {{ "is_risk": true/false, "category": "카테고리(예: 공장화재, 경찰인사, 횡령)", "reason": "판단 이유 요약" }}
    """
    
    try:
        safety = {
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
        }
        
        response = model.generate_content(
            prompt, 
            safety_settings=safety,
            generation_config={"response_mime_type": "application/json"}
        )
        return json.loads(response.text)
    except:
        return None

def main():
    print("☁️ 대구·경북 심층 감시 시작")
    processed_links = load_processed_links() # 기억장치 로드
    execution_logs = []
    
    for keyword in KEYWORDS:
        articles = search_naver_news(keyword)
        
        for art in articles:
            title = art['title'].replace('<b>','').replace('</b>','').replace('&quot;','"')
            link = art['link']
            
            # 이미 처리한 기사거나, 너무 오래되었거나, 네이버 뉴스가 아니면 패스
            if link in processed_links or not is_recent_news(art['pubDate']) or "news.naver.com" not in link:
                continue 

            print(f"분석 중: {title}")
            content = scrape_article(link)
            
            if content:
                result = analyze_with_ai(title, content)
                
                if result:
                    status = "ALERT" if result.get('is_risk') else "PASS"
                    
                    log_entry = {
                        "title": title,
                        "status": status,
                        "category": result.get('category', '기타'),
                        "reason": result.get('reason', '판단불가')
                    }
                    execution_logs.append(log_entry)

                    if status == "ALERT":
                        print(f"🚨 이슈 발견: {title}")
                        send_alert_discord(title, "주요 이슈 감지", result['reason'], link, result['category'])
                    
                    # 처리 완료된 링크 저장
                    save_processed_link(link)
                    time.sleep(1)
            
            time.sleep(1)

    send_status_report(execution_logs)

if __name__ == "__main__":
    main()
