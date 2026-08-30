#!/usr/bin/env python3
# Status: production
# Path: text_clean.py — language-aware preprocessing (Kiwi for ko, skip for en)
"""Language-aware text cleaner with sentence segmentation and token estimation.

Languages supported:
  - Korean (ko): NFKC → emoji → Kiwi typo correction → hanja substitution → sentence split
  - English (en): NFKC → emoji → sentence split (Kiwi.split_into_sents works for both)
  - Other: NFKC → whitespace normalization only

Sentence segmentation via Kiwi.split_into_sents (works for ko + en).
Token estimation via tiktoken (o200k_base).
"""

from __future__ import annotations

import re
import unicodedata
from typing import Dict, List, Optional, Tuple

import tiktoken

# Heavy NLP deps — lazy import to avoid pulling into container images unnecessarily
_kiwi = None
_langdetect = None


def _get_kiwi():
    global _kiwi
    if _kiwi is None:
        from kiwipiepy import Kiwi as _K

        _kiwi = _K()
    return _kiwi


def _get_langdetect():
    global _langdetect
    if _langdetect is None:
        from langdetect import LangDetectException as _LDE
        from langdetect import detect as _detect

        _langdetect = (_detect, _LDE)
    return _langdetect


# Emoji removal — only well-known emoji blocks, no Hangul overlap
RE_EMOJI = re.compile(
    "[\U0001f600-\U0001f64f"  # emoticons
    "\U0001f300-\U0001f5ff"  # symbols & pictographs
    "\U0001f680-\U0001f6ff"  # transport
    "\U0001f1e0-\U0001f1ff"  # flags
    "\U0001f900-\U0001f9ff"  # supplemental symbols
    "\U0001fa00-\U0001fa6f"  # chess symbols
    "\U0001fa70-\U0001faff"  # symbols extended-A
    "\U00002702-\U000027b0"  # dingbats
    "]+"
)
RE_KOREAN_EMOTICON = re.compile(r"[ㅋㅠㅜㅎㅡ]{3,}")
RE_REPEAT_HANGUL = re.compile(r"([가-힣])\1{3,}")
RE_ZERO_WIDTH = re.compile(r"[​‌‍‎‏﻿⁠­]")
RE_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F-\x9F]")
RE_NBSP = re.compile(r" ")
RE_MULTI_SPACE = re.compile(r"\s+")
RE_CODE_BLOCK = re.compile(r"```.*?```", re.DOTALL)
RE_INLINE_CODE = re.compile(r"`[^`]+`")

# Hanja range for Korean-only substitution
HANJA_RANGE = re.compile(r"[一-鿟]")

# Korean sentence boundary heuristics (for . which can also mark abbreviations)
RE_ABBREV = re.compile(r"(?:etc|vs|no|vol|fig|ref|Mr|Mrs|Ms|Dr|Prof|Sr|Jr|St)\.", re.IGNORECASE)

# Tags that carry lexical meaning for BM25 indexing
LEXICAL_TAGS = frozenset(
    {
        "NNG",
        "NNP",
        "NNB",
        "NR",
        "NP",  # nouns
        "VV",
        "VA",
        "VX",  # verbs/adjectives
        "MAG",
        "MAJ",  # adverbs
        "SL",
        "SH",
        "SN",  # foreign/chinese/numbers
        "XR",  # roots
    }
)

# Tags used for topic/keyword extraction (topic-bearing nouns)
TOPIC_TAGS = frozenset({"NNP"})


