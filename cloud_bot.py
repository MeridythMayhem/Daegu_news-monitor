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

KEYWORDS = ["대구", "경북", "경상북도", "국세청"]

# =========================================================
# [2] AI 모델 연결
# =========================================================
def get_available_model():
    if not GOOGLE_API_KEY:
        return None
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

# [디스코드] 즉시 알림 (긴급 이슈)
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
                    {"name": "🔗 링크", "value": f"[기사 원문 보기]({link})", "inline": True}
                ],
                "footer": {"text": "Urgent Alert System"}
            }]
        }
        requests.post(DISCORD_WEBHOOK_URL, json=data)
    except:
        pass

# [디스코드] 1시간 정기 보고
def send_hourly_report(logs, duplicate_content_count):
    total_scanned = len(logs)
    risk_alerts = [l for l in logs if l['status'] == 'ALERT']
    risk_count = len(risk_alerts)
    
    # 보고서 멘트 조합
    msg_parts = []
    if duplicate_content_count > 0:
        msg_parts.append(f"• 중복(도배) 제외: **{duplicate_content_count}**건")
    
    exclusion_msg = "\n".join(msg_parts)
    if exclusion_msg: exclusion_msg = "\n\n(참고)\n" + exclusion_msg

    # 1. 이슈 없음
    if risk_count == 0:
        title = "🟢 정기 보고 (특이사항 없음)"
        if total_scanned == 0:
            description = f"지난 1시간 동안 새로운 기사가 없습니다.{exclusion_msg}"
        else:
            description = f"새로운 기사 **{total_scanned}**건을 확인했으나 위험 요소는 없습니다.{exclusion_msg}\n\n**[확인한 주요 기사]**\n"
            # 로그에서 최대 5개까지만 보여줌
            for log in logs[:5]:
                description += f"• {log['title'][:40]}...\n"
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

# =========================================================
# [4] 분석 로직
# =========================================================

def is_suspicious_title(title):
    risk_keywords = [
        # 사고/재난
        "화재", "폭발", "붕괴", "사망", "숨진", "변사", "추락", "산재", "중대재해", "응급", "대피", "고립", "침수",
        # 범죄/수사
        "구속", "체포", "입건", "송치", "압수수색", "비리", "횡령", "배임", "뇌물", "도박", "마약", "성범죄", "폭행", "살인",
        # 경제/기업 위기
        "부도", "파산", "해고", "폐업", "법정관리", "워크아웃", "임금체불", "세무조사", "탈세", "추징",
        # 사법/행정 및 인사
        "검찰", "경찰", "수사", "법원", "징역", "선고", "재판", "기소", "징계", "감사", "적발", "의혹", "논란", "위기",
        "인사", "전보", "발령", "승진", "청장", "서장", "과장", "검사" 
    ]
    return any(keyword in title for keyword in risk_keywords)

def search_naver_news(keyword):
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {"X-Naver-Client-Id": NAVER_CLIENT_ID, "X-Naver-Client-Secret": NAVER_CLIENT_SECRET}
    params = {"query": keyword, "display": 30, "sort": "date"}
    try:
        return requests.get(url, headers=headers, params=params).json().get('items', [])
    except:
        return []

def scrape_article(url):
    try:
        if "news.naver.com" not in url:
            return None
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=5)
        soup = BeautifulSoup(response.text, 'html.parser')
        content = soup.select_one('#dic_area') or soup.select_one('#articeBody') or soup.select_one('.go_trans._article_content')
        return content.get_text(strip=True) if content else None
    except:
        return None

def analyze_with_ai(title, content):
    if not model: return None
    
    prompt = f"""
    기사 제목: {title}
    기사 본문(일부): {content[:800]}

    [분석 목표]
    다음 3가지 중 하나라도 해당하면 'is_risk': true 로 판별하시오.
    
    1. 대구·경북 지역의 '기업 사건사고', '경제범죄(횡령/배임 등)'
    2. 국세청 및 세무서 관련 부정적 이슈 (압수수색, 자살, 감사, 업무문제) - 지역 무관
    3. 경찰 및 검찰의 '인사', '승진', '전보' 소식 - 지역 무관

    JSON 포맷 응답:
    {{ "is_risk": true/false, "category": "기업재난 / 국세청 / 경검인사", "reason": "이유 한 줄 요약" }}
    """
    
    try:
        response = model.generate_content(prompt, generation_config={"response_mime_type": "application/json"})
        return json.loads(response.text)
    except:
        return None

def main():
    print("☁️ 감시 봇 작동 시작...")
    execution_logs = []  
    duplicate_content_count = 0 
    recent_risk_titles = []

    # 깃허브 액션용 시간 필터 (최근 70분)
    time_threshold = datetime.now() - timedelta(minutes=70)
    processed_urls_in_session = set() 

    if not model:
        print("🛑 모델 에러: API 키를 확인하세요.")
        return

    for keyword in KEYWORDS:
        articles = search_naver_news(keyword)
        
        for art in articles:
            title = art['title'].replace('<b>','').replace('</b>','').replace('&quot;','"')
            link = art['link']
            
            # 1. URL 중복 체크
            if link in processed_urls_in_session: continue
            processed_urls_in_session.add(link)

            # 2. 날짜 체크
            try:
                pub_date = parsedate_to_datetime(art['pubDate']).replace(tzinfo=None)
                if pub_date < time_threshold: continue
            except: continue

            # [수정] 로그 엔트리 미리 생성
            log_entry = {
                "title": title,
                "status": "PASS",
                "category": "일반",
                "reason": "특이사항 없음"
            }

            # 3. 키워드 필터
            if not is_suspicious_title(title):
                # 키워드가 없어도 로그에 저장하고 넘김 (보고서 포함용)
                execution_logs.append(log_entry)
                continue
            
            print(f"🔍 AI 분석 요청: {title}")
            
            content = scrape_article(link)
            
            if content:
                result = analyze_with_ai(title, content)
                
                if result:
                    if result.get('is_risk'):
                        # [도배 방지] 내용 유사도 체크
                        is_duplicate_content = False
                        for past_title in recent_risk_titles:
                            if get_similarity(title, past_title) > 0.6: 
                                is_duplicate_content = True
                                break
                        
                        if is_duplicate_content:
                            print(f"🔇 [중복 이슈] 알림 생략: {title}")
                            log_entry['status'] = "DUPLICATE_RISK"
                            duplicate_content_count += 1
                        else:
                            # 진짜 새로운 위험 기사
                            log_entry['status'] = "ALERT"
                            log_entry['category'] = result.get('category')
                            log_entry['reason'] = result.get('reason')
                            recent_risk_titles.append(title)
                            
                            print(f"🚨 이슈 발견: {title}")
                            send_alert_discord(title, "주요 이슈 감지", result['reason'], link, result['category'])
                            time.sleep(2)
                    else:
                        log_entry['reason'] = "AI 분석 결과 안전함"
                
                # 분석 마친 기사 로그 저장
                execution_logs.append(log_entry)
            
            time.sleep(1)

    # [디스코드] 정기 보고 전송
    send_hourly_report(execution_logs, duplicate_content_count)
    print("✅ 실행 완료")

if __name__ == "__main__":
    main()
