"""
🤖 Telegram News Bot - Версия 12.1
АБСОЛЮТНАЯ ЗАЩИТА ОТ ДУБЛИКАТОВ (5 УРОВНЕЙ) + 9111.RU
- Уровень 1: Проверка по URL
- Уровень 2: Проверка по нормализованному заголовку
- Уровень 3: Проверка по хешу содержимого
- Уровень 4: Проверка по первому предложению
- Уровень 5: Проверка по схожести заголовков (75%) с историей Telegram
- Исправлен метод получения истории Telegram
- Исправлена публикация на 9111.ru (передача cookies)
- Сохранение скриншотов и HTML для анализа
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
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import TimeoutException, WebDriverException
import subprocess

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

# Данные для 9111.ru - пробуем разные варианты имён
NINTH_EMAIL = (os.getenv('NINTH_EMAIL') or 
               os.getenv('EMAIL_9111') or 
               os.getenv('EMAIL') or 
               '')

NINTH_PASSWORD = (os.getenv('NINTH_PASSWORD') or 
                  os.getenv('EMAIL_PASSWORD') or 
                  os.getenv('PASSWORD') or 
                  '')

# Проверка обязательных переменных
if not TELEGRAM_TOKEN or not CHANNEL_ID:
    logger.error("❌ TELEGRAM_TOKEN или CHANNEL_ID не заданы!")
    logger.error("Проверьте переменные окружения в Railway")
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
        
        # Загружаем все четыре базы данных
        self.sent_links = self.load_set(SENT_LINKS_FILE)
        self.sent_hashes = self.load_set(SENT_HASHES_FILE)
        self.sent_titles = self.load_set(SENT_TITLES_FILE)
        self.sent_first_sentences = self.load_set(SENT_FIRST_SENTENCES_FILE)
        self.posts_log = self.load_json(POSTS_LOG_FILE)
        
        self.session = None
        self.last_post_time = None
        self.post_queue = []
        
        # Кэш заголовков из Telegram (для проверки дубликатов)
        self.telegram_titles_cache = []
        
        # Проверяем наличие Chrome для 9111.ru
        self.chrome_path = self._find_chrome()
        
        # Отладка переменных окружения
        self._debug_env_vars()
        
        # Проверяем наличие данных для 9111.ru
        self.ninth_available = bool(self.chrome_path and NINTH_EMAIL and NINTH_PASSWORD)
        
        logger.info(f"📊 Загружено {len(self.sent_links)} ссылок")
        logger.info(f"📊 Загружено {len(self.sent_hashes)} хешей содержимого")
        logger.info(f"📊 Загружено {len(self.sent_titles)} заголовков")
        logger.info(f"📊 Загружено {len(self.sent_first_sentences)} первых предложений")
        logger.info(f"📊 Загружено {len(self.posts_log)} записей в логе")
        logger.info(f"🌐 Chrome для 9111.ru: {'✅ найден' if self.chrome_path else '❌ не найден'}")
        logger.info(f"📧 Email для 9111.ru: {'✅ задан' if NINTH_EMAIL else '❌ не задан'}")
        logger.info(f"🔑 Пароль для 9111.ru: {'✅ задан' if NINTH_PASSWORD else '❌ не задан'}")
        logger.info(f"🌐 9111.ru: {'✅ ДОСТУПЕН' if self.ninth_available else '❌ НЕДОСТУПЕН'}")

    def _debug_env_vars(self):
        """Отладка переменных окружения"""
        logger.info("=" * 50)
        logger.info("🔍 ОТЛАДКА ПЕРЕМЕННЫХ ОКРУЖЕНИЯ:")
        
        # Проверяем все возможные имена переменных
        possible_emails = [
            ('NINTH_EMAIL', os.getenv('NINTH_EMAIL')),
            ('EMAIL_9111', os.getenv('EMAIL_9111')),
            ('EMAIL', os.getenv('EMAIL')),
            ('NINTH_EMAIL_9111', os.getenv('NINTH_EMAIL_9111'))
        ]
        
        possible_passwords = [
            ('NINTH_PASSWORD', os.getenv('NINTH_PASSWORD')),
            ('EMAIL_PASSWORD', os.getenv('EMAIL_PASSWORD')),
            ('PASSWORD', os.getenv('PASSWORD')),
            ('NINTH_PASS', os.getenv('NINTH_PASS'))
        ]
        
        for name, value in possible_emails:
            if value:
                logger.info(f"📧 Найден email в {name}: {value}")
        
        for name, value in possible_passwords:
            if value:
                logger.info(f"🔑 Найден пароль в {name}: {'*' * len(value)}")
        
        # Проверяем все переменные
        logger.info("📋 Все переменные окружения (только relevant):")
        for key, value in os.environ.items():
            if any(x in key.upper() for x in ['EMAIL', 'PASS', 'NINTH', '9111', 'TOKEN', 'CHANNEL']):
                if 'PASS' in key.upper() or 'TOKEN' in key.upper():
                    logger.info(f"   {key}: {'*' * len(value)}")
                else:
                    logger.info(f"   {key}: {value}")
        
        logger.info("=" * 50)

    def _find_chrome(self) -> str:
        """Ищет Chrome в системе"""
        paths = [
            '/usr/bin/google-chrome',
            '/usr/bin/chromium',
            '/usr/bin/chromium-browser',
            '/usr/bin/google-chrome-stable',
            '/app/.chrome/chrome-linux64/chrome'
        ]
        
        for path in paths:
            if os.path.exists(path):
                try:
                    version = subprocess.check_output([path, '--version'], text=True).strip()
                    logger.info(f"✅ Chrome найден: {path} ({version})")
                except:
                    logger.info(f"✅ Chrome найден: {path}")
                return path
        
        logger.warning("⚠️ Chrome не найден, публикация на 9111.ru будет недоступна")
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
        """Создает хеш содержимого статьи"""
        if not content:
            return None
        
        sample = content[:500].encode('utf-8')
        return hashlib.md5(sample).hexdigest()

    def extract_first_sentence(self, content):
        """Извлекает первое предложение из текста"""
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
        Вычисляет процент схожести двух заголовков.
        Использует расстояние Левенштейна для сравнения.
        """
        if not title1 or not title2:
            return 0
        
        # Приводим к нижнему регистру и убираем лишние пробелы
        t1 = ' '.join(title1.lower().split())
        t2 = ' '.join(title2.lower().split())
        
        # Если заголовки идентичны, сразу возвращаем 100
        if t1 == t2:
            return 100.0
        
        # Вычисляем расстояние Левенштейна
        def levenshtein_distance(s1, s2):
            if len(s1) < len(s2):
                return levenshtein_distance(s2, s1)
            if len(s2) == 0:
                return len(s1)
            previous_row = range(len(s2) + 1)
            for i, c1 in enumerate(s1):
                current_row = [i + 1]
                for j, c2 in enumerate(s2):
                    insertions = previous_row[j + 1] + 1
                    deletions = current_row[j] + 1
                    substitutions = previous_row[j] + (c1 != c2)
                    current_row.append(min(insertions, deletions, substitutions))
                previous_row = current_row
            return previous_row[-1]
        
        distance = levenshtein_distance(t1, t2)
        max_len = max(len(t1), len(t2))
        if max_len == 0:
            return 0
        
        similarity = (1 - distance / max_len) * 100
        return round(similarity, 2)

    async def load_telegram_titles_cache(self):
        """Загружает заголовки последних постов из Telegram в кэш"""
        try:
            logger.info("📚 Загрузка заголовков из Telegram в кэш...")
            self.telegram_titles_cache = []
            
            # Получаем последние 100 сообщений из канала
            updates = await self.bot.get_updates()
            
            # Собираем все message_id из канала
            message_ids = []
            for update in updates:
                if update.channel_post and str(update.channel_post.chat_id) == CHANNEL_ID.replace('@', ''):
                    message_ids.append(update.channel_post.message_id)
            
            # Сортируем и берём последние 100
            message_ids.sort(reverse=True)
            message_ids = message_ids[:100]
            
            # Получаем каждое сообщение по ID
            for msg_id in message_ids:
                try:
                    message = await self.bot.get_message(chat_id=CHANNEL_ID, message_id=msg_id)
                    
                    if message.caption:  # Если пост с фото
                        # Извлекаем заголовок (первая строка, убираем HTML теги)
                        caption_lines = message.caption.split('\n')
                        if caption_lines:
                            title = re.sub(r'<[^>]+>', '', caption_lines[0]).strip()
                            if title and len(title) > 10:
                                self.telegram_titles_cache.append(title)
                    elif message.text:   # Если просто текст
                        text_lines = message.text.split('\n')
                        if text_lines:
                            title = re.sub(r'<[^>]+>', '', text_lines[0]).strip()
                            if title and len(title) > 10:
                                self.telegram_titles_cache.append(title)
                except Exception as e:
                    logger.debug(f"Не удалось получить сообщение {msg_id}: {e}")
                    continue
            
            logger.info(f"✅ Загружено {len(self.telegram_titles_cache)} заголовков")
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки заголовков из Telegram: {e}")
            # Пробуем альтернативный метод
            await self.load_telegram_titles_cache_alternative()

    async def load_telegram_titles_cache_alternative(self):
        """Альтернативный метод загрузки заголовков"""
        try:
            logger.info("📚 Альтернативный метод загрузки заголовков...")
            
            # Пробуем получить историю через другой метод
            # Для этого нам нужно знать ID канала в числовом формате
            chat = await self.bot.get_chat(chat_id=CHANNEL_ID)
            
            # Получаем последние сообщения через get_chat_history (если доступно)
            # В некоторых версиях python-telegram-bot есть этот метод
            if hasattr(self.bot, 'get_chat_history'):
                async for message in self.bot.get_chat_history(chat_id=CHANNEL_ID, limit=100):
                    if message.caption:
                        caption_lines = message.caption.split('\n')
                        if caption_lines:
                            title = re.sub(r'<[^>]+>', '', caption_lines[0]).strip()
                            if title:
                                self.telegram_titles_cache.append(title)
                    elif message.text:
                        text_lines = message.text.split('\n')
                        if text_lines:
                            title = re.sub(r'<[^>]+>', '', text_lines[0]).strip()
                            if title:
                                self.telegram_titles_cache.append(title)
            
            logger.info(f"✅ Альтернативный метод загрузил {len(self.telegram_titles_cache)} заголовков")
        except Exception as e:
            logger.error(f"❌ Альтернативный метод тоже не сработал: {e}")
            logger.info("ℹ️ Продолжаем работу без кэша заголовков")

    def check_telegram_duplicate(self, new_title):
        """Проверяет, нет ли похожего заголовка в кэше Telegram"""
        if not self.telegram_titles_cache:
            return False, 0
        
        for existing_title in self.telegram_titles_cache:
            similarity = self.calculate_title_similarity(new_title, existing_title)
            if similarity >= 75:
                logger.info(f"⏭️ ДУБЛИКАТ (Telegram заголовок {similarity}%): {new_title[:50]}...")
                logger.info(f"   с существующим: {existing_title[:50]}...")
                return True, similarity
        
        return False, 0

    def is_duplicate(self, article_data):
        """
        ПЯТИУРОВНЕВАЯ ПРОВЕРКА НА ДУБЛИКАТ:
        1. Проверка по URL
        2. Проверка по нормализованному заголовку
        3. Проверка по хешу содержимого
        4. Проверка по первому предложению
        5. Проверка по схожести заголовков (75%) с историей Telegram
        """
        url = article_data.get('link', '')
        title = article_data.get('title', '')
        content = article_data.get('content', '')
        
        # Уровень 1: Проверка по URL
        if url in self.sent_links:
            logger.info(f"⏭️ ДУБЛИКАТ (URL): {title[:50]}...")
            return True
        
        # Уровень 2: Проверка по нормализованному заголовку
        norm_title = self.normalize_title(title)
        if norm_title and norm_title in self.sent_titles:
            logger.info(f"⏭️ ДУБЛИКАТ (заголовок): {title[:50]}...")
            return True
        
        # Уровень 3: Проверка по хешу содержимого
        if content:
            content_hash = self.create_content_hash(content)
            if content_hash and content_hash in self.sent_hashes:
                logger.info(f"⏭️ ДУБЛИКАТ (содержимое): {title[:50]}...")
                return True
        
        # Уровень 4: Проверка по первому предложению
        if content:
            first_sentence = self.extract_first_sentence(content)
            if first_sentence and len(first_sentence) > 20:
                if first_sentence in self.sent_first_sentences:
                    logger.info(f"⏭️ ДУБЛИКАТ (первое предложение): {first_sentence[:50]}...")
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
        
        garbage_phrases = [
            r'(?:Фарси|Русский|Немецкий|Испанский|Португальский|Французский|Итальянский)[\s,]+',
            r'Subscribe', r'Newsletter', r'Sign up', r'Follow us',
            r'Share this', r'Read more', r'Comments', r'Advertisement',
            r'Morning Wire', r'Afternoon Wire', r'Daily Brief'
        ]
        
        for phrase in garbage_phrases:
            text = re.sub(phrase, '', text, flags=re.IGNORECASE)
        
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()

    # ========== ПРОВЕРКА ХАОТИЧНЫХ ЛИМИТОВ ==========
    def can_post_now(self):
        local_hour = (datetime.now().hour + TIMEZONE_OFFSET) % 24
        if 23 <= local_hour or local_hour < 7:
            logger.info(f"🌙 Ночное время ({local_hour}:00), пропускаю")
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
            logger.info(f"⏳ Дневной лимит {MAX_POSTS_PER_DAY} достигнут")
            return False

        if len(last_posts_times) >= 2:
            last_posts_times.sort(reverse=True)
            if len(last_posts_times) >= 2:
                time_diff = last_posts_times[0] - last_posts_times[1]
                if time_diff < timedelta(minutes=35):
                    next_allowed = last_posts_times[0] + timedelta(minutes=35)
                    wait_minutes = (next_allowed - datetime.now()).total_seconds() / 60
                    if wait_minutes > 0:
                        logger.info(f"⏳ Лимит частоты: следующий пост через {wait_minutes:.0f} минут")
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
    def get_apnews_articles_v2(self):
        try:
            logger.info("🌐 Парсинг главной страницы AP News")
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            response = requests.get('https://apnews.com/', headers=headers, timeout=15)
            if response.status_code != 200:
                return []

            soup = BeautifulSoup(response.text, 'html.parser')
            articles = []

            for link in soup.find_all('a', href=True):
                href = link['href']
                
                if '/article/' not in href:
                    continue
                
                full_url = None
                if href.startswith('https://apnews.com/'):
                    full_url = href
                elif href.startswith('/'):
                    full_url = 'https://apnews.com' + href
                elif 'apnews.com' in href:
                    full_url = href
                else:
                    continue
                
                title = None
                link_text = link.get_text(strip=True)
                if link_text and len(link_text) > 15:
                    title = link_text
                else:
                    parent_heading = link.find_parent(['h1', 'h2', 'h3', 'h4'])
                    if parent_heading:
                        title = parent_heading.get_text(strip=True)
                
                if not title or len(title) < 15:
                    continue
                
                title = re.sub(r'\s+', ' ', title).strip()
                
                lower_title = title.lower()
                if any(phrase in lower_title for phrase in ['newsletter', 'subscribe', 'sign up']):
                    continue
                
                articles.append({
                    'url': full_url,
                    'title': title
                })

            unique_articles = []
            seen_urls = set()
            for article in articles:
                if article['url'] not in seen_urls:
                    seen_urls.add(article['url'])
                    unique_articles.append(article)

            logger.info(f"✅ Найдено {len(unique_articles)} статей")
            return unique_articles[:10]

        except Exception as e:
            logger.error(f"❌ Ошибка парсинга AP News: {e}")
            return []

    def parse_apnews_article_v2(self, url, source_name):
        try:
            logger.info(f"🌐 Парсинг статьи AP News: {url}")
            headers = {'User-Agent': 'Mozilla/5.0'}
            response = requests.get(url, headers=headers, timeout=15)
            if response.status_code != 200:
                return None

            soup = BeautifulSoup(response.text, 'html.parser')

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

            main_image = None
            meta_img = soup.find('meta', property='og:image')
            if meta_img and meta_img.get('content'):
                main_image = meta_img['content']

            article_text = ""
            main_container = None
            
            possible_selectors = [
                ('article', {}),
                ('main', {}),
                ('div', {'class_': re.compile(r'Article', re.I)}),
                ('div', {'class_': re.compile(r'story-body', re.I)})
            ]
            
            for tag, attrs in possible_selectors:
                container = soup.find(tag, **attrs)
                if container:
                    main_container = container
                    break
            
            if not main_container:
                main_container = soup.body
            
            if main_container:
                for unwanted in main_container.find_all(['aside', 'nav', 'header', 'footer', 'script', 'style']):
                    unwanted.decompose()
                
                for elem in main_container.find_all(class_=re.compile(r'sidebar|newsletter|related|ad|promo', re.I)):
                    elem.decompose()
                
                paragraphs = []
                for p in main_container.find_all('p'):
                    p_text = p.get_text(strip=True)
                    if p_text and len(p_text) > 20:
                        lower_text = p_text.lower()
                        if not any(phrase in lower_text for phrase in 
                                 ['subscribe', 'newsletter', 'sign up', 'follow us']):
                            paragraphs.append(p_text)
                
                if paragraphs:
                    article_text = '\n\n'.join(paragraphs)

            if len(article_text) < 200:
                return None

            article_text = self.remove_metadata(article_text)

            return {
                'title': title,
                'content': article_text,
                'main_image': main_image
            }

        except Exception as e:
            logger.error(f"❌ Ошибка парсинга AP News: {e}")
            return None

    # ========== ПАРСЕР INFOBRICS ==========
    def parse_infobrics(self, url, source_name):
        try:
            logger.info(f"🌐 Парсинг InfoBrics: {url}")
            headers = {'User-Agent': 'Mozilla/5.0'}
            response = requests.get(url, headers=headers, timeout=15)
            if response.status_code != 200:
                return None

            soup = BeautifulSoup(response.text, 'html.parser')

            title = "Без заголовка"
            title_elem = soup.find('div', class_=re.compile(r'title.*big')) or soup.find('h1')
            if title_elem:
                title = self.clean_text(title_elem.get_text())

            main_image = None
            img_elem = soup.find('img', class_=re.compile(r'article.*image'))
            if img_elem and img_elem.get('src'):
                img_src = img_elem['src']
                if img_src.startswith('/'):
                    domain = url.split('/')[2]
                    main_image = f"https://{domain}{img_src}"
                elif not img_src.startswith('http'):
                    domain = url.split('/')[2]
                    main_image = f"https://{domain}/{img_src}"
                else:
                    main_image = img_src

            article_text = ""
            text_container = soup.find('div', class_=re.compile(r'article__text')) or soup.find('div', class_=re.compile(r'article'))
            if text_container:
                for unwanted in text_container.find_all(['script', 'style', 'button']):
                    unwanted.decompose()
                paragraphs = []
                for p in text_container.find_all('p'):
                    p_text = self.clean_text(p.get_text())
                    if p_text and len(p_text) > 15:
                        paragraphs.append(p_text)
                if paragraphs:
                    article_text = '\n\n'.join(paragraphs)

            if len(article_text) < 200:
                return None

            return {
                'title': title,
                'content': article_text,
                'main_image': main_image
            }
        except Exception as e:
            logger.error(f"❌ Ошибка парсинга InfoBrics: {e}")
            return None

    # ========== ПАРСЕР GLOBAL RESEARCH (УЛУЧШЕННЫЙ) ==========
    def parse_globalresearch(self, url, source_name):
        """
        Улучшенный парсинг Global Research
        """
        try:
            logger.info(f"🌐 Парсинг Global Research: {url}")
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            }
            response = requests.get(url, headers=headers, timeout=15)
            if response.status_code != 200:
                logger.warning(f"⚠️ HTTP {response.status_code} для {url}")
                return None

            soup = BeautifulSoup(response.text, 'html.parser')

            # Поиск заголовка
            title = "Без заголовка"
            
            meta_title = soup.find('meta', property='og:title')
            if meta_title and meta_title.get('content'):
                title = meta_title['content']
            else:
                h1 = soup.find('h1')
                if h1:
                    title = h1.get_text(strip=True)
                else:
                    title_tag = soup.find('title')
                    if title_tag:
                        title = title_tag.get_text(strip=True)
                        title = re.sub(r'\s*[|-]\s*Global Research.*$', '', title, flags=re.IGNORECASE)
            
            title = self.clean_text(title)
            logger.info(f"📌 Заголовок: {title[:70]}...")

            # Поиск изображения
            main_image = None
            
            meta_img = soup.find('meta', property='og:image')
            if meta_img and meta_img.get('content'):
                main_image = meta_img['content']
            else:
                img_elem = soup.find('img', class_=re.compile(r'featured|wp-post-image|attachment-', re.I))
                if img_elem and img_elem.get('src'):
                    img_src = img_elem['src']
                    if img_src.startswith('/'):
                        domain = url.split('/')[2]
                        main_image = f"https://{domain}{img_src}"
                    elif not img_src.startswith('http'):
                        domain = url.split('/')[2]
                        main_image = f"https://{domain}/{img_src}"
                    else:
                        main_image = img_src

            # Поиск текста
            article_text = ""
            
            possible_selectors = [
                'div.entry-content',
                'div.post-content',
                'div.article-content',
                'div.post-entry',
                'article',
                'div[itemprop="articleBody"]',
                'div.content'
            ]
            
            text_container = None
            for selector in possible_selectors:
                container = soup.select_one(selector)
                if container:
                    text_container = container
                    break
            
            if not text_container:
                for class_pattern in ['entry-content', 'post-content', 'article-content']:
                    container = soup.find('div', class_=re.compile(class_pattern, re.I))
                    if container:
                        text_container = container
                        break
            
            if not text_container:
                text_container = soup.body
            
            if text_container:
                for tag in ['script', 'style', 'nav', 'header', 'footer', 'aside']:
                    for elem in text_container.find_all(tag):
                        elem.decompose()
                
                for elem in text_container.find_all(class_=re.compile(r'sidebar|newsletter|related|ad|promo|comment|share|social', re.I)):
                    elem.decompose()
                
                paragraphs = []
                for p in text_container.find_all('p'):
                    p_text = p.get_text(strip=True)
                    if p_text and len(p_text) > 20:
                        lower_text = p_text.lower()
                        if not any(phrase in lower_text for phrase in 
                                 ['subscribe', 'newsletter', 'sign up', 'follow us', 'click here', 'read more']):
                            paragraphs.append(p_text)
                
                if paragraphs:
                    article_text = '\n\n'.join(paragraphs[:20])

            if len(article_text) < 200:
                logger.warning(f"⚠️ Текст слишком короткий ({len(article_text)} символов)")
                return None

            article_text = self.remove_metadata(article_text)
            logger.info(f"📄 Текст: {len(article_text)} символов")

            return {
                'title': title,
                'content': article_text,
                'main_image': main_image
            }
            
        except Exception as e:
            logger.error(f"❌ Ошибка парсинга Global Research: {e}")
            return None

    # ========== ЗАГРУЗКА НОВОСТЕЙ ==========
    async def fetch_from_apnews_v2(self):
        try:
            logger.info("🔄 AP News v2 (прямой парсинг)")
            
            articles = await asyncio.get_event_loop().run_in_executor(
                None, self.get_apnews_articles_v2
            )

            if not articles:
                return []

            news_items = []
            for article in articles[:3]:
                url = article['url']
                title = article['title']

                if url in self.sent_links:
                    logger.info(f"⏭️ УЖЕ БЫЛО (URL): {title[:50]}...")
                    continue

                logger.info(f"🔍 НОВАЯ (AP News): {title[:50]}...")

                article_data = await asyncio.get_event_loop().run_in_executor(
                    None, self.parse_apnews_article_v2, url, "AP News"
                )

                if article_data:
                    if self.is_duplicate({
                        'link': url,
                        'title': article_data['title'],
                        'content': article_data['content']
                    }):
                        continue
                    
                    news_items.append({
                        'source': 'AP News',
                        'title': article_data['title'],
                        'content': article_data['content'],
                        'link': url,
                        'main_image': article_data.get('main_image'),
                        'priority': 1
                    })
                    logger.info(f"✅ Статья добавлена")
                else:
                    logger.warning(f"❌ Не удалось спарсить")

                await asyncio.sleep(random.randint(3, 8))

            return news_items

        except Exception as e:
            logger.error(f"❌ Ошибка AP News: {e}")
            return []

    async def fetch_from_rss(self, feed_config):
        try:
            feed_url = feed_config['url']
            source_name = feed_config['name']
            parser_name = feed_config.get('parser', 'infobrics')
            priority = feed_config.get('priority', 5)
            
            logger.info(f"🔄 {source_name} (RSS)")

            if parser_name == 'infobrics':
                parser_func = self.parse_infobrics
            elif parser_name == 'globalresearch':
                parser_func = self.parse_globalresearch
            else:
                parser_func = self.parse_infobrics

            feed = feedparser.parse(feed_url)
            if feed.bozo:
                logger.error(f"❌ Ошибка RSS {source_name}: {feed.bozo_exception}")
                return []

            logger.info(f"📰 В RSS {len(feed.entries)} статей")

            news_items = []
            for entry in feed.entries[:3]:
                link = entry.get('link', '')
                title = entry.get('title', 'Без заголовка')

                if link in self.sent_links:
                    logger.info(f"⏭️ УЖЕ БЫЛО (URL): {title[:50]}...")
                    continue

                logger.info(f"🔍 НОВАЯ ({source_name}): {title[:50]}...")

                loop = asyncio.get_event_loop()
                article_data = await loop.run_in_executor(
                    None, parser_func, link, source_name
                )

                if article_data:
                    if self.is_duplicate({
                        'link': link,
                        'title': article_data['title'],
                        'content': article_data['content']
                    }):
                        continue
                    
                    news_items.append({
                        'source': source_name,
                        'title': article_data['title'],
                        'content': article_data['content'],
                        'link': link,
                        'main_image': article_data.get('main_image'),
                        'priority': priority
                    })
                    logger.info(f"✅ Статья добавлена")
                else:
                    logger.warning(f"❌ Не удалось спарсить {source_name}")

                await asyncio.sleep(random.randint(3, 8))

            return news_items

        except Exception as e:
            logger.error(f"❌ Ошибка RSS {feed_config['name']}: {e}")
            return []

    async def fetch_all_news(self):
        all_news = []

        for feed in ALL_FEEDS:
            if not feed['enabled']:
                continue

            if feed.get('type') == 'html_apnews_v2':
                news = await self.fetch_from_apnews_v2()
            else:
                news = await self.fetch_from_rss(feed)

            all_news.extend(news)
            await asyncio.sleep(random.randint(5, 10))

        all_news.sort(key=lambda x: x.get('priority', 5))

        logger.info(f"📊 ВСЕГО НОВЫХ УНИКАЛЬНЫХ СТАТЕЙ: {len(all_news)}")
        return all_news

    async def download_image(self, url):
        try:
            if not url:
                return None
            fd, path = tempfile.mkstemp(suffix='.jpg')
            os.close(fd)
            session = await self.get_session()
            async with session.get(url, timeout=10) as response:
                if response.status == 200:
                    with open(path, 'wb') as f:
                        f.write(await response.read())
                    return path
            return None
        except Exception as e:
            logger.error(f"Ошибка скачивания: {e}")
            return None

    def translate_text(self, text):
        try:
            if not text or len(text) < 20:
                return text
            if len(text) > 3000:
                parts = []
                for i in range(0, len(text), 2000):
                    part = text[i:i+2000]
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

    def truncate_first_paragraph_by_sentences(self, paragraph, max_length):
        if len(paragraph) <= max_length:
            return paragraph

        sentences = re.split(r'(?<=[.!?])\s+', paragraph)
        result_sentences = []
        current_length = 0

        for sent in sentences:
            sent_length = len(sent)
            if result_sentences:
                sent_length += 1

            if current_length + sent_length <= max_length:
                if result_sentences:
                    current_length += 1
                result_sentences.append(sent)
                current_length += len(sent)
            else:
                if not result_sentences:
                    words = sent.split()
                    for word in words:
                        if current_length + len(word) + 1 <= max_length:
                            if result_sentences:
                                result_sentences.append(' ' + word)
                                current_length += len(word) + 1
                            else:
                                result_sentences.append(word)
                                current_length += len(word)
                        else:
                            break
                break

        return ' '.join(result_sentences) if result_sentences else paragraph[:max_length]

    def build_caption_with_smart_truncation(self, title, paragraphs, max_length=TELEGRAM_MAX_CAPTION):
        title_part = f"<b>{title}</b>"
        current_text = title_part
        current_length = len(title_part)
        available_for_text = max_length - 5

        if current_length >= available_for_text:
            title_truncated = title[:50] + "..."
            title_part = f"<b>{title_truncated}</b>"
            current_text = title_part
            current_length = len(title_part)

        added_any_text = False

        for i, para in enumerate(paragraphs):
            separator = "\n\n" if (i == 0 and not added_any_text) or i > 0 else "\n\n"

            if i == 0:
                para_with_sep = separator + para
                para_length = len(para_with_sep)

                if current_length + para_length <= available_for_text:
                    current_text += para_with_sep
                    current_length += para_length
                    added_any_text = True
                else:
                    max_para_length = available_for_text - current_length - len(separator)
                    truncated_para = self.truncate_first_paragraph_by_sentences(para, max_para_length)
                    if truncated_para:
                        current_text += separator + truncated_para
                        current_length += len(separator) + len(truncated_para)
                        added_any_text = True
            else:
                para_with_sep = separator + para
                para_length = len(para_with_sep)

                if current_length + para_length <= available_for_text:
                    current_text += para_with_sep
                    current_length += para_length
                    added_any_text = True
                else:
                    break

        return current_text

    # ========== ПУБЛИКАЦИЯ НА 9111.RU (С РАСШИРЕННОЙ ОТЛАДКОЙ) ==========
    def publish_to_9111(self, title, content, source_url):
        """
        Публикация статьи на 9111.ru через Selenium с передачей cookies в requests
        """
        if not self.ninth_available:
            logger.warning("⚠️ 9111.ru недоступен (нет Chrome или данных для входа)")
            return False
        
        driver = None
        timestamp = int(time.time())
        session = requests.Session()
        
        try:
            logger.info("=" * 60)
            logger.info("🌐 ЗАПУСК ПУБЛИКАЦИИ НА 9111.RU")
            logger.info(f"📧 Email: {NINTH_EMAIL}")
            logger.info(f"📝 Заголовок: {title[:70]}...")
            logger.info(f"📄 Текст: {len(content)} символов")
            logger.info("=" * 60)
            
            options = Options()
            options.add_argument("--headless=new")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-gpu")
            options.add_argument("--window-size=1920,1080")
            options.binary_location = self.chrome_path
            
            driver = webdriver.Chrome(options=options)
            driver.set_page_load_timeout(30)
            
            # 1. Переходим на главную
            logger.info("1️⃣ Переход на главную страницу...")
            driver.get("https://www.9111.ru")
            time.sleep(2)
            logger.info(f"   URL: {driver.current_url}")
            logger.info(f"   Заголовок: {driver.title}")
            
            # Сохраняем скриншот главной
            driver.save_screenshot(f"debug_9111_main_{timestamp}.png")
            
            # 2. Авторизация
            logger.info("2️⃣ Попытка авторизации...")
            try:
                # Ищем ссылку "Вход"
                login_links = driver.find_elements(By.PARTIAL_LINK_TEXT, "Вход")
                if login_links:
                    logger.info(f"   Найдена ссылка 'Вход', текст: {login_links[0].text}")
                    login_links[0].click()
                    time.sleep(2)
                    
                    # Сохраняем скриншот формы входа
                    driver.save_screenshot(f"debug_9111_login_form_{timestamp}.png")
                    
                    # Заполняем форму
                    email_input = WebDriverWait(driver, 10).until(
                        EC.presence_of_element_located((By.NAME, "email"))
                    )
                    email_input.send_keys(NINTH_EMAIL)
                    logger.info("   Email введён")
                    
                    pass_input = driver.find_element(By.NAME, "pass")
                    pass_input.send_keys(NINTH_PASSWORD)
                    logger.info("   Пароль введён")
                    
                    # Нажимаем кнопку входа
                    submit_btn = driver.find_element(By.XPATH, "//input[@type='submit']")
                    submit_btn.click()
                    time.sleep(3)
                    
                    # Сохраняем скриншот после входа
                    driver.save_screenshot(f"debug_9111_after_login_{timestamp}.png")
                    logger.info("   Форма отправлена")
                    
                    # Проверяем, появилось ли имя пользователя
                    page_source = driver.page_source
                    if NINTH_EMAIL.split('@')[0] in page_source or "Вадим" in page_source:
                        logger.info("✅ Авторизация успешна")
                    else:
                        logger.warning("⚠️ Возможно, авторизация не удалась")
                else:
                    logger.info("   Ссылка 'Вход' не найдена, возможно уже авторизованы")
                    
                    # Проверяем, есть ли имя пользователя на странице
                    if "Вадим" in driver.page_source:
                        logger.info("✅ Уже авторизованы")
                    else:
                        logger.warning("⚠️ Не авторизованы и ссылка входа не найдена")
                        
            except Exception as e:
                logger.error(f"❌ Ошибка при авторизации: {e}")
                driver.save_screenshot(f"debug_9111_login_error_{timestamp}.png")
                # Продолжаем, возможно уже авторизованы
            
            # 3. Переход к созданию публикации
            logger.info("3️⃣ Переход к созданию публикации...")
            driver.get("https://www.9111.ru/pubs/add/title/")
            time.sleep(3)
            
            logger.info(f"   Текущий URL: {driver.current_url}")
            driver.save_screenshot(f"debug_9111_add_page_{timestamp}.png")
            
            # Проверяем, что мы на нужной странице
            if "pubs/add/title" not in driver.current_url:
                logger.error(f"❌ Не удалось перейти на страницу создания публикации")
                logger.info(f"   Сохраняю HTML для анализа")
                with open(f"debug_9111_error_page_{timestamp}.html", 'w', encoding='utf-8') as f:
                    f.write(driver.page_source)
                return False
            
            # 4. Извлекаем cookies из Selenium
            logger.info("🍪 Извлечение cookies для requests...")
            selenium_cookies = driver.get_cookies()
            for cookie in selenium_cookies:
                session.cookies.set(cookie['name'], cookie['value'], domain=cookie.get('domain', '.9111.ru'))
            
            # Добавляем необходимые заголовки
            session.headers.update({
                'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
                'Origin': 'https://www.9111.ru',
                'Referer': 'https://www.9111.ru/pubs/add/title/',
            })
            
            # 5. Подготавливаем данные для отправки
            logger.info("5️⃣ Подготовка данных для отправки...")
            
            # Получаем необходимые параметры из страницы (токены и т.д.)
            soup = BeautifulSoup(driver.page_source, 'html.parser')
            
            # Ищем CSRF токен если есть
            csrf_token = None
            csrf_input = soup.find('input', {'name': 'csrf_token'})
            if csrf_input:
                csrf_token = csrf_input.get('value')
            
            # Определяем URL для отправки формы
            form = soup.find('form', {'id': 'form_create_topic_group'})
            if form and form.get('action'):
                post_url = form['action']
                if not post_url.startswith('http'):
                    post_url = 'https://www.9111.ru' + post_url
            else:
                post_url = 'https://www.9111.ru/pubs/add/title/'
            
            logger.info(f"   URL отправки: {post_url}")
            
            # Формируем данные формы
            form_data = {
                'topic_name': title[:150],  # Заголовок
                'rubric_id': '382235',  # ID рубрики "Новости"
                'tag_list_input': 'новости, политика, экономика',  # Теги
                'title_tags': 'новости, политика, экономика',
                'title_private': 'off',  # Не отложенная публикация
            }
            
            # Добавляем CSRF токен если есть
            if csrf_token:
                form_data['csrf_token'] = csrf_token
            
            # Текст публикации
            full_text = f"{content}\n\nИсточник: {source_url}"
            if len(full_text) > 5000:
                full_text = full_text[:5000] + "..."
            
            form_data['komm'] = full_text
            
            # 6. Отправляем POST запрос
            logger.info("6️⃣ Отправка POST запроса...")
            response = session.post(post_url, data=form_data, allow_redirects=True, timeout=30)
            
            logger.info(f"   Статус ответа: {response.status_code}")
            logger.info(f"   URL после редиректа: {response.url}")
            
            # Сохраняем ответ для анализа
            with open(f"debug_9111_response_{timestamp}.html", 'w', encoding='utf-8') as f:
                f.write(response.text)
            
            # 7. Проверяем результат
            if response.status_code == 200:
                if "Спасибо" in response.text or "опубликована" in response.text or "успешно" in response.text.lower():
                    logger.info(f"✅ Статья успешно опубликована на 9111.ru")
                    return True
                else:
                    logger.warning("⚠️ Статья отправлена, но ответ неясен")
                    return True
            else:
                logger.error(f"❌ Ошибка HTTP {response.status_code} при отправке")
                return False
            
        except Exception as e:
            logger.error(f"❌ Критическая ошибка в publish_to_9111: {e}")
            import traceback
            logger.error(traceback.format_exc())
            
            if driver:
                driver.save_screenshot(f"debug_9111_critical_error_{timestamp}.png")
                with open(f"debug_9111_critical_error_{timestamp}.html", 'w', encoding='utf-8') as f:
                    f.write(driver.page_source)
            
            return False
        finally:
            if driver:
                driver.quit()
            logger.info("=" * 60)

    async def create_single_post(self, news_item):
        try:
            loop = asyncio.get_event_loop()

            logger.info("🔄 Перевод заголовка...")
            await asyncio.sleep(random.uniform(0.5, 2))
            title_ru = await loop.run_in_executor(None, self.translate_text, news_item['title'])

            logger.info(f"🔄 Перевод текста ({len(news_item['content'])} символов)...")
            await asyncio.sleep(random.uniform(1, 3))
            content_ru = await loop.run_in_executor(None, self.translate_text, news_item['content'])

            title_escaped = self.escape_html_for_telegram(title_ru)
            content_escaped = self.escape_html_for_telegram(content_ru)

            paragraphs = content_escaped.split('\n\n')
            logger.info(f"📊 Статья содержит {len(paragraphs)} абзацев")

            image_path = None
            if news_item.get('main_image'):
                logger.info(f"🖼️ Скачивание изображения...")
                image_path = await self.download_image(news_item['main_image'])

            final_caption = self.build_caption_with_smart_truncation(
                title=title_escaped,
                paragraphs=paragraphs
            )

            return {
                'image_path': image_path,
                'caption': final_caption,
                'title_ru': title_ru,
                'content_ru': content_ru,
                'source_url': news_item['link']
            }

        except Exception as e:
            logger.error(f"❌ Ошибка создания поста: {e}")
            return None

    async def publish_post(self, post_data, original_item):
        try:
            # Уровень 5: Проверка по схожести заголовков с Telegram
            logger.info("🔍 Проверка заголовка на дубликат в Telegram (75% схожести)...")
            is_dup, similarity = self.check_telegram_duplicate(post_data['title_ru'])
            if is_dup:
                logger.warning(f"⏭️ ПРОПУСК: заголовок совпадает на {similarity}% с существующим")
                return False

            # Публикация в Telegram
            if post_data['image_path']:
                with open(post_data['image_path'], 'rb') as photo:
                    await self.bot.send_photo(
                        chat_id=CHANNEL_ID,
                        photo=photo,
                        caption=post_data['caption'],
                        parse_mode='HTML'
                    )
                try:
                    os.unlink(post_data['image_path'])
                except:
                    pass
                logger.info("✅ Пост в Telegram опубликован")
            else:
                await self.bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=post_data['caption'],
                    parse_mode='HTML'
                )
                logger.info("✅ Пост в Telegram опубликован")

            # Обновляем кэш заголовков
            self.telegram_titles_cache.append(post_data['title_ru'])
            if len(self.telegram_titles_cache) > 100:
                self.telegram_titles_cache = self.telegram_titles_cache[-100:]

            # Публикация на 9111.ru
            if self.ninth_available:
                logger.info("🔄 Начинаю публикацию на 9111.ru...")
                loop = asyncio.get_event_loop()
                success_9111 = await loop.run_in_executor(
                    None, 
                    self.publish_to_9111,
                    post_data['title_ru'],
                    post_data['content_ru'],
                    post_data['source_url']
                )
                
                if success_9111:
                    logger.info("✅ Пост опубликован на 9111.ru")
                else:
                    logger.warning("⚠️ Пост опубликован только в Telegram")
            else:
                logger.info("⏭️ Пропускаем 9111.ru (нет доступа)")
            
            return True

        except TelegramError as e:
            if "Too Many Requests" in str(e):
                logger.warning("⚠️ Лимит Telegram, жду 1 час...")
                await asyncio.sleep(3600)
            elif "Can't parse entities" in str(e):
                plain_text = re.sub(r'<[^>]+>', '', post_data['caption'])
                await self.bot.send_message(chat_id=CHANNEL_ID, text=plain_text)
                return True
            else:
                logger.error(f"❌ Ошибка Telegram: {e}")
                return False

    async def check_and_publish(self):
        logger.info("="*60)
        logger.info(f"🔍 ПРОВЕРКА: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("="*60)

        # Загружаем свежие заголовки из Telegram
        await self.load_telegram_titles_cache()

        news_items = await self.fetch_all_news()
        
        if not news_items:
            logger.info("📭 НОВЫХ УНИКАЛЬНЫХ СТАТЕЙ НЕТ")
            return

        self.post_queue.extend(news_items)
        logger.info(f"📦 В очереди {len(self.post_queue)} уникальных статей")

        await self.try_publish_from_queue()

    async def try_publish_from_queue(self):
        if not self.post_queue:
            return

        if not self.can_post_now():
            next_try = self.get_next_post_delay()
            logger.info(f"⏰ Сейчас нельзя публиковать. Следующая попытка через {next_try//60} минут")
            asyncio.create_task(self.schedule_next_try(next_try))
            return

        item = self.post_queue.pop(0)
        
        logger.info(f"\n📝 ПУБЛИКАЦИЯ: {item['title'][:70]}...")
        logger.info(f"   Источник: {item['source']}")
        logger.info(f"   Осталось в очереди: {len(self.post_queue)}")

        post_data = await self.create_single_post(item)

        if post_data:
            success = await self.publish_post(post_data, item)
            if success:
                self.mark_as_sent(item)
                self.log_post(item['link'], item['title'])
                logger.info(f"✅ Статья опубликована и помечена")

                next_delay = self.get_next_post_delay()
                logger.info(f"⏰ Следующая публикация через {next_delay//60} минут")
                
                asyncio.create_task(self.schedule_next_try(next_delay))
            else:
                logger.error(f"❌ Не удалось опубликовать")
                self.post_queue.insert(0, item)

    async def schedule_next_try(self, delay):
        await asyncio.sleep(delay)
        await self.try_publish_from_queue()

    async def start(self):
        logger.info("="*80)
        logger.info("🚀 NEWS BOT 12.1 - 5 УРОВНЕЙ ЗАЩИТЫ + 9111.RU")
        logger.info("="*80)
        logger.info(f"📢 Канал: {CHANNEL_ID}")
        logger.info(f"⏱ ХАОТИЧНЫЙ РЕЖИМ: {MIN_POST_INTERVAL//60}-{MAX_POST_INTERVAL//60} мин")
        logger.info(f"🛡️ Лимит: {MAX_POSTS_PER_DAY} постов/день")
        logger.info(f"🌍 Часовой пояс: UTC+{TIMEZONE_OFFSET}")
        logger.info(f"🔒 Защита от дублей (5 уровней):")
        logger.info(f"   - URL: {len(self.sent_links)}")
        logger.info(f"   - Заголовки: {len(self.sent_titles)}")
        logger.info(f"   - Хеши: {len(self.sent_hashes)}")
        logger.info(f"   - Первые предложения: {len(self.sent_first_sentences)}")
        logger.info(f"   - Telegram заголовки (75% схожести)")
        logger.info(f"🌐 9111.ru: {'✅ ДОСТУПЕН' if self.ninth_available else '❌ НЕДОСТУПЕН'}")
        logger.info("="*80)

        try:
            me = await self.bot.get_me()
            logger.info(f"✅ Бот @{me.username} авторизован")
        except Exception as e:
            logger.error(f"❌ Ошибка подключения бота: {e}")
            return

        self.post_queue = []

        await self.check_and_publish()

        self.scheduler.add_job(
            self.check_and_publish,
            'interval',
            seconds=CHECK_INTERVAL,
            id='news_checker'
        )
        self.scheduler.start()
        logger.info(f"✅ Планировщик запущен. Проверка каждые {CHECK_INTERVAL//60} минут")

        try:
            while True:
                await asyncio.sleep(60)
        except KeyboardInterrupt:
            logger.info("🛑 Бот остановлен")
            if self.session:
                await self.session.close()

async def main():
    bot = NewsBot()
    await bot.start()

if __name__ == "__main__":
    asyncio.run(main())
