# --- START OF FILE scraper.py ---

import requests
import openpyxl
import time
import os
import re
import json
from bs4 import BeautifulSoup
from urllib.parse import urlparse, quote_plus
from dotenv import load_dotenv
import random

# Load API key from .env file
load_dotenv()
API_KEY = os.getenv('GOOGLE_MAPS_API_KEY')

OUTPUT_FILE = 'results.xlsx' # Changed from 'wyniki.xlsx'

# List of User-Agents for rotation to avoid blocking
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Safari/605.1.15',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:90.0) Gecko/20100101 Firefox/90.0',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/92.0.4515.107 Safari/537.36'
]

def get_random_user_agent():
    """Returns a random User-Agent string."""
    return random.choice(USER_AGENTS)

def scrape_panorama_firm(query, location, max_pages=3):
    """Scrapes data from Panorama Firm website."""
    results = []
    
    try:
        print(f"Scraping data from Panorama Firm for: {query} in {location}")
        
        # Build the search URL
        search_query = f"{query} {location}"
        encoded_query = quote_plus(search_query)
        base_url = "https://panoramafirm.pl"
        
        for page in range(1, max_pages + 1):
            url = f"{base_url}/szukaj?k={encoded_query}&o={page}"
            
            headers = {
                'User-Agent': get_random_user_agent(),
                'Accept': 'text/html,application/xhtml+xml,application/xml',
                'Accept-Language': 'en-US,en;q=0.9,pl;q=0.8', # Prioritize English
                'Referer': 'https://panoramafirm.pl/',
            }
            
            print(f"Fetching page {page} from Panorama Firm...")
            response = requests.get(url, headers=headers, timeout=15)
            response.raise_for_status() # Raise an exception for HTTP errors
            
            soup = BeautifulSoup(response.text, 'html.parser')
            businesses = soup.select('div.card.company-item')
            
            if not businesses:
                print(f"No more results found on page {page}")
                break
                
            for business in businesses:
                try:
                    # Basic data
                    name_elem = business.select_one('h2.company-name')
                    name = name_elem.text.strip() if name_elem else "Unknown Name"
                    
                    # Address
                    address_elem = business.select_one('div.address')
                    address = address_elem.text.strip() if address_elem else ""
                    
                    # Phone
                    phone_elem = business.select_one('a[data-company-phone]')
                    phone = phone_elem.get('data-company-phone', "") if phone_elem else ""
                    
                    # Website
                    website_elem = business.select_one('a.icon-website')
                    website = website_elem.get('href', "") if website_elem else ""
                    
                    # Check if it's not an internal Panorama Firm link
                    if website and not website.startswith(('http://', 'https://')):
                        website = ""
                    
                    result = {
                        'name': name,
                        'formatted_address': address,
                        'formatted_phone_number': phone,
                        'website': website,
                        'emails': [] # Initialize emails list
                    }
                    
                    results.append(result)
                    
                except Exception as e:
                    print(f"Error processing a business entry: {e}")
            
            print(f"Found {len(businesses)} businesses on page {page}")
            time.sleep(random.uniform(1.5, 3.0))  # Random delay between pages
            
        return results
    
    except Exception as e:
        print(f"Error during Panorama Firm scraping: {e}")
        return results

