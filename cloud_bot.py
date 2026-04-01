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

KEYWORDS = [
    "대구 압수수색", "경북 압수수색", "대구 공장 화재", "경북 공장 화재", 
    "대구 중대재해", "경북 중대재해", "대구 횡령", "경북 횡령",
    "대구 의혹", "경북 의혹", "대구 혐의", "경북 혐의", "대구 탈루", "경북 탈루",
    "포스코", "포항제철소", "에코프로", "엘앤에프", "iM뱅크", "대구은행", 
    "대구지방국세청", "대구 세무서", "경북 세무서", "국세청",
    "대구경찰청 인사", "경북경찰청 인사", "대구지검 인사", "대구지검 전보"
]

# 봇의 기억을 저장할 파일 이름
HISTORY_FILE = "news_history.json"

# =========================================================
# [2] 기억력(과거 데이터 저장/불러오기) 및 유틸리티
# =========================================================
def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except: pass
    return {"urls": [], "titles": []}

def save_history(history):
    # 파일이 너무 커지지 않도록 최근 500개만 기억합니다.
    history["urls"] = history["urls"][-500:]
    history["titles"] = history["titles"][-500:]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

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
# [3] 스나이퍼 필터 (점수 세분화)
# =========================================================
def check_critical_patterns(title):
    title_no_space = title.replace(" ", "")
    
    # 🚫 정치 관련 키워드 즉시 차단 (0점)
    politics_keywords = ["국회의원", "시의원", "도의원", "구의원", "시장", "군수", "구청장", "정치", "후보", "공천", "당선", "선거", "여당", "야당", "국회", "더불어민주당", "국민의힘"]
    if any(pol in title for pol in politics_keywords):
        return 0, ""

    local_areas = ["대구", "경북", "구미", "포항", "경주", "김천", "안동", "경산", "영천", "칠곡"]
    company_general = ["공장", "기업", "업체", "산단", "공단", "사업장", "법인", "본사", "사옥", "제조업"]
    figures_general = ["회장", "대표", "원장", "이사장", "총장", "임원", "지점장"]
    vip_companies = ["포스코", "포항제철", "에코프로", "엘앤에프", "대구은행", "iM뱅크", "에스엘", "화성산업", "삼보모터스", "한국가스공사", "한국수력원자력", "한수원", "성서산단", "구미산단"]
    
    agencies_police_prosecutor = ["경찰", "검찰", "지검", "지청"]
    agencies_tax = ["국세청", "세무서", "국세공무원"]

    # 🚨 100점짜리 치명적 이슈
    issue_crime = ["횡령", "배임", "비리", "탈세", "구속", "압수수색", "기소", "입건", "수사", "송치", "체포", "의혹", "혐의", "탈루"]
    issue_disaster = ["화재", "폭발", "붕괴", "산불"]
    issue_accident = ["사망", "숨져", "숨진", "중상", "중대재해", "추락", "끼임", "사상"]
    issue_personnel = ["인사", "전보", "승진", "발령", "내정", "프로필"]
    
    # ⚠️ 70점짜리 주의보 (위기, 갈등)
    issue_warning = ["논란", "위기", "적자", "파업", "노조", "갈등", "소송", "재판", "항소", "벌금", "제동", "하락"]

    is_local = any(loc in title for loc in local_areas)
    is_general_company = any(comp in title for comp in company_general)
    is_figure = any(fig in title for fig in figures_general)
    is_vip_company = any(vip in title for vip in vip_companies)
    
    target_company_or_figure = (is_local and (is_general_company or is_figure)) or is_vip_company
    target_pol_pro = is_local and any(agency in title for agency in agencies_police_prosecutor)
    target_tax = (is_local and any(tax in title for tax in agencies_tax)) or ("국세청" in title)

    # 타겟에 대한 세분화된 점수 부여
    if target_company_or_figure:
        if any(crime in title for crime in issue_crime): return 100, "기업(인물) 범죄/의혹/수사"
        if any(disaster in title for disaster in issue_disaster): return 100, "기업 재난(화재/폭발)"
        if any(acc in title for acc in issue_accident): return 100, "기업 노동자 사망/중대재해"
        if any(warn in title for warn in issue_warning): return 70, "기업 위기/갈등/소송 주의보"
        if is_vip_company: return 50, "VIP 기업 일반 동향" # VIP 기업은 별일 없어도 50점으로 모니터링

    if target_pol_pro:
        if any(personnel in title for personnel in issue_personnel): return 100, "경찰/검찰 인사"

    if target_tax:
        if any(crime in title for crime in issue_crime + issue_accident) or any(personnel in title for personnel in issue_personnel):
            return 100, "세무서 및 국세청 주요 이슈"

    return 0, ""

