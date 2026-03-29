import os
import sys
import json
import re
import math
from collections import Counter
from pathlib import Path
import time

sys.path.insert(0, str(Path(__file__).parent.resolve()))
from pageindex.utils import ChatGPT_API, extract_json
from settings import settings

class SimpleBM25:
    """A minimal BM25 implementation without external dependencies like rank_bm25."""
    def __init__(self, corpus):
        self.corpus_size = len(corpus)
        self.avgdl = sum(len(doc) for doc in corpus) / self.corpus_size if self.corpus_size else 1
        self.doc_freqs = []
        self.idf = {}
        self.doc_len = []

        for document in corpus:
            self.doc_len.append(len(document))
            frequencies = Counter(document)
            self.doc_freqs.append(frequencies)
            for word in frequencies:
                if word not in self.idf:
                    self.idf[word] = 0
                self.idf[word] += 1

        for word, freq in self.idf.items():
            self.idf[word] = math.log(1 + (self.corpus_size - freq + 0.5) / (freq + 0.5))

    def get_scores(self, query):
        scores = [0] * self.corpus_size
        for q in query:
            q_idf = self.idf.get(q, 0)
            if q_idf == 0:
                continue
            for i, freqs in enumerate(self.doc_freqs):
                f = freqs.get(q, 0)
                if f > 0:
                    score = q_idf * (f * 2.5) / (f + 1.5 * (1 - 0.75 + 0.75 * self.doc_len[i] / self.avgdl))
                    scores[i] += score
        return scores

def tokenize(text):
    """Simple tokenization for Chinese/English without external NLP libraries."""
    import string
    text = text.lower()
    # Replace punctuation with spaces
    for p in string.punctuation + "，。！？；：、‘’“”（）【】《》\n\r\t":
        text = text.replace(p, ' ')
    # Split by spaces and also split Chinese characters
    tokens = []
    for token in text.split():
        if re.search(r'[\u4e00-\u9fff]', token):
            # Contains Chinese characters, treat each character as a token for simplicity
            tokens.extend(list(token))
        else:
            tokens.append(token)
    return tokens

def load_all_trees(results_dir):
    print(f"Loading document trees from {results_dir} ...")
    docs = []
    start_time = time.time()
    for filename in os.listdir(results_dir):
        if filename.endswith(".json"):
            filepath = os.path.join(results_dir, filename)
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    tree = json.load(f)
                    
                # Extract search text: document title and node summaries
                search_text = []
                def extract_text(node):
                    if isinstance(node, dict):
                        if 'title' in node and node['title']: search_text.append(str(node['title']))
                        if 'summary' in node and node['summary']: search_text.append(str(node['summary']))
                        if 'prefix_summary' in node and node['prefix_summary']: search_text.append(str(node['prefix_summary']))
                        if 'text' in node and node['text']: search_text.append(str(node['text']))
                        if 'nodes' in node: extract_text(node['nodes'])
                        if 'structure' in node: extract_text(node['structure'])
                    elif isinstance(node, list):
                        for item in node: extract_text(item)
                
                extract_text(tree)
                combined_text = " ".join(search_text)
                
                # We tokenize it
                tokens = tokenize(combined_text)
                if tokens:
                    docs.append({
                        "filename": filename,
                        "filepath": filepath,
                        "tree": tree,
                        "tokens": tokens
                    })
            except Exception as e:
                print(f"Error loading {filename}: {e}")
                
    print(f"Loaded {len(docs)} documents in {time.time() - start_time:.2f}s")
    return docs

def stage1_retrieve(query, docs, top_k=5):
    query_tokens = tokenize(query)
    corpus = [doc["tokens"] for doc in docs]
    bm25 = SimpleBM25(corpus)
    scores = bm25.get_scores(query_tokens)
    
    # Sort by score
    doc_scores = [(docs[i], scores[i]) for i in range(len(docs))]
    doc_scores.sort(key=lambda x: x[1], reverse=True)
    
    # Filter out 0 score documents
    top_docs = [item for item in doc_scores if item[1] > 0][:top_k]
    return top_docs

def create_tree_summary(tree):
    """Create a simplified tree structure to pass to LLM for Stage 2."""
    def _simplify(node):
        if isinstance(node, dict):
            simplified = {}
            if 'node_id' in node: simplified['id'] = node['node_id']
            if 'title' in node: simplified['title'] = node['title']
            if 'summary' in node: simplified['summary'] = node['summary']
            if 'nodes' in node and node['nodes']:
                simplified['nodes'] = _simplify(node['nodes'])
            if 'structure' in node and node['structure']:
                simplified['structure'] = _simplify(node['structure'])
            return simplified
        elif isinstance(node, list):
            return [_simplify(item) for item in node]
        return node
    return json.dumps(_simplify(tree), ensure_ascii=False)

