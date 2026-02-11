import pandas as pd
from pathlib import Path
base = Path(__file__).resolve().parent
file_a = base / 'scraped' / 'test_spa_wroclaw.xlsx'
df = pd.read_excel(file_a)
print('Columns:', list(df.columns))
print('\nFirst 6 rows (Name, Email 1, Email 2, Email 3, Other Emails, Sources):\n')
print(df[['Name','Email 1','Email 2','Email 3','Other Emails','Sources']].head(6).to_string(index=False))
