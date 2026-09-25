#!/usr/bin/env python3

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.request import Request, urlopen


CHAPTER_URL    = 'https://sacred-texts.com/egy/pyt/pyt{:02d}.htm'
FIRST_CHAPTER  =  4
LAST_CHAPTER   = 62
PAGE_NUMBER_RE = re.compile(r'^p\.\s+[ivxlcdm0-9]+$', re.IGNORECASE)

# Range headings introduce several utterances; only singular headings select one.
UTTERANCE_RE = re.compile(r'^Utterances?,?\s+(\d+)\.?$', re.IGNORECASE)
UTTERANCE_RANGE_RE = re.compile(
   r'^Utterances\s+(\d+)\s*[-–]\s*(\d+)\.?(?:\s*(\d+)\.)?$',
   re.IGNORECASE,
)
NUMBERED_HEADING_RE = re.compile(r'^(\d{1,4})[A-Za-z]?\.?$')
COMBINED_HEADING_RE = re.compile(
   r'^(\d{1,4})\.\s+(\d{1,4}\s*[A-Za-z](?:\s*[-–]\s*[A-Za-z])?\.)\s+(.+)$'
)
HEADING_NOTE_RE = re.compile(r'^(\d{1,4})\s+\([^)]*\)\.?$')
SENTENCE_RE = re.compile(
   r'^(\d{1,4}\s*[A-Za-z](?:\s*[-–]\s*[A-Za-z])?|\d{1,4})'
   r'\s*[.)]\s*(.*)$'
)
LOOSE_SENTENCE_RE = re.compile(
   r'^(\d{1,4}(?:\s*\+\s*[0-9A-Za-z]+)?|\d{1,4}\s*[A-Za-z])\s+(.+)$'
)


@dataclass
class Node:
   tag: str
   attrs: dict[str, str]
   children: list['Node | str'] = field(default_factory=list)

   def text(self) -> str:
      return ''.join(
         child if isinstance(child, str) else child.text()
         for child in self.children
      )

   def has_tag(self, tag: str) -> bool:
      return self.tag == tag or any(
         isinstance(child, Node) and child.has_tag(tag)
         for child in self.children
      )


class PageParser(HTMLParser):
   """Build enough of the page tree to select the article prose."""

   VOID_TAGS = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
                'link', 'meta', 'param', 'source', 'track', 'wbr'}

   def __init__(self) -> None:
      super().__init__(convert_charrefs=True)
      self.root = Node('root', {})
      self.stack = [self.root]

   def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
      node = Node(tag, {key: value or '' for key, value in attrs})
      self.stack[-1].children.append(node)
      if tag not in self.VOID_TAGS:
         self.stack.append(node)

   def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
      self.handle_starttag(tag, attrs)
      if tag not in self.VOID_TAGS and len(self.stack) > 1:
         self.stack.pop()

   def handle_endtag(self, tag: str) -> None:
      for index in range(len(self.stack) - 1, 0, -1):
         if self.stack[index].tag == tag:
            del self.stack[index:]
            return

   def handle_data(self, data: str) -> None:
      self.stack[-1].children.append(data)


def descendants(node: Node, tags: set[str]) -> list[Node]:
   found = []
   for child in node.children:
      if isinstance(child, Node):
         if child.tag in tags:
            found.append(child)
         found.extend(descendants(child, tags))
   return found


def find_reader_prose(root: Node) -> Node | None:
   for node in descendants(root, {'article', 'div', 'section'}):
      if node.attrs.get('data-slot') == 'reader-prose':
         return node
   return None


def normalize_text(text: str) -> str:
   return ' '.join(text.split())


def extract_paragraphs(html: str) -> list[tuple[str, bool]]:
   parser = PageParser()
   parser.feed(html)
   prose = find_reader_prose(parser.root)
   if prose is None:
      raise ValueError('could not find the article prose container')

   return [
      (normalize_text(node.text()), node.has_tag('em') or node.has_tag('i'))
      for node in descendants(prose, {'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'})
      if normalize_text(node.text())
   ]


def fetch(url: str) -> str:
   request = Request(
      url,
      headers={'User-Agent': 'pyramid-texts-downloader/1.0'},
   )
   with urlopen(request, timeout=30) as response:
      return response.read().decode('utf-8')


