# Status: production
# Path: imported by — lib/extract_llm (extraction subpackage)
"""Text chunking utilities for LLM fact extraction.

Splitting, merging, paragraph grouping, compound expansion, code-block
protection, and section-aware overlap strategies (plain/contextual/hierarchical).
"""

import os
import re

CHUNK_OVERLAP_CHARS = 80  # chars of overlap between consecutive chunks
CHUNK_STRATEGY = os.environ.get(
    "CHUNK_STRATEGY", "plain"
)  # "plain" | "contextual" | "hierarchical"


def _split_dense_bullets(text: str) -> str:
    """Insert blank lines before each bullet in dense sections (4+ items).

    Preserves original text format — only adds paragraph boundaries so
    _split_atomic splits each item into its own chunk.  Without this,
    sections like Overview (8 bullets, no periods) become one chunk and
    hit the 16-fact cap, causing the LLM to skip HW specs and storage
    mounts.
    """
    lines = text.split("\n")
    out = []
    i = 0
    in_code = False
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_code = not in_code
            out.append(line)
            i += 1
            continue
        if in_code:
            out.append(line)
            i += 1
            continue
        # Detect run of 4+ consecutive bullet lines
        if stripped.startswith("- ") or stripped.startswith("* "):
            j = i
            count = 0
            while j < len(lines) and (
                lines[j].strip().startswith("- ") or lines[j].strip().startswith("* ")
            ):
                count += 1
                j += 1
            if count >= 4:
                out.append("")  # blank line before first bullet separates it from heading
                for k in range(i, j):
                    if k > i:
                        out.append("")
                    out.append(lines[k])
                i = j
                continue
        # Detect table (header+separator+4+ data rows).
        # Only insert blank lines for English 3-column tables (which
        # _expand_compounds converts into per-row bullets).  2-column tables
        # and non-English tables stay as single paragraphs to reduce chunk count.
        tbl_row = re.compile(r"^\|.*\|$")
        if (
            tbl_row.match(stripped)
            and i + 1 < len(lines)
            and re.match(r"^\|[\s\-:|]+\|$", lines[i + 1].strip())
        ):
            sep_cols = lines[i + 1].strip().count("|")
            j = i + 2
            rows = 0
            while j < len(lines) and tbl_row.match(lines[j].strip()):
                rows += 1
                j += 1
            if rows >= 4 and sep_cols == 4:
                header_cells = [c.strip() for c in lines[i].strip().split("|")[1:-1]]
                is_english_header = all(
                    re.match(r"^[A-Z][a-z]+(\s[A-Z][a-z]+)*$", c) for c in header_cells if c
                )
                if is_english_header:
                    out.append(lines[i])  # header
                    i += 1
                    out.append(lines[i])  # separator
                    i += 1
                    for k in range(rows):
                        if k > 0 or rows >= 4:
                            out.append("")
                        out.append(lines[i])
                        i += 1
                    continue
        out.append(line)
        i += 1
    return "\n".join(out)


def _protect_code_blocks(text: str) -> str:
    """Replace blank lines inside fenced code blocks with placeholder.

    Prevents _split_atomic's paragraph splitting (\n\\s*\n) from fragmenting
    code blocks while preserving internal line breaks after restoration.
    Handles ``` and ~~~ fences.
    """
    result = []
    in_code = False
    for line in text.split("\n"):
        if line.strip().startswith("```") or line.strip().startswith("~~~"):
            in_code = not in_code
            result.append(line)
        elif in_code and not line.strip():
            result.append("@@@CBNL@@@")
        else:
            result.append(line)
    return "\n".join(result)


def _merge_section_paragraphs(paragraphs: list[str], max_chars: int) -> list[str]:
    """Within each ##-headed section, merge consecutive small paragraphs.

    Section boundaries (## ) are never crossed — fixing the recall regression
    from the old unconditional merge (63dd64b).  Only paragraphs under
    MIN_MERGE_SIZE chars are merged into the preceding content paragraph,
    up to max_chars.  Section headers stay as their own paragraph.
    """
    MIN_MERGE_SIZE = 200

    groups = []
    current = []
    for p in paragraphs:
        if p.startswith("## "):
            if current:
                groups.append(current)
            current = [p]
        else:
            current.append(p)
    if current:
        groups.append(current)

    result = []
    for group in groups:
        if not group:
            continue
        header = group[0] if group[0].startswith("## ") else None
        content = group[1:] if header else group

        merged = []
        for para in content:
            if not merged:
                merged.append(para)
            elif len(para) < MIN_MERGE_SIZE and len(merged[-1]) + len(para) + 1 <= max_chars:
                merged[-1] += "\n" + para
            else:
                merged.append(para)

        if header:
            result.append(header)
        result.extend(merged)

    return result


