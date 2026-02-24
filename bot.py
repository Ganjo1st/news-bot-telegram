"""
🤖 Telegram News Bot - Версия 7.2
С ЗАПАСНЫМ ПАРСЕРОМ (РАБОТАЕТ БЕЗ lxml)
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
    # Пробуем импортировать lxml, если нет - будем использовать html.parser
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
        
        # Декодируем HTML entities
        text = html.unescape(text)
        
        # Удаляем HTML теги
        text = re.sub(r'<[^>]+>', '', text)
        
        # Нормализуем переносы строк
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
    
    # ========== ПАРСЕР ДЛЯ INFOBRICS С ЗАПАСНЫМ ВАРИАНТОМ ==========
    
    def parse_with_regex(self, html_content):
        """Запасной парсер на регулярных выражениях (если нет BeautifulSoup)"""
        logger.info("🔄 Использую regex парсер")
        
        result = {
            'title': 'Без заголовка',
            'content': '',
            'image': None
        }
        
        # Ищем заголовок
        title_match = re.search(r'<div[^>]*class="[^"]*title[^"]*big[^"]*"[^>]*>(.*?)</div>', html_content, re.DOTALL)
        if title_match:
            title = re.sub(r'<[^>]+>', '', title_match.group(1))
            result['title'] = title.strip()
        
        # Ищем изображение
        img_match = re.search(r'<img[^>]*class="[^"]*article__image[^"]*"[^>]*src="([^"]+)"', html_content)
        if img_match:
            img_src = img_match.group(1)
            if img_src.startswith('/'):
                result['image'] = f"https://infobrics.org{img_src}"
            elif not img_src.startswith('http'):
                result['image'] = f"https://infobrics.org/{img_src}"
            else:
                result['image'] = img_src
        
        # Ищем текст статьи
        # Находим div с классом article__text
        article_match = re.search(r'<div[^>]*class="[^"]*article__text[^"]*"[^>]*>(.*?)</div>', html_content, re.DOTALL)
        if article_match:
            article_html = article_match.group(1)
            # Удаляем все HTML теги, оставляем текст
            article_text = re.sub(r'<[^>]+>', ' ', article_html)
            # Разбиваем на параграфы по пустым строкам
            paragraphs = re.split(r'\n\s*\n', article_text)
            clean_paragraphs = []
            for p in paragraphs:
                p = re.sub(r'\s+', ' ', p).strip()
                if len(p) > 20:
                    clean_paragraphs.append(p)
            
            result['content'] = '\n\n'.join(clean_paragraphs)
        
        return result
    
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
            
            # Если нет BeautifulSoup, используем regex
            if not HAS_BEAUTIFULSOUP:
                return self.parse_with_regex(response.text)
            
            # Используем BeautifulSoup с доступным парсером
            try:
                soup = BeautifulSoup(response.text, PARSER)
            except:
                # Если указанный парсер не работает, пробуем html.parser
                soup = BeautifulSoup(response.text, 'html.parser')
            
            # ===== 1. ЗАГОЛОВОК =====
            title = None
            # Пробуем разные варианты поиска заголовка
            title_elem = soup.find('div', class_=re.compile(r'title.*big'))
            if not title_elem:
                title_elem = soup.find('h1')
            if not title_elem:
                # Ищем любой большой заголовок
                title_elem = soup.find(['h1', 'h2'], class_=re.compile(r'title|head'))
            
            if title_elem:
                title = self.clean_text(title_elem.get_text())
                logger.info(f"✅ Заголовок: {title[:50]}...")
            else:
                title = "Без заголовка"
            
            # ===== 2. ИЗОБРАЖЕНИЕ =====
            image_url = None
            img_elem = soup.find('img', class_=re.compile(r'article.*image'))
            if not img_elem:
                # Ищем любое изображение в статье
                article_div = soup.find('div', class_=re.compile(r'article|docs'))
                if article_div:
                    img_elem = article_div.find('img')
            
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
            
            # Находим контейнер с текстом
            text_container = soup.find('div', class_=re.compile(r'article__text'))
            if not text_container:
                text_container = soup.find('div', class_=re.compile(r'article'))
            if not text_container:
                text_container = soup.find('div', class_=re.compile(r'docs__article'))
            if not text_container:
                # Ищем основной контент страницы
                text_container = soup.find('main') or soup.find('div', class_=re.compile(r'content'))
            
            if text_container:
                logger.info(f"✅ Найден контейнер с текстом")
                
                # Удаляем ненужные элементы
                for unwanted in text_container.find_all(['script', 'style', 'button', 'nav', 'footer']):
                    unwanted.decompose()
                
                # Собираем все параграфы
                paragraphs = []
                for p in text_container.find_all('p'):
                    p_text = self.clean_text(p.get_text())
                    if p_text and len(p_text) > 15:
                        # Проверяем, что это не служебный текст
                        lower_text = p_text.lower()
                        if not any(skip in lower_text for skip in ['subscribe', 'follow us', 'share this', 'tags:', 'category:']):
                            paragraphs.append(p_text)
                
                # Если не нашли p, пробуем получить текст напрямую
                if not paragraphs:
                    raw_text = text_container.get_text(separator='\n', strip=True)
                    lines = raw_text.split('\n')
                    for line in lines:
                        line = line.strip()
                        if len(line) > 30 and not any(skip in line.lower() for skip in ['subscribe', 'follow', 'share', 'tags']):
                            paragraphs.append(line)
                
                if paragraphs:
                    article_text = '\n\n'.join(paragraphs)
                    logger.info(f"✅ Собрано {len(paragraphs)} параграфов, всего {len(article_text)} символов")
            
            # Проверяем, что текст достаточно длинный
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
            import traceback
            traceback.print_exc()
            return None
    
    # ========== ОСТАЛЬНАЯ ЛОГИКА (без изменений) ==========
    
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
            for entry in feed.entries[:1]:  # Только 1 самая свежая статья
                link = entry.get('link', '')
                
                if link in self.sent_links:
                    logger.info(f"⏭️ Уже было: {link}")
                    continue
                
                # Получаем полный текст статьи
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
                
                await asyncio.sleep(5)  # Пауза между запросами
            
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
            
            post_text = f"<b>{title_ru}</b>\n\n{content_ru}"
            
            source_link = news_item['link']
            source_name = news_item['source']
            post_text += f"\n\n📰 <a href='{source_link}'>Источник: {source_name}</a>"
            
            if len(post_text) > 4096:
                post_text = post_text[:4000] + "...\n\n"
                post_text += f"📰 <a href='{source_link}'>Читать полностью на источнике</a>"
            
            image_path = None
            if news_item.get('image'):
                logger.info(f"🖼️ Скачивание изображения...")
                image_path = await self.download_image(news_item['image'])
            
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
                try:
                    os.unlink(post_data['path'])
                except:
                    pass
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
            if "Can't parse entities" in str(e):
                logger.error(f"❌ Ошибка HTML: {e}")
                try:
                    plain_text = re.sub(r'<[^>]+>', '', post_data.get('caption', post_data.get('text', '')))
                    await self.bot.send_message(
                        chat_id=CHANNEL_ID,
                        text=plain_text
                    )
                    logger.info("✅ Пост отправлен без форматирования")
                    return True
                except Exception as e2:
                    logger.error(f"❌ Не удалось отправить даже без HTML: {e2}")
            elif "Too Many Requests" in str(e):
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
            
            logger.info(f"\n📝 Публикую: {item['title'][:50]}...")
            
            post = await self.create_post(item)
            
            if post:
                success = await self.publish_post(post)
                
                if success:
                    self.sent_links.add(item['link'])
                    self.save_json(SENT_LINKS_FILE, self.sent_links)
                    self.log_post(item['link'], item['title'])
                    published += 1
                    
                    if published < len(news_items):
                        logger.info(f"⏱ Пауза {MIN_POST_INTERVAL//60} минут...")
                        await asyncio.sleep(MIN_POST_INTERVAL)
        
        logger.info(f"\n📊 Опубликовано статей: {published}")
    
    async def start(self):
        """Запуск"""
        logger.info("=" * 60)
        logger.info("🚀 NEWS BOT 7.2 - С ЗАПАСНЫМ ПАРСЕРОМ")
        logger.info(f"📢 Канал: {CHANNEL_ID}")
        logger.info(f"⏱ Проверка: каждые {CHECK_INTERVAL//3600}ч")
        logger.info(f"🛡️ Лимит: {MAX_POSTS_PER_DAY} статей/день")
        if HAS_BEAUTIFULSOUP:
            logger.info(f"✅ BeautifulSoup: да (парсер: {PARSER})")
        else:
            logger.info(f"⚠️ BeautifulSoup: нет (используется regex)")
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
