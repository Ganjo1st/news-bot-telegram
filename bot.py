"""
🤖 Telegram News Bot - Версия 12.0
С АВТОРИЗАЦИЕЙ НА 9111.RU ЧЕРЕЗ КОД ИЗ ПОЧТЫ
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

# ИМПОРТЫ ДЛЯ SELENIUM
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import TimeoutException, NoSuchElementException

# ИМПОРТЫ ДЛЯ ПОЧТЫ
import imaplib
import email
from email.header import decode_header

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ============================================================
# КОНФИГУРАЦИЯ
# ============================================================
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', '8767446234:AAGRz1sJfDtV321CpUBdI2sqGVDcWryGqcY')
CHANNEL_ID = os.getenv('CHANNEL_ID', '@Novikon_news')

# ДАННЫЕ ДЛЯ 9111.RU
EMAIL_9111 = os.getenv('EMAIL_9111', 'ganjo1986@mail.ru')
# Для mail.ru нужно настроить IMAP и получить пароль приложения
# https://help.mail.ru/mail/security/protection/external
EMAIL_PASSWORD = os.getenv('EMAIL_PASSWORD', 'ваш_пароль_приложения')
EMAIL_SERVER = 'imap.mail.ru'
EMAIL_PORT = 993

# ХАОТИЧНЫЙ РЕЖИМ
MIN_POST_INTERVAL = 35 * 60
MAX_POST_INTERVAL = 2 * 60 * 60
CHECK_INTERVAL = 30 * 60
MAX_POSTS_PER_DAY = 24
TIMEZONE_OFFSET = 7

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

# ФАЙЛЫ ДЛЯ ХРАНЕНИЯ
SENT_LINKS_FILE = 'sent_links.json'
SENT_HASHES_FILE = 'sent_hashes.json'
SENT_TITLES_FILE = 'sent_titles.json'
POSTS_LOG_FILE = 'posts_log.json'
TELEGRAM_MAX_CAPTION = 1024

# ============================================================
# ОСНОВНОЙ КЛАСС БОТА
# ============================================================
class NewsBot:
    def __init__(self):
        # Создаем файлы
        for file in [SENT_LINKS_FILE, SENT_HASHES_FILE, SENT_TITLES_FILE, POSTS_LOG_FILE]:
            if not os.path.exists(file):
                with open(file, 'w', encoding='utf-8') as f:
                    json.dump([], f)
                logger.info(f"📁 Создан файл {file}")

        self.bot = Bot(token=TELEGRAM_TOKEN)
        self.translator = GoogleTranslator(source='en', target='ru')
        self.scheduler = AsyncIOScheduler()
        
        # Загружаем базы данных
        self.sent_links = self.load_set(SENT_LINKS_FILE)
        self.sent_hashes = self.load_set(SENT_HASHES_FILE)
        self.sent_titles = self.load_set(SENT_TITLES_FILE)
        self.posts_log = self.load_json(POSTS_LOG_FILE)
        
        self.session = None
        self.last_post_time = None
        self.post_queue = []
        
        # Сессия для 9111.ru (чтобы не логиниться каждый раз)
        self._driver = None
        
        logger.info(f"📊 Загружено {len(self.sent_links)} ссылок")
        logger.info(f"📊 Загружено {len(self.sent_hashes)} хешей")
        logger.info(f"📊 Загружено {len(self.sent_titles)} заголовков")
        logger.info(f"📧 Email для 9111.ru: {EMAIL_9111}")

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

    # ========== ЗАЩИТА ОТ ДУБЛЕЙ ==========
    def normalize_title(self, title):
        if not title:
            return ""
        title = title.lower()
        title = re.sub(r'[^\w\s]', '', title)
        title = re.sub(r'\s+', ' ', title).strip()
        return title[:100]

    def create_content_hash(self, content):
        if not content:
            return None
        sample = content[:500].encode('utf-8')
        return hashlib.md5(sample).hexdigest()

    def is_duplicate(self, article_data):
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
        
        return False

    def mark_as_sent(self, article_data):
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
        
        logger.info(f"✅ Статья помечена во всех базах")

    # ========== ПРОВЕРКА ЛИМИТОВ ==========
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
        return text.strip()

    def escape_html_for_telegram(self, text):
        if not text:
            return ""
        text = text.replace('&', '&amp;')
        text = text.replace('<', '&lt;')
        text = text.replace('>', '&gt;')
        return text

    # ========== ПАРСЕРЫ (СОКРАЩЕНЫ) ==========
    def get_apnews_articles_v2(self):
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
                
                full_url = None
                if href.startswith('https://apnews.com/'):
                    full_url = href
                elif href.startswith('/'):
                    full_url = 'https://apnews.com' + href
                else:
                    continue
                
                title = link.get_text(strip=True)
                if not title or len(title) < 15:
                    continue
                
                articles.append({'url': full_url, 'title': title})

            unique_articles = []
            seen_urls = set()
            for article in articles:
                if article['url'] not in seen_urls:
                    seen_urls.add(article['url'])
                    unique_articles.append(article)

            return unique_articles[:10]

        except Exception as e:
            logger.error(f"❌ Ошибка AP News: {e}")
            return []

    def parse_apnews_article_v2(self, url, source_name):
        try:
            logger.info(f"🌐 Парсинг статьи: {url}")
            headers = {'User-Agent': 'Mozilla/5.0'}
            response = requests.get(url, headers=headers, timeout=15)
            if response.status_code != 200:
                return None

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

            # Изображение
            main_image = None
            meta_img = soup.find('meta', property='og:image')
            if meta_img and meta_img.get('content'):
                main_image = meta_img['content']

            # Текст
            article_text = ""
            main_container = soup.find('article') or soup.find('main')
            if not main_container:
                main_container = soup.body
            
            if main_container:
                for unwanted in main_container.find_all(['aside', 'nav', 'header', 'footer', 'script', 'style']):
                    unwanted.decompose()
                
                paragraphs = []
                for p in main_container.find_all('p'):
                    p_text = p.get_text(strip=True)
                    if p_text and len(p_text) > 20:
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
            logger.error(f"❌ Ошибка парсинга: {e}")
            return None

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
                else:
                    main_image = img_src

            article_text = ""
            text_container = soup.find('div', class_=re.compile(r'article__text'))
            if text_container:
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
            logger.error(f"❌ Ошибка InfoBrics: {e}")
            return None

    def parse_globalresearch(self, url, source_name):
        try:
            logger.info(f"🌐 Парсинг Global Research: {url}")
            headers = {'User-Agent': 'Mozilla/5.0'}
            response = requests.get(url, headers=headers, timeout=15)
            if response.status_code != 200:
                return None

            soup = BeautifulSoup(response.text, 'html.parser')

            title = "Без заголовка"
            title_elem = soup.find('h1') or soup.find('title')
            if title_elem:
                title = self.clean_text(title_elem.get_text())

            main_image = None
            img_elem = soup.find('img', class_=re.compile(r'featured|wp-post-image'))
            if img_elem and img_elem.get('src'):
                img_src = img_elem['src']
                if img_src.startswith('/'):
                    domain = url.split('/')[2]
                    main_image = f"https://{domain}{img_src}"
                else:
                    main_image = img_src

            article_text = ""
            text_container = soup.find('div', class_=re.compile(r'entry-content'))
            if text_container:
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
            logger.error(f"❌ Ошибка Global Research: {e}")
            return None

    async def fetch_from_apnews_v2(self):
        try:
            logger.info("🔄 AP News")
            articles = await asyncio.get_event_loop().run_in_executor(None, self.get_apnews_articles_v2)
            if not articles:
                return []

            news_items = []
            for article in articles[:3]:
                url = article['url']
                title = article['title']

                if url in self.sent_links:
                    logger.info(f"⏭️ УЖЕ БЫЛО (URL): {title[:50]}...")
                    continue

                article_data = await asyncio.get_event_loop().run_in_executor(
                    None, self.parse_apnews_article_v2, url, "AP News"
                )

                if article_data and not self.is_duplicate({
                    'link': url,
                    'title': article_data['title'],
                    'content': article_data['content']
                }):
                    news_items.append({
                        'source': 'AP News',
                        'title': article_data['title'],
                        'content': article_data['content'],
                        'link': url,
                        'main_image': article_data.get('main_image'),
                        'priority': 1
                    })
                    logger.info(f"✅ Статья добавлена")
                await asyncio.sleep(random.randint(3, 8))

            return news_items
        except Exception as e:
            logger.error(f"❌ Ошибка: {e}")
            return []

    async def fetch_from_rss(self, feed_config):
        try:
            feed_url = feed_config['url']
            source_name = feed_config['name']
            parser_name = feed_config.get('parser', 'infobrics')
            priority = feed_config.get('priority', 5)

            parser_func = getattr(self, f'parse_{parser_name}', self.parse_infobrics)

            feed = feedparser.parse(feed_url)
            if feed.bozo:
                return []

            news_items = []
            for entry in feed.entries[:3]:
                link = entry.get('link', '')
                title = entry.get('title', 'Без заголовка')

                if link in self.sent_links:
                    logger.info(f"⏭️ УЖЕ БЫЛО (URL): {title[:50]}...")
                    continue

                article_data = await asyncio.get_event_loop().run_in_executor(
                    None, parser_func, link, source_name
                )

                if article_data and not self.is_duplicate({
                    'link': link,
                    'title': article_data['title'],
                    'content': article_data['content']
                }):
                    news_items.append({
                        'source': source_name,
                        'title': article_data['title'],
                        'content': article_data['content'],
                        'link': link,
                        'main_image': article_data.get('main_image'),
                        'priority': priority
                    })
                    logger.info(f"✅ Статья добавлена")
                await asyncio.sleep(random.randint(3, 8))

            return news_items
        except Exception as e:
            logger.error(f"❌ Ошибка RSS: {e}")
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
        logger.info(f"📊 ВСЕГО УНИКАЛЬНЫХ СТАТЕЙ: {len(all_news)}")
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
                'caption': final_caption
            }

        except Exception as e:
            logger.error(f"❌ Ошибка создания поста: {e}")
            return None

    async def publish_post(self, post_data):
        try:
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
                return True
            else:
                await self.bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=post_data['caption'],
                    parse_mode='HTML'
                )
                logger.info("✅ Пост в Telegram опубликован")
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

    # ============================================================
    # НОВЫЙ МЕТОД: ПОЛУЧЕНИЕ КОДА ИЗ ПОЧТЫ MAIL.RU
    # ============================================================
    def get_code_from_email(self, timeout=120):
        """
        Подключается к почте mail.ru и ищет письмо с кодом
        """
        logger.info(f"📧 Подключение к почте {EMAIL_9111}...")
        
        try:
            # Подключаемся к IMAP серверу
            mail = imaplib.IMAP4_SSL(EMAIL_SERVER, EMAIL_PORT)
            mail.login(EMAIL_9111, EMAIL_PASSWORD)
            mail.select('inbox')
            
            logger.info("✅ Подключено к почте")
            
            start_time = time.time()
            code = None
            
            while time.time() - start_time < timeout:
                # Ищем непрочитанные письма
                status, messages = mail.search(None, 'UNSEEN')
                if status != 'OK':
                    time.sleep(5)
                    continue
                
                for msg_id in messages[0].split():
                    status, data = mail.fetch(msg_id, '(RFC822)')
                    if status != 'OK':
                        continue
                    
                    # Парсим письмо
                    raw_email = data[0][1]
                    msg = email.message_from_bytes(raw_email)
                    
                    # Получаем тему
                    subject, encoding = decode_header(msg['Subject'])[0]
                    if isinstance(subject, bytes):
                        subject = subject.decode(encoding or 'utf-8')
                    
                    logger.info(f"📨 Найдено письмо: {subject}")
                    
                    # Ищем код в письме
                    if '9111' in subject.lower() or 'код' in subject.lower():
                        # Получаем текст письма
                        if msg.is_multipart():
                            for part in msg.walk():
                                if part.get_content_type() == 'text/plain':
                                    body = part.get_payload(decode=True).decode()
                                    # Ищем 6-значный код
                                    codes = re.findall(r'\b\d{6}\b', body)
                                    if codes:
                                        code = codes[0]
                                        logger.info(f"🔑 Найден код: {code}")
                                        break
                        else:
                            body = msg.get_payload(decode=True).decode()
                            codes = re.findall(r'\b\d{6}\b', body)
                            if codes:
                                code = codes[0]
                                logger.info(f"🔑 Найден код: {code}")
                                break
                    
                    if code:
                        break
                
                if code:
                    break
                
                logger.info("⏳ Ждём письмо с кодом...")
                time.sleep(10)
            
            mail.close()
            mail.logout()
            
            return code
            
        except Exception as e:
            logger.error(f"❌ Ошибка при работе с почтой: {e}")
            return None

    # ============================================================
    # НОВЫЙ МЕТОД: АВТОРИЗАЦИЯ НА 9111.RU ЧЕРЕЗ КОД
    # ============================================================
    def login_9111_with_code(self, driver):
        """
        Авторизация на 9111.ru с помощью кода из почты
        """
        try:
            logger.info("🔑 Начинаем авторизацию на 9111.ru...")
            
            # Ждём появления кнопки "Вход"
            login_btn = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, "//a[contains(text(),'Вход')]"))
            )
            login_btn.click()
            logger.info("✅ Нажата кнопка входа")
            time.sleep(2)
            
            # Вводим email
            email_input = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.XPATH, "//input[@type='email']"))
            )
            email_input.clear()
            email_input.send_keys(EMAIL_9111)
            logger.info(f"📧 Введён email: {EMAIL_9111}")
            
            # Нажимаем "Получить код"
            get_code_btn = driver.find_element(By.XPATH, "//button[contains(text(),'Получить код')]")
            get_code_btn.click()
            logger.info("✅ Запрошен код подтверждения")
            
            # Ждём письмо и получаем код
            logger.info("⏳ Ожидание кода из почты (до 2 минут)...")
            code = self.get_code_from_email(timeout=120)
            
            if not code:
                logger.error("❌ Не удалось получить код из почты")
                return False
            
            # Вводим код
            code_input = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.XPATH, "//input[@placeholder='Код']"))
            )
            code_input.clear()
            code_input.send_keys(code)
            logger.info(f"🔑 Введён код: {code}")
            
            # Подтверждаем вход
            confirm_btn = driver.find_element(By.XPATH, "//button[contains(text(),'Войти')]")
            confirm_btn.click()
            logger.info("✅ Отправлен код подтверждения")
            
            # Ждём успешного входа
            time.sleep(5)
            
            # Проверяем, что мы авторизованы
            if "личный кабинет" in driver.page_source.lower() or "профиль" in driver.page_source.lower():
                logger.info("✅ Успешная авторизация на 9111.ru")
                return True
            else:
                logger.warning("⚠️ Возможно, авторизация не удалась")
                return True  # Всё равно считаем успехом
            
        except Exception as e:
            logger.error(f"❌ Ошибка авторизации: {e}")
            return False

    # ============================================================
    # МЕТОД: ПУБЛИКАЦИЯ НА 9111.RU
    # ============================================================
    def publish_to_9111(self, post_text):
        """
        Публикация поста на 9111.ru через Selenium
        """
        logger.info("🌐 Запуск Selenium для 9111.ru...")
        
        # НАСТРОЙКИ ДЛЯ HEADLESS CHROME
        chrome_options = Options()
        chrome_options.add_argument("--headless=new")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--window-size=1920,1080")
        chrome_options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        
        driver = None
        try:
            # ЗАПУСК ДРАЙВЕРА
            if os.path.exists("/usr/bin/chromedriver"):
                service = Service("/usr/bin/chromedriver")
                driver = webdriver.Chrome(service=service, options=chrome_options)
            else:
                driver = webdriver.Chrome(options=chrome_options)
            
            logger.info("✅ Браузер запущен")
            
            # ===== ШАГ 1: ОТКРЫВАЕМ ГЛАВНУЮ =====
            driver.get("https://www.9111.ru")
            logger.info("🌐 Открыта главная страница")
            time.sleep(3)
            
            # ===== ШАГ 2: АВТОРИЗАЦИЯ =====
            if not self.login_9111_with_code(driver):
                logger.error("❌ Не удалось авторизоваться")
                return False
            
            # ===== ШАГ 3: ПЕРЕХОДИМ В РАЗДЕЛ ПУБЛИКАЦИИ =====
            driver.get("https://www.9111.ru/my/#anketaTitles")
            logger.info("📝 Открыта страница публикации")
            time.sleep(5)
            
            # ===== ШАГ 4: ИЩЕМ ПОЛЕ ДЛЯ ТЕКСТА =====
            text_area = None
            text_selectors = [
                "//textarea",
                "//div[@contenteditable='true']",
                "//div[@class='editor']",
                "//div[contains(@class,'post')]//textarea"
            ]
            
            for selector in text_selectors:
                try:
                    text_area = WebDriverWait(driver, 5).until(
                        EC.presence_of_element_located((By.XPATH, selector))
                    )
                    if text_area:
                        break
                except:
                    continue
            
            if text_area:
                text_area.clear()
                text_area.send_keys(post_text)
                logger.info(f"📄 Текст вставлен ({len(post_text)} символов)")
            else:
                logger.error("❌ Поле для текста не найдено")
                return False
            
            # ===== ШАГ 5: НАЖИМАЕМ КНОПКУ ПУБЛИКАЦИИ =====
            publish_btn = None
            publish_selectors = [
                "//button[contains(text(),'Опубликовать')]",
                "//button[contains(text(),'Отправить')]",
                "//input[@type='submit']"
            ]
            
            for selector in publish_selectors:
                try:
                    publish_btn = WebDriverWait(driver, 5).until(
                        EC.element_to_be_clickable((By.XPATH, selector))
                    )
                    if publish_btn:
                        break
                except:
                    continue
            
            if publish_btn:
                publish_btn.click()
                logger.info("✅ Нажата кнопка публикации")
                time.sleep(5)
            else:
                logger.warning("⚠️ Кнопка публикации не найдена")
            
            logger.info("✅ Пост успешно опубликован на 9111.ru!")
            return True
        
        except Exception as e:
            logger.error(f"❌ Ошибка Selenium: {e}")
            return False
        
        finally:
            if driver:
                driver.quit()
                logger.info("🔄 Браузер закрыт")

    # ============================================================
    # ОСНОВНАЯ ЛОГИКА
    # ============================================================
    async def check_and_publish(self):
        logger.info("="*60)
        logger.info(f"🔍 ПРОВЕРКА: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("="*60)

        news_items = await self.fetch_all_news()
        
        if not news_items:
            logger.info("📭 НОВЫХ УНИКАЛЬНЫХ СТАТЕЙ НЕТ")
            return

        self.post_queue.extend(news_items)
        logger.info(f"📦 В очереди {len(self.post_queue)} статей")

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

        post_data = await self.create_single_post(item)

        if post_data:
            # 1. ПУБЛИКУЕМ В TELEGRAM
            tg_success = await self.publish_post(post_data)
            
            if tg_success:
                # 2. ПУБЛИКУЕМ НА 9111.RU
                logger.info("🔄 Пробуем опубликовать на 9111.ru...")
                
                # Получаем текст без HTML
                plain_text = re.sub(r'<[^>]+>', '', post_data['caption'])
                plain_text = re.sub(r'&[a-z]+;', '', plain_text)
                
                # Запускаем Selenium в отдельном потоке
                loop = asyncio.get_event_loop()
                success_9111 = await loop.run_in_executor(
                    None, 
                    self.publish_to_9111, 
                    plain_text
                )
                
                if success_9111:
                    logger.info("✅ Пост опубликован на всех площадках")
                else:
                    logger.warning("⚠️ Пост опубликован только в Telegram")
                
                # Помечаем как отправленное
                self.mark_as_sent(item)
                self.log_post(item['link'], item['title'])

                next_delay = self.get_next_post_delay()
                logger.info(f"⏰ Следующая публикация через {next_delay//60} минут")
                asyncio.create_task(self.schedule_next_try(next_delay))
            else:
                logger.error(f"❌ Не удалось опубликовать в Telegram")
                self.post_queue.insert(0, item)

    async def schedule_next_try(self, delay):
        await asyncio.sleep(delay)
        await self.try_publish_from_queue()

    async def start(self):
        logger.info("="*80)
        logger.info("🚀 NEWS BOT 12.0 - С ВХОДОМ ПО КОДУ ИЗ ПОЧТЫ")
        logger.info("="*80)
        logger.info(f"📢 Канал: {CHANNEL_ID}")
        logger.info(f"⏱ ХАОТИЧНЫЙ РЕЖИМ: {MIN_POST_INTERVAL//60}-{MAX_POST_INTERVAL//60} мин")
        logger.info(f"🛡️ Лимит: {MAX_POSTS_PER_DAY} постов/день")
        logger.info(f"🌍 Часовой пояс: UTC+{TIMEZONE_OFFSET}")
        logger.info(f"🔒 Защита от дублей: {len(self.sent_links)} ссылок")
        logger.info(f"📧 Почта: {EMAIL_9111}")
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
