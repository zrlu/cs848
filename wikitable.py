# -*- coding: utf-8 -*-

import requests
from bs4 import BeautifulSoup as BS, NavigableString, Tag
import pprint
import sys
import io
import operator
from prettytable import PrettyTable
from html_table import HTMLTableParser
from logger import get_logger
import argparse
import os
import re
import urllib
from rules import summary_row_keywords, cell_remove_special_symbols, cell_replace_special_symbols
import pandas as pd
import pdb
import unicodedata
from unidecode import unidecode 

class WikiTable:

    JOIN = 'JOIN'
    LAST = 'LAST'
    
    def __init__(self, soup, log, name='default_name'):
        self.soup = soup
        self.title = None
        self.headers = []
        self.columns = []
        self.unmerged_table = []
        self.dataframe = None
        self.isvalid = False
        self.log = log
        self.string_headers_type = self.JOIN
        self.name = name
    
    def _clean_text(self, text):
        for pattern, repl in cell_replace_special_symbols:
            text = re.sub(pattern, repl, text)
        text = unidecode(text)
        text = re.sub(cell_remove_special_symbols, '', text.strip())
        return text

    def _to_float(self, texts):
        try:
            return float(texts)
        except TypeError:
            return None
        except ValueError:
            return None

    def _to_int(self, texts):
        try:
            return int(texts)
        except TypeError:
            return None
        except ValueError:
            return None

    def _remove_reference(self, element):
        sups = element.find_all("sup", class_="reference")
        if sups:
            for sup in sups:
                sup.decompose()

    def _extract_header_cell(self, element):
        # abbr = element.find('abbr')
        # if abbr:
        #     return abbr.get('title').strip()
        return self._clean_text(element.text)
    
    def _to_numeric(self, text):
        no_comma = text.replace(',', '')
        return self._to_int(no_comma) or self._to_float(no_comma)

    def _parse_currency(self, text):
        if text.startswith('$'):
            return self._to_numeric(text[1:].lstrip())
        return None
    
    def _parse_percentage(self, text):
        if text.endswith('%'):
            return self._to_numeric(text[:1].rstrip()) / 100
        return None

    def _is_not_available(self, text):
        return re.match(re.compile(not_available, re.IGNORECASE), text) is not None

    def _extract_data_cell(self, element):
        text = self._clean_text(element.text)
        extracted = self._to_numeric(text)
        if extracted is not None:
            return extracted
        extracted = self._parse_currency(text)
        if extracted is not None:
            return extracted
        extracted = self._parse_percentage(text)
        if extracted is not None:
            return extracted

        table = element.find("table") # nested table?
        if table:
            # texts = []
            # for cell in table.find_all("td"):
            #     texts.append(self.extract_data_cell(cell))
            # return "; ".join(texts)
            return None
        if not text:
            # children = list(element.children)
            # for child in children:
            #     if child.name == 'span':
            #         if child.get('title'):
            #             return child.get('title')
            return None

        return text

    def _is_row_th(self, th):
        return th.get("scope") == 'row'

    def _parse_headers(self):
        headers = []
        row_idx = 0
        while 1:
            row = []
            stop = False
            for col in self.unmerged_table:
                cell = col[row_idx]
                if cell.name == 'td':
                    stop = True
                    break
                if cell.name == 'th':
                    string = self._extract_header_cell(cell)
                    if self._to_numeric(string) is not None:
                        row_idx += 1
                        stop = True
                        break
                    row.append(string)
            if stop:
                row_idx -= 1
                break
            if row:
                headers.append(row)
            row_idx += 1

        if not headers:
            raise Exception("Header not found.")

        return list(map(list, zip(*headers))), row_idx
 
    def _is_summary_row(self, row):
        for data in row:
            if type(data) == str:
                if re.match(re.compile(summary_row_keywords, re.IGNORECASE), data) is not None:
                    return True, data
        return False, None

    def _remove_row(self, i):
        for col in self.columns:
            del col[i]

    def _tag_count(self, tags, tagname):
        count = 0
        for tag in tags:
            if tag.name == tagname:
                count += 1
        return count

    def _parse_data(self, row_idx):
        columns = []
        
        for col in self.unmerged_table:
            columns.append(list(map(self._extract_data_cell, col[row_idx+1:])))

        return columns

    def _is_empty(self, value):
        return value == '' or value is None
    
    def _is_empty_list(self, lst):
        return all(map(lambda value: self._is_empty(value), lst))
    
    def _remove_summary_rows(self):
        cur = 0
        removed = 0
        old_idx = 0
        while cur < self.count_rows():
            is_summary, s = self._is_summary_row(self.get_row(cur))
            if is_summary:
                self.log.info("Row {} looks like a summary row with keyword '{}', removed.".format(old_idx+1, s))
                self._remove_row(cur)
                removed += 1
                old_idx += 1
                continue
            cur += 1
            old_idx += 1
        self.log.info("Removed {} summary rows.".format(removed))

    def parse(self):
        try:
            parser = HTMLTableParser()
            parser.parse_soup(self.soup)
            self._remove_reference(self.soup)
            self.unmerged_table = parser.get_columns()
        except Exception as e:
            self.log.warn(e)
            self.log.warn("HTMLTableParser: unable to parse raw html table.")
            return False
        try:
            self.headers, row_idx = self._parse_headers()
        except Exception as e:
            self.log.warn(e)
            self.log.warn("Unable to parse header.")
            return False
        try:
            self.columns = self._parse_data(row_idx)
        except Exception as e:
            self.log.warn(e)
            self.log.warn("Unable to parse data.")
            return False
        
        if not all(len(self.columns[0]) == len(col) for col in self.columns):
            self.log.warn("Columns don't have the same number of entries. Expect {}".format(len(self.columns[0])))
            return False

        self._remove_summary_rows()

        self.log.debug(self.string_headers())
        self.log.debug(self.columns)

        mapping = {}
        for i, h in enumerate(self.string_headers()):
            if not self._is_empty_list(self.columns[i]):
                mapping[h] = self.columns[i]

        nattr = len(mapping)

        self.dataframe = pd.DataFrame(mapping)
        self.dataframe.infer_objects()

        if self.dataframe.empty:
            self.isvalid = False
            return False

        self.isvalid = True
        return True
    
    def string_headers(self):
        if self.string_headers_type == self.JOIN:
            return [":".join(header) for header in self.headers]
        return [header[-1] for header in self.headers]

    def count_rows(self):
        return len(self.columns[0])
    
    def count_cols(self):
        return len(self.columns)
    
    def get_row(self, i):
        row = [self.columns[j][i] for j in range(self.count_cols())]
        return row

    def iter_row(self):
        for i in range(self.count_rows()):
            yield self.get_row(i)
    
    def __iter__(self):
        return self.iter_row()

    def __str__(self):
        if not self.isvalid:
            return "INVALID TABLE"
        return str(self.dataframe)