def _split_atomic(text: str, max_chars: int = 1600) -> list[str]:
    """Split text into chunks at sentence boundaries, up to max_chars.

    Paragraphs under the same ## section header are merged when small,
    then split by sentence boundaries and re-grouped up to max_chars.
    Code blocks are never fragmented: internal blank lines are protected
    before paragraph splitting and restored in the final chunks.
    Cross-section merging is forbidden (fixes 63dd64b recall regression).

    Strategy modes (configurable via CHUNK_STRATEGY env var):
      - plain:        current behavior, no section context (default)
      - contextual:   each content chunk prefixed with [Section: ...]
      - hierarchical: contextual + parent-child expansion (surrounding context)
    """
    text = _split_dense_bullets(text)
    text = _expand_compounds(text)
    text = re.sub(r"(?<=\d)\.(?=\d)", "@@@DOT@@@", text)
    text = _protect_code_blocks(text)
    paragraphs = re.split(r"\n\s*\n", text)
    paragraphs = _merge_section_paragraphs(paragraphs, max_chars)

    if CHUNK_STRATEGY == "plain":
        return _chunk_paragraphs_plain(paragraphs, max_chars)

    return _chunk_paragraphs_sectioned(paragraphs, max_chars)


def _chunk_paragraphs_plain(paragraphs: list[str], max_chars: int) -> list[str]:
    """Original chunking: process all paragraphs flat, no section awareness."""
    chunks = []
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        sentences = re.split(r"(?<=[.!?])\s+", para)
        para_chunks = []
        for sent in sentences:
            sent = sent.strip().replace("@@@DOT@@@", ".").replace("@@@CBNL@@@", "")
            if not sent:
                continue
            if para_chunks and len(para_chunks[-1]) + len(sent) + 1 <= max_chars:
                para_chunks[-1] += " " + sent
            else:
                para_chunks.append(sent)
        chunks.extend(para_chunks)
    return chunks


def _restore_markers(text: str) -> str:
    """Restore @@DOT@@ and @@CBNL@@ placeholders in a single text."""
    return text.replace("@@@DOT@@@", ".").replace("@@@CBNL@@@", "")


def _parse_heading_level(text: str) -> tuple[int, str, str]:
    """Detect markdown heading. Returns (level, heading_text, remaining_content).

    Level is number of # chars (1-6). heading_text is the name without #.
    remaining_content is text after the heading line.
    If not a heading, returns (0, '', text).
    """
    m = re.match(r"^(#+)\s+([^\n]*?)(?:\n(.*))?$", text, re.DOTALL)
    if m:
        level = len(m.group(1))
        heading = m.group(2).strip()
        rest = (m.group(3) or "").strip()
        return (level, heading, rest)
    return (0, "", text)


def _build_section_prefix(header_stack: list[tuple[int, str]]) -> str:
    """Build '[Section: H2 > H3]' from header stack.

    Only H2+ levels are included in the prefix (H1 is the document title).
    """
    names = [name for level, name in header_stack if level >= 2]
    if not names:
        return ""
    return f"[Section: {' > '.join(names)}] "


