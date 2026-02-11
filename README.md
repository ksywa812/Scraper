# Multi-Source Business Lead Scraper

## Description

This Python script is designed to automate the collection of business contact information (leads) from multiple online sources for a specified industry and location. It scrapes data from:

1.  **Google Places API:** Fetches business listings, including names, addresses, phone numbers, and websites.
2.  **Panorama Firm (panoramafirm.pl):** A Polish business directory.
3.  **PKT.pl:** Another Polish business directory.
4.  **Booksy / Moment.pl:** Free listings from Booksy (Moment redirects to Booksy).
5.  **Fresha:** Listing data with category filters and pagination.
6.  **SPAeden:** Spa/wellness ranking list (for spa/wellness/massage).
7.  **ZnanyLekarz:** Profiles for massage/fizjoterapia (best‑effort).
8.  **Fixly:** Best‑effort (often JS‑rendered).
9.  **Cylex:** Polish business directory (may be protected).
10. **Oferteo:** Business profiles with JSON‑LD.
11. **Firmy.net:** Business directory (limited contact data).
12. **BiznesFinder:** Business profiles with JSON‑LD.

The script can optionally extract email addresses from the websites of the businesses found. All collected and deduplicated data can be saved to Excel (`.xlsx`), CSV, or JSON for easy access and use.

This tool is intended to help streamline lead generation and market research by consolidating information from various platforms into a single structured output.

## Features

*   **Multi-Source Scraping:** Gathers data from Google Places API, Panorama Firm, PKT.pl, Booksy, Fresha, SPAeden, ZnanyLekarz, Fixly, Cylex, Oferteo, Firmy.net, and BiznesFinder.
*   **Targeted Search:** Allows users to specify the industry/query and location for the search.
*   **Email Extraction:** Optionally crawls business websites to find and extract email addresses using regex and by checking common contact pages.
*   **Data Deduplication:** Merges results from all sources and removes duplicates using normalized name/address/phone.
*   **Multiple Output Formats:** Saves results to `.xlsx`, `.csv`, or `.json`.
*   **Caching:** Optional SQLite cache for email extraction to speed up repeated runs.
*   **API Key Management:** Uses a `.env` file to securely manage the Google Maps API key.
*   **Politeness Features:** Implements random User-Agent rotation and delays between requests to minimize server load and avoid blocking.
*   **User-Friendly CLI:** Interactive prompts or full CLI flags.
*   **Robots.txt Option:** Optional respect for `robots.txt` while scraping websites.
*   **Error Handling & Retries:** Retries with backoff for transient network failures.
*   **Headless Fallback (Optional):** Uses a headless browser for JS‑rendered or blocked pages.

## Technologies Used

*   **Python 3.x**
*   **Libraries:**
    *   `requests`: For making HTTP requests to APIs and websites.
    *   `BeautifulSoup4`: For parsing HTML content from websites.
    *   `openpyxl`: For creating and manipulating Excel files.
    *   `python-dotenv`: For loading environment variables from a `.env` file.
    *   Standard Python libraries: `argparse`, `csv`, `json`, `logging`, `sqlite3`, `os`, `time`, `re`, `random`, `urllib.parse`, `urllib.robotparser`.

## Setup and Installation

1.  **Clone the Repository (if applicable):**
    ```bash
    git clone <your-repository-url>
    cd <repository-directory>
    ```

2.  **Create a Virtual Environment (Recommended):**
    ```bash
    python -m venv venv
    source venv/bin/activate  # On Windows: venv\Scripts\activate
    ```

3.  **Install Dependencies:**
    Make sure you have a `requirements.txt` file in your project root. If not, you can create one from the active environment where the script runs correctly:
    ```bash
    pip freeze > requirements.txt
    ```
    Then, install the dependencies:
    ```bash
    pip install -r requirements.txt
    ```
    The `requirements.txt` should contain at least:
    ```
    requests
    beautifulsoup4
    openpyxl
    python-dotenv
    ```

    **Headless note:** If you use `--use-headless`, ensure Google Chrome (or a Chromium‑based browser) is installed. The script uses `undetected-chromedriver` to manage the driver automatically.

4.  **Set up Google Maps API Key:**
    *   You will need a Google Cloud Platform project with the **Places API** enabled.
    *   Create a `.env` file in the root directory of the project.
    *   Add your Google Maps API key to the `.env` file like this:
        ```env
        GOOGLE_MAPS_API_KEY=YOUR_ACTUAL_API_KEY_HERE
        ```
    *   **Important:** Ensure the `.env` file is listed in your `.gitignore` file to prevent your API key from being committed to version control.

5.  **Ensure `.gitignore` is set up:**
    Your `.gitignore` file should include at least:
    ```
    .env
    *.xlsx
    __pycache__/
    venv/
    *.pyc
    ```

