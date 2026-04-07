import requests
from bs4 import BeautifulSoup
import time
import json
import os
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from difflib import SequenceMatcher

# =========================================================
# [1] 환경변수 및 설정
# =========================================================
TEST_MODE = True  

NAVER_CLIENT_ID = os.environ.get("NAVER_ID")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_SECRET")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_URL")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY") 

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
# [2] 유틸리티 및 Groq 동적 모델 탐지 (🚨 핵심 추가 기능)
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
    a_clean = re.sub(r'[^가-힣a-zA-Z0-9]', '', a)
    b_clean = re.sub(r'[^가-힣a-zA-Z0-9]', '', b)
    return SequenceMatcher(None, a_clean, b_clean).ratio()

def get_active_groq_model():
    """Groq 서버에 실시간으로 접속해 현재 살아있는 모델 중 가장 좋은 것을 선택합니다."""
    if not GROQ_API_KEY: return None
    try:
        headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
        res = requests.get("https://api.groq.com/openai/v1/models", headers=headers, timeout=5)
        if res.status_code == 200:
            models_data = res.json().get('data', [])
            available_models = [m['id'] for m in models_data]
            
            # 1순위: 최신 70B, 2순위: 8B, 3순위: 믹스트랄 (살아있는 것 위주로 탐색)
            preferences = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "mixtral-8x7b-32768", "gemma2-9b-it"]
            for pref in preferences:
                if pref in available_models:
                    print(f"🤖 Groq 생존 모델 자동 선택 완료: {pref}")
                    return pref
            # 선호 모델이 다 죽었으면, 목록에 있는 첫 번째 모델 강제 선택
            if available_models:
                print(f"🤖 Groq 생존 모델 자동 선택 완료: {available_models[0]}")
                return available_models[0]
    except Exception as e:
        print(f"⚠️ 모델 탐지 실패: {e}")
    # 최후의 보루 (기본값)
    return "mixtral-8x7b-32768"

# =========================================================
# [3] 스나이퍼 필터
# =========================================================
def check_critical_patterns(title):
    politics_keywords = ["국회의원", "시의원", "도의원", "구의원", "시장", "군수", "구청장", "정치", "후보", "공천", "당선", "선거", "여당", "야당", "국회", "더불어민주당", "국민의힘"]
    if any(pol in title for pol in politics_keywords): return 0, "", False

    stock_keywords = ["주가", "상승", "하락", "급등", "급락", "증시", "코스피", "코스닥", "종목", "시황", "주식", "매수", "매도", "개미", "외인", "기관", "상장", "공모"]
    if any(stock in title for stock in stock_keywords): return 0, "", False

    local_areas = ["대구", "경북", "구미", "포항", "경주", "김천", "안동", "경산", "영천", "칠곡"]
    company_general = ["공장", "기업", "업체", "산단", "공단", "사업장", "법인", "본사", "사옥", "제조업", "신탁", "증권", "투자", "금융", "건설", "시행사", "조합", "은행", "지점"]
    figures_general = ["회장", "대표", "원장", "이사장", "총장", "임원", "지점장"]
    vip_companies = ["포스코", "포항제철", "에코프로", "엘앤에프", "대구은행", "iM뱅크", "에스엘", "화성산업", "삼보모터스", "한국가스공사", "한국수력원자력", "한수원", "성서산단", "구미산단"]
    
    issue_crime = ["횡령", "배임", "비리", "탈세", "구속", "압수수색", "기소", "입건", "수사", "송치", "체포", "의혹", "혐의", "탈루", "밀약"]
    issue_disaster = ["화재", "폭발", "붕괴", "산불"]
    issue_accident = ["사망", "숨져", "숨진", "중상", "중대재해", "추락", "끼임", "사상"]
    issue_personnel = ["인사", "전보", "승진", "발령", "내정", "프로필"]
    issue_warning = ["논란", "위기", "적자", "파업", "노조", "갈등", "소송", "재판", "항소", "벌금", "제동"]

    is_local = any(loc in title for loc in local_areas)
    is_general_company = any(comp in title for comp in company_general)
    is_vip_company = any(vip in title for vip in vip_companies)
    
    target_company_or_figure = (is_local and (is_general_company or any(fig in title for fig in figures_general))) or is_vip_company
    target_pol_pro = is_local and any(agency in title for agency in ["경찰", "검찰", "지검", "지청"])
    target_tax = (is_local and any(tax in title for tax in ["국세청", "세무서", "국세공무원"])) or ("국세청" in title)

    if target_company_or_figure:
        if any(crime in title for crime in issue_crime): return 100, "기업(인물) 범죄/의혹/수사", True
        if any(disaster in title for disaster in issue_disaster): return 100, "기업 재난(화재/폭발)", False
        if any(acc in title for acc in issue_accident): return 100, "기업 노동자 사망/중대재해", False
        if any(warn in title for warn in issue_warning): return 70, "기업 위기/갈등/소송 주의보", True
        if is_vip_company: return 50, "VIP 기업 일반 동향", True

    if target_pol_pro:
        if any(personnel in title for personnel in issue_personnel): return 100, "경찰/검찰 인사", False

    if target_tax:
        if any(crime in title for crime in issue_crime): return 100, "세무서/국세청 주요 이슈", True
        if any(personnel in title for personnel in issue_personnel): return 100, "세무서/국세청 인사", False

    return 0, "", False

