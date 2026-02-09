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

# 1. 이슈 발생 시 보내는 '긴급 알림' (빨간색)
def send_alert_discord(title, summary, reason, link, category):
    try:
        color = 0xFF0000 # 빨간색
        data = {
            "username": "뉴스 감시 봇",
            "embeds": [{
                "title": f"🚨 [{category}] 심각한 이슈 감지!",
                "description": f"**{title}**",
                "color": color,
                "fields": [
                    {"name": "📝 요약", "value": summary, "inline": False},
                    {"name": "💡 판단 근거", "value": reason, "inline": False},
                    {"name": "🔗 링크", "value": f"[기사 원문 보기]({link})", "inline": False}
                ],
                "footer": {"text": "AI News Monitor • Urgent Alert"}
            }]
        }
        requests.post(DISCORD_WEBHOOK_URL, json=data)
    except:
        pass

# 2. 30분마다 보내는 '활동 보고서' (회색/파란색) - 새로 추가된 기능!
def send_status_report(logs):
    if not logs:
        # 분석한 기사가 하나도 없을 때
        content = "💤 **[활동 보고]** 지난 30분간 새로 등록된 관련 뉴스가 없습니다."
        color = 0x95a5a6 # 회색
    else:
        # 분석한 기사가 있을 때
        alert_count = sum(1 for log in logs if log['status'] == 'ALERT')
        pass_count = len(logs) - alert_count
        
        description = f"🔍 총 **{len(logs)}**건 분석 완료 (🚨알림: {alert_count}건 / ❌패스: {pass_count}건)\n\n"
        
        # 상세 내역 (너무 길면 자름)
        for log in logs[:10]: # 최대 10개까지만 표시
            icon = "🚨" if log['status'] == 'ALERT' else "❌"
            # 제목이 길면 자르기
            short_title = (log['title'][:20] + '..') if len(log['title']) > 20 else log['title']
            description += f"{icon} **{short_title}** → {log['reason']} ({log['category']})\n"
            
        if len(logs) > 10:
            description += f"\n...외 {len(logs)-10}건 생략"

        content = ""
        color = 0x3498db # 파란색

    try:
        data = {
            "username": "뉴스 감시 봇",
            "embeds": [{
                "title": "📋 30분 주기 활동 보고서",
                "description": description if logs else content,
                "color": color,
                "footer": {"text": f"Execution Time: {datetime.now().strftime('%H:%M')}"}
            }]
        }
        requests.post(DISCORD_WEBHOOK_URL, json=data)
    except:
        pass

# 시간 체크 (최근 35분)
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
    2. category: 사건의 핵심 키워드 (예: 화재, 횡령, 홍보, 행사, 날씨 등)
    3. reason: 왜 리스크(또는 리스크 아님)로 판단했는가? (짧게, 10자 내외)

    JSON 형식으로 답해:
    {{ "is_risk": true/false, "category": "", "reason": "" }}
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
    print("☁️ 뉴스 감시 및 보고 시스템 시작")
    
    # [활동 기록장] 이번 실행에서 분석한 결과를 모아두는 곳
    execution_logs = []
    
    for keyword in KEYWORDS:
        articles = search_naver_news(keyword)
        
        for art in articles:
            title = art['title'].replace('<b>','').replace('</b>','').replace('&quot;','"')
            link = art['link']
            
            # 1. 시간 및 링크 체크
            if not is_recent_news(art['pubDate']) or "news.naver.com" not in link:
                continue 

            print(f"분석 중: {title}")
            content = scrape_article(link)
            
            if content:
                result = analyze_with_ai(title, content)
                
                if result:
                    # 기록장에 적기
                    status = "ALERT" if result.get('is_risk') else "PASS"
                    
                    log_entry = {
                        "title": title,
                        "status": status,
                        "category": result.get('category', '기타'),
                        "reason": result.get('reason', '판단불가')
                    }
                    execution_logs.append(log_entry)

                    # 리스크면 즉시 알림 발송
                    if status == "ALERT":
                        print(f"🚨 이슈 발견: {title}")
                        send_alert_discord(title, "긴급 이슈 발생", result['reason'], link, result['category'])
                    
                    time.sleep(1)
            
            time.sleep(1)

    # 모든 분석이 끝나면 [활동 보고서] 발송
    print("📋 활동 보고서 전송 중...")
    send_status_report(execution_logs)

if __name__ == "__main__":
    main()
