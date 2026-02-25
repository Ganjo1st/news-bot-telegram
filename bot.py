"""
🤖 Telegram News Bot - Версия 7.7
ИСПРАВЛЕНО: убрана подпись под фото, заголовок только в тексте
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
TELEGRAM_MAX_CAPTION = 1024
TELEGRAM_MAX_MESSAGE = 4096

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
        """Парсинг статьи InfoBrics с получением ВСЕХ изображений"""
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
            
            # ВСЕ изображения в статье
            all_images = []
            text_container = soup.find('div', class_=re.compile(r'article__text')) or soup.find('div', class_=re.compile(r'article'))
            
            if text_container:
                for img in text_container.find_all('img'):
                    if img.get('src'):
                        img_src = img['src']
                        if img_src.startswith('/'):
                            img_url = f"https://infobrics.org{img_src}"
                        elif not img_src.startswith('http'):
                            img_url = f"https://infobrics.org/{img_src}"
                        else:
                            img_url = img_src
                        
                        if img_url != main_image:
                            all_images.append(img_url)
            
            # Текст статьи
            article_text = ""
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
            
            logger.info(f"✅ Найдено изображений: главное - {main_image}, дополнительных - {len(all_images)}")
            
            return {
                'title': title,
                'content': article_text,
                'main_image': main_image,
                'additional_images': all_images
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
                        'main_image': article_data.get('main_image'),
                        'additional_images': article_data.get('additional_images', [])
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
    
    def split_text_into_parts_with_images(self, text, images, max_length=4096):
        """
        Разбивает текст на части и вставляет изображения между ними
        Гарантирует, что каждая часть не превышает max_length
        """
        if not text:
            return []
        
        # Разбиваем на абзацы
        paragraphs = text.split('\n\n')
        
        parts = []
        current_part = ""
        current_part_length = 0
        images_copy = images.copy() if images else []
        
        for para in paragraphs:
            para_length = len(para)
            
            # Проверяем, не превысит ли добавление этого параграфа лимит
            if current_part_length + para_length + 2 > max_length and current_part:
                # Сохраняем текущую часть
                part_data = {'text': current_part.strip(), 'image': None}
                if images_copy:
                    part_data['image'] = images_copy.pop(0)
                parts.append(part_data)
                
                # Начинаем новую часть с этого параграфа
                current_part = para
                current_part_length = para_length
            else:
                # Добавляем параграф к текущей части
                if current_part:
                    current_part += "\n\n" + para
                    current_part_length += para_length + 2
                else:
                    current_part = para
                    current_part_length = para_length
        
        # Добавляем последнюю часть
        if current_part:
            part_data = {'text': current_part.strip(), 'image': None}
            if images_copy:
                part_data['image'] = images_copy.pop(0)
            parts.append(part_data)
        
        # Если остались изображения, добавляем их как отдельные части
        for img in images_copy:
            parts.append({'text': None, 'image': img})
        
        # Финальная проверка
        final_parts = []
        for part in parts:
            if part['text'] and len(part['text']) > max_length:
                part['text'] = part['text'][:max_length-3] + '...'
            final_parts.append(part)
        
        logger.info(f"📦 Текст разбит на {len(final_parts)} частей")
        return final_parts
    
    async def create_post(self, news_item):
        """Создание поста - заголовок ТОЛЬКО в первом текстовом сообщении"""
        try:
            loop = asyncio.get_event_loop()
            
            # Переводим заголовок
            logger.info("🔄 Перевод заголовка...")
            title_ru = await loop.run_in_executor(None, self.translate_text, news_item['title'])
            
            # Переводим текст
            logger.info(f"🔄 Перевод текста ({len(news_item['content'])} символов)...")
            content_ru = await loop.run_in_executor(None, self.translate_text, news_item['content'])
            
            # Экранируем
            title_ru = self.escape_html_for_telegram(title_ru)
            content_ru = self.escape_html_for_telegram(content_ru)
            
            # Скачиваем главное изображение
            main_image_path = None
            if news_item.get('main_image'):
                logger.info(f"🖼️ Скачивание главного изображения...")
                main_image_path = await self.download_image(news_item['main_image'])
            
            # Скачиваем дополнительные изображения (максимум 2)
            additional_images_paths = []
            for i, img_url in enumerate(news_item.get('additional_images', [])[:2]):
                logger.info(f"🖼️ Скачивание доп. изображения {i+1}...")
                img_path = await self.download_image(img_url)
                if img_path:
                    additional_images_paths.append(img_path)
            
            # Формируем полный текст (без заголовка - он будет в первом сообщении)
            full_text = f"{content_ru}\n\n📰 <a href='{news_item['link']}'>Источник: {news_item['source']}</a>"
            
            # Разбиваем текст на части с изображениями
            text_parts = self.split_text_into_parts_with_images(full_text, additional_images_paths)
            
            posts = []
            
            # 1. Сначала отправляем главное фото БЕЗ ПОДПИСИ
            if main_image_path:
                posts.append({
                    'type': 'photo',
                    'path': main_image_path,
                    'caption': None  # Нет подписи!
                })
                logger.info("📸 Добавлено главное фото (без подписи)")
            
            # 2. Затем отправляем первую текстовую часть С ЗАГОЛОВКОМ
            if text_parts and text_parts[0]['text']:
                first_part_text = f"<b>{title_ru}</b>\n\n{text_parts[0]['text']}"
                posts.append({
                    'type': 'text',
                    'text': first_part_text
                })
                logger.info(f"📝 Добавлена текстовая часть 1 (С ЗАГОЛОВКОМ)")
                
                # Если к первой части есть изображение, добавляем его после текста
                if text_parts[0].get('image'):
                    posts.append({'type': 'pause', 'duration': 3})
                    posts.append({
                        'type': 'photo',
                        'path': text_parts[0]['image'],
                        'caption': None  # Без подписи
                    })
            
            # 3. Остальные части (без заголовка)
            for i, part in enumerate(text_parts[1:], 2):
                if part['text']:
                    posts.append({'type': 'pause', 'duration': 3})
                    posts.append({
                        'type': 'text',
                        'text': part['text']
                    })
                    logger.info(f"📝 Добавлена текстовая часть {i}")
                
                if part.get('image'):
                    posts.append({'type': 'pause', 'duration': 3})
                    posts.append({
                        'type': 'photo',
                        'path': part['image'],
                        'caption': None  # Без подписи
                    })
            
            logger.info(f"📦 Всего сообщений для публикации: {len([p for p in posts if p['type'] != 'pause'])}")
            return posts
            
        except Exception as e:
            logger.error(f"❌ Ошибка создания поста: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    async def publish_post(self, posts):
        """Публикация нескольких сообщений с учетом пауз"""
        try:
            published_count = 0
            total_messages = len([p for p in posts if p['type'] != 'pause'])
            
            for i, post in enumerate(posts):
                try:
                    if post['type'] == 'pause':
                        logger.info(f"⏱ Пауза {post['duration']} секунд...")
                        await asyncio.sleep(post['duration'])
                        continue
                    
                    if post['type'] == 'photo':
                        with open(post['path'], 'rb') as photo:
                            if post.get('caption'):
                                # Если есть подпись, отправляем с подписью
                                await self.bot.send_photo(
                                    chat_id=CHANNEL_ID,
                                    photo=photo,
                                    caption=post['caption'],
                                    parse_mode='HTML'
                                )
                            else:
                                # Если нет подписи, отправляем просто фото
                                await self.bot.send_photo(
                                    chat_id=CHANNEL_ID,
                                    photo=photo
                                )
                        # Удаляем файл после отправки
                        try:
                            os.unlink(post['path'])
                        except:
                            pass
                        published_count += 1
                        logger.info(f"✅ Фото {published_count}/{total_messages} опубликовано")
                        
                    else:  # text
                        await self.bot.send_message(
                            chat_id=CHANNEL_ID,
                            text=post['text'],
                            parse_mode='HTML',
                            disable_web_page_preview=False
                        )
                        published_count += 1
                        logger.info(f"✅ Текст {published_count}/{total_messages} опубликован")
                    
                    # Пауза между сообщениями
                    if i < len(posts) - 1 and posts[i+1]['type'] != 'pause':
                        logger.info(f"⏱ Пауза 3 секунды...")
                        await asyncio.sleep(3)
                        
                except TelegramError as e:
                    logger.error(f"❌ Ошибка публикации сообщения {i+1}: {e}")
                    if "Can't parse entities" in str(e):
                        # Пробуем без HTML
                        if post['type'] == 'text':
                            plain_text = re.sub(r'<[^>]+>', '', post['text'])
                            await self.bot.send_message(
                                chat_id=CHANNEL_ID,
                                text=plain_text
                            )
                            published_count += 1
                            logger.info(f"✅ Сообщение отправлено без HTML")
                    elif "Message is too long" in str(e) and post['type'] == 'text':
                        # Обрезаем и пробуем снова
                        plain_text = re.sub(r'<[^>]+>', '', post['text'])
                        plain_text = plain_text[:3500] + "... (обрезано)"
                        await self.bot.send_message(
                            chat_id=CHANNEL_ID,
                            text=plain_text
                        )
                        published_count += 1
                        logger.info(f"✅ Сообщение обрезано и отправлено")
                    else:
                        raise e
            
            return published_count == total_messages
            
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
            
            logger.info(f"\n📝 Публикую статью: {item['title'][:70]}...")
            
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
                else:
                    logger.error(f"❌ Не удалось опубликовать статью")
        
        logger.info(f"\n📊 Опубликовано статей: {published}")
    
    async def start(self):
        """Запуск"""
        logger.info("=" * 70)
        logger.info("🚀 NEWS BOT 7.7 - БЕЗ ПОДПИСЕЙ ПОД ФОТО")
        logger.info(f"📢 Канал: {CHANNEL_ID}")
        logger.info(f"⏱ Проверка: каждые {CHECK_INTERVAL//3600}ч")
        logger.info(f"🛡️ Лимит: {MAX_POSTS_PER_DAY} статей/день")
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
