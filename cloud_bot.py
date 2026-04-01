import requests
from bs4 import BeautifulSoup
from newspaper import Article # 💡 새로 추가된 범용 스크래핑 라이브러리
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

# 💡 기록을 저장할 파일 이름과 최대 보관 개수 설정
HISTORY_FILE = "bot_history.json"
MAX_HISTORY = 1000

# [핵심] 수집 단계부터 타겟팅된 정밀 키워드로만 검색합니다.
KEYWORDS = [
    "대구 압수수색", "경북 압수수색", "대구 공장 화재", "경북 공장 화재", 
    "대구 중대재해", "경북 중대재해", "대구 횡령", "경북 횡령",
    "포스코", "포항제철소", "에코프로", "엘앤에프", "iM뱅크", "대구은행", 
    "대구지방국세청", "대구 세무서", "경북 세무서", "국세청",
    "대구경찰청 인사", "경북경찰청 인사", "대구지검 인사", "대구지검 전보"
]

# =========================================================
# [2] 상태 관리 (기억 유지 로직 추가)
# =========================================================
def load_history():
    """실행 시 이전 기록(DB)을 불러옵니다."""
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return set(data.get("processed_urls", [])), data.get("seen_titles", [])
        except Exception as e:
            print(f"⚠️ 기록 파일을 읽는 중 오류 발생 (초기화 진행): {e}")
            return set(), []
    return set(), []

def save_history(processed_urls, seen_titles):
    """종료 전 현재 기록을 파일에 저장합니다."""
    data = {
        "processed_urls": list(processed_urls)[-MAX_HISTORY:],
        "seen_titles": seen_titles[-MAX_HISTORY:]
    }
    try:
        with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"❌ 기록 저장 실패: {e}")

# =========================================================
# [3] AI 모델 연결 및 유틸리티
# =========================================================
def get_available_model():
    if not GOOGLE_API_KEY: return None
    genai.configure(api_key=GOOGLE_API_KEY)
    try:
        return genai.GenerativeModel('gemini-1.5-flash')
    except:
        return genai.GenerativeModel('gemini-pro')

model = get_available_model()

def get_similarity(a, b):
    return SequenceMatcher(None, a, b).ratio()

# =========================================================
# [4] 스나이퍼 필터 (파이썬 강제 채점)
# =========================================================
def check_critical_patterns(title):
    title_no_space = title.replace(" ", "")
    
    local_areas = ["대구", "경북", "구미", "포항", "경주", "김천", "안동", "경산", "영천", "칠곡"]
    company_general = ["공장", "기업", "업체", "산단", "공단", "사업장", "법인", "본사", "사옥", "제조업"]
    vip_companies = ["포스코", "포항제철", "에코프로", "엘앤에프", "대구은행", "iM뱅크", "에스엘", "화성산업", "삼보모터스", "한국가스공사", "한국수력원자력", "한수원", "성서산단", "구미산단"]
    
    agencies_police_prosecutor = ["경찰", "검찰", "지검", "지청"]
    agencies_tax = ["국세청", "세무서", "국세공무원"]

    issue_crime = ["횡령", "배임", "비리", "탈세", "구속", "압수수색", "기소", "입건", "수사", "송치", "체포"]
    issue_disaster = ["화재", "폭발", "붕괴", "산불"]
    issue_accident = ["사망", "숨져", "숨진", "중상", "중대재해", "추락", "끼임", "사상"]
    issue_personnel = ["인사", "전보", "승진", "발령", "내정", "프로필"]

    is_local = any(loc in title for loc in local_areas)
    is_general_company = any(comp in title for comp in company_general)
    is_vip_company = any(vip in title for vip in vip_companies)
    
    target_company = (is_local and is_general_company) or is_vip_company
    target_pol_pro = is_local and any(agency in title for agency in agencies_police_prosecutor)
    target_tax = (is_local and any(tax in title for tax in agencies_tax)) or ("국세청" in title)

    if target_company:
        if any(crime in title for crime in issue_crime): return 100, "1. 대구/경북 기업 범죄/수사 이슈"
        if any(disaster in title for disaster in issue_disaster): return 100, "2. 대구/경북 기업 재난(화재/폭발) 이슈"
        if any(acc in title for acc in issue_accident): return 100, "3. 대구/경북 기업 노동자 사망/중대재해"

    if target_pol_pro:
        if any(personnel in title for personnel in issue_personnel): return 100, "4. 대구/경북 경찰/검찰 인사 소식"

    if target_tax:
        if any(crime in title for crime in issue_crime + issue_accident) or any(personnel in title for personnel in issue_personnel):
            return 100, "5. 대구/경북 세무서 및 국세청 주요 이슈"

    return 0, ""