def _chunk_paragraphs_sectioned(paragraphs: list[str], max_chars: int) -> list[str]:
    """Section-aware chunking: track full header hierarchy (H1-H6).

    Maintains a header stack so each chunk gets the full context path,
    e.g. [Section: Entry Points > Auto-Generated Docs]

    Preamble (text before first heading) is added as plain chunks.
    """
    result = []
    header_stack: list[tuple[int, str]] = []
    section_buffer: list[str] = []
    preamble_processed = False

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        level, heading, rest = _parse_heading_level(para)
        if level > 0:
            if not preamble_processed and section_buffer:
                _flush_section_chunks(result, section_buffer, header_stack, max_chars)
                section_buffer.clear()
                preamble_processed = True
            _flush_section_chunks(result, section_buffer, header_stack, max_chars)

            # Update header stack: pop entries at same or deeper level
            while header_stack and header_stack[-1][0] >= level:
                header_stack.pop()
            header_stack.append((level, heading))
            section_buffer = []

            # H2+ → skip (content chunks have [Section: ...] prefix)
            # H1 (document title) stays as standalone chunk
            if level == 1:
                result.append(f"{'#' * level} {heading}")

            # If heading has inline content, split and queue it
            if rest:
                section_buffer.append(rest)
        else:
            section_buffer.append(para)

    _flush_section_chunks(result, section_buffer, header_stack, max_chars)

    return result


def _flush_section_chunks(
    result: list[str],
    section_paragraphs: list[str],
    header_stack: list[tuple[int, str]],
    max_chars: int,
):
    """Chunk paragraphs with section-aware context.

    When header_stack is empty (preamble), falls back to plain chunking.
    """
    if not section_paragraphs:
        return

    # Build section-level content sentences
    all_sentences: list[str] = []
    for para in section_paragraphs:
        sents = re.split(r"(?<=[.!?])\s+", para)
        for s in sents:
            s = s.strip().replace("@@@DOT@@@", ".").replace("@@@CBNL@@@", "")
            if s:
                all_sentences.append(s)

    # Preamble (empty header_stack) → plain chunking
    if not header_stack:
        base_chunks = _group_sentences(all_sentences, max_chars)
        result.extend(base_chunks)
        return

    prefix = _build_section_prefix(header_stack)
    use_overlap = CHUNK_STRATEGY in ("contextual", "hierarchical")

    # When overlap is on, shrink base_chunks to leave room for prefix + overlap
    effective_max = max_chars
    if use_overlap:
        effective_max = max_chars - len(prefix) - CHUNK_OVERLAP_CHARS - 2  # 2 for spaces
        effective_max = max(effective_max, 100)  # floor at 100 chars

    base_chunks = _group_sentences(all_sentences, effective_max)

    if use_overlap:
        _emit_chunks_with_overlap(result, base_chunks, prefix, max_chars)
    else:
        result.extend(base_chunks)


def _group_sentences(sentences: list[str], max_len: int) -> list[str]:
    """Group sentences into chunks up to max_len chars each."""
    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for sent in sentences:
        if cur and cur_len + len(sent) + 1 > max_len:
            chunks.append(" ".join(cur))
            cur, cur_len = [], 0
        cur.append(sent)
        cur_len += len(sent) + 1
    if cur:
        chunks.append(" ".join(cur))
    return chunks


def _emit_chunks_with_overlap(
    result: list[str],
    base_chunks: list[str],
    prefix: str,
    max_chars: int,
):
    """Emit chunks with [Section: ...] prefix and 80-char overlap.

    Each chunk gets the section prefix. Consecutive base_chunks within
    the same section overlap by CHUNK_OVERLAP_CHARS of the previous
    chunk's tail, so facts near chunk boundaries get extracted twice.
    Overlap does NOT cross section boundaries (per-section base_chunks).
    """
    for i, child in enumerate(base_chunks):
        # Build overlap from previous base_chunk's tail (raw content, no prefix)
        overlap = ""
        if i > 0:
            prev_raw = base_chunks[i - 1]
            if len(prev_raw) > CHUNK_OVERLAP_CHARS:
                overlap = prev_raw[-CHUNK_OVERLAP_CHARS:].lstrip()
            else:
                overlap = prev_raw

        # Assemble: [prefix] overlap_seam child
        parts = []
        if prefix:
            parts.append(prefix.rstrip())
        if overlap:
            parts.append(overlap)
        parts.append(child)

        assembled = " ".join(parts)
        if len(assembled) <= max_chars:
            result.append(assembled)
        else:
            # Overlap doesn't fit → use prefix + child, truncated if needed
            flat = (prefix + child) if prefix else child
            result.append(flat[:max_chars])


