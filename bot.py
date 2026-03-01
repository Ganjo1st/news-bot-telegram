"""
🤖 Telegram News Bot - Версия 8.8
С AP NEWS ПО ВЫХОДНЫМ
- UTC+7 часовой пояс
- Без указания источника
- Умная обрезка текста
- AP News только в выходные
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
        'parser': 'infobrics'
    },
    {
        'name': 'Global Research',
        'url': 'https://www.globalresearch.ca/feed',
        'enabled': True,
        'parser': 'globalresearch'
    }
]

# Источники для выходных
WEEKEND_FEEDS = [
    {
        'name': 'AP News',
        'url': 'https://apnews.com/apf-topnews?utm_source=feedburner&utm_medium=feed&utm_campaign=Feed%3A+apnews%2FApTopNews+%28AP+Top+News%29',
        'enabled': True,
        'parser': 'apnews'
    },
    {
        'name': 'AP World News',
        'url': 'https://apnews.com/world-news?utm_source=feedburner&utm_medium=feed&utm_campaign=Feed%3A+apnews%2FApWorldNews+%28AP+World+News%29',
        'enabled': True,
        'parser': 'apnews'
    }
]

# Определяем, какие источники использовать сегодня
def get_today_feeds():
    """Возвращает список источников в зависимости от дня недели"""
    local_hour = (datetime.now().hour + TIMEZONE_OFFSET) % 24
    local_date = datetime.now() + timedelta(hours=TIMEZONE_OFFSET)
    weekday = local_date.weekday()  # 0-6: понедельник-воскресенье
    
    # Выходные: суббота (5) и воскресенье (6)
    if weekday >= 5:
        logger.info(f"📅 Сегодня выходной (день {weekday}), использую AP News")
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
    
    def parse_apnews(self, url, source_name):
        """Парсер для AP News"""
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
            img_elem = soup.find('meta', property='og:image')
            if img_elem and img_elem.get('content'):
                main_image = img_elem['content']
            else:
                img_elem = soup.find('img', class_=re.compile(r'image|photo|featured'))
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
            # AP News часто использует article или div с классом Article
            text_container = soup.find('article') or soup.find('div', class_=re.compile(r'Article|story-body|content'))
            
            if text_container:
                for unwanted in text_container.find_all(['script', 'style', 'button', 'aside']):
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
            logger.error(f"❌ Ошибка парсинга AP News: {e}")
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
    
    def get_parser(self, parser_name):
        """Возвращает функцию парсера по имени"""
        parsers = {
            'apnews': self.parse_apnews,
            'infobrics': self.parse_infobrics,
            'globalresearch': self.parse_globalresearch
        }
        return parsers.get(parser_name, self.parse_apnews)
    
    async def fetch_news_from_feed(self, feed_config):
        try:
            feed_url = feed_config['url']
            source_name = feed_config['name']
            parser_name = feed_config.get('parser', 'apnews')
            parser_func = self.get_parser(parser_name)
            
            logger.info(f"🔄 {source_name} (парсер: {parser_name})")
            
            feed = feedparser.parse(feed_url)
            
            if feed.bozo:
                logger.warning(f"⚠️ Ошибка RSS {source_name}, пробую запасной URL")
                # Запасной URL для AP News
                if 'apnews' in feed_url:
                    alt_url = 'https://apnews.com/world-news'
                    feed = feedparser.parse(alt_url)
                    if feed.bozo:
                        return []
            
            news_items = []
            for entry in feed.entries[:1]:
                link = entry.get('link', '')
                
                if link in self.sent_links:
                    continue
                
                loop = asyncio.get_event_loop()
                article_data = await loop.run_in_executor(
                    None, 
                    parser_func, 
                    link, 
                    source_name
                )
                
                if article_data:
                    news_items.append({
                        'source': source_name,
                        'title': article_data['title'],
                        'content': article_data['content'],
                        'link': link,
                        'main_image': article_data.get('main_image')
                    })
                
                await asyncio.sleep(random.randint(3, 8))
            
            return news_items
            
        except Exception as e:
            logger.error(f"❌ Ошибка: {e}")
            return []
    
    async def fetch_all_news(self):
        all_news = []
        
        # Получаем источники для сегодняшнего дня
        today_feeds = get_today_feeds()
        
        for feed in today_feeds:
            if feed['enabled']:
                news = await self.fetch_news_from_feed(feed)
                all_news.extend(news)
                await asyncio.sleep(random.randint(3, 8))
        
        random.shuffle(all_news)
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
        logger.info("=" * 60)
        logger.info("🔍 ПРОВЕРКА НОВОСТЕЙ")
        logger.info("=" * 60)
        
        if not self.can_post_now():
            logger.info("⏳ Нельзя публиковать сейчас")
            return
        
        news_items = await self.fetch_all_news()
        
        if not news_items:
            logger.info("📭 Новых статей нет")
            return
        
        published = 0
        
        for item in news_items:
            if not self.can_post_now():
                logger.info("⏳ Лимит достигнут")
                break
            
            logger.info(f"\n📝 Публикую: {item['title'][:50]}...")
            
            post_data = await self.create_single_post(item)
            
            if post_data:
                success = await self.publish_post(post_data)
                
                if success:
                    self.sent_links.add(item['link'])
                    self.save_json(SENT_LINKS_FILE, self.sent_links)
                    self.log_post(item['link'], item['title'])
                    published += 1
                    
                    if published < len(news_items):
                        pause = MIN_POST_INTERVAL + random.randint(-120, 300)
                        logger.info(f"⏱ Пауза {pause//60} минут...")
                        await asyncio.sleep(pause)
        
        logger.info(f"\n📊 Опубликовано: {published}")
    
    async def start(self):
        logger.info("=" * 70)
        logger.info("🚀 NEWS BOT 8.8 - С AP NEWS ПО ВЫХОДНЫМ")
        logger.info(f"📢 Канал: {CHANNEL_ID}")
        logger.info(f"⏱ Проверка: каждые {CHECK_INTERVAL//3600}ч")
        logger.info(f"🛡️ Лимит: {MAX_POSTS_PER_DAY}/день")
        logger.info(f"📏 Лимит подписи: {TELEGRAM_MAX_CAPTION}")
        logger.info(f"🌍 Часовой пояс: UTC+{TIMEZONE_OFFSET}")
        
        # Показываем, какие источники будут использоваться сегодня
        today_feeds = get_today_feeds()
        logger.info("📡 Источники сегодня:")
        for feed in today_feeds:
            if feed['enabled']:
                logger.info(f"   - {feed['name']}")
        
        logger.info("=" * 70)
        
        try:
            me = await self.bot.get_me()
            logger.info(f"✅ Бот @{me.username}")
        except Exception as e:
            logger.error(f"❌ Ошибка: {e}")
            return
        
        await self.check_and_publish()
        
        self.scheduler.add_job(
            self.check_and_publish,
            'interval',
            seconds=CHECK_INTERVAL
        )
        self.scheduler.start()
        logger.info(f"✅ Планировщик запущен")
        
        try:
            while True:
                await asyncio.sleep(60)
        except KeyboardInterrupt:
            if self.session:
                await self.session.close()

async def main():
    bot = NewsBot()
    await bot.start()

if __name__ == "__main__":
    asyncio.run(main())
