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

REGIONS = ["대구", "경북", "구미", "포항"]
CORE_RISKS = [
    "압수수색", "횡령", "배임", "비자금", "페이퍼컴퍼니", "분식회계", "세무조사", 
    "편법증여", "일감몰아주기", "가공거래", "역외탈세", "의견거절", "중대재해",
    "의혹", "비리", "혐의", "탈루", "구속", "밀약"
]

COMBINED_KEYWORDS = [f"{region} {risk}" for region in REGIONS for risk in CORE_RISKS]

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
        if res.status_code not in [200, 204]:
            print(f"❌ 디스코드 전송 실패: [{res.status_code}] {res.text}")
    except Exception as e:
        print(f"❌ 디스코드 전송 에러: {e}")

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

    issue_crime = [
        "횡령", "배임", "비리", "탈세", "구속", "압수수색", "기소", "입건", "송치", "체포", "장부압수", 
        "비자금", "가공거래", "허위세금계산서", "페이퍼컴퍼니", "유령법인", "해외법인송금", "조세회피처", "역외거래", "수출단가조작", 
        "분식회계", "의견거절", "회계처리기준위반", "세무조사", "특별세무조사", "조사4국", "검찰고발", "금융감독원",
        "의혹", "혐의", "탈루", "밀약"
    ]
    issue_finance = [
        "가업승계", "편법증여", "일감몰아주기", "일감 몰아주기", "지분매각", "자녀회사",
        "전환사채", "CB", "신주인수권부사채", "BW", "유상증자", "자사주매입", 
        "비상장주식", "주식저가양도", "우회상장", "내부회계관리", "자본잠식", "차입금", "내사", "포착"
    ]
    issue_disaster = ["화재", "폭발", "붕괴", "산불", "사망", "중대재해", "끼임", "추락", "누출"]
    issue_personnel = ["인사", "전보", "승진", "발령", "내정", "프로필"]
    issue_warning = ["논란", "위기", "적자", "파업", "노조", "갈등", "소송", "재판", "항소", "벌금", "제동", "승계", "지배구조"]

    has_critical_risk = any(word in title for word in issue_crime + issue_finance + issue_disaster)
    if not has_critical_risk:
        if any(pol in title for pol in politics_keywords): return 0, "", False
        if any(stock in title for stock in stock_keywords): return 0, "", False

    local_areas = ["대구", "경북", "구미", "포항", "경주", "김천", "안동", "경산", "영천", "칠곡", "성서산단", "국가산단"]
    company_general = ["공장", "기업", "업체", "산단", "공단", "사업장", "법인", "본사", "사옥", "제조업", "신탁", "증권", "투자", "자동차부품사", "이차전지", "섬유업체", "계열사", "자회사"]
    figures_general = ["회장", "대표", "원장", "이사장", "총장", "임원", "지점장", "오너일가", "특수관계인"]

    is_local = any(loc in title for loc in local_areas)
    is_general_company = any(comp in title for comp in company_general)
    is_vip_company = any(vip in title for vip in VIP_COMPANIES_KR)
    
    target_company_or_figure = (is_local and (is_general_company or any(fig in title for fig in figures_general))) or is_vip_company
    target_pol_pro = is_local and any(agency in title for agency in ["경찰", "검찰", "지검", "지청", "공소청", "중수청", "국가수사본부"])
    target_tax = (is_local and any(tax in title for tax in ["국세청", "세무서", "국세공무원"])) or ("국세청" in title)

    if target_company_or_figure:
        if any(crime in title for crime in issue_crime): return 100, "세무/재무/범죄 리스크 포착", True
        if any(fin in title for fin in issue_finance): return 80, "지배구조/자본거래 징후 포착", True
        if any(disaster in title for disaster in issue_disaster): return 100, "기업 재난/사고(화재 등)", False
        if any(warn in title for warn in issue_warning): return 70, "기업 위기/갈등/소송", True

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
        if response.status_code != 200: return []
        soup = BeautifulSoup(response.content, 'xml')
        return [{'title': item.title.text, 'link': item.link.text, 'pubDate': item.pubDate.text, 'lang': lang} for item in soup.find_all('item')[:10]]
    except: return []

def scrape_article(url):
    try:
        response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=3)
        soup = BeautifulSoup(response.text, 'html.parser')
        content = soup.select_one('#dic_area') or soup.select_one('#articeBody') or soup.select_one('.go_trans._article_content')
        if not content and 'news.google' not in url: content = soup.find('body')
        return content.get_text(strip=True)[:1000] if content else None
    except: return None

