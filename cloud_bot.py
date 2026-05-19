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

NAVER_CLIENT_ID     = os.environ.get("NAVER_ID")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_SECRET")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_URL")
GEMINI_API_KEY      = os.environ.get("GOOGLE_KEY")   # Google AI Studio 키

KST = timezone(timedelta(hours=9))
HISTORY_FILE = "news_history.json"

# =========================================================
# [2] VIP 기업 명단
# =========================================================

# 그룹 A: 본사/주력이 대구경북 → 지역어 없이 단독 검색
VIP_LOCAL = [
    # 대구 본사
    "iM뱅크", "대구은행", "대구백화점",
    "KCC글라스", "이노와이어리스", "드림텍", "SH에너지화학",
    "삼영엠텍", "제이브이엠", "인터플렉스",
    "화성산업", "삼보모터스", "동일산업",
    "에스엘", "아진산업", "피에이치에이", "평화산업",
    "메가젠임플란트",
    # 구미 본사/주력
    "에코프로", "엘앤에프", "씨아이에스",
    "서진시스템", "나라엠앤디",
    # 포항 본사/주력
    "포스코", "포항제철소", "세아베스틸",
    "포스코스틸리온", "포스코엠텍", "포스코케미칼",
    "포스코퓨처엠", "포스코DX", "POSCO홀딩스",
    # 경북 본사
    "대동", "이수페타시스", "대구텍", "풍산",
    "일진전기", "국일제지",
]

# 그룹 B: 지역 사업장 보유 대기업 → 지역어 붙여서 검색
VIP_REGIONAL = [
    "SK실트론",       # 구미 공장
    "한국수력원자력",  # 경주 월성
]

# 글로벌 외신 모니터링
VIP_COMPANIES_EN = [
    "POSCO", "EcoPro", "L&F battery", "iM Bank",
    "Isu Petasys", "Daedong", "TaeguTec", "Ajin Industrial", "CIS battery",
    "SeAH Besteel", "Poongsan",
]

# =========================================================
# [3] 키워드 체계 (OR 묶음으로 압축)
# =========================================================

# 지역 OR 묶음 (네이버 API OR 연산자 지원)
REGION_OR = "대구 OR 경북 OR 구미 OR 포항 OR 경주"

# 카테고리 1: 비리/세무/횡령 리스크
CAT1_RISKS = [
    "압수수색", "횡령", "배임", "비자금", "탈세", "탈루",
    "분식회계", "가공거래", "역외탈세", "편법증여", "일감몰아주기",
    "의혹", "비리", "혐의", "구속", "기소", "세무조사",
    "고발", "내부고발", "공익제보", "내사", "수사의뢰",
    "추징금", "허위세금계산서", "차명계좌", "차명주식", "명의신탁",
    "사익편취", "계열사 부당지원", "주주대표소송", "오너리스크",
    "주가조작", "시세조종", "미공개정보", "내부자거래",
    "임금체불", "폐수 불법방류", "환경오염",
    "페이퍼컴퍼니", "의견거절", "밀약", "배임증재", "불법자금",
]

# 카테고리 2: 화재/재난/산업재해
CAT2_DISASTERS = [
    "공장 화재", "산단 화재", "공단 화재", "폭발 사고",
    "화학물질 누출", "불산 누출", "중대재해",
    "노동자 사망", "산업재해", "끼임 사고", "추락 사고",
]

# 카테고리 3: 주요기관 인사
CAT3_AGENCIES = [
    "대구지검", "대구고검", "대구공소청", "대구중수청",
    "대구경찰청", "경북경찰청", "대구지방국세청",
    "대구고용노동청", "경북고용노동청", "대구금융감독원",
]
CAT3_PERSONNEL_OR = "인사 OR 전보 OR 발령 OR 승진 OR 내정"

# 지역 언론 모니터링
LOCAL_MEDIA_NAMES = ["영남일보", "매일신문", "대구일보", "경북일보", "경북도민일보", "TBC"]
LOCAL_TOPICS = ["경제", "기업", "산업단지", "투자", "부동산", "수출"]


