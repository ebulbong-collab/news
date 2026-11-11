"""수집·묶기 단계가 함께 쓰는 공통 도구."""
from __future__ import annotations

import calendar
import email.utils
import html
import json
import re
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urldefrag, urlparse
from urllib.robotparser import RobotFileParser

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
KST = timezone(timedelta(hours=9))


# ---------------------------------------------------------------- 설정·파일
def load_yaml(name: str):
    return yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))


def load_config() -> dict:
    return load_yaml("config.yaml")


def read_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def iso_kst(dt: datetime) -> str:
    return dt.astimezone(KST).isoformat(timespec="minutes")


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s).astimezone(timezone.utc)


# ---------------------------------------------------------------- HTTP
def body_user_agent(cfg: dict) -> str:
    """기사 본문 가져오기용 UA. site.url 이 아직 placeholder(.invalid)면 연락처 없는 UA 로 대신한다."""
    url = cfg["site"]["url"]
    host = (urlparse(url).hostname or "").lower()
    if host.endswith(".invalid") or host in ("example.com", "example.org", "example.net"):
        return cfg["http"]["user_agent"].split(" (")[0]
    return cfg["http"]["user_agent"].format(site_url=url)


class HostLimiter:
    """같은 호스트에는 min_interval 초에 1회 이하로만 요청하도록 직렬화한다."""

    def __init__(self, interval: float):
        self.interval = interval
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._last: dict[str, float] = {}

    def get(self, session: requests.Session, url: str, **kw) -> requests.Response:
        host = urlparse(url).netloc
        with self._guard:
            lock = self._locks[host]
        with lock:
            wait = self.interval - (time.monotonic() - self._last.get(host, 0.0))
            if wait > 0:
                time.sleep(wait)
            try:
                return session.get(url, **kw)
            finally:
                self._last[host] = time.monotonic()


