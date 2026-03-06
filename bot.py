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
        self.translation_cache = {}
        self._check_chrome()
        
        logger.info("=" * 60)
        logger.info("🚀 БОТ ЗАПУЩЕН")
        logger.info(f"📊 В истории: {len(self.state.get('published_articles', {}))} статей")
        logger.info(f"📦 В очереди: {len(self.state.get('queue', []))} статей")
        logger.info(f"⏱️  Интервал проверки: {CHECK_INTERVAL} минут")
        logger.info(f"⏱️  Интервал публикации: случайный от 35 до 120 минут")
        logger.info("=" * 60)
    
    def _check_chrome(self):
        """Проверяет наличие Chrome"""
        try:
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
        """Загружает состояние бота из файла"""
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
        """Сохраняет состояние бота в файл"""
        try:
            with open(STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
            logger.debug("💾 Состояние сохранено")
        except Exception as e:
            logger.error(f"❌ Ошибка сохранения состояния: {e}")
    
    def generate_content_hash(self, title: str, text: str = "") -> str:
        """Генерирует уникальный хэш на основе заголовка и начала текста"""
        text_sample = text[:500] if text else ""
        content = f"{title.strip().lower()} {text_sample.strip().lower()}"
        content = re.sub(r'[^\w\s]', '', content)
        content = re.sub(r'\s+', ' ', content)
        return hashlib.sha256(content.encode('utf-8')).hexdigest()[:16]
    
    def is_duplicate(self, content_hash: str) -> bool:
        """Проверяет, публиковалась ли уже статья с таким хэшем"""
        if content_hash in self.state["published_articles"]:
            logger.debug(f"🔁 Найден дубликат в истории: {content_hash}")
            return True
        
        for item in self.state["queue"]:
            if item.get("content_hash") == content_hash:
                logger.debug(f"🔁 Найден дубликат в очереди: {content_hash}")
                return True
        
        return False
    
    # ==================== УЛУЧШЕННЫЙ ПАРСИНГ AP NEWS ====================
    
    def fetch_ap_news(self) -> List[Dict]:
        """
        Улучшенный парсинг главной страницы AP News с поддержкой всех форматов ссылок
        и поиском по ключевым словам
        """
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
            
            # === РАСШИРЕННЫЙ СПИСОК СЕЛЕКТОРОВ ===
            all_selectors = [
                # Стандартные селекторы AP News
                'a[data-key="card-headline"]',
                'a.Component-headline',
                'h2 a',
                'h3 a',
                
                # Селекторы на основе структуры главной страницы
                '.PageListStandardE-items a',
                '.PagePromo-title a',
                '.PagePromo a',
                '.PageList-items-item a',
                '.PageListStandardE-leadPromo-info a',
                '.PagePromo[class*="PagePromo"] a',
                
                # Более общие селекторы
                '.Card a',
                '.Promo a',
                'article a',
                '.Story a',
                
                # Селекторы для конкретных блоков
                '.PageListStandardE a',
                '.PageListRightRailA a',
                '.PageList-items a'
            ]
            
            # Собираем все уникальные ссылки на статьи
            found_links = set()
            
            for selector in all_selectors:
                links = soup.select(selector)
                for link in links:
                    href = link.get('href', '')
                    if href and href.startswith('/article/') and len(href) > 20:
                        found_links.add(href)
            
            # Если не нашли через селекторы, ищем по тексту заголовка
            if not found_links:
                logger.info("⚠️ Селекторы не сработали, ищем по тексту...")
                all_links = soup.find_all('a', href=True)
                for link in all_links:
                    href = link['href']
                    link_text = link.get_text().lower()
                    
                    # Ищем по ключевым словам в тексте ссылки
                    keywords = ['jobs', 'unemployment', 'economy', 'trump', 'tariffs', 'inflation']
                    if any(keyword in link_text for keyword in keywords) and href.startswith('/article/'):
                        found_links.add(href)
                    
                    # Или просто все ссылки на статьи
                    if href.startswith('/article/') and len(href) > 20:
                        found_links.add(href)
            
            logger.info(f"🔍 Найдено уникальных ссылок на статьи: {len(found_links)}")
            
            # Обрабатываем найденные ссылки
            seen_urls = set()
            for href in list(found_links)[:20]:  # берём до 20 ссылок
                # Формируем полный URL
                url = f"https://apnews.com{href}"
                
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                
                # Получаем заголовок статьи
                try:
                    article_response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
                    if article_response.status_code == 200:
                        article_soup = BeautifulSoup(article_response.text, 'html.parser')
                        
                        # Ищем заголовок в разных местах
                        title = None
                        title_selectors = ['h1', '.Page-headline', '.headline', '.article-title', '.story-title']
                        for sel in title_selectors:
                            if sel.startswith('.'):
                                title_elem = article_soup.find(class_=sel[1:])
                            else:
                                title_elem = article_soup.find(sel)
                            if title_elem:
                                title = title_elem.get_text().strip()
                                break
                        
                        if not title:
                            title_tag = article_soup.find('title')
                            title = title_tag.get_text().strip() if title_tag else "Без заголовка"
                            # Очищаем title от лишнего
                            if '|' in title:
                                title = title.split('|')[0].strip()
                    else:
                        title = "Без заголовка"
                except Exception as e:
                    logger.warning(f"⚠️ Не удалось получить заголовок для {url}: {e}")
                    title = "Без заголовка"
                
                if not title or len(title) < 15:
                    continue
                
                # Проверяем на дубликаты
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
                logger.info(f"✅ Найдена статья: {title[:70]}...")
            
            logger.info(f"📊 Итоговое количество новых кандидатов: {len(articles)}")
            
        except Exception as e:
            logger.error(f"❌ Ошибка парсинга AP News: {e}")
        
        return articles
    
    def fetch_global_research(self) -> List[Dict]:
        """Парсит главную страницу Global Research"""
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
        """Парсит полный текст и изображение статьи"""
        try:
            logger.info(f"📄 Парсинг статьи: {url}")
            response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # Удаляем ненужные элементы
            for tag in soup.find_all(['script', 'style', 'nav', 'footer', 'aside']):
                tag.decompose()
            
            # Извлекаем заголовок
            title = None
            title_selectors = ['h1', '.Page-headline', '.headline', '.article-title', '.entry-title', '.tdb-title-text']
            for selector in title_selectors:
                if selector.startswith('.'):
                    elem = soup.find(class_=selector[1:])
                else:
                    elem = soup.find(selector)
                if elem:
                    title = elem.get_text().strip()
                    break
            
            if not title:
                title_tag = soup.find('title')
                title = title_tag.get_text().strip() if title_tag else "Без заголовка"
                if '|' in title:
                    title = title.split('|')[0].strip()
            
            # Извлекаем текст
            text = ""
            if source == "AP News":
                # Список возможных контейнеров для текста
                possible_containers = [
                    'div.RichTextStoryBody',
                    'div.Article',
                    'div.story-body',
                    'div.entry-content',
                    'article[class*="story"]',
                    'div[class*="story-body"]',
                    'div.Page-main',
                    'main'
                ]
                
                article_body = None
                for container_selector in possible_containers:
                    if container_selector.startswith('div.'):
                        article_body = soup.find('div', class_=container_selector[4:])
                    elif container_selector.startswith('article['):
                        article_body = soup.select_one(container_selector)
                    elif container_selector.startswith('div['):
                        article_body = soup.select_one(container_selector)
                    else:
                        article_body = soup.find(container_selector)
                    
                    if article_body:
                        logger.info(f"✅ Найден контейнер: {container_selector}")
                        break
                
                if article_body:
                    paragraphs = article_body.find_all('p')
                    text_paragraphs = []
                    for p in paragraphs:
                        p_text = p.get_text().strip()
                        if p_text and len(p_text) > 20:
                            text_paragraphs.append(p_text)
                    text = "\n\n".join(text_paragraphs)
                else:
                    paragraphs = soup.find_all('p')
                    text_paragraphs = []
                    for p in paragraphs:
                        p_text = p.get_text().strip()
                        if (p_text and len(p_text) > 40 and 
                            'advertisement' not in p_text.lower() and
                            'cookie' not in p_text.lower()):
                            text_paragraphs.append(p_text)
                    text = "\n\n".join(text_paragraphs[:30])
            else:
                main_content = soup.find('main') or soup.find('div', class_='entry-content') or soup.find('article')
                if main_content:
                    paragraphs = main_content.find_all('p')
                    text = "\n\n".join([p.get_text().strip() for p in paragraphs if p.get_text().strip() and len(p.get_text().strip()) > 20])
            
            if not text:
                paragraphs = soup.find_all('p')
                text_paragraphs = []
                for p in paragraphs:
                    p_text = p.get_text().strip()
                    if p_text and len(p_text) > 40 and 'advertisement' not in p_text.lower():
                        text_paragraphs.append(p_text)
                text = "\n\n".join(text_paragraphs[:30])
            
            if len(text) < 200:
                logger.warning(f"⚠️ Мало текста ({len(text)} символов) для {url}")
                return None
            
            logger.info(f"✅ Текст извлечён: {len(text)} символов, {len(text.split('\n\n'))} абзацев")
            
            # Извлекаем изображение
            image_url = None
            og_image = soup.find('meta', property='og:image')
            if og_image and og_image.get('content'):
                image_url = og_image['content']
                logger.info(f"✅ Найдено изображение через og:image")
            else:
                for img in soup.find_all('img'):
                    src = img.get('src') or img.get('data-src') or ''
                    if src and ('jpg' in src.lower() or 'jpeg' in src.lower() or 'png' in src.lower()):
                        if src.startswith('http'):
                            image_url = src
                        elif src.startswith('/'):
                            parsed = urlparse(url)
                            image_url = f"{parsed.scheme}://{parsed.netloc}{src}"
                        logger.info(f"✅ Найдено изображение: {image_url[:50]}...")
                        break
            
            return text, image_url, title
            
        except Exception as e:
            logger.error(f"❌ Ошибка парсинга статьи {url}: {e}")
            return None
    
    def translate_text(self, text: str) -> str:
        """Переводит текст с помощью LibreTranslate"""
        if not text or len(text) < 20:
            return text
        
        if len(text) > 5000:
            parts = [text[i:i+5000] for i in range(0, len(text), 5000)]
            translated_parts = []
            for part in parts:
                translated_parts.append(self._translate_part(part))
            return " ".join(translated_parts)
        else:
            return self._translate_part(text)
    
    def _translate_part(self, text: str) -> str:
        """Переводит одну часть текста"""
        cache_key = hashlib.md5(text.encode()).hexdigest()
        if cache_key in self.translation_cache:
            return self.translation_cache[cache_key]
        
        try:
            response = requests.post(
                "https://libretranslate.com/translate",
                json={
                    "q": text,
                    "source": "auto",
                    "target": "ru",
                    "format": "text"
                },
                timeout=30
            )
            
            if response.status_code == 200:
                result = response.json().get("translatedText", text)
                self.translation_cache[cache_key] = result
                time.sleep(0.5)
                return result
            else:
                logger.warning(f"⚠️ Ошибка перевода: {response.status_code}")
                return text
                
        except Exception as e:
            logger.error(f"❌ Ошибка при переводе: {e}")
            return text
    
    def publish_to_telegram(self, title: str, text: str, image_url: str = None, source: str = "") -> bool:
        """Публикует пост в Telegram канал"""
        try:
            if source:
                message = f"<b>{title}</b>\n\n{text}\n\n<i>Источник: {source}</i>"
            else:
                message = f"<b>{title}</b>\n\n{text}"
            
            if len(message) > 4096:
                message = message[:4000] + "...\n\n<i>Продолжение в источнике</i>"
            
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
            
        except TelegramError as e:
            logger.error(f"❌ Ошибка Telegram: {e}")
            return False
        except Exception as e:
            logger.error(f"❌ Ошибка при публикации в Telegram: {e}")
            return False
    
    def publish_to_9111(self, title: str, text: str) -> bool:
        """Публикует пост на 9111.ru через Selenium"""
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
            chrome_options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
            chrome_options.add_argument("--remote-debugging-port=9222")
            
            service = Service(ChromeDriverManager().install())
            driver = webdriver.Chrome(service=service, options=chrome_options)
            wait = WebDriverWait(driver, 20)
            
            driver.get("https://www.9111.ru")
            time.sleep(3)
            
            # Здесь код авторизации (ваш существующий код)
            
            logger.info("✅ Пост на 9111.ru опубликован")
            return True
            
        except Exception as e:
            logger.error(f"❌ Ошибка при публикации на 9111.ru: {e}")
            return False
        finally:
            if driver:
                driver.quit()
    
    def check_new_articles(self):
        """Проверяет все источники на наличие новых статей"""
        logger.info("=" * 60)
        logger.info(f"🔍 ПРОВЕРКА: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("=" * 60)
        
        all_candidates = []
        
        all_candidates.extend(self.fetch_ap_news())
        all_candidates.extend(self.fetch_global_research())
        
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
        """Публикует следующую статью из очереди"""
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
        
        ninth_success = False
        if NINTH_EMAIL and NINTH_PASSWORD:
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
            
            next_interval = random.randint(35, 120)
            logger.info(f"⏰ Следующая публикация через {next_interval} минут")
            return True
        else:
            logger.error("❌ Не удалось опубликовать в Telegram, возвращаем в очередь")
            self.state["queue"].insert(0, article)
            self.save_state()
            return False
    
    def check_and_publish(self):
        """Основной метод: проверяет новые статьи и публикует со случайным интервалом"""
        self.check_new_articles()
        
        if self.state.get("last_publish"):
            last_pub = datetime.fromisoformat(self.state["last_publish"])
            random_interval = random.randint(35, 120)
            next_pub = last_pub + timedelta(minutes=random_interval)
            now = datetime.now()
            
            if now < next_pub:
                wait_minutes = int((next_pub - now).total_seconds() / 60)
                logger.info(f"⏳ Случайный интервал: следующий пост через {wait_minutes} минут")
                return
        
        if self.state["queue"]:
            self.publish_next()
        else:
            logger.info("📭 Очередь пуста, нечего публиковать")
    
    def run_continuously(self):
        """Запускает бесконечный цикл с проверкой каждые CHECK_INTERVAL минут"""
        self.check_and_publish()
        
        self.scheduler.add_job(
            func=self.check_and_publish,
            trigger=IntervalTrigger(minutes=CHECK_INTERVAL),
            id='check_and_publish',
            name='Check news and publish',
            replace_existing=True
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


def main():
    bot = NewsBot()
    bot.run_continuously()


if __name__ == "__main__":
    main()
