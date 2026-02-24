"""
🤖 Telegram News Bot - Версия 5.0
ПОЛНЫЕ СТАТЬИ + КАРТИНКИ + ИСПРАВЛЕННЫЙ ПАРСИНГ + БЕЗОПАСНЫЙ ТАЙМИНГ
"""

import os
import logging
import feedparser
import re
import html
import requests
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Bot, InputMediaPhoto
from telegram.error import TelegramError
from deep_translator import GoogleTranslator
import asyncio
import json
from bs4 import BeautifulSoup
from newspaper import Article
import tempfile
from urllib.parse import urljoin, urlparse
import aiohttp

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Конфигурация
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', '8767446234:AAGRz1sJfDtV321CpUBdI2sqGVDcWryGqcY')
CHANNEL_ID = os.getenv('CHANNEL_ID', '@Novikon_news')
CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL', '21600'))  # 6 часов (безопасно!)
MIN_POST_INTERVAL = 600  # Минимум 10 минут между постами
MAX_POSTS_PER_DAY = 24   # Максимум постов в день

# RSS источники
RSS_FEEDS = [
    {
        'name': 'Global Research',
        'url': 'https://www.globalresearch.ca/feed',
        'enabled': True,
        'needs_full': True,
        'content_selector': 'article, .entry-content, .post-content'
    },
    {
        'name': 'InfoBrics',
        'url': 'https://infobrics.org/rss/en',
        'enabled': True,
        'needs_full': True,
        'content_selector': 'article, .post-content, .content'  # Специально для InfoBrics
    },
    {
        'name': 'RT News',
        'url': 'https://www.rt.com/rss/news',
        'enabled': True,
        'needs_full': True,
        'content_selector': '.article__text, .article-content, .content'
    }
]

SENT_LINKS_FILE = 'sent_links.json'
POSTS_LOG_FILE = 'posts_log.json'

