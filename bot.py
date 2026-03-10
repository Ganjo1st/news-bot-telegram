"""
🤖 Telegram News Bot - Версия 25.0 - ФИНАЛЬНАЯ ИСПРАВЛЕННАЯ
АБСОЛЮТНАЯ ЗАЩИТА ОТ ДУБЛИКАТОВ + 9111.RU
- Исправлена публикация на 9111.ru
- Удаление авторских прав AP News
- Работает на Railway
"""

import os
import logging
import feedparser
import re
import html
import requests
import time
import random
import hashlib
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Bot
from telegram.error import TelegramError
from deep_translator import GoogleTranslator
import asyncio
import json
import tempfile
import aiohttp
from bs4 import BeautifulSoup
import subprocess
import traceback

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ============================================================
# КОНФИГУРАЦИЯ
# ============================================================
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHANNEL_ID = os.getenv('CHANNEL_ID')
NINTH_EMAIL = os.getenv('NINTH_EMAIL', '')
NINTH_PASSWORD = os.getenv('NINTH_PASSWORD', '')

if not TELEGRAM_TOKEN or not CHANNEL_ID:
    logger.error("❌ TELEGRAM_TOKEN или CHANNEL_ID не заданы!")
    sys.exit(1)

# ХАОТИЧНЫЙ РЕЖИМ
MIN_POST_INTERVAL = 35 * 60
MAX_POST_INTERVAL = 2 * 60 * 60
CHECK_INTERVAL = 30 * 60
MAX_POSTS_PER_DAY = 24
TIMEZONE_OFFSET = 7

# ФАЙЛЫ
SENT_LINKS_FILE = 'sent_links.json'
SENT_HASHES_FILE = 'sent_hashes.json'
SENT_TITLES_FILE = 'sent_titles.json'
SENT_FIRST_SENTENCES_FILE = 'sent_first_sentences.json'
POSTS_LOG_FILE = 'posts_log.json'
TELEGRAM_MAX_CAPTION = 1024

# ИСТОЧНИКИ
ALL_FEEDS = [
    {
        'name': 'InfoBrics',
        'url': 'https://infobrics.org/rss/en',
        'enabled': True,
        'parser': 'infobrics',
        'type': 'rss',
        'priority': 1
    },
    {
        'name': 'Global Research',
        'url': 'https://www.globalresearch.ca/feed',
        'enabled': True,
        'parser': 'globalresearch',
        'type': 'rss',
        'priority': 2
    },
    {
        'name': 'AP News',
        'url': 'https://apnews.com/',
        'enabled': True,
        'type': 'html_apnews_v2',
        'priority': 1
    }
]