# =========================================================
# [4] 알림 보고 로직 (2단 분리)
# =========================================================
def send_hourly_report(logs):
    # 50점 이상인 의미 있는 기사만 필터링
    valid_logs = [l for l in logs if l.get('score', 0) >= 50]
    sorted_logs = sorted(valid_logs, key=lambda x: x.get('score', 0), reverse=True)
    
    high_risks = [l for l in sorted_logs if l.get('score', 0) >= 80]
    medium_risks = [l for l in sorted_logs if 50 <= l.get('score', 0) < 80]
    
    if not sorted_logs:
        title = "🟢 뉴스 모니터링 (특이사항 없음)"
        description = "설정하신 타겟(기업, 경검, 국세청) 관련 이슈 뉴스가 없습니다."
        color = 0x2ecc71
    else:
        title = f"📊 정기 보고 (총 {len(sorted_logs)}건 감지)"
        description = ""
        color = 0xe74c3c if high_risks else 0xFFA500
        
        # 1단: 100점짜리 치명적 리스크
        if high_risks:
            description += "🚨 **[핵심 리스크] 즉시 확인 요망**\n"
            for log in high_risks:
                description += f"**[{log['score']}점]** [{log['title']}]({log['link']})\n└ {log['reason']}\n\n"
        
        # 2단: 70점/50점짜리 주의 및 동향
        if medium_risks:
            if high_risks: description += "---\n" # 구분선
            description += "⚠️ **[주의 및 동향] 모니터링 필요**\n"
            for log in medium_risks[:7]: # 너무 길어지지 않게 7개까지만
                description += f"**[{log['score']}점]** [{log['title']}]({log['link']})\n└ {log['reason']}\n"

    try:
        data = {
            "username": "뉴스 요약 봇",
            "embeds": [{
                "title": title, "description": description, "color": color,
                "footer": {"text": f"{datetime.now().strftime('%H:%M')} 기준"}
            }]
        }
        requests.post(DISCORD_WEBHOOK_URL, json=data)
    except: pass

# =========================================================
# [5] 분석 로직 (AI 판사 프롬프트 수정)
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

    이 기사를 읽고 아래 기준에 따라 0에서 100 사이의 점수로 평가하시오.

    [🚨 80~100점: 확정적이고 치명적인 리스크]
    - 확정된 횡령, 구속, 압수수색, 탈루 수사
    - 실제 발생한 화재, 폭발, 노동자 사망사고
    - 실제 발표된 경검/세무서 인사 명단

    [⚠️ 50~79점: 주의 깊게 봐야 할 위기 및 논란]
    - 아직 확정되지 않은 의혹 제기, 고발장 접수, 재판 진행 중
    - 파업, 노조 갈등, 영업 적자, 주가 폭락, 소송 등의 기업 위기
    - VIP 기업의 일반적인 부정적 동향

    [❌ 0점: 가짜 뉴스 및 오탐지 방지]
    - 무조건 0점: 정치인(국회의원, 시장, 선거 등) 관련 기사
    - 단순 화재 "예방 캠페인", "안전 훈련", "대책 회의"
    - "성금 기부", "MOU 체결", "표창 수여" 등 긍정적 내용
    - 대구/경북과 무관한 타 지역 기사

    JSON 포맷 응답: {{ "score": 점수, "category": "카테고리명", "reason": "이유 한 줄 요약" }}
    """
    try:
        res = model.generate_content(prompt, generation_config={"response_mime_type": "application/json"})
        return json.loads(res.text)
    except: return None

def main():
    print("☁️ 스마트 기억력 & 2단 분리 봇 작동 시작...")
    
    # [새로운 기능] 과거 봇의 기억을 불러옵니다.
    history = load_history()
    
    execution_logs = []  
    processed_urls = set()
    time_threshold = datetime.now() - timedelta(minutes=70)

    if not model: 
        print("API 키 오류")
        return

    for keyword in KEYWORDS:
        articles = search_naver_news(keyword)
        for art in articles:
            title = art['title'].replace('<b>','').replace('</b>','').replace('&quot;','"')
            link = art['link']
            
            # 현재 실행 중 중복 방지
            if link in processed_urls: continue
            processed_urls.add(link)
            
            # [새로운 기능] 1시간 전, 어제 처리했던 URL이면 건너뜁니다.
            if link in history["urls"]: continue

            try:
                if parsedate_to_datetime(art['pubDate']).replace(tzinfo=None) < time_threshold: continue
            except: continue

            # [새로운 기능] 과거 봇이 처리했던 제목들과도 유사도를 비교합니다 (강력한 도배 방지)
            is_dup_title = False
            for past_title in history["titles"]:
                if get_similarity(title, past_title) > 0.8:
                    is_dup_title = True
                    break
            if is_dup_title: continue 

            forced_score, forced_reason = check_critical_patterns(title)
            
            log_entry = {
                "title": title, "link": link,
                "score": forced_score, "category": "일반", "reason": forced_reason
            }

            # 50점 이상(주의보 이상) 기사만 AI에게 검증을 맡깁니다.
            if forced_score >= 50:
                print(f"🔍 타겟 감지됨({forced_score}점). AI 검증 진행: {title}")
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
                            log_entry['reason'] = "[AI 기각] 정치 또는 무관한 내용"
                        else:
                            log_entry['reason'] = result.get('reason', forced_reason)
                    else:
                        log_entry['reason'] += " (AI 응답 지연)"
            
            execution_logs.append(log_entry)
            
            # AI 처리를 받았든 안 받았든, 새 기사는 봇의 '기억'에 추가합니다.
            history["urls"].append(link)
            history["titles"].append(title)
            time.sleep(1)

    send_hourly_report(execution_logs)
    
    # [새로운 기능] 새롭게 배운 제목과 링크를 파일에 저장합니다.
    save_history(history)
    print("✅ 완료 및 기억 저장 성공")

if __name__ == "__main__":
    main()
