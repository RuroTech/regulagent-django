import hashlib
import re
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup, NavigableString  # type: ignore
from django.core.management.base import BaseCommand, CommandParser


BASE_URL = "https://www.law.cornell.edu/regulations/texas/title-16/part-1/chapter-3"


@dataclass
class Section:
    path: str
    heading: str
    text: str
    anchor: str
    order_idx: int


def fetch_html(url: str) -> str:
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.text


def clean_text(s: str) -> str:
    """Strip whitespace, replace tabs with spaces, collapse runs of whitespace."""
    s = s.replace("\t", " ")
    s = re.sub(r"[ \n\r]+", " ", s)
    return s.strip()


def _own_text(tag) -> str:
    """Return only the direct NavigableString children of a tag, cleaned."""
    parts = [str(child) for child in tag.children if isinstance(child, NavigableString)]
    return clean_text("".join(parts))


def extract_rule_title(html: str) -> str:
    """
    Parse the human-readable rule name from <h1 id="page_title">.

    Examples:
      "16 Tex. Admin. Code § 3.14 - [Effective 7/1/2025] Plugging"  -> "Plugging"
      "16 Tex. Admin. Code § 3.13 - Casing and Cementing"           -> "Casing and Cementing"
    """
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1", id="page_title")
    if not h1:
        return ""
    raw = clean_text(h1.get_text(" "))
    # Find the last " - " separator
    idx = raw.rfind(" - ")
    if idx == -1:
        return raw
    after_dash = raw[idx + 3:].strip()
    # Strip leading "[Effective …]" bracket if present
    after_dash = re.sub(r"^\[.*?\]\s*", "", after_dash).strip()
    return after_dash


def extract_topic(title: str) -> Optional[str]:
    """
    Convert a rule title to a lowercase underscore slug, max 64 chars.

    "Plugging"            -> "plugging"
    "Casing and Cementing" -> "casing_and_cementing"
    """
    if not title:
        return None
    slug = title.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "_", slug)
    slug = slug.strip("_")
    return slug[:64] if slug else None


def parse_chapter_index(html: str) -> List[Tuple[str, str]]:
    """Return list of (rule_id, url) for §3.x pages in Chapter 3."""
    soup = BeautifulSoup(html, "html.parser")
    base = "https://www.law.cornell.edu"
    found_pairs: List[Tuple[str, str]] = []
    seen: set = set()

    for a in soup.find_all("a"):
        text = (a.get_text(" ", strip=True) or "")
        href = a.get("href")
        if not href:
            continue
        m = re.search(r"§\s*3\.(\d+)", text)
        num = m.group(1) if m else None
        if not num:
            m2 = re.search(r"(?:/|-)3\.(\d+)", href)
            if m2:
                num = m2.group(1)
        if not num:
            continue
        rule_id = f"tx.tac.16.3.{num}"
        abs_url = requests.compat.urljoin(base, href)
        key = (rule_id, abs_url)
        if key in seen:
            continue
        found_pairs.append(key)
        seen.add(key)

    return found_pairs


def parse_rule_sections(html: str) -> Iterable[Section]:
    """
    Parse a Cornell LII rule page into ordered legal subsections.

    Uses the verified HTML structure:
      - Root container: <div class="statereg-text">
      - Each level is a <div class="subsect indentN"> with a nested <span class="designator">
      - indent0 => (a)-level, indent1 => (1)-level, indent2 => (A)-level, indent3 => (i)-level
      - Own text is extracted via NavigableString children only (avoids double-counting nested divs)
      - indent2/3 nodes have no further subsect children so .get_text() is safe there
    """
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one("div.statereg-text")
    if not root:
        # Fallback: try article or main
        root = soup.select_one("article") or soup.select_one("main") or soup

    sections: List[Section] = []
    order = 0

    for indent0 in root.select("div.subsect.indent0"):
        # --- Level 0: (a) ---
        des0_tag = indent0.find("span", class_="designator")
        if not des0_tag:
            continue
        des0 = clean_text(des0_tag.get_text()).strip("()")  # e.g. "a"
        heading0 = _own_text(indent0)
        sections.append(Section(
            path=des0,
            heading=heading0,
            text="",
            anchor="",
            order_idx=order,
        ))
        order += 1

        for indent1 in indent0.select("div.subsect.indent1"):
            # --- Level 1: (1) ---
            des1_tag = indent1.find("span", class_="designator")
            if not des1_tag:
                continue
            des1 = clean_text(des1_tag.get_text()).strip("()")  # e.g. "1"
            text1 = _own_text(indent1)
            sections.append(Section(
                path=f"{des0}({des1})",
                heading="",
                text=text1,
                anchor="",
                order_idx=order,
            ))
            order += 1

            for indent2 in indent1.select("div.subsect.indent2"):
                # --- Level 2: (A) ---
                des2_tag = indent2.find("span", class_="designator")
                if not des2_tag:
                    continue
                des2 = clean_text(des2_tag.get_text()).strip("()")  # e.g. "A"

                # Check for indent3 children
                has_indent3 = bool(indent2.select("div.subsect.indent3"))

                if has_indent3:
                    text2 = _own_text(indent2)
                else:
                    # No deeper nesting — safe to use full text
                    text2 = clean_text(indent2.get_text(" "))

                sections.append(Section(
                    path=f"{des0}({des1})({des2})",
                    heading="",
                    text=text2,
                    anchor="",
                    order_idx=order,
                ))
                order += 1

                for indent3 in indent2.select("div.subsect.indent3"):
                    # --- Level 3: (i) ---
                    des3_tag = indent3.find("span", class_="designator")
                    if not des3_tag:
                        continue
                    des3 = clean_text(des3_tag.get_text()).strip("()")  # e.g. "i"
                    text3 = clean_text(indent3.get_text(" "))
                    sections.append(Section(
                        path=f"{des0}({des1})({des2})({des3})",
                        heading="",
                        text=text3,
                        anchor="",
                        order_idx=order,
                    ))
                    order += 1

    return sections


