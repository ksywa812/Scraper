Przygotowuję kompleksowy research na temat scraperów danych firm w Polsce oraz systemów auto-mailingu.

# **Research: Web Scraping Firm w Polsce i Systemy Auto-Mailingu**

## **1\. SCRAPERS DANYCH FIRM \- TECHNOLOGIE I JĘZYKI PROGRAMOWANIA**

## **Najpopularniejsze języki do web scrapingu**

Python \- absolutny lider (90%+ projektów)

Python dominuje w web scrapingu ze względu na bogaty ekosystem bibliotek:

Podstawowe biblioteki:

* BeautifulSoup \- parsowanie HTML/XML, ekstrakcja danych ze statycznych stron  
* Requests \- wysyłanie zapytań HTTP, pobieranie treści stron  
* Scrapy \- zaawansowany framework do crawlingu i scrapingu, obsługa wielu żądań jednocześnie  
* Selenium \- automatyzacja przeglądarki, obsługa JavaScript, dynamicznych treści  
* lxml \- szybkie parsowanie HTML/XML

Dodatkowe narzędzia:

* Pandas \- strukturyzacja i analiza zebranych danych  
* RegEx (re) \- wydobywanie wzorców (np. adresy email, telefony)

Inne języki:

* JavaScript/Node.js \- Puppeteer, Cheerio  
* PHP \- Goutte, Simple HTML DOM Parser  
* Java \- Jsoup

## **Czy to są boty?**

Tak, to automaty (boty), ale techniczne określenie:

Web Crawler (pająk) \- bot przemierzający strony internetowe, podążający za linkami  
Web Scraper \- bot wydobywający konkretne dane ze stron  
Email Extractor \- wyspecjalizowany scraper szukający adresów email

Mechanizm działania:

1. Bot wysyła zapytanie HTTP do strony (imituje przeglądarkę)  
2. Pobiera kod HTML strony  
3. Parsuje strukturę HTML (znajduje odpowiednie tagi, klasy, ID)  
4. Wydobywa dane według wzorców (np. wszystkie adresy email)  
5. Zapisuje dane do pliku (CSV, JSON, baza danych)

## **2\. JAK ZBUDOWAĆ SCRAPER EMAIL \- PRAKTYCZNY TUTORIAL**

## **Metoda 1: Prosty scraper Python z BeautifulSoup**

python

`import requests`  
`from bs4 import BeautifulSoup`  
`import re`  
`import csv`

*`# Funkcja wydobywająca emaile`*  
`def extract_emails(url):`  
    `try:`  
        `# Pobierz stronę`  
        `response = requests.get(url, timeout=10)`  
        `soup = BeautifulSoup(response.text, 'html.parser')`  
          
        `# Regex dla emaili`  
        `email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'`  
          
        `# Znajdź wszystkie emaile w tekście`  
        `emails = re.findall(email_pattern, response.text)`  
          
        `return list(set(emails))  # Unikalne emaile`  
          
    `except Exception as e:`  
        `print(f"Błąd: {e}")`  
        `return []`

*`# Lista firm/domen do scrapowania`*  
`urls = [`  
    `'https://firma1.pl/kontakt',`  
    `'https://firma2.pl/o-nas',`  
    `# ... więcej URL`  
`]`

*`# Zbierz dane`*  
`all_data = []`  
`for url in urls:`  
    `emails = extract_emails(url)`  
    `if emails:`  
        `all_data.append({`  
            `'url': url,`  
            `'emails': ', '.join(emails)`  
        `})`

*`# Zapisz do CSV`*  
`with open('emails.csv', 'w', newline='', encoding='utf-8') as f:`  
    `writer = csv.DictWriter(f, fieldnames=['url', 'emails'])`  
    `writer.writeheader()`  
    `writer.writerows(all_data)`

## **Metoda 2: Zaawansowany scraper z Selenium (dynamiczne strony)**

python

