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

# [검색어 추가] 의혹, 혐의, 탈루 검색어 추가
KEYWORDS = [
    "대구 압수수색", "경북 압수수색", "대구 공장 화재", "경북 공장 화재", 
    "대구 중대재해", "경북 중대재해", "대구 횡령", "경북 횡령",
    "대구 의혹", "경북 의혹", "대구 혐의", "경북 혐의", "대구 탈루", "경북 탈루",
    "포스코", "포항제철소", "에코프로", "엘앤에프", "iM뱅크", "대구은행", 
    "대구지방국세청", "대구 세무서", "경북 세무서", "국세청",
    "대구경찰청 인사", "경북경찰청 인사", "대구지검 인사", "대구지검 전보"
]

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

def get_similarity(a, b):
    return SequenceMatcher(None, a, b).ratio()

# =========================================================
# [3] 스나이퍼 필터 (파이썬 강제 채점)
# =========================================================
def check_critical_patterns(title):
    title_no_space = title.replace(" ", "")
    
    # 🚫 [강제 차단] 정치 관련 키워드가 하나라도 있으면 즉시 0점 처리 (Kill-switch)
    politics_keywords = ["국회의원", "시의원", "도의원", "구의원", "시장", "군수", "구청장", "정치", "후보", "공천", "당선", "선거", "여당", "야당", "국회", "더불어민주당", "국민의힘"]
    if any(pol in title for pol in politics_keywords):
        return 0, ""

    # 1. 지역 및 주체 사전
    local_areas = ["대구", "경북", "구미", "포항", "경주", "김천", "안동", "경산", "영천", "칠곡"]
    company_general = ["공장", "기업", "업체", "산단", "공단", "사업장", "법인", "본사", "사옥", "제조업"]
    
    # [추가] 인물 타겟팅 (기업인, 기관장 등)
    figures_general = ["회장", "대표", "원장", "이사장", "총장", "임원", "지점장"]
    
    vip_companies = ["포스코", "포항제철", "에코프로", "엘앤에프", "대구은행", "iM뱅크", "에스엘", "화성산업", "삼보모터스", "한국가스공사", "한국수력원자력", "한수원", "성서산단", "구미산단"]
    
    agencies_police_prosecutor = ["경찰", "검찰", "지검", "지청"]
    agencies_tax = ["국세청", "세무서", "국세공무원"]

    # 2. 이슈(사건) 사전 [추가: 의혹, 혐의, 탈루]
    issue_crime = ["횡령", "배임", "비리", "탈세", "구속", "압수수색", "기소", "입건", "수사", "송치", "체포", "의혹", "혐의", "탈루"]
    issue_disaster = ["화재", "폭발", "붕괴", "산불"]
    issue_accident = ["사망", "숨져", "숨진", "중상", "중대재해", "추락", "끼임", "사상"]
    issue_personnel = ["인사", "전보", "승진", "발령", "내정", "프로필"]

    # 3. 주체 파악
    is_local = any(loc in title for loc in local_areas)
    is_general_company = any(comp in title for comp in company_general)
    is_figure = any(fig in title for fig in figures_general)
    is_vip_company = any(vip in title for vip in vip_companies)
    
    # 기업/인물 타겟팅: (지역어 + 일반기업/인물어) 또는 (VIP기업명)
    target_company_or_figure = (is_local and (is_general_company or is_figure)) or is_vip_company
    
    # 경검/세무 타겟팅
    target_pol_pro = is_local and any(agency in title for agency in agencies_police_prosecutor)
    target_tax = (is_local and any(tax in title for tax in agencies_tax)) or ("국세청" in title)

    # 4. 타겟별 이슈 매칭
    if target_company_or_figure:
        if any(crime in title for crime in issue_crime):
            return 100, "1. 대구/경북 기업(인물) 범죄/의혹/수사 이슈"
        if any(disaster in title for disaster in issue_disaster):
            return 100, "2. 대구/경북 기업 재난(화재/폭발) 이슈"
        if any(acc in title for acc in issue_accident):
            return 100, "3. 대구/경북 기업 노동자 사망/중대재해"

    if target_pol_pro:
        if any(personnel in title for personnel in issue_personnel):
            return 100, "4. 대구/경북 경찰/검찰 인사 소식"

    if target_tax:
        if any(crime in title for crime in issue_crime + issue_accident) or any(personnel in title for personnel in issue_personnel):
            return 100, "5. 대구/경북 세무서 및 국세청 주요 이슈"

    return 0, ""

