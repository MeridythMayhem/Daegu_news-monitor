import requests
from bs4 import BeautifulSoup
import time
import json
import os
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from difflib import SequenceMatcher

# =========================================================
# [1] 환경변수 및 설정
# =========================================================
TEST_MODE = False  

NAVER_CLIENT_ID = os.environ.get("NAVER_ID")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_SECRET")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_URL")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY") 

# =========================================================
# 🏢 [VIP 기업 명단]
# =========================================================
VIP_COMPANIES_KR = [
    "포스코", "포항제철소", "에코프로", "엘앤에프", "iM뱅크", "대구은행", 
    "에스엘", "화성산업", "삼보모터스", "한국가스공사", "한국수력원자력",
    "대동", "이수페타시스", "씨아이에스", "아진산업", "대구텍", "피에이치에이", "평화산업", "메가젠임플란트"
]

VIP_COMPANIES_EN = [
    "POSCO", "EcoPro", "L&F battery", "iM Bank", 
    "Isu Petasys", "Daedong", "TaeguTec", "Ajin Industrial", "CIS battery"
]

# 🚨 [검색어 망 전체 복구 및 자본흐름 확장]
REGIONS = ["대구", "경북", "구미", "포항"]

# 1. 기존 핵심 리스크
CORE_RISKS = [
    "압수수색", "횡령", "배임", "비자금", "페이퍼컴퍼니", "분식회계", "세무조사", 
    "편법증여", "일감몰아주기", "가공거래", "역외탈세", "의견거절", "중대재해",
    "의혹", "비리", "혐의", "탈루", "구속", "밀약"
]

# 2. 신규: 대규모 자금/투자 흐름 (이름 모를 기업도 낚기 위함)
CORE_INVESTMENTS = [
    "투자협약", "MOU", "신공장", "건립", "M&A", "인수합병", "대규모 수주", "테크노폴리스"
]

COMBINED_KEYWORDS = [f"{region} {word}" for region in REGIONS for word in CORE_RISKS + CORE_INVESTMENTS]

KEYWORDS_KR_BASE = [
    "대구경찰청 인사", "경북경찰청 인사", "국세청 인사",
    "대구지검 인사", "대구지검 전보", "대구공소청 인사", "경북공소청 인사", "대구중수청 인사", "경북중수청 인사",
    "대구지방국세청", "대구지방국세청장", "대구 세무서", "경북 세무서",
    "대구 화재", "경북 화재", "대구 공장 화재", "경북 공장 화재", "성서산단 화재", "구미산단 화재", "구미공단 화재", "포항 철강공단",
    "대구 노동자 사망", "경북 노동자 사망", "대구 끼임 사고", "경북 추락 사고", "대구 화학물질 누출", "구미 불산 누출", "대구경북산업단지",
    "대구 업체 비리", "경북 업체 비리", "대구 세금 탈루", "경북 세금 탈루", "구미 업체 구속", "포항 업체 압수수색"
]

KEYWORDS_KR = KEYWORDS_KR_BASE + COMBINED_KEYWORDS + VIP_COMPANIES_KR
KEYWORDS_GLOBAL = VIP_COMPANIES_EN

HISTORY_FILE = "news_history.json"
KST = timezone(timedelta(hours=9))

# =========================================================
# [2] 디스코드 전송 도우미 & 유틸리티
# =========================================================
def send_discord_alert(embeds):
    if not DISCORD_WEBHOOK_URL: return
    try:
        res = requests.post(DISCORD_WEBHOOK_URL, json={"username": "뉴스 요약 봇", "embeds": embeds})
    except: pass

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
    a_clean = re.sub(r'[^가-힣a-zA-Z0-9]', '', a)
    b_clean = re.sub(r'[^가-힣a-zA-Z0-9]', '', b)
    return SequenceMatcher(None, a_clean, b_clean).ratio()

