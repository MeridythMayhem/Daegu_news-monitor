import requests
from bs4 import BeautifulSoup
import time
import json
import os
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold

# 1. 환경변수 로드
NAVER_CLIENT_ID = os.environ.get("NAVER_ID")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_SECRET")
GOOGLE_API_KEY = os.environ.get("GOOGLE_KEY")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_URL")

# 2. 감시 설정 (경상북도는 경북에 포함되므로 중복 제거)
KEYWORDS = ["대구", "경북"]
DB_FILE = "processed_links.txt"

# [요청 반영] GitHub 환경에 맞춰 작동하는 모델을 스스로 찾아내는 함수
def get_available_model():
    if not GOOGLE_API_KEY:
        print("❌ API 키가 없습니다.")
        return None
    
    genai.configure(api_key=GOOGLE_API_KEY)
    
    print("🔍 [시스템 점검] 사용 가능한 모델 탐색 중...")
    available_models = []
    
    try:
        # 현재 키로 사용 가능한 모델 리스트 조회
        for m in genai.list_models():
            if 'generateContent' in m.supported_generation_methods:
                available_models.append(m.name)
        
        target_model = None
        # 1순위: 1.5-flash (가장 빠름)
        if 'models/gemini-1.5-flash' in available_models:
            target_model = 'gemini-1.5-flash'
        # 2순위: 2.0-flash (최신)
        elif 'models/gemini-2.0-flash' in available_models:
            target_model = 'gemini-2.0-flash'
        # 3순위: 1.0-pro (가장 안정적)
        elif 'models/gemini-pro' in available_models:
            target_model = 'gemini-pro'
        # 비상: 목록에 있는 거 아무거나
        elif available_models:
            target_model = available_models[0].replace('models/', '')
            
        if target_model:
            print(f"✅ [연결 성공] 선택된 모델: {target_model}")
            return genai.GenerativeModel(target_model)
        else:
            print("❌ [오류] 사용 가능한 모델이 없습니다. API 키 권한을 확인하세요.")
            return None
            
    except Exception as e:
        print(f"❌ 모델 목록 조회 실패: {e}")
        return None

# 전역 변수로 모델 설정
model = get_available_model()

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
    
    if alert_count == 0:
        title = f"🟢 특이사항 없음 (일반 {pass_count}건)"
        description = f"총 **{pass_count}**건의 일반 뉴스가 감지되었으나,\n설정된 **주요 리스크**는 발견되지 않았습니다.\n\n"
        description += "**[감지된 기사 예시]**\n"
        for log in logs[:5]:
            short_title = log['title'][:30] + ".." if len(log['title']) > 30 else log['title']
            description += f"• {short_title}\n"
        color = 0x2ecc71 
    else:
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
                "footer": {"text": f"Reported at {datetime.now().strftime('%H:%M')} • 1hr Cycle"}
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
        return diff <= timedelta(minutes=65) # 1시간 주기라 65분 여유
    except:
        return False

def search_naver_news(keyword):
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {"X-Naver-Client-Id": NAVER_CLIENT_ID, "X-Naver-Client-Secret": NAVER_CLIENT_SECRET}
    
    # [요청 반영] 3개 -> 5개로 복구 (놓치는 뉴스 없도록)
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
        if not content: content = soup.select_one('#articeBody')
        if not content: content = soup.select_one('.go_trans._article_content')
            
        return content.get_text(strip=True) if content else None
    except:
        return None

def analyze_with_ai(title, content):
    if not model: return None 
    
    prompt = f"""
    기사 제목: {title}
    기사 본문: {content[:800]}

    [판단 기준: is_risk = true 조건]
    1. 필수 지역 조건: 내용이 '대구' 또는 '경북' 관련일 것.
    2. 타겟 주제:
       A. 기업 및 재난 리스크: 화재, 폭발, 붕괴, 사망, 산재, 횡령, 배임, 부도, 구속, 비리, 세무조사
       B. 수사기관 인사: 경찰/검찰 관련 인사 (일반 공무원 X)

    JSON 포맷 응답:
    {{ "is_risk": true/false, "category": "", "reason": "" }}
    """
    
    safety = {
        HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
    }

    # [수정됨] 들여쓰기 교정 완료 & 재시도 로직 유지
    max_retries = 1
    for attempt in range(max_retries + 1):
        try:
            response = model.generate_content(
                prompt, 
                safety_settings=safety,
                generation_config={"response_mime_type": "application/json"}
            )
            return json.loads(response.text)

        except Exception as e:
            error_msg = str(e)
            # 429 에러(속도 제한) 발생 시
            if "429" in error_msg or "quota" in error_msg.lower():
                if attempt < max_retries:
                    print(f"⏳ 속도 제한 감지! 잠시 대기 후 재시도... ({attempt+1}/{max_retries})")
                    time.sleep(20) # 재시도 대기 시간은 넉넉하게
                    continue
            print(f"❌ 분석 실패: {e}")
            return None
    return None

def main():
    print("☁️ 대구·경북 감시 봇 (안정성 강화 모드)")
    processed_links = load_processed_links()
    execution_logs = []
    
    if not model:
        print("🛑 모델 설정 실패로 종료")
        return

    for keyword in KEYWORDS:
        articles = search_naver_news(keyword)
        
        for art in articles:
            title = art['title'].replace('<b>','').replace('</b>','').replace('&quot;','"')
            link = art['link']
            
            # [기본 필터링] 중복/날짜/링크 체크만 수행
            if link in processed_links or not is_recent_news(art['pubDate']) or "news.naver.com" not in link:
                continue 

            # [요청 반영] 불필요한 키워드 필터링 삭제 -> 모든 기사를 AI가 직접 판단

            print(f"분석 시도: {title}")
            content = scrape_article(link)
            
            if content:
                result = analyze_with_ai(title, content)
                
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
                    
                    save_processed_link(link)
                    
                    # [요청 반영] 대기 시간 15초로 복구 (안전 제일)
                    print("⏳ 15초 대기 중...")
                    time.sleep(15) 
                else:
                    print("❌ AI 응답 없음")
            
            time.sleep(1)

    send_status_report(execution_logs)

if __name__ == "__main__":
    main()
