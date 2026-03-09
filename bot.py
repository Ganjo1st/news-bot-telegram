"""
🤖 Telegram News Bot - Версия 20.0
АБСОЛЮТНАЯ ЗАЩИТА ОТ ДУБЛИКАТОВ + 9111.RU (ЧЕРЕЗ REQUESTS)
- Удаление авторских прав AP News
- Публикация на 9111.ru без Selenium
- Исправлены все ошибки
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
from urllib.parse import urlparse
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

# Данные для 9111.ru
NINTH_EMAIL = os.getenv('NINTH_EMAIL', '')
NINTH_PASSWORD = os.getenv('NINTH_PASSWORD', '')

# Проверка обязательных переменных
if not TELEGRAM_TOKEN or not CHANNEL_ID:
    logger.error("❌ TELEGRAM_TOKEN или CHANNEL_ID не заданы!")
    sys.exit(1)

# ХАОТИЧНЫЙ РЕЖИМ (в секундах)
MIN_POST_INTERVAL = 35 * 60      # 35 минут
MAX_POST_INTERVAL = 2 * 60 * 60  # 2 часа
CHECK_INTERVAL = 30 * 60         # 30 минут
MAX_POSTS_PER_DAY = 24
TIMEZONE_OFFSET = 7

# ============================================================
# ИСТОЧНИКИ
# ============================================================
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

# ============================================================
# ФАЙЛЫ ДЛЯ ХРАНЕНИЯ ДАННЫХ
# ============================================================
SENT_LINKS_FILE = 'sent_links.json'
SENT_HASHES_FILE = 'sent_hashes.json'
SENT_TITLES_FILE = 'sent_titles.json'
SENT_FIRST_SENTENCES_FILE = 'sent_first_sentences.json'
POSTS_LOG_FILE = 'posts_log.json'
TELEGRAM_MAX_CAPTION = 1024

# ============================================================
# ОСНОВНОЙ КЛАСС БОТА
# ============================================================
class NewsBot:
    def __init__(self):
        # Создаем файлы если их нет
        for file in [SENT_LINKS_FILE, SENT_HASHES_FILE, SENT_TITLES_FILE, SENT_FIRST_SENTENCES_FILE, POSTS_LOG_FILE]:
            if not os.path.exists(file):
                with open(file, 'w', encoding='utf-8') as f:
                    json.dump([], f)
                logger.info(f"📁 Создан файл {file}")

        self.bot = Bot(token=TELEGRAM_TOKEN)
        self.translator = GoogleTranslator(source='en', target='ru')
        self.scheduler = AsyncIOScheduler()
        
        # Загружаем все базы данных
        self.sent_links = self.load_set(SENT_LINKS_FILE)
        self.sent_hashes = self.load_set(SENT_HASHES_FILE)
        self.sent_titles = self.load_set(SENT_TITLES_FILE)
        self.sent_first_sentences = self.load_set(SENT_FIRST_SENTENCES_FILE)
        self.posts_log = self.load_json(POSTS_LOG_FILE)
        
        self.session = None
        self.last_post_time = None
        self.post_queue = []
        
        # Кэш заголовков из Telegram
        self.telegram_titles_cache = []
        
        # Проверяем наличие Chrome (не обязателен теперь)
        self.chrome_path = self._find_chrome()
        
        # Отладка переменных
        self._debug_env_vars()
        
        # Проверяем доступность 9111.ru
        self.ninth_available = bool(NINTH_EMAIL and NINTH_PASSWORD)
        
        logger.info(f"📊 Загружено {len(self.sent_links)} ссылок")
        logger.info(f"📊 Загружено {len(self.sent_hashes)} хешей")
        logger.info(f"📊 Загружено {len(self.sent_titles)} заголовков")
        logger.info(f"📊 Загружено {len(self.sent_first_sentences)} первых предложений")
        logger.info(f"📊 Загружено {len(self.posts_log)} записей в логе")

    def _debug_env_vars(self):
        """Отладка переменных окружения"""
        logger.info("=" * 50)
        logger.info("🔍 ПРОВЕРКА ПЕРЕМЕННЫХ:")
        logger.info(f"📧 NINTH_EMAIL: {NINTH_EMAIL}")
        logger.info(f"🔑 NINTH_PASSWORD: {'*' * len(NINTH_PASSWORD) if NINTH_PASSWORD else 'не задан'}")
        logger.info("=" * 50)

    def _find_chrome(self) -> str:
        """Ищет Chrome в системе (не обязательно для работы)"""
        paths = [
            '/usr/bin/google-chrome',
            '/usr/bin/chromium',
            '/usr/bin/chromium-browser',
            '/usr/bin/google-chrome-stable',
        ]
        
        for path in paths:
            if os.path.exists(path):
                try:
                    version = subprocess.check_output([path, '--version'], text=True).strip()
                    logger.info(f"✅ Chrome найден: {path} ({version})")
                except:
                    logger.info(f"✅ Chrome найден: {path}")
                return path
        
        logger.info("ℹ️ Chrome не требуется для публикации на 9111.ru")
        return None

    # ========== РАБОТА С JSON ==========
    def load_json(self, filename):
        try:
            with open(filename, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки {filename}: {e}")
            return []

    def load_set(self, filename):
        try:
            if os.path.exists(filename):
                with open(filename, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return set(data) if isinstance(data, list) else set()
            return set()
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки {filename}: {e}")
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

    # ========== СОЗДАНИЕ УНИКАЛЬНЫХ КЛЮЧЕЙ ==========
    def normalize_title(self, title):
        """Нормализует заголовок для сравнения"""
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
        """Создает хеш содержимого"""
        if not content:
            return None
        
        sample = content[:500].encode('utf-8')
        return hashlib.md5(sample).hexdigest()

    def extract_first_sentence(self, content):
        """Извлекает первое предложение"""
        if not content:
            return ""
        
        match = re.search(r'^.*?[.!?]', content)
        if match:
            first_sentence = match.group(0)
        else:
            first_sentence = content[:100]
        
        first_sentence = first_sentence.lower().strip()
        first_sentence = re.sub(r'\s+', ' ', first_sentence)
        
        return first_sentence

    def calculate_title_similarity(self, title1, title2):
        """
        Сравнение заголовков
        """
        if not title1 or not title2:
            return 0
        
        t1 = ' '.join(title1.lower().split())
        t2 = ' '.join(title2.lower().split())
        
        if t1 == t2:
            return 100.0
        
        words1 = set(t1.split())
        words2 = set(t2.split())
        
        common_words = words1.intersection(words2)
        if not common_words:
            return 0
        
        similarity = (len(common_words) / max(len(words1), len(words2))) * 100
        len_ratio = min(len(t1), len(t2)) / max(len(t1), len(t2))
        similarity = similarity * len_ratio
        
        return round(similarity, 2)

    # ========== УДАЛЕНИЕ АВТОРСКИХ ПРАВ ==========
    def remove_copyright_sentences(self, text):
        """
        Удаляет предложения, содержащие фразы об авторских правах
        """
        if not text:
            return text
        
        copyright_phrases = [
            r'авторские? права? (?:принадлежат|защищены)',
            r'все права защищены',
            r'ассошиэйтед пресс',
            r'© ap',
            r'© associated press',
            r'перепечатка без разрешения запрещена',
            r'copyright (?:©)?\s*(?:the)?\s*associated press',
            r'copyright (?:©)?\s*ap',
            r'all rights reserved',
            r'this material may not be published',
            r'this material is protected by copyright',
            r'used with permission',
            r'associated press (?:text|photo|video)',
            r'ap (?:text|photo|video)',
            r'photos? (?:by|from) ap',
            r'video (?:by|from) ap',
            r'reproduction without permission is prohibited',
            r'redistribution without permission is prohibited',
        ]
        
        sentences = re.split(r'(?<=[.!?])\s+', text)
        original_count = len(sentences)
        
        filtered_sentences = []
        removed_count = 0
        
        for sentence in sentences:
            sentence_lower = sentence.lower()
            should_remove = False
            
            for phrase in copyright_phrases:
                if re.search(phrase, sentence_lower, re.IGNORECASE):
                    should_remove = True
                    removed_count += 1
                    logger.debug(f"🗑️ Удалено: {sentence[:100]}...")
                    break
            
            if not should_remove:
                filtered_sentences.append(sentence)
        
        cleaned_text = ' '.join(filtered_sentences)
        
        if removed_count > 0:
            logger.info(f"🗑️ Удалено {removed_count} предложений с авторскими правами")
        
        return cleaned_text

    # ========== ЗАГРУЗКА ЗАГОЛОВКОВ ИЗ TELEGRAM ==========
    async def load_telegram_titles_cache(self):
        """
        Загрузка заголовков из Telegram и из файла лога
        """
        try:
            logger.info("📚 Загрузка заголовков из Telegram и файла...")
            self.telegram_titles_cache = []
            
            # 1. Загружаем из файла лога
            if os.path.exists(POSTS_LOG_FILE):
                with open(POSTS_LOG_FILE, 'r', encoding='utf-8') as f:
                    posts = json.load(f)
                    for post in posts:
                        if 'title' in post and post['title']:
                            self.telegram_titles_cache.append(post['title'])
                logger.info(f"📁 Загружено {len(self.telegram_titles_cache)} заголовков из файла")
            
            # 2. Загружаем из Telegram
            try:
                updates = await self.bot.get_updates(timeout=30, limit=100)
                
                for update in updates:
                    if update.channel_post:
                        msg = update.channel_post
                        
                        if msg.caption:
                            clean = re.sub(r'<[^>]+>', '', msg.caption)
                            title = clean.split('\n')[0].strip()
                        elif msg.text:
                            clean = re.sub(r'<[^>]+>', '', msg.text)
                            title = clean.split('\n')[0].strip()
                        else:
                            continue
                        
                        if title and len(title) > 10:
                            if title not in self.telegram_titles_cache:
                                self.telegram_titles_cache.append(title)
                
                logger.info(f"📨 Из Telegram получено обновлений: {len(updates)}")
                
            except Exception as e:
                logger.warning(f"⚠️ Не удалось получить обновления из Telegram: {e}")
            
            logger.info(f"✅ Всего загружено {len(self.telegram_titles_cache)} заголовков")
            
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки заголовков: {e}")

    async def check_telegram_duplicate(self, new_title):
        """
        Проверка дубликата в Telegram
        """
        if not self.telegram_titles_cache:
            await self.load_telegram_titles_cache()
            
            if not self.telegram_titles_cache:
                logger.warning("⚠️ Кэш заголовков пуст, пропускаю проверку")
                return False, 0
        
        max_similarity = 0
        
        for existing_title in self.telegram_titles_cache:
            similarity = self.calculate_title_similarity(new_title, existing_title)
            
            if similarity > max_similarity:
                max_similarity = similarity
            
            if similarity >= 75:
                logger.info(f"⏭️ ДУБЛИКАТ ({similarity}%): {new_title[:50]}...")
                return True, similarity
        
        logger.info(f"✅ Макс. сходство: {max_similarity}%")
        return False, max_similarity

    def is_duplicate(self, article_data):
        """Проверка дубликата в истории"""
        url = article_data.get('link', '')
        title = article_data.get('title', '')
        content = article_data.get('content', '')
        
        if url in self.sent_links:
            logger.info(f"⏭️ ДУБЛИКАТ (URL)")
            return True
        
        norm_title = self.normalize_title(title)
        if norm_title and norm_title in self.sent_titles:
            logger.info(f"⏭️ ДУБЛИКАТ (заголовок)")
            return True
        
        if content:
            content_hash = self.create_content_hash(content)
            if content_hash and content_hash in self.sent_hashes:
                logger.info(f"⏭️ ДУБЛИКАТ (содержимое)")
                return True
        
        if content:
            first_sentence = self.extract_first_sentence(content)
            if first_sentence and len(first_sentence) > 20:
                if first_sentence in self.sent_first_sentences:
                    logger.info(f"⏭️ ДУБЛИКАТ (первое предложение)")
                    return True
        
        return False

    def mark_as_sent(self, article_data):
        """Помечает статью как отправленную"""
        url = article_data.get('link', '')
        title = article_data.get('title', '')
        content = article_data.get('content', '')
        
        if url:
            self.sent_links.add(url)
            self.save_set(SENT_LINKS_FILE, self.sent_links)
        
        norm_title = self.normalize_title(title)
        if norm_title:
            self.sent_titles.add(norm_title)
            self.save_set(SENT_TITLES_FILE, self.sent_titles)
        
        if content:
            content_hash = self.create_content_hash(content)
            if content_hash:
                self.sent_hashes.add(content_hash)
                self.save_set(SENT_HASHES_FILE, self.sent_hashes)
        
        if content:
            first_sentence = self.extract_first_sentence(content)
            if first_sentence and len(first_sentence) > 20:
                self.sent_first_sentences.add(first_sentence)
                self.save_set(SENT_FIRST_SENTENCES_FILE, self.sent_first_sentences)
        
        logger.info(f"✅ Статья помечена как отправленная")

    # ========== УДАЛЕНИЕ МЕТА-ДАННЫХ ==========
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
        
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()

    # ========== ПРОВЕРКА ВРЕМЕНИ ==========
    def can_post_now(self):
        local_hour = (datetime.now().hour + TIMEZONE_OFFSET) % 24
        if 23 <= local_hour or local_hour < 7:
            logger.info(f"🌙 Ночное время ({local_hour}:00)")
            return False

        today = datetime.now().date()
        today_posts = 0
        last_posts_times = []
        
        for post in self.posts_log:
            try:
                post_time_str = post['time'].split('.')[0]
                post_date = datetime.fromisoformat(post_time_str).date()
                if post_date == today:
                    today_posts += 1
                    last_posts_times.append(datetime.fromisoformat(post_time_str))
            except:
                continue

        if today_posts >= MAX_POSTS_PER_DAY:
            logger.info(f"⏳ Дневной лимит {MAX_POSTS_PER_DAY}")
            return False

        if len(last_posts_times) >= 2:
            last_posts_times.sort(reverse=True)
            time_diff = last_posts_times[0] - last_posts_times[1]
            if time_diff < timedelta(minutes=35):
                next_allowed = last_posts_times[0] + timedelta(minutes=35)
                wait = (next_allowed - datetime.now()).total_seconds() / 60
                if wait > 0:
                    logger.info(f"⏳ Лимит частоты: {wait:.0f} мин")
                    return False

        return True

    def get_next_post_delay(self):
        min_val = min(MIN_POST_INTERVAL, MAX_POST_INTERVAL)
        max_val = max(MIN_POST_INTERVAL, MAX_POST_INTERVAL)
        
        delay = random.randint(min_val, max_val)
        variation = random.uniform(0.85, 1.15)
        delay = int(delay * variation)
        delay = max(min_val, min(delay, max_val))
        
        return delay

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

    # ========== ОЧИСТКА ТЕКСТА ==========
    def clean_text(self, text):
        if not text:
            return ""
        text = html.unescape(text)
        text = re.sub(r'<[^>]+>', '', text)
        text = re.sub(r'\n\s*\n\s*\n', '\n\n', text)
        text = re.sub(r' +', ' ', text)
        text = self.remove_metadata(text)
        return text.strip()

    def escape_html_for_telegram(self, text):
        if not text:
            return ""
        text = text.replace('&', '&amp;')
        text = text.replace('<', '&lt;')
        text = text.replace('>', '&gt;')
        return text

    # ========== ПАРСЕР AP NEWS ==========
    def get_apnews_articles(self):
        """Парсинг AP News"""
        try:
            logger.info("🌐 Парсинг AP News")
            headers = {'User-Agent': 'Mozilla/5.0'}
            response = requests.get('https://apnews.com/', headers=headers, timeout=15)
            if response.status_code != 200:
                return []

            soup = BeautifulSoup(response.text, 'html.parser')
            articles = []

            for link in soup.find_all('a', href=True):
                href = link['href']
                if '/article/' in href:
                    url = href if href.startswith('http') else f"https://apnews.com{href}"
                    title = link.get_text(strip=True)
                    if title and len(title) > 15:
                        articles.append({'url': url, 'title': title})

            # Убираем дубликаты
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
        """Парсинг статьи AP News"""
        try:
            headers = {'User-Agent': 'Mozilla/5.0'}
            response = requests.get(url, headers=headers, timeout=15)
            soup = BeautifulSoup(response.text, 'html.parser')

            # Заголовок
            title = None
            meta_title = soup.find('meta', property='og:title')
            if meta_title and meta_title.get('content'):
                title = meta_title['content']
            else:
                h1 = soup.find('h1')
                if h1:
                    title = h1.get_text(strip=True)
            
            if not title:
                return None
            
            title = re.sub(r'\s*\|.*AP\s*News.*$', '', title, flags=re.IGNORECASE)
            title = self.clean_text(title)

            # Изображение
            main_image = None
            meta_img = soup.find('meta', property='og:image')
            if meta_img and meta_img.get('content'):
                main_image = meta_img['content']

            # Текст
            paragraphs = []
            for p in soup.find_all('p'):
                p_text = p.get_text(strip=True)
                if p_text and len(p_text) > 20:
                    paragraphs.append(p_text)
            
            article_text = '\n\n'.join(paragraphs[:20])

            if len(article_text) < 200:
                return None

            return {
                'title': title,
                'content': article_text,
                'main_image': main_image
            }

        except Exception as e:
            logger.error(f"❌ Ошибка парсинга: {e}")
            return None

    def parse_infobrics(self, url):
        """Парсинг InfoBrics"""
        try:
            headers = {'User-Agent': 'Mozilla/5.0'}
            response = requests.get(url, headers=headers, timeout=15)
            soup = BeautifulSoup(response.text, 'html.parser')

            title = soup.find('h1')
            if title:
                title = self.clean_text(title.get_text())
            else:
                title_tag = soup.find('title')
                title = self.clean_text(title_tag.get_text()) if title_tag else "Без заголовка"

            # Изображение
            main_image = None
            meta_img = soup.find('meta', property='og:image')
            if meta_img and meta_img.get('content'):
                main_image = meta_img['content']

            # Текст
            paragraphs = []
            for p in soup.find_all('p'):
                p_text = self.clean_text(p.get_text())
                if p_text and len(p_text) > 15:
                    paragraphs.append(p_text)
            
            article_text = '\n\n'.join(paragraphs[:15])

            if len(article_text) < 200:
                return None

            return {
                'title': title,
                'content': article_text,
                'main_image': main_image
            }
        except Exception as e:
            logger.error(f"❌ Ошибка InfoBrics: {e}")
            return None

    def parse_globalresearch(self, url):
        """Парсинг Global Research"""
        try:
            headers = {'User-Agent': 'Mozilla/5.0'}
            response = requests.get(url, headers=headers, timeout=15)
            soup = BeautifulSoup(response.text, 'html.parser')

            title = None
            meta_title = soup.find('meta', property='og:title')
            if meta_title and meta_title.get('content'):
                title = meta_title['content']
            else:
                h1 = soup.find('h1')
                if h1:
                    title = h1.get_text(strip=True)
                else:
                    title_tag = soup.find('title')
                    title = title_tag.get_text(strip=True) if title_tag else "Без заголовка"

            title = self.clean_text(title)

            # Изображение
            main_image = None
            meta_img = soup.find('meta', property='og:image')
            if meta_img and meta_img.get('content'):
                main_image = meta_img['content']

            # Текст
            paragraphs = []
            for p in soup.find_all('p'):
                p_text = p.get_text(strip=True)
                if p_text and len(p_text) > 20:
                    paragraphs.append(p_text)
            
            article_text = '\n\n'.join(paragraphs[:20])

            if len(article_text) < 200:
                return None

            return {
                'title': title,
                'content': article_text,
                'main_image': main_image
            }
        except Exception as e:
            logger.error(f"❌ Ошибка Global Research: {e}")
            return None

    # ========== ЗАГРУЗКА НОВОСТЕЙ ==========
    async def fetch_from_apnews(self):
        """Загрузка из AP News"""
        try:
            articles = await asyncio.get_event_loop().run_in_executor(
                None, self.get_apnews_articles
            )

            news_items = []
            for article in articles[:3]:
                url = article['url']
                title = article['title']

                if url in self.sent_links:
                    logger.info(f"⏭️ УЖЕ БЫЛО (URL)")
                    continue

                logger.info(f"🔍 НОВАЯ: {title[:50]}...")

                article_data = await asyncio.get_event_loop().run_in_executor(
                    None, self.parse_apnews_article, url
                )

                if article_data:
                    if self.is_duplicate({
                        'link': url,
                        'title': article_data['title'],
                        'content': article_data['content']
                    }):
                        continue
                    
                    logger.info("🔄 Перевод заголовка...")
                    title_ru = await asyncio.get_event_loop().run_in_executor(
                        None, self.translate_text, article_data['title']
                    )
                    
                    is_dup, sim = await self.check_telegram_duplicate(title_ru)
                    if is_dup:
                        logger.warning(f"⏭️ ДУБЛИКАТ в Telegram")
                        continue
                    
                    logger.info(f"🔄 Перевод текста...")
                    content_ru = await asyncio.get_event_loop().run_in_executor(
                        None, self.translate_text, article_data['content']
                    )
                    
                    # Удаляем авторские права
                    content_ru = self.remove_copyright_sentences(content_ru)
                    
                    news_items.append({
                        'source': 'AP News',
                        'title_original': article_data['title'],
                        'title_ru': title_ru,
                        'content_ru': content_ru,
                        'link': url,
                        'main_image': article_data.get('main_image'),
                        'priority': 1,
                        'timestamp': datetime.now().isoformat()
                    })
                    logger.info(f"✅ Статья готова")
                else:
                    logger.warning(f"❌ Не удалось спарсить")

                await asyncio.sleep(random.randint(2, 5))

            return news_items

        except Exception as e:
            logger.error(f"❌ Ошибка AP News: {e}")
            return []

    async def fetch_from_rss(self, feed_config):
        """Загрузка из RSS"""
        try:
            feed_url = feed_config['url']
            source_name = feed_config['name']
            parser_name = feed_config.get('parser', 'infobrics')
            priority = feed_config.get('priority', 5)

            if parser_name == 'infobrics':
                parser_func = self.parse_infobrics
            elif parser_name == 'globalresearch':
                parser_func = self.parse_globalresearch
            else:
                parser_func = self.parse_infobrics

            feed = feedparser.parse(feed_url)
            if feed.bozo:
                logger.error(f"❌ Ошибка RSS {source_name}")
                return []

            logger.info(f"📰 RSS: {len(feed.entries)} статей")

            news_items = []
            for entry in feed.entries[:3]:
                link = entry.get('link', '')
                title = entry.get('title', '')

                if link in self.sent_links:
                    logger.info(f"⏭️ УЖЕ БЫЛО (URL)")
                    continue

                logger.info(f"🔍 НОВАЯ: {title[:50]}...")

                article_data = await asyncio.get_event_loop().run_in_executor(
                    None, parser_func, link
                )

                if article_data:
                    if self.is_duplicate({
                        'link': link,
                        'title': article_data['title'],
                        'content': article_data['content']
                    }):
                        continue
                    
                    logger.info("🔄 Перевод заголовка...")
                    title_ru = await asyncio.get_event_loop().run_in_executor(
                        None, self.translate_text, article_data['title']
                    )
                    
                    is_dup, sim = await self.check_telegram_duplicate(title_ru)
                    if is_dup:
                        logger.warning(f"⏭️ ДУБЛИКАТ в Telegram")
                        continue
                    
                    logger.info(f"🔄 Перевод текста...")
                    content_ru = await asyncio.get_event_loop().run_in_executor(
                        None, self.translate_text, article_data['content']
                    )
                    
                    news_items.append({
                        'source': source_name,
                        'title_original': article_data['title'],
                        'title_ru': title_ru,
                        'content_ru': content_ru,
                        'link': link,
                        'main_image': article_data.get('main_image'),
                        'priority': priority,
                        'timestamp': datetime.now().isoformat()
                    })
                    logger.info(f"✅ Статья готова")
                else:
                    logger.warning(f"❌ Не удалось спарсить")

                await asyncio.sleep(random.randint(2, 5))

            return news_items

        except Exception as e:
            logger.error(f"❌ Ошибка RSS: {e}")
            return []

    async def fetch_all_news(self):
        """Загружает все новости"""
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

        # Сортируем по времени
        all_news.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
        logger.info(f"📊 ГОТОВО: {len(all_news)} статей")
        return all_news

    # ========== ЗАГРУЗКА ИЗОБРАЖЕНИЙ ==========
    async def download_image(self, url):
        """Скачивает изображение"""
        try:
            if not url:
                return None
            
            logger.info(f"🖼️ Скачивание: {url[:50]}...")
            
            fd, path = tempfile.mkstemp(suffix='.jpg')
            os.close(fd)
            
            session = await self.get_session()
            async with session.get(url, timeout=15) as response:
                if response.status == 200:
                    with open(path, 'wb') as f:
                        f.write(await response.read())
                    logger.info(f"✅ Изображение сохранено")
                    return path
                else:
                    logger.warning(f"⚠️ Ошибка скачивания: {response.status}")
                    return None
                    
        except Exception as e:
            logger.error(f"❌ Ошибка скачивания: {e}")
            return None

    # ========== ПЕРЕВОД ==========
    def translate_text(self, text):
        """Переводит текст"""
        try:
            if not text or len(text) < 20:
                return text
                
            if len(text) > 4000:
                parts = []
                for i in range(0, len(text), 3000):
                    part = text[i:i+3000]
                    try:
                        translated = self.translator.translate(part)
                        parts.append(translated)
                    except:
                        parts.append(part)
                    time.sleep(random.uniform(0.5, 1.5))
                return ' '.join(parts)
                
            return self.translator.translate(text)
            
        except Exception as e:
            logger.error(f"❌ Ошибка перевода: {e}")
            return text

    # ========== ФОРМАТИРОВАНИЕ ДЛЯ TELEGRAM ==========
    def truncate_text_by_sentences(self, text, max_length):
        """
        Обрезает текст по предложениям
        """
        if len(text) <= max_length:
            return text
        
        sentences = re.split(r'(?<=[.!?])\s+', text)
        result = []
        current_length = 0
        
        for sent in sentences:
            sent_length = len(sent)
            if current_length + sent_length + 2 <= max_length:
                if result:
                    result.append(' ' + sent)
                    current_length += sent_length + 1
                else:
                    result.append(sent)
                    current_length += sent_length
            else:
                break
        
        return ''.join(result)

    def format_telegram_post(self, title, content):
        """
        Форматирует пост для Telegram
        """
        title_escaped = self.escape_html_for_telegram(title)
        content_escaped = self.escape_html_for_telegram(content)
        
        header = f"<b>{title_escaped}</b>"
        
        max_content_length = TELEGRAM_MAX_CAPTION - len(header) - 10
        if len(content_escaped) > max_content_length:
            content_escaped = self.truncate_text_by_sentences(content_escaped, max_content_length)
        
        return f"{header}\n\n{content_escaped}"

    # ========== ПУБЛИКАЦИЯ В TELEGRAM ==========
    async def publish_to_telegram(self, post_data):
        """Публикует пост в Telegram"""
        try:
            caption = self.format_telegram_post(
                post_data['title_ru'],
                post_data['content_ru']
            )
            
            if post_data['main_image']:
                image_path = await self.download_image(post_data['main_image'])
                
                if image_path:
                    with open(image_path, 'rb') as photo:
                        await self.bot.send_photo(
                            chat_id=CHANNEL_ID,
                            photo=photo,
                            caption=caption,
                            parse_mode='HTML'
                        )
                    try:
                        os.unlink(image_path)
                    except:
                        pass
                    logger.info("✅ Пост с изображением опубликован")
                else:
                    await self.bot.send_message(
                        chat_id=CHANNEL_ID,
                        text=caption,
                        parse_mode='HTML'
                    )
                    logger.info("✅ Пост без изображения опубликован")
            else:
                await self.bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=caption,
                    parse_mode='HTML'
                )
                logger.info("✅ Пост опубликован")
            
            self.telegram_titles_cache.append(post_data['title_ru'])
            self.log_post(post_data['link'], post_data['title_original'])
            
            return True
            
        except TelegramError as e:
            if "Too Many Requests" in str(e):
                logger.warning("⚠️ Лимит Telegram, жду...")
                await asyncio.sleep(60)
            elif "Can't parse entities" in str(e):
                plain_text = re.sub(r'<[^>]+>', '', caption)
                await self.bot.send_message(chat_id=CHANNEL_ID, text=plain_text)
                logger.info("✅ Пост отправлен без HTML")
                return True
            else:
                logger.error(f"❌ Ошибка Telegram: {e}")
            return False

    # ========== ПУБЛИКАЦИЯ НА 9111.RU (ЧЕРЕЗ REQUESTS) ==========
    def publish_to_9111(self, title, content, source_url):
        """
        Публикация на 9111.ru через requests
        БЕЗ SELENIUM - надёжно и быстро
        """
        if not self.ninth_available:
            logger.warning("⚠️ 9111.ru недоступен (нет логина/пароля)")
            return False
        
        try:
            logger.info("=" * 60)
            logger.info("🌐 ПУБЛИКАЦИЯ НА 9111.RU (ЧЕРЕЗ REQUESTS)")
            logger.info(f"📧 Email: {NINTH_EMAIL}")
            logger.info(f"📝 Заголовок: {title[:50]}...")
            
            # Создаём сессию с заголовками как у браузера
            session = requests.Session()
            session.headers.update({
                'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
                'Origin': 'https://www.9111.ru',
                'Referer': 'https://www.9111.ru/',
            })
            
            # ШАГ 1: Получаем главную страницу для cookies
            logger.info("1️⃣ Получение cookies...")
            main_response = session.get('https://www.9111.ru', timeout=10)
            logger.info(f"   Статус: {main_response.status_code}")
            
            # ШАГ 2: Авторизация
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
            
            logger.info(f"   Статус авторизации: {login_response.status_code}")
            
            # Проверяем успешность авторизации
            if 'success' not in login_response.text and 'ok' not in login_response.text.lower():
                logger.error(f"❌ Ошибка авторизации: {login_response.text[:200]}")
                return False
            
            logger.info("✅ Авторизация успешна")
            
            # ШАГ 3: Получаем страницу создания публикации (для CSRF токена)
            logger.info("3️⃣ Получение страницы создания...")
            add_page = session.get('https://www.9111.ru/pubs/add/title/', timeout=10)
            logger.info(f"   Статус: {add_page.status_code}")
            
            # Парсим CSRF токен если есть
            soup = BeautifulSoup(add_page.text, 'html.parser')
            csrf_token = None
            csrf_input = soup.find('input', {'name': 'csrf_token'})
            if csrf_input:
                csrf_token = csrf_input.get('value')
                logger.info(f"   Найден CSRF токен")
            
            # ШАГ 4: Отправка публикации
            logger.info("4️⃣ Отправка публикации...")
            
            post_data = {
                'topic_name': title[:150],
                'komm': f"{content}\n\nИсточник: {source_url}"[:5000],
                'rubric_id': '382235',  # Новости
                'tag_list_input': 'новости, политика, экономика',
                'title_tags': 'новости, политика, экономика',
                'title_private': 'off',
                'submit': 'Опубликовать'
            }
            
            # Добавляем CSRF токен если есть
            if csrf_token:
                post_data['csrf_token'] = csrf_token
            
            # Отправляем POST запрос
            publish_response = session.post('https://www.9111.ru/pubs/add/title/', 
                                           data=post_data, 
                                           allow_redirects=True,
                                           timeout=15)
            
            logger.info(f"   Статус ответа: {publish_response.status_code}")
            logger.info(f"   URL после редиректа: {publish_response.url}")
            
            # Проверяем результат
            if publish_response.status_code == 200:
                if 'Спасибо' in publish_response.text or 'опубликована' in publish_response.text:
                    logger.info("✅ Статья успешно опубликована на 9111.ru")
                    return True
                else:
                    logger.warning("⚠️ Статья отправлена, но ответ неясен")
                    # Сохраняем ответ для анализа
                    with open(f"debug_9111_{int(time.time())}.html", 'w', encoding='utf-8') as f:
                        f.write(publish_response.text)
                    return True
            else:
                logger.error(f"❌ Ошибка HTTP {publish_response.status_code}")
                return False
            
        except requests.exceptions.Timeout:
            logger.error("❌ Таймаут при подключении к 9111.ru")
            return False
        except requests.exceptions.ConnectionError:
            logger.error("❌ Ошибка подключения к 9111.ru")
            return False
        except Exception as e:
            logger.error(f"❌ Неожиданная ошибка: {e}")
            logger.error(traceback.format_exc())
            return False

    async def publish_post(self, post_data):
        """Публикует пост везде"""
        try:
            # Публикация в Telegram
            tg_success = await self.publish_to_telegram(post_data)
            
            if not tg_success:
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
        """Основной процесс"""
        logger.info("=" * 60)
        logger.info(f"🔍 ЦИКЛ: {datetime.now()}")
        logger.info("=" * 60)

        # Загружаем заголовки из Telegram
        await self.load_telegram_titles_cache()
        
        # Загружаем новые статьи
        news_items = await self.fetch_all_news()
        
        if not news_items:
            logger.info("📭 НЕТ НОВЫХ СТАТЕЙ")
            return

        # Добавляем в очередь
        self.post_queue.extend(news_items)
        self.post_queue.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
        logger.info(f"📦 В ОЧЕРЕДИ: {len(self.post_queue)}")

        await self.try_publish_from_queue()

    async def try_publish_from_queue(self):
        """Публикует из очереди"""
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
        """Планирует следующую попытку"""
        await asyncio.sleep(delay)
        await self.try_publish_from_queue()

    async def start(self):
        """Запуск бота"""
        logger.info("=" * 80)
        logger.info("🚀 NEWS BOT 20.0 - 9111.RU ЧЕРЕЗ REQUESTS")
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

        # Первый цикл
        await self.process_and_publish()

        # Планировщик
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
