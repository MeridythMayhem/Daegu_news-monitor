import requests
from bs4 import BeautifulSoup
import time
import json
import os
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
import google.generativeai as genai
from difflib import SequenceMatcher

# =========================================================
# [1] 환경변수 및 설정
# =========================================================
NAVER_CLIENT_ID = os.environ.get("NAVER_ID")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_SECRET")
GOOGLE_API_KEY = os.environ.get("GOOGLE_KEY")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_URL")

KEYWORDS = ["대구", "경북", "국세청", "검찰 인사", "경찰 인사"] # 키워드 자체를 구체화

# =========================================================
# [2] AI 모델 연결
# =========================================================
def get_available_model():
    if not GOOGLE_API_KEY: return None
    genai.configure(api_key=GOOGLE_API_KEY)
    try:
        return genai.GenerativeModel('gemini-1.5-flash')
    except:
        return genai.GenerativeModel('gemini-pro')

model = get_available_model()

# =========================================================
# [3] 유틸리티
# =========================================================
def get_similarity(a, b):
    return SequenceMatcher(None, a, b).ratio()

def send_alert_discord(title, summary, reason, link, category):
    try:
        data = {
            "username": "뉴스 리스크 봇",
            "embeds": [{
                "title": f"🚨 [{category}] 이슈 감지",
                "description": f"**{title}**",
                "color": 0xFF0000, 
                "fields": [
                    {"name": "📝 요약", "value": summary, "inline": False},
                    {"name": "💡 판단 근거", "value": reason, "inline": False},
                    {"name": "🔗 링크", "value": f"[기사 원문]({link})", "inline": True}
                ],
                "footer": {"text": "AI Full-Scan System"}
            }]
        }
        requests.post(DISCORD_WEBHOOK_URL, json=data)
    except:
        pass

def send_hourly_report(logs, duplicate_content_count):
    total = len(logs)
    risk_count = len([l for l in logs if l['status'] == 'ALERT'])
    
    if risk_count == 0:
        title = "🟢 정기 점검 (특이사항 없음)"
        desc = f"지난 1시간 동안 **{total}건**의 기사를 AI가 정밀 분석했습니다."
        color = 0x2ecc71
    else:
        title = f"🚨 정기 점검 ({risk_count}건 감지)"
        desc = f"총 **{total}건**을 AI가 분석하여 **{risk_count}건**의 이슈를 찾아냈습니다."
        color = 0xe74c3c

    if duplicate_content_count > 0:
        desc += f"\n(중복 내용 생략: {duplicate_content_count}건)"

    # AI가 무슨 기사를 읽었는지 확인하기 위해 상위 5개 로그 출력
    if total > 0:
        desc += "\n\n**[AI가 검토한 기사들]**\n"
        for log in logs[:5]:
            status_icon = "🔥" if log['status'] == 'ALERT' else "✅"
            desc += f"{status_icon} {log['title'][:30]}...\n"

    try:
        requests.post(DISCORD_WEBHOOK_URL, json={
            "username": "모니터링 요약",
            "embeds": [{"title": title, "description": desc, "color": color}]
        })
    except:
        pass

# =========================================================
# [4] 분석 로직 (전면 개편)
# =========================================================

# [변경점] "이런 단어 있으면 통과" -> "이런 단어 있으면 무시(스팸)"
# 스포츠, 날씨, 부고, 단순 행사 알림 등은 AI 토큰 낭비이므로 1차 제거
def is_spam_news(title):
    spam_keywords = [
        "날씨", "기상", "비소식", "눈소식", "최저기온", "미세먼지", # 날씨
        "스포츠", "경기", "축구", "야구", "골프", "우승", "결승",  # 스포츠
        "전시", "개막", "행사", "축제", "마라톤",                # 단순 행사
        "부고", "별세", "화촉", "모집", "개장",                  # 일반 알림
        "특징주", "마감", "코스피", "환율",                      # 단순 주식 시황
        "여행", "맛집", "할인", "이벤트"                         # 광고성
    ]
    return any(keyword in title for keyword in spam_keywords)

def search_naver_news(keyword):
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {"X-Naver-Client-Id": NAVER_CLIENT_ID, "X-Naver-Client-Secret": NAVER_CLIENT_SECRET}
    # AI로 다 검사할 것이므로 display를 너무 늘리면 API 제한 걸림. 20개 정도가 적당.
    params = {"query": keyword, "display": 20, "sort": "date"}
    try:
        return requests.get(url, headers=headers, params=params).json().get('items', [])
    except:
        return []

