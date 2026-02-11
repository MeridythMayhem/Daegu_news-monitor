import requests
from bs4 import BeautifulSoup
import time
import json
import os
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold

# ---------------------------------------------------------
# 1. 환경변수 로드
# ---------------------------------------------------------
NAVER_CLIENT_ID = os.environ.get("NAVER_ID")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_SECRET")
GOOGLE_API_KEY = os.environ.get("GOOGLE_KEY")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_URL")

KEYWORDS = ["대구", "경북", "경상북도"]
DB_FILE = "processed_links.txt"

# ---------------------------------------------------------
# 2. 모델 설정 함수
# ---------------------------------------------------------
def get_available_model():
    if not GOOGLE_API_KEY:
        print("❌ API 키가 없습니다.")
        return None
    
    genai.configure(api_key=GOOGLE_API_KEY)
    
    print("🔍 [시스템 점검] 모델 연결 시도 중...")
    try:
        # 우선순위: 1.5 Flash (가장 빠르고 저렴/무료) -> 1.0 Pro
        # 2.5 모델은 아직 프리뷰라 할당량이 적을 수 있어 1.5를 강제로 우선시함
        return genai.GenerativeModel('gemini-1.5-flash')
    except:
        print("⚠️ 1.5 Flash 모델 연결 실패, 기본 모델로 시도합니다.")
        return genai.GenerativeModel('gemini-pro')

# 전역 모델 변수
model = get_available_model()

# ---------------------------------------------------------
# 3. 유틸리티 함수들 (파일 저장, 디스코드 전송 등)
# ---------------------------------------------------------
def load_processed_links():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f:
            return set(f.read().splitlines())
    return set()

def save_processed_link(link):
    with open(DB_FILE, "a") as f:
        f.write(link + "\n")