# =========================================================
# [5] 알림 및 보고 로직
# =========================================================
def send_alert_discord(title, summary, reason, link, category, score):
    color = 0xFF0000 if score >= 80 else 0xFFA500
    try:
        data = {
            "username": "리스크 감시 봇",
            "embeds": [{
                "title": f"🚨 [자동감지] {category}",
                "description": f"**{title}**",
                "color": color, 
                "fields": [
                    {"name": "💡 감지 사유", "value": reason, "inline": False},
                    {"name": "🔗 바로가기", "value": f"[기사 원문]({link})", "inline": True}
                ],
                "footer": {"text": "Critical News Alert"}
            }]
        }
        requests.post(DISCORD_WEBHOOK_URL, json=data)
    except Exception as e:
        print(f"⚠️ 디스코드 알림 발송 실패: {e}")

def send_hourly_report(logs):
    valid_logs = [l for l in logs if l.get('score', 0) > 0]
    sorted_logs = sorted(valid_logs, key=lambda x: x.get('score', 0), reverse=True)
    high_risks = [l for l in sorted_logs if l.get('score', 0) >= 80 and l['status'] == 'ALERT']
    
    if high_risks:
        title = f"🚨 정기 보고 (주요 타겟 뉴스 {len(high_risks)}건 감지)"
        description = "설정하신 **5대 핵심 타겟**에 부합하는 중대한 기사가 있습니다.\n\n"
        for log in high_risks:
            description += f"🔥 **[{log['score']}점]** {log['title']}\n└ {log['reason']}\n\n"
        color = 0xe74c3c
    else:
        title = "🟢 정기 보고 (특이사항 없음)"
        if not sorted_logs: 
            description = "설정하신 5대 타겟(기업 비리, 재난, 사망, 경검 인사, 국세청)과 일치하는 뉴스가 현재 없습니다."
        else:
            description = "주요 리스크는 없습니다. (AI가 낮게 평가한 의심 기사 목록)\n\n"
            for i, log in enumerate(sorted_logs[:5], 1):
                short = log['title'][:40] + "..." if len(log['title']) > 40 else log['title']
                description += f"**{i}.** [{short}]({log['link']}) `Score: {log['score']}`\n"
        color = 0x2ecc71

    try:
        data = {
            "username": "뉴스 모니터링 요약",
            "embeds": [{
                "title": title, "description": description, "color": color,
                "footer": {"text": f"{datetime.now().strftime('%H:%M')} 기준"}
            }]
        }
        requests.post(DISCORD_WEBHOOK_URL, json=data)
    except Exception as e:
        print(f"⚠️ 디스코드 리포트 발송 실패: {e}")

# =========================================================
# [6] 데이터 수집 및 분석 (스크래핑 로직 개선됨)
# =========================================================
def search_naver_news(keyword):
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {"X-Naver-Client-Id": NAVER_CLIENT_ID, "X-Naver-Client-Secret": NAVER_CLIENT_SECRET}
    params = {"query": keyword, "display": 15, "sort": "date"}
    try:
        return requests.get(url, headers=headers, params=params).json().get('items', [])
    except Exception as e: 
        print(f"⚠️ 네이버 API 검색 실패 ({keyword}): {e}")
        return []

def scrape_article(url):
    """newspaper3k를 활용한 범용 기사 스크래핑 (언론사 아웃링크 대응)"""
    try:
        # 1차 시도: newspaper3k
        article = Article(url, language='ko')
        article.download()
        article.parse()
        if article.text:
            return article.text.strip()[:1000]
    except Exception as e:
        pass # 조용히 2차 시도로 넘어감

    # 2차 시도 (Fallback): 기존 BeautifulSoup 네이버 인링크 전용 방식
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        response = requests.get(url, headers=headers, timeout=5)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        content = (soup.select_one('#dic_area') or 
                   soup.select_one('#articeBody') or 
                   soup.select_one('.go_trans._article_content') or
                   soup.select_one('article'))
                   
        return content.get_text(strip=True)[:1000] if content else None
    except Exception as e:
        print(f"❌ 스크래핑 최종 실패 ({url}): {e}")
        return None

