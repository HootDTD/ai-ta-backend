"""Retriever module for hybrid semantic+lexical search with context packing and answer generation.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple, Any, Set, Optional

import faiss
import numpy as np
import pandas as pd
import tiktoken
from openai import OpenAI
from .config import (
    get_citation_label,
    get_subject_name,
    get_subject_priority,
    get_subject_source,
    set_subject_name,
)
from .contracts import ResearchBundle, BundleSnippet, ParsedTask, ResearchMetadata
from .knowledge import KnowledgeManager
from .citations.formatter import build_citation_info, format_citations

WIRE = os.getenv("RETRIEVAL_WIRE_LOG", "off").lower() not in {"0","off","false","no"}

# ----------------------- Data classes -----------------------


@dataclass
class Hit:
    id: str
    score_sem: float
    rank_sem: int
    score_lex: float
    rank_lex: int
    score_fused: float
    score_equal: Optional[float] = None


@dataclass
class ContextSnippet:
    id: str
    type: str
    page: int
    section_path: str
    text: str
    figure_id: str | None
    why: str
    source_path: str
    doc_title: str | None
    doc_short: str
    final_score: Dict[str, float] | None = None
    origin_id: str | None = None


@dataclass
class ContextPack:
    snippets: List[ContextSnippet]
    used_ids: List[str]
    stats: Dict[str, int]


@dataclass
class Answer:
    text: str
    citations: List[Dict[str, Any]]
    proof: Dict[str, object]
    structured_citations: List[Dict[str, Any]] = field(default_factory=list)


# ----------------------- Globals -----------------------

_faiss_list: List[faiss.Index] = []
_sqlite_conns: List[sqlite3.Connection] = []
_items_dfs: List[pd.DataFrame] = []
_items_df: pd.DataFrame | None = None
_id_to_row: Dict[str, pd.Series] | None = None
_meta: Dict[str, object] | None = None
_meta_titles: Dict[str, str] | None = None
_client: OpenAI | None = None
_store_biases: Dict[str, float] = {}
_store_meta: Dict[str, Dict[str, Any]] = {}
_lex_table_map: Dict[int, Tuple[str, ...]] = {}


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


_STORE_WEIGHT_DEFAULTS = {
    "textbook": 0.12,
    "slides": 0.06,
    "homework": 0.05,
    "exams": 0.05,
    "other": 0.03,
}


def _store_weight_for_kind(kind: str) -> float:
    key = kind.strip().lower()
    env_var = f"RETRIEVAL_STORE_WEIGHT_{key.upper()}"
    val = os.getenv(env_var)
    if val is not None:
        try:
            return float(val)
        except (TypeError, ValueError):
            pass
    return _STORE_WEIGHT_DEFAULTS.get(key, 0.0)


_STORE_CONF_SCALE = _float_env("RETRIEVAL_STORE_CONF_SCALE", 0.0)
_STORE_CONF_BASE = _float_env("RETRIEVAL_STORE_CONF_BASE", 0.5)


def _compute_store_bias(store_entry: Optional[Dict[str, Any]], meta: Optional[Dict[str, Any]]) -> float:
    if not store_entry:
        return 0.0
    kind = store_entry.get("kind") or "other"
    base = _store_weight_for_kind(str(kind))
    if base == 0.0:
        return 0.0
    if _STORE_CONF_SCALE != 0.0:
        avg_conf: Optional[float] = None
        candidate = None
        if meta:
            candidate = meta.get("average_confidence") or meta.get("ocr_confidence")
        if candidate is None:
            candidate = store_entry.get("average_confidence")
        if isinstance(candidate, (int, float)):
            avg_conf = float(candidate)
        if avg_conf is not None:
            base *= 1.0 + _STORE_CONF_SCALE * (avg_conf - _STORE_CONF_BASE)
    return max(base, 0.0)


def _resolve_store_entry(root: Path) -> Optional[Dict[str, Any]]:
    try:
        manager = KnowledgeManager()
        return manager.find_store_entry(root)
    except Exception:
        return None

# symbol/alias mining globals
_alias_to_occurrences: Dict[str, List[Tuple[str, str, int, str, str]]] = {}
_term_to_aliases: Dict[str, set[str]] = {}
_symbol_index_stats: Dict[str, Any] = {}
_definition_ids: set[str] = set()
_last_expansion_plan: List[Dict[str, Any]] = []


# ----------------------- Helpers -----------------------


def _require_loaded():
    if not (
        _faiss_list
        and _items_df is not None
        and _sqlite_conns
        and _meta
    ):
        raise RuntimeError("Assets not loaded. Call load_assets() first.")


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is not set.")
        _client = OpenAI()
    return _client


def _fts_safe_query(text: str) -> str:
    tokens = re.findall(r"[A-Za-z0-9]+", text.lower())
    tokens = [t for t in tokens if len(t) > 1]
    return " OR ".join(tokens)


def _fts_term_count(term: str) -> int:
    """Return total FTS hit count across loaded indexes for ``term``."""

    q = _fts_safe_query(term)
    total = 0
    phrase = None
    if re.search(r"[\s-]", term):
        phrase = f'"{term}"'
    for conn in _sqlite_conns:
        try:
            if phrase:
                cur = conn.execute(
                    "SELECT count(*) FROM items WHERE items MATCH ?",
                    (phrase,),
                )
                row = cur.fetchone()
                if row and int(row[0]) > 0:
                    total += int(row[0])
                    continue
            cur = conn.execute(
                "SELECT count(*) FROM items WHERE items MATCH ?",
                (q,),
            )
            row = cur.fetchone()
            if row:
                total += int(row[0])
        except Exception:
            continue
    return total


_STOPWORDS: Set[str] = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "of",
    "to",
    "in",
    "for",
    "with",
    "on",
    "by",
    "what",
    "is",
    "are",
    "be",
    "from",
    "that",
    "this",
}


def _analyze_query(q: str) -> Dict[str, Any]:
    """Return term presence diagnostics for a sanitized query."""

    tokens = [
        t
        for t in re.findall(r"[a-z0-9_\-]+", q.lower())
        if t not in _STOPWORDS and not t.isdigit()
    ]
    term_presence: Dict[str, Dict[str, Any]] = {}
    missing: List[str] = []
    expansion: Dict[str, List[str]] = {}
    for term in tokens:
        fts = _fts_term_count(term)
        aliases = list(_term_to_aliases.get(term, set()))
        alias_hits = []
        cand_scores: List[Tuple[int, str]] = []
        for al in aliases:
            c = _fts_term_count(al)
            cand_scores.append((c, al))
            if c > 0:
                alias_hits.append(al)
        alias_hit = bool(alias_hits)
        # Morphology variants
        morph: Set[str] = set()
        if term.endswith("s"):
            morph.add(term[:-1])
        else:
            morph.add(term + "s")
        morph.add(term.replace("-", ""))
        for m in morph:
            c = _fts_term_count(m)
            cand_scores.append((c, m))
        cand_scores.sort(reverse=True)
        expansion[term] = [v for c, v in cand_scores if v != term]
        term_presence[term] = {"fts_count": fts, "alias_hit": alias_hit}
        if fts == 0 and not alias_hit:
            missing.append(term)
    return {
        "term_presence": term_presence,
        "missing_terms": missing,
        "expansion_candidates": expansion,
    }


def _prepare_indexes(doc_sets: List[str]) -> Tuple[List[str], List[Dict[str, Any]]]:
    """Load the requested document sets and return loaded/skipped metadata."""

    if not doc_sets:
        return [], []

    paths = [Path(p) for p in doc_sets]
    try:
        if len(paths) > 1:
            _, skipped = load_assets_all(paths)
        else:
            load_assets(paths[0])
            skipped = []
    except RuntimeError as exc:
        raise RuntimeError(str(exc))

    skipped_set = {s.get("path") for s in skipped if isinstance(s, dict)}
    loaded = [str(p) for p in paths if str(p) not in skipped_set]
    return loaded, skipped


def _norm_key(path: str) -> str:
    if not path:
        return ""
    return os.path.normcase(path.replace("/", os.sep).replace("\\", os.sep))


def _flag(name: str, default: bool = True) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.lower() not in {"0", "off", "false", "no"}


def _is_fts_table(sql: str | None) -> bool:
    if not sql:
        return False
    upper = sql.upper()
    return "VIRTUAL TABLE" in upper and "FTS5" in upper


def _detect_lex_tables(conn: sqlite3.Connection) -> Tuple[str, ...]:
    try:
        cur = conn.cursor()
        cur.execute("SELECT name, sql FROM sqlite_master WHERE type='table'")
        rows = cur.fetchall()
    except sqlite3.Error:
        return ("items",)
    info = {name: sql for name, sql in rows}
    tables: List[str] = []
    if _is_fts_table(info.get("items")):
        tables.append("items")
    for cand in ("fts", "items_fts"):
        if _is_fts_table(info.get(cand)):
            tables.append(cand)
    return tuple(tables or ("items",))


def _lex_fetch(
    cur: sqlite3.Cursor, table: str, query: str, limit: int, unlimited: bool
) -> List[Tuple[str, float]]:
    sql = (
        f"SELECT id, bm25({table}) as score FROM {table} "
        f"WHERE {table} MATCH ? ORDER BY score"
    )
    if unlimited:
        cur.execute(sql, (query,))
    else:
        cur.execute(sql + " LIMIT ?", (query, limit))
    return cur.fetchall()


def _compute_equal_scores(snippets: List[Any]) -> None:
    """Attach equal-weight scores to snippets based on semantic, lexical, and fused values."""
    if not snippets:
        return

    sem_vals: List[float] = []
    lex_vals: List[float] = []
    fused_vals: List[float] = []
    for sn in snippets:
        fs = getattr(sn, "final_score", None) or {}
        try:
            sem = fs.get("semantic")
            if sem is not None:
                sem_vals.append(float(sem))
        except Exception:
            pass
        try:
            lex = fs.get("lexical")
            if lex is not None:
                lex_vals.append(float(lex))
        except Exception:
            pass
        try:
            fused = fs.get("fused")
            if fused is not None:
                fused_vals.append(float(fused))
        except Exception:
            pass

    lex_lo = lex_hi = None
    lex_vals_stats: List[float] = []
    if lex_vals:
        try:
            lex_lo = float(np.percentile(lex_vals, 5))
            lex_hi = float(np.percentile(lex_vals, 95))
        except Exception:
            lex_lo = lex_hi = None
        if lex_lo is not None and lex_hi is not None:
            if lex_lo > lex_hi:
                lex_lo, lex_hi = lex_hi, lex_lo
            lex_vals_stats = [min(max(v, lex_lo), lex_hi) for v in lex_vals]
        else:
            lex_vals_stats = list(lex_vals)

    sem_mean = float(np.mean(sem_vals)) if sem_vals else 0.0
    sem_std = float(np.std(sem_vals)) if sem_vals else 0.0
    lex_mean = float(np.mean(lex_vals_stats)) if lex_vals_stats else 0.0
    lex_std = float(np.std(lex_vals_stats)) if lex_vals_stats else 0.0
    fused_mean = float(np.mean(fused_vals)) if fused_vals else 0.0
    fused_std = float(np.std(fused_vals)) if fused_vals else 0.0

    def _z_score(value: Optional[float], mean: float, std: float) -> Optional[float]:
        if value is None:
            return None
        if std <= 1e-9:
            return 0.0
        return (value - mean) / std

    for sn in snippets:
        fs_obj = getattr(sn, "final_score", None)
        fs = fs_obj if isinstance(fs_obj, dict) else {}
        sem_raw = fs.get("semantic")
        lex_raw = fs.get("lexical")
        fused_raw = fs.get("fused")
        sem_z = _z_score(
            float(sem_raw) if sem_raw is not None else None, sem_mean, sem_std
        )
        lex_clip = None
        if lex_raw is not None:
            try:
                lex_val = float(lex_raw)
                if lex_lo is not None and lex_hi is not None:
                    lex_val = min(max(lex_val, lex_lo), lex_hi)
                lex_clip = lex_val
            except Exception:
                lex_clip = None
        lex_z = _z_score(lex_clip, lex_mean, lex_std)
        fused_z = _z_score(
            float(fused_raw) if fused_raw is not None else None, fused_mean, fused_std
        )
        components = [v for v in (sem_z, lex_z, fused_z) if v is not None]
        equal = float(np.mean(components)) if components else -1e9
        if fs.get("equal") is None:
            fs["equal"] = equal
        setattr(sn, "final_score", fs)


def _compute_hit_equal_scores(hits: List[Hit]) -> None:
    """Compute equal scores for raw hits prior to snippet trimming."""
    if not hits:
        return

    class _HitProxy:
        __slots__ = ("final_score",)

        def __init__(self, hit: Hit):
            self.final_score = {
                "semantic": float(hit.score_sem),
                "lexical": float(hit.score_lex),
                "fused": float(hit.score_fused),
            }

    proxies: List[_HitProxy] = [_HitProxy(hit) for hit in hits]
    _compute_equal_scores(proxies)
    for hit, proxy in zip(hits, proxies):
        fs = getattr(proxy, "final_score", {}) or {}
        equal = fs.get("equal")
        hit.score_equal = float(equal) if equal is not None else None


def _extract_subject(meta: Dict[str, object] | None) -> str:
    if not isinstance(meta, dict):
        return ""
    subject = meta.get("subject")
    if isinstance(subject, str) and subject.strip():
        return subject
    discipline = meta.get("discipline")
    if isinstance(discipline, str) and discipline.strip():
        return discipline
    return ""

_SYMBOL_HEAD_CLASS = "[A-Za-z_\u0370-\u03FF\u1F00-\u1FFF]"
_SYMBOL_BODY_CLASS = "[A-Za-z0-9_\u0370-\u03FF\u1F00-\u1FFF]"
_SYMBOL_TOKEN_PATTERN = f"{_SYMBOL_HEAD_CLASS}{_SYMBOL_BODY_CLASS}*"
_SYMBOL_TOKEN_REGEX = re.compile(_SYMBOL_TOKEN_PATTERN)
_ALIAS_WHERE_REGEX = re.compile(rf"where\s+({_SYMBOL_TOKEN_PATTERN})\s*=")
_ALIAS_DEFINED_REGEX = re.compile(
    rf"({_SYMBOL_TOKEN_PATTERN})\s+(?:is\s+defined|defined\s+as|denoted\s+by)\b"
)


def _symbol_tokens(text: str) -> List[str]:
    return _SYMBOL_TOKEN_REGEX.findall(text)


_GREEK = {
    "α": "alpha",
    "Α": "alpha",
    "β": "beta",
    "Β": "beta",
    "γ": "gamma",
    "Γ": "gamma",
    "δ": "delta",
    "Δ": "delta",
    "ε": "epsilon",
    "Ε": "epsilon",
    "θ": "theta",
    "Θ": "theta",
    "λ": "lambda",
    "Λ": "lambda",
    "μ": "mu",
    "Μ": "mu",
    "π": "pi",
    "Π": "pi",
    "ρ": "rho",
    "Ρ": "rho",
    "σ": "sigma",
    "Σ": "sigma",
    "τ": "tau",
    "Τ": "tau",
    "φ": "phi",
    "Φ": "phi",
    "ω": "omega",
    "Ω": "omega",
    "I?": "alpha",
    "I?": "beta",
    "I3": "gamma",
    "I'": "delta",
    "I?": "epsilon",
    "I,": "theta",
    "I?": "lambda",
    "I?": "mu",
    "I?": "pi",
    "I?": "rho",
    "I?": "sigma",
    "I,": "tau",
    "I+": "phi",
    "I%": "omega",
}


def _norm_alias(token: str) -> str:
    token = token.strip().lower()
    for g, name in _GREEK.items():
        token = token.replace(g, name)
    token = re.sub(r"[_\s]+", "", token)
    return token


def _alias_strings_from_occurrences(norm: str) -> List[str]:
    display: Set[str] = set()
    for _, _, _, line, _ in _alias_to_occurrences.get(norm, []):
        for tok in _SYMBOL_TOKEN_REGEX.findall(line):
            if _norm_alias(tok) == norm:
                display.add(tok)
    return sorted(display)


def _looks_symbol(token: str) -> bool:
    return bool(_SYMBOL_TOKEN_REGEX.fullmatch(token))


def _mine_aliases(df: pd.DataFrame) -> None:
    if not _flag("RETRIEVAL_ALIAS_MINER", True):
        return
    alias_occ: Dict[str, List[Tuple[str, str, int, str, str]]] = {}
    term_map: Dict[str, set[str]] = {}
    def_ids: set[str] = set()
    for idx, row in df.iterrows():
        text = row.get("text", "") or ""
        doc = row.get("doc_short") or "doc"
        sec = row.get("section_path", "") or ""
        page = int(row.get("page", 0))
        for line in text.splitlines():
            line_stripped = line.strip()
            # term (Alias)
            for m in re.finditer(r"([A-Za-z][A-Za-z\s\-]+?)\(([^)]+)\)", line_stripped):
                part1 = m.group(1).strip()
                part2 = m.group(2).strip()
                if _looks_symbol(part2):
                    alias_norm = _norm_alias(part2)
                    alias_occ.setdefault(alias_norm, []).append((doc, sec, page, line_stripped, "paren"))
                    term_map.setdefault(part1.lower(), set()).add(alias_norm)
                    def_ids.add(idx)
                elif _looks_symbol(part1):
                    alias_norm = _norm_alias(part1)
                    alias_occ.setdefault(alias_norm, []).append((doc, sec, page, line_stripped, "paren"))
                    term_map.setdefault(part2.lower(), set()).add(alias_norm)
                    def_ids.add(idx)
            # where Alias = definition
            m = _ALIAS_WHERE_REGEX.search(line_stripped)
            if m:
                alias_norm = _norm_alias(m.group(1))
                alias_occ.setdefault(alias_norm, []).append((doc, sec, page, line_stripped, "where"))
                def_ids.add(idx)
            # Alias is defined as
            m = _ALIAS_DEFINED_REGEX.search(line_stripped)
            if m:
                alias_norm = _norm_alias(m.group(1))
                alias_occ.setdefault(alias_norm, []).append((doc, sec, page, line_stripped, "def"))
                def_ids.add(idx)

    for k, v in alias_occ.items():
        _alias_to_occurrences.setdefault(k, []).extend(v)
    for term, aliases in term_map.items():
        _term_to_aliases.setdefault(term, set()).update(aliases)
    _symbol_index_stats.update({
        "alias_count": len(_alias_to_occurrences),
        "term_count": len(_term_to_aliases),
    })
    _definition_ids.update(def_ids)


def _fts_count(q: str) -> int:
    total = 0
    if not q:
        return 0
    for conn in _sqlite_conns:
        cur = conn.cursor()
        try:
            cur.execute("SELECT count(*) FROM items WHERE items MATCH ?", (q,))
            total += int(cur.fetchone()[0])
        except sqlite3.OperationalError:
            continue
    return total


_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "that",
    "this",
    "from",
    "are",
    "was",
    "were",
    "have",
    "has",
    "into",
    "than",
    "such",
    "using",
    "use",
    "upon",
}


def _prf_terms(hits: List[Hit], top_n: int = 5) -> List[str]:
    texts: List[str] = []
    for h in hits[:top_n]:
        row = _id_to_row.get(h.id) if _id_to_row else None
        if row is not None:
            texts.append(row.get("text", ""))
    if not texts:
        return []
    tokens = re.findall(r"[A-Za-z]{3,}", " ".join(texts).lower())
    freq: Dict[str, int] = {}
    for t in tokens:
        if t in _STOPWORDS:
            continue
        freq[t] = freq.get(t, 0) + 1
    terms = [t for t, _ in sorted(freq.items(), key=lambda kv: kv[1], reverse=True)[:2]]
    return terms


def _canonical_marker(sn) -> str:
    """Return a normalized citation marker like ``[Textbook, p. 123]``."""

    label = get_citation_label()
    page_val = getattr(sn, "page", None)
    page = page_val if isinstance(page_val, int) and page_val > 0 else "?"

    return f"[{label}, p. {page}]"


# ----------------------- Converters -----------------------


def _context_to_bundle_snippets(ctx: ContextPack) -> List[BundleSnippet]:
    snippets: List[BundleSnippet] = []
    for sn in ctx.snippets:
        marker = _canonical_marker(sn)
        snippets.append(
            BundleSnippet(
                id=sn.id,
                type=sn.type,
                page=sn.page,
                section_path=sn.section_path,
                text=sn.text,
                figure_id=sn.figure_id,
                why=sn.why,
                source_path=sn.source_path,
                doc_title=sn.doc_title,
                doc_short=sn.doc_short,
                citation_marker=marker,
                final_score=sn.final_score,
            )
        )
    return snippets


def _summarize_snippets(
    snippets: List[BundleSnippet],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, int]]:
    eq_map: Dict[str, Dict[str, Any]] = {}
    glossary: List[Dict[str, Any]] = []
    assumptions: List[Dict[str, Any]] = []
    alias_counts: Dict[str, int] = {}

    for sn in snippets:
        sym_set: set[str] = set()
        lines = sn.text.splitlines()
        for line in lines:
            if "=" in line or re.search(r"\(\d+-\d+\)", line):
                norm = re.sub(r"\s+", " ", line.strip().rstrip(".;,"))
                norm = re.sub(r"\s*\(\d+-\d+\)\s*$", "", norm)
                if norm.startswith("(") and norm.endswith(")"):
                    norm = norm[1:-1]
                entry = eq_map.setdefault(
                    norm,
                    {
                        "eq_text": line.strip(),
                        "symbol_set": set(),
                        "source_snippet_ids": set(),
                    },
                )
                syms = re.findall(r"[A-Za-z]\w*", line)
                entry["symbol_set"].update(syms)
                entry["source_snippet_ids"].add(sn.id)
                sym_set.update(syms)
            match = re.search(r"where\s+([A-Za-z]\w*)\s+is\s+([^.;]+)", line)
            if match and match.group(1) in sym_set:
                meaning = match.group(2).strip()
                if len(meaning) <= 140:
                    glossary.append(
                        {
                            "term": match.group(1),
                            "meaning": meaning,
                            "source_snippet_ids": [sn.id],
                        }
                    )
            if re.search(r"assum(?:e|ing)|valid for|applicable when|boundary condition", line, re.I):
                assumptions.append(
                    {"text": line.strip(), "source_snippet_ids": [sn.id]}
                )

        for tok in _symbol_tokens(sn.text):
            norm = _norm_alias(tok)
            if norm in _alias_to_occurrences:
                alias_counts[norm] = alias_counts.get(norm, 0) + 1

    equations: List[Dict[str, Any]] = []
    for v in eq_map.values():
        equations.append(
            {
                "eq_text": v["eq_text"],
                "symbol_set": list(sorted(v["symbol_set"])),
                "source_snippet_ids": list(sorted(v["source_snippet_ids"])),
            }
        )

    return equations, glossary, assumptions, alias_counts


def _words_within_window(text: str, words: List[str], window: int = 60) -> bool:
    if not words:
        return False
    # Preserve input order but drop duplicates to avoid redundant tracking.
    seen: List[str] = []
    for w in words:
        if w not in seen:
            seen.append(w)
    occurrences: List[Tuple[int, int]] = []
    for idx, word in enumerate(seen):
        pattern = rf"\b{re.escape(word)}\b"
        for match in re.finditer(pattern, text):
            occurrences.append((match.start(), idx))
    if not occurrences:
        return False
    occurrences.sort(key=lambda item: item[0])
    counts = [0] * len(seen)
    have = 0
    left = 0
    for right, (pos, idx) in enumerate(occurrences):
        if counts[idx] == 0:
            have += 1
        counts[idx] += 1
        while have == len(seen) and left <= right:
            span = occurrences[right][0] - occurrences[left][0]
            if span <= window:
                return True
            l_idx = occurrences[left][1]
            counts[l_idx] -= 1
            if counts[l_idx] == 0:
                have -= 1
            left += 1
    return False


def _has_explicit_evidence(
    snippets: List[BundleSnippet], term: str, diag_entry: Optional[Dict[str, Any]] = None
) -> bool:
    """Return ``True`` if snippets explicitly reference ``term`` or its aliases."""

    term_lower = term.strip().lower()
    if not term_lower:
        return False

    alias_norms: Set[str] = set(_term_to_aliases.get(term_lower, set()))
    if diag_entry:
        for cand in diag_entry.get("alias_hits", []):
            alias_norms.add(_norm_alias(cand))

    expansions = []
    if diag_entry:
        raw = diag_entry.get("expansion_candidates") or []
        if isinstance(raw, dict):
            expansions.extend(raw.get(term_lower, []))
        elif isinstance(raw, list):
            expansions.extend(raw)

    term_norm = _norm_alias(term_lower.replace(" ", "")) if term_lower else ""
    words = [w for w in _symbol_tokens(term_lower) if len(w) > 1]

    window_chars = int(os.getenv("RETRIEVAL_PROXIMITY_CHARS", "60"))

    for sn in snippets:
        text = sn.text or ""
        text_lower = text.lower()
        if term_lower and term_lower in text_lower:
            return True
        if len(words) == 1 and words[0] in text_lower:
            return True
        if len(words) > 1 and _words_within_window(text_lower, words, window=window_chars):
            return True

        tokens = _symbol_tokens(text)
        norm_tokens = {_norm_alias(tok) for tok in tokens}
        if term_norm and term_norm in norm_tokens:
            return True
        if alias_norms.intersection(norm_tokens):
            return True
        for cand in expansions:
            if not isinstance(cand, str):
                continue
            cand_lower = cand.lower()
            if cand_lower and cand_lower in text_lower:
                return True
            cand_norm = _norm_alias(cand_lower.replace(" ", ""))
            if cand_norm in norm_tokens:
                return True
    return False


# ----------------------- Public API -----------------------


def _load_one(root: Path) -> Tuple[faiss.Index, pd.DataFrame, sqlite3.Connection, dict, Dict[str, str]]:
    """Load FAISS, items DataFrame, SQLite, and meta from an index directory."""
    faiss_path = root / "faiss.index"
    items_path = root / "items.jsonl"
    sqlite_path = root / "sqlite.db"
    meta_path = root / "meta.json"

    store_key = str(root.resolve())

    for p in [faiss_path, items_path, sqlite_path, meta_path]:
        if not p.exists():
            raise FileNotFoundError(
                f"Missing required asset: {p.name} in index directory {root}"
            )
    index = faiss.read_index(str(faiss_path))
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    aliases_raw = meta.get("aliases") or {}
    titles_raw = meta.get("doc_titles") or {}

    norm_aliases: Dict[str, str] = {}
    for k, v in aliases_raw.items():
        nk = _norm_key(k)
        nb = _norm_key(os.path.basename(k))
        norm_aliases[nk] = v
        norm_aliases[nb] = v

    norm_titles: Dict[str, str] = {}
    for k, v in titles_raw.items():
        nk = _norm_key(k)
        nb = _norm_key(os.path.basename(k))
        norm_titles[nk] = v
        norm_titles[nb] = v

    meta_titles = {**norm_titles, **norm_aliases}

    items = []
    with open(items_path, "r", encoding="utf-8") as f:
        for line in f:
            item = json.loads(line)
            sp = item.get("source_path") or item.get("source") or item.get("id")
            item["source_path"] = sp
            sp_norm = _norm_key(sp)
            sp_base = _norm_key(os.path.basename(sp))
            title = item.get("doc_title") or norm_titles.get(sp_norm) or norm_titles.get(sp_base)
            alias = norm_aliases.get(sp_norm) or norm_aliases.get(sp_base)
            item["doc_title"] = title
            item["doc_short"] = (
                item.get("doc_short")
                or alias
                or title
                or Path(sp).stem
                or "doc"
            )
            items.append(item)
    df = pd.DataFrame(items)
    df.set_index("id", inplace=True)
    df["store_key"] = store_key
    df["store_kind"] = None

    _mine_aliases(df)
    meta["symbol_index_stats"] = _symbol_index_stats

    conn = sqlite3.connect(str(sqlite_path))

    return index, df, conn, meta, meta_titles


def load_assets(root: Path) -> Tuple[faiss.Index, pd.DataFrame, sqlite3.Connection, dict]:
    global _alias_to_occurrences, _term_to_aliases, _symbol_index_stats, _definition_ids
    _alias_to_occurrences = {}
    _term_to_aliases = {}
    _symbol_index_stats = {}
    _definition_ids = set()
    global _store_biases, _store_meta
    _store_biases = {}
    _store_meta = {}

    store_entry = _resolve_store_entry(root)

    index, df, conn, meta, titles = _load_one(root)

    store_key = str(root.resolve())
    if store_entry:
        df["store_kind"] = store_entry.get("kind")
    bias = _compute_store_bias(store_entry, meta)
    _store_biases[store_key] = bias
    meta_info = {
        "kind": (store_entry or {}).get("kind", "textbook"),
        "title": (store_entry or {}).get("title"),
        "average_confidence": meta.get("average_confidence"),
    }
    _store_meta[store_key] = meta_info

    global _faiss_list, _sqlite_conns, _items_dfs, _items_df, _id_to_row, _meta, _meta_titles, _lex_table_map
    _faiss_list = [index]
    _sqlite_conns = [conn]
    _items_dfs = [df]
    _items_df = df
    _id_to_row = {idx: df.loc[idx] for idx in df.index}
    _meta = meta
    _meta_titles = titles
    _lex_table_map = {id(conn): _detect_lex_tables(conn)}

    subject = _extract_subject(meta)
    if subject:
        set_subject_name(subject, "meta")

    return index, df, conn, meta


def load_assets_all(
    roots: List[Path],
) -> Tuple[List[Tuple[faiss.Index, pd.DataFrame, sqlite3.Connection, dict]], List[Dict[str, str]]]:
    """Load assets from multiple index directories."""
    global _alias_to_occurrences, _term_to_aliases, _symbol_index_stats, _definition_ids
    _alias_to_occurrences = {}
    _term_to_aliases = {}
    _symbol_index_stats = {}
    _definition_ids = set()
    global _store_biases, _store_meta
    _store_biases = {}
    _store_meta = {}

    faiss_list: List[faiss.Index] = []
    conns: List[sqlite3.Connection] = []
    dfs: List[pd.DataFrame] = []
    metas: List[dict] = []
    title_map: Dict[str, str] = {}
    skipped: List[Dict[str, str]] = []
    meta_ref: dict | None = None
    store_entries: Dict[str, Optional[Dict[str, Any]]] = {}
    for root in roots:
        store_key = str(root.resolve())
        store_entries[store_key] = _resolve_store_entry(root)
    for root in roots:
        try:
            index, df, conn, meta, titles = _load_one(root)
        except Exception as exc:  # pragma: no cover - robustness
            skipped.append({"path": str(root), "reason": str(exc)})
            continue
        model = meta.get("model")
        dim = meta.get("dimensions")
        if meta_ref is None:
            meta_ref = meta
        elif meta_ref.get("model") != model or str(meta_ref.get("dimensions")) != str(dim):
            skipped.append(
                {
                    "path": str(root),
                    "reason": f"incompatible model/dimension {model}/{dim}",
                }
            )
            continue
        faiss_list.append(index)
        conns.append(conn)
        store_key = str(root.resolve())
        entry = store_entries.get(store_key)
        if entry:
            df["store_kind"] = entry.get("kind")
        bias = _compute_store_bias(entry, meta)
        _store_biases[store_key] = bias
        meta_info = {
            "kind": (entry or {}).get("kind", "textbook"),
            "title": (entry or {}).get("title"),
            "average_confidence": meta.get("average_confidence"),
        }
        _store_meta[store_key] = meta_info
        dfs.append(df)
        metas.append(meta)
        title_map.update(titles)

    if not faiss_list:
        reason_counts: Dict[str, int] = {}
        for s in skipped:
            r = s.get("reason", "")
            if "incompatible" in r or "model/dimension" in r:
                key = "mismatched dims"
            elif "malformed" in r or "Missing required" in r:
                key = "malformed DB"
            else:
                key = "load error"
            reason_counts[key] = reason_counts.get(key, 0) + 1
        parts = [f"{cnt} {reason}" for reason, cnt in reason_counts.items()]
        msg = f"No valid indexes loaded (skipped {len(skipped)}: " + "; ".join(parts) + ")"
        raise RuntimeError(msg)

    merged_df = pd.concat(dfs)
    id_map = {idx: merged_df.loc[idx] for idx in merged_df.index}

    global _faiss_list, _sqlite_conns, _items_dfs, _items_df, _id_to_row, _meta, _meta_titles, _lex_table_map
    _faiss_list = faiss_list
    _sqlite_conns = conns
    _items_dfs = dfs
    _items_df = merged_df
    _id_to_row = id_map
    _meta = meta_ref
    _meta_titles = title_map
    _lex_table_map = {id(conn): _detect_lex_tables(conn) for conn in conns}

    subjects_found: List[str] = []
    for meta in metas:
        subj = _extract_subject(meta)
        if subj:
            subjects_found.append(subj)
    if subjects_found:
        unique_subjects: List[str] = []
        for subj in subjects_found:
            if subj not in unique_subjects:
                unique_subjects.append(subj)
        chosen = unique_subjects[0]
        existing_priority = get_subject_priority()
        if existing_priority < 2:
            if len(unique_subjects) > 1 and WIRE:
                print(f'[Config] multiple subjects found; using "{chosen}"', flush=True)
            set_subject_name(chosen, "meta")
        elif len(unique_subjects) > 1 and WIRE:
            override = get_subject_name()
            source = get_subject_source().upper()
            print(
                f'[Config] multiple subjects found in indexes; keeping override "{override}" (source={source})',
                flush=True,
            )

    return list(zip(faiss_list, dfs, conns, metas)), skipped


# ---------------------------------------------------------


def _run_search(query: str, k_sem: int, k_lex: int, _prf: bool = False) -> List[Hit]:
    _require_loaded()
    client = _get_client()
    model = _meta["model"]
    dim = int(_meta["dimensions"])
    unlimited = _flag("RETRIEVAL_NO_FILTER", False)

    # alias expansion
    tokens = _symbol_tokens(query)
    aliases: set[str] = set()
    if _flag("RETRIEVAL_ALIAS_MINER", True):
        for t in tokens:
            aliases.update(_term_to_aliases.get(t.lower(), set()))
    alias_list = sorted(aliases)

    _last_expansion_plan.clear()
    base_count = _fts_count(_fts_safe_query(query))
    _last_expansion_plan.append({"type": "query", "terms": [query], "hit_count": base_count})
    for a in alias_list:
        _last_expansion_plan.append({"type": "alias", "terms": [a], "hit_count": _fts_count(a)})

    fts_tokens_base = [t for t in re.findall(r"[a-z0-9\-]+", query.lower()) if t not in _STOPWORDS and len(t) > 1]
    fts_tokens = sorted(set(fts_tokens_base + list(alias_list)))
    phrase_terms = [t for t in fts_tokens_base if len(t) > 1]
    fts_augmented = list(fts_tokens)
    for t in fts_tokens_base:
        if len(t) > 1:
            fts_augmented.append(f'"{t}"')
    if phrase_terms:
        fts_augmented.append('"' + " ".join(phrase_terms) + '"')
    seen_parts: Set[str] = set()
    fts_q_parts: List[str] = []
    for part in fts_augmented:
        if part and part not in seen_parts:
            seen_parts.add(part)
            fts_q_parts.append(part)
    fts_q = " OR ".join(fts_q_parts)

    if _flag("RETRIEVAL_PROXIMITY", True):
        if len(tokens) >= 2:
            window = int(os.getenv("RETRIEVAL_PROXIMITY_WINDOW", "3"))
            prox_q = " NEAR/{} ".format(window).join([t.lower() for t in tokens])
            if prox_q:
                _last_expansion_plan.append({"type": "proximity", "terms": [prox_q], "hit_count": _fts_count(prox_q)})
                if fts_q:
                    fts_q = f"({fts_q}) OR ({prox_q})"
                else:
                    fts_q = prox_q

    query_strings = [query] + list(alias_list)
    sem_scores: Dict[str, float] = {}
    sem_ranks: Dict[str, int] = {}
    for qstr in query_strings:
        q_emb = client.embeddings.create(model=model, input=[qstr], dimensions=dim).data[0].embedding
        q_vec = np.asarray(q_emb, dtype=np.float32)
        q_vec /= max(np.linalg.norm(q_vec), 1e-12)
        for df, index in zip(_items_dfs if len(_faiss_list) > 1 else [_items_df], _faiss_list):
            k_this = k_sem
            if unlimited:
                ntotal = None
                try:
                    ntotal = int(getattr(index, "ntotal"))
                except Exception:
                    ntotal = None
                if not ntotal and df is not None:
                    try:
                        ntotal = len(df)
                    except Exception:
                        ntotal = None
                if ntotal and ntotal > k_this:
                    k_this = ntotal
            scores, idxs = index.search(q_vec.reshape(1, -1), k_this)
            scores = scores[0]
            idxs = idxs[0]
            for rank, (i, s) in enumerate(zip(idxs, scores), start=1):
                if i < 0:
                    continue
                id_ = df.index[i]
                if s > sem_scores.get(id_, -1):
                    sem_scores[id_] = float(s)
                    sem_ranks[id_] = rank

    lex_scores: Dict[str, float] = {}
    lex_ranks: Dict[str, int] = {}
    if fts_q:
        for conn in _sqlite_conns:
            cur = conn.cursor()
            tables = _lex_table_map.get(id(conn), ("items",))
            rows: List[Tuple[str, float]] = []
            for table in tables:
                try:
                    rows = _lex_fetch(cur, table, fts_q, k_lex, unlimited)
                except sqlite3.OperationalError:
                    rows = []
                    continue
                if rows:
                    break
            for rank, (id_, bm25) in enumerate(rows, start=1):
                score = -float(bm25)
                if id_ not in lex_scores or score > lex_scores[id_]:
                    lex_scores[id_] = score
                    lex_ranks[id_] = rank

    ids = list({*sem_scores.keys(), *lex_scores.keys()})
    rrf_scores = []
    mix_scores = []
    for id_ in ids:
        s_sem = sem_scores.get(id_, 0.0)
        s_lex = lex_scores.get(id_, 0.0)
        r_sem = sem_ranks.get(id_, k_sem + 1)
        r_lex = lex_ranks.get(id_, k_lex + 1)
        rrf = 1 / (60 + r_sem) + 1 / (60 + r_lex)
        mix = 0.6 * s_sem + 0.4 * s_lex
        rrf_scores.append(rrf)
        mix_scores.append(mix)

    hits: List[Hit] = []
    if ids:
        rrf_arr = np.asarray(rrf_scores)
        mix_arr = np.asarray(mix_scores)
        rrf_z = (rrf_arr - rrf_arr.mean()) / (rrf_arr.std() + 1e-12)
        mix_z = (mix_arr - mix_arr.mean()) / (mix_arr.std() + 1e-12)
        fused = (rrf_z + mix_z) / 2

        figure_query = bool(re.search(r"figure|diagram|plot|graph|curve", query, re.I))
        mathy_query = bool(re.search(r"mach|\bRe\b|\bCL\b|\bCD\b|\bEq\b|\u0394|\u2202", query, re.I))

        for id_, s_fused in zip(ids, fused):
            row = _id_to_row[id_]
            if figure_query and row.get("type") == "figure":
                s_fused += 0.05
            if mathy_query:
                text = row.get("text", "")
                if re.search(r"mach|\bRe\b|\bCL\b|\bCD\b|\bEq\b|\u0394|\u2202", text, re.I):
                    s_fused += 0.02
            if _flag("PACK_DEF_BIAS", True) and id_ in _definition_ids:
                s_fused += 0.1
            store_key = row.get("store_key")
            if store_key:
                s_fused += _store_biases.get(str(store_key), 0.0)
            hits.append(
                Hit(
                    id=id_,
                    score_sem=sem_scores.get(id_, 0.0),
                    rank_sem=sem_ranks.get(id_, k_sem + 1),
                    score_lex=lex_scores.get(id_, 0.0),
                    rank_lex=lex_ranks.get(id_, k_lex + 1),
                    score_fused=float(s_fused),
                )
            )

    hits.sort(key=lambda h: h.score_fused, reverse=True)
    _compute_hit_equal_scores(hits)

    if _flag("RETRIEVAL_PRF", True) and not _prf and len(hits) < 3:
        terms = _prf_terms(hits)
        if terms:
            _last_expansion_plan.append({"type": "prf", "terms": terms, "hit_count": 0})
            return _run_search(query + " " + " ".join(terms), k_sem, k_lex, _prf=True)
    return hits


def search_multi(query: str, k_sem: int = 30, k_lex: int = 30) -> List[Hit]:
    hits, _ = search(query, k_sem, k_lex)
    return hits


def search(query: str, k_sem: int = 30, k_lex: int = 30) -> Tuple[List[Hit], Dict[str, Any]]:
    """Run semantic+lexical search returning hits and diagnostics."""

    diag = _analyze_query(query)
    hits = _run_search(query, k_sem, k_lex)
    hit_count_sem = sum(1 for h in hits if h.score_sem > 0)
    hit_count_lex = sum(1 for h in hits if h.score_lex > 0)
    diag.update({"hit_count_sem": hit_count_sem, "hit_count_lex": hit_count_lex})
    if WIRE:
        print("[Indexer AI] diag=" + json.dumps({
            "hit_count_sem": diag.get("hit_count_sem", 0),
            "hit_count_lex": diag.get("hit_count_lex", 0),
            "missing_terms": diag.get("missing_terms", []),
            "expansion_candidates": {k: v[:3] for k, v in diag.get("expansion_candidates", {}).items()},
        }, ensure_ascii=False), flush=True)
    return hits, diag


# ---------------------------------------------------------


def batch_lookup_terms(
    terms: List[str], options: Dict[str, Any] | None = None
) -> Tuple[List[Dict[str, Any]], List[str], Dict[str, Any]]:
    """Lookup each term individually and return found/not-found splits."""

    options = options or {}
    doc_sets = options.get("doc_sets") or []
    token_budget = int(options.get("token_budget", 6000))
    k_sem = int(options.get("k_sem", 30))
    k_lex = int(options.get("k_lex", 30))

    loaded_indexes, skipped_indexes = _prepare_indexes(doc_sets)

    found_array: List[Dict[str, Any]] = []
    not_found: List[str] = []
    diag_all: Dict[str, Any] = {
        "per_term": {},
        "loaded_indexes": loaded_indexes,
        "skipped_indexes": skipped_indexes,
        "token_budget": token_budget,
        "k_sem": k_sem,
        "k_lex": k_lex,
    }
    marker_equal_map: Dict[str, float] = {}

    # Option to request all citations (no truncation). Can also be enabled via env RETRIEVAL_ALL_CITATIONS=1.
    want_all_citations = bool(
        (options or {}).get("all_citations", True)
        or (os.getenv("RETRIEVAL_ALL_CITATIONS", "").lower() not in {"0", "off", "false", "no"})
    )

    for term in terms:
        query = (term or "").strip()
        if not query:
            not_found.append(term)
            continue

        # Enable no-filter for the FAISS/FTS search phase only (to gather all hits),
        # but keep normal packing constraints to avoid ballooning snippet payloads.
        prev_no_filter = os.getenv("RETRIEVAL_NO_FILTER")
        if want_all_citations:
            os.environ["RETRIEVAL_NO_FILTER"] = "1"
        hits, diag = search(query, k_sem=k_sem, k_lex=k_lex)
        if want_all_citations:
            if prev_no_filter is None:
                os.environ.pop("RETRIEVAL_NO_FILTER", None)
            else:
                os.environ["RETRIEVAL_NO_FILTER"] = prev_no_filter

        # Build snippets as before (used by callers), but these may be large only if no-filter is on.
        ctx = pack_context(hits, token_budget=token_budget)
        snippets = _context_to_bundle_snippets(ctx)
        equations, glossary, assumptions, alias_counts = _summarize_snippets(snippets)

        term_lower = query.lower()
        expansion_raw = diag.get("expansion_candidates", {})
        if isinstance(expansion_raw, dict):
            expansions = [
                v for v in expansion_raw.get(term_lower, []) if isinstance(v, str)
            ]
        elif isinstance(expansion_raw, list):
            expansions = [v for v in expansion_raw if isinstance(v, str)]
        else:
            expansions = []

        term_presence_raw = {}
        diag_presence = diag.get("term_presence")
        if isinstance(diag_presence, dict):
            term_presence_raw = diag_presence.get(term_lower, {}) or {}

        alias_hits_seen: Set[str] = set()
        term_aliases = _term_to_aliases.get(term_lower, set())
        if term_aliases:
            for sn in snippets:
                text = sn.text or ""
                for tok in _symbol_tokens(text):
                    if _norm_alias(tok) in term_aliases:
                        alias_hits_seen.add(tok)
            for alias_norm in term_aliases:
                alias_hits_seen.update(_alias_strings_from_occurrences(alias_norm))

        missing_terms = []
        diag_missing = diag.get("missing_terms")
        if isinstance(diag_missing, list):
            missing_terms = [m for m in diag_missing if isinstance(m, str)]

        alias_hits_list = sorted(alias_hits_seen)
        diag_entry = {
            "hit_count_sem": int(diag.get("hit_count_sem", 0) or 0),
            "hit_count_lex": int(diag.get("hit_count_lex", 0) or 0),
            "missing_terms": missing_terms,
            "expansion_candidates": expansions,
            "term_presence": term_presence_raw,
            "snippets_returned": len(snippets),
            "alias_hits": alias_hits_list,
        }

        should_accept = bool(snippets) and _has_explicit_evidence(
            snippets, query, diag_entry
        )

        if want_all_citations and not should_accept and hits:
            fallback_snippets: List[BundleSnippet] = []
            seen_ids: Set[str] = set()
            fallback_cap_raw = os.getenv("RETRIEVAL_ALL_CITATIONS_SNIPPETS", "50")
            try:
                fallback_cap = max(1, int(fallback_cap_raw))
            except (TypeError, ValueError):
                fallback_cap = 50
            try:
                label_for_fallback = get_citation_label()
            except Exception:
                label_for_fallback = "Textbook"
            for h in hits:
                if h.id in seen_ids:
                    continue
                row = _id_to_row.get(h.id) if _id_to_row else None
                if row is None:
                    continue
                text = row.get("text") or ""
                if not text.strip():
                    continue
                typ0 = row.get("type", "body")
                typ = "body" if typ0 == "ocr" else typ0
                page = int(row.get("page", 0))
                section_path = row.get("section_path")
                source_path = row.get("source_path") or ""
                doc_title = row.get("doc_title")
                doc_short = (
                    row.get("doc_short")
                    or doc_title
                    or (Path(source_path).stem if source_path else "doc")
                    or "doc"
                )
                marker = f"[{label_for_fallback}, p. {page if page > 0 else '?'}]"
                fallback_snippets.append(
                    BundleSnippet(
                        id=h.id,
                        type=typ,
                        page=page,
                        section_path=section_path,
                        text=text,
                        figure_id=row.get("figure_id"),
                        why="all-citations",
                        source_path=source_path,
                        doc_title=doc_title,
                        doc_short=doc_short,
                        citation_marker=marker,
                        final_score={
                            "semantic": float(h.score_sem),
                            "lexical": float(h.score_lex),
                            "fused": float(h.score_fused),
                            "equal": float(h.score_equal) if h.score_equal is not None else None,
                        },
                    )
                )
                if h.score_equal is not None:
                    marker_equal_map.setdefault(marker, float(h.score_equal))
                seen_ids.add(h.id)
                if len(fallback_snippets) >= fallback_cap:
                    break
            if fallback_snippets:
                _compute_equal_scores(fallback_snippets)
                for sn_fb in fallback_snippets:
                    marker_fb = getattr(sn_fb, "citation_marker", None)
                    eq_fb = (sn_fb.final_score or {}).get("equal") if sn_fb.final_score else None
                    if marker_fb and eq_fb is not None:
                        marker_equal_map[marker_fb] = float(eq_fb)
                fallback_snippets.sort(
                    key=lambda sn: (sn.final_score or {}).get("equal", float("-inf")),
                    reverse=True,
                )
                fallback_snippets = fallback_snippets[:20]
                snippets = fallback_snippets
                equations, glossary, assumptions, alias_counts = _summarize_snippets(
                    snippets
                )
                diag_entry["snippets_returned"] = len(snippets)
                should_accept = True

        term_diag = diag_entry
        diag_all["per_term"][term] = term_diag

        if not should_accept:
            not_found.append(term)
            continue

        if want_all_citations:
            try:
                label_for_hits = get_citation_label()
            except Exception:
                label_for_hits = "Textbook"

        # Default: derive citations from the returned snippets.
        citation_markers: List[str] = []
        seen_citation_ids: Set[str] = set()
        seen_citation_markers: Set[str] = set()

        # If requested, collect citation markers from all hits (full list), not only packed snippets.
        if want_all_citations:
            for h in hits:
                try:
                    row = _id_to_row.get(h.id) if _id_to_row else None
                    page = int(row.get("page", 0)) if row is not None else 0
                except Exception:
                    page = 0
                marker = f"[{label_for_hits}, p. {page if page > 0 else '?'}]"
                if h.id not in seen_citation_ids:
                    seen_citation_ids.add(h.id)
                    seen_citation_markers.add(marker)
                    citation_markers.append(marker)
                if h.score_equal is not None:
                    marker_equal_map.setdefault(marker, float(h.score_equal))

        # Also include markers from the packed snippets (kept for compatibility and dedupe).
        for sn in snippets:
            marker = getattr(sn, "citation_marker", None)
            sn_id = getattr(sn, "id", None)
            if marker and (sn_id not in seen_citation_ids):
                if sn_id:
                    seen_citation_ids.add(sn_id)
                if marker not in seen_citation_markers:
                    seen_citation_markers.add(marker)
                citation_markers.append(marker)
            eq_sn = (sn.final_score or {}).get("equal") if sn.final_score else None
            if marker and eq_sn is not None:
                marker_equal_map.setdefault(marker, float(eq_sn))

        found_array.append(
            {
                "term": term,
                "snippets": snippets,
                "equations": equations,
                "assumptions": assumptions,
                "glossary": glossary,
                "aliases_used": alias_counts,
                "citations": citation_markers,
                "marker_equal_map": marker_equal_map.copy(),
            }
        )
    diag_all["marker_equal_map"] = marker_equal_map
    return found_array, not_found, diag_all


# ---------------------------------------------------------


def pack_context(hits: List[Hit], token_budget: int = 6000) -> ContextPack:
    """Select diverse snippets under a token budget.

    Parameters
    ----------
    hits: List[Hit]
        Ranked hits from :func:`search`.
    token_budget: int, default ``6000``
        Maximum tokens allowed for the packed snippets. About 15% headroom is
        kept for the calling prompt.

    Returns
    -------
    ContextPack
        Snippets respecting the budget along with usage stats. ``stats`` will
        include ``token_budget`` and ``truncated`` to show whether the budget
        forced truncation.
    """
    _require_loaded()
    unlimited = _flag("RETRIEVAL_NO_FILTER", False)
    enc = tiktoken.get_encoding("cl100k_base")
    limit = float("inf") if unlimited else int(token_budget * 0.85)
    base_quotas = {"body": 6, "figure": 4, "heading": 2}
    quotas = {k: float("inf") for k in base_quotas} if unlimited else base_quotas
    counts = {"body": 0, "figure": 0, "heading": 0}
    used_ids: set[str] = set()
    used_locs: set[Tuple[int, str]] = set()
    snippets: List[ContextSnippet] = []
    total_tokens = 0
    hit_by_id: Dict[str, Hit] = {h.id: h for h in hits}

    def section_str(sec):
        if isinstance(sec, list):
            return " > ".join(sec)
        return str(sec)

    def add_item(
        item_id: str,
        why: str,
        *,
        force: bool = False,
        origin_id: Optional[str] = None,
    ) -> bool:
        nonlocal total_tokens
        if item_id in used_ids or item_id not in _items_df.index:
            return False
        row = _id_to_row[item_id]
        typ0 = row.get("type", "body")
        typ = "body" if typ0 == "ocr" else typ0
        is_def = item_id in _definition_ids if _flag("PACK_DEF_BIAS", True) else False
        quota = quotas.get(typ, float("inf"))
        if counts.get(typ, 0) >= quota and not is_def and not force:
            return False
        sec = section_str(row.get("section_path", ""))
        loc_key = (int(row.get("page", 0)), sec)
        if not unlimited and loc_key in used_locs:
            return False
        text = row.get("text") or ""
        toks = len(enc.encode(text))
        if total_tokens + toks > limit:
            return False
        source_path = row.get("source_path") or ""
        doc_title = row.get("doc_title")
        doc_short = (
            row.get("doc_short")
            or doc_title
            or Path(source_path).stem
            or "doc"
        )
        origin_hit_id = origin_id or item_id
        hit = hit_by_id.get(origin_hit_id)
        score_payload: Dict[str, float] | None = None
        if hit is not None:
            if origin_hit_id == item_id or why != "overflow-neighbor":
                score_payload = {
                    "semantic": float(hit.score_sem),
                    "lexical": float(hit.score_lex),
                    "fused": float(hit.score_fused),
                }
                if hit.score_equal is not None:
                    score_payload["equal"] = float(hit.score_equal)
            else:
                score_payload = {
                    "semantic": 0.0,
                    "lexical": 0.0,
                    "fused": 0.0,
                }
        snippet = ContextSnippet(
            id=item_id,
            type=typ,
            page=int(row.get("page", 0)),
            section_path=sec,
            text=text,
            figure_id=row.get("figure_id"),
            why="definition" if is_def and why == "hit" else why,
            source_path=source_path,
            doc_title=doc_title,
            doc_short=doc_short,
            final_score=score_payload,
            origin_id=origin_hit_id,
        )
        snippets.append(snippet)
        used_ids.add(item_id)
        used_locs.add(loc_key)
        counts[typ] = counts.get(typ, 0) + 1
        total_tokens += toks

        if typ == "figure":
            # attach nearest body neighbor
            neighs = row.get("neighbors") or []
            for nid in neighs:
                nrow = _id_to_row.get(nid)
                if nrow is not None and nrow.get("type") == "body":
                    add_item(nid, "figure-body", force=force, origin_id=origin_hit_id)
                    break
            # attach most recent heading from parents
            parents = row.get("parents") or []
            for pid in reversed(parents):
                prow = _id_to_row.get(pid)
                if prow is not None and prow.get("type") == "heading":
                    add_item(pid, "figure-heading", force=force, origin_id=origin_hit_id)
                    break
        return True

    for h in hits:
        if total_tokens >= limit:
            break
        if not add_item(h.id, "hit", origin_id=h.id):
            continue
        row = _id_to_row[h.id]
        neighs = row.get("neighbors") or []
        neighbor_ids = neighs if unlimited else neighs[:2]
        for nid in neighbor_ids:
            if total_tokens >= limit:
                break
            add_item(nid, "neighbor", origin_id=h.id)

    # If quotas prevented adding useful snippets but we still have budget
    if not unlimited and total_tokens < limit:
        for h in hits:
            if total_tokens >= limit:
                break
            add_item(h.id, "overflow", force=True, origin_id=h.id)
            if total_tokens >= limit:
                break
            row = _id_to_row.get(h.id)
            if row is None:
                continue
            for nid in row.get("neighbors") or []:
                if total_tokens >= limit:
                    break
                add_item(nid, "overflow-neighbor", force=True, origin_id=h.id)

    # Drop overflow neighbors entirely before any further scoring
    snippets = [sn for sn in snippets if sn.why != "overflow-neighbor"]

    strong_lex_hits: Set[str] = {hid for hid, hit in hit_by_id.items() if getattr(hit, "score_lex", 0.0) > 0}
    keep_reasons = {"neighbor", "figure-body", "figure-heading"}
    filtered_snippets: List[ContextSnippet] = []
    for sn in snippets:
        fs = sn.final_score or {}
        lex_val = float(fs.get("lexical") or 0.0)
        if lex_val > 0:
            filtered_snippets.append(sn)
            continue
        if sn.origin_id and sn.origin_id in strong_lex_hits and sn.why in keep_reasons:
            filtered_snippets.append(sn)
    if filtered_snippets:
        snippets = filtered_snippets

    _compute_equal_scores(snippets)

    scored_snippets = [sn for sn in snippets if (sn.final_score or {}).get("equal") is not None]
    positive_snippets = [
        sn for sn in scored_snippets if (sn.final_score or {}).get("equal", -1e9) > 0
    ]
    if positive_snippets:
        snippets = positive_snippets
    elif scored_snippets:
        snippets = scored_snippets

    snippets.sort(
        key=lambda sn: (sn.final_score or {}).get("equal", float("-inf")), reverse=True
    )
    max_pages = 20
    if len(snippets) > max_pages:
        snippets = snippets[:max_pages]

    stats = {
        "tokens": total_tokens,
        "token_budget": token_budget,
        "truncated": (not unlimited) and total_tokens >= limit,
    }
    return ContextPack(snippets=snippets, used_ids=[s.id for s in snippets], stats=stats)


# ---------------------------------------------------------


def answer(question: str, ctx: ContextPack) -> Answer:
    _require_loaded()
    client = _get_client()
    model = _meta.get("answer_model", "gpt-4o-mini")
    system_prompt = "Answer only from the provided context. If insufficient, say exactly: 'Not found in the approved materials.' Cite inline."

    parts = []
    for i, sn in enumerate(ctx.snippets, start=1):
        meta = f"(type={sn.type}, page={sn.page}, §{sn.section_path}"
        if sn.figure_id:
            meta += f", fig {sn.figure_id}"
        meta += ")"
        parts.append(f"[S{i}] {meta}\n{sn.text}")
    context_block = "\n\n".join(parts)

    user = f"Question: {question}\n\nContext:\n{context_block}\n\nUse [S#] for citations."

    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user}],
        temperature=0,
    )
    out = resp.choices[0].message.content.strip()

    # Only keep citations that are actually referenced in the model output
    marker_pattern = re.compile(r"\[S(\d+)\]")
    used_markers = {int(m.group(1)) for m in marker_pattern.finditer(out)}

    citations: List[Dict[str, Any]] = []
    for i, sn in enumerate(ctx.snippets, start=1):
        row = _id_to_row.get(sn.id) if _id_to_row else None
        info = build_citation_info(sn, row, _store_meta)
        label = info.get("label", "")
        if i in used_markers:
            citations.append({"id": sn.id, "marker": label or _canonical_marker(sn), "snippet": sn})
            replacement = label or ""
        else:
            replacement = ""
        out = out.replace(f"[S{i}]", replacement)

    _, structured = format_citations(citations, _id_to_row, _store_meta)
    proof = {"question": question, "used_ids": ctx.used_ids}
    return Answer(text=out, citations=citations, proof=proof, structured_citations=structured)


# ---------------------------------------------------------


def render_citations(ans: Answer) -> str:
    structured = getattr(ans, "structured_citations", []) or []
    labels = []
    seen: set[str] = set()
    for entry in structured:
        label = entry.get("label")
        if not label:
            continue
        if label in seen:
            continue
        seen.add(label)
        labels.append(label)
    if labels:
        return " ".join(labels)
    # Fallback to legacy markers if structured data missing
    seen.clear()
    legacy: List[str] = []
    for c in ans.citations:
        sn = c.get("snippet")
        m = _canonical_marker(sn) if sn else c.get("marker")
        if m not in seen:
            seen.add(m)
            legacy.append(m)
    return " ".join(legacy)


# ---------------------------------------------------------


def research(task: ParsedTask | str, options: Dict[str, Any] | None = None) -> ResearchBundle:
    """Run retrieval and return a structured ``ResearchBundle``."""

    options = options or {}
    doc_sets = options.get("doc_sets") or []
    token_budget = int(options.get("token_budget", 6000))
    k_sem = int(options.get("k_sem", 30))
    k_lex = int(options.get("k_lex", 30))

    question = task if isinstance(task, str) else task.problem_type
    asked_list: List[str] = []
    known_keys: List[str] = []
    constraint_list: List[str] = []
    if isinstance(task, ParsedTask):
        if isinstance(task.asked_outputs, list):
            asked_list = task.asked_outputs
        elif isinstance(task.asked_outputs, str) and task.asked_outputs.strip():
            asked_list = [task.asked_outputs]

        if isinstance(task.knowns, dict):
            known_keys = list(task.knowns.keys())

        if isinstance(task.constraints, list):
            constraint_list = task.constraints
        elif isinstance(task.constraints, str) and task.constraints.strip():
            constraint_list = [task.constraints]

        parts = asked_list + known_keys + constraint_list
        q = " ".join([p for p in parts if p])
        question = (q + f" {task.problem_type}").strip() if q else task.problem_type

    if WIRE:
        print(f"[Main AI -> Indexer AI] query: {question}", flush=True)

    loaded_indexes, skipped_indexes = _prepare_indexes(doc_sets)

    hits, diag = search(question, k_sem=k_sem, k_lex=k_lex)

    marker_equal_map: Dict[str, float] = {}
    try:
        label_for_hits = get_citation_label()
    except Exception:
        label_for_hits = "Textbook"
    for hit in hits:
        score_equal = getattr(hit, "score_equal", None)
        if score_equal is None:
            continue
        try:
            equal_val = float(score_equal)
        except Exception:
            continue
        row = _id_to_row.get(hit.id) if _id_to_row else None
        try:
            page = int(row.get("page", 0)) if row is not None else 0
        except Exception:
            page = 0
        marker = f"[{label_for_hits}, p. {page if page > 0 else '?'}]"
        existing = marker_equal_map.get(marker)
        if existing is None or equal_val > existing:
            marker_equal_map[marker] = equal_val
    if WIRE:
        sem = diag.get("hit_count_sem", 0)
        lex = diag.get("hit_count_lex", 0)
        missing = list(diag.get("missing_terms", []))
        print(
            f"[Indexer AI -> Main AI] hits_sem={sem} hits_lex={lex} missing={missing}",
            flush=True,
        )
    ctx = pack_context(hits, token_budget=token_budget)

    snippets = _context_to_bundle_snippets(ctx)
    for sn in snippets:
        marker = getattr(sn, "citation_marker", None)
        eq_val = (sn.final_score or {}).get("equal") if sn.final_score else None
        if marker and eq_val is not None:
            try:
                eq_float = float(eq_val)
            except Exception:
                continue
            existing = marker_equal_map.get(marker)
            if existing is None or eq_float > existing:
                marker_equal_map[marker] = eq_float
    equations, glossary, assumptions, alias_counts = _summarize_snippets(snippets)

    allowed_markers: List[str] = []
    if marker_equal_map:
        allowed_markers = [
            marker
            for marker, _ in sorted(
                marker_equal_map.items(), key=lambda item: item[1], reverse=True
            )
        ]
    else:
        allowed_seen: Set[str] = set()
        for sn in snippets:
            marker = getattr(sn, "citation_marker", None)
            if isinstance(marker, str):
                cleaned = marker.strip()
                if cleaned and cleaned not in allowed_seen:
                    allowed_seen.add(cleaned)
                    allowed_markers.append(cleaned)

    coverage_gaps: List[str] = []
    refinement: List[str] = []
    if isinstance(task, ParsedTask):
        alias_map = {
            "δ": "delta",
            "Δ": "delta",
            "θ": "theta",
            "τ": "tau",
            "τ_w": "tau_w",
            "α": "alpha",
            "β": "beta",
            "μ": "mu",
        }
        for sym in asked_list:
            variants = {sym}
            for k, v in alias_map.items():
                if k in sym:
                    variants.add(sym.replace(k, v))
            if not any(any(v in s.text for v in variants) for s in snippets):
                coverage_gaps.append(f"No snippet for {sym}")
                refinement.append(sym)

    meta = {
        "doc_sets": doc_sets,
        "loaded_indexes": loaded_indexes,
        "question": question,
        "problem_type": getattr(task, "problem_type", "unknown"),
        "asked_outputs_len": len(asked_list),
        "skipped_indexes": skipped_indexes,
        "model": _meta.get("model") if _meta else None,
        "dimensions": _meta.get("dimensions") if _meta else None,
        "k_sem": k_sem,
        "k_lex": k_lex,
        "token_budget": token_budget,
        "expansion_plan": list(_last_expansion_plan),
        "aliases_used": alias_counts,
        "symbol_index_stats": _symbol_index_stats,
        "term_presence": diag.get("term_presence", {}),
        "missing_terms": diag.get("missing_terms", []),
        "expansion_candidates": diag.get("expansion_candidates", {}),
        "hit_count_sem": diag.get("hit_count_sem", 0),
        "hit_count_lex": diag.get("hit_count_lex", 0),
        "allowed_markers": allowed_markers,
        "subject": get_subject_name(),
    }

    bundle = ResearchBundle(
        metadata=ResearchMetadata(**meta),
        snippets=snippets,
        equations=equations,
        glossary=glossary,
        assumptions=assumptions,
        coverage_gaps=coverage_gaps,
        refinement_queries=refinement,
        used_ids=ctx.used_ids,
        stats=ctx.stats,
        provenance={"source": "retriever"},
        allowed_markers=allowed_markers,
        not_found_terms=list(diag.get("missing_terms", [])) if isinstance(diag, dict) else [],
        attempted_terms=[question] if question else [],
        subject=get_subject_name(),
        marker_equal_map=marker_equal_map,
    )
    if WIRE:
        print(f"[Indexer AI -> Main AI] snippets={len(bundle.snippets)}", flush=True)
    return bundle


__all__ = [
    "Hit",
    "ContextSnippet",
    "ContextPack",
    "Answer",

    "load_assets",
    "load_assets_all",

    "search",
    "batch_lookup_terms",
    "pack_context",

    "answer",
    "render_citations",

    "research",
]






