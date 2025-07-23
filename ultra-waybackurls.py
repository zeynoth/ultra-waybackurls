import requests
import sys
import json
import threading
import queue
import re
import os
import time
import argparse
import random
import sqlite3
from urllib.parse import urlparse, parse_qs
from concurrent.futures import ThreadPoolExecutor
from typing import List, Set, Dict, Tuple
import logging
from datetime import datetime
from colorlog import ColoredFormatter
from collections import Counter
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import hashlib
import pickle
import html
import csv
from bs4 import BeautifulSoup

# لیست User-Agentهای متنوع
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:89.0) Gecko/20100101 Firefox/89.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:89.0) Gecko/20100101 Firefox/89.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 14_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.1.1 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edge/91.0.864.59 Safari/537.36"
]

# فیلتر لاگینگ برای اضافه کردن progress پیش‌فرض
class ProgressFilter(logging.Filter):
    def filter(self, record):
        if not hasattr(record, 'progress'):
            record.progress = ''
        return True

# تنظیم لاگینگ رنگی
log_format = "%(asctime)s - %(log_color)s%(levelname)s%(reset)s%(progress)s - %(message)s"
formatter = ColoredFormatter(
    log_format,
    datefmt='%Y-%m-%d %H:%M:%S',
    log_colors={
        'DEBUG': 'cyan',
        'INFO': 'green',
        'WARNING': 'yellow',
        'ERROR': 'red',
        'CRITICAL': 'red,bg_white',
    }
)
handler = logging.StreamHandler()
handler.setFormatter(formatter)
handler.addFilter(ProgressFilter())
logging.getLogger().handlers = [handler]
logging.getLogger().setLevel(logging.INFO)
logger = logging.getLogger(__name__)

