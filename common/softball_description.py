"""Source description contract: inert HTML plus a readable, multiline fallback.

Only apply to an identified event description, never a whole navigation page.
Raw captures belong in Tests, not in the public delivery payload.
"""
from html import escape
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Comment, NavigableString


ALLOWED = {'p', 'br', 'strong', 'em', 'u', 'h2', 'h3', 'h4', 'ul', 'ol',
           'li', 'blockquote', 'a', 'hr'}
BLOCKS = {'p', 'h2', 'h3', 'h4', 'ul', 'ol', 'blockquote'}
DROP = {'script', 'style', 'iframe', 'object', 'embed', 'svg', 'math', 'form',
        'input', 'button', 'textarea', 'select', 'template', 'noscript'}


def public_url(value, base_url=''):
    raw = str(value or '').strip()
    if not raw or any(ord(char) < 32 for char in raw):
        return ''
    try:
        url = urljoin(base_url, raw)
        parts = urlsplit(url)
        if parts.scheme in {'http', 'https'} and parts.hostname and not parts.username and not parts.password:
            return url
        if parts.scheme in {'mailto', 'tel'} and parts.path:
            return url
    except ValueError:
        pass
    return ''


def description(value, *, base_url=''):
    """Deterministic allowlist; no CSS, media data, handlers or source attributes."""
    source = str(value or '').replace('\r\n', '\n').replace('\r', '\n')
    # Plain JSON descriptions still have intentional paragraph/line boundaries.
    if '<' not in source:
        source = ''.join('<p>' + escape(part).replace('\n', '<br>') + '</p>'
                         for part in re.split(r'\n\s*\n', source) if part.strip())
    soup = BeautifulSoup(source, 'html.parser')

    def render(node):
        if isinstance(node, Comment):
            return ''
        if isinstance(node, NavigableString):
            return escape(str(node).replace('\xa0', ' '))
        name = node.name.lower()
        if name in DROP:
            return ''
        if name == 'img':
            # A useful alt label survives; image-only flyers require separate QA.
            return escape(str(node.get('alt') or ''))
        inside = ''.join(render(child) for child in node.children)
        name = {'div': 'p', 'section': 'p', 'article': 'p', 'h1': 'h2',
                'h5': 'h4', 'h6': 'h4', 'b': 'strong', 'i': 'em',
                'tr': 'p', 'td': 'span', 'th': 'strong'}.get(name, name)
        if name == 'span':
            # Office/editor spans often express emphasis only in inline styles.
            style = str(node.get('style') or '').lower()
            if re.search(r'font-weight\s*:\s*(?:bold|[6-9]00)\b', style):
                inside = '<strong>' + inside + '</strong>'
            if re.search(r'font-style\s*:\s*italic\b', style):
                inside = '<em>' + inside + '</em>'
        if name not in ALLOWED:
            return inside
        if name in {'br', 'hr'}:
            return '<' + name + '>'
        if name == 'a':
            href = public_url(node.get('href'), base_url)
            if not href:
                return inside
            return '<a href="' + escape(href, quote=True) + '" rel="noopener noreferrer">' + inside + '</a>'
        # Do not produce invalid nested paragraphs from editor wrapper divs.
        if name == 'p' and re.search(r'<(?:p|h[234]|ul|ol|blockquote)(?:\s|>)', inside):
            return inside
        if not inside.strip():
            return ''
        return '<' + name + '>' + inside + '</' + name + '>'

    safe = ''.join(render(child) for child in soup.children).strip()
    clean = BeautifulSoup(safe, 'html.parser')

    def plain(node):
        if isinstance(node, NavigableString):
            return str(node)
        if node.name in {'br', 'hr'}:
            return '\n'
        inside = ''.join(plain(child) for child in node.children)
        if node.name == 'li':
            prefix = '- '
            if node.parent.name == 'ol':
                prefix = str(len(node.find_previous_siblings('li')) + 1) + '. '
            return '\n' + prefix + inside.strip() + '\n'
        if node.name in BLOCKS:
            return '\n\n' + inside.strip() + '\n\n'
        if node.name == 'a' and node.get('href') and node['href'] != inside.strip():
            return inside + ' (' + node['href'] + ')'
        return inside

    text = ''.join(plain(child) for child in clean.children)
    text = '\n'.join(re.sub(r'[^\S\n]+', ' ', line).strip() for line in text.splitlines())
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return {'version': 1, 'format': 'safe_html', 'html': safe, 'text': text}
