import requests
from bs4 import BeautifulSoup
from newspaper import Article
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

HISTORY_FILE = "bot_history.json"
MAX_HISTORY = 1000

# [핵심] 수집 키워드 (의혹, 탈루, 혐의 관련 정밀화)
KEYWORDS = [
    "대구 압수수색", "경북 압수수색", "대구 공장 화재", "경북 공장 화재", 
    "대구 중대재해", "경북 중대재해", "대구 횡령", "경북 횡령",
    "대구 기업 의혹", "경북 기업 의혹", "대구 기업 탈루", "경북 기업 탈루", "대구 기업 혐의", "경북 기업 혐의",
    "포스코", "포항제철소", "에코프로", "엘앤에프", "iM뱅크", "대구은행", 
    "대구지방국세청", "대구 세무서", "경북 세무서", "국세청",
    "대구경찰청 인사", "경북경찰청 인사", "대구지검 인사", "대구지검 전보"
]

# =========================================================
# [2] 상태 관리 (JSON DB)
# =========================================================
def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return set(data.get("processed_urls", [])), data.get("seen_titles", [])
        except:
            return set(), []
    return set(), []

def save_history(processed_urls, seen_titles):
    data = {
        "processed_urls": list(processed_urls)[-MAX_HISTORY:],
        "seen_titles": seen_titles[-MAX_HISTORY:]
    }
    try:
        with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"기록 저장 실패: {e}")

# =========================================================
# [3] AI 및 유틸리티
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
# [4] 스나이퍼 필터 (1차망)
# =========================================================
def check_critical_patterns(title):
    local_areas = ["대구", "경북", "구미", "포항", "경주", "김천", "안동", "경산", "영천", "칠곡"]
    company_general = ["공장", "기업", "업체", "산단", "공단", "사업장", "법인", "본사", "사옥", "제조업"]
    vip_companies = ["포스코", "포항제철", "에코프로", "엘앤에프", "대구은행", "iM뱅크", "에스엘", "화성산업", "삼보모터스", "한국가스공사", "한국수력원자력", "한수원", "성서산단", "구미산단"]
    
    agencies_police_prosecutor = ["경찰", "검찰", "지검", "지청"]
    agencies_tax = ["국세청", "세무서", "국세공무원"]

    # 혐의, 의혹, 탈루 키워드 반영
    issue_crime = ["횡령", "배임", "비리", "탈세", "구속", "압수수색", "기소", "입건", "수사", "송치", "체포", "의혹", "탈루", "혐의"]
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
        if any(crime in title for crime in issue_crime): return 100, "1. 대구/경북 기업 범죄/의혹/수사 이슈"
        if any(disaster in title for disaster in issue_disaster): return 100, "2. 대구/경북 기업 재난(화재/폭발) 이슈"
        if any(acc in title for acc in issue_accident): return 100, "3. 대구/경북 기업 노동자 사망/중대재해"

    if target_pol_pro:
        if any(personnel in title for personnel in issue_personnel): return 100, "4. 대구/경북 경찰/검찰 인사 소식"

    if target_tax:
        if any(crime in title for crime in issue_crime + issue_accident) or any(personnel in title for personnel in issue_personnel):
            return 100, "5. 대구/경북 세무서 및 국세청 주요 이슈"

    return 0, ""

# =========================================================
# [5] 알림 및 보고
# =========================================================
def send_alert_discord(title, reason, link, category, score):
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
                "footer": {"text": f"Score: {score} | Critical Alert"}
            }]
        }
        requests.post(DISCORD_WEBHOOK_URL, json=data)
    except: pass

def send_hourly_report(logs):
    valid_logs = [l for l in logs if l.get('score', 0) > 0]
    if not valid_logs: return
    
    sorted_logs = sorted(valid_logs, key=lambda x: x.get('score', 0), reverse=True)
    high_risks = [l for l in sorted_logs if l.get('score', 0) >= 80 and l['status'] == 'ALERT']
    
    if high_risks:
        title = f"🚨 정기 보고 (핵심 타겟 {len(high_risks)}건)"
        description = ""
        for log in high_risks:
            description += f"🔥 **[{log['score']}점]** {log['title']}\n└ {log['reason']}\n\n"
        color = 0xe74c3c
    else:
        title = "🟢 정기 보고 (특이사항 없음)"
        description = "주요 리스크는 발견되지 않았습니다."
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
# [6] 수집 및 분석 (Newspaper3k 반영)
# =========================================================
def search_naver_news(keyword):
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {"X-Naver-Client-Id": NAVER_CLIENT_ID, "X-Naver-Client-Secret": NAVER_CLIENT_SECRET}
    params = {"query": keyword, "display": 15, "sort": "date"}
    try:
        res = requests.get(url, headers=headers, params=params)
        return res.json().get('items', [])
    except: return []

def scrape_article(url):
    # 1차 시도: newspaper3k
    try:
        article = Article(url, language='ko')
        article.download()
        article.parse()
        if article.text: return article.text.strip()[:1000]
    except: pass
    
    # 2차 시도: BeautifulSoup
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        res = requests.get(url, headers=headers, timeout=5)
        soup = BeautifulSoup(res.text, 'html.parser')
        content = (soup.select_one('#dic_area') or soup.select_one('#articeBody') or 
                   soup.select_one('.go_trans._article_content') or soup.select_one('article'))
        return content.get_text(strip=True)[:1000] if content else None
    except: return None

def analyze_with_ai(title, content, forced_reason):
    if not model: return None
    prompt = f"""
    기사 제목: {title}
    본문 내용: {content[:600]}
    감지 타겟: {forced_reason}

    [엄격 검증 기준]
    1. 실제로 사건(화재, 사고, 압수수색, 횡령, 기소, 의혹, 혐의, 탈루)이 발생/제기되었는가?
    2. 단순 예방 교육, 캠페인, 모의 훈련, 기부, 수상 소식인가? (이 경우 0점)
    3. 대구/경북 지역 소식 혹은 주요 기업(포스코 등) 소식인가?

    형식: {{"score": 점수, "category": "카테고리", "reason": "요약"}}
    """
    try:
        res = model.generate_content(prompt, generation_config={"response_mime_type": "application/json"})
        return json.loads(res.text)
    except: return None

# =========================================================
# [7] 메인 루프
# =========================================================
def main():
    processed_urls, seen_titles = load_history()
    execution_logs = []
    time_threshold = datetime.now() - timedelta(minutes=70)

    for keyword in KEYWORDS:
        articles = search_naver_news(keyword)
        for art in articles:
            title = art['title'].replace('<b>','').replace('</b>','').replace('&quot;','"')
            link = art['link']
            
            if link in processed_urls: continue
            processed_urls.add(link)
            
            # 제목 유사도 체크
            if any(get_similarity(title, t) > 0.8 for t in seen_titles): continue
            seen_titles.append(title)

            forced_score, forced_reason = check_critical_patterns(title)
            log_entry = {"title": title, "link": link, "status": "PASS", "score": 0}

            if forced_score == 100:
                content = scrape_article(link) or art.get('description', '')
                result = analyze_with_ai(title, content, forced_reason)
                
                if result:
                    final_score = result.get('score', 0)
                    log_entry.update({"score": final_score, "reason": result.get('reason'), "status": "ALERT" if final_score >= 80 else "PASS"})
                    if final_score >= 80:
                        send_alert_discord(title, result.get('reason'), link, result.get('category'), final_score)
            
            execution_logs.append(log_entry)
            time.sleep(0.5)

    send_hourly_report(execution_logs)
    save_history(processed_urls, seen_titles)

if __name__ == "__main__":
    main()