def scrape_pkt_pl(query, location, max_pages=3):
    """Scrapes data from PKT.pl website."""
    results = []
    
    try:
        print(f"Scraping data from PKT.pl for: {query} in {location}")
        
        # Build the search URL
        search_query = f"{query} {location}"
        encoded_query = quote_plus(search_query)
        base_url = "https://www.pkt.pl"
        
        for page in range(1, max_pages + 1):
            url = f"{base_url}/szukaj/{encoded_query}/{page}"
            
            headers = {
                'User-Agent': get_random_user_agent(),
                'Accept': 'text/html,application/xhtml+xml,application/xml',
                'Accept-Language': 'en-US,en;q=0.9,pl;q=0.8',
                'Referer': 'https://www.pkt.pl/',
            }
            
            print(f"Fetching page {page} from PKT.pl...")
            response = requests.get(url, headers=headers, timeout=15)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            businesses = soup.select('li.list-items')
            
            if not businesses:
                print(f"No more results found on page {page}")
                break
                
            for business in businesses:
                try:
                    # Basic data
                    name_elem = business.select_one('h2.company-name a')
                    name = name_elem.text.strip() if name_elem else "Unknown Name"
                    
                    # Address
                    address_elem = business.select_one('address.rest-address')
                    address = address_elem.text.strip() if address_elem else ""
                    
                    # Phone
                    phone_elem = business.select_one('a.icon-telephone')
                    phone = phone_elem.text.strip() if phone_elem else ""
                    
                    # Website
                    website_elem = business.select_one('a.company-url')
                    website = website_elem.get('href', "") if website_elem else ""
                    
                    # Check if it's not an internal PKT.pl link
                    if website and base_url in website:
                        website = ""
                    
                    result = {
                        'name': name,
                        'formatted_address': address,
                        'formatted_phone_number': phone,
                        'website': website,
                        'emails': []
                    }
                    
                    results.append(result)
                    
                except Exception as e:
                    print(f"Error processing a business entry: {e}")
            
            print(f"Found {len(businesses)} businesses on page {page}")
            time.sleep(random.uniform(1.5, 3.0))  # Random delay between pages
            
        return results
    
    except Exception as e:
        print(f"Error during PKT.pl scraping: {e}")
        return results

def search_places(query, location):
    """Searches for places using Google Places API."""
    if not API_KEY:
        print("Google Maps API key not found. Skipping Google Places search.")
        return [], None
    
    try:
        url = 'https://maps.googleapis.com/maps/api/place/textsearch/json'
        params = {'query': f'{query} in {location}', 'key': API_KEY}
        resp = requests.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        
        if data.get('status') not in ['OK', 'ZERO_RESULTS']: # Check for OK or ZERO_RESULTS
            print(f"API Error: {data.get('status')} - {data.get('error_message', '')}")
            return [], None
            
        return data.get('results', []), data.get('next_page_token')
    except requests.exceptions.RequestException as e:
        print(f"Error during place search: {e}")
        return [], None

def search_next_page(next_page_token):
    """Fetches the next page of results from Google Places API."""
    if not API_KEY:
        return [], None
        
    try:
        url = 'https://maps.googleapis.com/maps/api/place/textsearch/json'
        params = {'pagetoken': next_page_token, 'key': API_KEY}
        resp = requests.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        
        if data.get('status') not in ['OK', 'ZERO_RESULTS']:
            print(f"API Error: {data.get('status')} - {data.get('error_message', '')}")
            return [], None
            
        return data.get('results', []), data.get('next_page_token')
    except requests.exceptions.RequestException as e:
        print(f"Error fetching next page: {e}")
        return [], None

def get_place_details(place_id):
    """Fetches place details from Google Places API."""
    if not API_KEY:
        return {}
        
    try:
        url = 'https://maps.googleapis.com/maps/api/place/details/json'
        params = {
            'place_id': place_id,
            'fields': 'name,formatted_address,formatted_phone_number,website', # Requested fields
            'key': API_KEY
        }
        resp = requests.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        
        if data.get('status') != 'OK':
            print(f"Error fetching details for place {place_id}: {data.get('status')}")
            return {}
            
        return data.get('result', {})
    except requests.exceptions.RequestException as e:
        print(f"Error fetching details for place {place_id}: {e}")
        return {}

