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

# 국세청은 지역 무관하게 잡기 위해 별도 로직 처리 예정
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
                "footer": {"text": "Github Action News Monitor"}
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
        desc = f"지난 1시간 동안 {total}건의 기사를 스캔했습니다."
        color = 0x2ecc71
    else:
        title = f"🚨 정기 점검 ({risk_count}건 감지)"
        desc = f"총 {total}건 중 {risk_count}건의 이슈를 전송했습니다."
        color = 0xe74c3c

    if duplicate_content_count > 0:
        desc += f"\n(중복 내용 생략: {duplicate_content_count}건)"

    try:
        requests.post(DISCORD_WEBHOOK_URL, json={
            "username": "모니터링 요약",
            "embeds": [{"title": title, "description": desc, "color": color}]
        })
    except:
        pass

# =========================================================
# [4] 분석 로직 (수정됨)
# =========================================================

# [수정] 3번 요구사항(인사)을 위한 키워드 추가
def is_suspicious_title(title):
    risk_keywords = [
        # 1. 재해/사고/범죄
        "화재", "폭발", "붕괴", "사망", "숨진", "변사", "추락", "산재", "중대재해", "응급", "대피", "고립", "침수",
        "구속", "체포", "입건", "송치", "압수수색", "비리", "횡령", "배임", "뇌물", "도박", "마약", "성범죄", "폭행", "살인",
        "부도", "파산", "해고", "폐업", "법정관리", "워크아웃", "임금체불", "탈세", "추징",
        # 2. 국세청/감사 이슈
        "세무조사", "국세청", "세무서", "감사", "적발", "징계",
        # 3. 경찰/검찰 인사 (추가됨)
        "인사", "전보", "발령", "승진", "청장", "서장", "과장", "검사", "경무관", "총경"
    ]
    return any(keyword in title for keyword in risk_keywords)

def search_naver_news(keyword):
    # 깃허브 액션은 매번 초기화되므로 '최신순'으로 1시간 분량만 확실히 가져오는게 유리
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {"X-Naver-Client-Id": NAVER_CLIENT_ID, "X-Naver-Client-Secret": NAVER_CLIENT_SECRET}
    params = {"query": keyword, "display": 30, "sort": "date"} # display 수량 조절
    try:
        return requests.get(url, headers=headers, params=params).json().get('items', [])
    except:
        return []

def scrape_article(url):
    try:
        # 네이버 뉴스(news.naver.com)만 타겟팅 (일반 언론사 사이트는 구조가 달라 파싱 불가)
        if "news.naver.com" not in url:
            return None
            
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=5)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # 네이버 뉴스 본문 선택자들
        content = soup.select_one('#dic_area') or soup.select_one('#articeBody') or soup.select_one('.go_trans._article_content')
        return content.get_text(strip=True) if content else None
    except:
        return None

def analyze_with_ai(title, content):
    if not model: return None
    
    prompt = f"""
    기사 제목: {title}
    기사 본문(일부): {content[:600]}

    다음 3가지 기준 중 하나라도 해당하면 'is_risk': true 로 판별하시오.
    1. 대구/경북 기업의 재해, 사고, 경제범죄(횡령, 배임 등)
    2. 국세청/세무서 관련 부정적 기사 (압수수색, 자살, 감사 등) - 지역 무관
    3. 경찰/검찰의 '인사', '승진', '전보' 소식 - 지역 무관

    응답 형식(JSON):
    {{
        "is_risk": true/false,
        "category": "기업재난 / 국세청이슈 / 경검인사 중 택1",
        "reason": "판단 이유 한 줄 요약"
    }}
    """
    
    try:
        response = model.generate_content(prompt, generation_config={"response_mime_type": "application/json"})
        return json.loads(response.text)
    except:
        return None

def main():
    print("☁️ 깃허브 액션 뉴스 감시 시작...")
    execution_logs = []
    duplicate_content_count = 0
    recent_risk_titles = [] # 이번 실행 주기 내 중복 방지용

    # [핵심] 파일 로드 대신 '시간'으로 필터링 (최근 1시간 10분)
    time_threshold = datetime.now() - timedelta(minutes=70)

    if not model:
        print("API 키 오류")
        return

    processed_urls = set() # 이번 실행에서 처리한 URL (중복 검색 방지)

    for keyword in KEYWORDS:
        articles = search_naver_news(keyword)
        
        for art in articles:
            link = art['link']
            
            # 1. URL 중복 체크 (이번 실행 내)
            if link in processed_urls: continue
            processed_urls.add(link)

            # 2. 시간 체크 (깃허브 액션용 핵심 로직)
            try:
                # 네이버 API 날짜 포맷 파싱
                pub_date = parsedate_to_datetime(art['pubDate']).replace(tzinfo=None) # 시간대 정보 제거하여 비교 단순화
                if pub_date < time_threshold:
                    continue # 1시간 10분 넘은 기사는 패스
            except:
                continue

            title = art['title'].replace('<b>','').replace('</b>','').replace('&quot;','"')

            # 3. 1차 키워드 필터
            if not is_suspicious_title(title):
                continue

            # 4. 본문 스크래핑
            content = scrape_article(link)
            if not content: continue # 본문 못 가져오면 패스

            print(f"🔍 분석 중: {title}")
            
            # 5. AI 분석
            result = analyze_with_ai(title, content)
            
            log_entry = {"title": title, "status": "PASS", "category": "일반", "reason": "이슈 없음"}

            if result and result.get('is_risk'):
                # 6. 내용 유사도(도배) 체크
                is_duplicate = False
                for past_title in recent_risk_titles:
                    if get_similarity(title, past_title) > 0.6:
                        is_duplicate = True
                        break
                
                if is_duplicate:
                    log_entry['status'] = "DUPLICATE"
                    duplicate_content_count += 1
                    print(f"   └ 🔇 중복 기사 생략")
                else:
                    log_entry['status'] = "ALERT"
                    log_entry['category'] = result.get('category')
                    recent_risk_titles.append(title)
                    
                    print(f"   └ 🚨 알림 전송!")
                    send_alert_discord(title, "AI 자동 분류", result['reason'], link, result['category'])
                    time.sleep(2) # 디스코드 레이트 리밋 방지
            
            execution_logs.append(log_entry)
            time.sleep(1) # 요청 간격

    send_hourly_report(execution_logs, duplicate_content_count)
    print("✅ 실행 완료")

if __name__ == "__main__":
    main()