def build_keywords():
    """키워드 목록 생성 (OR 묶음으로 압축)"""
    keywords = []

    # 카테고리 1: 지역 OR + 리스크 단어
    for risk in CAT1_RISKS:
        keywords.append({"query": f"{REGION_OR} {risk}", "track": "kr", "cat": 1})

    # 카테고리 2: 지역 OR + 재난 단어
    for disaster in CAT2_DISASTERS:
        keywords.append({"query": f"{REGION_OR} {disaster}", "track": "kr", "cat": 2})

    # 카테고리 3: 기관명 + 인사 OR
    for agency in CAT3_AGENCIES:
        keywords.append({"query": f"{agency} {CAT3_PERSONNEL_OR}", "track": "kr", "cat": 3})

    # 그룹 A: 본사/주력이 대구경북 → 단독 검색 (vip 트랙)
    for company in VIP_LOCAL:
        keywords.append({"query": company, "track": "vip", "cat": 1})

    # 그룹 B: 지역 사업장 보유 대기업 → 지역어 붙여서 검색 (vip 트랙)
    for company in VIP_REGIONAL:
        keywords.append({"query": f"{company} {REGION_OR}", "track": "vip", "cat": 1})

    # 글로벌 외신
    for company in VIP_COMPANIES_EN:
        keywords.append({"query": company, "track": "en", "cat": 1})

    # 지역 언론 전용
    for media in LOCAL_MEDIA_NAMES:
        for topic in LOCAL_TOPICS:
            keywords.append({"query": f"{media} {topic}", "track": "local", "cat": 0})

    return keywords


# =========================================================
# [4] 1차 필터 (통과/차단만 결정 — 점수는 AI가 결정)
# =========================================================
BLOCK_SPORTS = [
    "프로농구", "KBL", "프로야구", "KBO", "프로축구", "K리그",
    "감독", "선수", "득점", "리바운드", "홈런", "페가수스", "라이온즈",
    "대구FC", "스포츠", "MVP", "결승골", "끝내기",
]
BLOCK_POLITICS = [
    "국회의원", "시의원", "도의원", "구의원", "정치", "후보", "공천",
    "당선", "선거", "여당", "야당", "국회", "민주당", "국민의힘",
    "경선", "여론조사", "지지율", "출마", "총선", "지선", "대선",
    "최고위", "원내대표",
]
BLOCK_STOCK = [
    "주가", "상승", "하락", "급등", "급락", "증시",
    "코스피", "코스닥", "시황", "매수", "매도",
]
# 범죄/리스크 단어가 있으면 정치/주식 차단 면제
CRIME_OVERRIDE = [
    "횡령", "배임", "비리", "탈세", "구속", "압수수색",
    "기소", "의혹", "혐의", "비자금", "주가조작", "내부자거래",
]

def passes_prefilter(title: str, track: str) -> bool:
    """True면 AI 분석 대상, False면 즉시 폐기"""
    has_crime = any(w in title for w in CRIME_OVERRIDE)

    if any(w in title for w in BLOCK_SPORTS):
        return False
    if any(w in title for w in BLOCK_POLITICS) and not has_crime:
        return False
    if any(w in title for w in BLOCK_STOCK) and not has_crime:
        return False

    # vip 트랙은 1차 필터 없이 전부 AI로 넘김
    if track == "vip":
        return True

    # local 트랙은 지역 관련 경제/기업 기사만
    if track == "local":
        local_signal = ["대구", "경북", "구미", "포항", "경주", "영남"]
        econ_signal  = ["기업", "공장", "산단", "투자", "수출", "부동산",
                        "경제", "무역", "토지", "상공", "테크노"]
        if not any(w in title for w in local_signal):
            return False
        if not any(w in title for w in econ_signal):
            return False

    return True


# =========================================================
# [5] Gemini AI 분석
# =========================================================
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.5-flash:generateContent"
)

CATEGORY_TAGS = {
    1: ["[세무/재무]", "[자본이동]", "[경영/갈등]", "[사법/인사]", "[일반동향]"],
    2: ["[사고/재난]"],
    3: ["[사법/인사]"],
    0: ["[거시경제]", "[부동산/토지]", "[지역기업동향]", "[지자체정책]"],
}