def extract_emails_from_website(url):
    """Extracts email addresses from a website."""
    if not url:
        return []
    
    try:
        print(f"Fetching emails from website: {url}")
        # Add protocol if missing
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
            
        headers = {
            'User-Agent': get_random_user_agent(),
            'Accept': 'text/html,application/xhtml+xml,application/xml',
            'Accept-Language': 'en-US,en;q=0.9,pl;q=0.8',
        }
        
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        
        # Use regex to find email addresses
        email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,7}\b' # Updated TLD length
        emails = re.findall(email_pattern, response.text)
        
        # Also, get emails from contact pages if available
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Check subpages like "kontakt" or "contact"
        contact_links = []
        base_domain = '{uri.scheme}://{uri.netloc}'.format(uri=urlparse(url))
        for link in soup.find_all('a', href=True):
            href = link['href']
            if any(keyword in href.lower() for keyword in ['kontakt', 'contact', 'about', 'o-nas']): # Added Polish 'o-nas'
                # Add full URL if it's relative
                if href.startswith('/'):
                    contact_links.append(base_domain + href)
                elif href.startswith('http'):
                    contact_links.append(href)
                else: # Handle cases like 'contact.html'
                    contact_links.append(base_domain + '/' + href)
        
        # Remove duplicate contact links
        contact_links = list(set(contact_links))
        
        # Visit found contact pages (limit to 2 to avoid excessive requests)
        for contact_url in contact_links[:2]:
            try:
                contact_response = requests.get(contact_url, headers={'User-Agent': get_random_user_agent()}, timeout=10)
                contact_response.raise_for_status()
                contact_emails = re.findall(email_pattern, contact_response.text)
                emails.extend(contact_emails)
            except Exception as e:
                print(f"Could not fetch contact page {contact_url}: {e}")
        
        # Check JavaScript and other page elements that might contain hidden emails
        # Emails are often obfuscated from bots
        for script_tag in soup.find_all(['script', 'noscript']): # Changed 'script' to 'script_tag'
            if script_tag.string:
                script_emails = re.findall(email_pattern, script_tag.string)
                emails.extend(script_emails)
        
        # Check 'data-email' attribute often used for hidden emails
        for element in soup.find_all(attrs={"data-email": True}):
            data_email = element.get('data-email')
            if data_email and '@' in data_email: # Ensure data_email is not None
                emails.append(data_email)
        
        # Remove duplicates and filter emails
        unique_emails = list(set(emails))
        
        # Filter out common generic or spammy domains
        filtered_emails = [email for email in unique_emails if not any(
            domain in email.lower() for domain in ['example.com', 'domain.com', 'yourmail.com', 'wixpress.com', 'sentry.io'] # Added more common spam/service domains
        )]
        
        print(f"Found {len(filtered_emails)} unique email addresses.")
        return filtered_emails
    
    except Exception as e:
        print(f"Error fetching emails from {url}: {e}")
        return []

def merge_results(google_results, panorama_results, pkt_results):
    """Merges results from different sources and removes duplicates."""
    all_results = []
    all_results.extend(google_results)
    all_results.extend(panorama_results)
    all_results.extend(pkt_results)
    
    # Remove duplicates based on name and address
    unique_results = []
    unique_keys = set()
    
    for result in all_results:
        name = result.get('name', '').strip().lower() # Add strip() for consistency
        address_parts = result.get('formatted_address', '').lower().split(',')
        # Use first part of address (street) and name for a more robust key
        simple_address = address_parts[0].strip() if address_parts else ""
        key = f"{name}|{simple_address}" # Simplified key for better deduplication
        
        if key not in unique_keys and name and simple_address: # Ensure name and address part are present
            unique_keys.add(key)
            unique_results.append(result)
        elif name and not simple_address and f"{name}|" not in unique_keys: # Handle cases with name but no address
             unique_keys.add(f"{name}|")
             unique_results.append(result)

    return unique_results

def save_to_excel(data, filename=OUTPUT_FILE):
    """Saves data to an Excel file."""
    # Check if file exists
    if os.path.exists(filename):
        confirm = input(f"File {filename} already exists. Overwrite? (y/n): ").lower()
        if confirm != 'y':
            new_name = input("Enter a new filename: ")
            if new_name:
                filename = new_name if new_name.endswith('.xlsx') else f"{new_name}.xlsx"
    
    try:
        wb = openpyxl.Workbook()
        ws = wb.active
        
        # Headers with three separate columns for email addresses
        ws.append(['Name', 'Address', 'Phone', 'Website', 'Email 1', 'Email 2', 'Email 3', 'Other Emails'])
        
        for item in data:
            # Get the list of emails
            emails = item.get('emails', [])
            
            # Prepare list of values to add to the row
            row_data = [
                item.get('name', ''),
                item.get('formatted_address', ''),
                item.get('formatted_phone_number', ''),
                item.get('website', '')
            ]
            
            # Add the first three emails in separate columns
            for i in range(3):
                if i < len(emails):
                    row_data.append(emails[i])
                else:
                    row_data.append('')  # Empty column if no email
            
            # Add remaining emails in the last column, comma-separated
            if len(emails) > 3:
                row_data.append(', '.join(emails[3:]))
            else:
                row_data.append('')
                
            ws.append(row_data)
            
        # Adjust column widths
        for column_cells in ws.columns: # Changed 'column' to 'column_cells' for clarity
            max_length = 0
            column_letter = openpyxl.utils.get_column_letter(column_cells[0].column)
            for cell in column_cells:
                try:
                    if cell.value is not None and len(str(cell.value)) > max_length: # Check for None
                        max_length = len(str(cell.value))
                except: # Broad except, consider specific exceptions if known
                    pass
            adjusted_width = (max_length + 2)
            ws.column_dimensions[column_letter].width = min(adjusted_width, 60)  # Limit max width slightly more
        
        wb.save(filename)
        print(f'✅ Saved {len(data)} records to {filename}')
    except Exception as e:
        print(f"Error saving to Excel: {e}")

