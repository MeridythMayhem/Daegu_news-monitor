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

KEYWORDS = ["대구", "국세청"]

# Gemini 설정
if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)
    model = genai.GenerativeModel('gemini-1.5-flash')

# 디스코드 알림 (예쁜 카드 형태)
def send_discord(title, summary, reason, link, category):
    try:
        # 화재/사망 등은 빨간색, 나머지는 주황색
        color = 0xFF0000 if any(x in category for x in ["화재", "사망", "구속"]) else 0xFFA500
        
        data = {
            "username": "뉴스 감시 봇",
            "embeds": [{
                "title": f"🚨 [{category}] 이슈 감지",
                "description": f"**{title}**",
                "color": color,
                "fields": [
                    {"name": "📝 요약", "value": summary, "inline": False},
                    {"name": "💡 감지 이유", "value": reason, "inline": False},
                    {"name": "🔗 링크", "value": f"[기사 보러가기]({link})", "inline": False}
                ],
                "footer": {"text": "AI News Monitor"}
            }]
        }
        requests.post(DISCORD_WEBHOOK_URL, json=data)
    except:
        pass

# [시간 체크] 30분 간격 실행이므로, 최근 35분 이내 기사만 가져옴 (5분은 여유버퍼)
def is_recent_news(pubDate_str):
    try:
        news_date = parsedate_to_datetime(pubDate_str)
        now = datetime.now(news_date.tzinfo)
        diff = now - news_date
        return diff <= timedelta(minutes=35)
    except:
        return False

# 네이버 뉴스 검색
def search_naver_news(keyword):
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {"X-Naver-Client-Id": NAVER_CLIENT_ID, "X-Naver-Client-Secret": NAVER_CLIENT_SECRET}
    params = {"query": keyword, "display": 10, "sort": "date"}
    try:
        return requests.get(url, headers=headers, params=params).json().get('items', [])
    except:
        return []

# 본문 스크래핑
def scrape_article(url):
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=5)
        soup = BeautifulSoup(response.text, 'html.parser')
        content = soup.select_one('#dic_area')
        return content.get_text(strip=True) if content else None
    except:
        return None

# AI 분석
def analyze_with_ai(title, content):
    if not GOOGLE_API_KEY: return None
    
    prompt = f"""
    기사 제목: {title}
    기사 본문: {content[:800]}

    [판단 기준]
    1. is_risk: 기사 내용이 '화재, 횡령, 배임, 사망, 자살, 비리, 세무조사, 구속, 징계, 부도, 사고' 등 심각한 리스크인가?
       (단순 행사, 인사 이동, 홍보, 날씨, 정책 안내, 동정 기사는 false)
    2. category: 사건의 핵심 키워드 (예: 화재, 횡령, 비리 등)
    3. reason: 왜 리스크로 판단했는가?
    4. summary: 1줄 요약

    JSON 형식으로 답해:
    {{ "is_risk": true/false, "category": "", "reason": "", "summary": "" }}
    """
    try:
        # 안전 설정 (뉴스 분석을 위해 차단 해제)
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
    print("☁️ 뉴스 감시 봇 실행 (30분 간격)")
    
    for keyword in KEYWORDS:
        articles = search_naver_news(keyword)
        
        for art in articles:
            title = art['title'].replace('<b>','').replace('</b>','').replace('&quot;','"')
            
            # [중요] 네이버 뉴스 링크 우선 사용
            link = art['link']
            
            # 1. 시간 체크 (최근 35분)
            if not is_recent_news(art['pubDate']):
                continue 

            # 2. 링크 체크
            if "news.naver.com" not in link:
                continue

            print(f"분석 중: {title}")
            content = scrape_article(link)
            
            if content:
                # AI 분석 호출
                result = analyze_with_ai(title, content)
                
                # 리스크(True)일 때만 알림 전송
                if result and result.get('is_risk'):
                    print(f"🚨 이슈 발견: {title}")
                    send_discord(title, result['summary'], result['reason'], link, result['category'])
                    time.sleep(1)
            
            time.sleep(1)

if __name__ == "__main__":
    main()
