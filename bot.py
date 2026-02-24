"""
🤖 Telegram News Bot - Мультиисточник
Версия 3.0 - с полными статьями и без дублей
"""

import os
import logging
import feedparser
import re
import html
import requests
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Bot
from telegram.error import TelegramError
from deep_translator import GoogleTranslator
import asyncio
import json
from bs4 import BeautifulSoup  # Новая библиотека для парсинга

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Конфигурация из переменных окружения
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', '8767446234:AAGRz1sJfDtV321CpUBdI2sqGVDcWryGqcY')
CHANNEL_ID = os.getenv('CHANNEL_ID', '@Novikon_news')
CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL', '7200'))  # 2 часа

# Несколько RSS источников
RSS_FEEDS = [
    {
        'name': 'Global Research',
        'url': 'https://www.globalresearch.ca/feed',
        'enabled': True
    },
    {
        'name': 'InfoBrics',
        'url': 'https://infobrics.org/rss/en',
        'enabled': True
    },
    {
        'name': 'RT News',
        'url': 'https://www.rt.com/rss/news',
        'enabled': True
    }
]

# Файл для хранения отправленных ссылок
SENT_LINKS_FILE = 'sent_links.json'

class NewsBot:
    def __init__(self):
        # СОЗДАЕМ ФАЙЛ АВТОМАТИЧЕСКИ
        if not os.path.exists(SENT_LINKS_FILE):
            logger.info(f"📁 Создаю файл {SENT_LINKS_FILE}")
            with open(SENT_LINKS_FILE, 'w', encoding='utf-8') as f:
                json.dump([], f)
        
        self.bot = Bot(token=TELEGRAM_TOKEN)
        self.translator = GoogleTranslator(source='en', target='ru')
        self.scheduler = AsyncIOScheduler()
        self.sent_links = self.load_sent_links()
        
    def load_sent_links(self):
        """Загружает список отправленных ссылок из файла"""
        try:
            if os.path.exists(SENT_LINKS_FILE):
                with open(SENT_LINKS_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    logger.info(f"📂 Загружено {len(data)} отправленных ссылок")
                    return set(data)
        except Exception as e:
            logger.error(f"Ошибка загрузки sent_links: {e}")
        return set()
    
    def save_sent_links(self):
        """Сохраняет список отправленных ссылок в файл"""
        try:
            with open(SENT_LINKS_FILE, 'w', encoding='utf-8') as f:
                json.dump(list(self.sent_links), f, ensure_ascii=False, indent=2)
            logger.info(f"💾 Сохранено {len(self.sent_links)} ссылок")
        except Exception as e:
            logger.error(f"Ошибка сохранения sent_links: {e}")
    
    def clean_html(self, text):
        """Удаляет HTML теги и декодирует спецсимволы"""
        if not text:
            return ""
        
        # Декодируем HTML entities (&#8230; → ...)
        text = html.unescape(text)
        
        # Удаляем HTML теги
        text = re.sub(r'<[^>]+>', '', text)
        
        # Удаляем лишние пробелы и переносы
        text = re.sub(r'\s+', ' ', text)
        
        # Удаляем CDATA
        text = re.sub(r'<!\[CDATA\[|\]\]>', '', text)
        
        # Удаляем странные символы, оставляем только нормальный текст
        text = re.sub(r'[^\x00-\x7Fа-яА-ЯёЁ\s\.,!?:;()-]', '', text)
        
        return text.strip()
    
    def fetch_full_article(self, url):
        """Получает полный текст статьи со страницы"""
        try:
            logger.info(f"📄 Загружаю полную статью: {url}")
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            
            response = requests.get(url, headers=headers, timeout=10)
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, 'html.parser')
                
                # Удаляем ненужные элементы
                for element in soup.find_all(['script', 'style', 'nav', 'header', 'footer']):
                    element.decompose()
                
                # Ищем основной контент (разные селекторы для разных сайтов)
                content = None
                
                # Для Global Research
                if 'globalresearch' in url:
                    content = soup.find('div', class_='entry-content')
                    if not content:
                        content = soup.find('article')
                
                # Для InfoBrics
                elif 'infobrics' in url:
                    content = soup.find('div', class_='post-content')
                    if not content:
                        content = soup.find('div', class_='content')
                
                # Для RT
                elif 'rt.com' in url:
                    content = soup.find('div', class_='article__text')
                    if not content:
                        content = soup.find('div', class_='article-content')
                
                # Если нашли контент
                if content:
                    # Получаем текст
                    text = content.get_text(separator='\n\n', strip=True)
                    
                    # Ограничиваем длину
                    if len(text) > 3000:
                        text = text[:3000] + "..."
                    
                    logger.info(f"✅ Получено {len(text)} символов")
                    return text
                else:
                    logger.warning(f"⚠️ Не найден контент на странице")
            
            return None
            
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки статьи: {e}")
            return None
    
    async def fetch_news_from_feed(self, feed_config):
        """Получение новостей из конкретного RSS"""
        try:
            feed_url = feed_config['url']
            source_name = feed_config['name']
            
            logger.info(f"🔄 Проверка {source_name}: {feed_url}")
            
            # Парсим RSS
            feed = feedparser.parse(feed_url)
            
            if feed.bozo:  # Ошибка парсинга
                logger.error(f"❌ Ошибка RSS {source_name}: {feed.bozo_exception}")
                return []
            
            news_items = []
            for entry in feed.entries[:3]:  # Берем последние 3 из каждого источника
                # Получаем ссылку
                link = entry.get('link', '')
                
                # Пропускаем если уже отправляли (УЛУЧШЕННАЯ ПРОВЕРКА)
                if link in self.sent_links:
                    logger.info(f"⏭️ Пропускаем дубль: {link}")
                    continue
                
                # Получаем заголовок
                title = self.clean_html(entry.get('title', 'Без заголовка'))
                
                # Пробуем получить полную статью
                full_text = None
                
                # Для некоторых источников пробуем загрузить полную статью
                if 'globalresearch' in link or 'infobrics' in link:
                    loop = asyncio.get_event_loop()
                    full_text = await loop.run_in_executor(None, self.fetch_full_article, link)
                
                # Если не получили полный текст, используем описание из RSS
                if not full_text:
                    if hasattr(entry, 'description'):
                        full_text = self.clean_html(entry.description)
                    elif hasattr(entry, 'summary'):
                        full_text = self.clean_html(entry.summary)
                    elif hasattr(entry, 'content'):
                        full_text = self.clean_html(entry.content[0].value)
                    else:
                        full_text = ""
                
                news_items.append({
                    'source': source_name,
                    'title': title,
                    'content': full_text,
                    'link': link,
                })
                
                logger.info(f"📰 {source_name}: '{title[:50]}...'")
            
            logger.info(f"📰 {source_name}: найдено новых {len(news_items)}")
            return news_items
            
        except Exception as e:
            logger.error(f"❌ Ошибка получения новостей из {feed_config['name']}: {e}")
            return []
    
    async def fetch_all_news(self):
        """Получение новостей из всех источников"""
        all_news = []
        
        for feed_config in RSS_FEEDS:
            if not feed_config.get('enabled', True):
                continue
                
            news = await self.fetch_news_from_feed(feed_config)
            all_news.extend(news)
        
        # Удаляем дубли по ссылкам (на всякий случай)
        unique_news = []
        seen_links = set()
        
        for item in all_news:
            if item['link'] not in seen_links:
                seen_links.add(item['link'])
                unique_news.append(item)
        
        logger.info(f"📊 Всего уникальных новостей: {len(unique_news)}")
        return unique_news
    
    def translate_text(self, text):
        """Перевод текста на русский"""
        try:
            if not text or len(text.strip()) < 10:
                return text
            
            # Ограничиваем длину для перевода
            if len(text) > 4000:
                text = text[:4000] + "..."
            
            # Используем deep-translator
            translated = self.translator.translate(text)
            
            if translated:
                return translated
            
            return text
            
        except Exception as e:
            logger.error(f"❌ Ошибка перевода: {e}")
            return text
    
    async def create_post_text(self, news_item):
        """Создание текста поста"""
        try:
            # Переводим заголовок и текст
            loop = asyncio.get_event_loop()
            title_ru = await loop.run_in_executor(None, self.translate_text, news_item['title'])
            content_ru = await loop.run_in_executor(None, self.translate_text, news_item['content'])
            
            # Формируем пост
            post = f"<b>{title_ru}</b>\n\n"
            
            if content_ru and len(content_ru) > 50:
                # Разбиваем на абзацы для лучшей читаемости
                paragraphs = content_ru.split('\n\n')
                for para in paragraphs[:5]:  # Максимум 5 абзацев
                    if para.strip():
                        post += f"{para.strip()}\n\n"
            
            # Добавляем ссылку на источник (ИЗМЕНЕННАЯ ПОДПИСЬ)
            source_name = news_item['source']
            post += f"📰 <a href='{news_item['link']}'>Источник: {source_name}</a>"
            
            # Telegram лимит: 4096 символов
            if len(post) > 4000:
                post = post[:4000] + "...\n\n📰 <a href='{news_item['link']}'>Читать оригинал</a>"
            
            return post
            
        except Exception as e:
            logger.error(f"❌ Ошибка создания поста: {e}")
            return None
    
    async def publish_post(self, post_text):
        """Публикация в Telegram канал"""
        try:
            await self.bot.send_message(
                chat_id=CHANNEL_ID,
                text=post_text,
                parse_mode='HTML',
                disable_web_page_preview=False
            )
            logger.info(f"✅ Пост опубликован")
            return True
            
        except TelegramError as e:
            logger.error(f"❌ Ошибка Telegram: {e}")
            return False
    
    async def check_and_publish(self):
        """Основная функция проверки и публикации"""
        logger.info("🔍 Запуск проверки новостей из всех источников...")
        
        # Получаем новости
        news_items = await self.fetch_all_news()
        
        if not news_items:
            logger.info("📭 Новых новостей нет")
            return
        
        published_count = 0
        
        # Обрабатываем каждую новость
        for item in news_items:
            # Создаем пост
            post_text = await self.create_post_text(item)
            
            if post_text:
                # Публикуем
                success = await self.publish_post(post_text)
                
                if success:
                    # Запоминаем ссылку
                    self.sent_links.add(item['link'])
                    self.save_sent_links()
                    published_count += 1
                    
                    # Пауза между постами
                    if len(news_items) > 1 and published_count < len(news_items):
                        logger.info(f"⏱ Ожидание 3 минуты...")
                        await asyncio.sleep(180)  # 3 минуты
            else:
                logger.warning(f"⚠️ Не удалось создать пост")
        
        logger.info(f"📊 Опубликовано: {published_count} постов")
    
    async def start(self):
        """Запуск бота"""
        logger.info("=" * 60)
        logger.info("🚀 NEWS BOT 3.0 ЗАПУСКАЕТСЯ")
        logger.info(f"📢 Канал: {CHANNEL_ID}")
        logger.info(f"📡 Источники:")
        for feed in RSS_FEEDS:
            if feed['enabled']:
                logger.info(f"   - {feed['name']}")
        logger.info("=" * 60)
        
        # Проверяем подключение к Telegram
        try:
            me = await self.bot.get_me()
            logger.info(f"✅ Бот @{me.username} авторизован")
        except Exception as e:
            logger.error(f"❌ Ошибка подключения бота: {e}")
            return
        
        # Первая проверка
        logger.info("🔍 Первая проверка...")
        await self.check_and_publish()
        
        # Планировщик
        self.scheduler.add_job(
            self.check_and_publish,
            'interval',
            seconds=CHECK_INTERVAL,
            id='news_checker'
        )
        self.scheduler.start()
        logger.info(f"✅ Следующая проверка через {CHECK_INTERVAL//3600} часов")
        
        # Держим бота запущенным
        try:
            while True:
                await asyncio.sleep(60)
        except KeyboardInterrupt:
            logger.info("🛑 Бот остановлен")

async def main():
    bot = NewsBot()
    await bot.start()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Бот остановлен")
