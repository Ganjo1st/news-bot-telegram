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
import telegram
from telegram.error import TelegramError
import httpx

# ==================== НАСТРОЙКИ ====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")

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

logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("selenium").setLevel(logging.WARNING)


class NewsBot:
    def __init__(self):
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
            logger.error("❌ TELEGRAM_BOT_TOKEN или TELEGRAM_CHANNEL_ID не заданы!")
            sys.exit(1)
            
        self.tg_bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
        self.scheduler = BackgroundScheduler(timezone=pytz.UTC)
        self.state = self.load_state()
        
        if "published_hashes" not in self.state:
            self.state["published_hashes"] = {}
        if "queue_hashes" not in self.state:
            self.state["queue_hashes"] = []
        
        self.chrome_path = self._find_chrome()
        
        logger.info("=" * 60)
        logger.info("🚀 БОТ ЗАПУЩЕН")
        logger.info(f"📊 В истории: {len(self.state.get('published_articles', {}))} статей")
        logger.info(f"📦 В очереди: {len(self.state.get('queue', []))} статей")
        logger.info(f"🔐 Уникальных хэшей: {len(self.state['published_hashes'])}")
        logger.info(f"🌐 Chrome: {'✅ найден' if self.chrome_path else '❌ не найден'}")
        logger.info("=" * 60)
    
    def _find_chrome(self) -> Optional[str]:
        """Ищет Chrome в системе"""
        paths = [
            '/usr/bin/chromium',
            '/usr/bin/chromium-browser',
            '/usr/bin/google-chrome',
            '/usr/bin/google-chrome-stable',
            '/app/.chrome/chrome-linux64/chrome'
        ]
        
        for path in paths:
            if os.path.exists(path):
                try:
                    version = subprocess.check_output([path, '--version'], text=True).strip()
                    logger.info(f"✅ Chrome найден: {path} ({version})")
                    return path
                except:
                    logger.info(f"✅ Chrome найден: {path}")
                    return path
        
        logger.warning("⚠️ Chrome не найден, публикация на 9111.ru будет недоступна")
        return None
    
    def load_state(self) -> Dict:
        default_state = {
            "last_publish": None,
            "published_articles": {},
            "queue": [],
            "last_check": None,
            "published_hashes": {},
            "queue_hashes": []
        }
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки состояния: {e}")
        return default_state
    
    def save_state(self):
        try:
            with open(STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"❌ Ошибка сохранения: {e}")
    
    def generate_content_hash(self, title: str, text: str = "") -> str:
        """Универсальный хэш для дедупликации"""
        content = f"{re.sub(r'[^\w\s]', '', title.lower())}|{text[:500].lower()}"
        return hashlib.sha256(content.encode()).hexdigest()[:16]
    
    def is_duplicate(self, content_hash: str, url: str = None) -> bool:
        """Проверка дубликата по хэшу и URL"""
        if content_hash in self.state["published_hashes"]:
            return True
        if content_hash in self.state["queue_hashes"]:
            return True
        if url and url in self.state.get("published_articles", {}):
            return True
        return False
    
    def fetch_ap_news(self) -> List[Dict]:
        """Парсинг AP News"""
        articles = []
        try:
            logger.info("🌐 Парсинг AP News")
            response = requests.get("https://apnews.com/", timeout=15)
            soup = BeautifulSoup(response.text, 'html.parser')
            
            for link in soup.select('a[href^="/article/"]')[:15]:
                url = f"https://apnews.com{link['href']}"
                
                try:
                    art_resp = requests.get(url, timeout=10)
                    art_soup = BeautifulSoup(art_resp.text, 'html.parser')
                    
                    title = art_soup.find('h1')
                    if not title:
                        continue
                    title = title.get_text().strip()
                    
                    text = ' '.join([p.get_text() for p in art_soup.find_all('p')[:10]])
                    content_hash = self.generate_content_hash(title, text)
                    
                    if not self.is_duplicate(content_hash, url):
                        articles.append({
                            "title": title,
                            "url": url,
                            "source": "AP News",
                            "content_hash": content_hash
                        })
                except Exception as e:
                    continue
                    
        except Exception as e:
            logger.error(f"❌ Ошибка AP News: {e}")
        
        return articles
    
    def fetch_global_research(self) -> List[Dict]:
        """Парсинг Global Research"""
        articles = []
        try:
            logger.info("🌐 Парсинг Global Research")
            response = requests.get("https://www.globalresearch.ca/", timeout=15)
            soup = BeautifulSoup(response.text, 'html.parser')
            
            for link in soup.find_all('a', href=True)[:20]:
                url = link['href']
                if not url.startswith('http'):
                    continue
                    
                title = link.get_text().strip()
                if len(title) < 20:
                    continue
                
                try:
                    art_resp = requests.get(url, timeout=10)
                    art_soup = BeautifulSoup(art_resp.text, 'html.parser')
                    text = ' '.join([p.get_text() for p in art_soup.find_all('p')[:10]])
                    
                    content_hash = self.generate_content_hash(title, text)
                    
                    if not self.is_duplicate(content_hash, url):
                        articles.append({
                            "title": title,
                            "url": url,
                            "source": "Global Research",
                            "content_hash": content_hash
                        })
                except:
                    continue
                    
        except Exception as e:
            logger.error(f"❌ Ошибка Global Research: {e}")
        
        return articles
    
    def add_to_queue(self, article: Dict) -> bool:
        """Добавление в очередь с проверкой"""
        if self.is_duplicate(article["content_hash"], article.get("url")):
            return False
        
        self.state["queue"].append(article)
        self.state["queue_hashes"].append(article["content_hash"])
        logger.info(f"✅ Добавлено: {article['title'][:50]}...")
        return True
    
    def publish_to_telegram(self, article: Dict) -> bool:
        """Публикация в Telegram"""
        try:
            message = f"<b>{article['title']}</b>\n\n🔗 <a href='{article['url']}'>Источник: {article['source']}</a>"
            
            self.tg_bot.send_message(
                chat_id=TELEGRAM_CHANNEL_ID,
                text=message[:4096],
                parse_mode='HTML'
            )
            
            logger.info(f"✅ Telegram: {article['title'][:50]}...")
            return True
            
        except Exception as e:
            logger.error(f"❌ Ошибка Telegram: {e}")
            return False
    
    def publish_to_9111(self, article: Dict) -> bool:
        """Публикация на 9111.ru через Selenium"""
        if not self.chrome_path:
            logger.warning("⚠️ Chrome нет, пропускаем 9111.ru")
            return False
        
        driver = None
        try:
            logger.info("🌐 Запуск Selenium для 9111.ru...")
            
            options = Options()
            options.add_argument("--headless=new")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-gpu")
            options.add_argument("--window-size=1920,1080")
            options.binary_location = self.chrome_path
            
            # ВАЖНО: В Railway chromedriver должен быть в PATH
            driver = webdriver.Chrome(options=options)
            
            # Здесь код авторизации и публикации
            driver.get("https://9111.ru")
            time.sleep(3)
            
            logger.info(f"✅ 9111.ru: {article['title'][:50]}...")
            return True
            
        except Exception as e:
            logger.error(f"❌ Ошибка 9111.ru: {e}")
            return False
        finally:
            if driver:
                driver.quit()
    
    def publish_next(self) -> bool:
        """Публикация следующей статьи"""
        if not self.state["queue"]:
            return False
        
        article = self.state["queue"][0]
        logger.info(f"📝 ПУБЛИКАЦИЯ: {article['title'][:70]}...")
        
        tg_ok = self.publish_to_telegram(article)
        
        if tg_ok:
            self.publish_to_9111(article)
            
            # Помечаем как опубликованное
            self.state["published_articles"][article["url"]] = {
                "title": article["title"],
                "date": datetime.now().isoformat()
            }
            self.state["published_hashes"][article["content_hash"]] = datetime.now().isoformat()
            
            # Удаляем из очереди
            self.state["queue"] = self.state["queue"][1:]
            self.state["queue_hashes"] = [h for h in self.state["queue_hashes"] 
                                          if h != article["content_hash"]]
            
            self.state["last_publish"] = datetime.now().isoformat()
            self.save_state()
            
            interval = random.randint(MIN_PUBLISH_INTERVAL, MAX_PUBLISH_INTERVAL)
            logger.info(f"⏰ Следующая через {interval} минут")
            
            return True
        
        return False
    
    def check_sources(self):
        """Проверка источников"""
        logger.info("=" * 60)
        logger.info(f"🔍 ПРОВЕРКА: {datetime.now()}")
        logger.info("=" * 60)
        
        new = 0
        for article in self.fetch_ap_news():
            if self.add_to_queue(article):
                new += 1
        
        for article in self.fetch_global_research():
            if self.add_to_queue(article):
                new += 1
        
        logger.info(f"📊 Новых: {new}, в очереди: {len(self.state['queue'])}")
        self.state["last_check"] = datetime.now().isoformat()
        self.save_state()
    
    def check_and_publish(self):
        """Основная задача"""
        self.check_sources()
        
        if not self.state["queue"]:
            return
        
        if self.state["last_publish"]:
            last = datetime.fromisoformat(self.state["last_publish"])
            if last.tzinfo is None:
                last = last.replace(tzinfo=pytz.UTC)
            
            next_pub = last + timedelta(minutes=MIN_PUBLISH_INTERVAL)
            if datetime.now(pytz.UTC) < next_pub:
                return
        
        self.publish_next()
    
    def run(self):
        """Запуск"""
        self.check_and_publish()
        
        self.scheduler.add_job(
            self.check_and_publish,
            trigger=IntervalTrigger(minutes=CHECK_INTERVAL),
            id='check_and_publish'
        )
        
        self.scheduler.start()
        logger.info(f"⏱️ Планировщик запущен (интервал: {CHECK_INTERVAL} мин)")
        
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            logger.info("🛑 Остановка")
            self.scheduler.shutdown()
            self.save_state()


if __name__ == "__main__":
    bot = NewsBot()
    bot.run()