class Fetcher:
    def __init__(self, cfg: dict, user_agent: str):
        h = cfg["http"]
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept": "*/*"})
        self.timeout = h["timeout_sec"]
        self.limiter = HostLimiter(h["min_interval_sec"])

    def get(self, url: str, retries: int = 1):
        """(response | None, error 문자열 | None). 연결 오류·5xx 는 retries 회 재시도."""
        last_err = None
        for attempt in range(retries + 1):
            try:
                r = self.limiter.get(self.session, url, timeout=self.timeout, allow_redirects=True)
                if r.status_code >= 500 and attempt < retries:
                    time.sleep(2)
                    continue
                return r, None
            except requests.exceptions.Timeout:
                last_err = "timeout"
            except requests.exceptions.SSLError as e:
                last_err = f"ssl: {str(e)[:80]}"
            except requests.exceptions.RequestException as e:
                last_err = f"{type(e).__name__}: {str(e)[:80]}"
            if attempt < retries:
                time.sleep(2)
        return None, last_err


# ---------------------------------------------------------------- robots.txt
class RobotsCache:
    """origin 별 robots.txt 를 한 번만 받아 판정한다."""

    def __init__(self, fetcher: Fetcher, bot_token: str):
        self.f = fetcher
        self.token = bot_token
        self._lock = threading.Lock()
        self._cache: dict[str, dict] = {}

    def _load(self, origin: str) -> dict:
        r, err = self.f.get(origin + "/robots.txt", retries=0)
        if r is None:
            return {"status": f"error({err})", "rp": None, "mode": "unknown"}
        code = r.status_code
        if code == 200:
            rp = RobotFileParser()
            rp.parse(r.text.splitlines())
            return {"status": "200", "rp": rp, "mode": "rules",
                    "crawl_delay": rp.crawl_delay(self.token)}
        if code in (401, 403):  # 표준 라이브러리와 같이 보수적으로 전부 금지로 본다
            return {"status": str(code), "rp": None, "mode": "deny_all"}
        if 400 <= code < 500:   # robots.txt 없음 → 제한 없음
            return {"status": str(code), "rp": None, "mode": "allow_all"}
        return {"status": str(code), "rp": None, "mode": "unknown"}

    def check(self, url: str) -> tuple[str, str]:
        """(판정, robots 상태). 판정: allowed | blocked | unknown"""
        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}"
        with self._lock:
            if origin not in self._cache:
                self._cache[origin] = self._load(origin)
            info = self._cache[origin]
        mode = info["mode"]
        if mode == "allow_all":
            return "allowed", info["status"]
        if mode == "deny_all":
            return "blocked", info["status"]
        if mode == "unknown":
            return "unknown", info["status"]
        ok = info["rp"].can_fetch(self.token, url)
        return ("allowed" if ok else "blocked"), info["status"]


# ---------------------------------------------------------------- 텍스트·날짜
_ORPHAN_ENTITY = re.compile(r"(?<![&A-Za-z0-9])(quot|hellip|middot|ldquo|rdquo|lsquo|rsquo|nbsp|amp);")
_ORPHAN_MAP = {"quot": '"', "hellip": "…", "middot": "·", "ldquo": "“", "rdquo": "”",
               "lsquo": "‘", "rsquo": "’", "nbsp": " ", "amp": "&"}


def clean_text(raw: str) -> str:
    return BeautifulSoup(raw or "", "html.parser").get_text(" ", strip=True)


def clean_title(raw: str) -> str:
    """제목 정리: 이중 인코딩된 엔티티를 풀고, '&' 가 빠진 'quot;' 류를 복구한다.

    - '&amp;quot;' → '"', '&#8764;' → '∼' (이중 인코딩)
    - 프레시안처럼 원본에서 '&' 가 빠져 'quot;', 'hellip;' 로 내려오는 경우
    """
    t = raw or ""
    for _ in range(3):  # 이중·삼중 인코딩 대비
        u = html.unescape(t)
        if u == t:
            break
        t = u
    t = BeautifulSoup(t, "html.parser").get_text(" ", strip=True)
    t = _ORPHAN_ENTITY.sub(lambda m: _ORPHAN_MAP[m.group(1)], t)
    return re.sub(r"\s+", " ", t).strip()


TZ_RE = re.compile(r"(Z|GMT|UTC|UT|[+-]\d{2}:?\d{2}|\b[A-Z]{3,4})\s*$")


def entry_dt(e) -> datetime | None:
    """항목 발행 시각(UTC). 타임존 표기가 없는 날짜는 한국 시각(KST)으로 본다.

    - feedparser 는 타임존 없는 날짜를 UTC 로 읽어 9시간 어긋나므로 되돌린다.
    - 'Wed,11 Nov 2026 …' 처럼 쉼표 뒤 공백이 없는 형식은 feedparser 가 못 읽어 직접 파싱한다.
    """
    raw = (e.get("published") or e.get("updated") or "").strip()
    t = e.get("published_parsed") or e.get("updated_parsed")
    if t:
        dt = datetime.fromtimestamp(calendar.timegm(t), tz=timezone.utc)
        if raw and not TZ_RE.search(raw):
            dt -= timedelta(hours=9)
        return dt
    if not raw:
        return None
    fixed = re.sub(r"^([A-Za-z]{3}),(\S)", r"\1, \2", raw)
    try:
        dt = email.utils.parsedate_to_datetime(fixed)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return dt.astimezone(timezone.utc)


def norm_link(link: str) -> str:
    return urldefrag((link or "").strip())[0]


# ---------------------------------------------------------------- 키워드 필터
@dataclass
class Match:
    passed: bool
    reason: str                      # strong_title | weak_ctx | desc_strong | excluded | none
    hits: list[str] = field(default_factory=list)
    v1_passed: bool = False          # 지시서 원안(제목 단순 매칭) 판정, 비교용


class Keywords:
    def __init__(self, d: dict | None = None):
        d = d or load_yaml("keywords.yaml")
        self.strong = d["strong"]
        self.weak = d["weak"]
        self.context = d["context"]
        self.mask = d["mask"]
        self.exclude_v1 = d["exclude"]
        self.exclude = d["exclude"] + d.get("exclude_added", [])

    def _masked(self, title: str) -> str:
        for m in self.mask:
            title = title.replace(m, " ")
        return title

    def match(self, title: str, desc: str = "") -> Match:
        v1 = (any(k in title for k in self.strong + self.weak)
              and not any(k in title for k in self.exclude_v1))
        t = self._masked(title)
        if any(k in t for k in self.exclude):
            return Match(False, "excluded", [], v1)
        s_hits = [k for k in self.strong if k in t]
        if s_hits:
            return Match(True, "strong_title", s_hits, v1)
        w_hits = [k for k in self.weak if k in t]
        has_ctx = any(k in t for k in self.context)
        if w_hits and has_ctx:
            return Match(True, "weak_ctx", w_hits, v1)
        d_hits = [k for k in self.strong if k in desc]
        if d_hits and has_ctx:
            return Match(True, "desc_strong", d_hits, v1)
        return Match(False, "none", [], v1)
