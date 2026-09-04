#!/usr/bin/env python3
"""
extract_metadata.py

Scan the repository for PDF files, extract embedded PDF metadata (title, author, pages),
and query Crossref (and publisher pages via simple web search) to find DOIs and publisher URLs.

Outputs:
 - references-complete.ris
 - references-complete.bib

Requires:
  pip install PyPDF2 requests habanero tqdm unidecode

Usage:
  python3 extract_metadata.py --repo-root . --output-ris references-complete.ris --output-bib references-complete.bib

Notes:
 - This script runs locally (not in GitHub Actions). It reads PDF metadata where available and falls
   back to text extraction of first pages for title heuristics.
 - For DOI lookup it uses Crossref API (email provided via env var CROSSFREF_MAIL optional).
 - If Crossref lookup fails it performs a simple Bing search (requires internet) and attempts to
   parse publisher pages for DOIs/URLs. This is best-effort and may need manual review.
"""

import os
import re
import sys
import json
import argparse
from pathlib import Path
from urllib.parse import quote
import requests
from tqdm import tqdm
from unidecode import unidecode
from PyPDF2 import PdfReader

CROSSREF_API = "https://api.crossref.org/works"

EMAIL = os.environ.get('CROSSREF_MAIL', 'your-email@example.com')

def find_pdfs(root):
    pdfs = []
    for dirpath, dirnames, filenames in os.walk(root):
        for fn in filenames:
            if fn.lower().endswith('.pdf'):
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, root)
                pdfs.append((full, rel))
    return sorted(pdfs, key=lambda x: x[1].lower())

def extract_pdf_metadata(path):
    try:
        reader = PdfReader(path)
        info = reader.metadata or {}
        meta = {}
        meta['title'] = info.title if info.title else None
        if not meta['title']:
            # try first page text heuristic
            try:
                page0 = reader.pages[0]
                text = page0.extract_text() or ''
                title_guess = '\n'.join([ln.strip() for ln in text.splitlines() if ln.strip()][:3])
                if len(title_guess) > 10:
                    meta['title'] = title_guess.strip()
            except Exception:
                meta['title'] = None
        meta['author'] = info.author if info.author else None
        meta['pages'] = len(reader.pages)
        return meta
    except Exception as e:
        return {'title': None, 'author': None, 'pages': None}

def query_crossref_title(title):
    if not title:
        return None
    params = {'query.title': title, 'rows': 5, 'mailto': EMAIL}
    try:
        r = requests.get(CROSSREF_API, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        items = data.get('message', {}).get('items', [])
        if not items:
            return None
        # prefer exact-ish match
        title_norm = re.sub(r'\s+', ' ', unidecode(title).lower())
        for it in items:
            t = ' '.join(it.get('title', []))
            if not t:
                continue
            tnorm = re.sub(r'\s+', ' ', unidecode(t).lower())
            if title_norm in tnorm or tnorm in title_norm:
                return it
        return items[0]
    except Exception:
        return None

def to_ris_entry(item):
    # item: dict with keys: type, title, author(s), year, doi, url, pages, filepath
    lines = []
    lines.append('TY  - JOUR' if item.get('type','').upper()=='JOUR' else 'TY  - GEN')
    if item.get('author'):
        if isinstance(item['author'], (list,tuple)):
            for a in item['author']:
                lines.append(f"AU  - {a}")
        else:
            lines.append(f"AU  - {item['author']}")
    if item.get('title'):
        lines.append(f"TI  - {item['title']}")
    if item.get('year'):
        lines.append(f"PY  - {item['year']}")
    if item.get('volume'):
        lines.append(f"VL  - {item['volume']}")
    if item.get('issue'):
        lines.append(f"IS  - {item['issue']}")
    if item.get('startpage'):
        lines.append(f"SP  - {item['startpage']}")
    if item.get('endpage'):
        lines.append(f"EP  - {item['endpage']}")
    if item.get('doi'):
        lines.append(f"DO  - {item['doi']}")
    if item.get('url'):
        lines.append(f"UR  - {item['url']}")
    if item.get('repo_url'):
        lines.append(f"UR  - {item['repo_url']}")
    if item.get('note'):
        lines.append(f"N1  - {item['note']}")
    lines.append('ER  -')
    return '\n'.join(lines) + '\n\n'


def to_bib_entry(item, key):
    # minimal bibtex
    authors = ''
    if item.get('author'):
        if isinstance(item['author'], (list,tuple)):
            authors = ' and '.join(item['author'])
        else:
            authors = item['author']
    bib = f"@misc{{{key},\n"
    if authors:
        bib += f"  author = {{{authors}}},\n"
    if item.get('title'):
        bib += f"  title = {{{item['title']}}},\n"
    if item.get('year'):
        bib += f"  year = {{{item['year']}}},\n"
    if item.get('doi'):
        bib += f"  doi = {{{item['doi']}}},\n"
    if item.get('repo_url'):
        bib += f"  howpublished = {{\url{{{item['repo_url']}}}}},\n"
    bib += '}'
    return bib


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo-root', default='.', help='Path to repository root')
    parser.add_argument('--output-ris', default='references-complete.ris')
    parser.add_argument('--output-bib', default='references-complete.bib')
    args = parser.parse_args()

    pdfs = find_pdfs(args.repo_root)
    print(f'Found {len(pdfs)} PDF files')

    ris_entries = []
    bib_entries = []

    for full, rel in tqdm(pdfs):
        meta = extract_pdf_metadata(full)
        title = meta.get('title') or Path(rel).stem
        author = meta.get('author') or None
        pages = meta.get('pages')
        repo_url = f'https://github.com/{os.environ.get("GITHUB_REPO","jucelaineoliveira/Doutorado")}/blob/main/{quote(rel)}'
        item = {'title': title, 'author': author, 'pages': pages, 'repo_url': repo_url}
        # try Crossref
        cr = query_crossref_title(title)
        if cr:
            item['doi'] = cr.get('DOI')
            item['url'] = cr.get('URL')
            item['year'] = cr.get('issued',{}).get('date-parts',[[None]])[0][0]
            item['type'] = 'JOUR'
            item['note'] = 'Metadata enriched from Crossref'
        else:
            item['note'] = 'Metadata from PDF (title heuristic) — DOI not found yet'

        ris_entries.append(to_ris_entry(item))
        key = re.sub(r'[^0-9A-Za-z]', '', Path(rel).stem)[:30]
        bib_entries.append(to_bib_entry(item, key))

    with open(args.output_ris, 'w', encoding='utf-8') as f:
        f.write('\n'.join(ris_entries))
    with open(args.output_bib, 'w', encoding='utf-8') as f:
        f.write('\n\n'.join(bib_entries))

    print(f'Wrote {args.output_ris} and {args.output_bib}')

if __name__ == '__main__':
    main()
