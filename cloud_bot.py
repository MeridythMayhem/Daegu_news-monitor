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
    history["urls"] = history["urls"][-500:]
    history["titles"] = history["titles"][-500:]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

def get_similarity(a, b):
    return SequenceMatcher(None, a, b).ratio()

# =========================================================
# [3] 스나이퍼 필터 (점수 세분화 및 타겟 감지)
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

    if target_company_or_figure:
        if any(crime in title for crime in issue_crime): return 100, "기업(인물) 범죄/의혹/수사"
        if any(disaster in title for disaster in issue_disaster): return 100, "기업 재난(화재/폭발)"
        if any(acc in title for acc in issue_accident): return 100, "기업 노동자 사망/중대재해"
        if any(warn in title for warn in issue_warning): return 70, "기업 위기/갈등/소송 주의보"
        if is_vip_company: return 50, "VIP 기업 일반 동향"

    if target_pol_pro:
        if any(personnel in title for personnel in issue_personnel): return 100, "경찰/검찰 인사"

    if target_tax:
        if any(crime in title for crime in issue_crime + issue_accident) or any(personnel in title for personnel in issue_personnel):
            return 100, "세무서 및 국세청 주요 이슈"

    return 0, ""

# =========================================================
# [4] 알림 보고 로직 (2단 분리 모아 쏘기)
# =========================================================
def send_hourly_report(logs):
    valid_logs = [l for l in logs if l.get('score', 0) >= 50]
    sorted_logs = sorted(valid_logs, key=lambda x: x.get('score', 0), reverse=True)
    
    high_risks = [l for l in sorted_logs if l.get('score', 0) >= 80]
    medium_risks = [l for l in sorted_logs if 50 <= l.get('score', 0) < 80]
    
    if not sorted_logs:
        title = "🟢 뉴스 모니터링 (특이사항 없음)"
        description = "설정하신 5대 핵심 타겟 관련 이슈 뉴스가 없습니다."
        color = 0x2ecc71
    else:
        title = f"📊 정기 보고 (총 {len(sorted_logs)}건 감지)"
        description = ""
        color = 0xe74c3c if high_risks else 0xFFA500
        
        if high_risks:
            description += "🚨 **[핵심 리스크] 즉시 확인 요망**\n"
            for log in high_risks:
                description += f"**[{log['score']}점]** [{log['title']}]({log['link']})\n└ {log['reason']}\n\n"
        
        if medium_risks:
            if high_risks: description += "---\n"
            description += "⚠️ **[주의 및 동향] 모니터링 필요**\n"
            for log in medium_risks[:7]:
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
# [5] 분석 로직 (🚨 서버에서 사용 가능한 모델 자동 탐색)
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
    if not GOOGLE_API_KEY: return None
    genai.configure(api_key=GOOGLE_API_KEY)
    
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

    반드시 아래와 같은 JSON 포맷으로만 응답할 것:
    {{ "score": 점수, "category": "카테고리명", "reason": "이유 한 줄 요약" }}
    """
    
    safety_settings = [
        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}
    ]

    try:
        # [핵심 로직] 내 API 키로 사용할 수 있는 모델 목록을 구글 서버에서 직접 받아옵니다.
        valid_models = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        
        target_model = None
        # 1.5 계열의 똑똑한 모델을 먼저 찾고, 없으면 1.0이나 pro를 찾습니다.
        for pref in ['models/gemini-1.5-flash', 'models/gemini-1.5-pro', 'models/gemini-1.0-pro', 'models/gemini-pro']:
            if pref in valid_models:
                target_model = pref.replace('models/', '')
                break
                
        # 만약 위 이름들이 다 없으면 구글이 허락한 첫 번째 텍스트 생성 모델을 무조건 씁니다.
        if not target_model:
            if valid_models:
                target_model = valid_models[0].replace('models/', '')
            else:
                print("❌ API 키에 연결된 사용 가능한 Gemini 모델이 없습니다.")
                return None

        # 확정된 모델로 세팅
        model = genai.GenerativeModel(target_model)

        # 1.5 최신 버전일 때만 JSON 강제 출력 옵션을 사용합니다 (구버전 충돌 방지)
        if "1.5" in target_model:
            res = model.generate_content(
                prompt, 
                safety_settings=safety_settings,
                generation_config={"response_mime_type": "application/json"}
            )
        else:
            res = model.generate_content(
                prompt, 
                safety_settings=safety_settings
            )

        # JSON 마크다운 찌꺼기 깔끔하게 제거
        raw_text = res.text.strip()
        marker = "`" * 3
        if raw_text.startswith(f"{marker}json"): raw_text = raw_text[7:]
        elif raw_text.startswith(marker): raw_text = raw_text[3:]
        if raw_text.endswith(marker): raw_text = raw_text[:-3]
        
        return json.loads(raw_text.strip())
        
    except Exception as e:
        print(f"❌ AI 분석 최종 실패: {title} | 사유: {e}")
        return None

def main():
    print("☁️ 스마트 기억력 & AI 모델 자동 탐색 봇 작동 시작...")
    
    history = load_history()
    execution_logs = []  
    processed_urls = set()
    time_threshold = datetime.now() - timedelta(minutes=70)

    for keyword in KEYWORDS:
        articles = search_naver_news(keyword)
        for art in articles:
            title = art['title'].replace('<b>','').replace('</b>','').replace('&quot;','"')
            link = art['link']
            
            if link in processed_urls: continue
            processed_urls.add(link)
            if link in history["urls"]: continue

            try:
                if parsedate_to_datetime(art['pubDate']).replace(tzinfo=None) < time_threshold: continue
            except: continue

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
                            log_entry['reason'] = "[AI 기각] 정치 또는 가짜 뉴스"
                        else:
                            log_entry['reason'] = result.get('reason', forced_reason)
                    else:
                        log_entry['reason'] += " (AI 분석 에러 - 파이썬 점수 유지)"
            
            execution_logs.append(log_entry)
            
            history["urls"].append(link)
            history["titles"].append(title)
            time.sleep(1)

    send_hourly_report(execution_logs)
    save_history(history)
    print("✅ 완료 및 기억 저장 성공")

if __name__ == "__main__":
    main()
