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

# 🚨 [검색어 망]
REGIONS = ["대구", "경북", "구미", "포항"]
CORE_RISKS = [
    "압수수색", "횡령", "배임", "비자금", "페이퍼컴퍼니", "분식회계", "세무조사", 
    "편법증여", "일감몰아주기", "가공거래", "역외탈세", "의견거절", "중대재해",
    "의혹", "비리", "혐의", "탈루", "구속", "밀약"
]
CORE_INVESTMENTS = ["투자협약", "MOU", "신공장", "건립", "M&A", "인수합병", "대규모 수주", "테크노폴리스"]
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

# 🚨 신규: 지역 언론 특화 검색어 대폭 확장 (수출, 토지, 무역 등 돈 냄새가 나는 단어 추가)
LOCAL_MEDIA_NAMES = ["영남일보", "매일신문", "대구일보", "경북일보", "경북도민일보", "TBC"]
LOCAL_TOPICS = ["경제", "기업", "산업단지", "투자", "부동산", "수출", "무역", "토지", "상공회의소", "테크노파크"]
KEYWORDS_LOCAL_MEDIA = [f"{media} {topic}" for media in LOCAL_MEDIA_NAMES for topic in LOCAL_TOPICS]

HISTORY_FILE = "news_history.json"
KST = timezone(timedelta(hours=9))

# =========================================================
# [2] 디스코드 전송 도우미 & 유틸리티
# =========================================================
def send_discord_alert(embeds):
    if not DISCORD_WEBHOOK_URL: return
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"username": "뉴스 요약 봇", "embeds": embeds})
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
# [3] 스나이퍼 필터 (🚨 쓰레기(-1)와 중립(0)의 완벽한 분리)
# =========================================================
def check_critical_patterns(title):
    # 1. 절대 쓰레기 단어
    sports_keywords = ["프로농구", "KBL", "프로야구", "KBO", "프로축구", "K리그", "감독", "선수", "득점", "리바운드", "홈런", "페가수스", "라이온즈", "대구FC", "실내체육관", "끝내기", "결승", "스포츠", "MVP", "매치"]
    politics_keywords = ["국회의원", "시의원", "도의원", "구의원", "시장", "군수", "구청장", "도지사", "정치", "후보", "공천", "당선", "선거", "여당", "야당", "국회", "민주당", "국민의힘", "우세", "추격", "경선", "여론조사", "지지율", "출마", "득표", "총선", "지선", "대선"]
    stock_keywords = ["주가", "상승", "하락", "급등", "급락", "증시", "코스피", "코스닥", "종목", "시황", "주식", "매수", "매도", "개미", "외인", "기관", "상장", "공모"]

    issue_crime = ["횡령", "배임", "비리", "탈세", "구속", "압수수색", "기소", "입건", "송치", "체포", "비자금", "가공거래", "허위세금계산서", "페이퍼컴퍼니", "의혹", "혐의", "탈루", "밀약"]
    issue_finance = ["가업승계", "편법증여", "일감몰아주기", "일감 몰아주기", "지분매각", "전환사채", "CB", "신주인수권부사채", "BW", "비상장주식", "우회상장", "자본잠식"]
    issue_disaster = ["화재", "폭발", "붕괴", "산불", "사망", "중대재해", "끼임", "추락", "누출"]
    issue_personnel = ["인사", "전보", "승진", "발령", "내정", "프로필"]
    issue_warning = ["논란", "위기", "적자", "파업", "노조", "소송", "재판", "승계", "지배구조"]
    issue_investment = ["투자협약", "MOU", "신공장", "팩토리", "건립", "신설", "M&A", "인수합병", "대규모 수주", "투자 유치", "자금 조달", "테크노폴리스"]

    # 🚨 [절대 방어선] -1점 처리하여 지역 언론이라도 칼같이 버립니다.
    if any(sport in title for sport in sports_keywords): return -1, "", False
    
    has_crime_risk = any(word in title for word in issue_crime)
    has_finance_risk = any(word in title for word in issue_finance)
    
    if any(pol in title for pol in politics_keywords) and not has_crime_risk: return -1, "", False
    if any(stock in title for stock in stock_keywords) and not (has_crime_risk or has_finance_risk): return -1, "", False

    local_areas = ["대구", "경북", "구미", "포항", "경주", "성서산단", "국가산단", "테크노폴리스"]
    company_general = ["공장", "기업", "업체", "산단", "공단", "사업장", "법인", "본사", "자동차부품사", "이차전지", "계열사", "제조"]
    figures_general = ["회장", "대표", "임원", "오너일가", "특수관계인"]

    is_local = any(loc in title for loc in local_areas)
    is_general_company = any(comp in title for comp in company_general)
    is_vip_company = any(vip in title for vip in VIP_COMPANIES_KR)
    
    target_company_or_figure = (is_local and (is_general_company or any(fig in title for fig in figures_general))) or is_vip_company
    target_pol_pro = is_local and any(agency in title for agency in ["경찰", "검찰", "지검", "공소청", "중수청", "수사본부"])
    target_tax = (is_local and any(tax in title for tax in ["국세청", "세무서"])) or ("국세청" in title)

    # 4. 점수 할당
    if target_company_or_figure:
        if any(crime in title for crime in issue_crime): return 100, "[세무/재무]", True
        if any(fin in title for fin in issue_finance): return 80, "[자본이동]", True
        if any(disaster in title for disaster in issue_disaster): return 100, "[사고/재난]", False
        if any(warn in title for warn in issue_warning): return 70, "[경영/갈등]", True
        if any(inv in title for inv in issue_investment): return 70, "[자본이동]", True

    if target_pol_pro:
        if any(personnel in title for personnel in issue_personnel): return 100, "[사법/인사]", False

    if target_tax:
        if any(crime in title for crime in issue_crime): return 100, "[세무/재무]", True
        if any(personnel in title for personnel in issue_personnel): return 100, "[사법/인사]", False

    # 범죄/투자 등은 아니지만 일반적인 경제 기사인 경우 (0점 반환)
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
    url = f"https://news.google.com/rss/search?q={safe_keyword}&hl=ko&gl=KR&ceid=KR:ko"
    try:
        response = requests.get(url, headers=headers, timeout=5)
        soup = BeautifulSoup(response.content, 'xml')
        return [{'title': item.title.text, 'link': item.link.text, 'pubDate': item.pubDate.text} for item in soup.find_all('item')[:10]]
    except: return []

