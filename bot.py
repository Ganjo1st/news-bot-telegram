"""
🤖 Telegram News Bot - Версия 13.1
АБСОЛЮТНАЯ ЗАЩИТА ОТ ДУБЛИКАТОВ (5 УРОВНЕЙ) + 9111.RU
ИСПРАВЛЕНА ЗАГРУЗКА ЗАГОЛОВКОВ ИЗ TELEGRAM
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

# Данные для 9111.ru
NINTH_EMAIL = (os.getenv('NINTH_EMAIL') or 
               os.getenv('EMAIL_9111') or 
               '')

NINTH_PASSWORD = (os.getenv('NINTH_PASSWORD') or 
                  os.getenv('EMAIL_PASSWORD') or 
                  '')

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
        
        # Проверяем наличие Chrome
        self.chrome_path = self._find_chrome()
        
        # Отладка переменных
        self._debug_env_vars()
        
        # Проверяем доступность 9111.ru
        self.ninth_available = bool(self.chrome_path and NINTH_EMAIL and NINTH_PASSWORD)

    def _debug_env_vars(self):
        """Отладка переменных окружения"""
        logger.info("=" * 50)
        logger.info("🔍 ПРОВЕРКА ПЕРЕМЕННЫХ:")
        logger.info(f"📧 EMAIL_9111: {os.getenv('EMAIL_9111', 'не задан')}")
        logger.info(f"🔑 EMAIL_PASSWORD: {'*' * len(os.getenv('EMAIL_PASSWORD', '')) if os.getenv('EMAIL_PASSWORD') else 'не задан'}")
        logger.info("=" * 50)

    def _find_chrome(self) -> str:
        """Ищет Chrome в системе"""
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
        
        logger.warning("⚠️ Chrome не найден")
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
        УЛУЧШЕННОЕ сравнение заголовков
        """
        if not title1 or not title2:
            return 0
        
        # Приводим к нижнему регистру и убираем лишние пробелы
        t1 = ' '.join(title1.lower().split())
        t2 = ' '.join(title2.lower().split())
        
        # Если заголовки идентичны
        if t1 == t2:
            return 100.0
        
        # Разбиваем на слова
        words1 = set(t1.split())
        words2 = set(t2.split())
        
        # Считаем общие слова
        common_words = words1.intersection(words2)
        if not common_words:
            return 0
        
        # Вычисляем процент общих слов
        similarity = (len(common_words) / max(len(words1), len(words2))) * 100
        
        # Учитываем длину
        len_ratio = min(len(t1), len(t2)) / max(len(t1), len(t2))
        similarity = similarity * len_ratio
        
        return round(similarity, 2)

    async def load_telegram_titles_cache(self):
        """
        ИСПРАВЛЕННЫЙ МЕТОД загрузки заголовков из Telegram
        Использует get_chat_history (доступно в python-telegram-bot v20+)
        """
        try:
            logger.info("📚 Загрузка заголовков из Telegram...")
            self.telegram_titles_cache = []
            
            # Получаем историю чата
            messages = []
            async for message in self.bot.get_chat_history(chat_id=CHANNEL_ID, limit=100):
                messages.append(message)
            
            logger.info(f"📨 Получено {len(messages)} сообщений")
            
            for message in messages:
                # Извлекаем заголовок из caption или text
                if message.caption:
                    # Убираем HTML теги и берем первую строку
                    clean_caption = re.sub(r'<[^>]+>', '', message.caption)
                    title = clean_caption.split('\n')[0].strip()
                elif message.text:
                    clean_text = re.sub(r'<[^>]+>', '', message.text)
                    title = clean_text.split('\n')[0].strip()
                else:
                    continue
                
                if title and len(title) > 10:
                    self.telegram_titles_cache.append(title)
            
            logger.info(f"✅ Загружено {len(self.telegram_titles_cache)} заголовков")
            
            # Показываем первые 5 для проверки
            if self.telegram_titles_cache:
                logger.info("📋 Примеры заголовков:")
                for i, t in enumerate(self.telegram_titles_cache[:5]):
                    logger.info(f"   {i+1}. {t[:70]}...")
            
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки заголовков: {e}")
            # Пробуем альтернативный метод
            await self._load_telegram_titles_alternative()

    async def _load_telegram_titles_alternative(self):
        """Альтернативный метод через get_updates"""
        try:
            logger.info("📚 Альтернативная загрузка заголовков...")
            
            offset = 0
            limit = 100
            total = 0
            
            while total < 200:  # Максимум 200 сообщений
                updates = await self.bot.get_updates(offset=offset, limit=limit, timeout=30)
                
                if not updates:
                    break
                
                for update in updates:
                    offset = update.update_id + 1
                    
                    # Проверяем channel_post
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
                            self.telegram_titles_cache.append(title)
                            total += 1
                
                await asyncio.sleep(0.5)
            
            logger.info(f"✅ Альтернативный метод загрузил {len(self.telegram_titles_cache)} заголовков")
            
        except Exception as e:
            logger.error(f"❌ Альтернативный метод тоже не сработал: {e}")

    async def check_telegram_duplicate(self, new_title):
        """
        Проверяет заголовок на дубликат в Telegram
        """
        if not self.telegram_titles_cache:
            logger.warning("⚠️ Кэш заголовков пуст, загружаю...")
            await self.load_telegram_titles_cache()
            
            if not self.telegram_titles_cache:
                logger.warning("⚠️ Кэш всё ещё пуст, пропускаю проверку")
                return False, 0, ""
        
        max_similarity = 0
        most_similar_title = ""
        
        for existing_title in self.telegram_titles_cache:
            similarity = self.calculate_title_similarity(new_title, existing_title)
            
            if similarity > max_similarity:
                max_similarity = similarity
                most_similar_title = existing_title
            
            if similarity >= 75:
                logger.info(f"⏭️ НАЙДЕН ДУБЛИКАТ ({similarity}%):")
                logger.info(f"   Новый: {new_title[:70]}...")
                logger.info(f"   Старый: {existing_title[:70]}...")
                return True, similarity, existing_title
        
        logger.info(f"✅ Максимальное сходство: {max_similarity}%")
        return False, max_similarity, most_similar_title

    def is_duplicate(self, article_data):
        """
        ЧЕТЫРЁХУРОВНЕВАЯ ПРОВЕРКА (без Telegram)
        """
        url = article_data.get('link', '')
        title = article_data.get('title', '')
        content = article_data.get('content', '')
        
        if url in self.sent_links:
            logger.info(f"⏭️ ДУБЛИКАТ (URL): {title[:50]}...")
            return True
        
        norm_title = self.normalize_title(title)
        if norm_title and norm_title in self.sent_titles:
            logger.info(f"⏭️ ДУБЛИКАТ (заголовок): {title[:50]}...")
            return True
        
        if content:
            content_hash = self.create_content_hash(content)
            if content_hash and content_hash in self.sent_hashes:
                logger.info(f"⏭️ ДУБЛИКАТ (содержимое): {title[:50]}...")
                return True
        
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
            r'Subscribe|Newsletter|Sign up|Follow us|Share this|Read more|Comments|Advertisement',
            r'Morning Wire|Afternoon Wire|Daily Brief'
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
                if '/article/' not in href:
                    continue
                
                full_url = f"https://apnews.com{href}" if href.startswith('/') else href
                title = link.get_text(strip=True)
                
                if not title or len(title) < 15:
                    continue
                
                title = re.sub(r'\s+', ' ', title).strip()
                articles.append({'url': full_url, 'title': title})

            # Убираем дубликаты
            unique = []
            seen = set()
            for article in articles:
                if article['url'] not in seen:
                    seen.add(article['url'])
                    unique.append(article)

            logger.info(f"✅ Найдено {len(unique)} статей")
            return unique[:10]

        except Exception as e:
            logger.error(f"❌ Ошибка AP News: {e}")
            return []

    def parse_apnews_article(self, url):
        """Парсинг конкретной статьи AP News"""
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
            logger.error(f"❌ Ошибка парсинга статьи: {e}")
            return None

    def parse_infobrics(self, url):
        """Парсинг InfoBrics"""
        try:
            headers = {'User-Agent': 'Mozilla/5.0'}
            response = requests.get(url, headers=headers, timeout=15)
            soup = BeautifulSoup(response.text, 'html.parser')

            title = "Без заголовка"
            title_elem = soup.find('h1') or soup.find('title')
            if title_elem:
                title = self.clean_text(title_elem.get_text())

            main_image = None
            img_elem = soup.find('img', class_=re.compile(r'article-image|featured', re.I))
            if img_elem and img_elem.get('src'):
                img_src = img_elem['src']
                if img_src.startswith('/'):
                    domain = url.split('/')[2]
                    main_image = f"https://{domain}{img_src}"
                else:
                    main_image = img_src

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

            title = "Без заголовка"
            meta_title = soup.find('meta', property='og:title')
            if meta_title and meta_title.get('content'):
                title = meta_title['content']
            else:
                h1 = soup.find('h1')
                if h1:
                    title = h1.get_text(strip=True)

            title = self.clean_text(title)

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
                None, self.get_apnews_articles_v2
            )

            news_items = []
            for article in articles[:3]:
                url = article['url']
                title = article['title']

                # Проверка по URL
                if url in self.sent_links:
                    logger.info(f"⏭️ УЖЕ БЫЛО (URL): {title[:50]}...")
                    continue

                logger.info(f"🔍 НОВАЯ (AP News): {title[:50]}...")

                article_data = await asyncio.get_event_loop().run_in_executor(
                    None, self.parse_apnews_article, url
                )

                if article_data:
                    # Проверка на дубликат (первые 4 уровня)
                    if self.is_duplicate({
                        'link': url,
                        'title': article_data['title'],
                        'content': article_data['content']
                    }):
                        continue
                    
                    # Переводим заголовок
                    logger.info("🔄 Перевод заголовка...")
                    title_ru = await asyncio.get_event_loop().run_in_executor(
                        None, self.translate_text, article_data['title']
                    )
                    
                    # Проверяем с заголовками из Telegram
                    is_dup, similarity, matching = await self.check_telegram_duplicate(title_ru)
                    if is_dup:
                        logger.warning(f"⏭️ ДУБЛИКАТ в Telegram ({similarity}%): {title_ru[:50]}...")
                        continue
                    
                    # Переводим текст
                    logger.info(f"🔄 Перевод текста ({len(article_data['content'])} символов)...")
                    content_ru = await asyncio.get_event_loop().run_in_executor(
                        None, self.translate_text, article_data['content']
                    )
                    
                    news_items.append({
                        'source': 'AP News',
                        'title_original': article_data['title'],
                        'title_ru': title_ru,
                        'content_ru': content_ru,
                        'link': url,
                        'main_image': article_data.get('main_image'),
                        'priority': 1
                    })
                    logger.info(f"✅ Статья готова к публикации")
                else:
                    logger.warning(f"❌ Не удалось спарсить")

                await asyncio.sleep(random.randint(3, 8))

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

            # Выбираем парсер
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

            logger.info(f"📰 В RSS {len(feed.entries)} статей")

            news_items = []
            for entry in feed.entries[:3]:
                link = entry.get('link', '')
                title = entry.get('title', 'Без заголовка')

                # Проверка по URL
                if link in self.sent_links:
                    logger.info(f"⏭️ УЖЕ БЫЛО (URL): {title[:50]}...")
                    continue

                logger.info(f"🔍 НОВАЯ ({source_name}): {title[:50]}...")

                article_data = await asyncio.get_event_loop().run_in_executor(
                    None, parser_func, link
                )

                if article_data:
                    # Проверка на дубликат (первые 4 уровня)
                    if self.is_duplicate({
                        'link': link,
                        'title': article_data['title'],
                        'content': article_data['content']
                    }):
                        continue
                    
                    # Переводим заголовок
                    logger.info("🔄 Перевод заголовка...")
                    title_ru = await asyncio.get_event_loop().run_in_executor(
                        None, self.translate_text, article_data['title']
                    )
                    
                    # Проверяем с заголовками из Telegram
                    is_dup, similarity, matching = await self.check_telegram_duplicate(title_ru)
                    if is_dup:
                        logger.warning(f"⏭️ ДУБЛИКАТ в Telegram ({similarity}%): {title_ru[:50]}...")
                        continue
                    
                    # Переводим текст
                    logger.info(f"🔄 Перевод текста ({len(article_data['content'])} символов)...")
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
                        'priority': priority
                    })
                    logger.info(f"✅ Статья готова к публикации")
                else:
                    logger.warning(f"❌ Не удалось спарсить")

                await asyncio.sleep(random.randint(3, 8))

            return news_items

        except Exception as e:
            logger.error(f"❌ Ошибка RSS {feed_config['name']}: {e}")
            return []

    async def fetch_all_news(self):
        """Загружает новости из всех источников"""
        all_news = []

        for feed in ALL_FEEDS:
            if not feed['enabled']:
                continue

            if feed.get('type') == 'html_apnews_v2':
                news = await self.fetch_from_apnews()
            else:
                news = await self.fetch_from_rss(feed)

            all_news.extend(news)
            await asyncio.sleep(random.randint(5, 10))

        all_news.sort(key=lambda x: x.get('priority', 5))
        logger.info(f"📊 ВСЕГО ГОТОВЫХ К ПУБЛИКАЦИИ: {len(all_news)}")
        return all_news

    async def download_image(self, url):
        """Скачивает изображение"""
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
        """Переводит текст"""
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

    def build_caption(self, title, content, max_length=TELEGRAM_MAX_CAPTION):
        """Формирует подпись для Telegram"""
        title_part = f"<b>{title}</b>"
        
        if len(title_part) + len(content) + 10 <= max_length:
            return f"{title_part}\n\n{content}"
        
        # Обрезаем контент
        available = max_length - len(title_part) - 10
        if available > 100:
            truncated = content[:available] + "..."
            return f"{title_part}\n\n{truncated}"
        else:
            return title_part

    # ========== ПУБЛИКАЦИЯ НА 9111.RU ==========
    def publish_to_9111(self, title, content, source_url):
        """Публикация на 9111.ru"""
        if not self.ninth_available:
            logger.warning("⚠️ 9111.ru недоступен")
            return False
        
        driver = None
        timestamp = int(time.time())
        
        try:
            logger.info("=" * 60)
            logger.info("🌐 ПУБЛИКАЦИЯ НА 9111.RU")
            
            options = Options()
            options.add_argument("--headless=new")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.binary_location = self.chrome_path
            
            driver = webdriver.Chrome(options=options)
            driver.set_page_load_timeout(30)
            
            # Авторизация
            driver.get("https://www.9111.ru")
            time.sleep(2)
            
            try:
                login_link = driver.find_element(By.PARTIAL_LINK_TEXT, "Вход")
                login_link.click()
                time.sleep(1)
                
                email_input = WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.NAME, "email"))
                )
                email_input.send_keys(NINTH_EMAIL)
                
                pass_input = driver.find_element(By.NAME, "pass")
                pass_input.send_keys(NINTH_PASSWORD)
                
                submit_btn = driver.find_element(By.XPATH, "//input[@type='submit']")
                submit_btn.click()
                time.sleep(2)
                logger.info("✅ Авторизация выполнена")
            except:
                logger.info("ℹ️ Возможно уже авторизованы")
            
            # Переход к созданию
            driver.get("https://www.9111.ru/pubs/add/title/")
            time.sleep(2)
            
            # Заголовок
            title_div = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.ID, "topic_name"))
            )
            title_div.click()
            driver.execute_script("arguments[0].innerHTML = '';", title_div)
            title_div.send_keys(title[:150])
            
            # Рубрика
            try:
                rubric = driver.find_element(By.ID, "rubric_id2")
                for option in rubric.find_elements(By.TAG_NAME, "option"):
                    if "Новости" in option.text:
                        option.click()
                        break
            except:
                pass
            
            # Текст
            text_div = driver.find_element(By.ID, "lite_editor")
            full_text = f"{content}\n\nИсточник: {source_url}"[:5000]
            driver.execute_script("arguments[0].innerHTML = arguments[1];", 
                                 text_div, full_text.replace('\n', '<br>'))
            
            # Теги
            try:
                tags = driver.find_element(By.ID, "tag_list_input")
                tags.send_keys("новости, политика, экономика")
            except:
                pass
            
            # Отправка
            publish_btn = driver.find_element(By.ID, "button_create_pubs")
            publish_btn.click()
            time.sleep(3)
            
            logger.info("✅ Пост отправлен на 9111.ru")
            return True
            
        except Exception as e:
            logger.error(f"❌ Ошибка 9111.ru: {e}")
            if driver:
                driver.save_screenshot(f"error_9111_{timestamp}.png")
            return False
        finally:
            if driver:
                driver.quit()

    async def publish_post(self, post_data):
        """Публикует пост в Telegram и на 9111.ru"""
        try:
            # Публикация в Telegram
            caption = self.build_caption(post_data['title_ru'], post_data['content_ru'])
            
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
                else:
                    await self.bot.send_message(
                        chat_id=CHANNEL_ID,
                        text=caption,
                        parse_mode='HTML'
                    )
            else:
                await self.bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=caption,
                    parse_mode='HTML'
                )
            
            logger.info(f"✅ Пост в Telegram опубликован")
            
            # Добавляем в кэш
            self.telegram_titles_cache.append(post_data['title_ru'])
            if len(self.telegram_titles_cache) > 200:
                self.telegram_titles_cache = self.telegram_titles_cache[-200:]

            # Публикация на 9111.ru
            if self.ninth_available:
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
            
            return True

        except TelegramError as e:
            if "Too Many Requests" in str(e):
                logger.warning("⚠️ Лимит Telegram, жду 1 час...")
                await asyncio.sleep(3600)
            else:
                logger.error(f"❌ Ошибка Telegram: {e}")
            return False

    async def process_and_publish(self):
        """Основной процесс"""
        logger.info("=" * 60)
        logger.info(f"🔍 НАЧАЛО ЦИКЛА: {datetime.now()}")
        logger.info("=" * 60)

        # 1. Загружаем заголовки из Telegram
        await self.load_telegram_titles_cache()
        
        # 2. Загружаем новые статьи
        news_items = await self.fetch_all_news()
        
        if not news_items:
            logger.info("📭 НОВЫХ СТАТЕЙ НЕТ")
            return

        # 3. Добавляем в очередь
        self.post_queue.extend(news_items)
        logger.info(f"📦 В ОЧЕРЕДИ: {len(self.post_queue)} статей")

        # 4. Публикуем
        await self.try_publish_from_queue()

    async def try_publish_from_queue(self):
        """Пытается опубликовать следующую статью"""
        if not self.post_queue:
            return

        if not self.can_post_now():
            next_try = self.get_next_post_delay()
            logger.info(f"⏰ Сейчас нельзя публиковать. Следующая попытка через {next_try//60} мин")
            asyncio.create_task(self._schedule_next_try(next_try))
            return

        item = self.post_queue.pop(0)
        
        logger.info(f"\n📝 ПУБЛИКАЦИЯ: {item['title_ru'][:70]}...")
        logger.info(f"   Источник: {item['source']}")
        logger.info(f"   Осталось в очереди: {len(self.post_queue)}")

        success = await self.publish_post(item)
        
        if success:
            # Помечаем как отправленное
            self.mark_as_sent({
                'link': item['link'],
                'title': item['title_original'],
                'content': item['content_ru']
            })
            self.log_post(item['link'], item['title_original'])
            
            next_delay = self.get_next_post_delay()
            logger.info(f"⏰ Следующая публикация через {next_delay//60} минут")
            asyncio.create_task(self._schedule_next_try(next_delay))
        else:
            logger.error(f"❌ Не удалось опубликовать")
            self.post_queue.insert(0, item)
            asyncio.create_task(self._schedule_next_try(300))

    async def _schedule_next_try(self, delay):
        """Планирует следующую попытку"""
        await asyncio.sleep(delay)
        await self.try_publish_from_queue()

    async def start(self):
        """Запускает бота"""
        logger.info("=" * 80)
        logger.info("🚀 NEWS BOT 13.1 - 5 УРОВНЕЙ ЗАЩИТЫ")
        logger.info("=" * 80)
        logger.info(f"📢 Канал: {CHANNEL_ID}")
        logger.info(f"⏱ Интервал: {MIN_POST_INTERVAL//60}-{MAX_POST_INTERVAL//60} мин")
        logger.info(f"🛡️ Лимит: {MAX_POSTS_PER_DAY} постов/день")
        logger.info(f"🔒 Защита: URL + Заголовки + Хеши + Первые предложения + Telegram (75%)")
        logger.info(f"🌐 9111.ru: {'✅ ДОСТУПЕН' if self.ninth_available else '❌ НЕДОСТУПЕН'}")
        logger.info("=" * 80)

        try:
            me = await self.bot.get_me()
            logger.info(f"✅ Бот @{me.username} авторизован")
        except Exception as e:
            logger.error(f"❌ Ошибка авторизации: {e}")
            return

        # Запускаем первый цикл
        await self.process_and_publish()

        # Планировщик
        self.scheduler.add_job(
            self.process_and_publish,
            'interval',
            seconds=CHECK_INTERVAL,
            id='news_processor'
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
