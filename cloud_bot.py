import requests
from bs4 import BeautifulSoup
import time
import json
import os
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
import google.generativeai as genai

# 환경변수 설정
NAVER_CLIENT_ID = os.environ.get("NAVER_ID")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_SECRET")
GOOGLE_API_KEY = os.environ.get("GOOGLE_KEY")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_URL")

KEYWORDS = ["대구", "국세청"]

if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)
    model = genai.GenerativeModel('gemini-1.5-flash')

def send_discord(title, summary, link):
    try:
        data = {
            "username": "테스트 봇",
            "content": f"✅ **[테스트 알림]**\n제목: {title}\n요약: {summary}\n[기사 보기]({link})"
        }
        requests.post(DISCORD_WEBHOOK_URL, json=data)
    except:
        pass

# [중요] 24시간 이내 뉴스인지 확인 (테스트용으로 늘림)
def is_recent_news(pubDate_str):
    try:
        news_date = parsedate_to_datetime(pubDate_str)
        now = datetime.now(news_date.tzinfo)
        # 24시간(하루) 전 뉴스까지 모두 가져옴!
        return (now - news_date) <= timedelta(hours=24)
    except:
        return False

def search_naver_news(keyword):
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {"X-Naver-Client-Id": NAVER_CLIENT_ID, "X-Naver-Client-Secret": NAVER_CLIENT_SECRET}
    params = {"query": keyword, "display": 5, "sort": "date"}
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

def main():
    print("📢 24시간 테스트 모드 시작! (지난 뉴스를 강제로 가져옵니다)")
    
    for keyword in KEYWORDS:
        print(f"🔍 '{keyword}' 검색 중...")
        articles = search_naver_news(keyword)
        
        for art in articles:
            title = art['title'].replace('<b>','').replace('</b>','').replace('&quot;','"')
            link = art['originallink'] or art['link']
            
            # 1. 24시간 이내인지 확인
            if not is_recent_news(art['pubDate']):
                print(f"   PASS (너무 옛날): {title}")
                continue
            
            # 2. 네이버 뉴스 링크인지 확인
            if "news.naver.com" not in link:
                print(f"   PASS (링크 안맞음): {title}")
                continue

            print(f"   🚀 분석 시도: {title}")
            content = scrape_article(link)
            
            if content:
                # 테스트를 위해 AI 분석 없이 무조건 알림을 보내봅니다.
                print("   🔔 알림 발송!")
                send_discord(title, "테스트 발송입니다.", link)
                time.sleep(1) # 디스코드 도배 방지

if __name__ == "__main__":
    main()
