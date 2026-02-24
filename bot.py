"""
🤖 Telegram News Bot - Версия 4.0
ПОЛНЫЕ СТАТЬИ + КАРТИНКИ
"""

import os
import logging
import feedparser
import re
import html
import requests
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Bot, InputMediaPhoto
from telegram.error import TelegramError
from deep_translator import GoogleTranslator
import asyncio
import json
from bs4 import BeautifulSoup
from newspaper import Article  # Для извлечения статей
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
CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL', '10800'))  # 3 часа

# RSS источники
RSS_FEEDS = [
    {
        'name': 'Global Research',
        'url': 'https://www.globalresearch.ca/feed',
        'enabled': True,
        'needs_full': True  # Нужно загружать полную статью
    },
    {
        'name': 'InfoBrics',
        'url': 'https://infobrics.org/rss/en',
        'enabled': True,
        'needs_full': True
    },
    {
        'name': 'RT News',
        'url': 'https://www.rt.com/rss/news',
        'enabled': True,
        'needs_full': True
    }
]

SENT_LINKS_FILE = 'sent_links.json'

class NewsBot:
    def __init__(self):
        if not os.path.exists(SENT_LINKS_FILE):
            with open(SENT_LINKS_FILE, 'w', encoding='utf-8') as f:
                json.dump([], f)
        
        self.bot = Bot(token=TELEGRAM_TOKEN)
        self.translator = GoogleTranslator(source='en', target='ru')
        self.scheduler = AsyncIOScheduler()
        self.sent_links = self.load_sent_links()
        self.session = None
    
    async def get_session(self):
        """Получение aiohttp сессии"""
        if not self.session:
            self.session = aiohttp.ClientSession()
        return self.session
    
    def load_sent_links(self):
        try:
            with open(SENT_LINKS_FILE, 'r', encoding='utf-8') as f:
                return set(json.load(f))
        except:
            return set()
    
    def save_sent_links(self):
        with open(SENT_LINKS_FILE, 'w', encoding='utf-8') as f:
            json.dump(list(self.sent_links), f, ensure_ascii=False, indent=2)
    
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
    
    async def download_image(self, url):
        """Скачивание изображения"""
        try:
            if not url:
                return None
            
            # Создаем временный файл
            fd, path = tempfile.mkstemp(suffix='.jpg')
            os.close(fd)
            
            # Скачиваем изображение
            session = await self.get_session()
            async with session.get(url) as response:
                if response.status == 200:
                    with open(path, 'wb') as f:
                        f.write(await response.read())
                    return path
            return None
        except Exception as e:
            logger.error(f"Ошибка скачивания изображения: {e}")
            return None
    
    def extract_full_article(self, url):
        """Извлечение полной статьи с помощью newspaper3k"""
        try:
            logger.info(f"📰 Извлекаю полную статью: {url}")
            
            # Создаем объект Article
            article = Article(url)
            
            # Загружаем и парсим
            article.download()
            article.parse()
            
            # Получаем данные
            title = article.title
            text = article.text
            top_image = article.top_image
            
            # Если нет текста, пробуем через BeautifulSoup
            if not text or len(text) < 100:
                logger.info("🔄 Пробую альтернативный метод...")
                return self.extract_with_soup(url)
            
            # Очищаем текст
            text = self.clean_text(text)
            
            # Ограничиваем длину
            if len(text) > 4000:
                text = text[:4000] + "..."
            
            logger.info(f"✅ Получено {len(text)} символов, изображение: {top_image}")
            
            return {
                'title': title,
                'text': text,
                'image': top_image
            }
            
        except Exception as e:
            logger.error(f"❌ Ошибка извлечения статьи: {e}")
            return self.extract_with_soup(url)
    
    def extract_with_soup(self, url):
        """Запасной метод через BeautifulSoup"""
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            
            response = requests.get(url, headers=headers, timeout=15)
            if response.status_code != 200:
                return None
            
            soup = BeautifulSoup(response.text, 'lxml')
            
            # Удаляем ненужное
            for element in soup.find_all(['script', 'style', 'nav', 'footer', 'header']):
                element.decompose()
            
            # Ищем заголовок
            title = None
            title_tag = soup.find('h1')
            if title_tag:
                title = title_tag.get_text(strip=True)
            
            # Ищем контент
            content = None
            
            # Пробуем разные селекторы
            selectors = [
                'article',
                '.post-content',
                '.entry-content',
                '.article-content',
                '.content',
                'main',
                '#content'
            ]
            
            for selector in selectors:
                content = soup.select_one(selector)
                if content:
                    break
            
            if not content:
                # Берем весь body
                content = soup.body
            
            if content:
                # Получаем текст
                text = content.get_text(separator='\n\n', strip=True)
                text = self.clean_text(text)
                
                # Ищем первое изображение
                image = None
                img_tag = content.find('img')
                if img_tag and img_tag.get('src'):
                    img_url = img_tag['src']
                    # Преобразуем относительный URL в абсолютный
                    if not img_url.startswith('http'):
                        parsed = urlparse(url)
                        base = f"{parsed.scheme}://{parsed.netloc}"
                        img_url = urljoin(base, img_url)
                    image = img_url
                
                return {
                    'title': title or 'Без заголовка',
                    'text': text[:4000],
                    'image': image
                }
            
            return None
            
        except Exception as e:
            logger.error(f"❌ Ошибка Soup парсинга: {e}")
            return None
    
    async def fetch_news_from_feed(self, feed_config):
        """Получение новостей из RSS"""
        try:
            feed_url = feed_config['url']
            source_name = feed_config['name']
            needs_full = feed_config.get('needs_full', True)
            
            logger.info(f"🔄 {source_name}: {feed_url}")
            
            feed = feedparser.parse(feed_url)
            
            if feed.bozo:
                logger.error(f"❌ Ошибка RSS {source_name}")
                return []
            
            news_items = []
            for entry in feed.entries[:2]:  # По 2 статьи из каждого источника
                link = entry.get('link', '')
                
                if link in self.sent_links:
                    logger.info(f"⏭️ Дубль: {link[:50]}...")
                    continue
                
                # Заголовок из RSS
                title = self.clean_text(entry.get('title', ''))
                
                # Получаем полную статью если нужно
                article_data = None
                if needs_full and link:
                    loop = asyncio.get_event_loop()
                    article_data = await loop.run_in_executor(None, self.extract_full_article, link)
                
                if article_data and article_data.get('text'):
                    # Используем данные из полной статьи
                    final_title = article_data.get('title') or title
                    final_text = article_data.get('text', '')
                    final_image = article_data.get('image')
                else:
                    # Используем данные из RSS
                    final_title = title
                    final_text = ""
                    if hasattr(entry, 'description'):
                        final_text = self.clean_text(entry.description)
                    elif hasattr(entry, 'summary'):
                        final_text = self.clean_text(entry.summary)
                    final_image = None
                
                news_items.append({
                    'source': source_name,
                    'title': final_title,
                    'content': final_text,
                    'link': link,
                    'image': final_image
                })
                
                logger.info(f"📰 {source_name}: '{final_title[:50]}...'")
            
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
                await asyncio.sleep(5)  # Пауза между источниками
        
        # Удаляем дубликаты
        unique = []
        seen = set()
        for item in all_news:
            if item['link'] not in seen:
                seen.add(item['link'])
                unique.append(item)
        
        logger.info(f"📊 Всего уникальных: {len(unique)}")
        return unique
    
    def translate_text(self, text):
        """Перевод текста"""
        try:
            if not text or len(text) < 20:
                return text
            
            # Ограничиваем длину для перевода
            if len(text) > 4000:
                text = text[:4000] + "..."
            
            # Переводим
            translated = self.translator.translate(text)
            
            if translated and len(translated) > 10:
                return translated
            
            return text
            
        except Exception as e:
            logger.error(f"❌ Ошибка перевода: {e}")
            return text
    
    async def create_post_with_image(self, news_item):
        """Создание поста с изображением"""
        try:
            loop = asyncio.get_event_loop()
            
            # Переводим заголовок и текст
            title_ru = await loop.run_in_executor(None, self.translate_text, news_item['title'])
            content_ru = await loop.run_in_executor(None, self.translate_text, news_item['content'])
            
            # Формируем текст поста
            post_text = f"<b>{title_ru}</b>\n\n"
            
            if content_ru:
                # Разбиваем на абзацы для читаемости
                paragraphs = content_ru.split('\n\n')
                for para in paragraphs[:8]:  # Больше абзацев
                    if para.strip():
                        post_text += f"{para.strip()}\n\n"
            
            # Добавляем источник
            post_text += f"📰 <a href='{news_item['link']}'>Источник: {news_item['source']}</a>"
            
            # Проверяем длину
            if len(post_text) > 4000:
                post_text = post_text[:4000] + "...\n\n📰 <a href='{news_item['link']}'>Читать оригинал</a>"
            
            # Если есть изображение, скачиваем и отправляем с ним
            if news_item['image']:
                image_path = await self.download_image(news_item['image'])
                if image_path:
                    return {
                        'type': 'photo',
                        'path': image_path,
                        'caption': post_text
                    }
            
            # Если нет изображения - просто текст
            return {
                'type': 'text',
                'text': post_text
            }
            
        except Exception as e:
            logger.error(f"❌ Ошибка создания поста: {e}")
            return None
    
    async def publish_post(self, post_data):
        """Публикация поста в Telegram"""
        try:
            if post_data['type'] == 'photo':
                # Отправляем с фото
                with open(post_data['path'], 'rb') as photo:
                    await self.bot.send_photo(
                        chat_id=CHANNEL_ID,
                        photo=photo,
                        caption=post_data['caption'],
                        parse_mode='HTML'
                    )
                # Удаляем временный файл
                os.unlink(post_data['path'])
                logger.info("✅ Пост с фото опубликован")
                
            else:
                # Отправляем текст
                await self.bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=post_data['text'],
                    parse_mode='HTML',
                    disable_web_page_preview=False
                )
                logger.info("✅ Текстовый пост опубликован")
            
            return True
            
        except Exception as e:
            logger.error(f"❌ Ошибка публикации: {e}")
            return False
    
    async def check_and_publish(self):
        """Основной цикл"""
        logger.info("🔍 НАЧАЛО ПРОВЕРКИ")
        
        # Собираем новости
        news_items = await self.fetch_all_news()
        
        if not news_items:
            logger.info("📭 Новых новостей нет")
            return
        
        published = 0
        
        # Публикуем каждую
        for item in news_items:
            post = await self.create_post_with_image(item)
            
            if post:
                success = await self.publish_post(post)
                
                if success:
                    self.sent_links.add(item['link'])
                    self.save_sent_links()
                    published += 1
                    
                    # Пауза между постами
                    if len(news_items) > 1 and published < len(news_items):
                        logger.info("⏱ Пауза 5 минут...")
                        await asyncio.sleep(300)  # 5 минут
        
        logger.info(f"📊 Опубликовано: {published}")
    
    async def start(self):
        """Запуск бота"""
        logger.info("=" * 60)
        logger.info("🚀 NEWS BOT 4.0 - ПОЛНЫЕ СТАТЬИ")
        logger.info(f"📢 Канал: {CHANNEL_ID}")
        logger.info(f"📡 Источники: {[f['name'] for f in RSS_FEEDS if f['enabled']]}")
        logger.info("=" * 60)
        
        # Проверяем бота
        try:
            me = await self.bot.get_me()
            logger.info(f"✅ Бот @{me.username} активен")
        except Exception as e:
            logger.error(f"❌ Ошибка бота: {e}")
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
        logger.info(f"✅ Следующая проверка через {CHECK_INTERVAL//3600}ч")
        
        # Держим запущенным
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
