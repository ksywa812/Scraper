# Multi-Source Business Lead Scraper

## Description

This Python script is designed to automate the collection of business contact information (leads) from multiple online sources for a specified industry and location. It scrapes data from:

1.  **Google Places API:** Fetches business listings, including names, addresses, phone numbers, and websites.
2.  **Panorama Firm (panoramafirm.pl):** A Polish business directory.
3.  **PKT.pl:** Another Polish business directory.

The script can optionally extract email addresses from the websites of the businesses found. All collected and deduplicated data is then saved into an Excel (`.xlsx`) file for easy access and use.

This tool is intended to help streamline lead generation and market research by consolidating information from various platforms into a single structured output.

## Features

*   **Multi-Source Scraping:** Gathers data from Google Places API, Panorama Firm, and PKT.pl.
*   **Targeted Search:** Allows users to specify the industry/query and location for the search.
*   **Email Extraction:** Optionally crawls business websites to find and extract email addresses using regex and by checking common contact pages.
*   **Data Deduplication:** Merges results from all sources and attempts to remove duplicate entries based on business name and address.
*   **Excel Output:** Saves the final, consolidated data into a well-formatted `.xlsx` file, with separate columns for emails.
*   **API Key Management:** Uses a `.env` file to securely manage the Google Maps API key.
*   **Politeness Features:** Implements random User-Agent rotation and delays between requests to minimize server load and avoid blocking.
*   **User-Friendly CLI:** Interactive command-line interface for inputs and confirmations.
*   **Error Handling:** Includes basic error handling for network issues and API errors.

## Technologies Used

*   **Python 3.x**
*   **Libraries:**
    *   `requests`: For making HTTP requests to APIs and websites.
    *   `BeautifulSoup4`: For parsing HTML content from websites.
    *   `openpyxl`: For creating and manipulating Excel files.
    *   `python-dotenv`: For loading environment variables from a `.env` file.
    *   Standard Python libraries: `os`, `time`, `re`, `json`, `random`, `urllib.parse`.

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
3.  Run the script:
    ```bash
    python scraper.py
    ```
4.  The script will then prompt you for the following information:
    *   **Industry/Query:** (e.g., `hairdresser`, `restaurant`, `software company`)
    *   **City/Location:** (e.g., `Krakow`, `Warsaw`)
    *   **Extract Emails? (y/n):** Whether to attempt email extraction from websites.
    *   **Use Google Places API? (y/n):** Whether to include Google Places in the search (requires API key).
5.  If an output file (e.g., `results.xlsx`) already exists, you will be prompted to confirm if you want to overwrite it or provide a new filename.
6.  The script will print progress updates to the console.
7.  Once completed, the results will be saved in an Excel file (default: `results.xlsx`) in the project directory.

## Ethical Considerations & Disclaimer

*   **Respect Website Terms of Service:** Always be mindful of the terms of service of the websites you are scraping. This script is provided for educational and demonstrative purposes.
*   **API Usage Limits & Costs:** Be aware of Google Places API usage limits and potential costs associated with your API key. The script includes a `max_google_pages` limit to help manage this.
*   **Rate Limiting:** The script includes random delays and User-Agent rotation as basic politeness measures. Aggressive scraping can lead to IP bans.
*   **Data Privacy:** Be responsible with the data you collect and adhere to relevant data privacy regulations (e.g., GDPR).
*   This tool should be used responsibly and ethically. The author is not responsible for any misuse of this script.

## Potential Future Improvements

*   Implement `robots.txt` parsing and adherence.
*   More sophisticated email obfuscation decoding.
*   Option to specify different output formats (e.g., CSV, JSON).
*   More advanced error handling and retry mechanisms.
*   Integration with proxy services for more robust scraping.
*   GUI interface for easier use.
*   Asynchronous requests for improved performance.

---

Feel free to modify this README to better suit any specific nuances of your project or how you intend to present it!