# =========================================================
# [5] Groq AI 분석 로직
# =========================================================
def analyze_with_ai(title, content, forced_reason, lang, model_name, api_status):
    if not api_status["is_alive"] or not GROQ_API_KEY or not model_name: return None
    
    system_instr = "You are a news risk analyst for a regional tax authority. Respond in JSON only."
    
    if lang == 'en':
        prompt = f"""
        [GLOBAL NEWS ANALYSIS] Title: {title} | Snippet: {content[:800]}
        1. Translate the core event to Korean.
        2. Score 0-100: [🚨 80-100] Crisis/Financial crime. [⚠️ 50-79] M&A, Strategy. [❌ 0] Stock market, simple PR.
        JSON format: {{ "score": 50, "category": "글로벌 동향", "reason": "한국어 요약" }}
        """
    else:
        prompt = f"""
        [국내 뉴스 분석] 기사 제목: {title} | 본문: {content[:600]}
        당신은 국세청 조사국을 위한 자본시장/기업 리스크 감별사입니다.
        
        평가 기준:
        [🚨 80~100점] 비자금, 가공거래, 페이퍼컴퍼니, 편법증여, 일감몰아주기, CB/BW 꼼수발행 등 세금 탈루 및 지배구조 의혹, 세무조사/압수수색 징후
        [⚠️ 50~79점] 단순 경영권 갈등, 기업 위기, 화재/사고/재난, 금감원 공시
        [❌ 0점] 주식/증시 시황, 단순 실적발표, 정치인 가십, 단순 기부
        
        포맷: {{ "score": 점수, "category": "카테고리명", "reason": "세무/재무/지배구조/사고 리스크 중심 핵심 요약" }}
        """
    
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": model_name, "messages": [{"role": "system", "content": system_instr}, {"role": "user", "content": prompt}], "temperature": 0.2}

    try:
        res = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload, timeout=12)
        if res.status_code == 200:
            raw_text = res.json()['choices'][0]['message']['content'].strip()
            marker = chr(96) * 3
            if f"{marker}json" in raw_text: raw_text = raw_text.split(f"{marker}json")[1].split(marker)[0]
            elif marker in raw_text: raw_text = raw_text.split(marker)[1].split(marker)[0]
            return json.loads(raw_text.strip())
    except: pass
    return None

def deduplicate_with_ai_desk(logs, model_name):
    if len(logs) <= 1 or not GROQ_API_KEY or not model_name: return logs
    print(f"🤖 AI 국장 데스킹 진행 중... (총 {len(logs)}개 기사 검토)")
    
    prompt = "뉴스 목록:\n"
    for i, log in enumerate(logs): prompt += f"[{i}] {log['title']} | 요약: {log['reason']}\n"
    prompt += "동일한 사건/의혹을 다룬 중복 기사들을 찾아 대표 1개만 남기고, 독립적인 사건들은 모두 남기시오. 인덱스 번호를 JSON 리스트로 응답하시오: [{\"index\": 0}, {\"index\": 1}]"
    
    try:
        res = requests.post("https://api.groq.com/openai/v1/chat/completions", headers={"Authorization": f"Bearer {GROQ_API_KEY}"}, 
                           json={"model": model_name, "messages": [{"role": "user", "content": prompt}], "temperature": 0.1}, timeout=15)
        if res.status_code == 200:
            raw_text = res.json()['choices'][0]['message']['content'].strip()
            marker = chr(96) * 3
            if marker in raw_text: raw_text = re.sub(f'{marker}(json)?|{marker}', '', raw_text)
            parsed = json.loads(raw_text.strip())
            return [logs[item['index']] for item in parsed if 'index' in item and 0 <= item['index'] < len(logs)]
    except: pass
    return logs

