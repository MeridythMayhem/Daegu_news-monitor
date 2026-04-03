import requests
from bs4 import BeautifulSoup
import time
import json
import os
import re
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
# [2] 기억력 및 중복 제거 유틸리티
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
    # 특수문자 및 공백을 제거하고 순수 한글/영어/숫자만 비교하여 정확도 향상
    a_clean = re.sub(r'[^가-힣a-zA-Z0-9]', '', a)
    b_clean = re.sub(r'[^가-힣a-zA-Z0-9]', '', b)
    return SequenceMatcher(None, a_clean, b_clean).ratio()

# =========================================================
# [3] 스나이퍼 필터 (정치/주식 차단 및 선택적 AI)
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
# [4] 수집 로직 (네이버 + 구글 RSS 쌍끌이)
# =========================================================
def search_naver_news(keyword):
    url = "[https://openapi.naver.com/v1/search/news.json](https://openapi.naver.com/v1/search/news.json)"
    headers = {"X-Naver-Client-Id": NAVER_CLIENT_ID, "X-Naver-Client-Secret": NAVER_CLIENT_SECRET}
    params = {"query": keyword, "display": 15, "sort": "date"}
    try:
        return requests.get(url, headers=headers, params=params).json().get('items', [])
    except: return []

def search_google_news(keyword):
    url = f"[https://news.google.com/rss/search?q=](https://news.google.com/rss/search?q=){keyword}&hl=ko&gl=KR&ceid=KR:ko"
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
# [5] AI 분석 로직 (서킷 브레이커 포함)
# =========================================================
def analyze_with_ai(title, content, forced_reason, model_name, api_status):
    if not api_status["is_alive"] or not model_name: return None
    
    prompt = f"""
    [분석 요청] 제목: {title} | 본문: {content[:600]} | 사전 감지: {forced_reason}
    0~100점 사이로 평가하시오. [🚨 80~100] 확정된 횡령, 배임, 비리 수사 혐의 [⚠️ 50~79] 의혹/재판, 기업 위기, VIP 사업 동향
    [❌ 0점] 정치인 기사, 주식 등락 시황, 단순 안전 캠페인, 타 지역 뉴스
    JSON 응답: {{ "score": 점수, "category": "카테고리명", "reason": "이유 한 줄 요약" }}
    """
    
    safety_settings = [{"category": c, "threshold": "BLOCK_NONE"} for c in ["HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH", "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT"]]
    
    try:
        model = genai.GenerativeModel(model_name)
        res = model.generate_content(prompt, safety_settings=safety_settings, generation_config={"response_mime_type": "application/json"})
        raw_text = res.text.strip()
        
        # UI 깨짐 방지를 위해 마커 변수를 우회해서 사용합니다.
        marker = "`" * 3
        if f"{marker}json" in raw_text: 
            raw_text = raw_text.split(f"{marker}json")[1].split(marker)[0]
            
        return json.loads(raw_text)
    except Exception as e:
        if any(err in str(e) for err in ["429", "Quota", "503", "500"]):
            print(f"⏳ 구글 AI 서버 지연. 이번 실행 동안 AI 모드를 제한합니다.")
            api_status["is_alive"] = False
        return None

# =========================================================
# [6] 메인 실행 루프
# =========================================================
def main():
    print("☁️ 쌍끌이 수집 & 중복 최적화 봇 작동 시작...")
    
    genai.configure(api_key=GOOGLE_API_KEY)
    ai_model_name = None
    try:
        valid_models = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        for pref in ['models/gemini-2.5-flash', 'models/gemini-1.5-flash', 'models/gemini-pro']:
            if pref in valid_models: ai_model_name = pref.replace('models/', ''); break
    except: pass

    history = load_history()
    execution_logs = []  
    processed_urls = set()
    now_kst = datetime.now(KST)
    api_status = {"is_alive": True}
    
    time_threshold = now_kst - (timedelta(hours=24) if TEST_MODE else timedelta(minutes=70))

    for keyword in KEYWORDS:
        raw_articles = search_naver_news(keyword) + search_google_news(keyword)
        
        for art in raw_articles:
            title = art['title'].replace('<b>','').replace('</b>','').replace('&quot;','"')
            link = art['link']
            
            # 최적화 1: URL 중복 제거
            if link in processed_urls or link in history["urls"]: continue
            processed_urls.add(link)

            try:
                pub_dt = parsedate_to_datetime(art['pubDate'])
                if pub_dt.tzinfo is None: pub_dt = pub_dt.replace(tzinfo=timezone.utc)
                if pub_dt < time_threshold: continue
            except: continue

            # 최적화 2: 과거 기록 유사도 검사
            is_dup = False
            for past in history["titles"]:
                if get_similarity(title, past) > 0.8:
                    is_dup = True
                    break
            if is_dup: continue 

            # 최적화 3: 이번 실행 내 중복 검사 (구글-네이버 교차 중복 방지)
            for log in execution_logs:
                if get_similarity(title, log['title']) > 0.8:
                    is_dup = True
                    break
            if is_dup: continue

            forced_score, forced_reason, need_ai = check_critical_patterns(title)
            log_entry = {"title": title, "link": link, "score": forced_score, "category": "일반", "reason": forced_reason}

            if forced_score >= 50:
                if need_ai and api_status["is_alive"]:
                    print(f"🔍 타겟 감지: {title}")
                    content = scrape_article(link) or art.get('description', '').replace('<b>','').replace('</b>','')
                    if content:
                        result = analyze_with_ai(title, content, forced_reason, ai_model_name, api_status)
                        if result:
                            log_entry['score'] = result.get('score', 0)
                            log_entry['reason'] = result.get('reason', forced_reason)
                    time.sleep(4) 
                else:
                    log_entry['reason'] += " (즉시 통과)"
            
            execution_logs.append(log_entry)
            history["urls"].append(link)
            history["titles"].append(title)
            
    # 최종 보고 및 저장
    final_logs = [l for l in execution_logs if l.get('score', 0) >= 50]
    if final_logs:
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
    print("✅ 모든 작업 완료")

if __name__ == "__main__":
    main()