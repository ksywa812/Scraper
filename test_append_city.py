from scraper import save_to_excel

# Small synthetic test item for insertion under Bydgoszcz
item = {
    'name': 'TEST APPEND CITY SPA',
    'formatted_address': 'ul. Testowa 1, 85-001 Bydgoszcz',
    'formatted_phone_number': '500 500 500',
    'emails': ['append-test@example.com'],
    'sources': ['testsource']
}

save_to_excel([item], filename='scraped/IdeaMusicLeads.xlsx', city='Bydgoszcz', append=True)
print('Test append executed')