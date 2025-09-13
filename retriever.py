"""Retriever module for hybrid semantic+lexical search with context packing and answer generation.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple, Any

import faiss
import numpy as np
import pandas as pd
import tiktoken
from openai import OpenAI
from .contracts import ResearchBundle, BundleSnippet, ParsedTask

# ----------------------- Data classes -----------------------


@dataclass
class Hit:
    id: str
    score_sem: float
    rank_sem: int
    score_lex: float
    rank_lex: int
    score_fused: float


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


# ----------------------- Globals -----------------------

_faiss_list: List[faiss.Index] = []
_sqlite_conns: List[sqlite3.Connection] = []
_items_dfs: List[pd.DataFrame] = []
_items_df: pd.DataFrame | None = None
_id_to_row: Dict[str, pd.Series] | None = None
_meta: Dict[str, object] | None = None
_meta_titles: Dict[str, str] | None = None
_client: OpenAI | None = None


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


def _norm_key(path: str) -> str:
    if not path:
        return ""
    return os.path.normcase(path.replace("/", os.sep).replace("\\", os.sep))


def _canonical_marker(sn) -> str:
    sp = getattr(sn, "source_path", "")
    doc_short = getattr(sn, "doc_short", None)
    if not doc_short:
        sp_norm = _norm_key(sp)
        sp_base = _norm_key(os.path.basename(sp))
        title = None
        if _meta_titles:
            title = _meta_titles.get(sp_norm) or _meta_titles.get(sp_base)
        if not title:
            title = getattr(sn, "doc_title", None)
        if not title and sp:
            title = Path(sp).stem
        if not title and _meta:
            source_pdf = _meta.get("source_pdf", "")
            title = Path(source_pdf).stem if source_pdf else None
        doc_short = title or "doc"
    section_label = (
        getattr(sn, "section_path", None)
        or getattr(sn, "heading", None)
        or "—"
    )
    page = getattr(sn, "page", None)
    page = page if isinstance(page, int) and page > 0 else "?"
    marker = f"[§{doc_short} • {section_label}, p.{page}"
    fig_id = getattr(sn, "figure_id", None)
    if fig_id:
        marker += f"; Fig {fig_id}"
    marker += "]"
    return marker


# ----------------------- Public API -----------------------


def _load_one(root: Path) -> Tuple[faiss.Index, pd.DataFrame, sqlite3.Connection, dict, Dict[str, str]]:
    """Load FAISS, items DataFrame, SQLite, and meta from an index directory."""
    faiss_path = root / "faiss.index"
    items_path = root / "items.jsonl"
    sqlite_path = root / "sqlite.db"
    meta_path = root / "meta.json"

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

    conn = sqlite3.connect(str(sqlite_path))

    return index, df, conn, meta, meta_titles


def load_assets(root: Path) -> Tuple[faiss.Index, pd.DataFrame, sqlite3.Connection, dict]:
    index, df, conn, meta, titles = _load_one(root)

    global _faiss_list, _sqlite_conns, _items_dfs, _items_df, _id_to_row, _meta, _meta_titles
    _faiss_list = [index]
    _sqlite_conns = [conn]
    _items_dfs = [df]
    _items_df = df
    _id_to_row = {idx: df.loc[idx] for idx in df.index}
    _meta = meta
    _meta_titles = titles

    return index, df, conn, meta


def load_assets_all(
    roots: List[Path],
) -> Tuple[List[Tuple[faiss.Index, pd.DataFrame, sqlite3.Connection, dict]], List[Dict[str, str]]]:
    """Load assets from multiple index directories."""
    faiss_list: List[faiss.Index] = []
    conns: List[sqlite3.Connection] = []
    dfs: List[pd.DataFrame] = []
    metas: List[dict] = []
    title_map: Dict[str, str] = {}
    skipped: List[Dict[str, str]] = []
    meta_ref: dict | None = None
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

    global _faiss_list, _sqlite_conns, _items_dfs, _items_df, _id_to_row, _meta, _meta_titles
    _faiss_list = faiss_list
    _sqlite_conns = conns
    _items_dfs = dfs
    _items_df = merged_df
    _id_to_row = id_map
    _meta = meta_ref
    _meta_titles = title_map

    return list(zip(faiss_list, dfs, conns, metas)), skipped


# ---------------------------------------------------------


def search_multi(query: str, k_sem: int = 30, k_lex: int = 30) -> List[Hit]:
    _require_loaded()
    client = _get_client()

    model = _meta["model"]
    dim = int(_meta["dimensions"])
    q_emb = client.embeddings.create(model=model, input=[query], dimensions=dim).data[0].embedding
    q_vec = np.asarray(q_emb, dtype=np.float32)
    q_vec /= max(np.linalg.norm(q_vec), 1e-12)

    sem_scores: Dict[str, float] = {}
    sem_ranks: Dict[str, int] = {}
    for df, index in zip(_items_dfs, _faiss_list):
        scores, idxs = index.search(q_vec.reshape(1, -1), k_sem)
        scores = scores[0]
        idxs = idxs[0]
        for rank, (i, s) in enumerate(zip(idxs, scores), start=1):
            if i < 0:
                continue
            id_ = df.index[i]
            sem_scores[id_] = float(s)
            sem_ranks[id_] = rank

    fts_q = _fts_safe_query(query)
    lex_scores: Dict[str, float] = {}
    lex_ranks: Dict[str, int] = {}
    if fts_q:
        for conn in _sqlite_conns:
            cur = conn.cursor()
            try:
                cur.execute(
                    "SELECT id, bm25(fts) as score FROM fts WHERE fts MATCH ? ORDER BY score LIMIT ?",
                    (fts_q, k_lex),
                )
                rows = cur.fetchall()
            except sqlite3.OperationalError:
                rows = []
            for rank, (id_, bm25) in enumerate(rows, start=1):
                score = 1.0 / (1.0 + float(bm25))
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

    rrf_arr = np.asarray(rrf_scores)
    mix_arr = np.asarray(mix_scores)
    rrf_z = (rrf_arr - rrf_arr.mean()) / (rrf_arr.std() + 1e-12)
    mix_z = (mix_arr - mix_arr.mean()) / (mix_arr.std() + 1e-12)
    fused = (rrf_z + mix_z) / 2

    figure_query = bool(re.search(r"figure|diagram|plot|graph|curve", query, re.I))
    mathy_query = bool(re.search(r"mach|\bRe\b|\bCL\b|\bCD\b|\bEq\b|Δ|∂", query, re.I))

    hits: List[Hit] = []
    for id_, s_fused in zip(ids, fused):
        row = _id_to_row[id_]
        if figure_query and row.get("type") == "figure":
            s_fused += 0.05
        if mathy_query:
            text = row.get("text", "")
            if re.search(r"mach|\bRe\b|\bCL\b|\bCD\b|\bEq\b|Δ|∂", text, re.I):
                s_fused += 0.02
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
    return hits


def search(query: str, k_sem: int = 30, k_lex: int = 30) -> List[Hit]:
    if len(_faiss_list) > 1:
        return search_multi(query, k_sem=k_sem, k_lex=k_lex)

    _require_loaded()
    client = _get_client()

    model = _meta["model"]
    dim = int(_meta["dimensions"])
    q_emb = client.embeddings.create(model=model, input=[query], dimensions=dim).data[0].embedding
    q_vec = np.asarray(q_emb, dtype=np.float32)
    q_vec /= max(np.linalg.norm(q_vec), 1e-12)

    scores, idxs = _faiss_list[0].search(q_vec.reshape(1, -1), k_sem)
    scores = scores[0]
    idxs = idxs[0]
    sem_scores: Dict[str, float] = {}
    sem_ranks: Dict[str, int] = {}
    for rank, (i, s) in enumerate(zip(idxs, scores), start=1):
        if i < 0:
            continue
        id_ = _items_df.index[i]
        sem_scores[id_] = float(s)
        sem_ranks[id_] = rank

    fts_q = _fts_safe_query(query)
    rows = []
    if fts_q:
        cur = _sqlite_conns[0].cursor()
        try:
            cur.execute(
                "SELECT id, bm25(fts) as score FROM fts WHERE fts MATCH ? ORDER BY score LIMIT ?",
                (fts_q, k_lex),
            )
            rows = cur.fetchall()
        except sqlite3.OperationalError:
            rows = []
    lex_scores: Dict[str, float] = {}
    lex_ranks: Dict[str, int] = {}
    for rank, (id_, bm25) in enumerate(rows, start=1):
        lex_scores[id_] = 1.0 / (1.0 + float(bm25))
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

    rrf_arr = np.asarray(rrf_scores)
    mix_arr = np.asarray(mix_scores)
    rrf_z = (rrf_arr - rrf_arr.mean()) / (rrf_arr.std() + 1e-12)
    mix_z = (mix_arr - mix_arr.mean()) / (mix_arr.std() + 1e-12)
    fused = (rrf_z + mix_z) / 2

    figure_query = bool(re.search(r"figure|diagram|plot|graph|curve", query, re.I))
    mathy_query = bool(re.search(r"mach|\bRe\b|\bCL\b|\bCD\b|\bEq\b|Δ|∂", query, re.I))

    hits: List[Hit] = []
    for id_, s_fused in zip(ids, fused):
        row = _id_to_row[id_]
        if figure_query and row.get("type") == "figure":
            s_fused += 0.05
        if mathy_query:
            text = row.get("text", "")
            if re.search(r"mach|\bRe\b|\bCL\b|\bCD\b|\bEq\b|Δ|∂", text, re.I):
                s_fused += 0.02
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
    return hits


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
    enc = tiktoken.get_encoding("cl100k_base")
    limit = int(token_budget * 0.85)
    quotas = {"body": 6, "figure": 4, "heading": 2}
    counts = {"body": 0, "figure": 0, "heading": 0}
    used_ids: set[str] = set()
    used_locs: set[Tuple[int, str]] = set()
    snippets: List[ContextSnippet] = []
    total_tokens = 0

    def section_str(sec):
        if isinstance(sec, list):
            return " > ".join(sec)
        return str(sec)

    def add_item(item_id: str, why: str) -> bool:
        nonlocal total_tokens
        if item_id in used_ids or item_id not in _items_df.index:
            return False
        row = _id_to_row[item_id]
        typ0 = row.get("type", "body")
        typ = "body" if typ0 == "ocr" else typ0
        if counts.get(typ, 0) >= quotas.get(typ, 0):
            return False
        sec = section_str(row.get("section_path", ""))
        loc_key = (int(row.get("page", 0)), sec)
        if loc_key in used_locs:
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
        snippet = ContextSnippet(
            id=item_id,
            type=typ,
            page=int(row.get("page", 0)),
            section_path=sec,
            text=text,
            figure_id=row.get("figure_id"),
            why=why,
            source_path=source_path,
            doc_title=doc_title,
            doc_short=doc_short,
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
                    add_item(nid, "figure-body")
                    break
            # attach most recent heading from parents
            parents = row.get("parents") or []
            for pid in reversed(parents):
                prow = _id_to_row.get(pid)
                if prow is not None and prow.get("type") == "heading":
                    add_item(pid, "figure-heading")
                    break
        return True

    for h in hits:
        if total_tokens >= limit:
            break
        if not add_item(h.id, "hit"):
            continue
        row = _id_to_row[h.id]
        neighs = row.get("neighbors") or []
        for nid in neighs[:2]:
            if total_tokens >= limit:
                break
            add_item(nid, "neighbor")

    stats = {
        "tokens": total_tokens,
        "token_budget": token_budget,
        "truncated": total_tokens >= limit,
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
        marker = _canonical_marker(sn)
        if i in used_markers:
            citations.append({"id": sn.id, "marker": marker, "snippet": sn})
        # strip placeholder regardless of usage
        out = out.replace(f"[S{i}]", "")

    proof = {"question": question, "used_ids": ctx.used_ids}
    return Answer(text=out, citations=citations, proof=proof)


# ---------------------------------------------------------


def render_citations(ans: Answer) -> str:
    seen: set[str] = set()
    markers: List[str] = []
    for c in ans.citations:
        sn = c.get("snippet")
        m = _canonical_marker(sn) if sn else c.get("marker")
        if m not in seen:
            seen.add(m)
            markers.append(m)
    return " ".join(markers)


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

    skipped_indexes: List[Dict[str, str]] = []
    loaded_indexes: List[str] = []
    if doc_sets:
        paths = [Path(p) for p in doc_sets]
        try:
            if len(paths) > 1:
                _, skipped_indexes = load_assets_all(paths)
            else:
                load_assets(paths[0])
            skipped_set = {s["path"] for s in skipped_indexes}
            loaded_indexes = [str(p) for p in paths if str(p) not in skipped_set]
        except RuntimeError as exc:
            raise RuntimeError(str(exc))

    hits = search(question, k_sem=k_sem, k_lex=k_lex)
    ctx = pack_context(hits, token_budget=token_budget)

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
            )
        )

    eq_map: Dict[str, Dict[str, Any]] = {}
    glossary: List[Dict[str, Any]] = []
    assumptions: List[Dict[str, Any]] = []
    for sn in snippets:
        sym_set: set[str] = set()
        lines = sn.text.splitlines()
        for line in lines:
            if "=" in line or re.search(r"\(\d+-\d+\)", line):
                norm = re.sub(r"\s+", " ", line.strip().rstrip(".;,"))
                norm = re.sub(r"\s*\(\d+-\d+\)\s*$", "", norm)  # drop trailing eq numbers like (2-2)
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

    equations: List[Dict[str, Any]] = []
    for v in eq_map.values():
        equations.append(
            {
                "eq_text": v["eq_text"],
                "symbol_set": list(sorted(v["symbol_set"])),
                "source_snippet_ids": list(sorted(v["source_snippet_ids"])),
            }
        )

    coverage_gaps: List[str] = []
    refinement: List[str] = []
    if isinstance(task, ParsedTask):
        alias_map = {
            "δ": "delta",
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
    }

    bundle = ResearchBundle(
        metadata=meta,
        snippets=snippets,
        equations=equations,
        glossary=glossary,
        assumptions=assumptions,
        coverage_gaps=coverage_gaps,
        refinement_queries=refinement,
        used_ids=ctx.used_ids,
        stats=ctx.stats,
        provenance={"source": "retriever"},
    )
    return bundle


__all__ = [
    "Hit",
    "ContextSnippet",
    "ContextPack",
    "Answer",

    "load_assets",
    "load_assets_all",

    "search",
    "pack_context",

    "answer",
    "render_citations",

    "research",
]