# =========================================================
# [4] 수집 로직 (네이버 + 구글 RSS)
# =========================================================
def search_naver_news(keyword):
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {"X-Naver-Client-Id": NAVER_CLIENT_ID, "X-Naver-Client-Secret": NAVER_CLIENT_SECRET}
    params = {"query": keyword, "display": 15, "sort": "date"}
    try:
        return requests.get(url, headers=headers, params=params).json().get('items', [])
    except: return []

def search_google_news(keyword):
    url = f"https://news.google.com/rss/search?q={keyword}&hl=ko&gl=KR&ceid=KR:ko"
    try:
        response = requests.get(url, timeout=5)
        soup = BeautifulSoup(response.content, 'xml')
        items = soup.find_all('item')
        
        google_articles = []
        for item in items[:15]:
            google_articles.append({
                'title': item.title.text,
                'link': item.link.text,
                'pubDate': item.pubDate.text
            })
        return google_articles
    except: return []

def scrape_article(url):
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=3)
        soup = BeautifulSoup(response.text, 'html.parser')
        content = soup.select_one('#dic_area') or soup.select_one('#articeBody') or soup.select_one('.go_trans._article_content')
        return content.get_text(strip=True)[:1000] if content else None
    except: return None

# =========================================================
# [5] Groq AI 분석 로직
# =========================================================
def analyze_with_ai(title, content, forced_reason, model_name, api_status):
    if not api_status["is_alive"] or not GROQ_API_KEY or not model_name: return None
    
    prompt = f"""
    [분석 요청] 기사 제목: {title} | 기사 본문: {content[:600]} | 사전 감지: {forced_reason}

    이 기사를 읽고 아래 기준에 따라 0~100점 사이로 평가하시오.
    [🚨 80~100점] 확정된 횡령, 배임, 비리 의혹, 세금 탈루 제기 및 수사 혐의
    [⚠️ 50~79점] 의혹/재판 진행, 기업 위기(적자, 파업), VIP 기업 사업 동향
    [❌ 0점] 정치인 기사, 주식/증시(상승, 하락, 시황) 기사, 단순 안전/기부 캠페인, 타 지역 기사

    응답은 반드시 아래와 같은 순수 JSON 형태로만 작성하시오 (마크다운 기호 금지, 다른 설명 금지):
    {{
      "score": 점수숫자,
      "category": "카테고리명",
      "reason": "이유 한 줄 요약"
    }}
    """
    
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    
    # 🚨 이제 코드에 이름을 박아두지 않고, 동적으로 살아남은 모델(model_name)을 변수로 넘깁니다.
    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": "You are a helpful assistant. You must respond in valid JSON format only."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.2
    }

    max_retries = 3
    for attempt in range(max_retries):
        try:
            res = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload, timeout=10)
            
            if res.status_code != 200:
                print(f"❌ API 에러({res.status_code}): {res.text}")
                if res.status_code == 400: return None # 모델이 진짜 죽었거나 요청이 잘못되면 즉시 포기
                time.sleep(10)
                continue
            
            result_data = res.json()
            raw_text = result_data['choices'][0]['message']['content'].strip()
            
            marker = chr(96) * 3
            if f"{marker}json" in raw_text: 
                raw_text = raw_text.split(f"{marker}json")[1].split(marker)[0]
            elif marker in raw_text:
                raw_text = raw_text.split(marker)[1].split(marker)[0]
            
            return json.loads(raw_text.strip())
            
        except Exception as e:
            print(f"❌ AI 분석 에러 발생: {str(e)}")
            return None
            
    api_status["is_alive"] = False 
    return None

