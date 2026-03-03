"""
🤖 Telegram News Bot - Версия 9.1
ХАОТИЧНАЯ ПУБЛИКАЦИЯ + ИСПРАВЛЕННЫЙ ПАРСИНГ AP NEWS

- Публикация: не чаще 2 постов за 35 мин, не реже 1 поста в 2 часа
- Удаление всех мета-данных
- Правильный парсинг AP News (только основной текст)
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
import hashlib

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

# ХАОТИЧНЫЙ РЕЖИМ
MIN_POST_INTERVAL = 35 * 60  # 35 минут в секундах (максимальная частота)
MAX_POST_INTERVAL = 2 * 60 * 60  # 2 часа в секундах (минимальная частота)

# Проверка новых статей - каждые 30 минут (чтобы не пропустить)
CHECK_INTERVAL = 30 * 60  # 30 минут

MAX_POSTS_PER_DAY = 24  # Лимит постов в день (максимум)
TIMEZONE_OFFSET = 7  # Смещение для UTC+7

# ============================================================
# ВСЕ ИСТОЧНИКИ
# ============================================================
ALL_FEEDS = [
    # InfoBrics
    {
        'name': 'InfoBrics',
        'url': 'https://infobrics.org/rss/en',
        'enabled': True,
        'parser': 'infobrics',
        'type': 'rss',
        'priority': 1
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
    # AP News (исправленный парсинг)
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
TELEGRAM_MAX_CAPTION = 1024

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
        self.next_post_time = None
        
        # Статистика
        logger.info(f"📊 Загружено {len(self.sent_links)} ранее опубликованных ссылок")
        logger.info(f"📊 Загружено {len(self.posts_log)} записей в логе постов")
        logger.info(f"⏱ ХАОТИЧНЫЙ РЕЖИМ: не чаще 2 постов за 35 мин, не реже 1 поста в 2 часа")

    # ========== РАБОТА С JSON ==========
    def load_json(self, filename):
        try:
            with open(filename, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки {filename}: {e}")
            return []

    def load_sent_links(self):
        try:
            if os.path.exists(SENT_LINKS_FILE):
                with open(SENT_LINKS_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        return set(data)
                    else:
                        return set()
            else:
                return set()
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки {SENT_LINKS_FILE}: {e}")
            return set()

    def save_sent_links(self):
        try:
            with open(SENT_LINKS_FILE, 'w', encoding='utf-8') as f:
                json.dump(list(self.sent_links), f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"❌ Ошибка сохранения {SENT_LINKS_FILE}: {e}")

    def save_json(self, filename, data):
        try:
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"❌ Ошибка сохранения {filename}: {e}")

    # ========== УДАЛЕНИЕ МЕТА-ДАННЫХ ==========
    def remove_metadata(self, text):
        """
        Удаляет все возможные мета-данные из текста
        - Временные метки
        - Информацию об авторе
        - Служебные надписи
        - Даты публикации
        """
        if not text:
            return text
        
        # Удаляем временные метки (например: "2 hours ago", "3 min read", "Updated 5:30 PM")
        text = re.sub(r'\d+\s*(hour|min|sec|day|minute|second)s?\s+ago', '', text, flags=re.IGNORECASE)
        text = re.sub(r'Updated\s*:?\s*[\d:APM\s-]+', '', text, flags=re.IGNORECASE)
        text = re.sub(r'Published\s*:?\s*[\d:APM\s-]+', '', text, flags=re.IGNORECASE)
        
        # Удаляем информацию об авторе
        text = re.sub(r'By\s+[\w\s,]+\n', '', text, flags=re.IGNORECASE)
        text = re.sub(r'Written\s+by\s+[\w\s,]+\n', '', text, flags=re.IGNORECASE)
        
        # Удаляем служебные надписи
        service_phrases = [
            r'Read\s+more',
            r'Click\s+here',
            r'Subscribe\s+to',
            r'Sign\s+up',
            r'Newsletter',
            r'Daily\s+Brief',
            r'Morning\s+Briefing',
            r'Evening\s+Update',
            r'Follow\s+us',
            r'Share\s+this',
            r'Comments',
            r'Advertisement',
            r'Photo\s+by',
            r'Image\s+credit'
        ]
        
        for phrase in service_phrases:
            text = re.sub(phrase, '', text, flags=re.IGNORECASE)
        
        # Удаляем множественные переносы строк
        text = re.sub(r'\n{3,}', '\n\n', text)
        
        return text.strip()

    # ========== ПРОВЕРКА ХАОТИЧНЫХ ЛИМИТОВ ==========
    def can_post_now(self):
        """
        Проверка хаотичных лимитов:
        - Не чаще 2 постов за 35 минут
        - Не реже 1 поста в 2 часа
        """
        local_hour = (datetime.now().hour + TIMEZONE_OFFSET) % 24
        # Ночной режим (можно отключить если нужно)
        if 23 <= local_hour or local_hour < 7:
            logger.info(f"🌙 Ночное время ({local_hour}:00), пропускаю")
            return False

        # Проверка дневного лимита
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

        # Проверка "не чаще 2 постов за 35 минут"
        if len(last_posts_times) >= 2:
            # Сортируем по времени (от новых к старым)
            last_posts_times.sort(reverse=True)
            
            # Берем два последних поста
            if len(last_posts_times) >= 2:
                time_diff = last_posts_times[0] - last_posts_times[1]
                if time_diff < timedelta(minutes=35):
                    next_allowed = last_posts_times[0] + timedelta(minutes=35)
                    wait_minutes = (next_allowed - datetime.now()).total_seconds() / 60
                    if wait_minutes > 0:
                        logger.info(f"⏳ Лимит частоты: следующий пост не ранее чем через {wait_minutes:.0f} минут")
                        return False

        # Проверка "не реже 1 поста в 2 часа"
        if last_posts_times:
            last_post = max(last_posts_times)
            time_since_last = datetime.now() - last_post
            if time_since_last > timedelta(hours=2):
                logger.info(f"✅ Пора публиковать (прошло {time_since_last.seconds//3600}ч {time_since_last.seconds%3600//60}м)")
                return True

        # Если последний пост был недавно, но лимиты не нарушены
        return True

    def get_next_post_delay(self):
        """
        Возвращает случайную задержку до следующего поста
        от MIN_POST_INTERVAL до MAX_POST_INTERVAL
        """
        # Базовый случайный интервал
        delay = random.randint(MIN_POST_INTERVAL, MAX_POSTS_PER_DAY * 60)
        
        # Но не больше MAX_POST_INTERVAL
        delay = min(delay, MAX_POST_INTERVAL)
        
        # И не меньше MIN_POST_INTERVAL
        delay = max(delay, MIN_POST_INTERVAL)
        
        # Добавляем случайную вариацию ±15%
        variation = random.uniform(0.85, 1.15)
        delay = int(delay * variation)
        
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
        # Удаляем мета-данные
        text = self.remove_metadata(text)
        return text.strip()

    def escape_html_for_telegram(self, text):
        if not text:
            return ""
        text = text.replace('&', '&amp;')
        text = text.replace('<', '&lt;')
        text = text.replace('>', '&gt;')
        return text

    # ========== ИСПРАВЛЕННЫЙ ПАРСЕР AP NEWS ==========
    def get_apnews_articles(self):
        """Парсит главную страницу AP News"""
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

            # Ищем ссылки на статьи (обычно в блоках с class="Card")
            for card in soup.find_all(['div', 'article'], class_=re.compile(r'Card|Article|FeedCard', re.I)):
                link = card.find('a', href=True)
                if not link:
                    continue
                    
                href = link['href']
                
                # Формируем полный URL
                full_url = None
                if href.startswith('https://apnews.com/'):
                    full_url = href
                elif href.startswith('/'):
                    full_url = 'https://apnews.com' + href
                elif 'apnews.com' in href:
                    full_url = href
                else:
                    continue

                # Получаем заголовок
                title_elem = card.find(['h1', 'h2', 'h3', 'h4'])
                if title_elem:
                    title = title_elem.get_text(strip=True)
                else:
                    title = link.get_text(strip=True)

                if not title or len(title) < 15:
                    continue

                title = re.sub(r'\s+', ' ', title).strip()

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

            logger.info(f"✅ Найдено {len(unique_articles)} статей")
            return unique_articles[:5]

        except Exception as e:
            logger.error(f"❌ Ошибка парсинга AP News: {e}")
            return []

    def parse_apnews_article(self, url, source_name):
        """ИСПРАВЛЕННЫЙ парсинг статьи AP News - берет ТОЛЬКО основной текст"""
        try:
            logger.info(f"🌐 Парсинг статьи AP News: {url}")
            headers = {'User-Agent': 'Mozilla/5.0'}
            response = requests.get(url, headers=headers, timeout=15)
            if response.status_code != 200:
                return None

            soup = BeautifulSoup(response.text, 'html.parser')

            # 1. ЗАГОЛОВОК
            title = "Без заголовка"
            
            # Ищем в meta og:title (самый надежный)
            meta_title = soup.find('meta', property='og:title')
            if meta_title and meta_title.get('content'):
                title = meta_title['content']
            else:
                # Ищем h1
                h1 = soup.find('h1')
                if h1:
                    title = h1.get_text(strip=True)
            
            # Чистим заголовок
            title = re.sub(r'\s*\|.*AP\s*News.*$', '', title, flags=re.IGNORECASE)
            title = re.sub(r'\s*-\s*AP\s*News.*$', '', title, flags=re.IGNORECASE)

            # 2. ИЗОБРАЖЕНИЕ
            main_image = None
            
            # Сначала ищем в meta
            meta_img = soup.find('meta', property='og:image')
            if meta_img and meta_img.get('content'):
                main_image = meta_img['content']
            else:
                # Ищем первое значимое изображение в статье
                article_body = soup.find(['article', 'main', 'div'], class_=re.compile(r'Article|story-body|content', re.I))
                if article_body:
                    img = article_body.find('img', src=re.compile(r'\.(jpg|jpeg|png|webp)', re.I))
                    if img and img.get('src'):
                        img_src = img['src']
                        if img_src.startswith('/'):
                            main_image = 'https://apnews.com' + img_src
                        elif img_src.startswith('http'):
                            main_image = img_src

            # 3. ТЕКСТ СТАТЬИ (САМОЕ ВАЖНОЕ)
            article_text = ""
            
            # Находим ОСНОВНОЙ контейнер со статьей
            main_container = None
            
            # Пробуем разные селекторы для основного текста
            possible_selectors = [
                # Специфичные для AP News
                ('div', {'class_': re.compile(r'Article', re.I)}),
                ('div', {'class_': re.compile(r'story-body', re.I)}),
                ('div', {'class_': re.compile(r'RichTextStoryBody', re.I)}),
                ('div', {'class_': re.compile(r'content', re.I)}),
                ('article', {}),
                ('main', {})
            ]
            
            for tag, attrs in possible_selectors:
                container = soup.find(tag, **attrs)
                if container:
                    main_container = container
                    break
            
            if not main_container:
                # Если не нашли, берем body
                main_container = soup.body
            
            if main_container:
                # УДАЛЯЕМ все боковые панели, навигацию и рекламу
                for unwanted in main_container.find_all(['aside', 'nav', 'header', 'footer', 'script', 'style']):
                    unwanted.decompose()
                
                # Удаляем элементы с классами, содержащими "sidebar", "newsletter", "related"
                for elem in main_container.find_all(class_=re.compile(r'sidebar|newsletter|related|recommended|ad|promo', re.I)):
                    elem.decompose()
                
                # Теперь собираем ТОЛЬКО параграфы
                paragraphs = []
                
                # Ищем все параграфы в основном контейнере
                for p in main_container.find_all('p'):
                    p_text = p.get_text(strip=True)
                    
                    # Фильтруем:
                    # - Длина больше 20 символов
                    # - Не содержит служебных слов
                    # - Не похоже на мета-данные
                    if p_text and len(p_text) > 20:
                        lower_text = p_text.lower()
                        
                        # Пропускаем если похоже на служебный текст
                        skip_phrases = [
                            'subscribe', 'newsletter', 'sign up', 'follow us',
                            'share this', 'read more', 'click here', 'comments',
                            'advertisement', 'photo by', 'image credit', 'related',
                            'you might also like', 'recommended for you',
                            # Добавляем то, что видно на скриншотах
                            'immigration', 'weather', 'education', 'transportation',
                            'abortion', 'lgbtq', 'notable deaths', 'sections',
                            'morning wire', 'afternoon wire', 'newsletters'
                        ]
                        
                        skip = False
                        for phrase in skip_phrases:
                            if phrase in lower_text:
                                skip = True
                                break
                        
                        if not skip:
                            paragraphs.append(p_text)
                
                if paragraphs:
                    article_text = '\n\n'.join(paragraphs)
                    logger.info(f"✅ Найдено {len(paragraphs)} параграфов основного текста")
            
            # Если не нашли параграфы, пробуем запасной метод
            if len(article_text) < 200:
                logger.warning("⚠️ Параграфы не найдены, пробую запасной метод")
                
                # Ищем div с текстом (без боковых панелей)
                content_divs = main_container.find_all(['div', 'section'], 
                    class_=re.compile(r'content|text|body', re.I))
                
                for div in content_divs[:3]:  # Проверяем первые 3
                    div_text = div.get_text(separator='\n', strip=True)
                    if len(div_text) > 500 and 'newsletter' not in div_text.lower():
                        # Разбиваем на параграфы
                        potential_paragraphs = [p.strip() for p in div_text.split('\n') if len(p.strip()) > 50]
                        if potential_paragraphs:
                            article_text = '\n\n'.join(potential_paragraphs[:10])
                            logger.info(f"✅ Текст найден в div: {len(article_text)} символов")
                            break

            # Финальная проверка
            if len(article_text) < 200:
                logger.warning(f"⚠️ Текст слишком короткий ({len(article_text)} символов)")
                return None

            # Удаляем мета-данные
            article_text = self.remove_metadata(article_text)

            logger.info(f"✅ Успешно спарсено: {len(article_text)} символов основного текста")
            return {
                'title': title,
                'content': article_text,
                'main_image': main_image
            }

        except Exception as e:
            logger.error(f"❌ Ошибка парсинга статьи AP News: {e}")
            import traceback
            traceback.print_exc()
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
        try:
            logger.info("🔄 AP News (прямой парсинг)")
            articles = await asyncio.get_event_loop().run_in_executor(
                None, self.get_apnews_articles
            )

            if not articles:
                logger.warning("⚠️ Не удалось получить список статей")
                return []

            news_items = []
            for article in articles[:3]:  # Берем первые 3 статьи
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
                    logger.info(f"✅ Статья добавлена")
                else:
                    logger.warning(f"❌ Не удалось спарсить")

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
            for entry in feed.entries[:3]:  # Берем первые 3 статьи
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
                    logger.info(f"✅ Статья добавлена")
                else:
                    logger.warning(f"❌ Не удалось спарсить")

                await asyncio.sleep(random.randint(3, 8))

            return news_items

        except Exception as e:
            logger.error(f"❌ Ошибка: {e}")
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
            await asyncio.sleep(random.randint(5, 10))

        # Сортируем по приоритету и случайно
        all_news.sort(key=lambda x: (x.get('priority', 5), random.random()))

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
                else:
                    max_para_length = available_for_text - current_length - len(separator)
                    truncated_para = self.truncate_first_paragraph_by_sentences(para, max_para_length)
                    if truncated_para and len(truncated_para) > 0:
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
                plain_text = re.sub(r'<[^>]+>', '', post_data['caption'])
                await self.bot.send_message(chat_id=CHANNEL_ID, text=plain_text)
                return True
            else:
                logger.error(f"❌ Ошибка Telegram: {e}")
                return False

    async def check_and_publish(self):
        """Проверка наличия новых статей"""
        logger.info("="*60)
        logger.info(f"🔍 ПРОВЕРКА: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("="*60)

        # Собираем новые статьи
        news_items = await self.fetch_all_news()
        
        if not news_items:
            logger.info("📭 НОВЫХ СТАТЕЙ НЕТ")
            return

        # Сохраняем в очередь (в памяти)
        if not hasattr(self, 'post_queue'):
            self.post_queue = []
        
        self.post_queue.extend(news_items)
        logger.info(f"📦 В очереди {len(self.post_queue)} статей")

        # Пытаемся опубликовать, если можно
        await self.try_publish_from_queue()

    async def try_publish_from_queue(self):
        """Пытается опубликовать одну статью из очереди"""
        if not hasattr(self, 'post_queue') or not self.post_queue:
            return

        if not self.can_post_now():
            # Если сейчас нельзя публиковать, планируем следующую попытку
            next_try = self.get_next_post_delay()
            logger.info(f"⏰ Сейчас нельзя публиковать. Следующая попытка через {next_try//60} минут")
            
            # Планируем следующую попытку
            asyncio.create_task(self.schedule_next_try(next_try))
            return

        # Берем первую статью из очереди
        item = self.post_queue.pop(0)
        
        logger.info(f"\n📝 ПУБЛИКАЦИЯ: {item['title'][:70]}...")
        logger.info(f"   Источник: {item['source']}")
        logger.info(f"   Осталось в очереди: {len(self.post_queue)}")

        post_data = await self.create_single_post(item)

        if post_data:
            success = await self.publish_post(post_data)
            if success:
                self.sent_links.add(item['link'])
                self.save_sent_links()
                self.log_post(item['link'], item['title'])
                logger.info(f"✅ Статья опубликована")

                # Планируем следующую публикацию
                next_delay = self.get_next_post_delay()
                logger.info(f"⏰ Следующая публикация через {next_delay//60} минут")
                
                asyncio.create_task(self.schedule_next_try(next_delay))
            else:
                logger.error(f"❌ Не удалось опубликовать")
                # Возвращаем в очередь для повторной попытки позже
                self.post_queue.insert(0, item)

    async def schedule_next_try(self, delay):
        """Планирует следующую попытку публикации"""
        await asyncio.sleep(delay)
        await self.try_publish_from_queue()

    async def start(self):
        """Запуск бота"""
        logger.info("="*80)
        logger.info("🚀 NEWS BOT 9.1 - ХАОТИЧНАЯ ПУБЛИКАЦИЯ + ЧИСТЫЙ AP NEWS")
        logger.info("="*80)
        logger.info(f"📢 Канал: {CHANNEL_ID}")
        logger.info(f"⏱ ХАОТИЧНЫЙ РЕЖИМ:")
        logger.info(f"   - Не чаще 2 постов за 35 минут")
        logger.info(f"   - Не реже 1 поста в 2 часа")
        logger.info(f"🛡️ Лимит: {MAX_POSTS_PER_DAY} постов/день")
        logger.info(f"🌍 Часовой пояс: UTC+{TIMEZONE_OFFSET}")
        logger.info(f"🔒 Защита от дублей: {len(self.sent_links)} ссылок")
        logger.info(f"🧹 Удаление мета-данных: ВКЛЮЧЕНО")
        logger.info("="*80)

        # Показываем все источники
        logger.info("📡 ИСТОЧНИКИ (КАЖДЫЙ ДЕНЬ):")
        for feed in ALL_FEEDS:
            if feed['enabled']:
                logger.info(f"   - {feed['name']}")
        logger.info("="*80)

        try:
            me = await self.bot.get_me()
            logger.info(f"✅ Бот @{me.username} авторизован")
        except Exception as e:
            logger.error(f"❌ Ошибка подключения бота: {e}")
            return

        # Инициализируем очередь
        self.post_queue = []

        # Первая проверка сразу при запуске
        logger.info("🚀 ЗАПУСК ПЕРВОЙ ПРОВЕРКИ")
        await self.check_and_publish()

        # Планировщик для регулярных проверок новых статей
        self.scheduler.add_job(
            self.check_and_publish,
            'interval',
            seconds=CHECK_INTERVAL,
            id='news_checker'
        )
        self.scheduler.start()
        logger.info(f"✅ Планировщик запущен. Проверка новых статей каждые {CHECK_INTERVAL//60} минут")

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
