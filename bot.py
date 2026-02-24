"""
🤖 Telegram News Bot - Версия 6.0
ИНДИВИДУАЛЬНЫЕ ПАРСЕРЫ ДЛЯ КАЖДОГО САЙТА
ПОЛНЫЕ СТАТЬИ БЕЗ ОБРЫВАНИЯ
"""

import os
import logging
import feedparser
import re
import html
import requests
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Bot
from telegram.error import TelegramError
from deep_translator import GoogleTranslator
import asyncio
import json
from bs4 import BeautifulSoup
from newspaper import Article
import tempfile
from urllib.parse import urljoin, urlparse
import aiohttp
import trafilatura
from readability import Document

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

# RSS источники с индивидуальными настройками
RSS_FEEDS = [
    {
        'name': 'Global Research',
        'url': 'https://www.globalresearch.ca/feed',
        'enabled': True,
        'parser': 'globalresearch',  # Индивидуальный парсер
        'domain': 'globalresearch.ca'
    },
    {
        'name': 'InfoBrics',
        'url': 'https://infobrics.org/rss/en',
        'enabled': True,
        'parser': 'infobrics',  # Индивидуальный парсер
        'domain': 'infobrics.org'
    },
    {
        'name': 'RT News',
        'url': 'https://www.rt.com/rss/news',
        'enabled': True,
        'parser': 'rt',  # Индивидуальный парсер
        'domain': 'rt.com'
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
        self.last_post_time = None
    
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
        """Проверка лимитов"""
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
        
        return text.strip()
    
    # ========== ИНДИВИДУАЛЬНЫЕ ПАРСЕРЫ ==========
    
    def parse_globalresearch(self, url):
        """Парсер для Global Research"""
        try:
            logger.info(f"🌐 Парсинг Global Research: {url}")
            
            # Пробуем trafilatura (лучший для текста)
            downloaded = trafilatura.fetch_url(url)
            if downloaded:
                text = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
                if text and len(text) > 500:
                    logger.info(f"✅ Trafilatura: {len(text)} символов")
                    return text
            
            # Запасной вариант через readability
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
            response = requests.get(url, headers=headers, timeout=15)
            if response.status_code == 200:
                doc = Document(response.text)
                content_html = doc.summary()
                soup = BeautifulSoup(content_html, 'html.parser')
                text = soup.get_text(separator='\n\n', strip=True)
                if text and len(text) > 500:
                    logger.info(f"✅ Readability: {len(text)} символов")
                    return text
            
            return None
        except Exception as e:
            logger.error(f"❌ Ошибка Global Research: {e}")
            return None
    
    def parse_infobrics(self, url):
        """Специальный парсер для InfoBrics"""
        try:
            logger.info(f"🌐 Парсинг InfoBrics: {url}")
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
                'Connection': 'keep-alive',
            }
            
            response = requests.get(url, headers=headers, timeout=15)
            if response.status_code != 200:
                return None
            
            soup = BeautifulSoup(response.text, 'lxml')
            
            # Удаляем ВСЕ элементы навигации
            for element in soup.find_all(['nav', 'header', 'footer', 'aside']):
                element.decompose()
            
            # Удаляем все элементы с классами содержащими menu, nav, sidebar, widget
            for element in soup.find_all(class_=re.compile(r'(menu|nav|sidebar|widget|header|footer|banner|ad|comment)', re.I)):
                element.decompose()
            
            # Ищем основной контент - специфично для InfoBrics
            content = None
            
            # 1. Пробуем найти article
            article = soup.find('article')
            if article:
                content = article
            
            # 2. Пробуем div с классом content
            if not content:
                content = soup.find('div', class_=re.compile(r'content', re.I))
            
            # 3. Пробуем main
            if not content:
                content = soup.find('main')
            
            # 4. Если ничего не нашли, берем body
            if not content:
                content = soup.body
            
            if content:
                # Удаляем все ссылки навигации внутри контента
                for link in content.find_all('a', href=re.compile(r'(category|tag|archive|author)', re.I)):
                    link.decompose()
                
                # Получаем текст
                text = content.get_text(separator='\n\n', strip=True)
                
                # Находим начало статьи (обычно после заголовка)
                lines = text.split('\n\n')
                filtered_lines = []
                
                for line in lines:
                    # Пропускаем строки с навигацией
                    if re.search(r'(Home|About|Contact|Subscribe|Follow us|Share this|Tags|Categories)', line, re.I):
                        continue
                    # Пропускаем короткие строки (менее 30 символов) в начале
                    if len(filtered_lines) < 3 and len(line) < 30:
                        continue
                    filtered_lines.append(line)
                
                result = '\n\n'.join(filtered_lines)
                
                if len(result) > 300:
                    logger.info(f"✅ InfoBrics: {len(result)} символов")
                    return result
            
            return None
            
        except Exception as e:
            logger.error(f"❌ Ошибка InfoBrics: {e}")
            return None
    
    def parse_rt(self, url):
        """Парсер для RT"""
        try:
            logger.info(f"🌐 Парсинг RT: {url}")
            
            # Для RT хорошо работает newspaper3k
            article = Article(url)
            article.download()
            article.parse()
            
            if article.text and len(article.text) > 300:
                logger.info(f"✅ RT: {len(article.text)} символов")
                return article.text
            
            return None
        except Exception as e:
            logger.error(f"❌ Ошибка RT: {e}")
            return None
    
    def get_parser(self, parser_name):
        """Возвращает функцию парсера по имени"""
        parsers = {
            'globalresearch': self.parse_globalresearch,
            'infobrics': self.parse_infobrics,
            'rt': self.parse_rt
        }
        return parsers.get(parser_name, self.parse_globalresearch)
    
    def extract_article_text(self, url, feed_config):
        """Извлечение текста статьи через соответствующий парсер"""
        try:
            parser_name = feed_config.get('parser', 'globalresearch')
            parser_func = self.get_parser(parser_name)
            
            # Запускаем парсер
            text = parser_func(url)
            
            if text:
                # Очищаем текст
                text = self.clean_text(text)
                
                # НЕ обрезаем текст, а разбиваем на части если нужно
                return text
            
            return None
            
        except Exception as e:
            logger.error(f"❌ Ошибка извлечения: {e}")
            return None
    
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
            for entry in feed.entries[:2]:  # По 2 статьи из каждого источника
                link = entry.get('link', '')
                
                if link in self.sent_links:
                    logger.info(f"⏭️ Уже было: {link[:50]}...")
                    continue
                
                # Получаем полный текст статьи
                loop = asyncio.get_event_loop()
                full_text = await loop.run_in_executor(
                    None, 
                    self.extract_article_text, 
                    link, 
                    feed_config
                )
                
                if full_text and len(full_text) > 200:
                    # Получаем заголовок из RSS или из текста
                    title = entry.get('title', '')
                    
                    # Ищем изображение (пока пропускаем, добавим позже)
                    image = None
                    
                    news_items.append({
                        'source': source_name,
                        'title': title,
                        'content': full_text,
                        'link': link,
                        'image': image
                    })
                    
                    logger.info(f"📰 Добавлено: {title[:50]}... ({len(full_text)} символов)")
                else:
                    logger.warning(f"⚠️ Не удалось извлечь текст для {link}")
            
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
                await asyncio.sleep(10)
        
        # Удаляем дубликаты
        unique = []
        seen = set()
        for item in all_news:
            if item['link'] not in seen:
                seen.add(item['link'])
                unique.append(item)
        
        logger.info(f"📊 Найдено новых: {len(unique)}")
        return unique
    
    def split_long_text(self, text, max_length=4000):
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
                # Если один параграф слишком длинный, разбиваем его
                if len(para) > max_length:
                    words = para.split()
                    temp = ""
                    for word in words:
                        if len(temp) + len(word) + 1 <= max_length:
                            if temp:
                                temp += " " + word
                            else:
                                temp = word
                        else:
                            parts.append(temp)
                            temp = word
                    if temp:
                        current_part = temp
                    else:
                        current_part = ""
                else:
                    current_part = para
        
        if current_part:
            parts.append(current_part)
        
        return parts
    
    def translate_text(self, text):
        """Перевод текста с сохранением структуры"""
        try:
            if not text or len(text) < 20:
                return text
            
            # Разбиваем на абзацы для перевода
            paragraphs = text.split('\n\n')
            translated_paragraphs = []
            
            for para in paragraphs:
                if para.strip():
                    # Если абзац слишком длинный, разбиваем его
                    if len(para) > 4000:
                        sub_paras = [para[i:i+4000] for i in range(0, len(para), 4000)]
                        translated_sub = []
                        for sub in sub_paras:
                            try:
                                translated_sub.append(self.translator.translate(sub))
                            except:
                                translated_sub.append(sub)
                        translated_paragraphs.append(' '.join(translated_sub))
                    else:
                        try:
                            translated_paragraphs.append(self.translator.translate(para))
                        except:
                            translated_paragraphs.append(para)
                else:
                    translated_paragraphs.append('')
            
            return '\n\n'.join(translated_paragraphs)
            
        except Exception as e:
            logger.error(f"❌ Ошибка перевода: {e}")
            return text
    
    async def create_post(self, news_item):
        """Создание поста (возможно несколько сообщений)"""
        try:
            loop = asyncio.get_event_loop()
            
            # Переводим
            logger.info(f"🔄 Перевод статьи {len(news_item['content'])} символов...")
            translated = await loop.run_in_executor(None, self.translate_text, news_item['content'])
            
            # Разбиваем на части если нужно
            parts = self.split_long_text(translated)
            
            posts = []
            
            # Первая часть с заголовком
            title_ru = await loop.run_in_executor(None, self.translate_text, news_item['title'])
            first_part = f"<b>{title_ru}</b>\n\n{parts[0]}"
            
            # Добавляем ссылку на источник в конец первой части
            first_part += f"\n\n📰 <a href='{news_item['link']}'>Источник: {news_item['source']}</a>"
            
            posts.append({
                'type': 'text',
                'text': first_part
            })
            
            # Остальные части
            for part in parts[1:]:
                posts.append({
                    'type': 'text',
                    'text': part
                })
            
            logger.info(f"📦 Создано {len(posts)} частей")
            return posts
            
        except Exception as e:
            logger.error(f"❌ Ошибка создания поста: {e}")
            return None
    
    async def publish_post(self, post_data):
        """Публикация одного сообщения"""
        try:
            await self.bot.send_message(
                chat_id=CHANNEL_ID,
                text=post_data['text'],
                parse_mode='HTML',
                disable_web_page_preview=False
            )
            return True
        except TelegramError as e:
            if "Too Many Requests" in str(e):
                logger.warning("⚠️ Лимит Telegram, жду 1 час...")
                await asyncio.sleep(3600)
            else:
                logger.error(f"❌ Ошибка: {e}")
            return False
    
    async def check_and_publish(self):
        """Основной цикл"""
        logger.info("=" * 60)
        logger.info("🔍 ПРОВЕРКА НОВОСТЕЙ")
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
            
            posts = await self.create_post(item)
            
            if posts:
                success = True
                for i, post in enumerate(posts, 1):
                    if not success:
                        break
                    
                    logger.info(f"📎 Часть {i}/{len(posts)}")
                    success = await self.publish_post(post)
                    
                    if success and i < len(posts):
                        logger.info("⏱ Пауза 5 секунд между частями...")
                        await asyncio.sleep(5)
                
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
        logger.info("🚀 NEWS BOT 6.0 - ПОЛНЫЕ СТАТЬИ")
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