## Usage

1.  Navigate to the project directory in your terminal.
2.  Ensure your virtual environment is activated (if you created one).
3.  Run the script (interactive mode):
    ```bash
    python scraper.py
    ```
4.  The script will run a step-by-step wizard:
    *   **Industry/Query** (category list + custom option)
    *   **City/Location** (voivodeship → city list)
    *   **Extract Emails** (yes/no)
5.  At the end, you will be asked for the output filename (default suggestion included).
6.  The script will print progress updates to the console.
7.  Once completed, the results will be saved in the chosen format (default: `results.xlsx`) in the project directory.

### Running with .venv (Windows)

On Windows it's recommended to use the bundled virtual environment so the script uses the correct Python and installed packages.

PowerShell (recommended):
```powershell
# create venv if missing
python -m venv .venv
# activate
& .\.venv\Scripts\Activate.ps1
# install deps (first run)
pip install -r requirements.txt
# run (the script will prompt if saving to the template file)
python scraper.py --output scraped/IdeaMusicLeads.xlsx
```

CMD:
```cmd
.venv\Scripts\activate.bat
python scraper.py --output scraped/IdeaMusicLeads.xlsx
```

You can also simply launch the helper batch file which prefers the `.venv` Python if available:
```cmd
.\run_scraper.bat
```

Note: if the output file is `scraped/IdeaMusicLeads.xlsx` and it already exists, the script will ask:
`Plik scraped\IdeaMusicLeads.xlsx już istnieje. Dopisać do niego? (t/n):`
- answer `t` to merge/append into the template (updates existing rows and adds new ones);
- answer `n` to be prompted for a new filename and create a separate file.

### CLI Flags (Optional)

You can also run the script non-interactively with CLI flags:

```bash
python scraper.py --query "hairdresser" --location "Krakow" --emails --use-google --format xlsx --output results.xlsx
```

Available flags:

*   `--query` — Industry or search query
*   `--location` — City or location
*   `--emails` — Extract emails from websites
*   `--use-google` — Use Google Places API
*   `--format` — Output format: `xlsx`, `csv`, or `json`
*   `--output` — Output filename
*   `--max-pages` — Max pages per source (default: 3)
*   `--respect-robots` — Respect `robots.txt` for website scraping
*   `--cache-db` — SQLite cache filename for email extraction (default: `cache.sqlite`)
*   `--use-headless` — Use headless Chrome for JS/blocked pages (default: on)
*   `--no-headless` — Disable headless browser
*   `--headless-wait` — Seconds to wait after render in headless mode (default: 3)

## Ethical Considerations & Disclaimer

*   **Respect Website Terms of Service:** Always be mindful of the terms of service of the websites you are scraping. This script is provided for educational and demonstrative purposes.
*   **API Usage Limits & Costs:** Be aware of Google Places API usage limits and potential costs associated with your API key. The script includes a `max_google_pages` limit to help manage this.
*   **Rate Limiting:** The script includes random delays and User-Agent rotation as basic politeness measures. Aggressive scraping can lead to IP bans.
*   **Data Privacy:** Be responsible with the data you collect and adhere to relevant data privacy regulations (e.g., GDPR).
*   This tool should be used responsibly and ethically. The author is not responsible for any misuse of this script.

## Runtime Notes & Known Limitations

*   **Per-run logs:** Each run writes a timestamped log file to the `logs/` folder (e.g., `logs/spa_wroclaw_YYYYMMDD_HHMMSS.log`).
*   **Google Places API:** Requires a valid `GOOGLE_MAPS_API_KEY` with Places API enabled. Invalid keys will return `REQUEST_DENIED`.
*   **Headless scraping:** Some sources require JS rendering. The script can use a headless browser (`undetected-chromedriver`) when enabled.
*   **Cylex:** часто blokuje ruch (Cloudflare). Bez proxy wyniki mogą być puste.
*   **Fresha:** lokalizacje używają własnych slugów. W razie 404 skrypt stosuje fallback bez lokalizacji.
*   **Booksy/Moment:** Booksy posiada własne slugi kategorii i miast — w razie braku dopasowania wyniki mogą być puste.
*   **Firmy.net:** link do prawdziwej strony firmy bywa ukryty w JS; ekstrakcja jest best‑effort.
*   **Email extraction:** działa tylko dla stron firm. Jeśli źródło podaje wyłącznie link do katalogu, emaile mogą się nie pojawić.

## Potential Future Improvements

*   More sophisticated email obfuscation decoding.
*   Integration with proxy services for more robust scraping.
*   GUI interface for easier use.
*   Asynchronous requests for improved performance.

---

Feel free to modify this README to better suit any specific nuances of your project or how you intend to present it!
