"""
🤖 Telegram News Bot - Версия 9.0
ЕЖЕЧАСНАЯ ПУБЛИКАЦИЯ СО ВСЕХ ИСТОЧНИКОВ

- UTC+7 часовой пояс
- Без указания источника
- Умная обрезка текста
- ВСЕ источники КАЖДЫЙ ДЕНЬ
- Публикация КАЖДЫЙ ЧАС
"""

import os
import logging
import feedparser
import re
import html
import requests
import time
import random
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
CHECK_INTERVAL = 3600  # 1 час (3600 секунд)
MIN_POST_INTERVAL = 600  # Базовая пауза 10 минут (на случай нескольких постов подряд)
MAX_POSTS_PER_DAY = 24  # Лимит постов в день (максимум 24 при ежечасной публикации)
TIMEZONE_OFFSET = 7  # Смещение для UTC+7

# ============================================================
# ВСЕ ИСТОЧНИКИ (КАЖДЫЙ ДЕНЬ)
# ============================================================
ALL_FEEDS = [
    # InfoBrics
    {
        'name': 'InfoBrics',
        'url': 'https://infobrics.org/rss/en',
        'enabled': True,
        'parser': 'infobrics',
        'type': 'rss',
        'priority': 1  # Приоритет (чем меньше, тем важнее)
    },
    # Global Research
    {
        'name': 'Global Research',
        'url': 'https://www.globalresearch.ca/feed',
        'enabled': True,
        'parser': 'globalresearch',
        'type': 'rss',
        'priority': 2
    },
    # AP News (прямой парсинг)
    {
        'name': 'AP News',
        'url': 'https://apnews.com/',
        'enabled': True,
        'type': 'html_apnews',
        'priority': 1
    }
]

# ============================================================
# ФАЙЛЫ ДЛЯ ХРАНЕНИЯ ДАННЫХ
# ============================================================
SENT_LINKS_FILE = 'sent_links.json'
POSTS_LOG_FILE = 'posts_log.json'
TELEGRAM_MAX_CAPTION = 1024  # Лимит подписи к фото

