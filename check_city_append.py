from openpyxl import load_workbook
from pathlib import Path
p = Path('scraped') / 'IdeaMusicLeads.xlsx'
wb = load_workbook(p)
s = wb[wb.sheetnames[0]]
rows = list(s.iter_rows(values_only=False))
# find rows where cell in col 1 has the city name (uppercase)
for i, row in enumerate(rows):
    v = row[0].value
n = len(rows)
print('Total rows:', n)
for i, row in enumerate(rows[-80:], start=n-80+1):
    first = row[0].value
    status = None
    # try to get 'Status' column value (find header index)
    # find header row first
header = [c.value for c in rows[0]]
try:
    status_idx = header.index('Status')
except ValueError:
    status_idx = None
print('Header Index of Status:', status_idx)
for i, row in enumerate(rows[-30:], start=n-30+1):
    vals = [cell.value for cell in row[:10]]
    print(i, vals)
    # check if color fill on cell
    if row[0].fill and row[0].fill.fill_type:
        if row[0].fill.fill_type == 'solid':
            print('  -> Row has fill color:', row[0].fill.start_color.index)

