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

# [수정] '국세청' 키워드 복구 및 지역 키워드 유지
KEYWORDS = ["대구", "경북", "국세청"]
DB_FILE = "processed_links.txt"

# 모델 설정 (GitHub 환경 호환성 확보)
def get_available_model():
    if not GOOGLE_API_KEY:
        print("❌ API 키가 없습니다.")
        return None
    
    genai.configure(api_key=GOOGLE_API_KEY)
    
    print("🔍 [시스템 점검] 사용 가능한 모델 탐색 중...")
    available_models = []
    
    try:
        for m in genai.list_models():
            if 'generateContent' in m.supported_generation_methods:
                available_models.append(m.name)
        
        target_model = None
        if 'models/gemini-1.5-flash' in available_models:
            target_model = 'gemini-1.5-flash'
        elif 'models/gemini-2.0-flash' in available_models:
            target_model = 'gemini-2.0-flash'
        elif 'models/gemini-pro' in available_models:
            target_model = 'gemini-pro'
        elif available_models:
            target_model = available_models[0].replace('models/', '')
            
        if target_model:
            print(f"✅ [연결 성공] 선택된 모델: {target_model}")
            return genai.GenerativeModel(target_model)
        else:
            print("❌ [오류] 사용 가능한 모델이 없습니다.")
            return None
            
    except Exception as e:
        print(f"❌ 모델 목록 조회 실패: {e}")
        return None

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
            "username": "리스크 감시 봇",
            "embeds": [{
                "title": f"🚨 [{category}] 주요 소식 감지",
                "description": f"**{title}**",
                "color": color,
                "fields": [
                    {"name": "📝 요약", "value": summary, "inline": False},
                    {"name": "💡 판단 근거", "value": reason, "inline": False},
                    {"name": "🔗 링크", "value": f"[기사 원문 보기]({link})", "inline": False}
                ],
                "footer": {"text": "Risk Monitor • Urgent Alert"}
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
        description = f"총 **{pass_count}**건의 뉴스가 감지되었으나,\n설정된 **주요 리스크**는 발견되지 않았습니다.\n\n"
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
            "username": "감시 봇 보고",
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
        return diff <= timedelta(minutes=65)
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
        if not content: content = soup.select_one('#articeBody')
        if not content: content = soup.select_one('.go_trans._article_content')
            
        return content.get_text(strip=True) if content else None
    except:
        return None

def analyze_with_ai(title, content):
    if not model: return None 
    
    # [중요] 국세청 이슈를 포함하도록 프롬프트 수정
    prompt = f"""
    기사 제목: {title}
    기사 본문: {content[:800]}

    [판단 기준: is_risk = true 조건]
    기사가 아래 A 또는 B 중 하나에 해당하면 true로 판단하라.

    A. 대구·경북 지역 리스크:
       - 내용이 '대구' 또는 '경북' 지역과 관련될 것.
       - 주제: 공장/기업 화재, 폭발, 사망사고, 산재, 횡령, 배임, 부도, 구속, 수사기관(경/검) 인사 등.
    
    B. 국세청(전국/지방청) 중대 이슈:
       - 내용이 '국세청' 또는 '세무서'와 관련될 것.
       - 주제: 직원 자살, 감사원 감사, 압수수색, 뇌물/비리, 구속, 중대 징계.
       - (제외: 단순 세금 신고 안내, 연말정산 홍보, 정책 설명은 false)

    JSON 포맷 응답:
    {{ "is_risk": true/false, "category": "", "reason": "" }}
    """
    
    safety = {
        HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
    }

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
            if "429" in error_msg or "quota" in error_msg.lower():
                if attempt < max_retries:
                    print(f"⏳ 속도
