"""
🤖 Telegram News Bot - Версия 7.3
ИСПРАВЛЕНО: слишком длинная подпись к фото
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

# Пробуем импортировать BeautifulSoup с запасным парсером
try:
    from bs4 import BeautifulSoup
    HAS_BEAUTIFULSOUP = True
    try:
        import lxml
        PARSER = 'lxml'
        logging.info("✅ Используется парсер lxml")
    except ImportError:
        PARSER = 'html.parser'
        logging.info("⚠️ lxml не найден, используется html.parser")
except ImportError:
    HAS_BEAUTIFULSOUP = False
    logging.error("❌ BeautifulSoup не установлен!")

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
TELEGRAM_MAX_CAPTION = 1024  # Максимальная длина подписи к фото
TELEGRAM_MAX_MESSAGE = 4096   # Максимальная длина сообщения

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
        """Очистка текста от HTML тегов и лишних символов"""
        if not text:
            return ""
        
        text = html.unescape(text)
        text = re.sub(r'<[^>]+>', '', text)
        text = re.sub(r'\n\s*\n\s*\n', '\n\n', text)
        text = re.sub(r' +', ' ', text)
        
        return text.strip()
    
    def escape_html_for_telegram(self, text):
        """Экранирует специальные символы для Telegram HTML"""
        if not text:
            return ""
        
        text = text.replace('&', '&amp;')
        text = text.replace('<', '&lt;')
        text = text.replace('>', '&gt;')
        
        return text
    
    # ========== ПАРСЕР ДЛЯ INFOBRICS ==========
    
    def parse_infobrics(self, url):
        """Берет только: заголовок, картинку, текст статьи"""
        try:
            logger.info(f"🌐 Парсинг InfoBrics: {url}")
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            }
            
            response = requests.get(url, headers=headers, timeout=15)
            if response.status_code != 200:
                logger.error(f"❌ Ошибка загрузки: {response.status_code}")
                return None
            
            if not HAS_BEAUTIFULSOUP:
                return self.parse_with_regex(response.text)
            
            try:
                soup = BeautifulSoup(response.text, PARSER)
            except:
                soup = BeautifulSoup(response.text, 'html.parser')
            
            # Заголовок
            title = None
            title_elem = soup.find('div', class_=re.compile(r'title.*big'))
            if not title_elem:
                title_elem = soup.find('h1')
            
            if title_elem:
                title = self.clean_text(title_elem.get_text())
                logger.info(f"✅ Заголовок: {title[:50]}...")
            else:
                title = "Без заголовка"
            
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
            
            # Текст статьи
            article_text = ""
            text_container = soup.find('div', class_=re.compile(r'article__text'))
            if not text_container:
                text_container = soup.find('div', class_=re.compile(r'article'))
            
            if text_container:
                for unwanted in text_container.find_all(['script', 'style', 'button', 'nav', 'footer']):
                    unwanted.decompose()
                
                paragraphs = []
                for p in text_container.find_all('p'):
                    p_text = self.clean_text(p.get_text())
                    if p_text and len(p_text) > 15:
                        lower_text = p_text.lower()
                        if not any(skip in lower_text for skip in ['subscribe', 'follow us', 'share this', 'tags:', 'category:']):
                            paragraphs.append(p_text)
                
                if paragraphs:
                    article_text = '\n\n'.join(paragraphs)
                    logger.info(f"✅ Собрано {len(paragraphs)} параграфов, всего {len(article_text)} символов")
            
            if len(article_text) < 200:
                logger.warning(f"⚠️ Текст слишком короткий ({len(article_text)} символов)")
                return None
            
            return {
                'title': title,
                'content': article_text,
                'image': image_url
            }
            
        except Exception as e:
            logger.error(f"❌ Ошибка парсинга InfoBrics: {e}")
            return None
    
    def parse_with_regex(self, html_content):
        """Запасной парсер на регулярных выражениях"""
        logger.info("🔄 Использую regex парсер")
        
        result = {
            'title': 'Без заголовка',
            'content': '',
            'image': None
        }
        
        title_match = re.search(r'<div[^>]*class="[^"]*title[^"]*big[^"]*"[^>]*>(.*?)</div>', html_content, re.DOTALL)
        if title_match:
            title = re.sub(r'<[^>]+>', '', title_match.group(1))
            result['title'] = title.strip()
        
        img_match = re.search(r'<img[^>]*class="[^"]*article__image[^"]*"[^>]*src="([^"]+)"', html_content)
        if img_match:
            img_src = img_match.group(1)
            if img_src.startswith('/'):
                result['image'] = f"https://infobrics.org{img_src}"
            elif not img_src.startswith('http'):
                result['image'] = f"https://infobrics.org/{img_src}"
            else:
                result['image'] = img_src
        
        article_match = re.search(r'<div[^>]*class="[^"]*article__text[^"]*"[^>]*>(.*?)</div>', html_content, re.DOTALL)
        if article_match:
            article_html = article_match.group(1)
            article_text = re.sub(r'<[^>]+>', ' ', article_html)
            paragraphs = re.split(r'\n\s*\n', article_text)
            clean_paragraphs = []
            for p in paragraphs:
                p = re.sub(r'\s+', ' ', p).strip()
                if len(p) > 20:
                    clean_paragraphs.append(p)
            
            result['content'] = '\n\n'.join(clean_paragraphs)
        
        return result
    
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
            for entry in feed.entries[:1]:
                link = entry.get('link', '')
                
                if link in self.sent_links:
                    logger.info(f"⏭️ Уже было: {link}")
                    continue
                
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
                    logger.info(f"📰 Добавлено: {article_data['title'][:50]}...")
                
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
            
            if len(text) > 4000:
                parts = []
                for i in range(0, len(text), 3000):
                    part = text[i:i+3000]
                    try:
                        translated = self.translator.translate(part)
                        parts.append(translated)
                    except:
                        parts.append(part)
                    time.sleep(1)
                return ' '.join(parts)
            
            return self.translator.translate(text)
            
        except Exception as e:
            logger.error(f"❌ Ошибка перевода: {e}")
            return text
    
    def split_long_text(self, text, max_length=TELEGRAM_MAX_MESSAGE):
        """Разбивает длинный текст на части для Telegram"""
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
        
        return parts
    
    async def create_post(self, news_item):
        """Создание поста (фото + текст отдельно)"""
        try:
            loop = asyncio.get_event_loop()
            
            # Переводим
            logger.info("🔄 Перевод заголовка...")
            title_ru = await loop.run_in_executor(None, self.translate_text, news_item['title'])
            
            logger.info(f"🔄 Перевод текста ({len(news_item['content'])} символов)...")
            content_ru = await loop.run_in_executor(None, self.translate_text, news_item['content'])
            
            # Экранируем
            title_ru = self.escape_html_for_telegram(title_ru)
            content_ru = self.escape_html_for_telegram(content_ru)
            
            # Скачиваем изображение если есть
            image_path = None
            if news_item.get('image'):
                logger.info(f"🖼️ Скачивание изображения...")
                image_path = await self.download_image(news_item['image'])
            
            # Формируем текст поста (без фото)
            full_text = f"<b>{title_ru}</b>\n\n{content_ru}"
            
            # Добавляем ссылку на источник
            source_link = news_item['link']
            source_name = news_item['source']
            full_text += f"\n\n📰 <a href='{source_link}'>Источник: {source_name}</a>"
            
            # Разбиваем на части если нужно
            text_parts = self.split_long_text(full_text)
            
            posts = []
            
            # Если есть фото - отправляем его отдельно с короткой подписью
            if image_path:
                short_caption = f"<b>{title_ru}</b>\n\n📸 Иллюстрация к статье"
                if len(short_caption) > TELEGRAM_MAX_CAPTION:
                    short_caption = f"<b>{title_ru[:100]}...</b>"
                
                posts.append({
                    'type': 'photo',
                    'path': image_path,
                    'caption': short_caption
                })
            
            # Добавляем все текстовые части
            for i, part in enumerate(text_parts):
                posts.append({
                    'type': 'text',
                    'text': part,
                    'is_first': i == 0 and not image_path  # Только если нет фото
                })
            
            logger.info(f"📦 Создано {len(posts)} сообщений для публикации")
            return posts
            
        except Exception as e:
            logger.error(f"❌ Ошибка создания поста: {e}")
            return None
    
    async def publish_post(self, posts):
        """Публикация нескольких сообщений для одной статьи"""
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
                        # Удаляем временный файл
                        try:
                            os.unlink(post['path'])
                        except:
                            pass
                        logger.info(f"✅ Фото {i+1}/{len(posts)} опубликовано")
                        
                    else:  # text
                        await self.bot.send_message(
                            chat_id=CHANNEL_ID,
                            text=post['text'],
                            parse_mode='HTML',
                            disable_web_page_preview=False
                        )
                        logger.info(f"✅ Текст {i+1}/{len(posts)} опубликован")
                    
                    published_count += 1
                    
                    # Пауза между сообщениями одной статьи
                    if i < len(posts) - 1:
                        logger.info(f"⏱ Пауза 5 секунд...")
                        await asyncio.sleep(5)
                        
                except TelegramError as e:
                    if "Can't parse entities" in str(e):
                        # Если HTML не работает, отправляем без форматирования
                        plain_text = re.sub(r'<[^>]+>', '', post.get('caption', post.get('text', '')))
                        await self.bot.send_message(
                            chat_id=CHANNEL_ID,
                            text=plain_text
                        )
                        logger.info(f"✅ Сообщение {i+1} отправлено без HTML")
                        published_count += 1
                    else:
                        raise e
            
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
            
            logger.info(f"\n📝 Публикую статью: {item['title'][:50]}...")
            
            posts = await self.create_post(item)
            
            if posts:
                success = await self.publish_post(posts)
                
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
        logger.info("🚀 NEWS BOT 7.3 - ФОТО + ТЕКСТ ОТДЕЛЬНО")
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
