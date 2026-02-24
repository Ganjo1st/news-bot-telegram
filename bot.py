"""
🤖 Telegram News Bot - Версия 7.0
СПЕЦИАЛЬНО ДЛЯ INFOBRICS
Берет: заголовок, картинку, полный текст
"""

import os
import logging
import feedparser
import re
import html
import requests
import time
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Bot
from telegram.error import TelegramError
from deep_translator import GoogleTranslator
import asyncio
import json
from bs4 import BeautifulSoup
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
CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL', '21600'))  # 6 часов
MIN_POST_INTERVAL = 600  # 10 минут между постами
MAX_POSTS_PER_DAY = 24

# ТОЛЬКО INFOBRICS (другие источники отключены)
RSS_FEEDS = [
    {
        'name': 'InfoBrics',
        'url': 'https://infobrics.org/rss/en',
        'enabled': True,
        'domain': 'infobrics.org'
    }
    # Global Research и RT отключены для фокуса на InfoBrics
]

SENT_LINKS_FILE = 'sent_links.json'
POSTS_LOG_FILE = 'posts_log.json'

class NewsBot:
    def __init__(self):
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
    
    def load_json(self, filename):
        try:
            with open(filename, 'r', encoding='utf-8') as f:
                return set(json.load(f)) if filename == SENT_LINKS_FILE else json.load(f)
        except:
            return set() if filename == SENT_LINKS_FILE else []
    
    def save_json(self, filename, data):
        with open(filename, 'w', encoding='utf-8') as f:
            if isinstance(data, set):
                json.dump(list(data), f, ensure_ascii=False, indent=2)
            else:
                json.dump(data, f, ensure_ascii=False, indent=2)
    
    def can_post_now(self):
        if not self.posts_log:
            return True
        
        last_post = max(self.posts_log, key=lambda x: x['time']) if self.posts_log else None
        if last_post:
            last_time = datetime.fromisoformat(last_post['time'])
            if datetime.now() - last_time < timedelta(seconds=MIN_POST_INTERVAL):
                return False
        
        today = datetime.now().date()
        today_posts = [p for p in self.posts_log 
                      if datetime.fromisoformat(p['time']).date() == today]
        
        return len(today_posts) < MAX_POSTS_PER_DAY
    
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
        """Очистка текста"""
        if not text:
            return ""
        
        # Декодируем HTML entities
        text = html.unescape(text)
        
        # Удаляем HTML теги
        text = re.sub(r'<[^>]+>', '', text)
        
        # Нормализуем переносы строк
        text = re.sub(r'\n\s*\n\s*\n', '\n\n', text)
        text = re.sub(r' +', ' ', text)
        
        # Удаляем лишние пробелы в начале и конце
        text = text.strip()
        
        return text
    
    # ========== ИДЕАЛЬНЫЙ ПАРСЕР ДЛЯ INFOBRICS ==========
    
    def parse_infobrics(self, url):
        """Берет только: заголовок, картинку, текст статьи"""
        try:
            logger.info(f"🌐 Парсинг InfoBrics: {url}")
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
                'Connection': 'keep-alive',
            }
            
            response = requests.get(url, headers=headers, timeout=15)
            if response.status_code != 200:
                logger.error(f"❌ Ошибка загрузки: {response.status_code}")
                return None
            
            soup = BeautifulSoup(response.text, 'lxml')
            
            # ===== 1. ЗАГОЛОВОК =====
            title = None
            # Пробуем найти заголовок в разных местах
            title_elem = soup.find('div', class_=re.compile(r'title.*big'))
            if not title_elem:
                title_elem = soup.find('h1')
            if not title_elem:
                title_elem = soup.find('div', class_=re.compile(r'docs.*head'))
            
            if title_elem:
                title = title_elem.get_text(strip=True)
                logger.info(f"✅ Заголовок: {title[:50]}...")
            else:
                title = "Без заголовка"
                logger.warning("⚠️ Заголовок не найден")
            
            # ===== 2. ИЗОБРАЖЕНИЕ =====
            image_url = None
            img_elem = soup.find('img', class_=re.compile(r'article.*image'))
            if not img_elem:
                img_elem = soup.find('div', class_=re.compile(r'article')).find('img') if soup.find('div', class_=re.compile(r'article')) else None
            
            if img_elem and img_elem.get('src'):
                img_src = img_elem['src']
                if img_src.startswith('/'):
                    image_url = f"https://infobrics.org{img_src}"
                elif not img_src.startswith('http'):
                    image_url = f"https://infobrics.org/{img_src}"
                else:
                    image_url = img_src
                logger.info(f"✅ Изображение: {image_url}")
            
            # ===== 3. ТЕКСТ СТАТЬИ =====
            article_text = ""
            
            # Находим контейнер с текстом статьи
            text_container = None
            
            # Самый точный селектор - div с классом article__text
            text_container = soup.find('div', class_=re.compile(r'article__text'))
            
            # Если не нашли, ищем любой div с article в классе
            if not text_container:
                text_container = soup.find('div', class_=re.compile(r'article'))
            
            # Если все еще не нашли, ищем docs__article
            if not text_container:
                text_container = soup.find('div', class_=re.compile(r'docs__article'))
            
            if text_container:
                logger.info(f"✅ Найден контейнер с текстом")
                
                # Удаляем ненужные элементы (кнопки шаринга, скрипты и т.д.)
                for unwanted in text_container.find_all(['script', 'style', 'button', 'div.social', 'div.share']):
                    unwanted.decompose()
                
                # Собираем все параграфы
                paragraphs = []
                
                # Ищем все теги p
                for p in text_container.find_all('p'):
                    p_text = p.get_text(strip=True)
                    # Пропускаем пустые или слишком короткие параграфы
                    if p_text and len(p_text) > 15:
                        # Проверяем, что это не служебный текст
                        lower_text = p_text.lower()
                        if not any(skip in lower_text for skip in ['subscribe', 'follow us', 'share this', 'tags:', 'category:']):
                            paragraphs.append(p_text)
                
                # Если не нашли p, пробуем получить весь текст
                if not paragraphs:
                    # Получаем текст, разделяя по двойным переносам
                    raw_text = text_container.get_text(separator='\n\n', strip=True)
                    # Разбиваем на блоки
                    blocks = raw_text.split('\n\n')
                    for block in blocks:
                        if len(block) > 30 and not any(skip in block.lower() for skip in ['subscribe', 'follow', 'share', 'tags', 'category']):
                            paragraphs.append(block)
                
                # Объединяем параграфы
                if paragraphs:
                    article_text = '\n\n'.join(paragraphs)
                    logger.info(f"✅ Собрано {len(paragraphs)} параграфов, всего {len(article_text)} символов")
                else:
                    # Если не нашли параграфы, берем весь текст
                    article_text = text_container.get_text(separator='\n\n', strip=True)
                    logger.info(f"✅ Взят весь текст контейнера: {len(article_text)} символов")
            
            # Проверяем, что текст достаточно длинный (не меню, не сайдбар)
            if len(article_text) < 200:
                logger.warning(f"⚠️ Текст слишком короткий ({len(article_text)} символов), возможно, взят не тот контейнер")
                
                # Пробуем найти альтернативный контейнер
                # Ищем основной контент страницы
                main_content = soup.find('main') or soup.find('div', class_=re.compile(r'content'))
                if main_content:
                    # Удаляем явно служебные части
                    for nav in main_content.find_all(['nav', 'header', 'footer']):
                        nav.decompose()
                    
                    article_text = main_content.get_text(separator='\n\n', strip=True)
                    logger.info(f"✅ Взят текст из main: {len(article_text)} символов")
            
            # Финальная очистка
            article_text = self.clean_text(article_text)
            
            # Возвращаем результат
            if article_text and len(article_text) > 300:
                result = {
                    'title': title,
                    'content': article_text,
                    'image': image_url
                }
                logger.info(f"🎉 УСПЕХ: статья '{title[:50]}...' ({len(article_text)} символов)")
                return result
            else:
                logger.error(f"❌ Не удалось извлечь текст статьи (длина: {len(article_text)})")
                return None
            
        except Exception as e:
            logger.error(f"❌ Ошибка парсинга InfoBrics: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    # ========== ОСНОВНАЯ ЛОГИКА ==========
    
    async def fetch_news_from_feed(self, feed_config):
        """Получение новостей из RSS"""
        try:
            feed_url = feed_config['url']
            source_name = feed_config['name']
            
            logger.info(f"🔄 {source_name}: {feed_url}")
            
            feed = feedparser.parse(feed_url)
            
            if feed.bozo:
                logger.error(f"❌ Ошибка RSS {source_name}")
                return []
            
            news_items = []
            # Берем только 1 самую свежую статью за раз
            for entry in feed.entries[:1]:
                link = entry.get('link', '')
                
                if link in self.sent_links:
                    logger.info(f"⏭️ Уже было: {link}")
                    continue
                
                # Получаем полный текст статьи через наш специальный парсер
                loop = asyncio.get_event_loop()
                article_data = await loop.run_in_executor(
                    None, 
                    self.parse_infobrics, 
                    link
                )
                
                if article_data:
                    news_items.append({
                        'source': source_name,
                        'title': article_data['title'],
                        'content': article_data['content'],
                        'link': link,
                        'image': article_data.get('image')
                    })
                    
                    logger.info(f"📰 Добавлено: {article_data['title'][:50]}...")
                else:
                    logger.warning(f"⚠️ Не удалось извлечь статью для {link}")
                
                # Пауза между запросами к сайту
                await asyncio.sleep(5)
            
            return news_items
            
        except Exception as e:
            logger.error(f"❌ Ошибка: {e}")
            return []
    
    async def fetch_all_news(self):
        """Сбор новостей"""
        all_news = []
        
        for feed in RSS_FEEDS:
            if feed['enabled']:
                news = await self.fetch_news_from_feed(feed)
                all_news.extend(news)
        
        logger.info(f"📊 Найдено новых статей: {len(all_news)}")
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
            logger.error(f"Ошибка скачивания изображения: {e}")
            return None
    
    def translate_text(self, text):
        """Перевод текста"""
        try:
            if not text or len(text) < 20:
                return text
            
            # Если текст очень длинный, переводим по частям
            if len(text) > 4000:
                parts = []
                for i in range(0, len(text), 3000):
                    part = text[i:i+3000]
                    try:
                        translated = self.translator.translate(part)
                        parts.append(translated)
                    except:
                        parts.append(part)
                    time.sleep(1)  # Пауза между частями
                return ' '.join(parts)
            
            return self.translator.translate(text)
            
        except Exception as e:
            logger.error(f"❌ Ошибка перевода: {e}")
            return text
    
    async def create_post(self, news_item):
        """Создание поста с изображением"""
        try:
            loop = asyncio.get_event_loop()
            
            # Переводим заголовок
            logger.info("🔄 Перевод заголовка...")
            title_ru = await loop.run_in_executor(None, self.translate_text, news_item['title'])
            
            # Переводим текст
            logger.info(f"🔄 Перевод текста ({len(news_item['content'])} символов)...")
            content_ru = await loop.run_in_executor(None, self.translate_text, news_item['content'])
            
            # Если есть изображение, скачиваем его
            image_path = None
            if news_item.get('image'):
                logger.info(f"🖼️ Скачивание изображения...")
                image_path = await self.download_image(news_item['image'])
            
            # Формируем текст поста
            post_text = f"<b>{title_ru}</b>\n\n{content_ru}"
            
            # Добавляем ссылку на источник
            post_text += f"\n\n📰 <a href='{news_item['link']}'>Источник: {news_item['source']}</a>"
            
            # Ограничиваем длину для Telegram
            if len(post_text) > 4000:
                post_text = post_text[:4000] + "...\n\n📰 <a href='{news_item['link']}'>Читать полностью на источнике</a>"
            
            return {
                'type': 'photo' if image_path else 'text',
                'path': image_path,
                'caption': post_text,
                'text': post_text
            }
            
        except Exception as e:
            logger.error(f"❌ Ошибка создания поста: {e}")
            return None
    
    async def publish_post(self, post_data):
        """Публикация поста"""
        try:
            if post_data['type'] == 'photo' and post_data['path']:
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
                await self.bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=post_data['text'],
                    parse_mode='HTML',
                    disable_web_page_preview=False
                )
                logger.info("✅ Текстовый пост опубликован")
            
            return True
            
        except TelegramError as e:
            if "Too Many Requests" in str(e):
                logger.warning("⚠️ Лимит Telegram, жду 1 час...")
                await asyncio.sleep(3600)
            else:
                logger.error(f"❌ Ошибка Telegram: {e}")
            return False
        except Exception as e:
            logger.error(f"❌ Ошибка публикации: {e}")
            return False
    
    async def check_and_publish(self):
        """Основной цикл"""
        logger.info("=" * 60)
        logger.info("🔍 ПРОВЕРКА INFOBRICS")
        logger.info("=" * 60)
        
        if not self.can_post_now():
            logger.info("⏳ Достигнут лимит постов")
            return
        
        news_items = await self.fetch_all_news()
        
        if not news_items:
            logger.info("📭 Новых статей нет")
            return
        
        published = 0
        
        for item in news_items:
            if not self.can_post_now():
                logger.info("⏳ Лимит достигнут, останавливаюсь")
                break
            
            logger.info(f"\n📝 Публикую: {item['title']}")
            
            post = await self.create_post(item)
            
            if post:
                success = await self.publish_post(post)
                
                if success:
                    self.sent_links.add(item['link'])
                    self.save_json(SENT_LINKS_FILE, self.sent_links)
                    self.log_post(item['link'], item['title'])
                    published += 1
                    
                    if published < len(news_items):
                        logger.info(f"⏱ Пауза {MIN_POST_INTERVAL//60} минут до следующей статьи...")
                        await asyncio.sleep(MIN_POST_INTERVAL)
        
        logger.info(f"\n📊 Опубликовано статей: {published}")
    
    async def start(self):
        """Запуск"""
        logger.info("=" * 60)
        logger.info("🚀 NEWS BOT 7.0 - INFOBRICS ПАРСЕР")
        logger.info(f"📢 Канал: {CHANNEL_ID}")
        logger.info(f"⏱ Проверка: каждые {CHECK_INTERVAL//3600}ч")
        logger.info(f"🛡️ Лимит: {MAX_POSTS_PER_DAY} статей/день")
        logger.info("=" * 60)
        
        try:
            me = await self.bot.get_me()
            logger.info(f"✅ Бот @{me.username}")
        except Exception as e:
            logger.error(f"❌ Ошибка: {e}")
            return
        
        # Первая проверка
        await self.check_and_publish()
        
        # Планировщик
        self.scheduler.add_job(
            self.check_and_publish,
            'interval',
            seconds=CHECK_INTERVAL
        )
        self.scheduler.start()
        
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