def get_active_groq_model():
    if not GROQ_API_KEY: return None
    try:
        headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
        res = requests.get("https://api.groq.com/openai/v1/models", headers=headers, timeout=5)
        if res.status_code == 200:
            available_models = [m['id'] for m in res.json().get('data', [])]
            preferences = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "mixtral-8x7b-32768"]
            for pref in preferences:
                if pref in available_models: return pref
            if available_models: return available_models[0]
    except: pass
    return "mixtral-8x7b-32768"

# =========================================================
# [3] 스나이퍼 필터 
# =========================================================
def check_critical_patterns(title):
    politics_keywords = ["국회의원", "시의원", "도의원", "구의원", "시장", "군수", "구청장", "정치", "후보", "공천", "당선", "선거", "여당", "야당", "국회", "더불어민주당", "국민의힘"]
    stock_keywords = ["주가", "상승", "하락", "급등", "급락", "증시", "코스피", "코스닥", "종목", "시황", "주식", "매수", "매도", "개미", "외인", "기관", "상장", "공모"]

    issue_crime = ["횡령", "배임", "비리", "탈세", "구속", "압수수색", "기소", "입건", "송치", "체포", "비자금", "가공거래", "허위세금계산서", "페이퍼컴퍼니", "의혹", "혐의", "탈루", "밀약"]
    issue_finance = ["가업승계", "편법증여", "일감몰아주기", "일감 몰아주기", "지분매각", "전환사채", "CB", "신주인수권부사채", "BW", "비상장주식", "우회상장", "자본잠식"]
    issue_disaster = ["화재", "폭발", "붕괴", "산불", "사망", "중대재해", "끼임", "추락", "누출"]
    issue_personnel = ["인사", "전보", "승진", "발령", "내정", "프로필"]
    issue_warning = ["논란", "위기", "적자", "파업", "노조", "소송", "재판", "승계", "지배구조"]
    
    # 🚨 신규: 대규모 자본 흐름 패턴 (이걸로 대성하이텍 같은 기사를 잡습니다)
    issue_investment = ["투자협약", "MOU", "신공장", "팩토리", "건립", "신설", "M&A", "인수합병", "대규모 수주", "투자 유치", "자금 조달", "테크노폴리스"]

    # [슈퍼 패스] 
    has_critical_risk = any(word in title for word in issue_crime + issue_finance + issue_disaster + issue_investment)
    if not has_critical_risk:
        if any(pol in title for pol in politics_keywords): return 0, "", False
        if any(stock in title for stock in stock_keywords): return 0, "", False

    local_areas = ["대구", "경북", "구미", "포항", "경주", "성서산단", "국가산단", "테크노폴리스"]
    company_general = ["공장", "기업", "업체", "산단", "공단", "사업장", "법인", "본사", "자동차부품사", "이차전지", "계열사", "제조"]
    figures_general = ["회장", "대표", "임원", "오너일가", "특수관계인"]

    is_local = any(loc in title for loc in local_areas)
    is_general_company = any(comp in title for comp in company_general)
    is_vip_company = any(vip in title for vip in VIP_COMPANIES_KR)
    
    # VIP가 아니어도, 대구(is_local)에 있는 일반기업(is_general_company)이면 레이더에 들어옴
    target_company_or_figure = (is_local and (is_general_company or any(fig in title for fig in figures_general))) or is_vip_company
    target_pol_pro = is_local and any(agency in title for agency in ["경찰", "검찰", "지검", "공소청", "중수청", "수사본부"])
    target_tax = (is_local and any(tax in title for tax in ["국세청", "세무서"])) or ("국세청" in title)

    if target_company_or_figure:
        if any(crime in title for crime in issue_crime): return 100, "핵심 재무/수사 리스크", True
        if any(fin in title for fin in issue_finance): return 80, "지배구조/자본거래 징후", True
        if any(disaster in title for disaster in issue_disaster): return 100, "기업 재난/사고(화재 등)", False
        if any(warn in title for warn in issue_warning): return 70, "기업 위기/갈등/논란", True
        # 🚨 대규모 자금 흐름에 70점 부여
        if any(inv in title for inv in issue_investment): return 70, "지역 기업 대규모 자금/투자 동향", True

    if target_pol_pro:
        if any(personnel in title for personnel in issue_personnel): return 100, "사법/경찰 인사", False

    if target_tax:
        if any(crime in title for crime in issue_crime): return 100, "세무서/국세청 주요 이슈", True
        if any(personnel in title for personnel in issue_personnel): return 100, "세무서/국세청 인사", False

    return 0, "", False