class Command(BaseCommand):
    help = "Fetch and parse Texas TAC Chapter 3 from Cornell; print summary or upsert when --write is used."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--rule", dest="rule", help="Limit to a specific rule_id like tx.tac.16.3.14")
        parser.add_argument("--write", action="store_true", help="Persist to DB (PolicyRule/PolicySection)")
        parser.add_argument("--dry-run", dest="dry_run", action="store_true", help="Print summaries even when --write is used")
        parser.add_argument("--version-tag", dest="version_tag", default="manual", help="Version tag to apply (e.g., 2025-Q4)")
        parser.add_argument("--clear", action="store_true", help="Delete ALL TX PolicyRule and PolicySection records before fetching")

    def handle(self, *args, **options):
        from apps.policy_ingest.models import PolicyRule, PolicySection

        version_tag: str = options["version_tag"]
        limit_rule: str | None = options.get("rule")
        do_write: bool = options.get("write", False)
        do_dry: bool = options.get("dry_run", False)
        do_clear: bool = options.get("clear", False)

        if do_clear and do_write:
            deleted_rules, _ = PolicyRule.objects.filter(jurisdiction='TX').delete()
            self.stdout.write(self.style.WARNING(f"Cleared {deleted_rules} TX PolicyRule records (sections cascade-deleted)."))

        index_html = fetch_html(BASE_URL)
        rules = parse_chapter_index(index_html)
        self.stdout.write(f"Discovered {len(rules)} Chapter 3 rule links from index {BASE_URL}")
        for rid, url in rules[:10]:
            self.stdout.write(f"  - {rid} -> {url}")
        if limit_rule:
            rules = [r for r in rules if r[0] == limit_rule]

        for rule_id, url in rules:
            page_html = fetch_html(url)
            html_sha = hashlib.sha256(page_html.encode("utf-8")).hexdigest()

            # Extract human-readable title and topic from page
            title = extract_rule_title(page_html)
            topic = extract_topic(title)

            self.stdout.write(f"Fetched {rule_id} -> {url} sha={html_sha[:12]} title={title!r} topic={topic!r}")

            # Always show a small preview when dry-run requested or when not writing
            if do_dry or not do_write:
                secs = list(parse_rule_sections(page_html))[:5]
                for s in secs:
                    self.stdout.write(f"  - {s.order_idx:03d} {s.path} heading={s.heading[:40]!r} :: {s.text[:80]!r}")
                if not do_write:
                    continue

            jurisdiction = 'TX'
            doc_type = 'policy'

            rule_obj, _ = PolicyRule.objects.update_or_create(
                rule_id=rule_id,
                version_tag=version_tag,
                defaults={
                    'citation': rule_id.replace('tx.tac.', '').replace('.', ' '),
                    'title': title,
                    'source_urls': [url],
                    'jurisdiction': jurisdiction,
                    'doc_type': doc_type,
                    'topic': topic,
                    'effective_from': None,
                    'effective_to': None,
                    'html_sha256': html_sha,
                },
            )

            # Replace sections for this version
            PolicySection.objects.filter(rule=rule_obj, version_tag=version_tag).delete()
            batch: List[PolicySection] = []
            for s in parse_rule_sections(page_html):
                batch.append(
                    PolicySection(
                        rule=rule_obj,
                        version_tag=version_tag,
                        path=s.path,
                        heading=s.heading,
                        text=s.text,
                        anchor=s.anchor,
                        order_idx=s.order_idx,
                    )
                )
            PolicySection.objects.bulk_create(batch, batch_size=500)
            self.stdout.write(f"  wrote {len(batch)} sections for {rule_id}@{version_tag}")