def _expand_compounds(text: str) -> str:
    """Split compound sentences so each entity/attribute gets its own clause.
    Causal LLMs cannot extract the second entity from structures like
    'Pod A is DOWN and Pod B runs a model' (Slot Machines, 2025)."""
    # ", and" → ". " (most common compound pattern)
    text = re.sub(r",\s+and\s+", ". ", text)
    # " and [Entity verb]" → ". " (second clause has different subject — Slot Machines fix)
    # Do NOT split at pronouns (we, it, they) to avoid orphaning the referent
    text = re.sub(
        r"\s+and\s+(?=(?:[A-Z][a-z]+\s+(?:has|runs|uses|consumes|is|are|was|were)))", ". ", text
    )
    # "X has A with B" → "X has A. X has B." (preserve subject for 2nd attribute)
    text = re.sub(
        r"(\w+(?:\s+\w+){0,3})\s+has\s+([^.]*?)\s+with\s+(\d[\w.]*\s*\w+)",
        r"\1 has \2. \1 has \3.",
        text,
    )
    # ", \d" → ". \d" — split comma-separated hardware specs like "4-core, 22Gi, 4G"
    # inside parenthetical spec lists (e.g. "(ARM Neoverse-N1, 4-core, 22Gi)")
    text = re.sub(r",\s*(?=\d)", r". ", text)
    # " + digit" → ". digit" — split plus-separated specs like "22Gi + 4G zram + 12G swap"
    text = re.sub(r"\s*\+\s*(?=\d)", r". ", text)
    # Convert "`path` (size) — description" storage listings into attribute
    # format so the LLM can extract path/size triples instead of nothing.
    text = re.sub(
        r"^-[^\S\n]*`([^`]+)`[^\S\n]*\((\d+\.?\d*[KMGTPE]?[B]?)\)[^\S\n]*—[^\S\n]*(.+)$",
        r"- \1: size=\2, purpose=\3.",
        text,
        flags=re.MULTILINE,
    )
    # Convert "| entity | type | status |" table rows (without table header)
    # into bullet format so each service gets its own extraction.
    # [^\S\n]* = whitespace but NOT newline — prevents \s* from crossing
    # line boundaries when the regex engine backtracks past its own ^/$ anchors.
    # The lookahead (?!...) excludes header rows where all 3 cells are
    # single capitalized words ("Service", "Type", "Status").
    text = re.sub(
        r"^(?!\|[^\S\n]*[A-Z][a-z]+[^\S\n]*\|[^\S\n]*[A-Z][a-z]+[^\S\n]*\|[^\S\n]*[A-Z][a-z]+[^\S\n]*\|\s*$)"
        r"\|[^\S\n]*(.+?)[^\S\n]*\|[^\S\n]*(.+?)[^\S\n]*\|[^\S\n]*(\w[\w()\s]*\w)[^\S\n]*\|$",
        r"- Service \1: type=\2, status=\3",
        text,
        flags=re.MULTILINE,
    )

    # Expand "Label: Entity (spec1. spec2. spec3.)" into factual statements
    # so each spec gets its own subject-verb-object triple.
    # Pattern: bullet, label, colon, entity name, parenthetical with period-separated specs
    def _expand_spec_parens(m):
        label = m.group(1).strip()
        entity = m.group(2).strip()
        inner = m.group(3)
        parts = [p.strip().rstrip(")") for p in re.split(r"\.\s+", inner) if p.strip()]
        if len(parts) <= 1:
            return m.group(0)
        has_specs = any(bool(re.search(r"\d", p)) for p in parts)
        if not has_specs:
            return m.group(0)
        # Put each spec on its own paragraph so _split_atomic gives each
        # its own chunk.  Skip the label line (- Label: Entity.) to prevent
        # the LLM from treating section labels (Host, Runtime) as entities.
        result = ""
        for i, p in enumerate(parts):
            result += "\n\n" if result else ""
            result += f"- {entity} has {p}."
        result += "\n"
        return result

    text = re.sub(
        r"^-\s+([^:]+):\s+(\S[^(]+?)\s*\((.*)\)\s*$",
        _expand_spec_parens,
        text,
        flags=re.MULTILINE,
    )
    return text