def scrape_article(url):
    try:
        if "news.naver.com" not in url: return None
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=5)
        soup = BeautifulSoup(response.text, 'html.parser')
        content = soup.select_one('#dic_area') or soup.select_one('#articeBody') or soup.select_one('.go_trans._article_content')
        return content.get_text(strip=True) if content else None
    except:
        return None

def analyze_with_ai(title, content):
    if not model: return None
    
    # 프롬프트는 그대로 유지하되, 더 강력하게 판단 요구
    prompt = f"""
    기사 제목: {title}
    기사 본문(요약): {content[:700]}

    당신은 '리스크 모니터링 요원'입니다. 아래 3가지 카테고리에 해당하는지 엄격하게 분석하세요.
    
    [감시 대상]
    1. 지역 재난/경제범죄: '대구/경북' 지역 내의 기업 사고, 재해, 횡령, 배임, 부도 등
    2. 국세청 리스크: 국세청/세무서 관련 부정적 기사 (압수수색, 직원 비위, 고강도 감사 등)
    3. 수사기관 인사: 경찰/검찰의 '인사 이동', '승진', '발령' 소식 (단순 사건 보도 아님)

    [응답 형식 JSON]
    {{ 
        "is_risk": true/false, 
        "category": "기업재난 / 국세청 / 수사기관인사", 
        "reason": "판단 이유를 한 문장으로 작성" 
    }}
    """
    
    try:
        response = model.generate_content(prompt, generation_config={"response_mime_type": "application/json"})
        return json.loads(response.text)
    except Exception as e:
        print(f"AI Error: {e}")
        return None

def main():
    print("☁️ AI Full-Scan 모드 시작...")
    execution_logs = []
    duplicate_content_count = 0
    recent_risk_titles = []
    
    # 최근 70분 기사만 체크
    time_threshold = datetime.now() - timedelta(minutes=70)
    processed_urls = set()

    if not model:
        print("API 키 오류")
        return

    for keyword in KEYWORDS:
        articles = search_naver_news(keyword)
        print(f"--- 키워드 '{keyword}' 검색 결과: {len(articles)}건 ---")
        
        for art in articles:
            link = art['link']
            if link in processed_urls: continue
            processed_urls.add(link)

            # 날짜 체크
            try:
                pub_date = parsedate_to_datetime(art['pubDate']).replace(tzinfo=None)
                if pub_date < time_threshold: continue
            except: continue

            title = art['title'].replace('<b>','').replace('</b>','').replace('&quot;','"')

            # [핵심 변경] 키워드 필터 삭제 -> 스팸 필터로 대체
            # "날씨", "스포츠" 같은 명백한 쓰레기 데이터가 아니면 일단 통과
            if is_spam_news(title):
                print(f"🗑️ 스팸 패스: {title}")
                continue

            # 본문 수집
            content = scrape_article(link)
            if not content: continue 

            print(f"🧠 AI 분석 중: {title}")
            
            # AI에게 모든 판단 위임
            result = analyze_with_ai(title, content)
            
            log_entry = {"title": title, "status": "PASS", "category": "일반", "reason": "안전함"}

            if result and result.get('is_risk'):
                # 도배 방지
                is_duplicate = False
                for past_title in recent_risk_titles:
                    if get_similarity(title, past_title) > 0.6:
                        is_duplicate = True
                        break
                
                if is_duplicate:
                    log_entry['status'] = "DUPLICATE"
                    duplicate_content_count += 1
                else:
                    log_entry['status'] = "ALERT"
                    log_entry['category'] = result.get('category')
                    log_entry['reason'] = result.get('reason')
                    recent_risk_titles.append(title)
                    
                    print(f"🚨 발견!: {title}")
                    send_alert_discord(title, "AI 정밀 감지", result['reason'], link, result['category'])
            
            else:
                if result: log_entry['reason'] = result.get('reason') # AI가 안전하다고 판단한 이유 기록

            execution_logs.append(log_entry)
            
            # [중요] AI 무료 티어(RPM 15) 제한을 지키기 위해 강제 휴식
            # 1분에 15개 = 4초에 1개. 안전하게 4초 대기.
            time.sleep(4) 

    send_hourly_report(execution_logs, duplicate_content_count)
    print("✅ 실행 완료")

if __name__ == "__main__":
    main()