SYSTEM_PROMPT = """당신은 대구/경북 지역 뉴스를 분류하는 AI입니다.
반드시 JSON만 출력하세요. 설명, 마크다운, 코드블록 절대 금지.
출력 형식: {"score": 숫자, "tag": "태그"}"""

def call_gemini(prompt: str, retry: int = 2):
    if not GEMINI_API_KEY:
        return None
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "generationConfig": {
            "temperature": 0.0,
            "maxOutputTokens": 200,
        },
    }
    for attempt in range(retry + 1):
        try:
            res = requests.post(
                GEMINI_URL,
                params={"key": GEMINI_API_KEY},
                json=payload,
                timeout=12,
            )
            # 503 과부하 → 잠시 대기 후 재시도
            if res.status_code == 503:
                wait = 10 * (attempt + 1)
                print(f"  ⚠️ Gemini 503 과부하 — {wait}초 대기 후 재시도 ({attempt+1}/{retry})")
                time.sleep(wait)
                continue
            if res.status_code != 200:
                print(f"  ⚠️ Gemini HTTP {res.status_code}: {res.text[:100]}")
                return None

            data = res.json()
            # 빈 candidates 방어
            candidates = data.get("candidates", [])
            if not candidates:
                print("  ⚠️ Gemini 빈 candidates")
                return None

            parts = candidates[0].get("content", {}).get("parts", [])
            raw = ""
            for part in parts:
                if "text" in part:
                    raw = part["text"].strip()
                    break

            if not raw:
                print("  ⚠️ Gemini 빈 응답")
                return None

            raw = re.sub(r"```(?:json)?|```", "", raw).strip()
            return json.loads(raw)

        except Exception as e:
            print(f"  ⚠️ Gemini 오류 (시도 {attempt+1}): {e}")
            if attempt < retry:
                time.sleep(5)
    return None


