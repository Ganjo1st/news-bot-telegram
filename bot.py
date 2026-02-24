"""
🤖 Telegram News Bot - Мультиисточник
Безопасный бот для публикации новостей из нескольких RSS
Версия 2.1 - с автосозданием файла
"""

import os
import logging
import feedparser
import html
import re
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Bot
from telegram.error import TelegramError
from googletrans import Translator
import asyncio
import json

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Конфигурация из переменных окружения
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', '8767446234:AAGRz1sJfDtV321CpUBdI2sqGVDcWryGqcY')
CHANNEL_ID = os.getenv('CHANNEL_ID', '@Novikon_news')  # Замените на ваш Chat ID
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
        # СОЗДАЕМ ФАЙЛ АВТОМАТИЧЕСКИ, ЕСЛИ ЕГО НЕТ
        if not os.path.exists(SENT_LINKS_FILE):
            logger.info(f"📁 Создаю файл {SENT_LINKS_FILE}")
            with open(SENT_LINKS_FILE, 'w', encoding='utf-8') as f:
                json.dump([], f)
        
        self.bot = Bot(token=TELEGRAM_TOKEN)
        self.translator = Translator()
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
        """Удаляет HTML теги из текста"""
        if not text:
            return ""
        # Удаляем HTML теги
        text = re.sub(r'<[^>]+>', '', text)
        # Удаляем лишние пробелы
        text = re.sub(r'\s+', ' ', text)
        # Удаляем CDATA
        text = re.sub(r'<!\[CDATA\[|\]\]>', '', text)
        return text.strip()
    
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
            for entry in feed.entries[:5]:  # Берем последние 5 из каждого источника
                # Получаем ссылку
                link = entry.get('link', '')
                
                # Пропускаем если уже отправляли
                if link in self.sent_links:
                    continue
                
                # Получаем заголовок и очищаем от HTML
                title = self.clean_html(entry.get('title', 'Без заголовка'))
                
                # Получаем описание/содержание
                description = ''
                if hasattr(entry, 'description'):
                    description = self.clean_html(entry.description)
                elif hasattr(entry, 'summary'):
                    description = self.clean_html(entry.summary)
                elif hasattr(entry, 'content'):
                    description = self.clean_html(entry.content[0].value)
                
                # Получаем дату
                published = entry.get('published', '')
                
                news_items.append({
                    'source': source_name,
                    'title': title,
                    'description': description[:500],  # Обрезаем для перевода
                    'link': link,
                    'published': published
                })
            
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
        
        logger.info(f"📊 Всего новых новостей: {len(all_news)}")
        return all_news
    
    async def translate_text(self, text):
        """Перевод текста на русский"""
        try:
            if not text or len(text.strip()) < 10:
                return text
            
            # Ограничиваем длину для перевода
            if len(text) > 3000:
                text = text[:3000] + "..."
            
            # Используем Google Translate
            translated = await self.translator.translate(text, dest='ru', src='en')
            
            # Экранируем специальные символы для Telegram HTML
            result = translated.text
            result = result.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            
            return result
            
        except Exception as e:
            logger.error(f"❌ Ошибка перевода: {e}")
            # Возвращаем оригинал с экранированием
            return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    
    async def create_post_text(self, news_item):
        """Создание текста поста"""
        try:
            # Переводим заголовок и текст
            title_ru = await self.translate_text(news_item['title'])
            description_ru = await self.translate_text(news_item['description'])
            
            # Формируем пост
            post = f"<b>{title_ru}</b>\n\n"
            
            if description_ru and len(description_ru) > 20:
                post += f"{description_ru}\n\n"
            
            # Добавляем ссылку на источник с названием
            source_name = news_item['source']
            post += f"📰 <a href='{news_item['link']}'>Переведено из {source_name}</a>"
            
            # Telegram лимит: 4096 символов
            if len(post) > 4000:
                post = post[:4000] + "...\n\n📰 Читайте оригинал по ссылке выше"
            
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
                disable_web_page_preview=False  # Показываем превью ссылки
            )
            logger.info(f"✅ Пост опубликован в {CHANNEL_ID}")
            return True
            
        except TelegramError as e:
            logger.error(f"❌ Ошибка Telegram: {e}")
            return False
    
    async def check_and_publish(self):
        """Основная функция проверки и публикации"""
        logger.info("🔍 Запуск проверки новостей из всех источников...")
        
        # Получаем новости из всех источников
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
                    # Запоминаем, что отправили
                    self.sent_links.add(item['link'])
                    self.save_sent_links()
                    published_count += 1
                    
                    # Пауза между постами (чтобы не спамить)
                    logger.info(f"⏱ Ожидание 2 минуты перед следующим постом...")
                    await asyncio.sleep(120)  # 2 минуты
            else:
                logger.warning(f"⚠️ Не удалось создать пост для {item['link']}")
        
        logger.info(f"📊 Итого опубликовано: {published_count} постов")
    
    async def start(self):
        """Запуск бота"""
        logger.info("=" * 60)
        logger.info("🚀 MULTI-SOURCE NEWS BOT ЗАПУСКАЕТСЯ")
        logger.info(f"📢 Канал: {CHANNEL_ID}")
        logger.info(f"📡 Источники:")
        for feed in RSS_FEEDS:
            if feed['enabled']:
                logger.info(f"   - {feed['name']}: {feed['url']}")
        logger.info(f"⏱ Интервал: {CHECK_INTERVAL} секунд")
        logger.info("=" * 60)
        
        # Проверяем подключение к Telegram
        try:
            me = await self.bot.get_me()
            logger.info(f"✅ Бот @{me.username} авторизован")
            logger.info(f"🆔 Bot ID: {me.id}")
        except Exception as e:
            logger.error(f"❌ Ошибка подключения бота: {e}")
            logger.error("Проверьте TELEGRAM_TOKEN и подключение к интернету")
            return
        
        # Публикуем сразу при запуске
        logger.info("🔍 Первая проверка новостей...")
        await self.check_and_publish()
        
        # Настраиваем регулярную проверку
        self.scheduler.add_job(
            self.check_and_publish,
            'interval',
            seconds=CHECK_INTERVAL,
            id='news_checker',
            next_run_time=datetime.now()  # Запускаем сразу
        )
        self.scheduler.start()
        logger.info(f"✅ Планировщик запущен, проверка каждые {CHECK_INTERVAL} сек")
        
        # Держим бота запущенным
        try:
            while True:
                await asyncio.sleep(60)
        except KeyboardInterrupt:
            logger.info("🛑 Бот остановлен пользователем")

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
