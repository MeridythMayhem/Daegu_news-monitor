import requests
from bs4 import BeautifulSoup
import time
import json
import os
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
import google.generativeai as genai

# ==========================================
# [설정 영역] 환경변수 확인 (디버깅용)
# ==========================================
NAVER_CLIENT_ID = os.environ.get("NAVER_ID")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_SECRET")
GOOGLE_API_KEY = os.environ.get("GOOGLE_KEY")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_URL")

# 키가 제대로 들어왔는지 확인 (보안상 앞 2글자만 출력)
print(f"🔑 네이버 ID 확인: {NAVER_CLIENT_ID[:2]}***" if NAVER_CLIENT_ID else "❌ 네이버 ID 없음!")
print(f"🔑 구글 키 확인: {GOOGLE_API_KEY[:2]}***" if GOOGLE_API_KEY else "❌ 구글 키 없음!")

KEYWORDS = ["대구", "국세청"]

if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)
    model = genai.GenerativeModel('gemini-1.5-flash')

def send_discord(title, summary, reason, link, category):
    try:
        data = {
            "username": "뉴스 감시 봇",
            "content": f"🚨 **{title}**\n{summary}\n[링크]({link})"
        }
        requests.post(DISCORD_WEBHOOK_URL, json=data)
    except Exception as e:
        print(f"❌ 디스코드 전송 실패: {e}")

def is_recent_news(pubDate_str):
    try:
        news_date = parsedate_to_datetime(pubDate_str)
        now = datetime.now(news_date.tzinfo)
        diff = now - news_date
        # 디버깅: 기사 시간과 현재 시간 차이 출력
        # print(f"   [시간차] {diff} (기사: {news_date})")
        return diff <= timedelta(minutes=180) # 테스트를 위해 3시간(180분)으로 늘림
    except Exception as e:
        print(f"⚠️ 날짜 계산 오류: {e}")
        return False

def search_naver_news(keyword):
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {"X-Naver-Client-Id": NAVER_CLIENT_ID, "X-Naver-Client-Secret": NAVER_CLIENT_SECRET}
    params = {"query": keyword, "display": 5, "sort": "date"}
    
    try:
        response = requests.get(url, headers=headers, params=params)
        if response.status_code == 200:
            items = response.json().get('items', [])
            print(f"✅ '{keyword}' 검색 성공! (발견된 기사: {len(items)}개)")
            return items
        else:
            print(f"❌ 네이버 API 호출 실패! 상태코드: {response.status_code}")
            print(f"   에러 내용: {response.text}")
            return []
    except Exception as e:
        print(f"❌ 네이버 연결 중 치명적 오류: {e}")
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

def analyze_with_ai(title, content):
    if not GOOGLE_API_KEY: return None
    
    prompt = f"""
    기사 제목: {title}
    기사 본문: {content[:500]}
    
    이 기사가 '화재, 횡령, 사망, 사고, 비리, 조사' 등 리스크인가?
    JSON으로 답해: {{ "is_risk": true/false, "summary": "한줄요약" }}
    """
    try:
        response = model.generate_content(prompt, generation_config={"response_mime_type": "application/json"})
        return json.loads(response.text)
    except Exception as e:
        print(f"❌ AI 분석 실패: {e}")
        return None

def main():
    print("☁️ 클라우드 뉴스 감시 시작 (디버깅 모드)...")
    
    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
        print("⛔ [치명적 오류] 네이버 API 키가 설정되지 않았습니다. Secrets를 확인하세요.")
        return

    for keyword in KEYWORDS:
        print(f"\n🔍 키워드 검색 중: {keyword}")
        articles = search_naver_news(keyword)
        
        if not articles:
            print(f"   -> '{keyword}' 관련 최신 기사가 하나도 없습니다.")
            continue

        for art in articles:
            title = art['title'].replace('<b>','').replace('</b>','').replace('&quot;','"')
            link = art['originallink'] or art['link']
            
            # 1. 시간 체크
            if not is_recent_news(art['pubDate']):
                print(f"   ⏭️ [패스] 너무 오래된 기사입니다: {title}")
                continue 

            # 2. 네이버 뉴스 링크 체크
            if "news.naver.com" not in link: 
                print(f"   ⏭️ [패스] 네이버 뉴스 링크가 아님: {title}")
                continue

            print(f"   🚀 [분석 시작] {title}")
            content = scrape_article(link)
            
            if content:
                result = analyze_with_ai(title, content)
                # 테스트를 위해 리스크 여부 상관없이 로그 출력
                print(f"      🤖 AI 판단: {result}")
                
                if result and result.get('is_risk'):
                    print("      🔔 이슈 발견! 알림 전송!")
                    send_discord(title, result['summary'], "이슈 감지", link, "테스트")
            else:
                print("      ⚠️ 본문 스크래핑 실패")
            
            time.sleep(1)

if __name__ == "__main__":
    main()