def sentence_from_line(line: str) -> str | None:
   match = SENTENCE_RE.match(line)
   if match is None:
      match = LOOSE_SENTENCE_RE.match(line)
   if match is None:
      return None

   # Keep sentence markers free of trailing punctuation in the output.
   marker = re.sub(r'\s+', '', match.group(1))
   body = match.group(2).strip()
   return f'{marker}\t{body}' if body else f'{marker}\t'


def parse_chapter(
   html: str,
   utterances: dict[int, list[str]],
   active_utterances: list[int] | None = None,
) -> tuple[list[str], list[int]]:
   warnings = []
   active_utterances = active_utterances or []

   for line, has_emphasis in extract_paragraphs(html):
      if PAGE_NUMBER_RE.fullmatch(line):
         continue

      heading = UTTERANCE_RE.fullmatch(line)
      if heading:
         active_utterances = [int(heading.group(1))]
         utterances.setdefault(active_utterances[0], [])
         continue

      range_heading = UTTERANCE_RANGE_RE.fullmatch(line)
      if range_heading:
         start = int(range_heading.group(1))
         end = int(range_heading.group(2))
         first = range_heading.group(3)
         active_utterances = [int(first)] if first else list(range(start, end + 1))
         for number in active_utterances:
            utterances.setdefault(number, [])
         continue

      if line[:1].isdigit() and 'utterance' in line.lower():
         continue

      numbered_heading = NUMBERED_HEADING_RE.fullmatch(line)
      if has_emphasis and numbered_heading:
         active_utterances = [int(numbered_heading.group(1))]
         utterances.setdefault(active_utterances[0], [])
         continue

      if numbered_heading and line.endswith('.'):
         active_utterances = [int(numbered_heading.group(1))]
         utterances.setdefault(active_utterances[0], [])
         continue

      note_heading = HEADING_NOTE_RE.fullmatch(line)
      if has_emphasis and note_heading:
         active_utterances = [int(note_heading.group(1))]
         utterances.setdefault(active_utterances[0], [])
         continue

      combined_heading = COMBINED_HEADING_RE.fullmatch(line)
      if has_emphasis and combined_heading:
         active_utterances = [int(combined_heading.group(1))]
         utterances.setdefault(active_utterances[0], [])
         line = f'{combined_heading.group(2)} {combined_heading.group(3)}'

      sentence = sentence_from_line(line)
      if sentence is None:
         continue
      if not active_utterances:
         warnings.append(f'ignored sentence before an utterance heading: {line}')
         continue
      for number in active_utterances:
         utterances[number].append(sentence)

   return warnings, active_utterances


def write_utterances(output_dir: Path, utterances: dict[int, list[str]]) -> None:
   output_dir.mkdir(parents=True, exist_ok=True)
   for number, lines in sorted(utterances.items()):
      path = output_dir / f'{number:03d}.txt'
      # Keep utterances with no extracted sentences as truly empty files.
      content = '\n'.join(lines)
      path.write_text(f'{content}\n' if content else '', encoding='utf-8')


def correct_source_numbering(utterances: dict[int, list[str]]) -> None:
 # Separate the misplaced 497 section from the legitimate 457 content.
   source_lines = utterances.get(457, [])
   target_lines = utterances.get(497, [])
   if source_lines or target_lines:
      lines = source_lines + target_lines
      actual_457 = [
         line for line in lines
         if not re.match(r'^1067[A-Za-z]?\t', line)
      ]
      actual_497 = [
         line for line in lines
         if re.match(r'^1067[A-Za-z]?\t', line)
      ]

      if actual_497:
         utterances[457] = actual_457
         utterances[497] = actual_497

 # Discard the duplicate 979 bucket; 979 is a text marker within utterance 478.
   misplaced_575 = utterances.pop(979, None)
   if misplaced_575 is not None:
      utterances.setdefault(575, misplaced_575)


output_dir = Path('utterances')
delay = 0.25
utterances: dict[int, list[str]] = {}
warnings = []
active_utterances = []

for chapter_number in range(FIRST_CHAPTER, LAST_CHAPTER + 1):
   url = CHAPTER_URL.format(chapter_number)
   print(f'Downloading {url}')
   chapter_warnings, active_utterances = parse_chapter(
      fetch(url),
      utterances,
      active_utterances,
   )
   warnings.extend(chapter_warnings)
   if chapter_number < LAST_CHAPTER:
      time.sleep(delay)

correct_source_numbering(utterances)
write_utterances(output_dir, utterances)
print(f'Wrote {len(utterances)} utterances to {output_dir}')
for warning in warnings:
   print(f'Warning: {warning}')