def search_google_news_en(keyword):
    headers = {'User-Agent': 'Mozilla/5.0'}
    safe_keyword = urllib.parse.quote_plus(keyword)
    url = f"https://news.google.com/rss/search?q={safe_keyword}&hl=en-US&gl=US&ceid=US:en"
    try:
        response = requests.get(url, headers=headers, timeout=5)
        soup = BeautifulSoup(response.content, 'xml')
        return [{'title': item.title.text, 'link': item.link.text, 'pubDate': item.pubDate.text} for item in soup.find_all('item')[:10]]
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
# [5] AI 데스킹 로직
# =========================================================
def deduplicate_with_ai_desk(logs, model_name):
    if len(logs) <= 1 or not GROQ_API_KEY or not model_name: return logs
    print(f"\n🤖 AI 국장 데스킹 진행 중... (총 {len(logs)}개 기사 통폐합)")
    
    prompt = "목록 중 '동일한 사건/이슈'를 다룬 중복 기사들을 찾아 대표 1개만 남기고, 독립적인 사건들은 모두 남기시오. 인덱스 번호를 JSON 배열로 응답: [{\"index\": 0}, {\"index\": 1}]\n뉴스 목록:\n"
    for i, log in enumerate(logs): prompt += f"[{i}] {log['title']}\n"
    
    try:
        res = requests.post("https://api.groq.com/openai/v1/chat/completions", headers={"Authorization": f"Bearer {GROQ_API_KEY}"}, 
                           json={"model": model_name, "messages": [{"role": "system", "content": "You are a data deduping AI. Output JSON array only."}, {"role": "user", "content": prompt}], "temperature": 0.0}, timeout=15)
        if res.status_code == 200:
            raw_text = res.json()['choices'][0]['message']['content'].strip()
            marker = chr(96) * 3
            if marker in raw_text: raw_text = re.sub(f'{marker}(json)?|{marker}', '', raw_text)
            parsed = json.loads(raw_text.strip())
            deduped = [logs[item['index']] for item in parsed if 'index' in item and 0 <= item['index'] < len(logs)]
            if deduped: return deduped
    except: pass
    return logs