def stage2_tree_search(query, selected_docs, model="Qwen3-32B"):
    print("\n--- Stage 2: Tree Search using LLM ---")
    
    def get_node_mapping(tree):
        mapping = {}
        def _map(node):
            if isinstance(node, dict):
                if 'node_id' in node:
                    mapping[node['node_id']] = node
                if 'nodes' in node: _map(node['nodes'])
                if 'structure' in node: _map(node['structure'])
            elif isinstance(node, list):
                for item in node: _map(item)
        _map(tree)
        return mapping

    final_contexts = []
    for doc, score in selected_docs:
        print(f"Analyzing tree for: {doc['filename']} (BM25 Score: {score:.2f})")
        simplified_tree = create_tree_summary(doc['tree'])
        
        prompt = f"""You are a smart search assistant. Given the following document tree structure (with node IDs, titles, and summaries), determine which nodes are most relevant to the user's query.

User Query: "{query}"

Document Tree:
{simplified_tree}

Respond with a JSON array containing the node IDs that contain information relevant to answering the query. For example: ["0001", "0005"]. If none are relevant, return []. ONLY output valid JSON.
"""     
        # Call LLM
        response = ChatGPT_API(model=model, prompt=prompt)
        try:
            relevant_node_ids = extract_json(response)
            if not isinstance(relevant_node_ids, list):
                relevant_node_ids = []
            
            if relevant_node_ids:
                print(f"  -> Relevant nodes found: {relevant_node_ids}")
                node_map = get_node_mapping(doc['tree'])
                doc_context = f"--- From document: {doc['filename']} ---\n"
                for nid in relevant_node_ids:
                    if nid in node_map and 'text' in node_map[nid]:
                        doc_context += f"## {node_map[nid].get('title', 'Untitled')}\n"
                        doc_context += f"{node_map[nid]['text']}\n\n"
                    elif nid in node_map and 'summary' in node_map[nid]:
                        # Fallback to summary if text is not available
                        doc_context += f"## {node_map[nid].get('title', 'Untitled')} (Summary)\n"
                        doc_context += f"{node_map[nid]['summary']}\n\n"
                final_contexts.append(doc_context)
            else:
                print("  -> No relevant nodes found in this document.")
        except Exception as e:
            print(f"  -> Error parsing LLM response/extracting JSON: {e}")
            
    return "\n".join(final_contexts)

def generate_final_answer(query, context, model="Qwen3-32B"):
    if not context.strip():
        return "Sorry, I couldn't find any relevant information in your notes to answer this question."
        
    print("\n--- Generating Final Answer ---")
    prompt = f"""You are a helpful assistant. Use the following excerpted information from my personal notes to answer the question.
If the information provided is not sufficient to answer the question, state that clearly.

Question: {query}

Reference Material:
{context}

Answer:"""
    
    return ChatGPT_API(model=model, prompt=prompt)

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Two-stage local search for PageIndex results.")
    parser.add_argument("query", type=str, help="The search query.")
    parser.add_argument("--top_k", type=int, default=5, help="Number of documents to retrieve in Stage 1.")
    parser.add_argument("--model", type=str, default=settings.model, help="LLM to use for Stage 2.")
    parser.add_argument("--results_dir", type=str, default=str(settings.results_dir), help="Directory containing _structure.json files.")
    
    args = parser.parse_args()
    
    if not os.path.isdir(args.results_dir):
        print(f"Error: Directory {args.results_dir} does not exist.")
        return

    # Stage 1
    docs = load_all_trees(args.results_dir)
    print(f"\nSearching for: '{args.query}'")
    top_docs = stage1_retrieve(args.query, docs, top_k=args.top_k)
    
    if not top_docs:
        print("No documents matched your query in Stage 1.")
        return
        
    print(f"\nTop {len(top_docs)} candidate documents found:")
    for doc, score in top_docs:
        print(f"- {doc['filename']} (Score: {score:.2f})")
        
    # Stage 2
    context = stage2_tree_search(args.query, top_docs, model=args.model)
    
    # Final Answer
    answer = generate_final_answer(args.query, context, model=args.model)
    
    print("\n================== FINAL ANSWER ==================")
    print(answer)
    print("==================================================")

if __name__ == "__main__":
    main()