`from selenium import webdriver`  
`from selenium.webdriver.chrome.options import Options`  
`from bs4 import BeautifulSoup`  
`import re`

*`# Konfiguracja headless Chrome`*  
`chrome_options = Options()`  
`chrome_options.add_argument("--headless")`  
`chrome_options.add_argument("--no-sandbox")`

`driver = webdriver.Chrome(options=chrome_options)`

`def scrape_with_js(url):`  
    `driver.get(url)`  
    `# Czekaj na załadowanie JS`  
    `driver.implicitly_wait(10)`  
      
    `# Pobierz renderowany HTML`  
    `html = driver.page_source`  
    `soup = BeautifulSoup(html, 'html.parser')`  
      
    `# Wydobądź emaile`  
    `email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'`  
    `emails = re.findall(email_pattern, html)`  
      
    `return emails`

*`# Użycie`*  
`emails = scrape_with_js('https://firma.pl')`  
`driver.quit()`

## **Gotowe narzędzia do scrapingu firm w Polsce**

Polskie rozwiązania:

1. ExtraScraper.pl \- automatyczne zbieranie danych firm  
   * Nazwy, emaile, telefony, NIP, lokalizacje  
   * Export do CSV/Excel  
   * Filtrowanie po branży i województwie  
2. Legalna-baza.pl \- dane z CEIDG i KRS  
   * Legalne pobieranie z publicznych rejestrów  
   * Filtrowanie wg PKD, daty rejestracji, lokalizacji  
   * Integracje z CRM  
3. DataMiners.pl \- profesjonalna ekstrakcja danych  
   * Dostęp do milionów firm w Polsce  
   * Wzbogacanie baz o dane kontaktowe  
   * Dedykowane rozwiązania

Międzynarodowe narzędzia:

4. Outscraper \- Email & Contacts Scraper  
   * 500 domen/miesiąc za darmo  
   * Wydobywa emaile, telefony, social media  
   * Wsparcie dla Google Maps  
5. Maps Scraper AI \- scraping z Google Maps  
   * Rozszerzenie Chrome  
   * Export do CSV  
   * Zbieranie firm według lokalizacji i branży

## **3\. ASPEKTY PRAWNE \- RODO I LEGALNOŚĆ**

## **Czy web scraping jest legalny w Polsce?**

Odpowiedź: To zależy od kontekstu

✅ LEGALNE:

* Scraping publicznie dostępnych danych nieosobowych  
* Dane z rejestrów publicznych (CEIDG, KRS)  
* Text and data mining do celów badawczych  
* Pobieranie danych z własnej zgody (regulamin pozwala)

❌ NIELEGALNE/RYZYKOWNE:

* Scraping danych osobowych bez podstawy prawnej (RODO)  
* Naruszanie regulaminu strony (zakaz w ToS)  
* Łamanie zabezpieczeń technicznych  
* Nadmierne obciążanie serwerów (DoS)  
* Naruszanie praw autorskich

## **RODO \- kluczowe aspekty**

Dane osobowe (art. 4 RODO):

* Email służbowy typu   
* jan.kowalski@firma.pl  
*  \= dane osobowe  
* Email ogólny typu   
* kontakt@firma.pl  
*  \= NIE są danymi osobowymi  
* Telefon, imię, nazwisko \= dane osobowe

Podstawy prawne przetwarzania (art. 6 RODO):

* Zgoda \- trudna do uzyskania przy scrapingu  
* Prawnie uzasadniony interes \- najczęściej stosowana (marketing B2B)  
* Wykonanie umowy \- nie ma zastosowania  
* Obowiązek prawny \- nie ma zastosowania

Obowiązki:

* Obowiązek informacyjny (art. 14 RODO) \- trudny do spełnienia  
* Możliwość wycofania zgody, sprzeciwu, usunięcia danych  
* Secure storage danych

Wyjątki od obowiązku informacyjnego (art. 14 ust. 5):

* Udzielenie informacji jest niemożliwe  
* Wymaga niewspółmiernie dużego wysiłku