class NewsBot:
    def __init__(self):
        # Создаем файлы
        for file in [SENT_LINKS_FILE, SENT_HASHES_FILE, SENT_TITLES_FILE, SENT_FIRST_SENTENCES_FILE, POSTS_LOG_FILE]:
            if not os.path.exists(file):
                with open(file, 'w', encoding='utf-8') as f:
                    json.dump([], f)
                logger.info(f"📁 Создан файл {file}")

        self.bot = Bot(token=TELEGRAM_TOKEN)
        self.translator = GoogleTranslator(source='en', target='ru')
        self.scheduler = AsyncIOScheduler()
        
        # Загружаем данные
        self.sent_links = self.load_set(SENT_LINKS_FILE)
        self.sent_hashes = self.load_set(SENT_HASHES_FILE)
        self.sent_titles = self.load_set(SENT_TITLES_FILE)
        self.sent_first_sentences = self.load_set(SENT_FIRST_SENTENCES_FILE)
        self.posts_log = self.load_json(POSTS_LOG_FILE)
        
        self.session = None
        self.last_post_time = None
        self.post_queue = []
        self.telegram_titles_cache = []
        
        # Проверяем наличие Chrome (необязательно)
        self.chrome_path = self._find_chrome()
        
        # Проверяем переменные
        self.ninth_available = bool(NINTH_EMAIL and NINTH_PASSWORD)
        
        logger.info(f"📊 Загружено {len(self.sent_links)} ссылок")
        logger.info(f"📊 Загружено {len(self.sent_hashes)} хешей")
        logger.info(f"📊 Загружено {len(self.sent_titles)} заголовков")
        logger.info(f"📊 Загружено {len(self.sent_first_sentences)} первых предложений")
        logger.info(f"📊 Загружено {len(self.posts_log)} записей в логе")
        logger.info(f"🌐 Chrome: {'✅ найден' if self.chrome_path else '❌ не найден'}")
        logger.info(f"🌐 9111.ru: {'✅ ДОСТУПЕН' if self.ninth_available else '❌ НЕДОСТУПЕН'}")

    def _find_chrome(self):
        paths = [
            '/usr/bin/google-chrome',
            '/usr/bin/chromium',
            '/usr/bin/chromium-browser',
        ]
        for path in paths:
            if os.path.exists(path):
                try:
                    version = subprocess.check_output([path, '--version'], text=True).strip()
                    logger.info(f"✅ Chrome найден: {path} ({version})")
                except:
                    logger.info(f"✅ Chrome найден: {path}")
                return path
        logger.info("ℹ️ Chrome не найден (для 9111.ru не требуется)")
        return None

    def load_json(self, filename):
        try:
            with open(filename, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return []

    def load_set(self, filename):
        try:
            if os.path.exists(filename):
                with open(filename, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return set(data) if isinstance(data, list) else set()
            return set()
        except:
            return set()

    def save_set(self, filename, data):
        try:
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(list(data), f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"❌ Ошибка сохранения {filename}: {e}")

    def save_json(self, filename, data):
        try:
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"❌ Ошибка сохранения {filename}: {e}")

    def normalize_title(self, title):
        if not title:
            return ""
        title = title.lower()
        title = re.sub(r'[^\w\s]', '', title)
        title = re.sub(r'\s+', ' ', title).strip()
        common_words = ['the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by']
        words = title.split()
        words = [w for w in words if w not in common_words]
        return ' '.join(words)[:100]

    def create_content_hash(self, content):
        if not content:
            return None
        return hashlib.md5(content[:500].encode('utf-8')).hexdigest()

    def extract_first_sentence(self, content):
        if not content:
            return ""
        match = re.search(r'^.*?[.!?]', content)
        if match:
            return match.group(0).lower().strip()
        return content[:100].lower().strip()

    def calculate_title_similarity(self, title1, title2):
        if not title1 or not title2:
            return 0
        t1 = ' '.join(title1.lower().split())
        t2 = ' '.join(title2.lower().split())
        if t1 == t2:
            return 100.0
        words1 = set(t1.split())
        words2 = set(t2.split())
        common = words1.intersection(words2)
        if not common:
            return 0
        similarity = (len(common) / max(len(words1), len(words2))) * 100
        len_ratio = min(len(t1), len(t2)) / max(len(t1), len(t2))
        return round(similarity * len_ratio, 2)

    def remove_copyright_sentences(self, text):
        if not text:
            return text
        phrases = [
            r'авторские? права? (?:принадлежат|защищены)',
            r'все права защищены',
            r'ассошиэйтед пресс',
            r'© ap',
            r'© associated press',
            r'copyright (?:©)?\s*(?:the)?\s*associated press',
            r'copyright (?:©)?\s*ap',
            r'all rights reserved',
            r'this material may not be published',
            r'associated press (?:text|photo|video)',
            r'ap (?:text|photo|video)',
        ]
        sentences = re.split(r'(?<=[.!?])\s+', text)
        filtered = []
        removed = 0
        for s in sentences:
            lower = s.lower()
            if not any(re.search(p, lower, re.IGNORECASE) for p in phrases):
                filtered.append(s)
            else:
                removed += 1
        if removed:
            logger.info(f"🗑️ Удалено {removed} предложений с авторскими правами")
        return ' '.join(filtered)

    def remove_metadata(self, text):
        if not text:
            return text
        text = re.sub(r'\d+\s*(hour|min|sec|day|minute|second)s?\s+ago', '', text, flags=re.IGNORECASE)
        text = re.sub(r'Updated\s*:?\s*[\d:APM\s-]+', '', text, flags=re.IGNORECASE)
        text = re.sub(r'Published\s*:?\s*[\d:APM\s-]+', '', text, flags=re.IGNORECASE)
        text = re.sub(r'^By\s+[\w\s,]+\n', '', text, flags=re.IGNORECASE | re.MULTILINE)
        garbage = ['Subscribe', 'Newsletter', 'Sign up', 'Follow us', 'Share this', 'Read more', 'Comments', 'Advertisement']
        for word in garbage:
            text = re.sub(word, '', text, flags=re.IGNORECASE)
        return re.sub(r'\n{3,}', '\n\n', text).strip()

    def clean_text(self, text):
        if not text:
            return ""
        text = html.unescape(text)
        text = re.sub(r'<[^>]+>', '', text)
        text = re.sub(r'\n\s*\n\s*\n', '\n\n', text)
        text = re.sub(r' +', ' ', text)
        return self.remove_metadata(text).strip()

    def escape_html_for_telegram(self, text):
        if not text:
            return ""
        return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    def truncate_text_by_sentences(self, text, max_length):
        if len(text) <= max_length:
            return text
        sentences = re.split(r'(?<=[.!?])\s+', text)
        result = []
        length = 0
        for s in sentences:
            s_len = len(s)
            if length + s_len + 2 <= max_length:
                if result:
                    result.append(' ' + s)
                    length += s_len + 1
                else:
                    result.append(s)
                    length += s_len
            else:
                break
        return ''.join(result)

    def format_telegram_post(self, title, content):
        title_escaped = self.escape_html_for_telegram(title)
        content_escaped = self.escape_html_for_telegram(content)
        header = f"<b>{title_escaped}</b>"
        max_content = TELEGRAM_MAX_CAPTION - len(header) - 10
        if len(content_escaped) > max_content:
            content_escaped = self.truncate_text_by_sentences(content_escaped, max_content)
        return f"{header}\n\n{content_escaped}"

    def can_post_now(self):
        hour = (datetime.now().hour + TIMEZONE_OFFSET) % 24
        if 23 <= hour or hour < 7:
            logger.info(f"🌙 Ночное время ({hour}:00)")
            return False
        today = datetime.now().date()
        today_posts = 0
        for post in self.posts_log:
            try:
                post_date = datetime.fromisoformat(post['time'].split('.')[0]).date()
                if post_date == today:
                    today_posts += 1
            except:
                continue
        if today_posts >= MAX_POSTS_PER_DAY:
            logger.info(f"⏳ Дневной лимит {MAX_POSTS_PER_DAY}")
            return False
        if len(self.posts_log) >= 2:
            try:
                last_two = sorted([datetime.fromisoformat(p['time'].split('.')[0]) for p in self.posts_log[-2:]])
                if len(last_two) == 2 and (last_two[1] - last_two[0]) < timedelta(minutes=35):
                    next_allowed = last_two[0] + timedelta(minutes=35)
                    wait = (next_allowed - datetime.now()).total_seconds() / 60
                    if wait > 0:
                        logger.info(f"⏳ Лимит частоты: {wait:.0f} мин")
                        return False
            except:
                pass
        return True

    def get_next_post_delay(self):
        return random.randint(MIN_POST_INTERVAL, MAX_POST_INTERVAL)

    def log_post(self, link, title):
        self.posts_log.append({
            'link': link,
            'title': title[:50],
            'time': datetime.now().isoformat()
        })
        if len(self.posts_log) > 100:
            self.posts_log = self.posts_log[-100:]
        self.save_json(POSTS_LOG_FILE, self.posts_log)
        self.last_post_time = datetime.now()

    async def get_session(self):
        if not self.session:
            self.session = aiohttp.ClientSession()
        return self.session

    async def load_telegram_titles_cache(self):
        try:
            logger.info("📚 Загрузка заголовков...")
            self.telegram_titles_cache = []
            
            if os.path.exists(POSTS_LOG_FILE):
                with open(POSTS_LOG_FILE, 'r', encoding='utf-8') as f:
                    posts = json.load(f)
                    for post in posts:
                        if post.get('title'):
                            self.telegram_titles_cache.append(post['title'])
                logger.info(f"📁 Загружено {len(self.telegram_titles_cache)} из файла")
            
            try:
                updates = await self.bot.get_updates(timeout=5, limit=50)
                for update in updates:
                    if update.channel_post:
                        msg = update.channel_post
                        if msg.caption:
                            title = re.sub(r'<[^>]+>', '', msg.caption).split('\n')[0].strip()
                        elif msg.text:
                            title = re.sub(r'<[^>]+>', '', msg.text).split('\n')[0].strip()
                        else:
                            continue
                        if title and len(title) > 10 and title not in self.telegram_titles_cache:
                            self.telegram_titles_cache.append(title)
                logger.info(f"📨 Получено обновлений: {len(updates)}")
            except Exception as e:
                logger.warning(f"⚠️ Ошибка получения обновлений: {e}")
            
            logger.info(f"✅ Всего загружено {len(self.telegram_titles_cache)} заголовков")
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки заголовков: {e}")

    async def check_telegram_duplicate(self, new_title):
        if not self.telegram_titles_cache:
            await self.load_telegram_titles_cache()
            if not self.telegram_titles_cache:
                return False, 0
        max_sim = 0
        for existing in self.telegram_titles_cache:
            sim = self.calculate_title_similarity(new_title, existing)
            max_sim = max(max_sim, sim)
            if sim >= 75:
                logger.info(f"⏭️ ДУБЛИКАТ ({sim}%): {new_title[:50]}...")
                return True, sim
        logger.info(f"✅ Макс. сходство: {max_sim}%")
        return False, max_sim

    def is_duplicate(self, article_data):
        url = article_data.get('link', '')
        title = article_data.get('title', '')
        content = article_data.get('content', '')
        
        if url in self.sent_links:
            logger.info(f"⏭️ ДУБЛИКАТ (URL)")
            return True
        norm = self.normalize_title(title)
        if norm and norm in self.sent_titles:
            logger.info(f"⏭️ ДУБЛИКАТ (заголовок)")
            return True
        if content:
            h = self.create_content_hash(content)
            if h and h in self.sent_hashes:
                logger.info(f"⏭️ ДУБЛИКАТ (содержимое)")
                return True
        if content:
            first = self.extract_first_sentence(content)
            if first and len(first) > 20 and first in self.sent_first_sentences:
                logger.info(f"⏭️ ДУБЛИКАТ (первое предложение)")
                return True
        return False

    def mark_as_sent(self, article_data):
        url = article_data.get('link', '')
        title = article_data.get('title', '')
        content = article_data.get('content', '')
        
        if url:
            self.sent_links.add(url)
            self.save_set(SENT_LINKS_FILE, self.sent_links)
        norm = self.normalize_title(title)
        if norm:
            self.sent_titles.add(norm)
            self.save_set(SENT_TITLES_FILE, self.sent_titles)
        if content:
            h = self.create_content_hash(content)
            if h:
                self.sent_hashes.add(h)
                self.save_set(SENT_HASHES_FILE, self.sent_hashes)
        if content:
            first = self.extract_first_sentence(content)
            if first and len(first) > 20:
                self.sent_first_sentences.add(first)
                self.save_set(SENT_FIRST_SENTENCES_FILE, self.sent_first_sentences)
        logger.info(f"✅ Статья помечена как отправленная")

    def get_apnews_articles(self):
        try:
            logger.info("🌐 Парсинг AP News")
            r = requests.get('https://apnews.com/', headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
            if r.status_code != 200:
                return []
            soup = BeautifulSoup(r.text, 'html.parser')
            articles = []
            for link in soup.find_all('a', href=True):
                href = link['href']
                if '/article/' in href:
                    url = href if href.startswith('http') else f"https://apnews.com{href}"
                    title = link.get_text(strip=True)
                    if title and len(title) > 15:
                        articles.append({'url': url, 'title': title})
            unique = []
            seen = set()
            for a in articles:
                if a['url'] not in seen:
                    seen.add(a['url'])
                    unique.append(a)
            logger.info(f"✅ Найдено {len(unique)} статей")
            return unique[:10]
        except Exception as e:
            logger.error(f"❌ Ошибка AP News: {e}")
            return []

    def parse_apnews_article(self, url):
        try:
            r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
            soup = BeautifulSoup(r.text, 'html.parser')
            title = None
            meta = soup.find('meta', property='og:title')
            if meta and meta.get('content'):
                title = meta['content']
            else:
                h1 = soup.find('h1')
                if h1:
                    title = h1.get_text(strip=True)
            if not title:
                return None
            title = re.sub(r'\s*\|.*AP\s*News.*$', '', title, flags=re.IGNORECASE)
            title = self.clean_text(title)
            img = None
            meta_img = soup.find('meta', property='og:image')
            if meta_img and meta_img.get('content'):
                img = meta_img['content']
            paragraphs = [p.get_text(strip=True) for p in soup.find_all('p') if len(p.get_text(strip=True)) > 20]
            text = '\n\n'.join(paragraphs[:20])
            if len(text) < 200:
                return None
            return {'title': title, 'content': text, 'main_image': img}
        except Exception as e:
            logger.error(f"❌ Ошибка парсинга: {e}")
            return None

    def parse_infobrics(self, url):
        try:
            r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
            soup = BeautifulSoup(r.text, 'html.parser')
            title = soup.find('h1')
            title = self.clean_text(title.get_text()) if title else "Без заголовка"
            img = None
            meta = soup.find('meta', property='og:image')
            if meta and meta.get('content'):
                img = meta['content']
            paragraphs = [self.clean_text(p.get_text()) for p in soup.find_all('p') if len(self.clean_text(p.get_text())) > 15]
            text = '\n\n'.join(paragraphs[:15])
            if len(text) < 200:
                return None
            return {'title': title, 'content': text, 'main_image': img}
        except Exception as e:
            logger.error(f"❌ Ошибка InfoBrics: {e}")
            return None

    def parse_globalresearch(self, url):
        try:
            r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
            soup = BeautifulSoup(r.text, 'html.parser')
            title = None
            meta = soup.find('meta', property='og:title')
            if meta and meta.get('content'):
                title = meta['content']
            else:
                h1 = soup.find('h1')
                if h1:
                    title = h1.get_text(strip=True)
                else:
                    title_tag = soup.find('title')
                    title = title_tag.get_text(strip=True) if title_tag else "Без заголовка"
            title = self.clean_text(title)
            img = None
            meta_img = soup.find('meta', property='og:image')
            if meta_img and meta_img.get('content'):
                img = meta_img['content']
            paragraphs = [p.get_text(strip=True) for p in soup.find_all('p') if len(p.get_text(strip=True)) > 20]
            text = '\n\n'.join(paragraphs[:20])
            if len(text) < 200:
                return None
            return {'title': title, 'content': text, 'main_image': img}
        except Exception as e:
            logger.error(f"❌ Ошибка Global Research: {e}")
            return None

    async def fetch_from_apnews(self):
        try:
            articles = await asyncio.get_event_loop().run_in_executor(None, self.get_apnews_articles)
            items = []
            for a in articles[:3]:
                if a['url'] in self.sent_links:
                    logger.info(f"⏭️ УЖЕ БЫЛО (URL)")
                    continue
                logger.info(f"🔍 НОВАЯ: {a['title'][:50]}...")
                data = await asyncio.get_event_loop().run_in_executor(None, self.parse_apnews_article, a['url'])
                if data:
                    if self.is_duplicate({'link': a['url'], 'title': data['title'], 'content': data['content']}):
                        continue
                    logger.info("🔄 Перевод заголовка...")
                    title_ru = await asyncio.get_event_loop().run_in_executor(None, self.translate_text, data['title'])
                    is_dup, _ = await self.check_telegram_duplicate(title_ru)
                    if is_dup:
                        logger.warning(f"⏭️ ДУБЛИКАТ в Telegram")
                        continue
                    logger.info("🔄 Перевод текста...")
                    content_ru = await asyncio.get_event_loop().run_in_executor(None, self.translate_text, data['content'])
                    content_ru = self.remove_copyright_sentences(content_ru)
                    items.append({
                        'source': 'AP News',
                        'title_original': data['title'],
                        'title_ru': title_ru,
                        'content_ru': content_ru,
                        'link': a['url'],
                        'main_image': data.get('main_image'),
                        'priority': 1,
                        'timestamp': datetime.now().isoformat()
                    })
                    logger.info(f"✅ Статья готова")
                await asyncio.sleep(random.randint(2, 5))
            return items
        except Exception as e:
            logger.error(f"❌ Ошибка AP News: {e}")
            return []

    async def fetch_from_rss(self, feed_config):
        try:
            name = feed_config['name']
            parser = feed_config.get('parser', 'infobrics')
            func = self.parse_infobrics if parser == 'infobrics' else self.parse_globalresearch
            feed = feedparser.parse(feed_config['url'])
            if feed.bozo:
                logger.error(f"❌ Ошибка RSS {name}")
                return []
            logger.info(f"📰 RSS: {len(feed.entries)} статей")
            items = []
            for entry in feed.entries[:3]:
                link = entry.get('link', '')
                title = entry.get('title', '')
                if link in self.sent_links:
                    logger.info(f"⏭️ УЖЕ БЫЛО (URL)")
                    continue
                logger.info(f"🔍 НОВАЯ: {title[:50]}...")
                data = await asyncio.get_event_loop().run_in_executor(None, func, link)
                if data:
                    if self.is_duplicate({'link': link, 'title': data['title'], 'content': data['content']}):
                        continue
                    logger.info("🔄 Перевод заголовка...")
                    title_ru = await asyncio.get_event_loop().run_in_executor(None, self.translate_text, data['title'])
                    is_dup, _ = await self.check_telegram_duplicate(title_ru)
                    if is_dup:
                        logger.warning(f"⏭️ ДУБЛИКАТ в Telegram")
                        continue
                    logger.info("🔄 Перевод текста...")
                    content_ru = await asyncio.get_event_loop().run_in_executor(None, self.translate_text, data['content'])
                    items.append({
                        'source': name,
                        'title_original': data['title'],
                        'title_ru': title_ru,
                        'content_ru': content_ru,
                        'link': link,
                        'main_image': data.get('main_image'),
                        'priority': feed_config.get('priority', 5),
                        'timestamp': datetime.now().isoformat()
                    })
                    logger.info(f"✅ Статья готова")
                await asyncio.sleep(random.randint(2, 5))
            return items
        except Exception as e:
            logger.error(f"❌ Ошибка RSS: {e}")
            return []

    async def fetch_all_news(self):
        all_news = []
        for feed in ALL_FEEDS:
            if not feed['enabled']:
                continue
            if feed.get('type') == 'html_apnews_v2':
                news = await self.fetch_from_apnews()
            else:
                news = await self.fetch_from_rss(feed)
            all_news.extend(news)
            await asyncio.sleep(random.randint(3, 7))
        all_news.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
        logger.info(f"📊 ГОТОВО: {len(all_news)} статей")
        return all_news

    async def download_image(self, url):
        if not url:
            return None
        try:
            logger.info(f"🖼️ Скачивание: {url[:50]}...")
            fd, path = tempfile.mkstemp(suffix='.jpg')
            os.close(fd)
            session = await self.get_session()
            async with session.get(url, timeout=15) as resp:
                if resp.status == 200:
                    with open(path, 'wb') as f:
                        f.write(await resp.read())
                    logger.info(f"✅ Изображение сохранено")
                    return path
            return None
        except Exception as e:
            logger.error(f"❌ Ошибка скачивания: {e}")
            return None

    def translate_text(self, text):
        try:
            if not text or len(text) < 20:
                return text
            if len(text) > 4000:
                parts = []
                for i in range(0, len(text), 3000):
                    part = text[i:i+3000]
                    try:
                        parts.append(self.translator.translate(part))
                    except:
                        parts.append(part)
                    time.sleep(random.uniform(0.5, 1.5))
                return ' '.join(parts)
            return self.translator.translate(text)
        except Exception as e:
            logger.error(f"❌ Ошибка перевода: {e}")
            return text

    async def publish_to_telegram(self, post_data):
        try:
            caption = self.format_telegram_post(post_data['title_ru'], post_data['content_ru'])
            if post_data['main_image']:
                img = await self.download_image(post_data['main_image'])
                if img:
                    with open(img, 'rb') as photo:
                        await self.bot.send_photo(chat_id=CHANNEL_ID, photo=photo, caption=caption, parse_mode='HTML')
                    try:
                        os.unlink(img)
                    except:
                        pass
                    logger.info("✅ Пост с изображением опубликован")
                else:
                    await self.bot.send_message(chat_id=CHANNEL_ID, text=caption, parse_mode='HTML')
                    logger.info("✅ Пост без изображения опубликован")
            else:
                await self.bot.send_message(chat_id=CHANNEL_ID, text=caption, parse_mode='HTML')
                logger.info("✅ Пост опубликован")
            self.telegram_titles_cache.append(post_data['title_ru'])
            self.log_post(post_data['link'], post_data['title_original'])
            return True
        except TelegramError as e:
            if "Too Many Requests" in str(e):
                logger.warning("⚠️ Лимит Telegram, жду...")
                await asyncio.sleep(60)
            elif "Can't parse entities" in str(e):
                await self.bot.send_message(chat_id=CHANNEL_ID, text=re.sub(r'<[^>]+>', '', caption))
                return True
            logger.error(f"❌ Ошибка Telegram: {e}")
            return False

    # ========== ИСПРАВЛЕННАЯ ПУБЛИКАЦИЯ НА 9111.RU ==========
    def publish_to_9111(self, title, content, source_url):
        """Публикация на 9111.ru через requests (рабочая версия)"""
        if not self.ninth_available:
            logger.warning("⚠️ 9111.ru недоступен (нет логина/пароля)")
            return False
        
        try:
            logger.info("=" * 60)
            logger.info("🌐 ПУБЛИКАЦИЯ НА 9111.RU")
            
            session = requests.Session()
            session.headers.update({
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
            })
            
            # 1. Получаем главную страницу
            logger.info("1️⃣ Получение cookies...")
            main_response = session.get('https://www.9111.ru', timeout=10)
            
            # 2. Авторизация
            logger.info("2️⃣ Авторизация...")
            login_data = {
                'email': NINTH_EMAIL,
                'pass': NINTH_PASSWORD,
                'action': 'login',
                'remember': '1'
            }
            
            login_response = session.post('https://www.9111.ru/ajax/auth.php', 
                                         data=login_data, 
                                         timeout=10,
                                         allow_redirects=True)
            
            if 'success' not in login_response.text.lower():
                logger.error("❌ Ошибка авторизации")
                return False
            
            logger.info("✅ Авторизация успешна")
            
            # 3. Получаем страницу создания
            logger.info("3️⃣ Получение страницы создания...")
            add_page = session.get('https://www.9111.ru/pubs/add/title/', timeout=10)
            
            # 4. Парсим CSRF токен
            soup = BeautifulSoup(add_page.text, 'html.parser')
            csrf = None
            csrf_input = soup.find('input', {'name': 'csrf_token'})
            if csrf_input:
                csrf = csrf_input.get('value')
                logger.info("   Найден CSRF токен")
            
            # 5. Находим ID рубрики "Новости"
            rubric_id = '382235'  # значение по умолчанию
            rubric_select = soup.find('select', {'id': 'rubric_id2'})
            if rubric_select:
                for opt in rubric_select.find_all('option'):
                    if 'Новости' in opt.text:
                        rubric_id = opt.get('value')
                        logger.info(f"   Найдена рубрика 'Новости' (ID: {rubric_id})")
                        break
            
            # 6. Отправка публикации
            logger.info("4️⃣ Отправка публикации...")
            post_data = {
                'topic_name': title[:150],
                'komm': f"{content}\n\nИсточник: {source_url}"[:5000],
                'rubric_id': rubric_id,
                'tag_list_input': 'новости, политика, экономика',
                'title_tags': 'новости, политика, экономика',
                'title_private': 'off',
                'submit': 'Опубликовать'
            }
            if csrf:
                post_data['csrf_token'] = csrf
            
            result = session.post('https://www.9111.ru/pubs/add/title/', 
                                 data=post_data, 
                                 timeout=15,
                                 allow_redirects=True)
            
            if result.status_code == 200:
                if 'Спасибо' in result.text or 'опубликована' in result.text:
                    logger.info("✅ Статья опубликована на 9111.ru")
                    return True
                else:
                    logger.warning("⚠️ Статья отправлена, но ответ неясен")
                    return True
            else:
                logger.error(f"❌ Ошибка HTTP {result.status_code}")
                return False
                
        except Exception as e:
            logger.error(f"❌ Ошибка: {e}")
            return False

    async def publish_post(self, post_data):
        try:
            # Публикация в Telegram
            tg_ok = await self.publish_to_telegram(post_data)
            if not tg_ok:
                logger.error("❌ Не удалось опубликовать в Telegram")
                return False
            
            # Публикация на 9111.ru
            if self.ninth_available:
                logger.info("🔄 Начинаю публикацию на 9111.ru...")
                loop = asyncio.get_event_loop()
                success = await loop.run_in_executor(
                    None, self.publish_to_9111,
                    post_data['title_ru'],
                    post_data['content_ru'],
                    post_data['link']
                )
                if success:
                    logger.info("✅ Пост опубликован на 9111.ru")
                else:
                    logger.warning("⚠️ Ошибка публикации на 9111.ru")
            else:
                logger.info("⏭️ Пропускаю 9111.ru (нет данных для входа)")
            
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка публикации: {e}")
            return False

    async def process_and_publish(self):
        logger.info("=" * 60)
        logger.info(f"🔍 ЦИКЛ: {datetime.now()}")
        logger.info("=" * 60)

        await self.load_telegram_titles_cache()
        
        news = await self.fetch_all_news()
        if not news:
            logger.info("📭 НЕТ НОВЫХ СТАТЕЙ")
            return

        self.post_queue.extend(news)
        self.post_queue.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
        logger.info(f"📦 В ОЧЕРЕДИ: {len(self.post_queue)}")

        await self.try_publish_from_queue()

    async def try_publish_from_queue(self):
        if not self.post_queue:
            return
        if not self.can_post_now():
            next_try = self.get_next_post_delay()
            logger.info(f"⏰ Следующая попытка через {next_try//60} мин")
            asyncio.create_task(self._schedule_next_try(next_try))
            return
        item = self.post_queue.pop(0)
        logger.info(f"\n📝 ПУБЛИКАЦИЯ: {item['title_ru'][:50]}...")
        logger.info(f"   Источник: {item['source']}")
        
        success = await self.publish_post(item)
        if success:
            self.mark_as_sent({
                'link': item['link'],
                'title': item['title_original'],
                'content': item['content_ru']
            })
            next_delay = self.get_next_post_delay()
            logger.info(f"⏰ Следующая через {next_delay//60} мин")
            asyncio.create_task(self._schedule_next_try(next_delay))
        else:
            logger.error(f"❌ Ошибка публикации")
            self.post_queue.insert(0, item)
            asyncio.create_task(self._schedule_next_try(300))

    async def _schedule_next_try(self, delay):
        await asyncio.sleep(delay)
        await self.try_publish_from_queue()

    async def start(self):
        logger.info("=" * 80)
        logger.info("🚀 NEWS BOT 25.0 - ФИНАЛЬНАЯ ВЕРСИЯ")
        logger.info("=" * 80)
        logger.info(f"📢 Канал: {CHANNEL_ID}")
        logger.info(f"⏱ Интервал: {MIN_POST_INTERVAL//60}-{MAX_POST_INTERVAL//60} мин")
        logger.info(f"🛡️ Лимит: {MAX_POSTS_PER_DAY} постов/день")
        logger.info(f"🌐 9111.ru: {'✅ ДОСТУПЕН' if self.ninth_available else '❌ НЕДОСТУПЕН'}")
        logger.info("=" * 80)

        try:
            me = await self.bot.get_me()
            logger.info(f"✅ Бот @{me.username}")
        except Exception as e:
            logger.error(f"❌ Ошибка: {e}")
            return

        await self.process_and_publish()

        self.scheduler.add_job(
            self.process_and_publish,
            'interval',
            seconds=CHECK_INTERVAL,
            id='news_processor'
        )
        self.scheduler.start()
        logger.info(f"✅ Планировщик: каждые {CHECK_INTERVAL//60} мин")

        try:
            while True:
                await asyncio.sleep(60)
        except KeyboardInterrupt:
            logger.info("🛑 Остановка")
            if self.session:
                await self.session.close()

async def main():
    bot = NewsBot()
    await bot.start()

if __name__ == "__main__":
    asyncio.run(main())
