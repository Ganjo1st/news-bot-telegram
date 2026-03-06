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
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
import pytz
import telegram
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import WebDriverException
from webdriver_manager.chrome import ChromeDriverManager

# ==================== НАСТРОЙКИ ====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8767446234:AAGRz1sJfDtV321CpUBdI2sqGVDcWryGqcY")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "-1002484885240")

# Для 9111.ru
NINTH_EMAIL = os.getenv("NINTH_EMAIL", "")
NINTH_PASSWORD = os.getenv("NINTH_PASSWORD", "")

# Интервал проверки новых статей (в минутах)
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "30"))

# Файлы для хранения состояния
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
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("selenium").setLevel(logging.WARNING)


class NewsBot:
    def __init__(self):
        self.tg_bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
        self.scheduler = BackgroundScheduler(timezone=pytz.UTC)
        self.state = self.load_state()
        self.translation_cache = {}
        
        # Проверяем Chrome
        self._check_chrome()

    def _check_chrome(self):
        """Проверяет наличие Chrome"""
        try:
            import subprocess
            result = subprocess.run(['which', 'google-chrome'], capture_output=True, text=True)
            if result.returncode == 0:
                chrome_path = result.stdout.strip()
                logger.info(f"✅ Chrome найден: {chrome_path}")
                
                version_result = subprocess.run(['google-chrome', '--version'], capture_output=True, text=True)
                logger.info(f"✅ Chrome версия: {version_result.stdout.strip()}")
            else:
                logger.warning("⚠️ Chrome не найден, публикация на 9111.ru будет недоступна")
        except Exception as e:
            logger.error(f"❌ Ошибка при проверке Chrome: {e}")

    def load_state(self) -> Dict:
        default_state = {
            "last_publish": None,
            "published_articles": {},
            "queue": [],
            "last_check": None
        }
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, 'r', encoding='utf-8') as f:
                    state = json.load(f)
                    logger.info(f"📥 Загружено состояние: {len(state.get('published_articles', {}))} статей в истории, {len(state.get('queue', []))} в очереди")
                    return state
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки состояния: {e}")
        return default_state

    def save_state(self):
        try:
            with open(STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"❌ Ошибка сохранения состояния: {e}")

    def generate_content_hash(self, title: str, text: str = "") -> str:
        text_sample = text[:500] if text else ""
        content = f"{title.strip().lower()} {text_sample.strip().lower()}"
        content = re.sub(r'[^\w\s]', '', content)
        content = re.sub(r'\s+', ' ', content)
        return hashlib.sha256(content.encode('utf-8')).hexdigest()[:16]

    def is_duplicate(self, content_hash: str) -> bool:
        if content_hash in self.state["published_articles"]:
            return True
        for item in self.state["queue"]:
            if item.get("content_hash") == content_hash:
                return True
        return False

    def fetch_ap_news(self) -> List[Dict]:
        articles = []
        try:
            logger.info("🌐 Парсинг главной страницы AP News")
            response = requests.get(
                "https://apnews.com/",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=15
            )
            soup = BeautifulSoup(response.text, 'html.parser')
            
            selectors = ['a[data-key="card-headline"]', 'a.Component-headline', 'h2 a']
            links = []
            for selector in selectors:
                links = soup.select(selector)
                if links:
                    break

            seen_urls = set()
            for link in links[:15]:
                href = link.get('href', '')
                if not href:
                    continue
                
                url = f"https://apnews.com{href}" if href.startswith('/') else href
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                
                title = link.get_text().strip()
                if not title or len(title) < 20:
                    continue
                
                temp_hash = self.generate_content_hash(title)
                if self.is_duplicate(temp_hash):
                    logger.info(f"⏭️ УЖЕ БЫЛО (заголовок): {title[:50]}...")
                    continue
                
                articles.append({
                    "title": title,
                    "url": url,
                    "source": "AP News",
                    "temp_hash": temp_hash
                })
            
            logger.info(f"🔍 Найдено новых кандидатов: {len(articles)}")
        except Exception as e:
            logger.error(f"❌ Ошибка парсинга AP News: {e}")
        return articles

    def fetch_global_research(self) -> List[Dict]:
        articles = []
        try:
            logger.info("🌐 Парсинг главной страницы Global Research")
            response = requests.get(
                "https://www.globalresearch.ca/",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=15
            )
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
                
                title = link.get_text().strip()
                if not title or len(title) < 15:
                    continue
                
                temp_hash = self.generate_content_hash(title)
                if self.is_duplicate(temp_hash):
                    logger.info(f"⏭️ УЖЕ БЫЛО (заголовок): {title[:50]}...")
                    continue
                
                articles.append({
                    "title": title,
                    "url": url,
                    "source": "Global Research",
                    "temp_hash": temp_hash
                })
        except Exception as e:
            logger.error(f"❌ Ошибка парсинга Global Research: {e}")
        return articles

    def parse_article_content(self, url: str, source: str) -> Optional[Tuple[str, str, str]]:
        try:
            logger.info(f"📄 Парсинг статьи: {url}")
            response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
            soup = BeautifulSoup(response.text, 'html.parser')
            
            for tag in soup.find_all(['script', 'style', 'nav', 'footer', 'aside']):
                tag.decompose()
            
            title = soup.find('h1')
            title = title.get_text().strip() if title else soup.find('title').get_text().strip()
            
            text = ""
            if source == "AP News":
                article = soup.find('div', class_='Article')
                if article:
                    paragraphs = article.find_all('p')
                    text = "\n\n".join([p.get_text().strip() for p in paragraphs if p.get_text().strip()])
            else:
                main_content = soup.find('main') or soup.find('div', class_='entry-content') or soup.find('article')
                if main_content:
                    paragraphs = main_content.find_all('p')
                    text = "\n\n".join([p.get_text().strip() for p in paragraphs if p.get_text().strip() and len(p.get_text().strip()) > 20])
            
            if not text:
                paragraphs = soup.find_all('p')
                text = "\n\n".join([p.get_text().strip() for p in paragraphs if p.get_text().strip() and len(p.get_text().strip()) > 40])
            
            if len(text) < 200:
                logger.warning(f"⚠️ Мало текста ({len(text)} символов)")
                return None
            
            image_url = None
            og_image = soup.find('meta', property='og:image')
            if og_image and og_image.get('content'):
                image_url = og_image['content']
            else:
                for img in soup.find_all('img'):
                    src = img.get('src') or img.get('data-src') or ''
                    if src and ('jpg' in src.lower() or 'jpeg' in src.lower() or 'png' in src.lower()):
                        if src.startswith('http'):
                            image_url = src
                        elif src.startswith('/'):
                            parsed = urlparse(url)
                            image_url = f"{parsed.scheme}://{parsed.netloc}{src}"
                        break
            
            return text, image_url, title
            
        except Exception as e:
            logger.error(f"❌ Ошибка парсинга статьи {url}: {e}")
            return None

    def translate_text(self, text: str) -> str:
        if not text or len(text) < 20:
            return text
        
        try:
            response = requests.post(
                "https://libretranslate.com/translate",
                json={
                    "q": text[:2000],
                    "source": "auto",
                    "target": "ru",
                    "format": "text"
                },
                timeout=30
            )
            
            if response.status_code == 200:
                return response.json().get("translatedText", text)
            return text
        except Exception as e:
            logger.error(f"❌ Ошибка перевода: {e}")
            return text

    def publish_to_telegram(self, title: str, text: str, image_url: str = None, source: str = "") -> bool:
        try:
            message = f"<b>{title}</b>\n\n{text}\n\n<i>Источник: {source}</i>"
            if len(message) > 4096:
                message = message[:4000] + "..."
            
            if image_url:
                img_data = requests.get(image_url, timeout=15).content
                self.tg_bot.send_photo(
                    chat_id=TELEGRAM_CHANNEL_ID,
                    photo=img_data,
                    caption=message,
                    parse_mode='HTML'
                )
            else:
                self.tg_bot.send_message(
                    chat_id=TELEGRAM_CHANNEL_ID,
                    text=message,
                    parse_mode='HTML'
                )
            
            logger.info("✅ Пост в Telegram опубликован")
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка публикации в Telegram: {e}")
            return False

    def publish_to_9111(self, title: str, text: str) -> bool:
        if not NINTH_EMAIL or not NINTH_PASSWORD:
            logger.warning("⚠️ Креды для 9111.ru не указаны, пропускаем")
            return False
        
        driver = None
        try:
            logger.info("🌐 Запуск Selenium для 9111.ru...")
            
            chrome_options = Options()
            chrome_options.add_argument("--headless")
            chrome_options.add_argument("--no-sandbox")
            chrome_options.add_argument("--disable-dev-shm-usage")
            chrome_options.add_argument("--disable-gpu")
            chrome_options.add_argument("--window-size=1920,1080")
            
            service = Service(ChromeDriverManager().install())
            driver = webdriver.Chrome(service=service, options=chrome_options)
            wait = WebDriverWait(driver, 20)
            
            driver.get("https://www.9111.ru")
            time.sleep(3)
            
            # Здесь код авторизации и публикации
            # ... (ваш существующий код)
            
            logger.info("✅ Пост на 9111.ru опубликован")
            return True
            
        except Exception as e:
            logger.error(f"❌ Ошибка при публикации на 9111.ru: {e}")
            return False
        finally:
            if driver:
                driver.quit()

    def check_new_articles(self):
        logger.info("=" * 60)
        logger.info(f"🔍 ПРОВЕРКА: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("=" * 60)
        
        all_candidates = self.fetch_ap_news() + self.fetch_global_research()
        
        if not all_candidates:
            logger.info("📭 Новых кандидатов не найдено")
            self.state["last_check"] = datetime.now().isoformat()
            self.save_state()
            return

        logger.info(f"📊 Найдено кандидатов: {len(all_candidates)}")
        
        added = 0
        for candidate in all_candidates:
            try:
                result = self.parse_article_content(candidate["url"], candidate["source"])
                if not result:
                    continue
                
                text, image_url, full_title = result
                content_hash = self.generate_content_hash(full_title, text)
                
                if self.is_duplicate(content_hash):
                    logger.info(f"⏭️ УЖЕ БЫЛО (контент): {full_title[:70]}...")
                    continue
                
                logger.info(f"🔄 Перевод: {full_title[:50]}...")
                ru_title = self.translate_text(full_title)
                ru_text = self.translate_text(text[:2000])
                
                self.state["queue"].append({
                    "title": ru_title,
                    "original_title": full_title,
                    "text": ru_text,
                    "full_text": text,
                    "image_url": image_url,
                    "source": candidate["source"],
                    "url": candidate["url"],
                    "content_hash": content_hash,
                    "timestamp": datetime.now().isoformat()
                })
                
                added += 1
                logger.info(f"✅ УНИКАЛЬНАЯ: {ru_title[:70]}...")
                logger.info(f"✅ Статья добавлена в очередь")
                
            except Exception as e:
                logger.error(f"❌ Ошибка при обработке кандидата: {e}")
        
        self.state["last_check"] = datetime.now().isoformat()
        self.save_state()
        logger.info(f"📦 В очереди {len(self.state['queue'])} статей")

    def publish_next(self):
        if not self.state["queue"]:
            logger.info("📭 Очередь пуста")
            return False
        
        article = self.state["queue"].pop(0)
        
        logger.info("\n" + "=" * 60)
        logger.info(f"📝 ПУБЛИКАЦИЯ: {article['title']}")
        logger.info(f"   Источник: {article['source']}")
        logger.info("=" * 60)
        
        tg_success = self.publish_to_telegram(
            title=article['title'],
            text=article['text'],
            image_url=article.get('image_url'),
            source=article['source']
        )
        
        ninth_success = self.publish_to_9111(article['title'], article['text'])
        
        if tg_success:
            self.state["published_articles"][article["content_hash"]] = {
                "title": article["title"],
                "timestamp": datetime.now().isoformat(),
                "url": article["url"],
                "source": article["source"]
            }
            
            self.state["last_publish"] = datetime.now().isoformat()
            self.save_state()
            
            if ninth_success:
                logger.info("✅ Статья опубликована во все каналы")
            else:
                logger.warning("⚠️ Пост опубликован только в Telegram")
            
            # Генерируем случайный интервал для следующей публикации
            next_interval = random.randint(35, 120)
            logger.info(f"⏰ Следующая публикация через {next_interval} минут (случайный интервал)")
            return True
        else:
            logger.error("❌ Не удалось опубликовать в Telegram, возвращаем в очередь")
            self.state["queue"].insert(0, article)
            self.save_state()
            return False

    def check_and_publish(self):
        """
        Основной метод: проверяет новые статьи и публикует со случайным интервалом
        """
        # Сначала проверяем новые статьи
        self.check_new_articles()
        
        # Проверяем, можно ли публиковать сейчас
        if self.state.get("last_publish"):
            last_pub = datetime.fromisoformat(self.state["last_publish"])
            
            # Случайный интервал от 35 до 120 минут
            random_interval = random.randint(35, 120)
            next_pub = last_pub + timedelta(minutes=random_interval)
            now = datetime.now()
            
            if now < next_pub:
                wait_minutes = int((next_pub - now).total_seconds() / 60)
                logger.info(f"⏳ Случайный интервал: следующий пост через {wait_minutes} минут")
                return
        
        # Пробуем опубликовать
        if self.state["queue"]:
            self.publish_next()
        else:
            logger.info("📭 Очередь пуста, нечего публиковать")

    def run_continuously(self):
        logger.info("=" * 60)
        logger.info("🚀 БОТ ЗАПУЩЕН")
        logger.info(f"📊 В истории: {len(self.state.get('published_articles', {}))} статей")
        logger.info(f"📦 В очереди: {len(self.state.get('queue', []))} статей")
        logger.info(f"⏱️  Интервал проверки: {CHECK_INTERVAL} минут")
        logger.info(f"⏱️  Интервал публикации: случайный от 35 до 120 минут")
        logger.info("=" * 60)
        
        # Немедленная проверка при старте
        self.check_and_publish()
        
        # Планируем регулярные проверки
        self.scheduler.add_job(
            func=self.check_and_publish,
            trigger=IntervalTrigger(minutes=CHECK_INTERVAL),
            id='check_and_publish'
        )
        
        self.scheduler.start()
        logger.info(f"⏰ Планировщик запущен, следующая проверка через {CHECK_INTERVAL} минут")
        
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            logger.info("🛑 Бот остановлен пользователем")
            self.scheduler.shutdown()
            self.save_state()


if __name__ == "__main__":
    bot = NewsBot()
    bot.run_continuously()
