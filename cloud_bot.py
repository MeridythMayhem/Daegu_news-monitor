import requests
from bs4 import BeautifulSoup
import time
import json
import os
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold

# ==========================================
# [설정 영역] 깃허브 'Secrets'에서 가져오게 설정됨
# ==========================================
# (주의: 이 코드는 내 컴퓨터에서 그냥 실행하면 에러 납니다. 깃허브에 올려야 작동합니다.)
NAVER_CLIENT_ID = os.environ.get("NAVER_ID")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_SECRET")
GOOGLE_API_KEY = os.environ.get("GOOGLE_KEY")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_URL")

KEYWORDS = ["대구", "국세청"]

# Gemini 설정
genai.configure(api_key=GOOGLE_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

# 디스코드 알림
def send_discord(title, summary, reason, link, category):
    try:
        color = 0xFF0000 if "화재" in category or "사망" in category else 0xFFA500
        data = {
            "username": "뉴스 감시 봇",
            "embeds": [{
                "title": f"🚨 [{category}] 주요 이슈 감지",
                "description": f"**{title}**",
                "color": color,
                "fields": [
                    {"name": "📝 요약", "value": summary, "inline": False},
                    {"name": "🔗 링크", "value": f"[기사 보러가기]({link})", "inline": False}
                ],
                "footer": {"text": "Github Action Bot"}
            }]
        }
        requests.post(DISCORD_WEBHOOK_URL, json=data)
    except:
        pass

# 날짜 파싱 및 시간 비교 (핵심 로직 변경)
def is_recent_news(pubDate_str):
    try:
        # 네이버 뉴스 날짜 형식: "Mon, 09 Feb 2025 14:00:00 +0900"
        news_date = parsedate_to_datetime(pubDate_str)
        
        # 현재 시간
        now = datetime.now(news_date.tzinfo)
        
        # [설정] 최근 70분 이내의 기사만 통과 (1시간마다 실행할 것이므로 여유 있게 70분)
        diff = now - news_date
        return diff <= timedelta(minutes=70)
    except:
        return False

# 네이버 검색
def search_naver_news(keyword):
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {"X-Naver-Client-Id": NAVER_CLIENT_ID, "X-Naver-Client-Secret": NAVER_CLIENT_SECRET}
    # 반드시 최신순(date)으로 정렬해야 함
    params = {"query": keyword, "display": 10, "sort": "date"}
    try:
        response = requests.get(url, headers=headers, params=params)
        return response.json().get('items', []) if response.status_code == 200 else []
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
    prompt = f"""
    기사 제목: {title}
    기사 본문: {content[:800]}

    [판단 기준]
    1. is_risk: '화재, 횡령, 배임, 사망, 자살, 비리, 세무조사, 구속' 등 심각한 이슈인가? (단순 행사/홍보 X)
    2. category: 사건 종류 (예: 화재, 비리, 사고)
    3. summary: 1줄 요약

    JSON 형식으로 답해:
    {{ "is_risk": true/false, "category": "", "summary": "" }}
    """
    try:
        response = model.generate_content(prompt, generation_config={"response_mime_type": "application/json"})
        return json.loads(response.text)
    except:
        return None

# 메인 실행
def main():
    print("☁️ 클라우드 뉴스 감시 시작...")
    
    for keyword in KEYWORDS:
        articles = search_naver_news(keyword)
        for art in articles:
            # 1. 시간 체크: 최근 1시간 내 기사인가?
            if not is_recent_news(art['pubDate']):
                continue # 너무 옛날 기사면 패스

            link = art['originallink'] or art['link']
            title = art['title'].replace('<b>','').replace('</b>','').replace('&quot;','"')
            
            if "news.naver.com" not in link: continue

            print(f"분석 중: {title}")
            content = scrape_article(link)
            
            if content:
                result = analyze_with_ai(title, content)
                if result and result['is_risk']:
                    send_discord(title, result['summary'], "이슈 감지", link, result['category'])
                    time.sleep(2) # 도배 방지

if __name__ == "__main__":
    main()