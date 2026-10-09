"""Smoke test using the installed Firefox with a fresh WebDriver profile."""
import json
import os
import sys
import tempfile
import threading
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from selenium import webdriver
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import server

with tempfile.TemporaryDirectory() as directory:
    server.DB=directory+'/browser.db';os.environ['SETUP_TOKEN']='browser-test-setup-secret';server.init()
    http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);server.ORIGIN='http://localhost:'+str(http.server_address[1])
    threading.Thread(target=http.serve_forever,daemon=True).start()
    driver=None;artifacts=Path('artifacts');artifacts.mkdir(exist_ok=True)
    try:
        options=Options();options.add_argument('-headless')
        installed=Path('/Applications/Firefox.app/Contents/MacOS/firefox')
        if installed.exists():options.binary_location=str(installed)
        print('Starting installed Firefox with isolated profile',flush=True)
        driver=webdriver.Firefox(options=options)
        driver.set_window_size(1512,1100);driver.set_page_load_timeout(20);wait=WebDriverWait(driver,15)
        driver.get(server.ORIGIN)
        wait.until(EC.presence_of_element_located((By.ID,'email'))).send_keys('firefox@example.com')
        driver.find_element(By.ID,'password').send_keys('browser-test-password');driver.find_element(By.ID,'setupToken').send_keys('browser-test-setup-secret');driver.find_element(By.CSS_SELECTOR,'#auth-form button').click()
        wait.until(EC.text_to_be_present_in_element((By.TAG_NAME,'h1'),'Your trading. Connected.'))
        driver.execute_script("window.__errors=[];window.addEventListener('error',e=>window.__errors.push(e.message));window.addEventListener('unhandledrejection',e=>window.__errors.push(String(e.reason)));")
        driver.get_full_page_screenshot_as_file(str(artifacts/'firefox-overview.png'))
        for route,title in [('accounts','Every account. One workspace.'),('rules','Your strategy. In sync.'),('trades','Every execution tells a story.'),('analytics','Understand your edge.'),('activity','Nothing gets lost.'),('settings','Make it yours.')]:
            driver.find_element(By.CSS_SELECTOR,'.nav[data-page="'+route+'"]').click();wait.until(EC.text_to_be_present_in_element((By.TAG_NAME,'h1'),title))
            if route=='analytics':
                driver.find_element(By.CSS_SELECTOR,'[data-period="30"]').click();driver.find_element(By.CSS_SELECTOR,'[data-period="all"]').click()
                driver.get_full_page_screenshot_as_file(str(artifacts/'firefox-analytics.png'))
        driver.find_element(By.ID,'change-password').click();driver.find_element(By.ID,'current').send_keys('browser-test-password');driver.find_element(By.ID,'new-password').send_keys('browser-test-password-new');driver.find_element(By.CSS_SELECTOR,'.modal [type="submit"]').click();wait.until(EC.invisibility_of_element_located((By.CSS_SELECTOR,'.modal-bg')))
        driver.set_window_size(390,844);driver.find_element(By.ID,'mobile-menu').click();driver.find_element(By.CSS_SELECTOR,'.nav[data-page="analytics"]').click();wait.until(EC.text_to_be_present_in_element((By.TAG_NAME,'h1'),'Understand your edge.'))
        assert driver.execute_script('return document.documentElement.scrollWidth <= window.innerWidth'),'Mobile overflow'
        driver.get_full_page_screenshot_as_file(str(artifacts/'firefox-mobile.png'))
        assert not driver.execute_script('return window.__errors'),driver.execute_script('return window.__errors')
        driver.find_element(By.CSS_SELECTOR,'.topbar [data-logout]').click();wait.until(EC.text_to_be_present_in_element((By.CSS_SELECTOR,'.auth-form h1'),'Welcome back.'))
        print(json.dumps({'browser':'installed Firefox','routes':7,'mobileWidth':390,'screenshots':str(artifacts),'status':'passed'}),flush=True)
    finally:
        if driver:driver.quit()
        http.shutdown();http.server_close()
