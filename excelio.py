"""Read simple Excel (.xlsx) and CSV files for imports. No extra library is needed.

Only the first worksheet is read, as text: one list of cell values per row.
"""
import csv
import io
import re
import zipfile
import xml.etree.ElementTree as ET

from core import RuleError

NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
MAX_ROWS = 5000


def _col_index(ref):
    letters = re.match(r'[A-Z]+', ref or '')
    n = 0
    for ch in (letters.group(0) if letters else 'A'):
        n = n * 26 + ord(ch) - 64
    return n - 1


def _text(el):
    """All text inside an element (rich text runs included)."""
    return ''.join(t.text or '' for t in el.iter(NS + 't'))


def _number(v):
    try:
        f = float(v)
    except ValueError:
        return v
    if f == int(f) and abs(f) < 1e15:
        return str(int(f))
    return f'{f:.6f}'.rstrip('0').rstrip('.')


def read_xlsx(data):
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise RuleError('This is not an Excel .xlsx file. Save it as "Excel Workbook (.xlsx)" or CSV and try again.')
    names = z.namelist()
    shared = []
    if 'xl/sharedStrings.xml' in names:
        root = ET.fromstring(z.read('xl/sharedStrings.xml'))
        shared = [_text(si) for si in root.iter(NS + 'si')]
    sheets = sorted(n for n in names if n.startswith('xl/worksheets/sheet') and n.endswith('.xml'))
    if not sheets:
        raise RuleError('No worksheet found in this Excel file.')
    sheet = 'xl/worksheets/sheet1.xml' if 'xl/worksheets/sheet1.xml' in sheets else sheets[0]
    root = ET.fromstring(z.read(sheet))
    rows = []
    for r in root.iter(NS + 'row'):
        vals = {}
        for i, c in enumerate(r.iter(NS + 'c')):
            idx = _col_index(c.get('r')) if c.get('r') else i
            t = c.get('t')
            v = c.find(NS + 'v')
            if t == 's' and v is not None:
                val = shared[int(v.text)] if v.text and int(v.text) < len(shared) else ''
            elif t == 'inlineStr':
                val = _text(c)
            elif t == 'b' and v is not None:
                val = 'yes' if v.text == '1' else 'no'
            elif v is not None and v.text is not None:
                val = v.text if t == 'str' else _number(v.text)
            else:
                val = ''
            vals[idx] = val.strip()
        if vals:
            rows.append([vals.get(i, '') for i in range(max(vals) + 1)])
        if len(rows) > MAX_ROWS + 1:
            raise RuleError(f'The file has more than {MAX_ROWS:,} rows. Split it into smaller files.')
    return rows


def read_csv(data):
    text = None
    for enc in ('utf-8-sig', 'cp1252', 'latin-1'):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    sample = text[:2000]
    delim = ';' if sample.count(';') > sample.count(',') else ','
    rows = [[c.strip() for c in r] for r in csv.reader(io.StringIO(text), delimiter=delim)]
    if len(rows) > MAX_ROWS + 1:
        raise RuleError(f'The file has more than {MAX_ROWS:,} rows. Split it into smaller files.')
    return rows


def read_table(filename, data):
    """Rows of an uploaded .xlsx or .csv file, without completely empty rows."""
    name = (filename or '').lower()
    if name.endswith('.csv'):
        rows = read_csv(data)
    elif name.endswith('.xlsx') or data[:2] == b'PK':
        rows = read_xlsx(data)
    elif name.endswith('.xls'):
        raise RuleError('Old .xls files cannot be read. In Excel use "Save as" → "Excel Workbook (.xlsx)".')
    else:
        raise RuleError('Choose an Excel (.xlsx) or CSV file.')
    return [r for r in rows if any(c.strip() for c in r)]


def header_map(header, fields):
    """Match the header row to field names. fields = {field: [accepted header texts]}. Returns {field: column}."""
    norm = [re.sub(r'[^a-z0-9]+', ' ', h.lower().replace('*', '')).strip() for h in header]
    out = {}
    for field, names in fields.items():
        for i, h in enumerate(norm):
            if h in names and i not in out.values():
                out[field] = i
                break
    return out
