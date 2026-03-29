"""BM25 + LLM two-stage retrieval — shared by mcp_server and web_ui."""

import json
import math
import re
import logging
from collections import Counter
from pathlib import Path

from settings import settings

RESULTS_DIR = settings.results_dir
MODEL       = settings.model

log = logging.getLogger('pageindex.retrieval')


class _BM25:
    def __init__(self, corpus):
        self.corpus_size = len(corpus)
        self.avgdl = sum(len(d) for d in corpus) / self.corpus_size if self.corpus_size else 1
        self.doc_freqs = []
        self.idf = {}
        self.doc_len = []
        for doc in corpus:
            self.doc_len.append(len(doc))
            freq = Counter(doc)
            self.doc_freqs.append(freq)
            for w in freq:
                self.idf[w] = self.idf.get(w, 0) + 1
        for w, f in self.idf.items():
            self.idf[w] = math.log(1 + (self.corpus_size - f + 0.5) / (f + 0.5))

    def get_scores(self, query):
        scores = [0.0] * self.corpus_size
        for q in query:
            q_idf = self.idf.get(q, 0)
            if not q_idf:
                continue
            for i, freq in enumerate(self.doc_freqs):
                f = freq.get(q, 0)
                if f:
                    scores[i] += q_idf * (f * 2.5) / (
                        f + 1.5 * (1 - 0.75 + 0.75 * self.doc_len[i] / self.avgdl)
                    )
        return scores


def _tokenize(text: str) -> list:
    import string
    text = text.lower()
    for p in string.punctuation + '，。！？；：、\'\'""（）【】《》\n\r\t':
        text = text.replace(p, ' ')
    tokens = []
    for token in text.split():
        if re.search(r'[\u4e00-\u9fff]', token):
            tokens.extend(list(token))
        else:
            tokens.append(token)
    return tokens


def _load_docs(results_dir: Path) -> list:
    docs = []
    for fpath in results_dir.glob('*.json'):
        try:
            with open(fpath, encoding='utf-8') as f:
                tree = json.load(f)
            texts = []

            def _extract(node):
                if isinstance(node, dict):
                    for key in ('title', 'summary', 'prefix_summary', 'text'):
                        if node.get(key):
                            texts.append(str(node[key]))
                    if 'nodes' in node:
                        _extract(node['nodes'])
                    if 'structure' in node:
                        _extract(node['structure'])
                elif isinstance(node, list):
                    for item in node:
                        _extract(item)

            _extract(tree)
            tokens = _tokenize(' '.join(texts))
            if tokens:
                docs.append({'filename': fpath.name, 'filepath': str(fpath), 'tree': tree, 'tokens': tokens})
        except Exception as e:
            log.debug(f'skip {fpath.name}: {e}')
    return docs


def _tree_summary(tree) -> str:
    def _s(node):
        if isinstance(node, dict):
            r = {}
            if 'node_id'   in node: r['id']       = node['node_id']
            if 'title'     in node: r['title']     = node['title']
            if 'summary'   in node: r['summary']   = node['summary']
            if node.get('nodes'):     r['nodes']     = _s(node['nodes'])
            if node.get('structure'): r['structure'] = _s(node['structure'])
            return r
        elif isinstance(node, list):
            return [_s(i) for i in node]
        return node
    return json.dumps(_s(tree), ensure_ascii=False)


def _result_filename_to_note_name(filename: str) -> str:
    """Convert result filename back to note stem.

    e.g. 'folder__sub__My_Note_structure.json' -> 'My_Note'
    """
    stem = filename.removesuffix('_structure.json')
    return stem.split('__')[-1]


def search_notes_impl(query: str, top_k: int = 5, model: str = None, lang: str = 'en') -> str:
    if model is None:
        model = MODEL

    if not RESULTS_DIR.exists():
        return 'Results directory does not exist. Please run batch indexing first.'

    docs = _load_docs(RESULTS_DIR)
    if not docs:
        return 'No indexed documents found.'

    # Stage 1: BM25
    q_tokens = _tokenize(query)
    bm25 = _BM25([d['tokens'] for d in docs])
    scores = bm25.get_scores(q_tokens)
    ranked = sorted(zip(docs, scores), key=lambda x: x[1], reverse=True)
    top_docs = [(d, s) for d, s in ranked if s > 0][:top_k]

    if not top_docs:
        return 'No documents matched the query.'

    # Stage 2: LLM tree search
    from pageindex.utils import ChatGPT_API, extract_json

    def get_node_map(tree):
        mapping = {}
        def _m(node):
            if isinstance(node, dict):
                if 'node_id' in node:
                    mapping[node['node_id']] = node
                if 'nodes'     in node: _m(node['nodes'])
                if 'structure' in node: _m(node['structure'])
            elif isinstance(node, list):
                for item in node: _m(item)
        _m(tree)
        return mapping

    def _search_doc(doc_score):
        doc, score = doc_score
        summary = _tree_summary(doc['tree'])
        prompt = (
            f'You are a smart search assistant. Given the following document tree structure '
            f'(with node IDs, titles, and summaries), determine which nodes are most relevant '
            f'to the user\'s query.\n\n'
            f'User Query: "{query}"\n\n'
            f'Document Tree:\n{summary}\n\n'
            f'Respond with a JSON array containing the node IDs that contain information '
            f'relevant to answering the query. For example: ["0001", "0005"]. '
            f'If none are relevant, return []. ONLY output valid JSON.'
        )
        resp = ChatGPT_API(model=model, prompt=prompt)
        try:
            node_ids = extract_json(resp)
            if not isinstance(node_ids, list):
                node_ids = []
        except Exception:
            node_ids = []

        if not node_ids:
            return None
        node_map = get_node_map(doc['tree'])
        ctx = f'--- From: {doc["filename"]} ---\n'
        for nid in node_ids:
            if nid in node_map:
                n = node_map[nid]
                title   = n.get('title', 'Untitled')
                content = n.get('text') or n.get('summary', '')
                ctx += f'## {title}\n{content}\n\n'
        return ctx

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(len(top_docs), 5)) as ex:
        results = list(ex.map(_search_doc, top_docs))
    contexts = [r for r in results if r]

    if not contexts:
        return 'Found candidate documents but no relevant nodes matched the query.'

    context = '\n'.join(contexts)
    lang_instruction = '请用中文回答。' if lang == 'zh' else 'Please answer in English.'
    final_prompt = (
        f'You are a helpful assistant. Use the following excerpted information from '
        f'my personal notes to answer the question. {lang_instruction}\n'
        f'If the information provided is not sufficient, state that clearly.\n\n'
        f'Question: {query}\n\n'
        f'Reference Material:\n{context}\n\nAnswer:'
    )
    return ChatGPT_API(model=model, prompt=final_prompt)


def find_notes_impl(query: str, top_k: int = 5) -> list[str]:
    """BM25-only retrieval; returns a list of note name stems."""
    if not RESULTS_DIR.exists():
        return []

    docs = _load_docs(RESULTS_DIR)
    if not docs:
        return []

    q_tokens = _tokenize(query)
    bm25 = _BM25([d['tokens'] for d in docs])
    scores = bm25.get_scores(q_tokens)
    ranked = sorted(zip(docs, scores), key=lambda x: x[1], reverse=True)
    top_docs = [(d, s) for d, s in ranked if s > 0][:top_k]

    return [_result_filename_to_note_name(d['filename']) for d, _ in top_docs]