## **Najlepsze praktyki legalnego scrapingu**

1. Sprawdź regulamin strony (robots.txt, Terms of Service)  
2. Ogranicz się do danych ogólnych (  
3. kontakt@firma.pl  
4.  zamiast osobistych emaili)  
5. Używaj danych z publicznych rejestrów (CEIDG, KRS)  
6. Nie obciążaj serwerów \- delay między requestami (time.sleep)  
7. Dokumentuj podstawę prawną przetwarzania danych  
8. Umożliw opt-out w pierwszym emailu

## **4\. SYSTEMY AUTO-MAILINGU**

## **Google Apps Script \- czy wystarczy?**

Limity wysyłki Google Apps Script:

Darmowe konto Gmail:

* 100 emaili/dzień (MailApp.sendEmail)  
* 50 odbiorców per email  
* Limit NIE zwiększa się z czasem

Google Workspace:

* Nowe konta: 100 emaili/dzień (pierwsze \~60 dni)  
* Ustabilizowane konta: 1500 emaili/dzień  
* 50 odbiorców per email

Zalety Google Apps Script:

javascript

`function sendEmails() {`  
  `var sheet = SpreadsheetApp.getActiveSheet();`  
  `var data = sheet.getDataRange().getValues();`  
    
  `for (var i = 1; i < data.length; i++) {`  
    `var email = data[i][0];`  
    `var name = data[i][1];`  
    `var subject = "Temat wiadomości";`  
    `var body = "Cześć " + name + ", treść emaila...";`  
      
    `MailApp.sendEmail(email, subject, body);`  
  `}`  
`}`

Wady:

* Bardzo niskie limity (100/dzień na start)  
* Brak zaawansowanych funkcji (A/B testing, analytics)  
* Brak warmup domeny  
* Słaba deliverability przy cold emailach  
* Brak automatycznego follow-up  
* Ryzyko trafienia do SPAM

Werdykt: Google Apps Script nadaje się tylko do wewnętrznej komunikacji lub bardzo małych kampanii (\<100 emaili/dzień).

## **Profesjonalne systemy cold email (B2B)**

TOP narzędzia na 2026:

## **1\. Instantly.ai ⭐ Najlepsze dla cold email**

* Cena: od $360/rok  
* Unlimited email accounts \- rotacja skrzynek  
* Built-in warmup i deliverability protection  
* Visual sequence builder  
* Team analytics  
* Idealny dla: Zespoły sales, agencje

## **2\. Saleshandy**

* Cena: od $25/miesiąc  
* AI Copilot do personalizacji  
* Największa baza leadów B2B  
* Unlimited mailboxes  
* Built-in Email Infrastructure  
* Idealny dla: High-volume cold email

## **3\. Lemlist**

* Cena: od $662/rok  
* Hyper-personalizacja (wideo, obrazy)  
* Multichannel outreach (email \+ LinkedIn)  
* LemWarm included (warmup)  
* Idealny dla: Creative teams

## **4\. Apollo.io**

* Cena: Freemium, paid od $49/miesiąc  
* Baza 270M+ kontaktów B2B  
* CRM \+ cold email w jednym  
* AI-powered sequences  
* Idealny dla: All-in-one sales platform

## **5\. Woodpecker**

* Cena: od $240/rok  
* Wysoka deliverability  
* Free warmup (2 inboxes)  
* Idealny dla: Małe firmy, agencje

## **Marketing automation (warm audience)**

Dla warm leadów i newsletter:

1. ActiveCampaign \- zaawansowana automatyzacja  
2. Brevo (Sendinblue) \- unlimited contacts, SMS  
3. MailerLite \- prosty, przystępny cenowo  
4. GetResponse \- webinary, landing pages  
5. Mailchimp \- all-in-one marketing

## **Porównanie: Google Apps Script vs Profesjonalne narzędzia**