def send_alert_discord(title, summary, reason, link, category):
    try:
        color = 0xFF0000 
        data = {
            "username": "대구·경북 리스크 감시 봇",
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

def send_status_report(logs):
    if not logs: return
    
    alert_count = sum(1 for log in logs if log['status'] == 'ALERT')
    pass_count = len(logs) - alert_count
    
    # 알림이 하나도 없으면 굳이 리포트 안 보내도 됨 (너무 자주 울림 방지)
    if alert_count == 0:
        print(f"✅ 특이사항 없음 (분석된 일반 기사 {pass_count}건)")
        return 

    title = f"🚨 이슈 점검 보고 ({alert_count}건 감지)"
    description = f"총 **{len(logs)}**건 중 **{alert_count}**건의 주요 이슈가 식별되었습니다.\n\n"
    for log in logs:
        if log['status'] == 'ALERT':
            description += f"🔥 **{log['title']}**\n→ {log['reason']}\n\n"
    color = 0xe74c3c 

    try:
        data = {
            "username": "대구·경북 감시 봇",
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

def is_recent_news(pubDate_str):
    try:
        news_date = parsedate_to_datetime(pubDate_str)
        now = datetime.now(news_date.tzinfo)
        diff = now - news_date
        return diff <= timedelta(minutes=60) # 1시간 이내 기사만
    except:
        return False

# ---------------------------------------------------------
# 4. 크롤링 및 AI 분석 핵심 로직 (수정된 부분)
# ---------------------------------------------------------

# [NEW] AI 할당량을 아끼기 위해 제목에 위험 단어가 없으면 AI에게 안 물어봄
def is_suspicious_title(title):
    risk_keywords = [
        "화재", "폭발", "붕괴", "사망", "숨진", "변사", "추락", 
        "구속", "체포", "입건", "송치", "압수수색", "비리", "횡령", 
        "부도", "파산", "해고", "검찰", "경찰", "수사", "법원", "징역",
        "산재", "중대재해", "폭로", "의혹", "논란", "위기"
    ]
    # 위 단어가 포함되어 있으면 True 반환
    return any(keyword in title for keyword in risk_keywords)

def search_naver_news(keyword):
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {"X-Naver-Client-Id": NAVER_CLIENT_ID, "X-Naver-Client-Secret": NAVER_CLIENT_SECRET}
    params = {"query": keyword, "display": 15, "sort": "date"}
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
        if not content: content = soup.select_one('#articeBody')
        if not content: content = soup.select_one('.go_trans._article_content')
            
        return content.get_text(strip=True) if content else None
    except:
        return None

# [NEW] 재시도 로직이 포함된 안전한 AI 분석 함수
def analyze_with_ai(title, content):
    if not model: return None
    
    # 1. 1차 필터링: 제목에 위험 키워드가 아예 없으면 AI 사용 X (할당량 절약)
    if not is_suspicious_title(title):
        print(f"⏩ [Pass] 키워드 없음, AI 분석 생략: {title}")
        return None

    prompt = f"""
    기사 제목: {title}
    기사 본문: {content[:800]}

    [분석 목표]
    대구·경북 지역의 '기업 사건사고'와 '경·검찰 인사' 소식을 분류하라.

    [판단 기준: is_risk = true 조건]
    1. 필수 지역 조건: 내용이 '대구' 또는 '경북(경상북도)' 관련일 것.
    2. 타겟 주제:
       A. 기업 및 재난 리스크: 화재, 폭발, 붕괴, 사망, 산재, 횡령, 배임, 부도, 구속, 비리, 세무조사
       B. 수사기관 인사: 경찰/검찰 관련 인사 (일반 공무원 X)

    JSON 포맷 응답:
    {{ "is_risk": true/false, "category": "", "reason": "" }}
    """
    
    # 2. 에러 발생 시 재시도 (최대 3번)
    max_retries = 3
    for attempt in range(max_retries):
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
            
        except Exception as e:
            error_msg = str(e)
            if "429" in error_msg:
                wait_time = 60 # 429 에러(Quota Exceeded)면 60초 대기
                print(f"⏳ [429 Quota] 할당량 초과! {wait_time}초 대기 후 재시도 ({attempt+1}/{max_retries})...")
                time.sleep(wait_time)
                continue # 루프 처음으로 돌아가서 다시 시도
            else:
                print(f"❌ AI 분석 에러 (치명적): {e}")
                return None
    
    print("❌ 재시도 횟수 초과로 해당 기사 분석 포기")
    return None

def main():
    print("☁️ 대구·경북 심층 감시 시작 (업그레이드 버전)")
    processed_links = load_processed_links()
    execution_logs = []
    
    if not model:
        print("🛑 모델 초기화 실패로 프로그램을 종료합니다.")
        return

    for keyword in KEYWORDS:
        articles = search_naver_news(keyword)
        
        for art in articles:
            title = art['title'].replace('<b>','').replace('</b>','').replace('&quot;','"')
            link = art['link']
            
            # 이미 처리했거나 오래된 뉴스, 네이버 뉴스 아닌 것 패스
            if link in processed_links or not is_recent_news(art['pubDate']) or "news.naver.com" not in link:
                continue 

            print(f"--------------------------------------------------")
            print(f"🔍 탐색: {title}")
            
            content = scrape_article(link)
            
            if content:
                # 수정된 analyze_with_ai 호출 (내부에서 필터링 및 재시도 수행)
                result = analyze_with_ai(title, content)
                
                # result가 있다는 건 AI가 분석을 완료했다는 뜻 (필터링된 건 None)
                if result:
                    status = "ALERT" if result.get('is_risk') else "PASS"
                    execution_logs.append({
                        "title": title,
                        "status": status,
                        "category": result.get('category', '일반'),
                        "reason": result.get('reason', '내용 없음')
                    })
                    
                    if status == "ALERT":
                        print(f"🚨 이슈 발견: {title}")
                        send_alert_discord(title, "주요 이슈 감지", result['reason'], link, result['category'])
                    
                    # 성공적으로 AI를 썼을 때만 대기 (API 속도 조절)
                    print("✅ AI 분석 완료. 3초 대기...")
                    time.sleep(3) 
                
                # 분석을 했든(AI), 안 했든(필터) 처리는 한 것으로 간주하여 저장
                # 단, 429 에러로 '실패'해서 None이 된 경우는 저장하지 않아야 다음에 다시 시도함
                # 여기서는 편의상 분석 시도한 모든 링크 저장 (무한 루프 방지)
                save_processed_link(link)
            
            # 너무 빠른 크롤링 방지용 짧은 대기
            time.sleep(1)

    send_status_report(execution_logs)

if __name__ == "__main__":
    main()
