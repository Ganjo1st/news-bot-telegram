"""
🤖 Telegram News Bot - Версия 8.0
ОДНО СООБЩЕНИЕ: фото + текст до 1024 символов
Для правильной работы с Синхроботом Дзена
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
TELEGRAM_MAX_CAPTION = 1024  # Лимит подписи к фото

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
        """Парсинг статьи InfoBrics - только главное изображение и текст"""
        try:
            logger.info(f"🌐 Парсинг InfoBrics: {url}")
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            
            response = requests.get(url, headers=headers, timeout=15)
            if response.status_code != 200:
                return None
            
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # Заголовок
            title = "Без заголовка"
            title_elem = soup.find('div', class_=re.compile(r'title.*big')) or soup.find('h1')
            if title_elem:
                title = self.clean_text(title_elem.get_text())
            
            # Главное изображение
            main_image = None
            img_elem = soup.find('img', class_=re.compile(r'article.*image'))
            if img_elem and img_elem.get('src'):
                img_src = img_elem['src']
                if img_src.startswith('/'):
                    main_image = f"https://infobrics.org{img_src}"
                elif not img_src.startswith('http'):
                    main_image = f"https://infobrics.org/{img_src}"
                else:
                    main_image = img_src
            
            # Текст статьи
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
            
            if len(article_text) < 200:
                return None
            
            logger.info(f"✅ Найдено: заголовок, главное изображение, текст {len(article_text)} символов")
            
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
                        'main_image': article_data.get('main_image')
                    })
                
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
            
            # Для длинных текстов переводим по частям
            if len(text) > 3000:
                parts = []
                for i in range(0, len(text), 2000):
                    part = text[i:i+2000]
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
    
    def truncate_to_last_paragraph(self, text, max_length):
        """
        Обрезает текст до последнего полного абзаца, который помещается в лимит
        Учитывает, что после текста нужно добавить ссылку на источник
        """
        if not text:
            return ""
        
        # Разбиваем на абзацы
        paragraphs = text.split('\n\n')
        
        # Резервируем место для ссылки на источник (примерно 50 символов)
        available_length = max_length - 50
        
        result_paragraphs = []
        current_length = 0
        
        for para in paragraphs:
            para_length = len(para)
            
            # Проверяем, поместится ли этот абзац
            if current_length + para_length + 2 <= available_length:
                result_paragraphs.append(para)
                current_length += para_length + 2
            else:
                # Если не помещается, останавливаемся
                break
        
        if not result_paragraphs:
            # Если ни один абзац не поместился, берем начало первого с обрезкой
            first_para = paragraphs[0]
            if len(first_para) > available_length - 3:
                return first_para[:available_length-3] + "..."
            return first_para
        
        return '\n\n'.join(result_paragraphs)
    
    async def create_single_post(self, news_item):
        """
        Создание ОДНОГО поста с фото и текстом (до 1024 символов)
        """
        try:
            loop = asyncio.get_event_loop()
            
            # Переводим заголовок (не используется в финальном посте, но для логов)
            logger.info("🔄 Перевод заголовка...")
            title_ru = await loop.run_in_executor(None, self.translate_text, news_item['title'])
            
            # Переводим текст
            logger.info(f"🔄 Перевод текста ({len(news_item['content'])} символов)...")
            full_content_ru = await loop.run_in_executor(None, self.translate_text, news_item['content'])
            
            # Экранируем
            title_ru_escaped = self.escape_html_for_telegram(title_ru)
            content_ru_escaped = self.escape_html_for_telegram(full_content_ru)
            
            # Скачиваем главное изображение
            image_path = None
            if news_item.get('main_image'):
                logger.info(f"🖼️ Скачивание изображения...")
                image_path = await self.download_image(news_item['main_image'])
            
            # ФОРМИРУЕМ ПОЛНЫЙ ТЕКСТ (сначала заголовок, потом текст, потом ссылка)
            full_text_with_source = f"<b>{title_ru_escaped}</b>\n\n{content_ru_escaped}\n\n📰 <a href='{news_item['link']}'>Источник: {news_item['source']}</a>"
            
            # Обрезаем текст до последнего полного абзаца, который помещается в лимит
            # Вычитаем длину заголовка и ссылки, которые уже точно будут
            title_length = len(f"<b>{title_ru_escaped}</b>\n\n")
            source_length = len(f"\n\n📰 <a href='{news_item['link']}'>Источник: {news_item['source']}</a>")
            
            # Максимальная длина для текста (без заголовка и ссылки)
            max_content_length = TELEGRAM_MAX_CAPTION - title_length - source_length - 5  # 5 про запас
            
            # Обрезаем текст с сохранением абзацев
            truncated_content = self.truncate_to_last_paragraph(content_ru_escaped, max_content_length)
            
            # Если текст был обрезан, добавляем многоточие
            if len(truncated_content) < len(content_ru_escaped):
                if truncated_content.endswith('...'):
                    pass  # уже есть
                else:
                    truncated_content += "..."
            
            # Финальный текст для подписи
            final_caption = f"<b>{title_ru_escaped}</b>\n\n{truncated_content}\n\n📰 <a href='{news_item['link']}'>Источник: {news_item['source']}</a>"
            
            # Проверяем длину (должна быть <= 1024)
            caption_length = len(final_caption)
            logger.info(f"📏 Длина подписи: {caption_length}/{TELEGRAM_MAX_CAPTION} символов")
            
            if caption_length > TELEGRAM_MAX_CAPTION:
                # Если все еще слишком длинно, обрезаем еще жестче
                excess = caption_length - TELEGRAM_MAX_CAPTION + 10
                truncated_content = truncated_content[:-excess] + "..."
                final_caption = f"<b>{title_ru_escaped}</b>\n\n{truncated_content}\n\n📰 <a href='{news_item['link']}'>Источник: {news_item['source']}</a>"
                logger.info(f"📏 Повторная обрезка: {len(final_caption)} символов")
            
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
        """Публикация ОДНОГО сообщения с фото и подписью"""
        try:
            if post_data['image_path']:
                with open(post_data['image_path'], 'rb') as photo:
                    await self.bot.send_photo(
                        chat_id=CHANNEL_ID,
                        photo=photo,
                        caption=post_data['caption'],
                        parse_mode='HTML'
                    )
                # Удаляем временный файл
                try:
                    os.unlink(post_data['image_path'])
                except:
                    pass
                logger.info("✅ Пост с фото и подписью опубликован")
                return True
            else:
                # Если нет фото, отправляем только текст
                await self.bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=post_data['caption'],
                    parse_mode='HTML',
                    disable_web_page_preview=False
                )
                logger.info("✅ Текстовый пост опубликован")
                return True
                
        except TelegramError as e:
            if "Too Many Requests" in str(e):
                logger.warning("⚠️ Лимит Telegram, жду 1 час...")
                await asyncio.sleep(3600)
            elif "Can't parse entities" in str(e):
                # Если HTML не работает, отправляем без форматирования
                logger.warning("⚠️ Ошибка HTML, отправляю без форматирования")
                plain_text = re.sub(r'<[^>]+>', '', post_data['caption'])
                await self.bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=plain_text
                )
                return True
            else:
                logger.error(f"❌ Ошибка Telegram: {e}")
            return False
        except Exception as e:
            logger.error(f"❌ Ошибка публикации: {e}")
            return False
    
    async def check_and_publish(self):
        """Основной цикл"""
        logger.info("=" * 60)
        logger.info("🔍 ПРОВЕРКА INFOBRICS (ОДИН ПОСТ НА СТАТЬЮ)")
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
            
            logger.info(f"\n📝 Публикую статью: {item['title'][:70]}...")
            
            post_data = await self.create_single_post(item)
            
            if post_data:
                success = await self.publish_post(post_data)
                
                if success:
                    self.sent_links.add(item['link'])
                    self.save_json(SENT_LINKS_FILE, self.sent_links)
                    self.log_post(item['link'], item['title'])
                    published += 1
                    
                    if published < len(news_items):
                        logger.info(f"⏱ Пауза {MIN_POST_INTERVAL//60} минут до следующей статьи...")
                        await asyncio.sleep(MIN_POST_INTERVAL)
                else:
                    logger.error(f"❌ Не удалось опубликовать статью")
        
        logger.info(f"\n📊 Опубликовано статей: {published}")
    
    async def start(self):
        """Запуск"""
        logger.info("=" * 70)
        logger.info("🚀 NEWS BOT 8.0 - ОДИН ПОСТ НА СТАТЬЮ")
        logger.info(f"📢 Канал: {CHANNEL_ID}")
        logger.info(f"⏱ Проверка: каждые {CHECK_INTERVAL//3600}ч")
        logger.info(f"🛡️ Лимит: {MAX_POSTS_PER_DAY} статей/день")
        logger.info(f"📏 Лимит подписи: {TELEGRAM_MAX_CAPTION} символов")
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
        logger.info(f"⏰ Следующая проверка через {CHECK_INTERVAL//3600} часов")
        
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