| Funkcja | Google Apps Script | Instantly/Saleshandy |
| :---- | :---- | :---- |
| Limit dzienny | 100-1500 | Unlimited |
| Email warmup | ❌ | ✅ |
| Deliverability | Niska (cold) | Wysoka |
| Follow-up sequences | Ręczne | Automatyczne |
| A/B testing | ❌ | ✅ |
| Analytics | Podstawowe | Zaawansowane |
| Rotacja kont | ❌ | ✅ |
| Cena | Darmowy | $25-50/m |
| Najlepsze dla | Internal, \<100/day | Cold outreach B2B |

## **5\. BEST PRACTICES \- DELIVERABILITY**

## **Jak nie trafić do SPAM**

1\. Email warmup (3 tygodnie):

* Dzień 1: 5 emaili  
* Zwiększaj o 5/dzień  
* Cap na 50 emaili/dzień podczas warmup  
* Używaj SmartLead lub Instantly

2\. Czyszczenie listy:

* Weryfikuj emaile przed wysyłką (FindyMail, NeverBounce)  
* Bounce rate \< 2%  
* Usuń hard bounces natychmiast

3\. Zawartość emaila:

* Unikaj spam words ("free", "guarantee", "act now")  
* Text-to-image ratio: 80/20  
* Naturalne, konwersacyjne języki  
* ZAWSZE link do unsubscribe

4\. Infrastruktura:

* Dedykowane domeny do cold email  
* SPF, DKIM, DMARC records skonfigurowane  
* Oddzielna infrastruktura dla cold vs transactional

5\. Metryki:

* Reply rate: \>5% (target)  
* Bounce rate: \<2%  
* Spam complaints: \<0.1%

6\. NIE śledź open rates \- tracking pixels obniżają deliverability

## **6\. REKOMENDOWANY STACK DLA B2B COLD EMAIL**

## **Kompletny setup:**

Krok 1: Budowanie listy

* Clay.io \- live data enrichment, 40+ integracji  
* Apollo.io \- baza 270M+ kontaktów  
* Alternatywa PL: ExtraScraper.pl / DataMiners.pl

Krok 2: Weryfikacja emaili

* Findymail \- weryfikacja adresów  
* ZeroBounce \- validation API

Krok 3: Wysyłka

* Instantly.ai lub Saleshandy \- cold email platform  
* Minimum 3-5 email accounts (rotacja)

Krok 4: Warmup

* Built-in w Instantly/Saleshandy  
* LemWarm (standalone)

Krok 5: Monitoring

* Google Postmaster Tools  
* Email deliverability checkers

## **Przykładowy koszt miesięczny:**

* Clay (lead enrichment): $149/m  
* Instantly.ai (wysyłka): $37/m  
* Findymail (weryfikacja): $49/m  
* Google Workspace (5 kont): $30/m  
* RAZEM: \~$265/miesiąc

## **7\. PODSUMOWANIE I REKOMENDACJE**

## **Web Scraping:**

✅ Używaj Python (BeautifulSoup, Scrapy, Selenium)  
✅ Zacznij od legalnych źródeł (CEIDG, KRS via Legalna-baza.pl)  
✅ Alternatywnie: gotowe narzędzia (ExtraScraper, Outscraper)  
✅ Respektuj RODO \- ogranicz się do firmowych emaili (kontakt@, biuro@)  
✅ Sprawdzaj regulaminy stron przed scrapingiem

## **Auto-mailing:**

❌ Google Apps Script NIE JEST wystarczający dla cold email

* Limity 100/dzień to za mało  
* Brak warmup \= SPAM  
* Brak profesjonalnych funkcji

✅ Użyj dedykowanego cold email software:

* Instantly.ai \- najlepszy overall  
* Saleshandy \- high volume, niski koszt  
* Apollo.io \- all-in-one z bazą danych

✅ Cold email wymaga:

* Minimum 3-5 email accounts (rotacja)  
* 3 tygodnie warmup  
* Profesjonalne narzędzie z analytics  
* Czyszczenie listy przed wysyłką
