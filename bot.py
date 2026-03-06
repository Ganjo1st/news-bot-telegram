#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import json
import hashlib
import logging
import asyncio
import random
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from urllib.parse import urljoin, urlparse
from contextlib import contextmanager
import re

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
import chromedriver_autoinstaller
import telegram
from telegram.error import TelegramError
import httpx

# ==================== НАСТРОЙКИ ====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8767446234:AAGRz1sJfDtV321CpUBdI2sqGVDcWryGqcY")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "-1002484885240")

# Для 9111.ru
NINTH_EMAIL = os.getenv("NINTH_EMAIL", "your_email@example.com")  # Замените
NINTH_PASSWORD = os.getenv("NINTH_PASSWORD", "your_password")      # Замените

# Интервал проверки новых статей (в минутах)
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "30"))

# Минимальный интервал между публикациями (в минутах)
PUBLISH_INTERVAL = int(os.getenv("PUBLISH_INTERVAL", "120"))

# Файлы для хранения состояния
STATE_FILE = "bot_state.json"
LOG_FILE = "bot.log"
# ====================================================

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Подавляем лишние логи от библиотек
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)


class NewsBot:
    """Универсальный бот для сбора и публикации новостей"""
    
    def __init__(self):
        self.tg_bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
        self.scheduler = BackgroundScheduler(timezone=pytz.UTC)
        self.state = self.load_state()
        
        # Автоустановка ChromeDriver
        chromedriver_autoinstaller.install()
        logger.info("✅ ChromeDriver готов")
        
        # Для перевода (можно заменить на любой API)
        self.translation_cache = {}
    
    # ==================== УПРАВЛЕНИЕ СОСТОЯНИЕМ ====================
    
    def load_state(self) -> Dict:
        """Загружает состояние бота из файла"""
        default_state = {
            "last_publish": None,
            "published_articles": {},  # {content_hash: {"title": str, "timestamp": str, "url": str}}
            "queue": [],               # [{title, url, source, content_hash, timestamp}]
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
    
    # ==================== ГЕНЕРАЦИЯ ХЭША ====================
    
    def generate_content_hash(self, title: str, text: str = "") -> str:
        """
        Генерирует уникальный хэш на основе заголовка и начала текста.
        Это ключевой метод для дедупликации!
        """
        # Берём первые 500 символов текста для хэша (достаточно для уникальности)
        text_sample = text[:500] if text else ""
        
        # Объединяем и очищаем от лишних пробелов/знаков препинания
        content = f"{title.strip().lower()} {text_sample.strip().lower()}"
        content = re.sub(r'[^\w\s]', '', content)  # удаляем знаки препинания
        content = re.sub(r'\s+', ' ', content)     # нормализуем пробелы
        
        return hashlib.sha256(content.encode('utf-8')).hexdigest()[:16]  # 16 символов достаточно
    
    def is_duplicate(self, content_hash: str) -> bool:
        """Проверяет, публиковалась ли уже статья с таким хэшем"""
        # Проверяем в истории публикаций
        if content_hash in self.state["published_articles"]:
            logger.debug(f"🔁 Найден дубликат в истории: {content_hash}")
            return True
        
        # Проверяем в очереди (на случай, если уже добавлена, но ещё не опубликована)
        for item in self.state["queue"]:
            if item.get("content_hash") == content_hash:
                logger.debug(f"🔁 Найден дубликат в очереди: {content_hash}")
                return True
        
        return False
    
    # ==================== ПАРСИНГ ИСТОЧНИКОВ ====================
    
    def fetch_ap_news(self) -> List[Dict]:
        """Парсит главную страницу AP News"""
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
            
            # AP News использует разные селекторы, попробуем основные
            selectors = [
                'a[data-key="card-headline"]',
                'a.Component-headline',
                'h2 a',
                '.PagePromo-title a',
                '.Promo-title a'
            ]
            
            links = []
            for selector in selectors:
                links = soup.select(selector)
                if links:
                    break
            
            seen_urls = set()
            for link in links[:15]:  # берём до 15 ссылок
                href = link.get('href', '')
                if not href:
                    continue
                
                # Формируем полный URL
                if href.startswith('/'):
                    url = f"https://apnews.com{href}"
                elif href.startswith('http'):
                    url = href
                else:
                    continue
                
                # Фильтруем дубликаты URL в рамках одного запуска
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                
                title = link.get_text().strip()
                if not title or len(title) < 20:  # слишком короткий заголовок
                    continue
                
                # Проверяем, не публиковали ли мы уже эту статью
                # Пока без текста, хэш временный
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
            
            # Global Research использует структуру с тегами h2 и h3
            links = []
            for header in soup.find_all(['h2', 'h3'], class_=lambda x: x != 'td-module-title'):
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
        """
        Парсит полный текст и изображение статьи.
        Возвращает (текст, изображение_url, полный_заголовок) или None
        """
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
            title_selectors = ['h1', '.headline', '.article-title', '.entry-title', '.tdb-title-text']
            for selector in title_selectors:
                if selector.startswith('.'):
                    elem = soup.find(class_=selector[1:])
                else:
                    elem = soup.find(selector)
                if elem:
                    title = elem.get_text().strip()
                    break
            
            if not title:
                title = soup.find('title').get_text().strip() if soup.find('title') else "Без заголовка"
            
            # Извлекаем текст
            text = ""
            if source == "AP News":
                # AP News: текст в div с class="Article"
                article = soup.find('div', class_='Article')
                if article:
                    paragraphs = article.find_all('p')
                    text = "\n\n".join([p.get_text().strip() for p in paragraphs if p.get_text().strip()])
            else:
                # Global Research и другие: ищем все параграфы в основной части
                main_content = soup.find('main') or soup.find('div', class_='entry-content') or soup.find('article')
                if main_content:
                    paragraphs = main_content.find_all('p')
                    text = "\n\n".join([p.get_text().strip() for p in paragraphs if p.get_text().strip() and len(p.get_text().strip()) > 20])
            
            if not text:
                # Fallback: берём все параграфы
                paragraphs = soup.find_all('p')
                text = "\n\n".join([p.get_text().strip() for p in paragraphs if p.get_text().strip() and len(p.get_text().strip()) > 40])
            
            if len(text) < 200:  # слишком мало текста
                logger.warning(f"⚠️ Мало текста ({len(text)} символов)")
                return None
            
            # Извлекаем изображение
            image_url = None
            # Сначала ищем meta og:image
            og_image = soup.find('meta', property='og:image')
            if og_image and og_image.get('content'):
                image_url = og_image['content']
            else:
                # Ищем первое подходящее изображение
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
    
    # ==================== ПЕРЕВОД ====================
    
    def translate_text(self, text: str, target_lang: str = "ru") -> str:
        """
        Переводит текст с помощью LibreTranslate (бесплатно, без ключа)
        """
        if len(text) > 5000:
            # Разбиваем на части
            parts = [text[i:i+5000] for i in range(0, len(text), 5000)]
            translated_parts = []
            for part in parts:
                translated_parts.append(self._translate_part(part))
            return " ".join(translated_parts)
        else:
            return self._translate_part(text)
    
    def _translate_part(self, text: str) -> str:
        """Переводит одну часть текста"""
        if not text or len(text) < 20:
            return text
        
        # Простой кэш
        cache_key = hashlib.md5(text.encode()).hexdigest()
        if cache_key in self.translation_cache:
            return self.translation_cache[cache_key]
        
        try:
            # Используем публичный экземпляр LibreTranslate
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
                time.sleep(0.5)  # Не перегружаем сервис
                return result
            else:
                logger.warning(f"⚠️ Ошибка перевода: {response.status_code}")
                return text
                
        except Exception as e:
            logger.error(f"❌ Ошибка при переводе: {e}")
            return text
    
    # ==================== ПУБЛИКАЦИЯ ====================
    
    def publish_to_telegram(self, title: str, text: str, image_url: str = None, source: str = "") -> bool:
        """
        Публикует пост в Telegram канал
        """
        try:
            # Формируем сообщение
            if source:
                message = f"<b>{title}</b>\n\n{text}\n\n<i>Источник: {source}</i>"
            else:
                message = f"<b>{title}</b>\n\n{text}"
            
            # Ограничение длины
            if len(message) > 4096:
                message = message[:4000] + "...\n\n<i>Продолжение в источнике</i>"
            
            if image_url:
                # Скачиваем изображение
                img_data = requests.get(image_url, timeout=15).content
                # Отправляем с фото
                self.tg_bot.send_photo(
                    chat_id=TELEGRAM_CHANNEL_ID,
                    photo=img_data,
                    caption=message,
                    parse_mode='HTML'
                )
            else:
                # Отправляем только текст
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
        """
        Публикует пост на 9111.ru через Selenium
        """
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
            
            driver = webdriver.Chrome(options=chrome_options)
            wait = WebDriverWait(driver, 20)
            
            # Переходим на сайт
            driver.get("https://www.9111.ru")
            time.sleep(3)
            
            # Ищем кнопку входа/логин
            try:
                login_btn = wait.until(
                    EC.element_to_be_clickable((By.XPATH, "//a[contains(text(), 'Вход') or contains(@href, 'login')]"))
                )
                login_btn.click()
                time.sleep(2)
                
                # Вводим email/логин
                email_input = wait.until(
                    EC.presence_of_element_located((By.XPATH, "//input[@type='email' or @name='email' or @name='login']"))
                )
                email_input.send_keys(NINTH_EMAIL)
                
                # Вводим пароль
                password_input = driver.find_element(By.XPATH, "//input[@type='password']")
                password_input.send_keys(NINTH_PASSWORD)
                
                # Отправляем форму
                submit_btn = driver.find_element(By.XPATH, "//button[@type='submit']")
                submit_btn.click()
                time.sleep(5)
                
            except Exception as e:
                logger.warning(f"⚠️ Возможно, уже авторизованы: {e}")
            
            # Ищем кнопку создания поста/статьи
            try:
                create_btn = wait.until(
                    EC.element_to_be_clickable((By.XPATH, "//a[contains(text(), 'Добавить') or contains(text(), 'Создать') or contains(@href, 'add')]"))
                )
                create_btn.click()
                time.sleep(3)
            except:
                # Пробуем перейти напрямую
                driver.get("https://www.9111.ru/blogs/add/")
                time.sleep(3)
            
            # Заполняем заголовок
            try:
                title_input = wait.until(
                    EC.presence_of_element_located((By.XPATH, "//input[@name='title' or @placeholder='Заголовок']"))
                )
                title_input.clear()
                title_input.send_keys(title[:255])  # ограничение длины
            except Exception as e:
                logger.error(f"❌ Не найдено поле заголовка: {e}")
                return False
            
            # Заполняем текст
            try:
                # Ищем textarea или div с contenteditable
                text_area = driver.find_element(By.XPATH, "//textarea[@name='text' or @name='content']")
                text_area.clear()
                # Разбиваем на параграфы
                paragraphs = text.split('\n\n')
                for p in paragraphs:
                    text_area.send_keys(p)
                    text_area.send_keys('\n\n')
            except:
                try:
                    # Пробуем contenteditable
                    editor = driver.find_element(By.XPATH, "//div[@contenteditable='true']")
                    editor.clear()
                    paragraphs = text.split('\n\n')
                    for p in paragraphs:
                        editor.send_keys(p)
                        editor.send_keys('\n\n')
                except Exception as e:
                    logger.error(f"❌ Не найдено поле текста: {e}")
                    return False
            
            # Публикуем
            try:
                publish_btn = driver.find_element(By.XPATH, "//button[contains(text(), 'Опубликовать') or contains(text(), 'Сохранить')]")
                publish_btn.click()
                time.sleep(5)
                
                logger.info("✅ Пост на 9111.ru опубликован")
                return True
            except Exception as e:
                logger.error(f"❌ Не найдена кнопка публикации: {e}")
                return False
            
        except WebDriverException as e:
            logger.error(f"❌ Ошибка Selenium: {e}")
            return False
        except Exception as e:
            logger.error(f"❌ Ошибка при публикации на 9111.ru: {e}")
            return False
        finally:
            if driver:
                driver.quit()
    
    # ==================== ОСНОВНАЯ ЛОГИКА ====================
    
    def check_new_articles(self):
        """
        Проверяет все источники на наличие новых статей
        """
        logger.info("=" * 60)
        logger.info(f"🔍 ПРОВЕРКА: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("=" * 60)
        
        all_candidates = []
        
        # Собираем с разных источников
        all_candidates.extend(self.fetch_ap_news())
        all_candidates.extend(self.fetch_global_research())
        
        if not all_candidates:
            logger.info("📭 Новых кандидатов не найдено")
            self.state["last_check"] = datetime.now().isoformat()
            self.save_state()
            return
        
        logger.info(f"📊 Найдено кандидатов: {len(all_candidates)}")
        
        # Для каждого кандидата парсим полный текст и добавляем в очередь
        added = 0
        for candidate in all_candidates:
            try:
                # Парсим полную статью
                result = self.parse_article_content(candidate["url"], candidate["source"])
                if not result:
                    continue
                
                text, image_url, full_title = result
                
                # Генерируем окончательный хэш
                content_hash = self.generate_content_hash(full_title, text)
                
                # Проверяем дубликат
                if self.is_duplicate(content_hash):
                    logger.info(f"⏭️ УЖЕ БЫЛО (контент): {full_title[:70]}...")
                    continue
                
                # Переводим
                logger.info(f"🔄 Перевод: {full_title[:50]}...")
                ru_title = self.translate_text(full_title)
                ru_text = self.translate_text(text[:2000])  # Переводим начало текста
                
                # Добавляем в очередь
                self.state["queue"].append({
                    "title": ru_title,
                    "original_title": full_title,
                    "text": ru_text,
                    "full_text": text,  # сохраняем оригинал на будущее
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
        """
        Публикует следующую статью из очереди
        """
        if not self.state["queue"]:
            logger.info("📭 Очередь пуста")
            return False
        
        # Берём первую статью из очереди
        article = self.state["queue"].pop(0)
        
        logger.info("\n" + "=" * 60)
        logger.info(f"📝 ПУБЛИКАЦИЯ: {article['title']}")
        logger.info(f"   Источник: {article['source']}")
        logger.info("=" * 60)
        
        # Публикуем в Telegram
        tg_success = self.publish_to_telegram(
            title=article['title'],
            text=article['text'],
            image_url=article.get('image_url'),
            source=article['source']
        )
        
        # Публикуем на 9111.ru (если есть креды)
        ninth_success = False
        if NINTH_EMAIL != "your_email@example.com" and NINTH_PASSWORD != "your_password":
            logger.info("🔄 Пробуем опубликовать на 9111.ru...")
            ninth_success = self.publish_to_9111(article['title'], article['text'])
        else:
            logger.warning("⚠️ Креды для 9111.ru не указаны, пропускаем")
        
        if tg_success:
            # Сохраняем в историю
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
            
            logger.info(f"⏰ Следующая публикация через {PUBLISH_INTERVAL} минут")
            return True
        else:
            logger.error("❌ Не удалось опубликовать в Telegram, возвращаем в очередь")
            # Возвращаем в очередь (в начало или в конец?)
            self.state["queue"].insert(0, article)
            self.save_state()
            return False
    
    def check_and_publish(self):
        """
        Основной метод: проверяет новые статьи и публикует, если пришло время
        """
        # Сначала проверяем новые статьи
        self.check_new_articles()
        
        # Проверяем, можно ли публиковать сейчас
        if self.state.get("last_publish"):
            last_pub = datetime.fromisoformat(self.state["last_publish"])
            next_pub = last_pub + timedelta(minutes=PUBLISH_INTERVAL)
            now = datetime.now()
            
            if now < next_pub:
                wait_minutes = int((next_pub - now).total_seconds() / 60)
                logger.info(f"⏳ Лимит частоты: следующий пост через {wait_minutes} минут")
                return
        
        # Пробуем опубликовать
        if self.state["queue"]:
            self.publish_next()
        else:
            logger.info("📭 Очередь пуста, нечего публиковать")
    
    def run_continuously(self):
        """
        Запускает бесконечный цикл с проверкой каждые CHECK_INTERVAL минут
        """
        logger.info("=" * 60)
        logger.info("🚀 БОТ ЗАПУЩЕН")
        logger.info(f"📊 В истории: {len(self.state.get('published_articles', {}))} статей")
        logger.info(f"📦 В очереди: {len(self.state.get('queue', []))} статей")
        logger.info(f"⏱️  Интервал проверки: {CHECK_INTERVAL} минут")
        logger.info(f"⏱️  Интервал публикации: {PUBLISH_INTERVAL} минут")
        logger.info("=" * 60)
        
        # Немедленная проверка при старте
        self.check_and_publish()
        
        # Планируем регулярные проверки
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
            # Держим процесс живым
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
