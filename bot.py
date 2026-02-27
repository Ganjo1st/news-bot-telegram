"""
🤖 Telegram News Bot - Версия 8.3
АБСОЛЮТНО ПОЛНЫЕ АБЗАЦЫ - никаких обрывов!
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
MAX_POSTS_PER_DAY = 20   # Чуть меньше лимита

# Источники
RSS_FEEDS = [
    {
        'name': 'InfoBrics',
        'url': 'https://infobrics.org/rss/en',
        'enabled': True
    },
    {
        'name': 'Global Research',
        'url': 'https://www.globalresearch.ca/feed',
        'enabled': True
    }
]

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
        """Проверка всех лимитов"""
        hour = datetime.now().hour
        if 23 <= hour or hour < 7:
            logger.info("🌙 Ночное время, пропускаю")
            return False
        
        if self.posts_log:
            last_post = max(self.posts_log, key=lambda x: x['time'])
            last_time = datetime.fromisoformat(last_post['time'])
            time_diff = datetime.now() - last_time
            if time_diff < timedelta(seconds=MIN_POST_INTERVAL):
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
        text = html.unescape(text)
        text = re.sub(r'<[^>]+>', '', text)
        text = re.sub(r'\n\s*\n\s*\n', '\n\n', text)
        text = re.sub(r' +', ' ', text)
        return text.strip()
    
    def escape_html_for_telegram(self, text):
        """Экранирование для Telegram"""
        if not text:
            return ""
        text = text.replace('&', '&amp;')
        text = text.replace('<', '&lt;')
        text = text.replace('>', '&gt;')
        return text
    
    def parse_article(self, url, source_name):
        """Парсинг статьи"""
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
            if 'infobrics' in url:
                title_elem = soup.find('div', class_=re.compile(r'title.*big')) or soup.find('h1')
            else:
                title_elem = soup.find('h1')
            
            if title_elem:
                title = self.clean_text(title_elem.get_text())
            
            # Изображение
            main_image = None
            img_elem = soup.find('img', class_=re.compile(r'article.*image')) or soup.find('img', class_=re.compile(r'featured'))
            
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
            text_container = soup.find('div', class_=re.compile(r'article__text|post-content|entry-content'))
            
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
            logger.error(f"❌ Ошибка парсинга: {e}")
            return None
    
    async def fetch_news_from_feed(self, feed_config):
        """Получение новостей из RSS"""
        try:
            feed_url = feed_config['url']
            source_name = feed_config['name']
            
            logger.info(f"🔄 {source_name}")
            
            feed = feedparser.parse(feed_url)
            
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
                    self.parse_article, 
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
        """Сбор новостей"""
        all_news = []
        
        for feed in RSS_FEEDS:
            if feed['enabled']:
                news = await self.fetch_news_from_feed(feed)
                all_news.extend(news)
                await asyncio.sleep(random.randint(3, 8))
        
        random.shuffle(all_news)
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
    
    def build_caption_with_full_paragraphs(self, title, paragraphs, source_link, source_name, max_length=TELEGRAM_MAX_CAPTION):
        """
        Строит подпись, добавляя абзацы ТОЛЬКО ЦЕЛИКОМ.
        Если абзац не помещается - не добавляет его, а ставит многоточие.
        """
        # Базовая часть с заголовком
        title_part = f"<b>{title}</b>"
        base_length = len(title_part)
        
        # Добавляем заголовок
        result_parts = [title_part]
        current_length = base_length
        
        # Переменная для отслеживания, были ли добавлены абзацы
        added_any_paragraph = False
        
        for i, para in enumerate(paragraphs):
            # Определяем разделитель (для первого абзаца после заголовка - два переноса)
            if i == 0 and not added_any_paragraph:
                separator = "\n\n"
            else:
                separator = "\n\n"
            
            # Сколько займет этот абзац с разделителем
            para_with_sep = separator + para
            para_length = len(para_with_sep)
            
            # Проверяем, поместится ли абзац с учетом будущей ссылки
            # Резервируем место для ссылки (примерно 70 символов)
            estimated_final_length = current_length + para_length + 70
            
            if estimated_final_length <= max_length:
                # Абзац помещается - добавляем
                result_parts.append(para_with_sep)
                current_length += para_length
                added_any_paragraph = True
            else:
                # Абзац не помещается - останавливаемся и добавляем многоточие
                if added_any_paragraph:
                    result_parts.append("\n\n...")
                    current_length += 5  # длина "..."
                break
        
        # Добавляем ссылку на источник
        source_part = f"\n\n📰 <a href='{source_link}'>Источник: {source_name}</a>"
        result_parts.append(source_part)
        
        # Собираем финальный текст
        final_caption = ''.join(result_parts)
        
        # Проверка длины (для отладки)
        logger.info(f"📏 Построена подпись: {len(final_caption)}/{max_length}")
        
        return final_caption
    
    async def create_single_post(self, news_item):
        """
        Создание ОДНОГО поста с фото и текстом - только целые абзацы!
        """
        try:
            loop = asyncio.get_event_loop()
            
            # Переводим
            logger.info("🔄 Перевод заголовка...")
            await asyncio.sleep(random.uniform(0.5, 2))
            title_ru = await loop.run_in_executor(None, self.translate_text, news_item['title'])
            
            logger.info(f"🔄 Перевод текста ({len(news_item['content'])} символов)...")
            await asyncio.sleep(random.uniform(1, 3))
            content_ru = await loop.run_in_executor(None, self.translate_text, news_item['content'])
            
            # Экранируем
            title_escaped = self.escape_html_for_telegram(title_ru)
            content_escaped = self.escape_html_for_telegram(content_ru)
            
            # Разбиваем на абзацы
            paragraphs = content_escaped.split('\n\n')
            logger.info(f"📊 Статья содержит {len(paragraphs)} абзацев")
            
            # Скачиваем изображение
            image_path = None
            if news_item.get('main_image'):
                logger.info(f"🖼️ Скачивание изображения...")
                image_path = await self.download_image(news_item['main_image'])
            
            # СТРОИМ ПОДПИСЬ С ЦЕЛЫМИ АБЗАЦАМИ
            final_caption = self.build_caption_with_full_paragraphs(
                title=title_escaped,
                paragraphs=paragraphs,
                source_link=news_item['link'],
                source_name=news_item['source'],
                max_length=TELEGRAM_MAX_CAPTION
            )
            
            # Финальная проверка длины
            if len(final_caption) > TELEGRAM_MAX_CAPTION:
                logger.warning(f"⚠️ КАК ЭТО ВОЗМОЖНО? Подпись слишком длинная: {len(final_caption)}")
                # Экстренная обрезка - просто берем первые 1000 символов
                final_caption = final_caption[:1000] + "...\n\n📰 <a href='{news_item['link']}'>Источник</a>"
            
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
        """Публикация поста"""
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
        """Основной цикл"""
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
        """Запуск"""
        logger.info("=" * 70)
        logger.info("🚀 NEWS BOT 8.3 - ТОЛЬКО ЦЕЛЫЕ АБЗАЦЫ")
        logger.info(f"📢 Канал: {CHANNEL_ID}")
        logger.info(f"⏱ Проверка: каждые {CHECK_INTERVAL//3600}ч")
        logger.info(f"🛡️ Лимит: {MAX_POSTS_PER_DAY}/день")
        logger.info(f"📏 Лимит подписи: {TELEGRAM_MAX_CAPTION}")
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