def main():
    query = input("Enter the industry (e.g., hairdresser): ").strip()
    location = input("Enter the city (e.g., Krakow): ").strip()
    
    scrape_emails_choice = input("Do you want to extract emails from websites? (y/n): ").lower() == 'y'
    use_google_choice = input("Do you want to use Google Places API? (y/n): ").lower() == 'y'
    
    all_google_details = []
    all_panorama_results = []
    all_pkt_results = []
    
    # Fetch data from Panorama Firm (always)
    print("\n=== Fetching data from Panorama Firm ===")
    all_panorama_results = scrape_panorama_firm(query, location)
    
    # Fetch data from PKT.pl (always)
    print("\n=== Fetching data from PKT.pl ===")
    all_pkt_results = scrape_pkt_pl(query, location)
    
    # Fetch data from Google Places (optional)
    if use_google_choice and API_KEY:
        print("\n=== Fetching data from Google Places API ===")
        next_page_token_val = None # Renamed 'token' to avoid conflict with token module
        page_count = 0
        max_google_pages = 3  # Limit number of pages to avoid API limits/costs
        
        try:
            while page_count < max_google_pages:
                if next_page_token_val:
                    print(f"Fetching page {page_count + 1} from Google Places...")
                    time.sleep(2) # API best practice: wait before requesting next page
                    results, next_page_token_val = search_next_page(next_page_token_val)
                else:
                    results, next_page_token_val = search_places(query, location)
                    
                if not results:
                    if page_count == 0: # Only print if no results on the first try
                        print("No results found in Google Places.")
                    break # Exit loop if no results
                    
                print(f"Found {len(results)} places on page {page_count + 1}")
                
                for i, place in enumerate(results):
                    place_id_val = place.get('place_id') # Renamed 'pid'
                    if place_id_val:
                        print(f"Fetching details {i+1}/{len(results)}: {place.get('name', 'Unknown Name')}")
                        details = get_place_details(place_id_val)
                        
                        if details:
                            details['emails'] = [] # Initialize emails for Google results
                            all_google_details.append(details)
                            
                        time.sleep(random.uniform(0.2, 0.5)) # Shorter delay for details
                
                page_count += 1
                if not next_page_token_val: # If no more pages
                    break
        except Exception as e:
            print(f"Error fetching data from Google Places: {e}")
    
    # Merge all results
    all_results = merge_results(all_google_details, all_panorama_results, all_pkt_results)
    print(f"\nAfter deduplication, we have {len(all_results)} unique businesses.")
    
    # If user wants emails, fetch them for each business with a website URL
    if scrape_emails_choice:
        print("\n=== Fetching emails from websites ===")
        total_with_website = sum(1 for result in all_results if result.get('website'))
        print(f"Found {total_with_website} businesses with website addresses.")
        
        processed_websites = 0
        for result in all_results: # No need for index 'i' if not used
            website = result.get('website')
            if website:
                processed_websites += 1
                print(f"[{processed_websites}/{total_with_website}] Fetching emails for: {result.get('name', 'Unknown Name')}")
                emails = extract_emails_from_website(website)
                result['emails'] = emails
                # Delay to avoid overloading servers
                time.sleep(random.uniform(1.0, 2.0))
    
    # Save all data to Excel file
    if all_results:
        save_to_excel(all_results)
    else:
        print("No data to save.")

if __name__ == '__main__':
    main()