class TextCleaner:
    """Language-aware text cleaner with Kiwi-based tokenization.

    Languages:
      - Korean (ko): clean → Kiwi typo correction → hanja substitution
      - English (en): clean only (no Kiwi, no hanja)
      - Other: basic NLFKC + whitespace normalization

    Usage:
        cleaner = TextCleaner()
        lang = cleaner.detect_language(text)  # 'ko', 'en', 'unknown'
        clean_text = cleaner.clean(text, lang=lang)
        sents = cleaner.split_sentences(clean_text, lang=lang)
        tokens = cleaner.tokenize(clean_text)  # Kiwi POS (Korean only)
        tok_count = cleaner.estimate_tokens(clean_text)  # tiktoken
    """

    def __init__(self) -> None:
        self._tiktoken_enc = tiktoken.get_encoding("o200k_base")

    # ------------------------------------------------------------------
    # Language Detection
    # ------------------------------------------------------------------

    @staticmethod
    def detect_language(text: str) -> Tuple[str, float]:
        """Detect text language using langdetect.

        Returns ('ko', confidence) or ('en', confidence) or ('unknown', 0.0).
        Confidence threshold: returns 'ko'/'en' only if > 0.5.
        """
        if not text.strip():
            return "unknown", 0.0
        _detect, _LDE = _get_langdetect()
        try:
            lang = _detect(text)
        except _LDE:
            return "unknown", 0.0
        if lang not in ("ko", "en"):
            return "unknown", 0.0
        return lang, 0.8

    # ------------------------------------------------------------------
    # Sentence Segmentation (language-dependent)
    # ------------------------------------------------------------------

    def split_sentences(self, text: str, lang: str = "ko") -> List[str]:
        """Split text into sentences.

        Korean (ko): Kiwi.split_into_sents (morphology-aware).
        English (en): PySBD (rule-based, Golden Rule Set 97.9%).
        Other: simple regex split on [.!?].
        """
        if not text.strip():
            return [text] if text else []

        try:
            if lang == "ko":
                sents = _get_kiwi().split_into_sents(text)
                return [s.text for s in sents if s.text.strip()]
            elif lang == "en":
                import pysbd

                segmenter = pysbd.Segmenter(language="en", clean=False)
                return segmenter.segment(text)
            else:
                # Simple regex fallback for unknown languages
                cleaned = RE_ABBREV.sub(lambda m: m.group().replace(".", "\x00DOT\x00"), text)
                parts = re.split(r"(?<=[.!?])\s+", cleaned)
                return [p.replace("\x00DOT\x00", ".") for p in parts if p.strip()]
        except Exception:
            pass

        # Ultimate fallback
        parts = re.split(r"(?<=[.!?])\s+", text)
        return [p for p in parts if p.strip()]

    # ------------------------------------------------------------------
    # Hanja Substitution (Korean only)
    # ------------------------------------------------------------------

    @staticmethod
    def _has_hanja(text: str) -> bool:
        return bool(HANJA_RANGE.search(text))

    def hanja_substitute(self, text: str) -> Tuple[str, List[Dict[str, str]]]:
        """Replace hanja (Chinese characters) with Korean hangul.

        Korean-only operation. Code blocks and inline code preserved.
        Returns (corrected_text, changes_list).
        """
        import hanja

        if not text.strip() or not self._has_hanja(text):
            return text, []

        code_blocks: List[str] = []
        inline_codes: List[str] = []

        def _save_code(m: re.Match) -> str:
            code_blocks.append(m.group())
            return f"\x00BLOCK{len(code_blocks) - 1}\x00"

        def _save_inline(m: re.Match) -> str:
            inline_codes.append(m.group())
            return f"\x00INLINE{len(inline_codes) - 1}\x00"

        t = RE_CODE_BLOCK.sub(_save_code, text)
        t = RE_INLINE_CODE.sub(_save_inline, t)

        before = t
        t = hanja.translate(t, "substitution")

        changes = []
        for bl, al in zip(before.split("\n"), t.split("\n")):
            if bl.strip() != al.strip():
                changes.append({"from": bl.strip(), "to": al.strip()})

        for i, cb in enumerate(code_blocks):
            t = t.replace(f"\x00BLOCK{i}\x00", cb)
        for i, ic in enumerate(inline_codes):
            t = t.replace(f"\x00INLINE{i}\x00", ic)

        return t, changes

    # ------------------------------------------------------------------
    # Text Cleaning (Language-Aware)
    # ------------------------------------------------------------------

    def _clean_non_kiwi(self, text: str) -> Tuple[str, List[str], List[str]]:
        """Apply non-Kiwi cleaning steps (1-6). Returns (cleaned, code_blocks, inline_codes)."""
        if not text:
            return "", [], []

        code_blocks: List[str] = []
        inline_codes: List[str] = []

        def _save_code(m: re.Match) -> str:
            code_blocks.append(m.group())
            return f"\x00BLOCK{len(code_blocks) - 1}\x00"

        def _save_inline(m: re.Match) -> str:
            inline_codes.append(m.group())
            return f"\x00INLINE{len(inline_codes) - 1}\x00"

        t = RE_CODE_BLOCK.sub(_save_code, text)
        t = RE_INLINE_CODE.sub(_save_inline, t)

        t = RE_KOREAN_EMOTICON.sub(lambda m: m.group()[0] * 2, t)
        t = unicodedata.normalize("NFKC", t)
        t = RE_ZERO_WIDTH.sub("", t)
        t = RE_CONTROL_CHARS.sub("", t)
        t = RE_EMOJI.sub(" ", t)
        t = RE_REPEAT_HANGUL.sub(lambda m: m.group(1) * 2, t)
        t = RE_NBSP.sub(" ", t)
        t = RE_MULTI_SPACE.sub(" ", t)
        t = t.strip()
        return t, code_blocks, inline_codes

    def _apply_kiwi(self, text: str) -> str:
        """Apply Kiwi typo correction only. Placeholders pass through unchanged."""
        if not text.strip():
            return text
        kiwi = _get_kiwi()
        tokens = kiwi.tokenize(text, typos="basic_with_continual_and_lengthening")
        return kiwi.join(tokens)

    def _restore_placeholders(
        self, text: str, code_blocks: List[str], inline_codes: List[str]
    ) -> str:
        for i, cb in enumerate(code_blocks):
            text = text.replace(f"\x00BLOCK{i}\x00", cb)
        for i, ic in enumerate(inline_codes):
            text = text.replace(f"\x00INLINE{i}\x00", ic)
        return text

    def clean(self, text: str, lang: Optional[str] = None) -> str:
        """Language-aware text normalization.

        Args:
            text: Raw text to clean.
            lang: Language hint ('ko', 'en', None=auto-detect).

        Korean: NFKC → emoji → Kiwi typo correction → hanja substitution.
        English: NFKC → emoji → whitespace (no Kiwi, no hanja).
        Other: NFKC → whitespace only.

        Code blocks (``````) and inline code (``) preserved verbatim.
        """
        if not text:
            return ""

        if lang is None:
            lang, _ = self.detect_language(text)

        t, code_blocks, inline_codes = self._clean_non_kiwi(text)

        if t and lang == "ko":
            t = self._apply_kiwi(t)

        result = self._restore_placeholders(t, code_blocks, inline_codes)

        if lang == "ko":
            result, _ = self.hanja_substitute(result)

        return result

    def detect_kiwi_changes(self, text: str) -> bool:
        """True if Kiwi typo correction modified the text (vs NFKC/whitespace-only changes).

        Only meaningful for Korean text.
        """
        if not text.strip():
            return False
        t, _, _ = self._clean_non_kiwi(text)
        after = self._apply_kiwi(t)
        return t != after

    # ------------------------------------------------------------------
    # Token Estimation (tiktoken)
    # ------------------------------------------------------------------

    def estimate_tokens(self, text: str) -> int:
        """Estimate token count using tiktoken (o200k_base).

        Returns 0 for empty text. Minimum 4 tokens for non-empty.
        Works for any language (Korean, English, code, etc.).
        """
        if not text.strip():
            return 0
        tokens = self._tiktoken_enc.encode(text)
        return max(4, len(tokens))

    # ------------------------------------------------------------------
    # Batch processing
    # ------------------------------------------------------------------

    def tokenize(self, text: str) -> List[Dict]:
        """Kiwi POS tokenization. Returns list of {form, tag, start, len} dicts."""
        if not text or not text.strip():
            return []
        try:
            kiwi = _get_kiwi()
            results = kiwi.tokenize(text)
            return [{"form": t.form, "tag": t.tag, "start": t.start, "len": t.len} for t in results]
        except Exception:
            return []

    def extract_nnp(self, text: str) -> List[str]:
        """Extract proper nouns (NNP) via Kiwi tokenization."""
        tokens = self.tokenize(text)
        return [t["form"] for t in tokens if t["tag"] == "NNP"]

    def extract_terms(self, text: str) -> List[str]:
        """Extract lexical terms (NNG/NNP/NNB/NR/...) via Kiwi tokenization."""
        if not text.strip():
            return []
        tokens = tokenize(text)
        return [t["form"] for t in tokens if t["tag"] in LEXICAL_TAGS]

    def process_document(self, text: str) -> Dict:
        """Full document processing: clean → tokenize → extract.

        Returns dict suitable for storing in turns.tokens jsonb column.
        """
        if not text:
            return {"clean": "", "terms": [], "tokens": [], "kiwi_changed": False, "nnp": []}

        clean_text = self.clean(text)
        tokens = tokenize(clean_text)
        terms = [t["form"] for t in tokens if t["tag"] in LEXICAL_TAGS]
        nnp = self.extract_nnp(clean_text)
        kiwi_changed = self.detect_kiwi_changes(text)

        return {
            "clean": clean_text,
            "terms": terms,
            "tokens": tokens,
            "kiwi_changed": kiwi_changed,
            "nnp": nnp,
        }


# Module-level singleton
_cleaner: Optional[TextCleaner] = None


def get_cleaner() -> TextCleaner:
    global _cleaner
    if _cleaner is None:
        _cleaner = TextCleaner()
    return _cleaner


def detect_language(text: str) -> Tuple[str, float]:
    return get_cleaner().detect_language(text)


def split_sentences(text: str, lang: str = "ko") -> List[str]:
    return get_cleaner().split_sentences(text, lang=lang)


def clean(text: str, lang: Optional[str] = None) -> str:
    return get_cleaner().clean(text, lang=lang)


def tokenize(text: str) -> List[Dict]:
    return get_cleaner().tokenize(text)


def extract_terms(text: str) -> List[str]:
    return get_cleaner().extract_terms(text)


def extract_nnp(text: str) -> List[str]:
    return get_cleaner().extract_nnp(text)


def estimate_tokens(text: str) -> int:
    return get_cleaner().estimate_tokens(text)


def detect_kiwi_changes(text: str) -> bool:
    return get_cleaner().detect_kiwi_changes(text)


def hanja_substitute(text: str) -> Tuple[str, List[Dict[str, str]]]:
    return get_cleaner().hanja_substitute(text)
