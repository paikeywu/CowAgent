"""
Text chunking utilities for memory

Splits text into chunks with token limits and overlap
"""

from __future__ import annotations
from typing import List, Optional, Tuple
from dataclasses import dataclass, field


@dataclass
class TextChunk:
    """Represents a text chunk with line numbers"""
    text: str
    start_line: int
    end_line: int
    # Ordinal of a hard-split piece of one over-long line; 0 for ordinary chunks.
    part: int = 0
    # P1-阶段三 第四轮: the SEGMENT SEQUENCE this chunk was assembled from.
    #
    # A markdown chunk may be built from several NON-CONTIGUOUS line ranges
    # (the chunker merges adjacent heading blocks).  ``segments`` lists them
    # in order; ``gaps[i]`` is the number of trailing BLANK lines the
    # chunker's ``rstrip()`` removed from segment ``i``.
    #
    # Both default to empty, so every OLD caller (and every plain-text
    # chunk) keeps working unchanged: an empty ``segments`` means "this
    # chunk is one contiguous span", which is exactly what the bare anchor
    # ``L<start>-<end>`` already says.
    segments: List[Tuple[int, int]] = field(default_factory=list)
    gaps: List[int] = field(default_factory=list)


def chunk_for_file(text: str, path: str) -> List[TextChunk]:
    """THE chunking decision — one place, every caller (G-3).

    A chunk's identity is ``md5(path:start:end[:part])`` — so WHICH
    chunker cut the file decides the chunk's id. The writer and the
    rebuilder must therefore make the SAME decision, or a rebuild does
    not refresh the projection: it REPLACES the chunk identities, and
    every hit count, reference and audit trail that pointed at the old
    ids goes nowhere (measured on the real corpus: 56 of 86 files
    diverged, 180 ids vs 217 ids).

    The rule (the one the writer already used, kept unchanged so the
    identities ALREADY IN THE INDEX do not move):

      * ``.md`` files -> structure-aware :meth:`TextChunker.chunk_markdown`
      * anything else -> the plain splitter :meth:`TextChunker.chunk_text`

    Both the writer (``manager.add_memory`` / ``manager.sync``) and the
    rebuilder (``rebuilder.rebuild_file``) call THIS function. A second
    copy of the decision is how the two drifted apart in the first
    place.
    """
    chunker = TextChunker()
    name = (path or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
    if name.endswith(".md"):
        return chunker.chunk_markdown(text)
    return chunker.chunk_text(text)


class TextChunker:
    """Chunks text by line count with token estimation"""
    
    def __init__(self, max_tokens: int = 500, overlap_tokens: int = 50):
        """
        Initialize chunker
        
        Args:
            max_tokens: Maximum tokens per chunk
            overlap_tokens: Overlap tokens between chunks
        """
        self.max_tokens = max_tokens
        self.overlap_tokens = overlap_tokens
        # Rough estimation: ~4 chars per token for English/Chinese mixed
        self.chars_per_token = 4
    
    def chunk_text(self, text: str) -> List[TextChunk]:
        """
        Chunk text into overlapping segments
        
        Args:
            text: Input text to chunk
            
        Returns:
            List of TextChunk objects
        """
        if not text.strip():
            return []
        
        lines = text.split('\n')
        chunks = []
        
        max_chars = self.max_tokens * self.chars_per_token
        overlap_chars = self.overlap_tokens * self.chars_per_token
        
        current_chunk = []
        current_chars = 0
        start_line = 1
        
        for i, line in enumerate(lines, start=1):
            line_chars = len(line)
            
            # If single line exceeds max, split it
            if line_chars > max_chars:
                # Save current chunk if exists
                if current_chunk:
                    chunks.append(TextChunk(
                        text='\n'.join(current_chunk),
                        start_line=start_line,
                        end_line=i - 1
                    ))
                    current_chunk = []
                    current_chars = 0
                
                # Split long line into multiple chunks
                for part, sub_chunk in enumerate(self._split_long_line(line, max_chars)):
                    chunks.append(TextChunk(
                        text=sub_chunk,
                        start_line=i,
                        end_line=i,
                        part=part
                    ))
                
                start_line = i + 1
                continue
            
            # Check if adding this line would exceed limit
            separator_chars = 1 if current_chunk else 0
            if current_chars + separator_chars + line_chars > max_chars and current_chunk:
                # Save current chunk
                chunks.append(TextChunk(
                    text='\n'.join(current_chunk),
                    start_line=start_line,
                    end_line=i - 1
                ))
                
                # Start new chunk with overlap
                # Leave room for the incoming line and its separator.
                overlap_budget = min(overlap_chars, max_chars - line_chars - 1)
                overlap_lines = self._get_overlap_lines(current_chunk, overlap_budget)
                current_chunk = overlap_lines + [line]
                current_chars = sum(len(l) for l in current_chunk) + len(current_chunk) - 1
                start_line = i - len(overlap_lines)
            else:
                # Add line to current chunk
                current_chunk.append(line)
                current_chars += separator_chars + line_chars
        
        # Save last chunk
        if current_chunk:
            chunks.append(TextChunk(
                text='\n'.join(current_chunk),
                start_line=start_line,
                end_line=len(lines)
            ))
        
        # A blank line sitting next to an over-long line is flushed as its own
        # chunk whose text is empty. Such chunks carry nothing, and embedding
        # APIs reject an empty string inside a batch by failing the whole
        # request, so they must not reach the index.
        return [c for c in chunks if c.text.strip()]
    
    def _split_long_line(self, line: str, max_chars: int) -> List[str]:
        """Split a single long line into multiple chunks"""
        chunks = []
        for i in range(0, len(line), max_chars):
            chunks.append(line[i:i + max_chars])
        return chunks
    
    def _get_overlap_lines(self, lines: List[str], target_chars: int) -> List[str]:
        """Get last few lines that fit within target_chars for overlap"""
        overlap = []
        chars = 0
        
        for line in reversed(lines):
            line_chars = len(line) + (1 if overlap else 0)
            if chars + line_chars > target_chars:
                break
            overlap.insert(0, line)
            chars += line_chars
        
        return overlap
    
    # --- Markdown structure-aware chunking ---------------------------------
    # Fixed char thresholds (independent of the token-based params above).
    # Calibrated by eval: single-file boundary, title-aware split only when a
    # file exceeds the target, cross-heading greedy merge + single tail fold.
    MD_CHUNK_TARGET = 1500   # char soft ceiling per chunk
    MD_FRAGMENT_MAX = 400    # a trailing block <= this folds into the previous

    # Bump whenever chunk_markdown()'s strategy changes in a way that alters
    # existing chunk boundaries. Used to tell an already-built index apart from
    # one produced by the current algorithm (see detect_chunker_version), so
    # /memory status can suggest a rebuild instead of silently keeping stale
    # boundaries forever (file hashes do not change when only the chunker does).
    # v2: keep text before the first heading; end a heading's body at the next
    # heading of any level (skipped levels used to be indexed twice).
    # v3: hard-split pieces of one over-long line carry a part ordinal in their id.
    # v4 (P1-阶段三 第四轮): the chunk now CARRIES ITS SEGMENT SEQUENCE
    # (the non-contiguous line ranges it was assembled from) and the
    # tail-blank count of each segment.  The chunk TEXT and the chunk ID are
    # UNCHANGED — only the anchor string gains a ``+S`` suffix — but the
    # index must be able to tell a v3 row from a v4 row, and
    # ``files.chunker_version`` is the one place that says which algorithm
    # produced a projection.  Not bumping it would leave the inspector
    # unable to tell "this row has no segment sequence" from "this row was
    # cut by an algorithm that did not need one".
    CHUNKER_VERSION = 4

    def chunk_markdown(self, text: str) -> List[TextChunk]:
        """Chunk a markdown file while respecting its heading structure.

        Strategy (per file):
          - A file <= MD_CHUNK_TARGET chars is ONE chunk (whole file is a
            strong semantic unit; most real memory files take this path).
          - Larger files are heading-aware split via markdown-it:
              * candidate segment = every heading node's OWN direct body only
                (heading line up to its first direct child heading); parent
                headings do NOT swallow child bodies;
              * cross-heading greedy merge: walk segments in order, a segment
                joins the current block iff the total stays <= MD_CHUNK_TARGET,
                else it starts a new block;
              * single tail fold: if the last block is <= MD_FRAGMENT_MAX chars
                it is folded into the previous block unconditionally.
        Line numbers are 1-based over the whole file, matching chunk_text.

        Args:
            text: full markdown file content

        Returns:
            List of TextChunk objects
        """
        if not text.strip():
            return []
        lines = text.split('\n')

        # Whole file under target: single chunk (semantic isolation).
        if len(text) <= self.MD_CHUNK_TARGET:
            return [TextChunk(text=text, start_line=1, end_line=len(lines))]

        # Delay import so the zero-dep chunk_text path and module import are
        # unaffected when markdown-it is unavailable.
        try:
            import markdown_it
        except ImportError:
            # Fallback: no parser -> plain line splitter.
            return self.chunk_text(text)

        segments = self._md_leaf_segments(text, lines, markdown_it)
        if not segments:
            return [TextChunk(text=text, start_line=1, end_line=len(lines))]

        # Greedy cross-heading merge.
        blocks: List[List[dict]] = [[segments[0]]]
        cur_len = len(segments[0]['text'])
        for seg in segments[1:]:
            if cur_len + len(seg['text']) <= self.MD_CHUNK_TARGET:
                blocks[-1].append(seg)
                cur_len += len(seg['text'])
            else:
                blocks.append([seg])
                cur_len = len(seg['text'])

        # Single tail fold: if the last block is a fragment (<= MD_FRAGMENT_MAX
        # chars), fold it unconditionally into the previous block.
        if len(blocks) >= 2:
            tail_len = sum(len(s['text']) for s in blocks[-1])
            if tail_len <= self.MD_FRAGMENT_MAX:
                tail = blocks.pop()
                blocks[-1].extend(tail)

        result: List[TextChunk] = []
        for blk in blocks:
            content = '\n\n'.join(s['text'] for s in blk)
            start_line = blk[0]['start_line']           # already 1-based
            end_line = blk[-1]['end_line']               # already 1-based
            # P1-阶段三 第四轮: the segment sequence and the tail-blank
            # count of each segment.  The chunk TEXT is unchanged — the
            # sequence is extra INFORMATION, so the chunk id (which is
            # built from the span alone) does not move.
            segs = [(s['start_line'], s['end_line']) for s in blk]
            gaps = [self._tail_blanks(lines, a, b) for (a, b) in segs]
            result.append(TextChunk(text=content, start_line=start_line,
                                    end_line=end_line,
                                    segments=segs, gaps=gaps))
        return result

    @staticmethod
    def _tail_blanks(lines: List[str], a: int, b: int) -> int:
        """How many trailing BLANK lines the segment's ``rstrip()`` removed.

        The chunker builds a segment's text as
        ``'\\n'.join(lines[a-1:b]).rstrip()`` — the ``rstrip`` eats the
        segment's trailing blank lines, and that is information the truth
        layer holds but the chunk text does not.  Recording the count is
        what lets a reader explain WHY a rebuilt fragment differs from the
        stored text instead of just reporting "not equal".

        It is a RECORD, not a rebuild input: the rebuild joins the
        segments with a fixed blank line (measured — see
        ``body_pointer.rebuild_from_anchor``).
        """
        seg = lines[a - 1:b]
        n = 0
        while n < len(seg) and seg[len(seg) - 1 - n].strip() == "":
            n += 1
        return n

    def _md_leaf_segments(self, text: str, lines: List[str], markdown_it) -> List[dict]:
        """Return every heading's OWN direct body as a candidate segment.

        A segment is the heading line plus its text up to the next heading of
        any level, so parent headings do NOT swallow child bodies. Text before
        the first heading is a segment of its own. 1-based line numbers; dicts
        carry {'start_line','end_line','text'}.
        """
        md = markdown_it.MarkdownIt()
        toks = md.parse(text)

        heads = []
        for i, t in enumerate(toks):
            if t.type == 'heading_open' and i + 1 < len(toks) and toks[i + 1].type == 'inline':
                heads.append({'line': t.map[0], 'level': int(t.tag[1])})  # 0-based line

        if not heads:
            return [{'start_line': 1, 'end_line': len(lines), 'text': text}]

        n = len(heads)
        segs = []
        # Text before the first heading (a preface, a note, a TOC) belongs to
        # no heading, so it gets a segment of its own instead of being dropped.
        first0 = heads[0]['line']
        preface = '\n'.join(lines[:first0]).rstrip()
        if preface.strip():
            segs.append({'start_line': 1, 'end_line': first0, 'text': preface})
        for i, h in enumerate(heads):
            start0 = h['line']
            # A heading's own body stops at the next heading of ANY level: a
            # deeper one is a child (even when levels are skipped, e.g. # then
            # ###), a shallower or equal one closes the section.
            end0 = heads[i + 1]['line'] if i + 1 < n else len(lines)  # exclusive 0-based
            # Convert to 1-based inclusive lines.
            start_line = start0 + 1
            end_line = end0  # end0 is 0-based exclusive -> 1-based inclusive end
            content = '\n'.join(lines[start0:end0]).rstrip()
            if content.strip():
                segs.append({'start_line': start_line, 'end_line': end_line,
                             'text': content})
        return segs

