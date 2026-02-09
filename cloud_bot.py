import requests
from bs4 import BeautifulSoup
import time
import json
import os
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
import google.generativeai as genai

# 환경변수
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

def is_recent_news(pubDate_str):
    try:
        news_date = parsedate_to_datetime(pubDate_str)
        now = datetime.now(news_date.tzinfo)
        # 테스트용: 48시간(2일) 전 뉴스까지 다 긁어옴
        return (now - news_date) <= timedelta(hours=48)
    except:
        return False

def search_naver_news(keyword):
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {"X-Naver-Client-Id": NAVER_CLIENT_ID, "X-Naver-Client-Secret": NAVER_CLIENT_SECRET}
    # 검색 결과를 10개로 늘림
    params = {"query": keyword, "display": 10, "sort": "date"}
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
    print("📢 24시간 테스트 모드 (네이버 링크 우선 버전)")
    
    for keyword in KEYWORDS:
        print(f"🔍 '{keyword}' 검색 중...")
        articles = search_naver_news(keyword)
        
        for art in articles:
            title = art['title'].replace('<b>','').replace('</b>','').replace('&quot;','"')
            
            # [수정된 부분] 네이버 뉴스 링크(link)를 무조건 먼저 씁니다.
            link = art['link'] 
            
            # 1. 날짜 체크
            if not is_recent_news(art['pubDate']):
                print(f"   PASS (너무 옛날): {title}")
                continue
            
            # 2. 링크 체크
            if "news.naver.com" not in link:
                # 네이버 뉴스 링크가 없으면 분석을 못하므로 건너뜁니다.
                print(f"   PASS (네이버 뉴스 아님): {title}")
                continue

            print(f"   🚀 분석 시도: {title}")
            content = scrape_article(link)
            
            if content:
                print("   🔔 알림 발송!")
                send_discord(title, "테스트 발송입니다.", link)
                time.sleep(1)
            else:
                print("   ⚠️ 본문 읽기 실패")

if __name__ == "__main__":
    main()
