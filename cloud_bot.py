import requests
from bs4 import BeautifulSoup
import time
import json
import os
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import google.generativeai as genai
from difflib import SequenceMatcher

# =========================================================
# [1] 환경변수 및 설정
# =========================================================
TEST_MODE = False  

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
KST = timezone(timedelta(hours=9))

# =========================================================
# [2] 기억력 및 유틸리티
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
# [3] 스나이퍼 필터 (정치/주식 차단 및 선택적 AI)
# =========================================================
def check_critical_patterns(title):
    title_no_space = title.replace(" ", "")
    
    politics_keywords = ["국회의원", "시의원", "도의원", "구의원", "시장", "군수", "구청장", "정치", "후보", "공천", "당선", "선거", "여당", "야당", "국회", "더불어민주당", "국민의힘"]
    if any(pol in title for pol in politics_keywords): return 0, "", False

    stock_keywords = ["주가", "상승", "하락", "급등", "급락", "증시", "코스피", "코스닥", "종목", "시황", "주식", "매수", "매도", "개미", "외인", "기관", "상장", "공모"]
    if any(stock in title for stock in stock_keywords): return 0, "", False

    local_areas = ["대구", "경북", "구미", "포항", "경주", "김천", "안동", "경산", "영천", "칠곡"]
    company_general = ["공장", "기업", "업체", "산단", "공단", "사업장", "법인", "본사", "사옥", "제조업", "신탁", "증권", "투자", "금융", "건설", "시행사", "조합", "은행", "지점"]
    figures_general = ["회장", "대표", "원장", "이사장", "총장", "임원", "지점장"]
    vip_companies = ["포스코", "포항제철", "에코프로", "엘앤에프", "대구은행", "iM뱅크", "에스엘", "화성산업", "삼보모터스", "한국가스공사", "한국수력원자력", "한수원", "성서산단", "구미산단"]
    
    agencies_police_prosecutor = ["경찰", "검찰", "지검", "지청"]
    agencies_tax = ["국세청", "세무서", "국세공무원"]

    issue_crime = ["횡령", "배임", "비리", "탈세", "구속", "압수수색", "기소", "입건", "수사", "송치", "체포", "의혹", "혐의", "탈루", "밀약"]
    issue_disaster = ["화재", "폭발", "붕괴", "산불"]
    issue_accident = ["사망", "숨져", "숨진", "중상", "중대재해", "추락", "끼임", "사상"]
    issue_personnel = ["인사", "전보", "승진", "발령", "내정", "프로필"]
    issue_warning = ["논란", "위기", "적자", "파업", "노조", "갈등", "소송", "재판", "항소", "벌금", "제동"]

    is_local = any(loc in title for loc in local_areas)
    is_general_company = any(comp in title for comp in company_general)
    is_figure = any(fig in title for fig in figures_general)
    is_vip_company = any(vip in title for vip in vip_companies)
    
    target_company_or_figure = (is_local and (is_general_company or is_figure)) or is_vip_company
    target_pol_pro = is_local and any(agency in title for agency in agencies_police_prosecutor)
    target_tax = (is_local and any(tax in title for tax in agencies_tax)) or ("국세청" in title)

    if target_company_or_figure:
        if any(crime in title for crime in issue_crime): return 100, "기업(인물) 범죄/의혹/수사", True
        if any(disaster in title for disaster in issue_disaster): return 100, "기업 재난(화재/폭발)", False
        if any(acc in title for acc in issue_accident): return 100, "기업 노동자 사망/중대재해", False
        if any(warn in title for warn in issue_warning): return 70, "기업 위기/갈등/소송 주의보", True
        if is_vip_company: return 50, "VIP 기업 일반 동향", True

    if target_pol_pro:
        if any(personnel in title for personnel in issue_personnel): return 100, "경찰/검찰 인사", False

    if target_tax:
        if any(crime in title for crime in issue_crime + issue_accident): return 100, "세무서/국세청 주요 이슈", True
        if any(personnel in title for personnel in issue_personnel): return 100, "세무서/국세청 인사", False

    return 0, "", False

# =========================================================
# [4] 알림 보고 로직
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
        if TEST_MODE: title = "🛠️ [테스트 모드] " + title
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
                "footer": {"text": f"{datetime.now(KST).strftime('%H:%M')} 기준"}
            }]
        }
        requests.post(DISCORD_WEBHOOK_URL, json=data)
    except: pass

# =========================================================
# [5] 분석 로직 (🚨 서킷 브레이커: AI 자동 포기 기능)
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
        # 응답 지연 시 파이썬까지 멈추는 것을 방지하기 위해 타임아웃을 3초로 짧게 줍니다.
        response = requests.get(url, headers=headers, timeout=3)
        soup = BeautifulSoup(response.text, 'html.parser')
        content = soup.select_one('#dic_area') or soup.select_one('#articeBody') or soup.select_one('.go_trans._article_content')
        return content.get_text(strip=True)[:1000] if content else None
    except: return None

def get_best_ai_model_name():
    if not GOOGLE_API_KEY: return None
    genai.configure(api_key=GOOGLE_API_KEY)
    try:
        valid_models = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        for pref in ['models/gemini-2.5-flash', 'models/gemini-1.5-flash', 'models/gemini-2.5-pro', 'models/gemini-1.5-pro', 'models/gemini-1.0-pro', 'models/gemini-pro']:
            if pref in valid_models: return pref.replace('models/', '')
        return valid_models[0].replace('models/', '') if valid_models else None
    except Exception as e:
        print(f"❌ AI 모델 초기화 실패: {e}")
        return None