# =========================================================
# [6] 메인 실행 루프
# =========================================================
def main():
    print("☁️ 초고속 스나이퍼 봇 작동 시작...")
    
    # 🚨 봇이 실행될 때마다 살아있는 모델을 알아서 찾습니다.
    active_model = get_active_groq_model()
    
    if not GROQ_API_KEY:
        print("⚠️ GROQ_API_KEY가 설정되지 않아 AI 없이 파이썬 필터로만 작동합니다.")
        
    history = load_history()
    execution_logs = []  
    processed_urls = set()
    now_kst = datetime.now(KST)
    
    api_status = {"is_alive": True}

    if TEST_MODE:
        history = {"urls": [], "titles": []}
        time_threshold = now_kst - timedelta(hours=24)
    else:
        time_threshold = now_kst - timedelta(minutes=70)

    for keyword in KEYWORDS:
        raw_articles = search_naver_news(keyword) + search_google_news(keyword)
        
        for art in raw_articles:
            title = art['title'].replace('<b>','').replace('</b>','').replace('&quot;','"')
            link = art['link']
            
            if link in processed_urls or link in history["urls"]: continue
            processed_urls.add(link)

            try:
                pub_dt = parsedate_to_datetime(art['pubDate'])
                if pub_dt.tzinfo is None: pub_dt = pub_dt.replace(tzinfo=timezone.utc)
                if pub_dt < time_threshold: continue
            except: continue

            is_dup = False
            for past in history["titles"]:
                if get_similarity(title, past) > 0.8: is_dup = True; break
            if is_dup: continue 

            for log in execution_logs:
                if get_similarity(title, log['title']) > 0.8: is_dup = True; break
            if is_dup: continue

            forced_score, forced_reason, need_ai = check_critical_patterns(title)
            log_entry = {"title": title, "link": link, "score": forced_score, "category": "일반", "reason": forced_reason}

            if forced_score >= 50:
                if need_ai and api_status["is_alive"] and GROQ_API_KEY and active_model:
                    print(f"🔍 타겟 감지({forced_score}점). AI 검증 진행: {title}")
                    content = scrape_article(link) or art.get('description', '').replace('<b>','').replace('</b>','')
                    if content:
                        result = analyze_with_ai(title, content, forced_reason, active_model, api_status)
                        if result:
                            log_entry['score'] = result.get('score', 0)
                            log_entry['reason'] = result.get('reason', forced_reason)
                            if log_entry['score'] >= 80: log_entry['status'] = "ALERT"
                        elif not api_status["is_alive"]:
                            log_entry['reason'] += " (AI 응답 지연 - 파이썬 점수 유지)"
                            
                    if api_status["is_alive"]: time.sleep(1.5) 
                else:
                    print(f"⚡ [AI 패스] 안전/인사 기사 감지({forced_score}점). 즉시 통과: {title}")
                    log_entry['reason'] += " (사건/사고/인사 팩트)"
            
            execution_logs.append(log_entry)
            history["urls"].append(link)
            history["titles"].append(title)
            
    final_logs = [l for l in execution_logs if l.get('score', 0) >= 50]
    
    if not final_logs:
        requests.post(DISCORD_WEBHOOK_URL, json={
            "username": "뉴스 요약 봇",
            "embeds": [{
                "title": "🟢 뉴스 모니터링 (특이사항 없음)", 
                "description": "설정하신 핵심 타겟 관련 뉴스가 없습니다.", 
                "color": 0x2ecc71
            }]
        })
    else:
        sorted_logs = sorted(final_logs, key=lambda x: x.get('score', 0), reverse=True)
        high = [l for l in sorted_logs if l['score'] >= 80]
        med = [l for l in sorted_logs if 50 <= l['score'] < 80]
        
        color = 0xe74c3c if high else 0xFFA500
        desc = ""
        if high:
            desc += "🚨 **[핵심 리스크]**\n"
            for l in high: desc += f"**[{l['score']}]** [{l['title']}]({l['link']})\n└ {l['reason']}\n\n"
        if med:
            if high: desc += "---\n"
            desc += "⚠️ **[주의 및 동향]**\n"
            for l in med[:7]: desc += f"**[{l['score']}]** [{l['title']}]({l['link']})\n└ {l['reason']}\n"
            
        requests.post(DISCORD_WEBHOOK_URL, json={
            "username": "뉴스 요약 봇",
            "embeds": [{"title": f"📊 정기 보고 (KST {datetime.now(KST).strftime('%H:%M')})", "description": desc, "color": color}]
        })

    if not TEST_MODE: save_history(history)
    print("✅ 완료")

if __name__ == "__main__":
    main()