class WaybackScraper:
    def __init__(self, hosts: List[str], with_subs: bool = False, output_dir: str = "output",
                 max_threads: int = 2, max_pages: int = 10, delay: float = 3.0,
                 cache_dir: str = "cache", db_path: str = "wayback.db",
                 url_filter: str = ""):
        self.hosts = [host.strip() for host in hosts]
        self.with_subs = with_subs
        self.output_dir = output_dir
        self.cache_dir = cache_dir
        self.db_path = db_path
        self.url_filter = re.compile(url_filter) if url_filter else None
        self.max_threads = max_threads
        self.max_pages = max_pages
        self.delay = delay
        self.urls: Dict[str, Set[str]] = {host: set() for host in self.hosts}
        self.params: Dict[str, int] = Counter()
        self.stats: Dict[str, int] = Counter()
        self.content_types: Dict[str, int] = Counter()
        self.paths: Dict[str, int] = Counter()
        self.years: Dict[str, int] = Counter()
        self.sensitive_params: Dict[str, int] = Counter()
        self.status_codes: Dict[int, int] = Counter()
        self.keywords: Dict[str, int] = Counter()
        self.digests: Dict[str, int] = Counter()
        self.lengths: Dict[str, int] = Counter()
        self.queue = queue.Queue()
        self.session = self.setup_session()
        self.lock = threading.Lock()
        self.setup_directories()
        self.setup_database()

    def setup_session(self) -> requests.Session:
        """تنظیم جلسه با User-Agent تصادفی و بازآزمایی"""
        session = requests.Session()
        session.headers.update({
            'User-Agent': random.choice(USER_AGENTS)
        })
        retries = Retry(total=5, backoff_factor=2, status_forcelist=[429, 500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retries, pool_connections=2, pool_maxsize=2)
        session.mount('http://', adapter)
        session.mount('https://', adapter)
        return session

    def setup_directories(self):
        """ایجاد دایرکتوری‌های خروجی و کش"""
        for directory in [self.output_dir, self.cache_dir]:
            if not os.path.exists(directory):
                os.makedirs(directory)
                logger.info(f"Created directory: {directory}", extra={'progress': ''})

    def setup_database(self):
        """ایجاد یا به‌روزرسانی پایگاه داده SQLite"""
        with sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            # ایجاد جدول urls در صورت عدم وجود
            c.execute('''
                CREATE TABLE IF NOT EXISTS urls (
                    url TEXT PRIMARY KEY,
                    host TEXT,
                    source TEXT,
                    timestamp TEXT,
                    content_type TEXT,
                    status_code INTEGER,
                    path TEXT,
                    year TEXT,
                    digest TEXT,
                    length INTEGER,
                    keywords TEXT
                )
            ''')
            # بررسی وجود ستون‌های جدید و افزودن در صورت عدم وجود
            c.execute("PRAGMA table_info(urls)")
            columns = [info[1] for info in c.fetchall()]
            if 'keywords' not in columns:
                c.execute('ALTER TABLE urls ADD COLUMN keywords TEXT')
                logger.info("Added keywords column to urls table", extra={'progress': ''})
            if 'digest' not in columns:
                c.execute('ALTER TABLE urls ADD COLUMN digest TEXT')
                logger.info("Added digest column to urls table", extra={'progress': ''})
            if 'length' not in columns:
                c.execute('ALTER TABLE urls ADD COLUMN length INTEGER')
                logger.info("Added length column to urls table", extra={'progress': ''})

            # ایجاد جدول params
            c.execute('''
                CREATE TABLE IF NOT EXISTS params (
                    url TEXT,
                    parameter TEXT,
                    count INTEGER,
                    sensitive BOOLEAN,
                    FOREIGN KEY(url) REFERENCES urls(url)
                )
            ''')
            conn.commit()
            logger.info(f"Initialized SQLite database: {self.db_path}", extra={'progress': ''})

    def get_cache_file(self, host: str, page: int, source: str) -> str:
        """ساخت مسیر فایل کش"""
        url = self.build_url(host, page, source)
        cache_key = hashlib.md5(url.encode()).hexdigest()
        return os.path.join(self.cache_dir, f"{host}_{source}_{cache_key}.pkl")

    def load_from_cache(self, host: str, page: int, source: str) -> List[Tuple[str, str, str, str, str, str]]:
        """بارگذاری داده از کش"""
        cache_file = self.get_cache_file(host, page, source)
        if os.path.exists(cache_file):
            try:
                with open(cache_file, 'rb') as f:
                    logger.debug(f"Loaded page {page} for {host} from {source} cache", extra={'progress': ''})
                    return pickle.load(f)
            except Exception as e:
                logger.error(f"Error loading cache for page {page} ({host}, {source}): {e}", extra={'progress': ''})
        return []

    def save_to_cache(self, host: str, page: int, source: str, data: List[Tuple[str, str, str, str, str, str]]):
        """ذخیره داده در کش"""
        cache_file = self.get_cache_file(host, page, source)
        try:
            with open(cache_file, 'wb') as f:
                pickle.dump(data, f)
                logger.debug(f"Saved page {page} for {host} to {source} cache", extra={'progress': ''})
        except Exception as e:
            logger.error(f"Error saving cache for page {page} ({host}, {source}): {e}", extra={'progress': ''})

    def build_url(self, host: str, page: int, source: str) -> str:
        """ساخت URL برای درخواست به Wayback Machine"""
        if source == "wayback":
            base = 'http://web.archive.org/cdx/search/cdx'
            domain = f'*.{host}/*' if self.with_subs else f'{host}/*'
            return f'{base}?url={domain}&output=json&fl=original,timestamp,mimetype,statuscode,digest,length&collapse=urlkey&page={page}'
        return ""

    def check_domain(self, host: str) -> bool:
        """چک کردن وجود دامنه"""
        try:
            test_url = f"http://web.archive.org/cdx/search/cdx?url={host}&limit=1&fl=original"
            logger.debug(f"Checking domain availability: {test_url}", extra={'progress': ''})
            response = self.session.get(test_url, timeout=30)
            response.raise_for_status()
            logger.debug(f"Response content: {response.text[:100]}...", extra={'progress': ''})
            data = response.json()
            if data and len(data) > 1:
                logger.info(f"Domain {host} has archived data in Wayback Machine", extra={'progress': ''})
                return True
            logger.warning(f"No archived data found for {host} in Wayback Machine", extra={'progress': ''})
            return False
        except requests.RequestException as e:
            logger.error(f"Error checking domain {host}: {e}", extra={'progress': ''})
            logger.debug(f"Response status: {getattr(e.response, 'status_code', 'N/A')}, content: {getattr(e.response, 'text', 'N/A')[:100]}...", extra={'progress': ''})
            return True  # Proceed with scraping despite the error
        except ValueError as e:
            logger.error(f"JSON parsing error for {host}: {e}", extra={'progress': ''})
            logger.debug(f"Response content: {response.text[:100] if 'response' in locals() else 'N/A'}...", extra={'progress': ''})
            return True  # Proceed with scraping despite JSON error

    def check_url_accessibility(self, url: str) -> bool:
        """چک کردن دسترسی به URL قبل از درخواست HEAD"""
        try:
            response = self.session.head(url, timeout=3, allow_redirects=False)
            return response.status_code < 400
        except requests.RequestException:
            return False

    def fetch_wayback(self, host: str, page: int) -> List[Tuple[str, str, str, str, str, str]]:
        """دریافت داده‌های Wayback Machine"""
        cached_data = self.load_from_cache(host, page, "wayback")
        if cached_data:
            return cached_data

        try:
            url = self.build_url(host, page, "wayback")
            progress = (page / self.max_pages) * 100
            logger.debug(f"Fetching Wayback page {page} for {host} ({progress:.1f}%): {url}", extra={'progress': f'[{progress:.1f}%]'})
            time.sleep(self.delay)
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            data = response.json()
            result = [(item[0], item[1], item[2], item[3], item[4], item[5]) for item in data[1:] if len(data) > 1]
            self.save_to_cache(host, page, "wayback", result)
            return result
        except requests.RequestException as e:
            logger.error(f"Error fetching Wayback page {page} for {host}: {e}", extra={'progress': ''})
            self.stats['fetch_errors'] += 1
            return []
        except ValueError as e:
            logger.error(f"JSON parsing error for page {page} ({host}): {e}", extra={'progress': ''})
            logger.debug(f"Response content: {response.text[:100] if 'response' in locals() else 'N/A'}...", extra={'progress': ''})
            return []

    def fetch_commoncrawl(self, host: str, page: int) -> List[Tuple[str, str, str, str, str, str]]:
        """دریافت داده‌های Common Crawl"""
        cached_data = self.load_from_cache(host, page, "commoncrawl")
        if cached_data:
            return cached_data

        try:
            url = self.build_url(host, page, "commoncrawl")
            progress = (page / self.max_pages) * 100
            logger.debug(f"Fetching Common Crawl page {page} for {host} ({progress:.1f}%): {url}", extra={'progress': f'[{progress:.1f}%]'})
            time.sleep(self.delay)
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            lines = response.text.splitlines()
            result = []
            for line in lines:
                try:
                    data = json.loads(line)
                    result.append((data.get("url", ""), data.get("timestamp", ""), data.get("mimetype", ""),
                                   data.get("statuscode", ""), data.get("digest", ""), data.get("length", "")))
                except json.JSONDecodeError:
                    continue
            self.save_to_cache(host, page, "commoncrawl", result)
            return result
        except requests.RequestException as e:
            logger.error(f"Error fetching Common Crawl page {page} for {host}: {e}", extra={'progress': ''})
            self.stats['fetch_errors'] += 1
            return []

    def fetch_virustotal(self, host: str) -> List[Tuple[str, str, str, str, str, str]]:
        """دریافت داده‌های VirusTotal"""
        if not hasattr(self, 'vt_api_key'):
            logger.warning(f"No VirusTotal API key provided for {host}", extra={'progress': ''})
            return []

        cached_data = self.load_from_cache(host, 0, "virustotal")
        if cached_data:
            return cached_data

        try:
            url = self.build_url(host, 0, "virustotal")
            logger.debug(f"Fetching VirusTotal data for {host}: {url}", extra={'progress': ''})
            time.sleep(self.delay)
            response = self.session.get(url, timeout=15)
            response.raise_for_status()
            data = response.json()
            result = [(item.get("url", ""), item.get("scan_date", ""), "", "", "", "") for item in data.get("detected_urls", [])]
            self.save_to_cache(host, 0, "virustotal", result)
            return result
        except requests.RequestException as e:
            logger.error(f"Error fetching VirusTotal data for {host}: {e}", extra={'progress': ''})
            self.stats['fetch_errors'] += 1
            return []

    def extract_params(self, url: str):
        """استخراج پارامترهای URL"""
        parsed = urlparse(url)
        query_params = parse_qs(parsed.query)
        for key, values in query_params.items():
            for value in values:
                param = f"{key}={value}"
                self.params[param] += 1
                if re.search(r'(key|token|password|secret|auth)', key, re.IGNORECASE):
                    self.sensitive_params[param] += 1

    def extract_path(self, url: str):
        """استخراج مسیر URL"""
        parsed = urlparse(url)
        path = parsed.path.strip('/')
        if path:
            self.paths[path] += 1
        return path

    def extract_year(self, timestamp: str):
        """استخراج سال از timestamp"""
        try:
            year = timestamp[:4]
            self.years[year] += 1
            return year
        except Exception:
            self.years['unknown'] += 1
            return 'unknown'

    def extract_keywords(self, url: str) -> str:
        """استخراج کلمات کلیدی از محتوای HTML"""
        try:
            response = self.session.get(url, timeout=10)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            text = soup.get_text(separator=' ', strip=True)
            words = re.findall(r'\w+', text.lower())
            for word in words:
                if len(word) > 3:
                    self.keywords[word] += 1
            return ','.join([word for word, _ in self.keywords.most_common(5)])
        except requests.RequestException:
            return ''

    def get_content_type_and_status(self, url: str) -> Tuple[str, int]:
        """دریافت Content-Type و Status Code با درخواست HEAD"""
        if not self.check_url_accessibility(url):
            self.status_codes[0] += 1
            return 'unknown', 0
        try:
            response = self.session.head(url, timeout=10, allow_redirects=True)
            content_type = response.headers.get('Content-Type', 'unknown')
            status_code = response.status_code
            self.status_codes[status_code] += 1
            return content_type, status_code
        except requests.RequestException:
            self.status_codes[0] += 1
            return 'unknown', 0

    def filter_urls(self, urls: List[Tuple[str, str, str, str, str, str]], host: str, source: str) -> List[str]:
        """فیلتر کردن و تحلیل URL‌ها"""
        filtered = []
        with sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            for item in urls:
                url_str, timestamp, mimetype, statuscode, digest, length = item
                if self.url_filter and not self.url_filter.search(url_str):
                    self.stats['filtered_urls'] += 1
                    continue
                parsed = urlparse(url_str)
                if parsed.scheme not in ('http', 'https'):
                    self.stats['invalid_urls'] += 1
                    continue
                if self.with_subs and parsed.hostname and parsed.hostname != host and not parsed.hostname.endswith('.' + host):
                    self.stats['filtered_urls'] += 1
                    continue
                if re.search(r'\.(ico|woff|woff2|ttf|svg|eot|css|js)$', url_str, re.IGNORECASE):
                    self.stats['filtered_urls'] += 1
                    continue
                if re.match(r'.*\.(html|php|asp|aspx|pdf|docx?|txt|png|jpg|jpeg|gif|xml|json)$', url_str, re.IGNORECASE) or mimetype in ('text/html', 'warc/revisit'):
                    filtered.append(url_str)
                    self.stats['valid_urls'] += 1
                    ext = url_str.split('.')[-1].lower() if '.' in url_str else 'none'
                    self.stats[f'ext_{ext}'] += 1
                    path = self.extract_path(url_str)
                    year = self.extract_year(timestamp)
                    content_type = mimetype if mimetype else 'unknown'
                    status_code = int(statuscode) if statuscode and statuscode.isdigit() else 0
                    digest = digest if digest else 'unknown'
                    length = int(length) if length and length.isdigit() else 0
                    self.content_types[content_type] += 1
                    self.status_codes[status_code] += 1
                    self.digests[digest] += 1
                    self.lengths[str(length)] += 1
                    keywords = self.extract_keywords(url_str) if content_type.startswith('text/html') else ''
                    c.execute('INSERT OR IGNORE INTO urls (url, host, source, timestamp, content_type, status_code, path, year, digest, length, keywords) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                              (url_str, host, source, timestamp, content_type, status_code, path, year, digest, length, keywords))
                    self.extract_params(url_str)
                    for param, count in self.params.items():
                        sensitive = param in self.sensitive_params
                        c.execute('INSERT INTO params (url, parameter, count, sensitive) VALUES (?, ?, ?, ?)',
                                  (url_str, param, count, sensitive))
                    conn.commit()
                    # نمایش رنگی وضعیت
                    if status_code == 200:
                        logger.info(f"Success: {url_str} (Status: {status_code}, Type: {content_type}, Length: {length})", extra={'progress': ''})
                    elif status_code in (301, 302):
                        logger.warning(f"Redirect: {url_str} (Status: {status_code}, Type: {content_type}, Length: {length})", extra={'progress': ''})
                    elif status_code >= 400:
                        logger.error(f"Error: {url_str} (Status: {status_code}, Type: {content_type}, Length: {length})", extra={'progress': ''})
                    else:
                        logger.debug(f"Unknown: {url_str} (Status: {status_code}, Type: {content_type}, Length: {length})", extra={'progress': ''})
                else:
                    self.stats['filtered_urls'] += 1
        return filtered

    def scrape(self):
        """اسکریپینگ با چندنخی فقط برای Wayback Machine"""
        start_time = time.time()
        for host in self.hosts:
            if not self.check_domain(host):
                logger.warning(f"Proceeding with scraping for {host} despite check_domain failure", extra={'progress': ''})
            logger.info(f"Starting scrape for {host} (subdomains: {self.with_subs}, max_pages: {self.max_pages}, delay: {self.delay}s)", extra={'progress': ''})

            with ThreadPoolExecutor(max_workers=self.max_threads) as executor:
                futures = []
                for page in range(self.max_pages):
                    futures.append(executor.submit(self.fetch_wayback, host, page))

                for i, future in enumerate(futures):
                    result = future.result()
                    with self.lock:
                        self.urls[host].update(self.filter_urls(result, host, "wayback"))
                    progress = ((i + 1) / self.max_pages) * 100
                    logger.info(f"Progress for {host}: {progress:.1f}%", extra={'progress': f'[{progress:.1f}%]'})

        elapsed = time.time() - start_time
        total_urls = sum(len(urls) for urls in self.urls.values())
        logger.info(f"Scraping completed in {elapsed:.2f} seconds. Found {total_urls} unique URLs", extra={'progress': ''})
        self.log_stats()

    def log_stats(self):
        """نمایش آمار URL‌ها"""
        logger.info("=== URL Statistics ===", extra={'progress': ''})
        logger.info(f"Valid URLs: {self.stats['valid_urls']}", extra={'progress': ''})
        logger.info(f"Filtered URLs: {self.stats['filtered_urls']}", extra={'progress': ''})
        logger.info(f"Invalid URLs: {self.stats['invalid_urls']}", extra={'progress': ''})
        logger.info(f"Fetch Errors: {self.stats['fetch_errors']}", extra={'progress': ''})
        logger.info("Top 5 Extensions:", extra={'progress': ''})
        for ext, count in self.stats.most_common(5):
            if ext.startswith('ext_'):
                logger.info(f"  .{ext[4:]}: {count}", extra={'progress': ''})
        logger.info("Top 5 Content-Types:", extra={'progress': ''})
        for ct, count in self.content_types.most_common(5):
            logger.info(f"  {ct}: {count}", extra={'progress': ''})
        logger.info("Top 5 URL Parameters:", extra={'progress': ''})
        for param, count in self.params.most_common(5):
            logger.info(f"  {param}: {count}", extra={'progress': ''})
        logger.info("Top 5 Sensitive Parameters:", extra={'progress': ''})
        for param, count in self.sensitive_params.most_common(5):
            logger.info(f"  {param}: {count}", extra={'progress': ''})
        logger.info("Top 5 Paths:", extra={'progress': ''})
        for path, count in self.paths.most_common(5):
            logger.info(f"  /{path}: {count}", extra={'progress': ''})
        logger.info("URLs by Year:", extra={'progress': ''})
        for year, count in self.years.most_common():
            logger.info(f"  {year}: {count}", extra={'progress': ''})
        logger.info("Status Codes:", extra={'progress': ''})
        for code, count in self.status_codes.most_common():
            if code == 200:
                logger.info(f"  {code} (Success): {count}", extra={'progress': ''})
            elif code in (301, 302):
                logger.warning(f"  {code} (Redirect): {count}", extra={'progress': ''})
            elif code >= 400:
                logger.error(f"  {code} (Error): {count}", extra={'progress': ''})
            else:
                logger.debug(f"  {code} (Unknown): {count}", extra={'progress': ''})
        logger.info("Top 5 Digests (Unique Content):", extra={'progress': ''})
        for digest, count in self.digests.most_common(5):
            logger.info(f"  {digest}: {count}", extra={'progress': ''})
        logger.info("Top 5 Content Lengths:", extra={'progress': ''})
        for length, count in self.lengths.most_common(5):
            logger.info(f"  {length}: {count}", extra={'progress': ''})
        logger.info("Top 5 Keywords:", extra={'progress': ''})
        for keyword, count in self.keywords.most_common(5):
            logger.info(f"  {keyword}: {count}", extra={'progress': ''})

    def generate_charts(self) -> List[Dict]:
        """تولید چارت‌ها برای سال، Content-Type، Status Code، و Digest"""
        charts = []

        # چارت سال‌ها
        labels = list(self.years.keys())
        data = list(self.years.values())
        if labels:
            charts.append({
                "type": "bar",
                "data": {
                    "labels": labels,
                    "datasets": [{
                        "label": "URLs by Year",
                        "data": data,
                        "backgroundColor": ["#36A2EB", "#FF6384", "#FFCE56", "#4BC0C0", "#9966FF"],
                        "borderColor": ["#2A8ABF", "#D44F6E", "#D4A837", "#3A9A9A", "#7A52CC"],
                        "borderWidth": 1
                    }]
                },
                "options": {
                    "scales": {
                        "y": {
                            "beginAtZero": True,
                            "title": {
                                "display": True,
                                "text": "Number of URLs"
                            }
                        },
                        "x": {
                            "title": {
                                "display": True,
                                "text": "Year"
                            }
                        }
                    },
                    "plugins": {
                        "title": {
                            "display": True,
                            "text": "URL Distribution by Year"
                        }
                    }
                }
            })

        # چارت Content-Type
        labels = list(self.content_types.keys())
        data = list(self.content_types.values())
        if labels:
            charts.append({
                "type": "pie",
                "data": {
                    "labels": labels,
                    "datasets": [{
                        "label": "Content Types",
                        "data": data,
                        "backgroundColor": ["#36A2EB", "#FF6384", "#FFCE56", "#4BC0C0", "#9966FF"],
                        "borderColor": ["#2A8ABF", "#D44F6E", "#D4A837", "#3A9A9A", "#7A52CC"],
                        "borderWidth": 1
                    }]
                },
                "options": {
                    "plugins": {
                        "title": {
                            "display": True,
                            "text": "Content Type Distribution"
                        }
                    }
                }
            })

        # چارت Status Code
        labels = [str(code) for code in self.status_codes.keys()]
        data = list(self.status_codes.values())
        if labels:
            charts.append({
                "type": "bar",
                "data": {
                    "labels": labels,
                    "datasets": [{
                        "label": "Status Codes",
                        "data": data,
                        "backgroundColor": ["#36A2EB", "#FF6384", "#FFCE56", "#4BC0C0", "#9966FF"],
                        "borderColor": ["#2A8ABF", "#D44F6E", "#D4A837", "#3A9A9A", "#7A52CC"],
                        "borderWidth": 1
                    }]
                },
                "options": {
                    "scales": {
                        "y": {
                            "beginAtZero": True,
                            "title": {
                                "display": True,
                                "text": "Count"
                            }
                        },
                        "x": {
                            "title": {
                                "display": True,
                                "text": "Status Code"
                            }
                        }
                    },
                    "plugins": {
                        "title": {
                            "display": True,
                            "text": "Status Code Distribution"
                        }
                    }
                }
            })

        # چارت Digest
        labels = list(self.digests.keys())[:5]
        data = list(self.digests.values())[:5]
        if labels:
            charts.append({
                "type": "bar",
                "data": {
                    "labels": labels,
                    "datasets": [{
                        "label": "Top Digests",
                        "data": data,
                        "backgroundColor": ["#36A2EB", "#FF6384", "#FFCE56", "#4BC0C0", "#9966FF"],
                        "borderColor": ["#2A8ABF", "#D44F6E", "#D4A837", "#3A9A9A", "#7A52CC"],
                        "borderWidth": 1
                    }]
                },
                "options": {
                    "scales": {
                        "y": {
                            "beginAtZero": True,
                            "title": {
                                "display": True,
                                "text": "Count"
                            }
                        },
                        "x": {
                            "title": {
                                "display": True,
                                "text": "Digest"
                            }
                        }
                    },
                    "plugins": {
                        "title": {
                            "display": True,
                            "text": "Top Content Digests"
                        }
                    }
                }
            })

        return charts

    def save_results(self):
        """ذخیره نتایج در فایل‌های JSON، TXT، HTML، CSV و چارت"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        for host in self.hosts:
            base_filename = f"{self.output_dir}/{host}_{timestamp}"

            # ذخیره JSON
            json_data = {
                'host': host,
                'subdomains': self.with_subs,
                'timestamp': timestamp,
                'urls': list(self.urls[host]),
                'stats': dict(self.stats),
                'content_types': dict(self.content_types),
                'parameters': dict(self.params),
                'sensitive_parameters': dict(self.sensitive_params),
                'paths': dict(self.paths),
                'years': dict(self.years),
                'status_codes': dict(self.status_codes),
                'digests': dict(self.digests),
                'lengths': dict(self.lengths),
                'keywords': dict(self.keywords.most_common(10))
            }
            with open(f"{base_filename}.json", 'w', encoding='utf-8') as f:
                json.dump(json_data, f, indent=2, ensure_ascii=False)
                logger.info(f"Saved JSON results to {base_filename}.json", extra={'progress': ''})

            # ذخیره TXT
            with open(f"{base_filename}.txt", 'w', encoding='utf-8') as f:
                for url in self.urls[host]:
                    f.write(f"{url}\n")
                logger.info(f"Saved TXT results to {base_filename}.txt", extra={'progress': ''})

            # ذخیره CSV
            with open(f"{base_filename}.csv", 'w', encoding='utf-8', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(['URL', 'Host', 'Source', 'Timestamp', 'Content-Type', 'Status Code', 'Path', 'Year', 'Digest', 'Length', 'Keywords'])
                with sqlite3.connect(self.db_path) as conn:
                    c = conn.cursor()
                    c.execute('SELECT url, host, source, timestamp, content_type, status_code, path, year, digest, length, keywords FROM urls WHERE host = ?', (host,))
                    for row in c.fetchall():
                        writer.writerow(row)
                    logger.info(f"Saved CSV results to {base_filename}.csv", extra={'progress': ''})

            # ذخیره HTML
            charts = self.generate_charts()
            html_content = f"""
            <html>
            <head>
                <title>Wayback Scraper Report - {host}</title>
                <style>
                    body {{ font-family: Arial, sans-serif; margin: 20px; }}
                    table {{ border-collapse: collapse; width: 100%; }}
                    th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
                    th {{ background-color: #f2f2f2; }}
                    .success {{ color: green; }}
                    .redirect {{ color: orange; }}
                    .error {{ color: red; }}
                </style>
                <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
            </head>
            <body>
                <h1>Wayback Scraper Report for {host}</h1>
                <p>Timestamp: {timestamp}</p>
                <p>Subdomains: {self.with_subs}</p>
                <h2>Statistics</h2>
                <table>
                    <tr><th>Metric</th><th>Value</th></tr>
                    <tr><td>Valid URLs</td><td>{self.stats['valid_urls']}</td></tr>
                    <tr><td>Filtered URLs</td><td>{self.stats['filtered_urls']}</td></tr>
                    <tr><td>Invalid URLs</td><td>{self.stats['invalid_urls']}</td></tr>
                    <tr><td>Fetch Errors</td><td>{self.stats['fetch_errors']}</td></tr>
                </table>
                <h2>Top 5 Extensions</h2>
                <table>
                    <tr><th>Extension</th><th>Count</th></tr>
                    {"".join(f"<tr><td>.{ext[4:]}</td><td>{count}</td></tr>" for ext, count in self.stats.most_common(5) if ext.startswith('ext_'))}
                </table>
                <h2>Top 5 Content-Types</h2>
                <table>
                    <tr><th>Content-Type</th><th>Count</th></tr>
                    {"".join(f"<tr><td>{html.escape(ct)}</td><td>{count}</td></tr>" for ct, count in self.content_types.most_common(5))}
                </table>
                <h2>Top 5 Parameters</h2>
                <table>
                    <tr><th>Parameter</th><th>Count</th></tr>
                    {"".join(f"<tr><td>{html.escape(param)}</td><td>{count}</td></tr>" for param, count in self.params.most_common(5))}
                </table>
                <h2>Top 5 Sensitive Parameters</h2>
                <table>
                    <tr><th>Parameter</th><th>Count</th></tr>
                    {"".join(f"<tr><td>{html.escape(param)}</td><td>{count}</td></tr>" for param, count in self.sensitive_params.most_common(5))}
                </table>
                <h2>Top 5 Digests</h2>
                <table>
                    <tr><th>Digest</th><th>Count</th></tr>
                    {"".join(f"<tr><td>{html.escape(digest)}</td><td>{count}</td></tr>" for digest, count in self.digests.most_common(5))}
                </table>
                <h2>Top 5 Content Lengths</h2>
                <table>
                    <tr><th>Length</th><th>Count</th></tr>
                    {"".join(f"<tr><td>{length}</td><td>{count}</td></tr>" for length, count in self.lengths.most_common(5))}
                </table>
                <h2>Top 5 Keywords</h2>
                <table>
                    <tr><th>Keyword</th><th>Count</th></tr>
                    {"".join(f"<tr><td>{html.escape(keyword)}</td><td>{count}</td></tr>" for keyword, count in self.keywords.most_common(5))}
                </table>
                <h2>Charts</h2>
                {"".join(f'<canvas id="chart_{i}" width="400" height="200"></canvas><script>const ctx{i} = document.getElementById("chart_{i}").getContext("2d"); new Chart(ctx{i}, {json.dumps(chart)});</script>' for i, chart in enumerate(charts))}
            </body>
            </html>
            """
            with open(f"{base_filename}.html", 'w', encoding='utf-8') as f:
                f.write(html_content)
                logger.info(f"Saved HTML report to {base_filename}.html", extra={'progress': ''})

    def run(self):
        """اجرای اسکریپینگ"""
        try:
            self.scrape()
            if any(self.urls.values()):
                self.save_results()
            else:
                logger.warning("No URLs found to save", extra={'progress': ''})
        except KeyboardInterrupt:
            logger.warning("Scraping interrupted by user", extra={'progress': ''})
        except Exception as e:
            logger.error(f"Fatal error: {e}", extra={'progress': ''})
        finally:
            self.session.close()

def main():
    parser = argparse.ArgumentParser(description="Wayback Machine URL Scraper with Colored Logs and Database")
    parser.add_argument("host", help="Target host (e.g., example.com) or 'stdin' for multiple hosts")
    parser.add_argument("--subs", action="store_true", help="Include subdomains")
    parser.add_argument("--output-dir", default="output", help="Output directory")
    parser.add_argument("--cache-dir", default="cache", help="Cache directory")
    parser.add_argument("--db-path", default="wayback.db", help="SQLite database path")
    parser.add_argument("--url-filter", default="", help="Regex pattern to filter URLs")
    parser.add_argument("--threads", type=int, default=2, help="Number of threads (1-5)")
    parser.add_argument("--pages", type=int, default=10, help="Max pages to scrape")
    parser.add_argument("--delay", type=float, default=3.0, help="Delay between requests (seconds)")
    args = parser.parse_args()

    hosts = []
    if args.host.lower() == "stdin":
        for line in sys.stdin:
            host = line.strip()
            if host:
                hosts.append(host)
    else:
        hosts.append(args.host)

    if not hosts:
        logger.error("No valid hosts provided", extra={'progress': ''})
        sys.exit(1)

    scraper = WaybackScraper(
        hosts,
        args.subs,
        args.output_dir,
        max_threads=max(1, min(args.threads, 5)),
        max_pages=args.pages,
        delay=args.delay,
        cache_dir=args.cache_dir,
        db_path=args.db_path,
        url_filter=args.url_filter
    )
    scraper.run()

if __name__ == "__main__":
    main()
