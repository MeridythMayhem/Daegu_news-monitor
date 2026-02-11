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

KEYWORDS = ["대구", "경북", "국세청", "검찰 인사", "경찰 인사"]

# =========================================================
# [2] AI 모델 연결 (안정성 강화)
# =========================================================
def get_available_model():
    if not GOOGLE_API_KEY:
        print("❌ API 키 누락")
        return None
    genai.configure(api_key=GOOGLE_API_KEY)
    
    # 여러 모델명을 순서대로 시도 (Flash -> Pro 순)
    models_to_try = [
        'gemini-1.5-flash',
        'gemini-1.5-flash-latest', 
        'gemini-1.5-pro',
        'gemini-pro'
    ]
    
    for model_name in models_to_try:
        try:
            model = genai.GenerativeModel(model_name)
            # 테스트 호출로 실제 작동 확인
            model.generate_content("test")
            print(f"✅ 모델 연결 성공: {model_name}")
            return model
        except Exception as e:
            continue
            
    print("❌ 모든 AI 모델 연결 실패 (404/Quota Error)")
    return None

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
    error_count = len([l for l in logs if l['status'] == 'ERROR'])
    
    if risk_count == 0:
        title = "🟢 정기 점검 (리스크 없음)"
        desc = f"지난 1시간 동안 **{total}건**의 기사를 스캔했습니다."
        color = 0x2ecc71
    else:
        title = f"🚨 정기 점검 ({risk_count}건 감지)"
        desc = f"총 **{total}건** 중 **{risk_count}건**의 이슈를 발견했습니다."
        color = 0xe74c3c

    if duplicate_content_count > 0:
        desc += f"\n(중복 내용 생략: {duplicate_content_count}건)"
        
    if error_count > 0:
        desc += f"\n⚠️ **{error_count}건**의 기사는 AI 오류로 분석하지 못했습니다."

    # AI가 무슨 기사를 읽었는지 확인 (상위 5개)
    if total > 0:
        desc += "\n\n**[최근 확인 내역]**\n"
        for log in logs[:5]:
            icon = "✅"
            if log['status'] == 'ALERT': icon = "🔥"
            elif log['status'] == 'ERROR': icon = "⚠️"
            
            desc += f"{icon} {log['title'][:30]}...\n"

    try:
        requests.post(DISCORD_WEBHOOK_URL, json={
            "username": "모니터링 요약",
            "embeds": [{"title": title, "description": desc, "color": color}]
        })
    except:
        pass

# =========================================================
# [4] 분석 로직 (필터링 강화)
# =========================================================

# 스팸성 기사 필터링 (토큰 절약)
def is_spam_news(title):
    spam_keywords = [
        "날씨", "기상", "비소식", "눈소식", "최저기온", "미세먼지",
        "스포츠", "경기", "축구", "야구", "골프", "우승", "결승",
        "전시", "개막", "행사", "축제", "마라톤", "모집", "개장", "부고", "별세", "화촉",
        "특징주", "마감", "코스피", "환율", "여행", "맛집", "할인", "이벤트"
    ]
    return any(keyword in title for keyword in spam_keywords)

def search_naver_news(keyword):
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {"X-Naver-Client-Id": NAVER_CLIENT_ID, "X-Naver-Client-Secret": NAVER_CLIENT_SECRET}
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
    
    prompt = f"""
    기사 제목: {title}
    기사 본문(요약): {content[:700]}

    당신은 '리스크 모니터링 요원'입니다. 아래 3가지 카테고리에 해당하는지 엄격하게 분석하세요.
    
    [감시 대상]
    1. 지역 재난/경제범죄: '대구/경북' 지역 내의 기업 사고, 재해, 횡령, 배임, 부도, 탈세 등
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
        print(f"AI 호출 에러: {e}")
        return None

def main():
    print("☁️ AI Full-Scan 모드 시작...")
    execution_logs = []
    duplicate_content_count = 0
    recent_risk_titles = []
    
    time_threshold = datetime.now() - timedelta(minutes=70)
    processed_urls = set()

    if not model:
        print("🛑 실행 중단: AI 모델 연결 실패")
        # 실패했다는 알림을 디스코드에 한 번 보내주는 센스
        try:
            requests.post(DISCORD_WEBHOOK_URL, json={"content": "⚠️ [치명적 오류] AI 모델 연결에 실패하여 모니터링을 수행하지 못했습니다."})
        except: pass
        return

    for keyword in KEYWORDS:
        articles = search_naver_news(keyword)
        print(f"--- '{keyword}' 검색: {len(articles)}건 ---")
        
        for art in articles:
            link = art['link']
            if link in processed_urls: continue
            processed_urls.add(link)

            try:
                pub_date = parsedate_to_datetime(art['pubDate']).replace(tzinfo=None)
                if pub_date < time_threshold: continue
            except: continue

            title = art['title'].replace('<b>','').replace('</b>','').replace('&quot;','"')

            if is_spam_news(title):
                continue

            content = scrape_article(link)
            if not content: continue 

            print(f"🧠 분석 중: {title}")
            
            # [수정됨] AI 분석 결과 처리 로직 (에러 핸들링 추가)
            result = analyze_with_ai(title, content)
            
            # 기본값
            log_entry = {"title": title, "status": "PASS", "category": "일반", "reason": "안전함"}

            if result is None:
                # 💥 AI가 에러를 뱉었을 때 (중요!)
                log_entry['status'] = "ERROR"
                log_entry['reason'] = "AI 모델 응답 없음"
                print(f"   └ ⚠️ 분석 실패 (API 오류)")
            
            elif result.get('is_risk'):
                # 🚨 위험 감지 시
                is_duplicate = False
                for past_title in recent_risk_titles:
                    if get_similarity(title, past_title) > 0.6:
                        is_duplicate = True
                        break
                
                if is_duplicate:
                    log_entry['status'] = "DUPLICATE"
                    duplicate_content_count += 1
                    print(f"   └ 🔇 중복 이슈 생략")
                else:
                    log_entry['status'] = "ALERT"
                    log_entry['category'] = result.get('category')
                    log_entry['reason'] = result.get('reason')
                    recent_risk_titles.append(title)
                    
                    print(f"   └ 🚨 이슈 발견! 알림 전송")
                    send_alert_discord(title, "AI 정밀 감지", result['reason'], link, result['category'])
            
            else:
                # 안전한 경우
                log_entry['reason'] = result.get('reason', '특이사항 없음')

            execution_logs.append(log_entry)
            
            # 무료 티어 제한(RPM 15) 준수
            time.sleep(4) 

    send_hourly_report(execution_logs, duplicate_content_count)
    print("✅ 실행 완료")

if __name__ == "__main__":
    main()