def analyze_with_ai(title, content, forced_reason, model_name, api_status):
    # 🚨 서킷 브레이커 발동 상태면 아예 AI 근처에도 안 가고 바로 None 반환
    if not api_status["is_alive"]: return None
    if not model_name: return None
    
    prompt = f"""
    [분석 요청] 기사 제목: {title} | 기사 본문: {content[:600]} | 사전 감지: {forced_reason}

    이 기사를 읽고 아래 기준에 따라 0~100점 사이로 평가하시오.
    [🚨 80~100점] 확정된 횡령, 배임, 비리 의혹, 세금 탈루 제기 및 수사 혐의
    [⚠️ 50~79점] 의혹/재판 진행, 기업 위기(적자, 파업), VIP 기업 사업 동향
    [❌ 0점 (가짜 뉴스)] 정치인 기사, 주식/증시(상승, 하락, 시황) 기사, 단순 안전/기부 캠페인, 타 지역 기사

    응답 포맷: {{ "score": 점수, "category": "카테고리명", "reason": "이유 한 줄 요약" }}
    """
    
    safety_settings = [
        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}
    ]

    model = genai.GenerativeModel(model_name)
    max_retries = 3
    for attempt in range(max_retries):
        try:
            if "1.5" in model_name or "2.5" in model_name:
                res = model.generate_content(prompt, safety_settings=safety_settings, generation_config={"response_mime_type": "application/json"})
            else:
                res = model.generate_content(prompt, safety_settings=safety_settings)

            raw_text = res.text.strip()
            marker = "`" * 3
            if raw_text.startswith(f"{marker}json"): raw_text = raw_text[7:]
            elif raw_text.startswith(marker): raw_text = raw_text[3:]
            if raw_text.endswith(marker): raw_text = raw_text[:-3]
            return json.loads(raw_text.strip())
            
        except Exception as e:
            error_msg = str(e)
            if "429" in error_msg or "Quota" in error_msg or "503" in error_msg or "500" in error_msg:
                print(f"⏳ 구글 AI 서버 지연. 25초 대기 후 재시도... ({attempt+1}/{max_retries})")
                time.sleep(25)
                continue
            return None
            
    # 🚨 3번 연속 실패하면 하루 한도 고갈로 판단하고, 봇 전체의 AI 전원을 차단합니다. (서킷 브레이커 작동)
    print("❌ 3회 재시도 실패. 하루 무료 할당량이 고갈된 것으로 판단하여, 이번 실행 동안 AI를 완전 차단합니다.")
    api_status["is_alive"] = False 
    return None

def main():
    print("☁️ 초고속 스나이퍼 봇(서킷 브레이커 탑재) 작동 시작...")
    ai_model_name = get_best_ai_model_name()
    history = load_history()
    execution_logs = []  
    processed_urls = set()
    now_kst = datetime.now(KST)
    
    # 🚨 AI 생존 여부를 체크하는 상태 값
    api_status = {"is_alive": True}

    if TEST_MODE:
        history = {"urls": [], "titles": []}
        time_threshold = now_kst - timedelta(hours=24)
    else:
        time_threshold = now_kst - timedelta(minutes=70)

    for keyword in KEYWORDS:
        articles = search_naver_news(keyword)
        for art in articles:
            title = art['title'].replace('<b>','').replace('</b>','').replace('&quot;','"')
            link = art['link']
            
            if link in processed_urls or link in history["urls"]: continue
            processed_urls.add(link)

            try:
                pub_dt = parsedate_to_datetime(art['pubDate'])
                if pub_dt < time_threshold: continue
            except: continue

            is_dup = False
            for past in history["titles"]:
                if get_similarity(title, past) > 0.8: is_dup = True; break
            if is_dup: continue 

            forced_score, forced_reason, need_ai = check_critical_patterns(title)
            log_entry = {"title": title, "link": link, "score": forced_score, "category": "일반", "reason": forced_reason}

            if forced_score >= 50:
                if need_ai:
                    print(f"🔍 타겟 감지({forced_score}점). AI 검증 진행: {title}")
                    content = scrape_article(link) or art.get('description', '').replace('<b>','').replace('</b>','')
                    if content:
                        # api_status를 넘겨서 할당량이 죽었는지 실시간으로 판단합니다.
                        result = analyze_with_ai(title, content, forced_reason, ai_model_name, api_status)
                        if result:
                            log_entry['score'] = result.get('score', 0)
                            log_entry['reason'] = result.get('reason', forced_reason)
                            if log_entry['score'] >= 80: log_entry['status'] = "ALERT"
                        elif not api_status["is_alive"]:
                            log_entry['reason'] += " (AI 할당량 고갈 - 파이썬 점수 유지)"
                            
                    # AI가 살아있을 때만 속도 조절용 4초를 쉽니다. (죽었으면 0.1초만에 스킵)
                    if api_status["is_alive"]: time.sleep(4) 
                else:
                    print(f"⚡ [AI 패스] 안전/인사 기사 감지({forced_score}점). 즉시 통과: {title}")
                    log_entry['reason'] += " (사건/사고/인사 팩트)"
            
            execution_logs.append(log_entry)
            history["urls"].append(link)
            history["titles"].append(title)
            
    send_hourly_report(execution_logs)
    if not TEST_MODE: save_history(history)
    print("✅ 완료")

if __name__ == "__main__":
    main()