# =========================================================
# [6] 메인 실행 루프
# =========================================================
def main():
    print("☁️ 스나이퍼 봇 작동 시작 (지역언론 거시경제 심층 탐지)...")
    active_model = get_active_groq_model()
    history = load_history()
    execution_logs = []  
    raw_articles = []
    unique_links = set()
    now_kst = datetime.now(KST)

    print(f"\n⚡ [1단계] 국내 핵심 타겟 수집 중... (키워드 {len(KEYWORDS_KR)}개)")
    for kw in KEYWORDS_KR:
        for it in search_naver_news(kw) + search_google_news(kw):
            it['track'] = 'kr'
            link = it.get('link') or it.get('originallink')
            if link and link not in unique_links:
                unique_links.add(link)
                raw_articles.append(it)
        time.sleep(0.05)
    
    print(f"🌍 [2단계] 글로벌 외신 수집 중... (키워드 {len(KEYWORDS_GLOBAL)}개)")
    for kw in KEYWORDS_GLOBAL:
        for it in search_google_news_en(kw):
            it['track'] = 'en'
            if it['link'] not in unique_links:
                unique_links.add(it['link'])
                raw_articles.append(it)
        time.sleep(0.4)

    print(f"📰 [3단계] 지역 언론(영남일보 등) 전용망 수집 중... (키워드 {len(KEYWORDS_LOCAL_MEDIA)}개)")
    for kw in KEYWORDS_LOCAL_MEDIA:
        for it in search_naver_news(kw):
            it['track'] = 'local'
            link = it.get('link') or it.get('originallink')
            if link and link not in unique_links:
                unique_links.add(link)
                raw_articles.append(it)
        time.sleep(0.05)

    print(f"\n📊 [수집 결과] 총 검색된 고유 기사: {len(raw_articles)}건")
    
    time_threshold = now_kst - (timedelta(hours=24) if TEST_MODE else timedelta(minutes=75))
    valid_articles = []
    
    for art in raw_articles:
        title = art['title'].replace('<b>','').replace('</b>','').replace('&quot;','"')
        link = art.get('link') or art.get('originallink')
        track = art['track']

        try:
            pub_dt = parsedate_to_datetime(art['pubDate'])
            if pub_dt.tzinfo is None: pub_dt = pub_dt.replace(tzinfo=timezone.utc)
            if pub_dt < time_threshold: continue
            if link in history["urls"]: continue
            if any(get_similarity(title, t) > 0.85 for t in history["titles"]): continue
        except: continue

        score, reason, need_ai = check_critical_patterns(title)
        
        # 🚨 절대 쓰레기(-1점)는 무조건 폐기
        if score == -1:
            continue

        if track == 'en': 
            score = 50; need_ai = True 
        elif track == 'local':
            # 🚨 지역 언론은 쓰레기(-1)만 아니면(0점이어도) AI 검토를 받게 살려줍니다.
            if score < 70:
                score = 60; need_ai = True; reason = "[지역언론 확인용]"
        elif score == 0:
            continue # 국내 일반 트랙인데 0점(매칭 안됨)이면 버림

        if score >= 50:
            valid_articles.append({'title': title, 'link': link, 'score': score, 'reason': reason, 'track': track, 'need_ai': need_ai, 'raw': art})

    print(f"   - 최근 1시간 이내 타겟 기사: {len(valid_articles)}건")
    print(f"⏳ 이제 {len(valid_articles)}건의 기사에 대해 AI 정밀 태그 부여를 시작합니다...\n")

    api_status = {"is_alive": True}
    
    for v in valid_articles:
        if v['need_ai'] and api_status["is_alive"] and active_model:
            print(f"🔍 AI 분석 중: {v['title'][:40]}...")
            
            scraped_text = scrape_article(v['link'])
            full_content = scraped_text[:800] if scraped_text else v['raw'].get('description', '')[:500]
            
            system_instr = "You are a categorical tagging AI. Respond in JSON only."
            
            if v['track'] == 'en':
                prompt = f"""[GLOBAL NEWS TAGGING] Title: {v['title']} | Content: {full_content}
                1. Score (0-100). 2. Choose ONE tag: [글로벌동향], [자본이동], [사고/재난], [경영/갈등].
                Format: {{ "score": 50, "category": "Global", "reason": "[글로벌동향]" }}"""
            
            elif v['track'] == 'local' and v['score'] == 60:
                # 🚨 지역 언론 태그 및 점수 기준 완전 개편 (거시경제, 토지 집중)
                prompt = f"""[지역 언론 경제/정책 분석] 제목: {v['title']} | 본문: {full_content}
                지시사항:
                1. 대구/경북 지역의 의미 있는 '거시경제(수출/물가), 부동산/토지, 지역기업 동향(실적/애로사항), 산단 개발, 지자체 정책' 뉴스라면 65점을 부여하세요. (예: 수출 부진, 외국인 토지 증가 등)
                2. 단순 날씨, 교통사고, 미담, 행사 안내, 단순 가십은 무조건 0점을 부여하여 폐기하세요.
                3. 65점을 줄 경우, 아래 4개 태그 중 1개만 복사해서 출력. 문장 작성 절대 금지.
                [거시경제], [부동산/토지], [지역기업동향], [지자체정책]
                포맷: {{ "score": 65, "category": "분류", "reason": "[거시경제]" }}"""

            else:
                prompt = f"""[국내 기사 태그 분류] 제목: {v['title']} | 본문: {full_content}
                1. '점수(score)'는 국세청 관점 리스크로.
                2. '요약(reason)'은 기사와 가장 잘 맞는 아래 6개 태그 중 딱 1개만 복사해서 출력. 문장 작성 금지.
                [세무/재무], [자본이동], [사고/재난], [경영/갈등], [사법/인사], [일반동향]
                포맷: {{ "score": 85, "category": "분류", "reason": "[세무/재무]" }}"""

            headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
            try:
                res = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json={"model": active_model, "messages": [{"role": "system", "content": system_instr}, {"role": "user", "content": prompt}], "temperature": 0.1}, timeout=12)
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

    final_logs = deduplicate_with_ai_desk(execution_logs, active_model)
    if not final_logs: final_logs = execution_logs 

    if not final_logs:
        send_discord_alert([{"title": "🟢 뉴스 모니터링 (이상 없음)", "description": "최근 1시간 내 발견된 타겟 기사가 없습니다.", "color": 0x2ecc71}])
    else:
        high = [l for l in final_logs if l['score'] >= 80]
        med = [l for l in final_logs if 70 <= l['score'] < 80]
        local_news = [l for l in final_logs if 50 <= l['score'] < 70] 
        
        desc = ""
        if high:
            desc += "🚨 **[핵심 리스크 / 징후]**\n"
            for l in high: desc += f"**[{l['score']}점]** {l['reason']} [{l['title']}]({l['link']})\n\n"
        if med:
            desc += "🏢 **[대규모 자본이동 및 동향]**\n"
            for l in med: desc += f"**[{l['score']}점]** {l['reason']} [{l['title']}]({l['link']})\n\n"
        if local_news:
            desc += "📰 **[지역 언론 주요 경제/정책]**\n"
            for l in local_news: desc += f"**[{l['score']}점]** {l['reason']} [{l['title']}]({l['link']})\n\n"
        
        send_discord_alert([{"title": f"📊 정기 보고 ({datetime.now(KST).strftime('%H:%M')})", "description": desc, "color": 0xe74c3c if high else 0xFFA500}])

    if not TEST_MODE: save_history(history)
    print("✅ 완료")

if __name__ == "__main__":
    main()