def build_prompt(title: str, content: str, cat: int, track: str) -> str:
    tags = CATEGORY_TAGS.get(cat, CATEGORY_TAGS[1])
    tags_str = ", ".join(tags)

    if track == "en":
        return (
            f"[글로벌 외신 분류]\n"
            f"제목: {title}\n본문: {content}\n\n"
            f"지시:\n"
            f"1. score: 이 기사가 대구/경북 기업 리스크에 얼마나 중요한지 0~100점\n"
            f"2. tag: 다음 중 하나만 선택 → {tags_str}\n"
            f"출력: {{\"score\": 숫자, \"tag\": \"태그\"}}"
        )

    if track == "local":
        return (
            f"[지역 언론 경제/정책 분류]\n"
            f"제목: {title}\n본문: {content}\n\n"
            f"지시:\n"
            f"1. 단순 날씨·교통사고·미담·행사·정치 가십 → score 0\n"
            f"2. 대구/경북 지역의 의미 있는 경제·기업·투자·부동산·정책 기사 → score 65\n"
            f"3. tag: 다음 중 하나만 선택 → {tags_str}\n"
            f"출력: {{\"score\": 숫자, \"tag\": \"태그\"}}"
        )

    if track == "vip":
        return (
            f"[VIP 기업 이상징후 감지]\n"
            f"제목: {title}\n본문: {content}\n\n"
            f"[즉시 score 0 처리 — 아래 중 하나라도 해당하면]\n"
            f"- VIP 기업이 기사의 주인공이 아니라 단순 언급·연관 기업으로만 등장\n"
            f"  (예: 인수금융 주선 은행 기사에 VIP 기업이 피인수 대상으로 언급만 된 경우)\n"
            f"- 실적 발표 (매출/영업이익/순이익 증감)\n"
            f"- 신제품·신기술 출시, 공장 준공, 생산 개시\n"
            f"- 수주·공급계약·MOU (비리 의혹 없는 단순 계약)\n"
            f"- 주가·증시·종목 분석\n"
            f"- 채용·인턴·복지·사내 행사\n"
            f"- 홍보·마케팅·CSR 활동\n\n"
            f"[점수 부여 — VIP 기업이 주인공이고 위 해당 없을 때만]\n"
            f"- 80+ : 압수수색·구속·기소·세무조사·횡령·배임 등 직접 수사/제재\n"
            f"- 70~79 : 비리 의혹·내부고발·소송·분쟁·오너리스크·지배구조 문제\n"
            f"- 65~69 : 경영진 갑작스러운 교체·대규모 M&A·화재·중대재해\n"
            f"tag: 다음 중 하나만 선택 → {tags_str}\n"
            f"출력: {{\"score\": 숫자, \"tag\": \"태그\"}}"
        )

    cat_desc = {
        1: "세무·횡령·비리·기업 리스크 관점 (대구/경북 지역 한정)",
        2: "산업재해·화재·재난 심각도 관점 (대구/경북 지역 한정)",
        3: "수사기관·행정기관 인사 중요도 관점",
    }.get(cat, "종합 중요도 관점")

    return (
        f"[국내 뉴스 분류 — {cat_desc}]\n"
        f"제목: {title}\n본문: {content}\n\n"
        f"[즉시 score 0 처리 — 아래 중 하나라도 해당하면]\n"
        f"- 대구/경북 소재 기업·기관·인물이 기사의 주인공이 아닌 경우\n"
        f"  (예: 전국 이슈 기사에 대구/경북이 단순 언급만 된 경우)\n"
        f"- 정치인 관련 기사 (선거·공천·여론조사 등)\n"
        f"- 단순 날씨·교통·행사·미담\n\n"
        f"[점수 부여 — 대구/경북 주체 기사일 때]\n"
        f"- 80+ : 즉각 대응 필요 (압수수색·구속·대형사고 등)\n"
        f"- 65~79 : 주의 요망 (의혹·분쟁·인사·자본이동)\n"
        f"- 50~64 : 참고 동향\n"
        f"tag: 다음 중 하나만 선택 → {tags_str}\n"
        f"출력: {{\"score\": 숫자, \"tag\": \"태그\"}}"
    )


# =========================================================
# [6] 뉴스 수집
# =========================================================
def search_naver_news(query: str) -> list:
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    params = {"query": query, "display": 10, "sort": "date"}
    try:
        res = requests.get(url, headers=headers, params=params, timeout=5)
        return res.json().get("items", [])
    except Exception as e:
        print(f"  ⚠️ 네이버 검색 오류 ({query[:20]}): {e}")
        return []


def search_google_news(query: str, lang: str = "ko") -> list:
    safe = urllib.parse.quote_plus(query)
    if lang == "en":
        url = f"https://news.google.com/rss/search?q={safe}&hl=en-US&gl=US&ceid=US:en"
    else:
        url = f"https://news.google.com/rss/search?q={safe}&hl=ko&gl=KR&ceid=KR:ko"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        res = requests.get(url, headers=headers, timeout=5)
        soup = BeautifulSoup(res.content, "xml")
        return [
            {"title": i.title.text, "link": i.link.text, "pubDate": i.pubDate.text}
            for i in soup.find_all("item")[:10]
        ]
    except Exception as e:
        print(f"  ⚠️ 구글 뉴스 오류 ({query[:20]}): {e}")
        return []


def scrape_article(url: str) -> str:
    """기사 본문 스크래핑 — 실패 시 빈 문자열 반환"""
    try:
        res = requests.get(
            url, headers={"User-Agent": "Mozilla/5.0"}, timeout=4
        )
        soup = BeautifulSoup(res.text, "html.parser")
        for selector in ["#dic_area", "#articeBody", ".go_trans._article_content"]:
            content = soup.select_one(selector)
            if content:
                return content.get_text(strip=True)[:800]
        # fallback: body 전체
        body = soup.find("body")
        return body.get_text(strip=True)[:800] if body else ""
    except:
        return ""


# =========================================================
# [7] 중복 제거
# =========================================================
def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            pass
    return {"urls": [], "titles": []}