def analyze_with_ai(title, content, forced_reason):
    if not model: return None
    
    prompt = f"""
    [분석 요청]
    기사 제목: {title}
    기사 본문: {content[:600]}
    사전 감지된 타겟: {forced_reason}

    이 기사가 사전 감지된 타겟(대구/경북 기업 재난/사망/비리, 경검 인사, 국세청 이슈)에 **실제로** 부합하는지 엄격하게 검증하시오.

    [🚨 100점 처리 기준 (진짜 상황일 때)]
    - 실제로 화재/폭발/사망 사고가 발생한 경우
    - 실제로 압수수색, 횡령, 구속, 비리 등 수사가 진행/발표된 경우
    - 실제로 대구/경북 경찰, 검찰, 세무서의 인사/전보 명단이 포함된 경우

    [⚠️ 0점 처리 기준 (오탐지 방지 - 가짜 상황일 때)]
    - 제목만 자극적이고 본문은 "화재 예방 캠페인", "안전 점검 실시", "모의 훈련", "대책 마련"인 경우
    - 과거의 사고를 단순히 언급하며 "성금 기탁", "위로금 전달", "표창장 수여", "MOU 체결"을 하는 내용인 경우
    - 대구/경북 지역과 완전히 무관한 타 지역(서울, 충남 등)의 소식인 경우

    JSON 포맷 응답: {{ "score": 점수, "category": "카테고리명", "reason": "이유 한 줄 요약" }}
    """
    try:
        res = model.generate_content(prompt, generation_config={"response_mime_type": "application/json"})
        return json.loads(res.text)
    except Exception as e:
        print(f"⚠️ AI 분석 오류: {e}")
        return None

# =========================================================
# [7] 메인 실행 (수정됨)
# =========================================================
def main():
    print("☁️ 5대 타겟 전용 봇 작동 시작...")
    execution_logs = []  
    
    # 💡 [추가] 시작할 때 이전 기록 불러오기 (중복 방지)
    processed_urls, seen_titles = load_history()
    
    time_threshold = datetime.now() - timedelta(minutes=70)

    if not model: 
        print("❌ API 키 오류: 구글 제미나이 API 키를 확인하세요.")
        return

    for keyword in KEYWORDS:
        articles = search_naver_news(keyword)
        for art in articles:
            title = art['title'].replace('<b>','').replace('</b>','').replace('&quot;','"')
            link = art['link']
            
            # 이미 처리한 URL이면 건너뛰기 (기록 파일 덕분에 봇이 꺼졌다 켜져도 기억함)
            if link in processed_urls: continue
            processed_urls.add(link)
            
            try:
                if parsedate_to_datetime(art['pubDate']).replace(tzinfo=None) < time_threshold: continue
            except: continue

            # 제목 유사도 도배 방지
            is_dup_title = False
            for past_title in seen_titles:
                if get_similarity(title, past_title) > 0.8:
                    is_dup_title = True
                    break
            if is_dup_title: continue 
            seen_titles.append(title) 

            # 1. 스나이퍼 필터 (파이썬 1차망)
            forced_score, forced_reason = check_critical_patterns(title)
            
            log_entry = {
                "title": title, "link": link, "status": "PASS",
                "score": 0, "category": "일반", "reason": ""
            }

            if forced_score == 100:
                print(f"🔍 타겟 감지됨. AI 검증 진행: {title}")
                content = scrape_article(link)
                
                if not content:
                    content = art.get('description', '').replace('<b>','').replace('</b>','')

                if content:
                    result = analyze_with_ai(title, content, forced_reason)
                    
                    if result:
                        final_score = result.get('score', 0)
                        log_entry['score'] = final_score
                        log_entry['category'] = result.get('category', forced_reason)
                        
                        if final_score == 0:
                            log_entry['reason'] = "[AI 기각] " + result.get('reason', '관련 없는 내용')
                        else:
                            log_entry['reason'] = result.get('reason', forced_reason)
                        
                        if final_score >= 80:
                            log_entry['status'] = "ALERT"
                            print(f"🚨 중요 타겟 뉴스 확정: {title}")
                            send_alert_discord(title, "주요 타겟 뉴스", log_entry['reason'], link, log_entry['category'], final_score)
                            
                    else:
                        log_entry['score'] = 100
                        log_entry['status'] = "ALERT" 
                        log_entry['reason'] = forced_reason + " (AI 응답 지연)"
                        print(f"🚨 타겟 감지 (AI 대체): {title}")
                        send_alert_discord(title, "주요 타겟 뉴스", log_entry['reason'], link, forced_reason, 100)
            
            execution_logs.append(log_entry)
            time.sleep(1) # API 과부하 방지

    # 1시간 요약 리포트 전송
    send_hourly_report(execution_logs)
    
    # 💡 [추가] 모든 작업이 끝나면 기록 저장하기
    save_history(processed_urls, seen_titles)
    print("✅ 완료 및 기록 저장 성공 (bot_history.json 생성됨)")

if __name__ == "__main__":
    main()