# =========================================================
# [4] 알림 및 보고 로직
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
    except: pass

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
            description = "설정하신 5대 타겟(기업 비리, 의혹, 재난, 사망, 경검 인사, 국세청)과 일치하는 뉴스가 현재 없습니다."
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
    except: pass

# =========================================================
# [5] 분석 로직 (AI 판사)
# =========================================================
def search_naver_news(keyword):
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {"X-Naver-Client-Id": NAVER_CLIENT_ID, "X-Naver-Client-Secret": NAVER_CLIENT_SECRET}
    params = {"query": keyword, "display": 15, "sort": "date"}
    try:
        return requests.get(url, headers=headers, params=params).json().get('items', [])
    except: return []

def scrape_article(url):
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=5)
        soup = BeautifulSoup(response.text, 'html.parser')
        content = soup.select_one('#dic_area') or soup.select_one('#articeBody') or soup.select_one('.go_trans._article_content')
        return content.get_text(strip=True)[:1000] if content else None
    except: return None

def analyze_with_ai(title, content, forced_reason):
    if not model: return None
    
    prompt = f"""
    [분석 요청]
    기사 제목: {title}
    기사 본문: {content[:600]}
    사전 감지된 타겟: {forced_reason}

    이 기사가 사전 감지된 타겟에 **실제로** 부합하는지 엄격하게 검증하시오.

    [🚨 100점 처리 기준 (진짜 상황일 때)]
    - 기업, 공장, 주요 인물(기업인, 단체장 등)의 횡령, 배임, 비리 의혹, 세금 탈루 제기 및 수사 혐의
    - 실제로 화재/폭발/사망 사고가 발생한 경우
    - 실제로 대구/경북 경찰, 검찰, 세무서의 인사/전보 명단이 포함된 경우

    [⚠️ 0점 처리 기준 (오탐지 방지 - 가짜 상황일 때)]
    - 🚨 **정치인(국회의원, 시장, 구청장, 도의원, 선거 후보 등)과 관련된 의혹이나 재판 (무조건 0점 처리)**
    - 제목만 자극적이고 본문은 "화재 예방 캠페인", "안전 점검 실시", "대책 마련"인 경우
    - 단순히 "성금 기탁", "위로금 전달", "표창장 수여"를 하는 내용인 경우
    - 대구/경북 지역과 무관한 타 지역의 소식인 경우

    JSON 포맷 응답: {{ "score": 점수, "category": "카테고리명", "reason": "이유 한 줄 요약" }}
    """
    try:
        res = model.generate_content(prompt, generation_config={"response_mime_type": "application/json"})
        return json.loads(res.text)
    except: return None

def main():
    print("☁️ 5대 타겟 전용 봇 작동 시작...")
    execution_logs = []  
    processed_urls = set()
    seen_titles = [] 
    
    time_threshold = datetime.now() - timedelta(minutes=70)

    if not model: 
        print("API 키 오류")
        return

    for keyword in KEYWORDS:
        articles = search_naver_news(keyword)
        for art in articles:
            title = art['title'].replace('<b>','').replace('</b>','').replace('&quot;','"')
            link = art['link']
            
            if link in processed_urls: continue
            processed_urls.add(link)
            
            try:
                if parsedate_to_datetime(art['pubDate']).replace(tzinfo=None) < time_threshold: continue
            except: continue

            # 제목 유사도 도배 방지 (0.8 기준)
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
                "score": 0,
                "category": "일반", "reason": ""
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
                            log_entry['reason'] = "[AI 기각] " + result.get('reason', '관련 없는 내용 (또는 정치 기사)')
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
            time.sleep(1)

    # 1시간 요약 리포트 전송
    send_hourly_report(execution_logs)
    print("✅ 완료")

if __name__ == "__main__":
    main()