def save_history(history: dict):
    history["urls"]   = history["urls"][-2000:]
    history["titles"] = history["titles"][-2000:]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def get_similarity(a: str, b: str) -> float:
    a_c = re.sub(r"[^가-힣a-zA-Z0-9]", "", a)
    b_c = re.sub(r"[^가-힣a-zA-Z0-9]", "", b)
    return SequenceMatcher(None, a_c, b_c).ratio()


def deduplicate_final(articles: list) -> list:
    """제목 유사도 기반 최종 중복 제거 — 점수 높은 것 우선 유지"""
    # 점수 내림차순 정렬 후 중복 제거 (높은 점수 기사를 대표로)
    sorted_arts = sorted(articles, key=lambda x: x["score"], reverse=True)
    result = []
    for art in sorted_arts:
        # 0.65 이상이면 같은 사건으로 판단 (기존 0.80보다 완화)
        if not any(get_similarity(art["title"], r["title"]) > 0.45 for r in result):
            result.append(art)
    return result


# =========================================================
# [8] 디스코드 전송
# =========================================================
def send_discord(embeds: list):
    if not DISCORD_WEBHOOK_URL:
        return
    try:
        requests.post(
            DISCORD_WEBHOOK_URL,
            json={"username": "뉴스 요약 봇", "embeds": embeds},
            timeout=5,
        )
    except Exception as e:
        print(f"  ⚠️ 디스코드 전송 오류: {e}")


def build_discord_message(final_logs: list, is_morning: bool) -> list:
    high  = [l for l in final_logs if l["score"] >= 80]
    mid   = [l for l in final_logs if 65 <= l["score"] < 80 and l["track"] != "local"]
    local = [l for l in final_logs if l["track"] == "local" and l["score"] >= 65]
    low   = [l for l in final_logs if 50 <= l["score"] < 65]

    desc = ""
    if high:
        desc += "🚨 **[핵심 리스크 / 즉각 확인]**\n"
        for l in high:
            desc += f"**[{l['score']}점]** {l['tag']} [{l['title']}]({l['link']})\n\n"
    if mid:
        desc += "🏢 **[주요 동향 / 주의 요망]**\n"
        for l in mid:
            desc += f"**[{l['score']}점]** {l['tag']} [{l['title']}]({l['link']})\n\n"
    if low:
        desc += "📋 **[참고 동향]**\n"
        for l in low:
            desc += f"[{l['score']}점] {l['tag']} [{l['title']}]({l['link']})\n\n"
    if local:
        desc += "📰 **[지역 언론 경제/정책]**\n"
        for l in local:
            desc += f"[{l['score']}점] {l['tag']} [{l['title']}]({l['link']})\n\n"

    if not desc.strip():
        msg = "밤사이 주요 기사 없음" if is_morning else "최근 1시간 주요 기사 없음"
        return [{"title": "🟢 이상 없음", "description": msg, "color": 0x2ECC71}]

    title_str = (
        f"🌅 아침 브리핑 ({datetime.now(KST).strftime('%m/%d %H:%M')})"
        if is_morning
        else f"📊 정기 보고 ({datetime.now(KST).strftime('%H:%M')})"
    )
    if TEST_MODE:
        title_str = "🛠️ [테스트] " + title_str

    color = 0xE74C3C if high else 0xFFA500
    return [{"title": title_str, "description": desc, "color": color}]


