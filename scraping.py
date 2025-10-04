# scraping.py
import logging
import re
import time
from urllib.parse import urljoin, urlparse, urlunparse

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

logger = logging.getLogger(__name__)

def get_max_quality_url(url):
    if not url: return None
    patterns = [(r'/[wh]\d{2,4}-[wh]\d{2,4}-c/', '/'), (r'_\d{2,4}x\d{2,4}(\.(jpe?g|png|webp))', r'\1'), (r'\.\d{2,4}x\d{2,4}(\.(jpe?g|png|webp))', r'\1'), (r'-\d{2,4}x\d{2,4}(\.(jpe?g|png|webp))', r'\1'), (r'/thumb/', '/'), (r'\?(w|h|width|height|size|quality|crop|fit)=.*', '')]
    cleaned_url = url
    for pattern, replacement in patterns:
        cleaned_url = re.sub(pattern, replacement, cleaned_url)
    parsed_url = urlparse(cleaned_url)
    return urlunparse(parsed_url._replace(query='', fragment=''))

def setup_selenium_driver():
    service = Service(executable_path="/usr/bin/chromedriver")
    options = Options()
    options.binary_location = "/usr/bin/google-chrome"
    options.add_argument("--headless"); options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage"); options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1200")
    try: return webdriver.Chrome(service=service, options=options)
    except Exception as e: logger.critical(f"Failed to setup Selenium driver: {e}"); return None

def scrape_images_from_url_sync(url: str):
    logger.info(f"Starting MAX-QUALITY scrape for URL: {url}")
    driver = setup_selenium_driver()
    if not driver: return set()
    images = set()
    try:
        driver.get(url)
        WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        def wait_for_network_idle(driver, timeout=10, idle_time=2):
            script = "const callback=arguments[arguments.length-1];let last_resource_count=0;let stable_checks=0;const check_interval=250;const check_network=()=>{const current_resource_count=window.performance.getEntriesByType('resource').length;if(current_resource_count===last_resource_count)stable_checks++;else{stable_checks=0;last_resource_count=current_resource_count}if(stable_checks*check_interval>=(idle_time*1000))callback(true);else setTimeout(check_network,check_interval)};check_network();"
            try: driver.set_script_timeout(timeout); driver.execute_async_script(script)
            except Exception: logger.warning(f"Network idle check timed out.")
        wait_for_network_idle(driver)
        total_height = driver.execute_script("return document.body.scrollHeight")
        for i in range(0, total_height, 500):
            driver.execute_script(f"window.scrollTo(0, {i});"); time.sleep(0.3)
        wait_for_network_idle(driver); time.sleep(2)
        js_extraction_script = "const imageCandidates=new Map();const imageExtensions=/\\.(jpeg|jpg|png|gif|webp|bmp|svg)/i;function addCandidate(key,url,priority){if(!url||url.startsWith('data:image'))return;if(!imageCandidates.has(key)){imageCandidates.set(key,{url:url,priority:priority})}else if(priority>imageCandidates.get(key).priority){imageCandidates.set(key,{url:url,priority:priority})}}document.querySelectorAll('img').forEach((img,index)=>{const key=img.src||`img_${index}`;addCandidate(key,img.src,1);if(img.dataset.src)addCandidate(key,img.dataset.src,1);if(img.srcset){let maxUrl=null;let maxWidth=0;img.srcset.split(',').forEach(part=>{const parts=part.trim().split(/\\s+/);const url=parts[0];const widthMatch=parts[1]?parts[1].match(/(\\d+)w/):null;if(widthMatch){const width=parseInt(widthMatch[1],10);if(width>maxWidth){maxWidth=width;maxUrl=url}}else{maxUrl=url}});if(maxUrl)addCandidate(key,maxUrl,2)}const parentAnchor=img.closest('a');if(parentAnchor&&parentAnchor.href&&imageExtensions.test(parentAnchor.href)){addCandidate(key,parentAnchor.href,3)}});document.querySelectorAll('*').forEach(el=>{const style=window.getComputedStyle(el,null).getPropertyValue('background-image');if(style&&style.includes('url')){const match=style.match(/url\\([\"']?([^\"']*)[\"']?\\)/);if(match&&match[1]){addCandidate(match[1],match[1],1)}}});return Array.from(imageCandidates.values()).map(c=>c.url);"
        extracted_urls = driver.execute_script(js_extraction_script)
        for img_url in extracted_urls:
            if img_url and img_url.strip():
                images.add(get_max_quality_url(urljoin(url, img_url.strip())))
    except Exception as e: logger.error(f"Error scraping {url}: {e}", exc_info=True)
    finally: driver.quit()
    return {img for img in images if img and img.startswith('http')}
