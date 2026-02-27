"""
🤖 Telegram News Bot - Версия 8.2
ОДНО СООБЩЕНИЕ: фото + текст до 1024 символов
Текст обрезается строго по окончании абзаца!
Для идеальной работы с Синхроботом Дзена
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
MAX_POSTS_PER_DAY = 20   # Чуть меньше лимита, чтобы был запас

# Несколько источников для разнообразия
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
        
        logger.info(f"📊 Загружено {len(self.sent_links)} ранее опубликованных ссылок")
        logger.info(f"📊 Загружено {len(self.posts_log)} записей в логе постов")
    
    def load_json(self, filename):
        """Загрузка JSON файла"""
        try:
            with open(filename, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return set(data) if filename == SENT_LINKS_FILE else data
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки {filename}: {e}")
            return set() if filename == SENT_LINKS_FILE else []
    
    def save_json(self, filename, data):
        """Сохранение JSON файла"""
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
        """Проверка всех лимитов"""
        # Проверка времени суток (не публикуем ночью)
        hour = datetime.now().hour
        if 23 <= hour or hour < 7:
            logger.info("🌙 Ночное время (23:00-07:00), пропускаю публикацию")
            return False
        
        # Проверка интервала между постами
        if self.posts_log:
            last_post = max(self.posts_log, key=lambda x: x['time'])
            last_time = datetime.fromisoformat(last_post['time'])
            time_diff = datetime.now() - last_time
            min_interval = timedelta(seconds=MIN_POST_INTERVAL)
            if time_diff < min_interval:
                logger.info(f"⏳ С последнего поста прошло {time_diff.seconds}с, нужно ждать {MIN_POST_INTERVAL}с")
                return False
        
        # Проверка дневного лимита
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
        # Оставляем только последние 100 записей
        if len(self.posts_log) > 100:
            self.posts_log = self.posts_log[-100:]
        self.save_json(POSTS_LOG_FILE, self.posts_log)
        logger.info(f"📝 Пост залогирован: {title[:50]}...")
    
    async def get_session(self):
        """Получение aiohttp сессии"""
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
        """Экранирование специальных символов для Telegram HTML"""
        if not text:
            return ""
        text = text.replace('&', '&amp;')
        text = text.replace('<', '&lt;')
        text = text.replace('>', '&gt;')
        return text
    
    def parse_article(self, url, source_name):
        """
        Универсальный парсер для разных источников
        Возвращает заголовок, текст и главное изображение
        """
        try:
            logger.info(f"🌐 Парсинг {source_name}: {url}")
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            
            response = requests.get(url, headers=headers, timeout=15)
            if response.status_code != 200:
                logger.error(f"❌ Ошибка загрузки: HTTP {response.status_code}")
                return None
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # === ЗАГОЛОВОК ===
            title = "Без заголовка"
            if 'infobrics' in url:
                title_elem = soup.find('div', class_=re.compile(r'title.*big')) or soup.find('h1')
            else:
                title_elem = soup.find('h1')
            
            if title_elem:
                title = self.clean_text(title_elem.get_text())
                logger.info(f"✅ Заголовок: {title[:50]}...")
            
            # === ГЛАВНОЕ ИЗОБРАЖЕНИЕ ===
            main_image = None
            # Пробуем найти изображение статьи
            img_elem = soup.find('img', class_=re.compile(r'article.*image')) or soup.find('img', class_=re.compile(r'featured'))
            if not img_elem:
                # Если не нашли, берем первое изображение в статье
                article_div = soup.find('div', class_=re.compile(r'article|post|content'))
                if article_div:
                    img_elem = article_div.find('img')
            
            if img_elem and img_elem.get('src'):
                img_src = img_elem['src']
                # Преобразуем относительный URL в абсолютный
                if img_src.startswith('/'):
                    domain = url.split('/')[2]
                    main_image = f"https://{domain}{img_src}"
                elif not img_src.startswith('http'):
                    domain = url.split('/')[2]
                    main_image = f"https://{domain}/{img_src}"
                else:
                    main_image = img_src
                logger.info(f"✅ Изображение: {main_image}")
            
            # === ТЕКСТ СТАТЬИ ===
            article_text = ""
            # Ищем контейнер с текстом
            text_container = soup.find('div', class_=re.compile(r'article__text|post-content|entry-content'))
            
            if text_container:
                # Удаляем ненужные элементы
                for unwanted in text_container.find_all(['script', 'style', 'button', 'nav', 'footer']):
                    unwanted.decompose()
                
                # Собираем все параграфы
                paragraphs = []
                for p in text_container.find_all('p'):
                    p_text = self.clean_text(p.get_text())
                    if p_text and len(p_text) > 15:
                        # Пропускаем служебный текст
                        lower_text = p_text.lower()
                        if not any(skip in lower_text for skip in ['subscribe', 'follow us', 'share this', 'tags:', 'category:']):
                            paragraphs.append(p_text)
                
                if paragraphs:
                    article_text = '\n\n'.join(paragraphs)
                    logger.info(f"✅ Собрано {len(paragraphs)} параграфов, {len(article_text)} символов")
            
            # Проверяем, что текст достаточно длинный
            if len(article_text) < 200:
                logger.warning(f"⚠️ Текст слишком короткий ({len(article_text)} символов)")
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
        """Получение новостей из RSS ленты"""
        try:
            feed_url = feed_config['url']
            source_name = feed_config['name']
            
            logger.info(f"🔄 {source_name}: {feed_url}")
            
            feed = feedparser.parse(feed_url)
            
            if feed.bozo:
                logger.error(f"❌ Ошибка RSS {source_name}: {feed.bozo_exception}")
                return []
            
            # Логируем общее количество статей в RSS
            logger.info(f"📰 В RSS всего {len(feed.entries)} статей")
            
            news_items = []
            # Берем только самую свежую статью
            for entry in feed.entries[:1]:
                link = entry.get('link', '')
                title = entry.get('title', 'Без заголовка')
                
                logger.info(f"  Статья: {title[:50]}... - {link}")
                
                if link in self.sent_links:
                    logger.info(f"  ⏭️ Уже опубликовано ранее")
                    continue
                
                logger.info(f"  🔄 Новая статья, начинаю парсинг...")
                
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
                    logger.info(f"  ✅ Статья успешно спарсена")
                else:
                    logger.warning(f"  ❌ Не удалось спарсить статью")
                
                # Пауза между запросами к сайту
                await asyncio.sleep(random.randint(3, 8))
            
            logger.info(f"📊 {source_name}: найдено {len(news_items)} новых статей")
            return news_items
            
        except Exception as e:
            logger.error(f"❌ Ошибка в fetch_news_from_feed: {e}")
            return []
    
    async def fetch_all_news(self):
        """Сбор новостей из всех источников"""
        all_news = []
        
        for feed in RSS_FEEDS:
            if feed['enabled']:
                news = await self.fetch_news_from_feed(feed)
                all_news.extend(news)
                # Случайная пауза между источниками
                await asyncio.sleep(random.randint(3, 8))
        
        # Перемешиваем новости из разных источников
        random.shuffle(all_news)
        
        logger.info(f"📊 ВСЕГО НАЙДЕНО НОВЫХ СТАТЕЙ: {len(all_news)}")
        return all_news
    
    async def download_image(self, url):
        """Скачивание изображения во временный файл"""
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
        """Перевод текста с английского на русский"""
        try:
            if not text or len(text) < 20:
                return text
            
            # Если текст очень длинный, переводим по частям
            if len(text) > 3000:
                parts = []
                for i in range(0, len(text), 2000):
                    part = text[i:i+2000]
                    try:
                        translated = self.translator.translate(part)
                        parts.append(translated)
                    except Exception as e:
                        logger.error(f"Ошибка перевода части: {e}")
                        parts.append(part)
                    # Случайная пауза между частями
                    time.sleep(random.uniform(0.5, 1.5))
                return ' '.join(parts)
            
            return self.translator.translate(text)
            
        except Exception as e:
            logger.error(f"❌ Ошибка перевода: {e}")
            return text
    
    def truncate_to_last_paragraph(self, text, max_length):
        """
        Обрезает текст строго по окончании последнего полного абзаца,
        который помещается в лимит. Никаких обрывов посередине!
        """
        if not text:
            return ""
        
        # Разбиваем на абзацы (по двойному переносу строки)
        paragraphs = text.split('\n\n')
        
        # Резервируем место для многоточия, если текст будет обрезан
        ELLIPSIS = "..."
        ellipsis_length = len(ELLIPSIS)
        
        result_paragraphs = []
        current_length = 0
        
        for i, para in enumerate(paragraphs):
            para_length = len(para)
            
            # Проверяем, поместится ли этот абзац целиком
            # +2 за \n\n между абзацами, но для первого абзаца это не нужно
            if i > 0:
                needed_length = current_length + 2 + para_length
            else:
                needed_length = current_length + para_length
            
            # Если абзац помещается, добавляем его
            if needed_length <= max_length:
                if i > 0:
                    current_length += 2  # Добавляем разделитель
                result_paragraphs.append(para)
                current_length += para_length
            else:
                # Если текущий абзац не помещается, проверяем особый случай:
                # это первый абзац и он уже слишком длинный
                if i == 0 and para_length > max_length:
                    # Обрезаем первый абзац, но сохраняем последнее предложение
                    # Находим последнюю точку в пределах лимита
                    truncated = para[:max_length]
                    last_dot = truncated.rfind('. ')
                    if last_dot > 0:
                        return truncated[:last_dot+1] + " " + ELLIPSIS
                    else:
                        # Если нет точки, ищем другой знак препинания
                        last_punct = max(
                            truncated.rfind('? '),
                            truncated.rfind('! '),
                            truncated.rfind('... ')
                        )
                        if last_punct > 0:
                            return truncated[:last_punct+1] + " " + ELLIPSIS
                        else:
                            # Совсем нет знаков препинания - обрезаем по словам
                            words = truncated.split()
                            result_words = []
                            word_length = 0
                            for word in words:
                                if word_length + len(word) + 1 <= max_length - ellipsis_length:
                                    result_words.append(word)
                                    word_length += len(word) + 1
                                else:
                                    break
                            return ' '.join(result_words) + " " + ELLIPSIS
                
                # Если это не первый абзац, просто прекращаем добавление
                break
        
        # Если мы добавили хотя бы один абзац
        if result_paragraphs:
            result = '\n\n'.join(result_paragraphs)
            
            # Проверяем, был ли текст обрезан (не все абзацы добавлены)
            if len(result_paragraphs) < len(paragraphs):
                result += "\n\n" + ELLIPSIS
            
            return result
        
        # Если ни один абзац не поместился (крайне редкий случай)
        # Берем первое предложение первого абзаца
        first_para = paragraphs[0]
        sentences = re.split(r'(?<=[.!?])\s+', first_para)
        
        result_sentences = []
        current_length = 0
        
        for sent in sentences:
            sent_length = len(sent)
            if current_length + sent_length <= max_length - ellipsis_length:
                result_sentences.append(sent)
                current_length += sent_length
            else:
                break
        
        if result_sentences:
            return ' '.join(result_sentences) + " " + ELLIPSIS
        else:
            # Если даже первое предложение не помещается - жестоко обрезаем
            return first_para[:max_length - ellipsis_length] + " " + ELLIPSIS
    
    async def create_single_post(self, news_item):
        """
        Создание ОДНОГО поста с фото и текстом (до 1024 символов)
        Текст обрезается строго по окончании абзаца!
        """
        try:
            loop = asyncio.get_event_loop()
            
            # Переводим с случайными паузами (имитация человека)
            logger.info("🔄 Перевод заголовка...")
            await asyncio.sleep(random.uniform(0.5, 2))
            title_ru = await loop.run_in_executor(None, self.translate_text, news_item['title'])
            
            logger.info(f"🔄 Перевод текста ({len(news_item['content'])} символов)...")
            await asyncio.sleep(random.uniform(1, 3))
            full_content_ru = await loop.run_in_executor(None, self.translate_text, news_item['content'])
            
            # Экранируем специальные символы
            title_ru_escaped = self.escape_html_for_telegram(title_ru)
            content_ru_escaped = self.escape_html_for_telegram(full_content_ru)
            
            # Скачиваем изображение
            image_path = None
            if news_item.get('main_image'):
                logger.info(f"🖼️ Скачивание изображения...")
                image_path = await self.download_image(news_item['main_image'])
            
            # ВАЖНО: Рассчитываем доступное место для текста
            # Заголовок с форматированием
            title_part = f"<b>{title_ru_escaped}</b>\n\n"
            title_length = len(title_part)
            
            # Ссылка на источник
            source_part = f"\n\n📰 <a href='{news_item['link']}'>Источник: {news_item['source']}</a>"
            source_length = len(source_part)
            
            # Доступно для текста (с запасом 10 символов)
            max_content_length = TELEGRAM_MAX_CAPTION - title_length - source_length - 10
            
            logger.info(f"📏 Лимиты: всего {TELEGRAM_MAX_CAPTION}, заголовок {title_length}, ссылка {source_length}, текст {max_content_length}")
            
            # Обрезаем текст с сохранением целостности абзацев
            truncated_content = self.truncate_to_last_paragraph(content_ru_escaped, max_content_length)
            
            # Финальный текст
            final_caption = f"<b>{title_ru_escaped}</b>\n\n{truncated_content}\n\n📰 <a href='{news_item['link']}'>Источник: {news_item['source']}</a>"
            
            # Финальная проверка длины
            final_length = len(final_caption)
            logger.info(f"📏 Итоговая длина: {final_length}/{TELEGRAM_MAX_CAPTION}")
            
            # Если все еще слишком длинно (редкий случай), обрезаем еще жестче
            if final_length > TELEGRAM_MAX_CAPTION:
                excess = final_length - TELEGRAM_MAX_CAPTION + 5
                # Обрезаем текст, но сохраняем последнее предложение
                words = truncated_content.split()
                while words and len(' '.join(words)) > len(truncated_content) - excess:
                    words.pop()
                truncated_content = ' '.join(words) + "..."
                
                final_caption = f"<b>{title_ru_escaped}</b>\n\n{truncated_content}\n\n📰 <a href='{news_item['link']}'>Источник: {news_item['source']}</a>"
                logger.info(f"📏 Повторная обрезка: {len(final_caption)}")
            
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
                # Если нет фото, отправляем только текст (на всякий случай)
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
            elif "Message caption is too long" in str(e):
                # Если подпись слишком длинная (редкий случай), обрезаем еще
                logger.warning("⚠️ Подпись слишком длинная, обрезаю...")
                plain_text = re.sub(r'<[^>]+>', '', post_data['caption'])
                plain_text = plain_text[:950] + "..."
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
        """Основной цикл проверки и публикации"""
        logger.info("=" * 60)
        logger.info("🔍 ПРОВЕРКА НОВОСТЕЙ (ОДИН ПОСТ НА СТАТЬЮ)")
        logger.info("=" * 60)
        
        if not self.can_post_now():
            logger.info("⏳ Нельзя публиковать сейчас (лимит или время)")
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
                        # Случайная пауза между постами (8-15 минут)
                        pause = MIN_POST_INTERVAL + random.randint(-120, 300)
                        logger.info(f"⏱ Пауза {pause//60} минут до следующей статьи...")
                        await asyncio.sleep(pause)
                else:
                    logger.error(f"❌ Не удалось опубликовать статью")
        
        logger.info(f"\n📊 ИТОГО ОПУБЛИКОВАНО: {published} статей")
    
    async def start(self):
        """Запуск бота"""
        logger.info("=" * 70)
        logger.info("🚀 NEWS BOT 8.2 - БЕЗОПАСНЫЙ РЕЖИМ ДЛЯ ДЗЕНА")
        logger.info(f"📢 Канал: {CHANNEL_ID}")
        logger.info(f"⏱ Проверка: каждые {CHECK_INTERVAL//3600}ч ({CHECK_INTERVAL}с)")
        logger.info(f"🛡️ Лимит: {MAX_POSTS_PER_DAY} статей/день")
        logger.info(f"📏 Лимит подписи: {TELEGRAM_MAX_CAPTION} символов")
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
        
        # Планировщик для регулярных проверок
        self.scheduler.add_job(
            self.check_and_publish,
            'interval',
            seconds=CHECK_INTERVAL,
            id='news_checker'
        )
        self.scheduler.start()
        logger.info(f"✅ Планировщик запущен")
        logger.info(f"⏰ Следующая проверка через {CHECK_INTERVAL//3600} часов")
        
        try:
            while True:
                await asyncio.sleep(60)
                logger.debug("🟢 Бот работает, ожидание...")
        except KeyboardInterrupt:
            logger.info("🛑 Бот остановлен пользователем")
            if self.session:
                await self.session.close()

async def main():
    """Точка входа"""
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