# =========================================================
# [4] 수집 로직 
# =========================================================
def search_naver_news(keyword):
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {"X-Naver-Client-Id": NAVER_CLIENT_ID, "X-Naver-Client-Secret": NAVER_CLIENT_SECRET}
    params = {"query": keyword, "display": 10, "sort": "date"}
    try:
        return requests.get(url, headers=headers, params=params).json().get('items', [])
    except: return []

def search_google_news(keyword, lang='ko'):
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
    safe_keyword = urllib.parse.quote_plus(keyword)
    if lang == 'ko':
        url = f"https://news.google.com/rss/search?q={safe_keyword}&hl=ko&gl=KR&ceid=KR:ko"
    else:
        url = f"https://news.google.com/rss/search?q={safe_keyword}&hl=en-US&gl=US&ceid=US:en"
    try:
        response = requests.get(url, headers=headers, timeout=5)
        soup = BeautifulSoup(response.content, 'xml')
        return [{'title': item.title.text, 'link': item.link.text, 'pubDate': item.pubDate.text, 'lang': lang} for item in soup.find_all('item')[:10]]
    except: return []

# =========================================================
# [5] 메인 실행 루프
# =========================================================
def main():
    print("☁️ 글로벌 & 재무 리스크 스나이퍼 봇 작동 시작...")
    active_model = get_active_groq_model()
    history = load_history()
    execution_logs = []  
    raw_articles = []
    unique_links = set()
    now_kst = datetime.now(KST)

    print(f"\n⚡ [1단계] 네이버/구글 뉴스 수집 중... (키워드 {len(KEYWORDS_KR)}개)")
    for kw in KEYWORDS_KR:
        items = search_naver_news(kw) + search_google_news(kw, lang='ko')
        for it in items:
            link = it.get('link') or it.get('originallink')
            if link and link not in unique_links:
                unique_links.add(link)
                raw_articles.append(it)
        time.sleep(0.05)
    
    print(f"🌍 [2단계] 글로벌 외신 수집 중...")
    for kw in KEYWORDS_GLOBAL:
        items = search_google_news(kw, lang='en')
        for it in items:
            if it['link'] not in unique_links:
                unique_links.add(it['link'])
                raw_articles.append(it)
        time.sleep(0.4)

    print(f"\n📊 [수집 결과 보고]")
    print(f"   - 총 검색된 고유 기사: {len(raw_articles)}건 (키워드 중복 제거 완료)")
    
    time_threshold = now_kst - (timedelta(hours=24) if TEST_MODE else timedelta(minutes=75))
    valid_articles = []
    
    for art in raw_articles:
        title = art['title'].replace('<b>','').replace('</b>','').replace('&quot;','"')
        link = art.get('link') or art.get('originallink')
        lang = art.get('lang', 'ko')

        try:
            pub_dt = parsedate_to_datetime(art['pubDate'])
            if pub_dt.tzinfo is None: pub_dt = pub_dt.replace(tzinfo=timezone.utc)
            if pub_dt < time_threshold: continue
            if link in history["urls"]: continue
            if any(get_similarity(title, t) > 0.85 for t in history["titles"]): continue
        except: continue

        score, reason, need_ai = check_critical_patterns(title)
        if lang == 'en': score = 50; need_ai = True 
        
        if score >= 50:
            valid_articles.append({'title': title, 'link': link, 'score': score, 'reason': reason, 'lang': lang, 'need_ai': need_ai, 'raw': art})

    print(f"   - 최근 1시간 이내 타겟 기사: {len(valid_articles)}건 (시간/리스크 필터 통과)")
    print(f"⏳ 이제 {len(valid_articles)}건의 기사에 대해 AI 정밀 분석을 시작합니다...\n")

    api_status = {"is_alive": True}
    
    for v in valid_articles:
        if v['need_ai'] and api_status["is_alive"] and active_model:
            print(f"🔍 AI 분석 중: {v['title'][:40]}...")
            
            system_instr = "You are a news risk analyst for a regional tax authority. Respond in JSON only."
            if v['lang'] == 'en':
                prompt = f"""[GLOBAL NEWS ANALYSIS] Title: {v['title']} | Snippet: {v['raw'].get('description', '')[:500]}
                1. Translate to Korean. 2. Score 0-100: [🚨 80-100] Crisis. [⚠️ 50-79] M&A, Strategy. [❌ 0] Stock.
                Format: {{ "score": 50, "category": "글로벌 동향", "reason": "한국어 요약" }}"""
            else:
                prompt = f"""[국내 뉴스 분석] 기사 제목: {v['title']} | 본문: {v['raw'].get('description', '')[:500]}
                당신은 국세청 조사국을 위한 기업 리스크/자본흐름 감별사입니다.
                [🚨 80~100점] 세금 탈루, 횡령, 지배구조 의혹, 세무조사, 기업 재난/사망
                [⚠️ 50~79점] 대규모 투자협약, 신공장 건립, M&A, 수백억대 자금조달 및 수주
                [❌ 0점] 단순 주가 시황, 실적발표, 기부, 제품출시
                포맷: {{ "score": 점수, "category": "카테고리명", "reason": "세무/재무/자본흐름 중심 1줄 요약" }}"""

            headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
            try:
                res = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json={"model": active_model, "messages": [{"role": "system", "content": system_instr}, {"role": "user", "content": prompt}], "temperature": 0.2}, timeout=12)
                if res.status_code == 200:
                    raw_text = res.json()['choices'][0]['message']['content'].strip()
                    marker = chr(96) * 3
                    if marker in raw_text: raw_text = re.sub(f'{marker}(json)?|{marker}', '', raw_text)
                    parsed = json.loads(raw_text.strip())
                    v['score'] = parsed.get('score', v['score'])
                    v['reason'] = parsed.get('reason', v['reason'])
            except: pass
            time.sleep(1.2)
            
        if v['score'] >= 50:
            execution_logs.append(v)
            history["urls"].append(v['link'])
            history["titles"].append(v['title'])

    # AI 데스킹 및 디스코드 전송 로직
    if not execution_logs:
        send_discord_alert([{"title": "🟢 뉴스 모니터링 (이상 없음)", "description": "최근 1시간 내 발견된 리스크 및 자본이동 기사가 없습니다.", "color": 0x2ecc71}])
    else:
        # 중복 제거 생략 (간소화)
        final_logs = execution_logs
        
        high = [l for l in final_logs if l['score'] >= 80]
        med = [l for l in final_logs if 50 <= l['score'] < 80]
        desc = ""
        if high:
            desc += "🚨 **[핵심 리스크 / 징후]**\n"
            for l in high: desc += f"**[{l['score']}]** [{l['title']}]({l['link']})\n└ {l['reason']}\n\n"
        if med:
            desc += "🏢 **[대규모 자본이동 및 동향]**\n"
            for l in med: desc += f"**[{l['score']}]** [{l['title']}]({l['link']})\n└ {l['reason']}\n"
        
        send_discord_alert([{"title": f"📊 정기 보고 ({datetime.now(KST).strftime('%H:%M')})", "description": desc, "color": 0xe74c3c if high else 0xFFA500}])

    if not TEST_MODE: save_history(history)
    print("✅ 완료")

if __name__ == "__main__":
    main()