class TableNameFactory:

    def __init__(self):
        self.counter = 0
    
    def get_name(self):
        name = 'table_{0:05d}'.format(self.counter)
        self.counter += 1
        return name


class WikiPage:

    def __init__(self, url, log):
        self.url = url
        self.tables = []
        self.log = log
        self.table_name_factory = TableNameFactory()
    
    def _get_table_name(self, table):
        return None

    def parse_tables(self):
        self.log.info("GET " + self.url)
        r = requests.get(self.url)
        self.log.info("Response: {}".format(r.status_code))
        soup = BS(r.content, features="html.parser", from_encoding='utf-8')
        tables = soup.findAll("table", attrs={"class": "wikitable"})
        self.log.info("{} table(s) found.".format(len(tables)))
        nvalid = 0
        for i, t in enumerate(tables):
            self.log.info("Parsing table {}/{}...".format(i+1, len(tables)))
            table_name = self._get_table_name(t) or self.table_name_factory.get_name()
            wtable = WikiTable(t, self.log, name=table_name)
            wtable.parse()
            if wtable.isvalid:
                nvalid += 1
                self.tables.append(wtable)
                self.log.info("=== TABLE ({}/{}) ===".format(i+1, len(tables)))
                self.log.info("Number of attributes: {}".format(wtable.count_cols()))
                self.log.info("Number of rows: {}".format(wtable.count_rows()))
                self.log.info("\n{}".format(str(wtable)))
                self.log.info("\n{}".format(str(wtable.dataframe.dtypes)))
                self.log.info("=== END OF TABLE ===")
        self.log.info("{}/{} tables are valid.".format(nvalid, len(tables)))
    
    def save(self, outpath='.'):
        for i, wtable in enumerate(self.tables):
            self.log.debug("mkdir -p {}".format(outpath))
            os.makedirs(outpath, exist_ok=True)
            fp = os.path.join(outpath, wtable.name + '.csv'.format(i))
            self.log.info("Write to file {}".format(fp))
            wtable.dataframe.to_csv(fp, index=False)

if __name__ == "__main__":

    pass
