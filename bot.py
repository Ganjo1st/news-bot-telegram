"""
🤖 Telegram News Bot - Версия 8.14
С УЛУЧШЕННОЙ ФИЛЬТРАЦИЕЙ ТЕКСТА
- UTC+7 часовой пояс
- Без указания источника
- Умная обрезка текста
- Очистка от служебной информации
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

# Конфигурация
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', '8767446234:AAGRz1sJfDtV321CpUBdI2sqGVDcWryGqcY')
CHANNEL_ID = os.getenv('CHANNEL_ID', '@Novikon_news')
CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL', '7200'))  # 2 часа
MIN_POST_INTERVAL = 600  # Базовая пауза 10 минут
MAX_POSTS_PER_DAY = 20   # Лимит постов в день
TIMEZONE_OFFSET = 7      # Смещение для UTC+7

# Основные источники (будние дни)
WEEKDAY_FEEDS = [
    {
        'name': 'InfoBrics',
        'url': 'https://infobrics.org/rss/en',
        'enabled': True,
        'parser': 'infobrics',
        'type': 'rss'
    },
    {
        'name': 'Global Research',
        'url': 'https://www.globalresearch.ca/feed',
        'enabled': True,
        'parser': 'globalresearch',
        'type': 'rss'
    }
]

# Источники для выходных (прямой парсинг HTML)
WEEKEND_FEEDS = [
    {
        'name': 'AP Top News',
        'url': 'https://apnews.com/',
        'enabled': True,
        'parser': 'apnews_list',
        'type': 'html'
    },
    {
        'name': 'AP World News',
        'url': 'https://apnews.com/world-news',
        'enabled': True,
        'parser': 'apnews_list',
        'type': 'html'
    }
]

# Определяем, какие источники использовать сегодня
def get_today_feeds():
    """Возвращает список источников в зависимости от дня недели"""
    local_date = datetime.now() + timedelta(hours=TIMEZONE_OFFSET)
    weekday = local_date.weekday()  # 0-6: понедельник-воскресенье
    
    # Выходные: суббота (5) и воскресенье (6)
    if weekday >= 5:
        logger.info(f"📅 Сегодня выходной (день {weekday}), использую AP News (прямой парсинг)")
        return WEEKEND_FEEDS
    else:
        logger.info(f"📅 Сегодня будний день (день {weekday}), использую основные источники")
        return WEEKDAY_FEEDS

SENT_LINKS_FILE = 'sent_links.json'
POSTS_LOG_FILE = 'posts_log.json'

# Константы Telegram
TELEGRAM_MAX_CAPTION = 1024  # Лимит подписи к фото

class NewsBot:
    def __init__(self):
        for file in [SENT_LINKS_FILE, POSTS_LOG_FILE]:
            if not os.path.exists(file):
                with open(file, 'w', encoding='utf-8') as f:
                    json.dump([], f)
                logger.info(f"📁 Создан файл {file}")
        
        self.bot = Bot(token=TELEGRAM_TOKEN)
        self.translator = GoogleTranslator(source='en', target='ru')
        self.scheduler = AsyncIOScheduler()
        self.sent_links = self.load_json(SENT_LINKS_FILE)
        self.posts_log = self.load_json(POSTS_LOG_FILE)
        self.session = None
        
        logger.info(f"📊 Загружено {len(self.sent_links)} ранее опубликованных ссылок")
    
    def load_json(self, filename):
        try:
            with open(filename, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return set(data) if filename == SENT_LINKS_FILE else data
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки {filename}: {e}")
            return set() if filename == SENT_LINKS_FILE else []
    
    def save_json(self, filename, data):
        try:
            with open(filename, 'w', encoding='utf-8') as f:
                if isinstance(data, set):
                    json.dump(list(data), f, ensure_ascii=False, indent=2)
                else:
                    json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"❌ Ошибка сохранения {filename}: {e}")
    
    def can_post_now(self):
        """Проверка всех лимитов с учетом часового пояса UTC+7"""
        local_hour = (datetime.now().hour + TIMEZONE_OFFSET) % 24
        
        # Не публикуем с 23:00 до 07:00 по местному времени
        if 23 <= local_hour or local_hour < 7:
            logger.info(f"🌙 Ночное время по местному ({local_hour}:00), пропускаю")
            return False
        
        if self.posts_log:
            last_post = max(self.posts_log, key=lambda x: x['time'])
            last_time = datetime.fromisoformat(last_post['time'])
            time_diff = datetime.now() - last_time
            if time_diff < timedelta(seconds=MIN_POST_INTERVAL):
                logger.info(f"⏳ С последнего поста прошло {time_diff.seconds}с, нужно ждать {MIN_POST_INTERVAL}с")
                return False
        
        today = datetime.now().date()
        today_posts = [p for p in self.posts_log 
                      if datetime.fromisoformat(p['time']).date() == today]
        
        if len(today_posts) >= MAX_POSTS_PER_DAY:
            logger.info(f"⏳ Достигнут дневной лимит {MAX_POSTS_PER_DAY} постов")
            return False
        
        logger.debug(f"✅ Можно публиковать (сегодня {len(today_posts)}/{MAX_POSTS_PER_DAY})")
        return True
    
    def log_post(self, link, title):
        self.posts_log.append({
            'link': link,
            'title': title[:50],
            'time': datetime.now().isoformat()
        })
        if len(self.posts_log) > 100:
            self.posts_log = self.posts_log[-100:]
        self.save_json(POSTS_LOG_FILE, self.posts_log)
    
    async def get_session(self):
        if not self.session:
            self.session = aiohttp.ClientSession()
        return self.session
    
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
    
    def is_junk_text(self, text):
        """Проверяет, является ли текст служебным (не частью статьи)"""
        text_lower = text.lower()
        
        # Список маркеров служебного текста
        junk_markers = [
            'subscribe',
            'newsletter',
            'click here',
            'read more',
            'follow us',
            'sign up',
            'share this',
            'facebook',
            'twitter',
            'linkedin',
            'reddit',
            'pinterest',
            'whatsapp',
            'telegram',
            'email',
            'print',
            'copy link',
            'link copied',
            'edited by',
            'editors:',
            'editor:',
            'photographer:',
            'video by',
            'photos by',
            'associated press',
            'ap news',
            'add ap news',
            'add to google',
            'preferred source',
            'see more',
            'view comments',
            'comments:',
            '©',
            'copyright',
            'all rights reserved',
            'terms of use',
            'privacy policy',
            'help',
            'feedback',
            'contact us',
            'about us',
            'advertise',
            'careers',
            'press releases'
        ]
        
        for marker in junk_markers:
            if marker in text_lower:
                return True
        
        # Проверяем на очень короткий текст (менее 20 символов)
        if len(text) < 20:
            return True
        
        # Проверяем на наличие множества специальных символов (обычно в кнопках шаринга)
        special_chars = sum(1 for c in text if not c.isalnum() and not c.isspace())
        if len(text) > 0 and special_chars / len(text) > 0.3:  # >30% спецсимволов
            return True
        
        return False
    
    def clean_apnews_content(self, text):
        """Дополнительная очистка текста AP News от служебной информации"""
        # Удаляем строки с информацией о редакторах
        text = re.sub(r'(?i)edited by.*?(?:\n|$)', '', text)
        text = re.sub(r'(?i)editor:.*?(?:\n|$)', '', text)
        text = re.sub(r'(?i)photographer:.*?(?:\n|$)', '', text)
        text = re.sub(r'(?i)video by.*?(?:\n|$)', '', text)
        
        # Удаляем строки с призывами подписаться
        text = re.sub(r'(?i)add ap news.*?(?:\n|$)', '', text)
        text = re.sub(r'(?i)add to google.*?(?:\n|$)', '', text)
        text = re.sub(r'(?i)preferred source.*?(?:\n|$)', '', text)
        
        # Удаляем строки с кнопками шаринга
        text = re.sub(r'(?i)share this.*?(?:\n|$)', '', text)
        text = re.sub(r'(?i)share on.*?(?:\n|$)', '', text)
        
        # Удаляем строки с именами социальных сетей (если они отдельно)
        social_pattern = r'(?i)(?:facebook|twitter|linkedin|reddit|pinterest|whatsapp|telegram|email|print|cop(?:y|ied)).*?(?:\n|$)'
        text = re.sub(social_pattern, '', text)
        
        # Удаляем пустые строки, которые могли образоваться
        lines = text.split('\n')
        cleaned_lines = [line for line in lines if line.strip()]
        text = '\n'.join(cleaned_lines)
        
        return text.strip()
    
    def get_apnews_articles(self, url):
        """Парсит страницу AP News и возвращает список свежих статей"""
        try:
            logger.info(f"🌐 Парсинг AP News: {url}")
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
            }
            
            response = requests.get(url, headers=headers, timeout=15)
            if response.status_code != 200:
                logger.error(f"❌ Ошибка загрузки AP News: {response.status_code}")
                return []
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            articles = []
            
            # Поиск статей по различным селекторам
            selectors = [
                ('article', None),
                ('div', 'PagePromo'),
                ('div', 'PageList-items-item'),
                ('div', 'Card'),
                ('div', 'FeedCard'),
                ('a', None)
            ]
            
            for tag, class_name in selectors:
                if class_name:
                    elements = soup.find_all(tag, class_=re.compile(class_name))
                else:
                    elements = soup.find_all(tag)
                
                for elem in elements:
                    link = elem if tag == 'a' else elem.find('a', href=True)
                    if not link or not link.get('href'):
                        continue
                    
                    href = link['href']
                    
                    if not ('/article/' in href or 'apnews.com' in href):
                        continue
                    
                    if href.startswith('/'):
                        full_url = 'https://apnews.com' + href
                    elif href.startswith('https://apnews.com'):
                        full_url = href
                    else:
                        continue
                    
                    title = None
                    title_elem = elem.find(['h1', 'h2', 'h3', 'h4']) if tag != 'a' else elem
                    if title_elem:
                        title = self.clean_text(title_elem.get_text())
                    
                    if not title or len(title) < 15:
                        title = self.clean_text(link.get_text())
                    
                    if title and len(title) > 15 and not self.is_junk_text(title):
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
            
            logger.info(f"✅ Найдено {len(unique_articles)} статей на {url}")
            return unique_articles[:3]
            
        except Exception as e:
            logger.error(f"❌ Ошибка парсинга AP News: {e}")
            return []
    
    def parse_apnews_article(self, url, source_name):
        """Парсинг отдельной статьи AP News с улучшенной фильтрацией"""
        try:
            logger.info(f"🌐 Парсинг статьи AP News: {url}")
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            }
            
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
                title = re.sub(r'\s*\|\s*AP\s*News.*$', '', title)
                title = re.sub(r'\s*-\s*AP\s*News.*$', '', title)
            
            # Изображение
            main_image = None
            og_image = soup.find('meta', property='og:image')
            if og_image and og_image.get('content'):
                main_image = og_image['content']
            
            if not main_image:
                twitter_image = soup.find('meta', {'name': 'twitter:image'})
                if twitter_image and twitter_image.get('content'):
                    main_image = twitter_image['content']
            
            if not main_image:
                img = soup.find('img', class_=re.compile(r'image|photo|featured|Figure-image|LeadImage'))
                if img and img.get('src'):
                    img_src = img['src']
                    if img_src.startswith('//'):
                        main_image = 'https:' + img_src
                    elif img_src.startswith('/'):
                        main_image = 'https://apnews.com' + img_src
                    elif img_src.startswith('http'):
                        main_image = img_src
                    else:
                        main_image = 'https://apnews.com/' + img_src
            
            # Текст статьи
            article_text = ""
            text_container = None
            
            selectors = [
                'div.Article',
                'div.Article-content',
                'div.StoryBody',
                'div.story-body',
                'div.content',
                'article',
                'div.body-text',
                'div.body-copy',
                'div[class*="article-body"]',
                'div[class*="story-body"]'
            ]
            
            for selector in selectors:
                container = soup.select_one(selector)
                if container:
                    text_container = container
                    break
            
            if text_container:
                # Удаляем ненужные элементы
                for unwanted in text_container.find_all(['script', 'style', 'button', 'aside', 'figure', 'nav', 'footer', 'iframe', 'div[class*="share"]', 'div[class*="social"]']):
                    unwanted.decompose()
                
                # Собираем параграфы
                paragraphs = []
                for p in text_container.find_all('p'):
                    p_text = self.clean_text(p.get_text())
                    if p_text and len(p_text) > 20 and not self.is_junk_text(p_text):
                        paragraphs.append(p_text)
                
                if paragraphs:
                    article_text = '\n\n'.join(paragraphs)
            
            # Если не нашли структурированный текст или он слишком короткий
            if len(article_text) < 200:
                # Удаляем скрипты и стили
                for script in soup(['script', 'style', 'nav', 'footer', 'header']):
                    script.decompose()
                
                # Получаем весь текст
                all_text = self.clean_text(soup.get_text())
                
                # Разбиваем на строки и фильтруем
                lines = all_text.split('\n')
                filtered_lines = []
                
                for line in lines:
                    line = line.strip()
                    if line and len(line) > 20 and not self.is_junk_text(line):
                        filtered_lines.append(line)
                
                if filtered_lines:
                    article_text = '\n\n'.join(filtered_lines[:15])  # Берем первые 15 строк
            
            # Дополнительная очистка от служебной информации
            article_text = self.clean_apnews_content(article_text)
            
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
    
    def parse_infobrics(self, url, source_name):
        """Парсер для InfoBrics"""
        try:
            logger.info(f"🌐 Парсинг {source_name}: {url}")
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            
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
                    if p_text and len(p_text) > 15 and not self.is_junk_text(p_text):
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
    
    def parse_globalresearch(self, url, source_name):
        """Парсер для Global Research"""
        try:
            logger.info(f"🌐 Парсинг {source_name}: {url}")
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            
            response = requests.get(url, headers=headers, timeout=15)
            if response.status_code != 200:
                return None
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # Заголовок
            title = "Без заголовка"
            title_elem = soup.find('h1')
            if not title_elem:
                title_elem = soup.find('title')
            
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
                    if p_text and len(p_text) > 15 and not self.is_junk_text(p_text):
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
    
    async def fetch_from_apnews(self, feed_config):
        """Получение свежих статей с AP News"""
        try:
            url = feed_config['url']
            source_name = feed_config['name']
            
            logger.info(f"🔄 {source_name} (прямой парсинг)")
            
            articles = await asyncio.get_event_loop().run_in_executor(
                None, self.get_apnews_articles, url
            )
            
            if not articles:
                logger.warning(f"⚠️ Не удалось получить список статей с {url}")
                return []
            
            news_items = []
            for article in articles[:1]:
                article_url = article['url']
                article_title = article['title']
                
                if article_url in self.sent_links:
                    logger.info(f"⏭️ Уже опубликовано: {article_title[:50]}...")
                    continue
                
                logger.info(f"🔍 Новая статья: {article_title[:50]}...")
                
                article_data = await asyncio.get_event_loop().run_in_executor(
                    None, self.parse_apnews_article, article_url, source_name
                )
                
                if article_data:
                    news_items.append({
                        'source': source_name,
                        'title': article_data['title'],
                        'content': article_data['content'],
                        'link': article_url,
                        'main_image': article_data.get('main_image')
                    })
                    logger.info(f"✅ Статья успешно спарсена")
                else:
                    logger.warning(f"❌ Не удалось спарсить статью")
                
                await asyncio.sleep(random.randint(3, 8))
            
            return news_items
            
        except Exception as e:
            logger.error(f"❌ Ошибка в fetch_from_apnews: {e}")
            return []
    
    async def fetch_from_rss(self, feed_config):
        """Получение статей из RSS"""
        try:
            feed_url = feed_config['url']
            source_name = feed_config['name']
            parser_name = feed_config.get('parser', 'infobrics')
            
            logger.info(f"🔄 {source_name} (RSS)")
            
            parser_func = getattr(self, f'parse_{parser_name}', self.parse_infobrics)
            
            feed = feedparser.parse(feed_url)
            
            if feed.bozo:
                logger.error(f"❌ Ошибка RSS {source_name}: {feed.bozo_exception}")
                return []
            
            logger.info(f"📰 В RSS {len(feed.entries)} статей")
            
            news_items = []
            for entry in feed.entries[:1]:
                link = entry.get('link', '')
                title = entry.get('title', 'Без заголовка')
                
                if link in self.sent_links:
                    logger.info(f"⏭️ Уже опубликовано: {title[:50]}...")
                    continue
                
                logger.info(f"🔍 Новая статья: {title[:50]}...")
                
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
                        'main_image': article_data.get('main_image')
                    })
                    logger.info(f"✅ Статья успешно спарсена")
                else:
                    logger.warning(f"❌ Не удалось спарсить статью")
                
                await asyncio.sleep(random.randint(3, 8))
            
            return news_items
            
        except Exception as e:
            logger.error(f"❌ Ошибка в fetch_from_rss: {e}")
            return []
    
    async def fetch_all_news(self):
        """Сбор новостей из всех источников"""
        all_news = []
        
        today_feeds = get_today_feeds()
        
        for feed in today_feeds:
            if feed['enabled']:
                if feed.get('type') == 'html':
                    news = await self.fetch_from_apnews(feed)
                else:
                    news = await self.fetch_from_rss(feed)
                
                all_news.extend(news)
                await asyncio.sleep(random.randint(3, 8))
        
        random.shuffle(all_news)
        logger.info(f"📊 Всего новых статей: {len(all_news)}")
        return all_news
    
    async def download_image(self, url):
        """Скачивание изображения"""
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
        """Перевод текста"""
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
        """Обрезка первого абзаца по предложениям"""
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
        """Создание подписи с умной обрезкой"""
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
            separator = "\n\n"
            para_with_sep = separator + para
            para_length = len(para_with_sep)
            
            if current_length + para_length <= available_for_text:
                current_text += para_with_sep
                current_length += para_length
                added_any_text = True
                logger.info(f"✅ Добавлен абзац {i+1} целиком ({len(para)} символов)")
            else:
                if i == 0:
                    max_para_length = available_for_text - current_length - len(separator)
                    truncated_para = self.truncate_first_paragraph_by_sentences(para, max_para_length)
                    
                    if truncated_para and len(truncated_para) > 0:
                        current_text += separator + truncated_para
                        current_length += len(separator) + len(truncated_para)
                        added_any_text = True
                        logger.info(f"✂️ Первый абзац обрезан по предложениям ({len(truncated_para)} символов)")
                else:
                    logger.info(f"⏹️ Останов на абзаце {i+1}, дальше не влезает")
                break
        
        if not added_any_text:
            logger.warning("⚠️ Не удалось добавить текст, публикую только заголовок")
        
        final_caption = current_text
        logger.info(f"📏 Итоговая длина: {len(final_caption)}/{max_length}")
        
        return final_caption
    
    async def create_single_post(self, news_item):
        """Создание одного поста"""
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
            return None
    
    async def publish_post(self, post_data):
        """Публикация поста в Telegram"""
        try:
            if post_data['image_path
