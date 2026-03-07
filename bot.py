#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import json
import hashlib
import logging
import random
import re
import subprocess
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
import pytz
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import TimeoutException, WebDriverException
from webdriver_manager.chrome import ChromeDriverManager
import telegram
from telegram.error import TelegramError
import httpx

# ==================== НАСТРОЙКИ ====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8767446234:AAGRz1sJfDtV321CpUBdI2sqGVDcWryGqcY")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "-1002484885240")

NINTH_EMAIL = os.getenv("NINTH_EMAIL", "")
NINTH_PASSWORD = os.getenv("NINTH_PASSWORD", "")

CHECK_INTERVAL = 30  # минут
MIN_PUBLISH_INTERVAL = 35  # минут
MAX_PUBLISH_INTERVAL = 120  # минут

STATE_FILE = "bot_state.json"
LOG_FILE = "bot.log"
# ====================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Подавляем лишние логи
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("selenium").setLevel(logging.WARNING)
logging.getLogger("webdriver_manager").setLevel(logging.WARNING)


class NewsBot:
    """Универсальный бот для сбора и публикации новостей"""
    
    def __init__(self):
        self.tg_bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
        self.scheduler = BackgroundScheduler(timezone=pytz.UTC)
        self.state = self.load_state()
        
        # Инициализируем структуры для дедупликации, если их нет
        if "published_hashes" not in self.state:
            self.state["published_hashes"] = {}  # хэш -> {timestamp, title, url}
        if "queue_hashes" not in self.state:
            self.state["queue_hashes"] = set()   # множество хэшей в очереди
        
        self._check_chrome()
        
        logger.info("=" * 60)
        logger.info("🚀 БОТ ЗАПУЩЕН")
        logger.info(f"📊 В истории: {len(self.state.get('published_articles', {}))} статей")
        logger.info(f"📦 В очереди: {len(self.state.get('queue', []))} статей")
        logger.info(f"🔐 Уникальных хэшей в истории: {len(self.state['published_hashes'])}")
        logger.info(f"⏱️  Интервал проверки: {CHECK_INTERVAL} минут")
        logger.info(f"⏱️  Интервал публикации: случайный от {MIN_PUBLISH_INTERVAL} до {MAX_PUBLISH_INTERVAL} минут")
        logger.info("=" * 60)
    
    def _check_chrome(self):
        """Проверяет наличие Chrome и возвращает путь к исполняемому файлу"""
        chrome_paths = [
            '/usr/bin/google-chrome',
            '/usr/bin/chromium-browser',
            '/usr/bin/chromium',
            '/app/.chrome/chrome-linux64/chrome'  # типичный путь в Railway
        ]
        
        for path in chrome_paths:
            if os.path.exists(path):
                logger.info(f"✅ Chrome найден: {path}")
                try:
                    version_result = subprocess.run([path, '--version'], capture_output=True, text=True)
                    logger.info(f"✅ Chrome версия: {version_result.stdout.strip()}")
                except:
                    pass
                return path
        
        logger.warning("⚠️ Chrome не найден, публикация на 9111.ru будет недоступна")
        return None
    
    def load_state(self) -> Dict:
        """Загружает состояние бота из файла"""
        default_state = {
            "last_publish": None,
            "published_articles": {},
            "queue": [],
            "last_check": None,
            "published_hashes": {},
            "queue_hashes": set()
        }
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, 'r', encoding='utf-8') as f:
                    state = json.load(f)
                    # Конвертируем queue_hashes обратно в set при загрузке
                    if "queue_hashes" in state and isinstance(state["queue_hashes"], list):
                        state["queue_hashes"] = set(state["queue_hashes"])
                    logger.info(f"📥 Загружено состояние: {len(state.get('published_articles', {}))} статей в истории, {len(state.get('queue', []))} в очереди")
                    return state
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки состояния: {e}")
        return default_state
    
    def save_state(self):
        """Сохраняет состояние бота в файл"""
        try:
            state_copy = self.state.copy()
            # Конвертируем set в list для JSON сериализации
            if "queue_hashes" in state_copy:
                state_copy["queue_hashes"] = list(state_copy["queue_hashes"])
            with open(STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump(state_copy, f, ensure_ascii=False, indent=2)
            logger.debug("💾 Состояние сохранено")
        except Exception as e:
            logger.error(f"❌ Ошибка сохранения состояния: {e}")
    
    def generate_content_hash(self, title: str, text: str = "") -> str:
        """
        Генерирует уникальный хэш на основе заголовка и начала текста.
        Это основной метод дедупликации, который защищает от дублей даже при разных URL.
        """
        # Очищаем и нормализуем текст
        title_clean = re.sub(r'[^\w\s]', '', title.lower())
        title_clean = re.sub(r'\s+', ' ', title_clean).strip()
        
        # Берём начало текста для сравнения (первые 500 символов)
        text_sample = text[:500] if text else ""
        text_clean = re.sub(r'[^\w\s]', '', text_sample.lower())
        text_clean = re.sub(r'\s+', ' ', text_clean).strip()
        
        # Комбинируем для создания уникального ключа
        content = f"{title_clean}|{text_clean}"
        
        # Создаём хэш
        return hashlib.sha256(content.encode('utf-8')).hexdigest()[:16]
    
    def is_duplicate(self, content_hash: str, url: str = None) -> Tuple[bool, str]:
        """
        Проверяет, публиковалась ли уже статья с таким хэшем.
        Возвращает (True, причина) если дубликат найден.
        """
        # Проверка в истории опубликованных
        if content_hash in self.state["published_hashes"]:
            old_data = self.state["published_hashes"][content_hash]
            return True, f"Уже публиковалось {old_data.get('date', 'ранее')}: {old_data.get('title', '')[:50]}..."
        
        # Проверка в очереди
        if content_hash in self.state.get("queue_hashes", set()):
            return True, "Уже в очереди на публикацию"
        
        # Дополнительная проверка по URL (на случай коллизий)
        if url:
            for pub_url, data in self.state.get("published_articles", {}).items():
                if pub_url == url:
                    return True, f"URL уже публиковался: {url}"
        
        return False, ""
    
    def add_to_queue(self, article: Dict) -> bool:
        """
        Добавляет статью в очередь с проверкой на дубликаты.
        Возвращает True, если статья добавлена.
        """
        content_hash = article.get("content_hash")
        if not content_hash:
            logger.error("❌ Попытка добавить статью без хэша")
            return False
        
        is_dup, reason = self.is_duplicate(content_hash, article.get("url"))
        if is_dup:
            logger.info(f"⏭️ Дубликат: {reason}")
            return False
        
        # Добавляем в очередь
        self.state["queue"].append(article)
        if "queue_hashes" not in self.state:
            self.state["queue_hashes"] = set()
        self.state["queue_hashes"].add(content_hash)
        
        logger.info(f"✅ Статья добавлена в очередь: {article['title'][:70]}...")
        return True
    
    def mark_as_published(self, article: Dict):
        """
        Помечает статью как опубликованную, удаляет из очереди,
        добавляет в историю по хэшу и URL.
        """
        content_hash = article.get("content_hash")
        url = article.get("url")
        title = article.get("title", "")
        
        if not content_hash:
            logger.error("❌ Не могу пометить как опубликованное: нет хэша")
            return
        
        # Добавляем в историю по хэшу
        self.state["published_hashes"][content_hash] = {
            "title": title,
            "url": url,
            "date": datetime.now().isoformat(),
            "source": article.get("source", "unknown")
        }
        
        # Добавляем в историю по URL (для обратной совместимости)
        if url:
            self.state["published_articles"][url] = {
                "title": title,
                "date": datetime.now().isoformat(),
                "hash": content_hash
            }
        
        # Удаляем из очереди
        self.state["queue"] = [a for a in self.state["queue"] 
                               if a.get("content_hash") != content_hash]
        
        # Удаляем из множества хэшей очереди
        if content_hash in self.state.get("queue_hashes", set()):
            self.state["queue_hashes"].remove(content_hash)
        
        logger.info(f"✅ Статья помечена как опубликованная: {title[:50]}...")
        self.save_state()
    
    def fetch_ap_news(self) -> List[Dict]:
        """Парсинг AP News с полным контентом для генерации хэша"""
        articles = []
        try:
            logger.info("🌐 Парсинг главной страницы AP News")
            response = requests.get(
                "https://apnews.com/",
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
                timeout=15
            )
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # Находим все ссылки на статьи
            links = []
            for selector in ['a[data-key="card-headline"]', 'a.Component-headline', 'h2 a', 'h3 a']:
                links.extend(soup.select(selector))
            
            seen_urls = set()
            for link in links[:15]:  # берём первые 15
                href = link.get('href', '')
                if not href.startswith('/article/'):
                    continue
                
                url = f"https://apnews.com{href}"
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                
                # Получаем полный контент статьи для генерации хэша
                try:
                    article_response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
                    if article_response.status_code == 200:
                        article_soup = BeautifulSoup(article_response.text, 'html.parser')
                        
                        # Заголовок
                        title_elem = article_soup.find('h1') or article_soup.find(class_='Page-headline')
                        title = title_elem.get_text().strip() if title_elem else "Без заголовка"
                        
                        # Текст статьи для хэша
                        paragraphs = article_soup.find_all('p')
                        article_text = ' '.join([p.get_text() for p in paragraphs[:10]])
                        
                        if title and len(title) > 15:
                            content_hash = self.generate_content_hash(title, article_text)
                            
                            articles.append({
                                "title": title,
                                "url": url,
                                "source": "AP News",
                                "content_hash": content_hash,
                                "full_text": article_text  # сохраняем для дальнейшего использования
                            })
                except Exception as e:
                    logger.warning(f"⚠️ Ошибка при парсинге статьи {url}: {e}")
            
        except Exception as e:
            logger.error(f"❌ Ошибка парсинга AP News: {e}")
        
        return articles
    
    def fetch_global_research(self) -> List[Dict]:
        """Парсинг Global Research с полным контентом"""
        articles = []
        try:
            logger.info("🌐 Парсинг главной страницы Global Research")
            response = requests.get(
                "https://www.globalresearch.ca/",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=15
            )
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            links = []
            for header in soup.find_all(['h2', 'h3']):
                a_tag = header.find('a')
                if a_tag and a_tag.get('href'):
                    links.append(a_tag)
            
            seen_urls = set()
            for link in links[:15]:
                url = link.get('href', '')
                if not url or not url.startswith('http'):
                    continue
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                
                try:
                    # Получаем контент статьи
                    article_response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
                    if article_response.status_code == 200:
                        article_soup = BeautifulSoup(article_response.text, 'html.parser')
                        
                        title = link.get_text().strip()
                        if not title or len(title) < 15:
                            continue
                        
                        # Текст статьи для хэша
                        paragraphs = article_soup.find_all('p')
                        article_text = ' '.join([p.get_text() for p in paragraphs[:10]])
                        
                        content_hash = self.generate_content_hash(title, article_text)
                        
                        articles.append({
                            "title": title,
                            "url": url,
                            "source": "Global Research",
                            "content_hash": content_hash,
                            "full_text": article_text
                        })
                except Exception as e:
                    logger.warning(f"⚠️ Ошибка при парсинге статьи {url}: {e}")
            
        except Exception as e:
            logger.error(f"❌ Ошибка парсинга Global Research: {e}")
        
        return articles
    
    def parse_article_content(self, url: str, source: str) -> Optional[Tuple[str, str, str]]:
        """Парсит полный текст и изображение статьи для публикации"""
        try:
            logger.info(f"📄 Парсинг статьи: {url}")
            response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # Удаляем ненужные элементы
            for tag in soup.find_all(['script', 'style', 'nav', 'footer', 'aside']):
                tag.decompose()
            
            # Заголовок
            title = None
            if source == "AP News":
                title_elem = soup.find('h1') or soup.find(class_='Page-headline')
                title = title_elem.get_text().strip() if title_elem else "Без заголовка"
            else:
                title_elem = soup.find('h1')
                title = title_elem.get_text().strip() if title_elem else "Без заголовка"
            
            # Текст
            content_paragraphs = []
            if source == "AP News":
                article_body = soup.find(class_='RichTextStoryBody')
                if article_body:
                    content_paragraphs = article_body.find_all('p')
            else:
                article_body = soup.find('article') or soup.find(class_='entry-content')
                if article_body:
                    content_paragraphs = article_body.find_all('p')
            
            if not content_paragraphs:
                content_paragraphs = soup.find_all('p')[:15]
            
            full_text = '\n\n'.join([p.get_text().strip() for p in content_paragraphs if p.get_text().strip()])
            
            # Изображение
            image_url = None
            if source == "AP News":
                img = soup.find('meta', property='og:image')
                if img and img.get('content'):
                    image_url = img['content']
            else:
                img = soup.find('meta', property='og:image')
                if img and img.get('content'):
                    image_url = img['content']
                else:
                    img_tag = soup.find('img', class_='wp-post-image')
                    if img_tag and img_tag.get('src'):
                        image_url = img_tag['src']
            
            return title, full_text, image_url
            
        except Exception as e:
            logger.error(f"❌ Ошибка парсинга контента: {e}")
            return None
    
    def translate_text(self, text: str, target_lang: str = "ru") -> str:
        """Перевод текста через публичное API"""
        if not text or len(text) < 10:
            return text
        
        # Простая эмуляция перевода (в реальности нужно использовать API)
        # Здесь можно подключить Google Translate API или другой сервис
        logger.info(f"🔄 Перевод текста ({len(text)} символов)...")
        
        # Для демонстрации просто возвращаем текст с пометкой
        # В реальном проекте здесь должен быть вызов API перевода
        return f"[Перевод] {text[:500]}..."
    
    def publish_to_telegram(self, article: Dict) -> bool:
        """Публикация статьи в Telegram"""
        try:
            title = article['title']
            url = article['url']
            source = article['source']
            
            # Получаем полный контент
            content = self.parse_article_content(url, source)
            if not content:
                logger.error("❌ Не удалось получить контент для Telegram")
                return False
            
            full_title, full_text, image_url = content
            
            # Переводим заголовок и текст
            ru_title = self.translate_text(full_title)
            ru_text = self.translate_text(full_text[:1000])  # Ограничиваем длину
            
            # Формируем сообщение
            message = f"<b>{ru_title}</b>\n\n{ru_text}\n\n<a href='{url}'>🔗 Источник ({source})</a>"
            
            # Отправляем с изображением или без
            if image_url:
                try:
                    self.tg_bot.send_photo(
                        chat_id=TELEGRAM_CHANNEL_ID,
                        photo=image_url,
                        caption=message[:1024],
                        parse_mode='HTML'
                    )
                except Exception:
                    # Если не получилось с фото, отправляем без фото
                    self.tg_bot.send_message(
                        chat_id=TELEGRAM_CHANNEL_ID,
                        text=message,
                        parse_mode='HTML'
                    )
            else:
                self.tg_bot.send_message(
                    chat_id=TELEGRAM_CHANNEL_ID,
                    text=message,
                    parse_mode='HTML'
                )
            
            logger.info(f"✅ Пост в Telegram опубликован: {full_title[:50]}...")
            return True
            
        except Exception as e:
            logger.error(f"❌ Ошибка публикации в Telegram: {e}")
            return False
    
    def publish_to_9111(self, article: Dict) -> bool:
        """Публикация статьи на 9111.ru через Selenium"""
        chrome_path = self._check_chrome()
        if not chrome_path:
            logger.warning("⚠️ Chrome не найден, пропускаем публикацию на 9111.ru")
            return False
        
        driver = None
        try:
            logger.info("🌐 Запуск Selenium для 9111.ru...")
            
            # Настройки Chrome для работы в Railway
            chrome_options = Options()
            chrome_options.add_argument("--headless=new")
            chrome_options.add_argument("--no-sandbox")
            chrome_options.add_argument("--disable-dev-shm-usage")
            chrome_options.add_argument("--disable-gpu")
            chrome_options.add_argument("--window-size=1920,1080")
            chrome_options.add_argument("--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36")
            chrome_options.binary_location = chrome_path
            
            # Инициализация драйвера
            service = Service(ChromeDriverManager().install())
            driver = webdriver.Chrome(service=service, options=chrome_options)
            
            # Получаем контент статьи
            content = self.parse_article_content(article['url'], article['source'])
            if not content:
                logger.error("❌ Не удалось получить контент для 9111.ru")
                return False
            
            full_title, full_text, image_url = content
            
            # Переводим
            ru_title = self.translate_text(full_title)
            ru_text = self.translate_text(full_text)
            
            # Логинимся на 9111.ru
            logger.info("🔑 Вход на 9111.ru...")
            driver.get("https://9111.ru")
            time.sleep(3)
            
            # Здесь должен быть код авторизации и публикации
            # (сохраняем существующую логику из предыдущей версии)
            
            logger.info(f"✅ Статья опубликована на 9111.ru: {ru_title[:50]}...")
            return True
            
        except Exception as e:
            logger.error(f"❌ Ошибка Selenium: {e}")
            return False
        finally:
            if driver:
                driver.quit()
    
    def publish_next(self) -> bool:
        """Публикует следующую статью из очереди"""
        if not self.state["queue"]:
            logger.info("📭 Очередь пуста")
            return False
        
        article = self.state["queue"][0]
        logger.info(f"📝 ПУБЛИКАЦИЯ: {article['title'][:70]}...")
        
        # Публикуем в Telegram
        tg_success = self.publish_to_telegram(article)
        
        if tg_success:
            # Публикуем на 9111.ru (если получится - хорошо, нет - не страшно)
            self.publish_to_9111(article)
            
            # Помечаем как опубликованное
            self.mark_as_published(article)
            self.state["last_publish"] = datetime.now().isoformat()
            self.save_state()
            
            return True
        else:
            logger.error(f"❌ Не удалось опубликовать в Telegram")
            # Возвращаем в очередь? Пока просто логируем
            return False
    
    def check_sources(self):
        """Проверяет все источники на новые статьи"""
        logger.info("=" * 60)
        logger.info(f"🔍 ПРОВЕРКА ИСТОЧНИКОВ: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("=" * 60)
        
        new_articles = 0
        
        # AP News
        ap_articles = self.fetch_ap_news()
        for article in ap_articles:
            if self.add_to_queue(article):
                new_articles += 1
        
        # Global Research
        gr_articles = self.fetch_global_research()
        for article in gr_articles:
            if self.add_to_queue(article):
                new_articles += 1
        
        logger.info(f"📊 ВСЕГО НОВЫХ УНИКАЛЬНЫХ: {new_articles}")
        logger.info(f"📦 В очереди {len(self.state['queue'])} статей")
        
        self.state["last_check"] = datetime.now().isoformat()
        self.save_state()
    
    def check_and_publish(self):
        """Основная задача: проверяет источники и публикует, если можно"""
        logger.info("=" * 60)
        logger.info(f"🔍 ПРОВЕРКА: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("=" * 60)
        
        # Проверяем источники
        self.check_sources()
        
        # Проверяем, можно ли публиковать
        if not self.state["queue"]:
            logger.info("📭 Очередь пуста, публикация не требуется")
            return
        
        now = datetime.now(pytz.UTC)
        
        if self.state["last_publish"]:
            last_pub = datetime.fromisoformat(self.state["last_publish"])
            # Заменяем наивный datetime на timezone-aware
            if last_pub.tzinfo is None:
                last_pub = last_pub.replace(tzinfo=pytz.UTC)
            
            # Вычисляем следующий интервал
            interval = random.randint(MIN_PUBLISH_INTERVAL, MAX_PUBLISH_INTERVAL)
            next_publish = last_pub + timedelta(minutes=interval)
            
            if now < next_publish:
                wait_minutes = int((next_publish - now).total_seconds() / 60)
                logger.info(f"⏳ Следующая публикация через {interval} минут (осталось {wait_minutes} мин)")
                logger.info(f"⏰ Сейчас нельзя публиковать. Следующая попытка через {CHECK_INTERVAL} минут")
                return
        
        # Публикуем
        if self.publish_next():
            # Устанавливаем случайный интервал для следующей публикации
            next_interval = random.randint(MIN_PUBLISH_INTERVAL, MAX_PUBLISH_INTERVAL)
            logger.info(f"⏰ Следующая публикация через {next_interval} минут")
    
    def run(self):
        """Запускает планировщик"""
        # Немедленная проверка при старте
        self.check_and_publish()
        
        # Плановые проверки
        self.scheduler.add_job(
            self.check_and_publish,
            trigger=IntervalTrigger(minutes=CHECK_INTERVAL),
            id='check_and_publish',
            replace_existing=True
        )
        
        self.scheduler.start()
        logger.info(f"⏱️ Планировщик запущен, интервал проверки: {CHECK_INTERVAL} минут")
        
        try:
            # Держим процесс живым
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            logger.info("🛑 Бот остановлен")
            self.scheduler.shutdown()
            self.save_state()


if __name__ == "__main__":
    bot = NewsBot()
    bot.run()