# =========================================================
# [7] 메인 실행 루프 (🚨 속도 최적화 - 투트랙 엔진)
# =========================================================
def main():
    print("☁️ 글로벌 & 재무 리스크 스나이퍼 봇 작동 시작...")
    active_model = get_active_groq_model()
    history = load_history()
    execution_logs = []  
    processed_urls = set()
    now_kst = datetime.now(KST)
    api_status = {"is_alive": True}

    time_threshold = now_kst - (timedelta(hours=24) if TEST_MODE else timedelta(minutes=75))
    articles_all = []
    
    kr_count = 0
    en_count = 0

    # 🚀 [1단계] 네이버 초고속 수집 (공식 API라 딜레이 거의 없이 돌파)
    print(f"\n⚡ [1단계] 네이버 초고속 엔진 가동... (키워드 {len(KEYWORDS_KR)}개)")
    for kw in KEYWORDS_KR:
        fetched = search_naver_news(kw)
        articles_all += fetched
        kr_count += len(fetched)
        time.sleep(0.05) # 0.05초로 극강의 속도! (약 5초만에 전체 스캔 완료)

    # 🛡️ [2단계] 구글 국내 안전 수집 (차단 방지를 위해 0.4초 딜레이)
    print(f"\n🛡️ [2단계] 구글 스텔스 엔진 가동 (국내)... (키워드 {len(KEYWORDS_KR)}개)")
    for kw in KEYWORDS_KR:
        fetched = search_google_news(kw, lang='ko')
        articles_all += fetched
        kr_count += len(fetched)
        time.sleep(0.4) 

    # 🌍 [3단계] 구글 외신 안전 수집
    print(f"\n🌍 [3단계] 구글 스텔스 엔진 가동 (외신)... (키워드 {len(KEYWORDS_GLOBAL)}개)")
    for kw in KEYWORDS_GLOBAL:
        fetched = search_google_news(kw, lang='en')
        articles_all += fetched
        en_count += len(fetched)
        time.sleep(0.4) 

    print(f"\n🎯 [탐지 결과] 국내 기사 {kr_count}건 / 외신 기사 {en_count}건 수집 완료")
    print("⏳ 기사 선별 및 AI(조사관 모드) 분석을 시작합니다...\n")

    for art in articles_all:
        title = art['title'].replace('<b>','').replace('</b>','').replace('&quot;','"')
        link = art['link']
        lang = art.get('lang', 'ko')
        
        if link in processed_urls or link in history["urls"]: continue
        processed_urls.add(link)

        try:
            pub_dt = parsedate_to_datetime(art['pubDate'])
            if pub_dt.tzinfo is None: pub_dt = pub_dt.replace(tzinfo=timezone.utc)
            if pub_dt < time_threshold: continue
        except: continue

        if any(get_similarity(title, p) > 0.85 for p in history["titles"] + [l['title'] for l in execution_logs]): continue

        forced_score, forced_reason, need_ai = check_critical_patterns(title)
        
        if lang == 'en': need_ai = True; forced_score = 50; forced_reason = "글로벌 VIP 동향"

        log_entry = {"title": title, "link": link, "score": forced_score, "reason": forced_reason, "lang": lang}

        if forced_score >= 50 or lang == 'en':
            if need_ai and api_status["is_alive"] and active_model:
                print(f"🔍 분석 진행 ({'외신' if lang == 'en' else '국내'}): {title[:40]}...")
                content = scrape_article(link) or art.get('description', '')
                if content:
                    result = analyze_with_ai(title, content, forced_reason, lang, active_model, api_status)
                    if result:
                        log_entry['score'] = result.get('score', 0)
                        log_entry['reason'] = result.get('reason', forced_reason)
                        if lang == 'en': log_entry['title'] = "🌐 (외신) " + log_entry['title']
                time.sleep(1.2)
            
            if log_entry['score'] >= 50:
                execution_logs.append(log_entry)
                history["urls"].append(link)
                history["titles"].append(title)
            
    final_logs = deduplicate_with_ai_desk(execution_logs, active_model)
    
    if not final_logs:
        embed = {"title": "🟢 뉴스 모니터링 (이상 없음)", "description": "최근 1시간 내 특이 리스크가 발견되지 않았습니다.", "color": 0x2ecc71}
        if TEST_MODE: embed["title"] = "🛠️ [테스트] " + embed["title"]
        send_discord_alert([embed])
    else:
        high = [l for l in final_logs if l['score'] >= 80]
        med = [l for l in final_logs if 50 <= l['score'] < 80]
        desc = ""
        if high:
            desc += "🚨 **[재무/수사 핵심 리스크]**\n"
            for l in high: desc += f"**[{l['score']}]** [{l['title']}]({l['link']})\n└ {l['reason']}\n\n"
        if med:
            if high: desc += "---\n"
            desc += "⚠️ **[동향 및 징후 주의]**\n"
            for l in med: desc += f"**[{l['score']}]** [{l['title']}]({l['link']})\n└ {l['reason']}\n"
            
        title_str = f"📊 정기 보고 (KST {datetime.now(KST).strftime('%H:%M')})"
        if TEST_MODE: title_str = "🛠️ [테스트] " + title_str
        
        send_discord_alert([{"title": title_str, "description": desc, "color": 0xe74c3c if high else 0xFFA500}])

    if not TEST_MODE: save_history(history)
    print("✅ 완료")

if __name__ == "__main__":
    main()