class NewsBot:
    def __init__(self):
        # Создаем файлы если их нет
        for file in [SENT_LINKS_FILE, POSTS_LOG_FILE]:
            if not os.path.exists(file):
                with open(file, 'w', encoding='utf-8') as f:
                    json.dump([], f)
        
        self.bot = Bot(token=TELEGRAM_TOKEN)
        self.translator = GoogleTranslator(source='en', target='ru')
        self.scheduler = AsyncIOScheduler()
        self.sent_links = self.load_json(SENT_LINKS_FILE)
        self.posts_log = self.load_json(POSTS_LOG_FILE)
        self.session = None
        self.last_post_time = None
    
    def load_json(self, filename):
        """Загрузка JSON файла"""
        try:
            with open(filename, 'r', encoding='utf-8') as f:
                return set(json.load(f)) if filename == SENT_LINKS_FILE else json.load(f)
        except:
            return set() if filename == SENT_LINKS_FILE else []
    
    def save_json(self, filename, data):
        """Сохранение JSON файла"""
        with open(filename, 'w', encoding='utf-8') as f:
            if isinstance(data, set):
                json.dump(list(data), f, ensure_ascii=False, indent=2)
            else:
                json.dump(data, f, ensure_ascii=False, indent=2)
    
    def can_post_now(self):
        """Проверка, можно ли публиковать сейчас (защита от спама)"""
        if not self.posts_log:
            return True
        
        # Проверяем последний пост
        last_post = max(self.posts_log, key=lambda x: x['time']) if self.posts_log else None
        if last_post:
            last_time = datetime.fromisoformat(last_post['time'])
            if datetime.now() - last_time < timedelta(seconds=MIN_POST_INTERVAL):
                return False
        
        # Проверяем лимит за день
        today = datetime.now().date()
        today_posts = [p for p in self.posts_log 
                      if datetime.fromisoformat(p['time']).date() == today]
        
        if len(today_posts) >= MAX_POSTS_PER_DAY:
            return False
        
        return True
    
    def log_post(self, link, title):
        """Логирование поста"""
        self.posts_log.append({
            'link': link,
            'title': title[:50],
            'time': datetime.now().isoformat()
        })
        # Оставляем только последние 100 записей
        if len(self.posts_log) > 100:
            self.posts_log = self.posts_log[-100:]
        self.save_json(POSTS_LOG_FILE, self.posts_log)
    
    async def get_session(self):
        if not self.session:
            self.session = aiohttp.ClientSession()
        return self.session
    
    def clean_text(self, text):
        """Очистка текста от мусора"""
        if not text:
            return ""
        
        # Декодируем HTML entities
        text = html.unescape(text)
        
        # Удаляем HTML теги
        text = re.sub(r'<[^>]+>', '', text)
        
        # Удаляем множественные переносы строк
        text = re.sub(r'\n\s*\n\s*\n', '\n\n', text)
        
        # Удаляем лишние пробелы
        text = re.sub(r' +', ' ', text)
        
        # Удаляем мусорные символы
        text = re.sub(r'[^\x00-\x7Fа-яА-ЯёЁ\s\.,!?:;"\'()\[\]-]', '', text)
        
        return text.strip()
    
    def extract_main_content(self, soup, selectors):
        """Извлечение основного контента по селекторам"""
        for selector in selectors:
            content = soup.select_one(selector)
            if content:
                return content
        return None
    
    def extract_full_article(self, url, feed_config):
        """Извлечение полной статьи с правильными селекторами"""
        try:
            logger.info(f"📰 Извлекаю: {url}")
            
            # Пробуем через newspaper3k
            try:
                article = Article(url)
                article.download()
                article.parse()
                
                if article.text and len(article.text) > 200:
                    logger.info(f"✅ Newspaper: {len(article.text)} символов")
                    return {
                        'title': article.title,
                        'text': self.clean_text(article.text),
                        'image': article.top_image
                    }
            except:
                pass
            
            # Если не получилось, используем BeautifulSoup
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
            response = requests.get(url, headers=headers, timeout=15)
            
            if response.status_code != 200:
                return None
            
            soup = BeautifulSoup(response.text, 'lxml')
            
            # Удаляем ненужные элементы
            for element in soup.find_all(['script', 'style', 'nav', 'footer', 'header', 'aside']):
                element.decompose()
            
            # Ищем заголовок
            title_tag = soup.find('h1')
            title = title_tag.get_text(strip=True) if title_tag else "Без заголовка"
            
            # Ищем основной контент
            selectors = feed_config.get('content_selector', 'article, .content, main').split(', ')
            content = self.extract_main_content(soup, selectors)
            
            if not content:
                # Если не нашли, берем body
                content = soup.body
            
            if content:
                # Удаляем лишние элементы из контента
                for element in content.find_all(['div', 'section'], class_=re.compile(r'(sidebar|menu|nav|footer|header|comment)')):
                    element.decompose()
                
                # Получаем текст
                text = content.get_text(separator='\n\n', strip=True)
                text = self.clean_text(text)
                
                # Ищем изображение
                image = None
                img_tag = content.find('img')
                if img_tag and img_tag.get('src'):
                    img_url = img_tag['src']
                    if not img_url.startswith('http'):
                        parsed = urlparse(url)
                        base = f"{parsed.scheme}://{parsed.netloc}"
                        img_url = urljoin(base, img_url)
                    image = img_url
                
                return {
                    'title': title,
                    'text': text[:4000],  # Ограничиваем для перевода
                    'image': image
                }
            
            return None
            
        except Exception as e:
            logger.error(f"❌ Ошибка: {e}")
            return None
    
    async def fetch_news_from_feed(self, feed_config):
        """Получение новостей из RSS"""
        try:
            feed_url = feed_config['url']
            source_name = feed_config['name']
            
            logger.info(f"🔄 {source_name}")
            
            feed = feedparser.parse(feed_url)
            
            if feed.bozo:
                logger.error(f"❌ Ошибка RSS {source_name}")
                return []
            
            news_items = []
            for entry in feed.entries[:2]:  # По 2 статьи из каждого источника
                link = entry.get('link', '')
                
                if link in self.sent_links:
                    continue
                
                # Получаем полную статью
                loop = asyncio.get_event_loop()
                article_data = await loop.run_in_executor(
                    None, 
                    self.extract_full_article, 
                    link, 
                    feed_config
                )
                
                if article_data and article_data.get('text'):
                    news_items.append({
                        'source': source_name,
                        'title': article_data.get('title', entry.get('title', '')),
                        'content': article_data.get('text', ''),
                        'link': link,
                        'image': article_data.get('image')
                    })
                    logger.info(f"📰 Добавлено: {article_data.get('title', '')[:50]}...")
            
            return news_items
            
        except Exception as e:
            logger.error(f"❌ Ошибка: {e}")
            return []
    
    async def fetch_all_news(self):
        """Сбор новостей из всех источников"""
        all_news = []
        
        for feed in RSS_FEEDS:
            if feed['enabled']:
                news = await self.fetch_news_from_feed(feed)
                all_news.extend(news)
                await asyncio.sleep(10)  # Пауза между источниками
        
        # Удаляем дубликаты
        unique = []
        seen = set()
        for item in all_news:
            if item['link'] not in seen:
                seen.add(item['link'])
                unique.append(item)
        
        logger.info(f"📊 Найдено новых: {len(unique)}")
        return unique
    
    def translate_text(self, text):
        """Перевод текста"""
        try:
            if not text or len(text) < 20:
                return text
            
            # Переводим по частям если длинный текст
            if len(text) > 4000:
                parts = [text[i:i+4000] for i in range(0, len(text), 4000)]
                translated_parts = []
                for part in parts[:3]:  # Максимум 3 части
                    translated_parts.append(self.translator.translate(part))
                return ' '.join(translated_parts)
            
            return self.translator.translate(text)
            
        except Exception as e:
            logger.error(f"❌ Ошибка перевода: {e}")
            return text
    
    async def create_post_with_image(self, news_item):
        """Создание поста с изображением"""
        try:
            loop = asyncio.get_event_loop()
            
            # Переводим
            title_ru = await loop.run_in_executor(None, self.translate_text, news_item['title'])
            content_ru = await loop.run_in_executor(None, self.translate_text, news_item['content'])
            
            # Формируем текст
            post_text = f"<b>{title_ru}</b>\n\n"
            
            if content_ru:
                paragraphs = content_ru.split('\n\n')
                for para in paragraphs[:10]:
                    if para.strip():
                        post_text += f"{para.strip()}\n\n"
            
            post_text += f"📰 <a href='{news_item['link']}'>Источник: {news_item['source']}</a>"
            
            # Ограничиваем длину
            if len(post_text) > 4000:
                post_text = post_text[:4000] + "...\n\n📰 <a href='{news_item['link']}'>Читать оригинал</a>"
            
            # Если есть изображение
            if news_item['image']:
                try:
                    image_path = await self.download_image(news_item['image'])
                    if image_path:
                        return {
                            'type': 'photo',
                            'path': image_path,
                            'caption': post_text
                        }
                except:
                    pass
            
            return {
                'type': 'text',
                'text': post_text
            }
            
        except Exception as e:
            logger.error(f"❌ Ошибка создания: {e}")
            return None
    
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
        except:
            return None
    
    async def publish_post(self, post_data):
        """Публикация поста"""
        try:
            if post_data['type'] == 'photo':
                with open(post_data['path'], 'rb') as photo:
                    await self.bot.send_photo(
                        chat_id=CHANNEL_ID,
                        photo=photo,
                        caption=post_data['caption'],
                        parse_mode='HTML'
                    )
                os.unlink(post_data['path'])
            else:
                await self.bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=post_data['text'],
                    parse_mode='HTML',
                    disable_web_page_preview=False
                )
            
            logger.info("✅ Пост опубликован")
            return True
            
        except TelegramError as e:
            if "Too Many Requests" in str(e):
                logger.warning("⚠️ Слишком много запросов, жду 1 час...")
                await asyncio.sleep(3600)
            else:
                logger.error(f"❌ Ошибка: {e}")
            return False
    
    async def check_and_publish(self):
        """Основной цикл с защитой от спама"""
        logger.info("🔍 ПРОВЕРКА НОВОСТЕЙ")
        
        # Проверяем лимиты
        if not self.can_post_now():
            logger.info("⏳ Достигнут лимит постов, пропускаю")
            return
        
        # Собираем новости
        news_items = await self.fetch_all_news()
        
        if not news_items:
            return
        
        published = 0
        
        # Публикуем
        for item in news_items:
            # Проверяем лимиты перед каждым постом
            if not self.can_post_now():
                logger.info("⏳ Достигнут лимит, останавливаюсь")
                break
            
            post = await self.create_post_with_image(item)
            
            if post:
                success = await self.publish_post(post)
                
                if success:
                    self.sent_links.add(item['link'])
                    self.save_json(SENT_LINKS_FILE, self.sent_links)
                    self.log_post(item['link'], item['title'])
                    published += 1
                    
                    # Пауза между постами
                    if published < len(news_items):
                        logger.info(f"⏱ Пауза {MIN_POST_INTERVAL//60} минут...")
                        await asyncio.sleep(MIN_POST_INTERVAL)
        
        logger.info(f"📊 Опубликовано: {published}")
    
    async def start(self):
        """Запуск бота"""
        logger.info("=" * 60)
        logger.info("🚀 NEWS BOT 5.0 - БЕЗОПАСНЫЙ РЕЖИМ")
        logger.info(f"📢 Канал: {CHANNEL_ID}")
        logger.info(f"⏱ Интервал: {CHECK_INTERVAL//3600}ч")
        logger.info(f"🛡️ Лимит: {MAX_POSTS_PER_DAY} постов/день")
        logger.info("=" * 60)
        
        # Проверяем бота
        try:
            me = await self.bot.get_me()
            logger.info(f"✅ Бот @{me.username}")
        except Exception as e:
            logger.error(f"❌ Ошибка: {e}")
            return
        
        # Первый запуск
        await self.check_and_publish()
        
        # Планировщик
        self.scheduler.add_job(
            self.check_and_publish,
            'interval',
            seconds=CHECK_INTERVAL
        )
        self.scheduler.start()
        
        # Держим запущенным
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