# =========================================================
# [9] 메인
# =========================================================
def main():
    print("☁️ 스나이퍼 봇 시작...")
    now_kst = datetime.now(KST)
    is_morning = (now_kst.hour == 8)

    if TEST_MODE:
        lookback = timedelta(hours=24)
        print("⏳ [테스트] 24시간 수집")
    elif is_morning:
        lookback = timedelta(hours=24)
        print("🌅 [모닝 브리핑] 24시간 수집")
    else:
        lookback = timedelta(minutes=75)
        print("🕒 [정기] 75분 수집")

    time_threshold = now_kst - lookback
    history = load_history()

    # ── 수집 ──────────────────────────────────────────────
    keywords   = build_keywords()
    raw_map    = {}   # link → article dict (중복 URL 제거)

    print(f"\n⚡ 수집 시작 (키워드 {len(keywords)}개)")

    for kw in keywords:
        query = kw["query"]
        track = kw["track"]
        cat   = kw["cat"]

        if track == "en":
            items = search_google_news(query, lang="en")
            time.sleep(0.4)
        else:
            items = search_naver_news(query) + search_google_news(query)
            time.sleep(0.05)

        for it in items:
            link = it.get("link") or it.get("originallink", "")
            if not link or link in raw_map:
                continue
            it["track"] = track
            it["cat"]   = cat
            raw_map[link] = it

    print(f"   수집된 고유 기사: {len(raw_map)}건")

    # ── 시간 필터 + 히스토리 중복 제거 ────────────────────
    candidates = []
    for link, art in raw_map.items():
        title = re.sub(r"<.*?>|&quot;|&amp;|&lt;|&gt;", "", art.get("title", "")).strip()
        if not title:
            continue
        try:
            pub_dt = parsedate_to_datetime(art["pubDate"])
            if pub_dt.tzinfo is None:
                pub_dt = pub_dt.replace(tzinfo=timezone.utc)
            if pub_dt < time_threshold:
                continue
        except:
            continue

        if link in history["urls"]:
            continue
        if any(get_similarity(title, t) > 0.85 for t in history["titles"]):
            continue

        # 1차 필터
        if not passes_prefilter(title, art["track"]):
            continue

        candidates.append({
            "title": title,
            "link":  link,
            "track": art["track"],
            "cat":   art["cat"],
            "raw":   art,
        })

    print(f"   1차 필터 통과: {len(candidates)}건")

    # ── Gemini AI 분석 ─────────────────────────────────────
    print(f"\n🤖 Gemini 분석 시작...")
    final_logs = []

    for art in candidates:
        title   = art["title"]
        link    = art["link"]
        track   = art["track"]
        cat     = art["cat"]

        # 본문 스크래핑 (구글 뉴스 리다이렉트 링크 제외)
        if "news.google.com" not in link:
            content = scrape_article(link)
        else:
            content = ""
        if not content:
            content = re.sub(r"<.*?>", "", art["raw"].get("description", ""))[:500]

        prompt  = build_prompt(title, content, cat, track)
        result  = call_gemini(prompt)
        time.sleep(0.5)   # Gemini free tier: 15 RPM → 0.5초면 충분

        if result is None:
            # AI 실패 시: 재난(cat2)/인사(cat3)만 기본 점수로 통과, 나머지 폐기
            # cat1(비리)은 AI 없이 판단 불가 → 폐기하여 노이즈 방지
            fallback_score = {2: 70, 3: 70}.get(cat, 0)
            if fallback_score == 0:
                continue
            result = {
                "score": fallback_score,
                "tag":   CATEGORY_TAGS.get(cat, ["[일반동향]"])[0],
            }

        score = int(result.get("score", 0))
        tag   = result.get("tag", "[일반동향]")

        print(f"  [{score}점] {tag} | {title[:40]}")

        # 점수 무관하게 모든 분석 기사를 히스토리에 등록
        # → 0점 폐기된 기사도 다음 실행에서 재수집/재분석 방지
        history["urls"].append(link)
        history["titles"].append(title)

        if score >= 50:
            final_logs.append({
                "title": title,
                "link":  link,
                "score": score,
                "tag":   tag,
                "track": track,
            })

    # ── 최종 중복 제거 ─────────────────────────────────────
    final_logs = deduplicate_final(final_logs)
    final_logs.sort(key=lambda x: x["score"], reverse=True)

    print(f"\n📊 최종 발송 기사: {len(final_logs)}건")

    # ── 디스코드 전송 ──────────────────────────────────────
    embeds = build_discord_message(final_logs, is_morning)
    send_discord(embeds)

    # ── 히스토리 저장 ──────────────────────────────────────
    if not TEST_MODE:
        save_history(history)

    print("✅ 완료")


if __name__ == "__main__":
    main()
