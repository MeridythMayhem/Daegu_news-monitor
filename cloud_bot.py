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

# [변경 1] 감시 키워드 확장 (대구 + 경북)
KEYWORDS = ["대구", "경북", "경상북도"]

# [추가] 중복 알림 방지를 위한 파일 경로
DB_FILE = "processed_links.txt"

# Gemini 설정
if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)
    model = genai.GenerativeModel('gemini-1.5-flash')

# 파일에서 이미 보낸 기사 링크 불러오기
def load_processed_links():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f:
            return set(f.read().splitlines())
    return set()

# 보낸 기사 링크 저장하기
def save_processed_link(link):
    with open(DB_FILE, "a") as f:
        f.write(link + "\n")

# 1. 이슈 발생 시 보내는 '긴급 알림' (빨간색)
def send_alert_discord(title, summary, reason, link, category):
    try:
        color = 0xFF0000 # 빨간색
        data = {
            "username": "대구·경북 리스크 감시 봇", # 이름 변경
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

# 2. 30분마다 보내는 '활동 보고서' (3단 변신 기능 적용)
def send_status_report(logs):
    # [CASE 1] 분석할 기사가 하나도 없을 때
    if not logs:
        title = "💤 활동 보고 (데이터 없음)"
        description = "지난 30분간 새로 등록된 관련 뉴스가 없습니다.\n(네이버 뉴스 검색 결과 없음)"
        color = 0x95a5a6 # 회색 (비활성 느낌)
        
    else:
        # 통계 계산
        alert_count = sum(1 for log in logs if log['status'] == 'ALERT')
        pass_count = len(logs) - alert_count
        
        # [CASE 2] 기사는 있지만, 리스크(위험)는 없을 때 -> "평온함"
        if alert_count == 0:
            title = f"🟢 특이사항 없음 (일반 {pass_count}건)"
            description = f"총 **{pass_count}**건의 일반 뉴스가 감지되었으나,\n설정된 **주요 리스크(재난/범죄/인사)**는 발견되지 않았습니다.\n\n"
            
            # 어떤 기사들이 지나갔는지 제목만 살짝 보여주기 (최대 5개)
            description += "**[감지된 일반 기사 예시]**\n"
            for log in logs[:5]:
                short_title = log['title'][:30] + ".." if len(log['title']) > 30 else log['title']
                description += f"• {short_title}\n"
            
            if len(logs) > 5:
                description += f"...외 {len(logs)-5}건"
                
            color = 0x2ecc71 # 초록색 (안전함 의미)

        # [CASE 3] 리스크 기사가 섞여 있을 때 -> "경고"
        else:
            title = f"🚨 이슈 점검 보고 ({alert_count}건 감지)"
            description = f"총 **{len(logs)}**건 중 **{alert_count}**건의 주요 이슈가 식별되었습니다.\n\n"
            
            # 리스크 기사 목록 표시
            for log in logs:
                if log['status'] == 'ALERT':
                    description += f"🔥 **{log['title']}**\n→ {log['reason']}\n\n"
            
            color = 0xe74c3c # 빨간색 (위험 의미)

    # 디스코드 전송
    try:
        data = {
            "username": "대구·경북 감시 봇",
            "embeds": [{
                "title": title,
                "description": description,
                "color": color,
                "footer": {"text": f"Reported at {datetime.now().strftime('%H:%M')} • 30min Cycle"}
            }]
        }
        requests.post(DISCORD_WEBHOOK_URL, json=data)
    except:
        pass

# 시간 체크 (최근 60분 - 범위를 조금 늘림)
def is_recent_news(pubDate_str):
    try:
        news_date = parsedate_to_datetime(pubDate_str)
        now = datetime.now(news_date.tzinfo)
        diff = now - news_date
        return diff <= timedelta(minutes=60) # 30분 주기 실행이므로 여유있게 60분
    except:
        return False

def search_naver_news(keyword):
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {"X-Naver-Client-Id": NAVER_CLIENT_ID, "X-Naver-Client-Secret": NAVER_CLIENT_SECRET}
    params = {"query": keyword, "display": 15, "sort": "date"} # 검색량 약간 증가
    try:
        return requests.get(url, headers=headers, params=params).json().get('items', [])
    except:
        return []

# [수정] 본문 수집 기능을 좀 더 강력하게 보완
def scrape_article(url):
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36'}
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # 1차 시도: 일반 뉴스
        content = soup.select_one('#dic_area')
        
        # 2차 시도: 연예/스포츠 등 다른 형식이면 여기서 재시도
        if not content:
            content = soup.select_one('#articeBody') # 오타 아님 (과거 네이버 태그)
        if not content:
            content = soup.select_one('.go_trans._article_content')
            
        return content.get_text(strip=True) if content else None
    except Exception as e:
        print(f"❌ 스크래핑 에러: {e}")
        return None

# [수정] 에러 원인을 상세하게 출력하도록 변경
def analyze_with_ai(title, content):
    # 1. API 키가 있는지부터 확인
    if not GOOGLE_API_KEY:
        print("❌ [치명적 오류] GOOGLE_API_KEY가 환경변수에 없습니다!")
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
    
    try:
        safety = {
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
        }
        
        # 모델 생성 시도
        response = model.generate_content(
            prompt, 
            safety_settings=safety,
            generation_config={"response_mime_type": "application/json"}
        )
        
        # 응답 텍스트 확인 (디버깅용)
        # print(f"🤖 AI 원본 응답: {response.text}") 
        
        return json.loads(response.text)

    except Exception as e:
        # [중요] 구체적인 에러 메시지를 출력
        print(f"❌ Gemini API 호출 에러: {e}")
        return None

# [수정] 실패한 기록도 보고서에 포함시키는 메인 로직
def main():
    print("☁️ 대구·경북 심층 감시 시작")
    processed_links = load_processed_links() 
    execution_logs = []
    
    for keyword in KEYWORDS:
        articles = search_naver_news(keyword)
        
        for art in articles:
            title = art['title'].replace('<b>','').replace('</b>','').replace('&quot;','"')
            link = art['link']
            
            # 중복/시간 체크
            if link in processed_links or not is_recent_news(art['pubDate']) or "news.naver.com" not in link:
                continue 

            print(f"분석 시도: {title}") # 로그 메시지 변경
            content = scrape_article(link)
            
            # [중요] 성공/실패 여부에 따라 모두 기록
            if content:
                result = analyze_with_ai(title, content)
                if result:
                    # 정상 분석 완료
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
                else:
                    # AI 분석 실패 시
                    print("❌ AI 분석 실패")
                    execution_logs.append({
                        "title": title,
                        "status": "ERROR",
                        "category": "AI오류",
                        "reason": "AI가 응답하지 않음"
                    })
            else:
                # 본문 수집 실패 시
                print("❌ 본문 수집 실패 (Selector 불일치)")
                execution_logs.append({
                    "title": title,
                    "status": "ERROR",
                    "category": "수집실패",
                    "reason": "본문을 찾을 수 없음"
                })

            # 성공하든 실패하든 처리는 했으므로 링크 저장 (무한 루프 방지)
            save_processed_link(link)
            time.sleep(1)
            
            time.sleep(1)

    send_status_report(execution_logs)

if __name__ == "__main__":
    main()