# ============================================================
# ОСНОВНОЙ КЛАСС БОТА
# ============================================================
class NewsBot:
    def __init__(self):
        # Создаем файлы если их нет
        for file in [SENT_LINKS_FILE, POSTS_LOG_FILE]:
            if not os.path.exists(file):
                with open(file, 'w', encoding='utf-8') as f:
                    json.dump([], f)
                logger.info(f"📁 Создан файл {file}")

        self.bot = Bot(token=TELEGRAM_TOKEN)
        self.translator = GoogleTranslator(source='en', target='ru')
        self.scheduler = AsyncIOScheduler()
        self.sent_links = self.load_sent_links()
        self.posts_log = self.load_json(POSTS_LOG_FILE)
        self.session = None
        self.last_post_time = None
        
        # Статистика
        logger.info(f"📊 Загружено {len(self.sent_links)} ранее опубликованных ссылок")
        logger.info(f"📊 Загружено {len(self.posts_log)} записей в логе постов")
        logger.info(f"⏱ Режим: ежечасная публикация (каждые {CHECK_INTERVAL//60} минут)")

    # ========== РАБОТА С JSON ==========
    def load_json(self, filename):
        """Стандартная загрузка JSON"""
        try:
            with open(filename, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки {filename}: {e}")
            return []

    def load_sent_links(self):
        """Загрузка опубликованных ссылок"""
        try:
            if os.path.exists(SENT_LINKS_FILE):
                with open(SENT_LINKS_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        return set(data)
                    else:
                        logger.warning(f"⚠️ Неверный формат {SENT_LINKS_FILE}, создаю новый")
                        return set()
            else:
                return set()
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки {SENT_LINKS_FILE}: {e}")
            return set()

    def save_sent_links(self):
        """Сохраняет множество опубликованных ссылок"""
        try:
            with open(SENT_LINKS_FILE, 'w', encoding='utf-8') as f:
                json.dump(list(self.sent_links), f, ensure_ascii=False, indent=2)
            logger.debug(f"💾 Сохранено {len(self.sent_links)} ссылок")
        except Exception as e:
            logger.error(f"❌ Ошибка сохранения {SENT_LINKS_FILE}: {e}")

    def save_json(self, filename, data):
        """Стандартное сохранение JSON"""
        try:
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"❌ Ошибка сохранения {filename}: {e}")

    # ========== ПРОВЕРКА ЛИМИТОВ ==========
    def can_post_now(self):
        """Проверка всех лимитов с учетом часового пояса UTC+7"""
        local_hour = (datetime.now().hour + TIMEZONE_OFFSET) % 24
        # Не публикуем с 23:00 до 07:00 по местному времени
        if 23 <= local_hour or local_hour < 7:
            logger.info(f"🌙 Ночное время по местному ({local_hour}:00), пропускаю")
            return False

        # Проверка интервала между постами
        if self.last_post_time:
            time_diff = datetime.now() - self.last_post_time
            if time_diff < timedelta(seconds=MIN_POST_INTERVAL):
                logger.info(f"⏳ С последнего поста прошло {time_diff.seconds}с, нужно ждать {MIN_POST_INTERVAL}с")
                return False

        # Проверка дневного лимита
        today = datetime.now().date()
        today_posts = 0
        for post in self.posts_log:
            try:
                post_time_str = post['time'].split('.')[0]
                post_date = datetime.fromisoformat(post_time_str).date()
                if post_date == today:
                    today_posts += 1
            except:
                continue

        if today_posts >= MAX_POSTS_PER_DAY:
            logger.info(f"⏳ Достигнут дневной лимит {MAX_POSTS_PER_DAY} постов")
            return False

        logger.info(f"✅ Можно публиковать (сегодня {today_posts}/{MAX_POSTS_PER_DAY})")
        return True

    def log_post(self, link, title):
        """Запись информации об опубликованном посте"""
        self.posts_log.append({
            'link': link,
            'title': title[:50],
            'time': datetime.now().isoformat()
        })
        # Оставляем только последние 100 записей
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

    # ========== ПАРСЕР AP NEWS ==========
    def get_apnews_articles(self):
        """Парсит главную страницу AP News и возвращает список свежих статей"""
        try:
            logger.info("🌐 Парсинг главной страницы AP News")
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            response = requests.get('https://apnews.com/', headers=headers, timeout=15)
            if response.status_code != 200:
                logger.error(f"❌ Ошибка загрузки AP News: {response.status_code}")
                return []

            soup = BeautifulSoup(response.text, 'html.parser')
            articles = []

            # Ищем все ссылки на статьи
            for link in soup.find_all('a', href=True):
                href = link['href']
                
                full_url = None
                if '/article/' in href:
                    if href.startswith('https://apnews.com/'):
                        full_url = href
                    elif href.startswith('/'):
                        full_url = 'https://apnews.com' + href
                
                if not full_url:
                    continue

                # Получаем заголовок
                title = link.get_text(strip=True)
                if not title or len(title) < 15:
                    parent = link.find_parent(['h2', 'h3'])
                    if parent:
                        title = parent.get_text(strip=True)
                    else:
                        continue

                title = re.sub(r'\s+', ' ', title).strip()
                if len(title) < 15:
                    continue

                articles.append({
                    'url': full_url,
                    'title': title
                })

            # Убираем дубликаты
            unique_articles = []
            seen_urls = set()
            for article in articles:
                if article['url'] not in seen_urls:
                    seen_urls.add(article['url'])
                    unique_articles.append(article)

            logger.info(f"✅ Найдено {len(unique_articles)} статей на главной AP News")
            return unique_articles[:5]

        except Exception as e:
            logger.error(f"❌ Ошибка парсинга AP News: {e}")
            return []

    def parse_apnews_article(self, url, source_name):
        """Парсинг отдельной статьи AP News"""
        try:
            logger.info(f"🌐 Парсинг статьи AP News: {url}")
            headers = {'User-Agent': 'Mozilla/5.0'}
            response = requests.get(url, headers=headers, timeout=15)
            if response.status_code != 200:
                logger.error(f"❌ Ошибка загрузки: HTTP {response.status_code}")
                return None

            soup = BeautifulSoup(response.text, 'html.parser')

            # Заголовок
            title = "Без заголовка"
            title_elem = soup.find('h1')
            if title_elem:
                title = self.clean_text(title_elem.get_text())
                title = re.sub(r'\s*\|.*AP\s*News.*$', '', title, flags=re.IGNORECASE)

            # Изображение
            main_image = None
            meta_img = soup.find('meta', property='og:image')
            if meta_img and meta_img.get('content'):
                main_image = meta_img['content']
            else:
                img_elem = soup.find('img', src=re.compile(r'\.(jpg|jpeg|png|webp)', re.I))
                if img_elem and img_elem.get('src'):
                    img_src = img_elem['src']
                    if img_src.startswith('/'):
                        main_image = 'https://apnews.com' + img_src
                    elif img_src.startswith('http'):
                        main_image = img_src

            # Текст статьи
            article_text = ""
            possible_containers = [
                soup.find('div', class_=re.compile(r'Article', re.I)),
                soup.find('div', class_=re.compile(r'story-body', re.I)),
                soup.find('div', class_=re.compile(r'RichTextStoryBody', re.I)),
                soup.find('article'),
                soup.find('main')
            ]

            text_container = None
            for container in possible_containers:
                if container:
                    text_container = container
                    break

            if text_container:
                for unwanted in text_container.find_all(['script', 'style', 'button', 'aside', 'nav']):
                    unwanted.decompose()

                paragraphs = []
                for p in text_container.find_all('p'):
                    p_text = self.clean_text(p.get_text())
                    if p_text and len(p_text) > 20:
                        paragraphs.append(p_text)

                if paragraphs:
                    article_text = '\n\n'.join(paragraphs)

            if len(article_text) < 200:
                all_text = self.clean_text(soup.get_text())
                if title in all_text:
                    start = all_text.find(title) + len(title)
                    article_text = all_text[start:start + 2000].strip()
                else:
                    article_text = all_text[:2000]

            if len(article_text) < 200:
                logger.warning(f"⚠️ Текст слишком короткий ({len(article_text)} символов)")
                return None

            logger.info(f"✅ Успешно спарсено: {len(article_text)} символов")
            return {
                'title': title,
                'content': article_text,
                'main_image': main_image
            }

        except Exception as e:
            logger.error(f"❌ Ошибка парсинга статьи AP News: {e}")
            return None

    # ========== ПАРСЕР INFOBRICS ==========
    def parse_infobrics(self, url, source_name):
        try:
            logger.info(f"🌐 Парсинг {source_name}: {url}")
            headers = {'User-Agent': 'Mozilla/5.0'}
            response = requests.get(url, headers=headers, timeout=15)
            if response.status_code != 200:
                return None

            soup = BeautifulSoup(response.text, 'html.parser')

            # Заголовок
            title = "Без заголовка"
            title_elem = soup.find('div', class_=re.compile(r'title.*big')) or soup.find('h1')
            if title_elem:
                title = self.clean_text(title_elem.get_text())

            # Изображение
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

            # Текст
            article_text = ""
            text_container = soup.find('div', class_=re.compile(r'article__text')) or soup.find('div', class_=re.compile(r'article'))
            if text_container:
                for unwanted in text_container.find_all(['script', 'style', 'button']):
                    unwanted.decompose()
                paragraphs = []
                for p in text_container.find_all('p'):
                    p_text = self.clean_text(p.get_text())
                    if p_text and len(p_text) > 15:
                        lower_text = p_text.lower()
                        if not any(skip in lower_text for skip in ['subscribe', 'follow us', 'share this']):
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

    # ========== ПАРСЕР GLOBAL RESEARCH ==========
    def parse_globalresearch(self, url, source_name):
        try:
            logger.info(f"🌐 Парсинг {source_name}: {url}")
            headers = {'User-Agent': 'Mozilla/5.0'}
            response = requests.get(url, headers=headers, timeout=15)
            if response.status_code != 200:
                return None

            soup = BeautifulSoup(response.text, 'html.parser')

            # Заголовок
            title = "Без заголовка"
            title_elem = soup.find('h1') or soup.find('title')
            if title_elem:
                title = self.clean_text(title_elem.get_text())

            # Изображение
            main_image = None
            img_elem = soup.find('img', class_=re.compile(r'featured|wp-post-image'))
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

            # Текст
            article_text = ""
            text_container = soup.find('div', class_=re.compile(r'entry-content|post-content'))
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
            logger.error(f"❌ Ошибка парсинга Global Research: {e}")
            return None

    # ========== ЗАГРУЗКА НОВОСТЕЙ ==========
    async def fetch_from_apnews(self):
        """Получение свежих статей с AP News"""
        try:
            logger.info("🔄 AP News (прямой парсинг)")

            articles = await asyncio.get_event_loop().run_in_executor(
                None, self.get_apnews_articles
            )

            if not articles:
                logger.warning("⚠️ Не удалось получить список статей с AP News")
                return []

            news_items = []
            for article in articles[:2]:  # Берем первые 2 статьи
                url = article['url']
                title = article['title']

                if url in self.sent_links:
                    logger.info(f"⏭️ УЖЕ БЫЛО: {title[:50]}...")
                    continue

                logger.info(f"🔍 НОВАЯ статья: {title[:50]}...")

                article_data = await asyncio.get_event_loop().run_in_executor(
                    None, self.parse_apnews_article, url, "AP News"
                )

                if article_data:
                    news_items.append({
                        'source': 'AP News',
                        'title': article_data['title'],
                        'content': article_data['content'],
                        'link': url,
                        'main_image': article_data.get('main_image'),
                        'priority': 1
                    })
                    logger.info(f"✅ Статья добавлена в очередь")
                else:
                    logger.warning(f"❌ Не удалось спарсить статью")

                await asyncio.sleep(random.randint(3, 8))

            return news_items

        except Exception as e:
            logger.error(f"❌ Ошибка в fetch_from_apnews: {e}")
            return []

    async def fetch_from_rss(self, feed_config):
        try:
            feed_url = feed_config['url']
            source_name = feed_config['name']
            parser_name = feed_config.get('parser', 'infobrics')
            priority = feed_config.get('priority', 5)
            logger.info(f"🔄 {source_name} (RSS)")

            # Выбираем функцию парсера
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
            for entry in feed.entries[:2]:  # Берем первые 2 статьи из RSS
                link = entry.get('link', '')
                title = entry.get('title', 'Без заголовка')

                if link in self.sent_links:
                    logger.info(f"⏭️ УЖЕ БЫЛО: {title[:50]}...")
                    continue

                logger.info(f"🔍 НОВАЯ статья: {title[:50]}...")

                loop = asyncio.get_event_loop()
                article_data = await loop.run_in_executor(
                    None, parser_func, link, source_name
                )

                if article_data:
                    news_items.append({
                        'source': source_name,
                        'title': article_data['title'],
                        'content': article_data['content'],
                        'link': link,
                        'main_image': article_data.get('main_image'),
                        'priority': priority
                    })
                    logger.info(f"✅ Статья добавлена в очередь")
                else:
                    logger.warning(f"❌ Не удалось спарсить статью")

                await asyncio.sleep(random.randint(3, 8))

            return news_items

        except Exception as e:
            logger.error(f"❌ Ошибка в fetch_from_rss: {e}")
            return []

    async def fetch_all_news(self):
        """Сбор новостей из ВСЕХ источников"""
        all_news = []

        for feed in ALL_FEEDS:
            if not feed['enabled']:
                continue

            if feed.get('type') == 'html_apnews':
                news = await self.fetch_from_apnews()
            else:
                news = await self.fetch_from_rss(feed)

            all_news.extend(news)
            await asyncio.sleep(random.randint(5, 10))  # Пауза между источниками

        # Сортируем по приоритету (сначала важные)
        all_news.sort(key=lambda x: x.get('priority', 5))

        logger.info(f"📊 ВСЕГО НОВЫХ СТАТЕЙ: {len(all_news)}")
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

    # ========== ПЕРЕВОД ==========
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

    # ========== УМНАЯ ОБРЕЗКА ==========
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

        if result_sentences:
            return ' '.join(result_sentences)
        else:
            return paragraph[:max_length]

    def build_caption_with_smart_truncation(self, title, paragraphs, max_length=TELEGRAM_MAX_CAPTION):
        title_part = f"<b>{title}</b>"
        current_text = title_part
        current_length = len(title_part)
        available_for_text = max_length - 5

        if current_length >= available_for_text:
            logger.warning("⚠️ Заголовок слишком длинный, обрезаю...")
            title_truncated = title[:50] + "..."
            title_part = f"<b>{title_truncated}</b>"
            current_text = title_part
            current_length = len(title_part)

        added_any_text = False

        for i, para in enumerate(paragraphs):
            if i == 0 and not added_any_text:
                separator = "\n\n"
            else:
                separator = "\n\n"

            if i == 0:
                para_with_sep = separator + para
                para_length = len(para_with_sep)

                if current_length + para_length <= available_for_text:
                    current_text += para_with_sep
                    current_length += para_length
                    added_any_text = True
                    logger.info(f"✅ Первый абзац поместился целиком ({len(para)} символов)")
                else:
                    max_para_length = available_for_text - current_length - len(separator)
                    truncated_para = self.truncate_first_paragraph_by_sentences(para, max_para_length)
                    if truncated_para and len(truncated_para) > 0:
                        current_text += separator + truncated_para
                        current_length += len(separator) + len(truncated_para)
                        added_any_text = True
                        logger.info(f"✂️ Первый абзац обрезан по предложениям ({len(truncated_para)} символов)")
            else:
                para_with_sep = separator + para
                para_length = len(para_with_sep)

                if current_length + para_length <= available_for_text:
                    current_text += para_with_sep
                    current_length += para_length
                    added_any_text = True
                    logger.info(f"✅ Добавлен абзац {i+1} целиком")
                else:
                    logger.info(f"⏹️ Останов на абзаце {i+1}, дальше не влезает")
                    break

        final_caption = current_text
        logger.info(f"📏 Итоговая длина: {len(final_caption)}/{max_length}")
        return final_caption

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
                paragraphs=paragraphs,
                max_length=TELEGRAM_MAX_CAPTION
            )

            return {
                'image_path': image_path,
                'caption': final_caption
            }

        except Exception as e:
            logger.error(f"❌ Ошибка создания поста: {e}")
            import traceback
            traceback.print_exc()
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
                logger.info("✅ Пост опубликован")
                return True
            else:
                await self.bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=post_data['caption'],
                    parse_mode='HTML'
                )
                return True

        except TelegramError as e:
            if "Too Many Requests" in str(e):
                logger.warning("⚠️ Лимит Telegram, жду 1 час...")
                await asyncio.sleep(3600)
            elif "Can't parse entities" in str(e):
                logger.warning("⚠️ Ошибка HTML, отправляю без форматирования")
                plain_text = re.sub(r'<[^>]+>', '', post_data['caption'])
                await self.bot.send_message(chat_id=CHANNEL_ID, text=plain_text)
                return True
            else:
                logger.error(f"❌ Ошибка Telegram: {e}")
                return False

    async def check_and_publish(self):
        """Основной цикл - запускается каждый час"""
        logger.info("="*70)
        logger.info(f"🕐 ЕЖЕЧАСНАЯ ПРОВЕРКА: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("="*70)

        if not self.can_post_now():
            logger.info("⏳ Нельзя публиковать сейчас (ночь или лимиты)")
            return

        # Собираем новости из ВСЕХ источников
        news_items = await self.fetch_all_news()
        
        if not news_items:
            logger.info("📭 НОВЫХ СТАТЕЙ НЕТ НИ В ОДНОМ ИСТОЧНИКЕ")
            return

        # Публикуем ОДНУ статью (самую свежую/приоритетную)
        item = news_items[0]
        logger.info(f"\n📝 ПУБЛИКАЦИЯ: {item['title'][:70]}...")
        logger.info(f"   Источник: {item['source']}")
        logger.info(f"   Ссылка: {item['link']}")

        post_data = await self.create_single_post(item)

        if post_data:
            success = await self.publish_post(post_data)
            if success:
                self.sent_links.add(item['link'])
                self.save_sent_links()
                self.log_post(item['link'], item['title'])
                logger.info(f"✅ Статья опубликована. Всего в базе: {len(self.sent_links)} ссылок")
                
                # Если остались еще статьи, они будут опубликованы в следующие часы
                if len(news_items) > 1:
                    logger.info(f"📦 В очереди еще {len(news_items)-1} статей (будут опубликованы позже)")
            else:
                logger.error(f"❌ Не удалось опубликовать статью")
        else:
            logger.error(f"❌ Не удалось создать пост")

        logger.info(f"\n⏰ Следующая проверка через 1 час")

    async def start(self):
        """Запуск бота"""
        logger.info("="*80)
        logger.info("🚀 NEWS BOT 9.0 - ЕЖЕЧАСНАЯ ПУБЛИКАЦИЯ")
        logger.info("="*80)
        logger.info(f"📢 Канал: {CHANNEL_ID}")
        logger.info(f"⏱ Режим: публикация КАЖДЫЙ ЧАС")
        logger.info(f"🛡️ Лимит: {MAX_POSTS_PER_DAY} постов/день")
        logger.info(f"📏 Лимит подписи: {TELEGRAM_MAX_CAPTION}")
        logger.info(f"🌍 Часовой пояс: UTC+{TIMEZONE_OFFSET}")
        logger.info(f"🔒 Защита от дублей: {len(self.sent_links)} ссылок в базе")
        logger.info("="*80)

        # Показываем все источники
        logger.info("📡 ИСТОЧНИКИ (КАЖДЫЙ ДЕНЬ):")
        for feed in ALL_FEEDS:
            if feed['enabled']:
                logger.info(f"   - {feed['name']} (приоритет: {feed.get('priority', 5)})")
        logger.info("="*80)

        try:
            me = await self.bot.get_me()
            logger.info(f"✅ Бот @{me.username} авторизован")
        except Exception as e:
            logger.error(f"❌ Ошибка подключения бота: {e}")
            return

        # Первая проверка сразу при запуске
        logger.info("🚀 ЗАПУСК ПЕРВОЙ ПРОВЕРКИ")
        await self.check_and_publish()

        # Планировщик для ежечасных проверок
        self.scheduler.add_job(
            self.check_and_publish,
            'interval',
            seconds=CHECK_INTERVAL,
            id='hourly_checker',
            next_run_time=datetime.now() + timedelta(seconds=CHECK_INTERVAL)
        )
        self.scheduler.start()
        logger.info(f"✅ Планировщик запущен. Следующая проверка через 1 час")

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
