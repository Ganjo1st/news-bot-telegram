"""
🤖 Telegram News Bot - Версия 7.4
С РАСШИРЕННЫМ ЛОГИРОВАНИЕМ
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
import tempfile
from urllib.parse import urljoin, urlparse
import aiohttp

# Настройка логирования с более подробным форматом
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Конфигурация
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', '8767446234:AAGRz1sJfDtV321CpUBdI2sqGVDcWryGqcY')
CHANNEL_ID = os.getenv('CHANNEL_ID', '@Novikon_news')
CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL', '7200'))  # 2 часа
MIN_POST_INTERVAL = 600  # 10 минут между постами
MAX_POSTS_PER_DAY = 24

# Только InfoBrics
RSS_FEEDS = [
    {
        'name': 'InfoBrics',
        'url': 'https://infobrics.org/rss/en',
        'enabled': True,
        'domain': 'infobrics.org'
    }
]

SENT_LINKS_FILE = 'sent_links.json'
POSTS_LOG_FILE = 'posts_log.json'

# Константы Telegram
TELEGRAM_MAX_CAPTION = 1024
TELEGRAM_MAX_MESSAGE = 4096

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
        self.sent_links = self.load_json(SENT_LINKS_FILE)
        self.posts_log = self.load_json(POSTS_LOG_FILE)
        self.session = None
        
        # Статистика
        self.stats = {
            'total_checks': 0,
            'total_published': 0,
            'last_check': None,
            'errors': []
        }
        
        logger.info(f"📊 Загружено {len(self.sent_links)} ранее опубликованных ссылок")
        logger.info(f"📊 Загружено {len(self.posts_log)} записей в логе постов")
    
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
            logger.debug(f"💾 Сохранено {filename}")
        except Exception as e:
            logger.error(f"❌ Ошибка сохранения {filename}: {e}")
    
    def can_post_now(self):
        """Проверка лимитов"""
        if not self.posts_log:
            logger.debug("✅ Нет истории постов, можно публиковать")
            return True
        
        last_post = max(self.posts_log, key=lambda x: x['time']) if self.posts_log else None
        if last_post:
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
        """Логирование опубликованного поста"""
        self.posts_log.append({
            'link': link,
            'title': title[:50],
            'time': datetime.now().isoformat()
        })
        if len(self.posts_log) > 100:
            self.posts_log = self.posts_log[-100:]
        self.save_json(POSTS_LOG_FILE, self.posts_log)
        self.stats['total_published'] += 1
        logger.info(f"📝 Пост залогирован: {title[:50]}...")
    
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
    
    def parse_infobrics(self, url):
        """Парсинг статьи InfoBrics"""
        try:
            logger.info(f"🌐 Парсинг InfoBrics: {url}")
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            
            response = requests.get(url, headers=headers, timeout=15)
            if response.status_code != 200:
                logger.error(f"❌ Ошибка загрузки: HTTP {response.status_code}")
                return None
            
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # Заголовок
            title = "Без заголовка"
            title_elem = soup.find('div', class_=re.compile(r'title.*big')) or soup.find('h1')
            if title_elem:
                title = self.clean_text(title_elem.get_text())
                logger.info(f"✅ Заголовок: {title[:50]}...")
            
            # Изображение
            image_url = None
            img_elem = soup.find('img', class_=re.compile(r'article.*image'))
            if img_elem and img_elem.get('src'):
                img_src = img_elem['src']
                if img_src.startswith('/'):
                    image_url = f"https://infobrics.org{img_src}"
                elif not img_src.startswith('http'):
                    image_url = f"https://infobrics.org/{img_src}"
                else:
                    image_url = img_src
                logger.info(f"✅ Изображение: {image_url}")
            
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
                    logger.info(f"✅ Собрано {len(paragraphs)} параграфов, {len(article_text)} символов")
            
            if len(article_text) < 200:
                logger.warning(f"⚠️ Текст слишком короткий ({len(article_text)} символов)")
                return None
            
            return {
                'title': title,
                'content': article_text,
                'image': image_url
            }
            
        except Exception as e:
            logger.error(f"❌ Ошибка парсинга: {e}")
            return None
    
    async def fetch_news_from_feed(self, feed_config):
        """Получение новостей из RSS"""
        try:
            feed_url = feed_config['url']
            source_name = feed_config['name']
            
            logger.info(f"🔄 Проверка RSS {source_name}: {feed_url}")
            
            feed = feedparser.parse(feed_url)
            
            if feed.bozo:
                logger.error(f"❌ Ошибка RSS {source_name}: {feed.bozo_exception}")
                return []
            
            # Логируем все статьи в RSS
            logger.info(f"📰 В RSS всего {len(feed.entries)} статей")
            
            news_items = []
            for i, entry in enumerate(feed.entries[:3]):  # Смотрим последние 3
                link = entry.get('link', '')
                title = entry.get('title', 'Без заголовка')
                
                logger.info(f"  {i+1}. {title[:50]}... - {link}")
                
                if link in self.sent_links:
                    logger.info(f"     ⏭️ Уже опубликовано ранее")
                    continue
                
                logger.info(f"     🔄 Новая статья, начинаю парсинг...")
                
                loop = asyncio.get_event_loop()
                article_data = await loop.run_in_executor(None, self.parse_infobrics, link)
                
                if article_data:
                    news_items.append({
                        'source': source_name,
                        'title': article_data['title'],
                        'content': article_data['content'],
                        'link': link,
                        'image': article_data.get('image')
                    })
                    logger.info(f"     ✅ Статья успешно спарсена")
                else:
                    logger.warning(f"     ❌ Не удалось спарсить статью")
                
                await asyncio.sleep(5)
            
            logger.info(f"📊 {source_name}: найдено {len(news_items)} новых статей")
            return news_items
            
        except Exception as e:
            logger.error(f"❌ Ошибка в fetch_news_from_feed: {e}")
            return []
    
    async def fetch_all_news(self):
        """Сбор новостей из всех источников"""
        self.stats['total_checks'] += 1
        self.stats['last_check'] = datetime.now().isoformat()
        
        logger.info("=" * 60)
        logger.info(f"🔍 ПРОВЕРКА #{self.stats['total_checks']}")
        logger.info("=" * 60)
        
        all_news = []
        
        for feed in RSS_FEEDS:
            if feed['enabled']:
                news = await self.fetch_news_from_feed(feed)
                all_news.extend(news)
        
        logger.info(f"📊 ВСЕГО НАЙДЕНО НОВЫХ СТАТЕЙ: {len(all_news)}")
        
        if len(all_news) == 0:
            logger.info("📭 Нет новых статей для публикации")
        else:
            for i, item in enumerate(all_news, 1):
                logger.info(f"  {i}. {item['title'][:70]}...")
        
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
                    logger.info(f"🖼️ Изображение скачано: {path}")
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
            
            if len(text) > 4000:
                parts = []
                for i in range(0, len(text), 3000):
                    part = text[i:i+3000]
                    try:
                        translated = self.translator.translate(part)
                        parts.append(translated)
                    except Exception as e:
                        logger.error(f"Ошибка перевода части: {e}")
                        parts.append(part)
                    time.sleep(1)
                return ' '.join(parts)
            
            return self.translator.translate(text)
            
        except Exception as e:
            logger.error(f"❌ Ошибка перевода: {e}")
            return text
    
    def split_long_text(self, text, max_length=TELEGRAM_MAX_MESSAGE):
        """Разбивает длинный текст на части"""
        if len(text) <= max_length:
            return [text]
        
        parts = []
        paragraphs = text.split('\n\n')
        current_part = ""
        
        for para in paragraphs:
            if len(current_part) + len(para) + 2 <= max_length:
                if current_part:
                    current_part += "\n\n" + para
                else:
                    current_part = para
            else:
                if current_part:
                    parts.append(current_part)
                current_part = para
        
        if current_part:
            parts.append(current_part)
        
        logger.info(f"✂️ Текст разбит на {len(parts)} частей")
        return parts
    
    async def create_post(self, news_item):
        """Создание поста"""
        try:
            loop = asyncio.get_event_loop()
            
            logger.info("🔄 Перевод заголовка...")
            title_ru = await loop.run_in_executor(None, self.translate_text, news_item['title'])
            
            logger.info(f"🔄 Перевод текста ({len(news_item['content'])} символов)...")
            content_ru = await loop.run_in_executor(None, self.translate_text, news_item['content'])
            
            title_ru = self.escape_html_for_telegram(title_ru)
            content_ru = self.escape_html_for_telegram(content_ru)
            
            # Скачиваем изображение
            image_path = None
            if news_item.get('image'):
                logger.info(f"🖼️ Скачивание изображения...")
                image_path = await self.download_image(news_item['image'])
            
            # Формируем полный текст
            full_text = f"<b>{title_ru}</b>\n\n{content_ru}"
            source_link = news_item['link']
            source_name = news_item['source']
            full_text += f"\n\n📰 <a href='{source_link}'>Источник: {source_name}</a>"
            
            # Разбиваем на части
            text_parts = self.split_long_text(full_text)
            
            posts = []
            
            # Если есть фото - отправляем отдельно
            if image_path:
                short_caption = f"<b>{title_ru[:100]}...</b>" if len(title_ru) > 100 else f"<b>{title_ru}</b>"
                posts.append({
                    'type': 'photo',
                    'path': image_path,
                    'caption': short_caption
                })
                logger.info("📸 Добавлено фото")
            
            # Добавляем текстовые части
            for i, part in enumerate(text_parts):
                posts.append({
                    'type': 'text',
                    'text': part
                })
            
            logger.info(f"📦 Всего сообщений для публикации: {len(posts)}")
            return posts
            
        except Exception as e:
            logger.error(f"❌ Ошибка создания поста: {e}")
            return None
    
    async def publish_post(self, posts):
        """Публикация нескольких сообщений"""
        try:
            published_count = 0
            
            for i, post in enumerate(posts):
                try:
                    if post['type'] == 'photo':
                        with open(post['path'], 'rb') as photo:
                            await self.bot.send_photo(
                                chat_id=CHANNEL_ID,
                                photo=photo,
                                caption=post['caption'],
                                parse_mode='HTML'
                            )
                        try:
                            os.unlink(post['path'])
                        except:
                            pass
                        logger.info(f"✅ Фото {i+1}/{len(posts)} опубликовано")
                        
                    else:
                        await self.bot.send_message(
                            chat_id=CHANNEL_ID,
                            text=post['text'],
                            parse_mode='HTML',
                            disable_web_page_preview=False
                        )
                        logger.info(f"✅ Текст {i+1}/{len(posts)} опубликован")
                    
                    published_count += 1
                    
                    if i < len(posts) - 1:
                        logger.info(f"⏱ Пауза 5 секунд...")
                        await asyncio.sleep(5)
                        
                except TelegramError as e:
                    logger.error(f"❌ Ошибка публикации сообщения {i+1}: {e}")
                    if "Can't parse entities" in str(e):
                        # Пробуем без HTML
                        plain_text = re.sub(r'<[^>]+>', '', post.get('caption', post.get('text', '')))
                        await self.bot.send_message(
                            chat_id=CHANNEL_ID,
                            text=plain_text
                        )
                        logger.info(f"✅ Сообщение {i+1} отправлено без HTML")
                        published_count += 1
            
            return published_count == len(posts)
            
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
        try:
            news_items = await self.fetch_all_news()
            
            if not news_items:
                logger.info("📭 Нет новых статей для публикации")
                return
            
            published = 0
            
            for item in news_items:
                if not self.can_post_now():
                    logger.info("⏳ Достигнут лимит постов, останавливаюсь")
                    break
                
                logger.info(f"\n📝 Начинаю публикацию статьи: {item['title'][:70]}...")
                
                posts = await self.create_post(item)
                
                if posts:
                    success = await self.publish_post(posts)
                    
                    if success:
                        self.sent_links.add(item['link'])
                        self.save_json(SENT_LINKS_FILE, self.sent_links)
                        self.log_post(item['link'], item['title'])
                        published += 1
                        logger.info(f"✅ Статья полностью опубликована")
                        
                        if published < len(news_items):
                            logger.info(f"⏱ Пауза {MIN_POST_INTERVAL//60} минут до следующей статьи...")
                            await asyncio.sleep(MIN_POST_INTERVAL)
                    else:
                        logger.error(f"❌ Не удалось опубликовать статью")
            
            logger.info(f"\n📊 ИТОГО ОПУБЛИКОВАНО: {published} статей")
            
        except Exception as e:
            logger.error(f"❌ Критическая ошибка в check_and_publish: {e}")
            import traceback
            traceback.print_exc()
    
    async def start(self):
        """Запуск"""
        logger.info("=" * 70)
        logger.info("🚀 NEWS BOT 7.4 - РАСШИРЕННОЕ ЛОГИРОВАНИЕ")
        logger.info(f"📢 Канал: {CHANNEL_ID}")
        logger.info(f"⏱ Проверка: каждые {CHECK_INTERVAL//3600}ч ({CHECK_INTERVAL}с)")
        logger.info(f"🛡️ Лимит: {MAX_POSTS_PER_DAY} статей/день")
        logger.info(f"📁 Ранее опубликовано: {len(self.sent_links)} ссылок")
        logger.info("=" * 70)
        
        try:
            me = await self.bot.get_me()
            logger.info(f"✅ Бот @{me.username} авторизован")
        except Exception as e:
            logger.error(f"❌ Ошибка подключения бота: {e}")
            return
        
        # Первая проверка
        logger.info("🚀 ЗАПУСК ПЕРВОЙ ПРОВЕРКИ")
        await self.check_and_publish()
        
        # Планировщик
        self.scheduler.add_job(
            self.check_and_publish,
            'interval',
            seconds=CHECK_INTERVAL,
            id='news_checker',
            next_run_time=datetime.now() + timedelta(seconds=CHECK_INTERVAL)
        )
        self.scheduler.start()
        logger.info(f"✅ Планировщик запущен")
        logger.info(f"⏰ Следующая проверка через {CHECK_INTERVAL//3600} часов")
        
        try:
            while True:
                await asyncio.sleep(60)
                logger.debug("🟢 Бот работает, ожидание...")
        except KeyboardInterrupt:
            logger.info("🛑 Бот остановлен")
            if self.session:
                await self.session.close()

async def main():
    bot = NewsBot()
    await bot.start()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Бот остановлен")
    except Exception as e:
        print(f"❌ Критическая ошибка: {e}")
        import traceback
        traceback.print_exc()
